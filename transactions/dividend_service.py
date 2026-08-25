"""
Dividend / Interest Posting Service
====================================
Adapted from nodicbs for nodicbslite.

All the heavy lifting for posting a calculated dividend batch:
  - Balanced GL journal entries (expense, savings liability, WHT, fees)
  - Member subledger credits
  - After-commit SMS notifications
  - Audit logging
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction as db_tx
from django.utils import timezone

from accounting.journal import journal_entry, leg
from accounting.models import SaccoAccount
from customers.models import Customer
from sms.events import after_commit, notify_dividend_credited
from transactions.models import (
    SavingsTransaction, DividendBatch, DividendDetail,
    CustomerAccountsSetup,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


class DividendPostingError(Exception):
    """Raised when a batch cannot be posted."""


def post_dividend_batch(batch_id: int, posted_by: str = "system") -> dict:
    """
    Post all unposted details in a DividendBatch.

    Returns a summary dict:
        {members, total_gross, total_tax, total_fee, total_net}

    Raises DividendPostingError on any inconsistency -- the caller's
    atomic block rolls back cleanly.
    """
    from transactions.utils import make_tr_ref

    # ── Operational Global GL Fallbacks ────────────────────────────
    gl_codes = getattr(settings, "DIVIDEND_GL_CODES", {})
    code_expense = gl_codes.get("expense",        "900-301000")
    code_wht     = gl_codes.get("wht_payable",    "900-700001")
    code_fees    = gl_codes.get("fee_income",      "900-124006")

    now = timezone.now()
    base_ref = make_tr_ref("dividend")

    sms_jobs = []
    total_gross = total_tax = total_fee = total_net = ZERO
    members_count = 0

    with db_tx.atomic():
        batch = DividendBatch.objects.select_for_update().get(pk=batch_id)
        if batch.is_posted:
            raise DividendPostingError(f"Batch {batch.batch_no} is already posted.")

        pending = list(
            batch.details.filter(is_posted=False).only(
                "id", "cust_no", "member_name", "gross_interest",
                "withholding_tax", "processing_fee", "net_payout", "is_posted",
            )
        )
        if not pending:
            raise DividendPostingError("No pending details to post.")

        # ── DYNAMIC LEDGER LOOKUP ──────────────────────────────────
        try:
            product_setup = CustomerAccountsSetup.objects.select_related(
                'sacco_gl_account'
            ).get(account_type=batch.saving_type, is_active=True)
            savings_acc = product_setup.sacco_gl_account
            if not savings_acc:
                raise DividendPostingError(
                    f"Product setup '{batch.saving_type}' lacks a linked GL Balance Account."
                )
        except CustomerAccountsSetup.DoesNotExist:
            raise DividendPostingError(
                f"No active account configuration found for type: '{batch.saving_type}'"
            )

        # Resolve remaining operational accounts
        try:
            expense_acc = SaccoAccount.objects.get(account_code=code_expense)
        except SaccoAccount.DoesNotExist as e:
            raise DividendPostingError(f"Required GL Expense account missing: {e}")

        wht_acc  = SaccoAccount.objects.filter(account_code=code_wht).first()
        fees_acc = SaccoAccount.objects.filter(account_code=code_fees).first()

        # ── NORMALIZE cust_no ─────────────────────────────────────
        cust_nos = list({str(d.cust_no) for d in pending})
        cust_map = {str(c.cust_no): c for c in Customer.objects.filter(cust_no__in=cust_nos)}

        savings_rows = []
        details_done = []

        for d in pending:
            cust_no_str = str(d.cust_no)

            gross  = Decimal(str(d.gross_interest or 0))
            tax    = Decimal(str(d.withholding_tax or 0))
            fee    = Decimal(str(d.processing_fee or 0))
            payout = Decimal(str(d.net_payout or 0))

            expected = gross - tax - fee
            if (expected - payout).copy_abs() > Decimal("0.01"):
                raise DividendPostingError(
                    f"Detail {d.id} (cust {cust_no_str}): gross={gross} "
                    f"!= net({payout}) + tax({tax}) + fee({fee})."
                )

            d.is_posted = True
            details_done.append(d)

            # Skip ledger operations if gross is zero
            if gross <= ZERO:
                continue

            tr_ref = f"{base_ref}-{cust_no_str}"
            desc = f"Dividends Payout - Batch {batch.batch_no}"

            # ── Subledger credit ──────────────────────────────────
            if payout > ZERO:
                savings_rows.append(SavingsTransaction(
                    cust_no=cust_no_str,
                    saving_type=batch.saving_type,
                    tr_date=now,
                    tr_ref=tr_ref,
                    tr_desc=desc,
                    credit_amount=payout,
                    debit_amount=ZERO,
                    created_by=posted_by,
                ))

            # ── Balanced GL journal ───────────────────────────────
            legs = [
                leg(expense_acc, debit=gross,
                    description=f"Dividend gross - {cust_no_str}"),
            ]

            if payout > ZERO:
                legs.append(leg(savings_acc, credit=payout,
                    customer=cust_map.get(cust_no_str),
                    description=f"Dividend net ({product_setup.account_name}) - {cust_no_str}"))

            if tax > ZERO:
                if not wht_acc:
                    raise DividendPostingError(
                        f"WHT of {tax} but no WHT Payable GL ({code_wht})."
                    )
                legs.append(leg(wht_acc, credit=tax,
                                description=f"WHT - {cust_no_str}"))

            if fee > ZERO:
                if not fees_acc:
                    raise DividendPostingError(
                        f"Fee of {fee} but no Fee Income GL ({code_fees})."
                    )
                legs.append(leg(fees_acc, credit=fee,
                                description=f"Processing fee - {cust_no_str}"))

            journal_entry(
                reference=tr_ref,
                description=desc,
                created_by=posted_by,
                legs=legs,
            )

            cust = cust_map.get(cust_no_str)
            if cust and payout > ZERO:
                sms_jobs.append((cust, payout))

            total_gross += gross
            total_tax += tax
            total_fee += fee
            total_net += payout

        # ── Bulk writes ───────────────────────────────────────────
        SavingsTransaction.objects.bulk_create(savings_rows, batch_size=500)
        DividendDetail.objects.bulk_update(details_done, ["is_posted"], batch_size=500)

        if not batch.details.filter(is_posted=False).exists():
            batch.is_posted = True
            batch.save(update_fields=["is_posted"])

        members_count = len(details_done)

    # ── After-commit SMS ──────────────────────────────────────────
    for cust, payout in sms_jobs:
        after_commit(notify_dividend_credited, cust, payout,
                     batch.saving_type, batch.batch_no, posted_by)

    # ── Audit ─────────────────────────────────────────────────────
    try:
        from audit.services import log_financial_event
        log_financial_event(
            event="DIVIDEND_BATCH_POSTED",
            amount=total_net,
            reference=batch.batch_no,
            actor=posted_by,
            details=f"members={members_count}, gross={total_gross}, "
                    f"tax={total_tax}, fee={total_fee}, net={total_net}",
        )
    except Exception:
        logger.exception("audit log for dividend batch failed")

    return {
        "members": members_count,
        "total_gross": total_gross,
        "total_tax": total_tax,
        "total_fee": total_fee,
        "total_net": total_net,
    }
