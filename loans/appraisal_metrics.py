"""
loans/appraisal_metrics.py
==========================
The analytical engine that powers the loan appraisal form and its PDF.

Every metric here is derived once, from settings-driven windows, and
returned as a small dataclass-like dict so the view/template code stays
declarative. Nothing here mutates state; the module is safe to call
from views, from the PDF renderer, or from tests.

Settings consumed (all defined in nodicbs/settings.py):
    LUMPSUM_BASELINE                — e.g. Decimal("2.5")
    LUMPSUM_AVERAGE_PERIOD          — months, default 6
    DEPOSITS_SUMMARY_PERIOD         — months, default 6
    REPAYMENTS_SUMMARY_PERIOD       — months, default 6
    DEFAULTER_HISTORY_LOANS_SHOWN   — count, default 3
    DEFAULTER_MIN_DAYS              — days, default 1

Public helpers:
    compute_lumpsum_metrics(customer, loan)
    compute_deposits_summary(customer, loan, base_deposit_types=None)
    compute_repayments_summary(customer, loan)
    fetch_defaulter_history(customer)
    compute_eligibility(customer, loan)
    build_appraisal_verdicts(customer, loan, **balances)
    live_guarantor_metrics(customer, exclude_loan=None)

Each helper is defensive: missing FK / null aggregate results become
Decimal('0.00') instead of raising.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db.models import Sum

ZERO = Decimal('0.00')


# ═══════════════════════════════════════════════════════════════════════════
#  Small utilities
# ═══════════════════════════════════════════════════════════════════════════

def _dec(value) -> Decimal:
    """Coerce anything to Decimal safely; None / '' become 0."""
    if value is None or value == '':
        return ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return ZERO


def _window(loan, months: int) -> tuple:
    """
    Return (start, end_exclusive) covering `months` calendar months ending
    in the loan.loan_date month. Anchor to day=1 of the loan month.
    """
    anchor = loan.loan_date.replace(day=1)
    start  = anchor - relativedelta(months=months - 1)
    end    = anchor + relativedelta(months=1)  # exclusive: first day of next month
    return start, end


# ═══════════════════════════════════════════════════════════════════════════
#  1. LUMPSUM DETECTION
# ═══════════════════════════════════════════════════════════════════════════

def compute_lumpsum_metrics(customer, loan) -> dict:
    """
    Look at the customer's monthly SavingsTransaction net-credit totals
    over LUMPSUM_AVERAGE_PERIOD months ending at loan.loan_date. A month
    whose net credit is ≥ average * LUMPSUM_BASELINE is a "lump sum" and
    the FULL month amount (not the excess) is recorded.

    Returns
    -------
        {
          "average":         Decimal — mean monthly net credit,
          "baseline":        Decimal — the threshold applied,
          "months_window":   int,
          "monthly_totals":  [ {"month": "YYYY-MM", "amount": Decimal, "is_lumpsum": bool}, ... ],
          "lumpsums":        [ {"month": "YYYY-MM", "amount": Decimal}, ... ],
          "total_lumpsum":   Decimal,
          "has_lumpsum":     bool,
        }
    """
    from django.db.models.functions import TruncMonth
    from transactions.models import SavingsTransaction

    months  = int(getattr(settings, 'LUMPSUM_AVERAGE_PERIOD', 6))
    factor  = _dec(getattr(settings, 'LUMPSUM_BASELINE', Decimal('2.5')))
    start, end = _window(loan, months)

    monthly = (
        SavingsTransaction.objects
        .filter(cust_no=customer.cust_no,
                saving_type='savings_deposit',
                tr_date__gte=start, tr_date__lt=end)
        .annotate(month=TruncMonth('tr_date'))
        .values('month')
        .annotate(net=Sum('credit_amount') - Sum('debit_amount'))
        .order_by('month')
    )

    monthly_map = {
        row['month'].strftime('%Y-%m'): _dec(row['net'])
        for row in monthly if row['month']
    }

    # Build a dense list (zero-fill months with no activity)
    dense = []
    cursor = start
    while cursor < end:
        key = cursor.strftime('%Y-%m')
        dense.append((key, monthly_map.get(key, ZERO)))
        cursor += relativedelta(months=1)

    non_zero = [amt for _, amt in dense if amt > 0]
    average  = (sum(non_zero) / len(non_zero)) if non_zero else ZERO
    threshold = (average * factor).quantize(Decimal('0.01'))

    monthly_totals, lumpsums = [], []
    for key, amt in dense:
        is_lump = bool(amt > 0 and threshold > 0 and amt >= threshold)
        monthly_totals.append({'month': key, 'amount': amt, 'is_lumpsum': is_lump})
        if is_lump:
            lumpsums.append({'month': key, 'amount': amt})

    return {
        'average':        average.quantize(Decimal('0.01')),
        'baseline':       factor,
        'threshold':      threshold,
        'months_window':  months,
        'monthly_totals': monthly_totals,
        'lumpsums':       lumpsums,
        'total_lumpsum':  sum((l['amount'] for l in lumpsums), ZERO),
        'has_lumpsum':    bool(lumpsums),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  2. DEPOSITS SUMMARY (last N months, by base-deposit product)
# ═══════════════════════════════════════════════════════════════════════════

def _resolve_base_deposit_types(loan) -> list:
    """
    Return the list of SavingsTransaction.saving_type strings that are
    'base deposits' for the applied loan product. If the loan product
    has base_deposits configured (M2M on CustomerAccountsSetup), use
    those; else fall back to the classic 'savings_deposit'.
    """
    lt = getattr(loan, 'loan_type', None)
    if lt and hasattr(lt, 'base_deposits'):
        codes = list(lt.base_deposits.values_list('account_type', flat=True))
        if codes:
            return codes
    return ['savings_deposit']


def compute_deposits_summary(customer, loan, base_deposit_types=None) -> dict:
    """
    Aggregate base-deposit net credits over DEPOSITS_SUMMARY_PERIOD
    months ending at loan.loan_date. Broken out per-product so the form
    can render one row per base deposit type.
    """
    from transactions.models import SavingsTransaction, CustomerAccountsSetup

    months  = int(getattr(settings, 'DEPOSITS_SUMMARY_PERIOD', 6))
    start, end = _window(loan, months)
    types = base_deposit_types or _resolve_base_deposit_types(loan)

    label_map = {
        s.account_type: f"{s.account_code} · {s.account_name}"
        for s in CustomerAccountsSetup.objects.filter(account_type__in=types)
    }

    rows = (
        SavingsTransaction.objects
        .filter(cust_no=customer.cust_no,
                saving_type__in=types,
                tr_date__gte=start, tr_date__lt=end)
        .values('saving_type')
        .annotate(net=Sum('credit_amount') - Sum('debit_amount'),
                  credits=Sum('credit_amount'),
                  debits=Sum('debit_amount'))
    )

    per_product, total_net = [], ZERO
    seen = set()
    for row in rows:
        stype = row['saving_type']
        seen.add(stype)
        net = _dec(row['net'])
        total_net += net
        per_product.append({
            'saving_type': stype,
            'label':       label_map.get(stype, stype.replace('_', ' ').title()),
            'credits':     _dec(row['credits']),
            'debits':      _dec(row['debits']),
            'net':         net,
        })

    # include products with zero activity so the table is complete
    for stype in types:
        if stype not in seen:
            per_product.append({
                'saving_type': stype,
                'label':       label_map.get(stype, stype.replace('_', ' ').title()),
                'credits':     ZERO,
                'debits':      ZERO,
                'net':         ZERO,
            })

    return {
        'months_window':  months,
        'window_start':   start,
        'window_end':     end,
        'per_product':    per_product,
        'total_net':      total_net,
        'monthly_average': (total_net / months).quantize(Decimal('0.01')) if months else ZERO,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  3. REPAYMENTS SUMMARY (last N months)
# ═══════════════════════════════════════════════════════════════════════════

def compute_repayments_summary(customer, loan) -> dict:
    """
    All loan-account CREDITS (repayments) posted for this customer over
    REPAYMENTS_SUMMARY_PERIOD months ending at loan.loan_date, split
    per prior loan.
    """
    from transactions.models import LoanTransaction

    months  = int(getattr(settings, 'REPAYMENTS_SUMMARY_PERIOD', 6))
    start, end = _window(loan, months)

    rows = (
        LoanTransaction.objects
        .filter(cust_no=customer.cust_no,
                tr_date__gte=start, tr_date__lt=end,
                credit_amount__gt=0)
        .exclude(loan_id=loan.id)
        .values('loan_no', 'loan_type')
        .annotate(total=Sum('credit_amount'))
        .order_by('-total')
    )

    per_loan = [
        {'loan_no': r['loan_no'] or 'UNKNOWN',
         'loan_type': (r['loan_type'] or 'LOAN').upper(),
         'total_repaid': _dec(r['total'])}
        for r in rows
    ]
    total = sum((r['total_repaid'] for r in per_loan), ZERO)

    return {
        'months_window':   months,
        'window_start':    start,
        'window_end':      end,
        'per_loan':        per_loan,
        'total_repaid':    total,
        'monthly_average': (total / months).quantize(Decimal('0.01')) if months else ZERO,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  4. DEFAULTER HISTORY
# ═══════════════════════════════════════════════════════════════════════════

def fetch_defaulter_history(customer) -> dict:
    """
    Query LoanDefaulterHistory for this customer. Returns the top N
    distinct loans by worst-ever arrears — driven by
    DEFAULTER_HISTORY_LOANS_SHOWN.
    """
    from loans.models import LoanDefaulterHistory

    limit = int(getattr(settings, 'DEFAULTER_HISTORY_LOANS_SHOWN', 3))

    qs = (LoanDefaulterHistory.objects
          .filter(cust_no=customer.cust_no)
          .order_by('-loan_arrears', '-defaulted_days'))
    total = qs.count()
    rows = list(qs[:limit].values(
        'loan_no', 'product_name', 'product_code',
        'first_default_date', 'last_seen_date',
        'loan_arrears', 'defaulted_days',
        'loan_classification', 'is_resolved',
    ))

    return {
        'has_defaulted_before': total > 0,
        'total_defaulted_loans': total,
        'top_loans': rows,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  5. ELIGIBILITY (base_deposits × loan_multiplier)
# ═══════════════════════════════════════════════════════════════════════════

def compute_eligibility(customer, loan) -> dict:
    """
    Sum the customer's CURRENT balance across the loan product's
    base_deposits, then multiply each product's balance by its
    loan_multiplier to derive the total eligible ceiling.

    Falls back to `savings_balance * 3` if the loan product has no
    base_deposits configured, preserving the historical rule.
    """
    from transactions.models import SavingsTransaction, CustomerAccountsSetup

    lt = getattr(loan, 'loan_type', None)
    base_qs = (lt.base_deposits.all() if lt and hasattr(lt, 'base_deposits')
               else CustomerAccountsSetup.objects.none())
    base_products = list(base_qs.values('account_type', 'account_code',
                                        'account_name', 'loan_multiplier'))

    detail, total_balance, eligible_ceiling = [], ZERO, ZERO

    if base_products:
        # Per-product balance × multiplier
        for prod in base_products:
            bal = _dec(
                SavingsTransaction.objects
                .filter(cust_no=customer.cust_no,
                        saving_type=prod['account_type'])
                .aggregate(b=Sum('credit_amount') - Sum('debit_amount'))['b']
            )
            mult = _dec(prod['loan_multiplier']) or Decimal('1')
            capacity = bal * mult
            total_balance   += bal
            eligible_ceiling += capacity
            detail.append({
                'label':        f"{prod['account_code']} · {prod['account_name']}",
                'saving_type':  prod['account_type'],
                'balance':      bal,
                'multiplier':   mult,
                'capacity':     capacity,
            })
    else:
        # Legacy fallback — total savings × 3
        bal = _dec(
            SavingsTransaction.objects
            .filter(cust_no=customer.cust_no, saving_type='savings_deposit')
            .aggregate(b=Sum('credit_amount') - Sum('debit_amount'))['b']
        )
        total_balance    = bal
        eligible_ceiling = bal * Decimal('3')
        detail.append({
            'label':      'Total Savings (legacy 3× rule)',
            'saving_type': 'savings_deposit',
            'balance':     bal,
            'multiplier':  Decimal('3.00'),
            'capacity':    eligible_ceiling,
        })

    principal = _dec(loan.principal)
    passes    = principal <= eligible_ceiling
    coverage_pct = ((eligible_ceiling / principal) * Decimal('100')).quantize(Decimal('0.01')) \
                   if principal > 0 else ZERO

    return {
        'per_product':      detail,
        'total_balance':    total_balance,
        'eligible_ceiling': eligible_ceiling,
        'principal':        principal,
        'multiplier_pass':  passes,
        'coverage_pct':     coverage_pct,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  6. GUARANTOR LIVE METRICS
# ═══════════════════════════════════════════════════════════════════════════

def live_guarantor_metrics(customer, exclude_loan=None) -> dict:
    """
    Live picture used by the add-guarantor form:

      * total deposits balance across all savings products,
      * guarantee ceiling (Σ balance × guarantee_multiplier per product),
      * committed guarantees currently outstanding — computed against
        RunningLoanStat (loan_balance > 0) so cleared loans don't
        double-count,
      * remaining headroom.
    """
    from loans.models import Guarantor, RunningLoanStat
    from transactions.models import SavingsTransaction, CustomerAccountsSetup

    # Total savings & per-product breakdown
    per_product, total_balance, guarantee_ceiling = [], ZERO, ZERO
    savings_products = CustomerAccountsSetup.objects.filter(
        is_loan_account=False, is_active=True,
    )
    for prod in savings_products:
        bal = _dec(
            SavingsTransaction.objects
            .filter(cust_no=customer.cust_no, saving_type=prod.account_type)
            .aggregate(b=Sum('credit_amount') - Sum('debit_amount'))['b']
        )
        if bal == 0:
            continue
        mult = _dec(prod.guarantee_multiplier) or Decimal('1')
        capacity = bal * mult
        total_balance     += bal
        guarantee_ceiling += capacity
        per_product.append({
            'label':      f"{prod.account_code} · {prod.account_name}",
            'balance':    bal,
            'multiplier': mult,
            'capacity':   capacity,
        })

    # Committed guarantees on OPEN loans only (RunningLoanStat.loan_balance > 0)
    # → cleared loans no longer occupy headroom.
    open_loan_nos = list(
        RunningLoanStat.objects
        .filter(loan_balance__gt=0)
        .values_list('loan_no', flat=True)
    )

    committed_qs = Guarantor.objects.filter(
        guarantor_cust=customer,
        loan__loan_no__in=open_loan_nos,
    )
    if exclude_loan is not None:
        committed_qs = committed_qs.exclude(loan=exclude_loan)

    committed = _dec(committed_qs.aggregate(total=Sum('amount'))['total'])
    remaining = guarantee_ceiling - committed

    return {
        'per_product':       per_product,
        'total_balance':     total_balance,
        'guarantee_ceiling': guarantee_ceiling,
        'committed':         committed,
        'remaining':         remaining,
        'has_capacity':      remaining > 0,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  7. VERDICT AGGREGATOR
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Verdict:
    label: str      # "Lumpsum Deposits", "Multiplier Pass", ...
    passed: bool
    detail: str
    severity: str = 'info'   # 'success' | 'warning' | 'danger' | 'info'


def _sev(passed, warn_ok=False):
    return 'success' if passed else ('warning' if warn_ok else 'danger')


def build_appraisal_verdicts(customer, loan, *,
                             lumpsum, deposits, repayments,
                             defaulter, eligibility,
                             total_outstanding_debt,
                             total_security,
                             repayment_score) -> list:
    """
    Merge every metric into a compact, template-friendly list of Verdict
    dicts. Order = the order they should appear on the appraisal form.
    """
    from transactions.models import SavingsTransaction, CustomerAccountsSetup

    verdicts: list[Verdict] = []

    # ── 0a. Base Deposits Balance ≥ min_balance ──────────────────────
    # For each base-deposit product linked to this loan, check that the
    # member's current balance meets the product's min_balance.
    base_deposit_types = _resolve_base_deposit_types(loan)
    base_deposit_setups = list(
        CustomerAccountsSetup.objects.filter(
            account_type__in=base_deposit_types,
            is_loan_account=False,
        )
    )
    for setup in base_deposit_setups:
        bal = _dec(
            SavingsTransaction.objects
            .filter(cust_no=customer.cust_no, saving_type=setup.account_type)
            .aggregate(b=Sum('credit_amount') - Sum('debit_amount'))['b']
        )
        min_bal = _dec(setup.min_balance)
        passes = bal >= min_bal
        verdicts.append(Verdict(
            label=f"Base Deposit Balance — {setup.account_code} · {setup.account_name}",
            passed=passes,
            detail=(f"Balance {bal:,.2f} vs minimum {min_bal:,.2f}"
                    + ("" if passes else f" — shortfall {(min_bal - bal):,.2f}")),
            severity=_sev(passes),
        ))

    # ── 0b. Share Capital Balance ≥ min_balance ──────────────────────
    sc_setup = CustomerAccountsSetup.objects.filter(
        account_type='share_capital',
        is_loan_account=False,
    ).first()
    if sc_setup:
        sc_bal = _dec(
            SavingsTransaction.objects
            .filter(cust_no=customer.cust_no, saving_type='share_capital')
            .aggregate(b=Sum('credit_amount') - Sum('debit_amount'))['b']
        )
        sc_min = _dec(sc_setup.min_balance)
        sc_pass = sc_bal >= sc_min
        verdicts.append(Verdict(
            label=f"Share Capital Balance — {sc_setup.account_code} · {sc_setup.account_name}",
            passed=sc_pass,
            detail=(f"Balance {sc_bal:,.2f} vs minimum {sc_min:,.2f}"
                    + ("" if sc_pass else f" — shortfall {(sc_min - sc_bal):,.2f}")),
            severity=_sev(sc_pass),
        ))

    # ── 0c. Base Deposits (6 months) ≥ min_balance × 6 ──────────────
    # The 6-month net deposit total for each base deposit product must
    # be at least the product's min_balance × 6 (i.e. member has been
    # saving at least the minimum each month over the review window).
    months_window = deposits.get('months_window', 6)
    for setup in base_deposit_setups:
        required = _dec(setup.min_balance) * months_window
        # Find the matching row from the deposits summary
        actual = ZERO
        for row in deposits.get('per_product', []):
            if row.get('saving_type') == setup.account_type:
                actual = _dec(row.get('net', ZERO))
                break
        passes_6m = actual >= required
        verdicts.append(Verdict(
            label=f"Base Deposits ({months_window}m) — {setup.account_code} · {setup.account_name}",
            passed=passes_6m,
            detail=(f"Net deposits {actual:,.2f} vs required "
                    f"{required:,.2f} (min {_dec(setup.min_balance):,.2f} × {months_window})"
                    + ("" if passes_6m else f" — shortfall {(required - actual):,.2f}")),
            severity=_sev(passes_6m),
        ))

    # 1. Multiplier pass
    verdicts.append(Verdict(
        label="Multiplier Pass (Eligibility Ceiling)",
        passed=eligibility['multiplier_pass'],
        detail=(f"Principal {loan.principal:,.2f} vs eligible ceiling "
                f"{eligibility['eligible_ceiling']:,.2f} "
                f"({eligibility['coverage_pct']}% coverage)"),
        severity=_sev(eligibility['multiplier_pass']),
    ))

    # 2. Lumpsum deposits — a lumpsum triggers a WARNING (not fatal): the
    #    loan product may attach a lumpsum penalty charge that fines the
    #    applicant that amount.
    verdicts.append(Verdict(
        label="Lumpsum Deposit(s) Detected",
        passed=not lumpsum['has_lumpsum'],
        detail=(
            (f"{len(lumpsum['lumpsums'])} month(s) flagged · total "
             f"{lumpsum['total_lumpsum']:,.2f} · threshold "
             f"{lumpsum['threshold']:,.2f} "
             f"({lumpsum['baseline']}× avg {lumpsum['average']:,.2f})")
            if lumpsum['has_lumpsum'] else
            f"No lumpsum in last {lumpsum['months_window']} months (avg "
            f"{lumpsum['average']:,.2f}, threshold {lumpsum['threshold']:,.2f})"
        ),
        severity=('warning' if lumpsum['has_lumpsum'] else 'success'),
    ))

    # 3. Base deposits activity
    verdicts.append(Verdict(
        label=f"Base Deposits ({deposits['months_window']}m)",
        passed=deposits['total_net'] > 0,
        detail=(f"Net {deposits['total_net']:,.2f} · monthly avg "
                f"{deposits['monthly_average']:,.2f}"),
        severity=_sev(deposits['total_net'] > 0, warn_ok=True),
    ))

    # 4. Repayments history
    verdicts.append(Verdict(
        label=f"Prior Repayments ({repayments['months_window']}m)",
        passed=True,  # informational
        detail=(f"{repayments['total_repaid']:,.2f} across "
                f"{len(repayments['per_loan'])} loan(s) · monthly avg "
                f"{repayments['monthly_average']:,.2f}"),
        severity='info',
    ))

    # 5. Has defaulted before?
    verdicts.append(Verdict(
        label="Has Defaulted Before?",
        passed=not defaulter['has_defaulted_before'],
        detail=(f"Yes — {defaulter['total_defaulted_loans']} loan(s) on record"
                if defaulter['has_defaulted_before']
                else "No prior defaults on record"),
        severity=('warning' if defaulter['has_defaulted_before'] else 'success'),
    ))

    # 6. Historical repayment score
    if repayment_score != 'N/A':
        try:
            score = float(repayment_score)
            passed = score >= 60
            verdicts.append(Verdict(
                label="Repayment Score",
                passed=passed,
                detail=f"{score:.2f}%",
                severity=('success' if score >= 85 else
                          'warning' if score >= 40 else 'danger'),
            ))
        except (TypeError, ValueError):
            pass

    # 7. Security coverage
    principal = _dec(loan.principal)
    coverage = ((total_security / principal) * Decimal('100')) if principal > 0 else ZERO
    verdicts.append(Verdict(
        label="Security Coverage",
        passed=coverage >= 100,
        detail=f"{coverage.quantize(Decimal('0.01'))}% of principal",
        severity=_sev(coverage >= 100, warn_ok=(coverage >= 60)),
    ))

    # 8. Exposure vs eligibility
    total_exposure = total_outstanding_debt + principal
    within_limit = total_exposure <= eligibility['eligible_ceiling']
    verdicts.append(Verdict(
        label="Total Exposure Within Ceiling",
        passed=within_limit,
        detail=(f"Exposure {total_exposure:,.2f} vs ceiling "
                f"{eligibility['eligible_ceiling']:,.2f}"),
        severity=_sev(within_limit),
    ))

    # Turn to plain dicts for template convenience
    return [
        {'label': v.label, 'passed': v.passed,
         'detail': v.detail, 'severity': v.severity}
        for v in verdicts
    ]
