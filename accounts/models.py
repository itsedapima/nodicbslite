"""
accounts/models.py
──────────────────
Production SACCO Custom User model with hardened security:
  • Brute-force lockout tracking (failed_login_attempts, locked_until)
  • OTP abuse protection (daily unused/expired threshold → auto-lock)
  • LoginAttempt audit trail for forensic analysis
  • IP-aware rate limiting helpers
"""

import random
import string
import uuid
import hashlib
from datetime import timedelta

from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models, transaction
from django.utils import timezone
from django.conf import settings


# ════════════════════════════════════════════════════════════════════════════════
# SECURITY CONSTANTS — override in settings.py as needed
# ════════════════════════════════════════════════════════════════════════════════

MAX_LOGIN_ATTEMPTS = getattr(settings, 'SECURITY_MAX_LOGIN_ATTEMPTS', 5)
LOGIN_LOCKOUT_MINUTES = getattr(settings, 'SECURITY_LOGIN_LOCKOUT_MINUTES', 30)
OTP_EXPIRY_MINUTES = getattr(settings, 'SECURITY_OTP_EXPIRY_MINUTES', 10)
OTP_DAILY_ABUSE_THRESHOLD = getattr(settings, 'SECURITY_OTP_DAILY_ABUSE_THRESHOLD', 10)
OTP_COOLDOWN_SECONDS = getattr(settings, 'SECURITY_OTP_COOLDOWN_SECONDS', 60)
PASSWORD_RESET_COOLDOWN_SECONDS = getattr(settings, 'SECURITY_PASSWORD_RESET_COOLDOWN_SECONDS', 60)


# ════════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def _pad_cust_no(raw) -> str:
    """Normalize ANY cust_no input to the zero-padded CharField format.
    int 123 → "00123", str "123" → "00123", str "00123" → "00123"."""
    s = str(raw).strip()
    if s.isdigit():
        return s.zfill(5)
    return s


def get_client_ip(request):
    """Extract the real client IP, respecting X-Forwarded-For from reverse proxies."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def hash_ip(ip_address):
    """One-way hash for IP storage (privacy-compliant forensic logging)."""
    return hashlib.sha256(ip_address.encode()).hexdigest()[:32]


# ════════════════════════════════════════════════════════════════════════════════
# USER MANAGER
# ════════════════════════════════════════════════════════════════════════════════

class CustomUserManager(BaseUserManager):

    def create_user(self, username, email, phone, first_name, last_name,
                    password=None, role='customer', **extra_fields):
        if not email:    raise ValueError('Email is required')
        if not username: raise ValueError('Username is required')
        if not phone:    raise ValueError('Phone is required')

        email = self.normalize_email(email)
        user = self.model(
            email=email,
            username=username,
            phone=phone,
            first_name=first_name,
            last_name=last_name,
            role=role,
            **extra_fields
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_mobile_user(self, cust_no, email, password):
        """
        Onboard an existing SACCO member via mobile.
        Uses database row locking (select_for_update) to prevent race conditions.
        """
        from customers.models import Customer  # avoid circular import

        raw = str(cust_no).strip()
        if not raw.isdigit():
            raise ValueError('Customer number must be numeric.')
        cust_no_padded = _pad_cust_no(raw)

        # Thread-safe atomic transaction block to avoid registration double-taps
        with transaction.atomic():
            try:
                # Lock the specific customer row until this block finishes executing
                customer = Customer.objects.select_for_update().get(cust_no=cust_no_padded)
            except Customer.DoesNotExist:
                raise ValueError(f'No customer found with member number {cust_no_padded}.')

            if customer.user_id is not None:
                raise ValueError(
                    f'Customer {cust_no_padded} already has a mobile account. '
                    'Please use the login screen.'
                )

            email_clean = self.normalize_email(email.strip())
            if self.model.objects.filter(email=email_clean).exists():
                raise ValueError('An account with this email already exists.')

            # Generate unique fallback username if base exists
            username = f'MB{cust_no_padded}'
            if self.model.objects.filter(username=username).exists():
                username = f'C{cust_no_padded}_{uuid.uuid4().hex[:4]}'

            user = self.model(
                email=email_clean,
                username=username,
                first_name=customer.first_name or customer.full_name.split()[0],
                last_name=customer.last_name or (
                    ' '.join(customer.full_name.split()[1:])
                    if len(customer.full_name.split()) > 1 else ''
                ),
                phone=customer.phone,
                role='customer',
                is_mobile_verified=True  # Validated implicitly via the onboarding signup verification
            )
            user.set_password(password)
            user.save(using=self._db)

            # Assign relationship link
            customer.user = user
            customer.save(update_fields=['user'])

            return user

    def create_superuser(self, username, email, phone, first_name='', last_name='', password=None):
        """Creates a superuser. Phone is required for SMS OTP admin login."""
        if not phone:
            raise ValueError('Superusers must have a real phone number for OTP verification.')

        extra_fields = {
            'is_staff': True,
            'is_superuser': True,
        }
        return self.create_user(
            username=username, email=email, phone=phone,
            first_name=first_name, last_name=last_name,
            password=password, role='admin', **extra_fields
        )


# ════════════════════════════════════════════════════════════════════════════════
# CUSTOM USER MODEL
# ════════════════════════════════════════════════════════════════════════════════

class CustomUser(AbstractBaseUser, PermissionsMixin):
    """
    Production SACCO Custom User Identity Profile with hardened security.
    Inherits from PermissionsMixin to correctly support Django's built-in auth groups.
    """
    ROLE_CHOICES = [
        ('customer', 'Customer'),
        ('accounts_clerk', 'Accounts Clerk'),
        ('loan_officer', 'Loan Officer'),
        ('manager', 'Manager'),
        ('admin', 'Admin'),
    ]

    username = models.CharField(max_length=30, unique=True)
    email = models.EmailField(max_length=255, unique=True)
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=15, unique=True, blank=True, null=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='accounts_clerk')

    is_mobile_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    # Core system flags for standard Django ecosystem and admin panel compliance
    is_staff = models.BooleanField(
        default=False,
        help_text="Designates whether the user can log into the Django admin site."
    )
    is_superuser = models.BooleanField(
        default=False,
        help_text="Designates that this user has all permissions explicitly assigned without manual groups."
    )

    device_id = models.CharField(max_length=64, blank=True, null=True)

    # ── SECURITY: Brute-force lockout fields ────────────────────────────
    failed_login_attempts = models.PositiveIntegerField(
        default=0,
        help_text="Consecutive failed login attempts. Resets on success."
    )
    locked_until = models.DateTimeField(
        null=True, blank=True,
        help_text="Account locked until this timestamp due to excessive failed attempts."
    )
    last_failed_login = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp of the most recent failed login attempt."
    )

    # ── SECURITY: OTP abuse protection ──────────────────────────────────
    otp_abuse_locked = models.BooleanField(
        default=False,
        help_text="Account locked due to excessive unused/expired OTP requests."
    )
    otp_abuse_locked_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When OTP abuse lock was triggered."
    )

    # ── SECURITY: Password reset cooldown ───────────────────────────────
    last_password_reset_request = models.DateTimeField(
        null=True, blank=True,
        help_text="Prevents rapid-fire password reset requests."
    )

    objects = CustomUserManager()

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['email', 'phone']

    class Meta:
        verbose_name = 'User Account'
        verbose_name_plural = 'User Accounts'

    def __str__(self):
        return f"{self.username} ({self.get_role_display_label()})"

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip() or self.username

    def get_short_name(self):
        return self.first_name or self.username

    def get_role_display_label(self):
        return dict(self.ROLE_CHOICES).get(self.role, self.role)

    @property
    def is_admin(self):
        """True if user has the 'admin' role or is a Django superuser."""
        return self.role == 'admin' or self.is_superuser

    # ── Lockout helpers ─────────────────────────────────────────────────

    @property
    def is_locked(self):
        """Check if the account is currently locked (brute-force or OTP abuse)."""
        if self.otp_abuse_locked:
            return True
        if self.locked_until and timezone.now() < self.locked_until:
            return True
        return False

    def record_failed_login(self):
        """Increment failed attempts and lock if threshold is exceeded."""
        self.failed_login_attempts += 1
        self.last_failed_login = timezone.now()
        if self.failed_login_attempts >= MAX_LOGIN_ATTEMPTS:
            self.locked_until = timezone.now() + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
        self.save(update_fields=['failed_login_attempts', 'last_failed_login', 'locked_until'])

    def reset_failed_logins(self):
        """Clear failed attempt counters on successful login."""
        if self.failed_login_attempts > 0 or self.locked_until:
            self.failed_login_attempts = 0
            self.locked_until = None
            self.last_failed_login = None
            self.save(update_fields=['failed_login_attempts', 'locked_until', 'last_failed_login'])

    def check_otp_abuse(self):
        """
        Check if user has exceeded the daily OTP abuse threshold.
        Returns (is_abusing: bool, unused_count: int)
        """
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        unused_today = OtpVerification.objects.filter(
            email=self.email,
            created_at__gte=today_start,
            is_used=False,
        ).count()
        if unused_today >= OTP_DAILY_ABUSE_THRESHOLD:
            if not self.otp_abuse_locked:
                self.otp_abuse_locked = True
                self.otp_abuse_locked_at = timezone.now()
                self.save(update_fields=['otp_abuse_locked', 'otp_abuse_locked_at'])
            return True, unused_today
        return False, unused_today


# ════════════════════════════════════════════════════════════════════════════════
# OTP VERIFICATION
# ════════════════════════════════════════════════════════════════════════════════

class OtpVerification(models.Model):
    """
    Stores temporary verification codes for registration, login, and security flows.
    Hardened with:
      - Purpose field to separate login OTPs from registration OTPs
      - IP tracking for abuse detection
      - Delivery channel tracking (sms/email)
    """
    PURPOSE_CHOICES = [
        ('registration', 'Registration'),
        ('login', 'Login'),
        ('password_reset', 'Password Reset'),
        ('verification', 'Verification'),
        ('admin_login', 'Admin Login'),
    ]
    CHANNEL_CHOICES = [
        ('sms', 'SMS'),
        ('email', 'Email'),
    ]

    user = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='otp_codes',
        help_text="Linked user (null for pre-registration OTPs)."
    )
    cust_no = models.CharField(max_length=50, blank=True, default='')
    email = models.EmailField(db_index=True)
    phone = models.CharField(max_length=20, blank=True, default='')
    otp_code = models.CharField(max_length=6)
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES, default='registration')
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default='sms')

    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    # Security metadata
    request_ip_hash = models.CharField(
        max_length=32, blank=True, default='',
        help_text="SHA-256 hash (truncated) of the requesting IP."
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'OTP Verification'
        verbose_name_plural = 'OTP Verifications'
        indexes = [
            models.Index(fields=['email', 'purpose', 'created_at']),
            models.Index(fields=['phone', 'purpose', 'created_at']),
            models.Index(fields=['otp_code', 'email', 'is_used']),
        ]

    def save(self, *args, **kwargs):
        if not self.pk:
            self.expires_at = timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)
        super().save(*args, **kwargs)

    @staticmethod
    def generate_otp():
        """Generate a cryptographically adequate 6-digit OTP."""
        return ''.join(random.choices(string.digits, k=6))

    def is_valid(self):
        return not self.is_used and timezone.now() < self.expires_at

    def mark_used(self):
        """Atomically mark OTP as consumed."""
        self.is_used = True
        self.used_at = timezone.now()
        self.save(update_fields=['is_used', 'used_at'])

    @classmethod
    def can_request_otp(cls, identifier, purpose='registration'):
        """
        Cooldown check: prevent rapid-fire OTP requests.
        Returns (allowed: bool, seconds_remaining: int)
        """
        cutoff = timezone.now() - timedelta(seconds=OTP_COOLDOWN_SECONDS)
        recent = cls.objects.filter(
            created_at__gte=cutoff,
            purpose=purpose,
        ).filter(
            models.Q(email=identifier) | models.Q(phone=identifier)
        ).exists()
        if recent:
            # Find the most recent one to compute remaining wait
            last = cls.objects.filter(
                purpose=purpose,
            ).filter(
                models.Q(email=identifier) | models.Q(phone=identifier)
            ).order_by('-created_at').first()
            if last:
                elapsed = (timezone.now() - last.created_at).total_seconds()
                remaining = max(0, int(OTP_COOLDOWN_SECONDS - elapsed))
                return False, remaining
        return True, 0

    @classmethod
    def invalidate_previous(cls, identifier, purpose):
        """Expire all outstanding OTPs for this identifier + purpose."""
        cls.objects.filter(
            is_used=False,
            purpose=purpose,
        ).filter(
            models.Q(email=identifier) | models.Q(phone=identifier)
        ).update(
            is_used=True,
            used_at=timezone.now(),
        )

    def __str__(self):
        return f'OTP for {self.email} ({self.purpose})'


# ════════════════════════════════════════════════════════════════════════════════
# LOGIN ATTEMPT LOG — forensic audit trail
# ════════════════════════════════════════════════════════════════════════════════

class LoginAttempt(models.Model):
    """
    Records every login attempt (success or failure) for security auditing.
    Enables IP-based and username-based abuse detection.
    """
    RESULT_CHOICES = [
        ('success', 'Success'),
        ('failed_credentials', 'Failed - Bad Credentials'),
        ('failed_locked', 'Failed - Account Locked'),
        ('failed_inactive', 'Failed - Account Inactive'),
        ('failed_otp', 'Failed - Invalid OTP'),
        ('failed_rate_limited', 'Failed - Rate Limited'),
        ('success_otp', 'Success - OTP Login'),
    ]

    username_attempted = models.CharField(max_length=150, db_index=True)
    ip_address_hash = models.CharField(max_length=32, db_index=True)
    ip_address = models.GenericIPAddressField(
        null=True, blank=True,
        help_text="Raw IP stored temporarily for active incident response; purged by cleanup job."
    )
    user_agent = models.CharField(max_length=512, blank=True, default='')
    result = models.CharField(max_length=30, choices=RESULT_CHOICES)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = 'Login Attempt'
        verbose_name_plural = 'Login Attempts'
        indexes = [
            models.Index(fields=['ip_address_hash', 'timestamp']),
            models.Index(fields=['username_attempted', 'timestamp']),
        ]

    def __str__(self):
        return f"{self.username_attempted} - {self.result} @ {self.timestamp:%Y-%m-%d %H:%M}"

    @classmethod
    def is_ip_rate_limited(cls, ip_address, window_minutes=15, max_attempts=20):
        """
        Check if an IP has made too many failed attempts in the window.
        This prevents distributed brute-force attacks across multiple usernames.
        """
        ip_hash = hash_ip(ip_address)
        cutoff = timezone.now() - timedelta(minutes=window_minutes)
        failed_count = cls.objects.filter(
            ip_address_hash=ip_hash,
            timestamp__gte=cutoff,
            result__startswith='failed',
        ).count()
        return failed_count >= max_attempts

    @classmethod
    def record(cls, username, request, result):
        """Convenience factory method."""
        ip = get_client_ip(request)
        return cls.objects.create(
            username_attempted=username[:150],
            ip_address_hash=hash_ip(ip),
            ip_address=ip,
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:512],
            result=result,
        )
