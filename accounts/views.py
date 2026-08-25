"""
accounts/views.py
------------------
Authentication & user-administration views — HARDENED.

Security measures implemented:
  1. Brute-force lockout (per-account + per-IP)
  2. Enumeration-safe password reset (same response regardless of email existence)
  3. Rate limiting on login, password reset, OTP requests
  4. OTP-only login flow with SMS/Email delivery logging
  5. OTP abuse auto-blocking (10 unused/expired per day → account lock)
  6. Constant-time responses to prevent timing attacks on user existence
  7. Login attempt audit trail (LoginAttempt model)

Every security-sensitive flow is funnelled through audit.services helpers.
"""

import time

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.sites.shortcuts import get_current_site
from django.template.loader import render_to_string
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode, url_has_allowed_host_and_scheme
from django.utils.encoding import force_bytes, force_str
from django.contrib.auth.tokens import default_token_generator
from django.utils.html import strip_tags
from django.contrib import messages
from django.utils.crypto import get_random_string
from django.urls import reverse
from django.db.models import Q
from django.conf import settings
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import (
    CustomUser, OtpVerification, LoginAttempt,
    get_client_ip, hash_ip,
    MAX_LOGIN_ATTEMPTS, LOGIN_LOCKOUT_MINUTES,
    OTP_DAILY_ABUSE_THRESHOLD, PASSWORD_RESET_COOLDOWN_SECONDS,
)
from .forms import (
    CustomUserCreationForm, CustomAuthenticationForm,
    CustomPasswordResetForm, CustomSetPasswordForm,
    CustomUserEditForm, AdminCustomUserCreationForm,
    OtpLoginRequestForm, OtpLoginVerifyForm,
)
from accounts.decorators import role_required

# Notification / audit layer
from sms.services import email_notify
from audit.services import log_security_event


# ════════════════════════════════════════════════════════════════════════════════
# INTERNAL SECURITY HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def _check_ip_rate_limit(request, action='login'):
    """Returns True if the IP is currently rate-limited."""
    ip = get_client_ip(request)
    # Different thresholds for different actions
    limits = {
        'login': (15, 20),           # 20 failures in 15 min
        'password_reset': (15, 5),   # 5 requests in 15 min
        'otp_request': (15, 10),     # 10 requests in 15 min
    }
    window, max_attempts = limits.get(action, (15, 20))
    return LoginAttempt.is_ip_rate_limited(ip, window_minutes=window, max_attempts=max_attempts)


def _log_sms_for_otp(phone, otp_code, created_by='system'):
    """
    Create an SMSLog entry for the OTP so the background SMS sender picks it up.
    Uses the sms.models.SMSLog model directly.
    """
    try:
        from sms.models import SMSLog
        SMSLog.objects.create(
            phone=phone,
            message=f"Your EAST AKIBA SACCO  login code is: {otp_code}. Valid for 10 minutes. Do not share this code.",
            status="pending",
            created_by=created_by[:20],
        )
    except Exception:
        pass  # Fail silently — the OTP is also stored in OtpVerification


def _log_email_for_otp(email, otp_code, created_by='system'):
    """
    Create an EmailLog entry for the OTP so the background email sender picks it up.
    Uses the sms.models.EmailLog model directly.
    """
    try:
        from sms.models import EmailLog
        EmailLog.objects.create(
            recipient_to=email,
            subject="EAST AKIBA SACCO  Login Verification Code",
            message_body=(
                f"Your EAST AKIBA SACCO  login verification code is: {otp_code}\n\n"
                f"This code is valid for 10 minutes. Do not share it with anyone.\n\n"
                f"If you did not request this, please ignore this message and "
                f"contact your administrator."
            ),
            is_html=False,
            status="pending",
            created_by=created_by[:50],
        )
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════════
# SIGNUP
# ════════════════════════════════════════════════════════════════════════════════

def signup_view_contact_admin(request):
    messages.warning(request, 'Please contact Administrator for registration.')
    return redirect('accounts:login')


def signup_view(request):
    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = False  # Deactivate account until email confirmation
            user.save()

            current_site = get_current_site(request)
            mail_subject = 'Activate your account.'
            email = form.cleaned_data['email']
            html_message = render_to_string('accounts/acc_active_email.html', {
                'user': user,
                'domain': current_site.domain,
                'uid': urlsafe_base64_encode(force_bytes(user.pk)),
                'token': default_token_generator.make_token(user),
            })
            plain_message = strip_tags(html_message)

            email_notify(
                recipient_to=email,
                subject=mail_subject,
                body=plain_message,
                html_body=html_message,
                created_by=user.username,
            )

            log_security_event(
                'USER_SIGNUP_PENDING_ACTIVATION',
                request=request,
                actor=user.username,
                severity='info',
                object_ref=f'User {user.username}',
                details=f'New self-signup awaiting activation. Email: {email}',
            )

            messages.success(request, 'Please confirm your email address to complete the registration.')
            return redirect('accounts:login')
        else:
            messages.error(request, 'There was an error with your submission. Please check the form for errors.')
    else:
        form = CustomUserCreationForm()
    return render(request, 'accounts/signup.html', {'form': form})


# ════════════════════════════════════════════════════════════════════════════════
# LOGIN — with brute-force protection
# ════════════════════════════════════════════════════════════════════════════════

@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.method == 'POST':
        attempted_username = request.POST.get('username', '').strip()[:150]

        # ── IP-level rate limiting ──────────────────────────────────────
        if _check_ip_rate_limit(request, 'login'):
            LoginAttempt.record(attempted_username, request, 'failed_rate_limited')
            log_security_event(
                'LOGIN_IP_RATE_LIMITED',
                request=request,
                actor=attempted_username or 'anonymous',
                severity='critical',
                details=f'IP rate-limited. Too many failed login attempts from {get_client_ip(request)}.',
            )
            messages.error(request, "Too many login attempts. Please try again later.")
            return render(request, 'accounts/login.html', {'form': CustomAuthenticationForm()})

        # ── Account-level lockout check (before wasting DB auth queries) ─
        try:
            target_user = CustomUser.objects.get(username=attempted_username)

            if target_user.is_locked:
                LoginAttempt.record(attempted_username, request, 'failed_locked')
                log_security_event(
                    'LOGIN_BLOCKED_LOCKED',
                    request=request,
                    actor=attempted_username,
                    severity='warning',
                    object_ref=f'User {attempted_username}',
                    details='Login attempt on a locked account.',
                )
                if target_user.otp_abuse_locked:
                    messages.error(
                        request,
                        "Your account has been locked due to suspicious activity. "
                        "Contact your administrator."
                    )
                else:
                    remaining = target_user.locked_until - timezone.now()
                    mins_left = max(1, int(remaining.total_seconds() / 60))
                    messages.error(
                        request,
                        f"Account temporarily locked due to too many failed attempts. "
                        f"Try again in {mins_left} minute(s)."
                    )
                return render(request, 'accounts/login.html', {'form': CustomAuthenticationForm()})

        except CustomUser.DoesNotExist:
            # Don't reveal that the username doesn't exist — continue to
            # authenticate() which will fail naturally, then show generic error.
            target_user = None

        # ── Standard credential check ──────────────────────────────────
        form = CustomAuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()

            if not user.is_active:
                LoginAttempt.record(user.username, request, 'failed_inactive')
                log_security_event(
                    'LOGIN_BLOCKED_INACTIVE',
                    request=request, actor=user.username,
                    severity='warning',
                    object_ref=f'User {user.username}',
                    details='Login attempt on a deactivated account.',
                )
                messages.error(request, "Your account is blocked. Contact the admin.")
                return redirect('accounts:login')

            if user.role == 'customer':
                LoginAttempt.record(user.username, request, 'failed_locked')
                log_security_event(
                    'LOGIN_BLOCKED_CUSTOMER',
                    request=request, actor=user.username,
                    severity='warning',
                    object_ref=f'User {user.username}',
                    details='Customer attempted to log into back-office.',
                )
                messages.error(
                    request,
                    "Members must use the EAST AKIBA SACCO  mobile app. This system is for staff only."
                )
                return redirect('accounts:login')

            # ── SUCCESS ─────────────────────────────────────────────────
            user.reset_failed_logins()
            login(request, user)
            LoginAttempt.record(user.username, request, 'success')
            log_security_event(
                'LOGIN_SUCCESS',
                request=request, actor=user,
                severity='info',
                object_ref=f'User {user.username}',
                email_admin=False,
            )

            # Resolve next destination
            next_url = request.POST.get('next') or request.GET.get('next')
            is_safe = url_has_allowed_host_and_scheme(
                url=next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure()
            )
            if next_url and is_safe:
                return redirect(next_url)
            return redirect('dashboard:dashboard')
        else:
            # ── FAILURE: record on the target user ─────────────────────
            if target_user:
                target_user.record_failed_login()
                remaining_attempts = max(0, MAX_LOGIN_ATTEMPTS - target_user.failed_login_attempts)
                if remaining_attempts > 0 and remaining_attempts <= 2:
                    messages.warning(
                        request,
                        f"Invalid credentials. {remaining_attempts} attempt(s) remaining before lockout."
                    )
                elif remaining_attempts == 0:
                    messages.error(
                        request,
                        f"Account locked for {LOGIN_LOCKOUT_MINUTES} minutes due to too many failed attempts."
                    )
                else:
                    messages.error(request, 'Invalid username or password.')
            else:
                messages.error(request, 'Invalid username or password.')

            LoginAttempt.record(attempted_username, request, 'failed_credentials')
            log_security_event(
                'LOGIN_FAILED',
                request=request,
                actor=attempted_username or 'anonymous',
                severity='warning',
                details=f'Failed login attempt for username "{attempted_username}".',
            )
    else:
        form = CustomAuthenticationForm()

    return render(request, 'accounts/login.html', {'form': form})


# ════════════════════════════════════════════════════════════════════════════════
# OTP-ONLY LOGIN FLOW
# ════════════════════════════════════════════════════════════════════════════════

@require_http_methods(["GET", "POST"])
def otp_login_request_view(request):
    """
    Step 1: User provides username → system sends OTP to their registered
    phone (via SMSLog) and email (via EmailLog) for background delivery.
    """
    if request.method == 'POST':
        form = OtpLoginRequestForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']

            # ── IP rate limit ───────────────────────────────────────────
            if _check_ip_rate_limit(request, 'otp_request'):
                LoginAttempt.record(username, request, 'failed_rate_limited')
                messages.error(request, "Too many requests. Please try again later.")
                return render(request, 'accounts/otp_login_request.html', {'form': form})

            # Generic success message regardless of whether user exists
            # to prevent username enumeration.
            generic_msg = (
                "If a matching account exists, a verification code has been sent "
                "to the registered phone number and email."
            )

            try:
                user = CustomUser.objects.get(username=username, is_active=True)

                # Check OTP abuse threshold
                is_abusing, count = user.check_otp_abuse()
                if is_abusing:
                    LoginAttempt.record(username, request, 'failed_rate_limited')
                    log_security_event(
                        'OTP_ABUSE_LOCKOUT',
                        request=request,
                        actor=username,
                        severity='critical',
                        object_ref=f'User {username}',
                        details=f'{count} unused OTPs today. Account locked for OTP abuse.',
                    )
                    # Still show generic message to not reveal account state
                    messages.info(request, generic_msg)
                    return redirect('accounts:otp_login_verify')

                # Check cooldown
                can_request, wait_seconds = OtpVerification.can_request_otp(
                    user.email, purpose='login'
                )
                if not can_request:
                    messages.warning(
                        request,
                        f"Please wait {wait_seconds} seconds before requesting another code."
                    )
                    return render(request, 'accounts/otp_login_request.html', {'form': form})

                # Invalidate previous login OTPs for this user
                OtpVerification.invalidate_previous(user.email, purpose='login')

                # Generate and store new OTP
                otp_code = OtpVerification.generate_otp()
                OtpVerification.objects.create(
                    user=user,
                    email=user.email,
                    phone=user.phone or '',
                    otp_code=otp_code,
                    purpose='login',
                    channel='sms',
                    request_ip_hash=hash_ip(get_client_ip(request)),
                )

                # ── Log OTP to SMSLog for background SMS delivery ──────
                if user.phone:
                    _log_sms_for_otp(user.phone, otp_code, created_by=username)

                # ── Log OTP to EmailLog for background email delivery ──
                if user.email:
                    _log_email_for_otp(user.email, otp_code, created_by=username)

                log_security_event(
                    'OTP_LOGIN_REQUESTED',
                    request=request,
                    actor=username,
                    severity='info',
                    object_ref=f'User {username}',
                    details='Login OTP generated and queued for delivery.',
                    email_admin=False,
                )

            except CustomUser.DoesNotExist:
                # Deliberate: do NOT reveal that the username doesn't exist.
                # Add a small constant-ish delay to prevent timing attacks.
                time.sleep(0.1)

            messages.info(request, generic_msg)
            # Store username in session for the verify step
            request.session['otp_login_username'] = username
            return redirect('accounts:otp_login_verify')
    else:
        form = OtpLoginRequestForm()

    return render(request, 'accounts/otp_login_request.html', {'form': form})


@require_http_methods(["GET", "POST"])
def otp_login_verify_view(request):
    """
    Step 2: User enters the OTP code → system verifies and logs them in.
    """
    username = request.session.get('otp_login_username')
    if not username:
        messages.warning(request, "Please start the login process by entering your username.")
        return redirect('accounts:otp_login_request')

    if request.method == 'POST':
        form = OtpLoginVerifyForm(request.POST)
        if form.is_valid():
            otp_code = form.cleaned_data['otp_code']

            try:
                user = CustomUser.objects.get(username=username, is_active=True)

                if user.is_locked:
                    LoginAttempt.record(username, request, 'failed_locked')
                    messages.error(request, "Your account is currently locked. Contact admin.")
                    return redirect('accounts:login')

                # Find valid OTP
                otp = OtpVerification.objects.filter(
                    user=user,
                    otp_code=otp_code,
                    purpose='login',
                    is_used=False,
                    expires_at__gt=timezone.now(),
                ).order_by('-created_at').first()

                if otp:
                    otp.mark_used()
                    user.reset_failed_logins()
                    login(request, user)

                    # Clean up session
                    if 'otp_login_username' in request.session:
                        del request.session['otp_login_username']

                    LoginAttempt.record(username, request, 'success_otp')
                    log_security_event(
                        'LOGIN_SUCCESS_OTP',
                        request=request,
                        actor=user,
                        severity='info',
                        object_ref=f'User {user.username}',
                        details='User logged in via OTP verification.',
                        email_admin=False,
                    )
                    messages.success(request, 'Login successful.')

                    if user.role == 'customer':
                        messages.error(request, "Members must use the EAST AKIBA SACCO  mobile app.")
                        logout(request)
                        return redirect('accounts:login')

                    return redirect('dashboard:dashboard')
                else:
                    # Invalid OTP
                    user.record_failed_login()
                    LoginAttempt.record(username, request, 'failed_otp')
                    log_security_event(
                        'OTP_LOGIN_FAILED',
                        request=request,
                        actor=username,
                        severity='warning',
                        object_ref=f'User {username}',
                        details='Invalid or expired OTP code entered.',
                    )
                    messages.error(request, "Invalid or expired verification code.")

            except CustomUser.DoesNotExist:
                time.sleep(0.1)
                messages.error(request, "Invalid or expired verification code.")
    else:
        form = OtpLoginVerifyForm()

    return render(request, 'accounts/otp_login_verify.html', {
        'form': form,
        'username': username,
    })


# ════════════════════════════════════════════════════════════════════════════════
# LOGOUT
# ════════════════════════════════════════════════════════════════════════════════

def logout_view(request):
    reason = request.GET.get('reason')
    actor = request.user if request.user.is_authenticated else None

    logout(request)

    log_security_event(
        'LOGOUT_TIMEOUT' if reason == 'expired' else 'LOGOUT',
        request=request, actor=actor,
        severity='info',
        details=f'Reason: {reason or "user-initiated"}',
        email_admin=False,
    )

    if reason == 'expired':
        messages.warning(request, 'Your session expired due to inactivity.')
    else:
        messages.success(request, 'You have been logged out.')

    return redirect('accounts:login')


# ════════════════════════════════════════════════════════════════════════════════
# ACCOUNT ACTIVATION
# ════════════════════════════════════════════════════════════════════════════════

def activate(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = CustomUser.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, CustomUser.DoesNotExist):
        user = None

    if user is not None and default_token_generator.check_token(user, token):
        user.is_active = True
        user.save()
        log_security_event(
            'ACCOUNT_ACTIVATED',
            request=request, actor=user.username,
            severity='info',
            object_ref=f'User {user.username}',
            details='Email-verified self-signup completed activation.',
        )
        messages.success(request, 'Thank you for your email confirmation. You can now log in.')
        return redirect('accounts:login')
    else:
        log_security_event(
            'ACCOUNT_ACTIVATION_FAILED',
            request=request,
            severity='warning',
            details=f'Invalid/expired activation link: uid={uidb64[:32]}',
        )
        messages.error(request, 'Activation link is invalid or has expired. Please contact the administrator.')
        return redirect('accounts:signup')


# ════════════════════════════════════════════════════════════════════════════════
# PASSWORD RESET — enumeration-safe
# ════════════════════════════════════════════════════════════════════════════════

@require_http_methods(["GET", "POST"])
def password_reset_request_view(request):
    """
    HARDENED: Always shows the same success message regardless of whether
    the email exists. Prevents user enumeration via password reset.
    Also rate-limited per IP and per-user cooldown.
    """
    if request.method == 'POST':
        form = CustomPasswordResetForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']

            # ── IP rate limiting ────────────────────────────────────────
            if _check_ip_rate_limit(request, 'password_reset'):
                log_security_event(
                    'PASSWORD_RESET_IP_RATE_LIMITED',
                    request=request,
                    severity='critical',
                    details=f'IP rate-limited on password reset from {get_client_ip(request)}.',
                )
                # Still show generic success to prevent information leakage
                messages.success(
                    request,
                    'If an account with that email exists, a password reset link has been sent.'
                )
                return redirect('accounts:login')

            # Always show the same response — process silently in background
            generic_msg = 'If an account with that email exists, a password reset link has been sent.'

            try:
                user = CustomUser.objects.get(email=email)

                # ── Per-user cooldown ───────────────────────────────────
                if user.last_password_reset_request:
                    elapsed = (timezone.now() - user.last_password_reset_request).total_seconds()
                    if elapsed < PASSWORD_RESET_COOLDOWN_SECONDS:
                        # Silently skip — don't reveal cooldown to requester
                        messages.success(request, generic_msg)
                        return redirect('accounts:login')

                # Update cooldown timestamp
                user.last_password_reset_request = timezone.now()
                user.save(update_fields=['last_password_reset_request'])

                token = default_token_generator.make_token(user)
                uid = urlsafe_base64_encode(force_bytes(user.pk))
                domain = get_current_site(request).domain
                mail_subject = 'Password reset'
                html_message = render_to_string('accounts/password_reset_email.html', {
                    'user': user,
                    'domain': domain,
                    'uid': uid,
                    'token': token,
                })
                plain_message = strip_tags(html_message)

                email_notify(
                    recipient_to=email,
                    subject=mail_subject,
                    body=plain_message,
                    html_body=html_message,
                    created_by=user.username,
                )

                log_security_event(
                    'PASSWORD_RESET_REQUESTED',
                    request=request, actor=user.username,
                    severity='warning',
                    object_ref=f'User {user.username}',
                    details=f'Reset link sent to {email}.',
                )

            except CustomUser.DoesNotExist:
                # Deliberate: do NOT reveal that the email doesn't exist.
                # Add a small delay to prevent timing-based enumeration.
                time.sleep(0.1)
                log_security_event(
                    'PASSWORD_RESET_UNKNOWN_EMAIL',
                    request=request,
                    severity='info',
                    details=f'Password reset attempted for unknown email (hash: {hash_ip(email)}).',
                )

            messages.success(request, generic_msg)
            return redirect('accounts:login')
        else:
            messages.warning(request, 'Please enter a valid email address.')
    else:
        form = CustomPasswordResetForm()
    return render(request, 'accounts/password_reset_request.html', {'form': form})


def password_reset_confirm_view(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = CustomUser.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, CustomUser.DoesNotExist):
        user = None

    if user is not None and default_token_generator.check_token(user, token):
        if request.method == 'POST':
            form = CustomSetPasswordForm(user, request.POST)
            if form.is_valid():
                form.save()
                # Clear any lockouts since the user proved identity via email
                user.reset_failed_logins()
                log_security_event(
                    'PASSWORD_RESET_COMPLETED',
                    request=request, actor=user.username,
                    severity='warning',
                    object_ref=f'User {user.username}',
                    details=f'Password was reset for {user.email}.',
                )
                messages.success(request, 'Your password has been reset. You can now log in with the new password.')
                return redirect('accounts:login')
            else:
                messages.warning(request, 'Please correct the errors below.')
        else:
            form = CustomSetPasswordForm(user)
        return render(request, 'accounts/password_reset_confirm.html', {'form': form})
    else:
        log_security_event(
            'PASSWORD_RESET_LINK_INVALID',
            request=request,
            severity='warning',
            details=f'Invalid/expired reset link: uid={uidb64[:32]}',
        )
        messages.error(request, 'Password reset link is invalid or has expired.')
        return redirect('accounts:password_reset_request')


# ════════════════════════════════════════════════════════════════════════════════
# ADMIN: USER MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════════

@login_required
@role_required('admin')
def add_user(request):
    if request.method == 'POST':
        form = AdminCustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)

            # Generate a secure random password
            password = get_random_string(
                length=12,
                allowed_chars='abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$'
            )
            user.set_password(password)
            user.save()

            subject = "Your Account Details"
            message = (
                f"Dear {user.first_name},\n\n"
                f"Your account has been created successfully.\n\n"
                f"Login Details:\n"
                f"Username: {user.username}\n"
                f"Password: {password}\n\n"
                f"Login here: {request.build_absolute_uri(reverse('accounts:login'))}\n\n"
                f"Please change your password after logging in.\n\n"
                f"Regards,\nAdmin Team"
            )

            email_notify(
                recipient_to=user.email,
                subject=subject,
                body=message,
                created_by=request.user.username,
            )

            log_security_event(
                'USER_CREATED_BY_ADMIN',
                request=request, actor=request.user,
                severity='critical',
                object_ref=f'User {user.username}',
                details=(
                    f'New user "{user.username}" (email {user.email}) '
                    f'created by admin "{request.user.username}". '
                    f'Initial password emailed to user.'
                ),
            )

            messages.success(request, f"User added successfully. Login details sent to {user.email}.")
            return redirect('accounts:view_users')
        else:
            messages.error(request, 'There was an error adding the user.')
    else:
        form = AdminCustomUserCreationForm()

    return render(request, 'accounts/add_user.html', {'form': form})


@login_required
@role_required('admin')
def view_users(request):
    query = request.GET.get('q', '').strip()

    if query:
        users = CustomUser.objects.filter(
            Q(username__icontains=query) |
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(email__icontains=query) |
            Q(phone__icontains=query)
        )[:100]
    else:
        users = CustomUser.objects.order_by('-id')[:100]

    return render(request, 'accounts/view_users.html', {'users': users, 'query': query})


@login_required
@role_required('admin')
def edit_user(request, user_id):
    user = get_object_or_404(CustomUser, pk=user_id)
    if request.method == 'POST':
        form = CustomUserEditForm(request.POST, instance=user)
        if form.is_valid():
            # Capture pre-change snapshot
            old_role = getattr(user, 'role', None)
            old_active = user.is_active
            old_email = user.email

            form.save()

            user.refresh_from_db()
            changes = []
            if old_role != getattr(user, 'role', None):
                changes.append(f"role: {old_role} -> {getattr(user, 'role', None)}")
            if old_active != user.is_active:
                changes.append(f"is_active: {old_active} -> {user.is_active}")
            if old_email != user.email:
                changes.append(f"email: {old_email} -> {user.email}")

            log_security_event(
                'USER_UPDATED_BY_ADMIN',
                request=request, actor=request.user,
                severity='critical' if changes else 'info',
                object_ref=f'User {user.username}',
                details='Changes: ' + (', '.join(changes) if changes else 'no field-level diff captured'),
            )

            messages.success(request, 'User updated successfully.')
            return redirect('accounts:view_users')
        else:
            messages.error(request, 'There was an error updating the user.')
    else:
        form = CustomUserEditForm(instance=user)
    return render(request, 'accounts/edit_user.html', {'form': form, 'user': user})


@login_required
@role_required('admin')
def unlock_user(request, user_id):
    """Admin action to manually unlock a locked account."""
    user = get_object_or_404(CustomUser, pk=user_id)
    user.failed_login_attempts = 0
    user.locked_until = None
    user.otp_abuse_locked = False
    user.otp_abuse_locked_at = None
    user.save(update_fields=[
        'failed_login_attempts', 'locked_until',
        'otp_abuse_locked', 'otp_abuse_locked_at',
    ])

    log_security_event(
        'ACCOUNT_UNLOCKED_BY_ADMIN',
        request=request, actor=request.user,
        severity='critical',
        object_ref=f'User {user.username}',
        details=f'Account manually unlocked by admin "{request.user.username}".',
    )

    messages.success(request, f'Account "{user.username}" has been unlocked.')
    return redirect('accounts:edit_user', user_id=user_id)
