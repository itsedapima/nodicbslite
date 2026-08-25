"""
accounts/admin.py
─────────────────
Django admin configuration for the hardened accounts app.
Includes LoginAttempt audit viewer and account lockout management.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.crypto import get_random_string
from django.contrib.auth.forms import AdminPasswordChangeForm
from django.contrib.admin.utils import unquote

from .models import CustomUser, OtpVerification, LoginAttempt
from sms.models import SMSLog, EmailLog


# ════════════════════════════════════════════════════════════════════════
# FORMS FOR SECURE PASSWORD HANDLING IN ADMIN
# ════════════════════════════════════════════════════════════════════════

class CustomUserCreationForm(UserCreationForm):
    class Meta:
        model = CustomUser
        fields = ('username', 'email', 'phone', 'role')

class CustomUserChangeForm(UserChangeForm):
    class Meta:
        model = CustomUser
        fields = '__all__'


# ════════════════════════════════════════════════════════════════════════
# CUSTOM USER ADMIN
# ════════════════════════════════════════════════════════════════════════

@admin.register(CustomUser)
class CustomUserAdmin(BaseUserAdmin):
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm

    list_display = (
        'username', 'email', 'phone', 'get_role_badge',
        'is_active', 'get_lockout_status', 'is_mobile_verified',
        'is_staff', 'is_superuser',
    )
    list_filter = (
        'role', 'is_active', 'is_mobile_verified', 'is_staff',
        'is_superuser', 'otp_abuse_locked',
    )
    search_fields = ('username', 'email', 'phone', 'first_name', 'last_name')
    ordering = ('username',)
    filter_horizontal = ('groups', 'user_permissions')

    actions = ['reset_password_and_log_communications', 'unlock_selected_accounts']

    fieldsets = (
        ('Authentication Credentials', {'fields': ('username', 'password')}),
        ('Personal Profile Information', {'fields': ('first_name', 'last_name', 'email', 'phone')}),
        ('SACCO Role & System Governance', {'fields': ('role', 'is_active', 'is_staff', 'is_superuser')}),
        ('Mobile Banking Security Parameters', {'fields': ('is_mobile_verified', 'device_id')}),
        ('Security & Lockout Status', {
            'classes': ('collapse',),
            'fields': (
                'failed_login_attempts', 'locked_until', 'last_failed_login',
                'otp_abuse_locked', 'otp_abuse_locked_at',
                'last_password_reset_request',
            ),
            'description': (
                'These fields track brute-force lockouts and OTP abuse. '
                'Use the "Unlock Selected Accounts" action to clear lockouts.'
            ),
        }),
        ('Important Log Dates', {'fields': ('last_login',)}),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'phone', 'role', 'password1', 'password2'),
        }),
    )

    readonly_fields = (
        'failed_login_attempts', 'locked_until', 'last_failed_login',
        'otp_abuse_locked_at', 'last_password_reset_request',
    )

    # ── CENTRALIZED LOGGING HELPER ──
    def _execute_notification_logging(self, admin_username, user, plain_password):
        """Helper to ensure fields exist and strings are clean before logging."""
        phone_target = getattr(user, 'phone', '')
        email_target = getattr(user, 'email', '')

        if phone_target and str(phone_target).strip():
            SMSLog.objects.create(
                phone=str(phone_target).strip(),
                message=(
                    f"Your SACCO password has been updated. "
                    f"Credentials: Password: {plain_password}. "
                    f"Please change it immediately."
                ),
                status="pending",
                created_by=admin_username[:20]
            )

        if email_target and str(email_target).strip():
            EmailLog.objects.create(
                recipient_to=str(email_target).strip(),
                subject="SACCO Account - Administrative Password Update",
                message_body=(
                    f"Hello {user.get_short_name()},\n\n"
                    f"An administrator has updated your account credentials.\n\n"
                    f"Temporary Password: {plain_password}\n\n"
                    f"Please log in and update your password immediately to secure your account."
                ),
                is_html=False,
                status="pending",
                created_by=admin_username[:50]
            )

    # ── ENTRY POINT 1: DEFAULT FLOW RIDER (INDIVIDUAL PROFILE RESET) ──
    def user_change_password(self, request, id, form_url=""):
        """Rider that captures the clean password string before passing execution to Django."""
        if request.method == "POST":
            user = self.get_object(request, unquote(id))
            if user and self.has_change_permission(request, user):
                form = AdminPasswordChangeForm(user, request.POST)
                if form.is_valid():
                    new_password = (
                        form.cleaned_data.get('new_password1')
                        or request.POST.get('new_password1')
                    )
                    if new_password:
                        self._execute_notification_logging(
                            request.user.username, user, new_password
                        )
        return super().user_change_password(request, id, form_url)

    # ── ENTRY POINT 2: BULK ACTION FROM LIST VIEW ──
    def reset_password_and_log_communications(self, request, queryset):
        processed_count = 0
        admin_username = request.user.username

        for user in queryset:
            temp_password = get_random_string(
                length=12,
                allowed_chars='abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$'
            )
            user.set_password(temp_password)
            user.save()
            self._execute_notification_logging(admin_username, user, temp_password)
            processed_count += 1

        self.message_user(
            request,
            f"Successfully reset passwords for {processed_count} user(s). Notifications queued."
        )
    reset_password_and_log_communications.short_description = (
        "⚡ Reset Passwords & Log Communications"
    )

    # ── BULK ACTION: UNLOCK ACCOUNTS ──
    def unlock_selected_accounts(self, request, queryset):
        count = queryset.update(
            failed_login_attempts=0,
            locked_until=None,
            otp_abuse_locked=False,
            otp_abuse_locked_at=None,
        )
        self.message_user(request, f"Unlocked {count} account(s).")
    unlock_selected_accounts.short_description = "🔓 Unlock Selected Accounts"

    # ── DISPLAY HELPERS ──
    def get_role_badge(self, obj):
        """Display user role as a colored badge."""
        color_map = {
            'admin': '#dc3545', 'manager': '#fd7e14', 'loan_officer': '#0d6efd',
            'accounts_clerk': '#6f42c1', 'customer': '#198754'
        }
        bg_color = color_map.get(obj.role, '#6c757d')
        # Use get_role_display() which is the Django convention for choice fields
        role_label = obj.get_role_display() if hasattr(obj, 'get_role_display') else str(obj.role)
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; '
            'border-radius: 4px; font-weight: bold; font-size: 11px;">{}</span>',
            bg_color, role_label
        )
    get_role_badge.short_description = 'Access Role'

    def get_lockout_status(self, obj):
        """Display account lockout status with visual indicator."""
        if obj.otp_abuse_locked:
            return mark_safe(
                '<span style="color: #dc3545; font-weight: bold;" '
                'title="Locked due to OTP abuse">🔒 OTP Abuse</span>'
            )
        if obj.locked_until and timezone.now() < obj.locked_until:
            remaining = obj.locked_until - timezone.now()
            mins = max(1, int(remaining.total_seconds() / 60))
            return format_html(
                '<span style="color: #fd7e14; font-weight: bold;" '
                'title="Locked for {} min(s)">🔒 {}m</span>',
                mins, mins
            )
        if obj.failed_login_attempts > 0:
            return format_html(
                '<span style="color: #ffc107;" title="{} failed attempt(s)">⚠️ {}</span>',
                obj.failed_login_attempts, obj.failed_login_attempts
            )
        return mark_safe('<span style="color: #28a745;">✅</span>')
    get_lockout_status.short_description = 'Security'


# ════════════════════════════════════════════════════════════════════════
# OTP VERIFICATION ADMIN
# ════════════════════════════════════════════════════════════════════════

@admin.register(OtpVerification)
class OtpVerificationAdmin(admin.ModelAdmin):
    list_display = (
        'email', 'phone', 'purpose', 'channel',
        'get_status_badge', 'created_at', 'expires_at',
    )
    list_filter = ('is_used', 'purpose', 'channel', 'created_at')
    search_fields = ('cust_no', 'email', 'phone')
    ordering = ['-created_at']
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Allow delete for cleanup of old records
        return request.user.is_superuser

    def get_status_badge(self, obj):
        """Display OTP verification status."""
        if obj.is_used:
            return mark_safe(
                '<span style="color: #6c757d; font-weight: bold;">Used</span>'
            )
        elif timezone.now() > obj.expires_at:
            return mark_safe(
                '<span style="color: #dc3545; font-weight: bold;">Expired</span>'
            )
        return mark_safe(
            '<span style="color: #28a745; font-weight: bold;">Active / Valid</span>'
        )
    get_status_badge.short_description = 'Status'


# ════════════════════════════════════════════════════════════════════════
# LOGIN ATTEMPT AUDIT LOG — read-only forensic viewer
# ════════════════════════════════════════════════════════════════════════

@admin.register(LoginAttempt)
class LoginAttemptAdmin(admin.ModelAdmin):
    list_display = (
        'timestamp', 'username_attempted', 'get_result_badge',
        'ip_address_hash_short', 'user_agent_short',
    )
    list_filter = ('result', 'timestamp')
    search_fields = ('username_attempted', 'ip_address_hash', 'ip_address')
    ordering = ['-timestamp']
    date_hierarchy = 'timestamp'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_result_badge(self, obj):
        """Display login attempt result with color coding."""
        color_map = {
            'success': '#28a745',
            'success_otp': '#28a745',
            'failed_credentials': '#ffc107',
            'failed_locked': '#dc3545',
            'failed_inactive': '#6c757d',
            'failed_otp': '#fd7e14',
            'failed_rate_limited': '#dc3545',
        }
        color = color_map.get(obj.result, '#6c757d')
        label = obj.get_result_display()
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color, label
        )
    get_result_badge.short_description = 'Result'

    def ip_address_hash_short(self, obj):
        """Display truncated IP address hash."""
        return obj.ip_address_hash[:12] + '…' if obj.ip_address_hash else '–'
    ip_address_hash_short.short_description = 'IP Hash'

    def user_agent_short(self, obj):
        """Display truncated user agent string."""
        ua = obj.user_agent or ''
        return ua[:60] + ('…' if len(ua) > 60 else '')
    user_agent_short.short_description = 'User Agent'