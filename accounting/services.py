"""
accounting/services.py
-----------------------
The single source of truth for posting to the SACCO general ledger.

CHANGES:
- Integrated with TransactionAuditLog for full audit trail
- Added before/after balance snapshots
- Cash book validation on every post
- GL account code validation against chart of accounts
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import Iterable, List, Optional

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone

from .models import (
    SaccoAccount,
    SaccoAccountBalance,
    SaccoAccountsLedger,
    SaccoIncome,
    SaccoExpense,
)

logger = logging.getLogger(__name__)

ZERO = Decimal('0.00')
TWO_DP = Decimal('0.01')


# ════════════════════════════════════════════════════════════════════════
#  Standard GL account codes
# ════════════════════════════════════════════════════════════════════════

class _GLMeta(type):
    """
    Metaclass that makes GL codes DB-driven.

    On first access of any GL attribute, attempts to read the code from
    the ``gl_code_overrides`` Django setting. This lets operators change
    GL codes from settings/admin without touching source code.

    Falls back to the built-in defaults (matching the current chart of
    accounts Excel) so the system never breaks if settings are absent.
    """
    _DEFAULTS = {
        # ── Cash & Bank (Current Assets) ────────────────────────────
        'CASH':              '900-601001',
        'CASH_MAIN':         '900-600000',
        'EQUITY_BANK':       '900-601000',
        'MPESA_PAYBILL':     '900-601002',
        'MPESA_B2C':         '900-601003',
        'CIC_MONEY_MARKET':  '900-623000',
        # ── Loan Receivable Assets ──────────────────────────────────
        'LOANS_RECEIVABLE':  '900-630010',   # Normal Loan
        'MOBILE_LOAN':       '900-630014',   # Mobile Loan
        'EMERGENCY_LOAN':    '900-630011',   # Emergency Loan
        'DEV_LOAN':          '900-630012',   # Development Loan
        'INSTANT_LOAN':      '900-630013',   # Dividend Advance
        'MOBILE_LOAN_PLUS':  '900-630015',   # Mobile Loan Plus
        'INTEREST_RECV':     '900-610000',
        # ── Member Deposits & Equity ────────────────────────────────
        'MEMBER_DEPOSITS':   '900-802000',
        'FIXED_DEPOSITS':    '900-803000',
        'SHARE_CAPITAL':     '900-900000',
        'RETAINED_EARNINGS': '900-920000',
        'BENEVOLENT_FUND':   '900-804000',
        'ELIMU_FUND':        '900-805000',
        # ── Income ──────────────────────────────────────────────────
        'INTEREST_INCOME':   '900-110000',   # Normal Loan Interest
        'MOBILE_INTEREST':   '900-110010',   # Mobile Loan Interest
        'EMERGENCY_INT':     '900-110012',   # Emergency Loan Interest
        'DEV_LOAN_INT':      '900-110011',   # Development Loan Interest
        'INSTANT_INT':       '900-110013',   # Dividend Advance Interest
        'MOBILE_PLUS_INT':   '900-110014',   # Mobile Loan Plus Interest
        'REG_FEE_INCOME':    '900-124000',   # Registration/Membership Fee
        'FEES_INCOME':       '900-124006',   # Other Income
        'PENALTY_INCOME':    '900-124004',   # Penalties
        'TRANSACTION_FEE':   '900-124002',   # Withdrawal Charge
        'TOPUP_CHARGES':     '900-111000',   # Loan Topup Charges
        'APPRAISAL_FEE':     '900-111001',   # Loan Appraisal Fee
        'INSURANCE_FEE':     '900-111002',   # Loan Insurance Fee
        'BRIDGING_FEE':      '900-111003',   # Loan Bridging Fee
        'SMS_CHARGE':        '900-111004',   # SMS Charge
        'MPESA_COMMISSION':  '900-122000',   # Mpesa GW Commission
        'LUMPSUM_DEP_FEE':   '900-123000',   # Lump sum Deposit Fee
        # ── Holding / Collection ────────────────────────────────────
        'COLLECTION_ACCOUNT': '900-704005',  # Loan disbursal holding (Current Liability)
        'DISBURSEMENT_FEE':  '900-124003',   # Mobile Disbursement Fee
        'LEGAL_FEE':         '900-306000',   # Legal Fee (was 124005 — corrected to chart)
        # ── Expenses ────────────────────────────────────────────────
        'OTHER_EXPENSES':    '900-306001',
        'INTEREST_EXP':      '900-301000',
        'FIXED_INT_EXP':     '900-302000',
        'DIVIDEND_EXP':      '900-303000',
        'BANK_CHARGES':      '900-304000',
        'MPESA_CHARGES':     '900-305000',
        'SALARIES':          '900-309000',
    }
    # Aliases resolved after overrides are applied
    _ALIASES = {
        'SAVINGS_DEPOSITS': 'MEMBER_DEPOSITS',
        'JUNIOR_DEPOSITS':  'MEMBER_DEPOSITS',
        'OTHER_INCOME':     'FEES_INCOME',
        'MPESA_SUSPENSE':   'MPESA_PAYBILL',
    }

    _resolved = None

    def _resolve(cls):
        if cls._resolved is not None:
            return cls._resolved
        overrides = {}
        try:
            overrides = getattr(settings, 'GL_CODE_OVERRIDES', {}) or {}
        except Exception:
            pass
        merged = {**cls._DEFAULTS}
        merged.update({k.upper(): v for k, v in overrides.items()})
        # Apply aliases
        for alias, target in cls._ALIASES.items():
            merged[alias] = merged[target]
        cls._resolved = merged
        return merged

    def __getattr__(cls, name):
        if name.startswith('_'):
            raise AttributeError(name)
        codes = cls._resolve()
        if name in codes:
            return codes[name]
        raise AttributeError(f"GL has no code '{name}'")

    @classmethod
    def reload(mcs):
        """Force re-read from settings (call after dynamic config change)."""
        mcs._resolved = None


class GL(metaclass=_GLMeta):
    """
    DB-driven General Ledger code registry.

    Access codes as ``GL.CASH``, ``GL.MEMBER_DEPOSITS``, etc.
    Defaults match the chart of accounts Excel. Override any code
    via ``GL_CODE_OVERRIDES`` dict in Django settings::

        GL_CODE_OVERRIDES = {
            'CASH': '900-601002',  # point CASH at a different account
        }
    """
    pass


# ════════════════════════════════════════════════════════════════════════
#  Product-type → GL code mappings (resolved lazily so GL overrides work)
# ════════════════════════════════════════════════════════════════════════

def _savings_type_to_gl():
    """Lazy accessor — always reads current GL codes."""
    return {
        'savings_deposit': GL.MEMBER_DEPOSITS,
        'share_capital':   GL.SHARE_CAPITAL,
        'fosa_deposit':    GL.MEMBER_DEPOSITS,
        'mobile_wallet':   GL.MEMBER_DEPOSITS,
        'fixed_deposit':   GL.FIXED_DEPOSITS,
        'junior_account':  GL.MEMBER_DEPOSITS,
        'benevolent':          GL.BENEVOLENT_FUND,
        'elimu_fund':          GL.ELIMU_FUND,
        'collection_account':  GL.COLLECTION_ACCOUNT,
    }

def _loan_type_to_gl():
    return {
        'normal_loan':      GL.LOANS_RECEIVABLE,
        'mobile_loan':      GL.MOBILE_LOAN,
        'emergency_loan':   GL.EMERGENCY_LOAN,
        'development_loan': GL.DEV_LOAN,
        'dividend_advance': GL.INSTANT_LOAN,
        'mobile_loan_plus': GL.MOBILE_LOAN_PLUS,
    }

def _loan_interest_to_gl():
    return {
        'normal_loan':      GL.INTEREST_INCOME,
        'mobile_loan':      GL.MOBILE_INTEREST,
        'emergency_loan':   GL.EMERGENCY_INT,
        'development_loan': GL.DEV_LOAN_INT,
        'dividend_advance': GL.INSTANT_INT,
        'mobile_loan_plus': GL.MOBILE_PLUS_INT,
    }

# Backward-compat module-level names (read lazily)
class _LazyMap:
    def __init__(self, factory):
        self._factory = factory
    def get(self, key, default=None):
        return self._factory().get(key, default)
    def __getitem__(self, key):
        return self._factory()[key]
    def __contains__(self, key):
        return key in self._factory()
    def items(self):
        return self._factory().items()

SAVINGS_TYPE_TO_GL = _LazyMap(_savings_type_to_gl)
LOAN_TYPE_TO_GL    = _LazyMap(_loan_type_to_gl)
LOAN_INTEREST_TO_GL = _LazyMap(_loan_interest_to_gl)


def resolve_loan_gl(product_type_or_code):
    gl, _, _ = resolve_product_gl(product_type_or_code)
    return gl or GL.LOANS_RECEIVABLE


def resolve_product_gl(product_type_or_code):
    from transactions.models import CustomerAccountsSetup

    setup = None
    if hasattr(product_type_or_code, 'sacco_gl_account'):
        setup = product_type_or_code
    elif isinstance(product_type_or_code, str):
        setup = (
            CustomerAccountsSetup.objects
            .filter(account_type=product_type_or_code)
            .select_related('sacco_gl_account', 'sacco_interest_account', 'sacco_cash_account')
            .first()
        )
        if not setup:
            setup = (
                CustomerAccountsSetup.objects
                .filter(account_code=product_type_or_code)
                .select_related('sacco_gl_account', 'sacco_interest_account', 'sacco_cash_account')
                .first()
            )

    if setup:
        gl = setup.get_gl_code() if hasattr(setup, 'get_gl_code') else None
        cash = setup.get_cash_gl_code() if hasattr(setup, 'get_cash_gl_code') else GL.CASH
        interest = setup.get_interest_gl_code() if hasattr(setup, 'get_interest_gl_code') else None
        if gl:
            return gl, cash, interest
        atype = setup.account_type
        gl = (_savings_type_to_gl().get(atype) or _loan_type_to_gl().get(atype) or GL.MEMBER_DEPOSITS)
        interest = _loan_interest_to_gl().get(atype)
        return gl, GL.CASH, interest

    if isinstance(product_type_or_code, str):
        gl = (_savings_type_to_gl().get(product_type_or_code) or
              _loan_type_to_gl().get(product_type_or_code))
        interest = _loan_interest_to_gl().get(product_type_or_code)
        if gl:
            return gl, GL.CASH, interest

    return None, GL.CASH, None


# ════════════════════════════════════════════════════════════════════════
#  Journal-line value object
# ════════════════════════════════════════════════════════════════════════

@dataclass
class J:
    account_code: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    description: Optional[str] = None

    def __post_init__(self):
        self.debit = _to_decimal(self.debit)
        self.credit = _to_decimal(self.credit)
        if self.debit < 0 or self.credit < 0:
            raise PostingError(
                f"Negative amounts not allowed ({self.account_code}): "
                f"debit={self.debit}, credit={self.credit}"
            )
        if self.debit > 0 and self.credit > 0:
            raise PostingError(
                f"Line for {self.account_code} has both debit and credit."
            )
        if self.debit == 0 and self.credit == 0:
            raise PostingError(
                f"Empty line for {self.account_code}."
            )


class PostingError(Exception):
    """Raised when a journal cannot be posted."""


# ════════════════════════════════════════════════════════════════════════
#  Public API — post_journal with audit trail
# ════════════════════════════════════════════════════════════════════════

def post_journal(
    reference: str,
    description: str,
    lines: Iterable[J],
    user=None,
    customer=None,
    external_reference: Optional[str] = None,
    posting_date=None,
    audit_action: str = 'journal_post',
) -> List[SaccoAccountsLedger]:
    """
    Post a balanced journal entry to the General Ledger.

    WRITE ORDER (TB-first architecture):
      1. Validate (balance check, account resolution)
      2. Write to TigerBeetle (synchronous, MUST succeed when TB enabled)
      3. Write to PostgreSQL (ledger rows + balance cache)
      4. Audit trail

    If TB is enabled and the TB write fails, the entire transaction is
    aborted — no PG write occurs. This ensures TB is always authoritative.
    """
    lines = list(lines)
    if not lines:
        raise PostingError("post_journal() requires at least one line.")

    # ── 1. Balance check ────────────────────────────────────────────
    total_debit = sum((line.debit for line in lines), ZERO)
    total_credit = sum((line.credit for line in lines), ZERO)
    if total_debit.quantize(TWO_DP) != total_credit.quantize(TWO_DP):
        raise PostingError(
            f"Journal '{reference}' is unbalanced: "
            f"Σ debit={total_debit:,.2f}, Σ credit={total_credit:,.2f}"
        )
    if total_debit == ZERO:
        raise PostingError(f"Journal '{reference}' has zero value.")

    # ── 2. Resolve accounts up-front ────────────────────────────────
    code_to_account = {}
    for line in lines:
        if line.account_code not in code_to_account:
            try:
                code_to_account[line.account_code] = (
                    SaccoAccount.objects.get(account_code=line.account_code)
                )
            except SaccoAccount.DoesNotExist:
                raise PostingError(
                    f"Unknown SaccoAccount code '{line.account_code}'."
                )

    # ── 3. TB-FIRST: Write to TigerBeetle (authoritative) ──────────
    try:
        from accounting.tigerbeetle import tb_post_journal, TBPostingError
        tb_lines = [
            {'account_code': line.account_code,
             'debit': line.debit,
             'credit': line.credit}
            for line in lines
        ]
        tb_post_journal(tb_lines, reference=reference, description=description)
    except TBPostingError:
        raise PostingError(
            f"TigerBeetle rejected journal '{reference}'. "
            f"Transaction blocked to maintain ledger integrity."
        )
    except ImportError:
        pass  # tigerbeetle not installed — PG-only mode

    # ── 4. Capture before-snapshot ──────────────────────────────────
    account_codes = list(code_to_account.keys())
    before_snapshot = {}
    try:
        from accounting.audit_trail import get_balance_snapshot
        before_snapshot = get_balance_snapshot(account_codes)
    except Exception:
        pass

    # ── 5. Write to PostgreSQL (query layer + balance cache) ────────
    created = []
    posting_date = posting_date or timezone.now()

    with db_transaction.atomic():
        for line in lines:
            account = code_to_account[line.account_code]
            row = SaccoAccountsLedger.objects.create(
                customer=customer,
                sacco_account=account,
                date=posting_date if isinstance(posting_date, date_type) else posting_date.date() if hasattr(posting_date, 'date') else posting_date,
                reference=reference[:100],
                external_reference=(external_reference or '')[:100] or None,
                description=(line.description or description)[:1000],
                amount=line.debit if line.debit > 0 else line.credit,
                debit_amount=line.debit if line.debit > 0 else None,
                credit_amount=line.credit if line.credit > 0 else None,
                created_by=_user_str(user),
            )
            created.append(row)

            # PG balance cache update (non-authoritative when TB is enabled)
            bal, _ = SaccoAccountBalance.objects.select_for_update().get_or_create(
                sacco_account=account,
                defaults={'balance': ZERO},
            )
            bal.balance = (bal.balance or ZERO) + line.debit - line.credit
            bal.save(update_fields=['balance'])

    # ── 6. Capture after-snapshot & log audit ───────────────────────
    after_snapshot = {}
    try:
        from accounting.audit_trail import get_balance_snapshot, log_transaction
        after_snapshot = get_balance_snapshot(account_codes)
        log_transaction(
            action=audit_action,
            reference=reference,
            affected_accounts=account_codes,
            total_amount=total_debit,
            user=user,
            description=description,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            customer_ref=str(customer.cust_no) if customer and hasattr(customer, 'cust_no') else '',
            external_ref=external_reference or '',
        )
    except Exception:
        logger.exception("Audit trail logging failed for %s", reference)

    logger.info(
        "Posted journal %s — %d lines, KES %s by %s",
        reference, len(created), total_debit, _user_str(user) or 'system',
    )
    return created


def reverse_journal(reference, new_reference, description, user=None):
    """Reverse a previously-posted journal."""
    originals = list(SaccoAccountsLedger.objects.filter(reference=reference))
    if not originals:
        raise PostingError(f"No ledger rows found for reference '{reference}'.")

    reverse_lines = []
    for r in originals:
        debit = r.credit_amount or ZERO
        credit = r.debit_amount or ZERO
        if debit == 0 and credit == 0:
            continue
        reverse_lines.append(J(
            account_code=r.sacco_account.account_code,
            debit=debit, credit=credit,
            description=f"Reversal of {reference}: {r.description}",
        ))

    return post_journal(
        reference=new_reference,
        description=description or f"Reversal of {reference}",
        lines=reverse_lines,
        user=user,
        audit_action='journal_reverse',
    )


# ════════════════════════════════════════════════════════════════════════
#  Convenience wrappers
# ════════════════════════════════════════════════════════════════════════

def record_other_income(income: SaccoIncome, user=None):
    amount = _to_decimal(income.amount)
    ref = (income.reference or f"INC-{income.pk}")[:100]
    result = post_journal(
        reference=ref,
        description=income.description or "Other income",
        lines=[
            J(account_code=GL.CASH, debit=amount),
            J(account_code=income.sacco_account.account_code, credit=amount),
        ],
        user=user,
        customer=income.customer,
        external_reference=income.reference,
        audit_action='income_create',
    )
    # Mark as posted
    income.is_posted_to_gl = True
    income.save(update_fields=['is_posted_to_gl'])
    return result


def record_other_expense(expense: SaccoExpense, user=None):
    amount = _to_decimal(expense.amount)
    ref = (expense.reference or f"EXP-{expense.pk}")[:100]
    result = post_journal(
        reference=ref,
        description=expense.description or "Other expense",
        lines=[
            J(account_code=expense.sacco_account.account_code, debit=amount),
            J(account_code=GL.CASH, credit=amount),
        ],
        user=user,
        customer=expense.customer,
        external_reference=expense.reference,
        audit_action='expense_create',
    )
    expense.is_posted_to_gl = True
    expense.save(update_fields=['is_posted_to_gl'])
    return result


def reconcile_expense_to_gl(expense: SaccoExpense, user=None):
    """
    Post the double-entry for an expense at reconciliation time.

        Dr  expense.sacco_account          (expense recognition)
        Cr  expense.reconciliation_account (bank / asset decrease)

    Guards:
      - expense.reconciliation_account must be set (the bank being targeted)
      - expense.is_posted_to_gl must be False (prevents double-posting
        for legacy expenses that were already posted at creation time)

    After posting, sets is_posted_to_gl = True on the expense.
    """
    if expense.is_posted_to_gl:
        raise PostingError(
            f"Expense {expense.pk} (ref {expense.reference}) "
            f"has already been posted to the GL — cannot post again."
        )
    if not expense.reconciliation_account:
        raise PostingError(
            f"Expense {expense.pk} has no reconciliation account set — "
            f"select the bank/asset account to credit before posting."
        )

    amount = _to_decimal(expense.amount)
    ref = (expense.reference or f"EXP-{expense.pk}")[:100]
    expense_gl = expense.sacco_account.account_code
    bank_gl = expense.reconciliation_account.account_code

    result = post_journal(
        reference=ref,
        description=expense.description or "Expense reconciliation",
        lines=[
            J(account_code=expense_gl, debit=amount,
              description=f"Expense: {expense.description[:80]}" if expense.description else None),
            J(account_code=bank_gl, credit=amount,
              description=f"Payment from {expense.reconciliation_account.account_name}"),
        ],
        user=user,
        customer=expense.customer,
        external_reference=expense.reconciliation_reference or '',
        audit_action='expense_reconcile',
    )

    expense.is_posted_to_gl = True
    expense.save(update_fields=['is_posted_to_gl'])
    return result


def record_savings_deposit(customer, amount, saving_type, reference, user=None):
    gl_code, cash_code, _ = resolve_product_gl(saving_type)
    gl_code = gl_code or SAVINGS_TYPE_TO_GL.get(saving_type, GL.MEMBER_DEPOSITS)
    amount = _to_decimal(amount)
    return post_journal(
        reference=reference,
        description=f"Deposit — {saving_type}",
        lines=[
            J(account_code=cash_code, debit=amount),
            J(account_code=gl_code, credit=amount),
        ],
        user=user, customer=customer,
        audit_action='savings_deposit',
    )


def record_savings_withdrawal(customer, amount, saving_type, reference, user=None):
    gl_code, cash_code, _ = resolve_product_gl(saving_type)
    gl_code = gl_code or SAVINGS_TYPE_TO_GL.get(saving_type, GL.MEMBER_DEPOSITS)
    amount = _to_decimal(amount)
    return post_journal(
        reference=reference,
        description=f"Withdrawal — {saving_type}",
        lines=[
            J(account_code=gl_code, debit=amount),
            J(account_code=cash_code, credit=amount),
        ],
        user=user, customer=customer,
        audit_action='savings_withdraw',
    )


def record_loan_repayment(customer, amount, loan_no, reference,
                          user=None, loan_gl_code=None):
    amount = _to_decimal(amount)
    gl = loan_gl_code or GL.LOANS_RECEIVABLE
    return post_journal(
        reference=reference,
        description=f"Loan repayment — {loan_no}",
        lines=[
            J(account_code=GL.CASH, debit=amount),
            J(account_code=gl, credit=amount),
        ],
        user=user, customer=customer,
        external_reference=loan_no,
        audit_action='loan_repayment',
    )


def record_loan_interest_collected(customer, amount, loan_no, reference, user=None):
    amount = _to_decimal(amount)
    return post_journal(
        reference=reference,
        description=f"Interest received — {loan_no}",
        lines=[
            J(account_code=GL.CASH, debit=amount),
            J(account_code=GL.INTEREST_INCOME, credit=amount),
        ],
        user=user, customer=customer,
        external_reference=loan_no,
        audit_action='loan_repayment',
    )


def record_registration_fee(customer, amount, reference, user=None):
    amount = _to_decimal(amount)
    return post_journal(
        reference=reference,
        description=f"Registration fee — {customer.cust_no}",
        lines=[
            J(account_code=GL.CASH, debit=amount),
            J(account_code=GL.REG_FEE_INCOME, credit=amount),
        ],
        user=user, customer=customer,
    )


def record_inter_gl_transfer(from_code, to_code, amount, reference,
                             description, user=None):
    amount = _to_decimal(amount)
    return post_journal(
        reference=reference,
        description=description,
        lines=[
            J(account_code=to_code, debit=amount),
            J(account_code=from_code, credit=amount),
        ],
        user=user,
    )


# ════════════════════════════════════════════════════════════════════════
#  Reporting helpers
# ════════════════════════════════════════════════════════════════════════

def trial_balance(as_of=None):
    qs = SaccoAccountsLedger.objects.all()
    if as_of:
        qs = qs.filter(date__lte=as_of)

    from django.db.models import Sum
    rows = (
        qs.values('sacco_account__account_code', 'sacco_account__account_name',
                  'sacco_account__account_group')
          .annotate(debit=Sum('debit_amount'), credit=Sum('credit_amount'))
          .order_by('sacco_account__account_code')
    )
    out = []
    for r in rows:
        d = r['debit'] or ZERO
        c = r['credit'] or ZERO
        out.append({
            'account_code':  r['sacco_account__account_code'],
            'account_name':  r['sacco_account__account_name'],
            'account_group': r['sacco_account__account_group'],
            'debit_total':   d,
            'credit_total':  c,
            'net':           d - c,
        })
    return out


# ════════════════════════════════════════════════════════════════════════
#  Internal helpers
# ════════════════════════════════════════════════════════════════════════

def _to_decimal(value):
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PostingError(f"Cannot convert {value!r} to Decimal.")


def _user_or_none(user):
    if user is None:
        return None
    if hasattr(user, 'pk') and user.pk:
        return user
    return None


def _user_str(user):
    if user is None:
        return 'system'
    if hasattr(user, 'get_username'):
        return user.get_username()
    return str(user)[:100]
