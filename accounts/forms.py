"""
accounts/forms.py
─────────────────
All authentication, user management, and OTP login forms.
"""

from django import forms
from django.contrib.auth.forms import (
    UserCreationForm, AuthenticationForm,
    PasswordResetForm, SetPasswordForm,
)
from .models import CustomUser


# ════════════════════════════════════════════════════════════════════════════════
# USER REGISTRATION FORM
# ════════════════════════════════════════════════════════════════════════════════

class CustomUserCreationForm(UserCreationForm):
    email = forms.EmailField(required=True)
    phone = forms.CharField(required=True)
    first_name = forms.CharField(required=True)
    last_name = forms.CharField(required=True)

    class Meta:
        model = CustomUser
        fields = ('username', 'first_name', 'last_name', 'email', 'phone')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            field.widget.attrs.update({'class': 'form-control'})


class AdminCustomUserCreationForm(forms.ModelForm):
    email = forms.EmailField(required=True)
    phone = forms.CharField(required=True)
    first_name = forms.CharField(required=True)
    last_name = forms.CharField(required=True)
    role = forms.ChoiceField(choices=CustomUser.ROLE_CHOICES, required=True)

    class Meta:
        model = CustomUser
        fields = ('username', 'first_name', 'last_name', 'email', 'phone', 'role')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.update({'class': 'form-select rounded-0'})
            else:
                display_label = field.label or field_name.replace('_', ' ')
                field.widget.attrs.update({
                    'class': 'form-control rounded-0',
                    'placeholder': f"Enter {display_label.lower()}"
                })


class CustomUserEditForm(forms.ModelForm):
    class Meta:
        model = CustomUser
        fields = ['email', 'first_name', 'last_name', 'phone', 'role', 'is_active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.update({'class': 'form-check-input', 'role': 'switch'})
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.update({'class': 'form-select'})
            else:
                field.widget.attrs.update({
                    'class': 'form-control',
                    'placeholder': f"Enter {field.label.lower()}"
                })


# ════════════════════════════════════════════════════════════════════════════════
# CORE AUTHENTICATION & SECURITY PASS RECOVERY FORMS
# ════════════════════════════════════════════════════════════════════════════════

class CustomAuthenticationForm(AuthenticationForm):
    username = forms.CharField(
        label='Username',
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'autocomplete': 'username',
            'autofocus': True,
        })
    )
    password = forms.CharField(
        label='Password',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'autocomplete': 'current-password',
        })
    )


class CustomPasswordResetForm(PasswordResetForm):
    email = forms.EmailField(
        label='Email',
        max_length=254,
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'autocomplete': 'email',
        })
    )


class CustomSetPasswordForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            field.widget.attrs.update({'class': 'form-control'})


# ════════════════════════════════════════════════════════════════════════════════
# OTP LOGIN FORMS
# ════════════════════════════════════════════════════════════════════════════════

class OtpLoginRequestForm(forms.Form):
    """Step 1: User enters their username to receive an OTP."""
    username = forms.CharField(
        label='Username',
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter your username',
            'autocomplete': 'username',
            'autofocus': True,
        })
    )


class OtpLoginVerifyForm(forms.Form):
    """Step 2: User enters the OTP code received via SMS/email."""
    otp_code = forms.CharField(
        label='Verification Code',
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={
            'class': 'form-control text-center',
            'placeholder': '000000',
            'autocomplete': 'one-time-code',
            'inputmode': 'numeric',
            'pattern': '[0-9]{6}',
            'maxlength': '6',
            'autofocus': True,
            'style': 'font-size: 1.5rem; letter-spacing: 0.5rem;',
        })
    )

    def clean_otp_code(self):
        code = self.cleaned_data.get('otp_code', '').strip()
        if not code.isdigit() or len(code) != 6:
            raise forms.ValidationError('Please enter a valid 6-digit code.')
        return code
