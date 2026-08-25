from django import forms
from .models import StatementSchedule

class StatementFilterForm(forms.Form):
    cust_no = forms.CharField(required=True)
    account_code = forms.CharField(required=True)
    from_date = forms.DateField(required=False)
    to_date = forms.DateField(required=False)

class StatementScheduleForm(forms.ModelForm):
    class Meta:
        model = StatementSchedule
        fields = ['day_of_month', 'is_active']
        widgets = {
            'day_of_month': forms.NumberInput(attrs={'class': 'form-control', 'min': 1, 'max': 28}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }