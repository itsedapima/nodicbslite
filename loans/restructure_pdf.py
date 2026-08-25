"""
loans/restructure_pdf.py
=========================
PDF summary generators for:
  • Loan Restructure
  • Guarantor Defaulter Offload
"""

import io
from decimal import Decimal

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


BRAND_NAVY = colors.HexColor("#0E2B4D")
BRAND_GOLD = colors.HexColor("#B58A3E")
LIGHT_BG = colors.HexColor("#F5F7FA")


def _base_styles():
    ss = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "Title", parent=ss["Title"],
            fontSize=16, textColor=BRAND_NAVY, alignment=TA_CENTER,
            spaceAfter=8, fontName="Helvetica-Bold",
        ),
        "subtitle": ParagraphStyle(
            "Sub", parent=ss["Normal"],
            fontSize=10, alignment=TA_CENTER, textColor=colors.grey,
            spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "H2", parent=ss["Heading2"], fontSize=11,
            textColor=BRAND_NAVY, spaceBefore=8, spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "Body", parent=ss["BodyText"], fontSize=9, leading=12,
        ),
        "small": ParagraphStyle(
            "Small", parent=ss["Normal"], fontSize=7, textColor=colors.grey,
        ),
    }


def _header(styles, title, subtitle):
    org = getattr(settings, "SACCO_NAME", "NODi CBS")
    return [
        Paragraph(org, styles["title"]),
        Paragraph(title, styles["h2"]),
        Paragraph(subtitle, styles["subtitle"]),
        HRFlowable(width="100%", thickness=1, color=BRAND_GOLD, spaceAfter=10),
    ]


def _kv_table(pairs, col_widths=(2.2 * inch, 4.0 * inch)):
    tbl = Table(pairs, colWidths=list(col_widths))
    tbl.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), BRAND_NAVY),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#DDDDDD")),
    ]))
    return tbl


def _money(v):
    try:
        return f"KES {Decimal(str(v)):,.2f}"
    except Exception:
        return str(v)


# ═════════════════════════════════════════════════════════════════════
#  RESTRUCTURE SUMMARY PDF
# ═════════════════════════════════════════════════════════════════════

def restructure_summary_pdf(loan, result) -> HttpResponse:
    """
    Build an HttpResponse containing a PDF summary of a completed
    restructure. `result` is a RestructureResult dataclass instance.
    """
    styles = _base_styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.6*inch, rightMargin=0.6*inch,
        topMargin=0.6*inch, bottomMargin=0.6*inch,
    )
    story = []
    story.extend(_header(
        styles,
        "Loan Restructure Summary",
        f"Reference: {loan.loan_no}   ·   Generated {timezone.now():%Y-%m-%d %H:%M}",
    ))

    orig = result.original_snapshot

    story.append(Paragraph("Member Details", styles["h2"]))
    story.append(_kv_table([
        ["Member Number", loan.customer.cust_no],
        ["Member Name", loan.customer.full_name],
        ["Phone",
         getattr(loan.customer, "phone", "") or
         getattr(loan.customer, "mobile", "") or "—"],
        ["Product", loan.loan_type.account_name],
    ]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Original Terms (Prior to Restructure)", styles["h2"]))
    story.append(_kv_table([
        ["Original Loan No.", orig.get("original_loan_no", loan.loan_no)],
        ["Original Loan Date", orig.get("original_loan_date", "—")],
        ["Original Principal", _money(orig.get("original_principal", 0))],
        ["Original Installment", _money(orig.get("original_installment", 0))],
        ["Original Period", f"{orig.get('original_loan_period', '—')} months"],
        ["Original Interest Rate",
         f"{orig.get('original_interest_rate', '—')}%"],
    ]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Balances at Restructure", styles["h2"]))
    story.append(_kv_table([
        ["Outstanding Balance",
         _money(result.outstanding_at_restructure)],
        ["    · Principal Portion",
         _money(result.principal_at_restructure)],
        ["    · Interest Portion",
         _money(result.interest_at_restructure)],
        ["Total Repaid Before Restructure",
         _money(orig.get("total_repaid_before_restructure", 0))],
    ]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("New Terms (Effective Now)", styles["h2"]))
    story.append(_kv_table([
        ["New Loan Date (Aging Reset)", result.new_loan_date],
        ["New Principal (= Outstanding)",
         _money(result.outstanding_at_restructure)],
        ["New Repayment Period", f"{result.new_period} months"],
        ["New Monthly Installment", _money(result.new_installment)],
        ["Restructure Fee Rate",
         f"{orig.get('restructure_fee_rate', 0)}%"],
        ["Restructure Fee Charged", _money(result.restructure_fee)],
        ["Fee Transaction Reference", result.fee_reference or "—"],
    ]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Reason & Authorisation", styles["h2"]))
    story.append(_kv_table([
        ["Reason", orig.get("reason", "—")],
        ["Restructured By", orig.get("restructured_by", "—")],
        ["Restructured At", orig.get("snapshot_taken_at", "—")],
    ]))
    story.append(Spacer(1, 10))

    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "This loan is now treated by the system as a fresh facility. "
        "Arrears aging starts from the new loan date. Prior arrears and "
        "original terms are preserved in the audit trail.",
        styles["small"],
    ))

    doc.build(story)
    buf.seek(0)

    resp = HttpResponse(buf.getvalue(), content_type="application/pdf")
    resp["Content-Disposition"] = (
        f'inline; filename="restructure_{loan.loan_no}.pdf"'
    )
    return resp


# ═════════════════════════════════════════════════════════════════════
#  GUARANTOR OFFLOAD SUMMARY PDF
# ═════════════════════════════════════════════════════════════════════

def guarantor_offload_summary_pdf(loan, result, reason="") -> HttpResponse:
    """
    Build an HttpResponse with a PDF summary of a completed guarantor
    offload. `result` is an OffloadResult dataclass instance.
    """
    styles = _base_styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.6*inch, rightMargin=0.6*inch,
        topMargin=0.6*inch, bottomMargin=0.6*inch,
    )
    story = []
    story.extend(_header(
        styles,
        "Guarantor Defaulter Offload Summary",
        f"Original Loan: {loan.loan_no}   ·   Ref: {result.reference}   ·   "
        f"Generated {timezone.now():%Y-%m-%d %H:%M}",
    ))

    story.append(Paragraph("Defaulter (Original Borrower)", styles["h2"]))
    story.append(_kv_table([
        ["Member Number", loan.customer.cust_no],
        ["Member Name", loan.customer.full_name],
        ["Loan Number", loan.loan_no],
        ["Product", loan.loan_type.account_name],
        ["Original Loan Date",
         loan.loan_date.strftime("%Y-%m-%d") if loan.loan_date else "—"],
        ["Original Principal", _money(loan.principal)],
    ]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Balances Analysed", styles["h2"]))
    total_outstanding = (
        result.principal_balance_before + result.interest_balance_before
    )
    story.append(_kv_table([
        ["Total Outstanding Balance", _money(total_outstanding)],
        ["    · Principal Balance (distributed)",
         _money(result.principal_balance_before)],
        ["    · Interest / Penalties (retained)",
         _money(result.interest_balance_before)],
    ]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Guarantor Distribution", styles["h2"]))
    header_row = ["#", "Guarantor", "Cust No.",
                  "Guarantee (KES)", "% of Pool",
                  "Allocated (KES)", "New Loan No."]
    data_rows = [header_row]
    for i, a in enumerate(result.allocations, start=1):
        data_rows.append([
            str(i),
            a.guarantor_name,
            a.guarantor_cust_no,
            f"{a.guarantee_amount:,.2f}",
            f"{a.percentage:.2f}%",
            f"{a.allocated_amount:,.2f}",
            a.new_loan_no or "—",
        ])
    data_rows.append([
        "", "TOTALS", "",
        f"{result.total_pool:,.2f}", "100.00%",
        f"{result.total_allocated:,.2f}", "",
    ])

    tbl = Table(data_rows, colWidths=[
        0.3*inch, 1.8*inch, 0.9*inch, 1.1*inch,
        0.7*inch, 1.1*inch, 1.0*inch,
    ])
    tbl.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (3, 1), (5, -1), "RIGHT"),
        ("ALIGN", (4, 1), (4, -1), "CENTER"),
        ("BACKGROUND", (0, -1), (-1, -1), LIGHT_BG),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 10))

    story.append(Paragraph("Residual Balance on Original Loan", styles["h2"]))
    story.append(_kv_table([
        ["Residual Balance (Interest + Penalties)",
         _money(result.residual_balance)],
        ["Action Required",
         "May be settled via inter-account transfer from the "
         "defaulter's savings/deposits."],
    ]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Authorisation & Notes", styles["h2"]))
    story.append(_kv_table([
        ["Batch Reference", result.reference],
        ["Reason / Notes", reason or "—"],
        ["Processed At", timezone.now().strftime("%Y-%m-%d %H:%M")],
    ]))
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "New defaulter recovery loans have been created for each guarantor "
        "with the allocated amount. The original loan's principal portion "
        "has been credited (repaid) by the same amount. Interest / penalty "
        "balance remains on the original loan for officer follow-up.",
        styles["small"],
    ))

    doc.build(story)
    buf.seek(0)

    resp = HttpResponse(buf.getvalue(), content_type="application/pdf")
    resp["Content-Disposition"] = (
        f'inline; filename="offload_{loan.loan_no}_{result.reference}.pdf"'
    )
    return resp
