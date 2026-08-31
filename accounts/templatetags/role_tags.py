"""
accounts/templatetags/role_tags.py
-----------------------------------
Template tags for role-based UI visibility in NODi CBS Lite templates.

Load in any template with: {% load role_tags %}

Tags:
    {% has_role 'manager' 'admin' as can_approve %}
    {% min_role 'accounts_clerk' as is_staff %}
    {% user_role_badge %}
"""

from django import template
from django.utils.html import mark_safe

register = template.Library()

# Import the canonical hierarchy from decorators so they stay in sync.
try:
    from accounts.decorators import ROLE_HIERARCHY
except ImportError:
    ROLE_HIERARCHY = {
        'customer':       -1,
        'accounts_clerk': 1,
        'loan_officer':   2,
        'manager':        3,
        'admin':          4,
    }

ROLE_BADGE_COLORS = {
    'admin':          'danger',
    'manager':        'primary',
    'loan_officer':   'info',
    'accounts_clerk': 'secondary',
    'customer':       'light',
}

ROLE_DISPLAY = {
    'admin':          'Admin',
    'manager':        'Manager',
    'loan_officer':   'Loan Officer',
    'accounts_clerk': 'Accounts Clerk',
    'customer':       'Customer',
}


def _user_level(user):
    if not user or not user.is_authenticated:
        return -1
    if getattr(user, 'is_admin', False):
        return ROLE_HIERARCHY.get('admin', 4)
    return ROLE_HIERARCHY.get(getattr(user, 'role', 'customer'), 0)


@register.simple_tag(takes_context=True)
def has_role(context, *roles):
    """
    Returns True if the current user has any of the listed roles.

    {% has_role 'manager' 'admin' as can_approve %}
    {% if can_approve %} ... {% endif %}
    """
    request = context.get('request')
    if not request:
        return False
    user = request.user
    if not user.is_authenticated:
        return False
    if getattr(user, 'is_admin', False):
        return True
    return getattr(user, 'role', 'customer') in roles


@register.simple_tag(takes_context=True)
def min_role(context, role):
    """
    Returns True if the current user is at or above the given role level.

    {% min_role 'manager' as is_manager_plus %}
    {% if is_manager_plus %} ... {% endif %}
    """
    request = context.get('request')
    if not request:
        return False
    user_level     = _user_level(request.user)
    required_level = ROLE_HIERARCHY.get(role, 99)
    return user_level >= required_level


@register.simple_tag(takes_context=True)
def user_role_badge(context):
    """
    Renders a Bootstrap badge with the user's current role.
    {% user_role_badge %}
    """
    request = context.get('request')
    if not request or not request.user.is_authenticated:
        return ''
    role   = getattr(request.user, 'role', 'customer')
    colour = ROLE_BADGE_COLORS.get(role, 'secondary')
    label  = ROLE_DISPLAY.get(role, role.replace('_', ' ').title())
    return mark_safe(
        f'<span class="badge bg-{colour} text-uppercase" style="font-size:0.65rem;">'
        f'{label}</span>'
    )


@register.filter
def status_badge(status):
    """
    Renders a Bootstrap status badge for any status string.
    {{ item.status|status_badge }}
    """
    colour_map = {
        'pending':           'warning',
        'pending_approval':  'warning',
        'approved':          'success',
        'active':            'success',
        'processed':         'success',
        'posted':            'success',
        'reconciled':        'success',
        'rejected':          'danger',
        'failed':            'danger',
        'cancelled':         'danger',
        'inactive':          'secondary',
        'not_requested':     'secondary',
        'draft':             'info',
        'unreconciled':      'info',
        'open':              'info',
    }
    if not status:
        return ''
    key    = str(status).lower().replace(' ', '_')
    colour = colour_map.get(key, 'secondary')
    label  = str(status).replace('_', ' ').title()
    return mark_safe(
        f'<span class="badge bg-{colour}">{label}</span>'
    )
