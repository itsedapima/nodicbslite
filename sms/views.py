"""
sms/views.py
-------------
Views for composing bulk SMS, viewing SMS logs, and managing
FrequentNotification templates (reminders & marketing).

All delivery is queue-based — views only create SMSLog rows with
status='pending'. The Django-Q2 worker handles actual sending.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

import csv
from io import StringIO, BytesIO

from customers.models import Customer
from .models import SMSLog, FrequentNotification, MemberSnapshot
from .forms import ComposeSMSForm, FrequentNotificationForm


# ═══════════════════════════════════════════════════════════════════════════
# AJAX customer search
# ═══════════════════════════════════════════════════════════════════════════

@login_required
def search_customers(request):
    """Fast AJAX customer search — limited to 15 results, .values() only."""
    query = (request.GET.get('q') or '').strip()

    if len(query) < 2:
        return JsonResponse([], safe=False)

    customers = Customer.objects.filter(
        Q(full_name__icontains=query) |
        Q(phone__icontains=query) |
        Q(cust_no__icontains=query)
    ).values('id', 'full_name', 'phone', 'cust_no')[:15]

    return JsonResponse([
        {
            'id': c['id'],
            'text': f"{c['full_name']} ({c['phone']}) — {c['cust_no']}"
        }
        for c in customers
    ], safe=False)


# ═══════════════════════════════════════════════════════════════════════════
# File parsing
# ═══════════════════════════════════════════════════════════════════════════

def _parse_phone_file(uploaded_file):
    """Parse CSV or Excel; returns [{'phone': str, 'first_name': str}, ...]."""
    fname = uploaded_file.name.lower()
    recipients = []

    def _find_column(headers, candidates):
        for idx, h in enumerate(headers):
            if h and str(h).strip().lower() in candidates:
                return idx
        return None

    PHONE_COLS = {'phone', 'phone_number', 'phonenumber', 'msisdn', 'mobile'}
    NAME_COLS = {'first_name', 'firstname', 'name', 'full_name', 'fullname'}

    if fname.endswith('.csv'):
        text = uploaded_file.read().decode('utf-8', errors='ignore')
        reader = csv.reader(StringIO(text))
        rows = list(reader)
        if not rows:
            raise ValueError("Empty CSV file.")

        headers = rows[0]
        phone_idx = _find_column(headers, PHONE_COLS)
        name_idx = _find_column(headers, NAME_COLS)

        if phone_idx is None:
            raise ValueError("CSV must contain a 'phone' or 'mobile' column.")

        for row in rows[1:]:
            if len(row) > phone_idx and row[phone_idx]:
                phone = str(row[phone_idx]).strip()
                name = str(row[name_idx]).strip() if name_idx is not None and len(row) > name_idx else ''
                if phone:
                    recipients.append({'phone': phone, 'first_name': name})

    elif fname.endswith(('.xlsx', '.xls')):
        import openpyxl
        wb = openpyxl.load_workbook(BytesIO(uploaded_file.read()), read_only=True)
        ws = wb.active

        rows_iter = ws.iter_rows(values_only=True)
        try:
            headers = next(rows_iter)
        except StopIteration:
            raise ValueError("Empty Excel file.")

        phone_idx = _find_column(headers, PHONE_COLS)
        name_idx = _find_column(headers, NAME_COLS)

        if phone_idx is None:
            raise ValueError("Excel must contain a 'phone' or 'mobile' column.")

        for row in rows_iter:
            if row and len(row) > phone_idx and row[phone_idx]:
                phone = str(row[phone_idx]).strip()
                name = (
                    str(row[name_idx]).strip()
                    if name_idx is not None and len(row) > name_idx and row[name_idx]
                    else ''
                )
                if phone:
                    recipients.append({'phone': phone, 'first_name': name})
    else:
        raise ValueError("Unsupported file format. Use CSV or Excel.")

    if not recipients:
        raise ValueError("No valid phone numbers found in the file.")

    return recipients


# ═══════════════════════════════════════════════════════════════════════════
# Message formatting
# ═══════════════════════════════════════════════════════════════════════════

def _format_message(salutation_template, first_name, message_content):
    """Format message with personalization. Safe against bad templates."""
    try:
        sal = salutation_template.format(first_name=first_name)
    except (KeyError, IndexError, ValueError):
        sal = salutation_template.replace('{first_name}', first_name)
    return f"{sal} {message_content}".strip()


# ═══════════════════════════════════════════════════════════════════════════
# Bulk SMS compose view
# ═══════════════════════════════════════════════════════════════════════════

@login_required
def bulk_sms(request):
    """Compose & schedule bulk SMS. Creates SMSLog rows with status='pending'."""

    if request.method == 'POST':
        form = ComposeSMSForm(request.POST, request.FILES)

        if form.is_valid():
            recipient_type = form.cleaned_data['recipient_type']
            salutation = form.cleaned_data['salutation'].strip()
            message_content = form.cleaned_data['message_content'].strip()
            username = request.user.username if request.user.is_authenticated else 'system'

            try:
                recipients = []

                if recipient_type == 'single':
                    phone = form.cleaned_data['phone_number'].strip()
                    recipients.append({'phone': phone, 'first_name': ''})

                elif recipient_type == 'all_customers':
                    customers = Customer.objects.filter(
                        phone__isnull=False
                    ).exclude(phone='').values_list('phone', 'first_name')
                    recipients = [
                        {'phone': phone, 'first_name': fname or ''}
                        for phone, fname in customers
                    ]

                elif recipient_type == 'customer_group':
                    customer_ids = form.cleaned_data['customer_ids']
                    customers = Customer.objects.filter(
                        id__in=customer_ids
                    ).values_list('phone', 'first_name')
                    recipients = [
                        {'phone': phone, 'first_name': fname or ''}
                        for phone, fname in customers if phone
                    ]

                elif recipient_type == 'file_upload':
                    recipients = _parse_phone_file(form.cleaned_data['phone_file'])

                if not recipients:
                    messages.error(request, 'No valid recipients found.')
                    return render(request, 'sms/bulk_sms.html', {'form': form})

                sms_logs = [
                    SMSLog(
                        phone=r['phone'].strip(),
                        message=_format_message(salutation, r['first_name'].strip(), message_content),
                        status='pending',
                        created_by=username,
                    )
                    for r in recipients
                ]
                SMSLog.objects.bulk_create(sms_logs, batch_size=500)

                messages.success(
                    request,
                    f'\u2713 {len(sms_logs)} SMS scheduled successfully. '
                    'They will be sent by the background job.'
                )
                return redirect('sms:bulk_sms')

            except ValueError as e:
                messages.error(request, str(e))
            except Exception as e:
                messages.error(request, f'Unexpected error: {e}')
    else:
        form = ComposeSMSForm()

    return render(request, 'sms/bulk_sms.html', {'form': form})


# ═══════════════════════════════════════════════════════════════════════════
# SMS log viewer
# ═══════════════════════════════════════════════════════════════════════════

@login_required
def view_sms(request):
    """SMS logs view with filtering, search, and pagination."""
    status_filter = request.GET.get('status', '').strip()
    search_query = request.GET.get('q', '').strip()
    page_number = request.GET.get('page', 1)
    per_page = 50

    queryset = SMSLog.objects.all().order_by('-created_at')

    if search_query:
        queryset = queryset.filter(
            Q(phone__icontains=search_query) |
            Q(message__icontains=search_query)
        )

    if status_filter in ('sent', 'pending', 'failed'):
        queryset = queryset.filter(status=status_filter)
    else:
        status_filter = None

    paginator = Paginator(queryset, per_page)
    page_obj = paginator.get_page(page_number)

    current = page_obj.number
    total = paginator.num_pages
    window = 2
    start = max(current - window, 1)
    end = min(current + window, total)
    page_range = range(start, end + 1)
    show_first = start > 1
    show_last = end < total

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
        sms_list = [
            {
                'id': log.id,
                'phone': log.phone,
                'message': log.message,
                'status': log.status,
                'attempts': log.attempts,
                'error_message': log.error_message or None,
                'created_at': log.created_at.strftime('%d %b %Y, %H:%M'),
            }
            for log in page_obj.object_list
        ]
        return JsonResponse({
            'results': sms_list,
            'has_next': page_obj.has_next(),
            'has_previous': page_obj.has_previous(),
            'current_page': page_obj.number,
            'total_pages': paginator.num_pages,
            'total_count': paginator.count,
        })

    context = {
        'page_obj': page_obj,
        'sms_logs': page_obj.object_list,
        'current_status': status_filter,
        'search_query': search_query,
        'page_range': page_range,
        'show_first': show_first,
        'show_last': show_last,
    }
    return render(request, 'sms/view_sms.html', context)


# ═══════════════════════════════════════════════════════════════════════════
#  FREQUENT NOTIFICATIONS — CRUD + Run
# ═══════════════════════════════════════════════════════════════════════════

@login_required
def frequent_notifications_list(request):
    """List all FrequentNotification templates grouped by type."""
    notifs = FrequentNotification.objects.all()
    snapshot_count = MemberSnapshot.objects.count()
    last_snapshot = MemberSnapshot.objects.order_by('-refreshed_at').first()

    context = {
        'notifications': notifs,
        'snapshot_count': snapshot_count,
        'last_snapshot_at': last_snapshot.refreshed_at if last_snapshot else None,
    }
    return render(request, 'sms/frequent_notifications_list.html', context)


@login_required
def frequent_notification_create(request):
    """Create a new FrequentNotification template."""
    if request.method == 'POST':
        form = FrequentNotificationForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, '✓ Notification template created.')
            return redirect('sms:frequent_notifications')
    else:
        form = FrequentNotificationForm()

    return render(request, 'sms/frequent_notification_form.html', {
        'form': form,
        'title': 'Create Notification Template',
        'is_edit': False,
    })


@login_required
def frequent_notification_edit(request, pk):
    """Edit an existing FrequentNotification template."""
    notif = get_object_or_404(FrequentNotification, pk=pk)

    if request.method == 'POST':
        form = FrequentNotificationForm(request.POST, instance=notif)
        if form.is_valid():
            form.save()
            messages.success(request, f'✓ "{notif.name}" updated.')
            return redirect('sms:frequent_notifications')
    else:
        form = FrequentNotificationForm(instance=notif)

    return render(request, 'sms/frequent_notification_form.html', {
        'form': form,
        'title': f'Edit: {notif.name}',
        'is_edit': True,
        'notif': notif,
    })


@login_required
def frequent_notification_toggle(request, pk):
    """Toggle a notification on/off via AJAX or regular POST."""
    notif = get_object_or_404(FrequentNotification, pk=pk)
    notif.is_active = not notif.is_active
    notif.save(update_fields=['is_active'])

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'is_active': notif.is_active, 'name': notif.name})

    status = 'activated' if notif.is_active else 'paused'
    messages.success(request, f'"{notif.name}" {status}.')
    return redirect('sms:frequent_notifications')


@login_required
def frequent_notification_delete(request, pk):
    """Delete a notification template."""
    notif = get_object_or_404(FrequentNotification, pk=pk)
    if request.method == 'POST':
        name = notif.name
        notif.delete()
        messages.success(request, f'"{name}" deleted.')
        return redirect('sms:frequent_notifications')
    return render(request, 'sms/frequent_notification_confirm_delete.html', {
        'notif': notif,
    })


@login_required
def frequent_notification_run(request, pk):
    """
    Manually trigger a notification — generates SMS immediately.
    POST only for safety.
    """
    notif = get_object_or_404(FrequentNotification, pk=pk)

    if request.method != 'POST':
        return redirect('sms:frequent_notifications')

    from .notification_helpers import run_notification
    result = run_notification(notification_id=notif.pk)

    if result.get('status') == 'ok':
        count = result.get('queued', 0)
        messages.success(
            request,
            f'✓ "{notif.name}" executed — {count} personalized SMS queued for delivery.'
        )
    else:
        messages.error(
            request,
            f'Error running "{notif.name}": {result.get("message", "Unknown error")}'
        )

    return redirect('sms:frequent_notifications')


@login_required
def frequent_notification_preview(request, pk):
    """AJAX endpoint: preview the SMS template with sample data."""
    notif = get_object_or_404(FrequentNotification, pk=pk)

    from django.conf import settings
    # Dynamic account_no based on category
    acct_map = {
        'fixed_deposit_marketing': '00123FD',
        'share_capital_marketing': '00123SC',
        'share_capital_howto': '00123SC',
        'savings_deposit_howto': '00123DEP',
        'dormant_reactivation': '00123DEP',
    }
    sample_ctx = {
        'first_name': 'John',
        'cust_no': '00123',
        'paybill': getattr(settings, 'MPESA_SHORTCODE', '000000') or '000000',
        'sacco_name': getattr(settings, 'SMS_SENDER_NAME', 'SACCO'),
        'account_no': acct_map.get(notif.category, '00123EL'),
        'loan_no': 'LN000045',
        'loan_name': 'Emergency Loan',
        'loan_offer': 'Emergency Loan',
        'offers_list': 'Emergency Loan up to KES 150,000 and Development Loan up to KES 90,000',
        'loan_balance': '45,000.00',
        'arrears': '5,200.00',
        'installment': '4,500.00',
        'eligible_amount': '150,000.00',
        'min_balance': '10,000.00',
        'balance': '25,000.00',
    }

    try:
        rendered = notif.message_template.format(**sample_ctx)
    except (KeyError, IndexError, ValueError):
        rendered = notif.message_template
        for k, v in sample_ctx.items():
            rendered = rendered.replace('{' + k + '}', v)

    return JsonResponse({
        'name': notif.name,
        'category': notif.get_category_display(),
        'rendered': rendered,
        'char_count': len(rendered),
    })
