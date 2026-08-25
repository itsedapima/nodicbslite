from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db.models import Q
import csv
from io import StringIO, BytesIO

from customers.models import Customer
from sms.models import SMSLog
from .forms import ComposeSMSForm


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


def _parse_phone_file(uploaded_file):
    """Parse CSV or Excel; returns [{'phone': str, 'first_name': str}, ...]."""
    fname = uploaded_file.name.lower()
    recipients = []
    
    def _find_column(headers, candidates):
        """Find first matching column index by lowercase header name."""
        for idx, h in enumerate(headers):
            if h and str(h).strip().lower() in candidates:
                return idx
        return None
    
    PHONE_COLS = {'phone', 'phone', 'phone_number', 'phonenumber', 'msisdn'}
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
            raise ValueError("CSV must contain a 'phone', 'phone', or 'phone_number' column.")
        
        for row in rows[1:]:
            if len(row) > phone_idx and row[phone_idx]:
                phone = str(row[phone_idx]).strip()
                name = str(row[name_idx]).strip() if name_idx is not None and len(row) > name_idx else ''
                if phone:
                    recipients.append({'phone': phone, 'first_name': name})
    
    elif fname.endswith(('.xlsx', '.xls')):
        try:
            import openpyxl
        except ImportError:
            raise ValueError("openpyxl is not installed. Run: pip install openpyxl")
        
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
            raise ValueError("Excel must contain a 'phone', 'phone', or 'phone_number' column.")
        
        for row in rows_iter:
            if row and len(row) > phone_idx and row[phone_idx]:
                phone = str(row[phone_idx]).strip()
                name = str(row[name_idx]).strip() if name_idx is not None and len(row) > name_idx and row[name_idx] else ''
                if phone:
                    recipients.append({'phone': phone, 'first_name': name})
    else:
        raise ValueError("Unsupported file format. Use CSV or Excel.")
    
    if not recipients:
        raise ValueError("No valid phone numbers found in the file.")
    
    return recipients


def _format_message(salutation_template, first_name, message_content):
    """Format message with personalization. Safe against bad templates."""
    try:
        sal = salutation_template.format(first_name=first_name)
    except (KeyError, IndexError, ValueError):
        sal = salutation_template.replace('{first_name}', first_name)
    return f"{sal} {message_content}".strip()


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
                # 1. Collect recipients
                recipients = []
                
                if recipient_type == 'single':
                    phone = form.cleaned_data['phone_number'].strip()
                    recipients.append({'phone': phone, 'first_name': ''})
                
                elif recipient_type == 'all_customers':
                    # Streaming-friendly: only fetch the two fields we need
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
                
                # 2. BULK CREATE SMSLog entries (one DB hit, not N)
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
                    f'✓ {len(sms_logs)} SMS scheduled successfully. '
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
