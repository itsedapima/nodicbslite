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

def loan_dashboard(request):
    # Fetch last 50 loans for the table
    loans = LoanHistory.objects.select_related('customer').order_by('-id')[:50]
    return render(request, 'loans/dashboard.html', {'loans': loans})

def search_customer_api(request):
    cust_no = request.GET.get('cust_no')
    if cust_no:
        try:
            # Assuming cust_no is a string in URL but integer in DB
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
                
                # 3. Calculate Net Disbursed (Backend Verification)
                principal = loan.principal
                deductions = loan.processing_fee + loan.insurance_fee + loan.other_charges
                
                # Get dynamic lists from POST
                offset_accounts = request.POST.getlist('offset_accounts[]')
                offset_amounts = request.POST.getlist('offset_amounts[]')
                
                total_offset = Decimal(0)
                valid_offsets = []

                # Validate offsets
                for acc, amt in zip(offset_accounts, offset_amounts):
                    if amt and float(amt) > 0:
                        dec_amt = Decimal(amt)
                        total_offset += dec_amt
                        valid_offsets.append({'type': acc, 'amount': dec_amt})
                
                loan.net_disbursed = principal - (deductions + total_offset)
                loan.save()

                # 4. Create Main Loan Disbursement Transaction
                # Note: Transactions app uses PositiveIntegerField for cust_no
                cust_no_int = int(customer.cust_no) 
                tr_ref_main = make_tr_ref("LD")
                
                LoanTransaction.objects.create(
                    cust_no=cust_no_int,
                    loan_type=loan.loan_type,
                    tr_date=datetime.now(),
                    tr_ref=tr_ref_main,
                    tr_desc=f"Loan Disbursement {tr_ref_main}",
                    debit_amount=principal, # Debit the full principal
                    credit_amount=0,
                    created_by=loan.created_by
                )

                # 5. Process Offsets (Routing to specific tables)
                for off in valid_offsets:
                    acc_type = off['type']
                    amount = off['amount']
                    off_ref = make_tr_ref("OFF")
                    desc = f"Offset Recovery {off_ref} against {loan.loan_type}"

                    if acc_type == 'share_capital':
                        # Post to ShareCapitalTransaction
                        ShareCapitalTransaction.objects.create(
                            cust_no=cust_no_int,
                            saving_type='share_capital',
                            tr_date=datetime.now(),
                            tr_ref=off_ref,
                            tr_desc=desc,
                            debit_amount=0,
                            credit_amount=amount, # Credit checks/shares
                            created_by=loan.created_by
                        )
                    elif acc_type == 'savings':
                        # Post to SavingsTransaction
                        SavingsTransaction.objects.create(
                            cust_no=cust_no_int,
                            saving_type='savings', # or specific saving product
                            tr_date=datetime.now(),
                            tr_ref=off_ref,
                            tr_desc=desc,
                            debit_amount=0,
                            credit_amount=amount, # Credit savings
                            created_by=loan.created_by
                        )
                    else:
                        # It is a loan offset (normal, emergency, mobile)
                        # Post to LoanTransaction
                        LoanTransaction.objects.create(
                            cust_no=cust_no_int,
                            loan_id=loan,
                            loan_type=acc_type, # The loan being paid off
                            tr_date=datetime.now(),
                            tr_ref=off_ref,
                            tr_desc=desc,
                            debit_amount=0,
                            credit_amount=amount, # Credit the old loan
                            created_by=loan.created_by
                        )

                messages.success(request, f"Loan posted successfully! Net Disbursed: {loan.net_disbursed}")
                return redirect('loans:loan_dashboard')

            except Customer.DoesNotExist:
                messages.error(request, "Customer Number does not exist.")
            except Exception as e:
                messages.error(request, f"An error occurred: {str(e)}")
    else:
        form = LoanDispatchForm()

    return render(request, 'loans/loan_dispatch.html', {'form': form})

def make_tr_ref2(prefix):
    """Generates a unique transaction reference"""
    import random
    timestamp = datetime.now().strftime("%Y%m%d%H%M")
    rand = random.randint(100, 999)
    return f"{prefix}{timestamp}{rand}"

def calculate_savings_balance(cust_no):
    """
    Calculates savings balance manually since the method 
    is missing in the Customer model.
    Formula: Total Credits - Total Debits
    """
    agg = SavingsTransaction.objects.filter(cust_no=cust_no).aggregate(
        total_credit=Sum('credit_amount', default=0),
        total_debit=Sum('debit_amount', default=0)
    )
    # Handle None values if table is empty
    credits = agg['total_credit'] or Decimal(0)
    debits = agg['total_debit'] or Decimal(0)
    return credits - debits

def calculate_loan_balance(cust_no, loan_type, target_date):
    """
    Calculates outstanding loan balance based on transactions.
    Formula: Total Debits (Principal + Interest) - Total Credits (Repayments)
    """
    agg = LoanTransaction.objects.filter(
        cust_no=cust_no,
        loan_type=loan_type,
        tr_date__lte=target_date
    ).aggregate(
        total_debit=Sum('debit_amount', default=0),
        total_credit=Sum('credit_amount', default=0)
    )
    debits = agg['total_debit'] or Decimal(0)
    credits = agg['total_credit'] or Decimal(0)
    return debits - credits

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
                # 2. Calculate Balance using Transaction History (Transactions App)
                balance = calculate_loan_balance(loan.customer.cust_no, loan_type, target_date)
                
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