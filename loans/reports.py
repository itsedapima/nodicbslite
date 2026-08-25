"""
loans/reports.py
================
Report views owned by the loans app.
URL routing is centralized in reports/urls.py.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from reports.utils import get_date_range, render_report

logger = logging.getLogger(__name__)
ZERO = Decimal("0.00")


def _money(value) -> str:
    try:
        return f"{Decimal(str(value)):,.2f}"
    except Exception:
        return str(value)


# ===================================================================
#  LOANS REGISTER
# ===================================================================

@login_required
def loans_register(request):
    """All disbursed loans + per-charge-type columns + totals."""
    from loans.models import LoanChargeRecovery, LoanHistory

    start, end = get_date_range(request, default_days=365)
    product = request.GET.get("product")

    qs = (LoanHistory.objects
          .filter(is_disbursed=True, loan_date__gte=start, loan_date__lte=end)
          .select_related("customer", "loan_type")
          .order_by("loan_date"))
    if product:
        qs = qs.filter(loan_type__account_type=product)

    # Discover every charge type that appears in the date range
    charge_names = list(
        LoanChargeRecovery.objects
        .filter(loan__in=qs)
        .values_list("charge__name", flat=True)
        .distinct()
        .order_by("charge__name")
    )

    # Pre-fetch per-loan, per-charge-type amounts
    per_loan_charges = (
        LoanChargeRecovery.objects
        .filter(loan__in=qs)
        .values("loan_id", "charge__name")
        .annotate(total=Sum("amount"))
    )
    charges_map = defaultdict(lambda: defaultdict(lambda: ZERO))
    for r in per_loan_charges:
        charges_map[r["loan_id"]][r["charge__name"]] = r["total"] or ZERO

    fixed_headers = [
        "Loan No", "Loan Date", "Cust No", "Member", "Product",
        "Principal", "Net Disbursed",
    ]
    tail_headers = ["Period (m)", "Rate %", "Disbursed By", "Disbursed At"]
    headers = (
        fixed_headers
        + charge_names
        + ["Total Charges"]
        + tail_headers
    )

    n = len(charge_names)
    numeric_columns = (
        [5, 6]
        + list(range(7, 7 + n))
        + [7 + n]
        + [7 + n + 2]
    )

    rows = []
    tot_principal = tot_net = ZERO
    tot_per_charge = defaultdict(lambda: ZERO)
    tot_all_charges = ZERO

    for ln in qs:
        loan_charges = charges_map.get(ln.id, {})
        row_total_charges = sum(loan_charges.values(), ZERO)

        tot_principal += ln.principal or ZERO
        tot_net += ln.net_disbursed or ZERO
        tot_all_charges += row_total_charges
        for cname in charge_names:
            tot_per_charge[cname] += loan_charges.get(cname, ZERO)

        row = [
            ln.loan_no,
            ln.loan_date.strftime("%Y-%m-%d") if ln.loan_date else "",
            getattr(ln.customer, "cust_no", ""),
            getattr(ln.customer, "full_name", ""),
            ln.get_loan_type_display(),
            float(ln.principal or 0),
            float(ln.net_disbursed or 0),
        ]
        for cname in charge_names:
            row.append(float(loan_charges.get(cname, ZERO)))
        row.append(float(row_total_charges))
        row += [
            ln.loan_period,
            float(ln.interest_rate or 0),
            ln.created_by or "",
            ln.disbursed_at.strftime("%Y-%m-%d") if ln.disbursed_at else "",
        ]
        rows.append(row)

    summary = [
        ("Total Loans",     str(qs.count())),
        ("Total Principal", _money(tot_principal)),
        ("Total Disbursed", _money(tot_net)),
    ]
    for cname in charge_names:
        summary.append((f"Total {cname}", _money(tot_per_charge[cname])))
    summary.append(("Total All Charges", _money(tot_all_charges)))

    from transactions.models import CustomerAccountsSetup
    return render_report(
        request,
        template="reports/generic_report.html",
        title="Loans Register",
        subtitle=f"Disbursed loans - {start} to {end}",
        headers=headers,
        rows=rows,
        summary=summary,
        numeric_columns=numeric_columns,
        extra_context={
            "start_date": start, "end_date": end,
            "product": product or "",
            "products": (CustomerAccountsSetup.objects
                         .filter(is_loan_account=True, is_active=True)
                         .values_list("account_type", "account_name").distinct()),
        },
        filename=f"loans_register_{start}_{end}",
    )


# ===================================================================
#  INTEREST PAID / INCOME REPORT
# ===================================================================

@login_required
def interest_paid_report(request):
    """
    Interest income report with four filter modes:
      this_month, ytd, custom, tenure

    Handles flat_rate upfront interest (charged as a debit at disbursement)
    as well as reducing_balance interest charged monthly.
    """
    from loans.models import LoanHistory
    from transactions.models import LoanTransaction

    mode = request.GET.get("mode", "custom")
    today = timezone.localdate()

    if mode == "this_month":
        start = today.replace(day=1)
        end = today
    elif mode == "ytd":
        start = today.replace(month=1, day=1)
        end = today
    elif mode == "tenure":
        start = None
        end = None
    else:
        mode = "custom"
        start, end = get_date_range(request, default_days=30)

    is_tenure = mode == "tenure"

    _q_interest = Q(tr_desc__icontains="interest")
    _q_non_payment_credit = (
        Q(tr_desc__icontains="bridged")
        | Q(tr_desc__icontains="REVERSAL")
        | Q(tr_desc__icontains="reverse")
    )
    _q_reversal_debit = (
        Q(tr_desc__icontains="reversal")
        | Q(tr_desc__icontains="reverse")
    )

    base_qs = LoanTransaction.objects.all()
    if not is_tenure:
        base_qs = base_qs.filter(
            tr_date__date__gte=start,
            tr_date__date__lte=end,
        )

    agg_qs = (
        base_qs
        .values("loan_id", "loan_no")
        .annotate(
            repayment_credits=Coalesce(
                Sum("credit_amount", filter=~_q_non_payment_credit),
                Value(0, output_field=DecimalField()),
            ),
            reversal_debits=Coalesce(
                Sum("debit_amount",
                    filter=_q_reversal_debit & ~_q_interest),
                Value(0, output_field=DecimalField()),
            ),
            interest_charged=Coalesce(
                Sum("debit_amount", filter=_q_interest),
                Value(0, output_field=DecimalField()),
            ),
            all_debits=Coalesce(
                Sum("debit_amount"),
                Value(0, output_field=DecimalField()),
            ),
            all_credits=Coalesce(
                Sum("credit_amount"),
                Value(0, output_field=DecimalField()),
            ),
        )
    )

    data = list(agg_qs)
    loan_ids = [r["loan_id"] for r in data]

    loans_by_id = {}
    if loan_ids:
        loans_by_id = {
            l.id: l for l in
            LoanHistory.objects.filter(id__in=loan_ids)
            .select_related("customer", "loan_type")
        }

    if is_tenure:
        headers = [
            "Loan No", "Cust No", "Member", "Product",
            "Loan Date", "Status", "Principal",
            "Interest Charged", "Total Repaid",
            "Interest Paid", "Principal Paid", "Balance",
        ]
        numeric_cols = [6, 7, 8, 9, 10, 11]
    else:
        headers = [
            "Loan No", "Cust No", "Member", "Product",
            "Total Repaid", "Interest Charged",
            "Interest Paid", "Principal Paid",
        ]
        numeric_cols = [4, 5, 6, 7]

    rows = []
    sum_repaid = sum_charged = sum_int_paid = sum_prin_paid = ZERO

    for r in data:
        ln = loans_by_id.get(r["loan_id"])
        rep = r["repayment_credits"] or ZERO
        rev = r["reversal_debits"] or ZERO
        int_charged = r["interest_charged"] or ZERO

        net_repaid = max(ZERO, rep - rev)
        interest_paid = min(net_repaid, int_charged)
        principal_paid = max(ZERO, net_repaid - interest_paid)

        if net_repaid == ZERO and int_charged == ZERO:
            continue

        sum_repaid += net_repaid
        sum_charged += int_charged
        sum_int_paid += interest_paid
        sum_prin_paid += principal_paid

        loan_no = r["loan_no"] or (ln.loan_no if ln else "")
        cust_no = getattr(getattr(ln, "customer", None), "cust_no", "") if ln else ""
        full_name = getattr(getattr(ln, "customer", None), "full_name", "") if ln else ""
        product = ""
        if ln and getattr(ln, "loan_type", None):
            product = getattr(ln.loan_type, "account_type", "").replace("_", " ").title()

        if is_tenure:
            balance = max(ZERO, (r["all_debits"] or ZERO) - (r["all_credits"] or ZERO))
            loan_date = ""
            status_str = ""
            principal_str = 0.0
            if ln:
                loan_date = str(ln.loan_date) if ln.loan_date else ""
                principal_str = float(ln.principal) if ln.principal else 0.0
                from loans.models import RunningLoanStat
                stat = RunningLoanStat.objects.filter(
                    loan_no=ln.loan_no).values_list(
                    "loan_status", flat=True).first()
                status_str = stat or ("Settled" if balance <= ZERO else "Active")

            rows.append([
                loan_no, cust_no, full_name, product,
                loan_date, status_str, principal_str,
                float(int_charged), float(net_repaid),
                float(interest_paid), float(principal_paid),
                float(balance),
            ])
        else:
            rows.append([
                loan_no, cust_no, full_name, product,
                float(net_repaid), float(int_charged),
                float(interest_paid), float(principal_paid),
            ])

    summary = [
        ("Total Repaid",           _money(sum_repaid)),
        ("Total Interest Charged", _money(sum_charged)),
        ("Total Interest Paid",    _money(sum_int_paid)),
        ("Total Principal Paid",   _money(sum_prin_paid)),
    ]

    if is_tenure:
        subtitle = "Full Loan Tenure (Lifetime)"
    elif mode == "this_month":
        subtitle = f"This Month: {start} to {end}"
    elif mode == "ytd":
        subtitle = f"Year to Date: {start} to {end}"
    else:
        subtitle = f"{start} to {end}"

    return render_report(
        request,
        template="reports/generic_report.html",
        title="Interest Income Report",
        subtitle=subtitle,
        headers=headers,
        rows=rows,
        summary=summary,
        numeric_columns=numeric_cols,
        extra_context={
            "start_date": start,
            "end_date": end,
            "mode": mode,
        },
        filename=f"interest_income_{mode}_{start or 'tenure'}_{end or 'all'}",
    )


# ===================================================================
#  LOAN BOOK
# ===================================================================

@login_required
def loan_book(request):
    """Snapshot of all running loans (RunningLoanStat)."""
    from loans.models import RunningLoanStat

    classification = request.GET.get("classification")
    product = request.GET.get("product")
    qs = RunningLoanStat.objects.filter(loan_status__iexact="Active").order_by("cust_no")
    if classification:
        qs = qs.filter(loan_classification__iexact=classification)
    if product:
        qs = qs.filter(product_code__iexact=product)

    headers = ["Loan No", "Cust No", "Member", "Product",
               "Approved", "Balance", "Principal Bal", "Interest Bal",
               "Arrears", "Days Late", "Installment", "Next Due",
               "Classification"]
    rows = []
    tot_appr = tot_bal = tot_pbal = tot_ibal = tot_arr = ZERO
    for r in qs:
        tot_appr += r.approved_amount or ZERO
        tot_bal += r.loan_balance or ZERO
        tot_pbal += r.principle_balance or ZERO
        tot_ibal += r.interest_balance or ZERO
        tot_arr += r.total_arrears or ZERO
        rows.append([
            r.loan_no,
            r.cust_no,
            r.full_name,
            r.product_description or r.product_code,
            float(r.approved_amount or 0),
            float(r.loan_balance or 0),
            float(r.principle_balance or 0),
            float(r.interest_balance or 0),
            float(r.total_arrears or 0),
            r.defaulted_days,
            float(r.monthly_installment or 0),
            r.next_repayment_date.strftime("%Y-%m-%d") if r.next_repayment_date else "",
            r.loan_classification,
        ])

    summary = [
        ("Running Loans", str(qs.count())),
        ("Total Approved", _money(tot_appr)),
        ("Total Balance",  _money(tot_bal)),
        ("Principal Balance", _money(tot_pbal)),
        ("Interest Balance", _money(tot_ibal)),
        ("Total Arrears",  _money(tot_arr)),
    ]
    return render_report(
        request,
        template="reports/generic_report.html",
        title="Loan Book - Running Loans",
        subtitle=f"As at {timezone.localdate()}",
        headers=headers,
        rows=rows,
        summary=summary,
        numeric_columns=[4, 5, 6, 7, 8, 10],
        extra_context={
            "classification": classification or "",
            "product": product or "",
            "classifications": (
                RunningLoanStat.objects.order_by()
                .values_list("loan_classification", flat=True).distinct()
            ),
            "products": (
                RunningLoanStat.objects.order_by()
                .values_list("product_code", flat=True).distinct()
            ),
        },
        filename="loan_book",
    )


# ===================================================================
#  MOBILE LOANS REPORT
# ===================================================================

@login_required
def mobile_loans_report(request):
    """All mobile loans (prefix MOBI or product flagged is_mobile_loan)."""
    from loans.models import LoanHistory

    start, end = get_date_range(request, default_days=365)
    qs = (LoanHistory.objects
          .select_related("customer", "loan_type")
          .filter(loan_date__gte=start, loan_date__lte=end)
          .filter(Q(loan_no__startswith="MOBI") | Q(loan_type__is_mobile_loan=True))
          .order_by("loan_date"))

    headers = ["Loan No", "Date", "Cust No", "Member", "Principal",
               "Net Disbursed", "Period (m)", "Rate %", "Disbursed?",
               "Disbursed At"]
    rows = []
    tot_principal = tot_net = ZERO
    for ln in qs:
        tot_principal += ln.principal or ZERO
        tot_net += ln.net_disbursed or ZERO
        rows.append([
            ln.loan_no,
            ln.loan_date.strftime("%Y-%m-%d") if ln.loan_date else "",
            getattr(ln.customer, "cust_no", ""),
            getattr(ln.customer, "full_name", ""),
            float(ln.principal or 0),
            float(ln.net_disbursed or 0),
            ln.loan_period,
            float(ln.interest_rate or 0),
            "Yes" if ln.is_disbursed else "No",
            ln.disbursed_at.strftime("%Y-%m-%d") if ln.disbursed_at else "",
        ])

    summary = [
        ("Total Loans", str(qs.count())),
        ("Total Principal", _money(tot_principal)),
        ("Total Net Disbursed", _money(tot_net)),
    ]
    return render_report(
        request,
        template="reports/generic_report.html",
        title="Mobile Loans Report",
        subtitle=f"{start} to {end}",
        headers=headers,
        rows=rows,
        summary=summary,
        numeric_columns=[4, 5, 7],
        extra_context={"start_date": start, "end_date": end},
        filename=f"mobile_loans_{start}_{end}",
    )
