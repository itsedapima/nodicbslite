"""
accounting/journal.py
======================
Central double-entry journal service.

This is the **only** sanctioned way to write rows to SaccoAccountsLedger.

WHY
~~~
Every financial event has a balanced set of legs (sum of debits ==
sum of credits). If one leg ever goes in without its partner, the
trial balance is out of balance for the rest of the company's life.

Previously, 17 different call sites scattered across accounting/views.py,
loans/disbursement.py, transactions/views.py and the bankers-cheque
workflow each composed their own raw `SaccoAccountsLedger.objects.create(...)`
calls. Several of them are demonstrably unbalanced — that bleeds onto
the trial balance.

This service makes balance non-negotiable. A caller submits a list of
legs; the service:

  1. Validates: at least 2 legs, totals balance to within 0.01,
     each leg has a non-empty reference, non-negative amounts, and
     exactly ONE side (debit XOR credit, not both, not neither).
  2. Wraps the writes in transaction.atomic() so a failure halfway
     never leaves a half-posted journal.
  3. Returns the saved SaccoAccountsLedger rows.

Usage:

    from accounting.journal import journal_entry, leg

    journal_entry(
        reference="CHQ-001234",
        description="Banker's cheque #001234 - Acme Supplies",
        created_by=request.user.username,
        legs=[
            leg(bank_acc,    credit=10_000),
            leg(expense_acc, debit=10_000),
        ],
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Sequence, Union

from django.db import transaction as db_tx

# Tolerance for floating-point / rounding fuzz when comparing dr vs cr totals.
# 0.01 covers a single-cent rounding gap; anything bigger is a real bug.
TOLERANCE = Decimal("0.01")
ZERO = Decimal("0")


class JournalImbalanceError(ValueError):
    """Raised when a proposed journal entry doesn't balance."""


class JournalLegError(ValueError):
    """Raised when an individual leg is structurally invalid."""


@dataclass
class JournalLeg:
    """One side of a journal entry."""
    account: object              # SaccoAccount instance OR account_code str
    debit:   Decimal = ZERO
    credit:  Decimal = ZERO
    customer: object = None      # optional Customer (for member-related legs)
    description: Optional[str] = None  # leg-specific override


def _money(v) -> Decimal:
    """Coerce to Decimal with 2dp banker's rounding."""
    if v is None:
        return ZERO
    if not isinstance(v, Decimal):
        v = Decimal(str(v))
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def leg(account, debit=0, credit=0, customer=None, description=None) -> JournalLeg:
    """
    Convenience constructor. Pass `account` as either a SaccoAccount
    instance or its account_code string.
    """
    return JournalLeg(
        account=account,
        debit=_money(debit),
        credit=_money(credit),
        customer=customer,
        description=description,
    )


def _resolve_account(value):
    """Return a SaccoAccount instance, looking up by code if needed."""
    from accounting.models import SaccoAccount
    if isinstance(value, SaccoAccount):
        return value
    if isinstance(value, str):
        try:
            return SaccoAccount.objects.get(account_code=value)
        except SaccoAccount.DoesNotExist:
            raise JournalLegError(f"No SaccoAccount with code {value!r}")
    raise JournalLegError(f"Invalid account: {value!r}")


def _validate(legs: Sequence[JournalLeg]) -> tuple[Decimal, Decimal]:
    """
    Run structural and balance checks. Returns (total_debit, total_credit)
    once everything looks good.
    """
    if len(legs) < 2:
        raise JournalLegError(
            f"A journal entry needs at least 2 legs, got {len(legs)}."
        )

    total_dr = ZERO
    total_cr = ZERO

    for i, lg in enumerate(legs, start=1):
        if lg.debit < 0 or lg.credit < 0:
            raise JournalLegError(
                f"Leg {i}: amounts must be non-negative (dr={lg.debit}, cr={lg.credit})"
            )
        if lg.debit == ZERO and lg.credit == ZERO:
            raise JournalLegError(
                f"Leg {i}: must have a non-zero debit OR credit."
            )
        if lg.debit > ZERO and lg.credit > ZERO:
            raise JournalLegError(
                f"Leg {i}: a leg cannot be both a debit AND a credit "
                f"(dr={lg.debit}, cr={lg.credit}). Split it."
            )
        total_dr += lg.debit
        total_cr += lg.credit

    diff = (total_dr - total_cr).copy_abs()
    if diff > TOLERANCE:
        raise JournalImbalanceError(
            f"Journal does not balance: total debit KES {total_dr:,.2f}, "
            f"total credit KES {total_cr:,.2f}, difference KES {diff:,.2f}. "
            f"A double-entry transaction must have sum(debits) == sum(credits)."
        )

    return total_dr, total_cr


def journal_entry(
    *,
    reference: str,
    description: str,
    legs: Sequence[JournalLeg],
    created_by: str = "system",
    external_reference: str = "",
) -> List:
    """
    Post a balanced journal entry. Returns the SaccoAccountsLedger rows.

    WRITE ORDER (TB-first architecture):
      1. Validate legs (balance, structure)
      2. Write to TigerBeetle (synchronous, MUST succeed when TB enabled)
      3. Write to PostgreSQL (ledger rows for queries)
      4. Update PG balance cache

    Raises:
        JournalLegError       — if any leg is malformed.
        JournalImbalanceError — if total debits != total credits.
    """
    from accounting.models import SaccoAccountsLedger, SaccoAccountBalance

    if not reference or not str(reference).strip():
        raise JournalLegError("Journal reference is required.")
    if not description or not str(description).strip():
        raise JournalLegError("Journal description is required.")

    _validate(legs)

    # Resolve accounts up-front (needed for both TB and PG writes)
    resolved = [(lg, _resolve_account(lg.account)) for lg in legs]

    # ── TB-FIRST: Write to TigerBeetle (authoritative) ─────────────
    try:
        from accounting.tigerbeetle import tb_post_journal, TBPostingError
        tb_lines = [
            {'account_code': acct.account_code,
             'debit': lg.debit,
             'credit': lg.credit}
            for lg, acct in resolved
        ]
        tb_post_journal(tb_lines, reference=reference, description=description)
    except TBPostingError as e:
        raise JournalImbalanceError(
            f"TigerBeetle rejected journal '{reference}': {e}"
        )
    except ImportError:
        pass  # tigerbeetle not installed — PG-only mode

    # ── Write to PostgreSQL (query layer + balance cache) ──────────
    rows = []
    with db_tx.atomic():
        for lg, account in resolved:
            row_desc = lg.description or description
            row = SaccoAccountsLedger.objects.create(
                customer=lg.customer,
                sacco_account=account,
                reference=str(reference)[:100],
                external_reference=(external_reference or None),
                description=row_desc,
                amount=lg.debit or lg.credit,
                debit_amount=lg.debit,
                credit_amount=lg.credit,
                created_by=str(created_by)[:100],
            )
            rows.append(row)

            # PG balance cache update (non-authoritative when TB enabled)
            bal, _ = SaccoAccountBalance.objects.select_for_update().get_or_create(
                sacco_account=account,
                defaults={'balance': ZERO},
            )
            bal.balance = (bal.balance or ZERO) + lg.debit - lg.credit
            bal.save(update_fields=['balance'])

    return rows


# ── Bulk journal entry — high-throughput batch mode ───────────────────────────

def bulk_journal_entry(
    *,
    entries: Sequence[dict],
    created_by: str = "system",
) -> int:
    """
    Post many balanced journal entries in one shot using bulk_create.

    Each entry in `entries` is a dict:
        {
            "reference": str,
            "description": str,
            "legs": [JournalLeg, ...],
            "external_reference": str (optional),
        }

    Returns the total number of ledger rows created.

    All entries are validated BEFORE any writes. If any single entry
    fails validation, the entire batch is rejected (no partial writes).

    Balance cache updates are aggregated per account and applied in
    bulk at the end, rather than per-leg.

    Raises:
        JournalLegError       — if any leg is malformed.
        JournalImbalanceError — if any entry doesn't balance.
    """
    from accounting.models import SaccoAccountsLedger, SaccoAccountBalance

    if not entries:
        return 0

    # ── Phase 1: Validate ALL entries up-front ────────────────────────
    resolved_entries = []
    for idx, entry in enumerate(entries):
        ref = entry.get("reference", "")
        desc = entry.get("description", "")
        legs = entry.get("legs", [])
        ext_ref = entry.get("external_reference", "")

        if not ref or not str(ref).strip():
            raise JournalLegError(f"Entry {idx}: reference is required.")
        if not desc or not str(desc).strip():
            raise JournalLegError(f"Entry {idx}: description is required.")

        _validate(legs)
        resolved = [(lg, _resolve_account(lg.account)) for lg in legs]
        resolved_entries.append((ref, desc, ext_ref, resolved))

    # ── Phase 1b: TigerBeetle (if enabled) ────────────────────────────
    try:
        from accounting.tigerbeetle import tb_post_journal, TBPostingError
        for ref, desc, ext_ref, resolved in resolved_entries:
            tb_lines = [
                {'account_code': acct.account_code,
                 'debit': lg.debit, 'credit': lg.credit}
                for lg, acct in resolved
            ]
            tb_post_journal(tb_lines, reference=ref, description=desc)
    except TBPostingError as e:
        raise JournalImbalanceError(f"TigerBeetle rejected batch: {e}")
    except ImportError:
        pass  # PG-only mode

    # ── Phase 2: Build all ledger rows in memory ──────────────────────
    ledger_rows = []
    balance_deltas = {}  # {account_id: Decimal delta}

    for ref, desc, ext_ref, resolved in resolved_entries:
        for lg, account in resolved:
            row_desc = lg.description or desc
            ledger_rows.append(SaccoAccountsLedger(
                customer=lg.customer,
                sacco_account=account,
                reference=str(ref)[:100],
                external_reference=(ext_ref or None),
                description=row_desc,
                amount=lg.debit or lg.credit,
                debit_amount=lg.debit,
                credit_amount=lg.credit,
                created_by=str(created_by)[:100],
            ))

            # Aggregate balance delta per account
            delta = lg.debit - lg.credit
            balance_deltas[account.pk] = balance_deltas.get(account.pk, ZERO) + delta

    # ── Phase 3: Bulk write inside atomic block ───────────────────────
    with db_tx.atomic():
        SaccoAccountsLedger.objects.bulk_create(ledger_rows, batch_size=1000)

        # Bulk-update balance cache — one UPDATE per account, not per leg
        for acct_id, delta in balance_deltas.items():
            if delta == ZERO:
                continue
            bal, _ = SaccoAccountBalance.objects.select_for_update().get_or_create(
                sacco_account_id=acct_id,
                defaults={'balance': ZERO},
            )
            bal.balance = (bal.balance or ZERO) + delta
            bal.save(update_fields=['balance'])

    return len(ledger_rows)


# ── Convenience: assert the trial balance is in balance ─────────────────────

def assert_trial_balance(end_date=None) -> Decimal:
    """
    For tests and management commands. Returns the absolute difference
    between total debits and total credits across the whole ledger
    (optionally as of `end_date`). Should be 0.00 in a healthy system.
    """
    from django.db.models import Sum
    from accounting.models import SaccoAccountsLedger

    qs = SaccoAccountsLedger.objects.all()
    if end_date is not None:
        qs = qs.filter(date__lte=end_date)
    agg = qs.aggregate(
        dr=Sum("debit_amount"),
        cr=Sum("credit_amount"),
    )
    dr = agg["dr"] or ZERO
    cr = agg["cr"] or ZERO
    return (dr - cr).copy_abs()
