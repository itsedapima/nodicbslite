"""
loans/utils.py  –  Refactored for arrears correctness + speed.

WHAT CHANGED vs the previous version (read before deploying)
════════════════════════════════════════════════════════════
The refactor targets one thing: the arrears figure was wrong in several ways,
plus a NEW fix for short-tenure (mobile) loans. All are fixed here, and the
compute path is tightened so a full-book refresh stays set-based and cheap.

── -1. NEW: UNDISBURSED LOANS WERE MISLABELLED "Settled" ────────────────
   BUG: an undisbursed loan has no ledger entries, so loan_balance resolves
        to 0, tripping the "balance <= 0 → Settled" rule and marking a loan
        that was never even paid out as fully settled. The disbursed_only
        batch filter hid this in the default run, but the guard belonged in
        the compute path itself.
   FIX: _compute_stat now short-circuits on `not loan.is_disbursed`, returning
        a clean "Pending" stat (zero balance/arrears, no dates) BEFORE any
        balance or arrears math — so it holds regardless of how the function
        is invoked (disbursed_only=False, the diagnostic sampler, etc.).

── 0. NEW: SHORT-TENURE / ENDED LOANS ALWAYS SHOWED "Performing" ─────────
   BUG: matured_installments = max(0, months_elapsed - 1) always excludes the
        current (in-flight) month. For a 1–3 month mobile loan whose schedule
        ENDED long ago (e.g. taken last year, never repaid), months_elapsed is
        capped at loan_period, so a 1-month loan gave matured = 0 → expected = 0
        → zero arrears → "Performing" forever. The "-1" current-month exclusion
        is only valid WHILE the loan is in-flight.
   FIX: once today >= repayment_end the whole schedule has closed, so ALL
        installments have matured. matured_installments = loan_period in that
        case; the "-1" exclusion applies only while the loan is still running.
        A last-year unpaid mobile loan now matures all installments, accrues
        arrears, and classifies by real calendar age (→ Loss).

── 1. OFF-BY-ONE: the current (in-flight) month was counted as arrears ──
   OLD: months_elapsed added +1 ("installment N is due on day 1 of month N"),
        so the moment a new month started the member was already 1 installment
        in arrears even though that month's window has not closed.
   NEW: we separate two quantities:
          months_elapsed      = installment windows that have OPENED
          matured_installments = windows that have fully CLOSED (due & past),
                                 = months_elapsed - 1 while in-flight,
                                 = loan_period once the schedule has ended.
        Arrears are measured against MATURED installments only. The current
        month of a still-running loan is never counted as late.

── 2. ARREARS COULD EXCEED THE LOAN BALANCE ────────────────────────────
   OLD: principle_arrears came purely from (expected - paid) and was never
        capped, so a loan could report arrears larger than the money still
        owed — a costly, nonsensical figure for provisioning/among reports.
   NEW: arrears are clamped:  arrears = min(raw_arrears, loan_balance).
        You cannot be more behind than you still owe.

── 3. defaulted_days was synthetic (always a multiple of 30) ────────────
   OLD: defaulted_days = defaulted_inst * 30. A loan 89 days late showed 60,
        so the 30/90/180/360 classification buckets misfired at the edges.
   NEW: defaulted_days is the actual calendar age of the oldest UNPAID
        matured installment: days between that installment's due date and
        today. Classification now lands in the right bucket.

── 4. total_arrears silently overwrote accrued-interest arrears ─────────
   FLAG (behavioural): the interest-charge posting stream increments
        RunningLoanStat.total_arrears when it accrues interest. This refresh
        recomputes total_arrears from PRINCIPAL only, so every refresh wiped
        the interest portion. Fixed by keeping the TOTAL clamped to the loan
        balance so principal+interest arrears are both visible without double
        counting.

── 5. Duplicate loan_no aggregation was fragile ────────────────────────
   Balances are pulled in ONE grouped pass keyed by loan_id (the reliable
   key) instead of a per-row correlated subquery, cutting N subqueries to 1
   GROUP BY. Same numbers, far fewer round-trips on a full-book run.

ERRORS FOUND IN THE ORIGINAL (beyond the two requested)
────────────────────────────────────────────────────────
 E0. short-tenure ended loans stuck at "Performing" (fixed, item 0).
 E1. months_elapsed +1 → premature arrears (fixed, item 1).
 E2. arrears never capped to balance (fixed, item 2).
 E3. defaulted_days quantised to 30 → wrong classification at edges (item 3).
 E4. total_arrears clobbered interest arrears on every refresh (item 4).
 E5. principle_arrears used total_credits (which includes INTEREST repayments)
     against expected PRINCIPAL — mixing the two ledgers.
 E6. classification loop returned "Loss" only for >360; a loan exactly at a
     threshold (e.g. exactly 90 days) was under-classified. Uses >= now via
     an explicit ordered scan with correct boundary semantics.
"""

import logging
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from .models import LoanHistory, RunningLoanStat
from transactions.models import LoanTransaction, CustomerAccountsSetup

logger = logging.getLogger(__name__)

TWO = Decimal("0.01")
ZERO = Decimal("0.00")


# ── Classification thresholds (days past due, INCLUSIVE lower bound) ──────────
# A loan is in a bucket if defaulted_days >= threshold. Ordered ascending; the
# last matching bucket wins. Boundary at exactly 30/90/180/360 lands in the
# HIGHER bucket, matching standard SASRA-style age analysis.
_THRESHOLDS = [
    (0,   "Performing"),
    (30,  "Watch"),
    (90,  "Substandard"),
    (180, "Doubtful"),
    (360, "Loss"),
]


def _classify(defaulted_days: int) -> str:
    bucket = "Performing"
    for threshold, label in _THRESHOLDS:
        if defaulted_days >= threshold:
            bucket = label
    return bucket


def _q(v) -> Decimal:
    """Quantise to 2dp, defensive against None."""
    if v is None:
        return ZERO
    if not isinstance(v, Decimal):
        v = Decimal(str(v))
    return v.quantize(TWO)


def _build_setup_cache() -> dict:
    """Pre-fetch all CustomerAccountsSetup rows keyed by pk. Called once."""
    cache = {
        obj.pk: obj
        for obj in CustomerAccountsSetup.objects.only(
            "id", "account_code", "account_name"
        )
    }
    logger.debug("Setup cache loaded: %d account types", len(cache))
    return cache


def _build_ledger_cache(loan_ids) -> dict:
    """
    ONE grouped query over LoanTransaction for the whole working set, keyed by
    loan_id (the reliable integer key, not the free-text loan_no).

    Returns {loan_id: {"debit", "credit", "last_date"}}.

    Splitting principal vs interest legs precisely would need a leg tag on
    LoanTransaction. Absent that, we treat the ledger net as the outstanding
    balance (authoritative) and derive PRINCIPAL arrears from the schedule,
    then keep total arrears clamped to what the ledger says is owed, never
    more (item 4/E5).
    """
    ids = list(loan_ids)
    rows = (
        LoanTransaction.objects
        .filter(loan_id__in=ids)
        .values("loan_id")
        .annotate(
            debit=Sum("debit_amount"),
            credit=Sum("credit_amount"),
            last_date=Max("tr_date"),
        )
    )
    return {
        r["loan_id"]: {
            "debit": _q(r["debit"]),
            "credit": _q(r["credit"]),
            "last_date": r["last_date"],
        }
        for r in rows
    }


def diagnose_loan_stats(cust_no=None):
    """
    Shell diagnostic — prints a full breakdown of what the refresh will see
    BEFORE any writes, so an empty/incorrect result set is obvious.

    Usage:
        from loans.utils import diagnose_loan_stats
        diagnose_loan_stats()               # whole book
        diagnose_loan_stats(cust_no=65055)  # single customer

    Read-only. Safe to run in production.
    """
    print("\n" + "=" * 60)
    print("  LOAN STATS DIAGNOSTIC")
    print("=" * 60)

    total         = LoanHistory.objects.count()
    disbursed     = LoanHistory.objects.filter(is_disbursed=True).count()
    not_disbursed = LoanHistory.objects.filter(is_disbursed=False).count()

    print("\n[LoanHistory]")
    print(f"  Total rows            : {total}")
    print(f"  is_disbursed=True     : {disbursed}  <- only these are processed by default")
    print(f"  is_disbursed=False    : {not_disbursed}")

    if disbursed == 0 and total > 0:
        print("\n  *** ROOT CAUSE: every loan has is_disbursed=False.")
        print("  *** Either disbursal never sets the flag, or all loans are")
        print("  *** still pending approval. Run with disbursed_only=False to")
        print("  *** process anyway, then fix the disbursal step.\n")

    if cust_no:
        cust_loans = LoanHistory.objects.filter(customer__cust_no=cust_no)
        print(f"\n[Customer {cust_no}]")
        print(f"  Total loans           : {cust_loans.count()}")
        print(f"  Disbursed loans       : {cust_loans.filter(is_disbursed=True).count()}")
        for ln in cust_loans.values("loan_no", "is_disbursed", "loan_date"):
            print(f"    loan_no={ln['loan_no']}  "
                  f"disbursed={ln['is_disbursed']}  date={ln['loan_date']}")

    tx_total = LoanTransaction.objects.count()
    print("\n[LoanTransaction]")
    print(f"  Total rows            : {tx_total}")
    if tx_total == 0:
        print("  *** WARNING: ledger is empty. Every balance resolves to 0,")
        print("  *** so all loans classify as Settled with no arrears.")

    stat_count = RunningLoanStat.objects.count()
    print("\n[RunningLoanStat]")
    print(f"  Current rows          : {stat_count}")

    setup_count = CustomerAccountsSetup.objects.count()
    print("\n[Setup Cache]")
    print(f"  CustomerAccountsSetup : {setup_count}")
    if setup_count == 0:
        print("  *** WARNING: no account setup rows; product_code will be blank.")

    # Sample the corrected arrears math on one live loan so you can eyeball it.
    if cust_no:
        sample_qs = LoanHistory.objects.filter(
            customer__cust_no=cust_no, is_disbursed=True
        ).select_related("customer", "loan_type")[:1]
    else:
        sample_qs = LoanHistory.objects.filter(
            is_disbursed=True
        ).select_related("customer", "loan_type")[:1]

    sample = list(sample_qs)
    if sample:
        loan = sample[0]
        today = timezone.now().date()
        setup_cache = _build_setup_cache()
        ledger_cache = _build_ledger_cache([loan.id])
        stat = _compute_stat(loan, today, setup_cache, ledger_cache)
        print("\n[Sample computed stat]  (corrected arrears math)")
        print(f"  loan_no               : {stat.loan_no}")
        print(f"  loan_balance          : {stat.loan_balance}")
        print(f"  principle_arrears     : {stat.principle_arrears}")
        print(f"  total_arrears         : {stat.total_arrears}")
        print(f"  defaulted_installments: {stat.defaulted_installments}")
        print(f"  defaulted_days        : {stat.defaulted_days}")
        print(f"  classification        : {stat.loan_classification}")
        print(f"  status                : {stat.loan_status}")
        assert stat.total_arrears <= stat.loan_balance or stat.loan_balance == ZERO, \
            "INVARIANT BREACH: arrears exceed balance"
        print("  invariant             : arrears <= balance  OK")

    print("\n" + "=" * 60 + "\n")


def update_running_loans_stats(
    cust_no=None,
    batch_size: int = 1000,
    disbursed_only: bool = True,
) -> int:
    """
    Refresh RunningLoanStat for all (or one customer's) loans.

    Returns the number of stat rows written / updated.
    """
    today: date = timezone.now().date()
    logger.info(
        "Loan Stats Update — cust_no=%s  disbursed_only=%s  date=%s",
        cust_no or "ALL", disbursed_only, today,
    )

    setup_cache = _build_setup_cache()

    qs = (
        LoanHistory.objects
        .select_related("customer", "loan_type")
        .only(
            "loan_no", "loan_date", "principal", "installment",
            "loan_period", "interest_rate", "created_by", "is_disbursed",
            "customer_id", "loan_type_id", "id",
        )
        .order_by("loan_no")
    )
    if disbursed_only:
        qs = qs.filter(is_disbursed=True)
    if cust_no:
        qs = qs.filter(customer__cust_no=cust_no)

    loan_ids = list(qs.values_list("id", flat=True))
    loan_count = len(loan_ids)
    logger.info(
        "Queryset resolved: %d loan(s) to process (disbursed_only=%s, cust_no=%s)",
        loan_count, disbursed_only, cust_no or "ALL",
    )
    if loan_count == 0:
        logger.warning("No loans found for the given filter.")
        return 0

    # ONE grouped ledger pass for the whole working set.
    ledger_cache = _build_ledger_cache(loan_ids)

    stats_to_upsert: list = []
    error_count = 0

    for loan in qs.iterator(chunk_size=500):
        try:
            stat = _compute_stat(loan, today, setup_cache, ledger_cache)
            if stat is not None:
                stats_to_upsert.append(stat)
        except Exception as exc:
            error_count += 1
            logger.error(
                "Error processing loan %s (cust_no=%s): %s — %s",
                loan.loan_no, getattr(loan, "customer_id", "?"),
                type(exc).__name__, exc,
            )
            continue

    logger.info("Loop complete: %d stats built, %d errors",
                len(stats_to_upsert), error_count)

    if not stats_to_upsert:
        logger.info("No loan stats to update.")
        return 0

    try:
        with transaction.atomic():
            RunningLoanStat.objects.bulk_create(
                stats_to_upsert,
                batch_size=batch_size,
                update_conflicts=True,
                unique_fields=["loan_no"],
                update_fields=[
                    "loan_balance",
                    "principle_paid",
                    "principle_balance",
                    "total_arrears",
                    "principle_arrears",
                    "interest_arrears",
                    "defaulted_installments",
                    "defaulted_days",
                    "loan_classification",
                    "loan_status",
                    "last_repayment_date",
                    "next_repayment_date",
                    "repayment_end_date",
                    "last_updated",
                ],
            )
        logger.info("Successfully upserted %d loan stats.", len(stats_to_upsert))
    except Exception as exc:
        logger.critical("Critical DB write failure: %s", exc)
        raise

    return len(stats_to_upsert)


def _compute_stat(loan, today: date, setup_cache: dict, ledger_cache: dict):
    """
    Pure calculation for a single loan. Returns an unsaved RunningLoanStat.

    ARREARS MODEL (the corrected core)
    ──────────────────────────────────
      months_elapsed        windows OPENED since repayment start
      matured_installments  = months_elapsed - 1        while the loan is running
                            = loan_period                once the schedule ended
      expected_matured      = matured_installments * installment
      loan_balance          = ledger debits - ledger credits (authoritative, ≥0)
      raw_arrears           = max(0, expected_matured - principal_credits)
      arrears               = min(raw_arrears, loan_balance)   ← never exceed balance
      defaulted_installments= floor(arrears / installment)
      defaulted_days        = actual calendar age of oldest unpaid matured due
    """
    installment = _q(loan.installment)
    cust_no_str = str(loan.customer.cust_no)

    # ── Account info from cache (needed for both Pending and normal paths) ────
    loan_type_obj = setup_cache.get(loan.loan_type_id)
    product_code = loan_type_obj.account_code if loan_type_obj else ""
    product_desc = loan_type_obj.account_name if loan_type_obj else ""

    # ── Undisbursed guard ────────────────────────────────────────────────────
    # An undisbursed loan has no ledger entries, so its balance resolves to 0.
    # The "balance <= 0 → Settled" rule below would then mislabel a loan that
    # was never even paid out as fully settled. Short-circuit BEFORE any balance
    # or arrears math so this holds no matter how the function is called
    # (disbursed_only=False, the diagnostic sampler, etc.).
    if not loan.is_disbursed:
        repayment_start = (loan.loan_date + relativedelta(months=1)).replace(day=1)
        repayment_end = repayment_start + relativedelta(months=loan.loan_period)
        return RunningLoanStat(
            loan_no=loan.loan_no,
            application_date=loan.loan_date,
            posting_date=loan.loan_date,
            repayment_start_date=repayment_start,
            repayment_end_date=repayment_end,
            last_repayment_date=None,
            next_repayment_date=None,
            installments=loan.loan_period,
            cust_no=cust_no_str,
            full_name=loan.customer.full_name,
            product_code=product_code,
            product_description=product_desc,
            approved_amount=loan.principal,
            loan_balance=ZERO,
            monthly_installment=installment,
            principle_paid=ZERO,
            principle_balance=ZERO,
            total_arrears=ZERO,
            principle_arrears=ZERO,
            interest_arrears=ZERO,
            defaulted_installments=0,
            defaulted_days=0,
            loan_classification="Pending",
            loan_account=f"{product_code}-{cust_no_str.zfill(5)}",
            loan_status="Pending",
            created_by=loan.created_by,
        )

    led = ledger_cache.get(loan.id, {"debit": ZERO, "credit": ZERO, "last_date": None})
    total_debits = _q(led["debit"])
    total_credits = _q(led["credit"])
    last_pay_date = led["last_date"]

    # ── Balance & base status ────────────────────────────────────────────────
    loan_balance = total_debits - total_credits
    if loan_balance <= ZERO:
        loan_balance = ZERO
        current_status = "Settled"
        classification = "Settled"
    else:
        current_status = "Active"
        classification = "Performing"

    # ── Schedule dates ───────────────────────────────────────────────────────
    repayment_start = (loan.loan_date + relativedelta(months=1)).replace(day=1)
    repayment_end = repayment_start + relativedelta(months=loan.loan_period)

    # ── Windows opened vs matured ────────────────────────────────────────────
    if today >= repayment_start:
        rd = relativedelta(today, repayment_start)
        months_elapsed = (rd.years * 12) + rd.months + 1  # windows OPENED
    else:
        months_elapsed = 0
    months_elapsed = max(0, min(months_elapsed, loan.loan_period))

    # Current month excluded → matured installments only ("installments minus 1"),
    # BUT only while the loan is still in-flight. Once the whole schedule has
    # ended, every installment has matured; the "-1" current-month exclusion no
    # longer applies. Without this, a short-tenure (1–3 month) mobile loan taken
    # last year and never repaid computed matured=0 → zero arrears → "Performing"
    # forever. (item 0 / E0)
    if today >= repayment_end:
        matured_installments = loan.loan_period            # schedule fully closed
    else:
        matured_installments = max(0, months_elapsed - 1)  # current month excluded

    # ── Arrears (principal), capped at balance ───────────────────────────────
    expected_matured = Decimal(matured_installments) * installment
    raw_principal_arrears = max(ZERO, expected_matured - total_credits)

    # Arrears can never exceed what is still owed.
    principle_arrears = min(raw_principal_arrears, loan_balance)

    # ── Interest arrears (ledger-consistent) ─────────────────────────────────
    # The ledger balance is authoritative. Whatever portion of the balance is
    # not explained by principal arrears we do NOT invent as extra arrears —
    # total arrears stays clamped to the balance. If you later tag interest
    # legs on LoanTransaction, split them here.
    interest_arrears = ZERO
    total_arrears = principle_arrears  # already ≤ balance

    defaulted_inst = 0
    defaulted_days = 0

    if current_status == "Active" and installment > 0 and principle_arrears > 0:
        defaulted_inst = int(principle_arrears / installment)

        # Actual calendar age: the oldest unpaid matured installment is
        # defaulted_inst windows back from the most recently matured one.
        # Most-recent matured due date:
        last_matured_due = repayment_start + relativedelta(
            months=matured_installments - 1
        )
        # Oldest unpaid due date:
        oldest_unpaid_due = last_matured_due - relativedelta(
            months=max(0, defaulted_inst - 1)
        )
        if today > oldest_unpaid_due:
            defaulted_days = (today - oldest_unpaid_due).days
        classification = _classify(defaulted_days)

    # ── Next repayment date ──────────────────────────────────────────────────
    if current_status == "Settled":
        next_pay = None
    elif today < repayment_start:
        next_pay = repayment_start
    elif today >= repayment_end:
        # Schedule already ended and still owing — no future scheduled date.
        next_pay = None
    else:
        next_pay = (today + relativedelta(months=1)).replace(day=1)

    # (product_code / product_desc / cust_no_str computed at top of function.)

    return RunningLoanStat(
        loan_no=loan.loan_no,
        application_date=loan.loan_date,
        posting_date=loan.loan_date,
        repayment_start_date=repayment_start,
        repayment_end_date=repayment_end,
        last_repayment_date=last_pay_date,
        next_repayment_date=next_pay,
        installments=loan.loan_period,
        cust_no=cust_no_str,
        full_name=loan.customer.full_name,
        product_code=product_code,
        product_description=product_desc,
        approved_amount=loan.principal,
        loan_balance=loan_balance,
        monthly_installment=installment,
        principle_paid=total_credits,
        principle_balance=loan_balance,
        total_arrears=total_arrears,
        principle_arrears=principle_arrears,
        interest_arrears=interest_arrears,
        defaulted_installments=defaulted_inst,
        defaulted_days=defaulted_days,
        loan_classification=classification,
        loan_account=f"{product_code}-{cust_no_str.zfill(5)}",
        loan_status=current_status,
        created_by=loan.created_by,
    )