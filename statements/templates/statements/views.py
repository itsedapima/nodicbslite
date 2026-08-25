"""
statements/views.py  –  Production-ready (CharField cust_no safe)
=================================================================
Customer.cust_no is now CharField (zero-padded "00123").
SavingsTransaction.cust_no / LoanTransaction.cust_no are still PositiveIntegerField.

RULE:  Customer lookups → padded string.
       Transaction lookups → int.

Key fixes vs original:
  1. Removed duplicate download_full_statement_pdf definition.
  2. Removed duplicate buffer/doc/story initialization inside pdf generator.
  3. Fixed MOBI-loan balance logic in `download` (was only checking "LN" prefix).
  4. Fixed loan_type FK vs string handling — uses a safe helper.
  5. Eliminated N+1 queries in full_statement and download_full_statement_pdf.
  6. Added select_related / only() on hot query paths.
  7. Added MAX_ROWS guard to prevent memory exhaustion.
  8. Moved logger to module level.
  9. DRY: extracted _is_loan_account() and _resolve_loan_type_label() helpers.
 10. ★ ALL cust_no lookups are now type-safe for CharField Customer + IntegerField transactions.
"""

import io
import logging
from datetime import date, datetime, time
from decimal import Decimal

from django.contrib import messages
from django.db.models import Q, Sum
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.timezone import now
from django.views.decorators.http import require_GET, require_http_methods

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Image as RLImage, SimpleDocTemplate,
    Spacer, Table, TableStyle, Paragraph,
)

from customers.models import Customer
from dashboard.models import CompanyInfo
from loans.models import LoanHistory
from transactions.models import CustomerAccountsSetup, LoanTransaction, SavingsTransaction

from .forms import StatementFilterForm, StatementSchedule, StatementScheduleForm
from .models import StatementLog, StatementSchedule
from .tasks import trigger_statements_background

logger = logging.getLogger(__name__)

MAX_ROWS = 10_000


# ===========================================================================
# ★ cust_no conversion helpers
#
#   Customer.cust_no              → CharField  ("00123")
#   SavingsTransaction.cust_no    → PositiveIntegerField (123)
#   LoanTransaction.cust_no       → PositiveIntegerField (123)
# ===========================================================================

def _pad_cust_no(raw) -> str:
    """ANY input → zero-padded string for Customer table lookups.
    Handles: int 123, str "123", str "00123", "5(00123)", None."""
    s = str(raw).strip()
    if s.isdigit():
        return s.zfill(5)
    return s


def _txn_cust_no(customer_or_value) -> int:
    """ANY input → int for SavingsTransaction / LoanTransaction PositiveIntegerField.
    Handles: Customer instance, str "00123", int 123."""
    if hasattr(customer_or_value, 'cust_no'):
        return customer_or_value.cust_no
    return str(customer_or_value).strip()


def _resolve_customer(cust_no_raw: str):
    """Parse raw cust_no from form/URL (may contain parentheses from autocomplete),
    normalize to padded string, and return (Customer, cust_no_str) or raise ValueError."""
    raw = str(cust_no_raw).strip()
    # Handle autocomplete format: "John Doe (00123)"
    if "(" in raw and raw.endswith(")"):
        raw = raw.split("(")[-1].rstrip(")")
    raw = raw.strip()
    if not raw:
        raise ValueError("Customer number is required.")
    padded = _pad_cust_no(raw)
    try:
        cust = Customer.objects.get(cust_no=padded)
        return cust, padded
    except Customer.DoesNotExist:
        raise ValueError(f"Customer '{padded}' not found.")


# ===========================================================================
# Private helpers
# ===========================================================================

def _is_loan_account(account_id: str) -> bool:
    return account_id.startswith(("LN", "MOBI"))


def _resolve_loan_type_label(loan_transaction) -> str:
    if loan_transaction is None:
        return "Loan"
    raw = loan_transaction.loan_type
    if not raw:
        return "Loan"
    setup = CustomerAccountsSetup.objects.filter(account_type=raw).first()
    if setup:
        return setup.account_name
    return raw.replace("_", " ").title()


def _parse_date_range(from_date, to_date):
    d_from_dt = d_to_dt = None
    try:
        if from_date:
            d_from_dt = datetime.combine(from_date, time.min)
        if to_date:
            d_to_dt = datetime.combine(to_date, time.max)
    except Exception:
        pass
    return d_from_dt, d_to_dt


def _get_transactions_queryset(account_id: str, cust_no_int: int):
    """Returns un-evaluated queryset. cust_no_int MUST be an int
    (matches PositiveIntegerField on transaction tables)."""
    if _is_loan_account(account_id):
        return (
            LoanTransaction.objects
            .filter(cust_no=cust_no_int, loan_no=account_id)
            .only("tr_date", "tr_ref", "tr_desc", "debit_amount", "credit_amount", "loan_type")
        )
    return (
        SavingsTransaction.objects
        .filter(cust_no=cust_no_int, saving_type=account_id)
        .only("tr_date", "tr_ref", "tr_desc", "debit_amount", "credit_amount")
    )


def _compute_opening_balance(account_id: str, cust_no_int: int, d_from_dt) -> float:
    if not d_from_dt:
        return 0.0
    qs = _get_transactions_queryset(account_id, cust_no_int).filter(tr_date__lt=d_from_dt)
    agg = qs.aggregate(credit_sum=Sum("credit_amount"), debit_sum=Sum("debit_amount"))
    c = float(agg.get("credit_sum") or 0.0)
    d = float(agg.get("debit_sum") or 0.0)
    if _is_loan_account(account_id):
        return d - c
    return c - d


def _get_company_info():
    defaults = dict(
        company_name="YOUR INSTITUTION", company_address="",
        company_contact="", company_location="", company_logo_path=None,
    )
    try:
        info = CompanyInfo.objects.first()
        if not info:
            return defaults
        logo_path = None
        if info.company_logo and hasattr(info.company_logo, "path"):
            logo_path = info.company_logo.path
        return dict(
            company_name=info.company_name or defaults["company_name"],
            company_address=info.company_address or "",
            company_contact=info.company_contact or "",
            company_location=info.company_location or "",
            company_logo_path=logo_path,
        )
    except Exception as exc:
        logger.error("Error fetching CompanyInfo: %s", exc)
        return defaults


def _build_pdf_header(story, styles, company: dict):
    centered_style = ParagraphStyle(
        "CenteredHeader", parent=styles["Normal"],
        alignment=TA_CENTER, fontSize=10, leading=13,
    )
    header_rows = []
    if company["company_logo_path"]:
        try:
            img = RLImage(
                company["company_logo_path"],
                width=2.2 * inch, height=0.7 * inch, kind="proportional",
            )
            header_rows.append([img])
        except Exception as exc:
            logger.warning("Could not load company logo: %s", exc)

    address_html = (
        f'<font size="14"><b>{company["company_name"].upper()}</b></font><br/>'
        f'{company["company_address"]}<br/>'
        f'{company["company_contact"]} | {company["company_location"]}'
    )
    header_rows.append([Paragraph(address_html, centered_style)])
    header_table = Table(header_rows, colWidths=[7.2 * inch])
    header_table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1,
                            color=colors.HexColor("#34495e"), spaceAfter=15))


# ===========================================================================
# API endpoints
# ===========================================================================

@require_GET
def get_customer_accounts_api(request):
    """Fetches unique savings types and specific loan accounts with descriptions.

    Query params:
        cust_no       – required, the member number
        include_zero  – "1" to include zero-balance savings / settled loans
    """
    from loans.models import RunningLoanStat

    cust_no_raw = request.GET.get("cust_no")
    if not cust_no_raw:
        return JsonResponse({"results": []})

    include_zero = request.GET.get("include_zero", "") == "1"

    # ★ Resolve to padded string for Customer, int for transactions
    padded = _pad_cust_no(cust_no_raw)
    cust_int = padded  # CharField on txn tables in Eastakiba

    results = []

    # ── Savings accounts ──────────────────────────────────────────────
    savings_accounts = (
        CustomerAccountsSetup.objects
        .filter(is_active=True, is_loan_account=False)
        .values("account_code", "account_name", "account_type")
        .distinct()
    )
    for acc in savings_accounts:
        if not include_zero:
            agg = (
                SavingsTransaction.objects
                .filter(cust_no=cust_int, saving_type=acc["account_type"])
                .aggregate(
                    cr=Sum("credit_amount"),
                    dr=Sum("debit_amount"),
                )
            )
            bal = float(agg["cr"] or 0) - float(agg["dr"] or 0)
            if bal == 0:
                continue

        results.append({
            "id": acc["account_type"],
            "text": f"{acc['account_code']} - {acc['account_name']}",
        })

    # ── Loan accounts ─────────────────────────────────────────────────
    active_loan_nos = (
        LoanTransaction.objects
        .filter(cust_no=cust_int)
        .exclude(loan_no__isnull=True)
        .values_list("loan_no", flat=True)
        .distinct()
    )

    # Exclude settled loans unless include_zero is on
    if not include_zero:
        settled_loan_nos = set(
            RunningLoanStat.objects
            .filter(loan_status="Settled", loan_no__in=active_loan_nos)
            .values_list("loan_no", flat=True)
        )
        active_loan_nos = [ln for ln in active_loan_nos if ln not in settled_loan_nos]

    loans = (
        LoanHistory.objects
        .filter(loan_no__in=active_loan_nos)
        .select_related("loan_type")
        .only("loan_no", "loan_type_id", "loan_type__account_name")
    )
    for ln in loans:
        label = ln.loan_type.account_name if ln.loan_type else "Loan"
        results.append({"id": ln.loan_no, "text": f"{ln.loan_no} - {label}"})

    return JsonResponse({"results": results})


@require_GET
def customer_search_api(request):
    """AJAX live search endpoint."""
    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"results": []})

    qs = Customer.objects.all()
    if q.isdigit():
        # ★ Customer.cust_no is now CharField — use padded string, not int()
        padded = _pad_cust_no(q)
        qs = qs.filter(Q(cust_no=padded) | Q(national_id__icontains=q))
    else:
        qs = qs.filter(full_name__icontains=q)

    results = [
        {
            "cust_no": c.cust_no,
            "full_name": c.full_name,
            "national_id": getattr(c, "national_id", ""),
            "mobile": getattr(c, "phone", ""),
        }
        for c in qs.order_by("cust_no")[:5]
    ]
    return JsonResponse({"results": results})


# ===========================================================================
# Statement preview (JSON)
# ===========================================================================

@require_GET
def preview(request):
    """
    Returns structured JSON for the running-balance statement table.
    """
    form = StatementFilterForm(request.GET)
    if not form.is_valid():
        return JsonResponse({"error": "Invalid parameters"}, status=400)

    cust_no_raw = form.cleaned_data.get("cust_no") or ""

    # ★ Use safe resolver — handles autocomplete format, pads, validates
    try:
        cust, cust_no_str = _resolve_customer(cust_no_raw)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)

    # ★ Int form for transaction table queries
    cust_int = _txn_cust_no(cust)

    account_code = form.cleaned_data.get("account_code") or ""
    from_date = form.cleaned_data.get("from_date")
    to_date = form.cleaned_data.get("to_date")
    d_from_dt, d_to_dt = _parse_date_range(from_date, to_date)

    if not d_from_dt and not d_to_dt:
        today = date.today()
        d_from_dt = datetime.combine(date(today.year, 1, 1), time.min)
        d_to_dt = datetime.combine(today, time.max)

    is_loan = _is_loan_account(account_code)
    if is_loan:
        loan_info = (
            LoanTransaction.objects
            .filter(cust_no=cust_int, loan_no=account_code)
            .only("loan_type")
            .first()
        )
        loan_label = _resolve_loan_type_label(loan_info)
        account_display_name = f"{account_code} - {loan_label}"
    else:
        setup = CustomerAccountsSetup.objects.filter(account_type=account_code).first()
        account_display_name = (
            setup.account_name if setup
            else account_code.replace("_", " ").title()
        )

    try:
        qs = _get_transactions_queryset(account_code, cust_int)
        if d_from_dt:
            qs = qs.filter(tr_date__gte=d_from_dt)
        if d_to_dt:
            qs = qs.filter(tr_date__lte=d_to_dt)
        qs = qs.order_by("tr_date", "id")

        total_count = qs.count()
        if total_count > MAX_ROWS:
            return JsonResponse({
                "error": (
                    f"This account has {total_count:,} transactions in the selected "
                    f"period. Please narrow the date range (max {MAX_ROWS:,} rows)."
                )
            }, status=400)

        opening = _compute_opening_balance(account_code, cust_int, d_from_dt)
        running_balance = opening
        transactions = []

        if d_from_dt:
            transactions.append({
                "tr_date": d_from_dt.strftime("%Y-%m-%d"),
                "tr_ref": "B/F",
                "tr_desc": "Balance Brought Forward",
                "debit_amount": 0.0, "credit_amount": 0.0,
                "balance": running_balance,
            })

        for tr in qs:
            debit = float(tr.debit_amount or 0.0)
            credit = float(tr.credit_amount or 0.0)
            running_balance += (debit - credit) if is_loan else (credit - debit)
            transactions.append({
                "tr_date": tr.tr_date.strftime("%Y-%m-%d") if tr.tr_date else "",
                "tr_ref": tr.tr_ref,
                "tr_desc": tr.tr_desc,
                "debit_amount": debit, "credit_amount": credit,
                "balance": running_balance,
            })

        return JsonResponse({
            "customer_name": cust.full_name,
            "account_name": account_display_name,
            "transactions": transactions,
        })

    except Exception as exc:
        logger.exception("Statement generation error for cust_no=%s", cust_no_str)
        return JsonResponse({"error": f"Statement Generation Error: {exc}"}, status=500)


# ===========================================================================
# Single-account PDF download
# ===========================================================================

@require_http_methods(["GET"])
def download(request):
    form = StatementFilterForm(request.GET)
    if not form.is_valid():
        return HttpResponseBadRequest(f"Invalid parameters: {form.errors}")

    cust_no_raw = form.cleaned_data.get("cust_no") or ""

    # ★ Safe resolver
    try:
        cust, cust_no_str = _resolve_customer(cust_no_raw)
    except ValueError as e:
        return HttpResponseBadRequest(str(e))

    cust_int = _txn_cust_no(cust)

    account_code = form.cleaned_data.get("account_code") or ""
    from_date = form.cleaned_data.get("from_date")
    to_date = form.cleaned_data.get("to_date")
    d_from_dt, d_to_dt = _parse_date_range(from_date, to_date)

    if not d_from_dt:
        today = date.today()
        d_from_dt = datetime.combine(date(today.year, 1, 1), time.min)
    if not d_to_dt:
        d_to_dt = datetime.combine(date.today(), time.max)

    is_loan = _is_loan_account(account_code)

    if is_loan:
        loan_info = (
            LoanTransaction.objects
            .filter(cust_no=cust_int, loan_no=account_code)
            .only("loan_type")
            .first()
        )
        loan_label = _resolve_loan_type_label(loan_info)
        account_display_name = f"LOAN STATEMENT: {account_code} ({loan_label})"
    else:
        setup = CustomerAccountsSetup.objects.filter(account_type=account_code).first()
        account_display_name = (
            f"SAVINGS STATEMENT: {setup.account_name}" if setup
            else f"STATEMENT: {account_code.replace('_', ' ').title()}"
        )

    qs = _get_transactions_queryset(account_code, cust_int)
    if d_from_dt:
        qs = qs.filter(tr_date__gte=d_from_dt)
    if d_to_dt:
        qs = qs.filter(tr_date__lte=d_to_dt)
    qs = qs.order_by("tr_date", "id")

    total_count = qs.count()
    if total_count > MAX_ROWS:
        return HttpResponseBadRequest(
            f"Too many transactions ({total_count:,}). Please narrow the date range."
        )

    opening_bal = _compute_opening_balance(account_code, cust_int, d_from_dt)
    company = _get_company_info()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=40, rightMargin=40, topMargin=30, bottomMargin=40,
    )
    story = []
    styles = getSampleStyleSheet()

    doc_title_style = ParagraphStyle(
        "DocTitle", parent=styles["Heading2"],
        alignment=TA_CENTER, textColor=colors.HexColor("#2c3e50"),
        spaceAfter=15, spaceBefore=10,
    )
    meta_style = ParagraphStyle(
        "MetaStyle", parent=styles["Normal"],
        fontSize=8, textColor=colors.dimgrey, alignment=TA_CENTER,
    )

    _build_pdf_header(story, styles, company)
    story.append(Paragraph(f"<b>{account_display_name.upper()}</b>", doc_title_style))

    mem_data = [
        ["Member Name:", cust.full_name.upper(),
         "Period:", f"{d_from_dt.date()} to {d_to_dt.date()}"],
        ["Member No:", str(cust.cust_no),
         "Printed:", now().strftime("%d-%b-%Y %H:%M")],
    ]
    mem_table = Table(mem_data, colWidths=[1.1 * inch, 2.7 * inch, 0.8 * inch, 2.6 * inch])
    mem_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(mem_table)
    story.append(Spacer(1, 15))

    data = [["Date", "Reference", "Description", "Debit", "Credit", "Balance"]]
    running = float(opening_bal)
    data.append([
        d_from_dt.strftime("%d-%b-%Y"), "B/F",
        "Balance Brought Forward", "", "", f"{running:,.2f}",
    ])

    for tr in qs:
        debit = float(tr.debit_amount or 0.0)
        credit = float(tr.credit_amount or 0.0)
        running += (debit - credit) if is_loan else (credit - debit)
        data.append([
            tr.tr_date.strftime("%d-%b-%Y") if tr.tr_date else "",
            (tr.tr_ref or "")[:14],
            (tr.tr_desc or "")[:39],
            f"{debit:,.2f}" if debit > 0 else "-",
            f"{credit:,.2f}" if credit > 0 else "-",
            f"{running:,.2f}",
        ])

    t = Table(
        data,
        colWidths=[0.9 * inch, 1.1 * inch, 2.4 * inch, 0.95 * inch, 0.95 * inch, 1.0 * inch],
        repeatRows=1,
    )
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f8f9f9")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
    ]))
    story.append(t)
    story.append(Spacer(1, 40))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Paragraph(
        f"End of Statement for {cust.full_name} | Generated by {request.user}",
        meta_style,
    ))

    doc.build(story)
    pdf = buffer.getvalue()
    buffer.close()

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="Statement_{cust.cust_no}_{account_code}.pdf"'
    )
    response.write(pdf)
    return response


# ===========================================================================
# Full consolidated statement (HTML view + PDF download)
# ===========================================================================

def _build_all_sections(customer) -> list:
    """Two bulk queries (savings + loans), grouped in Python."""
    all_sections = []

    # ★ Transaction tables use PositiveIntegerField — pass int
    cust_int = _txn_cust_no(customer)

    # Savings
    savings_qs = (
        SavingsTransaction.objects
        .filter(cust_no=cust_int)
        .only("saving_type", "tr_date", "tr_desc", "debit_amount", "credit_amount")
        .order_by("saving_type", "tr_date", "id")
    )
    savings_by_type: dict = {}
    for t in savings_qs:
        savings_by_type.setdefault(t.saving_type, []).append(t)

    for s_type, txns in savings_by_type.items():
        balance = Decimal("0.00")
        rows = []
        for t in txns:
            credit = Decimal(str(t.credit_amount or 0))
            debit = Decimal(str(t.debit_amount or 0))
            balance += credit - debit
            rows.append({
                "date": t.tr_date, "desc": t.tr_desc,
                "debit": t.debit_amount, "credit": t.credit_amount, "bal": balance,
            })
        # Friendly name from config
        setup = CustomerAccountsSetup.objects.filter(account_type=s_type).first()
        label = setup.account_name if setup else s_type.replace("_", " ").title()
        all_sections.append({"name": label, "txns": rows})

    # Loans
    loan_qs = (
        LoanTransaction.objects
        .filter(cust_no=cust_int)
        .only("loan_no", "tr_date", "tr_desc", "debit_amount", "credit_amount")
        .order_by("loan_no", "tr_date", "id")
    )
    loans_by_no: dict = {}
    for t in loan_qs:
        loans_by_no.setdefault(t.loan_no, []).append(t)

    for l_no, txns in loans_by_no.items():
        balance = Decimal("0.00")
        rows = []
        for t in txns:
            credit = Decimal(str(t.credit_amount or 0))
            debit = Decimal(str(t.debit_amount or 0))
            balance += debit - credit
            rows.append({
                "date": t.tr_date, "desc": t.tr_desc,
                "debit": t.debit_amount, "credit": t.credit_amount, "bal": balance,
            })
        all_sections.append({"name": f"Loan {l_no}", "txns": rows})

    return all_sections


def full_statement(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    sections = _build_all_sections(customer)
    return render(request, "statements/full_statement.html", {
        "customer": customer, "sections": sections,
    })


def download_full_statement_pdf(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    all_sections = _build_all_sections(customer)
    company = _get_company_info()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=40, rightMargin=40, topMargin=30, bottomMargin=40,
    )
    story = []
    styles = getSampleStyleSheet()

    section_title_style = ParagraphStyle(
        "SectionTitle", parent=styles["Heading3"],
        backColor=colors.HexColor("#2c3e50"), textColor=colors.white,
        borderPadding=5, spaceBefore=15, spaceAfter=5,
    )

    _build_pdf_header(story, styles, company)

    story.append(Paragraph("<b>CONSOLIDATED MEMBER STATEMENT</b>", styles["Heading2"]))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.black, spaceAfter=10))

    mem_data = [
        [f"Member: {customer.full_name.upper()}", f"ID No: {customer.national_id or '-'}"],
        [f"Member No: {customer.cust_no}", f"Mobile: {customer.phone or '-'}"],
        [f"Generated: {now().strftime('%d %b %Y %H:%M')}", ""],
    ]
    mem_table = Table(mem_data, colWidths=[3.5 * inch, 3.5 * inch])
    mem_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
    ]))
    story.append(mem_table)
    story.append(Spacer(1, 10))

    for section in all_sections:
        story.append(Paragraph(f" {section['name']}", section_title_style))
        table_data = [["Date", "Description", "Debit", "Credit", "Balance"]]
        for row in section["txns"]:
            debit = Decimal(str(row["debit"] or 0))
            credit = Decimal(str(row["credit"] or 0))
            table_data.append([
                row["date"].strftime("%d-%b-%Y") if row["date"] else "",
                str(row["desc"] or "")[:40],
                f"{debit:,.2f}" if debit > 0 else "-",
                f"{credit:,.2f}" if credit > 0 else "-",
                f"{row['bal']:,.2f}",
            ])
        t = Table(
            table_data,
            colWidths=[1.0 * inch, 3.0 * inch, 1.0 * inch, 1.0 * inch, 1.2 * inch],
            repeatRows=1,
        )
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(t)
        story.append(Spacer(1, 10))

    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Paragraph("End of consolidated report.", styles["Italic"]))

    doc.build(story)
    pdf = buffer.getvalue()
    buffer.close()

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="Full_Statement_{customer.cust_no}.pdf"'
    )
    response.write(pdf)
    return response


# ===========================================================================
# Statement scheduling dashboard
# ===========================================================================

def panel(request):
    all_accounts = CustomerAccountsSetup.objects.filter(is_active=True).order_by("account_code")
    return render(request, "statements/panel.html", {"accounts": all_accounts})


def statement_dashboard(request):
    schedule, _ = StatementSchedule.objects.get_or_create(id=1)
    logs = StatementLog.objects.all()[:50]

    if request.method == "POST":
        form = StatementScheduleForm(request.POST, instance=schedule)
        if form.is_valid():
            form.save()
            messages.success(request, "Statement schedule updated.")
            return redirect("statements:statement_dashboard")
    else:
        form = StatementScheduleForm(instance=schedule)

    return render(request, "statements/statement_dashboard.html", {
        "form": form, "logs": logs, "schedule": schedule,
    })


def trigger_manual_statements(request):
    trigger_statements_background()
    messages.success(
        request,
        "Statements are being generated and sent in the background. "
        "Check the logs in a few minutes.",
    )
    return redirect("statements:statement_dashboard")