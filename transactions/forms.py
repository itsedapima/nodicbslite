from django import forms
from django.forms import formset_factory
from django.utils import timezone

class TransactionForm(forms.Form):
    cust_no = forms.CharField(label="Customer No")
    # Changed to DateField and set widget to type="date"
    date = forms.DateField(label="Transaction Date", initial=timezone.now, 
                           widget=forms.DateInput(attrs={
            "type": "date", 
            "class": "form-control"
        }))
    amount_paid = forms.DecimalField(max_digits=14, decimal_places=2, required=True)
    share_amt = forms.DecimalField(max_digits=14, decimal_places=2, required=False, initial=0)
    savings_amt = forms.DecimalField(max_digits=14, decimal_places=2, required=False, initial=0)
    description = forms.CharField(max_length=255, required=True)

    def clean(self):
        cleaned = super().clean()
        # additional validations can go here
        return cleaned

from django import forms
from loans.models import LoanHistory

class LoanPaymentForm(forms.Form):
    # We use ModelChoiceField to link directly to the LoanHistory model
    loan = forms.ModelChoiceField(
        queryset=LoanHistory.objects.none(), # Start empty
        label="Select Loan",
        to_field_name="loan_no", # This ensures the value submitted is the Loan Number
        required=False,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    amount = forms.DecimalField(max_digits=14, decimal_places=2, required=False)

    def __init__(self, *args, **kwargs):
        # Pop the customer_id so it doesn't interfere with standard form init
        customer_id = kwargs.pop('customer_id', None)
        super(LoanPaymentForm, self).__init__(*args, **kwargs)
        
        if customer_id:
            # Filter loans belonging only to this customer
            self.fields['loan'].queryset = LoanHistory.objects.filter(customer__cust_no=customer_id)
            # Update labels to show Loan No and Type for clarity
            self.fields['loan'].label_from_instance = lambda obj: f"{obj.loan_no} ({obj.get_loan_type_display()})"
from django import forms
from .models import CustomerAccountsSetup

class CustomerAccountsSetupForm(forms.ModelForm):
    INTEREST_CHOICES = [
        ('', '--- Select Method ---'),
        ('pro_rata', 'Pro Rata'),
        ('flat_rate', 'Flat Rate'),
        ('principal_flat_rate', 'Principal Flat Rate'),
        ('reducing_balance', 'Reducing Balance'),
    ]

    interest_calc_method = forms.ChoiceField(
        choices=INTEREST_CHOICES,
        required=False,
        widget=forms.Select(attrs={'class': 'form-select form-select-sm'})
    )

    is_loan = forms.ChoiceField(
        choices=[(True, 'Yes, this is a Loan Account'), (False, 'No, this is a Savings/Deposit Account')],
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'}),
        initial=False
    )

    # REMOVED FROM META: Explicitly decoupled UI component
    account_type = forms.CharField(
        required=False,
        label="Account Type (Auto-generated)",
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-sm bg-light', 
            'id': 'id_account_type', 
            'readonly': 'readonly'
        })
    )

    class Meta:
        model = CustomerAccountsSetup
        # Notice 'account_type' is omitted from here to kill the choice validation error
        fields = ['account_code', 'account_name', 'acc_initials', 'is_loan', 'interest_calc_method']
        widgets = {
            'account_code': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'e.g. ACC-001'}),
            'account_name': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'id': 'id_account_name', 'placeholder': 'e.g. Welfare Deposit'}),
            'acc_initials': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'e.g. WD'}),
        }
from django import forms
from django.forms import formset_factory
from decimal import Decimal

# --- Savings/Share Payment Form ---
class SavingsPaymentForm(forms.Form):
    # Retrieve the choices dynamically for the form
    CHOICES = [] 
    try:
        # Filter for savings and share types
        qs = CustomerAccountsSetup.objects.filter(
            account_type__in=['share_capital', 'seed_deposit', 'welfare_deposit', 'junior_account'],
            is_active=True
        ).values_list('account_type', 'account_name').order_by('account_code')
        CHOICES = [('', '---------')] + list(qs)
    except Exception:
        # Handle cases where the database or model isn't ready (e.g., migrations)
        CHOICES = [('', '---------'), ('share_capital', 'Share Capital Placeholder')]

    account_type = forms.ChoiceField(
        choices=CHOICES,
        label="Account Type",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    amount = forms.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        min_value=Decimal('0.01'), 
        label="Amount", 
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'})
    )

from django import forms
from decimal import Decimal

class LoanPaymentForm(forms.Form):
    # We define the field, but leave choices empty for now
    loan_no = forms.ChoiceField(
        label="Loan Account",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    amount = forms.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        min_value=Decimal('0.01'), 
        label="Amount", 
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'})
    )

    def __init__(self, *args, **kwargs):
        # 1. Pop 'loan_choices' out so super() doesn't complain
        loan_choices = kwargs.pop('loan_choices', [])
        super().__init__(*args, **kwargs)
        
        # 2. Assign the choices dynamically
        # loan_choices is a list of dicts: [{'id': 'LN01', 'name': 'LN01 - Emergency'}, ...]
        self.fields['loan_no'].choices = [('', '---------')] + [
            (c['id'], c['name']) for c in loan_choices
        ]
# --- Define Formsets ---
SavingsPaymentFormSet = formset_factory(SavingsPaymentForm, extra=1)
LoanPaymentFormSet = formset_factory(LoanPaymentForm, extra=1)

from django import forms
from .models import DividendDeclaration

class DividendCalculationForm(forms.ModelForm):
    class Meta:
        model = DividendDeclaration
        fields = [
            'title', 'start_date', 'end_date', 'total_profit', 
            'withholding_tax_percent', 'processing_fee', 'saving_type'
        ]
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'end_date': forms.DateInput(attrs={'type': 'date'}),
        }