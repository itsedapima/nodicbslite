from decimal import Decimal
from django import forms
from django.utils import timezone

# Local App Models
from .models import (
    Collateral,
    LoanCharge,
    LoanHistory,
)
from transactions.models import CustomerAccountsSetup
# --- 1. Custom Widget FIX ---
class LoanTypeSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if value:
            # FIX: ModelChoiceIteratorValue must be unwrapped to get the actual PK
            # We use getattr to safely get '.value' if it exists, else use value itself
            lookup_id = getattr(value, 'value', value)
            
            try:
                # Now the ORM gets the number/string it expects
                instance = CustomerAccountsSetup.objects.get(pk=lookup_id)
                option['attrs']['data-calc-method'] = instance.interest_calc_method
            except (CustomerAccountsSetup.DoesNotExist, ValueError, TypeError):
                # Fallback if the ID is invalid or empty (like the "---------")
                pass
        return option

class LoanDispatchForm(forms.ModelForm):
    cust_no = forms.CharField(
        label="Customer Number", 
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg', 
            'id': 'selectedCustomerNo', 
            'readonly': 'readonly',
            'placeholder': 'Select a borrower from search...'
        })
    )

    loan_type = forms.ModelChoiceField(
        queryset=CustomerAccountsSetup.objects.none(),
        empty_label="--- Select Loan Type ---",
        widget=forms.Select(attrs={'class': 'form-select doc-calc', 'id': 'id_loan_type'})
    )

    class Meta:
        model = LoanHistory
        fields = [
            'loan_date', 'principal', 'interest_rate', 'installment', 
            'loan_type', 'loan_period'
        ]
        widgets = {
            'loan_date': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'class': 'form-control', 'id': 'id_loan_date'}),
            'principal': forms.NumberInput(attrs={'class': 'form-control fw-bold text-primary doc-calc', 'id': 'id_principal', 'step': '0.01', 'placeholder': '0.00'}),
            'interest_rate': forms.NumberInput(attrs={'class': 'form-control doc-calc', 'id': 'id_interest_rate', 'step': '0.01', 'placeholder': 'e.g. 1.5'}),
            'installment': forms.NumberInput(attrs={'class': 'form-control bg-light fw-bold', 'id': 'id_installment', 'readonly': 'readonly'}),
            'loan_period': forms.NumberInput(attrs={'class': 'form-control doc-calc', 'id': 'id_loan_period', 'min': '1'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Dynamically load active loan products
        self.fields['loan_type'].queryset = CustomerAccountsSetup.objects.filter(is_active=True, is_loan_account=True)
        self.fields['loan_date'].initial = timezone.now().date()
        
        # Enforce flexible calculations; view logic handles safe structural fallbacks
        numeric_fields = ['installment', 'interest_rate', 'principal', 'loan_period']
        for field_name in numeric_fields:
            if field_name in self.fields:
                field = self.fields[field_name]
                field.required = False
                if field.initial is None:
                    field.initial = Decimal('0.00')

class InterestChargeForm(forms.Form):
    date = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'})
    )
    
    # Change to standard ChoiceField to pass pure string values
    loan_type = forms.ChoiceField(
        widget=forms.Select(attrs={'class': 'form-select'}),
        choices=[]  # Will be populated dynamically below
    )
    
    interest_rate = forms.DecimalField(
        label="Interest %", 
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': 'e.g. 1.5'})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Pull active configurations from the database
        active_setups = CustomerAccountsSetup.objects.filter(is_loan_account=True, is_active=True)
        
        # Build options mapping: (database_string_value, human_readable_label)
        choices = [("", "-- Select Loan Product --")]
        for setup in active_setups:
            choices.append((setup.account_type, f"{setup.account_code} - {setup.account_name}"))
            
        self.fields['loan_type'].choices = choices

class AddGuarantorForm(forms.Form):
    guarantor_no = forms.CharField(label="Guarantor Cust No", widget=forms.TextInput(attrs={'class': 'form-control'}))
    amount = forms.DecimalField(widget=forms.NumberInput(attrs={'class': 'form-control'}))


class CollateralForm(forms.ModelForm):
    class Meta:
        model = Collateral
        fields = [
            'collateral_type', 
            'market_value', 'forced_sale_value', 'mortgage_value', 'insurance_value',
            'title_deed_no', 'location', 'size',
            'registration_no', 'chassis_no', 'model',
            'description'
        ]
        widgets = {
            'collateral_type': forms.Select(attrs={'class': 'form-select', 'id': 'id_collateral_type'}),
            'description': forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
            # Add bootstrap classes to all inputs
            'market_value': forms.NumberInput(attrs={'class': 'form-control'}),
            'forced_sale_value': forms.NumberInput(attrs={'class': 'form-control'}),
            'mortgage_value': forms.NumberInput(attrs={'class': 'form-control'}),
            'insurance_value': forms.NumberInput(attrs={'class': 'form-control'}),
            
            # Specifics
            'title_deed_no': forms.TextInput(attrs={'class': 'form-control land-field'}),
            'location': forms.TextInput(attrs={'class': 'form-control land-field'}),
            'size': forms.TextInput(attrs={'class': 'form-control land-field'}),
            
            'registration_no': forms.TextInput(attrs={'class': 'form-control vehicle-field'}),
            'chassis_no': forms.TextInput(attrs={'class': 'form-control vehicle-field'}),
            'model': forms.TextInput(attrs={'class': 'form-control vehicle-field'}),
        }

class ReplaceGuarantorForm(forms.Form):
    new_guarantor_no = forms.CharField(
        label="New Guarantor Customer No",
        widget=forms.TextInput(attrs={'class': 'form-control', 'id': 'id_new_guarantor'})
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        required=False
    )

from django import forms
from .models import LoanCharge, CustomerAccountsSetup

class LoanChargeForm(forms.ModelForm):
    class Meta:
        model = LoanCharge
        fields = ['name', 'description', 'loan_products', 'charge_type', 'amount', 'min_amount', 'max_amount', 'is_mandatory']
        
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Ledger Fee'}),
            'description': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Brief description'}),
            'loan_products': forms.SelectMultiple(attrs={'class': 'form-select', 'id': 'id_loan_product', 'size': '4'}),
            'charge_type': forms.Select(attrs={'class': 'form-select'}),
            'amount': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'min_amount': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'max_amount': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'is_mandatory': forms.RadioSelect(choices=[(True, 'Yes'), (False, 'No')]),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # 1. Scope down queryset query directly
        active_loans = CustomerAccountsSetup.objects.filter(is_loan_account=True, is_active=True)
        self.fields['loan_products'].queryset = active_loans
        
        # 2. Assign text representation structure dynamically
        self.fields['loan_products'].label_from_instance = lambda obj: f"{obj.account_code} - {obj.account_name}"
        
        # 3. Help text for multi-select
        self.fields['loan_products'].help_text = "Select one or more loan products this charge applies to."

# ══════════════════════════════════════════════════════════════════════
#  LOAN RESTRUCTURE FORM
# ══════════════════════════════════════════════════════════════════════

class LoanRestructureForm(forms.Form):
    """
    Fields captured to restructure a loan.
    After submission, the loan is re-originated as if it were a new
    facility: the outstanding balance becomes the new principal, a new
    loan_date is stamped (so aging resets), and a new installment /
    period is applied.
    """
    new_loan_date = forms.DateField(
        label="New Loan Date",
        help_text="Aging (arrears) will be recomputed as if the loan started on this date.",
        widget=forms.DateInput(attrs={
            "type": "date", "class": "form-control",
        }),
        required=True,
    )
    new_period = forms.IntegerField(
        label="New Repayment Period (Months)",
        min_value=1, max_value=120,
        widget=forms.NumberInput(attrs={
            "class": "form-control", "placeholder": "e.g. 12",
        }),
        required=True,
    )
    new_installment = forms.DecimalField(
        label="New Monthly Installment (KES)",
        min_value=Decimal("0.01"),
        max_digits=14, decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "form-control", "step": "0.01",
        }),
        help_text=(
            "Leave 0 to auto-calculate from the outstanding balance, "
            "new period, and product interest rate."
        ),
        required=False,
    )
    restructure_fee_rate = forms.DecimalField(
        label="Restructure Fee (%)",
        min_value=Decimal("0"), max_value=Decimal("100"),
        max_digits=5, decimal_places=2,
        initial=Decimal("10.00"),
        widget=forms.NumberInput(attrs={
            "class": "form-control", "step": "0.01",
        }),
        required=True,
    )
    reason = forms.CharField(
        label="Reason for Restructure",
        widget=forms.Textarea(attrs={
            "class": "form-control", "rows": 2,
            "placeholder": "e.g. Member requested extended tenure due to hardship...",
        }),
        max_length=500,
        required=True,
    )


# ══════════════════════════════════════════════════════════════════════
#  GUARANTOR OFFLOAD FORM
# ══════════════════════════════════════════════════════════════════════

class GuarantorOffloadForm(forms.Form):
    """
    Offload a defaulted loan to guarantors. The officer confirms the
    calculated distribution before it is posted.
    """
    new_loan_period = forms.IntegerField(
        label="Recovery Loan Period (Months)",
        min_value=1, max_value=60,
        initial=12,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
        required=True,
    )
    interest_rate = forms.DecimalField(
        label="Interest Rate on Recovery Loans (%)",
        min_value=Decimal("0"), max_value=Decimal("100"),
        max_digits=5, decimal_places=2,
        initial=Decimal("0.00"),
        widget=forms.NumberInput(attrs={
            "class": "form-control", "step": "0.01",
        }),
        required=True,
        help_text="Typically 0% for recovery loans."
    )
    reason = forms.CharField(
        label="Reason / Officer Notes",
        widget=forms.Textarea(attrs={
            "class": "form-control", "rows": 2,
        }),
        max_length=500,
        required=True,
    )
    confirm = forms.BooleanField(
        label=(
            "I confirm the distribution shown below is correct and I want "
            "to post it. This action cannot be undone."
        ),
        required=True,
    )
