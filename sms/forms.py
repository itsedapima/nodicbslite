from django import forms
from .models import FrequentNotification


class ComposeSMSForm(forms.Form):
    """
    Lean SMS form — no ModelChoiceField, no DB hits on render.
    Customer selection is handled via a hidden input populated by AJAX search.
    """
    
    RECIPIENT_CHOICES = (
        ('single', 'Single Phone Number'),
        ('customer_group', 'Selected Customers'),
        ('all_customers', 'All Customers'),
        ('file_upload', 'Upload File (CSV/Excel)'),
    )
    
    recipient_type = forms.ChoiceField(
        choices=RECIPIENT_CHOICES,
        initial='single',
        required=True,
    )
    
    phone_number = forms.CharField(
        max_length=20,
        required=False,
    )
    
    # Plain CharField — stores comma-separated customer IDs from JS
    customer_ids = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )
    
    phone_file = forms.FileField(
        required=False,
    )
    
    salutation = forms.CharField(
        max_length=100,
        initial='Dear {first_name},',
        required=True,
    )
    
    message_content = forms.CharField(
        widget=forms.Textarea(),
        required=True,
    )
    
    def clean_customer_ids(self):
        """Parse comma-separated IDs into a list of integers."""
        raw = self.cleaned_data.get('customer_ids', '').strip()
        if not raw:
            return []
        try:
            return [int(x) for x in raw.split(',') if x.strip().isdigit()]
        except (ValueError, AttributeError):
            return []
    
    def clean(self):
        cleaned_data = super().clean()
        recipient_type = cleaned_data.get('recipient_type')
        phone_number = (cleaned_data.get('phone_number') or '').strip()
        customer_ids = cleaned_data.get('customer_ids', [])
        phone_file = cleaned_data.get('phone_file')
        
        if recipient_type == 'single' and not phone_number:
            self.add_error('phone_number', 'Phone number is required.')
        elif recipient_type == 'customer_group' and not customer_ids:
            self.add_error('customer_ids', 'Please select at least one customer.')
        elif recipient_type == 'file_upload':
            if not phone_file:
                self.add_error('phone_file', 'Please upload a file.')
            else:
                fname = phone_file.name.lower()
                if not (fname.endswith('.csv') or fname.endswith('.xlsx') or fname.endswith('.xls')):
                    self.add_error('phone_file', 'File must be CSV or Excel format.')
        
        return cleaned_data


# ═══════════════════════════════════════════════════════════════════════════
#  FREQUENT NOTIFICATION FORM
# ═══════════════════════════════════════════════════════════════════════════

class FrequentNotificationForm(forms.ModelForm):
    """
    ModelForm for creating/editing FrequentNotification templates.
    The message_template textarea uses a larger default size and
    placeholder guidance. Balance filter fields appear for marketing
    categories that support balance-based targeting.
    """

    class Meta:
        model = FrequentNotification
        fields = [
            'name', 'category', 'message_template',
            'balance_filter_min', 'balance_filter_max',
            'is_active',
        ]
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. Monthly Savings Deposit Reminder',
            }),
            'category': forms.Select(attrs={
                'class': 'form-control',
            }),
            'message_template': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 5,
                'placeholder': (
                    'Dear {first_name}, to deposit to your Savings account, '
                    'use M-Pesa Paybill {paybill}, Account {account_no}. '
                    'Thank you - {sacco_name}'
                ),
            }),
            'balance_filter_min': forms.NumberInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. 5000',
                'step': '0.01',
            }),
            'balance_filter_max': forms.NumberInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. 100000',
                'step': '0.01',
            }),
            'is_active': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
        }

    def clean_message_template(self):
        template = self.cleaned_data.get('message_template', '').strip()
        if not template:
            raise forms.ValidationError('Message template cannot be empty.')
        if len(template) > 480:
            raise forms.ValidationError(
                f'Template is {len(template)} characters. '
                'Keep it under 480 to fit within 3 SMS segments.'
            )
        return template
