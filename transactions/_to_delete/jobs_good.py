import re
import logging
from django.db import transaction
from django.db.models import Sum, F
from django.utils.timezone import now
from decimal import Decimal
from customers.models import Customer
from loans.models import LoanHistory
from sms.models import SMSLog  
from .models import (
    MpesaNotification,
    PostedMpesaNotification,
    SavingsTransaction,
    LoanTransaction,
    CustomerAccountsSetup
)

logger = logging.getLogger(__name__)

def _get_earliest_uncleared_loan(cust_no, loan_type_str):
    """
    Finds the oldest loan of a specific type that still has a balance.
    FIXED: Uses a double-underscore relationship lookup to check against the configuration string.
    Returns a tuple: (LoanHistory instance, current_balance) or (None, Decimal('0.00')).
    """
    # FIX: Change 'loan_type' to match your configuration model's string identifier (e.g., loan_type__account_type)
    # If your LoanHistory field setup uses 'loan_type__loan_type', modify the key suffix below accordingly.
    active_loans = LoanHistory.objects.filter(
        customer__cust_no=cust_no, 
        loan_type__account_type=loan_type_str 
    ).order_by('loan_date')

    for loan in active_loans:
        metrics = LoanTransaction.objects.filter(loan_no=loan.loan_no).aggregate(
            total_debit=Sum('debit_amount'),
            total_credit=Sum('credit_amount')
        )
        
        debits = metrics.get('total_debit') or Decimal('0.00')
        credits = metrics.get('total_credit') or Decimal('0.00')
        
        balance = debits - credits
        if balance > 0:
            return loan, balance
            
    return None, Decimal('0.00')


def post_mpesa_notifications():
    """
    Process unposted Mpesa notifications. Includes smart redirection, 
    dynamic balance calculation, and structured transactional SMS logging.
    """
    unposted = MpesaNotification.objects.filter(posted=False)

    for notif in unposted:
        try:
            with transaction.atomic():
                bill_ref = (notif.bill_ref_number or "").strip().upper()
                match = re.match(r"^(\d+)(MS|FD|CA|JA|SC|NL|EL|ML)$", bill_ref)
                
                if not match:
                    raise ValueError(f"Invalid format: {bill_ref}")

                cust_no_str, suffix = match.groups()
                cust_no = int(cust_no_str)
                
                customer = Customer.objects.filter(cust_no=cust_no).first()
                if not customer:
                    raise ValueError(f"Customer {cust_no} not found")

                saving_type_map = {
                    "SC": "share_capital", 
                    "MS": "savings_deposit",
                    "FD": "fixed_deposit", 
                    "JA": "junior_account", 
                    "CA": "collection_account",
                }
                loan_type_map = {
                    "NL": "normal_loan", 
                    "EL": "emergency_loan", 
                    "ML": "mobile_loan",
                }

                account_posted_to = "" 
                trans_amount = Decimal(str(notif.trans_amount))
                sms_message = ""

                # 1. Handle Direct Savings Deposit
                if suffix in saving_type_map:
                    account_type = saving_type_map[suffix]
                    acc_setup = CustomerAccountsSetup.objects.filter(account_type=account_type).first()
                    acc_code = acc_setup.account_code if acc_setup else "S00"
                    account_posted_to = f"{acc_code}-{cust_no}"

                    SavingsTransaction.objects.create(
                        cust_no=cust_no,
                        saving_type=account_type,
                        tr_date=now(),
                        tr_ref=notif.trans_id,
                        tr_desc=f"M-Pesa Deposit {notif.trans_id}",
                        credit_amount=trans_amount,
                        created_by="mpesa_job",
                    )

                    # Compute running savings balance
                    bal = SavingsTransaction.objects.filter(
                        cust_no=cust_no, 
                        saving_type=account_type
                    ).aggregate(
                        total=Sum(F('credit_amount') - F('debit_amount'))
                    )['total'] or Decimal('0.00')

                    acc_display_name = account_type.replace('_', ' ').title()
                    sms_message = (f"Dear {customer.full_name}, we have received KES {trans_amount:,.2f} via M-Pesa "
                                   f"Ref: {notif.trans_id} for your {acc_display_name} Account. "
                                   f"New Balance is KES {bal:,.2f}.")

                # 2. Handle Loans (With Overflow/Fallback Logic)
                elif suffix in loan_type_map:
                    loan_type = loan_type_map[suffix]
                    target_loan, loan_balance = _get_earliest_uncleared_loan(cust_no, loan_type)

                    loan_post_amount = Decimal('0.00')
                    savings_post_amount = Decimal('0.00')
                    accounts_posted_list = []
                    sms_segments = []

                    # Determine split allocations
                    if not target_loan:
                        savings_post_amount = trans_amount
                    elif trans_amount > loan_balance:
                        loan_post_amount = loan_balance
                        savings_post_amount = trans_amount - loan_balance
                    else:
                        loan_post_amount = trans_amount

                    # Apply Loan Payment Segment
                    if loan_post_amount > 0:
                        accounts_posted_list.append(target_loan.loan_no)
                        LoanTransaction.objects.create(
                            cust_no=cust_no,
                            loan_id=target_loan.id,
                            loan_no=target_loan.loan_no,
                            loan_type=loan_type,
                            tr_date=now(),
                            tr_ref=notif.trans_id,
                            tr_desc=f"M-Pesa Loan Repayment {notif.trans_id}",
                            credit_amount=loan_post_amount,
                            created_by="mpesa_job",
                        )

                        loan_bal = LoanTransaction.objects.filter(
                            loan_id=target_loan.id
                        ).aggregate(
                            total=Sum(F('debit_amount') - F('credit_amount'))
                        )['total'] or Decimal('0.00')
                        
                        sms_segments.append(f"KES {loan_post_amount:,.2f} allocated to Loan {target_loan.loan_no} (O/S Bal: KES {loan_bal:,.2f})")

                    # Apply Savings Balance Overflow/Fallback Segment
                    if savings_post_amount > 0:
                        fallback_type = "savings_deposit"
                        acc_setup = CustomerAccountsSetup.objects.filter(account_type=fallback_type).first()
                        acc_code = acc_setup.account_code if acc_setup else "S00"
                        accounts_posted_list.append(f"{acc_code}-{cust_no}")
                        
                        desc_prefix = "Loan Overpayment" if loan_post_amount > 0 else f"Fallback (No Active {suffix} Loan)"
                        
                        SavingsTransaction.objects.create(
                            cust_no=cust_no,
                            saving_type=fallback_type,
                            tr_date=now(),
                            tr_ref=notif.trans_id,
                            tr_desc=f"{desc_prefix} - Paybill {notif.trans_id}",
                            credit_amount=savings_post_amount,
                            created_by="mpesa_job",
                        )

                        sav_bal = SavingsTransaction.objects.filter(
                            cust_no=cust_no, 
                            saving_type=fallback_type
                        ).aggregate(
                            total=Sum(F('credit_amount') - F('debit_amount'))
                        )['total'] or Decimal('0.00')

                        label = "Overpayment" if loan_post_amount > 0 else "Fallback"
                        sms_segments.append(f"KES {savings_post_amount:,.2f} diverted as {label} to Savings (Bal: KES {sav_bal:,.2f})")

                    account_posted_to = " & ".join(accounts_posted_list)
                    
                    # Consolidate split payment information into an articulate single message body
                    msg_body = " and ".join(sms_segments)
                    sms_message = (f"Dear {customer.full_name}, received KES {trans_amount:,.2f} via M-Pesa "
                                   f"Ref: {notif.trans_id}. {msg_body}.")

                # 3. Structural Tracking
                PostedMpesaNotification.objects.create(
                    mpesa_notification=notif,
                    customer_no=cust_no,
                    account_type=account_posted_to 
                )

                # 4. Save Outbound SMS Notification
                if customer.mobile and sms_message:
                    SMSLog.objects.create(
                        phone=customer.mobile,
                        message=sms_message,
                        status='pending',
                        created_by='mpesa_job'
                    )

                # Update operational staging values
                notif.posted = True
                notif.last_error = None
                notif.save()

        except Exception as e:
            logger.exception(f"M-Pesa processing failure for transaction ID {notif.trans_id}")
            notif.last_error = str(e)
            notif.save()