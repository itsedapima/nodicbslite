"""
accounting/jv_views.py
=======================
Views for the flexible Journal Voucher UI:
  · GET  /accounting/jv/                   → list
  · GET  /accounting/jv/new/               → grid entry page (D365 theme)
  · POST /accounting/jv/validate/          → JSON validation of posted lines
  · POST /accounting/jv/post/              → validate + post + redirect to PDF
  · POST /accounting/jv/upload/            → parse uploaded CSV/XLSX → JSON
  · GET  /accounting/jv/template.csv       → download the upload template
  · GET  /accounting/jv/lookup/customer/   → JSON: customer info + their accounts
  · GET  /accounting/jv/lookup/sacco/      → JSON: search chart of accounts
  · GET  /accounting/jv/<id>/              → detail page
  · GET  /accounting/jv/<id>/pdf/          → PDF summary

Synced from nodicbs — uses jv_service.py for validation/posting and
jv_pdf.py (reportlab) for PDF generation.
"""

from __future__ import annotations

import json
import logging

import uuid
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import (
    HttpResponse, JsonResponse, HttpResponseBadRequest,
    HttpResponseNotAllowed,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from accounts.decorators import role_required
from .jv_pdf import journal_voucher_pdf
from .jv_service import (
    build_template_csv, parse_uploaded_lines, post_voucher, validate_lines,
)
from .models import JournalVoucher, JournalVoucherDraft

logger = logging.getLogger(__name__)


# ─── LIST + DETAIL ─────────────────────────────────────────────

@login_required
def jv_list(request):
    qs = JournalVoucher.objects.all().order_by("-created_at")[:200]
    return render(request, "accounting/jv/list.html", {"vouchers": qs})


@login_required
def jv_detail(request, pk):
    voucher = get_object_or_404(JournalVoucher, pk=pk)
    return render(request, "accounting/jv/detail.html", {
        "voucher": voucher,
        "lines": voucher.lines.all().select_related(
            "sacco_account", "customer", "member_product",
        ),
    })


@login_required
def jv_pdf_view(request, pk):
    voucher = get_object_or_404(JournalVoucher, pk=pk)
    return journal_voucher_pdf(voucher)


# ─── ENTRY GRID ────────────────────────────────────────────────

@login_required
def jv_new(request):
    """Render the grid entry page. Actual posting is via jv_post below.
    Supports resume: GET ?draft=<session_key> loads a saved draft.
    """
    from .models import SaccoAccount
    from transactions.models import CustomerAccountsSetup

    # Resume support
    resume_key = request.GET.get("draft")
    resume_draft = None
    resume_grid_json = "[]"
    resume_desc = ""
    resume_date = ""

    if resume_key:
        resume_draft = JournalVoucherDraft.objects.filter(
            session_key=resume_key, created_by=request.user,
        ).exclude(status="posted").first()
        if resume_draft:
            session_id = resume_draft.session_key
            resume_grid_json = json.dumps(resume_draft.grid_data or [])
            resume_desc = resume_draft.description or ""
            resume_date = resume_draft.voucher_date.isoformat() if resume_draft.voucher_date else ""
        else:
            session_id = str(uuid.uuid4())
    else:
        session_id = str(uuid.uuid4())

    # Ship the SACCO chart-of-accounts + product setup as JSON to seed
    # the client-side autocomplete lookups.
    sacco_accounts = list(SaccoAccount.objects.values(
        "id", "account_code", "account_name", "account_group",
    ).order_by("account_code"))
    products = list(CustomerAccountsSetup.objects.filter(is_active=True).values(
        "account_code", "account_name", "acc_initials",
        "account_type", "is_loan_account",
    ).order_by("account_code"))

    return render(request, "accounting/jv/new.html", {
        "sacco_accounts_json": json.dumps(sacco_accounts),
        "products_json": json.dumps(products),
        "today": resume_date or timezone.localdate().isoformat(),
        "session_id": session_id,
        "resume_grid_json": resume_grid_json,
        "resume_desc": resume_desc,
    })


# ─── VALIDATE (AJAX) ───────────────────────────────────────────

@login_required
@require_POST
def jv_validate(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("Invalid JSON payload.")

    lines = payload.get("lines") or []
    report = validate_lines(lines)

    return JsonResponse({
        "ok": report["ok"],
        "is_balanced": report["is_balanced"],
        "total_debit": str(report["total_debit"]),
        "total_credit": str(report["total_credit"]),
        "errors": report["errors"],
        "warnings": report["warnings"],
        "resolved_display": [
            {
                "row": l["row"],
                "entry_type": l["entry_type"],
                "display": l.get("display", ""),
                "debit": str(l.get("debit", 0)),
                "credit": str(l.get("credit", 0)),
            }
            for l in report["resolved"]
        ],
    })


# ─── POST ──────────────────────────────────────────────────────

@login_required
@require_POST
def jv_post(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("Invalid JSON payload.")

    lines = payload.get("lines") or []
    description = (payload.get("description") or "").strip()
    voucher_date = payload.get("voucher_date") or timezone.localdate().isoformat()
    session_key = (payload.get("session_key") or "").strip()

    # ── Double-post guard: if this draft was already posted, reject ──
    if session_key:
        existing_draft = JournalVoucherDraft.objects.filter(
            session_key=session_key, status="posted",
        ).first()
        if existing_draft:
            return JsonResponse({
                "ok": False,
                "errors": [{
                    "row": 0, "field": "",
                    "message": (
                        f"This journal voucher was already posted on "
                        f"{existing_draft.posted_at.strftime('%d %b %Y at %H:%M') if existing_draft.posted_at else 'an earlier date'}. "
                        f"Duplicate posting is not allowed. Start a new voucher instead."
                    ),
                }],
            }, status=409)

    if not description:
        return JsonResponse({
            "ok": False,
            "errors": [{"row": 0, "field": "description",
                        "message": "A voucher description is required."}],
        }, status=400)

    try:
        vd = timezone.datetime.fromisoformat(voucher_date).date()
    except (ValueError, TypeError):
        vd = timezone.localdate()

    try:
        result = post_voucher(
            voucher_date=vd,
            description=description,
            lines=lines,
            user=request.user,
            request=request,
        )
    except ValueError as e:
        report = getattr(e, "report", None) or {"errors": [{"row": 0, "field": "", "message": str(e)}]}
        return JsonResponse({
            "ok": False,
            "errors": report.get("errors", []),
            "warnings": report.get("warnings", []),
            "is_balanced": report.get("is_balanced", False),
            "total_debit": str(report.get("total_debit", 0)),
            "total_credit": str(report.get("total_credit", 0)),
        }, status=400)
    except Exception as exc:
        logger.exception("JV post failed")
        return JsonResponse({
            "ok": False,
            "errors": [{"row": 0, "field": "", "message": f"Post failed: {exc}"}],
        }, status=500)

    # ── Mark the draft as posted so it can never be resubmitted ──
    if session_key:
        JournalVoucherDraft.objects.filter(session_key=session_key).update(
            status="posted",
            posted_at=timezone.now(),
            label=f"Posted → {result.voucher_no}",
        )

    return JsonResponse({
        "ok": True,
        "voucher_no": result.voucher_no,
        "voucher_id": result.voucher_id,
        "total_amount": str(result.total_amount),
        "line_count": result.line_count,
        "pdf_url": f"/accounting/jv/{result.voucher_id}/pdf/",
        "detail_url": f"/accounting/jv/{result.voucher_id}/",
    })


# ─── BULK UPLOAD ───────────────────────────────────────────────

@login_required
@require_POST
def jv_upload(request):
    f = request.FILES.get("file")
    if not f:
        return HttpResponseBadRequest("No file uploaded.")

    try:
        lines = parse_uploaded_lines(f)
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    except Exception:
        logger.exception("JV upload parse failed")
        return JsonResponse({"ok": False, "error": "Could not parse the uploaded file."}, status=400)

    return JsonResponse({"ok": True, "lines": lines, "count": len(lines)})


@login_required
@require_GET
def jv_template(request):
    csv_bytes = build_template_csv()
    resp = HttpResponse(csv_bytes, content_type="text/csv; charset=utf-8-sig")
    resp["Content-Disposition"] = 'attachment; filename="journal_voucher_template.csv"'
    return resp


# ─── LOOKUP APIs ───────────────────────────────────────────────

@login_required
@require_GET
def jv_lookup_customer(request):
    """
    /accounting/jv/lookup/customer/?cust_no=00011
    Returns:
        { ok: bool, cust_no, full_name, phone,
          savings_accounts: [ {ref, code, name, balance}, ...],
          loans: [ {loan_no, product, balance, status}, ... ] }
    """
    from customers.models import Customer
    from transactions.models import CustomerAccountsSetup, SavingsTransaction
    from loans.models import LoanHistory, RunningLoanStat
    from django.db.models import Sum

    cust_no = (request.GET.get("cust_no") or "").strip()
    if not cust_no:
        return JsonResponse({"ok": False, "error": "cust_no is required."}, status=400)

    customer = Customer.objects.filter(cust_no=cust_no).first()
    if not customer:
        return JsonResponse({"ok": False, "error": "Customer not found."}, status=404)

    # Savings accounts (all non-loan products the SACCO has, with balance)
    savings_products = CustomerAccountsSetup.objects.filter(
        is_loan_account=False, is_active=True,
    ).order_by("account_code")
    savings_out = []
    for p in savings_products:
        bal_agg = SavingsTransaction.objects.filter(
            cust_no=cust_no, saving_type=p.account_type,
        ).aggregate(cr=Sum("credit_amount"), dr=Sum("debit_amount"))
        cr = bal_agg["cr"] or 0
        dr = bal_agg["dr"] or 0
        balance = float(cr) - float(dr)
        ref = (
            f"{p.account_code}-{cust_no}"
            if p.account_code else
            f"{p.acc_initials}-{cust_no}"
        )
        savings_out.append({
            "ref": ref,
            "code": p.account_code,
            "name": p.account_name,
            "balance": f"{balance:,.2f}",
            "kind": "savings",
        })

    # Loans (RunningLoanStat if available; fall back to LoanHistory)
    loans_out = []
    stats = RunningLoanStat.objects.filter(cust_no=cust_no).order_by("-loan_no")
    stat_by_no = {s.loan_no: s for s in stats}
    loans = LoanHistory.objects.filter(
        customer=customer, is_disbursed=True,
    ).select_related("loan_type").order_by("-loan_date")
    for l in loans:
        stat = stat_by_no.get(l.loan_no)
        loans_out.append({
            "ref": l.loan_no,
            "loan_no": l.loan_no,
            "product": l.loan_type.account_name if l.loan_type else "",
            "balance": (f"{stat.loan_balance:,.2f}" if stat else "—"),
            "status": (stat.loan_status if stat else "Unknown"),
            "kind": "loan",
        })

    return JsonResponse({
        "ok": True,
        "cust_no": cust_no,
        "full_name": customer.full_name,
        "phone": getattr(customer, "phone", "") or getattr(customer, "mobile", "") or "",
        "savings_accounts": savings_out,
        "loans": loans_out,
    })


@login_required
@require_GET
def jv_lookup_sacco(request):
    """
    /accounting/jv/lookup/sacco/?q=630
    Returns up to 20 matching SACCO GL accounts.
    """
    from .models import SaccoAccount

    q = (request.GET.get("q") or "").strip()
    qs = SaccoAccount.objects.all()
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(account_code__icontains=q) | Q(account_name__icontains=q)
        )
    qs = qs.order_by("account_code")[:20]
    return JsonResponse({
        "ok": True,
        "results": [
            {"code": a.account_code,
             "name": a.account_name,
             "group": a.account_group or ""}
            for a in qs
        ],
    })


# ─── JV AUTOSAVE / HISTORY / RESUME ──────────────────────────────

@csrf_exempt   # sendBeacon on beforeunload cannot include CSRF header
@login_required
@require_POST
def jv_autosave(request):
    """Autosave JV grid state to JournalVoucherDraft."""
    try:
        data = json.loads(request.body.decode("utf-8"))
        session_key = data.get("session_key")
        grid_data = data.get("grid_data", [])
        description = data.get("description", "")
        voucher_date = data.get("voucher_date", "")
        label = data.get("label", "")

        if not session_key:
            return JsonResponse({"success": False, "error": "Missing session_key"}, status=400)

        total_dr = Decimal("0")
        total_cr = Decimal("0")
        valid_count = 0
        for line in grid_data:
            try:
                dr = Decimal(str(line.get("debit", 0) or 0))
                cr = Decimal(str(line.get("credit", 0) or 0))
                if dr > 0 or cr > 0:
                    total_dr += dr
                    total_cr += cr
                    valid_count += 1
            except Exception:
                pass

        vd = None
        if voucher_date:
            try:
                vd = timezone.datetime.fromisoformat(voucher_date).date()
            except (ValueError, TypeError):
                pass

        draft, _ = JournalVoucherDraft.objects.update_or_create(
            session_key=session_key,
            defaults={
                "grid_data": grid_data,
                "description": description[:500] if description else "",
                "voucher_date": vd,
                "line_count": valid_count,
                "total_debit": total_dr,
                "total_credit": total_cr,
                "status": "saved" if valid_count > 0 else "draft",
                "label": label[:200] if label else "",
                "created_by": request.user,
            },
        )
        return JsonResponse({
            "success": True,
            "saved_at": draft.updated_at.strftime("%H:%M:%S"),
            "line_count": valid_count,
        })
    except Exception as e:
        logger.exception("JV autosave error")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
def jv_draft_history(request):
    """List all JV drafts for the current user."""
    drafts = JournalVoucherDraft.objects.filter(
        created_by=request.user,
    ).order_by("-updated_at")[:50]
    return render(request, "accounting/jv/draft_history.html", {"drafts": drafts})


@login_required
@require_GET
def jv_draft_load(request, session_key):
    """Return the saved grid_data JSON for a JV draft."""
    draft = JournalVoucherDraft.objects.filter(
        session_key=session_key, created_by=request.user,
    ).first()
    if not draft:
        return JsonResponse({"success": False, "error": "Draft not found"}, status=404)
    return JsonResponse({
        "success": True,
        "grid_data": draft.grid_data,
        "description": draft.description,
        "voucher_date": draft.voucher_date.isoformat() if draft.voucher_date else "",
        "line_count": draft.line_count,
        "total_debit": str(draft.total_debit),
        "total_credit": str(draft.total_credit),
    })


@login_required
@role_required('admin')
@require_POST
def jv_draft_delete(request, session_key):
    """Delete a JV draft. Admin only."""
    draft = JournalVoucherDraft.objects.filter(
        session_key=session_key,
    ).exclude(status="posted").first()
    if not draft:
        return JsonResponse({"success": False, "error": "Draft not found or already posted"}, status=404)
    draft.delete()
    return JsonResponse({"success": True})
