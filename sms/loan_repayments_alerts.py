"""
sms/loan_repayments_alerts.py
==============================
Django management command — send SMS reminders to members whose loan
instalments are due within the next 3 days.

Usage:
    python manage.py loan_repayment_alerts
    # or schedule via Django-Q2 / cron
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from dateutil.relativedelta import relativedelta

from customers.models import Customer
from loans.models import LoanHistory
from transactions.models import LoanTransaction
from sms.services import notify

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Send SMS reminders to members with outstanding loans approaching their next due date."

    def handle(self, *args, **kwargs):
        today = timezone.localdate()
        sent = 0
        skipped = 0
        errors = 0

        # Active loans = disbursed, not fully repaid
        active_loans = LoanHistory.objects.filter(
            is_disbursed=True,
        ).select_related("customer")

        for loan in active_loans:
            try:
                # Compute outstanding balance from the loan subledger
                from django.db.models import Sum, F
                bal = (
                    LoanTransaction.objects
                    .filter(loan_no=loan.loan_no)
                    .aggregate(t=Sum(F("debit_amount") - F("credit_amount")))["t"]
                )
                outstanding = bal or 0
                if outstanding <= 0:
                    # Loan fully repaid
                    continue

                # Walk forward from loan date to find the next instalment due date
                due_date = loan.loan_date
                while due_date < today:
                    due_date += relativedelta(months=1)

                reminder_date = due_date - timedelta(days=3)

                if today != reminder_date:
                    skipped += 1
                    continue

                customer = loan.customer
                phone = getattr(customer, "mobile", None) or getattr(customer, "phone", None)
                if not phone or not str(phone).strip():
                    skipped += 1
                    continue

                message = (
                    f"Dear {customer.first_name}, your loan instalment of "
                    f"KES {loan.installment:,.2f} for loan {loan.loan_no} "
                    f"is due on {due_date.strftime('%d %b %Y')}. "
                    f"Outstanding balance: KES {outstanding:,.2f}. "
                    f"Please make your payment on time."
                )

                notify(str(phone).strip(), message, created_by="loan_alert")
                sent += 1
                logger.info("Reminder sent to %s for loan %s", customer.cust_no, loan.loan_no)

            except Exception:
                errors += 1
                logger.exception("Failed to process loan %s", getattr(loan, "loan_no", "?"))

        summary = f"Loan reminders: sent={sent}, skipped={skipped}, errors={errors}"
        logger.info(summary)
        self.stdout.write(self.style.SUCCESS(summary))
