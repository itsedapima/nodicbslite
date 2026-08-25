"""
loans/restructure_service.py
=============================
Business logic for loan restructure and guarantor defaulter offload.

Both operations use `accounting.services.post_journal()` for balanced
double-entry, and record `TransactionAuditLog` entries automatically.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


def _q(v) -> Decimal:
    if v is None:
        return ZERO
    if not isinstance(v, Decimal):
        v = Decimal(str(v))
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ═════════════════════════════════════════════════════════════════════
#  BALANCE HELPERS — principal vs interest split
# ═════════════════════════════════════════════════════════════════════

def get_loan_balances(loan) -> dict:
    """
    Break the outstanding balance into its principal and interest
    components using the LoanTransaction ledger.

    Convention observed in this codebase:
        • Debits = charges (principal disbursed, interest charged,
                            fees, penalties).
        • Credits = repayments received.
        • Interest debits are identified by tr_desc containing "interest".

    Repayments are applied first to the principal until it reaches zero,
    then to interest. This mirrors the practical policy of most SACCOs
    (interest is a "topping-up" charge that trails principal). If your
    SACCO applies repayments to interest first, adjust the split below.
    """
    from transactions.models import LoanTransaction

    txns = LoanTransaction.objects.filter(loan_no=loan.loan_no)

    totals = txns.aggregate(
        total_dr=Sum("debit_amount"),
        total_cr=Sum("credit_amount"),
        interest_dr=Sum("debit_amount", filter=Q(tr_desc__icontains="interest")),
    )

    total_dr = _q(totals["total_dr"] or 0)
    total_cr = _q(totals["total_cr"] or 0)
    interest_dr = _q(totals["interest_dr"] or 0)
    principal_dr = total_dr - interest_dr

    outstanding = max(ZERO, total_dr - total_cr)

    # Apply credits to principal first, then interest
    principal_balance = max(ZERO, principal_dr - total_cr)
    interest_repaid_after_principal = max(ZERO, total_cr - principal_dr)
    interest_balance = max(ZERO, interest_dr - interest_repaid_after_principal)

    return {
        "outstanding": outstanding,
        "principal_charged": principal_dr,
        "interest_charged": interest_dr,
        "total_repaid": total_cr,
        "principal_balance": principal_balance,
        "interest_balance": interest_balance,
    }


# ═════════════════════════════════════════════════════════════════════
#  RESTRUCTURE
# ═════════════════════════════════════════════════════════════════════

@dataclass
class RestructureResult:
    loan_id: int
    loan_no: str
    original_snapshot: dict
    new_loan_date: str
    new_period: int
    new_installment: Decimal
    outstanding_at_restructure: Decimal
    principal_at_restructure: Decimal
    interest_at_restructure: Decimal
    restructure_fee: Decimal
    fee_reference: Optional[str]


def restructure_loan(
    *,
    loan,
    new_loan_date,
    new_period: int,
    new_installment: Decimal,
    restructure_fee_rate: Decimal,
    reason: str,
    user,
    request=None,
) -> RestructureResult:
    """
    Restructure a disbursed loan. Steps:
      1. Snapshot the ORIGINAL loan terms into a JSON blob saved on the
         loan itself for downstream audit and reporting.
      2. Compute the outstanding balance (principal + interest).
      3. Set the loan's `loan_date` to `new_loan_date`, `loan_period` to
         `new_period`, `installment` to `new_installment`, `principal`
         to the outstanding balance. This makes the aging logic in
         `loans/utils.py::_compute_stat` treat this loan as a fresh
         facility (no arrears).
      4. Optionally charge a restructure fee (posts a balanced GL
         journal: DR Loans Receivable, CR Fee Income).
      5. Regenerate RunningLoanStat and log a TransactionAuditLog entry.
    """
    from .models import LoanHistory
    from transactions.models import LoanTransaction

    if not loan.is_disbursed:
        raise ValueError("Only disbursed loans can be restructured.")
    if loan.is_restructured and getattr(settings, "LOAN_RESTRUCTURE_ONCE_ONLY", False):
        raise ValueError("This loan has already been restructured.")

    balances = get_loan_balances(loan)
    outstanding = balances["outstanding"]
    if outstanding <= ZERO:
        raise ValueError(
            "This loan has zero or negative outstanding balance; there is "
            "nothing to restructure."
        )

    if new_period > loan.loan_type.max_repayment_period:
        raise ValueError(
            f"New period {new_period} exceeds product maximum "
            f"{loan.loan_type.max_repayment_period} months."
        )

    new_installment = _q(new_installment)
    if new_installment <= ZERO:
        raise ValueError("New installment must be positive.")

    fee_rate = _q(restructure_fee_rate)
    restructure_fee = _q(outstanding * fee_rate / Decimal("100"))

    # ── 1. Snapshot original loan ────────────────────────────────
    snapshot = {
        "snapshot_taken_at": timezone.now().isoformat(),
        "restructured_by": getattr(user, "username", str(user)),
        "reason": reason,
        # Original terms (immutable record for audit)
        "original_loan_no": loan.loan_no,
        "original_loan_date": loan.loan_date.isoformat() if loan.loan_date else None,
        "original_principal": str(loan.principal),
        "original_installment": str(loan.installment),
        "original_loan_period": loan.loan_period,
        "original_interest_rate": str(loan.interest_rate),
        # Balances at moment of restructure
        "outstanding_at_restructure": str(outstanding),
        "principal_balance_at_restructure": str(balances["principal_balance"]),
        "interest_balance_at_restructure": str(balances["interest_balance"]),
        "principal_charged_total": str(balances["principal_charged"]),
        "interest_charged_total": str(balances["interest_charged"]),
        "total_repaid_before_restructure": str(balances["total_repaid"]),
        # New terms
        "new_loan_date": new_loan_date.isoformat() if hasattr(new_loan_date, "isoformat") else str(new_loan_date),
        "new_period": new_period,
        "new_installment": str(new_installment),
        "restructure_fee_rate": str(fee_rate),
        "restructure_fee": str(restructure_fee),
    }

    fee_reference = None

    with transaction.atomic():
        # Preserve any prior restructure history — append instead of overwrite
        prior_history = []
        if loan.original_loan_summary:
            try:
                existing = json.loads(loan.original_loan_summary)
                prior_history = (
                    existing if isinstance(existing, list) else [existing]
                )
            except (ValueError, TypeError):
                prior_history = [{"raw": loan.original_loan_summary}]

        prior_history.append(snapshot)

        # ── 2. Mutate loan (aging resets because loan_date changes) ──
        loan.loan_date = new_loan_date
        loan.loan_period = new_period
        loan.installment = new_installment
        loan.principal = outstanding  # New "as-if" principal
        loan.is_restructured = True
        loan.restructured_at = timezone.now()
        loan.restructure_fee = restructure_fee
        loan.original_loan_summary = json.dumps(prior_history, default=str)
        loan.save(update_fields=[
            "loan_date", "loan_period", "installment", "principal",
            "is_restructured", "restructured_at", "restructure_fee",
            "original_loan_summary",
        ])

        # ── 3. Restructure fee (LoanTransaction + GL journal) ─────
        if restructure_fee > ZERO:
            fee_reference = f"RSTF{int(datetime.now().timestamp())}"
            LoanTransaction.objects.create(
                cust_no=loan.customer.cust_no,
                loan_id=loan.id,
                loan_no=loan.loan_no,
                loan_type=loan.loan_type.account_type,
                tr_date=timezone.now(),
                tr_ref=fee_reference,
                tr_desc="Loan restructure fee",
                debit_amount=restructure_fee,
                credit_amount=ZERO,
                created_by=getattr(user, "username", str(user)),
            )
            _post_restructure_fee_gl(
                loan=loan,
                fee=restructure_fee,
                reference=fee_reference,
                user=user,
            )

        # ── 4. Regenerate running-loan stat so aging is immediately
        #     visible as zero-arrears in the dashboard. ─────────────
        try:
            from .utils import update_running_loans_stats
            update_running_loans_stats(cust_no=str(loan.customer.cust_no))
        except Exception:
            logger.exception(
                "Failed to refresh running loan stat after restructure "
                "for loan %s", loan.loan_no,
            )

        # ── 5. Audit log ─────────────────────────────────────────
        try:
            from accounting.audit_trail import log_transaction
            log_transaction(
                action="journal_post",
                reference=f"RESTRUCTURE-{loan.loan_no}",
                affected_accounts=[],
                total_amount=outstanding,
                user=user,
                request=request,
                description=(
                    f"Loan {loan.loan_no} restructured. "
                    f"Old principal {snapshot['original_principal']} → "
                    f"new principal {outstanding} "
                    f"(period {snapshot['original_loan_period']} → {new_period} mo). "
                    f"Reason: {reason}"
                ),
                customer_ref=str(loan.customer.cust_no),
                external_ref=fee_reference or "",
            )
        except Exception:
            logger.exception("Failed to write restructure audit log")

    return RestructureResult(
        loan_id=loan.id,
        loan_no=loan.loan_no,
        original_snapshot=snapshot,
        new_loan_date=str(new_loan_date),
        new_period=new_period,
        new_installment=new_installment,
        outstanding_at_restructure=outstanding,
        principal_at_restructure=balances["principal_balance"],
        interest_at_restructure=balances["interest_balance"],
        restructure_fee=restructure_fee,
        fee_reference=fee_reference,
    )


def _post_restructure_fee_gl(*, loan, fee, reference, user):
    """DR Loans Receivable, CR Fee Income for restructure fee."""
    try:
        from accounting.services import post_journal, J
        from accounting.models import SaccoAccount

        from accounting.services import GL as _GL
        loans_code = _GL.LOANS_RECEIVABLE
        fee_code = _GL.TOPUP_CHARGES

        loans_acc = SaccoAccount.objects.filter(account_code=loans_code).first()
        fee_acc = SaccoAccount.objects.filter(account_code=fee_code).first()
        if not (loans_acc and fee_acc):
            logger.warning(
                "Restructure fee GL codes missing (loans=%s, fee=%s)",
                loans_code, fee_code,
            )
            return

        post_journal(
            reference=reference,
            description=f"Restructure fee on loan {loan.loan_no}",
            lines=[
                J(account_code=loans_acc.account_code, debit=fee, credit=ZERO),
                J(account_code=fee_acc.account_code, debit=ZERO, credit=fee),
            ],
            user=user,
            customer=loan.customer,
        )
    except Exception:
        logger.exception(
            "GL posting for restructure fee on loan %s failed",
            loan.loan_no,
        )


# ═════════════════════════════════════════════════════════════════════
#  GUARANTOR DEFAULTER OFFLOAD
# ═════════════════════════════════════════════════════════════════════

@dataclass
class GuarantorAllocation:
    guarantor_cust_no: str
    guarantor_name: str
    guarantee_amount: Decimal
    percentage: Decimal   # of pool
    allocated_amount: Decimal
    new_loan_no: Optional[str] = None
    new_loan_id: Optional[int] = None


@dataclass
class OffloadResult:
    original_loan_id: int
    original_loan_no: str
    principal_balance_before: Decimal
    interest_balance_before: Decimal
    total_pool: Decimal
    total_allocated: Decimal
    residual_balance: Decimal
    allocations: List[GuarantorAllocation]
    reference: str


def preview_guarantor_offload(loan) -> dict:
    """
    Compute what the offload WOULD do without writing anything. Used
    by the template to show the officer the split before they confirm.
    """
    from .models import Guarantor

    balances = get_loan_balances(loan)
    principal_balance = balances["principal_balance"]

    guarantors = list(Guarantor.objects.filter(loan=loan).select_related("guarantor_cust"))
    total_pool = _q(sum((g.amount or ZERO) for g in guarantors))

    allocations: List[GuarantorAllocation] = []
    total_allocated = ZERO

    if total_pool > ZERO and principal_balance > ZERO:
        # Distribute principal balance proportionally to guarantee amount.
        # We compute each share, then adjust the LAST allocation so the
        # sum matches the target exactly (rounding safety).
        running = ZERO
        n = len(guarantors)
        for i, g in enumerate(guarantors):
            pct = (g.amount / total_pool) * Decimal("100")
            if i < n - 1:
                allocated = _q(principal_balance * (g.amount / total_pool))
                running += allocated
            else:
                allocated = _q(principal_balance - running)
            total_allocated += allocated

            allocations.append(GuarantorAllocation(
                guarantor_cust_no=str(g.guarantor_cust.cust_no),
                guarantor_name=g.guarantor_cust.full_name,
                guarantee_amount=_q(g.amount),
                percentage=_q(pct),
                allocated_amount=allocated,
            ))

    residual = balances["outstanding"] - total_allocated

    return {
        "outstanding": balances["outstanding"],
        "principal_balance": principal_balance,
        "interest_balance": balances["interest_balance"],
        "total_pool": total_pool,
        "total_allocated": total_allocated,
        "residual_balance": max(ZERO, residual),
        "allocations": allocations,
    }


def execute_guarantor_offload(
    *,
    loan,
    new_loan_period: int = 12,
    interest_rate: Decimal = ZERO,
    reason: str = "",
    user=None,
    request=None,
) -> OffloadResult:
    """
    Post the guarantor offload:
      1. For each guarantor, create a Guarantor-Defaulter loan (product
         'defaulter_loan') for their allocated share.
      2. Post a matching DEBIT on the guarantor's new loan (opening).
      3. Post a matching CREDIT (repayment) on the original defaulted
         loan to reduce the principal.
      4. Post a GL journal that reflects the transfer: for each
         guarantor DR (New Loan Receivable), CR (Original Loan
         Receivable). This preserves total assets — money just moved
         from one member's loan to another's.
      5. The residual balance (interest + penalties + any rounding)
         stays on the original loan for the officer to handle via
         inter-account transfer from savings.
    """
    from .models import Guarantor, LoanHistory
    from transactions.models import LoanTransaction, CustomerAccountsSetup
    from accounting.services import post_journal, J
    from accounting.models import SaccoAccount

    preview = preview_guarantor_offload(loan)
    principal_balance = preview["principal_balance"]

    if principal_balance <= ZERO:
        raise ValueError(
            "The loan has no positive principal balance to distribute."
        )
    if preview["total_pool"] <= ZERO:
        raise ValueError(
            "No guarantors on file for this loan — nothing to offload."
        )
    if not preview["allocations"]:
        raise ValueError("No guarantor allocations could be computed.")

    # Look up / create the defaulter recovery product
    defaulter_product, _ = CustomerAccountsSetup.objects.get_or_create(
        account_code="L09",
        defaults={
            "account_name": "Guarantor Defaulter Loan Recovery",
            "acc_initials": "GDL",
            "account_type": "defaulter_loan",
            "interest_calc_method": "flat_rate",
            "is_loan_account": True,
            "max_repayment_period": 60,
        },
    )

    from accounting.services import GL as _GL
    loans_code = _GL.LOANS_RECEIVABLE
    loans_acc = SaccoAccount.objects.filter(account_code=loans_code).first()

    ts = int(datetime.now().timestamp())
    batch_ref = f"GDL{ts}"

    with transaction.atomic():
        allocations = preview["allocations"]

        for i, alloc in enumerate(allocations):
            if alloc.allocated_amount <= ZERO:
                continue

            # Look up the Guarantor row + Customer
            g_row = Guarantor.objects.filter(
                loan=loan,
                guarantor_cust__cust_no=alloc.guarantor_cust_no,
            ).select_related("guarantor_cust").first()
            if not g_row:
                logger.warning(
                    "Guarantor %s missing on loan %s at offload time",
                    alloc.guarantor_cust_no, loan.loan_no,
                )
                continue

            g_customer = g_row.guarantor_cust
            monthly = _q(alloc.allocated_amount / Decimal(new_loan_period))

            # 1. Create new defaulter loan for guarantor
            g_loan = LoanHistory.objects.create(
                customer=g_customer,
                loan_date=timezone.localdate(),
                principal=alloc.allocated_amount,
                installment=monthly,
                loan_type=defaulter_product,
                loan_period=new_loan_period,
                interest_rate=interest_rate,
                net_disbursed=alloc.allocated_amount,
                is_approved=True,
                approved_at=timezone.now(),
                approved_by=getattr(user, "username", "system"),
                is_disbursed=True,
                disbursed_at=timezone.now(),
                created_by=getattr(user, "username", "system"),
            )
            # Save meta into original_loan_summary so it's clearly flagged
            g_loan.original_loan_summary = json.dumps({
                "source": "guarantor_offload",
                "original_loan_no": loan.loan_no,
                "original_borrower_cust_no": str(loan.customer.cust_no),
                "original_borrower_name": loan.customer.full_name,
                "guarantee_amount": str(alloc.guarantee_amount),
                "percentage_of_pool": str(alloc.percentage),
                "allocated_amount": str(alloc.allocated_amount),
                "batch_ref": batch_ref,
                "reason": reason,
                "created_at": timezone.now().isoformat(),
            }, default=str)
            g_loan.save(update_fields=["original_loan_summary"])

            alloc.new_loan_no = g_loan.loan_no
            alloc.new_loan_id = g_loan.id

            # 2. Opening debit on new guarantor loan
            LoanTransaction.objects.create(
                cust_no=g_customer.cust_no,
                loan_id=g_loan.id,
                loan_no=g_loan.loan_no,
                loan_type=defaulter_product.account_type,
                tr_date=timezone.now(),
                tr_ref=f"{batch_ref}-OPEN-{i+1}",
                tr_desc=(
                    f"Defaulter recovery debt assigned from loan {loan.loan_no} "
                    f"({loan.customer.full_name})"
                ),
                debit_amount=alloc.allocated_amount,
                credit_amount=ZERO,
                created_by=getattr(user, "username", "system"),
            )

            # 3. Credit (repayment) on original defaulted loan
            LoanTransaction.objects.create(
                cust_no=loan.customer.cust_no,
                loan_id=loan.id,
                loan_no=loan.loan_no,
                loan_type=loan.loan_type.account_type,
                tr_date=timezone.now(),
                tr_ref=f"{batch_ref}-REPAY-{i+1}",
                tr_desc=(
                    f"Guarantor offload: {alloc.percentage:.2f}% assigned to "
                    f"{g_customer.full_name} ({g_customer.cust_no})"
                ),
                debit_amount=ZERO,
                credit_amount=alloc.allocated_amount,
                created_by=getattr(user, "username", "system"),
            )

            # 4. Post per-guarantor GL journal (DR new-loan, CR original-loan
            #    on the loans-receivable account — net zero movement, but
            #    the sub-ledger clearly shows the transfer per customer).
            if loans_acc:
                try:
                    post_journal(
                        reference=f"{batch_ref}-{i+1}",
                        description=(
                            f"Guarantor offload: {alloc.allocated_amount} "
                            f"transferred from {loan.customer.full_name} "
                            f"({loan.customer.cust_no}) → "
                            f"{g_customer.full_name} ({g_customer.cust_no})"
                        ),
                        lines=[
                            J(account_code=loans_acc.account_code,
                              debit=alloc.allocated_amount, credit=ZERO,
                              description=f"New defaulter loan {g_loan.loan_no}"),
                            J(account_code=loans_acc.account_code,
                              debit=ZERO, credit=alloc.allocated_amount,
                              description=f"Reduce original loan {loan.loan_no}"),
                        ],
                        user=user,
                        customer=g_customer,
                        external_reference=loan.loan_no,
                    )
                except Exception:
                    logger.exception(
                        "GL posting for guarantor allocation %s failed", i+1
                    )


        # 6. Refresh running stats for the original + all new loans
        try:
            from .utils import update_running_loans_stats
            # Refresh original borrower's loans and each guarantor's loans
            cust_nos = {str(loan.customer.cust_no)}
            for a in allocations:
                cust_nos.add(a.guarantor_cust_no)
            for cn in cust_nos:
                try:
                    update_running_loans_stats(cust_no=cn)
                except Exception:
                    logger.exception("stats refresh failed for %s", cn)
        except Exception:
            logger.exception("Failed to refresh running loan stats after offload")

        # 7. Audit log
        try:
            from accounting.audit_trail import log_transaction
            log_transaction(
                action="loan_repayment",
                reference=batch_ref,
                affected_accounts=[loans_code] if loans_acc else [],
                total_amount=preview["total_allocated"],
                user=user,
                request=request,
                description=(
                    f"Guarantor offload: loan {loan.loan_no} principal "
                    f"balance {principal_balance} distributed across "
                    f"{len(allocations)} guarantors. Reason: {reason}"
                ),
                customer_ref=str(loan.customer.cust_no),
                external_ref=loan.loan_no,
            )
        except Exception:
            logger.exception("Audit log for guarantor offload failed")

    return OffloadResult(
        original_loan_id=loan.id,
        original_loan_no=loan.loan_no,
        principal_balance_before=principal_balance,
        interest_balance_before=preview["interest_balance"],
        total_pool=preview["total_pool"],
        total_allocated=preview["total_allocated"],
        residual_balance=preview["residual_balance"],
        allocations=allocations,
        reference=batch_ref,
    )
