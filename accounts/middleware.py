"""
accounts/middleware.py
──────────────────────
Security middleware for the Nodi CBS back-office:

1. CustomerLockoutMiddleware — boots customer-role users from back-office pages.
2. GlobalRateLimitMiddleware — applies a blanket request-rate limit to
   unauthenticated requests on security-sensitive endpoints (login, reset, OTP)
   to mitigate volumetric abuse before it reaches view-level logic.
"""

from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.http import HttpResponse
from django.core.cache import cache

from .models import get_client_ip


# ════════════════════════════════════════════════════════════════════════════════
# CUSTOMER LOCKOUT (existing, unchanged logic)
# ════════════════════════════════════════════════════════════════════════════════

ALLOWED_FOR_CUSTOMERS = (
    '/accounts/login/',
    '/accounts/logout/',
    '/static/',
    '/media/',
    '/androidapi/',
    '/favicon',
)


class OfficialLockoutMiddleware:
    """Boots customer-role users out of any back-office page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user and user.is_authenticated and getattr(user, 'role', None) == 'customer':
            if not any(request.path.startswith(p) for p in ALLOWED_FOR_CUSTOMERS):
                logout(request)
                messages.error(
                    request,
                    "Members must use the Nodi mobile app. "
                    "This system is for staff only."
                )
                return redirect('accounts:login')
        return self.get_response(request)


# ════════════════════════════════════════════════════════════════════════════════
# GLOBAL RATE LIMITER FOR SECURITY ENDPOINTS
# ════════════════════════════════════════════════════════════════════════════════

# Paths that need global rate-limiting for unauthenticated users.
# Format: (path_prefix, max_requests_per_window, window_seconds)
RATE_LIMITED_PATHS = [
    ('/accounts/login/', 30, 300),         # 30 requests per 5 minutes
    ('/accounts/password_reset/', 10, 300), # 10 requests per 5 minutes
    ('/accounts/otp-login/', 15, 300),     # 15 requests per 5 minutes
]


class GlobalRateLimitMiddleware:
    """
    IP-based rate limiter for security-sensitive endpoints.
    Uses Django's cache framework (configure Redis/Memcached for production).
    Falls back gracefully if cache is unavailable.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Only rate-limit POST requests from unauthenticated users
        if request.method != 'POST':
            return self.get_response(request)

        user = getattr(request, 'user', None)
        if user and user.is_authenticated:
            return self.get_response(request)

        for path_prefix, max_requests, window in RATE_LIMITED_PATHS:
            if request.path.startswith(path_prefix):
                ip = get_client_ip(request)
                cache_key = f"rl:{path_prefix}:{ip}"

                try:
                    count = cache.get(cache_key, 0)
                    if count >= max_requests:
                        return HttpResponse(
                            "Too many requests. Please try again later.",
                            status=429,
                            content_type="text/plain",
                        )
                    cache.set(cache_key, count + 1, timeout=window)
                except Exception:
                    # If cache backend is down, don't block legitimate users
                    pass
                break

        return self.get_response(request)
