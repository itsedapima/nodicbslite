"""
customers/services.py
----------------------
Maker-checker execution layer for member exit.

MAKER (clerk/officer): process_member_exit validates the member is
eligible (no outstanding loans, no active guarantees) and submits an
ApprovalRequest carrying the exit details. No accounts are touched.

CHECKER (manager): on approval, execute_member_exit() settles all
positive savings balances in one atomic block:
  1. Sweeps all funds (preserving minimums) into the Collection Account
     — the same product used for loan disbursements (setting:
     LOAN_DISBURSEMENT_SAVINGS_PRODUCT, default 'collection_account').
  2. Updates the member's status and notifies by SMS.
  3. Staff create a bankers cheque manually from the accounting app.

Both active and dormant members are eligible for exit. Only members
already exited or deceased are blocked.
"""

import logging
from decimal import Decimal

from django.conf import settings as django_settings
from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils.timezone import now

from customers.models import Customer
from transactions.models import SavingsTransaction, LoanTransaction, CustomerAccountsSetup
from sms.services import notify, msg_member_exit

logger = logging.getLogger(__name__)


class ExitError(Exception):
    """Raised when an approved exit cannot be executed."""


def build_exit_payload(cust_no, reason, exit_type, exit_date, death_date=None):
    payload = {
        'cust_no':   str(cust_no),
        'reason':    reason or '',
        'exit_type': exit_type,
        'exit_date': str(exit_date),
    }
    if death_date:
        payload['death_date'] = str(death_date)
    return payload


def validate_exit(cust_no):
    """
    Validates if a member is legally eligible to exit the Sacco.
    Returns (is_ok, total_outstanding_loan_balance, active_guaranteed_loan_list)
    """
    from django.db.models import Sum
    from transactions.models import LoanTransaction
    from loans.models import Guarantor, RunningLoanStat

    # 1. Check direct outstanding loan debts
    direct_loans = LoanTransaction.objects.filter(cust_no=cust_no).aggregate(
        bal=(Sum('debit_amount') - Sum('credit_amount'))
    )['bal'] or Decimal('0.00')

    if direct_loans > 0:
        return False, direct_loans, []

    # 2. Check active external guarantorship liabilities
    active_guarantees = []
    guarantees_given = Guarantor.objects.filter(guarantor_cust__cust_no=cust_no).select_related('loan')

    for g in guarantees_given:
        stat = RunningLoanStat.objects.filter(loan_no=g.loan.loan_no).first()

        if not stat or stat.loan_status != "Settled":
            ledger_bal = LoanTransaction.objects.filter(loan_id=g.loan.id).aggregate(
                bal=(Sum('debit_amount') - Sum('credit_amount'))
            )['bal'] or Decimal('0.00')

            if ledger_bal > 0 or (stat and stat.loan_status != "Settled"):
                active_guarantees.append({
                    "loan_no": g.loan.loan_no,
                    "amount": float(g.amount),
                    "current_status": stat.loan_status if stat else "Active"
                })

    if active_guarantees:
        return False, Decimal('0.00'), active_guarantees

    return True, Decimal('0.00'), []


def _get_collection_product():
    """
    Return the savings product used as the disbursement / collection
    account — the same one used when disbursing loans. Reads
    settings.LOAN_DISBURSEMENT_SAVINGS_PRODUCT (default 'collection_account').
    """
    return getattr(django_settings, 'LOAN_DISBURSEMENT_SAVINGS_PRODUCT', 'collection_account')


def execute_member_exit(payload, checker_username='system'):
    """
    Executes an APPROVED member exit request within an isolated transaction block.

    Flow:
      1. Validate the member has no outstanding debts.
      2. Sweep all savings balances (preserving per-product minimums) into
         the Collection Account — same product used for loan disbursements.
      3. Update the member's status to the exit type.
      4. Notify the member by SMS and log the audit event.

    Staff can then create a bankers cheque manually from the accounting
    app when they are ready to pay the member out.
    """
    cust_no   = payload['cust_no']
    exit_type = payload['exit_type']
    exit_date = payload['exit_date']
    reason    = payload.get('reason', '')

    try:
        customer = Customer.objects.get(cust_no=cust_no)
    except Customer.DoesNotExist:
        raise ExitError(f"Member {cust_no} not found.")

    # Only already-exited or deceased members are ineligible
    if customer.customer_status in ('exited', 'deceased'):
        raise ExitError(
            f"Member {cust_no} has already been exited "
            f"(status: {customer.customer_status})."
        )

    # 1. Pre-Commit Policy Check
    is_ok, loan_bal, active_guarantees = validate_exit(cust_no)
    if not is_ok:
        if loan_bal > 0:
            raise ExitError(f"Exit rejected: Member owes KES {loan_bal:,.2f} on active loan profiles.")
        else:
            raise ExitError(f"Exit rejected: Member is actively guaranteeing un-settled asset portfolios: {active_guarantees}")

    collection_product = _get_collection_product()
    total_swept = Decimal('0.00')
    current_time_stamp = now()

    with db_transaction.atomic():
        # 2. Extract active savings accounts with positive standing balances
        savings_balances = (
            SavingsTransaction.objects.filter(cust_no=cust_no)
            .values('saving_type')
            .annotate(balance=Sum('credit_amount') - Sum('debit_amount'))
            .filter(balance__gt=0)
        )

        # Build minimum thresholds per product type
        account_policies = {
            setup.account_type: setup.min_balance
            for setup in CustomerAccountsSetup.objects.all()
        }

        for acc in savings_balances:
            acc_type = acc['saving_type']
            current_bal = acc['balance']

            min_required = account_policies.get(acc_type, Decimal('0.00'))

            # Skip if this IS the collection account — don't sweep into itself
            if acc_type == collection_product:
                continue

            transferable_amount = current_bal - min_required

            if transferable_amount > 0:
                # Debit the sub-account down to its minimum
                SavingsTransaction.objects.create(
                    cust_no=cust_no,
                    saving_type=acc_type,
                    tr_date=current_time_stamp,
                    tr_ref=f"SWEEP-EXIT-{cust_no}",
                    tr_desc=f"Exit Consolidation: Sweeping excess to {collection_product.replace('_', ' ').title()}",
                    debit_amount=transferable_amount,
                    credit_amount=0,
                    created_by=checker_username,
                )

                # Credit into the Collection Account
                SavingsTransaction.objects.create(
                    cust_no=cust_no,
                    saving_type=collection_product,
                    tr_date=current_time_stamp,
                    tr_ref=f"SWEEP-EXIT-{cust_no}",
                    tr_desc=f"Exit Consolidation: Received from {acc_type.replace('_', ' ').title()}",
                    debit_amount=0,
                    credit_amount=transferable_amount,
                    created_by=checker_username,
                )
                total_swept += transferable_amount

        # 2b. Calculate total collection account balance (for audit/SMS info)
        collection_bal = (
            SavingsTransaction.objects.filter(cust_no=cust_no, saving_type=collection_product)
            .aggregate(bal=Sum('credit_amount') - Sum('debit_amount'))
        )['bal'] or Decimal('0.00')

        # 3. Finalize Master Account File State Parameters
        customer.customer_status = exit_type  # E.g. 'exited', 'deceased'
        customer.exit_date = exit_date
        customer.exit_reason = reason
        if exit_type == 'deceased' and payload.get('death_date'):
            customer.death_date = payload['death_date']
        customer.save()

    # 4. Outbound Notification Dispatch
    if customer.phone and exit_type != 'deceased':
        try:
            notify(
                customer.phone,
                (
                    f"Dear {customer.first_name}, your member exit has been approved. "
                    f"Funds consolidated to {collection_product.replace('_', ' ').title()}: "
                    f"KES {total_swept:,.2f}. Total balance in collection account: "
                    f"KES {collection_bal:,.2f}. Please visit any branch to collect your payout."
                ),
                created_by=checker_username,
            )
        except Exception:
            logger.exception("Outbound SMS transmission exception logged post-commit.")

    # 5. Financial Audit Telemetry Logging
    try:
        from audit.services import log_financial_event
        log_financial_event(
            event='MEMBER_EXIT_PROCESSED',
            amount=total_swept,
            reference=f'EXIT-{cust_no}',
            actor=checker_username,
            details=(
                f"Member {customer.full_name} (No. {customer.cust_no}) exit executed as '{exit_type}'. "
                f"Retained individual account product minimum limits. "
                f"Swept KES {total_swept:,.2f} into {collection_product.replace('_', ' ').title()}. "
                f"Collection account balance: KES {collection_bal:,.2f}. "
                f"Staff to create bankers cheque for payout manually."
            ),
            severity='critical',
        )
    except Exception:
        logger.exception(f"Failed to log financial event for member exit {cust_no}")

    return customer, total_swept