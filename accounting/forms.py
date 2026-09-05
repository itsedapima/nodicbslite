from django import forms
from .models import SaccoAccount, SaccoIncome, SaccoExpense
from django.utils import timezone

class SaccoAccountForm(forms.ModelForm):
    class Meta:
        model = SaccoAccount
        fields = ['account_code', 'account_name', 'account_group',
                  'is_cash_account', 'show_on_admin_app']
        widgets = {
            'is_cash_account': forms.CheckboxInput(attrs={
                'class': 'form-check-input', 'role': 'switch',
            }),
            'show_on_admin_app': forms.CheckboxInput(attrs={
                'class': 'form-check-input', 'role': 'switch',
            }),
        }
        labels = {
            'is_cash_account': 'Cash / Bank Account',
            'show_on_admin_app': 'Show on Admin App',
        }
        help_texts = {
            'is_cash_account': 'Enable for cash-book tracking (bank, petty cash, M-Pesa).',
            'show_on_admin_app': 'Make this account available for data entry in the mobile admin app.',
        }




class IncomeForm(forms.ModelForm):
    class Meta:
        model = SaccoIncome
        fields = ['income_date','reference','cheque_number', 'amount', 'description', 'sacco_account','income_file']
        widgets = {
            'income_date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={ 'rows': 2}),
        }


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Filter to only show accounts where account_group is "Income"
        self.fields['sacco_account'].queryset = SaccoAccount.objects.filter(account_group='Income')

class ExpenseForm(forms.ModelForm):
    class Meta:
        model = SaccoExpense
        fields = ['expense_date','reference','cheque_number','expense_invoice', 'amount', 'description', 'sacco_account','expense_file']
        widgets = {
            'expense_date': forms.DateInput(attrs={'type': 'date'}),
             'description': forms.Textarea(attrs={ 'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Filter to only show accounts where account_group is "Expenditure"
        self.fields['sacco_account'].queryset = SaccoAccount.objects.filter(account_group='Expenditure')

class IncomeReconciliationForm(forms.ModelForm):
    reconciliation_account = forms.ModelChoiceField(queryset=SaccoAccount.objects.filter(account_group='Current Asset'))

    class Meta:
        model = SaccoIncome
        fields = ['reconciliation_reference','reconciliation_account','reconciliation_file']
    
    def save(self, commit=True):
        income = super().save(commit=False)
        income.reconciliation_status = 'reconciled'
        if commit:
            income.save()
        return income


class ExpenseReconciliationForm(forms.ModelForm):
    reconciliation_account = forms.ModelChoiceField(queryset=SaccoAccount.objects.filter(account_group='Current Asset'))

    class Meta:
        model = SaccoExpense
        fields = ['reconciliation_reference','reconciliation_account','reconciliation_file']
    
    def save(self, commit=True):
        expense = super().save(commit=False)
        expense.reconciliation_status = 'reconciled'
        if commit:
            expense.save()
        return expense

from django import forms
from .models import AutomatedReport


class AutomatedReportForm(forms.ModelForm):
    class Meta:
        model = AutomatedReport
        fields = ['name', 'day_of_month']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].widget.attrs.update({'class': 'form-control'})
        self.fields['day_of_month'].widget.attrs.update({'class': 'form-control', 'min': 1, 'max': 31})
from django import forms
from .models import AutomatedReport

class AutomatedReportFormEdit(forms.ModelForm):
    class Meta:
        model = AutomatedReport
        fields = ['name', 'day_of_month']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'day_of_month': forms.NumberInput(attrs={'class': 'form-control', 'min': 1, 'max': 31}),
        }


from django import forms
from .models import SaccoAccount

class AccountFilterForm(forms.Form):
    account = forms.ModelChoiceField(
        queryset=SaccoAccount.objects.filter(account_group="Expenditure"),
        required=True,
        label="Select Account",
        empty_label="Select an Account"
    )
    start_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
        label="Start Date"
    )
    end_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
        label="End Date"
    )


# Forms
class ReconciliationFilterForm(forms.Form):
    account = forms.ModelChoiceField(
        queryset=SaccoAccount.objects.filter(account_group="Current Assets"),
        label="Select Account",
        empty_label="Select an Account",
    )
    start_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
        label="Start Date"
    )
    end_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
        label="End Date"
    )
from django import forms
from django.db.models import Sum
from .models import SaccoAccount
from transactions.models import  CustomerAccountsSetup, SavingsTransaction, LoanTransaction
from django import forms
from django.db.models import Sum
from .models import SaccoAccount

class JournalVoucherForm(forms.Form):
    TRANSACTION_TYPES = [
        ('debit_customer', 'Debit Customer -> Credit Company (Income)'),
        ('credit_customer', 'Credit Customer <- Debit Company (Expense)'),
    ]
    
    cust_no = forms.IntegerField(widget=forms.HiddenInput())
    #date = forms.DateTimeField(label="Date/Time", initial=timezone.now, widget=forms.DateTimeInput(attrs={"type":"datetime-local"}))
    date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
        label="Date"
    )
    # FIX: Use CharField with a Select widget instead of ChoiceField
    # This bypasses Django's strict choice validation for AJAX fields
    target_account = forms.CharField(
        widget=forms.Select(attrs={'class': 'form-select'}), 
        label="Target Customer Account",
        help_text="Select the specific savings or loan account"
    )
    
    transaction_direction = forms.ChoiceField(choices=TRANSACTION_TYPES, label="Transaction Direction")
    sacco_account = forms.ModelChoiceField(queryset=SaccoAccount.objects.all(), label="Ledger Account")
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01)
    reference = forms.CharField(max_length=50, label="External Reference")
    description = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}))

    def clean(self):
        cleaned_data = super().clean()
        cust_no = cleaned_data.get("cust_no")
        amount = cleaned_data.get("amount")
        direction = cleaned_data.get("transaction_direction")
        
        # Safe extraction using .get()
        target_account = cleaned_data.get("target_account") 

        # Validation: Prevent withdrawing more than the savings balance
        if direction == 'debit_customer' and target_account and not target_account.startswith('LN'):
            totals = SavingsTransaction.objects.filter(
                cust_no=cust_no, 
                saving_type=target_account
            ).aggregate(
                total_credit=Sum('credit_amount'),
                total_debit=Sum('debit_amount')
            )
            
            balance = (totals['total_credit'] or 0) - (totals['total_debit'] or 0)
            
            if amount > balance:
                raise forms.ValidationError(
                    f"Insufficient Funds! The current balance for this account is {balance:,.2f}."
                )
                
        return cleaned_data