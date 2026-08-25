import re
from django.utils.timezone import now
from .models import (
    MpesaNotification,
    PostedMpesaNotification,
    SavingsTransaction,
    ShareCapitalTransaction,
    LoanTransaction,
    CustomerAccountsSetup,
)


def post_mpesa_notifications():
    """
    Process unposted Mpesa notifications, interpret bill_ref_number,
    and insert into the appropriate transaction table dynamically
    using CustomerAccountsSetup mapping.
    """
    unposted = MpesaNotification.objects.filter(posted=False)

    # Load account setups into a dict for quick lookup
    account_map = {
        acc.acc_initials.upper(): acc for acc in CustomerAccountsSetup.objects.all()
    }

    for notif in unposted:
        try:
            bill_ref = (notif.bill_ref_number or "").strip().upper()

            # Expected format: digits + suffix (cust_no + account initials)
            match = re.match(r"^(\d+)([A-Z]+)$", bill_ref)
            if not match:
                notif.last_error = f"Invalid bill ref format: {bill_ref}"
                notif.save()
                continue

            cust_no, acc_suffix = match.groups()
            acc_config = account_map.get(acc_suffix)

            if not acc_config:
                notif.last_error = f"Unknown account suffix: {acc_suffix}"
                notif.save()
                continue

            # --- Handle Savings Accounts ---
            if acc_config.account_type == "S":
                SavingsTransaction.objects.create(
                    cust_no=int(cust_no),
                    saving_type=acc_config.account_name.lower().replace(" ", "_"),
                    tr_date=now(),
                    tr_ref=notif.trans_id,
                    tr_desc=f"Mpesa Payment {notif.trans_id}",
                    debit_amount=0,
                    credit_amount=notif.trans_amount,
                    created_by="mpesa_job",
                )

            # --- Handle Share Capital ---
            elif acc_config.account_type == "S" and acc_config.acc_initials == "SC":
                ShareCapitalTransaction.objects.create(
                    cust_no=int(cust_no),
                    tr_date=now(),
                    tr_ref=notif.trans_id,
                    tr_desc=f"Mpesa Payment {notif.trans_id}",
                    debit_amount=0,
                    credit_amount=notif.trans_amount,
                    created_by="mpesa_job",
                )

            # --- Handle Loans ---
            elif acc_config.account_type == "L":
                LoanTransaction.objects.create(
                    cust_no=int(cust_no),
                    loan_type=acc_config.account_name.lower().replace(" ", "_"),
                    tr_date=now(),
                    tr_ref=notif.trans_id,
                    tr_desc=f"Mpesa Payment {notif.trans_id}",
                    debit_amount=0,
                    credit_amount=notif.trans_amount,
                    created_by="mpesa_job",
                )

            else:
                notif.last_error = f"Unhandled account type: {acc_config.account_type}"
                notif.save()
                continue

            # --- Mark as posted ---
            PostedMpesaNotification.objects.create(
                mpesa_notification=notif,
                customer_no=cust_no,
                account_type=acc_config.acc_initials,
            )

            notif.posted = True
            notif.last_error = None
            notif.save()

        except Exception as e:
            notif.last_error = str(e)
            notif.save()
