"""
accounting/audit_trail.py
==========================
Helper functions for recording transaction-level audit trail entries.
The TransactionAuditLog model itself lives in accounting/models.py so
Django's migration framework can discover it.
"""

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


def log_transaction(
    action,
    reference,
    affected_accounts,
    total_amount,
    user=None,
    request=None,
    description='',
    before_snapshot=None,
    after_snapshot=None,
    maker='',
    checker='',
    approval_id=None,
    customer_ref='',
    external_ref='',
):
    """
    Record a transaction in the audit trail.
    Called after every successful journal posting.
    Never raises — failures are logged but never propagate.
    """
    from accounting.models import TransactionAuditLog

    username = ''
    user_obj = None
    ip = None

    if user:
        if hasattr(user, 'pk'):
            user_obj = user
            username = (user.get_username()
                        if hasattr(user, 'get_username') else str(user))
        else:
            username = str(user)

    if request:
        fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
        ip = fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR')
        if not user_obj and hasattr(request, 'user') and request.user.is_authenticated:
            user_obj = request.user
            username = username or request.user.get_username()

    trial_diff = Decimal('0')
    try:
        from accounting.balance_guard import check_trial_balance
        _, trial_diff, _ = check_trial_balance()
    except Exception:
        pass

    try:
        return TransactionAuditLog.objects.create(
            action=action,
            reference=str(reference)[:100],
            description=description[:1000] if description else '',
            performed_by=user_obj,
            performed_by_username=str(username)[:150],
            ip_address=ip,
            affected_accounts=list(affected_accounts) if affected_accounts else [],
            total_amount=Decimal(str(total_amount or 0)),
            before_snapshot=before_snapshot or {},
            after_snapshot=after_snapshot or {},
            maker=str(maker)[:150] if maker else '',
            checker=str(checker)[:150] if checker else '',
            approval_id=approval_id,
            customer_ref=str(customer_ref)[:50] if customer_ref else '',
            external_ref=str(external_ref)[:100] if external_ref else '',
            trial_balance_diff=trial_diff,
        )
    except Exception:
        logger.exception(
            "Failed to create TransactionAuditLog for %s / %s", action, reference
        )
        return None


def get_balance_snapshot(account_codes):
    """
    Capture current balances for a list of account codes.
    Uses TigerBeetle as authoritative source when enabled.
    """
    try:
        from accounting.tigerbeetle import get_gl_balance, TB_ENABLED
        if TB_ENABLED:
            snapshot = {}
            for code in account_codes:
                try:
                    snapshot[code] = str(get_gl_balance(code))
                except Exception:
                    snapshot[code] = 'TB_ERROR'
            return snapshot
    except ImportError:
        pass

    # Fallback to PG
    from accounting.models import SaccoAccountBalance, SaccoAccount

    snapshot = {}
    for code in account_codes:
        try:
            acct = SaccoAccount.objects.get(account_code=code)
            bal = SaccoAccountBalance.objects.filter(sacco_account=acct).first()
            snapshot[code] = str(bal.balance) if bal else '0.00'
        except SaccoAccount.DoesNotExist:
            snapshot[code] = 'UNKNOWN'
    return snapshot
