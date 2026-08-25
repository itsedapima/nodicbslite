from django import forms
from .models import ChamaInfo, CompanyBranch, BackupConfiguration, Promotion


# ── Chama Info ───────────────────────────────────────────────────────────

class ChamaInfoForm(forms.ModelForm):
    class Meta:
        model = ChamaInfo
        fields = [
            'brand_name', 'brand_footer', 'chama_name', 'chama_address',
            'chama_contact', 'chama_location', 'chama_footer', 'chama_logo',
        ]
        widgets = {
            'brand_name':     forms.TextInput(attrs={'class': 'form-control'}),
            'brand_footer':   forms.TextInput(attrs={'class': 'form-control'}),
            'chama_name':     forms.TextInput(attrs={'class': 'form-control'}),
            'chama_address':  forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'chama_contact':  forms.TextInput(attrs={'class': 'form-control'}),
            'chama_location': forms.TextInput(attrs={'class': 'form-control'}),
            'chama_footer':   forms.TextInput(attrs={'class': 'form-control'}),
            'chama_logo':     forms.ClearableFileInput(attrs={'class': 'form-control'}),
        }


# ── Company Branch ───────────────────────────────────────────────────────

class CompanyBranchForm(forms.ModelForm):
    class Meta:
        model = CompanyBranch
        exclude = ['branch_code']
        widgets = {
            'name':           forms.TextInput(attrs={'class': 'form-control'}),
            'email':          forms.EmailInput(attrs={'class': 'form-control'}),
            'phone_number':   forms.TextInput(attrs={'class': 'form-control'}),
            'street_address': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'city':           forms.TextInput(attrs={'class': 'form-control'}),
            'state_province': forms.TextInput(attrs={'class': 'form-control'}),
            'postal_code':    forms.TextInput(attrs={'class': 'form-control'}),
            'country':        forms.TextInput(attrs={'class': 'form-control'}),
            'timezone':       forms.TextInput(attrs={'class': 'form-control'}),
        }


# ── Backup Settings ──────────────────────────────────────────────────────

class BackupSettingsForm(forms.ModelForm):
    class Meta:
        model = BackupConfiguration
        fields = ['email_recipient', 'interval_hours', 'is_active']
        widgets = {
            'email_recipient': forms.EmailInput(attrs={'class': 'form-control form-control-sm'}),
            'interval_hours':  forms.NumberInput(attrs={'class': 'form-control form-control-sm'}),
            'is_active':       forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


# ── Promotions ───────────────────────────────────────────────────────────

class PromotionForm(forms.ModelForm):
    """Form to create/edit a Promotion (mobile app ad / update)."""
    class Meta:
        model = Promotion
        fields = [
            'title', 'subtitle', 'body', 'image', 'promo_type',
            'cta_label', 'cta_url',
            'is_active', 'display_order', 'start_date', 'end_date',
        ]
        widgets = {
            'title':         forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. New Mobile Loan Product'}),
            'subtitle':      forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Optional second line'}),
            'body':          forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'image':         forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'promo_type':    forms.Select(attrs={'class': 'form-select'}),
            'cta_label':     forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Apply now'}),
            'cta_url':       forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://...'}),
            'is_active':     forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'display_order': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'start_date':    forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}),
            'end_date':      forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}),
        }

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get('start_date')
        end = cleaned.get('end_date')
        if start and end and end < start:
            raise forms.ValidationError({'end_date': 'End date must be after start date.'})
        return cleaned


# ── Notification Management ──────────────────────────────────────────────

class BulkNotificationForm(forms.Form):
    ACTION_CHOICES = [
        ("temp_enable_all", "Temporarily enable all notifications"),
        ("temp_disable_all", "Temporarily disable all notifications"),
        ("reset_all", "Reset to individual defaults"),
    ]
    action = forms.ChoiceField(choices=ACTION_CHOICES, widget=forms.HiddenInput())
    confirm = forms.BooleanField(
        required=True,
        label="I understand this affects all customers",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


# ── Mobile Activity Management ──────────────────────────────────────────

try:
    from androidapi.models import MobileActivity
except ImportError:
    MobileActivity = None


class MobileActivityForm(forms.ModelForm):
    """Create / edit a MobileActivity record (per-customer B2C authorization)."""
    class Meta:
        model = MobileActivity if MobileActivity else None
        fields = [
            'cust_no', 'full_name', 'phone', 'national_id',
            'username', 'device_id', 'authorize_withdrawal', 'deny_all',
        ]
        widgets = {
            'cust_no':      forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 00123'}),
            'full_name':    forms.TextInput(attrs={'class': 'form-control'}),
            'phone':        forms.TextInput(attrs={'class': 'form-control', 'placeholder': '2547XXXXXXXX'}),
            'national_id':  forms.TextInput(attrs={'class': 'form-control'}),
            'username':     forms.TextInput(attrs={'class': 'form-control'}),
            'device_id':    forms.TextInput(attrs={'class': 'form-control'}),
            'authorize_withdrawal': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'deny_all':     forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class MobileActivityBulkForm(forms.Form):
    ACTION_CHOICES = [
        ('deny_all', 'Deny All — Temporarily block ALL mobile withdrawals'),
        ('reset_all', 'Reset — Restore individual customer settings'),
    ]
    action = forms.ChoiceField(choices=ACTION_CHOICES, widget=forms.HiddenInput())
    confirm = forms.BooleanField(
        required=True,
        label='I understand this affects all customers',
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )


# ── Bulk Transactions ───────────────────────────────────────────────────

class BulkTransactionUploadForm(forms.Form):
    file = forms.FileField(
        label="Upload CSV file",
        help_text="Use the downloadable template. Accepted format: .csv",
        widget=forms.FileInput(attrs={
            "class": "form-control",
            "accept": ".csv",
        }),
    )
    description = forms.CharField(
        max_length=255,
        label="Batch description",
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "e.g. January 2026 salary deposits",
        }),
    )

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith(".csv"):
            raise forms.ValidationError("Only .csv files are accepted.")
        if f.size > 5 * 1024 * 1024:
            raise forms.ValidationError("File must be under 5 MB.")
        return f
