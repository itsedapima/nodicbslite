import hashlib
import io
import logging
import re
import uuid as _uuid
from decimal import Decimal
from datetime import datetime

import qrcode
from django.conf import settings
from django.utils.timezone import now

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph,
    Spacer, Image as RLImage, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

# IMPORTANT: Adjust these imports to match your actual app names
from transactions.models import SavingsTransaction, LoanTransaction
from administration.models import ChamaInfo  # Assuming ChamaInfo is here

logger = logging.getLogger(__name__)


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


def _create_statement_hash_no_request(customer, account_code, statement_type="consolidated"):
    """
    Create a StatementHash record without a Django request object.
    Uses ALLOWED_HOSTS / SITE_URL setting to build the verify URL.
    """
    from .models import StatementHash

    raw = f"{customer.cust_no}|{account_code}|{now().isoformat()}|{_uuid.uuid4().hex}"
    hash_value = hashlib.sha256(raw.encode()).hexdigest()

    StatementHash.objects.create(
        hash_value=hash_value,
        cust_no=str(customer.cust_no),
        customer_name=customer.full_name,
        account_code=account_code,
        statement_type=statement_type,
        generated_by="System",
    )

    # Build URL from settings
    base = getattr(settings, "SITE_URL", None)
    if not base:
        hosts = getattr(settings, "ALLOWED_HOSTS", [])
        domain = next((h for h in hosts if h not in ("*", "localhost", "127.0.0.1", "web")), "localhost")
        base = f"https://{domain}"
    verify_url = f"{base.rstrip('/')}/statements/verify/{hash_value}/"
    return hash_value, verify_url


def generate_statement_pdf_bytes(customer):
    """
    Generates a consolidated PDF statement for a member and returns the raw bytes.
    """
    # --- 1. DATA AGGREGATION ---
    all_sections = []

    # Savings Logic
    savings_types = SavingsTransaction.objects.filter(cust_no=customer.cust_no).values_list('saving_type', flat=True).distinct()
    for s_type in savings_types:
        qs = SavingsTransaction.objects.filter(cust_no=customer.cust_no, saving_type=s_type).order_by('tr_date', 'id')
        balance = Decimal('0.00')
        rows = []
        for t in qs:
            credit = Decimal(str(t.credit_amount or 0))
            debit = Decimal(str(t.debit_amount or 0))
            balance += (credit - debit)
            rows.append([
                t.tr_date.strftime("%d-%b-%Y"),
                t.tr_desc[:40],
                f"{debit:,.2f}" if debit > 0 else "-",
                f"{credit:,.2f}" if credit > 0 else "-",
                f"{balance:,.2f}"
            ])
        if rows:
            all_sections.append({'name': str(s_type).replace('_', ' ').title(), 'data': rows})

    # Loan Logic
    loan_nos = LoanTransaction.objects.filter(cust_no=customer.cust_no).values_list('loan_no', flat=True).distinct()
    for l_no in loan_nos:
        qs = LoanTransaction.objects.filter(cust_no=customer.cust_no, loan_no=l_no).order_by('tr_date', 'id')
        balance = Decimal('0.00')
        rows = []
        for t in qs:
            credit = Decimal(str(t.credit_amount or 0))
            debit = Decimal(str(t.debit_amount or 0))
            balance += (debit - credit)
            rows.append([
                t.tr_date.strftime("%d-%b-%Y"),
                t.tr_desc[:40],
                f"{debit:,.2f}" if debit > 0 else "-",
                f"{credit:,.2f}" if credit > 0 else "-",
                f"{balance:,.2f}"
            ])
        if rows:
            all_sections.append({'name': f"Loan Account: {l_no}", 'data': rows})

    # --- 2. PDF SETUP ---
    company_name = "YOUR INSTITUTION"
    company_address = company_contact = company_location = company_email = ""
    company_logo_path = None

    try:
        company_info = ChamaInfo.objects.first()
        if company_info:
            company_name = company_info.company_name or company_name
            company_address = company_info.company_address or ""
            company_contact = company_info.company_contact or ""
            company_location = company_info.company_location or ""
            if company_info.company_logo and hasattr(company_info.company_logo, "path"):
                company_logo_path = company_info.company_logo.path
            # Extract email from any text field
            all_text = f"{company_address} {company_contact} {company_location}"
            email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', all_text)
            company_email = email_match.group(0) if email_match else ""
    except Exception as e:
        logger.error(f"Error fetching company info: {e}")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=40, rightMargin=40,
        topMargin=30, bottomMargin=40
    )
    story = []
    styles = getSampleStyleSheet()

    ACCENT = colors.HexColor("#2c3e50")

    # Custom Styles
    header_name_style = ParagraphStyle(
        "CompanyName", parent=styles["Normal"],
        fontSize=16, leading=19, fontName="Helvetica-Bold",
        textColor=ACCENT, alignment=TA_CENTER,
    )
    header_addr_style = ParagraphStyle(
        "CompanyAddr", parent=styles["Normal"],
        fontSize=8, leading=11, textColor=colors.HexColor("#555555"),
        alignment=TA_CENTER,
    )
    section_title_style = ParagraphStyle(
        "SectionTitle", parent=styles["Heading3"],
        backColor=ACCENT, textColor=colors.white,
        borderPadding=5, spaceBefore=15, spaceAfter=5
    )
    consol_title_style = ParagraphStyle(
        "ConsolTitle", parent=styles["Normal"],
        alignment=TA_CENTER, textColor=ACCENT,
        fontSize=9, fontName="Helvetica-Bold",
        spaceAfter=8, spaceBefore=0,
    )
    label_style = ParagraphStyle(
        "MetaLabel", parent=styles["Normal"],
        fontSize=7.5, textColor=colors.HexColor("#888888"),
    )
    value_style = ParagraphStyle(
        "MetaValue", parent=styles["Normal"],
        fontSize=8.5, fontName="Helvetica-Bold", textColor=ACCENT,
    )

    # --- 3. BUILD HEADER (logo + text centered as one block) ---
    addr_parts = []
    if company_address:
        addr_parts.append(company_address)
    if company_contact:
        addr_parts.append(company_contact)
    addr_line = "  |  ".join(addr_parts)

    if company_logo_path:
        try:
            logo = RLImage(company_logo_path, width=0.9*inch, height=0.9*inch, kind='proportional')
            text_col = [
                Paragraph(company_name.upper(), header_name_style),
                Paragraph(addr_line, header_addr_style),
            ]
            inner_data = [[logo, text_col]]
            inner_table = Table(inner_data, colWidths=[1.0*inch, 3.5*inch],
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
        except Exception:
            center_name = ParagraphStyle("CNameC", parent=header_name_style, alignment=TA_CENTER)
            center_addr = ParagraphStyle("CAddrC", parent=header_addr_style, alignment=TA_CENTER)
            story.append(Paragraph(company_name.upper(), center_name))
            story.append(Paragraph(addr_line, center_addr))
    else:
        center_name = ParagraphStyle("CNameC2", parent=header_name_style, alignment=TA_CENTER)
        center_addr = ParagraphStyle("CAddrC2", parent=header_addr_style, alignment=TA_CENTER)
        story.append(Paragraph(company_name.upper(), center_name))
        story.append(Paragraph(addr_line, center_addr))

    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT, spaceAfter=2))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#7f8c8d"), spaceAfter=10))

    story.append(Paragraph("CONSOLIDATED MEMBER STATEMENT", consol_title_style))

    # Member Info Grid
    generated_at = now().strftime('%d-%b-%Y %H:%M')
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
            Paragraph("Phone", label_style),
            Paragraph(str(customer.phone or "-"), value_style),
        ],
        [
            Paragraph("Generated", label_style),
            Paragraph(generated_at, value_style),
            "", "",
        ],
    ]
    mem_table = Table(mem_data, colWidths=[0.9*inch, 2.8*inch, 0.7*inch, 2.8*inch])
    mem_table.setStyle(TableStyle([
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#dddddd")),
    ]))
    story.append(mem_table)
    story.append(Spacer(1, 10))

    # --- 4. DYNAMIC SECTION GENERATION ---
    for section in all_sections:
        story.append(Paragraph(f" {section['name']}", section_title_style))

        table_data = [["Date", "Description", "Debit", "Credit", "Balance"]]
        table_data.extend(section['data'])

        t = Table(table_data, colWidths=[1.0*inch, 3.0*inch, 1.0*inch, 1.0*inch, 1.2*inch], repeatRows=1)
        t.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('ALIGN', (2, 0), (-1, -1), 'RIGHT'),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(t)
        story.append(Spacer(1, 10))

    # --- 5. FOOTER ---
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 6))

    # QR code for authenticity verification — hash stored in DB
    _hash, verify_url = _create_statement_hash_no_request(customer, "CONSOLIDATED", "consolidated")
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

    left_cell = [
        Paragraph(f"End of consolidated report for {customer.full_name}", footer_left_style),
    ]
    if company_email:
        left_cell.append(Spacer(1, 2))
        left_cell.append(Paragraph(
            f"For any queries or complaints regarding this statement, "
            f"please contact us at {company_email}",
            ParagraphStyle("ContactLine", parent=styles["Normal"],
                           fontSize=6.5, textColor=colors.HexColor("#999999"),
                           alignment=TA_LEFT),
        ))

    right_cell = [
        Paragraph("Scan to verify", qr_hint_style),
        Paragraph("statement authenticity", qr_hint_style),
    ]

    footer_data = [[left_cell, right_cell, qr_img]]
    footer_table = Table(footer_data, colWidths=[4.6*inch, 1.4*inch, 1.2*inch])
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
    pdf_bytes = buffer.getvalue()
    buffer.close()

    return pdf_bytes
