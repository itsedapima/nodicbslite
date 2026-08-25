import re
from django.utils.timezone import now
from .models import (
    MpesaNotification,
    PostedMpesaNotification,
    SavingsTransaction,
    ShareCapitalTransaction,
    LoanTransaction,
)


def post_mpesa_notifications():
    """
    Process unposted Mpesa notifications, interpret bill_ref_number,
    and insert into the appropriate transaction table based on CustomerAccountsSetup mapping.
    """
    unposted = MpesaNotification.objects.filter(posted=False)

    for notif in unposted:
        try:
            bill_ref = (notif.bill_ref_number or "").strip().upper()

            # Expected format: digits + account suffix (e.g. 01146MS)
            match = re.match(r"^(\d+)(MS|FD|CA|JA|SC|NL|EL|ML)$", bill_ref)
            if not match:
                notif.last_error = f"Invalid bill ref format: {bill_ref}"
                notif.save()
                continue

            cust_no, account_type = match.groups()

            # --- Handle Savings Accounts ---
            if account_type in ["MS","SC", "FD","CA","JA"]:
                saving_type_map = {
                    "SC": "share_capital",
                    "MS": "savings_deposit",
                    "FD": "fixed_deposit",
                    "JA": "junior_account",
                    "CA": "collection_account",
                }

                SavingsTransaction.objects.create(
                    cust_no=int(cust_no),
                    saving_type=saving_type_map[account_type],
                    tr_date=now(),
                    tr_ref=notif.trans_id,
                    tr_desc=f"Mpesa Payment {notif.trans_id}",
                    debit_amount=0,
                    credit_amount=notif.trans_amount,
                    created_by="mpesa_job",
                )


            # --- Handle Loans ---
            elif account_type in ["NL", "EL", "ML"]:
                loan_type_map = {
                    "NL": "normal_loan",
                    "EL": "emergency_loan",
                    "ML": "mobile_loan",
                }

                LoanTransaction.objects.create(
                    cust_no=int(cust_no),
                    loan_type=loan_type_map[account_type],
                    tr_date=now(),
                    tr_ref=notif.trans_id,
                    tr_desc=f"Mpesa Payment {notif.trans_id}",
                    debit_amount=0,
                    credit_amount=notif.trans_amount,
                    created_by="mpesa_job",
                )

                # --- Record as posted ---
                 # Build flexible account code e.g. "S01-01146" or "NL-01146"
                cust_no_int = int(cust_no)
                account_code = f"{account_type}-{cust_no_int:05d}"  # zero-padded to 5 digits

                PostedMpesaNotification.objects.create(
                mpesa_notification=notif,
                customer_no=cust_no_int,
                account_type=account_code,  # store as compound code
                )


            notif.posted = True
            notif.last_error = None
            notif.save()

        except Exception as e:
            notif.last_error = str(e)
            notif.save()
