"""
accounts/decorators.py
----------------------
Role-Based Access Control decorators for SMIS views.

Roles (in ascending privilege order):
    customer | accounts_clerk | loan_officer | manager | admin

Usage:
    from accounts.decorators import role_required, min_role_required

    @login_required
    @role_required('admin')
    def user_management(request): ...

    @login_required
    @min_role_required('manager')   # manager AND admin can access
    def approve_expense(request): ...
"""

from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages

# ── Role hierarchy ──────────────────────────────────────────────────────────
ROLE_HIERARCHY = {
    'customer':       -1,
    'accounts_clerk': 1,
    'loan_officer':   2,
    'manager':        3,
    'admin':          4,
}

ROLE_DISPLAY = {
    'customer':       'Customer',
    'accounts_clerk': 'Accounts Clerk',
    'loan_officer':   'Loan Officer',
    'manager':        'Manager',
    'admin':          'Admin',
}


def _get_user_level(user):
    role = getattr(user, 'role', 'customer')
    # Treat is_admin flag as admin-level regardless of role field
    if getattr(user, 'is_admin', False):
        return ROLE_HIERARCHY['admin']
    return ROLE_HIERARCHY.get(role, 0)


# ── Decorators ──────────────────────────────────────────────────────────────

def role_required(*allowed_roles):
    """
    Restrict view to users with one of the explicitly listed roles.
    Admin users always pass.

    @login_required
    @role_required('manager', 'admin')
    def some_view(request): ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect('accounts:login')

            user_role = getattr(user, 'role', 'customer')
            is_admin  = getattr(user, 'is_admin', False)

            if is_admin or user_role == 'admin' or user_role in allowed_roles:
                return view_func(request, *args, **kwargs)

            role_labels = ', '.join(
                ROLE_DISPLAY.get(r, r) for r in allowed_roles
            )
            messages.warning(
                request,
                f"Access denied. This section requires: {role_labels}."
            )
            return redirect('administration:user_profile')
        return _wrapped
    return decorator


def min_role_required(min_role):
    """
    Restrict view to users at or above min_role in the hierarchy.
    e.g. min_role_required('manager') allows manager and admin.

    @login_required
    @min_role_required('manager')
    def some_view(request): ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect('accounts:login')

            user_level     = _get_user_level(user)
            required_level = ROLE_HIERARCHY.get(min_role, 99)

            if user_level >= required_level:
                return view_func(request, *args, **kwargs)

            messages.warning(
                request,
                f"Access denied. You need at least '{ROLE_DISPLAY.get(min_role, min_role)}' access."
            )
            return redirect('administration:user_profile')
        return _wrapped
    return decorator


def staff_required(view_func):
    """Convenience: any non-customer staff member (accounts_clerk and above)."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect('accounts:login')
        if _get_user_level(user) >= ROLE_HIERARCHY['accounts_clerk']:
            return view_func(request, *args, **kwargs)
        messages.warning(request, "Access denied. Staff access required.")
        return redirect('accounts:login')
    return _wrapped
