"""
sms/notification_helpers.py
============================
The personalized SMS generation engine for FrequentNotification templates.

Each public function corresponds to one FrequentNotification.category.
They all:
  1. Read from MemberSnapshot (cheap — no joins)
  2. Build a personalized message per eligible member
  3. bulk_create SMSLog rows with status='pending'
  4. Return the count of SMS queued

The existing sms.tasks.process_sms_queue() picks them up and delivers.

Usage from Django-Q2 schedule or management command:
    from sms.notification_helpers import run_notification
    run_notification(notification_id=5)      # by PK
    run_notification(category='loan_arrears')  # by category

All helpers are idempotent-safe — they create new SMSLog rows each run,
but the scheduler controls frequency (daily / weekly / monthly).
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from .models import FrequentNotification, MemberSnapshot, SMSLog

logger = logging.getLogger(__name__)

ZERO = Decimal('0.00')


# ═══════════════════════════════════════════════════════════════════════════
#  Shared context builders
# ═══════════════════════════════════════════════════════════════════════════

def _paybill() -> str:
    return getattr(settings, 'MPESA_SHORTCODE', '') or '000000'


def _sacco_name() -> str:
    return getattr(settings, 'SMS_SENDER_NAME', 'SACCO')


def _base_context(snap: MemberSnapshot) -> dict:
    """Context dict available to every template."""
    return {
        'first_name':   snap.first_name or 'Member',
        'cust_no':      snap.cust_no,
        'paybill':      _paybill(),
        'sacco_name':   _sacco_name(),
        'account_no':   '',    # overridden per-category
        'loan_no':      '',
        'loan_name':    '',
        'loan_balance': '',
        'arrears':      '',
        'installment':  '',
        'eligible_amount': '',
        'loan_offer':   '',
        'offers_list':  '',
        'min_balance':  '',
        'balance':      '',
    }


def _render(template: str, ctx: dict) -> str:
    """Safe format — unknown placeholders stay as-is."""
    try:
        return template.format(**ctx)
    except (KeyError, IndexError, ValueError):
        # Fallback: manual replace for each key
        msg = template
        for k, v in ctx.items():
            msg = msg.replace('{' + k + '}', str(v))
        return msg


def _queue_sms(rows: list[tuple[str, str]], created_by: str) -> int:
    """Bulk-create SMSLog rows. Returns count created."""
    if not rows:
        return 0
    logs = [
        SMSLog(
            phone=phone,
            message=message,
            status='pending',
            created_by=created_by[:20],
        )
        for phone, message in rows
        if phone and phone.strip()
    ]
    SMSLog.objects.bulk_create(logs, batch_size=500)
    return len(logs)


# ═══════════════════════════════════════════════════════════════════════════
#  Category handlers
# ═══════════════════════════════════════════════════════════════════════════

def _handle_savings_deposit_howto(notif: FrequentNotification) -> int:
    """
    Audience: ALL active members with a phone number.
    Tells them how to deposit to their savings_deposit account via Paybill.
    """
    snaps = MemberSnapshot.objects.filter(
        customer_status='active',
    ).exclude(phone='').exclude(phone__isnull=True)

    rows = []
    for snap in snaps.iterator(chunk_size=500):
        ctx = _base_context(snap)
        ctx['account_no'] = snap.savings_deposit_account or snap.cust_no
        rows.append((snap.phone, _render(notif.message_template, ctx)))

    return _queue_sms(rows, f'notif:{notif.pk}')


def _handle_share_capital_howto(notif: FrequentNotification) -> int:
    """
    Audience: ALL active members with a phone number.
    Tells them how to deposit to share capital via Paybill.
    """
    snaps = MemberSnapshot.objects.filter(
        customer_status='active',
    ).exclude(phone='').exclude(phone__isnull=True)

    rows = []
    for snap in snaps.iterator(chunk_size=500):
        ctx = _base_context(snap)
        ctx['account_no'] = snap.share_capital_account or snap.cust_no
        rows.append((snap.phone, _render(notif.message_template, ctx)))

    return _queue_sms(rows, f'notif:{notif.pk}')


def _handle_loan_repayment_howto(notif: FrequentNotification) -> int:
    """
    Audience: Members with at least one active loan.
    One SMS per active loan — each with the specific loan_no and account.
    """
    snaps = MemberSnapshot.objects.filter(
        customer_status='active',
        has_active_loan=True,
    ).exclude(phone='').exclude(phone__isnull=True)

    rows = []
    for snap in snaps.iterator(chunk_size=500):
        for loan in snap.iter_active_loans():
            ctx = _base_context(snap)
            ctx['loan_no'] = loan['loan_no']
            ctx['loan_name'] = loan['loan_name']
            ctx['loan_balance'] = f"{Decimal(loan['balance']):,.2f}"
            ctx['installment'] = f"{Decimal(loan['installment']):,.2f}"
            ctx['account_no'] = loan['account'] or snap.cust_no
            rows.append((snap.phone, _render(notif.message_template, ctx)))

    return _queue_sms(rows, f'notif:{notif.pk}')


def _handle_loan_arrears(notif: FrequentNotification) -> int:
    """
    Audience: Members whose snapshot shows total_arrears > 0.
    One SMS per loan that has arrears > 0.
    """
    snaps = MemberSnapshot.objects.filter(
        customer_status='active',
        has_active_loan=True,
        total_arrears__gt=0,
    ).exclude(phone='').exclude(phone__isnull=True)

    rows = []
    for snap in snaps.iterator(chunk_size=500):
        for loan in snap.iter_active_loans():
            arrears_val = Decimal(loan['arrears'] or '0')
            if arrears_val <= 0:
                continue
            ctx = _base_context(snap)
            ctx['loan_no'] = loan['loan_no']
            ctx['loan_name'] = loan['loan_name']
            ctx['loan_balance'] = f"{Decimal(loan['balance']):,.2f}"
            ctx['arrears'] = f"{arrears_val:,.2f}"
            ctx['installment'] = f"{Decimal(loan['installment']):,.2f}"
            ctx['account_no'] = loan['account'] or snap.cust_no
            rows.append((snap.phone, _render(notif.message_template, ctx)))

    return _queue_sms(rows, f'notif:{notif.pk}')


def _format_offers_list(offers: list) -> str:
    """
    Turn a list of offer dicts into readable prose:
      "Emergency Loan up to KES 150,000 and Development Loan up to KES 90,000"
    Handles 1 or 2 offers cleanly (snapshot caps at 2).
    """
    parts = []
    for o in offers:
        amt = Decimal(o.get('max_amount', '0'))
        parts.append(f"{o.get('name', 'a loan')} up to KES {amt:,.0f}")
    if not parts:
        return ''
    if len(parts) == 1:
        return parts[0]
    return ' and '.join(parts)


def _handle_loan_eligibility(notif: FrequentNotification) -> int:
    """
    Combined, product-agnostic loan marketing.

    Audience: Active members with at least one pre-qualified offer in
    their pool (`has_any_offer=True`). The pool is already filtered at
    snapshot-build time — member doesn't hold the product, has no
    arrears, ceiling > 0 — and capped to the top 2 by amount.

    We surface the member's BEST offer via {loan_offer}, {loan_name},
    {account_no}, {eligible_amount}. If the template uses {offers_list},
    both offers (up to 2) are named in one message — one SMS, maximum
    relevance, zero dead ends.

    Non-mobile loan offers below KES 50,000 are suppressed — a member
    won't apply for a non-mobile loan that small, so we save SMS tokens
    and maximize conversion.
    """
    MIN_NON_MOBILE_OFFER = Decimal('50000')

    snaps = MemberSnapshot.objects.filter(
        customer_status='active',
        has_any_offer=True,
    ).exclude(phone='').exclude(phone__isnull=True)

    rows = []
    for snap in snaps.iterator(chunk_size=500):
        offers = snap.eligible_offers or []
        # Filter out non-mobile offers below KES 50,000
        offers = [
            o for o in offers
            if o.get('is_mobile') or Decimal(o.get('max_amount', '0')) >= MIN_NON_MOBILE_OFFER
        ]
        if not offers:
            continue
        best = offers[0]  # already ranked by amount desc
        ctx = _base_context(snap)
        ctx['loan_offer'] = best['name']
        ctx['loan_name'] = best['name']
        ctx['account_no'] = best['account_no']
        ctx['eligible_amount'] = f"{Decimal(best['max_amount']):,.2f}"
        ctx['offers_list'] = _format_offers_list(offers)
        rows.append((snap.phone, _render(notif.message_template, ctx)))

    return _queue_sms(rows, f'notif:{notif.pk}')


def _handle_mobile_loan_eligibility(notif: FrequentNotification) -> int:
    """
    DEPRECATED — prefer `loan_eligibility`. Kept for backward compat.

    Audience: Active members who qualify for a mobile loan but do NOT
    currently have one — and are free of arrears (rules enforced at
    snapshot-build time via `eligible_offers`).
    """
    snaps = MemberSnapshot.objects.filter(
        customer_status='active',
        has_any_offer=True,
    ).exclude(phone='').exclude(phone__isnull=True)

    rows = []
    for snap in snaps.iterator(chunk_size=500):
        # Pick the member's best MOBILE offer, if any. The offer already
        # passed the "doesn't hold it + no arrears + cap>0" rules.
        offer = next((o for o in (snap.eligible_offers or []) if o.get('is_mobile')), None)
        if not offer:
            continue
        ctx = _base_context(snap)
        ctx['loan_name'] = offer['name']
        ctx['account_no'] = offer['account_no']
        ctx['eligible_amount'] = f"{Decimal(offer['max_amount']):,.2f}"
        rows.append((snap.phone, _render(notif.message_template, ctx)))

    return _queue_sms(rows, f'notif:{notif.pk}')


def _handle_normal_loan_eligibility(notif: FrequentNotification) -> int:
    """
    Audience: Active members who qualify for a NON-mobile loan product
    they do NOT already hold — and are free of arrears.

    This covers normal, emergency, development, or any other non-mobile
    loan the member doesn't have. The offer is chosen from the member's
    pre-computed top-2 `eligible_offers`, so we never pitch a product
    they already hold and never pitch to an arrears-carrying member.
    """
    snaps = MemberSnapshot.objects.filter(
        customer_status='active',
        has_any_offer=True,
    ).exclude(phone='').exclude(phone__isnull=True)

    rows = []
    for snap in snaps.iterator(chunk_size=500):
        # Best NON-mobile offer.
        offer = next((o for o in (snap.eligible_offers or []) if not o.get('is_mobile')), None)
        if not offer:
            continue
        ctx = _base_context(snap)
        ctx['loan_name'] = offer['name']
        ctx['account_no'] = offer['account_no']
        ctx['eligible_amount'] = f"{Decimal(offer['max_amount']):,.2f}"
        rows.append((snap.phone, _render(notif.message_template, ctx)))

    return _queue_sms(rows, f'notif:{notif.pk}')


def _handle_fixed_deposit_marketing(notif: FrequentNotification) -> int:
    """
    Marketing: Fixed Deposit Account.

    Audience: Active members, optionally filtered by savings_deposit_balance
    range using balance_filter_min / balance_filter_max on the template.
    Encourages members to save into their Fixed Deposit account.

    Available placeholders: {account_no} (FD account), {balance} (current
    savings deposit balance), plus all base placeholders.
    """
    filters = dict(customer_status='active')
    snaps_qs = MemberSnapshot.objects.filter(
        **filters
    ).exclude(phone='').exclude(phone__isnull=True)

    # Apply optional balance range filters (on savings_deposit_balance)
    if notif.balance_filter_min is not None:
        snaps_qs = snaps_qs.filter(savings_deposit_balance__gte=notif.balance_filter_min)
    if notif.balance_filter_max is not None:
        snaps_qs = snaps_qs.filter(savings_deposit_balance__lte=notif.balance_filter_max)

    rows = []
    for snap in snaps_qs.iterator(chunk_size=500):
        ctx = _base_context(snap)
        ctx['account_no'] = snap.fixed_deposit_account or snap.cust_no
        ctx['balance'] = f"{snap.savings_deposit_balance:,.2f}"
        rows.append((snap.phone, _render(notif.message_template, ctx)))

    return _queue_sms(rows, f'notif:{notif.pk}')


def _handle_share_capital_marketing(notif: FrequentNotification) -> int:
    """
    Marketing: Share Capital Below Minimum.

    Audience: Active members whose share_capital_balance is below the
    required minimum. The threshold comes from:
      1. balance_filter_min on the template (if set), OR
      2. CustomerAccountsSetup.min_balance for the share_capital product

    If balance_filter_max is also set, only target members within that
    range (e.g. between 0 and balance_filter_max).

    Available placeholders: {account_no} (SC account), {min_balance}
    (the required minimum), {balance} (current SC balance).
    """
    from transactions.models import CustomerAccountsSetup

    # Determine the threshold
    if notif.balance_filter_min is not None:
        threshold = notif.balance_filter_min
    else:
        sc_setup = CustomerAccountsSetup.objects.filter(
            account_type='share_capital'
        ).first()
        threshold = sc_setup.min_balance if sc_setup else ZERO

    snaps_qs = MemberSnapshot.objects.filter(
        customer_status='active',
        share_capital_balance__lt=threshold,
    ).exclude(phone='').exclude(phone__isnull=True)

    if notif.balance_filter_max is not None:
        snaps_qs = snaps_qs.filter(share_capital_balance__gte=notif.balance_filter_max)

    rows = []
    for snap in snaps_qs.iterator(chunk_size=500):
        ctx = _base_context(snap)
        ctx['account_no'] = snap.share_capital_account or snap.cust_no
        ctx['min_balance'] = f"{threshold:,.2f}"
        ctx['balance'] = f"{snap.share_capital_balance:,.2f}"
        rows.append((snap.phone, _render(notif.message_template, ctx)))

    return _queue_sms(rows, f'notif:{notif.pk}')


def _handle_dormant_reactivation(notif: FrequentNotification) -> int:
    """
    Audience: Dormant members — encourage them to deposit and reactivate.
    """
    snaps = MemberSnapshot.objects.filter(
        customer_status='dormant',
    ).exclude(phone='').exclude(phone__isnull=True)

    rows = []
    for snap in snaps.iterator(chunk_size=500):
        ctx = _base_context(snap)
        ctx['account_no'] = snap.savings_deposit_account or snap.cust_no
        rows.append((snap.phone, _render(notif.message_template, ctx)))

    return _queue_sms(rows, f'notif:{notif.pk}')


def _handle_happy_birthday(notif: FrequentNotification) -> int:
    """
    Audience: Active members whose birthday is TODAY.
    """
    today = date.today()
    snaps = MemberSnapshot.objects.filter(
        customer_status='active',
        dob__month=today.month,
        dob__day=today.day,
    ).exclude(phone='').exclude(phone__isnull=True)

    rows = []
    for snap in snaps.iterator(chunk_size=500):
        ctx = _base_context(snap)
        rows.append((snap.phone, _render(notif.message_template, ctx)))

    return _queue_sms(rows, f'notif:{notif.pk}')


def _handle_happy_holiday(notif: FrequentNotification) -> int:
    """
    Audience: ALL active members — seasonal/holiday greetings.
    """
    snaps = MemberSnapshot.objects.filter(
        customer_status='active',
    ).exclude(phone='').exclude(phone__isnull=True)

    rows = []
    for snap in snaps.iterator(chunk_size=500):
        ctx = _base_context(snap)
        rows.append((snap.phone, _render(notif.message_template, ctx)))

    return _queue_sms(rows, f'notif:{notif.pk}')


# ═══════════════════════════════════════════════════════════════════════════
#  Dispatcher — maps category → handler
# ═══════════════════════════════════════════════════════════════════════════

_HANDLERS = {
    'savings_deposit_howto':    _handle_savings_deposit_howto,
    'share_capital_howto':      _handle_share_capital_howto,
    'loan_repayment_howto':     _handle_loan_repayment_howto,
    'loan_arrears':             _handle_loan_arrears,
    'loan_eligibility':         _handle_loan_eligibility,
    'fixed_deposit_marketing':  _handle_fixed_deposit_marketing,
    'share_capital_marketing':  _handle_share_capital_marketing,
    'mobile_loan_eligibility':  _handle_mobile_loan_eligibility,   # deprecated
    'normal_loan_eligibility':  _handle_normal_loan_eligibility,   # deprecated
    'dormant_reactivation':     _handle_dormant_reactivation,
    'happy_birthday':           _handle_happy_birthday,
    'happy_holiday':            _handle_happy_holiday,
}


def run_notification(notification_id: int = None, category: str = None) -> dict:
    """
    Public entry point — call from Django-Q2 schedule or management command.

    Pass EITHER notification_id (PK) OR category (runs ALL active in that category).
    Returns {'notification': name, 'queued': count, 'status': 'ok'|'error', ...}
    """
    results = []

    if notification_id:
        notifs = FrequentNotification.objects.filter(pk=notification_id, is_active=True)
    elif category:
        notifs = FrequentNotification.objects.filter(category=category, is_active=True)
    else:
        logger.error("run_notification() called without notification_id or category.")
        return {'status': 'error', 'message': 'No notification_id or category provided.'}

    for notif in notifs:
        handler = _HANDLERS.get(notif.category)
        if not handler:
            logger.error("No handler for category '%s'", notif.category)
            results.append({
                'notification': notif.name,
                'status': 'error',
                'message': f"Unknown category: {notif.category}",
            })
            continue

        try:
            count = handler(notif)
            notif.last_run_at = timezone.now()
            notif.last_run_count = count
            notif.save(update_fields=['last_run_at', 'last_run_count'])
            logger.info(
                "Notification '%s' (pk=%d) queued %d SMS.",
                notif.name, notif.pk, count,
            )
            results.append({
                'notification': notif.name,
                'status': 'ok',
                'queued': count,
            })
        except Exception as exc:
            logger.exception("Failed to run notification '%s'", notif.name)
            results.append({
                'notification': notif.name,
                'status': 'error',
                'message': str(exc),
            })

    if len(results) == 1:
        return results[0]
    return {'results': results, 'status': 'ok'}
