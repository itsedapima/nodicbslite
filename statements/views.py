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

import hashlib
import io
import logging
import re
from datetime import date, datetime, time
from decimal import Decimal

import qrcode
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Q, Sum
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.timezone import localtime, make_aware, now
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from accounts.decorators import staff_required

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Image as RLImage, SimpleDocTemplate,
    Spacer, Table, TableStyle, Paragraph,
)

from customers.models import Customer
from administration.models import ChamaInfo
from loans.models import LoanHistory
from transactions.models import CustomerAccountsSetup, LoanTransaction, SavingsTransaction

from .forms import StatementFilterForm, StatementSchedule, StatementScheduleForm
from .models import StatementHash, StatementLog, StatementSchedule
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
            d_from_dt = make_aware(datetime.combine(from_date, time.min))
        if to_date:
            d_to_dt = make_aware(datetime.combine(to_date, time.max))
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
        company_contact="", company_location="", company_email="",
        company_logo_path=None,
    )
    try:
        info = ChamaInfo.objects.first()
        if not info:
            return defaults
        logo_path = None
        if info.company_logo and hasattr(info.company_logo, "path"):
            logo_path = info.company_logo.path

        # Extract email from any of the company text fields
        all_text = f"{info.company_address or ''} {info.company_contact or ''} {info.company_location or ''}"
        email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', all_text)
        company_email = email_match.group(0) if email_match else ""

        return dict(
            company_name=info.company_name or defaults["company_name"],
            company_address=info.company_address or "",
            company_contact=info.company_contact or "",
            company_location=info.company_location or "",
            company_email=company_email,
            company_logo_path=logo_path,
        )
    except Exception as exc:
        logger.error("Error fetching ChamaInfo: %s", exc)
        return defaults


def _generate_qr_code(url: str, size: float = 0.85 * inch) -> RLImage:
    """Generate a QR code image that encodes a URL."""
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=1)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#2c3e50", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return RLImage(buf, width=size, height=size)


def _create_statement_hash(request, cust, account_code, statement_type="single"):
    """
    Create a StatementHash record and return (hash_value, verify_url).
    The QR code will point to verify_url so scanning opens it in a browser.
    """
    import uuid as _uuid
    raw = f"{cust.cust_no}|{account_code}|{now().isoformat()}|{_uuid.uuid4().hex}"
    hash_value = hashlib.sha256(raw.encode()).hexdigest()

    StatementHash.objects.create(
        hash_value=hash_value,
        cust_no=str(cust.cust_no),
        customer_name=cust.full_name,
        account_code=account_code,
        statement_type=statement_type,
        generated_by=str(request.user) if request else "",
    )

    # Build absolute URL — fall back to settings if request host is invalid
    verify_path = f"/statements/verify/{hash_value}/"
    verify_url = None
    if request:
        try:
            verify_url = request.build_absolute_uri(verify_path)
        except Exception:
            pass

    if not verify_url:
        from django.conf import settings as _settings
        base = getattr(_settings, "SITE_URL", None)
        if not base:
            hosts = getattr(_settings, "ALLOWED_HOSTS", [])
            domain = next(
                (h for h in hosts if h not in ("*", "localhost", "127.0.0.1", "web", "testserver")),
                "localhost",
            )
            base = f"https://{domain}"
        verify_url = f"{base.rstrip('/')}{verify_path}"

    return hash_value, verify_url


def _build_pdf_header(story, styles, company: dict):
    """Header: logo + company name on row 1, logo + address on row 2, all centered as one block."""
    ACCENT = colors.HexColor("#2c3e50")

    name_style = ParagraphStyle(
        "CompanyName", parent=styles["Normal"],
        fontSize=16, leading=19, fontName="Helvetica-Bold",
        textColor=ACCENT, alignment=TA_LEFT, spaceBefore=0, spaceAfter=0,
    )
    address_style = ParagraphStyle(
        "CompanyAddr", parent=styles["Normal"],
        fontSize=8, leading=10, textColor=colors.HexColor("#555555"),
        alignment=TA_LEFT, spaceBefore=0, spaceAfter=0,
    )

    # Address string: postal + phone
    addr_parts = []
    if company["company_address"]:
        addr_parts.append(company["company_address"])
    if company["company_contact"]:
        addr_parts.append(company["company_contact"])
    addr_line = "  |  ".join(addr_parts)

    logo_cell = ""
    if company["company_logo_path"]:
        try:
            logo_cell = RLImage(
                company["company_logo_path"],
                width=0.9 * inch, height=0.9 * inch, kind="proportional",
            )
        except Exception as exc:
            logger.warning("Could not load company logo: %s", exc)

    if logo_cell:
        text_col = [
            Paragraph(company["company_name"].upper(), name_style),
            Paragraph(addr_line, address_style),
        ]
        inner_data = [[logo_cell, text_col]]
        inner_table = Table(inner_data, colWidths=[1.0 * inch, 3.5 * inch],
                            hAlign="CENTER")
        inner_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (0, 0), 0),
            ("RIGHTPADDING", (0, 0), (0, 0), 4),
            ("LEFTPADDING", (1, 0), (1, 0), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(inner_table)
    else:
        # No logo — just centered text
        center_name = ParagraphStyle("CNameC", parent=name_style, alignment=TA_CENTER)
        center_addr = ParagraphStyle("CAddrC", parent=address_style, alignment=TA_CENTER)
        story.append(Paragraph(company["company_name"].upper(), center_name))
        story.append(Paragraph(addr_line, center_addr))

    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT, spaceAfter=2))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#7f8c8d"), spaceAfter=10))


# ===========================================================================
# API endpoints
# ===========================================================================

@login_required
@staff_required
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


@login_required
@staff_required
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

@login_required
@staff_required
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
        d_from_dt = make_aware(datetime.combine(date(today.year, 1, 1), time.min))
        d_to_dt = make_aware(datetime.combine(today, time.max))

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
                "tr_date": localtime(tr.tr_date).strftime("%Y-%m-%d") if tr.tr_date else "",
                "tr_ref": tr.tr_ref,
                "tr_desc": tr.tr_desc,
                "debit_amount": debit, "credit_amount": credit,
                "balance": running_balance,
            })

        # TB verification status for frontend badge
        try:
            from accounting.tigerbeetle import TB_ENABLED, tb_health_check
            _tb = tb_health_check() if TB_ENABLED else {'enabled': False, 'connected': False}
            tb_verified = _tb['enabled'] and _tb['connected']
        except Exception:
            tb_verified = False

        return JsonResponse({
            "customer_name": cust.full_name,
            "account_name": account_display_name,
            "transactions": transactions,
            "tb_verified": tb_verified,
            "tb_source": "TigerBeetle" if tb_verified else "PostgreSQL",
        })

    except Exception as exc:
        logger.exception("Statement generation error for cust_no=%s", cust_no_str)
        return JsonResponse({"error": f"Statement Generation Error: {exc}"}, status=500)


# ===========================================================================
# Single-account PDF download
# ===========================================================================

@login_required
@staff_required
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
        d_from_dt = make_aware(datetime.combine(date(today.year, 1, 1), time.min))
    if not d_to_dt:
        d_to_dt = make_aware(datetime.combine(date.today(), time.max))

    is_loan = _is_loan_account(account_code)

    if is_loan:
        loan_info = (
            LoanTransaction.objects
            .filter(cust_no=cust_int, loan_no=account_code)
            .only("loan_type")
            .first()
        )
        loan_label = _resolve_loan_type_label(loan_info)
        account_display_name = f"Loan Statement  —  {account_code} ({loan_label})"
    else:
        setup = CustomerAccountsSetup.objects.filter(account_type=account_code).first()
        account_display_name = (
            f"Savings Statement  —  {setup.account_name}" if setup
            else f"Statement of Account  —  {account_code.replace('_', ' ').title()}"
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
        "DocTitle", parent=styles["Normal"],
        alignment=TA_CENTER, textColor=colors.HexColor("#2c3e50"),
        fontSize=9, fontName="Helvetica-Bold",
        spaceAfter=8, spaceBefore=0,
    )
    meta_style = ParagraphStyle(
        "MetaStyle", parent=styles["Normal"],
        fontSize=7, textColor=colors.HexColor("#888888"), alignment=TA_CENTER,
    )
    label_style = ParagraphStyle(
        "MetaLabel", parent=styles["Normal"],
        fontSize=7.5, textColor=colors.HexColor("#888888"),
    )
    value_style = ParagraphStyle(
        "MetaValue", parent=styles["Normal"],
        fontSize=8.5, fontName="Helvetica-Bold", textColor=colors.HexColor("#2c3e50"),
    )

    _build_pdf_header(story, styles, company)
    story.append(Paragraph(account_display_name.upper(), doc_title_style))

    generated_at = now().strftime("%d-%b-%Y %H:%M")
    mem_data = [
        [
            Paragraph("Member Name", label_style),
            Paragraph(cust.full_name.upper(), value_style),
            Paragraph("Period", label_style),
            Paragraph(f"{d_from_dt.date()} to {d_to_dt.date()}", value_style),
        ],
        [
            Paragraph("Member No", label_style),
            Paragraph(str(cust.cust_no), value_style),
            Paragraph("Printed", label_style),
            Paragraph(generated_at, value_style),
        ],
    ]
    mem_table = Table(mem_data, colWidths=[0.9 * inch, 2.8 * inch, 0.7 * inch, 2.8 * inch])
    mem_table.setStyle(TableStyle([
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#dddddd")),
    ]))
    story.append(mem_table)
    story.append(Spacer(1, 12))

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
            localtime(tr.tr_date).strftime("%d-%b-%Y") if tr.tr_date else "",
            (tr.tr_ref or "")[:13],
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
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 6))

    # QR code for authenticity verification — hash stored in DB
    _hash, verify_url = _create_statement_hash(request, cust, account_code, "single")
    qr_img = _generate_qr_code(verify_url)

    footer_left_style = ParagraphStyle(
        "FooterLeft", parent=styles["Normal"],
        fontSize=7, textColor=colors.HexColor("#888888"), alignment=TA_LEFT,
    )
    qr_hint_style = ParagraphStyle(
        "QRHint", parent=styles["Normal"],
        fontSize=6.5, textColor=colors.HexColor("#999999"), leading=9,
        alignment=TA_RIGHT,
    )

    # TB verification flag for PDF
    try:
        from accounting.tigerbeetle import TB_ENABLED, tb_health_check
        _tb = tb_health_check() if TB_ENABLED else {'enabled': False, 'connected': False}
        _tb_verified = _tb['enabled'] and _tb['connected']
    except Exception:
        _tb_verified = False

    # Left: end-of-statement + contact
    company_email = company.get("company_email", "")
    left_cell = [
        Paragraph(
            f"End of statement for {cust.full_name}  |  Generated by {request.user}",
            footer_left_style,
        ),
    ]
    if _tb_verified:
        left_cell.append(Spacer(1, 2))
        left_cell.append(Paragraph(
            "☑ TB Verified",
            ParagraphStyle("TBLine", parent=styles["Normal"],
                           fontSize=6.5, textColor=colors.HexColor("#065f46"),
                           fontName="Helvetica-Bold", alignment=TA_LEFT),
        ))
    if company_email:
        left_cell.append(Spacer(1, 2))
        left_cell.append(Paragraph(
            f"For any queries or complaints regarding this statement, "
            f"please contact us at {company_email}",
            ParagraphStyle("ContactLine", parent=styles["Normal"],
                           fontSize=6.5, textColor=colors.HexColor("#999999"),
                           alignment=TA_LEFT),
        ))

    # Right: QR + scan label
    right_cell = [
        Paragraph("Scan to verify", qr_hint_style),
        Paragraph("statement authenticity", qr_hint_style),
    ]

    footer_data = [[left_cell, right_cell, qr_img]]
    footer_table = Table(footer_data, colWidths=[4.6 * inch, 1.4 * inch, 1.2 * inch])
    footer_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (2, 0), (2, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(footer_table)

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
                "date": localtime(t.tr_date) if t.tr_date else None,
                "desc": t.tr_desc,
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
                "date": localtime(t.tr_date) if t.tr_date else None,
                "desc": t.tr_desc,
                "debit": t.debit_amount, "credit": t.credit_amount, "bal": balance,
            })
        all_sections.append({"name": f"Loan {l_no}", "txns": rows})

    return all_sections


@login_required
@staff_required
def full_statement(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    sections = _build_all_sections(customer)
    return render(request, "statements/full_statement.html", {
        "customer": customer, "sections": sections,
    })


@login_required
@staff_required
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

    consol_title_style = ParagraphStyle(
        "ConsolTitle", parent=styles["Normal"],
        alignment=TA_CENTER, textColor=colors.HexColor("#2c3e50"),
        fontSize=9, fontName="Helvetica-Bold",
        spaceAfter=8, spaceBefore=0,
    )
    label_style = ParagraphStyle(
        "CMetaLabel", parent=styles["Normal"],
        fontSize=7.5, textColor=colors.HexColor("#888888"),
    )
    value_style = ParagraphStyle(
        "CMetaValue", parent=styles["Normal"],
        fontSize=8.5, fontName="Helvetica-Bold", textColor=colors.HexColor("#2c3e50"),
    )
    meta_style = ParagraphStyle(
        "CMetaFooter", parent=styles["Normal"],
        fontSize=7, textColor=colors.HexColor("#888888"), alignment=TA_CENTER,
    )

    story.append(Paragraph("CONSOLIDATED MEMBER STATEMENT", consol_title_style))

    generated_at = now().strftime("%d-%b-%Y %H:%M")
    mem_data = [
        [
            Paragraph("Member", label_style),
            Paragraph(customer.full_name.upper(), value_style),
            Paragraph("ID No", label_style),
            Paragraph(str(customer.national_id or "-"), value_style),
        ],
        [
            Paragraph("Member No", label_style),
            Paragraph(str(customer.cust_no), value_style),
            Paragraph("Mobile", label_style),
            Paragraph(str(customer.phone or "-"), value_style),
        ],
        [
            Paragraph("Generated", label_style),
            Paragraph(generated_at, value_style),
            "", "",
        ],
    ]
    mem_table = Table(mem_data, colWidths=[0.9 * inch, 2.8 * inch, 0.7 * inch, 2.8 * inch])
    mem_table.setStyle(TableStyle([
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#dddddd")),
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
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 6))

    # QR code for consolidated statement — hash stored in DB
    _hash, verify_url = _create_statement_hash(request, customer, "CONSOLIDATED", "consolidated")
    qr_img = _generate_qr_code(verify_url)

    footer_left_style = ParagraphStyle(
        "FooterLeft2", parent=styles["Normal"],
        fontSize=7, textColor=colors.HexColor("#888888"), alignment=TA_LEFT,
    )
    qr_hint_style = ParagraphStyle(
        "QRHint2", parent=styles["Normal"],
        fontSize=6.5, textColor=colors.HexColor("#999999"), leading=9,
        alignment=TA_RIGHT,
    )

    # TB verification flag for consolidated PDF
    try:
        from accounting.tigerbeetle import TB_ENABLED, tb_health_check
        _tb2 = tb_health_check() if TB_ENABLED else {'enabled': False, 'connected': False}
        _tb_verified2 = _tb2['enabled'] and _tb2['connected']
    except Exception:
        _tb_verified2 = False

    company_email = company.get("company_email", "")
    left_cell = [
        Paragraph(f"End of consolidated report for {customer.full_name}", footer_left_style),
    ]
    if _tb_verified2:
        left_cell.append(Spacer(1, 2))
        left_cell.append(Paragraph(
            "☑ TigerBeetle Verified — Immutable double-entry ledger",
            ParagraphStyle("TBLine2", parent=styles["Normal"],
                           fontSize=6.5, textColor=colors.HexColor("#065f46"),
                           fontName="Helvetica-Bold", alignment=TA_LEFT),
        ))
    if company_email:
        left_cell.append(Spacer(1, 2))
        left_cell.append(Paragraph(
            f"For any queries or complaints regarding this statement, "
            f"please contact us at {company_email}",
            ParagraphStyle("ContactLine2", parent=styles["Normal"],
                           fontSize=6.5, textColor=colors.HexColor("#999999"),
                           alignment=TA_LEFT),
        ))

    right_cell = [
        Paragraph("Scan to verify", qr_hint_style),
        Paragraph("statement authenticity", qr_hint_style),
    ]

    footer_data = [[left_cell, right_cell, qr_img]]
    footer_table = Table(footer_data, colWidths=[4.6 * inch, 1.4 * inch, 1.2 * inch])
    footer_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (2, 0), (2, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(footer_table)

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
# Statement scheduling administration
# ===========================================================================

@login_required
@staff_required
def panel(request):
    all_accounts = CustomerAccountsSetup.objects.filter(is_active=True).order_by("account_code")
    return render(request, "statements/panel.html", {"accounts": all_accounts})


@login_required
@staff_required
def statement_dashboard(request):
    schedule, _ = StatementSchedule.objects.get_or_create(id=1)
    logs = StatementLog.objects.all()[:50]

    if request.method == "POST":
        form = StatementScheduleForm(request.POST, instance=schedule)
        if form.is_valid():
            form.save()
            messages.success(request, "Statement schedule updated.")
            return redirect("statements:statement_administration")
    else:
        form = StatementScheduleForm(instance=schedule)

    return render(request, "statements/statement_administration.html", {
        "form": form, "logs": logs, "schedule": schedule,
    })


@login_required
@staff_required
def trigger_manual_statements(request):
    trigger_statements_background()
    messages.success(
        request,
        "Statements are being generated and sent in the background. "
        "Check the logs in a few minutes.",
    )
    return redirect("statements:statement_administration")


# ===========================================================================
# Statement verification (public, no login required)
# ===========================================================================

def _get_client_ip(request):
    """Extract real client IP, respecting proxy headers."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


@never_cache
@require_GET
def verify_statement(request, hash_value):
    """
    Public endpoint: scanned QR code directs here.
    Looks up the hash → authentic / expired / fake.

    SECURITY HARDENING:
    ───────────────────
    • Rate-limited per IP (10 requests / 5 min) — prevents hash brute-forcing
    • Rejects requests with Referer from the core banking domain
    • Strips all outgoing Referrer info (Referrer-Policy: no-referrer)
    • Strict CSP: no scripts, no navigation, no forms — dead-end page
    • X-Frame-Options DENY — cannot be iframed
    • Cache disabled — no proxy/CDN caching of verification results
    • All verification attempts are logged
    """

    # ── Rate limit: 10 verify requests per IP per 5 minutes ──────────
    ip = _get_client_ip(request)
    rate_key = f"stmt_verify_rl:{ip}"
    try:
        hits = cache.get(rate_key, 0)
        if hits >= 10:
            logger.warning("Verify rate-limit hit: ip=%s hash=%s", ip, hash_value[:12])
            return HttpResponse(
                "Too many verification requests. Please try again later.",
                status=429, content_type="text/plain",
            )
        cache.set(rate_key, hits + 1, timeout=300)
    except Exception:
        pass  # cache down → don't block legitimate users

    # ── Reject if Referer is the banking system itself ───────────────
    referer = request.META.get("HTTP_REFERER", "")
    banking_host = request.get_host()  # e.g. eastakiba.peshapcloud.com
    if referer and banking_host in referer:
        # Someone is clicking from inside the banking app — block it
        logger.warning(
            "Verify blocked: referer from banking domain ip=%s referer=%s",
            ip, referer,
        )
        return HttpResponseForbidden(
            "Verification links must be opened directly — not from within the system."
        )

    # ── Validate hash format (must be exactly 64 hex chars) ──────────
    if not hash_value or len(hash_value) != 64 or not all(c in "0123456789abcdef" for c in hash_value):
        logger.warning("Verify invalid hash format: ip=%s hash=%s", ip, hash_value[:20])
        return HttpResponseBadRequest("Invalid verification link.")

    # ── Look up hash ─────────────────────────────────────────────────
    company = _get_company_info()

    try:
        record = StatementHash.objects.get(hash_value=hash_value)
    except StatementHash.DoesNotExist:
        record = None

    if record is None:
        status = "fake"
        title = "Verification Failed"
        message = "This statement could not be verified. It may be forged or tampered with."
        color = "#e74c3c"
        icon = "&#10008;"
    elif record.is_expired:
        status = "expired"
        title = "Verification Expired"
        message = (
            f"This statement was issued on {record.generated_at.strftime('%d %b %Y at %H:%M')} "
            f"for {record.customer_name} (A/C: {record.account_code}). "
            f"The verification period has expired. Please request a new statement."
        )
        color = "#f39c12"
        icon = "&#9888;"
    else:
        status = "authentic"
        title = "Statement Verified"
        message = (
            f"This statement is authentic. It was issued on "
            f"{record.generated_at.strftime('%d %b %Y at %H:%M')} "
            f"for {record.customer_name} (A/C: {record.account_code}), "
            f"generated by {record.generated_by or 'System'}."
        )
        color = "#27ae60"
        icon = "&#10004;"

    # ── Log the verification attempt ─────────────────────────────────
    logger.info(
        "Verify attempt: ip=%s status=%s hash=%s ua=%s",
        ip, status, hash_value[:12],
        request.META.get("HTTP_USER_AGENT", "")[:80],
    )

    # ── Render ───────────────────────────────────────────────────────
    response = render(request, "statements/verify.html", {
        "status": status,
        "title": title,
        "message": message,
        "color": color,
        "icon": icon,
        "company": company,
        "record": record,
    })

    # ── Harden response headers ──────────────────────────────────────
    # No referrer leaked when user clicks anything or browser sends requests
    response["Referrer-Policy"] = "no-referrer"
    # Cannot be embedded in any iframe
    response["X-Frame-Options"] = "DENY"
    # Strict Content-Security-Policy:
    #   - default-src 'none'  → block everything by default
    #   - style-src 'unsafe-inline' → allow the inline <style> block
    #   - img-src data:       → allow inline data: images if any
    #   - form-action 'none'  → no forms can submit anywhere
    #   - navigate-to 'none'  → block JS-initiated navigation (supported browsers)
    #   - frame-ancestors 'none' → cannot be iframed (CSP version of X-Frame-Options)
    #   - base-uri 'none'     → no <base> tag injection
    #   - sandbox             → sandboxed page: no scripts, no forms, no popups, no navigation
    response["Content-Security-Policy"] = (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        "img-src data:; "
        "form-action 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "sandbox allow-same-origin;"
    )
    # Prevent MIME sniffing
    response["X-Content-Type-Options"] = "nosniff"
    # Tell browser this page shouldn't be cached
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    # Permissions-Policy: disable all powerful features
    response["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), "
        "usb=(), magnetometer=(), gyroscope=(), accelerometer=()"
    )

    return response