"""
sms/tasks.py
-------------
Django-Q2 background workers for outbound SMS & email queues.

Retry semantics:
  • Only rows with status='pending' AND attempts < MAX_ATTEMPTS AND
    (never attempted OR last_attempt_at older than RETRY_COOLDOWN_HOURS)
    are picked up.
  • Each attempt increments `attempts` and stamps `last_attempt_at`.
  • On failure, the row stays 'pending' until MAX_ATTEMPTS is reached,
    at which point it is terminally marked 'failed'.
  • The SMS worker is called every 1 minute via Django-Q2 schedule.
  • The email worker is called every 1 minute via Django-Q2 schedule.

Both workers:
  1. Fetch a batch of eligible items ordered by created_at (oldest first).
  2. Check the recipient's notification permission (temp override → default).
  3. Send via the configured transport (Celcom Africa HTTP API / SMTP).
  4. Update each item's status individually so a mid-batch crash leaves
     the queue in a consistent state.

On the SMS side, a successful send captures the provider's `messageid`
into SMSLog.provider_message_id for later DLR lookups.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.db.models import Q
from django.utils import timezone

from .models import SMSLog, EmailLog
from .utils import (
    SMSGateway, SMSGatewayError,
    build_phone_permission_map, build_email_permission_map,
)

logger = logging.getLogger(__name__)

# Error tags — reused so the summary/reporting layer can group failures
ERR_DISABLED = 'Notifications disabled by user preference'
ERR_SMS_CONFIG = 'SMS config error'
ERR_EMAIL_CONFIG = 'Email config error'


# ═══════════════════════════════════════════════════════════════════════════
# Shared query filter — eligible for (re)attempt
# ═══════════════════════════════════════════════════════════════════════════

def _retryable_filter(max_attempts: int, cooldown_hours: int) -> Q:
    """
    Q filter for rows that are eligible for a delivery attempt:
      status = 'pending'
      AND attempts < max_attempts
      AND (last_attempt_at IS NULL  OR  last_attempt_at <= now − cooldown)
    """
    cutoff = timezone.now() - timedelta(hours=cooldown_hours)
    return (
        Q(status='pending')
        & Q(attempts__lt=max_attempts)
        & (Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lte=cutoff))
    )


# ═══════════════════════════════════════════════════════════════════════════
# SMS QUEUE WORKER
# ═══════════════════════════════════════════════════════════════════════════

def process_sms_queue(batch_size: int = 100) -> dict:
    """
    Called by Django-Q2 on a schedule (every 1 minute).
    Returns a small dict summary for logging / monitoring.
    """
    filt = _retryable_filter(SMSLog.MAX_ATTEMPTS, SMSLog.RETRY_COOLDOWN_HOURS)
    pending = list(
        SMSLog.objects.filter(filt).order_by('created_at')[:batch_size]
    )
    summary = {'total': len(pending), 'sent': 0, 'failed': 0, 'skipped': 0}

    if not pending:
        logger.debug('SMS queue clear — nothing to process.')
        return summary

    logger.info('SMS worker picked up %d item(s).', len(pending))

    # ── Config gate ──
    gateway = SMSGateway()
    ok, cfg_err = gateway.is_configured()
    if not ok:
        # Don't burn attempts on a config problem — leave rows untouched
        logger.error('%s: %s', ERR_SMS_CONFIG, cfg_err)
        summary['failed'] = len(pending)
        return summary

    # ── Permission lookup — one Customer query for the whole batch ──
    perm = build_phone_permission_map(sms.phone for sms in pending)

    try:
        for sms in pending:
            norm_phone = SMSGateway.normalise_phone(sms.phone)
            allowed = perm.get(norm_phone, True)  # non-customers → allowed
            if not allowed:
                sms.status = 'failed'
                sms.error_message = ERR_DISABLED
                sms.attempts = sms.MAX_ATTEMPTS  # terminal
                sms.last_attempt_at = timezone.now()
                sms.save(update_fields=[
                    'status', 'error_message', 'attempts', 'last_attempt_at',
                ])
                summary['skipped'] += 1
                continue

            try:
                provider_msg_id = gateway.send(norm_phone, sms.message)
                sms.record_success(
                    provider_message_id=provider_msg_id,
                    provider_response_code=200,
                )
                summary['sent'] += 1
                logger.info(
                    'SMS %s → %s delivered (msgid=%s).',
                    sms.pk, norm_phone, provider_msg_id,
                )
            except SMSGatewayError as e:
                sms.record_failure(str(e))
                summary['failed'] += 1
                logger.warning('SMS %s → %s failed (attempt %d/%d): %s',
                               sms.pk, norm_phone, sms.attempts,
                               sms.MAX_ATTEMPTS, e)
            except Exception as e:  # noqa: BLE001
                sms.record_failure(f'Unexpected: {e}')
                summary['failed'] += 1
                logger.exception('SMS %s — unexpected error', sms.pk)
    finally:
        gateway.close()

    logger.info(
        'SMS batch complete — sent=%d, failed=%d, skipped=%d.',
        summary['sent'], summary['failed'], summary['skipped'],
    )
    return summary


# ═══════════════════════════════════════════════════════════════════════════
# EMAIL QUEUE WORKER  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════

def _split_emails(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [e.strip() for e in raw.split(',') if e.strip()]


def process_email_queue(batch_size: int = 50) -> dict:
    """
    Send eligible EmailLog rows over a shared SMTP connection.
    Called by Django-Q2 on a schedule (every 1 minute).
    """
    filt = _retryable_filter(EmailLog.MAX_ATTEMPTS, EmailLog.RETRY_COOLDOWN_HOURS)
    pending = list(
        EmailLog.objects.filter(filt).order_by('created_at')[:batch_size]
    )
    summary = {'total': len(pending), 'sent': 0, 'failed': 0, 'skipped': 0}

    if not pending:
        logger.debug('Email queue clear — nothing to process.')
        return summary

    logger.info('Email worker picked up %d item(s).', len(pending))

    # ── Build a permission map over every recipient in the batch ──
    all_addrs = set()
    for item in pending:
        for raw in (item.recipient_to, item.recipient_cc, item.recipient_bcc):
            all_addrs.update(_split_emails(raw))
    perm = build_email_permission_map(all_addrs)

    def allowed(addr: str) -> bool:
        return perm.get(addr.lower(), True)

    # ── SMTP connection reused across the whole batch ──
    try:
        connection = get_connection()
        connection.open()
    except Exception as e:
        # Don't burn attempts on an SMTP config problem
        logger.error('%s: %s', ERR_EMAIL_CONFIG, e)
        summary['failed'] = len(pending)
        return summary

    try:
        for item in pending:
            to_list = [a for a in _split_emails(item.recipient_to) if allowed(a)]
            cc_list = [a for a in _split_emails(item.recipient_cc) if allowed(a)]
            bcc_list = [a for a in _split_emails(item.recipient_bcc) if allowed(a)]

            if not to_list:
                item.status = 'failed'
                item.error_message = (
                    ERR_DISABLED
                    if _split_emails(item.recipient_to)
                    else 'No valid recipient addresses'
                )
                item.attempts = item.MAX_ATTEMPTS
                item.last_attempt_at = timezone.now()
                item.save(update_fields=[
                    'status', 'error_message', 'attempts', 'last_attempt_at',
                ])
                summary['skipped'] += 1
                continue

            try:
                msg = EmailMessage(
                    subject=item.subject,
                    body=item.message_body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=to_list,
                    cc=cc_list or None,
                    bcc=bcc_list or None,
                    connection=connection,
                )
                if item.is_html:
                    msg.content_subtype = 'html'

                msg.send(fail_silently=False)
                item.record_success()
                summary['sent'] += 1
                logger.info('Email %s → %s delivered.', item.pk, to_list)
            except Exception as e:  # noqa: BLE001
                item.record_failure(str(e))
                summary['failed'] += 1
                logger.warning(
                    'Email %s failed (attempt %d/%d): %s',
                    item.pk, item.attempts, item.MAX_ATTEMPTS, e,
                )
    finally:
        try:
            connection.close()
        except Exception:
            pass

    logger.info(
        'Email batch complete — sent=%d, failed=%d, skipped=%d.',
        summary['sent'], summary['failed'], summary['skipped'],
    )
    return summary
