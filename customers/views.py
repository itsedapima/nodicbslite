from accounts.decorators import min_role_required, role_required
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
import datetime

# Local Imports: Customers App
from .forms import (
    ChurchOfficialFormSet, EconomicActivityForm, GroupOfficialFormSet, 
    NextOfKinForm, OrganizationDetailsForm, PersonalDetailsForm, 
    CommunicationResidenceForm
)
from .models import (
    ChurchOfficial, Customer, CustomerEconomicActivity, CustomerStats, 
    GroupOfficial, NextOfKin
)
from .utils import update_customer_statistics
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.utils import timezone
# Make sure to import your models correctly based on your app structure
from administration.models import ChamaInfo
from sms.models import SMSLog
from .models import Customer, CustomerEconomicActivity 

# Local Imports: Loans & Transactions Apps
from loans.models import Guarantor, LoanHistory
from transactions.models import LoanTransaction, SavingsTransaction, CustomerAccountsSetup
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.db.models import Q
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.shortcuts import render
from django.db.models import Q
from django.http import JsonResponse
from .models import Customer

@login_required
def list_customers(request):
    """
    Unified view to handle listing, searching, and server/client-side pagination 
    for the Customer model ecosystem.
    """
    query = request.GET.get('q', '').strip()
    page_number = request.GET.get('page', 1)
    per_page = 10  # Set your desired items per page limit

    # 1. Base Queryset with Search Filters applied if query exists
    if query:
        queryset = Customer.objects.filter(
            Q(full_name__icontains=query) |
            Q(cust_no__icontains=query) |
            Q(national_id__icontains=query) |
            Q(phone__icontains=query)
        ).order_by('-id')
    else:
        queryset = Customer.objects.order_by('-id')

    # 2. Django Pagination Implementation
    paginator = Paginator(queryset, per_page)
    
    try:
        page_obj = paginator.get_page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # 3. Formulate JSON response IF requested via AJAX header
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
        customer_list = [
            {
                "id": c.id,
                "cust_no": c.cust_no,
                "full_name": c.full_name,
                "national_id": c.national_id,
                "phone": c.phone
            }
            for c in page_obj.object_list
        ]
        
        return JsonResponse({
            "results": customer_list,
            "has_next": page_obj.has_next(),
            "has_previous": page_obj.has_previous(),
            "current_page": page_obj.number,
            "total_pages": paginator.num_pages,
            "total_count": paginator.count
        })

    # 4. Standard HTML Fallback response
    context = {
        "page_obj": page_obj,
        "query": query,
    }
    return render(request, "customers/list_customers.html", context)
@login_required
def dashboard_stats(request):
    stats = CustomerStats.objects.get(id=1)
    return render(request, 'customers/stats.html', {'stats': stats})

def trigger_stats_update(request):
    update_customer_statistics()
    return redirect('customers:dashboard_stats')
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404

import logging

logger = logging.getLogger(__name__)

from .forms import (
    PersonalDetailsForm,
    CommunicationResidenceForm,
    EconomicActivityForm,
    CustomerForm,
)
from .models import Customer, CustomerEconomicActivity
from administration.models import ChamaInfo, CompanyBranch  # adjust paths if needed
from sms.models import SMSLog, EmailLog  # adjust paths if needed


import datetime

from django.db.models import Model

ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii"]


def _json_safe(cleaned):
    """
    Convert a form's cleaned_data into something the session can store.
    Model instances (FKs like `branch`) -> their pk. Dates -> isoformat.
    Fix for: 'Object of type CompanyBranch is not JSON serializable'.
    """
    out = {}
    for key, value in cleaned.items():
        if isinstance(value, Model):
            out[key] = value.pk
        elif isinstance(value, (datetime.date, datetime.datetime)):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def _build_welcome_body(customer, savings_accounts, company_name, paybill_no):
    """Compose the welcome message text shared by SMS and email."""
    account_no = str(customer.cust_no).zfill(5)
    lines = ""
    for index, acc in enumerate(savings_accounts):
        prefix = ROMAN[index] if index < len(ROMAN) else str(index + 1)
        lines += f"\n   {prefix}) {acc.account_name} {account_no}{acc.acc_initials}"
    return (
        f"Dear {customer.full_name.upper()}, Welcome to {company_name}. "
        f"Your Account No. is {account_no}.\n"
        f"To pay via M-Pesa use Paybill: {paybill_no}, A/c:{lines}\n"
        f"Thank you."
    )


def _queue_welcome_messages(request, customer):
    """
    Write a pending SMSLog and (if an email exists) a pending EmailLog.
    Wrapped so any logging failure never blocks customer creation.
    """
    try:
        company = ChamaInfo.objects.first()
        company_name = company.company_name.upper() if company else "OUR COMPANY LTD"
        paybill_no = "866349"  # replace with company setting if available

        # CustomerAccountsSetup lives in the loans/accounts app, NOT customers.
        # Adjust this import to wherever the model actually is.
        from loans.models import CustomerAccountsSetup
        savings_accounts = CustomerAccountsSetup.objects.filter(
            is_loan_account=False, access_on_channels=True, is_active=True
        ).order_by("id")

        body = _build_welcome_body(customer, savings_accounts, company_name, paybill_no)
        creator = request.user.username if request.user.is_authenticated else "System"

        SMSLog.objects.create(
            phone=customer.phone,
            message=body,
            status="pending",
            created_by=creator,
        )

        if customer.reg_email:
            EmailLog.objects.create(
                recipient_to=customer.reg_email,
                subject=f"Welcome to {company_name}",
                message_body=body,
                is_html=False,
                status="pending",
                created_by=creator,
            )
    except Exception as e:
        # Don't crash registration, but DO make the failure visible so a
        # misconfigured import or missing field is not swallowed silently.
        logger.exception("Failed to queue welcome messages for customer %s", customer.pk)
        messages.warning(
            request,
            f"Customer saved, but welcome SMS/email could not be queued: {e}",
        )


@login_required
def add_customer_stepper(request, step=1):
    """Multi-step individual customer registration. Session-backed between steps."""
    if request.method == "POST":
        current_step = int(request.POST.get("step", 1))
        session_data = request.session.get("add_customer_data", {})

        if current_step == 1:
            form = PersonalDetailsForm(request.POST)
            if form.is_valid():
                session_data.update(_json_safe(form.cleaned_data))
                request.session["add_customer_data"] = session_data
                return redirect("customers:add_customer_stepper", step=2)
            return render(request, "customers/add_customer.html", {"form": form, "step": 1})

        elif current_step == 2:
            form = CommunicationResidenceForm(request.POST)
            if form.is_valid():
                cleaned = form.cleaned_data.copy()
                # Booleans are JSON-safe but let's be explicit
                cleaned["default_notifications_setting"] = bool(
                    cleaned.get("default_notifications_setting")
                )
                session_data.update(cleaned)
                request.session["add_customer_data"] = session_data
                return redirect("customers:add_customer_stepper", step=3)
            return render(request, "customers/add_customer.html", {
                "form": form,
                "step": 2,
            })

        elif current_step == 3:
            form = EconomicActivityForm(request.POST)
            if form.is_valid():
                data = request.session.get("add_customer_data", {})
                data.update(_json_safe(form.cleaned_data))

                if Customer.objects.filter(national_id=data["national_id"]).exists():
                    form.add_error(None, "A customer with this National ID already exists.")
                    return render(request, "customers/add_customer.html", {"form": form, "step": 3})

                customer = Customer.objects.create(
                    # Step 1 — Personal
                    full_name=data["full_name"],
                    first_name=data["first_name"],
                    middle_name=data.get("middle_name", ""),
                    last_name=data["last_name"],
                    gender=data["gender"],
                    marital_status=data["marital_status"],
                    dob=data["dob"],
                    branch_id=data.get("branch"),
                    national_id=data["national_id"],
                    kra_pin=data["kra_pin"],
                    # Step 2 — Communication & Residence
                    phone=data["phone"],
                    reg_email=data["reg_email"],
                    postal_address=data["postal_address"],
                    postal_code=data["postal_code"],
                    town=data["town"],
                    home_address=data["home_address"],
                    default_notifications_setting=data.get(
                        "default_notifications_setting", False
                    ),
                    # Step 3 — Flags
                    reg_fee_is_paid=data.get("reg_fee_is_paid", False),
                    is_treasury=data.get("is_treasury", False),
                )

                CustomerEconomicActivity.objects.create(
                    customer=customer,
                    employment_status=data["employment_status"],
                    economic_activity=data.get("economic_activity", ""),
                    profession=data.get("profession", ""),
                    monthly_income=data["monthly_income"],
                )

                _queue_welcome_messages(request, customer)

                request.session.pop("add_customer_data", None)
                messages.success(request, f"{customer.full_name} registered successfully.")
                return redirect("customers:customer_profile", customer_id=customer.id)

            return render(request, "customers/add_customer.html", {
                "form": form,
                "step": 3,
            })

    # ── GET ───────────────────────────────────────────────────────────
    current_step = int(step)
    session_data = request.session.get("add_customer_data", {})

    if current_step == 2:
        return render(request, "customers/add_customer.html", {
            "form": CommunicationResidenceForm(initial=session_data),
            "step": 2,
        })
    if current_step == 3:
        return render(request, "customers/add_customer.html", {
            "form": EconomicActivityForm(initial=session_data),
            "step": 3,
        })
    return render(request, "customers/add_customer.html", {
        "form": PersonalDetailsForm(initial=session_data),
        "step": 1,
    })
"""
Customer edit views — JSON-safe session handling.

THE BUG
-------
"Object of type CompanyBranch is not JSON serializable" is raised when Django
saves the session. It means a CompanyBranch *instance* (the cleaned value of
the `branch` ModelChoiceField) was placed into request.session, which is
serialized to JSON. Model instances are not JSON serializable.

THE FIX
-------
Two layers of defense:
  1. _json_safe(): converts model instances -> pk, dates -> isoformat.
  2. _store_session(): re-sanitizes the WHOLE dict immediately before it is
     written to the session, so even if a raw value slips past layer 1, it
     can never reach the JSON encoder. This makes the crash impossible.
"""

import datetime
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Model
from django.shortcuts import render, redirect, get_object_or_404

from .forms import (
    PersonalDetailsForm,
    CommunicationResidenceForm,
    EconomicActivityForm,
    CustomerForm,
)
from .models import Customer, CustomerEconomicActivity

logger = logging.getLogger(__name__)


def _json_safe(data):
    """Model instance -> pk, date/datetime -> isoformat, everything else as-is."""
    out = {}
    for key, value in data.items():
        if isinstance(value, Model):
            out[key] = value.pk
        elif isinstance(value, (datetime.date, datetime.datetime)):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def _store_session(request, key, data):
    """
    Write data into the session AFTER a final sanitization pass. This is the
    guarantee that no CompanyBranch (or any model/date) can reach the JSON
    encoder, regardless of how the dict was built upstream.
    """
    request.session[key] = _json_safe(data)


# ---------------------------------------------------------------------------
# Single-page edit
# ---------------------------------------------------------------------------
@login_required
def edit_customer(request, pk):
    """Single-page edit. Never touches the session, so it can't hit the bug."""
    customer = get_object_or_404(Customer, pk=pk)
    if request.method == "POST":
        form = CustomerForm(request.POST, instance=customer)
        if form.is_valid():
            form.save()
            messages.success(request, "Customer updated successfully!")
            return redirect("customers:list_customer")
    else:
        form = CustomerForm(instance=customer)
    return render(request, "customers/customer_form.html", {"form": form, "customer": customer})


@login_required
def edit_customer_stepper(request, pk, step=1):
    customer = get_object_or_404(Customer, pk=pk)
    session_key = f"edit_customer_data_{pk}"

    # Ensure the economic activity row exists immediately to prevent OneToOne reverse lookup crashes
    econ, _ = CustomerEconomicActivity.objects.get_or_create(customer=customer)

    if request.method == "POST":
        current_step = int(request.POST.get("step", 1))
        session_data = request.session.get(session_key, {})

        if current_step == 1:
            form = PersonalDetailsForm(request.POST, instance=customer)
            if form.is_valid():
                session_data.update(form.cleaned_data)
                _store_session(request, session_key, session_data)
                return redirect("customers:edit_customer_stepper", pk=pk, step=2)
            return render(request, "customers/add_customer.html", {
                "form": form, "step": 1, "editing": True, "customer": customer
            })

        elif current_step == 2:
            form = CommunicationResidenceForm(request.POST, instance=customer)
            if form.is_valid():
                session_data.update(form.cleaned_data)
                _store_session(request, session_key, session_data)
                return redirect("customers:edit_customer_stepper", pk=pk, step=3)
            return render(request, "customers/add_customer.html", {
                "form": form,
                "personal_form": PersonalDetailsForm(session_data, instance=customer),
                "step": 2, "editing": True, "customer": customer,
            })

        elif current_step == 3:
            form = EconomicActivityForm(request.POST, instance=econ, customer_instance=customer)
            if form.is_valid():
                data = dict(request.session.get(session_key, {}))
                data.update(_json_safe(form.cleaned_data))

                for field in ("customer_type",
                              "first_name", "middle_name", "last_name", "full_name",
                              "gender", "marital_status", "dob", "phone",
                              "national_id", "kra_pin", "reg_email",
                              "postal_address", "postal_code", "town", "home_address",
                              "default_notifications_setting",
                              "reg_fee_is_paid", "is_treasury"):
                    if field in data:
                        setattr(customer, field, data[field])
                if "branch" in data:
                    customer.branch_id = data["branch"]
                customer.save()

                form.save()

                request.session.pop(session_key, None)
                messages.success(request, f"{customer.full_name} updated successfully.")
                return redirect("customers:customer_profile", customer_id=customer.id)

            return render(request, "customers/add_customer.html", {
                "form": form,
                "personal_form": PersonalDetailsForm(request.session.get(session_key, {}), instance=customer),
                "residential_form": CommunicationResidenceForm(request.session.get(session_key, {}), instance=customer),
                "step": 3, "editing": True, "customer": customer,
            })

    # ---------------------------------------------------------------------------
    # GET — Render forms with existing database context safely
    # ---------------------------------------------------------------------------
    current_step = int(step)
    
    if current_step == 2:
        return render(request, "customers/add_customer.html", {
            "form": CommunicationResidenceForm(instance=customer),
            "personal_form": PersonalDetailsForm(instance=customer),
            "step": 2, "editing": True, "customer": customer,
        })
        
    if current_step == 3:
        return render(request, "customers/add_customer.html", {
            "form": EconomicActivityForm(instance=econ, customer_instance=customer),
            "personal_form": PersonalDetailsForm(instance=customer),
            "residential_form": CommunicationResidenceForm(instance=customer),
            "step": 3, "editing": True, "customer": customer,
        })
        
    # Default to Step 1
    return render(request, "customers/add_customer.html", {
        "form": PersonalDetailsForm(instance=customer),
        "step": 1, "editing": True, "customer": customer,
    })
from django.db.models import Sum
from django.shortcuts import get_object_or_404, render
from django.contrib.auth.decorators import login_required

from django.db.models import Sum
from django.shortcuts import get_object_or_404, render
from django.contrib.auth.decorators import login_required
from loans.models import RunningLoanStat

@login_required
def customer_profile(request, customer_id):
    """Displays detailed profile including branch, individual account breakdowns, and liabilities."""
    
    # 1. Fetch Customer with branch preloaded to minimize queries
    customer = get_object_or_404(Customer.objects.select_related('branch'), pk=customer_id)
    cust_no = customer.cust_no
    
    try:
        economic_activity = customer.economic_activity
    except CustomerEconomicActivity.DoesNotExist:
        economic_activity = None

    # 2. Dynamic Fetch: Organization Officials
    officials = []
    if customer.customer_type == 'group':
        officials = customer.groupofficials.all()
    elif customer.customer_type == 'church':
        officials = customer.churchofficials.all()

    # 3. INDIVIDUAL Savings Accounts Breakdowns
    individual_savings = (
        SavingsTransaction.objects.filter(cust_no=cust_no)
        .values('saving_type')
        .annotate(
            total_credits=Sum('credit_amount'),
            total_debits=Sum('debit_amount')
        )
    )
    
    savings_list = []
    total_savings_global = 0
    share_capital_balance = 0

    for acct in individual_savings:
        credits = acct['total_credits'] or 0
        debits = acct['total_debits'] or 0
        net_balance = credits - debits
        acct_type = acct['saving_type']
        
        account_data = {
            'account_name': acct_type.replace('_', ' ').title(),
            'balance': net_balance
        }
        savings_list.append(account_data)
        
        if acct_type == 'share_capital':
            share_capital_balance = net_balance
        else:
            total_savings_global += net_balance

    # 4. ACTIVE LOANS: Fetched directly from RunningLoanStat
    # We strictly filter out "Settled" loans and ensure there is an outstanding balance
    active_loans_queryset = RunningLoanStat.objects.filter(
        cust_no=cust_no
    ).exclude(
        loan_status__iexact="Settled"
    ).filter(
        loan_balance__gt=0
    )

    loans_list = []
    total_loans_taken = 0
    active_loan_balance = 0

    for loan in active_loans_queryset:
        # Accumulating overall stats for dashboard metrics
        total_loans_taken += loan.approved_amount
        active_loan_balance += loan.loan_balance

        loans_list.append({
            'loan_no': loan.loan_no,
            'loan_name': loan.product_description if loan.product_description else "Active Loan",
            'principal': loan.approved_amount,
            'balance': loan.loan_balance,
            'arrears': loan.total_arrears,
            'classification': loan.loan_classification,
            'next_payment_date': loan.next_repayment_date,
        })

    # 5. Guarantor Networks
    guarantees_given = Guarantor.objects.filter(guarantor_cust=customer).select_related('loan', 'loan__customer')
    my_guarantors = Guarantor.objects.filter(loan__customer=customer).select_related('guarantor_cust', 'loan')

    context = {
        "customer": customer,
        "branch_name": customer.branch.name if customer.branch else "Not Assigned",
        "economic_activity": economic_activity,
        "officials": officials,
        
        # Segmented Lists
        "savings_accounts": savings_list,
        "loans_accounts": loans_list,
        
        # Aggregated Metrics
        "savings_amount": total_savings_global,
        "share_capital_amount": share_capital_balance,
        "total_loans_taken": total_loans_taken,
        "active_loan_balance": active_loan_balance,
        
        # Guarantor Context
        "guarantees_given": guarantees_given,
        "my_guarantors": my_guarantors,
        "guarantees_count": guarantees_given.count(),
    }
    
    return render(request, "customers/customer_profile.html", context)

# --- Search API View (used by JavaScript for instant search) ---
def search_customer_api(request):
    query = request.GET.get("q", "").strip() # Get query and clean it
    
    # Define the base queryset (last 100 customers, newest first)
    customers_queryset = Customer.objects.order_by('-id')[:100]
    
    # If a valid search query is present (e.g., length 2 or more)
    if query and len(query) >= 2:
        # Filter the entire Customer table for the search query
        customers = Customer.objects.filter(
            models.Q(full_name__icontains=query) |
            models.Q(phone__icontains=query) |
            models.Q(national_id__icontains=query) |
            models.Q(cust_no__icontains=query)
        ).distinct()[:50] # Limit search results for performance
    else:
        # If no query (or query is too short), use the default last 100 list
        customers = customers_queryset
    
    # Serialize the results
    results = []
    for c in customers:
        results.append({
            "id": c.id,
            "full_name": c.full_name,
            "cust_no": c.cust_no,
            "national_id": c.national_id,
            "phone": c.phone,
        })
        
    return JsonResponse(results, safe=False)

# -------------------------------------------------------------------
# NOTE: The original search_customer view is redundant if you're using the JS API search.
# If you *must* keep it (for a non-API search form), here's a clean version:
def search_customer(request):
     query = request.GET.get("q")
     results = Customer.objects.none() # Start with an empty queryset
     if query:
         results = Customer.objects.filter(
             models.Q(cust_no__icontains=query) | # Using __icontains for partial matching
             models.Q(national_id__icontains=query) |
             models.Q(phone__icontains=query) |
             models.Q(full_name__icontains=query) # Assuming you meant full_name
         ).distinct()
     return render(request, "customers/search_customer_results.html", {"results": results, "query": query})
@login_required
def list_next_of_kin(request, customer_id):
    customer = get_object_or_404(Customer, pk=customer_id)
    kins = customer.kins.all()
    return render(request, "customers/list_next_of_kin.html", {"customer": customer, "kins": kins})

@login_required
def add_next_of_kin(request, customer_id):
    customer = get_object_or_404(Customer, pk=customer_id)
    if request.method == "POST":
        form = NextOfKinForm(request.POST)
        if form.is_valid():
            kin = form.save(commit=False)
            kin.customer = customer
            kin.save()
            messages.success(request, "Next of Kin added successfully!")
            return redirect("customers:list_next_of_kin", customer_id=customer.id)
    else:
        form = NextOfKinForm()
    return render(request, "customers/next_of_kin_form.html", {"form": form, "customer": customer})

@login_required
def edit_next_of_kin(request, pk):
    # 1. Fetch the specific Next of Kin record using the primary key
    kin = get_object_or_404(NextOfKin, id=pk)
    # 2. Identify the customer associated with this kin (for the "Back" button and context)
    customer = kin.customer 

    if request.method == 'POST':
        # 3. Pass the 'instance' so Django knows we are updating, not creating
        form = NextOfKinForm(request.POST, instance=kin)
        if form.is_valid():
            form.save()
            messages.success(request, f"Next of kin details for {kin.kin_name} updated successfully.")
            return redirect('customers:list_next_of_kin', customer_id=customer.id)
    else:
        # 4. Pre-fill the form with existing data
        form = NextOfKinForm(instance=kin)

    return render(request, 'customers/next_of_kin_form.html', {
        'form': form,
        'customer': customer,
        'kin': kin,
        'is_edit': True # Useful if you want to change the title in the template
    })
@login_required
def edit_customer_stepper(request, pk, step=1):
    customer = get_object_or_404(Customer, pk=pk)

    # If this is an organization (group/church), redirect to the org edit view
    if customer.customer_type in ('group', 'church'):
        return redirect('customers:edit_organization', pk=pk)

    # 1. ALWAYS DECLARE THIS FIRST SO IT IS AVAILABLE EVERYWHERE IN THE VIEW
    session_key = f"edit_customer_data_{pk}"

    # Ensure the economic activity row exists immediately to prevent OneToOne reverse lookup crashes
    econ, _ = CustomerEconomicActivity.objects.get_or_create(customer=customer)

    if request.method == "POST":
        current_step = int(request.POST.get("step", 1))
        session_data = request.session.get(session_key, {})

        if current_step == 1:
            form = PersonalDetailsForm(request.POST, instance=customer)
            if form.is_valid():
                # If customer_type was changed to group/church, save and redirect
                new_type = form.cleaned_data.get('customer_type', 'adult_individual')
                if new_type in ('group', 'church'):
                    customer.customer_type = new_type
                    customer.save(update_fields=['customer_type'])
                    request.session.pop(session_key, None)
                    return redirect('customers:edit_organization', pk=pk)

                # Copy the cleaned data dictionary to avoid mutating form internals
                step_data = dict(form.cleaned_data)
                
                # Extract the primary key out of the live CompanyBranch model object for JSON serialization
                if step_data.get("branch"):
                    if hasattr(step_data["branch"], "pk"):
                        step_data["branch"] = step_data["branch"].pk
                    else:
                        step_data["branch"] = str(step_data["branch"])

                # Save to session safely
                session_data.update(step_data)
                _store_session(request, session_key, session_data)
                return redirect("customers:edit_customer_stepper", pk=pk, step=2)
                
            return render(request, "customers/add_customer.html", {
                "form": form, "step": 1, "editing": True, "customer": customer
            })

        elif current_step == 2:
            form = CommunicationResidenceForm(request.POST, instance=customer)
            if form.is_valid():
                session_data.update(form.cleaned_data)
                _store_session(request, session_key, session_data)
                return redirect("customers:edit_customer_stepper", pk=pk, step=3)
            return render(request, "customers/add_customer.html", {
                "form": form,
                "personal_form": PersonalDetailsForm(session_data, instance=customer),
                "step": 2, "editing": True, "customer": customer,
            })

        elif current_step == 3:
            form = EconomicActivityForm(request.POST, instance=econ, customer_instance=customer)
            if form.is_valid():
                data = dict(request.session.get(session_key, {}))
                data.update(_json_safe(form.cleaned_data))

                for field in ("customer_type",
                              "first_name", "middle_name", "last_name", "full_name",
                              "gender", "marital_status", "dob", "phone",
                              "national_id", "kra_pin", "reg_email",
                              "postal_address", "postal_code", "town", "home_address",
                              "default_notifications_setting",
                              "reg_fee_is_paid", "is_treasury"):
                    if field in data:
                        setattr(customer, field, data[field])
                if "branch" in data:
                    customer.branch_id = data["branch"]
                customer.save()

                form.save()

                request.session.pop(session_key, None)
                messages.success(request, f"{customer.full_name} updated successfully.")
                return redirect("customers:customer_profile", customer_id=customer.id)

            return render(request, "customers/add_customer.html", {
                "form": form,
                "personal_form": PersonalDetailsForm(request.session.get(session_key, {}), instance=customer),
                "residential_form": CommunicationResidenceForm(request.session.get(session_key, {}), instance=customer),
                "step": 3, "editing": True, "customer": customer,
            })

    # ---------------------------------------------------------------------------
    # GET — Render forms with existing database context safely
    # ---------------------------------------------------------------------------
    current_step = int(step)
    
    if current_step == 2:
        return render(request, "customers/add_customer.html", {
            "form": CommunicationResidenceForm(instance=customer),
            "personal_form": PersonalDetailsForm(instance=customer),
            "step": 2, "editing": True, "customer": customer,
        })
        
    if current_step == 3:
        return render(request, "customers/add_customer.html", {
            "form": EconomicActivityForm(instance=econ, customer_instance=customer),
            "personal_form": PersonalDetailsForm(instance=customer),
            "residential_form": CommunicationResidenceForm(instance=customer),
            "step": 3, "editing": True, "customer": customer,
        })
        
    # Default to Step 1
    return render(request, "customers/add_customer.html", {
        "form": PersonalDetailsForm(instance=customer),
        "step": 1, "editing": True, "customer": customer,
    })

@login_required
def register_organization(request, org_type):
    # org_type will be 'group' or 'church'
    title = "Group" if org_type == 'group' else "Church"
    formset_class = GroupOfficialFormSet if org_type == 'group' else ChurchOfficialFormSet
    
    if request.method == "POST":
        form = OrganizationDetailsForm(request.POST, org_type=title)
        # Initialize formset here so it exists even if form.is_valid() is False
        formset = formset_class(request.POST) 
        
        if form.is_valid():
            customer = form.save(commit=False)
            customer.customer_type = org_type
            
            # Use username if authenticated, otherwise a default string
            if request.user.is_authenticated:
                customer.created_by = request.user.username
                customer.registered_by = request.user
            else:
                customer.created_by = "System"
                
            customer.save()
            
            # Re-initialize formset with the customer instance to link them
            formset = formset_class(request.POST, instance=customer)
            if formset.is_valid():
                formset.save()
                messages.success(request, f"{title} registered successfully!")
                return redirect('customers:list_customers')
            else:
                # If formset is invalid, we might want to delete the customer 
                # or handle the error so we don't have an org with no officials
                messages.error(request, "Please correct the errors in the officials section.")
        else:
            messages.error(request, "Please correct the errors in the entity details.")
            
    else:
        form = OrganizationDetailsForm(org_type=title)
        formset = formset_class()

    return render(request, 'customers/register_org.html', {
        'form': form,
        'formset': formset,
        'title': title
    })


@login_required
def edit_organization(request, pk):
    """
    Edit an existing group or church member — shows the org details form
    plus an officials formset (with delete capability).
    """
    from customers.forms import (
        OrganizationDetailsForm,
        GroupOfficialEditFormSet,
        ChurchOfficialEditFormSet,
    )

    customer = get_object_or_404(Customer, pk=pk)
    org_type = customer.customer_type  # 'group' or 'church'
    title = "Group" if org_type == 'group' else "Church"
    formset_class = GroupOfficialEditFormSet if org_type == 'group' else ChurchOfficialEditFormSet

    if request.method == "POST":
        form = OrganizationDetailsForm(request.POST, instance=customer, org_type=title)
        formset = formset_class(request.POST, instance=customer)

        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            messages.success(request, f"{title} '{customer.full_name}' updated successfully!")
            return redirect('customers:customer_profile', customer_id=customer.id)
        else:
            if not form.is_valid():
                messages.error(request, "Please correct the errors in the entity details.")
            if not formset.is_valid():
                messages.error(request, "Please correct the errors in the officials section.")
    else:
        form = OrganizationDetailsForm(instance=customer, org_type=title)
        formset = formset_class(instance=customer)

    return render(request, 'customers/edit_org.html', {
        'form': form,
        'formset': formset,
        'title': title,
        'customer': customer,
    })


@login_required
def exit_member_page(request):
    """Renders the member exit search and settlement page."""
    return render(request, 'customers/member_exit.html')
# 1. AJAX Live Search (No Select2)
@login_required
def member_search(request):
    query = request.GET.get('term', '')
    customers = Customer.objects.filter(
        models.Q(full_name__icontains=query) | 
        models.Q(cust_no__icontains=query) |
        models.Q(phone__icontains=query)
    ).exclude(customer_status__in=['exited', 'deceased'])[:10]

    results = [{"cust_no": c.cust_no, "name": c.full_name, "id": c.national_id} for c in customers]
    return JsonResponse(results, safe=False)
@login_required
def member_search_for_reactivation(request):
    query = request.GET.get('term', '')
    customers = Customer.objects.filter(
        models.Q(full_name__icontains=query) | 
        models.Q(cust_no__icontains=query) |
        models.Q(phone__icontains=query)
    ).filter(customer_status='exited')[:10]
    
    results = [{"cust_no": c.cust_no, "name": c.full_name, "id": c.national_id} for c in customers]
    return JsonResponse(results, safe=False)
from decimal import Decimal
from django.db.models import Sum
from .models import Customer
from loans.models import Guarantor, RunningLoanStat
from transactions.models import SavingsTransaction, CustomerAccountsSetup
# Assuming LoanTransaction exists for direct obligations
from transactions.models import LoanTransaction 

class ExitError(Exception):
    pass

def validate_exit(cust_no):
    """
    Validates if a member is legally eligible to exit the Sacco.
    Returns (is_ok, total_outstanding_direct_loan_balance, active_guaranteed_loan_list)
    """
    # 1. Check direct outstanding loan debts
    direct_loans = LoanTransaction.objects.filter(cust_no=cust_no).aggregate(
        bal=(Sum('debit_amount') - Sum('credit_amount'))
    )['bal'] or Decimal('0.00')
    
    if direct_loans > 0:
        return False, direct_loans, []

    # 2. Check active external guarantorship liabilities
    # Query loans this customer guaranteed that are NOT explicitly 'Settled'
    active_guarantees = []
    guarantees_given = Guarantor.objects.filter(
        guarantor_cust__cust_no=cust_no
    ).select_related('loan')

    for g in guarantees_given:
        # Cross-reference with the active execution states engine
        stat = RunningLoanStat.objects.filter(loan_no=g.loan.loan_no).first()
        
        # If the loan is missing from stats or its status is not marked explicitly as Settled
        if not stat or stat.loan_status != "Settled":
            # Also double check balance from the ledger if status is out of sync
            ledger_bal = LoanTransaction.objects.filter(loan_id=g.loan.id).aggregate(
                bal=(Sum('debit_amount') - Sum('credit_amount'))
            )['bal'] or Decimal('0.00')
            
            if ledger_bal > 0 or (stat and stat.loan_status != "Settled"):
                active_guarantees.append({
                    "loan_no": g.loan.loan_no,
                    "amount": g.amount,
                    "current_status": stat.loan_status if stat else "Active"
                })

    if active_guarantees:
        return False, direct_loans, active_guarantees

    return True, Decimal('0.00'), []

from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from customers.services import validate_exit

@login_required
def validate_member_exit(request, cust_no):
    """AJAX verification endpoint for the Maker layout interface"""
    # Calculate Savings Balance
    savings = SavingsTransaction.objects.filter(cust_no=cust_no).aggregate(
        bal=(Sum('credit_amount') - Sum('debit_amount'))
    )['bal'] or Decimal('0.00')

    is_ok, loan_bal, active_guarantees = validate_exit(cust_no)

    return JsonResponse({
        "savings_balance": float(savings),
        "loan_balance": float(loan_bal),
        "active_guarantees": active_guarantees,
        "can_exit": is_ok
    })
@login_required
@min_role_required('loan_officer')
def process_member_exit(request):
    if request.method == "POST":
        cust_no   = request.POST.get('cust_no')
        reason    = request.POST.get('reason')
        exit_type = request.POST.get('exit_type')
        exit_date = request.POST.get('exit_date')

        from customers.services import build_exit_payload, validate_exit
        from approvals.services import ApprovalService

        try:
            customer = Customer.objects.get(cust_no=cust_no)
        except Customer.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Member not found."}, status=404)

        # FIX: Unpack 3 values here using a throwaway variable (_) for guarantees if not needed in maker phase
        ok, loan_bal, active_guar = validate_exit(cust_no)
        if not ok:
            if loan_bal > 0:
                msg = f"Member owes KES {loan_bal:,.2f} on loans. Settle before exit."
            else:
                msg = f"Member is actively guaranteeing clearing accounts: {active_guar}. Cannot exit."
            return JsonResponse({"status": "error", "message": msg}, status=400)

        payload = build_exit_payload(
            cust_no, reason, exit_type, exit_date,
            death_date=request.POST.get('death_date'),
        )
        approval = ApprovalService.submit(
            action_type='member_exit',
            maker=request.user,
            obj=customer,
            payload=payload,
            note=reason or '',
        )

        return JsonResponse({
            "status": "success",
            "message": f"Exit request for {customer.full_name} submitted for approval (Request #{approval.pk}).",
        })
        
import datetime
from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.db import transaction as db_transaction
from .models import Customer

@login_required
def validate_member_reactivation(request, cust_no):
    """AJAX endpoint validating eligibility for Sacco reactivation."""
    try:
        customer = Customer.objects.get(cust_no=cust_no)
    except Customer.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Member profile not found."}, status=404)

    # Validate that they are actually eligible for reactivation
    is_exited = customer.customer_status in ['exited', 'withdrawn', 'resigned', 'suspended']
    
    return JsonResponse({
        "name": customer.full_name,
        "current_status": customer.get_customer_status_display() if hasattr(customer, 'get_customer_status_display') else customer.customer_status,
        "exit_date": customer.exit_date.strftime('%Y-%m-%d') if customer.exit_date else "N/A",
        "can_reactivate": is_exited
    })

'''
@login_required
# @min_role_required('loan_officer')
@require_POST
def process_member_reactivation(request):
    """
    Processes the member reactivation request.
    Flips 'reg_fee_is_paid' to False to trigger background job debiting processes.
    """
    cust_no = request.POST.get('cust_no')
    reason = request.POST.get('reason', '').strip()
    reactivation_date_str = request.POST.get('reactivation_date')

    if not cust_no or not reactivation_date_str:
        return JsonResponse({"status": "error", "message": "Missing mandatory field records."}, status=400)

    try:
        customer = Customer.objects.get(cust_no=cust_no)
    except Customer.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Member profile not found."}, status=404)

    if customer.customer_status == 'active':
        return JsonResponse({"status": "error", "message": "This member profile is already active."}, status=400)

    try:
        with db_transaction.atomic():
            # Apply Reactivation State Mutations
            customer.customer_status = 'active'
            customer.reg_fee_is_paid = False  # Triggers background processing engine to debit reinstatement dues
            customer.reactivation_date = datetime.datetime.strptime(reactivation_date_str, '%Y-%m-%d').date()
            customer.reactivation_reason = reason
            customer.updated_by = request.user.username
            
            # Clear historical exit information blocks
            customer.exit_date = None
            customer.exit_reason = None
            customer.save()

            # Optional: Log system telemetry audit event
            try:
                from audit.services import log_financial_event
                log_financial_event(
                    event='MEMBER_REACTIVATION_PROCESSED',
                    amount=0.00,
                    reference=f'REACT-{cust_no}',
                    actor=request.user.username,
                    details=f"Member {customer.full_name} status updated to Active. Re-activation subscription fee job queued.",
                    severity='info',
                )
            except Exception:
                pass

        return JsonResponse({
            "status": "success",
            "message": f"Member {customer.full_name} successfully re-activated. Re-activation debit fee job queued."
        })

    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Database pipeline execution failed: {str(e)}"}, status=500)
'''
@login_required
# @min_role_required('loan_officer')
def process_member_reactivation(request):
    """
    GET: Renders the reactivation workspace workspace.
    POST: Processes the database changes and resets reg_fee_is_paid to False.
    """
    if request.method == "GET":
        # Simply serve the template file to the browser
        return render(request, 'customers/member_reactivation.html')
        
    elif request.method == "POST":
        cust_no = request.POST.get('cust_no')
        reason = request.POST.get('reason', '').strip()
        reactivation_date_str = request.POST.get('reactivation_date')

        if not cust_no or not reactivation_date_str:
            return JsonResponse({"status": "error", "message": "Missing mandatory field records."}, status=400)

        try:
            customer = Customer.objects.get(cust_no=cust_no)
            with db_transaction.atomic():
                customer.customer_status = 'active'
                customer.reg_fee_is_paid = False  
                customer.is_reactivated = True
                customer.reactivation_date = datetime.datetime.strptime(reactivation_date_str, '%Y-%m-%d').date()
                customer.reactivation_reason = reason
                customer.updated_by = request.user.username
                customer.exit_date = None
                customer.exit_reason = None
                customer.save()
            return JsonResponse({
                "status": "success", 
                "message": f"Member {customer.full_name} successfully re-activated."
            })
        except Customer.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Member profile not found."}, status=404)