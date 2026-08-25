from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from decimal import Decimal
from datetime import datetime
from sms.utils import send_sms

# Import models from your main loans app
from customers.models import Customer
from.models import LoanHistory, Guarantor
from .forms import LoanDispatchForm, InterestChargeForm, AddGuarantorForm

# Import models from the Transactions App
from transactions.models import LoanTransaction, SavingsTransaction, ShareCapitalTransaction
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from decimal import Decimal
from datetime import datetime

# 1. Import local models and forms
from .models import LoanHistory, Guarantor
from .forms import LoanDispatchForm, InterestChargeForm, AddGuarantorForm

# 2. Import External Models (Customers & Transactions Apps)
from customers.models import Customer
from transactions.models import LoanTransaction, SavingsTransaction, ShareCapitalTransaction

# --- Helper Functions ---

# Helper to generate references (simulating your MakeTrRef)
def make_tr_ref(prefix):
    import random
    # Example: LD20231027999
    timestamp = datetime.now().strftime("%Y%m%d")
    rand = random.randint(100, 999)
    return f"{prefix}{timestamp}{rand}"

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from decimal import Decimal
from datetime import datetime
import math

# 1. Import local models and forms
from .models import LoanHistory, Guarantor
from .forms import LoanDispatchForm, InterestChargeForm, AddGuarantorForm

# 2. Import External Models (Customers & Transactions Apps)
from customers.models import Customer
from transactions.models import LoanTransaction, SavingsTransaction, ShareCapitalTransaction

# --- Helper Functions ---
def make_tr_ref(prefix):
    import random
    timestamp = datetime.now().strftime("%Y%m%d%H%M")
    rand = random.randint(100, 999)
    return f"{prefix}{timestamp}{rand}"
def loan_dashboard(request):
    # Fetch last 50 loans for the table
    loans = LoanHistory.objects.select_related('customer').order_by('-id')[:50]
    return render(request, 'loans/dashboard.html', {'loans': loans})
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

@transaction.atomic
def loan_dispatch(request):
    if request.method == 'POST':
        form = LoanDispatchForm(request.POST)
        if form.is_valid():
            try:
                # 1. Validate Customer
                cust_no_input = form.cleaned_data['cust_no']
                customer = Customer.objects.get(cust_no=cust_no_input)
                
                # 2. Prepare LoanHistory Object
                loan = form.save(commit=False)
                loan.customer = customer
                loan.created_by = request.user.username if request.user.is_authenticated else "system"
                
                # --- AUTO-CALCULATE FEES (Backend Enforcement) ---
                # Insurance: 1% of Principal
                loan.insurance_fee = loan.principal * Decimal("0.01")
                
                # Processing Fee: 0.75% of Principal
                loan.processing_fee = loan.principal * Decimal("0.0075")
                
                # --- CALCULATE MONTHLY INSTALLMENT (12% p.a => 1% p.m) ---
                rate_per_month = Decimal("0.12") / Decimal("12") # 0.01
                n_months = Decimal(loan.loan_period)
                
                if rate_per_month > 0 and n_months > 0:
                    # Amortization Formula: P * r * (1+r)^n / ((1+r)^n - 1)
                    numerator = loan.principal * rate_per_month * ((1 + rate_per_month) ** n_months)
                    denominator = ((1 + rate_per_month) ** n_months) - 1
                    loan.installment = numerator / denominator
                else:
                    loan.installment = loan.principal / n_months if n_months > 0 else 0

                # 3. Handle Offsets
                offset_accounts = request.POST.getlist('offset_accounts[]')
                offset_amounts = request.POST.getlist('offset_amounts[]')
                
                total_offset_principal = Decimal(0)
                total_offset_fees = Decimal(0)
                valid_offsets = []

                for acc, amt in zip(offset_accounts, offset_amounts):
                    if amt and float(amt) > 0:
                        dec_amt = Decimal(amt)
                        
                        # Calculate 10% Offset Fee
                        fee = dec_amt * Decimal("0.10")
                        
                        total_offset_principal += dec_amt
                        total_offset_fees += fee
                        
                        valid_offsets.append({
                            'type': acc, 
                            'amount': dec_amt,
                            'fee': fee
                        })
                
                # 4. Calculate Net Disbursed
                # Net = Principal - (Ins + Proc + Other + Offsets + Offset Fees)
                total_deductions = (loan.insurance_fee + 
                                    loan.processing_fee + 
                                    (loan.other_charges or 0) + 
                                    total_offset_principal + 
                                    total_offset_fees)
                
                loan.net_disbursed = loan.principal - total_deductions
                
                # Save the LoanHistory record
                loan.save()

                # 5. Create Transactions
                cust_no_int = int(customer.cust_no) 
                tr_ref_main = make_tr_ref("LD")

                # A. Main Disbursement (Debit Principal)
                LoanTransaction.objects.create(
                    cust_no=cust_no_int,
                    loan_id=loan.id, # Storing the ID of the loan history
                    loan_type=loan.loan_type,
                    tr_date=datetime.now(),
                    tr_ref=tr_ref_main,
                    tr_desc=f"Loan Disbursement (Principal: {loan.principal})",
                    debit_amount=loan.principal,
                    credit_amount=0,
                    created_by=loan.created_by
                )

                # B. Process Offsets & Fees
                for off in valid_offsets:
                    acc_type = off['type']
                    amount = off['amount']
                    fee = off['fee']
                    
                    # 1. Post the Offset Amount (Recovery)
                    off_ref = make_tr_ref("OFF")
                    desc = f"Offset Recovery against {loan.loan_type}"
                    
                    if acc_type in ['share_capital', 'savings']:
                        # Determine model
                        #Model = ShareCapitalTransaction if acc_type == 'share_capital' else SavingsTransaction
                        SavingsTransaction.objects.create(
                            cust_no=cust_no_int,
                            saving_type=acc_type,
                            tr_date=datetime.now(),
                            tr_ref=off_ref,
                            tr_desc=desc,
                            debit_amount=0,
                            credit_amount=amount, # Credit the savings/shares
                            created_by=loan.created_by
                        )
                    else:
                        # It is a loan offset
                        LoanTransaction.objects.create(
                            cust_no=cust_no_int,
                            loan_id=loan.id, # Linking to current loan or generally the customer
                            loan_type=acc_type, # The old loan type being paid off
                            tr_date=datetime.now(),
                            tr_ref=off_ref,
                            tr_desc=desc,
                            debit_amount=0,
                            credit_amount=amount, # Credit (repay) the old loan
                            created_by=loan.created_by
                        )

                    # 2. Post the Offset Fee (10%)
                    # We record this as a generic LoanTransaction credit (deduction) or specific fee record
                    # Here we treat it as a credit to the new loan account (deduction from proceeds)
                    # or a separate charge. Let's record it visibly.
                    fee_ref = make_tr_ref("FEE")
                    LoanTransaction.objects.create(
                        cust_no=cust_no_int,
                        loan_id=loan.id,
                        loan_type=loan.loan_type,
                        tr_date=datetime.now(),
                        tr_ref=fee_ref,
                        tr_desc=f"Bridging/Offset Fee (10% of {amount})",
                        debit_amount=fee,  # Debiting the customer (charge)
                        credit_amount=0,   
                        created_by=loan.created_by
                    )

                messages.success(request, f"Loan posted successfully! Net Disbursed: {loan.net_disbursed:,.2f}")
                return redirect('loans:loan_dashboard')

            except Customer.DoesNotExist:
                messages.error(request, "Customer Number does not exist.")
            except Exception as e:
                import traceback
                print(traceback.format_exc())
                messages.error(request, f"An error occurred: {str(e)}")
    else:
        form = LoanDispatchForm()

    return render(request, 'loans/loan_dispatch.html', {'form': form})
# loans/views.py

# ... (Keep your existing imports) ...
from django.db.models import Sum
from decimal import Decimal

# --- Helper Functions (Add these or ensure they exist) ---
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

def classify_repayment_score(score):
    """Classifies customer based on repayment score (0-100)."""
    if score >= 70:
        return "Excellent"  # >= 70%
    elif score >= 50:
        return "Fair"      # 50% - 69%
    elif score >= 30:
        return "Risky"     # 30% - 49%
    else:
        return "High Risk" # < 30%

def view_appraisal(request, pk):
    loan = get_object_or_404(LoanHistory.objects.select_related('customer'), pk=pk)
    customer = loan.customer
    
    # 1. Financial Balances
    savings_balance = calculate_savings_balance(customer.cust_no)
    share_capital_balance = calculate_share_balance(customer.cust_no)
    
    # 2. Existing Loans (Filter out the current loan being appraised if it's already saved)
    existing_loans = LoanHistory.objects.filter(customer=customer).exclude(pk=loan.pk)
    loan_balances = []
    
    for existing_loan in existing_loans:
        # Note: You would need a method to calculate the remaining *principal* balance only.
        # For simplicity, we use the aggregate balance across all transactions for that loan type.
        balance = calculate_loan_balance(customer.cust_no, existing_loan.loan_no)
        if balance > 0:
            loan_balances.append({
                'type': existing_loan.get_loan_type_display(),
                'principal': existing_loan.principal,
                'balance': balance,
                'loan_no': existing_loan.loan_no,      # Add this
                'loan_date': existing_loan.loan_date
            })
            
    # 3. Security (Guarantors & Collateral)
    guarantors = Guarantor.objects.filter(loan=loan).select_related('guarantor_cust')
    collaterals = Collateral.objects.filter(loan=loan)
    
    # 4. Customer Repayment Classification (Mock Score)
    # later we will develop a system to generate a real 'repayment_score' based on historical data.*
    # We will mock a score for demonstration.
    mock_repayment_score = 65 # Example Score
    customer_classification = classify_repayment_score(mock_repayment_score)

    context = {
        'loan': loan,
        'customer': customer,
        'guarantors': guarantors,
        'collaterals': collaterals,
        'savings_balance': savings_balance,
        'share_capital_balance': share_capital_balance,
        'loan_balances': loan_balances,
        'repayment_score': mock_repayment_score,
        'classification': customer_classification,
    }
    
    # Render the appraisal report template
    return render(request, 'loans/loan_appraisal_report.html', context)
# --- Views ---

def interest_charge(request):
    preview_data = []
    
    if request.method == 'POST':
        form = InterestChargeForm(request.POST)
        if form.is_valid():
            target_date = form.cleaned_data['date']
            loan_type = form.cleaned_data['loan_type']
            rate = form.cleaned_data['interest_rate']
            action = request.POST.get('action') # 'preview' or 'post'

            # 1. Get all loans of this type issued on or before the date
            # Note: We query LoanHistory to find *who* has this loan type
            loans = LoanHistory.objects.filter(loan_type=loan_type, loan_date__lte=target_date)
            
            for loan in loans:
                balance = calculate_loan_balance(loan.customer.cust_no, loan_type,target_date)
                
                if balance > 0:
                    interest = (balance * (rate / 100)).quantize(Decimal("0.01"))
                    if interest > 0:
                         preview_data.append({
                            'cust_no': loan.customer.cust_no,
                            'name': loan.customer.full_name,
                            'mobile': loan.customer.mobile, # Added for SMS
                            'balance': balance,
                            'interest': interest
                        })

            # 3. Handle Posting
            if action == 'post' and preview_data:
                with transaction.atomic():
                    count = 0
                    for item in preview_data:
                        tr_ref = make_tr_ref("INT")
                        
                        # Create Transaction in Transactions App
                        LoanTransaction.objects.create(
                            cust_no=item['cust_no'],
                            tr_date=target_date,
                            loan_type=loan_type,
                            tr_ref=tr_ref,
                            tr_desc=f"{target_date.strftime('%B')} Interest",
                            debit_amount=item['interest'], # Interest increases debt (Debit)
                            credit_amount=0,
                            created_by=request.user.username if request.user.is_authenticated else 'system'
                        )
                        
                        # Send SMS Logic (Placeholder)
                        try:
                            msg = f"Dear {item['name']}, your {loan_type} has been charged interest of Ksh {item['interest']}."
                            # send_sms(item['mobile'], msg) 
                            print(f"SMS to {item['mobile']}: {msg}")
                        except:
                            pass 
                        count += 1
                    
                    messages.success(request, f"Posted interest for {count} accounts.")
                    return redirect('loans:loan_dashboard')

    else:
        form = InterestChargeForm()

    return render(request, 'loans/interest_charge.html', {'form': form, 'preview_data': preview_data})


@transaction.atomic
def add_guarantor(request, loan_id):
    # Get the specific loan we are adding a guarantor to
    loan = get_object_or_404(LoanHistory, id=loan_id)
    existing_guarantors = Guarantor.objects.filter(loan=loan)
    
    if request.method == 'POST':
        form = AddGuarantorForm(request.POST)
        if form.is_valid():
            g_cust_no = form.cleaned_data['guarantor_no'] # This is the input string/int
            amount = form.cleaned_data['amount']
            
            try:
                # 1. Find the Guarantor in Customers App
                # Ensure we cast to int because Customer.cust_no is PositiveIntegerField
                guarantor_cust = Customer.objects.get(cust_no=int(g_cust_no))
                
                # 2. Prevent Self-Guaranteeing
                if guarantor_cust.cust_no == loan.customer.cust_no:
                    messages.error(request, "A member cannot guarantee their own loan.")
                    return redirect('loans:add_guarantor', loan_id=loan.id)

                # 3. Calculate Savings Balance (Transactions App)
                balance = calculate_savings_balance(guarantor_cust.cust_no)

                # Validation 4: Amount > Balance
                if amount > balance:
                    messages.error(request, f"Amount ({amount}) exceeds guarantor's savings balance ({balance}).")
                    return redirect('loans:add_guarantor', loan_id=loan.id)

                # Validation 5: Total guarantees > 3x Balance
                # Check all guarantees this person has given across ALL loans
                current_guarantees_agg = Guarantor.objects.filter(guarantor_cust=guarantor_cust).aggregate(total=Sum('amount'))
                current_total = current_guarantees_agg['total'] or Decimal(0)
                
                if (current_total + amount) > (balance * 3):
                    limit = balance * 3
                    messages.error(request, f"Limit Reached. Total active guarantees ({current_total}) + new ({amount}) exceeds 3x savings ({limit}).")
                    return redirect('loans:add_guarantor', loan_id=loan.id)
                
                # Save
                Guarantor.objects.create(
                    loan=loan, 
                    guarantor_cust=guarantor_cust, 
                    amount=amount
                )
                messages.success(request, f"Guarantor {guarantor_cust.full_name} added successfully!")
                return redirect('loans:add_guarantor', loan_id=loan.id)

            except Customer.DoesNotExist:
                messages.error(request, f"Customer number {g_cust_no} not found.")
            except ValueError:
                 messages.error(request, "Invalid customer number format.")
    
    else:
        form = AddGuarantorForm()

    return render(request, 'loans/add_guarantor.html', {
        'loan': loan, 
        'form': form, 
        'guarantors': existing_guarantors
    })

# Include other views (loan_dashboard, loan_dispatch, etc.) here...
from .models import Collateral # Add Collateral to imports
from .forms import CollateralForm, ReplaceGuarantorForm # Add forms to imports

# ... (Keep existing views) ...

def view_guarantors(request):
    # List all guarantors, grouped by loan could be better, but here is a flat list
    # Ordered by most recent loan
    guarantors = Guarantor.objects.select_related('loan', 'guarantor_cust', 'loan__customer').order_by('-loan__id')
    return render(request, 'loans/view_guarantors.html', {'guarantors': guarantors})

@transaction.atomic
def replace_guarantor(request, guarantor_id):
    old_guarantor = get_object_or_404(Guarantor, id=guarantor_id)
    loan = old_guarantor.loan
    
    if request.method == 'POST':
        form = ReplaceGuarantorForm(request.POST)
        if form.is_valid():
            new_cust_no = form.cleaned_data['new_guarantor_no']
            amount = old_guarantor.amount # We are swapping the exact amount
            
            try:
                # 1. Get New Guarantor
                new_cust = Customer.objects.get(cust_no=int(new_cust_no))
                
                # 2. Self-Guarantee Check
                if new_cust.cust_no == loan.customer.cust_no:
                    messages.error(request, "Borrower cannot be their own guarantor.")
                    return redirect('loans:replace_guarantor', guarantor_id=guarantor_id)

                # 3. Check Balance Limits (Reusing your logic)
                balance = calculate_savings_balance(new_cust.cust_no)
                if amount > balance:
                    messages.error(request, f"New guarantor insufficient balance. Need {amount}, Has {balance}.")
                    return redirect('loans:replace_guarantor', guarantor_id=guarantor_id)

                current_guarantees_agg = Guarantor.objects.filter(guarantor_cust=new_cust).aggregate(total=Sum('amount'))
                current_total = current_guarantees_agg['total'] or Decimal(0)
                
                if (current_total + amount) > (balance * 3):
                    messages.error(request, f"Limit Reached. New guarantor cannot cover this amount.")
                    return redirect('loans:replace_guarantor', guarantor_id=guarantor_id)

                # 4. Perform Swap
                # Optional: Log this swap in a history table if needed
                
                # Create new
                Guarantor.objects.create(loan=loan, guarantor_cust=new_cust, amount=amount)
                
                # Delete old
                old_name = old_guarantor.guarantor_cust.full_name
                old_guarantor.delete()
                
                messages.success(request, f"Successfully replaced {old_name} with {new_cust.full_name}")
                return redirect('loans:view_guarantors')

            except Customer.DoesNotExist:
                messages.error(request, "New customer number not found.")
            except ValueError:
                messages.error(request, "Invalid format.")

    else:
        form = ReplaceGuarantorForm()

    return render(request, 'loans/replace_guarantor.html', {
        'form': form, 
        'old_guarantor': old_guarantor
    })

def view_collaterals(request):
    collaterals = Collateral.objects.select_related('loan', 'owner').order_by('-created_at')
    return render(request, 'loans/view_collaterals.html', {'collaterals': collaterals})

@transaction.atomic
def add_collateral(request, loan_id):
    loan = get_object_or_404(LoanHistory, id=loan_id)
    
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

# loans/views.py
import csv
from django.http import HttpResponse
from .utils import get_loan_report_data

def export_loan_analysis(request):
    data = get_loan_report_data()
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="loan_analysis_report.csv"'

    writer = csv.writer(response)
    
    # Write Header
    if data:
        header = list(data[0].keys())
        writer.writerow(header)
        
        # Write Rows
        for row in data:
            writer.writerow(row.values())
            
    return response