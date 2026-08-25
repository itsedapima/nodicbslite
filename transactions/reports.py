"""
transactions/reports.py
================
Report views owned by the transactions app.
URL routing is centralized in reports/urls.py.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from reports.utils import get_date_range, render_report

logger = logging.getLogger(__name__)
User = get_user_model()
ZERO = Decimal("0.00")


def _money(value) -> str:
    try:
        return f"{Decimal(str(value)):,.2f}"
    except Exception:
        return str(value)


def _cust_name_map(cust_no_iterable):
    from customers.models import Customer
    cust_nos = [str(n).strip() for n in cust_no_iterable if n]
    if not cust_nos:
        return {}
    return {
        c.cust_no: c.full_name
        for c in Customer.objects.filter(cust_no__in=cust_nos).only("cust_no", "full_name")
    }


# ===================================================================
#  CASHIER STATEMENT
# ===================================================================

@login_required
def cashier_statement(request):
    """Manual postings booked through BulkUploadQueue (status='processed')."""
    from transactions.models import BulkUploadQueue

    start, end = get_date_range(request)
    user_id = request.GET.get("user")

    qs = (BulkUploadQueue.objects
          .filter(status="processed", date__gte=start, date__lte=end)
          .select_related("customer", "created_by")
          .order_by("date", "id"))

    if user_id:
        qs = qs.filter(created_by_id=user_id)

    headers = ["Date", "Cust No", "Member", "Ref", "Description",
               "Debit", "Credit", "Cashier"]
    rows = []
    total_dr = ZERO
    total_cr = ZERO
    count = 0
    for r in qs.iterator(chunk_size=2000):
        count += 1
        amt = r.amount or ZERO
        dr = ZERO
        cr = amt
        total_dr += dr
        total_cr += cr
        ref = r.loan_no or ""
        rows.append([
            r.date.strftime("%Y-%m-%d") if r.date else "",
            getattr(r.customer, "cust_no", ""),
            getattr(r.customer, "full_name", ""),
            ref,
            (r.description or r.saving_type or r.loan_type or "Manual posting"),
            float(dr) if dr else "",
            float(cr) if cr else "",
            (r.created_by.get_full_name() if r.created_by_id else ""),
        ])

    summary = [
        ("Total Postings", str(count)),
        ("Total Debit",    _money(total_dr)),
        ("Total Credit",   _money(total_cr)),
    ]
    return render_report(
        request,
        template="reports/generic_report.html",
        title="Cashier Statement - Manual Payments",
        subtitle=f"{start} to {end}",
        headers=headers,
        rows=rows,
        summary=summary,
        numeric_columns=[5, 6],
        extra_context={
            "start_date": start, "end_date": end,
            "user_id": user_id or "",
            "users": (User.objects.filter(is_active=True)
                      .only("id", "first_name", "last_name", "username")
                      .order_by("first_name")),
        },
        filename=f"cashier_statement_{start}_{end}",
    )


# ===================================================================
#  PAYMENTS SUMMARY
# ===================================================================

@login_required
def payments_summary_monthly(request):
    from transactions.models import (
        CustomerAccountsSetup, LoanTransaction, SavingsTransaction,
    )

    start, end = get_date_range(request, default_days=365)
    account_type = request.GET.get("account_type") or ""

    setup_qs = (CustomerAccountsSetup.objects.filter(is_active=True)
                .only("account_type", "account_name", "account_code",
                      "is_loan_account")
                .order_by("account_code"))
    setup_by_type = {s.account_type: s for s in setup_qs}
    loan_types = {t for t, s in setup_by_type.items() if s.is_loan_account}

    # -- DETAIL MODE
    if account_type:
        setup = setup_by_type.get(account_type)
        title_account = setup.account_name if setup else account_type
        is_loan = account_type in loan_types

        if is_loan:
            txn_qs = (LoanTransaction.objects
                      .filter(loan_type=account_type,
                              tr_date__date__gte=start,
                              tr_date__date__lte=end)
                      .order_by("tr_date", "id"))
        else:
            txn_qs = (SavingsTransaction.objects
                      .filter(saving_type=account_type,
                              tr_date__date__gte=start,
                              tr_date__date__lte=end)
                      .order_by("tr_date", "id"))

        cust_nos = set(txn_qs.values_list("cust_no", flat=True))
        name_map = _cust_name_map(cust_nos)

        headers = ["Cust No", "Member", "Ref", "Description", "Debit", "Credit"]
        rows = []
        total_dr = total_cr = ZERO
        count = 0
        for t in txn_qs.iterator(chunk_size=5000):
            count += 1
            dr = t.debit_amount or ZERO
            cr = t.credit_amount or ZERO
            total_dr += dr
            total_cr += cr
            rows.append([
                str(t.cust_no).zfill(5),
                name_map.get(t.cust_no, ""),
                t.tr_ref or (getattr(t, "loan_no", "") or ""),
                t.tr_desc or "",
                float(dr) if dr else "",
                float(cr) if cr else "",
            ])

        summary = [
            ("Entries", str(count)),
            ("Total Debit",  _money(total_dr)),
            ("Total Credit", _money(total_cr)),
            ("Net Payments", _money(total_cr - total_dr)),
        ]
        return render_report(
            request,
            template="reports/generic_report.html",
            title=f"{title_account} - Payments",
            subtitle=f"{start} to {end}",
            headers=headers,
            rows=rows,
            summary=summary,
            numeric_columns=[4, 5],
            extra_context={
                "start_date": start, "end_date": end,
                "account_type": account_type,
                "is_detail": True,
            },
            filename=f"payments_{account_type}_{start}_{end}",
        )

    # -- SUMMARY MODE (pivot)
    sav_rows = (SavingsTransaction.objects
                .filter(tr_date__date__gte=start, tr_date__date__lte=end,
                        credit_amount__gt=0)
                .annotate(m=TruncMonth("tr_date"))
                .values("saving_type", "m")
                .annotate(total=Sum("credit_amount")))

    loan_rows = (LoanTransaction.objects
                 .filter(tr_date__date__gte=start, tr_date__date__lte=end,
                         credit_amount__gt=0)
                 .annotate(m=TruncMonth("tr_date"))
                 .values("loan_type", "m")
                 .annotate(total=Sum("credit_amount")))

    grid = defaultdict(lambda: defaultdict(lambda: ZERO))
    months = set()
    for r in sav_rows:
        mkey = r["m"].strftime("%Y-%m") if r["m"] else ""
        if mkey:
            grid[r["saving_type"]][mkey] = r["total"] or ZERO
            months.add(mkey)
    for r in loan_rows:
        mkey = r["m"].strftime("%Y-%m") if r["m"] else ""
        if mkey:
            grid[r["loan_type"]][mkey] = r["total"] or ZERO
            months.add(mkey)

    months_sorted = sorted(months)
    headers = ["Account"] + months_sorted + ["Total"]
    rows = []
    col_totals = [ZERO] * (len(months_sorted) + 1)
    for setup in setup_qs:
        per_month = grid.get(setup.account_type, {})
        if not per_month:
            continue
        line = [setup.account_name]
        rtot = ZERO
        for i, m in enumerate(months_sorted):
            v = per_month.get(m, ZERO)
            rtot += v
            col_totals[i] += v
            line.append(float(v))
        line.append(float(rtot))
        col_totals[-1] += rtot
        rows.append(line)

    summary = ([(f"Total {m}", _money(col_totals[i])) for i, m in enumerate(months_sorted)]
               + [("Grand Total", _money(col_totals[-1]))])

    numeric_cols = list(range(1, 1 + len(months_sorted) + 1))
    return render_report(
        request,
        template="reports/generic_report.html",
        title="Payments Summary - Monthly",
        subtitle=f"{start} to {end}",
        headers=headers,
        rows=rows,
        summary=summary,
        numeric_columns=numeric_cols,
        extra_context={
            "start_date": start, "end_date": end,
            "account_type": "",
            "is_detail": False,
        },
        filename=f"payments_summary_{start}_{end}",
    )


# ===================================================================
#  MPESA PAYMENTS
# ===================================================================

@login_required
def mpesa_payments(request):
    """All M-PESA payments that have been posted to a member account."""
    from transactions.models import (
        CustomerAccountsSetup, MpesaNotification, PostedMpesaNotification,
    )

    start, end = get_date_range(request, default_days=30)
    account_type_filter = request.GET.get("account_type") or ""

    posted_qs = (PostedMpesaNotification.objects
                 .select_related("mpesa_notification")
                 .filter(mpesa_notification__trans_time__date__gte=start,
                         mpesa_notification__trans_time__date__lte=end))
    if account_type_filter:
        posted_qs = posted_qs.filter(account_type=account_type_filter)
    posted_qs = posted_qs.order_by("mpesa_notification__trans_time")

    cust_strings = set(posted_qs.values_list("customer_no", flat=True))
    from customers.models import Customer
    name_map = {
        c.cust_no: c.full_name
        for c in Customer.objects.filter(cust_no__in=cust_strings).only("cust_no", "full_name")
    }
    type_name = {
        a.account_type: a.account_name
        for a in CustomerAccountsSetup.objects.only("account_type", "account_name")
    }

    headers = ["Date", "Cust No", "Member", "Trans ID", "Description",
               "Dest. Account", "Amount"]
    rows = []
    total = ZERO
    count = 0
    for p in posted_qs.iterator(chunk_size=5000):
        n = p.mpesa_notification
        amt = n.trans_amount or ZERO
        total += amt
        count += 1
        rows.append([
            n.trans_time.strftime("%Y-%m-%d %H:%M") if n.trans_time else "",
            p.customer_no,
            name_map.get(p.customer_no, n.first_name or ""),
            n.trans_id,
            n.bill_ref_number or "",
            type_name.get(p.account_type, p.account_type),
            float(amt),
        ])

    summary = [
        ("Total Payments", str(count)),
        ("Total Amount",   _money(total)),
    ]
    return render_report(
        request,
        template="reports/generic_report.html",
        title="M-PESA Payments",
        subtitle=f"{start} to {end}",
        headers=headers,
        rows=rows,
        summary=summary,
        numeric_columns=[6],
        extra_context={
            "start_date": start, "end_date": end,
            "account_type": account_type_filter,
        },
        filename=f"mpesa_payments_{start}_{end}",
    )


# ===================================================================
#  SAVINGS BREAKDOWN MATRIX
# ===================================================================

@login_required
def savings_breakdown_matrix(request):
    """
    Monthly per-member contributions grid.
    Rows = members, Columns = months (Jan … Dec), last col = Total.
    Filterable by year and saving_type (account product).
    """
    from transactions.models import CustomerAccountsSetup, SavingsTransaction

    today = timezone.localdate()
    try:
        year = int(request.GET.get("year", today.year))
    except (ValueError, TypeError):
        year = today.year

    saving_type_filter = request.GET.get("saving_type") or ""

    import datetime as _dt
    year_start = _dt.date(year, 1, 1)
    year_end = _dt.date(year, 12, 31)

    txn_qs = SavingsTransaction.objects.filter(
        tr_date__date__gte=year_start,
        tr_date__date__lte=year_end,
        credit_amount__gt=0,
    )
    if saving_type_filter:
        txn_qs = txn_qs.filter(saving_type=saving_type_filter)

    monthly = (
        txn_qs
        .annotate(m=TruncMonth("tr_date"))
        .values("cust_no", "m")
        .annotate(total=Coalesce(
            Sum("credit_amount"),
            Value(0, output_field=DecimalField()),
        ))
    )

    # Build grid: { cust_no: { "2026-01": Decimal, ... } }
    grid = defaultdict(lambda: defaultdict(lambda: ZERO))
    cust_nos = set()
    for r in monthly:
        mkey = r["m"].strftime("%Y-%m") if r["m"] else ""
        if mkey:
            grid[r["cust_no"]][mkey] = r["total"] or ZERO
            cust_nos.add(r["cust_no"])

    # Resolve customer names
    name_map = _cust_name_map(cust_nos)

    # Month columns for the selected year
    import calendar
    month_keys = [f"{year}-{str(m).zfill(2)}" for m in range(1, 13)]
    month_labels = [calendar.month_abbr[m] + f" {year}" for m in range(1, 13)]

    headers = ["Cust No", "Full Name"] + month_labels + ["Total"]
    numeric_cols = list(range(2, 2 + 12 + 1))  # all month columns + total

    rows = []
    col_totals = [ZERO] * 13  # 12 months + grand total
    for cno in sorted(cust_nos):
        per_month = grid[cno]
        row = [str(cno).zfill(5), name_map.get(cno, "")]
        row_total = ZERO
        for i, mk in enumerate(month_keys):
            v = per_month.get(mk, ZERO)
            row_total += v
            col_totals[i] += v
            row.append(float(v))
        row.append(float(row_total))
        col_totals[12] += row_total
        rows.append(row)

    summary = (
        [(ml, _money(col_totals[i])) for i, ml in enumerate(month_labels)]
        + [("Grand Total", _money(col_totals[12]))]
    )

    # Available saving types for filter dropdown
    saving_types = (
        CustomerAccountsSetup.objects
        .filter(is_active=True, is_loan_account=False)
        .values_list("account_type", "account_name")
        .distinct()
        .order_by("account_code")
    )

    return render_report(
        request,
        template="reports/generic_report.html",
        title="Savings Breakdown Matrix",
        subtitle=f"Monthly Contributions - {year}"
                 + (f" ({saving_type_filter.replace('_', ' ').title()})" if saving_type_filter else " (All Products)"),
        headers=headers,
        rows=rows,
        summary=summary,
        numeric_columns=numeric_cols,
        extra_context={
            "year": year,
            "saving_type": saving_type_filter,
            "saving_types": saving_types,
            "years": list(range(today.year, today.year - 6, -1)),
        },
        filename=f"savings_matrix_{year}",
    )
