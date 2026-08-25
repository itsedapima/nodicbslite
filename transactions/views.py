# --- Python Standard Library ---
import json
from decimal import Decimal
import random
import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction, models
from django.http import JsonResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Q
from django.urls import reverse # Useful if you're redirecting by name
from loans.models import LoanHistory
import datetime
from django.db import transaction
from django.db.models import Sum, Q
from .models import SavingsTransaction, DividendBatch, DividendDetail,DividendSlipItem
from accounting.models import SaccoAccount, SaccoAccountsLedger,SaccoAccountBalance
# --- Project Specific Models & Forms ---
from customers.models import Customer # Assuming this is the correct path for Customer model
from .forms import (
    TransactionForm, 
    SavingsPaymentFormSet, 
    LoanPaymentFormSet)
from .models import (
    SavingsTransaction, 
    LoanTransaction, 
    CustomerAccountsSetup,
    MpesaNotification, 
)
from .utils import make_tr_ref
from django.shortcuts import render
from .models import DividendBatch

from django.shortcuts import render
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from .models import SavingsTransaction, LoanTransaction
from customers.models import Customer 
from transactions.models import CustomerAccountsSetup
from loans.models import LoanHistory
import datetime
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.http import JsonResponse

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from .models import CustomerAccountsSetup
from .forms import CustomerAccountsSetupForm

from django.shortcuts import render
from django.db.models import Q
from django.utils import timezone
from datetime import datetime
from .models import PostedMpesaNotification
from dateutil.relativedelta import relativedelta # You may need to: pip install python-dateutil
from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse
from .models import MpesaNotification, PostedMpesaNotification
from django.http import JsonResponse
from accounts.models import CustomUser
from django.http import JsonResponse
from django.db.models import Q

def customer_search_ajax(request):
    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse({'results': []})

    customers = Customer.objects.filter(
        Q(cust_no__icontains=query) |
        Q(full_name__icontains=query) |
        Q(national_id__icontains=query)
    )[:10]  # Limit to 10 for speed and UI compactness

    results = []
    for c in customers:
        results.append({
            'cust_no': c.cust_no,
            'full_name': c.full_name,
            'national_id': c.national_id,
        })
    
    return JsonResponse({'results': results})
@require_GET
def search_customers(request):
    q = request.GET.get("q", "").strip()
    results = []

    if q:
        qs = Customer.objects.filter(
            Q(cust_no__icontains=q) |
            Q(full_name__icontains=q) |
            Q(first_name__icontains=q) |
            Q(phone__icontains=q) |
            Q(national_id__icontains=q)
        ).order_by("full_name")[:20]  # limit to avoid huge payloads

        results = [
            {
                "id": c.id,
                "cust_no": c.cust_no,
                "full_name": c.full_name,
                "national_id": c.national_id,
                "phone": c.phone,
            }
            for c in qs
        ]

    return JsonResponse({"results": results})
@require_GET
def customer_by_cust_no(request):
    cust_no = request.GET.get("cust_no")
    try:
        c = Customer.objects.get(cust_no=cust_no)
        return JsonResponse({"found": True, "full_name": c.full_name, "first_name": c.first_name, "phone": c.phone})
    except Exception:
        return JsonResponse({"found": False})



# stubbed SMS function. Replace with your implementation

def send_sms_local(mobile, text, cust_no=None, username=None):
    # integrate your SMS gateway here
    # for now, just print to server logs
    print("SMS to", mobile, text)
    return True

@require_GET
def get_customer_accounts_api(request):
    """Fetches unique savings types and specific loan accounts with descriptions."""
    cust_no = request.GET.get("cust_no")
    if not cust_no:
        return JsonResponse({"results": []})

    results = []

    # 1. Fetch Savings Accounts
    savings_accounts = CustomerAccountsSetup.objects.filter(
        is_active=True, 
        is_loan_account=False
    ).values('account_code', 'account_name', 'account_type').distinct()

    for acc in savings_accounts:
        results.append({
            "id": acc['account_type'], 
            "text": f"{acc['account_code']} - {acc['account_name']}"
        })

    # 2. Fetch Loan Accounts
    # Find active loan numbers for this customer
    active_loan_nos = LoanTransaction.objects.filter(
        cust_no=cust_no
    ).exclude(loan_no__isnull=True).values_list('loan_no', flat=True).distinct()

    # Optimized Query: Use select_related to get the linked account name immediately
    loans = LoanHistory.objects.filter(
        loan_no__in=active_loan_nos
    ).select_related('loan_type').only('loan_no', 'loan_type__account_name')

    for ln in loans:
        # Access the name directly via the ForeignKey relationship
        loan_label = ln.loan_type.account_name if ln.loan_type else "Unknown Loan Type"
        results.append({
            "id": ln.loan_no, 
            "text": f"{ln.loan_no} - {loan_label}",
            "is_loan": True # Flag for loans
        })

    return JsonResponse({"results": results})

@login_required
def get_customer_loans(request):
    cust_no = request.GET.get('cust_no')
    try:
        customer = Customer.objects.get(cust_no=cust_no)
        # Fetching loan_type info in the same query
        loans = LoanHistory.objects.filter(customer=customer).select_related('loan_type').order_by('-loan_date')
        
        loan_data = []
        for l in loans:
            # Uses the __str__ method of CustomerAccountsSetup or specifically the account_name
            readable_type = l.loan_type.account_name if l.loan_type else "Loan"
            loan_data.append({
                'id': l.loan_no, 
                'name': f"{l.loan_no} - {readable_type}"
            })
        return JsonResponse({'success': True, 'loans': loan_data})
    except Customer.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Customer not found'}, status=404)
import uuid
import logging
from decimal import Decimal
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.utils import timezone
from sms.models import SMSLog
from customers.models import Customer
from loans.models import LoanHistory 
from .models import BulkUploadQueue, SavingsTransaction, LoanTransaction,CustomerAccountsSetup
from .utils import make_tr_ref # Your existing reference generator script

logger = logging.getLogger(__name__)

@login_required
def bulk_post_transaction(request):
    """Renders the Excel-style bulk posting interface."""
    # We generate a unique batch session ID for this page load
    session_id = str(uuid.uuid4())
    
    # Pre-fetch savings types for the dropdown configuration
    savings_accounts = CustomerAccountsSetup.objects.filter(
        #account_type__in=['share_capital', 'fixed_deposit', 'savings_deposit', 'junior_account'],
        is_active=True
    ).values('account_type', 'account_name').order_by('account_code')

    context = {
        "session_id": session_id,
        "savings_accounts": list(savings_accounts),
        "today": timezone.now().date().isoformat()
    }
    return render(request, "transactions/bulk_post_transaction.html", context)

from django.db import transaction
from decimal import Decimal, InvalidOperation
from django.utils import timezone
import json

@login_required
@require_POST
def save_bulk_queue(request):
    """
    Commercial-grade staging of bulk transactions using explicit model fields.
    """
    try:
        data = json.loads(request.body)
        rows = data.get("rows", [])
        session_id = data.get("session_id")
        
        if not session_id:
            return JsonResponse({"success": False, "error": "Missing session ID"}, status=400)

        with transaction.atomic():
            # Clear previous pending attempts for this session
            BulkUploadQueue.objects.filter(session_key=session_id, status='pending').delete()

            queue_entries = []
            for index, row in enumerate(rows):
                cust_no = row.get("cust_no")
                raw_account_id = row.get("account_type")  # Contains account_type string or loan_no string
                is_loan = row.get("is_loan", False)
                
                try:
                    amount = Decimal(str(row.get("amount") or 0))
                except (InvalidOperation, ValueError):
                    amount = Decimal('0.00')

                if not cust_no or not raw_account_id or amount <= 0:
                    continue 

                # Validate Customer
                customer = Customer.objects.filter(cust_no=cust_no).first()
                if not customer:
                    return JsonResponse({
                        "success": False, 
                        "error": f"Row {index + 1}: Member {cust_no} not found."
                    }, status=400)

                # Base entry payload matching your model's exact native architecture
                entry_data = {
                    "date": row.get("date") or timezone.now().date(),
                    "customer": customer,
                    "amount": amount,
                    "description": row.get("description", "Bulk Posting"),
                    "session_key": session_id,
                    "created_by": request.user,
                    "status": 'pending',
                    "is_loan": is_loan
                }

                if is_loan:
                    # Look up the loan using the account_type field value (which is the loan_no)
                    loan_obj = LoanHistory.objects.filter(
                        loan_no=raw_account_id, 
                        customer=customer
                    ).select_related('loan_type').first()

                    if not loan_obj:
                        return JsonResponse({
                            "success": False, 
                            "error": f"Row {index + 1}: Loan {raw_account_id} not found for Member {cust_no}."
                        }, status=400)

                    # Update using native primitive fields to prevent 'unexpected keyword' errors
                    entry_data.update({
                        "loan_id": loan_obj.id,
                        "loan_no": loan_obj.loan_no,
                        "loan_type": loan_obj.loan_type.account_name if loan_obj.loan_type else "Loan",
                        "saving_type": "" 
                    })
                else:
                    # For savings, validate that the account code/type is structural
                    setup = CustomerAccountsSetup.objects.filter(account_type=raw_account_id).first()
                    entry_data.update({
                        "saving_type": raw_account_id,
                        "loan_id": 0,
                        "loan_no": "",
                        "loan_type": setup.account_name if setup else "Savings"
                    })

                queue_entries.append(BulkUploadQueue(**entry_data))

            if queue_entries:
                BulkUploadQueue.objects.bulk_create(queue_entries)

        return JsonResponse({"success": True, "count": len(queue_entries)})

    except Exception as e:
        return JsonResponse({"success": False, "error": f"System Error: {str(e)}"}, status=500)
import logging
from django.db import transaction, models
from django.shortcuts import get_object_or_404
from decimal import Decimal

logger = logging.getLogger(__name__)
import logging
from django.db import transaction, models
from django.utils import timezone
from decimal import Decimal

logger = logging.getLogger(__name__)

@login_required
@require_POST
def process_bulk_post(request):
    """
    Commercial-grade bulk ledger posting using model primitive definitions.
    """
    try:
        data = json.loads(request.body)
        session_id = data.get("session_id")
        
        queue_items = BulkUploadQueue.objects.filter(
            session_key=session_id, 
            status='pending'
        ).select_related('customer')

        if not queue_items.exists():
            return JsonResponse({"success": False, "error": "No pending transactions found for this session."}, status=400)

        username = request.user.username
        sms_logs = []
        processed_count = 0

        with transaction.atomic():
            for item in queue_items:
                
                if not item.is_loan:
                    # 1. Generate unique reference using the savings string identifier
                    tr_ref = make_tr_ref(item.saving_type)

                    # 2. Write to Savings Ledger
                    SavingsTransaction.objects.create(
                        cust_no=item.customer.cust_no,
                        tr_date=item.date,
                        tr_ref=tr_ref,
                        tr_desc=item.description,
                        debit_amount=0,
                        credit_amount=item.amount,
                        created_by=username,
                        saving_type=item.saving_type
                    )
                    
                    # 3. Aggregate Balance safely
                    bal = SavingsTransaction.objects.filter(
                        cust_no=item.customer.cust_no, 
                        saving_type=item.saving_type
                    ).aggregate(
                        total=models.Sum(models.F('credit_amount') - models.F('debit_amount'))
                    )['total'] or Decimal('0.00')

                    acc_name = item.saving_type.replace('_', ' ').title()
                    msg = (f"Dear {item.customer.full_name}, we received KES {item.amount:,.2f} "
                           f"for your {acc_name} Account. New balance is KES {bal:,.2f}.")

                else:
                    # 4. Fetch the structural account config via the loan configuration text
                    # This ensures make_tr_ref receives the system's preferred type tracking
                    tr_ref = make_tr_ref(item.loan_no)

                    # 5. Write to Loan Ledger
                    LoanTransaction.objects.create(
                        cust_no=item.customer.cust_no,
                        loan_id=item.loan_id,
                        loan_no=item.loan_no,
                        loan_type=item.loan_type,
                        tr_date=item.date,
                        tr_ref=tr_ref,
                        tr_desc=item.description,
                        debit_amount=0,
                        credit_amount=item.amount,
                        created_by=username
                    )

                    # 6. Aggregate Loan Outstanding Balance cleanly
                    bal = LoanTransaction.objects.filter(
                        loan_id=item.loan_id,loan_no=item.loan_no
                    ).aggregate(
                        total=models.Sum(models.F('debit_amount') - models.F('credit_amount'))
                    )['total'] or Decimal('0.00')

                    msg = (f"Dear {item.customer.full_name}, KES {item.amount:,.2f} received "
                           f"for Loan {item.loan_no}. Outstanding balance is KES {bal:,.2f}.")

                # Queue outbound notifications
                if item.customer.phone:
                    sms_logs.append(SMSLog(
                        phone=item.customer.phone,
                        message=msg,
                        status='pending',
                        created_by=username
                    ))
                
                processed_count += 1

            # Update the staging processing boundaries
            queue_items.update(status='processed', processed_at=timezone.now())

            if sms_logs:
                SMSLog.objects.bulk_create(sms_logs)

        return JsonResponse({
            "success": True, 
            "processed_count": processed_count,
            "message": f"Successfully posted {processed_count} transactions."
        })

    except Exception as e:
        logger.exception("Bulk ledger posting aborted.") 
        BulkUploadQueue.objects.filter(session_key=session_id, status='pending').update(
            status='failed', 
            error_message=str(e)
        )
        return JsonResponse({"success": False, "error": f"Posting failed: {str(e)}"}, status=500)

@login_required
def post_transaction(request, pk=None):
    template_name = "transactions/post_transaction.html"
    customer = None
    if pk:
        customer = get_object_or_404(Customer, pk=pk)

    initial_data = {"date": timezone.now()}
    if customer:
        initial_data["cust_no"] = customer.cust_no

    # --- 1. Fetch Dynamic Context ---
    savings_choices = list(CustomerAccountsSetup.objects.filter(
        account_type__in=['share_capital', 'fixed_deposit', 'savings_deposit', 'junior_account'],
        is_active=True
    ).values('account_type', 'account_name').order_by('account_code'))

    def get_loan_labels(cust):
        if not cust: return []
        qs = LoanHistory.objects.filter(customer=cust).select_related('loan_type').order_by('-loan_date')
        return [
            {'id': loan.loan_no, 'name': f"{loan.loan_no} - {loan.loan_type.account_name if loan.loan_type else 'Loan'}"}
            for loan in qs
        ]

    loan_choices = get_loan_labels(customer)
    context = {"customer": customer, "savings_choices": savings_choices, "loan_choices": loan_choices}

    if request.method == "GET":
        tform = TransactionForm(initial=initial_data)
        savings_formset = SavingsPaymentFormSet(prefix="savings")
        loan_formset = LoanPaymentFormSet(prefix="loan", form_kwargs={'loan_choices': loan_choices})
        context.update({"form": tform, "savings_formset": savings_formset, "loan_formset": loan_formset})
        return render(request, template_name, context)

    # --- 3. POST Request Processing ---
    tform = TransactionForm(request.POST)
    posted_cust_no = request.POST.get('cust_no')
    
    if posted_cust_no:
        try:
            customer = Customer.objects.get(cust_no=posted_cust_no)
            loan_choices = get_loan_labels(customer)
            context.update({"customer": customer, "loan_choices": loan_choices})
        except Customer.DoesNotExist:
            customer = None

    savings_formset = SavingsPaymentFormSet(request.POST, prefix="savings")
    loan_formset = LoanPaymentFormSet(request.POST, prefix="loan", form_kwargs={'loan_choices': loan_choices})

    action = "post" if "post" in request.POST else "preview" if "preview" in request.POST else None

    if not tform.is_valid() or not savings_formset.is_valid() or not loan_formset.is_valid():
        messages.error(request, "Please fix form errors.")
        context.update({"form": tform, "savings_formset": savings_formset, "loan_formset": loan_formset})
        return render(request, template_name, context)

    # Data Extraction
    cust_no = tform.cleaned_data["cust_no"]
    date = tform.cleaned_data["date"]
    amount_paid = Decimal(tform.cleaned_data["amount_paid"] or 0)
    desc = tform.cleaned_data["description"]
    
    total_split = Decimal("0")
    savings_entries = []
    for sf in savings_formset:
        acc = sf.cleaned_data.get("account_type")
        amt = sf.cleaned_data.get("amount")
        if amt and amt > 0:
            savings_entries.append((acc, Decimal(amt)))
            total_split += amt

    loan_entries = []
    for lf in loan_formset:
        l_no = lf.cleaned_data.get("loan_no")
        amt = lf.cleaned_data.get("amount")
        if amt and amt > 0:
            loan_entries.append((l_no, Decimal(amt)))
            total_split += amt

    if total_split.quantize(Decimal("0.01")) != amount_paid.quantize(Decimal("0.01")):
        messages.error(request, f"Total Split (KES {total_split:,.2f}) must equal Amount Paid (KES {amount_paid:,.2f})")
        context.update({"form": tform, "savings_formset": savings_formset, "loan_formset": loan_formset})
        return render(request, template_name, context)

    if action == "preview":
        receipt_lines = [f"Receipt Preview ",f"Name: {customer.full_name}",f"Date: {date}", f"Desc: {desc}",  f"Total: {amount_paid:,.2f}", "---Split---"]
        for acc, amt in savings_entries: receipt_lines.append(f"Savings: {acc} - {amt:,.2f}")
        for lno, amt in loan_entries: receipt_lines.append(f"Loan: {lno} - {amt:,.2f}")
        context.update({"form": tform, "savings_formset": savings_formset, "loan_formset": loan_formset, "preview_text": "\n".join(receipt_lines)})
        return render(request, template_name, context)

    # --- 4. Atomic Persistence ---
    username = request.user.username if request.user.is_authenticated else "system"
    sms_queue = []

    try:
        with transaction.atomic():
            # A. Process Savings
            for acc_type, amt in savings_entries:
                ref = make_tr_ref(str(acc_type)) # FIXED: Convert object to string
                SavingsTransaction.objects.create(
                    cust_no=cust_no, tr_date=date, tr_ref=ref, tr_desc=desc,
                    debit_amount=0, credit_amount=amt, created_by=username, saving_type=acc_type
                )
                
                bal_data = SavingsTransaction.objects.filter(cust_no=cust_no, saving_type=acc_type).aggregate(
                    total=models.Sum(models.F('credit_amount') - models.F('debit_amount'))
                )
                bal = bal_data['total'] or 0
                
                # FIXED: Use str(acc_type).title()
                sms_queue.append(f"Received KES {amt:,.2f} for {str(acc_type).replace('_',' ').title()}. New Bal: {bal:,.2f}")

            # B. Process Loans
            for l_no, amt in loan_entries:
                loan_obj = LoanHistory.objects.select_related('loan_type').get(loan_no=l_no, customer=customer)
                
                # FIXED: Use the actual string code/name for the reference generator
                ref = make_tr_ref(str(loan_obj.loan_type.account_type)) 
                
                LoanTransaction.objects.create(
                    cust_no=cust_no,
                    loan_id=loan_obj.id, 
                    loan_no=l_no,
                    loan_type=loan_obj.loan_type, # This is now the FK object
                    tr_date=date, 
                    tr_ref=ref,
                    tr_desc=desc, 
                    debit_amount=0, 
                    credit_amount=amt, 
                    created_by=username
                )

                bal_data = LoanTransaction.objects.filter(loan_id=loan_obj.id).aggregate(
                    total=models.Sum(models.F('debit_amount') - models.F('credit_amount'))
                )
                bal = bal_data['total'] or 0

                sms_queue.append(f"Received KES {amt:,.2f} for Loan {l_no}. Outstanding: {bal:,.2f}")

        # --- 5. Post-Commit Communications ---
        if customer and customer.phone:
            combined_msg = f"Dear {customer.first_name}, " + " ".join(sms_queue)
            # send_sms_local(customer.phone, combined_msg, cust_no, username)

        messages.success(request, f"Transaction for {customer.full_name} posted successfully!")
        return redirect("transactions:post_transaction_empty")

    except Exception as e:
        messages.error(request, f"Critical System Error: {str(e)}")
        context.update({"form": tform, "savings_formset": savings_formset, "loan_formset": loan_formset})
        return render(request, template_name, context)
from datetime import datetime    
# The expected Mpesa time format
MPESA_TIME_FORMAT = "%Y%m%d%H%M%S"

@csrf_exempt
def mpesa_notification(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body.decode("utf-8"))
            payload = data.get("payload", {})

            # 1. Get the time string from the payload
            trans_time_str = payload.get("TransTime", "")
            
            # 2. Convert the string to a datetime object
            if trans_time_str:
                # Use strptime to parse the string into a datetime object
                trans_time_dt = datetime.datetime.strptime(trans_time_str, MPESA_TIME_FORMAT)
            else:
                trans_time_dt = None
            
            # 3. Handle trans_amount conversion (Mpesa sends it as a string)
            try:
                trans_amount_str = payload.get("TransAmount", "0")
                # Remove ".00" and convert to float/Decimal if necessary, 
                # or just float() if your model field is a FloatField/DecimalField
                trans_amount = float(trans_amount_str) 
            except ValueError:
                trans_amount = 0.0 # Default to zero on parsing failure

            notif = MpesaNotification.objects.create(
                transaction_type = payload.get("TransactionType", ""),
                trans_id = payload.get("TransID", ""),
                # Use the converted datetime object here
                trans_time = trans_time_dt, 
                trans_amount = trans_amount,
                business_shortcode = payload.get("BusinessShortCode", ""),
                bill_ref_number = payload.get("BillRefNumber", ""),
                invoice_number = payload.get("InvoiceNumber", ""),
                org_account_balance = payload.get("OrgAccountBalance", ""),
                third_party_trans_id = payload.get("ThirdPartyTransID", ""),
                msisdn = payload.get("MSISDN", ""),
                first_name = payload.get("FirstName", ""),
            )

            return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted successfully"})
        
        except Exception as e:
            # You might want to log the error here
            return JsonResponse({"ResultCode": 1, "ResultDesc": f"Error: {str(e)}"})
    else:
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Invalid method"})
    

@login_required
def unposted_notifications(request):
    # Show unposted notifications (limit 500, newest first)
    notifications = MpesaNotification.objects.filter(posted=False).order_by("-id")[:500]
    return render(request, "transactions/unposted_notifications.html", {"notifications": notifications})

@login_required
def posted_notifications(request):
    query = request.GET.get('q', '').strip()
    
    if query:
        # 1. Calculate the date 12 months ago from today
        twelve_months_ago = timezone.now() - relativedelta(months=12)
        
        # 2. Filter by search terms AND date range
        # Note: we filter on mpesa_notification's fields or the local customer_no
        notifications = (
            PostedMpesaNotification.objects
            .select_related("mpesa_notification")
            .filter(
                (Q(mpesa_notification__bill_ref_number__icontains=query) | 
                 Q(customer_no__icontains=query)),
                posted_at__gte=twelve_months_ago
            )
            .order_by("-posted_at")
        )
    else:
        # 3. Default view: Latest 100
        notifications = (
            PostedMpesaNotification.objects
            .select_related("mpesa_notification")
            .order_by("-id")[:100]
        )

    return render(
        request,
        "transactions/posted_notifications.html",
        {
            "notifications": notifications,
            "query": query
        }
    )

@login_required
def update_notification(request, pk):
    notif = get_object_or_404(MpesaNotification, pk=pk, posted=False)
    if request.method == "POST":
        billref = request.POST.get("BillRefNumber")
        notif.bill_ref_number = billref
        notif.save()
        return redirect("transactions:unposted_notifications")
    return render(request, "transactions/edit_notification.html", {"notification": notif})

# List accounts
@login_required
def account_list(request):
 accounts = CustomerAccountsSetup.objects.all().order_by('account_code')
 return render(request, 'transactions/account_list.html', {'accounts': accounts})

# Add new account
@login_required
def account_create(request):
    if request.method == 'POST':
        form = CustomerAccountsSetupForm(request.POST)
        if form.is_valid():
            # 1. Commit everything else except the database write 
            instance = form.save(commit=False)
            
            # 2. Extract the raw JavaScript-generated text securely from POST payload
            raw_account_type = request.POST.get('account_type', '').strip()
            
            # 3. Manually map it directly to the model attribute (bypasses Form Validation)
            if raw_account_type:
                instance.account_type = raw_account_type
                
            # 4. Finalize commit safely
            instance.save()
            return redirect('transactions:account_list')
    else:
        form = CustomerAccountsSetupForm()
        
    return render(request, 'transactions/account_form.html', {'form': form})

# Update account
@login_required
def account_update(request, pk):
  account = get_object_or_404(CustomerAccountsSetup, pk=pk)
  if request.method == "POST":
    form = CustomerAccountsSetupForm(request.POST, instance=account)
    if form.is_valid():
      form.save()
      messages.success(request, "Account updated successfully.")
      return redirect('transactions:account_list')
  else:
    form = CustomerAccountsSetupForm(instance=account)
  return render(request, 'transactions/account_form.html', {'form': form, 'title': 'Update Account'})

# Delete account
@login_required
def account_delete(request, pk):
  account = get_object_or_404(CustomerAccountsSetup, pk=pk)
  if request.method == "POST":
    account.delete()
    messages.success(request, "Account deleted successfully.")
    return redirect('transactions:account_list')
  return render(request, 'transactions/account_confirm_delete.html', {'account': account})

def search_customer_api(request):
    """AJAX Live Search for customers by Number or Name."""
    query = request.GET.get('q', '')
    if len(query) < 2:
        return JsonResponse({'results': []})
    
    customers = Customer.objects.filter(
        models.Q(cust_no__icontains=query) | models.Q(full_name__icontains=query)
    )[:10]  # Limit for performance
    
    results = [{'id': c.cust_no, 'text': f"{c.cust_no} - {c.full_name}"} for c in customers]
    return JsonResponse({'results': results})

@transaction.atomic
def inter_account_transfer(request):
    if request.method == "POST":
        try:
            # 1. Capture Data from Form
            from_cust = request.POST.get('from_cust_no')
            from_acc_type = request.POST.get('from_account')
            to_cust = request.POST.get('to_cust_no')
            to_acc_id = request.POST.get('to_account') # This is either a saving_type or loan_no
            amount = float(request.POST.get('amount', 0))
            user_ref = request.POST.get('reference', '')

            # 2. Basic Validation
            if amount <= 0:
                return JsonResponse({'status': 'error', 'message': 'Amount must be greater than zero.'})
            
            if not from_cust or not to_cust:
                return JsonResponse({'status': 'error', 'message': 'Please select both members.'})

            # 3. Generate Automated Reference: TRF + YYYYMMDDHHMMSS
            transfer_ref = make_tr_ref('transfer')
            final_desc = f"{user_ref} | Ref: {transfer_ref}".strip(" | ")

            # 4. PREVENT OVERDRAW: Calculate current balance of Source Account
            # We use aggregate to get the sum of credits minus debits
            balance_agg = SavingsTransaction.objects.filter(
                cust_no=from_cust, 
                saving_type=from_acc_type
            ).aggregate(
                total_balance=(Sum('credit_amount') - Sum('debit_amount'))
            )
            
            current_balance = balance_agg['total_balance'] or 0

            if current_balance < amount:
                return JsonResponse({
                    'status': 'error', 
                    'message': f'Insufficient funds. Current Balance: {current_balance:,.2f}'
                })

            # 5. EXECUTE DEBIT (Always Savings)
            SavingsTransaction.objects.create(
                cust_no=from_cust,
                saving_type=from_acc_type,
                tr_date=timezone.now(),
                tr_ref=transfer_ref,
                tr_desc=f"Transfer to Member {to_cust}: {final_desc}",
                debit_amount=amount,
                created_by=request.user.username if request.user.is_authenticated else "System"
            )

            # 6. EXECUTE CREDIT (Check if Loan or Savings)
            # Fetch LoanHistory to fix the "not-null constraint loan_id"
            loan_record = LoanHistory.objects.filter(loan_no=to_acc_id, customer_id=to_cust).first()

            if loan_record:
                # It's a Loan Account
                LoanTransaction.objects.create(
                    cust_no=to_cust,
                    loan_id=loan_record.id,  # <--- FIX: Passing the actual ID
                    loan_no=loan_record.loan_no,
                    loan_type=loan_record.loan_type,
                    tr_date=timezone.now(),
                    tr_ref=transfer_ref,
                    tr_desc=f"Transfer from Member {from_cust}: {final_desc}",
                    credit_amount=amount,
                    created_by=request.user.username if request.user.is_authenticated else "System"
                )
            else:
                # It's a Savings Account
                SavingsTransaction.objects.create(
                    cust_no=to_cust,
                    saving_type=to_acc_id,
                    tr_date=timezone.now(),
                    tr_ref=transfer_ref,
                    tr_desc=f"Transfer from Member {from_cust}: {final_desc}",
                    credit_amount=amount,
                    created_by=request.user.username if request.user.is_authenticated else "System"
                )

            return JsonResponse({
                'status': 'success', 
                'message': f'Transfer Successful! Reference: {transfer_ref}'
            })

        except Exception as e:
            return JsonResponse({'status': 'error', 'message': f'System Error: {str(e)}'})

    return render(request, 'transactions/transfer.html')

import datetime
from datetime import datetime, date, timedelta
from django.db import transaction
from django.db.models import Sum
from calendar import monthrange
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
import polars as pl
import calendar

from .utils import normalize_cust_no, normalize_cust_no_list, ensure_cust_no_dict


@login_required
def calculate_interest(request):
    """
    Polars-accelerated dividend / interest calculation engine.

    Supports two allocation strategies:
      - fixed_pool:  distribute a fixed KES amount proportionally
      - percentage:  apply a yield percentage to each weighted balance
    """
    if request.method == "POST":
        saving_type = request.POST.get('saving_type')
        calc_type = request.POST.get('calculation_type')
        allocation_type = request.POST.get('allocation_type')

        wht_rate = float(request.POST.get('wht_rate', '15.00'))
        global_processing_fee = float(request.POST.get('processing_fee', '100.00'))

        raw_date = request.POST.get('cut_off_date')
        if not raw_date:
            messages.error(request, "Cut-off date is required.")
            return redirect(request.path)

        cut_off_date = datetime.strptime(raw_date, '%Y-%m-%d').date()
        year_start = date(cut_off_date.year, 1, 1)
        prev_year_close = year_start - timedelta(days=1)

        # 1. Read entire relevant ledger into memory
        ledger_qs = SavingsTransaction.objects.filter(
            saving_type=saving_type,
            tr_date__date__lte=cut_off_date
        ).values('cust_no', 'tr_date', 'credit_amount', 'debit_amount')

        if not ledger_qs.exists():
            messages.error(request, f"No transactions found for '{saving_type}' up to the cut-off date.")
            return redirect(request.path)

        # 2. Build Polars DataFrame
        ledger_data = list(ledger_qs)
        for row in ledger_data:
            row['cust_no'] = normalize_cust_no(row['cust_no'])

        df = pl.DataFrame(ledger_data)
        df = df.with_columns([
            pl.col('cust_no').cast(pl.Utf8),
            pl.col('tr_date').cast(pl.Date),
            (pl.col('credit_amount').cast(pl.Float64) - pl.col('debit_amount').cast(pl.Float64)).alias('net_amount')
        ])

        unique_customers = df.select('cust_no').unique()

        # 3. Balance Brought Forward (Month 0)
        df_opening = df.filter(pl.col('tr_date') <= prev_year_close)
        if not df_opening.is_empty():
            df_op_bal = df_opening.group_by('cust_no').agg(
                pl.col('net_amount').sum().alias('change_amount')
            ).with_columns(
                pl.lit(0, dtype=pl.Int8).alias('month')
            ).select(['cust_no', 'month', 'change_amount'])
        else:
            df_op_bal = pl.DataFrame(schema={'cust_no': pl.Utf8, 'month': pl.Int8, 'change_amount': pl.Float64})

        # 4. Current Year Variations (Months 1..N)
        df_current_year = df.filter((pl.col('tr_date') >= year_start) & (pl.col('tr_date') <= cut_off_date))
        if not df_current_year.is_empty():
            df_monthly_changes = df_current_year.with_columns(
                pl.col('tr_date').dt.month().cast(pl.Int8).alias('month')
            ).group_by(['cust_no', 'month']).agg(
                pl.col('net_amount').sum().alias('change_amount')
            ).select(['cust_no', 'month', 'change_amount'])
        else:
            df_monthly_changes = pl.DataFrame(schema={'cust_no': pl.Utf8, 'month': pl.Int8, 'change_amount': pl.Float64})

        # 5. Unified Matrix Grid
        active_periods = pl.DataFrame({
            'month': [0] + list(range(1, cut_off_date.month + 1))
        }, schema={'month': pl.Int8})

        grid_df = unique_customers.join(active_periods, how='cross')
        all_changes = pl.concat([df_op_bal, df_monthly_changes])

        grid_df = grid_df.join(all_changes, on=['cust_no', 'month'], how='left').with_columns(
            pl.col('change_amount').fill_null(0.0)
        )

        # 6. Weighting Rules
        if calc_type == 'prorata':
            grid_df = grid_df.with_columns(
                pl.when(pl.col('month') == 0)
                .then(1.0)
                .otherwise((13.0 - pl.col('month')) / 12.0)
                .alias('weight')
            )
        else:
            grid_df = grid_df.with_columns(pl.lit(1.0).alias('weight'))

        grid_df = grid_df.with_columns(
            (pl.col('change_amount') * pl.col('weight')).alias('weighted_change')
        )

        # 7. Customer-Level Summary
        cust_summary = grid_df.group_by('cust_no').agg(
            pl.col('weighted_change').sum().alias('weighted_base')
        ).filter(pl.col('weighted_base') > 0)

        if cust_summary.is_empty():
            messages.warning(request, "No active accounts found with a positive weighted balance.")
            return redirect(request.path)

        # 8. Allocate Payout Pools
        if allocation_type == 'fixed_pool':
            amount_to_share = float(request.POST.get('amount_to_share', '0.00'))
            total_system_weighted_base = cust_summary.select(pl.col('weighted_base').sum()).item()

            cust_summary = cust_summary.with_columns(
                (pl.col('weighted_base') / total_system_weighted_base * amount_to_share).alias('total_gross_interest')
            )
        else:
            div_pct = float(request.POST.get('dividend_percentage', '7.00'))
            amount_to_share = 0.0  # will be computed from totals
            cust_summary = cust_summary.with_columns(
                (pl.col('weighted_base') * (div_pct / 100.0)).alias('total_gross_interest')
            )

        cust_summary = cust_summary.with_columns(
            (pl.col('total_gross_interest') * (wht_rate / 100.0)).alias('total_withholding_tax')
        ).with_columns(
            pl.min_horizontal(
                pl.lit(global_processing_fee),
                pl.col('total_gross_interest') - pl.col('total_withholding_tax')
            ).alias('total_fee')
        ).with_columns(
            (pl.col('total_gross_interest') - pl.col('total_withholding_tax') - pl.col('total_fee')).alias('total_net_payout')
        )

        # ── Normalize cust_no for DB operations ──
        cust_nos = cust_summary.select('cust_no').to_series().to_list()
        cust_nos = normalize_cust_no_list(cust_nos)

        customer_names = dict(Customer.objects.filter(cust_no__in=cust_nos).values_list('cust_no', 'full_name'))
        customer_names = ensure_cust_no_dict(customer_names)

        # 9. Split totals back to matrix rows
        detailed_grid = grid_df.join(cust_summary, on='cust_no', how='inner')
        detailed_grid = detailed_grid.with_columns(
            (pl.col('weighted_change') / pl.col('weighted_base')).alias('row_proportion')
        ).with_columns([
            (pl.col('total_gross_interest') * pl.col('row_proportion')).alias('row_gross'),
            (pl.col('total_withholding_tax') * pl.col('row_proportion')).alias('row_wht'),
            (pl.col('total_net_payout') * pl.col('row_proportion')).alias('row_net'),
            (pl.col('total_fee') / float(cut_off_date.month + 1)).alias('row_fee')
        ])

        # 10. Atomic Database Commit
        with transaction.atomic():
            actual_total_shared = cust_summary.select(pl.col('total_gross_interest').sum()).item()

            batch = DividendBatch.objects.create(
                batch_no=make_tr_ref('batch'),
                saving_type=saving_type,
                amount_to_share=Decimal(str(actual_total_shared)),
                wht_rate=Decimal(str(wht_rate)),
                processing_fee=Decimal(str(global_processing_fee)),
                cut_off_date=cut_off_date,
                created_by=request.user.username
            )

            detail_instances = []
            summary_records = cust_summary.to_dicts()
            for row in summary_records:
                c_no = normalize_cust_no(row['cust_no'])
                detail_instances.append(DividendDetail(
                    batch=batch,
                    cust_no=c_no,
                    member_name=customer_names.get(c_no, f"Member {c_no}"),
                    weighted_avg_balance=Decimal(str(row['weighted_base'])),
                    gross_interest=Decimal(str(row['total_gross_interest'])),
                    withholding_tax=Decimal(str(row['total_withholding_tax'])),
                    processing_fee=Decimal(str(row['total_fee'])),
                    net_payout=Decimal(str(row['total_net_payout']))
                ))

            DividendDetail.objects.bulk_create(detail_instances)

            saved_details = DividendDetail.objects.filter(batch=batch).values('id', 'cust_no')
            detail_lookup_map = {normalize_cust_no(sd['cust_no']): sd['id'] for sd in saved_details}

            slip_instances = []
            detailed_records = detailed_grid.to_dicts()

            for row in detailed_records:
                c_no = normalize_cust_no(row['cust_no'])
                detail_id = detail_lookup_map.get(c_no)
                if not detail_id:
                    continue

                m_idx = int(row['month'])
                if m_idx == 0:
                    period_date = prev_year_close
                else:
                    try:
                        last_day = calendar.monthrange(cut_off_date.year, m_idx)[1]
                        period_date = date(cut_off_date.year, m_idx, last_day)
                        if period_date > cut_off_date:
                            period_date = cut_off_date
                    except Exception:
                        period_date = cut_off_date

                slip_instances.append(DividendSlipItem(
                    detail_id=detail_id,
                    period_date=period_date,
                    savings_amount=Decimal(str(row['change_amount'])),
                    ratio=Decimal(str(row['weight'])).quantize(Decimal('0.0001')),
                    weighted_balance=Decimal(str(row['weighted_change'])),
                    gross_interest=Decimal(str(row['row_gross'])),
                    wht_amount=Decimal(str(row['row_wht'])),
                    fee_amount=Decimal(str(row['row_fee'])),
                    net_interest=Decimal(str(row['row_net']))
                ))

            if slip_instances:
                DividendSlipItem.objects.bulk_create(slip_instances)

            batch.total_gross = Decimal(str(cust_summary.select(pl.col('total_gross_interest').sum()).item()))
            batch.total_tax = Decimal(str(cust_summary.select(pl.col('total_withholding_tax').sum()).item()))
            batch.total_fees = Decimal(str(cust_summary.select(pl.col('total_fee').sum()).item()))
            batch.total_net_payout = Decimal(str(cust_summary.select(pl.col('total_net_payout').sum()).item()))
            batch.save()

            return redirect('transactions:interest_review', batch_id=batch.id)

    # GET — render form with dynamic product types
    savings_accounts = CustomerAccountsSetup.objects.filter(
        is_loan_account=False,
        is_active=True
    ).order_by('account_name')

    return render(request, 'transactions/interest_form.html', {
        'savings_accounts': savings_accounts
    })
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from django.utils import timezone
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger


@login_required
def post_interest_batch(request, batch_id):
    """
    Post a calculated dividend / interest batch via dividend_service.

    Maker-checker: officer creates the batch, manager posts here.
    """
    from transactions.dividend_service import (
        post_dividend_batch, DividendPostingError,
    )

    batch = get_object_or_404(DividendBatch, id=batch_id, is_posted=False)

    if not batch.details.filter(is_posted=False).exists():
        messages.info(request, "No pending items to post in this batch.")
        return redirect("transactions:interest_history")

    user = request.user.username if request.user.is_authenticated else "system"

    try:
        summary = post_dividend_batch(batch.id, posted_by=user)
    except DividendPostingError as exc:
        messages.error(request, f"Posting aborted: {exc}")
        return redirect("transactions:interest_review", batch_id=batch.id)

    messages.success(
        request,
        f"Posted dividends to {summary['members']} members. "
        f"Gross KES {summary['total_gross']:,.2f}, "
        f"net KES {summary['total_net']:,.2f}, "
        f"WHT KES {summary['total_tax']:,.2f}, "
        f"fees KES {summary['total_fee']:,.2f}."
    )
    return redirect("transactions:interest_history")


@login_required
def interest_review(request, batch_id):
    batch = get_object_or_404(DividendBatch, id=batch_id)

    total_weighted_avg = batch.details.aggregate(
        total=Sum('weighted_avg_balance')
    )['total'] or Decimal('1.00')

    effective_rate = (batch.amount_to_share / total_weighted_avg) * 100

    # Paginate with status filter
    details_queryset = batch.details.all().order_by('cust_no')
    status_filter = request.GET.get('view_status', 'unposted')
    if status_filter == 'unposted':
        details_queryset = details_queryset.filter(is_posted=False)
    elif status_filter == 'posted':
        details_queryset = details_queryset.filter(is_posted=True)

    paginator = Paginator(details_queryset, 50)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return render(request, 'transactions/interest_review.html', {
        'batch': batch,
        'effective_rate': round(effective_rate, 2),
        'total_weighted_avg': total_weighted_avg,
        'page_obj': page_obj,
        'status_filter': status_filter,
    })


@login_required
def delete_interest_batch(request, batch_id):
    """Delete a draft batch if the user spots an error during review."""
    if request.method == "POST":
        batch = get_object_or_404(DividendBatch, id=batch_id, is_posted=False)
        batch.delete()
        messages.warning(request, "Interest calculation draft discarded.")
    return redirect('transactions:interest_history')


@login_required
def interest_history(request):
    """Lists all interest calculation batches with pagination."""
    batches = DividendBatch.objects.all().order_by('-created_at')

    status_filter = request.GET.get('status')
    if status_filter == 'posted':
        batches = batches.filter(is_posted=True)
    elif status_filter == 'draft':
        batches = batches.filter(is_posted=False)

    paginator = Paginator(batches, 15)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return render(request, 'transactions/interest_history.html', {
        'page_obj': page_obj,
        'status_filter': status_filter,
    })
import json
from django.http import JsonResponse
from django.views.decorators.http import require_POST

import json
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.db import transaction
from .models import DividendDetail, SavingsTransaction, LoanTransaction
# Add your Loan model import here if you need to fetch loan_id by loan_no
# from .models import Loan 

@require_POST
def post_interest_individual(request, detail_id):
    try:
        data = json.loads(request.body)
        target_account = data.get('target_account')
        
        if not target_account:
            return JsonResponse({'success': False, 'message': 'Target account is required.'})

        # 1. Use an atomic block. If any step fails, the entire transaction rolls back.
        with transaction.atomic():
            # 2. select_for_update() locks the row until the transaction finishes, 
            #    preventing double-posting from double-clicks.
            detail = DividendDetail.objects.select_for_update().get(id=detail_id)
            
            if detail.is_posted:
                return JsonResponse({'success': False, 'message': 'Record already posted.'})

            # Prepare common variables
            now = timezone.now()
            # Create a clean, traceable reference number
            tx_reference = make_tr_ref('dividend') # Assuming this is your helper function
            #tx_reference = f"INT-{detail.batch.id}-{detail.id}" 
            description = f"Dividends Payout - Batch {detail.batch.batch_no}"
            
            # Assuming your detail model has a `net_payout` field
            payout_amount = detail.net_payout 
            
            # Determine user for auditing
            user_name = request.user.username if request.user.is_authenticated else 'system'

            # 3. Route to the correct Transaction Table
            # Checking if the target account is a loan (e.g., starts with 'LN')
            if str(target_account).upper().startswith('LN'):
                
                # OPTIONAL: Fetch the actual loan record to get its ID and Type if required by your schema
                # loan_record = Loan.objects.get(loan_no=target_account)
                loan_record = LoanHistory.objects.filter(loan_no=target_account, customer_id=detail.cust_no).first()
                
                LoanTransaction.objects.create(
                    cust_no=detail.cust_no,
                    loan_id=loan_record.id,
                    loan_no=target_account,
                    loan_type=loan_record.loan_type,
                    tr_date=now,
                    tr_ref=tx_reference,
                    tr_desc=description,
                    credit_amount=payout_amount, # Crediting a loan usually reduces the owed balance
                    debit_amount=0,
                    created_by=request.user.username
                )
            else:
                # It's a savings/deposit account (e.g., 'fixed_deposit', 'savings_deposit')
                SavingsTransaction.objects.create(
                    cust_no=detail.cust_no,
                    saving_type=target_account,
                    tr_date=now,
                    tr_ref=tx_reference,
                    tr_desc=description,
                    credit_amount=payout_amount, # Crediting savings increases the balance
                    debit_amount=0,
                    created_by=request.user.username
                )

            # 4. Update the Member's Balance Model
            # Note: If your system calculates balances dynamically via Sum() on the 
            # transaction tables, you can skip this step. If you have a static balance table:
            # 
            # balance_record = CustomerBalance.objects.get(cust_no=detail.cust_no, account=target_account)
            # balance_record.balance += payout_amount
            # balance_record.save()

            # 5. Mark the detail record as posted
            detail.is_posted = True
            detail.save()
            
            # Optional: Check if batch is fully posted and update batch status
            batch = detail.batch
            if not batch.details.filter(is_posted=False).exists():
                batch.is_posted = True
                batch.save()

        return JsonResponse({'success': True, 'message': 'Posted successfully'})
        
    except DividendDetail.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Detail record not found.'})
    except Exception as e:
        # Catch-all for unexpected errors (e.g., database constraint failures)
        return JsonResponse({'success': False, 'message': str(e)})
    
from django.template.loader import render_to_string
from django.http import HttpResponse
# import pdf_library if using one, e.g., from xhtml2pdf import pisa 

def search_dividend_slip(request):
    cust_no = request.GET.get('cust_no')
    results = []
    if cust_no:
        # Only show slips from batches that are actually posted
        results = DividendDetail.objects.filter(
            cust_no=cust_no, 
            #batch__is_posted=True
        ).select_related('batch').order_by('-batch__created_at')
    
    return render(request, 'transactions/slip_search.html', {
        'results': results,
        'cust_no': cust_no
    })

def preview_dividend_slip(request, detail_id):
    detail = get_object_or_404(DividendDetail, id=detail_id)
    # Fetch monthly breakdown
    slip_items = detail.slip_items.all().order_by('period_date')
    
    # Calculate totals for the footer
    totals = slip_items.aggregate(
        total_savings=Sum('savings_amount'),
        total_weighted=Sum('weighted_balance'),
        total_gross=Sum('gross_interest'),
        total_tax=Sum('wht_amount'),
        total_fee=Sum('fee_amount'),
        total_net=Sum('net_interest')
    )

    return render(request, 'transactions/slip_preview.html', {
        'detail': detail,
        'items': slip_items,
        'totals': totals
    })

import io
import logging
from decimal import Decimal
from datetime import datetime
from django.shortcuts import get_object_or_404
from django.http import HttpResponse
from django.utils.timezone import now
from django.contrib.auth.decorators import login_required

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, 
    Spacer, Image as RLImage, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT

# Assuming your models are imported correctly
# from .models import DividendDetail, DividendSlipItem, CompanyInfo

logger = logging.getLogger(__name__)

@login_required
def download_dividend_slip_pdf(request, detail_id):
    """
    Fetches dividend data and triggers a PDF download using ReportLab logic.
    """
    detail = get_object_or_404(DividendDetail, id=detail_id)
    
    # 1. Initialize Buffer and Document
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, 
        leftMargin=30, rightMargin=30, 
        topMargin=30, bottomMargin=30
    )
    story = []
    styles = getSampleStyleSheet()

    # --- 2. COMPANY INFO & HEADER LOGIC ---
    company_name = "YOUR INSTITUTION"
    company_address = company_contact = company_location = ""
    company_logo_path = None

    try:
        from dashboard.models import CompanyInfo 
        company_info = CompanyInfo.objects.first()
        if company_info:
            company_name = company_info.company_name or company_name
            company_address = company_info.company_address or ""
            company_contact = company_info.company_contact or ""
            company_location = company_info.company_location or ""
            if company_info.company_logo and hasattr(company_info.company_logo, "path"):
                company_logo_path = company_info.company_logo.path
    except Exception as e:
        logger.error(f"Error fetching company info: {e}")

    # Custom Styles
    centered_header_style = ParagraphStyle(
        "CenteredHeader", parent=styles["Normal"], 
        alignment=TA_CENTER, fontSize=9, leading=11
    )

    # Header Construction
    if company_logo_path:
        try:
            img = RLImage(company_logo_path, width=2.0*inch, height=0.6*inch, kind='proportional')
            story.append(img)
        except: pass

    header_html = f"""
        <font size="14"><b>{company_name.upper()}</b></font><br/>
        {company_address}<br/>
        {company_contact} | {company_location}
    """
    story.append(Paragraph(header_html, centered_header_style))
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#2c3e50"), spaceAfter=10))
    
    story.append(Paragraph(f"<b>DIVIDEND/INTEREST ADVICE SLIP</b>", styles["Heading2"]))
    story.append(Spacer(1, 5))

    # --- 3. MEMBER DATA GRID ---
    mem_data = [
        [f"MEMBER NAME: {detail.member_name.upper()}", f"MEMBER NO: {detail.cust_no}"],
        [f"ACCOUNT TYPE: {detail.batch.get_saving_type_display()}", f"BATCH REF: {detail.batch.batch_no}"],
        [f"CUT-OFF DATE: {detail.batch.cut_off_date.strftime('%d-%b-%Y')}", f"PRINTED: {now().strftime('%d-%m-%Y %H:%M')}"]
    ]
    mem_table = Table(mem_data, colWidths=[4.2*inch, 3.0*inch])
    mem_table.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(mem_table)
    story.append(Spacer(1, 15))

    # --- 4. BREAKDOWN TABLE (THE "SLIP ITEMS") ---
    # Header Row
    table_data = [["Period", "Savings", "Ratio", "Weighted Bal", "Gross Int", "WHT", "Fee", "Net Int"]]
    
    # Detail Rows from DividendSlipItem
    items = detail.slip_items.all().order_by('period_date')
    for item in items:
        table_data.append([
            item.period_date.strftime("%d-%m-%Y"),
            f"{item.savings_amount:,.2f}",
            f"{item.ratio:,.2f}",
            f"{item.weighted_balance:,.2f}",
            f"{item.gross_interest:,.2f}",
            f"{item.wht_amount:,.2f}",
            f"{item.fee_amount:,.2f}",
            f"{item.net_interest:,.2f}"
        ])

    # Table Widths (Total ~ 7.2 inches)
    col_widths = [0.9*inch, 1.0*inch, 0.5*inch, 1.1*inch, 0.9*inch, 0.8*inch, 0.8*inch, 1.2*inch]
    
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#34495e")),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('ALIGN', (1,0), (-1,-1), 'RIGHT'),  # All numeric columns
        ('ALIGN', (0,0), (0,-1), 'CENTER'), # Period column
        ('GRID', (0,0), (-1,-1), 0.25, colors.grey),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.whitesmoke]),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
    ]))
    story.append(t)

    # --- 5. SUMMARY SECTION ---
    story.append(Spacer(1, 20))
    summary_data = [
        ["", "TOTAL GROSS INTEREST:", f"KES {detail.gross_interest:,.2f}"],
        ["", f"WITHHOLDING TAX ({detail.batch.wht_rate}%):", f"- {detail.withholding_tax:,.2f}"],
        ["", "PROCESSING FEES:", f"- {detail.processing_fee:,.2f}"],
        ["", "NET PAYOUT AMOUNT:", f"KES {detail.net_payout:,.2f}"]
    ]
    
    summary_table = Table(summary_data, colWidths=[4.2*inch, 1.8*inch, 1.2*inch])
    summary_table.setStyle(TableStyle([
        ('FONTNAME', (1, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (1, 0), (-1, -1), 10),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('TEXTCOLOR', (2, 3), (2, 3), colors.darkgreen),
        ('LINEABOVE', (1, 3), (-1, 3), 1, colors.black),
    ]))
    story.append(summary_table)

    # --- 6. FOOTER ---
    story.append(Spacer(1, 40))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    footer_para = Paragraph(
        "This is a computer-generated advice slip. Official stamp/signature not required.<br/>"
        "Values based on daily weighted balances and prorated savings held during the period.", 
        centered_header_style
    )
    story.append(footer_para)

    # --- 7. BUILD AND RETURN ---
    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    filename = f"DividendSlip_{detail.cust_no}_{detail.batch.batch_no}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    return response
