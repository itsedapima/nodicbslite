"""
sms/events.py
==============
Single entry-point for "something happened to a member's account, so notify
them" — used by every accounting/transactions/loans/customers view that
touches a member's savings, loan, or membership state.

Why this exists alongside sms/services.py:
  • sms.services.notify() is the low-level transport — it persists an
    SMSLog row and tries to deliver it. It knows nothing about WHAT
    happened.
  • sms.events.notify_member_event() is the BUSINESS layer — it knows
    which template to use for which event, looks up the live balance,
    and never crashes the caller's transaction.

Every view that mutates a member's ledger should call exactly one of:
    notify_savings_deposit(...)
    notify_savings_withdrawal(...)
    notify_loan_repayment(...)
    notify_loan_disbursed(...)
    notify_guarantor_added(...)
    notify_guarantor_released(...)
    notify_dividend_credited(...)
    notify_cheque_issued(...)
    notify_member_exit(...)
    notify_welcome(...)
    notify_member_event(...)              # generic free-form
AFTER the database commit, never inside it. The decorator
@member_notification_after_commit handles this automatically.

NEVER raises. A failed notification must NEVER roll back a financial
transaction.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from functools import wraps
from typing import Optional

from django.db import transaction as db_tx

from .services import (
    notify,
    msg_deposit,
    msg_loan_repayment,
    msg_loan_disbursed,
    msg_guarantor_added,
    msg_guarantor_released,
    msg_member_exit,
    msg_approval_pending,
    msg_welcome,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════
#  Public event helpers
# ════════════════════════════════════════════════════════════════════════

def notify_savings_deposit(customer, amount, account_label, balance=None,
                           actor='system'):
    """Member deposited / received a credit to a savings account."""
    if not _has_phone(customer):
        return None
    try:
        if balance is None:
            balance = _savings_balance(customer.cust_no, account_label)
        msg = msg_deposit(customer.first_name, Decimal(str(amount)),
                          account_label, Decimal(str(balance)))
        return notify(customer.mobile or customer.phone, msg, created_by=str(actor)[:50])
    except Exception:
        logger.exception("notify_savings_deposit failed for %s", getattr(customer, "cust_no", "?"))
        return None


def notify_savings_withdrawal(customer, amount, account_label, balance=None,
                              actor='system'):
    """Member's savings account was debited (withdrawal, cheque, EFT)."""
    if not _has_phone(customer):
        return None
    try:
        if balance is None:
            balance = _savings_balance(customer.cust_no, account_label)
        msg = (
            f"Dear {customer.first_name}, a withdrawal of KES "
            f"{Decimal(str(amount)):,.2f} has been posted from your "
            f"{account_label} account. New balance: KES "
            f"{Decimal(str(balance)):,.2f}."
        )
        return notify(customer.mobile or customer.phone, msg, created_by=str(actor)[:50])
    except Exception:
        logger.exception("notify_savings_withdrawal failed for %s", getattr(customer, "cust_no", "?"))
        return None


def notify_loan_repayment(customer, amount, loan_no, outstanding=None,
                          actor='system'):
    """Member made a loan repayment."""
    if not _has_phone(customer):
        return None
    try:
        if outstanding is None:
            outstanding = _loan_outstanding(loan_no)
        msg = msg_loan_repayment(customer.first_name, Decimal(str(amount)),
                                 loan_no, Decimal(str(outstanding)))
        return notify(customer.mobile or customer.phone, msg, created_by=str(actor)[:50])
    except Exception:
        logger.exception("notify_loan_repayment failed for loan %s", loan_no)
        return None


def notify_loan_disbursed(customer, loan_no, principal, net_disbursed,
                          installment=None, actor='system'):
    """A loan was disbursed to a member."""
    if not _has_phone(customer):
        return None
    try:
        msg = msg_loan_disbursed(
            customer.first_name, loan_no,
            Decimal(str(principal)),
            Decimal(str(net_disbursed)),
            Decimal(str(installment or 0)),
        )
        return notify(customer.mobile or customer.phone, msg, created_by=str(actor)[:50])
    except Exception:
        logger.exception("notify_loan_disbursed failed for loan %s", loan_no)
        return None


def notify_guarantor_added(guarantor_customer, borrower_name, loan_no, amount,
                           actor='system'):
    if not _has_phone(guarantor_customer):
        return None
    try:
        msg = msg_guarantor_added(
            guarantor_customer.first_name, borrower_name, loan_no,
            Decimal(str(amount)),
        )
        return notify(guarantor_customer.mobile or guarantor_customer.phone,
                      msg, created_by=str(actor)[:50])
    except Exception:
        logger.exception("notify_guarantor_added failed for loan %s", loan_no)
        return None


def notify_guarantor_released(guarantor_customer, loan_no, actor='system'):
    if not _has_phone(guarantor_customer):
        return None
    try:
        msg = msg_guarantor_released(guarantor_customer.first_name, loan_no)
        return notify(guarantor_customer.mobile or guarantor_customer.phone,
                      msg, created_by=str(actor)[:50])
    except Exception:
        logger.exception("notify_guarantor_released failed for loan %s", loan_no)
        return None


def notify_dividend_credited(customer, amount, account_label, batch_no,
                             actor='system'):
    """Dividend / interest payout posted to a member's account."""
    if not _has_phone(customer):
        return None
    try:
        msg = (
            f"Dear {customer.first_name}, your dividend payout of KES "
            f"{Decimal(str(amount)):,.2f} (Batch {batch_no}) has been "
            f"credited to your {account_label} account."
        )
        return notify(customer.mobile or customer.phone, msg, created_by=str(actor)[:50])
    except Exception:
        logger.exception("notify_dividend_credited failed for batch %s", batch_no)
        return None


def notify_cheque_issued(customer, amount, cheque_number, account_label,
                        actor='system'):
    """A banker's cheque was issued against a member's account."""
    if not _has_phone(customer):
        return None
    try:
        msg = (
            f"Dear {customer.first_name}, a banker's cheque "
            f"#{cheque_number} for KES {Decimal(str(amount)):,.2f} has "
            f"been issued from your {account_label} account. "
            f"Contact us immediately if this was unauthorised."
        )
        return notify(customer.mobile or customer.phone, msg, created_by=str(actor)[:50])
    except Exception:
        logger.exception("notify_cheque_issued failed for #%s", cheque_number)
        return None


def notify_member_exit(customer, settlement_total, actor='system'):
    """Member exit settlement complete."""
    if not _has_phone(customer):
        return None
    try:
        msg = msg_member_exit(customer.first_name, Decimal(str(settlement_total)))
        return notify(customer.mobile or customer.phone, msg, created_by=str(actor)[:50])
    except Exception:
        logger.exception("notify_member_exit failed for %s", getattr(customer, "cust_no", "?"))
        return None


def notify_welcome(customer, actor='system'):
    """New member registered."""
    if not _has_phone(customer):
        return None
    try:
        msg = msg_welcome(customer.first_name)
        return notify(customer.mobile or customer.phone, msg, created_by=str(actor)[:50])
    except Exception:
        logger.exception("notify_welcome failed for %s", getattr(customer, "cust_no", "?"))
        return None


def notify_approval_pending(customer, action_label, ref, actor='system'):
    """Member's request is awaiting manager approval."""
    if not _has_phone(customer):
        return None
    try:
        msg = msg_approval_pending(customer.first_name, action_label, ref)
        return notify(customer.mobile or customer.phone, msg, created_by=str(actor)[:50])
    except Exception:
        logger.exception("notify_approval_pending failed for %s", ref)
        return None


def notify_member_event(customer, message: str, actor='system'):
    """Generic escape hatch — caller supplies a fully-formed message."""
    if not _has_phone(customer):
        return None
    try:
        return notify(customer.phone, message,
                      created_by=str(actor)[:50])
    except Exception:
        logger.exception("notify_member_event failed")
        return None


# ════════════════════════════════════════════════════════════════════════
#  After-commit wrapper — schedule SMS so it fires AFTER the transaction
#  successfully commits (and never if it rolls back).
# ════════════════════════════════════════════════════════════════════════

def after_commit(fn, *args, **kwargs):
    """
    Schedule fn(*args, **kwargs) to run after the current outer atomic
    block commits. If called outside an atomic block, runs immediately.
    Errors are caught and logged — they never propagate.

        with transaction.atomic():
            SavingsTransaction.objects.create(...)
            after_commit(notify_savings_deposit, customer, amount, label)
    """
    def _safe():
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception("after_commit callback failed")
    try:
        db_tx.on_commit(_safe)
    except Exception:
        # Outside an atomic block — just run it.
        _safe()


def member_notification_after_commit(get_args):
    """
    Decorator for view functions: after the view returns successfully,
    fire SMS notifications. `get_args` is a callable taking the request
    that returns a list of (notify_fn, args, kwargs) tuples.

    Usually it's simpler to call after_commit() inline; this decorator
    is here for cases where you want SMS to fire regardless of which
    branch of the view returned.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            response = view_func(request, *args, **kwargs)
            try:
                for fn, fargs, fkwargs in (get_args(request) or []):
                    after_commit(fn, *fargs, **(fkwargs or {}))
            except Exception:
                logger.exception("member_notification_after_commit setup failed")
            return response
        return _wrapped
    return decorator


# ════════════════════════════════════════════════════════════════════════
#  Internal helpers
# ════════════════════════════════════════════════════════════════════════

def _has_phone(customer) -> bool:
    if customer is None:
        return False
    mobile = getattr(customer, "mobile", None) or getattr(customer, "phone", None)
    return bool(mobile and str(mobile).strip())


def _sender_name() -> str:
    """Return the chama/SACCO display name for SMS sender branding."""
    try:
        from administration.models import ChamaInfo
        info = ChamaInfo.objects.first()
        if info and info.company_name:
            return info.company_name
    except Exception:
        pass
    return "SACCO"

def _savings_balance(cust_no, account_label) -> Decimal:
    """Compute the live savings balance for a member/account."""
    try:
        from django.db.models import Sum, F
        from transactions.models import SavingsTransaction
        bal = (
            SavingsTransaction.objects
            .filter(cust_no=cust_no, saving_type=account_label)
            .aggregate(t=Sum(F("credit_amount") - F("debit_amount")))["t"]
        )
        return Decimal(str(bal or 0))
    except Exception:
        return Decimal("0")


def _loan_outstanding(loan_no) -> Decimal:
    try:
        from django.db.models import Sum, F
        from transactions.models import LoanTransaction
        bal = (
            LoanTransaction.objects
            .filter(loan_no=loan_no)
            .aggregate(t=Sum(F("debit_amount") - F("credit_amount")))["t"]
        )
        return Decimal(str(bal or 0))
    except Exception:
        return Decimal("0")
