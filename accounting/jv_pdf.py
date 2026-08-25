"""
accounting/jv_pdf.py
=====================
PDF summary generator for a posted Journal Voucher.
"""

import io
from decimal import Decimal

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


NAVY = colors.HexColor("#0E2B4D")
GOLD = colors.HexColor("#B58A3E")
LIGHT = colors.HexColor("#F5F7FA")


def _money(v) -> str:
    try:
        return f"{Decimal(str(v)):,.2f}"
    except Exception:
        return str(v)


def journal_voucher_pdf(voucher, resolved_lines=None) -> HttpResponse:
    """
    Render a landscape PDF summary of a posted JournalVoucher.
    `voucher` is a JournalVoucher instance. `resolved_lines` is optional
    enriched list from validate_lines(); if omitted, we render straight
    from the persisted JournalVoucherLine rows.
    """
    ss = getSampleStyleSheet()
    style_title = ParagraphStyle(
        "Title", parent=ss["Title"],
        fontSize=16, textColor=NAVY, alignment=TA_CENTER,
        fontName="Helvetica-Bold",
    )
    style_sub = ParagraphStyle(
        "Sub", parent=ss["Normal"], fontSize=10, alignment=TA_CENTER,
        textColor=colors.grey, spaceAfter=6,
    )
    style_body = ParagraphStyle(
        "Body", parent=ss["BodyText"], fontSize=9, leading=12,
    )
    style_small = ParagraphStyle(
        "Small", parent=ss["Normal"], fontSize=7, textColor=colors.grey,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=0.5*inch, rightMargin=0.5*inch,
        topMargin=0.5*inch, bottomMargin=0.5*inch,
    )
    story = []
    org = getattr(settings, "SACCO_NAME", "NODi CBS")

    # ── Header ────────────────────────────────────────────────
    story.append(Paragraph(org, style_title))
    story.append(Paragraph("Journal Voucher", style_sub))
    story.append(HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=8))

    # ── Voucher meta ──────────────────────────────────────────
    meta_pairs = [
        ["Voucher No.", voucher.voucher_no],
        ["Date", str(voucher.voucher_date)],
        ["Description", voucher.description or "—"],
        ["Status", voucher.get_status_display()],
        ["Total Amount", f"KES {_money(voucher.total_amount)}"],
        ["Created By", str(getattr(voucher.created_by, "username", "system"))],
        ["Posted At",
         voucher.posted_at.strftime("%Y-%m-%d %H:%M") if voucher.posted_at else "—"],
        ["Generated", timezone.now().strftime("%Y-%m-%d %H:%M")],
    ]
    meta_tbl = Table(meta_pairs, colWidths=[1.4*inch, 4.0*inch])
    meta_tbl.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), NAVY),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#DDDDDD")),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 10))

    # ── Lines table ───────────────────────────────────────────
    header = ["#", "Type", "Cust No.", "Name", "Member Account",
              "SACCO Account", "Description", "Debit", "Credit"]
    rows = [header]

    lines_source = resolved_lines
    if not lines_source:
        # Rebuild from persisted rows
        lines_source = []
        for i, l in enumerate(voucher.lines.all().select_related(
            "sacco_account", "customer", "member_product",
        ), start=1):
            is_cust = (l.entry_type == "customer")
            lines_source.append({
                "row": i,
                "entry_type": l.entry_type,
                "cust_no": l.customer.cust_no if l.customer else "",
                "customer_name": l.customer.full_name if l.customer else "",
                "member_account_ref": l.member_account_ref or "",
                "sacco_display": (
                    f"{l.sacco_account.account_code} — {l.sacco_account.account_name}"
                    if l.sacco_account else ""
                ),
                "description": l.description or "",
                "debit": l.debit_amount or Decimal("0"),
                "credit": l.credit_amount or Decimal("0"),
            })

    total_dr = total_cr = Decimal("0")
    for l in lines_source:
        # Uniform pull no matter which shape the caller passed
        row_no = l.get("row", "")
        et = l.get("entry_type", "sacco")
        cust_no = l.get("cust_no", "") or (
            l["customer"].cust_no if isinstance(l.get("customer"), object)
            and hasattr(l.get("customer", None), "cust_no") else ""
        )
        cust_name = l.get("customer_name", "") or (
            l["customer"].full_name if hasattr(l.get("customer", None), "full_name") else ""
        )
        member_ref = l.get("member_account_ref", "")
        sacco_disp = l.get("sacco_display", "")
        if not sacco_disp and l.get("sacco_account"):
            sa = l["sacco_account"]
            sacco_disp = f"{sa.account_code} — {sa.account_name}"
        desc = l.get("description", "")
        dr = Decimal(str(l.get("debit", 0) or 0))
        cr = Decimal(str(l.get("credit", 0) or 0))
        total_dr += dr
        total_cr += cr

        # Truncate name/description for landscape fit
        def _cap(s, n):
            return (s or "")[:n]

        rows.append([
            str(row_no),
            "Cust" if et == "customer" else "SACCO",
            cust_no or "—",
            _cap(cust_name, 25),
            _cap(member_ref, 18),
            _cap(sacco_disp, 26),
            _cap(desc, 30),
            _money(dr) if dr else "",
            _money(cr) if cr else "",
        ])

    # Totals row
    rows.append([
        "", "", "", "", "", "", "TOTALS",
        _money(total_dr), _money(total_cr),
    ])

    tbl = Table(rows, colWidths=[
        0.25*inch,   # #
        0.55*inch,   # Type
        0.75*inch,   # Cust No
        1.6*inch,    # Name
        1.4*inch,    # Member Acc
        1.9*inch,    # SACCO Acc
        1.8*inch,    # Description
        1.0*inch,    # Debit
        1.0*inch,    # Credit
    ])
    tbl.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (7, 1), (8, -1), "RIGHT"),
        ("BACKGROUND", (0, -1), (-1, -1), LIGHT),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 8))

    balance_note = (
        "✓ BALANCED"
        if (total_dr - total_cr).copy_abs() <= Decimal("0.01")
        else f"⚠ OUT OF BALANCE (Δ {_money((total_dr - total_cr).copy_abs())})"
    )
    story.append(Paragraph(
        f"Debits {_money(total_dr)}  ·  Credits {_money(total_cr)}  ·  {balance_note}",
        style_body,
    ))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "This voucher has been posted to the General Ledger and, where "
        "customer member accounts are involved, to the corresponding "
        "sub-ledgers (SavingsTransaction / LoanTransaction). All entries "
        "are recorded in the transaction audit trail.",
        style_small,
    ))

    doc.build(story)
    buf.seek(0)
    resp = HttpResponse(buf.getvalue(), content_type="application/pdf")
    resp["Content-Disposition"] = (
        f'inline; filename="{voucher.voucher_no}.pdf"'
    )
    return resp
