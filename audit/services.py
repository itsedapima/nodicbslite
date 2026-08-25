"""
audit/services.py
------------------
Convenience helpers for recording security and operational events.

Every helper:
  • Writes a SecurityEvent row (the durable audit trail).
  • Writes an EmailLog row addressed to settings.ADMIN_EMAIL via
    sms.services.notify_admin().
  • Never raises — failures are logged but never propagate, so a
    failed email never blocks the actual operation.
"""
import logging

from django.conf import settings

from sms.services import (
    notify_admin,
    email_admin_security_event,
    email_admin_financial_event,
    email_admin_system_event,
)

from .models import SecurityEvent

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════
#  Core
# ════════════════════════════════════════════════════════════════════════

def log_security_event(event, request=None, actor=None, severity='info',
                       details='', object_ref='', email_admin=True):
    """
    Record a security event. Always returns the SecurityEvent row (or
    None on persistence failure).

    Args:
        event:       short uppercase tag, e.g. 'LOGIN_FAILED'
        request:     optional HttpRequest — used to pull IP, user agent,
                     and actor if not given.
        actor:       optional string or User instance overriding request.user
        severity:    'info' | 'warning' | 'critical'
        details:     free-form text
        object_ref:  e.g. 'Customer 1234', 'Loan LN-0042'
        email_admin: if True, sends an EmailLog row to ADMIN_EMAIL.

    Returns:
        SecurityEvent | None
    """
    actor_user = None
    actor_str = None
    ip = None
    ua = None

    if request is not None:
        # Actor from request.user if authenticated
        ru = getattr(request, 'user', None)
        if ru is not None and getattr(ru, 'is_authenticated', False):
            actor_user = ru
            actor_str = ru.get_username() if hasattr(ru, 'get_username') else str(ru)
        # IP
        ip = _client_ip(request)
        ua = (request.META.get('HTTP_USER_AGENT') or '')[:255]

    # Explicit actor override
    if actor is not None:
        if hasattr(actor, 'pk'):
            actor_user = actor
            actor_str = actor.get_username() if hasattr(actor, 'get_username') else str(actor)
        else:
            actor_str = str(actor)

    if not actor_str:
        actor_str = 'anonymous'

    # ── Persist the audit row ──────────────────────────────────────
    try:
        evt = SecurityEvent.objects.create(
            event=str(event)[:64],
            severity=severity if severity in {'info', 'warning', 'critical'} else 'info',
            actor=actor_str[:150],
            actor_user=actor_user,
            ip_address=ip,
            user_agent=ua,
            object_ref=(object_ref or '')[:120] or None,
            details=details or '',
            email_sent=False,
        )
    except Exception:
        logger.exception("Failed to persist SecurityEvent (%s)", event)
        evt = None

    # ── Always notify admin via EmailLog ───────────────────────────
    if email_admin:
        try:
            full_details = details
            if ip:
                full_details = f"{full_details}\n(IP: {ip}, UA: {ua})"
            if object_ref:
                full_details = f"{full_details}\nObject: {object_ref}"

            subject, body, _ = email_admin_security_event(
                event=event, actor=actor_str, details=full_details,
            )
            notify_admin(subject=subject, body=body,
                         created_by=actor_str[:50])
            if evt:
                evt.email_sent = True
                evt.save(update_fields=['email_sent'])
        except Exception:
            logger.exception("Failed to email admin for security event %s", event)

    return evt


# ════════════════════════════════════════════════════════════════════════
#  Convenience: financial events (large transactions, disbursements, etc)
# ════════════════════════════════════════════════════════════════════════

def log_financial_event(event, amount, reference, actor='system',
                        details='', email_admin=True, severity='info'):
    """
    Record a high-value or sensitive financial event. Always emails
    admin so they have a tamper-evident parallel record.
    """
    try:
        evt = SecurityEvent.objects.create(
            event=str(event)[:64],
            severity=severity if severity in {'info', 'warning', 'critical'} else 'info',
            actor=str(actor)[:150] if actor else 'system',
            object_ref=str(reference)[:120] if reference else None,
            details=f"Amount: KES {amount}\n{details}",
            email_sent=False,
        )
    except Exception:
        logger.exception("Failed to persist financial SecurityEvent (%s)", event)
        evt = None

    if email_admin:
        try:
            subject, body, _ = email_admin_financial_event(
                event=event, amount=amount, ref=reference,
                actor=actor or 'system', details=details,
            )
            notify_admin(subject=subject, body=body,
                         created_by=str(actor or 'system')[:50])
            if evt:
                evt.email_sent = True
                evt.save(update_fields=['email_sent'])
        except Exception:
            logger.exception("Failed to email admin for financial event %s", event)

    return evt


# ════════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════════

def _client_ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if fwd:
        return fwd.split(',')[0].strip() or None
    return request.META.get('REMOTE_ADDR') or None


# ════════════════════════════════════════════════════════════════════════
#  Configuration helpers
# ════════════════════════════════════════════════════════════════════════

def large_transaction_threshold():
    """
    Amounts at or above this value trigger an automatic financial
    notification to the admin. Override in settings as
    `LARGE_TRANSACTION_THRESHOLD` (Decimal-coercible).
    """
    from decimal import Decimal
    raw = getattr(settings, 'LARGE_TRANSACTION_THRESHOLD', '50000')
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal('50000')
