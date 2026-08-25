import csv
import io
import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP

# Django Imports
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import min_role_required, role_required

# Third-Party Imports (ReportLab)
from reportlab.graphics.shapes import Drawing, Rect
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
)

# Local App Imports
from accounting.models import (
    SaccoAccount, SaccoAccountBalance, SaccoAccountsLedger
)
from customers.models import Customer
from transactions.models import LoanTransaction, SavingsTransaction,CustomerAccountsSetup
from transactions.utils import make_tr_ref

# Current App Imports (.models / .forms / .utils)
from .forms import (
    AddGuarantorForm, CollateralForm, InterestChargeForm, 
    LoanChargeForm, LoanDispatchForm, ReplaceGuarantorForm
)
from .models import (
    Collateral, Guarantor, InterestChargeBatch, InterestChargeDraftItem,
    LoanCharge, LoanChargeRecovery, LoanHistory, RunningLoanStat
)
from .utils import update_running_loans_stats

logger = logging.getLogger(__name__)


def search_customer_api(request):
    """API to fetch customer details for the frontend"""
    cust_no = request.GET.get('cust_no')
    if cust_no:
        try:
            customer = Customer.objects.get(cust_no=cust_no)
            return JsonResponse({'found': True, 'full_name': customer.full_name})
        except (Customer.DoesNotExist, ValueError):
            pass
    return JsonResponse({'found': False})

def calculate_savings_balance(cust_no):
    agg = SavingsTransaction.objects.filter(cust_no=cust_no).aggregate(
        total_credit=Sum('credit_amount', default=0),
        total_debit=Sum('debit_amount', default=0)
    )
    return (agg['total_credit'] or Decimal(0)) - (agg['total_debit'] or Decimal(0))
def calculate_share_balance(cust_no):
    agg = SavingsTransaction.objects.filter(cust_no=cust_no, saving_type='share_capital').aggregate(
        total_credit=Sum('credit_amount', default=0),
        total_debit=Sum('debit_amount', default=0)
    )
    return (agg['total_credit'] or Decimal(0)) - (agg['total_debit'] or Decimal(0))

def calculate_loan_balance(cust_no, loan_no):
    # This assumes LoanTransaction tracks both principal debit and repayment credit
    agg = LoanTransaction.objects.filter(
        cust_no=cust_no,
        loan_no=loan_no
    ).aggregate(
        total_debit=Sum('debit_amount', default=0),
        total_credit=Sum('credit_amount', default=0)
    )
    return (agg['total_debit'] or Decimal(0)) - (agg['total_credit'] or Decimal(0))

from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
# Import your specific min_role_required and LoanHistory models here

@login_required
@min_role_required('accounts_clerk')
def loan_dashboard(request):
    query = request.GET.get('q', '').strip()
    
    # 1. Base queryset — select_related pulls customer + loan_type in one JOIN
    loans_list = LoanHistory.objects.select_related('customer', 'loan_type').order_by('-id')
    
    # 2. Apply Search Filter if query exists
    if query:
        loans_list = loans_list.filter(
            Q(loan_no__icontains=query) | 
            Q(customer__cust_no__icontains=query) |
            Q(customer__full_name__icontains=query)
        )
        
    # 3. Setup Pagination (e.g., 20 records per page)
    paginator = Paginator(loans_list, 20)
    page = request.GET.get('page')
    
    try:
        page_obj = paginator.page(page)
    except PageNotAnInteger:
        # If page is not an integer, deliver first page.
        page_obj = paginator.page(1)
    except EmptyPage:
        # If page is out of range, deliver last page of results.
        page_obj = paginator.page(paginator.num_pages)
    
    return render(request, 'loans/dashboard.html', {
        'page_obj': page_obj,
        'query': query
    })

@login_required
@min_role_required('accounts_clerk')
def api_borrower_info(request):
    cust_no = request.GET.get('cust_no')
    customer = Customer.objects.filter(cust_no=cust_no).first()
    if not customer:
        return JsonResponse({'found': False})
    
    active_loans = []
    # Get active loans for bridging
    for loan in LoanHistory.objects.filter(customer=customer):
        bal = LoanTransaction.objects.filter(loan_id=loan.id).aggregate(
            b=Sum('debit_amount') - Sum('credit_amount')
        )['b'] or 0
        if bal > 0:
            active_loans.append({'id': loan.id, 'desc': f"{loan.loan_type} (Bal: {bal:,.2f})", 'balance': float(bal)})
            
    return JsonResponse({'found': True, 'name': customer.full_name, 'loans': active_loans})

@login_required
@min_role_required('accounts_clerk')
def api_guarantor_info(request):
    query = request.GET.get('q')
    g = Customer.objects.filter(Q(cust_no=query) | Q(full_name__icontains=query)).first()
    
    if g:
        # Get Savings Balance
        savings = SavingsTransaction.objects.filter(cust_no=g.cust_no).aggregate(
            b=Sum('credit_amount') - Sum('debit_amount')
        )['b'] or 0
        
        # FIXED: Use the 'amount__sum' key and pass the instance 'g' to 'guarantor_cust'
        liabilities = Guarantor.objects.filter(guarantor_cust=g).aggregate(
            Sum('amount')
        )['amount__sum'] or 0
            
        available = (safe_decimal(savings) * Decimal('3')) - safe_decimal(liabilities)

        # Defaulter check — arrears > 3 months blocks guaranteeing
        defaulter_loan = RunningLoanStat.objects.filter(
            cust_no=g.cust_no,
            loan_status='Active',
            loan_balance__gt=0,
            defaulted_installments__gt=3,
        ).order_by('-defaulted_installments').first()

        is_defaulter = defaulter_loan is not None
        defaulter_detail = ''
        if is_defaulter:
            defaulter_detail = (
                f"Defaulter on {defaulter_loan.loan_no} — "
                f"{defaulter_loan.defaulted_installments} month(s) arrears "
                f"(KES {defaulter_loan.total_arrears:,.2f})"
            )
        
        return JsonResponse({
            'found': True, 
            'cust_no': g.cust_no, 
            'name': g.full_name, 
            'savings': float(savings), 
            'available': float(available),
            'is_defaulter': is_defaulter,
            'defaulter_detail': defaulter_detail,
            'customer_status': getattr(g, 'customer_status', 'Active'),
        })
        
    return JsonResponse({'found': False})

@require_GET
@login_required
@min_role_required('accounts_clerk')
def customer_unsettled_loans(request):
    """
    Triggers running loan stat compilation dynamically, then returns 
    all open accounts with outstanding debt for the selected customer.
    """
    cust_no = request.GET.get("cust_no", "").strip()
    if not cust_no:
        return JsonResponse({"error": "Customer number is required."}, status=400)
    
    try:
        # 1. Trigger the background routine safely before gathering records
        update_running_loans_stats(cust_no=cust_no)
        
        # 2. Extract active accounts with remaining balances
        unsettled_loans = RunningLoanStat.objects.filter(
            cust_no=cust_no,
            loan_balance__gt=0,
            loan_status__iexact="Active"
        ).order_by("-application_date")
        
        loans_data = []
        for loan in unsettled_loans:
            loans_data.append({
                "loan_no": loan.loan_no,
                "product_description": loan.product_description or "Commercial Loan",
                "loan_balance": str(loan.loan_balance),
                "monthly_installment": str(loan.monthly_installment)
            })
            
        return JsonResponse({
            "status": "success",
            "customer_no": cust_no,
            "loans": loans_data
        })
        
    except Exception as e:
        logger.error(f"Failed to fetch bridging profiles for client {cust_no}: {str(e)}")
        return JsonResponse({"error": "Internal ledger compilation failure."}, status=500)


def safe_decimal(value, default="0.00"):
    """Safely cast string/numeric inputs into standardized Decimal types."""
    if not value:
        return Decimal(default)
    try:
        # Strip commas or spacing introduced by human input copy-pasting
        cleaned_value = str(value).replace(',', '').strip()
        return Decimal(cleaned_value)
    except (ValueError, TypeError):
        return Decimal(default)
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from decimal import Decimal, ROUND_HALF_UP


@login_required
def api_product_charges(request):
    """
    GET /loans/api/product-charges/?product_id=<pk>
    Returns mandatory and optional LoanCharge records for a given
    CustomerAccountsSetup (loan product), plus its interest_calc_method.
    """
    product_id = request.GET.get('product_id')
    if not product_id:
        return JsonResponse({'status': 'error', 'message': 'product_id is required.'}, status=400)

    try:
        from transactions.models import CustomerAccountsSetup   # adjust import path to your project
        product = CustomerAccountsSetup.objects.get(pk=product_id, is_active=True)
    except CustomerAccountsSetup.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Loan product not found or inactive.'}, status=404)

    from .models import LoanCharge   # adjust to your app's import path

    # ── FIXED: Changed 'loan_product' lookup to 'loan_products' ──
    charge_fields = (
        'id', 'name', 'charge_type', 'amount',
        'min_amount', 'max_amount', 'is_bridging_fee',
    )

    mandatory_qs = LoanCharge.objects.filter(
        loan_products=product,
        is_mandatory=True,
        is_active=True,
    ).order_by('name').values(*charge_fields)

    optional_qs = LoanCharge.objects.filter(
        loan_products=product,
        is_mandatory=False,
        is_active=True,
    ).order_by('name').values(*charge_fields)

    def _serialise(qs):
        return [
            {
                'id': c['id'],
                'name': c['name'],
                'charge_type': c['charge_type'],
                'amount': str(c['amount']),
                'min_amount': str(c['min_amount']),
                'max_amount': str(c['max_amount']),
                'is_bridging_fee': c['is_bridging_fee'],
            }
            for c in qs
        ]

    return JsonResponse({
        'status': 'success',
        'interest_calc_method': product.interest_calc_method or 'reducing_balance',
        'mandatory': _serialise(mandatory_qs),
        'optional': _serialise(optional_qs),
    })

# ─────────────────────────────────────────────────────────────────────────────
# UPDATED: Main Loan Dispatch View
# Key changes:
#   • Mandatory charges are now scoped to the selected loan_type (product).
#   • Optional charges on POST are validated against the same product.
#   • Installment is calculated server-side from the product's calc method
#     (the frontend mirrors this; both must agree).
#   • context no longer pre-loads all charges (product charges are fetched
#     via api_product_charges when the user selects a product).
# ─────────────────────────────────────────────────────────────────────────────
@login_required
@min_role_required('loan_officer')
def loan_dispatch(request, pk=None):
    """
    Main view governing underwriter entry, dynamic charge calculation,
    bridging offset validations, and Sacco ledger execution.

    When pk is supplied (edit mode) and the loan has NOT been approved,
    the form is pre-populated so the officer can amend key fields.
    """
    editing_loan = None
    if pk is not None:
        editing_loan = get_object_or_404(LoanHistory, pk=pk)
        if editing_loan.is_approved:
            messages.error(request, "This loan has already been approved and cannot be edited.")
            return redirect('loans:loan_dashboard')

    if request.method == 'POST':
        form = LoanDispatchForm(request.POST, instance=editing_loan)
        if form.is_valid():
            try:
                with transaction.atomic():
                    # ─── 1. Initialise objects ────────────────────────────────
                    customer = get_object_or_404(Customer, cust_no=form.cleaned_data['cust_no'])
                    loan = form.save(commit=False)
                    loan.customer = customer
                    loan.created_by = request.user.username if request.user.is_authenticated else "system"

                    loan_config  = form.cleaned_data.get('loan_type')
                    calc_method  = loan_config.interest_calc_method if loan_config else 'flat_rate'

                    is_flat_rate          = (calc_method == 'flat_rate')
                    is_principal_flat     = (calc_method == 'principal_flat_rate')
                    is_reducing_balance   = (calc_method == 'reducing_balance')

                    # ─── 2. Base figures ──────────────────────────────────────
                    principal = safe_decimal(form.cleaned_data.get('principal'))
                    rate      = safe_decimal(form.cleaned_data.get('interest_rate')) / Decimal('100')
                    period    = safe_decimal(form.cleaned_data.get('loan_period', 1))

                    if period <= Decimal('0'):
                        period = Decimal('1')

                    # ─── 3. Installment & upfront interest ───────────────────
                    upfront_interest = Decimal('0')

                    if is_flat_rate:
                        upfront_interest  = principal * rate * period
                        loan.installment  = (principal + upfront_interest) / period

                    elif is_principal_flat:
                        upfront_interest  = principal * rate * period
                        loan.installment  = (principal / period) + (principal * rate)

                    elif is_reducing_balance:
                        if rate > Decimal('0'):
                            rate_factor      = (Decimal('1') + rate) ** int(period)
                            loan.installment = (principal * rate * rate_factor) / (rate_factor - Decimal('1'))
                        else:
                            loan.installment = principal / period
                    else:
                        loan.installment = principal / period

                    loan.installment = loan.installment.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                    # ─── 4. Process charges ───────────────────────────────────
                    total_dynamic_fees    = Decimal('0')
                    charges_data_to_save  = []

                    # A. Mandatory charges — scoped to the selected loan product
                    #    IMPORTANT: exclude is_bridging_fee charges here — those
                    #    are calculated per-offset-loan in step 5 to avoid
                    #    double-counting.
                    if loan_config:
                        mandatory_charges = LoanCharge.objects.filter(
                            loan_products=loan_config,
                            is_mandatory=True,
                            is_active=True,
                            is_bridging_fee=False,
                        )
                    else:
                        mandatory_charges = LoanCharge.objects.none()

                    for charge in mandatory_charges:
                        if charge.charge_type == 'percentage':
                            calc_amount = (principal * (charge.amount / Decimal('100'))).quantize(
                                Decimal('0.01'), rounding=ROUND_HALF_UP
                            )
                        else:
                            calc_amount = charge.amount

                        # ── Clamp to min/max bounds defined on the charge ──
                        if charge.min_amount > Decimal('0') and calc_amount < charge.min_amount:
                            calc_amount = charge.min_amount
                        if charge.max_amount > Decimal('0') and calc_amount > charge.max_amount:
                            calc_amount = charge.max_amount

                        total_dynamic_fees += calc_amount
                        charges_data_to_save.append((charge, calc_amount))

                    # B. Optional charges — validate against the same product
                    charge_ids    = request.POST.getlist('charge_profile[]')
                    charge_amounts = request.POST.getlist('charge_amount[]')

                    for c_id, c_amt in zip(charge_ids, charge_amounts):
                        val = safe_decimal(c_amt)
                        if val > Decimal('0') and c_id:
                            try:
                                # Only accept non-bridging charges linked to the
                                # submitted loan product.  Bridging fees are
                                # calculated per-offset-loan in step 5.
                                charge_obj = LoanCharge.objects.get(
                                    id=c_id,
                                    loan_products=loan_config,
                                    is_mandatory=False,
                                    is_bridging_fee=False,
                                    is_active=True,
                                )
                                # ── Clamp to min/max bounds ──
                                if charge_obj.min_amount > Decimal('0') and val < charge_obj.min_amount:
                                    val = charge_obj.min_amount
                                if charge_obj.max_amount > Decimal('0') and val > charge_obj.max_amount:
                                    val = charge_obj.max_amount

                                total_dynamic_fees += val
                                charges_data_to_save.append((charge_obj, val))
                            except LoanCharge.DoesNotExist:
                                # Silently skip charges not belonging to this product
                                logger.warning(
                                    f"Charge id={c_id} skipped — not linked to product '{loan_config}'."
                                )

                    # ─── 5. Bridging / offsets ────────────────────────────────
                    offset_loan_nos = request.POST.getlist('offset_loan_ids')
                    offset_amts     = request.POST.getlist('offset_amounts')

                    # Bridging charge — detected via is_bridging_fee flag on LoanCharge
                    bridging_charge_meta = LoanCharge.objects.filter(
                        loan_products=loan_config,
                        is_bridging_fee=True,
                        is_active=True,
                    ).first()

                    if bridging_charge_meta and bridging_charge_meta.amount > Decimal('0'):
                        bridging_rate = bridging_charge_meta.amount / Decimal('100')
                    else:
                        bridging_rate = Decimal('0.10')   # 10% safety fallback

                    total_bridging_amt  = Decimal('0')
                    total_bridging_fees = Decimal('0')
                    validated_offsets   = []

                    for old_loan_no, amt in zip(offset_loan_nos, offset_amts):
                        val = safe_decimal(amt)
                        if old_loan_no and val > Decimal('0'):
                            # MAKER PHASE: validate only — balances are mutated
                            # at approval time by loans.disbursement.
                            running_loan = RunningLoanStat.objects.get(loan_no=old_loan_no)
                            if val > running_loan.loan_balance:
                                raise ValueError(
                                    f"Offset amount ({val:,.2f}) exceeds current ledger balance for {old_loan_no}."
                                )

                            current_penalty = (val * bridging_rate).quantize(
                                Decimal('0.01'), rounding=ROUND_HALF_UP
                            )
                            total_bridging_amt  += val
                            total_bridging_fees += current_penalty
                            validated_offsets.append((old_loan_no, val))

                            if bridging_charge_meta:
                                charges_data_to_save.append((bridging_charge_meta, current_penalty))

                    # ─── 6. Net disbursed & save loan ─────────────────────────
                    loan.total_offset_amount = total_bridging_amt
                    loan.offset_data = [
                        {'loan_no': lno, 'amount': str(amt)}
                        for lno, amt in validated_offsets
                    ]
                    loan.net_disbursed = principal - (
                        total_dynamic_fees + total_bridging_amt + total_bridging_fees
                    )
                    loan.save()

                    # Edit mode: clear stale charge recoveries before re-building
                    if editing_loan:
                        LoanChargeRecovery.objects.filter(loan=loan).delete()

                    if charges_data_to_save:
                        LoanChargeRecovery.objects.bulk_create([
                            LoanChargeRecovery(
                                loan=loan, charge=c_obj, amount=c_amt,
                                date=loan.loan_date,
                                reference=loan.loan_no,
                                description=c_obj.name,
                            )
                            for c_obj, c_amt in charges_data_to_save
                        ])

                    # ─── 7. Guarantors & collateral are managed via their
                    #        dedicated views (add_guarantor / add_collateral).
                    #        Existing records are preserved on edit. ────────

                    # ─── 8. MAKER-CHECKER: submit for manager approval ───────
                    # No money has moved yet. LoanTransaction and Sacco ledger
                    # postings happen only when a manager approves
                    # (loans/disbursement.py via the approvals app).
                    from approvals.services import ApprovalService
                    from loans.disbursement import build_payload

                    payload = build_payload(
                        loan, principal, upfront_interest,
                        total_dynamic_fees, total_bridging_amt, total_bridging_fees,
                        validated_offsets,
                        loan_config.account_name if loan_config else "Standard Loan",
                    )
                    approval = ApprovalService.submit(
                        action_type='loan_disburse',
                        maker=request.user,
                        obj=loan,
                        payload=payload,
                        note=request.POST.get('approval_note', ''),
                    )

                # Notify the member their application is pending approval
                # (only on initial submission, not when editing/re-submitting)
                if not editing_loan:
                    from sms.services import notify, msg_approval_pending
                    if customer.phone:
                        notify(
                            customer.phone,
                            msg_approval_pending(customer.first_name, "loan application", loan.loan_no),
                            created_by=loan.created_by,
                        )

                if editing_loan:
                    messages.success(
                        request,
                        f"Loan {loan.loan_no} updated and re-submitted for approval "
                        f"(Request #{approval.pk})."
                    )
                else:
                    messages.success(
                        request,
                        f"Loan {loan.loan_no} captured and submitted for approval "
                        f"(Request #{approval.pk}). Disbursement will execute once a manager approves."
                    )
                return redirect('loans:loan_dashboard')

            except ValueError as ve:
                messages.error(request, f"Validation Error: {str(ve)}")
            except Exception as e:
                logger.error(f"System Error during dispatch: {str(e)}", exc_info=True)
                messages.error(request, f"System Error during dispatch: {str(e)}")
        else:
            logger.error(f"Form Validation Dropped. Field Error Payload: {form.errors.as_json()}")
            messages.error(request, "Form layout is invalid. Verify configuration inputs.")

    else:
        if editing_loan:
            form = LoanDispatchForm(instance=editing_loan, initial={
                'cust_no': editing_loan.customer.cust_no,
            })
        else:
            form = LoanDispatchForm()

    # Charges are now loaded dynamically per product via api_product_charges.
    context = {
        'form': form,
        'editing_loan': editing_loan,
    }
    return render(request, 'loans/loan_dispatch.html', context)
@transaction.atomic
@login_required
@min_role_required('loan_officer')
def add_guarantor(request, pk):
    """
    Add a guarantor to a loan, enforcing headroom against:
      * live deposit balances (per product × guarantee_multiplier), and
      * OUTSTANDING commitments only (Guarantor rows tied to a
        RunningLoanStat where loan_balance > 0) — cleared loans free up
        the guarantor's ceiling automatically.

    The template also renders a live deposits snapshot so the officer
    sees insufficient capacity BEFORE trying to commit.
    """
    from loans.appraisal_metrics import live_guarantor_metrics

    loan = get_object_or_404(LoanHistory, id=pk)
    existing_guarantors = Guarantor.objects.filter(loan=loan)
    metrics_ctx = None  # populated on GET / re-render when a cust_no is prefilled

    if request.method == 'POST':
        form = AddGuarantorForm(request.POST)
        if form.is_valid():
            g_cust_no = form.cleaned_data['guarantor_no']
            amount    = form.cleaned_data['amount']

            try:
                guarantor_cust = Customer.objects.get(cust_no=str(g_cust_no))

                # 1. Only ACTIVE members can guarantee
                cust_status = getattr(guarantor_cust, 'customer_status', 'Active')
                if cust_status in ('Dormant', 'Deceased', 'Exited'):
                    messages.error(
                        request,
                        f"Member {guarantor_cust.full_name} cannot guarantee — "
                        f"status is '{cust_status}'. Only active members are eligible.",
                    )
                    return redirect('loans:add_guarantor', pk=pk)

                # 2. Defaulter check — a member who is currently a defaulter
                #    with arrears of more than 3 installments on ANY running
                #    loan cannot guarantee.
                defaulter_loans = RunningLoanStat.objects.filter(
                    cust_no=guarantor_cust.cust_no,
                    loan_status='Active',
                    loan_balance__gt=0,
                    defaulted_installments__gt=3,
                )
                if defaulter_loans.exists():
                    worst = defaulter_loans.order_by('-defaulted_installments').first()
                    messages.error(
                        request,
                        f"Member {guarantor_cust.full_name} cannot guarantee — "
                        f"currently a defaulter on loan {worst.loan_no} with "
                        f"{worst.defaulted_installments} month(s) in arrears "
                        f"(total arrears {worst.total_arrears:,.2f}). "
                        f"Only members with 3 months or fewer arrears are eligible.",
                    )
                    return redirect('loans:add_guarantor', pk=pk)

                # 3. Self-guarantee: allowed ONLY if the borrower has no
                #    outstanding guarantee commitments on OTHER people's loans.
                is_self = (guarantor_cust.cust_no == loan.customer.cust_no)
                if is_self:
                    open_guarantees_for_others = Guarantor.objects.filter(
                        guarantor_cust=guarantor_cust,
                    ).exclude(
                        loan__customer=guarantor_cust,   # exclude own loans
                    ).filter(
                        loan__loan_no__in=RunningLoanStat.objects.filter(
                            loan_balance__gt=0,
                        ).values_list('loan_no', flat=True),
                    )
                    if open_guarantees_for_others.exists():
                        messages.error(
                            request,
                            "Self-guarantee denied — you have outstanding guarantee "
                            "commitments on other members' unsettled loans. Clear "
                            "those first before self-guaranteeing.",
                        )
                        return redirect('loans:add_guarantor', pk=pk)

                # 3. Live metrics for THIS guarantor (excluding the current loan)
                metrics = live_guarantor_metrics(
                    guarantor_cust, exclude_loan=loan,
                )

                # 3. Amount cannot exceed deposit balance
                balance = metrics['total_balance']
                if amount > balance:
                    messages.error(
                        request,
                        f"Amount ({amount:,.2f}) exceeds guarantor's deposit "
                        f"balance ({balance:,.2f}).",
                    )
                    return redirect('loans:add_guarantor', pk=pk)

                # 4. Headroom check — uses RunningLoanStat OPEN loans only,
                #    and per-product guarantee_multiplier.
                ceiling  = metrics['guarantee_ceiling']
                committed = metrics['committed']
                remaining = metrics['remaining']

                if (committed + amount) > ceiling:
                    messages.error(
                        request,
                        f"Guarantor headroom exceeded. Ceiling {ceiling:,.2f}, "
                        f"outstanding commitments {committed:,.2f}, remaining "
                        f"{remaining:,.2f}. Requested {amount:,.2f}.",
                    )
                    return redirect('loans:add_guarantor', pk=pk)

                # 5. Save
                Guarantor.objects.create(
                    loan=loan, guarantor_cust=guarantor_cust, amount=amount,
                )

                # 6. Notify the guarantor
                from sms.services import notify, msg_guarantor_added
                if guarantor_cust.phone:
                    notify(
                        guarantor_cust.phone,
                        msg_guarantor_added(
                            guarantor_cust.first_name, loan.customer.full_name,
                            loan.loan_no, amount,
                        ),
                        created_by=request.user.username,
                    )
                messages.success(
                    request,
                    f"Guarantor {guarantor_cust.full_name} added successfully!",
                )
                return redirect('loans:add_guarantor', pk=pk)

            except Customer.DoesNotExist:
                messages.error(request, f"Customer number {g_cust_no} not found.")
            except ValueError:
                messages.error(request, "Invalid customer number format.")

    else:
        form = AddGuarantorForm()

    return render(request, 'loans/add_guarantor.html', {
        'loan':        loan,
        'form':        form,
        'guarantors':  existing_guarantors,
        'metrics_ctx': metrics_ctx,  # not used on first GET; JS fetches live
    })


@require_POST
@login_required
@min_role_required('loan_officer')
def api_guarantor_update(request, pk):
    """
    AJAX endpoint to update a guarantor's amount inline.
    Only allowed if the parent loan has NOT been approved yet.
    """
    import json
    try:
        guarantor = get_object_or_404(Guarantor, pk=pk)
        if guarantor.loan.is_approved:
            return JsonResponse({'status': 'error', 'error': 'Cannot edit — loan already approved.'}, status=403)

        data = json.loads(request.body)
        new_amount = Decimal(str(data.get('amount', '0')))
        if new_amount <= Decimal('0'):
            return JsonResponse({'status': 'error', 'error': 'Amount must be greater than zero.'}, status=400)

        guarantor.amount = new_amount
        guarantor.save(update_fields=['amount'])
        return JsonResponse({'status': 'ok', 'new_amount': str(guarantor.amount)})
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'error': 'Invalid data.'}, status=400)


@require_POST
@login_required
@min_role_required('loan_officer')
def api_guarantor_delete(request, pk):
    """
    AJAX endpoint to de-link (delete) a guarantor from a loan.
    Only allowed if the parent loan has NOT been approved yet.
    """
    guarantor = get_object_or_404(Guarantor, pk=pk)
    if guarantor.loan.is_approved:
        return JsonResponse({'status': 'error', 'error': 'Cannot remove — loan already approved.'}, status=403)

    guarantor.delete()
    return JsonResponse({'status': 'ok'})


@require_GET
@login_required
@min_role_required('accounts_clerk')
def api_guarantor_metrics(request):
    """
    Live guarantor snapshot — called by add_guarantor.html when the
    officer types a cust_no. Returns deposits balance, guarantee ceiling
    (per-product multipliers), current commitments on OPEN loans, and
    remaining headroom. This is the officer's early-warning signal.
    """
    from loans.appraisal_metrics import live_guarantor_metrics

    cust_no = (request.GET.get('cust_no') or '').strip()
    exclude_loan_id = request.GET.get('exclude_loan') or None
    if not cust_no:
        return JsonResponse({'found': False, 'error': 'cust_no required'})

    try:
        cust = Customer.objects.get(cust_no=cust_no)
    except Customer.DoesNotExist:
        return JsonResponse({'found': False, 'error': 'Customer not found'})

    exclude_loan = None
    if exclude_loan_id:
        exclude_loan = LoanHistory.objects.filter(id=exclude_loan_id).first()

    m = live_guarantor_metrics(cust, exclude_loan=exclude_loan)

    # Customer status check
    cust_status = getattr(cust, 'customer_status', 'Active')
    is_active_member = cust_status not in ('Dormant', 'Deceased', 'Exited')

    # Defaulter check — arrears > 3 installments blocks guaranteeing
    defaulter_loan = RunningLoanStat.objects.filter(
        cust_no=cust.cust_no,
        loan_balance__gt=0,
        defaulted_installments__gt=3,
        ).exclude(
    loan_status='Settled'
    ).order_by('-defaulted_installments').first()

    is_defaulter = defaulter_loan is not None
    defaulter_detail = ''
    if is_defaulter:
        defaulter_detail = (
            f"Defaulter on {defaulter_loan.loan_no} — "
            f"{defaulter_loan.defaulted_installments} month(s) arrears "
            f"(KES {defaulter_loan.total_arrears:,.2f})"
        )

    return JsonResponse({
        'found':             True,
        'cust_no':           cust.cust_no,
        'full_name':         cust.full_name,
        'phone':             cust.phone or '',
        'customer_status':   cust_status,
        'is_active_member':  is_active_member,
        'is_defaulter':      is_defaulter,
        'defaulter_detail':  defaulter_detail,
        'total_balance':     float(m['total_balance']),
        'guarantee_ceiling': float(m['guarantee_ceiling']),
        'committed':         float(m['committed']),
        'remaining':         float(m['remaining']),
        'has_capacity':      m['has_capacity'],
        'per_product': [
            {'label':      row['label'],
             'balance':    float(row['balance']),
             'multiplier': float(row['multiplier']),
             'capacity':   float(row['capacity'])}
            for row in m['per_product']
        ],
    })

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q
from django.shortcuts import render

@login_required
@min_role_required('accounts_clerk')
def view_guarantors(request):
    query = request.GET.get('q', '').strip()
    
    if query:
        # Search logic across related models
        guarantors_list = Guarantor.objects.filter(
            Q(loan__id__icontains=query) | 
            Q(loan__loan_no__icontains=query) |
            Q(guarantor_cust__full_name__icontains=query) |
            Q(guarantor_cust__national_id__icontains=query) |
            Q(guarantor_cust__cust_no__icontains=query)
        ).select_related('loan', 'guarantor_cust', 'loan__customer').order_by('-loan__id')
    else:
        # Default logic: Fetch all ordered deterministically for regrouping
        guarantors_list = Guarantor.objects.select_related(
            'loan', 'guarantor_cust', 'loan__customer'
        ).order_by('-loan__id')

    # Setup Paginator (e.g., 20 guarantor records per page)
    paginator = Paginator(guarantors_list, 20)
    page_number = request.GET.get('page')
    
    try:
        guarantors = paginator.page(page_number)
    except PageNotAnInteger:
        # If page number is not an integer, fall back to page 1
        guarantors = paginator.page(1)
    except EmptyPage:
        # If page is out of bounds, deliver last page of results
        guarantors = paginator.page(paginator.num_pages)

    return render(request, 'loans/view_guarantors.html', {
        'guarantors': guarantors,
        'query': query
    })

@transaction.atomic
@login_required
@min_role_required('loan_officer')
def replace_guarantor(request, pk):
    old_guarantor = get_object_or_404(Guarantor, id=pk)
    loan = old_guarantor.loan
    
    if request.method == 'POST':
        form = ReplaceGuarantorForm(request.POST)
        if form.is_valid():
            new_cust_no = form.cleaned_data['new_guarantor_no']
            amount = old_guarantor.amount # We are swapping the exact amount
            
            try:
                # 1. Get New Guarantor
                new_cust = Customer.objects.get(cust_no=new_cust_no)
                
                # 2. Self-Guarantee Check
                if new_cust.cust_no == loan.customer.cust_no:
                    messages.error(request, "Borrower cannot be their own guarantor.")
                    return redirect('loans:replace_guarantor', guarantor_id=pk)

                # 3. Check Balance Limits (Reusing your logic)
                balance = calculate_savings_balance(new_cust.cust_no)
                if amount > balance:
                    messages.error(request, f"New guarantor insufficient balance. Need {amount}, Has {balance}.")
                    return redirect('loans:replace_guarantor', guarantor_id=pk)

                current_guarantees_agg = Guarantor.objects.filter(guarantor_cust=new_cust).aggregate(total=Sum('amount'))
                current_total = current_guarantees_agg['total'] or Decimal(0)
                
                if (current_total + amount) > (balance * 3):
                    messages.error(request, f"Limit Reached. New guarantor cannot cover this amount.")
                    return redirect('loans:replace_guarantor', guarantor_id=pk)

                # 4. Perform Swap
                # Optional: Log this swap in a history table if needed
                
                # Create new
                Guarantor.objects.create(loan=loan, guarantor_cust=new_cust, amount=amount)
                
                # Delete old
                old_cust = old_guarantor.guarantor_cust
                old_name = old_cust.full_name
                old_guarantor.delete()

                # Notify both parties — always logged to SMSLog
                from sms.services import notify, msg_guarantor_added, msg_guarantor_released
                if new_cust.phone:
                    notify(
                        new_cust.phone,
                        msg_guarantor_added(new_cust.first_name, loan.customer.full_name, loan.loan_no, amount),
                        created_by=request.user.username,
                    )
                if old_cust.phone:
                    notify(
                        old_cust.phone,
                        msg_guarantor_released(old_cust.first_name, loan.loan_no),
                        created_by=request.user.username,
                    )
                
                messages.success(request, f"Successfully replaced {old_name} with {new_cust.full_name}")
                return redirect('loans:view_guarantors')

            except Customer.DoesNotExist:
                messages.error(request, "New Member number not found.")
            except ValueError:
                messages.error(request, "Invalid format.")

    else:
        form = ReplaceGuarantorForm()

    return render(request, 'loans/replace_guarantor.html', {
        'form': form, 
        'old_guarantor': old_guarantor
    })

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q
from django.shortcuts import render

@login_required
@min_role_required('accounts_clerk')
def view_collaterals(request):
    query = request.GET.get('q', '').strip()
    
    if query:
        # Search across related loan information and asset identifiers
        collaterals_list = Collateral.objects.filter(
            Q(loan__id__icontains=query) |
            Q(loan__loan_no__icontains=query) |
            Q(owner__full_name__icontains=query) |
            Q(registration_no__icontains=query) |
            Q(title_deed_no__icontains=query)
        ).select_related('loan', 'owner').order_by('-created_at')
    else:
        # Default: Fetch all records ordered by creation date
        collaterals_list = Collateral.objects.select_related('loan', 'owner').order_by('-created_at')
        
    # Initialize Paginator - e.g., 15 items per page
    paginator = Paginator(collaterals_list, 15)
    page_number = request.GET.get('page')
    
    try:
        collaterals = paginator.page(page_number)
    except PageNotAnInteger:
        # If page variable is not an integer, default to the first page.
        collaterals = paginator.page(1)
    except EmptyPage:
        # If page out of range, deliver last page of results.
        collaterals = paginator.page(paginator.num_pages)
        
    return render(request, 'loans/view_collaterals.html', {
        'collaterals': collaterals,
        'query': query
    })

@login_required
@min_role_required('accounts_clerk')
def view_collateral_details(request, pk):
    collateral = get_object_or_404(Collateral.objects.select_related('loan', 'owner'), pk=pk)
    return render(request, 'loans/view_collateral_details.html', {'collateral': collateral})

@transaction.atomic
@login_required
@min_role_required('loan_officer')
def add_collateral(request, pk):
    loan = get_object_or_404(LoanHistory, id=pk)
    
    if request.method == 'POST':
        form = CollateralForm(request.POST)
        if form.is_valid():
            collateral = form.save(commit=False)
            collateral.loan = loan
            collateral.owner = loan.customer # Default owner is borrower
            collateral.created_by = request.user.username
            collateral.save()
            
            messages.success(request, "Collateral Added Successfully")
            return redirect('loans:view_collaterals')
    else:
        form = CollateralForm()

    return render(request, 'loans/add_collateral.html', {'form': form, 'loan': loan})

import json
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
 

@login_required
@min_role_required('accounts_clerk')
def view_loan_details(request, pk):
    # Fetch loan with customer data
    loan = get_object_or_404(LoanHistory.objects.select_related('customer', 'loan_type'), pk=pk)
    
    # Fetch related security
    collaterals = Collateral.objects.filter(loan=loan)
    guarantors = Guarantor.objects.filter(loan=loan).select_related('guarantor_cust')
    
    # Calculate coverage
    total_guaranteed = sum(g.amount for g in guarantors)
    total_collateral = sum(c.market_value for c in collaterals)
    
    # ─── PARSE RESTURCTURE SUMMARY METADATA ─────────────────────────
    restructure_meta = None
    if loan.is_restructured and loan.original_loan_summary:
        try:
            restructure_meta = json.loads(loan.original_loan_summary)
        except (json.JSONDecodeError, TypeError):
            restructure_meta = None

    return render(request, 'loans/view_loan_details.html', {
        'loan': loan,
        'collaterals': collaterals,
        'guarantors': guarantors,
        'total_guaranteed': total_guaranteed,
        'total_collateral': total_collateral,
        'total_security': total_guaranteed + total_collateral,
        'restructure_meta': restructure_meta,  # Passed to template
    })
@login_required
@min_role_required('loan_officer')
def edit_collateral(request, pk):
    collateral = get_object_or_404(Collateral, pk=pk)
    
    if request.method == 'POST':
        form = CollateralForm(request.POST, instance=collateral)
        if form.is_valid():
            form.save()
            messages.success(request, "Collateral updated successfully.")
            return redirect('loans:view_collaterals')
    else:
        form = CollateralForm(instance=collateral)
        
    return render(request, 'loans/edit_collateral.html', {
        'form': form,
        'collateral': collateral
    })
"""
Loan interest charging — three-table strategy.

ROLE OF EACH TABLE
------------------
  RunningLoanStat  -> WHICH loans are active. We never charge a settled loan.
                      Filter only; we do NOT trust its balance for a past date.
  LoanHistory      -> CROSS-CHECK that the loan is really disbursed
                      (is_disbursed=True) and the id<->loan_no<->cust_no map.
  LoanTransaction  -> THE BALANCE as at the target date. Sum of debits minus
                      credits with tr_date <= target_date. Backdated-safe:
                      repayments made after target_date are simply not summed.

The candidate set = active (stat) intersect disbursed (history). Each
candidate's balance is computed from the ledger as at the target date. This is
correct for today's runs and for backdated runs alike, because the ledger is
summed to a date, not read from a snapshot.

GUARDRAILS
----------
  * No future-dated runs (a ledger can't answer "what will the balance be").
  * Duplicate guard keyed on (loan_type, target_date) so a backdated
    correction can't double-charge a date already posted.
  * Everything set-based + bulk. Posting streams progress over SSE.
"""

import json
from decimal import Decimal, ROUND_HALF_UP

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum
from django.http import StreamingHttpResponse
from django.shortcuts import render, redirect
from django.utils import timezone

# adjust to your project layout
from .forms import InterestChargeForm
from .models import (
    LoanHistory,
    LoanTransaction,
    RunningLoanStat,
    InterestChargeBatch,
    InterestChargeDraftItem,
    CustomerAccountsSetup,
)
from accounting.models import SaccoAccount, SaccoAccountsLedger, SaccoAccountBalance

TWO_PLACES = Decimal("0.01")

# Interest account codes now sourced from the central GL registry.
# No more hardcoded 900-xxx strings — if the chart of accounts changes,
# update GL_CODE_OVERRIDES in settings or the SaccoAccount table.
from accounting.services import GL as _GL

ACCOUNT_MAP = {
    "normal_loan":      _GL.INTEREST_INCOME,   # Normal Loan Interest
    "repsi_loan":       _GL.INTEREST_INCOME,   # alias for normal
    "phone_loan":       _GL.MOBILE_INTEREST,   # Mobile Loan Interest
    "development_loan": _GL.DEV_LOAN_INT,      # Development Loan Interest
    "school_fees_loan": _GL.EMERGENCY_INT,     # Emergency Loan Interest
    "emergency_loan":   _GL.INSTANT_INT,       # Dividend Advance Interest
}


def _q(amount):
    return Decimal(str(amount or 0)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


SETTLED_STATUSES = {"Settled"}  # add any other terminal statuses here


def _candidate_loans(loan_type_slug):
    """
    disbursed-for-this-product (LoanHistory) MINUS settled (RunningLoanStat).

    Returns a dict keyed by loan_id:
      {loan_id: {"loan_no", "cust_no", "principal"}}

    WHY THIS SHAPE
    --------------
    LoanHistory is the authority on product + disbursement, because its
    loan_type FK (loan_type_id) points straight at CustomerAccountsSetup, so
    `loan_type__account_type=slug` is an exact, reliable match. We do NOT match
    the product on RunningLoanStat.product_code -- that column is free text and
    may hold a code/name, not the slug, which is what produced the empty set.

    RunningLoanStat is used ONLY to drop loans that are finished. A loan is
    live unless its status is terminal (e.g. "Settled"); aging statuses like
    "Normal", "Watch", "Substandard" are all still active, so we exclude by the
    terminal set rather than filtering on a literal "Active".

    The two tables join on loan_no -- the one key they reliably share.
    """
    history = (
        LoanHistory.objects
        .filter(loan_type__account_type=loan_type_slug, is_disbursed=True)
        .values("id", "loan_no", "customer_id", "principal")
    )
    history = list(history)
    if not history:
        return {}

    loan_nos = [h["loan_no"] for h in history]

    # loan_nos that are finished -> drop them. Anything not in this set
    # (including loans with no stat row yet) is treated as still live.
    settled_nos = set(
        RunningLoanStat.objects
        .filter(loan_no__in=loan_nos, loan_status__in=SETTLED_STATUSES)
        .values_list("loan_no", flat=True)
    )

    return {
        h["id"]: {
            "loan_no": h["loan_no"],
            "cust_no": h["customer_id"],
            "principal": _q(h["principal"]),
        }
        for h in history
        if h["loan_no"] not in settled_nos
    }


def _balances_asof(loan_ids, target_date):
    """
    True outstanding balance for each loan AS AT target_date, from the ledger.

    Sums debits and credits with tr_date <= end of target_date. A repayment
    posted after target_date is not in the window, so the balance reflects the
    date asked for -- not today. Returns {loan_id: Decimal}.

    NOTE: we scope ONLY by loan_id. We deliberately do NOT also filter on
    LoanTransaction.loan_type -- that column is free text and may not hold the
    slug (it was the cause of the empty result). The loan_ids already came from
    LoanHistory filtered to this exact product, so they are the correct set;
    re-matching the string adds nothing but a way to drop valid rows.

    The date bound uses a datetime upper bound (end of target_date in the
    active timezone) rather than __date__lte, so a tz-aware tr_date near
    midnight can't be shifted out of the window by a UTC date rollover.
    """
    from datetime import datetime, time
    upper = timezone.make_aware(datetime.combine(target_date, time.max)) \
        if timezone.is_aware(timezone.now()) else datetime.combine(target_date, time.max)

    rows = (
        LoanTransaction.objects
        .filter(loan_id__in=loan_ids, tr_date__lte=upper)
        .values("loan_id")
        .annotate(debit=Sum("debit_amount"), credit=Sum("credit_amount"))
    )
    return {
        r["loan_id"]: _q((r["debit"] or 0) - (r["credit"] or 0))
        for r in rows
    }


@login_required
@min_role_required("loan_officer")
def interest_charge(request):
    form = InterestChargeForm(request.POST or None)
    active_draft_batch = None

    if request.method == "POST" and form.is_valid():
        target_date = form.cleaned_data["date"]
        loan_type = form.cleaned_data["loan_type"]
        rate = Decimal(str(form.cleaned_data["interest_rate"]))
        action = request.POST.get("action")

        # Guardrail 1: no future-dated runs.
        if target_date > timezone.localdate():
            messages.error(request, "Cannot charge interest for a future date.")
            return redirect(request.path)

        # Guardrail 2: duplicate guard keyed on (loan_type, target_date).
        if InterestChargeBatch.objects.filter(
            loan_type=loan_type, target_date=target_date, status="posted"
        ).exists():
            messages.error(
                request,
                f"Interest for {loan_type.replace('_', ' ')} on {target_date} "
                f"has already been posted.",
            )
            return redirect(request.path)

        if action == "calculate":
            active_draft_batch = _build_draft(request, loan_type, target_date, rate)

        elif action == "post":
            batch_id = request.POST.get("batch_id")
            batch = _locate_postable_batch(batch_id, loan_type, target_date)
            if not batch:
                messages.error(request, "No valid draft batch found to post.")
            else:
                return render(request, "loans/interest_charge.html", {
                    "form": form,
                    "recent_batches": _recent(),
                    "stream_batch_id": batch.id,
                })

    return render(request, "loans/interest_charge.html", {
        "form": form,
        "recent_batches": _recent(),
        "active_draft_batch": active_draft_batch,
    })


def _build_draft(request, loan_type_slug, target_date, rate):
    account_setup = CustomerAccountsSetup.objects.filter(
        account_type=loan_type_slug, is_active=True
    ).first()
    if not account_setup:
        messages.error(request, f"No active account setup for: {loan_type_slug}")
        return None

    calc_method = account_setup.interest_calc_method or "reducing_balance"

    # active intersect disbursed
    candidates = _candidate_loans(loan_type_slug)
    if not candidates:
        messages.warning(
            request,
            "No live disbursed loans found for this product. Checked loan "
            "history (disbursed for this product) minus settled loans.",
        )
        return None

    # as-at-date balances from the ledger
    balances = _balances_asof(candidates.keys(), target_date)
    if not balances:
        messages.warning(
            request,
            f"No ledger balances found as at {target_date}. Loans may have no "
            f"transactions on or before that date.",
        )
        return None

    customer_names = _name_map([c["cust_no"] for c in candidates.values()])

    try:
        with transaction.atomic():
            batch = InterestChargeBatch.objects.create(
                loan_type=loan_type_slug,
                target_date=target_date,
                interest_rate=rate,
                calc_method_used=calc_method,
                created_by=request.user,
                status="draft",
            )

            rate_factor = rate / Decimal("100")
            items = []
            running_total = Decimal("0.00")

            for loan_id, meta in candidates.items():
                balance = balances.get(loan_id, Decimal("0.00"))
                if balance <= 0:
                    continue  # fully repaid as at this date -- nothing to charge

                base = meta["principal"] if calc_method == "flat_rate" else balance
                interest = _q(base * rate_factor)
                if interest <= 0:
                    continue

                running_total += interest
                items.append(InterestChargeDraftItem(
                    batch=batch,
                    loan_id=loan_id,
                    loan_no=meta["loan_no"],
                    cust_no=meta["cust_no"],
                    customer_name=customer_names.get(meta["cust_no"], "Unknown"),
                    approved_amount=meta["principal"],
                    outstanding_balance=balance,
                    calculated_interest=interest,
                ))

            if not items:
                transaction.set_rollback(True)
                messages.warning(
                    request,
                    f"All candidate loans had zero balance as at {target_date}; "
                    f"nothing to charge.",
                )
                return None

            InterestChargeDraftItem.objects.bulk_create(items, batch_size=1000)
            batch.total_interest = running_total
            batch.save(update_fields=["total_interest"])

            messages.success(
                request,
                f"Draft batch #{batch.id} ready: {len(items)} loans, "
                f"KES {running_total:,.2f} interest as at {target_date}.",
            )
            return batch

    except Exception as e:
        messages.error(request, f"Draft build failed: {e}")
        return None


def _name_map(cust_nos):
    """cust_no -> full_name, one query. Adjust model/field to your Customer."""
    from customers.models import Customer
    rows = Customer.objects.filter(cust_no__in=cust_nos).values("cust_no", "full_name")
    return {r["cust_no"]: r["full_name"] for r in rows}


def _locate_postable_batch(batch_id, loan_type, target_date):
    if batch_id:
        return InterestChargeBatch.objects.filter(id=batch_id, status="draft").first()
    return (
        InterestChargeBatch.objects
        .filter(loan_type=loan_type, target_date=target_date, status="draft")
        .order_by("-id")
        .first()
    )


def _recent():
    return InterestChargeBatch.objects.all().order_by("-id")[:10]


@login_required
@min_role_required("loan_officer")
def interest_charge_stream(request, batch_id):
    """SSE: commit the batch and emit progress frames as we work."""

    def event(payload):
        return f"data: {json.dumps(payload)}\n\n"

    def run():
        batch = InterestChargeBatch.objects.filter(id=batch_id, status="draft").first()
        if not batch:
            yield event({"type": "error", "message": "Draft not found or already posted."})
            return

        # Re-check the duplicate guard at post time (the draft may be old).
        if InterestChargeBatch.objects.filter(
            loan_type=batch.loan_type, target_date=batch.target_date, status="posted"
        ).exists():
            yield event({"type": "error",
                         "message": f"{batch.target_date} already posted for this product."})
            return

        items = list(batch.draft_items.all())
        if not items:
            yield event({"type": "error", "message": "Draft has no items."})
            return

        account_code = ACCOUNT_MAP.get(batch.loan_type)
        income_account = SaccoAccount.objects.filter(account_code=account_code).first()
        if not income_account:
            yield event({"type": "error", "message": f"GL account {account_code} not configured."})
            return

        total = len(items)
        yield event({"type": "start", "total": total, "batch": batch.id,
                     "amount": float(batch.total_interest or 0),
                     "date": str(batch.target_date)})

        batch_ref = f"INT-{batch.loan_type[:3].upper()}-{timezone.now():%y%m%d%H%M}"
        method_tag = batch.calc_method_used.split("_")[0].upper()
        desc = f"{batch.target_date:%B %Y} Interest ({method_tag}) @{batch.interest_rate}%"

        try:
            with transaction.atomic():
                # 1. Accrued-interest debit rows into the ledger, chunked.
                rows = [
                    LoanTransaction(
                        cust_no=it.cust_no,
                        loan_id=it.loan_id,
                        loan_no=it.loan_no,
                        loan_type=batch.loan_type,
                        tr_date=batch.target_date,
                        tr_ref=batch_ref,
                        tr_desc=desc,
                        debit_amount=it.calculated_interest,
                        credit_amount=0,
                        created_by=request.user,
                    )
                    for it in items
                ]
                CHUNK = 500
                inserted = 0
                for i in range(0, len(rows), CHUNK):
                    LoanTransaction.objects.bulk_create(rows[i:i + CHUNK], batch_size=CHUNK)
                    inserted += len(rows[i:i + CHUNK])
                    yield event({"type": "progress", "stage": "transactions",
                                 "done": inserted, "total": total})

                # 2. Refresh RunningLoanStat (display layer) in bulk.
                stat_by_no = {
                    s.loan_no: s
                    for s in RunningLoanStat.objects.filter(
                        loan_no__in=[it.loan_no for it in items]
                    )
                }
                to_update = []
                for it in items:
                    s = stat_by_no.get(it.loan_no)
                    if not s:
                        continue
                    s.interest_balance = _q(s.interest_balance + it.calculated_interest)
                    s.total_arrears = _q(s.total_arrears + it.calculated_interest)
                    s.last_interest_charge = batch.target_date
                    to_update.append(s)
                if to_update:
                    RunningLoanStat.objects.bulk_update(
                        to_update,
                        ["interest_balance", "total_arrears", "last_interest_charge"],
                        batch_size=500,
                    )
                yield event({"type": "progress", "stage": "running_stats",
                             "done": len(to_update), "total": total})

                # 3. Balanced GL: DR Loans Receivable, CR Interest Income
                from accounting.journal import journal_entry, leg as _jleg
                from django.conf import settings as _s
                _codes = getattr(_s, "DIVIDEND_GL_CODES", {})
                loans_acc = SaccoAccount.objects.filter(
                    account_code=_codes.get("loans_receivable", _GL.LOANS_RECEIVABLE)
                ).first()
                if not loans_acc:
                    raise ValueError(
                        "Loans Receivable GL account "
                        f"({_codes.get('loans_receivable', '900-630011')}) "
                        "not configured."
                    )
                journal_entry(
                    reference=batch_ref,
                    description=(f"{batch.loan_type.replace('_', ' ').title()} interest "
                                 f"batch #{batch.id} {batch.target_date:%b %Y}"),
                    created_by=request.user.username if hasattr(request.user, 'username') else 'system',
                    legs=[
                        _jleg(loans_acc,     debit=batch.total_interest),
                        _jleg(income_account, credit=batch.total_interest),
                    ],
                )
                # NOTE: PG balance cache is now updated inside journal_entry()
                # and TB is written first (authoritative). No manual update needed.
                # (Previously this was a double-update bug causing PG balance drift.)

                batch.status = "posted"
                batch.posted_at = timezone.now()
                batch.save(update_fields=["status", "posted_at"])

            yield event({"type": "done", "batch": batch.id, "total": total,
                         "amount": float(batch.total_interest or 0), "ref": batch_ref})

        except Exception as e:
            yield event({"type": "error", "message": f"Posting failed and was rolled back: {e}"})

    response = StreamingHttpResponse(run(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response

@login_required
@min_role_required('loan_officer')
def interest_batch_list(request):
    """Lists history entries for all calculated interest batches."""
    batches_queryset = InterestChargeBatch.objects.all().order_by('-id')
    
    paginator = Paginator(batches_queryset, 20)  # Shows 20 history batches per dashboard grid index
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    return render(request, "loans/interest_batch_list.html", {
        "page_obj": page_obj
    })


@login_required
@min_role_required('loan_officer')
def interest_batch_detail(request, batch_id):
    """Displays calculation detail items for a specific batch with pagination."""
    batch = get_object_or_404(InterestChargeBatch, id=batch_id)
    items_queryset = batch.draft_items.all().order_by('loan_no')
    
    paginator = Paginator(items_queryset, 50)  # Shows 50 items per view segment pagination window
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    return render(request, "loans/interest_batch_detail.html", {
        "batch": batch,
        "page_obj": page_obj
    })

"""
view_appraisal — additions summary
===================================
• Adds 6-month pivot calculations for savings deposits and loan repayments
  BEFORE the PDF branch so both HTML and PDF share the same data.
• Saves/deposit pivot groups by CustomerAccountsSetup (account_code + account_name).
• Loan-repayment pivot groups by loan_no + loan_type for every existing loan.
• Both pivots appear in the HTML template AND in the ReportLab PDF story.

Field-name assumption
---------------------
SavingsTransaction is assumed to have:
    cust_no         CharField
    tr_date         DateField / DateTimeField     ← used for month bucketing
    credit_amount   DecimalField
    debit_amount    DecimalField
    account         ForeignKey → CustomerAccountsSetup   ← drives label & filter

If your model uses a plain account_code CharField instead of an FK, replace
`account__account_code` / `account__account_name` with your actual field names
and remove the `account__account_type` filter (or adapt it accordingly).
"""

import io
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from dateutil.relativedelta import relativedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils.timezone import now

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
)
from reportlab.platypus import Image as RLImage

"""
view_appraisal — 6-month pivot tables
======================================
• SavingsTransaction uses `saving_type` (CharField, e.g. 'savings_deposit').
  There is NO FK to CustomerAccountsSetup — labels are resolved via a
  lookup dict keyed on account_type.
• LoanTransaction uses `loan_no` and `loan_type` (plain CharFields).
• Both pivot tables appear in the HTML template AND in the ReportLab PDF.
"""

import io
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from dateutil.relativedelta import relativedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils.timezone import now

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
)
from reportlab.platypus import Image as RLImage


def classify_repayment_score(score):
    """Classifies customer based on actual historical repayment rate."""
    if score == 'N/A':
        return "New Borrower (No History)"
    elif score >= 85:
        return "Excellent (Low Risk)"
    elif score >= 60:
        return "Fair (Moderate Risk)"
    elif score >= 40:
        return "Risky (Watch Closely)"
    else:
        return "High Risk (Likely Default)"


# ─── tiny helper ──────────────────────────────────────────────────────────────
def _month_str(dt):
    """Return 'YYYY-MM' string from date or datetime object safely."""
    return dt.strftime('%Y-%m') if hasattr(dt, 'strftime') else str(dt)[:7]


@login_required
def view_appraisal(request, pk):
    # ── Fetch core records ────────────────────────────────────────────────────
    loan     = get_object_or_404(LoanHistory.objects.select_related('customer'), pk=pk)
    customer = loan.customer

    # ── Charge recoveries (all charges go through LoanChargeRecovery now) ────
    charge_recoveries  = LoanChargeRecovery.objects.filter(loan=loan).select_related('charge')
    total_charges      = safe_decimal(charge_recoveries.aggregate(total=Sum('amount'))['total'])

    # ── Offset / bridging loan payoffs ────────────────────────────────────
    total_offset_amount = safe_decimal(loan.total_offset_amount)
    offset_data         = loan.offset_data or []

    # Total deductions = charges + offset payoffs
    total_deductions = total_charges + total_offset_amount
    net_disbursed    = loan.principal - total_deductions

    # ══════════════════════════════════════════════════════════════════════════
    # 1. ACTUAL FINANCIAL BALANCES
    # ══════════════════════════════════════════════════════════════════════════
    savings_aggr = SavingsTransaction.objects.filter(cust_no=customer.cust_no).aggregate(
        bal=Sum('credit_amount') - Sum('debit_amount')
    )['bal']
    savings_balance       = safe_decimal(savings_aggr)
    share_capital_balance = Decimal('0.00')

    # ══════════════════════════════════════════════════════════════════════════
    # 2. EXISTING LOANS & OUTSTANDING DEBT
    # ══════════════════════════════════════════════════════════════════════════
    existing_loans         = LoanHistory.objects.filter(customer_id=customer.cust_no).exclude(pk=loan.pk)
    loan_balances          = []
    total_outstanding_debt = Decimal('0.00')

    for el in existing_loans:
        bal_aggr = LoanTransaction.objects.filter(loan_id=el.id).aggregate(
            bal=Sum('debit_amount') - Sum('credit_amount')
        )['bal']
        bal = safe_decimal(bal_aggr)
        if bal > 0:
            total_outstanding_debt += bal
            loan_balances.append({
                'id':        el.id,
                'loan_no':   el.loan_no or f"LN-{el.id}",
                'type':      el.get_loan_type_display(),
                'principal': el.principal,
                'balance':   bal,
                'loan_date': el.loan_date,
            })

    # ══════════════════════════════════════════════════════════════════════════
    # 3. SIX-MONTH PIVOT TABLES
    #    Reference date  → loan.loan_date (day-of-month normalised to 1st)
    #    Window          → 6 months ending in the month of application
    # ══════════════════════════════════════════════════════════════════════════
    ref_date = loan.loan_date

    # Build ordered list of 6 month-start dates  [oldest … newest]
    pivot_months = [
        ref_date.replace(day=1) - relativedelta(months=i)
        for i in range(5, -1, -1)
    ]
    pivot_window_start = pivot_months[0]
    pivot_window_end   = pivot_months[-1] + relativedelta(months=1)   # exclusive

    # Human-readable labels:  "JAN 2026", "FEB 2026", …
    month_labels      = [m.strftime('%b %Y').upper() for m in pivot_months]
    pivot_month_strs  = [m.strftime('%Y-%m') for m in pivot_months]   # for dict keying

    # ── 3a. Savings Deposit Pivot ─────────────────────────────────────────────
    #
    # SavingsTransaction.saving_type is a plain CharField whose values match
    # CustomerAccountsSetup.account_type (e.g. 'savings_deposit').
    # Build a lookup dict once so labels show "S01 - SAVINGS DEPOSIT".
    #
    _account_label_map = {
        acct.account_type: f"{acct.account_code} - {acct.account_name.upper()}"
        for acct in CustomerAccountsSetup.objects.filter(is_active=True).only(
            'account_type', 'account_code', 'account_name'
        )
    }

    savings_agg = (
        SavingsTransaction.objects
        .filter(
            cust_no=customer.cust_no,
            saving_type='savings_deposit',         # direct CharField filter — no FK
            tr_date__gte=pivot_window_start,
            tr_date__lt=pivot_window_end,
        )
        .annotate(month=TruncMonth('tr_date'))
        .values('month', 'saving_type')            # group on the CharField itself
        .annotate(net_amount=Sum('credit_amount') - Sum('debit_amount'))
        .order_by('saving_type', 'month')
    )

    _savings_raw = {}
    for row in savings_agg:
        stype = row['saving_type'] or 'savings_deposit'
        # Resolve "savings_deposit" → "S01 - SAVINGS DEPOSIT"; tidy fallback
        label = _account_label_map.get(stype, stype.replace('_', ' ').upper())
        if label not in _savings_raw:
            _savings_raw[label] = {}
        _savings_raw[label][_month_str(row['month'])] = safe_decimal(row['net_amount'])

    savings_pivot = [
        {
            'label':  label,
            'values': [_savings_raw[label].get(ms, Decimal('0')) for ms in pivot_month_strs],
        }
        for label in _savings_raw
    ]

    # ── 3b. Loan Repayments Pivot ─────────────────────────────────────────────
    #
    # Shows credit_amount (repayment) per existing loan per month.
    # Uses loan_id__in to scope to loans belonging to this customer.
    #
    existing_loan_ids = [el.id for el in existing_loans]

    loan_repay_agg = (
        LoanTransaction.objects
        .filter(
            loan_id__in=existing_loan_ids,
            tr_date__gte=pivot_window_start,
            tr_date__lt=pivot_window_end,
            credit_amount__gt=0,
        )
        .annotate(month=TruncMonth('tr_date'))
        .values('month', 'loan_no', 'loan_type')
        .annotate(total_repaid=Sum('credit_amount'))
        .order_by('loan_no', 'month')
    )

    _repay_raw = {}
    for row in loan_repay_agg:
        loan_no   = row['loan_no'] or 'UNKNOWN'
        loan_type = (row['loan_type'] or 'LOAN').upper()
        label = f"{loan_no} - {loan_type}"
        if label not in _repay_raw:
            _repay_raw[label] = {}
        _repay_raw[label][_month_str(row['month'])] = safe_decimal(row['total_repaid'])

    # Also include existing loans that had NO transactions (show all-zero row)
    for lb in loan_balances:
        label = f"{lb['loan_no']} - {lb['type'].upper()}"
        if label not in _repay_raw:
            _repay_raw[label] = {}

    loan_repayment_pivot = [
        {
            'label':  label,
            'values': [_repay_raw[label].get(ms, Decimal('0')) for ms in pivot_month_strs],
        }
        for label in _repay_raw
    ]

    # ── 3c. Compress to "last 2 distinct" rows per pivot for the report ──────
    # Keep the 6-month window intact; slice down to the two most recent
    # distinct accounts (savings) and loan refs (repayments).
    savings_pivot        = savings_pivot[-2:]
    loan_repayment_pivot = loan_repayment_pivot[-2:]

    # ══════════════════════════════════════════════════════════════════════════
    # 4. SECURITY & COVERAGE ANALYSIS
    # ══════════════════════════════════════════════════════════════════════════
    guarantors      = Guarantor.objects.filter(loan=loan).select_related('guarantor_cust')
    total_guaranteed = safe_decimal(guarantors.aggregate(Sum('amount'))['amount__sum'])

    # ── Annotate each guarantor with their base-deposit savings balance ──
    # Uses the same deposit types that back the loan product's guarantee
    # multiplier (via base_deposits M2M or fallback to 'savings_deposit').
    from loans.appraisal_metrics import _resolve_base_deposit_types
    _base_types = _resolve_base_deposit_types(loan)
    for g in guarantors:
        _g_bal = SavingsTransaction.objects.filter(
            cust_no=g.guarantor_cust.cust_no,
            saving_type__in=_base_types,
        ).aggregate(
            bal=Sum('credit_amount') - Sum('debit_amount')
        )['bal']
        g.savings_balance = safe_decimal(_g_bal)

    collaterals      = Collateral.objects.filter(loan=loan)
    total_collateral = safe_decimal(collaterals.aggregate(Sum('forced_sale_value'))['forced_sale_value__sum'])
    total_security   = total_guaranteed + total_collateral

    security_coverage_percent = (
        (total_security / loan.principal) * Decimal('100')
        if loan.principal > 0 else Decimal('0.00')
    )

    # ══════════════════════════════════════════════════════════════════════════
    # 5. TRUE REPAYMENT SCORE
    # ══════════════════════════════════════════════════════════════════════════
    past_tx = LoanTransaction.objects.filter(cust_no=customer.cust_no).exclude(loan_id=loan.id)
    total_historical_debts = safe_decimal(past_tx.aggregate(Sum('debit_amount'))['debit_amount__sum'])
    total_historical_paid  = safe_decimal(past_tx.aggregate(Sum('credit_amount'))['credit_amount__sum'])

    if total_historical_debts > 0:
        historical_repayment_rate = (total_historical_paid / total_historical_debts) * Decimal('100')
        repayment_score           = round(historical_repayment_rate, 2)
        classification            = classify_repayment_score(repayment_score)
    else:
        repayment_score = 'N/A'
        classification  = "New (No History)"

    # ══════════════════════════════════════════════════════════════════════════
    # 6. EXPOSURE LIMIT POLICY CHECK
    # ══════════════════════════════════════════════════════════════════════════
    max_loan_limit  = savings_balance * Decimal('3')
    total_exposure  = total_outstanding_debt + loan.principal
    limit_status    = "Pass" if total_exposure <= max_loan_limit else "Fail (Exceeds 3x Limit)"

    # ══════════════════════════════════════════════════════════════════════════
    # 6b. NEW APPRAISAL METRICS (lumpsum, deposits, repayments, defaulter,
    #     eligibility, verdicts) — everything below is pulled from
    #     loans.appraisal_metrics, which is settings-driven and safe on
    #     empty data.
    # ══════════════════════════════════════════════════════════════════════════
    from loans.appraisal_metrics import (
        compute_lumpsum_metrics, compute_deposits_summary,
        compute_repayments_summary, fetch_defaulter_history,
        compute_eligibility, build_appraisal_verdicts,
    )

    lumpsum_metrics      = compute_lumpsum_metrics(customer, loan)
    deposits_summary     = compute_deposits_summary(customer, loan)
    repayments_summary   = compute_repayments_summary(customer, loan)
    defaulter_summary    = fetch_defaulter_history(customer)
    eligibility_summary  = compute_eligibility(customer, loan)

    appraisal_verdicts = build_appraisal_verdicts(
        customer, loan,
        lumpsum=lumpsum_metrics,
        deposits=deposits_summary,
        repayments=repayments_summary,
        defaulter=defaulter_summary,
        eligibility=eligibility_summary,
        total_outstanding_debt=total_outstanding_debt,
        total_security=total_security,
        repayment_score=repayment_score,
    )

    # ══════════════════════════════════════════════════════════════════════════
    # 7. ENGINE SWITCH: REPORTLAB PDF GENERATION — PROFESSIONAL LAYOUT
    # ══════════════════════════════════════════════════════════════════════════
    from administration.models import ChamaInfo
    if request.GET.get('pdf') == '1':
        # chama info defaults
        chama_name      = "MICROFINANCE INSTITUTION"
        chama_address   = ""
        chama_contact   = ""
        chama_location  = ""
        chama_logo_path = None
        try:
            chama_info = ChamaInfo.objects.first()
            if chama_info:
                chama_name     = chama_info.chama_name or chama_name
                chama_address  = chama_info.chama_address or ""
                chama_contact  = chama_info.chama_contact or ""
                chama_location = chama_info.chama_location or ""
                if chama_info.chama_logo and hasattr(chama_info.chama_logo, "path"):
                    chama_logo_path = chama_info.chama_logo.path
        except Exception as e:
            logger.error(f"Error fetching chama info: {e}")

        # ── Palette (balanced for clean print output) ────
        INK        = colors.HexColor("#1f2937")
        INK_SOFT   = colors.HexColor("#374151")
        INK_MUTE   = colors.HexColor("#6b7280")
        RULE       = colors.HexColor("#e5e7eb")
        RULE_SOFT  = colors.HexColor("#f9fafb")
        ACCENT     = colors.HexColor("#1e293b")
        POS        = colors.HexColor("#16a34a")
        NEG        = colors.HexColor("#dc2626")
        OK_BG      = colors.HexColor("#f0fdf4")
        OK_FG      = colors.HexColor("#166534")
        OK_BORDER  = colors.HexColor("#86efac")
        BAD_BG     = colors.HexColor("#fef2f2")
        BAD_FG     = colors.HexColor("#991b1b")
        BAD_BORDER = colors.HexColor("#fca5a5")

        # ── A4 with generous margins for a professional feel ────────────────
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            leftMargin=48, rightMargin=48, topMargin=44, bottomMargin=44
        )
        story  = []
        styles = getSampleStyleSheet()

        # Usable content width (A4 595pt − 96pt margins ≈ 499pt)
        CW = 499

        # ── Typography ───────────────────────────────────────────────────────
        chama_style = ParagraphStyle(
            "chama", parent=styles["Normal"],
            fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=INK,
        )
        doctitle_style = ParagraphStyle(
            "DocTitle", parent=styles["Normal"],
            fontName="Helvetica", fontSize=9, leading=11,
            textColor=INK_SOFT, spaceBefore=2,
        )
        meta_style = ParagraphStyle(
            "Meta", parent=styles["Normal"],
            fontName="Helvetica", fontSize=8.5, leading=11,
            textColor=INK, alignment=TA_RIGHT,
        )
        meta_label_style = ParagraphStyle(
            "MetaLabel", parent=styles["Normal"],
            fontName="Helvetica", fontSize=8.5, leading=11,
            textColor=INK_SOFT, alignment=TA_RIGHT,
        )
        block_title_style = ParagraphStyle(
            "BlockTitle", parent=styles["Normal"],
            fontName="Helvetica-Bold", fontSize=10, leading=13,
            textColor=ACCENT, spaceBefore=14, spaceAfter=6,
        )
        block_sub_style = ParagraphStyle(
            "BlockSub", parent=styles["Normal"],
            fontName="Helvetica", fontSize=8.5, leading=11,
            textColor=INK_SOFT,
        )
        subhead_style = ParagraphStyle(
            "SubHead", parent=styles["Normal"],
            fontName="Helvetica", fontSize=8.5, leading=11,
            textColor=INK_SOFT, spaceBefore=8, spaceAfter=4,
        )
        # Table body cells
        label_style      = ParagraphStyle("Label",     parent=styles["Normal"], fontSize=9,   leading=12, textColor=INK_SOFT)
        value_style      = ParagraphStyle("Value",     parent=styles["Normal"], fontSize=9,   leading=12, textColor=INK)
        value_r_style    = ParagraphStyle("ValueR",    parent=value_style, alignment=TA_RIGHT)
        amt_style        = ParagraphStyle("Amt",       parent=styles["Normal"], fontName="Courier", fontSize=9, leading=12, textColor=INK, alignment=TA_RIGHT)
        amt_strong_style = ParagraphStyle("AmtS",      parent=amt_style, fontName="Courier-Bold")
        amt_neg_style    = ParagraphStyle("AmtN",      parent=amt_style, textColor=NEG)
        amt_pos_style    = ParagraphStyle("AmtP",      parent=amt_style, textColor=POS)
        amt_mute_style   = ParagraphStyle("AmtM",      parent=amt_style, textColor=INK_MUTE)
        # Column headers (small caps look via letterspacing)
        col_hdr_style = ParagraphStyle(
            "ColHdr", parent=styles["Normal"],
            fontName="Helvetica-Bold", fontSize=7.5, leading=10,
            textColor=INK_SOFT,
        )
        col_hdr_r_style = ParagraphStyle("ColHdrR", parent=col_hdr_style, alignment=TA_RIGHT)
        col_hdr_c_style = ParagraphStyle("ColHdrC", parent=col_hdr_style, alignment=TA_CENTER)

        # Stat card typography
        stat_label_style = ParagraphStyle(
            "StatLabel", parent=styles["Normal"],
            fontName="Helvetica", fontSize=7.5, leading=10,
            textColor=INK_SOFT,
        )
        stat_value_style = ParagraphStyle(
            "StatValue", parent=styles["Normal"],
            fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=INK,
        )
        stat_amt_style = ParagraphStyle(
            "StatAmt", parent=styles["Normal"],
            fontName="Courier", fontSize=10, leading=13, textColor=INK,
        )
        stat_sub_style = ParagraphStyle(
            "StatSub", parent=styles["Normal"],
            fontName="Helvetica", fontSize=7.5, leading=10, textColor=INK_MUTE,
            spaceBefore=2,
        )

        # ── Helpers ──────────────────────────────────────────────────────────
        def hairline(space_before=2, space_after=6):
            return HRFlowable(width="100%", thickness=0.5, color=RULE,
                              spaceBefore=space_before, spaceAfter=space_after)

        def verdict_pill(passed):
            """Small subtle pill; returns a mini-Table so bg+border render cleanly."""
            if passed:
                txt, bg, fg, brd = "Pass", OK_BG, OK_FG, OK_BORDER
            else:
                txt, bg, fg, brd = "Review", BAD_BG, BAD_FG, BAD_BORDER
            p = Paragraph(
                f'<font size="8" color="{fg.hexval()}">{txt}</font>',
                ParagraphStyle("Pill", parent=styles["Normal"], alignment=TA_CENTER),
            )
            t = Table([[p]], colWidths=[52])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,-1), bg),
                ('BOX', (0,0), (-1,-1), 0.6, brd),
                ('LEFTPADDING', (0,0), (-1,-1), 4),
                ('RIGHTPADDING', (0,0), (-1,-1), 4),
                ('TOPPADDING', (0,0), (-1,-1), 1),
                ('BOTTOMPADDING', (0,0), (-1,-1), 2),
            ]))
            return t

        def verdict_word(passed):
            """Same pill as an inline flowable for the stat rows."""
            return verdict_pill(passed)

        def stat_card(label, value_flowable, sub_text=None):
            """Small labelled stat block — no borders, just typography."""
            cells = [[Paragraph(label.upper(), stat_label_style)]]
            if hasattr(value_flowable, 'wrap'):
                cells.append([value_flowable])
            else:
                cells.append([Paragraph(str(value_flowable), stat_value_style)])
            if sub_text:
                cells.append([Paragraph(sub_text, stat_sub_style)])
            t = Table(cells)
            t.setStyle(TableStyle([
                ('LEFTPADDING', (0,0), (-1,-1), 0),
                ('RIGHTPADDING', (0,0), (-1,-1), 0),
                ('TOPPADDING', (0,0), (-1,-1), 1),
                ('BOTTOMPADDING', (0,0), (-1,-1), 1),
            ]))
            return t

        # ── HEADER ───────────────────────────────────────────────────────────
        contact_bits = " · ".join(filter(None, [chama_address, chama_contact, chama_location]))
        header_left = [
            Paragraph(chama_name, chama_style),
            Paragraph("LOAN APPRAISAL REPORT", ParagraphStyle(
                "DocT", parent=doctitle_style,
                fontName="Helvetica", textColor=INK_SOFT,
                fontSize=8.5, leading=11,
            )),
        ]
        if contact_bits:
            header_left.append(Paragraph(
                f'<font size="7.5" color="{INK_MUTE.hexval()}">{contact_bits}</font>',
                styles["Normal"],
            ))

        meta_rows = [
            [Paragraph("Loan ID", meta_label_style),
             Paragraph(f"#{loan.id}", meta_style)],
            [Paragraph("Reference", meta_label_style),
             Paragraph(str(loan.loan_no or "—"), meta_style)],
            [Paragraph("Date", meta_label_style),
             Paragraph(loan.loan_date.strftime("%d %b %Y") if loan.loan_date else "—", meta_style)],
        ]
        meta_inner = Table(meta_rows, colWidths=[60, 130])
        meta_inner.setStyle(TableStyle([
            ('LEFTPADDING', (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING', (0,0), (-1,-1), 1),
            ('BOTTOMPADDING', (0,0), (-1,-1), 1),
        ]))

        header_tbl = Table(
            [[header_left, meta_inner]],
            colWidths=[CW - 200, 200],
        )
        header_tbl.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('LEFTPADDING', (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ]))
        story.append(header_tbl)
        story.append(hairline(space_before=10, space_after=14))

        # ── APPLICANT STRIP ──────────────────────────────────────────────────
        applicant_row = [[
            stat_card("Applicant", Paragraph(customer.full_name or "—", stat_value_style)),
            stat_card("Member No", Paragraph(customer.cust_no or str(customer.id),
                                             ParagraphStyle("MN", parent=stat_value_style, fontName="Courier-Bold"))),
            stat_card("Classification", Paragraph(classification, stat_value_style)),
        ]]
        applicant_tbl = Table(applicant_row, colWidths=[CW*0.50, CW*0.25, CW*0.25])
        applicant_tbl.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('LEFTPADDING', (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 8),
            ('TOPPADDING', (0,0), (-1,-1), 0),
            ('BOTTOMPADDING', (0,0), (-1,-1), 0),
        ]))
        story.append(applicant_tbl)
        story.append(hairline(space_before=12, space_after=4))

        # ── 1. LOAN REQUEST AND DEDUCTIONS ───────────────────────────────────
        story.append(Paragraph("Loan request and deductions", block_title_style))

        kv_rows = [
            [Paragraph("Principal requested", label_style),
             Paragraph(f"{loan.principal:,.2f}", amt_style),
             Paragraph("Monthly installment", label_style),
             Paragraph(f"{loan.installment:,.2f}", amt_style)],
            [Paragraph("Loan product", label_style),
             Paragraph(loan.get_loan_type_display(), value_r_style),
             Paragraph("Total deductions", label_style),
             Paragraph(f"{total_deductions:,.2f}", amt_neg_style)],
            [Paragraph("Repayment period", label_style),
             Paragraph(f"{loan.loan_period} months", value_r_style),
             Paragraph("Net disbursed", label_style),
             Paragraph(f"{net_disbursed:,.2f}", amt_strong_style)],
        ]
        kv_tbl = Table(kv_rows, colWidths=[CW*0.20, CW*0.28, CW*0.22, CW*0.30])
        kv_tbl.setStyle(TableStyle([
            ('LINEBELOW', (0,0), (-1,-2), 0.5, RULE),
            ('LEFTPADDING', (0,0), (-1,-1), 4),
            ('RIGHTPADDING', (0,0), (-1,-1), 4),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ]))
        story.append(kv_tbl)

        # ── 2. DETAIL OF DEDUCTIONS ──────────────────────────────────────────
        story.append(Paragraph("Detail of deductions", block_title_style))
        d_rows = [[
            Paragraph("Deduction", col_hdr_style),
            Paragraph("Amount", col_hdr_r_style),
        ]]
        # A. Loan charges (fees)
        for recovery in charge_recoveries:
            bridging_tag = (
                f' <font size="7" color="#b45309">· Bridging fee</font>'
                if recovery.charge.is_bridging_fee else ''
            )
            d_rows.append([
                Paragraph(
                    f'{recovery.charge.name} '
                    f'<font size="7.5" color="{INK_MUTE.hexval()}">· {recovery.charge.get_charge_type_display()}</font>'
                    f'{bridging_tag}',
                    value_style,
                ),
                Paragraph(f"{recovery.amount:,.2f}", amt_style),
            ])
        # B. Offset / bridged loan payoffs
        for offset in offset_data:
            offset_amt = safe_decimal(offset.get('amount', 0))
            d_rows.append([
                Paragraph(
                    f'Offset loan payoff '
                    f'<font size="7.5" color="{INK_MUTE.hexval()}">· {offset.get("loan_no", "")}</font>'
                    f' <font size="7" color="#b45309">· Bridging</font>',
                    value_style,
                ),
                Paragraph(f"{offset_amt:,.2f}", amt_style),
            ])
        if len(d_rows) == 1:
            d_rows.append([Paragraph("<i>No deductions applied.</i>",
                                     ParagraphStyle("Empty", parent=value_style, textColor=INK_MUTE)),
                           Paragraph("—", amt_mute_style)])
        d_rows.append([Paragraph("Total deductions",
                                 ParagraphStyle("Tot", parent=value_style, fontName="Helvetica-Bold")),
                       Paragraph(f"{total_deductions:,.2f}", amt_strong_style)])

        d_tbl = Table(d_rows, colWidths=[CW*0.75, CW*0.25])
        d_style = [
            ('LINEBELOW', (0,0), (-1,0), 0.5, RULE),
            ('LINEABOVE', (0,-1), (-1,-1), 0.5, INK_SOFT),
            ('LEFTPADDING', (0,0), (-1,-1), 8),
            ('RIGHTPADDING', (0,0), (-1,-1), 8),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ]
        # Hairlines between body rows
        for i in range(1, len(d_rows) - 1):
            d_style.append(('LINEBELOW', (0, i), (-1, i), 0.5, RULE))
        d_tbl.setStyle(TableStyle(d_style))
        story.append(d_tbl)

        # ── 3. TRANSACTION BEHAVIOUR ─────────────────────────────────────────
        story.append(Paragraph("Transaction behaviour", block_title_style))
        story.append(Paragraph(
            "Historical for the last 6 months · last 2 distinct rows shown",
            block_sub_style,
        ))

        LABEL_W = CW * 0.26
        MONTH_W = (CW - LABEL_W) / 6
        pivot_widths = [LABEL_W] + [MONTH_W] * 6

        def _pivot_header(first_label):
            return [Paragraph(first_label, col_hdr_style)] + [
                Paragraph(lbl, col_hdr_r_style) for lbl in month_labels
            ]

        def _build_pivot_table(rows, first_label, positive_style, negative_style=None):
            body = [_pivot_header(first_label)]
            if not rows:
                body.append([Paragraph("<i>No entries recorded in the window.</i>",
                                       ParagraphStyle("PE", parent=value_style, textColor=INK_MUTE))]
                            + [Paragraph("—", amt_mute_style) for _ in range(6)])
            else:
                for row in rows:
                    cells = [Paragraph(row['label'],
                                       ParagraphStyle("PL", parent=value_style, fontName="Helvetica-Bold"))]
                    for val in row['values']:
                        if val > 0:
                            cells.append(Paragraph(f"{val:,.0f}", positive_style))
                        elif val < 0 and negative_style is not None:
                            cells.append(Paragraph(f"{val:,.0f}", negative_style))
                        else:
                            cells.append(Paragraph("—", amt_mute_style))
                    body.append(cells)
            tbl = Table(body, colWidths=pivot_widths, repeatRows=1)
            style = [
                ('LINEBELOW', (0,0), (-1,0), 0.5, RULE),
                ('LEFTPADDING', (0,0), (-1,-1), 6),
                ('RIGHTPADDING', (0,0), (-1,-1), 6),
                ('TOPPADDING', (0,0), (-1,-1), 5),
                ('BOTTOMPADDING', (0,0), (-1,-1), 5),
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ]
            for i in range(1, len(body)):
                style.append(('LINEBELOW', (0, i), (-1, i), 0.5, RULE))
            tbl.setStyle(TableStyle(style))
            return tbl

        story.append(Paragraph(
            'Savings deposit activity <font color="' + INK_MUTE.hexval() + '">· net per month</font>',
            subhead_style,
        ))
        story.append(_build_pivot_table(savings_pivot, "Account", amt_pos_style, amt_neg_style))

        # ── 4. LOAN SECURITY AND GUARANTEES ──────────────────────────────────
        story.append(Paragraph("Loan security and guarantees", block_title_style))

        if guarantors:
            g_rows = [[
                Paragraph("#", col_hdr_style),
                Paragraph("Guarantor", col_hdr_style),
                Paragraph("Member no", col_hdr_style),
                Paragraph("Savings balance", col_hdr_r_style),
                Paragraph("Guaranteed amount", col_hdr_r_style),
            ]]
            for idx, g in enumerate(guarantors, 1):
                g_rows.append([
                    Paragraph(str(idx),
                              ParagraphStyle("Num", parent=value_style, textColor=INK_MUTE)),
                    Paragraph(str(getattr(g.guarantor_cust, 'full_name', '')), value_style),
                    Paragraph(str(getattr(g.guarantor_cust, 'cust_no', '')),
                              ParagraphStyle("Mono", parent=value_style, fontName="Courier", textColor=INK_SOFT)),
                    Paragraph(f"{getattr(g, 'savings_balance', 0):,.2f}", amt_style),
                    Paragraph(f"{g.amount:,.2f}", amt_strong_style),
                ])
            g_tbl = Table(g_rows, colWidths=[CW*0.06, CW*0.34, CW*0.18, CW*0.20, CW*0.22])
            g_style = [
                ('LINEBELOW', (0,0), (-1,0), 0.5, RULE),
                ('LEFTPADDING', (0,0), (-1,-1), 8),
                ('RIGHTPADDING', (0,0), (-1,-1), 8),
                ('TOPPADDING', (0,0), (-1,-1), 5),
                ('BOTTOMPADDING', (0,0), (-1,-1), 5),
                ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ]
            for i in range(1, len(g_rows)):
                g_style.append(('LINEBELOW', (0, i), (-1, i), 0.5, RULE))
            g_tbl.setStyle(TableStyle(g_style))
            story.append(g_tbl)
        else:
            story.append(Paragraph(
                "No guarantors have been mapped to this credit line.",
                ParagraphStyle("Empty", parent=value_style, textColor=INK_MUTE),
            ))

        # Security stat row
        story.append(Spacer(1, 10))
        sec_row = [[
            stat_card("Total security",
                      Paragraph(f"{total_security:,.2f}", stat_amt_style)),
            stat_card("Coverage",
                      Paragraph(f"{security_coverage_percent:,.2f}%", stat_amt_style)),
            stat_card("Maximum limit (3×)",
                      Paragraph(f"{max_loan_limit:,.2f}", stat_amt_style)),
            stat_card("Policy fit",
                      verdict_word(limit_status == "Pass")),
        ]]
        sec_tbl = Table(sec_row, colWidths=[CW*0.25]*4)
        sec_tbl.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('LEFTPADDING', (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 8),
        ]))
        story.append(sec_tbl)

        # ── 5. DEFAULTER HISTORY ─────────────────────────────────────────────
        story.append(Paragraph("Defaulter history", block_title_style))

        def_row = [[
            stat_card("Defaulted before",
                      verdict_word(not defaulter_summary['has_defaulted_before'])),
            stat_card("Total defaulted loans on record",
                      Paragraph(str(defaulter_summary['total_defaulted_loans']), stat_amt_style)),
            stat_card("", Paragraph("", value_style)),  # spacer
        ]]
        def_row_tbl = Table(def_row, colWidths=[CW/3]*3)
        def_row_tbl.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('LEFTPADDING', (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 8),
        ]))
        story.append(def_row_tbl)

        if defaulter_summary['top_loans']:
            story.append(Spacer(1, 10))
            df_rows = [[
                Paragraph("First default", col_hdr_style),
                Paragraph("Loan no", col_hdr_style),
                Paragraph("Product", col_hdr_style),
                Paragraph("Arrears", col_hdr_r_style),
                Paragraph("Days past due", col_hdr_r_style),
                Paragraph("Class", col_hdr_style),
                Paragraph("Status", col_hdr_style),
            ]]
            for d in defaulter_summary['top_loans']:
                df_rows.append([
                    Paragraph(str(d['first_default_date']), value_style),
                    Paragraph(d['loan_no'],
                              ParagraphStyle("Mn", parent=value_style, fontName="Courier")),
                    Paragraph(d['product_name'] or d['product_code'] or '—', value_style),
                    Paragraph(f"{d['loan_arrears']:,.0f}", amt_neg_style),
                    Paragraph(str(d['defaulted_days']), amt_style),
                    Paragraph(d['loan_classification'] or '—', value_style),
                    Paragraph('Resolved' if d.get('is_resolved') else 'Open', value_style),
                ])
            df_tbl = Table(df_rows,
                colWidths=[CW*0.14, CW*0.14, CW*0.22, CW*0.14, CW*0.12, CW*0.12, CW*0.12])
            df_style = [
                ('LINEBELOW', (0,0), (-1,0), 0.5, RULE),
                ('LEFTPADDING', (0,0), (-1,-1), 6),
                ('RIGHTPADDING', (0,0), (-1,-1), 6),
                ('TOPPADDING', (0,0), (-1,-1), 5),
                ('BOTTOMPADDING', (0,0), (-1,-1), 5),
                ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ]
            for i in range(1, len(df_rows)):
                df_style.append(('LINEBELOW', (0, i), (-1, i), 0.5, RULE))
            df_tbl.setStyle(TableStyle(df_style))
            story.append(df_tbl)

        # ── 6. APPRAISAL VERDICTS ────────────────────────────────────────────
        story.append(Paragraph("Appraisal verdicts", block_title_style))

        v_rows = [[
            Paragraph("Check", col_hdr_style),
            Paragraph("Result", col_hdr_c_style),
            Paragraph("Notes", col_hdr_style),
        ]]
        for v in appraisal_verdicts:
            v_rows.append([
                Paragraph(v['label'],
                          ParagraphStyle("VL", parent=value_style, fontName="Helvetica-Bold")),
                verdict_pill(v['passed']),
                Paragraph(v['detail'],
                          ParagraphStyle("VD", parent=value_style, textColor=INK_SOFT, fontSize=8.5)),
            ])
        v_tbl = Table(v_rows, colWidths=[CW*0.28, CW*0.14, CW*0.58])
        v_style = [
            ('LINEBELOW', (0,0), (-1,0), 0.5, RULE),
            ('LEFTPADDING', (0,0), (-1,-1), 8),
            ('RIGHTPADDING', (0,0), (-1,-1), 8),
            ('TOPPADDING', (0,0), (-1,-1), 6),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('ALIGN', (1,0), (1,-1), 'CENTER'),
        ]
        for i in range(1, len(v_rows)):
            v_style.append(('LINEBELOW', (0, i), (-1, i), 0.5, RULE))
        v_tbl.setStyle(TableStyle(v_style))
        story.append(v_tbl)

        # ── Footer ───────────────────────────────────────────────────────────
        story.append(Spacer(1, 20))
        story.append(hairline(space_before=0, space_after=6))
        footer_text = (
            f'<font size="7.5" color="{INK_MUTE.hexval()}">'
            f'Prepared by {loan.created_by or request.user} · '
            f'Generated {now().strftime("%d %b %Y %H:%M")}'
            f'</font>'
        )
        story.append(Paragraph(footer_text, ParagraphStyle(
            "Footer", parent=styles["Normal"], alignment=TA_CENTER,
        )))

        doc.build(story)
        pdf_data = buffer.getvalue()
        buffer.close()

        response = HttpResponse(content_type='application/pdf')
        response['Content-Disposition'] = (
            f'attachment; filename="Appraisal_Report_{loan.loan_no or loan.id}.pdf"'
        )
        response.write(pdf_data)
        return response

    # ══════════════════════════════════════════════════════════════════════════
    # 8. DEFAULT HTML TEMPLATE CONTEXT
    # ══════════════════════════════════════════════════════════════════════════
    context = {
        # Core
        'loan':              loan,
        'customer':          customer,
        # Deductions
        'charge_recoveries':  charge_recoveries,
        'total_charges':      total_charges,
        'offset_data':        offset_data,
        'total_offset_amount': total_offset_amount,
        'total_deductions':   total_deductions,
        'net_disbursed':      net_disbursed,
        # Financial profile
        'savings_balance':        savings_balance,
        'share_capital_balance':  share_capital_balance,
        'loan_balances':          loan_balances,
        'total_outstanding_debt': total_outstanding_debt,
        # Pivot data
        'month_labels':          month_labels,        # ["JAN 2026", …]
        'savings_pivot':         savings_pivot,        # [{label, values[6]}, …]
        'loan_repayment_pivot':  loan_repayment_pivot, # [{label, values[6]}, …]
        # Scoring
        'repayment_score':  repayment_score,
        'classification':   classification,
        # Security
        'guarantors':       guarantors,
        'total_guaranteed': total_guaranteed,
        'collaterals':      collaterals,
        'total_collateral': total_collateral,
        'total_security':   total_security,
        'security_coverage_percent': round(security_coverage_percent, 2),
        # Policy
        'max_loan_limit':  max_loan_limit,
        'total_exposure':  total_exposure,
        'limit_status':    limit_status,
        # ── NEW appraisal metrics (settings-driven) ─────────────────────
        'lumpsum_metrics':     lumpsum_metrics,
        'deposits_summary':    deposits_summary,
        'repayments_summary':  repayments_summary,
        'defaulter_summary':   defaulter_summary,
        'eligibility_summary': eligibility_summary,
        'appraisal_verdicts':  appraisal_verdicts,
    }
    return render(request, 'loans/loan_appraisal_report.html', context)

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q
from django.shortcuts import render

# Assuming your decorators exist exactly as written
@login_required
@min_role_required('accounts_clerk')
def running_loans_dashboard(request):
    # Start with base queryset
    loans_list = RunningLoanStat.objects.all().order_by('-last_updated')
    
    # 1. Capture the search parameter
    query = request.GET.get('q', '').strip()
    if query:
        loans_list = loans_list.filter(
            Q(cust_no__icontains=query) |
            Q(loan_no__icontains=query) |
            Q(full_name__icontains=query)
        )
    
    # Capture the latest update timestamp before paginating the filtered queryset
    last_update = loans_list.first().last_updated if loans_list.exists() else None
    
    # Show 100 loans per page
    paginator = Paginator(loans_list, 100) 
    page = request.GET.get('page')
    
    try:
        loans = paginator.page(page)
    except PageNotAnInteger:
        loans = paginator.page(1)
    except EmptyPage:
        loans = paginator.page(paginator.num_pages)
        
    # Annotate each loan with its LoanHistory pk for clickable detail links
    page_loan_nos = [l.loan_no for l in loans]
    pk_map = dict(
        LoanHistory.objects.filter(loan_no__in=page_loan_nos)
        .values_list('loan_no', 'pk')
    )
    for loan in loans:
        loan.detail_pk = pk_map.get(loan.loan_no)

    context = {
        'loans': loans,
        'last_update': last_update,
        'query': query,
    }
    return render(request, 'loans/running_loans.html', context)

@login_required
@min_role_required('manager')
def trigger_running_loans_update(request):
    update_running_loans_stats()
    return redirect('loans:running_loans_dashboard')

@login_required
@min_role_required('manager')
def export_running_loans_excel(request):
    from dashboard.utils import export_to_excel

    query = request.GET.get('q', '').strip()
    qs = RunningLoanStat.objects.all().order_by('-last_updated')
    if query:
        qs = qs.filter(
            Q(cust_no__icontains=query) |
            Q(loan_no__icontains=query) |
            Q(full_name__icontains=query)
        )

    headers = [
        'Application No', 'Application Date', 'Posting Date', 'Repayment Start Date',
        'Installments', 'Repayment End Date', 'Member No.', 'Member Name',
        'Product Code', 'Product Description', 'Approved Amount', 'Loan Balance',
        'Monthly Installment', 'Interest Rate', 'Interest Repayment Method',
        'Principle Paid', 'Principle Balance', 'Interest Paid', 'Interest Balance',
        'Defaulted Days', 'Total Arrears', 'Principle Arrears', 'Last Interest Charge',
        'Interest Arrears', 'Defaulted Installments', 'Loan Classification',
        'Sales Person', 'Loan Account', 'Disbursed', 'Created By', 'Loan Status',
    ]

    data_rows = []
    for loan in qs:
        data_rows.append([
            loan.loan_no, loan.application_date, loan.posting_date, loan.repayment_start_date,
            loan.installments, loan.repayment_end_date, loan.cust_no, loan.full_name,
            loan.product_code, loan.product_description, float(loan.approved_amount), float(loan.loan_balance),
            float(loan.monthly_installment), float(loan.interest_rate), loan.repayment_method,
            float(loan.principle_paid), float(loan.principle_balance), float(loan.interest_paid), float(loan.interest_balance),
            loan.defaulted_days, float(loan.total_arrears), float(loan.principle_arrears), loan.last_interest_charge,
            float(loan.interest_arrears), loan.defaulted_installments, loan.loan_classification,
            loan.sales_person, loan.loan_account, 'Yes' if loan.disbursed else 'No', loan.created_by,
            loan.loan_status,
        ])

    return export_to_excel('running_loans', headers, data_rows)


@login_required
@min_role_required('manager')
def loan_charge_list(request):
    """
    Fetches all predefined loan configurations and renders them in a dashboard table.
    """
    charges = LoanCharge.objects.all().order_by('name')
    
    context = {
        'charges': charges,
    }
    return render(request, 'loans/loan_charge_list.html', context)


@login_required
@min_role_required('manager')
def loan_charge_add(request):
    """
    Handles both the display of the blank configuration form and the 
    submission/validation of a new loan charge.
    """
    if request.method == 'POST':
        form = LoanChargeForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Loan Charge created successfully!")
            return redirect('loans:loan_charge_list')
    else:
        form = LoanChargeForm()

    context = {
        'form': form,
        'title': 'Define New Loan Charge'
    }
    return render(request, 'loans/loan_charge_form.html', context)


@login_required
@min_role_required('manager')
def loan_charge_edit(request, pk):
    """
    Retrieves an existing charge by primary key, fills the form instance for editing,
    and updates the database on valid POST request.
    """
    charge = get_object_or_404(LoanCharge, pk=pk)
    
    if request.method == 'POST':
        form = LoanChargeForm(request.POST, instance=charge)
        if form.is_valid():
            form.save()
            messages.success(request, f"Loan Charge '{charge.name}' updated successfully!")
            return redirect('loans:loan_charge_list')
    else:
        form = LoanChargeForm(instance=charge)

    context = {
        'form': form,
        'charge': charge,
        'title': f"Edit Loan Charge: {charge.name}"
    }
    return render(request, 'loans/loan_charge_form.html', context)

from django.shortcuts import render
from django.http import JsonResponse
from django.contrib import messages
from .services import update_mobile_loan_limits
from .models import LoanLimitGraduation

@login_required
@min_role_required('manager')
def trigger_loan_limit_graduation(request):
    """Admin view to trigger the graduation engine."""
    if request.method == "POST":
        result = update_mobile_loan_limits()
        if result["status"] == "success":
            messages.success(request, result["message"])
        else:
            messages.error(request, result["message"])
            
    return render(request, "loans/loan_limits_graduation.html")

def api_get_customer_limit(request, cust_no):
    """API Endpoint for the phone app to get the current limit."""
    # Get the latest graduation record for this customer
    latest_limit = LoanLimitGraduation.objects.filter(
        cust_no=cust_no
    ).order_by('-graduation_date', '-id').first()

    if latest_limit:
        return JsonResponse({
            "cust_no": cust_no,
            "available_limit": float(latest_limit.amount),
            "last_updated": latest_limit.graduation_date
        })
    
    return JsonResponse({
        "cust_no": cust_no,
        "available_limit": 0.00,
        "last_updated": None,
        "message": "No limit evaluated yet."
    })

import json
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from .models import LoanLimitGraduation

@login_required
@min_role_required('manager')
def loan_limits_list(request):
    """View to display searchable and paginated loan limits."""
    query = request.GET.get('q', '')
    
    # Base queryset
    limits_qs = LoanLimitGraduation.objects.all().order_by('-graduation_date', '-id')

    # Apply search filter if query exists
    if query:
        limits_qs = limits_qs.filter(
            Q(cust_no__icontains=query) | Q(full_name__icontains=query)
        )

    # Paginate (e.g., 50 records per page)
    paginator = Paginator(limits_qs, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'query': query,
    }
    return render(request, 'loans/loan_limits_list.html', context)


@login_required
@min_role_required('manager')
def update_loan_limit_amount(request, pk):
    """API endpoint to update the loan limit amount on the fly."""
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            new_amount = data.get('amount')
            
            # Fetch the record and update
            limit_record = get_object_or_404(LoanLimitGraduation, pk=pk)
            limit_record.amount = new_amount
            limit_record.save()
            
            return JsonResponse({
                'status': 'success', 
                'new_amount': limit_record.amount,
                'message': 'Limit updated successfully!'
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
            
    return JsonResponse({'status': 'error', 'message': 'Invalid request method'}, status=405)

import json
from decimal import Decimal
from datetime import datetime
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.views.generic import View
from django.contrib.auth.mixins import LoginRequiredMixin

from .models import LoanHistory, Guarantor, LoanTransaction
from transactions.models import CustomerAccountsSetup
from customers.models import Customer

def get_outstanding_balance(loan):
    """Calculates the current net principal balance of a loan."""
    totals = LoanTransaction.objects.filter(loan_no=loan.loan_no).aggregate(
        total_debit=Sum('debit_amount'),
        total_credit=Sum('credit_amount')
    )
    debits = totals['total_debit'] or Decimal('0.00')
    credits = totals['total_credit'] or Decimal('0.00')
    return debits - credits

def calculate_installment(principal, annual_rate, period_months, method):
    """Calculates the installment based on product configuration parameters."""
    if principal <= 0 or period_months <= 0:
        return Decimal('0.00')
        
    r = (annual_rate / Decimal('100.00')) / Decimal('12.00')
    n = period_months

    if method == "flat_rate":
        monthly_principal = principal / Decimal(n)
        monthly_interest = principal * r
        return (monthly_principal + monthly_interest).quantize(Decimal('0.01'))
    
    else:  # reducing_balance default
        if r == 0:
            return (principal / Decimal(n)).quantize(Decimal('0.01'))
        # PMT formula: P * (r * (1+r)^n) / ((1+r)^n - 1)
        compounded = (Decimal('1.00') + r) ** n
        installment = principal * (r * compounded) / (compounded - Decimal('1.00'))
        return installment.quantize(Decimal('0.01'))

# Loan restructure / guarantor offload service + PDF
from .forms import LoanRestructureForm, GuarantorOffloadForm  # noqa: E402
from .restructure_service import (  # noqa: E402
    get_loan_balances,
    restructure_loan,
    preview_guarantor_offload,
    execute_guarantor_offload,
)
from .restructure_pdf import (  # noqa: E402
    restructure_summary_pdf,
    guarantor_offload_summary_pdf,
)


# ═══════════════════════════════════════════════════════════════════════
#  LOAN RESTRUCTURE
# ═══════════════════════════════════════════════════════════════════════

def _q(v):
    if v is None:
        return Decimal("0.00")
    if not isinstance(v, Decimal):
        v = Decimal(str(v))
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class LoanRestructureView(LoginRequiredMixin, View):
    """
    GET  : show restructure form + calculator preview
    POST : validate & apply the restructure, then redirect to the
           PDF summary page.
    """
    template_name = "loans/restructure.html"

    def _get_loan(self, request, pk):
        """Fetch the loan with a helpful error instead of a bare 404."""
        loan = get_object_or_404(LoanHistory, pk=pk)
        if not loan.is_disbursed:
            # Auto-fix: if the loan has been approved and has ledger
            # entries, it was effectively disbursed but the flag was
            # never set (legacy data). Fix it in-place.
            from transactions.models import LoanTransaction
            has_entries = LoanTransaction.objects.filter(
                loan_no=loan.loan_no
            ).exists()
            if loan.is_approved and has_entries:
                loan.is_disbursed = True
                loan.disbursed_at = loan.approved_at or timezone.now()
                loan.save(update_fields=['is_disbursed', 'disbursed_at'])
            else:
                messages.error(
                    request,
                    f"Loan {loan.loan_no} has not been disbursed yet. "
                    f"Only disbursed loans can be restructured."
                )
                return None
        return loan

    def get(self, request, pk):
        loan = self._get_loan(request, pk)
        if not loan:
            return redirect("loans:view_loan_details", pk=pk)

        balances = get_loan_balances(loan)

        form = LoanRestructureForm(initial={
            "new_loan_date": timezone.localdate(),
            "new_period": loan.loan_period,
            "new_installment": Decimal("0.00"),
            "restructure_fee_rate": Decimal("10.00"),
        })
        return render(request, self.template_name, {
            "loan": loan,
            "form": form,
            "balances": balances,
            "outstanding": balances["outstanding"],
            "principal_balance": balances["principal_balance"],
            "interest_balance": balances["interest_balance"],
        })

    def post(self, request, pk):
        loan = self._get_loan(request, pk)
        if not loan:
            return redirect("loans:view_loan_details", pk=pk)
        balances = get_loan_balances(loan)
        form = LoanRestructureForm(request.POST)

        if not form.is_valid():
            return render(request, self.template_name, {
                "loan": loan, "form": form, "balances": balances,
                "outstanding": balances["outstanding"],
                "principal_balance": balances["principal_balance"],
                "interest_balance": balances["interest_balance"],
            })

        cd = form.cleaned_data
        new_installment = cd["new_installment"] or Decimal("0.00")

        # Auto-calculate installment if user left it at 0
        if new_installment <= 0:
            new_installment = calculate_installment(
                principal=balances["outstanding"],
                annual_rate=loan.interest_rate,
                period_months=cd["new_period"],
                method=loan.loan_type.interest_calc_method,
            )

        try:
            result = restructure_loan(
                loan=loan,
                new_loan_date=cd["new_loan_date"],
                new_period=cd["new_period"],
                new_installment=new_installment,
                restructure_fee_rate=cd["restructure_fee_rate"],
                reason=cd["reason"],
                user=request.user,
                request=request,
            )
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("loans:restructure", pk=pk)
        except Exception:
            logger.exception("Restructure failed for loan %s", loan.loan_no)
            messages.error(request, "Restructure failed — see logs.")
            return redirect("loans:restructure", pk=pk)

        # Notify member
        try:
            from sms.events import after_commit, notify_member_event
            if loan.customer and (loan.customer.phone or getattr(loan.customer, "mobile", None)):
                after_commit(
                    notify_member_event, loan.customer,
                    f"Dear {loan.customer.first_name}, your loan {loan.loan_no} "
                    f"has been restructured. New installment: KES "
                    f"{result.new_installment:,.2f} over {result.new_period} months. "
                    f"Restructure fee: KES {result.restructure_fee:,.2f}.",
                    request.user.username,
                )
        except Exception:
            logger.exception("restructure SMS scheduling failed")

        messages.success(
            request,
            f"Loan {loan.loan_no} restructured successfully. "
            f"New installment KES {result.new_installment:,.2f} × "
            f"{result.new_period} months.",
        )
        # Redirect to PDF summary
        return redirect("loans:restructure_pdf", pk=loan.pk)


class LoanRestructurePDFView(LoginRequiredMixin, View):
    """Regenerate the restructure PDF from the latest snapshot on file."""

    def get(self, request, pk):
        import json as _json
        from .restructure_service import RestructureResult

        loan = get_object_or_404(LoanHistory, pk=pk, is_restructured=True)
        if not loan.original_loan_summary:
            messages.error(request, "No restructure history on record for this loan.")
            return redirect("loans:loan_dashboard")

        try:
            history = _json.loads(loan.original_loan_summary)
            if isinstance(history, list):
                snapshot = history[-1]
            else:
                snapshot = history
        except (ValueError, TypeError):
            messages.error(request, "Corrupted restructure snapshot.")
            return redirect("loans:loan_dashboard")

        result = RestructureResult(
            loan_id=loan.id,
            loan_no=loan.loan_no,
            original_snapshot=snapshot,
            new_loan_date=snapshot.get("new_loan_date", ""),
            new_period=int(snapshot.get("new_period", 0) or 0),
            new_installment=_q(snapshot.get("new_installment", 0)),
            outstanding_at_restructure=_q(snapshot.get("outstanding_at_restructure", 0)),
            principal_at_restructure=_q(snapshot.get("principal_balance_at_restructure", 0)),
            interest_at_restructure=_q(snapshot.get("interest_balance_at_restructure", 0)),
            restructure_fee=_q(snapshot.get("restructure_fee", 0)),
            fee_reference=None,
        )
        return restructure_summary_pdf(loan, result)


# ═══════════════════════════════════════════════════════════════════════
#  GUARANTOR DEFAULTER OFFLOAD
# ═══════════════════════════════════════════════════════════════════════

class GuarantorOffloadDefaulterView(LoginRequiredMixin, View):
    """
    GET  : show live-calculated distribution preview + confirmation form.
    POST : execute the offload and redirect to the PDF summary.
    """
    template_name = "loans/guarantor_offload.html"

    def get(self, request, pk):
        loan = get_object_or_404(LoanHistory, pk=pk)
        preview = preview_guarantor_offload(loan)
        form = GuarantorOffloadForm(initial={
            "new_loan_period": 12,
            "interest_rate": Decimal("0.00"),
        })
        return render(request, self.template_name, {
            "loan": loan,
            "form": form,
            "preview": preview,
            "allocations": preview["allocations"],
            "outstanding": preview["outstanding"],
            "principal_balance": preview["principal_balance"],
            "interest_balance": preview["interest_balance"],
            "total_pool": preview["total_pool"],
            "total_allocated": preview["total_allocated"],
            "residual_balance": preview["residual_balance"],
        })

    def post(self, request, pk):
        loan = get_object_or_404(LoanHistory, pk=pk)
        preview = preview_guarantor_offload(loan)
        form = GuarantorOffloadForm(request.POST)

        if not form.is_valid():
            return render(request, self.template_name, {
                "loan": loan, "form": form, "preview": preview,
                "allocations": preview["allocations"],
                "outstanding": preview["outstanding"],
                "principal_balance": preview["principal_balance"],
                "interest_balance": preview["interest_balance"],
                "total_pool": preview["total_pool"],
                "total_allocated": preview["total_allocated"],
                "residual_balance": preview["residual_balance"],
            })

        cd = form.cleaned_data
        try:
            result = execute_guarantor_offload(
                loan=loan,
                new_loan_period=cd["new_loan_period"],
                interest_rate=cd["interest_rate"],
                reason=cd["reason"],
                user=request.user,
                request=request,
            )
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("loans:guarantor_offload", pk=pk)
        except Exception:
            logger.exception("Guarantor offload failed for loan %s", loan.loan_no)
            messages.error(request, "Guarantor offload failed — see logs.")
            return redirect("loans:guarantor_offload", pk=pk)

        # SMS the guarantors and original borrower
        try:
            from sms.events import after_commit, notify_member_event
            for alloc in result.allocations:
                from customers.models import Customer as _Customer
                cust = _Customer.objects.filter(cust_no=alloc.guarantor_cust_no).first()
                if cust and (cust.phone or getattr(cust, "mobile", None)):
                    after_commit(
                        notify_member_event, cust,
                        f"Dear {cust.first_name}, a defaulted loan "
                        f"{loan.loan_no} from {loan.customer.full_name} has "
                        f"been offloaded to you as a guarantor. Amount: KES "
                        f"{alloc.allocated_amount:,.2f}. New loan: "
                        f"{alloc.new_loan_no}. Please contact the office.",
                        request.user.username,
                    )
            if loan.customer and (loan.customer.phone or getattr(loan.customer, "mobile", None)):
                after_commit(
                    notify_member_event, loan.customer,
                    f"Dear {loan.customer.first_name}, KES "
                    f"{result.total_allocated:,.2f} of your loan {loan.loan_no} "
                    f"has been cleared through your guarantors. Residual balance "
                    f"of KES {result.residual_balance:,.2f} remains.",
                    request.user.username,
                )
        except Exception:
            logger.exception("offload SMS scheduling failed")

        # Stash result in session so PDF endpoint can render it
        request.session[f"offload_result_{loan.pk}"] = {
            "reference": result.reference,
            "principal_balance_before": str(result.principal_balance_before),
            "interest_balance_before": str(result.interest_balance_before),
            "total_pool": str(result.total_pool),
            "total_allocated": str(result.total_allocated),
            "residual_balance": str(result.residual_balance),
            "reason": cd["reason"],
            "allocations": [
                {
                    "guarantor_cust_no": a.guarantor_cust_no,
                    "guarantor_name": a.guarantor_name,
                    "guarantee_amount": str(a.guarantee_amount),
                    "percentage": str(a.percentage),
                    "allocated_amount": str(a.allocated_amount),
                    "new_loan_no": a.new_loan_no,
                    "new_loan_id": a.new_loan_id,
                }
                for a in result.allocations
            ],
        }

        messages.success(
            request,
            f"Offload posted. Reference {result.reference}. "
            f"KES {result.total_allocated:,.2f} distributed across "
            f"{len(result.allocations)} guarantor(s). Residual balance: "
            f"KES {result.residual_balance:,.2f}.",
        )
        return redirect("loans:guarantor_offload_pdf", pk=loan.pk)


class GuarantorOffloadPDFView(LoginRequiredMixin, View):
    """
    Render the PDF summary of the LATEST offload for this loan. Data
    comes from the session (set by GuarantorOffloadDefaulterView.post).
    If no session data is available, reconstructs from LoanHistory rows
    created by the offload (fallback for later revisits).
    """

    def get(self, request, pk):
        from .restructure_service import OffloadResult, GuarantorAllocation

        loan = get_object_or_404(LoanHistory, pk=pk)
        data = request.session.pop(f"offload_result_{loan.pk}", None)

        if data is None:
            messages.warning(
                request,
                "No offload session found. Re-run the offload to generate a "
                "fresh PDF.",
            )
            return redirect("loans:guarantor_offload", pk=pk)

        allocations = [
            GuarantorAllocation(
                guarantor_cust_no=a["guarantor_cust_no"],
                guarantor_name=a["guarantor_name"],
                guarantee_amount=_q(a["guarantee_amount"]),
                percentage=_q(a["percentage"]),
                allocated_amount=_q(a["allocated_amount"]),
                new_loan_no=a.get("new_loan_no"),
                new_loan_id=a.get("new_loan_id"),
            )
            for a in data["allocations"]
        ]
        result = OffloadResult(
            original_loan_id=loan.id,
            original_loan_no=loan.loan_no,
            principal_balance_before=_q(data["principal_balance_before"]),
            interest_balance_before=_q(data["interest_balance_before"]),
            total_pool=_q(data["total_pool"]),
            total_allocated=_q(data["total_allocated"]),
            residual_balance=_q(data["residual_balance"]),
            allocations=allocations,
            reference=data["reference"],
        )
        return guarantor_offload_summary_pdf(loan, result, reason=data.get("reason", ""))


# ═══════════════════════════════════════════════════════════════════════════════
# LOAN AMORTIZATION SCHEDULE
# ═══════════════════════════════════════════════════════════════════════════════

def _build_amortization_schedule(loan):
    """
    Build an amortization schedule with real calendar dates.

    Row 0 = disbursement row (no payment, opening balance = principal).
    Rows 1..N = monthly repayment rows, dated from the month after
    disbursement through to the final month.

    Returns a list of dicts:
      [{month, date, date_label, payment, principal, interest, balance,
        is_disbursement}, ...]
    """
    from dateutil.relativedelta import relativedelta

    principal = loan.principal
    rate = loan.interest_rate / Decimal('100')  # monthly rate
    period = int(loan.loan_period)
    calc_method = 'reducing_balance'
    if loan.loan_type and loan.loan_type.interest_calc_method:
        calc_method = loan.loan_type.interest_calc_method

    # Anchor date: the loan's disbursement / application date
    disburse_date = loan.loan_date

    schedule = []
    balance = principal

    # ── Row 0: Loan Disbursement ─────────────────────────────────────
    schedule.append({
        'month': 0,
        'date': disburse_date,
        'date_label': disburse_date.strftime('%b %Y'),
        'description': 'Loan Disbursement',
        'payment': Decimal('0.00'),
        'principal': Decimal('0.00'),
        'interest': Decimal('0.00'),
        'balance': principal.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
        'is_disbursement': True,
    })

    # ── Rows 1..N: Monthly repayments ────────────────────────────────
    if calc_method == 'flat_rate':
        total_interest = principal * rate * period
        monthly_payment = (principal + total_interest) / period
        monthly_principal = principal / period
        monthly_interest = total_interest / period

        for month in range(1, period + 1):
            pay_date = disburse_date + relativedelta(months=month)
            if month == period:
                p_portion = balance
            else:
                p_portion = monthly_principal
            i_portion = monthly_interest
            balance -= p_portion
            if balance < 0:
                balance = Decimal('0')
            schedule.append({
                'month': month,
                'date': pay_date,
                'date_label': pay_date.strftime('%b %Y'),
                'description': f'Installment {month}',
                'payment': (p_portion + i_portion).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'principal': p_portion.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'interest': i_portion.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'balance': balance.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'is_disbursement': False,
            })

    elif calc_method == 'principal_flat_rate':
        monthly_principal = principal / period
        for month in range(1, period + 1):
            pay_date = disburse_date + relativedelta(months=month)
            i_portion = principal * rate
            if month == period:
                p_portion = balance
            else:
                p_portion = monthly_principal
            payment = p_portion + i_portion
            balance -= p_portion
            if balance < 0:
                balance = Decimal('0')
            schedule.append({
                'month': month,
                'date': pay_date,
                'date_label': pay_date.strftime('%b %Y'),
                'description': f'Installment {month}',
                'payment': payment.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'principal': p_portion.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'interest': i_portion.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'balance': balance.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'is_disbursement': False,
            })

    else:  # reducing_balance (default)
        if rate > 0:
            rate_factor = (Decimal('1') + rate) ** period
            monthly_payment = (principal * rate * rate_factor) / (rate_factor - Decimal('1'))
        else:
            monthly_payment = principal / period

        monthly_payment = monthly_payment.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        for month in range(1, period + 1):
            pay_date = disburse_date + relativedelta(months=month)
            i_portion = (balance * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            p_portion = monthly_payment - i_portion
            if month == period:
                p_portion = balance
                i_portion = (balance * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                payment = p_portion + i_portion
            else:
                payment = monthly_payment
            balance -= p_portion
            if balance < 0:
                balance = Decimal('0')
            schedule.append({
                'month': month,
                'date': pay_date,
                'date_label': pay_date.strftime('%b %Y'),
                'description': f'Installment {month}',
                'payment': payment.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'principal': p_portion.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'interest': i_portion.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'balance': balance.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                'is_disbursement': False,
            })

    return schedule


@login_required
@min_role_required('accounts_clerk')
def loan_amortization_schedule(request, pk):
    """Render the amortization schedule page for a given loan."""
    loan = get_object_or_404(LoanHistory.objects.select_related('customer', 'loan_type'), pk=pk)
    schedule = _build_amortization_schedule(loan)

    payment_rows = [r for r in schedule if not r['is_disbursement']]
    total_payment = sum(row['payment'] for row in payment_rows)
    total_interest = sum(row['interest'] for row in payment_rows)
    total_principal = sum(row['principal'] for row in payment_rows)

    calc_method = 'Reducing Balance'
    if loan.loan_type and loan.loan_type.interest_calc_method:
        method = loan.loan_type.interest_calc_method
        calc_method = method.replace('_', ' ').title()

    return render(request, 'loans/loan_amortization_schedule.html', {
        'loan': loan,
        'schedule': schedule,
        'total_payment': total_payment,
        'total_interest': total_interest,
        'total_principal': total_principal,
        'calc_method': calc_method,
    })


@login_required
@min_role_required('accounts_clerk')
def loan_amortization_schedule_pdf(request, pk):
    """Generate a PDF amortization schedule for printing."""
    loan = get_object_or_404(LoanHistory.objects.select_related('customer', 'loan_type'), pk=pk)
    schedule = _build_amortization_schedule(loan)

    payment_rows = [r for r in schedule if not r['is_disbursement']]
    total_payment = sum(row['payment'] for row in payment_rows)
    total_interest = sum(row['interest'] for row in payment_rows)
    total_principal = sum(row['principal'] for row in payment_rows)

    calc_method = 'Reducing Balance'
    if loan.loan_type and loan.loan_type.interest_calc_method:
        method = loan.loan_type.interest_calc_method
        calc_method = method.replace('_', ' ').title()

    # ── PDF Generation ──────────────────────────────────────────────
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch,
                            leftMargin=0.5 * inch, rightMargin=0.5 * inch)

    styles = getSampleStyleSheet()
    BRAND_NAVY = colors.HexColor("#0E2B4D")
    BRAND_GOLD = colors.HexColor("#B58A3E")
    LIGHT_BG = colors.HexColor("#F5F7FA")
    DISBURSE_BG = colors.HexColor("#D6E4F0")

    title_style = ParagraphStyle('Title', parent=styles['Heading1'],
                                 fontSize=16, textColor=BRAND_NAVY,
                                 alignment=TA_CENTER, spaceAfter=4)
    subtitle_style = ParagraphStyle('Subtitle', parent=styles['Normal'],
                                    fontSize=10, textColor=colors.grey,
                                    alignment=TA_CENTER, spaceAfter=12)
    label_style = ParagraphStyle('Label', parent=styles['Normal'],
                                 fontSize=9, textColor=colors.grey)
    value_style = ParagraphStyle('Value', parent=styles['Normal'],
                                 fontSize=10, textColor=BRAND_NAVY)
    cell_right = ParagraphStyle('CellR', parent=styles['Normal'],
                                fontSize=8, alignment=TA_RIGHT)
    cell_left = ParagraphStyle('CellL', parent=styles['Normal'],
                               fontSize=8, alignment=TA_LEFT)
    cell_center = ParagraphStyle('CellC', parent=styles['Normal'],
                                 fontSize=8, alignment=TA_CENTER)
    cell_bold = ParagraphStyle('CellBold', parent=styles['Normal'],
                               fontSize=8, fontName='Helvetica-Bold',
                               alignment=TA_LEFT)
    header_cell = ParagraphStyle('HdrC', parent=styles['Normal'],
                                 fontSize=8, textColor=colors.white,
                                 alignment=TA_CENTER)

    story = []

    # Title
    story.append(Paragraph("Loan Amortization Schedule", title_style))
    story.append(Paragraph(f"Generated on {timezone.localdate():%d %B %Y}", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=1, color=BRAND_GOLD, spaceAfter=12))

    # Loan summary info table
    info_data = [
        [Paragraph("Member", label_style),
         Paragraph(f"{loan.customer.full_name} ({loan.customer.cust_no})", value_style),
         Paragraph("Loan No", label_style),
         Paragraph(str(loan.loan_no), value_style)],
        [Paragraph("Product", label_style),
         Paragraph(loan.loan_type.account_name if loan.loan_type else "—", value_style),
         Paragraph("Calculation", label_style),
         Paragraph(calc_method, value_style)],
        [Paragraph("Principal", label_style),
         Paragraph(f"KES {loan.principal:,.2f}", value_style),
         Paragraph("Interest Rate", label_style),
         Paragraph(f"{loan.interest_rate}% p.m.", value_style)],
        [Paragraph("Period", label_style),
         Paragraph(f"{loan.loan_period} months", value_style),
         Paragraph("Installment", label_style),
         Paragraph(f"KES {loan.installment:,.2f}", value_style)],
    ]
    info_table = Table(info_data, colWidths=[1.1 * inch, 2.2 * inch, 1.1 * inch, 2.2 * inch])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('LINEBELOW', (0, -1), (-1, -1), 0.5, colors.lightgrey),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 16))

    # Schedule table header — now with Date and Description columns
    header = [
        Paragraph("#", header_cell),
        Paragraph("Date", header_cell),
        Paragraph("Description", header_cell),
        Paragraph("Payment", header_cell),
        Paragraph("Principal", header_cell),
        Paragraph("Interest", header_cell),
        Paragraph("Balance", header_cell),
    ]

    table_data = [header]
    disburse_row_idx = None  # track for styling

    for idx, row in enumerate(schedule):
        if row['is_disbursement']:
            disburse_row_idx = idx + 1  # +1 because header is row 0
            table_data.append([
                Paragraph("—", cell_center),
                Paragraph(row['date_label'], cell_bold),
                Paragraph(row['description'], cell_bold),
                Paragraph("—", cell_center),
                Paragraph("—", cell_center),
                Paragraph("—", cell_center),
                Paragraph(f"{row['balance']:,.2f}",
                          ParagraphStyle('DisBal', parent=cell_right,
                                         fontName='Helvetica-Bold')),
            ])
        else:
            table_data.append([
                Paragraph(str(row['month']), cell_center),
                Paragraph(row['date_label'], cell_left),
                Paragraph(row['description'], cell_left),
                Paragraph(f"{row['payment']:,.2f}", cell_right),
                Paragraph(f"{row['principal']:,.2f}", cell_right),
                Paragraph(f"{row['interest']:,.2f}", cell_right),
                Paragraph(f"{row['balance']:,.2f}", cell_right),
            ])

    # Totals row
    bold_center = ParagraphStyle('BoldC', parent=cell_center, fontName='Helvetica-Bold')
    bold_right = ParagraphStyle('BoldR', parent=cell_right, fontName='Helvetica-Bold')
    table_data.append([
        Paragraph("", bold_center),
        Paragraph("", bold_center),
        Paragraph("Total", bold_center),
        Paragraph(f"{total_payment:,.2f}", bold_right),
        Paragraph(f"{total_principal:,.2f}", bold_right),
        Paragraph(f"{total_interest:,.2f}", bold_right),
        Paragraph("—", bold_center),
    ])

    col_widths = [0.35 * inch, 0.75 * inch, 1.15 * inch, 1.1 * inch, 1.1 * inch, 1.0 * inch, 1.15 * inch]
    sched_table = Table(table_data, colWidths=col_widths, repeatRows=1)

    sched_style = [
        # Header row
        ('BACKGROUND', (0, 0), (-1, 0), BRAND_NAVY),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        # Body rows
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
        ('TOPPADDING', (0, 1), (-1, -1), 5),
        ('GRID', (0, 0), (-1, -1), 0.3, colors.lightgrey),
        # Totals row
        ('BACKGROUND', (0, -1), (-1, -1), LIGHT_BG),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('LINEABOVE', (0, -1), (-1, -1), 1, BRAND_NAVY),
    ]

    # Disbursement row highlight
    if disburse_row_idx is not None:
        sched_style.append(('BACKGROUND', (0, disburse_row_idx), (-1, disburse_row_idx), DISBURSE_BG))
        sched_style.append(('FONTNAME', (0, disburse_row_idx), (-1, disburse_row_idx), 'Helvetica-Bold'))

    # Alternate row shading for payment rows (skip disbursement and totals)
    for i in range(1, len(table_data) - 1):
        if i == disburse_row_idx:
            continue
        if i % 2 == 0:
            sched_style.append(('BACKGROUND', (0, i), (-1, i), LIGHT_BG))

    sched_table.setStyle(TableStyle(sched_style))
    story.append(sched_table)

    # Footer
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey, spaceAfter=6))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'],
                                  fontSize=7, textColor=colors.grey, alignment=TA_CENTER)
    story.append(Paragraph(
        "This schedule is indicative and assumes regular monthly payments "
        "starting from the month after disbursement. "
        "Actual amounts may vary with early/late payments or restructuring.",
        footer_style
    ))

    doc.build(story)

    response = HttpResponse(buf.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="Amortization_{loan.loan_no}.pdf"'
    return response
