"""
data_imports/pdf.py
===================
Post-commit summary PDF using reportlab. Keeps the layout consistent with
NODi CBS's other operations reports (dark title band, thin borders).
"""
from __future__ import annotations

from io import BytesIO
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, PageBreak,
)


BRAND = colors.HexColor("#1F4E79")
GREY = colors.HexColor("#5A6570")
LIGHT = colors.HexColor("#F2F5F9")


def build_summary_pdf(import_type, summary, filename: str, meta) -> BytesIO:
    """
    summary keys: created, updated, skipped, problems (list[str])
    meta:    dict with user, when, total_rows, valid_rows, invalid_rows, options

    Layout: uses the full A4 portrait width (15 mm margins each side)
    so content fills the page rather than being squeezed into a narrow
    central strip.
    """
    buf = BytesIO()
    margin_h = 15 * mm
    margin_v = 16 * mm
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=margin_h, rightMargin=margin_h,
        topMargin=margin_v, bottomMargin=margin_v,
        title=f"Import Summary — {import_type.title}",
    )
    avail_w = A4[0] - 2 * margin_h   # full usable width ≈ 180 mm

    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle(
        "T", parent=styles["Heading1"], textColor=BRAND, fontSize=16, spaceAfter=4
    )
    sub_style = ParagraphStyle(
        "S", parent=styles["Normal"], textColor=GREY, fontSize=9, spaceAfter=8
    )
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], textColor=BRAND,
                        fontSize=12, spaceBefore=12, spaceAfter=6)
    body = ParagraphStyle("B", parent=styles["Normal"], fontSize=10, leading=14)

    story.append(Paragraph(f"Bulk Import Summary — {import_type.title}", title_style))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
        f"Filename <b>{filename}</b>",
        sub_style,
    ))

    # ── Meta table — spans full width ───────────────────────────────
    meta_rows = [
        ["Import type",  import_type.title],
        ["Ran by",       meta.get("user", "system")],
        ["Ran at",       meta.get("when", "")],
        ["Rows in file", f"{meta.get('total_rows', 0):,}"],
        ["Valid rows",   f"{meta.get('valid_rows', 0):,}"],
        ["Invalid rows", f"{meta.get('invalid_rows', 0):,}"],
    ]
    if meta.get("options"):
        opts = "; ".join(f"{k}={v}" for k, v in meta["options"].items())
        meta_rows.append(["Options", opts])
    t = Table(meta_rows, colWidths=[avail_w * 0.28, avail_w * 0.72])
    t.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (0, -1), LIGHT),
        ("FONTNAME",    (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, -1), 10),
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("BOX",         (0, 0), (-1, -1), 0.4, GREY),
        ("INNERGRID",   (0, 0), (-1, -1), 0.25, GREY),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",(0, 0), (-1, -1), 8),
        ("TOPPADDING",  (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
    ]))
    story.append(t)

    # ── Result KPIs — four equal columns spanning full width ────────
    story.append(Paragraph("Result", h2))
    kpi_col_w = avail_w / 4
    kpi_rows = [
        ["Created", "Updated", "Skipped", "Errors"],
        [
            f"{summary.get('created', 0):,}",
            f"{summary.get('updated', 0):,}",
            f"{summary.get('skipped', 0):,}",
            f"{len(summary.get('problems', [])):,}",
        ],
    ]
    kpi = Table(kpi_rows, colWidths=[kpi_col_w] * 4)
    kpi.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), BRAND),
        ("TEXTCOLOR",    (0, 0), (-1, 0), colors.white),
        ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0), 10),
        ("FONTSIZE",     (0, 1), (-1, 1), 16),
        ("FONTNAME",     (0, 1), (-1, 1), "Helvetica-Bold"),
        ("ALIGN",        (0, 0), (-1, -1), "CENTER"),
        ("BOX",          (0, 0), (-1, -1), 0.4, GREY),
        ("INNERGRID",    (0, 0), (-1, -1), 0.25, GREY),
        ("TOPPADDING",   (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 8),
    ]))
    story.append(kpi)

    # ── Problems block — spans full width ───────────────────────────
    problems = summary.get("problems", [])
    if problems:
        story.append(Paragraph("Rows that could not be committed", h2))
        story.append(Paragraph(
            "The rows below were rejected during commit. Fix them in the source "
            "file and re-upload — successful rows will be skipped as duplicates on "
            "re-import (where applicable).", body))
        story.append(Spacer(1, 4))
        prob_rows = [["#", "Message"]]
        for i, p in enumerate(problems[:500], start=1):
            prob_rows.append([str(i), Paragraph(str(p), body)])
        pt = Table(prob_rows, colWidths=[avail_w * 0.06, avail_w * 0.94],
                   repeatRows=1)
        pt.setStyle(TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0), LIGHT),
            ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",    (0, 0), (-1, -1), 9),
            ("VALIGN",      (0, 0), (-1, -1), "TOP"),
            ("BOX",         (0, 0), (-1, -1), 0.4, GREY),
            ("INNERGRID",   (0, 0), (-1, -1), 0.2, GREY),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",(0, 0), (-1, -1), 5),
            ("TOPPADDING",  (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ]))
        story.append(pt)
        if len(problems) > 500:
            story.append(Spacer(1, 4))
            story.append(Paragraph(
                f"… and {len(problems) - 500:,} more problems truncated.", body))
    else:
        story.append(Spacer(1, 10))
        story.append(Paragraph(
            "<font color='#227C4A'><b>All committed rows imported successfully.</b></font>",
            body))

    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "This document was auto-generated by the NODi Lite Bulk Data Import module. "
        "Retain for audit; the underlying source file is stored on the server for "
        "traceability.", sub_style))

    doc.build(story)
    buf.seek(0)
    return buf
