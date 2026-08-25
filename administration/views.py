import csv
import io
import re
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import models, transaction as db_transaction
from django.db.models import Count, Q, Case, When, BooleanField, Value
from django.http import HttpResponse, HttpResponseRedirect, FileResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone

from accounts.decorators import min_role_required, role_required
from accounts.models import CustomUser
from customers.models import Customer
from transactions.models import SavingsTransaction, LoanTransaction, CustomerAccountsSetup

from .models import (
    ChamaInfo, CompanyBranch,
    BackupLog, BackupConfiguration, Promotion,
)
from .forms import (
    ChamaInfoForm, CompanyBranchForm, BackupSettingsForm,
    PromotionForm, BulkNotificationForm, BulkTransactionUploadForm,
)
from .utils import perform_database_backup_now, backup_file_exists


# ═══════════════════════════════════════════════════════════════════════════
# CHAMA INFO (was CompanyInfo)
# ═══════════════════════════════════════════════════════════════════════════

@login_required
@role_required('admin')
def edit_chama_info(request):
    chama_info, created = ChamaInfo.objects.get_or_create(id=1)

    if request.method == "POST":
        form = ChamaInfoForm(request.POST, request.FILES, instance=chama_info)
        if form.is_valid():
            form.save()
            return redirect("administration:chama_details")
    else:
        form = ChamaInfoForm(instance=chama_info)

    return render(request, "administration/edit_chama_info.html", {"form": form})


@login_required
def chama_details(request):
    chama_info = ChamaInfo.objects.first()
    return render(request, "administration/chama_details.html", {"chama_info": chama_info})


def user_profile(request):
    user = request.user
    return render(request, "administration/user_profile.html", {"user": user})


# ═══════════════════════════════════════════════════════════════════════════
# BRANCHES
# ═══════════════════════════════════════════════════════════════════════════

def branch_list(request):
    branches = CompanyBranch.objects.all()
    return render(request, 'administration/branch_list.html', {'branches': branches})


def branch_detail(request, pk):
    branch = get_object_or_404(CompanyBranch, pk=pk)
    return render(request, 'administration/branch_detail.html', {'branch': branch})


def branch_create(request):
    if request.method == "POST":
        form = CompanyBranchForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('administration:branch_list')
    else:
        form = CompanyBranchForm()
    return render(request, 'administration/branch_form.html', {'form': form, 'title': 'Create Branch'})


def branch_update(request, pk):
    branch = get_object_or_404(CompanyBranch, pk=pk)
    if request.method == "POST":
        form = CompanyBranchForm(request.POST, instance=branch)
        if form.is_valid():
            form.save()
            return redirect('administration:branch_detail', pk=branch.pk)
    else:
        form = CompanyBranchForm(instance=branch)
    return render(request, 'administration/branch_form.html', {'form': form, 'title': 'Edit Branch', 'branch': branch})


def branch_delete(request, pk):
    branch = get_object_or_404(CompanyBranch, pk=pk)
    if request.method == "POST":
        branch.delete()
        return redirect('administration:branch_list')
    return render(request, 'administration/branch_confirm_delete.html', {'branch': branch})


# ═══════════════════════════════════════════════════════════════════════════
# BACKUPS
# ═══════════════════════════════════════════════════════════════════════════

@login_required
@role_required('admin')
def create_backup_settings(request):
    if BackupConfiguration.objects.exists():
        messages.info(request, "Backup settings already exist. Redirecting to edit page.")
        config = BackupConfiguration.objects.first()
        return redirect('administration:update_settings', pk=config.pk)

    if request.method == 'POST':
        form = BackupSettingsForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Backup configuration created successfully!")
            return redirect('administration:backup_dashboard')
    else:
        form = BackupSettingsForm()

    return render(request, 'administration/backup_settings.html', {
        'form': form,
        'title': 'Setup Backup',
    })


@login_required
@role_required('admin')
def update_backup_settings(request, pk):
    config = get_object_or_404(BackupConfiguration, pk=pk)

    if request.method == 'POST':
        form = BackupSettingsForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, "Backup settings updated successfully.")
            return redirect('administration:backup_dashboard')
    else:
        form = BackupSettingsForm(instance=config)

    return render(request, 'administration/backup_settings.html', {
        'form': form,
        'title': 'Update Settings',
        'is_update': True,
    })


@login_required
@role_required('admin')
def backup_config_list(request):
    configs = BackupConfiguration.objects.all()
    recent_logs = BackupLog.objects.all()[:5]
    return render(request, 'administration/backup_config_list.html', {
        'configs': configs,
        'recent_logs': recent_logs,
        'title': 'Backup Configurations',
    })


@login_required
@role_required('admin')
def backup_dashboard(request):
    logs = BackupLog.objects.all()[:20]
    for log in logs:
        log.file_on_disk = backup_file_exists(log.file_name) if log.status == 'success' else False
    return render(request, 'administration/backup_dashboard.html', {'logs': logs})


@login_required
@role_required('admin')
def download_backup(request, pk):
    """Securely downloads a structural backup file directly from storage."""
    import os
    log = get_object_or_404(BackupLog, pk=pk)

    if not log.file_name:
        messages.error(request, "No file registered for this log entry.")
        return redirect('administration:backup_dashboard')

    backup_dir = os.path.join(settings.BASE_DIR, 'backups')
    file_path = os.path.abspath(os.path.join(backup_dir, log.file_name))

    if not file_path.startswith(os.path.abspath(backup_dir)):
        messages.error(request, "Invalid file path requested.")
        return redirect('administration:backup_dashboard')

    if os.path.exists(file_path):
        return FileResponse(open(file_path, 'rb'), as_attachment=True, filename=log.file_name)
    else:
        messages.warning(request, "This backup file has been automatically deleted after the 7-day retention period.")
        return redirect('administration:backup_dashboard')


@login_required
@role_required('admin')
def trigger_manual_backup(request):
    success, message = perform_database_backup_now()
    if success:
        messages.success(request, f"Backup completed successfully! {message}")
    else:
        messages.error(request, f"Backup failed: {message}")
    return redirect('administration:backup_dashboard')


# ═══════════════════════════════════════════════════════════════════════════
# PROMOTIONS
# ═══════════════════════════════════════════════════════════════════════════

@login_required
@min_role_required('admin')
def promotion_list(request):
    promotions = Promotion.objects.all().order_by('display_order', '-created_at')
    return render(request, 'administration/promotion_list.html', {
        'promotions': promotions,
    })


@login_required
@min_role_required('admin')
def promotion_create(request):
    if request.method == 'POST':
        form = PromotionForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, 'Promotion created.')
            return redirect('administration:promotion_list')
    else:
        form = PromotionForm()
    return render(request, 'administration/promotion_form.html', {
        'form': form, 'title': 'Add Promotion',
    })


@login_required
@min_role_required('admin')
def promotion_update(request, pk):
    promo = get_object_or_404(Promotion, pk=pk)
    if request.method == 'POST':
        form = PromotionForm(request.POST, request.FILES, instance=promo)
        if form.is_valid():
            form.save()
            messages.success(request, 'Promotion updated.')
            return redirect('administration:promotion_list')
    else:
        form = PromotionForm(instance=promo)
    return render(request, 'administration/promotion_form.html', {
        'form': form, 'title': 'Edit Promotion', 'promotion': promo,
    })


@login_required
@min_role_required('admin')
def promotion_delete(request, pk):
    promo = get_object_or_404(Promotion, pk=pk)
    if request.method == 'POST':
        promo.delete()
        messages.success(request, 'Promotion deleted.')
    return redirect('administration:promotion_list')


@login_required
@min_role_required('admin')
def promotion_toggle(request, pk):
    promo = get_object_or_404(Promotion, pk=pk)
    promo.is_active = not promo.is_active
    promo.save(update_fields=['is_active', 'updated_at'])
    messages.success(request, f'"{promo.title}" is now {"active" if promo.is_active else "inactive"}.')
    return HttpResponseRedirect(reverse('administration:promotion_list'))


# ═══════════════════════════════════════════════════════════════════════════
# NOTIFICATION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

def _is_admin(user):
    return user.is_staff or user.is_superuser


@login_required
@user_passes_test(_is_admin)
def notification_management(request):
    """Dashboard + bulk actions for customer notification flags."""
    if request.method == "POST":
        form = BulkNotificationForm(request.POST)
        if form.is_valid():
            action = form.cleaned_data["action"]
            qs = Customer.objects.exclude(customer_status="exited")
            if action == "temp_enable_all":
                count = qs.update(temp_notifications_setting=True)
                messages.success(request, f"Temporarily enabled notifications for {count} customer(s).")
            elif action == "temp_disable_all":
                count = qs.update(temp_notifications_setting=False)
                messages.warning(request, f"Temporarily disabled notifications for {count} customer(s).")
            elif action == "reset_all":
                count = qs.update(temp_notifications_setting=None)
                messages.success(request, f"Reset notifications to individual defaults for {count} customer(s).")
            return redirect("administration:notification_management")

    customers = Customer.objects.exclude(customer_status="exited")
    stats = customers.aggregate(
        total=Count("id"),
        default_on=Count("id", filter=Q(default_notifications_setting=True)),
        default_off=Count("id", filter=Q(default_notifications_setting=False)),
        temp_on=Count("id", filter=Q(temp_notifications_setting=True)),
        temp_off=Count("id", filter=Q(temp_notifications_setting=False)),
        temp_unset=Count("id", filter=Q(temp_notifications_setting__isnull=True)),
    )
    effective = customers.annotate(
        effective_notify=Case(
            When(temp_notifications_setting__isnull=False, temp_notifications_setting=True, then=Value(True)),
            When(temp_notifications_setting__isnull=False, temp_notifications_setting=False, then=Value(False)),
            When(temp_notifications_setting__isnull=True, default_notifications_setting=True, then=Value(True)),
            default=Value(False), output_field=BooleanField(),
        )
    ).aggregate(
        effectively_on=Count("id", filter=Q(effective_notify=True)),
        effectively_off=Count("id", filter=Q(effective_notify=False)),
    )
    if stats["temp_unset"] == stats["total"]:
        override_status = "none"
    elif stats["temp_on"] == stats["total"]:
        override_status = "all_enabled"
    elif stats["temp_off"] == stats["total"]:
        override_status = "all_disabled"
    else:
        override_status = "mixed"

    return render(request, "administration/notification_management.html", {
        "stats": stats, "effective": effective, "override_status": override_status,
        "form_enable": BulkNotificationForm(initial={"action": "temp_enable_all"}),
        "form_disable": BulkNotificationForm(initial={"action": "temp_disable_all"}),
        "form_reset": BulkNotificationForm(initial={"action": "reset_all"}),
    })


# ═══════════════════════════════════════════════════════════════════════════
# BULK TRANSACTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _json_safe(value):
    """Recursively convert a value so it is JSON-serialisable for Django sessions."""
    import datetime as _dt
    from decimal import Decimal as _Dec
    from django.db import models as _m

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, _Dec):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, _m.Model):
        return str(value.pk)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


# ── Helpers ──────────────────────────────────────────────────────────────

def _get_cash_accounts():
    from accounting.models import SaccoAccount
    return list(
        SaccoAccount.objects.filter(
            account_code__startswith="900-60"
        ).values_list("account_code", "account_name")
    )


def _build_customer_lookup():
    return {
        c.cust_no: c
        for c in Customer.objects.filter(customer_status="active")
    }


def _build_account_lookup():
    return {
        a.account_code: a
        for a in CustomerAccountsSetup.objects.filter(is_active=True)
    }


def _build_loan_lookup():
    try:
        from loans.models import Loan
        return {
            ln.loan_no: ln
            for ln in Loan.objects.filter(is_active=True)
        }
    except (ImportError, Exception):
        return set(
            LoanTransaction.objects.values_list("loan_no", flat=True).distinct()
        )


_ACCOUNT_RE = re.compile(r"^[A-Z]\d{2}-\d{3,}$")
_LOAN_RE = re.compile(r"^(?:LN|MOBI)\d{4,}$")


def _parse_date(val):
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(val.strip(), fmt)
            if settings.USE_TZ and timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            return dt
        except ValueError:
            continue
    return None


def _validate_rows(rows):
    customer_map = _build_customer_lookup()
    account_map = _build_account_lookup()
    loan_data = _build_loan_lookup()

    all_valid = True
    for row in rows:
        errors = []

        parsed_date = _parse_date(row.get("date", ""))
        if not parsed_date:
            errors.append("Invalid date format (use DD-MM-YYYY)")
        else:
            row["_parsed_date"] = parsed_date

        cust_no = (row.get("customer_no") or "").strip()
        customer = customer_map.get(cust_no)
        if not customer:
            errors.append(f"Member {cust_no} does not exist or is inactive")
        else:
            row["_customer"] = customer
            row["customer_name"] = customer.full_name

        try:
            amount = Decimal(str(row.get("credit_amount", "0")).replace(",", ""))
            if amount <= 0:
                raise InvalidOperation
            row["_amount"] = amount
        except (InvalidOperation, ValueError):
            errors.append("Credit amount must be a positive number")

        account = (row.get("account") or "").strip().upper()
        row["account"] = account

        if _LOAN_RE.match(account):
            row["_is_loan"] = True
            if isinstance(loan_data, dict):
                loan = loan_data.get(account)
                if not loan:
                    errors.append(f"Loan {account} not found")
                else:
                    row["_loan"] = loan
            elif isinstance(loan_data, set):
                if account not in loan_data:
                    errors.append(f"Loan {account} not found in transactions")

        elif _ACCOUNT_RE.match(account):
            product_code = account.split("-")[0]
            product = account_map.get(product_code)
            if not product:
                errors.append(f"Account product {product_code} is not configured")
            else:
                row["_product"] = product
                row["_is_loan"] = False
                member_suffix = account.split("-")[1]
                if cust_no and member_suffix != cust_no:
                    errors.append(
                        f"Account suffix {member_suffix} does not match customer {cust_no}"
                    )
        else:
            errors.append(
                f"Invalid account format '{account}'. "
                "Expected S02-XXXXX (savings) or LNXXXXXX (loan)"
            )

        if not (row.get("receiving_bank") or "").strip():
            errors.append("Receiving bank is required")

        if errors:
            row["validation_status"] = "Error"
            row["error"] = "; ".join(errors)
            all_valid = False
        else:
            row["validation_status"] = "Validated"
            row["error"] = ""

    return all_valid, rows


def _generate_batch_ref():
    return f"BLK-{timezone.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"


# ── Template download ───────────────────────────────────────────────────

@login_required
@user_passes_test(_is_admin)
def download_bulk_template(request):
    cash_accounts = _get_cash_accounts()
    bank_options = ", ".join(f"{code} ({name})" for code, name in cash_accounts)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="bulk_credit_template.csv"'

    writer = csv.writer(response)
    writer.writerow([
        f"# Receiving bank options: {bank_options}. "
        "Account format: S02-XXXXX for savings or LNXXXXXX for loan. "
        "Date format: DD-MM-YYYY. Delete this row before uploading."
    ])
    writer.writerow([
        "Date", "ReceivingBank", "CustomerNo", "CustomerName",
        "CreditAmount", "Account", "ValidationStatus", "Error",
    ])
    writer.writerow([
        "01-01-2026", cash_accounts[0][0] if cash_accounts else "900-601001",
        "01146", "", "2000", "S02-01146", "", "",
    ])
    writer.writerow([
        "01-01-2026", cash_accounts[0][0] if cash_accounts else "900-601001",
        "01146", "", "5000", "LN000001", "", "",
    ])
    return response


# ── Upload & validate ───────────────────────────────────────────────────

@login_required
@user_passes_test(_is_admin)
def bulk_transaction_upload(request):
    validated_rows = None
    all_valid = False
    batch_desc = ""

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "bulk_post":
            return _handle_bulk_post(request)

        form = BulkTransactionUploadForm(request.POST, request.FILES)
        if form.is_valid():
            batch_desc = form.cleaned_data["description"]
            csv_file = form.cleaned_data["file"]
            decoded = csv_file.read().decode("utf-8-sig")
            reader = csv.DictReader(
                io.StringIO(decoded),
                fieldnames=[
                    "date", "receiving_bank", "customer_no", "customer_name",
                    "credit_amount", "account", "validation_status", "error",
                ],
            )
            rows = []
            for i, row in enumerate(reader):
                date_val = (row.get("date") or "").strip()
                if date_val.startswith("#") or date_val.lower() == "date":
                    continue
                rows.append(row)

            if not rows:
                messages.error(request, "The uploaded file contains no data rows.")
            else:
                all_valid, validated_rows = _validate_rows(rows)

                session_rows = []
                for r in validated_rows:
                    session_rows.append(_json_safe({
                        k: v for k, v in r.items()
                        if not k.startswith("_")
                    }))
                request.session["bulk_tx_rows"] = session_rows
                request.session["bulk_tx_desc"] = str(batch_desc)
                request.session.modified = True

                valid_count = sum(1 for r in validated_rows if r["validation_status"] == "Validated")
                error_count = len(validated_rows) - valid_count
                if all_valid:
                    messages.success(request, f"All {valid_count} row(s) validated successfully. Ready to post.")
                else:
                    messages.warning(
                        request,
                        f"{valid_count} row(s) valid, {error_count} row(s) have errors. Fix errors and re-upload."
                    )
    else:
        form = BulkTransactionUploadForm()

    return render(request, "administration/bulk_transaction_upload.html", {
        "form": form,
        "validated_rows": validated_rows,
        "all_valid": all_valid,
        "batch_desc": batch_desc,
    })


# ── Bulk post ───────────────────────────────────────────────────────────

def _handle_bulk_post(request):
    rows = request.session.get("bulk_tx_rows")
    batch_desc = request.session.get("bulk_tx_desc", "Bulk upload")
    if not rows:
        messages.error(request, "No validated data found. Please upload again.")
        return redirect("administration:bulk_transaction_upload")

    all_valid, rows = _validate_rows(rows)
    if not all_valid:
        messages.error(request, "Validation failed on re-check. Please review and re-upload.")
        request.session.pop("bulk_tx_rows", None)
        return redirect("administration:bulk_transaction_upload")

    batch_ref = _generate_batch_ref()
    now = timezone.now()
    user_name = request.user.get_full_name() or request.user.username
    posted = []

    try:
        with db_transaction.atomic():
            for row in rows:
                parsed_date = _parse_date(row["date"])
                amount = Decimal(str(row["credit_amount"]).replace(",", ""))
                cust_no = row["customer_no"].strip()
                account = row["account"].strip().upper()
                bank = row["receiving_bank"].strip()

                common_desc = f"{batch_desc} | {bank} | Ref: {batch_ref}"

                if _LOAN_RE.match(account):
                    loan_tx = LoanTransaction.objects.filter(
                        loan_no=account
                    ).values("loan_type", "loan_id", "account_code").first()

                    loan_type = loan_tx["loan_type"] if loan_tx else "normal_loan"
                    loan_id = loan_tx["loan_id"] if loan_tx else 0
                    acct_code = loan_tx["account_code"] if loan_tx else ""

                    LoanTransaction.objects.create(
                        cust_no=cust_no,
                        loan_id=loan_id,
                        loan_no=account,
                        loan_type=loan_type,
                        account_code=acct_code,
                        tr_date=parsed_date,
                        tr_ref=batch_ref,
                        ext_ref=bank,
                        tr_desc=common_desc,
                        debit_amount=Decimal("0.00"),
                        credit_amount=amount,
                        created_by=user_name,
                    )

                    _post_gl_entry(
                        account_code=acct_code,
                        tr_date=parsed_date,
                        tr_ref=batch_ref,
                        description=common_desc,
                        amount=amount,
                        entry_type="loan_repayment",
                        user_name=user_name,
                    )

                    posted.append({
                        **{k: v for k, v in row.items() if not k.startswith("_")},
                        "type": "Loan Repayment",
                        "loan_no": account, "batch_ref": batch_ref,
                    })

                else:
                    product_code = account.split("-")[0]
                    product = CustomerAccountsSetup.objects.filter(
                        account_code=product_code
                    ).first()

                    saving_type = product.account_type if product else product_code

                    SavingsTransaction.objects.create(
                        cust_no=cust_no,
                        saving_type=saving_type,
                        account_code=product_code,
                        tr_date=parsed_date,
                        tr_ref=batch_ref,
                        ext_ref=bank,
                        tr_desc=common_desc,
                        debit_amount=Decimal("0.00"),
                        credit_amount=amount,
                        created_by=user_name,
                    )

                    _post_gl_entry(
                        account_code=product_code,
                        tr_date=parsed_date,
                        tr_ref=batch_ref,
                        description=common_desc,
                        amount=amount,
                        entry_type="savings_deposit",
                        user_name=user_name,
                    )

                    posted.append({
                        **{k: v for k, v in row.items() if not k.startswith("_")},
                        "type": "Savings Deposit",
                        "product": product_code, "batch_ref": batch_ref,
                    })

    except Exception as e:
        messages.error(request, f"Bulk post failed: {e}")
        return redirect("administration:bulk_transaction_upload")

    request.session.pop("bulk_tx_rows", None)
    request.session.pop("bulk_tx_desc", None)

    request.session["bulk_tx_posted"] = _json_safe(posted)
    request.session["bulk_tx_batch_ref"] = str(batch_ref)
    request.session["bulk_tx_batch_desc"] = str(batch_desc)
    request.session.modified = True

    messages.success(request, f"Successfully posted {len(posted)} transaction(s). Ref: {batch_ref}")
    return redirect("administration:bulk_transaction_summary")


def _post_gl_entry(account_code, tr_date, tr_ref, description, amount,
                   entry_type, user_name):
    try:
        from accounting.models import SaccoAccount, SaccoAccountsLedger
    except ImportError:
        return

    product = CustomerAccountsSetup.objects.filter(
        account_code=account_code
    ).first()

    if not product:
        return

    # Use SaccoAccountsLedger for GL posting (lite version, no TigerBeetle)
    gl_account = getattr(product, 'sacco_gl_account', None)
    cash_account = getattr(product, 'sacco_cash_account', None)

    if not gl_account:
        return
    if not cash_account:
        cash_account = SaccoAccount.objects.filter(is_cash_account=True).first()
    if not cash_account:
        return

    if entry_type == "savings_deposit":
        SaccoAccountsLedger.objects.create(
            sacco_account=cash_account, date=tr_date, reference=tr_ref,
            description=description, debit=amount,
            credit=Decimal("0.00"), created_by=user_name,
        )
        SaccoAccountsLedger.objects.create(
            sacco_account=gl_account, date=tr_date, reference=tr_ref,
            description=description, debit=Decimal("0.00"),
            credit=amount, created_by=user_name,
        )
    elif entry_type == "loan_repayment":
        SaccoAccountsLedger.objects.create(
            sacco_account=cash_account, date=tr_date, reference=tr_ref,
            description=description, debit=amount,
            credit=Decimal("0.00"), created_by=user_name,
        )
        SaccoAccountsLedger.objects.create(
            sacco_account=gl_account, date=tr_date, reference=tr_ref,
            description=description, debit=Decimal("0.00"),
            credit=amount, created_by=user_name,
        )


# ── Summary page + PDF ─────────────────────────────────────────────────

@login_required
@user_passes_test(_is_admin)
def bulk_transaction_summary(request):
    posted = request.session.get("bulk_tx_posted", [])
    batch_ref = request.session.get("bulk_tx_batch_ref", "")
    batch_desc = request.session.get("bulk_tx_batch_desc", "")

    if not posted:
        messages.info(request, "No summary data available.")
        return redirect("administration:bulk_transaction_upload")

    total_amount = sum(Decimal(str(r.get("credit_amount", 0)).replace(",", "")) for r in posted)
    savings_count = sum(1 for r in posted if r.get("type") == "Savings Deposit")
    loan_count = sum(1 for r in posted if r.get("type") == "Loan Repayment")

    return render(request, "administration/bulk_transaction_summary.html", {
        "posted": posted,
        "batch_ref": batch_ref,
        "batch_desc": batch_desc,
        "total_amount": total_amount,
        "savings_count": savings_count,
        "loan_count": loan_count,
        "total_count": len(posted),
    })


@login_required
@user_passes_test(_is_admin)
def bulk_transaction_summary_pdf(request):
    posted = request.session.get("bulk_tx_posted", [])
    batch_ref = request.session.get("bulk_tx_batch_ref", "N/A")
    batch_desc = request.session.get("bulk_tx_batch_desc", "")

    if not posted:
        messages.info(request, "No summary data to export.")
        return redirect("administration:bulk_transaction_upload")

    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=15 * mm)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleCustom", parent=styles["Title"], fontSize=16, spaceAfter=4 * mm,
    )
    sub_style = ParagraphStyle(
        "SubCustom", parent=styles["Normal"], fontSize=9, textColor=colors.grey,
        spaceAfter=6 * mm,
    )
    cell_style = ParagraphStyle(
        "Cell", parent=styles["Normal"], fontSize=8, leading=10,
    )

    story = []
    story.append(Paragraph("Bulk Transaction Summary", title_style))
    story.append(Paragraph(
        f"Batch: {batch_ref} &nbsp;|&nbsp; {batch_desc} &nbsp;|&nbsp; "
        f"Generated: {timezone.now().strftime('%d %b %Y, %H:%M')}",
        sub_style,
    ))

    total_amount = sum(Decimal(str(r.get("credit_amount", 0)).replace(",", "")) for r in posted)
    savings_count = sum(1 for r in posted if r.get("type") == "Savings Deposit")
    loan_count = sum(1 for r in posted if r.get("type") == "Loan Repayment")

    summary_data = [
        ["Total Transactions", str(len(posted))],
        ["Savings Deposits", str(savings_count)],
        ["Loan Repayments", str(loan_count)],
        ["Total Amount", f"KES {total_amount:,.2f}"],
    ]
    summary_table = Table(summary_data, colWidths=[55 * mm, 45 * mm])
    summary_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 8 * mm))

    header = ["#", "Date", "Customer", "Name", "Account", "Type", "Amount (KES)"]
    detail_data = [header]
    for i, r in enumerate(posted, 1):
        detail_data.append([
            str(i),
            r.get("date", ""),
            r.get("customer_no", ""),
            Paragraph(r.get("customer_name", ""), cell_style),
            r.get("account", ""),
            r.get("type", ""),
            f'{Decimal(str(r.get("credit_amount", 0)).replace(",", "")):,.2f}',
        ])

    col_widths = [8 * mm, 22 * mm, 18 * mm, 42 * mm, 28 * mm, 28 * mm, 28 * mm]
    detail_table = Table(detail_data, colWidths=col_widths, repeatRows=1)
    detail_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (-1, 1), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f8f8")]),
    ]))
    story.append(detail_table)

    doc.build(story)

    buf.seek(0)
    response = HttpResponse(buf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="bulk_summary_{batch_ref}.pdf"'
    return response


# ═══════════════════════════════════════════════════════════════════════════
# MOBILE ACTIVITIES
# ═══════════════════════════════════════════════════════════════════════════

try:
    from androidapi.models import MobileActivity
except ImportError:
    MobileActivity = None

from .forms import MobileActivityForm, MobileActivityBulkForm


@login_required
@min_role_required('manager')
def mobile_activity_customer_search(request):
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return JsonResponse({'results': []})

    qs = Customer.objects.filter(
        models.Q(cust_no__icontains=q) |
        models.Q(national_id__icontains=q) |
        models.Q(full_name__icontains=q)
    ).select_related('user')[:15]

    results = []
    for c in qs:
        is_onboarded = c.user is not None
        already_exists = MobileActivity and MobileActivity.objects.filter(cust_no=c.cust_no).exists()
        results.append({
            'cust_no':      c.cust_no,
            'full_name':    c.full_name or '',
            'phone':        c.phone or '',
            'national_id':  c.national_id or '',
            'username':     c.user.username if c.user else '',
            'device_id':    c.user.device_id or '' if c.user else '',
            'is_onboarded': is_onboarded,
            'already_exists': already_exists,
        })

    return JsonResponse({'results': results})


def _sync_customer_fields(activity_obj):
    try:
        cust = Customer.objects.get(cust_no=activity_obj.cust_no)
    except Customer.DoesNotExist:
        return

    changed = False
    if activity_obj.phone and activity_obj.phone != (cust.phone or ''):
        cust.phone = activity_obj.phone
        changed = True
    if activity_obj.national_id and activity_obj.national_id != (cust.national_id or ''):
        cust.national_id = activity_obj.national_id
        changed = True
    if changed:
        cust.save(update_fields=['phone', 'national_id'])

    if cust.user and activity_obj.phone and activity_obj.phone != (cust.user.phone or ''):
        cust.user.phone = activity_obj.phone
        cust.user.save(update_fields=['phone'])


@login_required
@min_role_required('manager')
def mobile_activity_list(request):
    if MobileActivity is None:
        messages.error(request, 'MobileActivity model not available.')
        return redirect('administration:company_details')

    q = request.GET.get('q', '').strip()
    qs = MobileActivity.objects.all()
    if q:
        qs = qs.filter(
            models.Q(cust_no__icontains=q) |
            models.Q(full_name__icontains=q) |
            models.Q(phone__icontains=q) |
            models.Q(national_id__icontains=q)
        )

    bulk_form = MobileActivityBulkForm()
    return render(request, 'administration/mobile_activity_list.html', {
        'activities': qs,
        'query': q,
        'bulk_form': bulk_form,
        'total': MobileActivity.objects.count(),
        'authorized_count': MobileActivity.objects.filter(authorize_withdrawal=True).count(),
        'denied_count': MobileActivity.objects.filter(deny_all=True).count(),
    })


@login_required
@min_role_required('manager')
def mobile_activity_create(request):
    if MobileActivity is None:
        messages.error(request, 'MobileActivity model not available.')
        return redirect('administration:mobile_activity_list')

    prefill = {}
    cust_no_q = request.GET.get('cust_no', '').strip()
    not_onboarded_warning = False
    if cust_no_q:
        try:
            cust = Customer.objects.select_related('user').get(cust_no=cust_no_q)
            prefill = {
                'cust_no': cust.cust_no,
                'full_name': cust.full_name,
                'phone': cust.phone or '',
                'national_id': cust.national_id or '',
            }
            if cust.user:
                prefill['username'] = cust.user.username
                prefill['device_id'] = cust.user.device_id or ''
            else:
                not_onboarded_warning = True
        except Exception:
            pass

    if request.method == 'POST':
        form = MobileActivityForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            if obj.authorize_withdrawal:
                obj.enabled_at = timezone.now()
                obj.enabled_by = request.user.username
            obj.save()
            _sync_customer_fields(obj)
            if obj.authorize_withdrawal and obj.phone:
                _notify_mobile_enabled(obj, request.user.username)
            messages.success(request, f'Mobile activity for {obj.cust_no} ({obj.full_name}) created.')
            return redirect('administration:mobile_activity_list')
    else:
        form = MobileActivityForm(initial=prefill)

    return render(request, 'administration/mobile_activity_form.html', {
        'form': form,
        'title': 'Add Mobile Activity',
        'is_edit': False,
        'not_onboarded_warning': not_onboarded_warning,
    })


@login_required
@min_role_required('manager')
def mobile_activity_update(request, pk):
    obj = get_object_or_404(MobileActivity, pk=pk)
    was_authorized = obj.authorize_withdrawal

    not_onboarded_warning = False
    try:
        cust = Customer.objects.select_related('user').filter(cust_no=obj.cust_no).first()
        if cust and not cust.user:
            not_onboarded_warning = True
    except Exception:
        pass

    if request.method == 'POST':
        form = MobileActivityForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save(commit=False)
            if obj.authorize_withdrawal and not was_authorized:
                obj.enabled_at = timezone.now()
                obj.enabled_by = request.user.username
            obj.save()
            _sync_customer_fields(obj)
            if obj.authorize_withdrawal and not was_authorized and obj.phone:
                _notify_mobile_enabled(obj, request.user.username)
            messages.success(request, f'Mobile activity for {obj.cust_no} updated.')
            return redirect('administration:mobile_activity_list')
    else:
        form = MobileActivityForm(instance=obj)

    return render(request, 'administration/mobile_activity_form.html', {
        'form': form,
        'title': f'Edit Mobile Activity - {obj.cust_no}',
        'is_edit': True,
        'not_onboarded_warning': not_onboarded_warning,
    })


@login_required
@min_role_required('manager')
def mobile_activity_toggle(request, pk):
    obj = get_object_or_404(MobileActivity, pk=pk)
    was_authorized = obj.authorize_withdrawal
    obj.authorize_withdrawal = not obj.authorize_withdrawal
    if obj.authorize_withdrawal:
        obj.enabled_at = timezone.now()
        obj.enabled_by = request.user.username
    obj.save(update_fields=['authorize_withdrawal', 'enabled_at', 'enabled_by', 'updated_at'])

    if obj.authorize_withdrawal and not was_authorized and obj.phone:
        _notify_mobile_enabled(obj, request.user.username)

    status_label = 'enabled' if obj.authorize_withdrawal else 'disabled'
    messages.success(request, f'Mobile activities {status_label} for {obj.cust_no} ({obj.full_name}).')
    return redirect('administration:mobile_activity_list')


@login_required
@min_role_required('admin')
def mobile_activity_bulk(request):
    if request.method != 'POST':
        return redirect('administration:mobile_activity_list')

    form = MobileActivityBulkForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Please confirm the action.')
        return redirect('administration:mobile_activity_list')

    action = form.cleaned_data['action']
    if action == 'deny_all':
        MobileActivity.deny_all_customers()
        messages.warning(request, 'All mobile withdrawals temporarily DENIED. Individual settings preserved.')
    elif action == 'reset_all':
        MobileActivity.reset_all()
        messages.success(request, 'Deny-all cleared. Individual customer settings restored.')

    return redirect('administration:mobile_activity_list')


@login_required
@min_role_required('manager')
def mobile_activity_delete(request, pk):
    obj = get_object_or_404(MobileActivity, pk=pk)
    if request.method == 'POST':
        cust_no = obj.cust_no
        obj.delete()
        messages.success(request, f'Mobile activity for {cust_no} deleted.')
    return redirect('administration:mobile_activity_list')


def _notify_mobile_enabled(activity_obj, admin_username):
    try:
        from sms.models import SMSLog
        sms_body = (
            f"Dear {activity_obj.full_name or 'Member'}, "
            f"your mobile banking activities have been enabled. "
            f"You can now perform withdrawals and mobile loan disbursements "
            f"from your app. For support, contact your SACCO."
        )
        SMSLog.objects.create(
            phone=activity_obj.phone,
            message=sms_body,
            status='pending',
            created_by=admin_username,
        )
    except Exception:
        pass

    try:
        from sms.services import email_notify
        cust = Customer.objects.filter(cust_no=activity_obj.cust_no).first()
        if cust and cust.reg_email:
            email_notify(
                recipient_to=cust.reg_email,
                subject='Mobile Banking Activities Enabled',
                body=(
                    f"Dear {activity_obj.full_name or 'Member'},\n\n"
                    f"Your mobile banking activities have been enabled.\n"
                    f"You can now perform withdrawals and mobile loan disbursements "
                    f"directly from your mobile app.\n\n"
                    f"If you did not request this, please contact your SACCO immediately.\n\n"
                    f"Regards,\nYour SACCO"
                ),
                created_by=admin_username,
                send_now=False,
            )
    except Exception:
        pass
