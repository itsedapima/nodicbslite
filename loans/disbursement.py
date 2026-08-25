"""
loans/disbursement.py
----------------------
Maker-checker execution layer for loan disbursement.

MAKER  (loan officer, loans.views.loan_dispatch):
    Validates the application, computes installment/fees, saves the
    LoanHistory record with is_disbursed=False, records charges and
    guarantors, and submits an ApprovalRequest carrying the money-movement
    payload. NO ledger entries and NO balance mutations happen here.

CHECKER (manager, approvals app):
    On approval, execute_disbursement() runs inside one atomic block:
    re-validates bridging offsets, posts all LoanTransaction and
    SaccoAccountsLedger entries, marks the loan disbursed, and notifies
    the member and guarantors by SMS. Admin receives a financial alert
    via email.
"""

import logging
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from accounting.models import SaccoAccount, SaccoAccountsLedger
from loans.models import LoanHistory, RunningLoanStat
from transactions.models import LoanTransaction
from transactions.utils import make_tr_ref
from sms.services import (
    notify, msg_loan_approved, msg_loan_disbursed,
    email_notify, email_loan_disbursed,
)
from audit.services import log_financial_event

logger = logging.getLogger(__name__)

# GL account codes — resolved dynamically per loan product now.
# These are ONLY used as ultimate fallbacks if the product has no linked GL.
GL_LOANS_RECEIVABLE_DEFAULT = '900-630010'
GL_CASH             = '900-601001'
GL_FEES_INCOME      = '900-124006'
GL_INTEREST_INCOME  = '900-110000'


def _resolve_loan_gl(loan_type_name: str) -> str:
    """
    Resolve the correct GL receivable account for the loan product via
    CustomerAccountsSetup.sacco_gl_account. Falls back to the built-in
    default map if not linked.
    """
    from transactions.mpesa_posting import _resolve_product_gl
    try:
        return _resolve_product_gl(loan_type_name, is_loan=True)
    except Exception:
        return GL_LOANS_RECEIVABLE_DEFAULT


def _resolve_interest_gl_code(loan_type_name: str) -> str:
    """Interest-income GL for a loan product."""
    from transactions.mpesa_posting import _resolve_interest_gl
    try:
        return _resolve_interest_gl(loan_type_name)
    except Exception:
        return GL_INTEREST_INCOME


class DisbursementError(Exception):
    """Raised when an approved disbursement cannot be executed."""


def build_payload(loan, principal, upfront_interest,
                  total_dynamic_fees, total_bridging_amt, total_bridging_fees,
                  validated_offsets, loan_type_name):
    """
    Serialise everything the checker-side executor needs into a JSON-safe
    dict stored on the ApprovalRequest.
    """
    return {
        'loan_no':             loan.loan_no,
        'cust_no':             str(loan.customer.cust_no),
        'member_name':         loan.customer.full_name,
        'principal':           str(principal),
        'upfront_interest':    str(upfront_interest),
        'total_dynamic_fees':  str(total_dynamic_fees),
        'total_bridging_amt':  str(total_bridging_amt),
        'total_bridging_fees': str(total_bridging_fees),
        'net_disbursed':       str(loan.net_disbursed),
        'installment':         str(loan.installment),
        'loan_type':           loan_type_name,
        'offsets':             [
            {'loan_no': lno, 'amount': str(amt)} for lno, amt in validated_offsets
        ],
    }


def execute_disbursement(loan, payload, checker_username='system'):
    """
    Execute an APPROVED loan disbursement. Must be called inside (or will
    open) an atomic block. Raises DisbursementError on any inconsistency
    so the approval can be safely retried or rejected.
    """
    if loan.is_disbursed:
        raise DisbursementError(f"Loan {loan.loan_no} is already disbursed.")

    principal           = Decimal(payload['principal'])
    upfront_interest    = Decimal(payload.get('upfront_interest', '0'))
    total_dynamic_fees  = Decimal(payload.get('total_dynamic_fees', '0'))
    total_bridging_amt  = Decimal(payload.get('total_bridging_amt', '0'))
    total_bridging_fees = Decimal(payload.get('total_bridging_fees', '0'))
    offsets             = payload.get('offsets', [])

    customer  = loan.customer
    loan_type = payload.get('loan_type') or 'Standard Loan'
    loan_no   = loan.loan_no or f"LN-{loan.id}"

    # The effective date for all subledger and GL entries is the APPROVAL
    # date, not the application date.  E.g. applied 15th, approved 20th →
    # all debits/credits are dated 20th.
    approval_date = timezone.now()

    with db_transaction.atomic():
        # ── 1. Re-validate & apply bridging offsets (balances may have moved
        #       between maker submission and checker approval) ───────────────
        for off in offsets:
            old_no  = off['loan_no']
            val     = Decimal(off['amount'])
            try:
                running = RunningLoanStat.objects.select_for_update().get(loan_no=old_no)
            except RunningLoanStat.DoesNotExist:
                raise DisbursementError(f"Offset target {old_no} no longer exists.")
            if val > running.loan_balance:
                raise DisbursementError(
                    f"Offset KES {val:,.2f} exceeds current balance of {old_no} "
                    f"(KES {running.loan_balance:,.2f}). Reject and re-submit."
                )
            running.loan_balance -= val
            running.save(update_fields=['loan_balance'])

            # Resolve the real LoanHistory id for the ledger row
            old_loan = LoanHistory.objects.filter(loan_no=old_no).only('id').first()
            LoanTransaction.objects.create(
                cust_no=customer.cust_no,
                loan_id=old_loan.id if old_loan else running.id,
                loan_no=old_no, loan_type='offset',
                tr_date=approval_date, tr_ref=make_tr_ref('loan_offset'),
                tr_desc=f"Bridged by Loan {loan_no}",
                debit_amount=0, credit_amount=val,
                created_by=checker_username,
            )

        # ── 2. Principal disbursement ─────────────────────────────────────
        LoanTransaction.objects.create(
            cust_no=customer.cust_no, loan_id=loan.id,
            loan_no=loan_no, loan_type=loan_type,
            tr_date=approval_date, tr_ref=f"DISB-{loan_no}",
            tr_desc="Principal Disbursement",
            debit_amount=principal, credit_amount=0,
            created_by=checker_username,
        )

        # ── 3. Upfront interest ───────────────────────────────────────────
        if upfront_interest > Decimal('0'):
            LoanTransaction.objects.create(
                cust_no=customer.cust_no, loan_id=loan.id,
                loan_no=loan_no, loan_type=loan_type,
                tr_date=approval_date, tr_ref=make_tr_ref('loan_interest'),
                tr_desc="Upfront Interest Charge",
                debit_amount=upfront_interest, credit_amount=0,
                created_by=checker_username,
            )

        # ── 4. Sacco double-entry ledger (via post_journal for full
        #       balance tracking and audit trail) ──────────────────────
        from accounting.services import post_journal, J as _J, SAVINGS_TYPE_TO_GL, GL as _GL

        # Resolve the CORRECT receivable GL for this specific loan product
        loan_gl = _resolve_loan_gl(loan_type)
        interest_gl = _resolve_interest_gl_code(loan_type)

        # Resolve the withdrawable savings product where net funds are parked.
        # This is the product the member will withdraw from via banker's cheque.
        from django.conf import settings as _settings
        _disb_product = getattr(_settings, 'LOAN_DISBURSEMENT_SAVINGS_PRODUCT', 'fosa_deposit')
        _disb_gl_code = SAVINGS_TYPE_TO_GL.get(_disb_product, _GL.MEMBER_DEPOSITS)

        gl_legs = [
            _J(account_code=loan_gl, debit=principal,
               description=f"Disbursement {loan_no}"),
            _J(account_code=_disb_gl_code, credit=loan.net_disbursed,
               description=f"Net to {_disb_product.replace('_',' ').title()} — {loan_no}"),
        ]

        total_all_fees = total_dynamic_fees + total_bridging_fees
        if total_all_fees > Decimal('0'):
            gl_legs.append(
                _J(account_code=GL_FEES_INCOME, credit=total_all_fees,
                   description=f"Fees & Penalties {loan_no}")
            )

        if total_bridging_amt > Decimal('0'):
            gl_legs.append(
                _J(account_code=loan_gl, credit=total_bridging_amt,
                   description=f"Bridged Offsets {loan_no}")
            )

        if upfront_interest > Decimal('0'):
            gl_legs.append(
                _J(account_code=loan_gl, debit=upfront_interest,
                   description=f"Interest Load {loan_no}")
            )
            gl_legs.append(
                _J(account_code=interest_gl, credit=upfront_interest,
                   description=f"Upfront Interest {loan_no}")
            )

        post_journal(
            reference=f"DISB-{loan_no}",
            description=f"Loan disbursement {loan_no}",
            lines=gl_legs,
            user=None,
            customer=customer,
            external_reference=loan_no,
            posting_date=approval_date.date(),
        )

        # ── 5. Credit net disbursed to member's withdrawable savings product ──
        #    This is a SUBLEDGER-ONLY credit — the GL side is already covered
        #    by the Cr Member Deposits leg above. The member sees the balance
        #    appear in their FOSA / savings, ready for cheque withdrawal.
        from transactions.models import SavingsTransaction

        SavingsTransaction.objects.create(
            cust_no=customer.cust_no,
            saving_type=_disb_product,
            tr_date=approval_date,
            tr_ref=f"DISB-{loan_no}",
            tr_desc=f"Loan {loan_no} net disbursed to {_disb_product.replace('_',' ').title()}",
            credit_amount=loan.net_disbursed,
            debit_amount=Decimal("0"),
            created_by=checker_username,
        )

        # ── 6. Mark as approved AND disbursed ─────────────────────────────
        #    All GL entries, loan transactions, and savings credits are
        #    already posted above — the loan IS effectively disbursed.
        #    If the SACCO uses the cheque workflow, cheque_service will
        #    update disbursal_ref with the cheque number later.
        loan.is_approved  = True
        loan.approved_at  = approval_date
        loan.approved_by  = checker_username
        loan.is_disbursed = True
        loan.disbursed_at = approval_date
        loan.save(update_fields=[
            'is_approved', 'approved_at', 'approved_by',
            'is_disbursed', 'disbursed_at',
        ])

    # ── 6. Member communication — outside atomic block, never blocks ────────
    # At approval time, notify the member their loan is APPROVED (not disbursed).
    # The disbursement notification (msg_loan_disbursed) is sent separately
    # when the actual funds are released via bankers cheque or EFT.
    if customer.phone:
        notify(
            customer.phone,
            msg_loan_approved(
                customer.first_name, loan_no, principal,
                loan.installment,
            ),
            created_by=checker_username,
        )

    # NOTE: Guarantors are NOT notified here — they were already notified
    # when added via loans.views.add_guarantor.  Re-notifying at approval
    # time would produce duplicates (especially on reject → edit → re-approve).

    # ── 7. Admin financial alert ────────────────────────────────────────────
    log_financial_event(
        event='LOAN_APPROVED',
        amount=principal,
        reference=loan_no,
        actor=checker_username,
        details=(
            f"Loan {loan_no} approved for {customer.full_name} "
            f"(Member {customer.cust_no}). "
            f"Principal: KES {principal:,.2f}, Net: KES {loan.net_disbursed:,.2f}, "
            f"Installment: KES {loan.installment:,.2f}. "
            f"Awaiting disbursement via bankers cheque/EFT."
        ),
        severity='critical',
    )

    logger.info("Loan %s approved by %s — awaiting disbursement", loan_no, checker_username)
    return loan
