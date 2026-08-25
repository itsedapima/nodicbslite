from django import forms
from django.forms import inlineformset_factory

from .models import (
    Customer,
    NextOfKin,
    CustomerEconomicActivity,
    GroupOfficial,
    ChurchOfficial,
)
from administration.models import CompanyBranch  # adjust path if different

# ---------------------------------------------------------------------------
# Step 1: Personal Details
# ---------------------------------------------------------------------------
class PersonalDetailsForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            "customer_type",
            "first_name", "middle_name", "last_name", "full_name",
            "gender", "marital_status", "dob", "branch",
            "national_id", "kra_pin",
        ]
        widgets = {
            "full_name": forms.TextInput(attrs={
                "readonly": "readonly",
                "placeholder": "Auto-generated from names above",
            }),
            "dob": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = CompanyBranch.objects.filter(is_active=True)
        self.fields["branch"].empty_label = "-- Select branch --"
        self.fields["branch"].label = "Registration branch"
        if "middle_name" in self.fields:
            self.fields["middle_name"].required = False


# ---------------------------------------------------------------------------
# Step 2: Communication & Residence
# ---------------------------------------------------------------------------
class CommunicationResidenceForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            "phone", "reg_email",
            "postal_address", "postal_code", "town", "home_address",
            "default_notifications_setting",
        ]
        widgets = {
            "default_notifications_setting": forms.CheckboxInput(attrs={
                "class": "form-check-input",
                "role": "switch",
            }),
        }
        labels = {
            "default_notifications_setting": "Enable SMS & email notifications",
            "reg_email": "Email address",
        }

    def clean_phone(self):
        phone = self.cleaned_data.get("phone")
        if not phone:
            raise forms.ValidationError("Phone number is required.")
        phone = phone.replace(" ", "").replace("-", "")
        if phone.startswith("0"):
            phone = "254" + phone[1:]
        elif phone.startswith("+254"):
            phone = phone[1:]
        if not phone.startswith("254"):
            raise forms.ValidationError("Invalid phone format. Use 07xxxxxxxx.")
        return phone


# ---------------------------------------------------------------------------
# Step 3: Economic Activity & Flags
# ---------------------------------------------------------------------------
class EconomicActivityForm(forms.ModelForm):
    # ── Customer-level flags injected manually ──
    reg_fee_is_paid = forms.BooleanField(
        required=False,
        label="Registration fee paid",
        widget=forms.CheckboxInput(attrs={
            "class": "form-check-input", "role": "switch",
        }),
    )
    is_treasury = forms.BooleanField(
        required=False,
        label="Treasury account (company pooling account)",
        widget=forms.CheckboxInput(attrs={
            "class": "form-check-input", "role": "switch",
        }),
    )

    class Meta:
        model = CustomerEconomicActivity
        fields = [
            "employment_status", "economic_activity",
            "profession", "monthly_income",
        ]

    def __init__(self, *args, **kwargs):
        # Pop initial values for the non-model fields
        self.customer_instance = kwargs.pop("customer_instance", None)
        super().__init__(*args, **kwargs)
        self.fields["economic_activity"].required = False
        self.fields["profession"].required = False
        # Pre-populate flags if editing
        if self.customer_instance:
            self.fields["reg_fee_is_paid"].initial = self.customer_instance.reg_fee_is_paid
            self.fields["is_treasury"].initial = self.customer_instance.is_treasury
# ---------------------------------------------------------------------------
# Next of kin
# ---------------------------------------------------------------------------
class NextOfKinForm(forms.ModelForm):
    class Meta:
        model = NextOfKin
        fields = ["kin_name", "gender", "kin_relationship", "kin_dob", "kin_phone", "kin_national_id"]


# ---------------------------------------------------------------------------
# Organization (Group / Church) details
# ---------------------------------------------------------------------------
class OrganizationDetailsForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            "full_name", "national_id", "phone", "branch", "reg_email",
            "postal_address", "postal_code", "town", "home_address",
        ]

    def __init__(self, *args, **kwargs):
        org_type = kwargs.pop("org_type", "Group")
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = CompanyBranch.objects.filter(is_active=True)
        self.fields["branch"].empty_label = "-- Select branch --"
        self.fields["branch"].label = "Registration branch"
        self.fields["full_name"].label = f"{org_type} Name"
        self.fields["national_id"].label = "Certificate Number"
        self.fields["home_address"].label = "Physical Location / Plot No."


GroupOfficialFormSet = inlineformset_factory(
    Customer, GroupOfficial,
    fields=["name", "designation", "phone", "national_id"],
    extra=3, can_delete=False,
)
ChurchOfficialFormSet = inlineformset_factory(
    Customer, ChurchOfficial,
    fields=["name", "designation", "phone", "national_id"],
    extra=3, can_delete=False,
)

# Edit-mode formsets: extra=3 blank rows, can_delete existing officials
GroupOfficialEditFormSet = inlineformset_factory(
    Customer, GroupOfficial,
    fields=["name", "designation", "phone", "national_id"],
    extra=3, can_delete=True,
)
ChurchOfficialEditFormSet = inlineformset_factory(
    Customer, ChurchOfficial,
    fields=["name", "designation", "phone", "national_id"],
    extra=3, can_delete=True,
)


# ---------------------------------------------------------------------------
# Single-page edit form (used by edit_customer)
# ---------------------------------------------------------------------------
class CustomerForm(forms.ModelForm):
    """
    Full editable view of a Customer. Excludes system-managed fields
    (cust_no, reg_date, user, registered_by) that should never be hand-edited.
    """
    class Meta:
        model = Customer
        fields = [
            "customer_type", "first_name", "middle_name", "last_name", "full_name",
            "gender", "marital_status", "dob", "phone", "branch",
            "national_id", "kra_pin", "reg_email",
            "postal_address", "postal_code", "town", "home_address",
            "customer_status",
        ]
        widgets = {
            "full_name": forms.TextInput(attrs={
                "readonly": "readonly",
                "placeholder": "Auto-generated from names above",
            }),
            "dob": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = CompanyBranch.objects.filter(is_active=True)
        self.fields["branch"].empty_label = "-- Select branch --"
        self.fields["branch"].label = "Registration branch"
        if "middle_name" in self.fields:
            self.fields["middle_name"].required = False

    def clean_phone(self):
        phone = self.cleaned_data.get("phone")
        if not phone:
            raise forms.ValidationError("Phone number is required.")
        phone = phone.replace(" ", "").replace("-", "")
        if phone.startswith("0"):
            phone = "254" + phone[1:]
        elif phone.startswith("+254"):
            phone = phone[1:]
        if not phone.startswith("254"):
            raise forms.ValidationError("Invalid phone format. Use 07xxxxxxxx.")
        return phone