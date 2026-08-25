"""
customers/reports.py
================
Report views owned by the customers app.
URL routing is centralized in reports/urls.py.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import DecimalField, Sum, Value
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


@login_required
def members_listing(request):
    """Cust_no | National ID | Phone | Name | Reg date | Status"""
    from customers.models import Customer

    qs = Customer.objects.all().order_by("cust_no")

    status = request.GET.get("status")
    if status:
        qs = qs.filter(customer_status=status)

    start, end = get_date_range(request, default_days=3650)
    qs = qs.filter(reg_date__gte=start, reg_date__lte=end)

    headers = ["Cust No", "National ID", "Phone", "Full Name", "Reg Date", "Status"]
    rows = [
        [
            c.cust_no,
            c.national_id or "",
            c.phone or "",
            c.full_name or "",
            c.reg_date.strftime("%Y-%m-%d") if c.reg_date else "",
            c.get_customer_status_display(),
        ]
        for c in qs
    ]

    summary = [
        ("Total Members", str(qs.count())),
        ("Active",        str(qs.filter(customer_status="active").count())),
        ("Dormant",       str(qs.filter(customer_status="dormant").count())),
        ("Exited",        str(qs.filter(customer_status="exited").count())),
        ("Deceased",      str(qs.filter(customer_status="deceased").count())),
    ]

    return render_report(
        request,
        template="reports/generic_report.html",
        title="Members Listing",
        subtitle=f"Registered between {start} and {end}"
                 + (f"  -  status: {status}" if status else ""),
        headers=headers,
        rows=rows,
        summary=summary,
        extra_context={
            "start_date": start, "end_date": end, "status": status or "",
        },
        filename="members_listing",
    )


@login_required
def member_balances_listing(request):
    """Per-customer per-account-type running balances (Credits - Debits)."""
    from customers.models import Customer
    from transactions.models import CustomerAccountsSetup, SavingsTransaction

    cust_no = request.GET.get("cust_no")

    account_types = list(
        CustomerAccountsSetup.objects.filter(is_active=True)
        .order_by("account_code")
        .values_list("account_type", "account_name")
    )

    sav = SavingsTransaction.objects.values("cust_no", "saving_type").annotate(
        credit=Coalesce(Sum("credit_amount"), Value(0, output_field=DecimalField())),
        debit=Coalesce(Sum("debit_amount"), Value(0, output_field=DecimalField())),
    )
    if cust_no:
        try:
            sav = sav.filter(cust_no=str(cust_no))
        except (ValueError, TypeError):
            pass

    grid = defaultdict(lambda: defaultdict(lambda: ZERO))
    for r in sav.iterator(chunk_size=5000):
        grid[r["cust_no"]][r["saving_type"]] = (r["credit"] or ZERO) - (r["debit"] or ZERO)

    customer_qs = Customer.objects.only("cust_no", "full_name").order_by("cust_no")
    if cust_no:
        customer_qs = customer_qs.filter(cust_no=str(cust_no).zfill(5))

    headers = ["Cust No", "Member"] + [name for _, name in account_types] + ["Total"]
    rows = []
    grand_total = ZERO
    for c in customer_qs.iterator(chunk_size=2000):
        try:
            ck = str(c.cust_no)
        except (ValueError, TypeError):
            continue
        bal_by_type = grid.get(ck)
        if not bal_by_type:
            continue
        cust_total = ZERO
        cells = [c.cust_no, c.full_name]
        for code, _ in account_types:
            bal = bal_by_type.get(code, ZERO)
            cust_total += bal
            cells.append(float(bal))
        cells.append(float(cust_total))
        grand_total += cust_total
        rows.append(cells)

    summary = [("Grand Total Balance", _money(grand_total))]
    numeric_cols = list(range(2, 2 + len(account_types) + 1))

    return render_report(
        request,
        template="reports/generic_report.html",
        title="Members Account Balances",
        subtitle=f"As at {timezone.localdate()}",
        headers=headers,
        rows=rows,
        summary=summary,
        numeric_columns=numeric_cols,
        extra_context={"cust_no": cust_no or ""},
        filename="member_balances",
    )
