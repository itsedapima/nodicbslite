"""
transactions/jobs.py
=====================
Production-grade background M-Pesa notification processor for NODi CBS Lite.

Called by django-q2:
    from transactions.jobs import post_mpesa_notifications
    post_mpesa_notifications()

DESIGN GUARANTEES
-----------------
1. ACCURATE BALANCES
   Outstanding loan balance = SUM(debit_amount) - SUM(credit_amount)
   (repayment = credit, disbursement/charge = debit -- per confirmed schema).
   Savings balance       = SUM(credit_amount) - SUM(debit_amount).
   Every SMS balance is recomputed live from LoanTransaction /
   SavingsTransaction AFTER the new row is written, inside the same atomic
   block, so the figure quoted is always the post-transaction truth.

2. NO FALSE "LOAN DOES NOT EXIST"
   The source of truth for "does this member have a loan" is LoanHistory
   (is_disbursed=True and still carrying a balance) -- NOT RunningLoanStat.
   RunningLoanStat is only consulted as an *optional* hint for ordering /
   status; a missing or stale stat row can never hide a real disbursed loan.
   If genuinely no loan with an outstanding balance exists, the payment is
   routed to savings as an overflow instead of being rejected.

3. CORRECT account_type AUDIT VALUE
   For loan postings we store the real loan number (LNxxxxxx / MOBIxxxxxxxx),
   never a synthetic "DVL-xxx" / "ML-xxx" string. For savings we store the
   account code + member number.

4. RESILIENCE
   - Each notification is its own atomic transaction; one failure never rolls
     back a successful neighbour.
   - select_for_update(skip_locked=True) makes the job safe to run on
     multiple django-q2 workers concurrently.
   - Idempotent: a notification already in PostedMpesaNotification is skipped.
   - Connection is re-established defensively before error persistence so a
     dropped PgBouncer connection cannot swallow the audit trail.

Customer numbers (cust_no) are ALWAYS strings -- never cast to int.
"""

import re
import logging
import traceback
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Optional, Tuple, Dict, List

from django.db import transaction as db_tx, connection
from django.db.models import Sum, F, Value
from django.db.models.functions import Coalesce
from django.utils.timezone import now

from customers.models import Customer
from loans.models import LoanHistory, RunningLoanStat
from sms.models import SMSLog
from .models import (
    MpesaNotification,
    PostedMpesaNotification,
    SavingsTransaction,
    LoanTransaction,
    CustomerAccountsSetup,
)

# ════════════════════════════════════════════════════════════════════════
#  CONSTANTS & CONFIG
# ════════════════════════════════════════════════════════════════════════

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
BATCH_SIZE = 200          # Max notifications processed per run
SMS_BATCH_SIZE = 100      # Bulk-insert chunk size for SMS queue

# Order suffixes longest-first so DEVL/FOSA/MLP match before DL/FD/ML etc.
# (regex alternation is greedy left-to-right, so order matters).
_SUFFIXES_BY_LEN = sorted(
    ["DEP", "FOSA", "BF", "MS", "FD", "SD", "CA", "JA", "SC",
     "MLP", "DL", "DEVL", "NL", "EL", "ML"],
    key=len,
    reverse=True,
)
BILL_REF_PATTERN = re.compile(
    r"^(\d+)(" + "|".join(_SUFFIXES_BY_LEN) + r")$"
)

# Mapping suffixes to internal account types
SAVING_TYPE_MAP = {
    "SC": "share_capital",
    "MS": "savings_deposit",
    "FD": "fixed_deposit",
    "JA": "junior_account",
    "CA": "collection_account",
    "DEP": "fosa_deposit",
    "SD": "savings_deposit",
    "BF": "benevolent_fund",
    "FOSA": "fosa_deposit",
    "WD": "welfare_deposit",
}

LOAN_TYPE_MAP = {
    "DEVL": "development_loan",
    "NL": "normal_loan",
    "DL": "development_loan",
    "EL": "emergency_loan",
    "ML": "mobile_loan",
    "MLP": "mobile_loan_plus",
}

VALID_SUFFIXES = frozenset(SAVING_TYPE_MAP) | frozenset(LOAN_TYPE_MAP)

# Loan statuses that mean "this loan is closed -- do not pay into it".
CLOSED_LOAN_STATUSES = frozenset({
    "settled", "closed", "written off", "writtenoff",
})


# ════════════════════════════════════════════════════════════════════════
#  CUSTOM EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════

class AccountNotFoundError(ValueError):
    """Reference pattern doesn't match any member or account type."""
    pass


class InvalidAmountError(ValueError):
    """Transaction amount is invalid (non-positive or non-numeric)."""
    pass


# ════════════════════════════════════════════════════════════════════════
#  TELEMETRY
# ════════════════════════════════════════════════════════════════════════

_TELEMETRY_TABLE_READY = False


def _ensure_telemetry_table():
    """Create telemetry table on first call only. Idempotent."""
    global _TELEMETRY_TABLE_READY
    if _TELEMETRY_TABLE_READY:
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS transactions_mpesa_processor_run (
                    id SERIAL PRIMARY KEY,
                    started_at TIMESTAMPTZ DEFAULT NOW(),
                    ended_at TIMESTAMPTZ,
                    batch_size INTEGER,
                    processed INTEGER DEFAULT 0,
                    skipped INTEGER DEFAULT 0,
                    errored INTEGER DEFAULT 0,
                    error_summary TEXT,
                    duration_seconds NUMERIC(10, 3)
                );
                CREATE INDEX IF NOT EXISTS idx_mpesa_run_started
                ON transactions_mpesa_processor_run(started_at DESC);
            """)
        _TELEMETRY_TABLE_READY = True
    except Exception as e:
        logger.warning("Could not create telemetry table: %s", e)


def _log_run_start() -> Optional[int]:
    _ensure_telemetry_table()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO transactions_mpesa_processor_run
                (started_at, batch_size) VALUES (NOW(), %s)
                RETURNING id;
            """, [BATCH_SIZE])
            return cursor.fetchone()[0]
    except Exception as e:
        logger.warning("Could not log run start: %s", e)
        return None


def _log_run_end(run_id, processed, skipped, errored, error_summary=""):
    if not run_id:
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE transactions_mpesa_processor_run
                SET ended_at = NOW(),
                    processed = %s,
                    skipped = %s,
                    errored = %s,
                    error_summary = %s,
                    duration_seconds = EXTRACT(EPOCH FROM (NOW() - started_at))
                WHERE id = %s;
            """, [processed, skipped, errored, error_summary[:1000], run_id])
    except Exception as e:
        logger.warning("Could not log run end: %s", e)


# ════════════════════════════════════════════════════════════════════════
#  CACHED LOOKUPS
# ════════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=128)
def _account_code(account_type: str) -> str:
    """Return account_code for a savings type. Cached -- one DB hit per type."""
    setup = (
        CustomerAccountsSetup.objects
        .filter(account_type=account_type)
        .only("account_code")
        .first()
    )
    return setup.account_code if setup else "S00"


def _clear_caches():
    """Call if CustomerAccountsSetup changes during a long-running process."""
    _account_code.cache_clear()


# ════════════════════════════════════════════════════════════════════════
#  BALANCE HELPERS (single query, NULL-safe, sign-correct)
# ════════════════════════════════════════════════════════════════════════

def _savings_balance(cust_no: str, saving_type: str) -> Decimal:
    """Live savings balance = credits - debits. NULL-safe single aggregation."""
    result = (
        SavingsTransaction.objects
        .filter(cust_no=cust_no, saving_type=saving_type)
        .aggregate(
            total=Coalesce(
                Sum(
                    Coalesce(F("credit_amount"), Value(ZERO))
                    - Coalesce(F("debit_amount"), Value(ZERO))
                ),
                Value(ZERO),
            )
        )
    )
    return result["total"] or ZERO


def _loan_balance(loan_id: int) -> Decimal:
    """
    Live loan outstanding = debits - credits.
    (disbursement/charge = debit  ->  increases balance
     repayment           = credit ->  decreases balance)
    """
    result = (
        LoanTransaction.objects
        .filter(loan_id=loan_id)
        .aggregate(
            total=Coalesce(
                Sum(
                    Coalesce(F("debit_amount"), Value(ZERO))
                    - Coalesce(F("credit_amount"), Value(ZERO))
                ),
                Value(ZERO),
            )
        )
    )
    return result["total"] or ZERO


def _bulk_loan_balances(loan_ids: List[int]) -> Dict[int, Decimal]:
    """Fetch outstanding balances for MANY loans in a SINGLE query."""
    if not loan_ids:
        return {}

    results = (
        LoanTransaction.objects
        .filter(loan_id__in=loan_ids)
        .values("loan_id")
        .annotate(
            balance=Coalesce(
                Sum(
                    Coalesce(F("debit_amount"), Value(ZERO))
                    - Coalesce(F("credit_amount"), Value(ZERO))
                ),
                Value(ZERO),
            )
        )
    )

    balances = {r["loan_id"]: (r["balance"] or ZERO) for r in results}
    for lid in loan_ids:
        balances.setdefault(lid, ZERO)
    return balances


# ════════════════════════════════════════════════════════════════════════
#  LOAN SELECTION  (LoanHistory is the source of truth)
# ════════════════════════════════════════════════════════════════════════

def _loan_account_types_for(loan_type_str: str) -> List[str]:
    """
    Resolve the loan_type_str (e.g. 'development_loan') to the set of
    CustomerAccountsSetup account_type values that represent it.
    Returns the account_type strings used to match LoanHistory.loan_type.
    """
    return list(
        CustomerAccountsSetup.objects
        .filter(account_type=loan_type_str, is_loan_account=True)
        .values_list("account_type", flat=True)
    )


def _settled_loan_nos(cust_no: str) -> frozenset:
    """
    Loan numbers that RunningLoanStat marks as closed/settled for this member.
    Used only to EXCLUDE -- never to gate existence. A missing stat row leaves
    the loan eligible (correct: stat tables go stale; LoanHistory does not).
    """
    rows = (
        RunningLoanStat.objects
        .filter(cust_no=cust_no)
        .values_list("loan_no", "loan_status")
    )
    return frozenset(
        ln for (ln, status) in rows
        if ln and (status or "").strip().lower() in CLOSED_LOAN_STATUSES
    )


def _get_earliest_uncleared_loan(
    cust_no: str, loan_type_str: str
) -> Tuple[Optional[LoanHistory], Decimal]:
    """
    Find the earliest DISBURSED loan of the given type for this member that
    still carries an outstanding balance.

    Source of truth = LoanHistory (is_disbursed=True). RunningLoanStat is used
    only to skip loans explicitly marked settled/closed, so a stale or missing
    stat row can never produce a false "loan does not exist".

    Returns (LoanHistory instance, outstanding_balance) or (None, ZERO).
    """
    account_types = _loan_account_types_for(loan_type_str)
    if not account_types:
        # The product itself isn't configured -- this is a real config gap.
        logger.warning(
            "[LOAN] No CustomerAccountsSetup loan product for type '%s'",
            loan_type_str,
        )
        return None, ZERO

    # Candidate disbursed loans for this member & product, oldest first.
    # LoanHistory.customer is FK to Customer with to_field='cust_no',
    # so customer_id stores the cust_no string directly.
    candidates = list(
        LoanHistory.objects
        .filter(
            customer_id=cust_no,
            loan_type__account_type__in=account_types,
            is_disbursed=True,
        )
        .order_by("loan_date", "id")
        .values("id", "loan_no", "loan_date")
    )

    if not candidates:
        return None, ZERO

    settled = _settled_loan_nos(cust_no)
    candidates = [c for c in candidates if c["loan_no"] not in settled]
    if not candidates:
        return None, ZERO

    # One bulk query for all candidate balances.
    balances = _bulk_loan_balances([c["id"] for c in candidates])

    # Earliest loan still carrying a positive balance wins.
    for loan_dict in candidates:
        bal = balances.get(loan_dict["id"], ZERO)
        if bal > ZERO:
            loan_obj = LoanHistory.objects.get(id=loan_dict["id"])
            return loan_obj, bal

    return None, ZERO


# ════════════════════════════════════════════════════════════════════════
#  ERROR PERSISTENCE
# ════════════════════════════════════════════════════════════════════════

def _save_error(notif_id: int, error_msg: str):
    """Persist error to notification row. Resilient to connection drops."""
    try:
        connection.ensure_connection()
        with db_tx.atomic():
            MpesaNotification.objects.filter(id=notif_id).update(
                last_error=error_msg[:2000],
            )
    except Exception:
        logger.exception(
            "CRITICAL: Could not persist last_error for notif id=%s. "
            "Original error: %s", notif_id, error_msg,
        )


# ════════════════════════════════════════════════════════════════════════
#  SMS QUEUEING (buffered, bulk-inserted OUTSIDE atomic blocks)
# ════════════════════════════════════════════════════════════════════════

def _queue_sms(buffer: List[SMSLog], phone: str, message: str,
               created_by: str = "mpesa_job"):
    """Buffer SMS for bulk insert later. Non-blocking."""
    if not (phone and message):
        return
    try:
        buffer.append(SMSLog(
            phone=phone,
            message=message,
            status="pending",
            created_by=created_by,
        ))
    except Exception:
        logger.exception("Failed to queue SMS to %s", phone)


def _flush_sms_queue(buffer: List[SMSLog]):
    """Bulk insert all queued SMS in chunks. Called OUTSIDE atomic blocks."""
    if not buffer:
        return
    try:
        SMSLog.objects.bulk_create(buffer, batch_size=SMS_BATCH_SIZE)
        logger.info("Flushed %d SMS to queue", len(buffer))
    except Exception:
        logger.exception("Failed to bulk-insert %d SMS", len(buffer))


# ════════════════════════════════════════════════════════════════════════
#  MAIN PROCESSOR
# ════════════════════════════════════════════════════════════════════════

def post_mpesa_notifications():
    """
    Process pending M-Pesa notifications in batches.

    Concurrency-safe (select_for_update with skip_locked), idempotent
    (PostedMpesaNotification guard), bounded (BATCH_SIZE limit).

    Returns: number processed (int).
    """
    run_id = _log_run_start()
    logger.info("=" * 70)
    logger.info("post_mpesa_notifications() STARTED  [run_id=%s]", run_id)
    logger.info("=" * 70)

    try:
        notif_ids = list(
            MpesaNotification.objects
            .filter(posted=False)
            .order_by("id")
            .values_list("id", flat=True)[:BATCH_SIZE]
        )
    except Exception as e:
        logger.exception("FATAL: Could not fetch pending notifications")
        _log_run_end(run_id, 0, 0, 1, f"Could not fetch pending: {e}")
        return 0

    if not notif_ids:
        logger.info("post_mpesa_notifications: 0 pending -- nothing to do.")
        _log_run_end(run_id, 0, 0, 0, "No pending notifications")
        return 0

    logger.info(
        "Picked up %d pending notification(s) [ids %s..%s]",
        len(notif_ids), notif_ids[0], notif_ids[-1],
    )

    processed = skipped = errored = 0
    errors_list: List[str] = []
    sms_buffer: List[SMSLog] = []

    for notif_id in notif_ids:
        try:
            with db_tx.atomic():
                # Lock this row -- skip if another worker already has it
                notif = (
                    MpesaNotification.objects
                    .select_for_update(skip_locked=True)
                    .filter(id=notif_id, posted=False)
                    .first()
                )

                if notif is None:
                    skipped += 1
                    continue

                # Idempotency: already audited -> just mark posted.
                if PostedMpesaNotification.objects.filter(
                    mpesa_notification=notif
                ).exists():
                    notif.posted = True
                    notif.last_error = None
                    notif.save(update_fields=["posted", "last_error"])
                    skipped += 1
                    continue

                _process_one(notif, sms_buffer)
                processed += 1

        except (AccountNotFoundError, InvalidAmountError) as exc:
            errored += 1
            error_msg = str(exc)
            logger.warning("Validation error for notif id=%s: %s",
                           notif_id, error_msg)
            errors_list.append(f"id={notif_id}: {error_msg}")
            _save_error(notif_id, error_msg)

        except Exception as exc:
            errored += 1
            error_msg = f"System Error: {exc}\n{traceback.format_exc()}"
            logger.exception("Unexpected failure for notif id=%s", notif_id)
            errors_list.append(f"id={notif_id}: {exc}")
            _save_error(notif_id, error_msg)

    # SMS are flushed OUTSIDE the per-notification transactions so a DB hiccup
    # on send never rolls back a correctly posted payment.
    _flush_sms_queue(sms_buffer)

    summary_msg = "; ".join(errors_list[:5])
    _log_run_end(run_id, processed, skipped, errored, summary_msg)

    logger.info("=" * 70)
    logger.info("post_mpesa_notifications() COMPLETE  [run_id=%s]", run_id)
    logger.info("  processed=%d  skipped=%d  errored=%d  total=%d",
                processed, skipped, errored, len(notif_ids))
    logger.info("=" * 70)

    return processed


# ════════════════════════════════════════════════════════════════════════
#  PROCESS ONE NOTIFICATION
# ════════════════════════════════════════════════════════════════════════

def _process_one(notif: MpesaNotification, sms_buffer: List[SMSLog]):
    """Process a single notification inside an open atomic block."""
    bill_ref = (notif.bill_ref_number or "").strip().upper()

    if not bill_ref:
        raise AccountNotFoundError(
            f"Account Not Found: bill_ref_number is empty "
            f"(trans_id={notif.trans_id})"
        )

    match = BILL_REF_PATTERN.match(bill_ref)
    if not match:
        raise AccountNotFoundError(
            f"Account Not Found: Reference '{bill_ref}' does not match "
            f"pattern CUSTOMER_NO + SUFFIX (trans_id={notif.trans_id})"
        )

    # cust_no stays as STRING -- never cast to int (preserves leading zeros)
    cust_no, suffix = match.groups()

    customer = (
        Customer.objects
        .filter(cust_no=cust_no)
        .only("cust_no", "first_name", "last_name", "full_name", "phone")
        .first()
    )
    if not customer:
        raise AccountNotFoundError(
            f"Account Not Found: Member '{cust_no}' does not exist "
            f"(trans_id={notif.trans_id})"
        )

    try:
        trans_amount = Decimal(str(notif.trans_amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidAmountError(
            f"Invalid amount '{notif.trans_amount}' "
            f"(trans_id={notif.trans_id}): {exc}"
        )

    if trans_amount <= ZERO:
        raise InvalidAmountError(
            f"Non-positive amount {trans_amount} (trans_id={notif.trans_id})"
        )

    ref = notif.trans_id

    # ─── Route to savings or loan; capture the value to audit. ───
    if suffix in SAVING_TYPE_MAP:
        sms_message, audit_account = _post_savings(
            customer, cust_no, trans_amount, ref, suffix
        )
    elif suffix in LOAN_TYPE_MAP:
        sms_message, audit_account = _post_loan(
            notif, customer, cust_no, trans_amount, ref, suffix
        )
    else:
        raise AccountNotFoundError(
            f"Account Not Found: Suffix '{suffix}' is not mapped "
            f"(trans_id={notif.trans_id})"
        )

    # ─── Audit trail: store the REAL account paid into. ───
    #   loans  -> loan_no  (LNxxxxxx / MOBIxxxxxxxx)
    #   savings-> <account_code>-<cust_no>
    PostedMpesaNotification.objects.create(
        mpesa_notification=notif,
        customer_no=cust_no,
        account_type=audit_account,
    )

    notif.posted = True
    notif.last_error = None
    notif.save(update_fields=["posted", "last_error"])

    # Buffer SMS -- will be flushed OUTSIDE the atomic block
    phone = getattr(customer, "phone", None)
    if sms_message and phone:
        _queue_sms(sms_buffer, phone, sms_message, "mpesa_job")

    logger.info(
        "OK notif id=%s trans_id=%s member=%s suffix=%s amount=%s account=%s",
        notif.id, ref, cust_no, suffix, trans_amount, audit_account,
    )


# ════════════════════════════════════════════════════════════════════════
#  SAVINGS DEPOSIT
# ════════════════════════════════════════════════════════════════════════

def _post_savings(
    customer, cust_no: str, amount: Decimal, ref, suffix
) -> Tuple[str, str]:
    """Post a direct savings deposit. Returns (sms_message, audit_account)."""
    account_type = SAVING_TYPE_MAP[suffix]
    acc_code = _account_code(account_type)
    display = account_type.replace("_", " ").title()

    SavingsTransaction.objects.create(
        cust_no=cust_no,
        saving_type=account_type,
        tr_date=now(),
        tr_ref=ref,
        tr_desc=f"M-Pesa Deposit {ref}",
        credit_amount=amount,
        debit_amount=ZERO,
        created_by="mpesa_job",
    )

    balance = _savings_balance(cust_no, account_type)
    full_name = _display_name(customer)
    audit_account = f"{acc_code}-{cust_no}"

    sms = (
        f"Dear {full_name}, we have received KES {amount:,.2f} "
        f"via M-Pesa Ref: {ref} for your {display} account "
        f"({audit_account}). New balance: KES {balance:,.2f}."
    )
    return sms, audit_account


# ════════════════════════════════════════════════════════════════════════
#  LOAN REPAYMENT (with overflow to savings)
# ════════════════════════════════════════════════════════════════════════

def _post_loan(
    notif, customer, cust_no: str, amount: Decimal, ref, suffix
) -> Tuple[str, str]:
    """
    Post a loan repayment. Surplus beyond outstanding overflows to savings.
    If no loan with a balance exists, the whole amount goes to savings.
    Returns (sms_message, audit_account).
    """
    loan_type = LOAN_TYPE_MAP[suffix]
    target_loan, loan_balance = _get_earliest_uncleared_loan(cust_no, loan_type)

    full_name = _display_name(customer)
    fallback_type = "savings_deposit"

    # ─── No active loan -> route the whole payment to savings. ───
    if not target_loan:
        SavingsTransaction.objects.create(
            cust_no=cust_no,
            saving_type=fallback_type,
            tr_date=now(),
            tr_ref=ref,
            tr_desc=f"M-Pesa {suffix} payment -- no active loan, to savings {ref}",
            credit_amount=amount,
            debit_amount=ZERO,
            created_by="mpesa_job",
        )
        sav_bal = _savings_balance(cust_no, fallback_type)
        acc_code = _account_code(fallback_type)
        audit_account = f"{acc_code}-{cust_no}"
        logger.info(
            "No active %s for member %s -- KES %s routed to savings (notif id=%s)",
            loan_type, cust_no, amount, notif.id,
        )
        sms = (
            f"Dear {full_name}, we have received KES {amount:,.2f} "
            f"via M-Pesa Ref: {ref}. No active loan was found, so the amount "
            f"was deposited to your savings (balance: KES {sav_bal:,.2f})."
        )
        return sms, audit_account

    loan_payment = min(amount, loan_balance)
    overflow = amount - loan_payment
    sms_parts: List[str] = []

    # ─── 1. Apply to loan (repayment = credit). ───
    if loan_payment > ZERO:
        LoanTransaction.objects.create(
            cust_no=cust_no,
            loan_id=target_loan.id,
            loan_no=target_loan.loan_no,
            loan_type=loan_type,
            tr_date=now(),
            tr_ref=ref,
            tr_desc=f"M-Pesa Loan Repayment {ref}",
            credit_amount=loan_payment,
            debit_amount=ZERO,
            created_by="mpesa_job",
        )
        new_loan_bal = _loan_balance(target_loan.id)
        sms_parts.append(
            f"KES {loan_payment:,.2f} paid to loan {target_loan.loan_no} "
            f"(outstanding: KES {new_loan_bal:,.2f})"
        )

    # ─── 2. Overflow -> savings. ───
    if overflow > ZERO:
        SavingsTransaction.objects.create(
            cust_no=cust_no,
            saving_type=fallback_type,
            tr_date=now(),
            tr_ref=ref,
            tr_desc=f"Loan overpayment overflow {ref}",
            credit_amount=overflow,
            debit_amount=ZERO,
            created_by="mpesa_job",
        )
        sav_bal = _savings_balance(cust_no, fallback_type)
        sms_parts.append(
            f"KES {overflow:,.2f} overpayment deposited to savings "
            f"(balance: KES {sav_bal:,.2f})"
        )

    body = " and ".join(sms_parts)
    sms = (
        f"Dear {full_name}, we have received KES {amount:,.2f} "
        f"via M-Pesa Ref: {ref}. {body}."
    )
    # Audit the actual loan number, never a synthetic DVL-/ML- string.
    return sms, target_loan.loan_no


# ════════════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════════════

def _display_name(customer) -> str:
    """Build display name from Customer, preferring first+last over full_name."""
    parts = [
        getattr(customer, "first_name", None),
        getattr(customer, "last_name", None),
    ]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    return getattr(customer, "full_name", None) or f"Member {customer.cust_no}"
