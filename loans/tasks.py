"""
loans/tasks.py — Scheduled loan interest charge for Django-Q2
=============================================================

Standalone function that can be scheduled from Django-Q2 Admin:

    Function:  loans.tasks.charge_loan_interest
    Args:      "normal_loan", "1.0"
    Cluster:   default  (or reports-worker for heavy batches)
    Schedule:  Cron → 0 0 5 * *   (5th of every month at midnight)

The function looks up the product by account_type slug (e.g. "normal_loan")
or by account_code (e.g. "L01"). The interest rate is a monthly percentage.
Calc method (reducing_balance / flat_rate) comes from CustomerAccountsSetup.

GL accounts are resolved from the product's FK linkage:
    DR  product.sacco_gl_account        (Loans Receivable — asset)
    CR  product.sacco_interest_account  (Interest Income — income)

TigerBeetle enforces the double-entry. If TB rejects, the whole batch
rolls back — no partial interest charges.
"""

import logging
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger("loans.tasks")

ZERO = Decimal("0.00")
TWO_PLACES = Decimal("0.01")


def _q(amount):
    return Decimal(str(amount or 0)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


# ════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT — callable by Django-Q2
# ════════════════════════════════════════════════════════════════════════

def charge_loan_interest(product_identifier: str, rate: str, target_date: str = ""):
    """
    Calculate and post monthly loan interest for a single product.

    Args:
        product_identifier: Either the account_type slug (e.g. "normal_loan")
                           or the account_code (e.g. "L01").
        rate:              Monthly interest rate as a string, e.g. "1.0" for 1%.
        target_date:       Optional ISO date "YYYY-MM-DD". Defaults to today.

    Returns:
        dict with batch_id, loans_charged, total_interest, or error detail.

    Django-Q2 Admin setup:
        Function:  loans.tasks.charge_loan_interest
        Args:      "normal_loan", "1.0"
                   OR: "L01", "1.0"
                   OR: "normal_loan", "1.0", "2026-08-05"
        Schedule:  Cron → 0 0 5 * *
    """
    from transactions.models import CustomerAccountsSetup
    from loans.models import (
        LoanHistory, LoanTransaction, RunningLoanStat,
        InterestChargeBatch, InterestChargeDraftItem,
    )
    from accounting.models import SaccoAccount
    from accounting.journal import journal_entry, leg
    from accounts.models import CustomUser

    rate_decimal = Decimal(str(rate))
    if rate_decimal <= 0 or rate_decimal > 10:
        return {"error": f"Rate {rate} looks wrong — expected monthly % like 1.0 or 1.5"}

    # ── Resolve product ───────────────────────────────────────────────
    product = (
        CustomerAccountsSetup.objects
        .filter(is_loan_account=True, is_active=True)
        .filter(
            # Try account_type first, fall back to account_code
            **{"account_type": product_identifier}
        )
        .select_related("sacco_gl_account", "sacco_interest_account")
        .first()
    )
    if not product:
        # Try by account_code (e.g. "L01")
        product = (
            CustomerAccountsSetup.objects
            .filter(
                is_loan_account=True, is_active=True,
                account_code=product_identifier,
            )
            .select_related("sacco_gl_account", "sacco_interest_account")
            .first()
        )
    if not product:
        msg = f"No active loan product found for '{product_identifier}'"
        logger.error(msg)
        return {"error": msg}

    loan_type_slug = product.account_type
    calc_method = product.interest_calc_method or "reducing_balance"

    # ── Resolve GL accounts from product FK linkage ───────────────────
    loans_recv_code = product.get_gl_code()
    interest_income_code = product.get_interest_gl_code()

    if not loans_recv_code or not interest_income_code:
        msg = (
            f"Product {product.account_code} ({product.account_name}) "
            f"missing GL linkage: gl_account={loans_recv_code}, "
            f"interest_account={interest_income_code}. "
            f"Set these in Admin → Customer Accounts Setup."
        )
        logger.error(msg)
        return {"error": msg}

    # ── Resolve target date ───────────────────────────────────────────
    if target_date:
        try:
            t_date = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            return {"error": f"Invalid date format: {target_date}. Use YYYY-MM-DD."}
    else:
        t_date = timezone.localdate()

    if t_date > timezone.localdate():
        return {"error": f"Cannot charge interest for future date {t_date}"}

    logger.info(
        f"[INT-CHARGE] Starting: product={product.account_code} "
        f"({loan_type_slug}), rate={rate_decimal}%, method={calc_method}, "
        f"date={t_date}"
    )

    # ── Duplicate guard ───────────────────────────────────────────────
    if InterestChargeBatch.objects.filter(
        loan_type=loan_type_slug, target_date=t_date, status="posted"
    ).exists():
        msg = f"Interest already posted for {loan_type_slug} on {t_date}"
        logger.warning(f"[INT-CHARGE] {msg}")
        return {"skipped": True, "reason": msg}

    # ── Find candidate loans (disbursed, not settled) ─────────────────
    candidates = _candidate_loans(loan_type_slug)
    if not candidates:
        msg = f"No active disbursed loans found for {loan_type_slug}"
        logger.info(f"[INT-CHARGE] {msg}")
        return {"skipped": True, "reason": msg}

    # ── Get as-at-date balances ───────────────────────────────────────
    balances = _balances_asof(candidates.keys(), t_date)
    if not balances:
        msg = f"No ledger balances found as at {t_date}"
        logger.info(f"[INT-CHARGE] {msg}")
        return {"skipped": True, "reason": msg}

    # ── Get customer names ────────────────────────────────────────────
    from customers.models import Customer
    cust_ids = [c["cust_no"] for c in candidates.values()]
    name_map = dict(
        Customer.objects.filter(cust_no__in=cust_ids)
        .values_list("cust_no", "full_name")
    )

    # ── Get or create a system user for the batch ─────────────────────
    system_user = CustomUser.objects.filter(is_superuser=True).first()
    if not system_user:
        system_user = CustomUser.objects.first()
    if not system_user:
        return {"error": "No user found in the system to attribute the batch to"}

    # ── Build draft + post in one atomic block ────────────────────────
    rate_factor = rate_decimal / Decimal("100")

    try:
        with transaction.atomic():
            batch = InterestChargeBatch.objects.create(
                loan_type=loan_type_slug,
                target_date=t_date,
                interest_rate=rate_decimal,
                calc_method_used=calc_method,
                created_by=system_user,
                status="draft",
            )

            items = []
            running_total = ZERO

            for loan_id, meta in candidates.items():
                balance = balances.get(loan_id, ZERO)
                if balance <= 0:
                    continue

                base = meta["principal"] if calc_method == "flat_rate" else balance
                interest = _q(base * rate_factor)
                if interest <= 0:
                    continue

                running_total += interest
                items.append(InterestChargeDraftItem(
                    batch=batch,
                    loan_id=loan_id,
                    loan_no=meta["loan_no"],
                    cust_no=meta["cust_no"],
                    customer_name=name_map.get(meta["cust_no"], "Unknown"),
                    approved_amount=meta["principal"],
                    outstanding_balance=balance,
                    calculated_interest=interest,
                ))

            if not items:
                logger.info(f"[INT-CHARGE] All loans have zero balance as at {t_date}")
                return {"skipped": True, "reason": "All loans fully repaid"}

            InterestChargeDraftItem.objects.bulk_create(items, batch_size=500)
            batch.total_interest = running_total
            batch.save(update_fields=["total_interest"])

            # ── Post: write LoanTransaction debits ────────────────────
            batch_ref = f"INT-{loan_type_slug[:3].upper()}-{timezone.now():%y%m%d%H%M}"
            method_tag = calc_method.split("_")[0].upper()
            desc = f"{t_date:%B %Y} Interest ({method_tag}) @{rate_decimal}%"

            loan_txns = [
                LoanTransaction(
                    cust_no=it.cust_no,
                    loan_id=it.loan_id,
                    loan_no=it.loan_no,
                    loan_type=loan_type_slug,
                    tr_date=t_date,
                    tr_ref=batch_ref,
                    tr_desc=desc,
                    debit_amount=it.calculated_interest,
                    credit_amount=0,
                    created_by="system:interest_charge",
                )
                for it in items
            ]
            LoanTransaction.objects.bulk_create(loan_txns, batch_size=500)

            # ── Update RunningLoanStat ────────────────────────────────
            stat_by_no = {
                s.loan_no: s
                for s in RunningLoanStat.objects.filter(
                    loan_no__in=[it.loan_no for it in items]
                )
            }
            to_update = []
            for it in items:
                s = stat_by_no.get(it.loan_no)
                if not s:
                    continue
                s.interest_balance = _q(s.interest_balance + it.calculated_interest)
                s.total_arrears = _q(s.total_arrears + it.calculated_interest)
                s.last_interest_charge = t_date
                to_update.append(s)
            if to_update:
                RunningLoanStat.objects.bulk_update(
                    to_update,
                    ["interest_balance", "total_arrears", "last_interest_charge"],
                    batch_size=500,
                )

            # ── GL journal: DR Loans Receivable, CR Interest Income ───
            # Uses the product's FK-linked GL accounts, not the hardcoded
            # ACCOUNT_MAP. TigerBeetle enforces the balance — if it rejects,
            # the entire atomic block rolls back.
            journal_entry(
                reference=batch_ref,
                description=(
                    f"{product.account_name} interest batch #{batch.id} "
                    f"{t_date:%b %Y} @{rate_decimal}%"
                ),
                created_by="system:interest_charge",
                legs=[
                    leg(loans_recv_code, debit=running_total),
                    leg(interest_income_code, credit=running_total),
                ],
            )

            # ── Mark batch as posted ──────────────────────────────────
            batch.status = "posted"
            batch.posted_at = timezone.now()
            batch.save(update_fields=["status", "posted_at"])

        result = {
            "batch_id": batch.id,
            "batch_ref": batch_ref,
            "product": f"{product.account_code} - {product.account_name}",
            "loan_type": loan_type_slug,
            "calc_method": calc_method,
            "rate_pct": str(rate_decimal),
            "target_date": str(t_date),
            "loans_charged": len(items),
            "stats_updated": len(to_update),
            "total_interest": str(running_total),
        }
        logger.info(
            f"[INT-CHARGE] SUCCESS: {len(items)} loans, "
            f"KES {running_total:,.2f} interest, batch #{batch.id} ({batch_ref})"
        )
        return result

    except Exception as e:
        logger.exception(f"[INT-CHARGE] FAILED: {e}")
        return {"error": str(e)}


# ════════════════════════════════════════════════════════════════════════
#  HELPER: find loans eligible for interest charge
# ════════════════════════════════════════════════════════════════════════

SETTLED_STATUSES = {"Settled"}


def _candidate_loans(loan_type_slug):
    """
    Disbursed loans for this product minus settled ones.
    Returns {loan_id: {"loan_no", "cust_no", "principal"}}
    """
    from loans.models import LoanHistory, RunningLoanStat

    history = list(
        LoanHistory.objects
        .filter(loan_type__account_type=loan_type_slug, is_disbursed=True)
        .values("id", "loan_no", "customer_id", "principal")
    )
    if not history:
        return {}

    loan_nos = [h["loan_no"] for h in history]
    settled_nos = set(
        RunningLoanStat.objects
        .filter(loan_no__in=loan_nos, loan_status__in=SETTLED_STATUSES)
        .values_list("loan_no", flat=True)
    )

    return {
        h["id"]: {
            "loan_no": h["loan_no"],
            "cust_no": h["customer_id"],
            "principal": _q(h["principal"]),
        }
        for h in history
        if h["loan_no"] not in settled_nos
    }


# ════════════════════════════════════════════════════════════════════════
#  HELPER: as-at-date balances from the ledger
# ════════════════════════════════════════════════════════════════════════

def _balances_asof(loan_ids, target_date):
    """
    Outstanding balance per loan AS AT target_date.
    Returns {loan_id: Decimal}.
    """
    from loans.models import LoanTransaction

    upper = timezone.make_aware(datetime.combine(target_date, time.max)) \
        if timezone.is_aware(timezone.now()) else datetime.combine(target_date, time.max)

    rows = (
        LoanTransaction.objects
        .filter(loan_id__in=loan_ids, tr_date__lte=upper)
        .values("loan_id")
        .annotate(debit=Sum("debit_amount"), credit=Sum("credit_amount"))
    )
    return {
        r["loan_id"]: _q((r["debit"] or 0) - (r["credit"] or 0))
        for r in rows
    }
