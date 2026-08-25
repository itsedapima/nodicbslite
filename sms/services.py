"""
sms/services.py
----------------
Central member-communication service for NODi CBS / Eastakiba SACCO.

Two channels, one philosophy:
  • notify()        — SMS via SMSLog + Celcom Africa bulk-SMS HTTP API
  • email_notify()  — Email via EmailLog + Django SMTP backend
  • notify_admin()  — Email to settings.ADMIN_EMAIL (security/ops alerts)

Design principles:
  1. NEVER raise — a failed message must never roll back a financial
     transaction. Every message is persisted to its log table first
     (status='pending'), then delivery is attempted.
  2. Logs are the source of truth. A message that was attempted but
     failed to deliver is visible and will be retried by the queue
     worker (up to MAX_ATTEMPTS, with a 12-hour cooldown).
  3. Templates live here as functions, so subject/body wording is in
     one place. Callers pass facts, not strings.
"""

import logging
import traceback

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone
from django.utils.html import strip_tags

from .models import SMSLog, EmailLog
from .utils import SMSGateway, SMSGatewayError

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════
#                            SMS TEMPLATES
# ════════════════════════════════════════════════════════════════════════

def msg_deposit(first_name, amount, account_label, balance):
    return (
        f"Dear {first_name}, we have received KES {amount:,.2f} "
        f"for {account_label}. New balance is KES {balance:,.2f}. "
        f"Thank you."
    )


def msg_loan_repayment(first_name, amount, loan_no, outstanding):
    return (
        f"Dear {first_name}, your payment of KES {amount:,.2f} "
        f"for loan {loan_no} has been received. "
        f"Outstanding balance is KES {outstanding:,.2f}. Thank you."
    )


def msg_loan_approved(first_name, loan_no, principal, installment):
    """Sent when a manager approves the loan (before disbursement)."""
    return (
        f"Dear {first_name}, your loan application {loan_no} of "
        f"KES {principal:,.2f} has been approved. "
        f"Monthly installment: KES {installment:,.2f}. "
        f"You will be notified once funds are disbursed."
    )


def msg_loan_disbursed(first_name, loan_no, principal, net, installment):
    """Sent only when the loan is actually disbursed (bankers cheque/EFT)."""
    return (
        f"Dear {first_name}, your loan {loan_no} of KES {principal:,.2f} "
        f"has been disbursed. Net amount: KES {net:,.2f}. "
        f"Monthly installment: KES {installment:,.2f}."
    )


def msg_guarantor_added(first_name, borrower_name, loan_no, amount):
    return (
        f"Dear {first_name}, you have been recorded as a guarantor for "
        f"{borrower_name}'s loan {loan_no}, guaranteeing KES {amount:,.2f}. "
        f"Contact us if this was not authorised."
    )


def msg_guarantor_released(first_name, loan_no):
    return (
        f"Dear {first_name}, your guarantorship on loan {loan_no} "
        f"has been released."
    )


def msg_member_exit(first_name, settlement_total):
    return (
        f"Dear {first_name}, your membership exit has been processed. "
        f"Savings settled: KES {settlement_total:,.2f}. "
        f"We thank you for being a member."
    )


def msg_approval_pending(first_name, action_label, ref):
    return (
        f"Dear {first_name}, your {action_label} (Ref {ref}) has been "
        f"received and is pending approval. We will notify you once "
        f"processed."
    )


def msg_welcome(first_name):
    return (
        f"Dear {first_name}, welcome to {_sender()}! Your membership has "
        f"been registered. Save and grow with us."
    )


def _sender():
    return getattr(settings, 'SMS_SENDER_NAME', 'SACCO')


# ════════════════════════════════════════════════════════════════════════
#                          EMAIL TEMPLATES
#  Each returns (subject, plain_body, html_body_or_None)
# ════════════════════════════════════════════════════════════════════════

def email_welcome(customer):
    subject = f"Welcome to {_sender()}"
    body = (
        f"Dear {customer.first_name or customer.full_name},\n\n"
        f"Welcome to {_sender()}. Your membership number is {customer.cust_no}.\n\n"
        f"You can now start saving and become eligible for loans. "
        f"Our team will be in touch with next steps.\n\n"
        f"Kind regards,\n{_sender()}"
    )
    return subject, body, None


def email_deposit_received(customer, amount, account_label, balance, tr_ref):
    subject = f"Payment received — KES {amount:,.2f} (Ref {tr_ref})"
    body = (
        f"Dear {customer.first_name or customer.full_name},\n\n"
        f"We have received KES {amount:,.2f} for your {account_label}.\n"
        f"Reference: {tr_ref}\n"
        f"New balance: KES {balance:,.2f}\n\n"
        f"Thank you for transacting with {_sender()}."
    )
    return subject, body, None


def email_loan_repayment_received(customer, amount, loan_no, outstanding, tr_ref):
    subject = f"Loan repayment received — KES {amount:,.2f} (Loan {loan_no})"
    body = (
        f"Dear {customer.first_name or customer.full_name},\n\n"
        f"We have received KES {amount:,.2f} towards loan {loan_no}.\n"
        f"Reference: {tr_ref}\n"
        f"Outstanding balance: KES {outstanding:,.2f}\n\n"
        f"Thank you, {_sender()}."
    )
    return subject, body, None


def email_loan_disbursed(customer, loan_no, principal, net, installment):
    subject = f"Loan disbursed — {loan_no} (KES {principal:,.2f})"
    body = (
        f"Dear {customer.first_name or customer.full_name},\n\n"
        f"Your loan {loan_no} has been approved and disbursed.\n"
        f"  Principal:          KES {principal:,.2f}\n"
        f"  Net to you:         KES {net:,.2f}\n"
        f"  Monthly instalment: KES {installment:,.2f}\n\n"
        f"Please honour the repayment schedule.\n\n"
        f"Regards, {_sender()}"
    )
    return subject, body, None


def email_member_exit(customer, settlement_total):
    subject = f"Membership exit processed (Member {customer.cust_no})"
    body = (
        f"Dear {customer.first_name or customer.full_name},\n\n"
        f"Your exit from {_sender()} has been processed.\n"
        f"Savings settlement: KES {settlement_total:,.2f}.\n\n"
        f"We thank you for being a member."
    )
    return subject, body, None


def email_admin_security_event(event, actor, details):
    subject = f"[SMIS Security] {event}"
    body = (
        f"Security event recorded.\n\n"
        f"Event:   {event}\n"
        f"Actor:   {actor}\n"
        f"When:    {timezone.now().isoformat()}\n"
        f"Details: {details}\n"
    )
    return subject, body, None


def email_admin_financial_event(event, amount, ref, actor, details=''):
    subject = f"[SMIS Finance] {event} — KES {amount:,.2f} (Ref {ref})"
    body = (
        f"Financial event recorded.\n\n"
        f"Event:    {event}\n"
        f"Amount:   KES {amount:,.2f}\n"
        f"Ref:      {ref}\n"
        f"Actor:    {actor}\n"
        f"When:     {timezone.now().isoformat()}\n"
        f"Details:  {details}\n"
    )
    return subject, body, None


def email_admin_system_event(event, details=''):
    subject = f"[SMIS Ops] {event}"
    body = (
        f"System event recorded.\n\n"
        f"Event:   {event}\n"
        f"When:    {timezone.now().isoformat()}\n"
        f"Details: {details}\n"
    )
    return subject, body, None


# ════════════════════════════════════════════════════════════════════════
#                          SMS — public API
# ════════════════════════════════════════════════════════════════════════

def notify(phone, message, created_by='system', send_now=True):
    """
    Log an SMS to SMSLog and optionally attempt immediate delivery via
    the Celcom Africa HTTP API. NEVER raises — always returns the SMSLog
    row (or None if even logging failed).
    """
    if not phone or not str(phone).strip():
        logger.warning("notify() skipped — empty phone number. msg=%r", message[:60])
        return None

    phone = str(phone).strip()

    try:
        log = SMSLog.objects.create(
            phone=phone,
            message=message,
            status='pending',
            created_by=(created_by or 'system')[:20],
        )
    except Exception:
        logger.exception("Failed to persist SMSLog for %s", phone)
        return None

    if send_now:
        _sms_attempt_delivery(log)
    return log


def notify_many(recipients, created_by='system'):
    """recipients: iterable of (phone, message) tuples."""
    logs = []
    for phone, message in recipients:
        log = notify(phone, message, created_by=created_by)
        if log:
            logs.append(log)
    return logs


def _sms_attempt_delivery(log):
    """
    Attempt SMS delivery via the Celcom Africa HTTP API.
    Updates the log row's retry fields regardless of outcome.
    """
    gateway = SMSGateway()
    ok, cfg_err = gateway.is_configured()
    if not ok:
        # No gateway configured — leave as pending for the queue worker
        logger.debug("SMS gateway not configured (%s); queuing SMS %s.", cfg_err, log.pk)
        return

    try:
        norm_phone = SMSGateway.normalise_phone(log.phone)
        provider_msg_id = gateway.send(norm_phone, log.message)
        log.record_success(
            provider_message_id=provider_msg_id,
            provider_response_code=200,
        )
    except SMSGatewayError as exc:
        log.record_failure(str(exc))
        logger.warning("SMS delivery failed for %s: %s", log.phone, exc)
    except Exception as exc:
        log.record_failure(f"Unexpected: {exc}")
        logger.exception("SMS %s — unexpected error", log.pk)
    finally:
        gateway.close()


# ════════════════════════════════════════════════════════════════════════
#                         EMAIL — public API
# ════════════════════════════════════════════════════════════════════════

def email_notify(recipient_to, subject, body, html_body=None,
                 recipient_cc=None, recipient_bcc=None,
                 created_by='system', send_now=True):
    """
    Log an email to EmailLog and attempt delivery via Django's email
    backend. NEVER raises. Returns the EmailLog row (or None on log
    failure).
    """
    to_list  = _csv(recipient_to)
    cc_list  = _csv(recipient_cc)
    bcc_list = _csv(recipient_bcc)

    if not to_list:
        logger.warning("email_notify() skipped — no recipient. subject=%r", subject[:80])
        return None

    try:
        log = EmailLog.objects.create(
            recipient_to=to_list,
            recipient_cc=cc_list or None,
            recipient_bcc=bcc_list or None,
            subject=(subject or '')[:255],
            message_body=html_body if html_body else body,
            is_html=bool(html_body),
            status='pending',
            created_by=(created_by or 'system')[:50],
        )
    except Exception:
        logger.exception("Failed to persist EmailLog for %s", to_list)
        return None

    if send_now:
        _email_attempt_delivery(log, body, html_body)
    return log


def notify_admin(subject, body, html_body=None, created_by='system', send_now=True):
    """Send an alert to settings.ADMIN_EMAIL."""
    admin_email = (getattr(settings, 'ADMIN_EMAIL', '') or '').strip()
    if not admin_email:
        admin_email = (getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '').strip()
    if not admin_email:
        admin_email = 'admin-unconfigured@local'

    return email_notify(
        recipient_to=admin_email,
        subject=subject,
        body=body,
        html_body=html_body,
        created_by=created_by,
        send_now=send_now,
    )


def _email_attempt_delivery(log, plain_body, html_body=None):
    """
    Send the email through Django's email backend.
    Updates the log row's retry fields regardless of outcome.
    """
    if not getattr(settings, 'EMAIL_HOST', ''):
        return  # queue-only mode

    try:
        to_list  = _split(log.recipient_to)
        cc_list  = _split(log.recipient_cc)
        bcc_list = _split(log.recipient_bcc)

        msg = EmailMultiAlternatives(
            subject=log.subject,
            body=plain_body if plain_body else strip_tags(log.message_body),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=to_list,
            cc=cc_list or None,
            bcc=bcc_list or None,
        )
        if html_body or log.is_html:
            msg.attach_alternative(html_body or log.message_body, 'text/html')

        msg.send(fail_silently=False)
        log.record_success()

    except Exception as exc:
        tb = traceback.format_exc()[:5000]
        log.record_failure(f"{exc}\n{tb}")
        logger.warning("Email delivery failed for %s: %s", log.recipient_to, exc)


# ════════════════════════════════════════════════════════════════════════
#                            HELPERS
# ════════════════════════════════════════════════════════════════════════

def _csv(value):
    """Normalise a string or iterable of strings to a comma-separated string."""
    if not value:
        return ''
    if isinstance(value, str):
        return ','.join(part.strip() for part in value.split(',') if part.strip())
    return ','.join(str(v).strip() for v in value if str(v).strip())


def _split(csv_str):
    """Inverse of _csv — produces a list, safe on None/empty."""
    if not csv_str:
        return []
    return [s.strip() for s in csv_str.split(',') if s.strip()]
