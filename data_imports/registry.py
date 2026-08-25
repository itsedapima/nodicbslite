"""
data_imports/registry.py  — PERFORMANCE-OPTIMISED
==================================================
All DB lookups are pre-loaded into dicts/sets BEFORE the validation loop
(build_context) → validators do O(1) dict lookups, zero DB hits.

Committers use bulk_create() in configurable batches (default 500) and
build GL ledger rows in-memory then bulk_create in one shot — no per-row
atomic blocks.

Designed to import 10 000+ rows in < 10 seconds and never hit an nginx
gateway timeout.
"""
from __future__ import annotations

import logging
import uuid as _uuid_mod
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BULK_BATCH = 500          # bulk_create batch_size
ZERO = Decimal("0")

# ─── Column spec ────────────────────────────────────────────────────────

@dataclass
class Column:
    header: str
    required: bool = False
    help: str = ""
    example: str = ""


# ─── Import-type spec ───────────────────────────────────────────────────

@dataclass
class ImportType:
    slug: str
    title: str
    app_label: str
    icon: str
    description: str
    columns: List[Column]
    validate_row: Callable
    commit_rows: Callable
    build_context: Callable = None  # rows -> ctx dict (prefetch)
    options: List[Dict[str, Any]] = field(default_factory=list)


# ─── Parse helpers (pure functions, no DB) ──────────────────────────────

def _to_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _to_decimal(v, default=ZERO) -> Decimal:
    if v is None or v == "":
        return default
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"cannot parse '{v}' as a number")


def _to_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"cannot parse '{v}' as a date (use YYYY-MM-DD)")


def _to_datetime(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime.combine(v, datetime.min.time())
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"cannot parse '{v}' as a datetime")


def _pad_cust_no(v) -> str:
    s = _to_str(v)
    if not s:
        return ""
    if s.isdigit():
        return s.zfill(5) if len(s) <= 5 else s
    return s


def _split_full_name(full, first, middle, last):
    if not full and (first or middle or last):
        full = " ".join(p for p in [first, middle, last] if p).strip()
    if full and not (first or middle or last):
        parts = full.split()
        if len(parts) == 1:
            first = parts[0]
        elif len(parts) == 2:
            first, last = parts[0], parts[1]
        else:
            first, middle, last = parts[0], " ".join(parts[1:-1]), parts[-1]
    return first, middle, last


# ═══════════════════════════════════════════════════════════════════════
#  1. CUSTOMERS
# ═══════════════════════════════════════════════════════════════════════

CUSTOMER_COLUMNS = [
    Column("cust_no", required=True,  help="Member number. Digits only. Will be zero-padded to 5.",
           example="00116"),
    Column("full_name", required=True, help="Full legal name (used if first/middle/last are blank).",
           example="JOHN DOE OMONDI"),
    Column("first_name", help="Optional; derived from full_name if blank.", example="JOHN"),
    Column("middle_name", help="Optional; derived from full_name if blank.", example="DOE"),
    Column("last_name",  help="Optional; derived from full_name if blank.", example="OMONDI"),
    Column("dob",        help="Date of birth (YYYY-MM-DD).", example="1985-03-14"),
    Column("national_id", required=True, help="National ID number. Must be unique.",
           example="12345678"),
    Column("phone",      help="Mobile number. Must be unique. Placeholder generated if blank.",
           example="254712345678"),
    Column("reg_date",   help="Registration date (YYYY-MM-DD). Defaults to today.",
           example="2020-01-15"),
    Column("reg_email",  help="Registration email (optional).", example="john@example.com"),
    Column("customer_status", help="active | dormant | exited | deceased | suspended",
           example="active"),
    Column("branch",     help="Branch name or branch code (optional).", example="HQ"),
]

_VALID_STATUS = {"active", "dormant", "exited", "deceased", "suspended"}


def _ctx_customers(rows):
    """Pre-load branches and existing cust_nos in 2 queries."""
    from administration.models import CompanyBranch
    from customers.models import Customer

    branches_by_code = {}
    branches_by_name = {}
    for b in CompanyBranch.objects.all():
        branches_by_code[b.branch_code.upper()] = b
        branches_by_name[b.name.upper()] = b

    existing = set(Customer.objects.values_list("cust_no", flat=True))

    return {
        "branches_by_code": branches_by_code,
        "branches_by_name": branches_by_name,
        "existing_cust_nos": existing,
    }


def _validate_customer(row, ctx):
    errors = []
    cust_no = _pad_cust_no(row.get("cust_no"))
    if not cust_no:
        errors.append("cust_no is required")

    full_name = _to_str(row.get("full_name"))
    first = _to_str(row.get("first_name"))
    middle = _to_str(row.get("middle_name"))
    last = _to_str(row.get("last_name"))
    if not full_name and not (first or last):
        errors.append("full_name is required (or provide first + last)")
    first, middle, last = _split_full_name(full_name, first, middle, last)
    if not full_name:
        full_name = " ".join(p for p in [first, middle, last] if p).strip()

    national_id = _to_str(row.get("national_id"))
    if not national_id:
        national_id = f"TMPID{cust_no or 'X'}"

    phone = _to_str(row.get("phone"))
    if not phone:
        phone = f"TMPPHONE{cust_no or 'X'}"

    try:
        dob = _to_date(row.get("dob"))
    except ValueError as e:
        errors.append(f"dob: {e}")
        dob = None

    try:
        reg_date = _to_date(row.get("reg_date"))
    except ValueError as e:
        errors.append(f"reg_date: {e}")
        reg_date = None

    status = _to_str(row.get("customer_status")).lower() or "active"
    if status not in _VALID_STATUS:
        errors.append(f"customer_status '{status}' invalid; use one of {sorted(_VALID_STATUS)}")

    # Branch lookup from pre-loaded dict — 0 DB hits
    branch_ref = _to_str(row.get("branch"))
    branch = None
    if branch_ref:
        branch = (ctx.get("branches_by_code", {}).get(branch_ref.upper())
                  or ctx.get("branches_by_name", {}).get(branch_ref.upper()))
        if branch is None:
            errors.append(f"branch '{branch_ref}' not found")

    cleaned = dict(
        cust_no=cust_no,
        full_name=full_name.upper() if full_name else "",
        first_name=first.upper() if first else None,
        middle_name=middle.upper() if middle else None,
        last_name=last.upper() if last else None,
        dob=dob,
        national_id=national_id,
        phone=phone,
        reg_date=reg_date,
        reg_email=_to_str(row.get("reg_email")) or None,
        customer_status=status,
        branch=branch,
    )
    return cleaned, errors


def _commit_customers(rows, opts, user):
    from customers.models import Customer
    from django.db import connection

    overwrite = bool(opts.get("overwrite_existing"))
    username = getattr(user, "username", "system")

    # Pre-load existing in one query
    existing_map = {}
    for c in Customer.objects.filter(
        cust_no__in=[r["cust_no"] for r in rows]
    ).only("pk", "cust_no"):
        existing_map[c.cust_no] = c.pk

    to_create = []
    to_update = []
    skipped = 0
    problems = []
    create_reg_dates = {}   # cust_no → date for post-bulk UPDATE

    for i, r in enumerate(rows, start=2):
        try:
            reg_date = r.pop("reg_date")
            branch = r.pop("branch")
            pk = existing_map.get(r["cust_no"])

            if pk and not overwrite:
                skipped += 1
                continue
            if pk and overwrite:
                to_update.append((pk, r, branch, reg_date))
            else:
                obj = Customer(
                    cust_no=r["cust_no"],
                    full_name=r["full_name"],
                    first_name=r.get("first_name"),
                    middle_name=r.get("middle_name"),
                    last_name=r.get("last_name"),
                    dob=r.get("dob"),
                    national_id=r["national_id"],
                    phone=r["phone"],
                    reg_email=r.get("reg_email"),
                    customer_status=r.get("customer_status", "active"),
                    branch=branch,
                    created_by=username,
                )
                to_create.append(obj)
                if reg_date:
                    create_reg_dates[r["cust_no"]] = reg_date
        except Exception as e:
            problems.append(f"Row {i} ({r.get('cust_no')}): {e}")

    # ── Bulk create: bypass Customer.save() since we already padded cust_no
    created = 0
    if to_create:
        Customer.objects.bulk_create(to_create, batch_size=BULK_BATCH,
                                     ignore_conflicts=False)
        created = len(to_create)
        # Fix reg_date for created rows (auto_now_add makes it today)
        if create_reg_dates:
            # Batch raw UPDATE for reg_date overrides
            cases = " ".join(
                f"WHEN cust_no='{cn}' THEN '{d.isoformat()}'::date"
                for cn, d in create_reg_dates.items()
            )
            cust_ids = ",".join(f"'{cn}'" for cn in create_reg_dates)
            with connection.cursor() as cur:
                cur.execute(
                    f"UPDATE customers_customer SET reg_date = CASE {cases} END "
                    f"WHERE cust_no IN ({cust_ids})"
                )

    # ── Bulk update existing
    updated = 0
    if to_update:
        update_objs = []
        update_reg = {}
        for pk, r, branch, reg_date in to_update:
            obj = Customer(pk=pk, **r, branch=branch, updated_by=username)
            update_objs.append(obj)
            if reg_date:
                update_reg[r["cust_no"]] = reg_date

        Customer.objects.bulk_update(
            update_objs,
            fields=["full_name", "first_name", "middle_name", "last_name",
                    "dob", "national_id", "phone", "reg_email",
                    "customer_status", "branch", "updated_by"],
            batch_size=BULK_BATCH,
        )
        updated = len(update_objs)

        if update_reg:
            cases = " ".join(
                f"WHEN cust_no='{cn}' THEN '{d.isoformat()}'::date"
                for cn, d in update_reg.items()
            )
            cust_ids = ",".join(f"'{cn}'" for cn in update_reg)
            with connection.cursor() as cur:
                cur.execute(
                    f"UPDATE customers_customer SET reg_date = CASE {cases} END "
                    f"WHERE cust_no IN ({cust_ids})"
                )

    return dict(created=created, updated=updated, skipped=skipped, problems=problems)


# ═══════════════════════════════════════════════════════════════════════
#  2. NEXT OF KIN
# ═══════════════════════════════════════════════════════════════════════

NEXT_OF_KIN_COLUMNS = [
    Column("cust_no",         required=True, help="Existing member number.", example="00116"),
    Column("kin_name",        required=True, help="Full name of next of kin.", example="MARY DOE"),
    Column("kin_relationship", help="Relationship (e.g. Spouse, Child).", example="Spouse"),
    Column("kin_phone",       help="Kin phone (optional).", example="254722000111"),
    Column("kin_national_id", help="Kin National ID (optional).", example="87654321"),
]


def _ctx_nok(rows):
    from customers.models import Customer, NextOfKin
    # Collect cust_nos from the batch
    batch_cnos = {_pad_cust_no(r.get("cust_no")) for r in rows if r.get("cust_no")}
    cust_map = {}
    for c in Customer.objects.filter(cust_no__in=batch_cnos).only("pk", "cust_no"):
        cust_map[c.cust_no] = c

    # Pre-load existing kin names to skip duplicates during validation
    existing_kins = set()
    for nk in NextOfKin.objects.filter(
        customer__cust_no__in=batch_cnos
    ).values_list("customer__cust_no", "kin_name"):
        existing_kins.add((nk[0], nk[1].upper()))

    return {"cust_map": cust_map, "existing_kins": existing_kins}


def _validate_nok(row, ctx):
    errors = []
    cust_no = _pad_cust_no(row.get("cust_no"))
    if not cust_no:
        errors.append("cust_no is required")
    kin_name = _to_str(row.get("kin_name"))
    if not kin_name:
        errors.append("kin_name is required")

    customer = ctx.get("cust_map", {}).get(cust_no)
    if cust_no and not customer:
        errors.append(f"customer {cust_no} not found")

    cleaned = dict(
        customer=customer,
        kin_name=kin_name,
        kin_relationship=_to_str(row.get("kin_relationship")) or "Unspecified",
        kin_phone=_to_str(row.get("kin_phone")) or None,
        kin_national_id=_to_str(row.get("kin_national_id")) or None,
    )
    return cleaned, errors


def _commit_nok(rows, opts, user):
    from customers.models import NextOfKin

    # Pre-load existing in ONE query
    cust_ids = {r["customer"].pk for r in rows if r.get("customer")}
    existing = set()
    for nk in NextOfKin.objects.filter(customer_id__in=cust_ids).values_list(
        "customer_id", "kin_name"
    ):
        existing.add((nk[0], nk[1].upper()))

    to_create = []
    skipped = 0
    for r in rows:
        if not r.get("customer"):
            continue
        key = (r["customer"].pk, r["kin_name"].upper())
        if key in existing:
            skipped += 1
            continue
        existing.add(key)  # prevent dupes within same batch
        to_create.append(NextOfKin(**r))

    if to_create:
        NextOfKin.objects.bulk_create(to_create, batch_size=BULK_BATCH)

    return dict(created=len(to_create), updated=0, skipped=skipped, problems=[])


# ═══════════════════════════════════════════════════════════════════════
#  3. SAVINGS TRANSACTIONS
# ═══════════════════════════════════════════════════════════════════════

SAVINGS_TXN_COLUMNS = [
    Column("cust_no",       required=True, help="Existing member number.", example="00116"),
    Column("saving_type",   required=True, help="Product account_type from Customer Accounts Setup.",
           example="savings_deposit"),
    Column("tr_date",       required=True, help="Transaction date (YYYY-MM-DD).",
           example="2024-06-15"),
    Column("tr_ref",        required=True, help="Unique reference (auto-generated if blank).",
           example="TX0001"),
    Column("ext_ref",       help="External reference (bank, mpesa etc).", example="MPESA-QAB1234"),
    Column("tr_desc",       help="Description.", example="Opening deposit"),
    Column("debit_amount",  help="Debit (withdrawal).", example="0"),
    Column("credit_amount", help="Credit (deposit).",  example="5000"),
]


def _ctx_savings_txn(rows):
    from customers.models import Customer
    from transactions.models import CustomerAccountsSetup

    batch_cnos = {_pad_cust_no(r.get("cust_no")) for r in rows if r.get("cust_no")}
    cust_set = set(Customer.objects.filter(cust_no__in=batch_cnos).values_list("cust_no", flat=True))

    setup_map = {}
    for s in CustomerAccountsSetup.objects.select_related(
        "sacco_gl_account", "sacco_cash_account"
    ).filter(is_active=True):
        setup_map[s.account_type] = s

    return {"cust_set": cust_set, "setup_map": setup_map}


def _validate_savings_txn(row, ctx):
    errors = []
    cust_no = _pad_cust_no(row.get("cust_no"))
    saving_type = _to_str(row.get("saving_type"))

    if not cust_no:
        errors.append("cust_no is required")
    if not saving_type:
        errors.append("saving_type is required")

    if cust_no and cust_no not in ctx.get("cust_set", set()):
        errors.append(f"customer {cust_no} not found")

    setup = ctx.get("setup_map", {}).get(saving_type)
    if saving_type and not setup:
        errors.append(f"saving_type '{saving_type}' not in CustomerAccountsSetup")
    if setup and setup.is_loan_account:
        errors.append(f"'{saving_type}' is a loan product, not a savings product")

    try:
        tr_date = _to_datetime(row.get("tr_date"))
    except ValueError as e:
        errors.append(f"tr_date: {e}")
        tr_date = None
    if not tr_date:
        errors.append("tr_date is required")

    try:
        dr = _to_decimal(row.get("debit_amount"))
        cr = _to_decimal(row.get("credit_amount"))
    except ValueError as e:
        errors.append(str(e))
        dr, cr = ZERO, ZERO

    if dr <= 0 and cr <= 0:
        errors.append("either debit_amount or credit_amount must be > 0")
    if dr > 0 and cr > 0:
        errors.append("only one of debit_amount / credit_amount should be > 0")

    tr_ref = _to_str(row.get("tr_ref"))
    if not tr_ref:
        tr_ref = f"IMP-{_uuid_mod.uuid4().hex[:10].upper()}"

    cleaned = dict(
        cust_no=cust_no,
        saving_type=saving_type,
        account_code=setup.get_gl_code() if setup else None,
        tr_date=tr_date,
        tr_ref=tr_ref,
        ext_ref=_to_str(row.get("ext_ref")) or None,
        tr_desc=_to_str(row.get("tr_desc")) or "Imported savings transaction",
        debit_amount=dr,
        credit_amount=cr,
        _setup_key=saving_type,   # lightweight key — not the ORM object
    )
    return cleaned, errors


def _commit_savings_txn(rows, opts, user):
    from transactions.models import SavingsTransaction, CustomerAccountsSetup
    from accounting.models import SaccoAccount, SaccoAccountsLedger

    post_gl = bool(opts.get("post_gl"))
    username = getattr(user, "username", "system")

    # Pre-load setups & cash account in 2 queries
    setup_map = {}
    for s in CustomerAccountsSetup.objects.select_related(
        "sacco_gl_account", "sacco_cash_account"
    ).filter(is_active=True):
        setup_map[s.account_type] = s

    cash_acc = SaccoAccount.objects.filter(account_code="900-601001").first()

    txn_objs = []
    gl_objs = []

    for r in rows:
        setup = setup_map.get(r.pop("_setup_key", ""))
        txn_objs.append(SavingsTransaction(
            cust_no=r["cust_no"], saving_type=r["saving_type"],
            account_code=r.get("account_code"), tr_date=r["tr_date"],
            tr_ref=r["tr_ref"], ext_ref=r.get("ext_ref"),
            tr_desc=r["tr_desc"], debit_amount=r["debit_amount"],
            credit_amount=r["credit_amount"], created_by=username,
        ))

        if post_gl and setup and setup.sacco_gl_account_id:
            gl = setup.sacco_gl_account
            cash = setup.sacco_cash_account or cash_acc
            if not cash:
                continue
            desc = f"[IMPORT] {r['tr_desc']}"
            ref = r["tr_ref"]
            ext = r.get("ext_ref")
            if r["credit_amount"] > 0:
                amt = r["credit_amount"]
                gl_objs.append(SaccoAccountsLedger(
                    sacco_account=cash, reference=ref, external_reference=ext,
                    description=desc, amount=amt,
                    debit_amount=amt, credit_amount=ZERO, created_by=username))
                gl_objs.append(SaccoAccountsLedger(
                    sacco_account=gl, reference=ref, external_reference=ext,
                    description=desc, amount=amt,
                    debit_amount=ZERO, credit_amount=amt, created_by=username))
            else:
                amt = r["debit_amount"]
                gl_objs.append(SaccoAccountsLedger(
                    sacco_account=gl, reference=ref, external_reference=ext,
                    description=desc, amount=amt,
                    debit_amount=amt, credit_amount=ZERO, created_by=username))
                gl_objs.append(SaccoAccountsLedger(
                    sacco_account=cash, reference=ref, external_reference=ext,
                    description=desc, amount=amt,
                    debit_amount=ZERO, credit_amount=amt, created_by=username))

    SavingsTransaction.objects.bulk_create(txn_objs, batch_size=BULK_BATCH)
    if gl_objs:
        SaccoAccountsLedger.objects.bulk_create(gl_objs, batch_size=BULK_BATCH)

    return dict(created=len(txn_objs), updated=0, skipped=0, problems=[])


# ═══════════════════════════════════════════════════════════════════════
#  4. LOAN TRANSACTIONS
# ═══════════════════════════════════════════════════════════════════════

LOAN_TXN_COLUMNS = [
    Column("cust_no",       required=True, help="Existing member number.", example="00116"),
    Column("loan_no",       required=True, help="Existing loan number (from LoanHistory).",
           example="LN000123"),
    Column("tr_date",       required=True, help="Transaction date (YYYY-MM-DD).",
           example="2024-06-15"),
    Column("tr_ref",        required=True, help="Unique reference (auto-generated if blank).",
           example="LT0001"),
    Column("ext_ref",       help="External reference.", example=""),
    Column("tr_desc",       help="Description.", example="Loan repayment"),
    Column("debit_amount",  help="Debit (disbursement / interest charge).", example="0"),
    Column("credit_amount", help="Credit (repayment).",  example="1500"),
]


def _ctx_loan_txn(rows):
    from loans.models import LoanHistory

    batch_lnos = {_to_str(r.get("loan_no")) for r in rows if r.get("loan_no")}
    loan_map = {}
    for lh in LoanHistory.objects.select_related("loan_type").filter(loan_no__in=batch_lnos):
        loan_map[lh.loan_no] = lh

    return {"loan_map": loan_map}


def _validate_loan_txn(row, ctx):
    errors = []
    cust_no = _pad_cust_no(row.get("cust_no"))
    loan_no = _to_str(row.get("loan_no"))

    if not cust_no:
        errors.append("cust_no is required")
    if not loan_no:
        errors.append("loan_no is required")

    loan = ctx.get("loan_map", {}).get(loan_no)
    if loan_no and not loan:
        errors.append(f"loan '{loan_no}' not found")
    if loan and str(loan.customer_id) != cust_no:
        errors.append(f"loan '{loan_no}' does not belong to member {cust_no} (owner: {loan.customer_id})")

    try:
        tr_date = _to_datetime(row.get("tr_date"))
    except ValueError as e:
        errors.append(f"tr_date: {e}")
        tr_date = None
    if not tr_date:
        errors.append("tr_date is required")

    try:
        dr = _to_decimal(row.get("debit_amount"))
        cr = _to_decimal(row.get("credit_amount"))
    except ValueError as e:
        errors.append(str(e))
        dr, cr = ZERO, ZERO
    if dr <= 0 and cr <= 0:
        errors.append("either debit_amount or credit_amount must be > 0")
    if dr > 0 and cr > 0:
        errors.append("only one of debit_amount / credit_amount should be > 0")

    tr_ref = _to_str(row.get("tr_ref"))
    if not tr_ref:
        tr_ref = f"LIMP-{_uuid_mod.uuid4().hex[:10].upper()}"

    cleaned = dict(
        cust_no=cust_no,
        loan_id=loan.id if loan else 0,
        loan_no=loan_no,
        loan_type=(loan.loan_type.account_type if loan and loan.loan_type else "normal_loan"),
        account_code=(loan.loan_type.get_gl_code() if loan and loan.loan_type else None),
        tr_date=tr_date,
        tr_ref=tr_ref,
        ext_ref=_to_str(row.get("ext_ref")) or None,
        tr_desc=_to_str(row.get("tr_desc")) or "Imported loan transaction",
        debit_amount=dr,
        credit_amount=cr,
        _loan_no_key=loan_no,
    )
    return cleaned, errors


def _commit_loan_txn(rows, opts, user):
    from transactions.models import LoanTransaction
    from loans.models import LoanHistory
    from accounting.models import SaccoAccount, SaccoAccountsLedger

    post_gl = bool(opts.get("post_gl"))
    username = getattr(user, "username", "system")

    # Pre-load loan objects for GL account resolution (1 query)
    lnos = {r.get("_loan_no_key") or r.get("loan_no") for r in rows}
    loan_map = {}
    for lh in LoanHistory.objects.select_related(
        "loan_type", "loan_type__sacco_gl_account", "loan_type__sacco_cash_account"
    ).filter(loan_no__in=lnos):
        loan_map[lh.loan_no] = lh

    cash_acc = SaccoAccount.objects.filter(account_code="900-601001").first()

    txn_objs = []
    gl_objs = []

    for r in rows:
        r.pop("_loan_no_key", None)
        loan = loan_map.get(r["loan_no"])
        txn_objs.append(LoanTransaction(
            cust_no=r["cust_no"], loan_id=r["loan_id"], loan_no=r["loan_no"],
            loan_type=r["loan_type"], account_code=r.get("account_code"),
            tr_date=r["tr_date"], tr_ref=r["tr_ref"], ext_ref=r.get("ext_ref"),
            tr_desc=r["tr_desc"], debit_amount=r["debit_amount"],
            credit_amount=r["credit_amount"], created_by=username,
        ))

        if post_gl and loan and loan.loan_type and loan.loan_type.sacco_gl_account_id:
            gl = loan.loan_type.sacco_gl_account
            cash = loan.loan_type.sacco_cash_account or cash_acc
            if not cash:
                continue
            desc = f"[IMPORT] {r['tr_desc']}"
            ref = r["tr_ref"]
            ext = r.get("ext_ref")
            if r["debit_amount"] > 0:
                amt = r["debit_amount"]
                gl_objs.append(SaccoAccountsLedger(
                    sacco_account=gl, reference=ref, external_reference=ext,
                    description=desc, amount=amt,
                    debit_amount=amt, credit_amount=ZERO, created_by=username))
                gl_objs.append(SaccoAccountsLedger(
                    sacco_account=cash, reference=ref, external_reference=ext,
                    description=desc, amount=amt,
                    debit_amount=ZERO, credit_amount=amt, created_by=username))
            else:
                amt = r["credit_amount"]
                gl_objs.append(SaccoAccountsLedger(
                    sacco_account=cash, reference=ref, external_reference=ext,
                    description=desc, amount=amt,
                    debit_amount=amt, credit_amount=ZERO, created_by=username))
                gl_objs.append(SaccoAccountsLedger(
                    sacco_account=gl, reference=ref, external_reference=ext,
                    description=desc, amount=amt,
                    debit_amount=ZERO, credit_amount=amt, created_by=username))

    LoanTransaction.objects.bulk_create(txn_objs, batch_size=BULK_BATCH)
    if gl_objs:
        SaccoAccountsLedger.objects.bulk_create(gl_objs, batch_size=BULK_BATCH)

    return dict(created=len(txn_objs), updated=0, skipped=0, problems=[])


# ═══════════════════════════════════════════════════════════════════════
#  5. SACCO ACCOUNT BALANCES (opening balances)
# ═══════════════════════════════════════════════════════════════════════

SACCO_BALANCE_COLUMNS = [
    Column("account_code", required=True, help="Existing SaccoAccount code.", example="900-601001"),
    Column("account_name", help="Optional; for reference only. Real name comes from DB.",
           example="Coop Bank"),
    Column("debit_amount",  help="Debit balance (use this OR credit_amount, not both). "
           "If only 'balance' is provided, the system uses the account's normal side.",
           example="150000"),
    Column("credit_amount", help="Credit balance (use this OR debit_amount, not both).",
           example="0"),
    Column("balance",       help="Legacy fallback: positive number placed on the account's "
           "normal debit/credit side. Ignored if debit_amount or credit_amount is provided.",
           example="150000"),
    Column("as_at_date",    required=True,
           help="The cutoff date for this trial balance (YYYY-MM-DD). "
                "All rows MUST share the same date — a trial balance is a "
                "snapshot at one point in time. GL entries are posted with "
                "this date so the TB report aligns with your company books.",
           example="2025-12-31"),
]


def _ctx_sacco_balance(rows):
    from accounting.models import SaccoAccount
    acc_map = {}
    for a in SaccoAccount.objects.all():
        acc_map[a.account_code] = a
    return {"acc_map": acc_map}


def _validate_sacco_balance(row, ctx):
    errors = []
    code = _to_str(row.get("account_code"))
    if not code:
        errors.append("account_code is required")

    acc = ctx.get("acc_map", {}).get(code)
    if code and not acc:
        errors.append(f"SaccoAccount '{code}' not found")

    # ── Parse debit / credit / legacy balance ────────────────────────
    try:
        dr = _to_decimal(row.get("debit_amount"))
    except ValueError as e:
        errors.append(f"debit_amount: {e}")
        dr = ZERO
    try:
        cr = _to_decimal(row.get("credit_amount"))
    except ValueError as e:
        errors.append(f"credit_amount: {e}")
        cr = ZERO
    try:
        legacy_bal = _to_decimal(row.get("balance"))
    except ValueError as e:
        errors.append(f"balance: {e}")
        legacy_bal = ZERO

    # If user supplied explicit dr/cr, use them
    if dr > ZERO or cr > ZERO:
        if dr > ZERO and cr > ZERO:
            errors.append("Provide debit_amount OR credit_amount, not both on the same row")
    elif legacy_bal > ZERO and acc:
        # Fall back to the old behaviour: place on normal side
        if acc.normal_balance_side == "debit":
            dr = legacy_bal
        else:
            cr = legacy_bal
    elif legacy_bal > ZERO:
        dr = legacy_bal

    if dr == ZERO and cr == ZERO and not errors:
        errors.append("Row has no balance (debit_amount, credit_amount, or balance required)")

    if dr < ZERO:
        errors.append("debit_amount must be non-negative")
    if cr < ZERO:
        errors.append("credit_amount must be non-negative")

    # ── Parse as_at_date ─────────────────────────────────────────────
    try:
        as_at = _to_date(row.get("as_at_date"))
    except ValueError as e:
        errors.append(f"as_at_date: {e}")
        as_at = None
    if not as_at:
        errors.append("as_at_date is required (e.g. 2025-12-31)")

    cleaned = dict(
        account=acc,
        account_code=code,
        debit_amount=dr,
        credit_amount=cr,
        balance=dr if dr > ZERO else cr,
        as_at_date=as_at,
    )
    return cleaned, errors


def _commit_sacco_balances(rows, opts, user):
    """
    Industry-standard trial balance import.

    APPROACH (how SAP, Dynamics 365, QuickBooks, Tally all do it):
      1. Validate the entire uploaded TB balances: sum(DR) must equal
         sum(CR). If it doesn't, the import is rejected — the accountant
         must fix the source file. A TB that doesn't balance should never
         silently enter the ledger.
      2. Validate all rows share the same as_at_date (a single TB is for
         one cutoff date).
      3. Post all rows as ONE multi-leg journal entry with a single shared
         reference. Each account gets exactly ONE ledger row — no clearing
         account is needed because the TB is self-balancing.
      4. Update SaccoAccountBalance with each account's balance.

    This gives:
      • N ledger rows for N accounts (not 2N)
      • One atomic journal reference that an auditor can trace
      • Immediate detection of imbalanced TBs BEFORE they enter the system
      • Clean trial balance report "as at" the cutoff date

    FALLBACK MODE (allow_imbalance option):
      If the user explicitly opts in, an imbalanced TB is accepted and the
      difference is posted to a clearing/equity account. This is for partial
      imports or where the TB intentionally excludes accounts.
    """
    from accounting.models import SaccoAccountBalance, SaccoAccount, SaccoAccountsLedger
    from django.utils import timezone

    post_journal = bool(opts.get("post_journal_entries"))
    allow_imbalance = bool(opts.get("allow_imbalance"))
    username = getattr(user, "username", "system")
    problems = []

    # ═══════════════════════════════════════════════════════════════════
    #  STEP 1: Validate consistent as_at_date across all rows
    # ═══════════════════════════════════════════════════════════════════
    dates_seen = {r["as_at_date"] for r in rows if r.get("as_at_date")}
    if len(dates_seen) > 1:
        sorted_dates = sorted(str(d) for d in dates_seen)
        raise ValueError(
            f"All rows must share the same as_at_date for a valid trial balance. "
            f"Found {len(dates_seen)} different dates: {', '.join(sorted_dates)}. "
            f"A trial balance is a snapshot at ONE point in time."
        )
    as_at = dates_seen.pop() if dates_seen else None

    # ═══════════════════════════════════════════════════════════════════
    #  STEP 2: Validate trial balance equilibrium (sum DR == sum CR)
    # ═══════════════════════════════════════════════════════════════════
    total_dr = sum(r["debit_amount"] for r in rows)
    total_cr = sum(r["credit_amount"] for r in rows)
    tb_diff = total_dr - total_cr

    if post_journal and abs(tb_diff) > Decimal("0.01") and not allow_imbalance:
        raise ValueError(
            f"Trial balance does not balance! "
            f"Total Debits: KES {total_dr:,.2f}, Total Credits: KES {total_cr:,.2f}, "
            f"Difference: KES {tb_diff:,.2f}. "
            f"Fix your source file so debits equal credits, then re-upload. "
            f"If this is intentional (partial import), enable the "
            f"'Allow imbalance (post difference to equity)' option."
        )

    # ═══════════════════════════════════════════════════════════════════
    #  STEP 3: Update SaccoAccountBalance
    # ═══════════════════════════════════════════════════════════════════
    existing_bal = {}
    for sb in SaccoAccountBalance.objects.select_related("sacco_account").all():
        existing_bal[sb.sacco_account_id] = sb

    to_create_bal = []
    to_update_bal = []
    created = 0
    updated = 0

    for r in rows:
        acc = r["account"]
        bal = r["balance"]
        if not acc:
            continue

        sb = existing_bal.get(acc.pk)
        if sb:
            sb.balance = bal
            to_update_bal.append(sb)
            updated += 1
        else:
            new_sb = SaccoAccountBalance(sacco_account=acc, balance=bal)
            to_create_bal.append(new_sb)
            existing_bal[acc.pk] = new_sb
            created += 1

    if to_create_bal:
        SaccoAccountBalance.objects.bulk_create(to_create_bal, batch_size=BULK_BATCH)
    if to_update_bal:
        SaccoAccountBalance.objects.bulk_update(to_update_bal, ["balance"],
                                                batch_size=BULK_BATCH)

    # ═══════════════════════════════════════════════════════════════════
    #  STEP 4: Post single multi-leg journal entry to the GL
    # ═══════════════════════════════════════════════════════════════════
    gl_count = 0
    if post_journal and as_at:
        ts = timezone.now().strftime("%Y%m%d%H%M%S")
        ref = f"TB-OPEN-{as_at.isoformat()}-{ts}"
        desc_base = f"[TB-IMPORT] Trial balance opening entry as at {as_at}"

        gl_objs = []
        for r in rows:
            acc = r["account"]
            dr = r["debit_amount"]
            cr = r["credit_amount"]
            if not acc:
                continue
            # One ledger row per account — the natural side from the TB
            gl_objs.append(SaccoAccountsLedger(
                sacco_account=acc, date=as_at,
                reference=ref,
                description=f"{desc_base} | {acc.account_code}",
                amount=dr if dr > ZERO else cr,
                debit_amount=dr, credit_amount=cr,
                created_by=username,
            ))

        # If imbalance is allowed and present, post difference to equity
        if allow_imbalance and abs(tb_diff) > Decimal("0.01"):
            clearing = (
                SaccoAccount.objects.filter(
                    account_name__icontains="opening balance"
                ).first()
                or SaccoAccount.objects.filter(account_group="Equity").first()
            )
            if clearing:
                if tb_diff > ZERO:
                    # More debits than credits → credit the clearing account
                    gl_objs.append(SaccoAccountsLedger(
                        sacco_account=clearing, date=as_at,
                        reference=ref,
                        description=(
                            f"{desc_base} | TB imbalance adjustment "
                            f"(KES {abs(tb_diff):,.2f} to {clearing.account_code})"
                        ),
                        amount=abs(tb_diff),
                        debit_amount=ZERO, credit_amount=abs(tb_diff),
                        created_by=username,
                    ))
                else:
                    gl_objs.append(SaccoAccountsLedger(
                        sacco_account=clearing, date=as_at,
                        reference=ref,
                        description=(
                            f"{desc_base} | TB imbalance adjustment "
                            f"(KES {abs(tb_diff):,.2f} to {clearing.account_code})"
                        ),
                        amount=abs(tb_diff),
                        debit_amount=abs(tb_diff), credit_amount=ZERO,
                        created_by=username,
                    ))
                problems.append(
                    f"TB was out of balance by KES {tb_diff:,.2f}. "
                    f"Difference posted to {clearing.account_code} – "
                    f"{clearing.account_name}."
                )
            else:
                problems.append(
                    f"TB is out of balance by KES {tb_diff:,.2f} but no "
                    f"Opening Balance / Equity account found for the adjustment. "
                    f"GL entries posted WITHOUT the clearing leg — trial balance "
                    f"will show a difference until manually corrected."
                )

        if gl_objs:
            SaccoAccountsLedger.objects.bulk_create(gl_objs, batch_size=BULK_BATCH)
            gl_count = len(gl_objs)

    return dict(
        created=created, updated=updated, skipped=0, problems=problems,
        gl_entries_posted=gl_count,
        total_debits=str(total_dr),
        total_credits=str(total_cr),
        difference=str(tb_diff),
        as_at_date=str(as_at) if as_at else "",
    )


# ═══════════════════════════════════════════════════════════════════════
#  6. LOAN HISTORY
# ═══════════════════════════════════════════════════════════════════════

LOAN_HISTORY_COLUMNS = [
    Column("loan_no",       help="Loan number. If blank AND auto-generate is on, we assign one.",
           example="LN000123"),
    Column("cust_no",       required=True, help="Existing member number.", example="00116"),
    Column("loan_date",     required=True, help="Loan issue date (YYYY-MM-DD).", example="2024-05-01"),
    Column("loan_type",     required=True,
           help="account_type from CustomerAccountsSetup (must be a loan product).",
           example="normal_loan"),
    Column("principal",     required=True, help="Approved principal.", example="50000"),
    Column("installment",   required=True, help="Monthly installment.", example="5000"),
    Column("loan_period",   required=True, help="Repayment period in months.", example="12"),
    Column("interest_rate", help="Annual interest rate (default 12).", example="12"),
    Column("net_disbursed", help="Net amount paid to member (defaults to principal).",
           example="48000"),
    Column("is_disbursed",   help="TRUE if the loan was already disbursed.", example="TRUE"),
]


def _ctx_loan_history(rows):
    from customers.models import Customer
    from transactions.models import CustomerAccountsSetup
    from loans.models import LoanHistory

    batch_cnos = {_pad_cust_no(r.get("cust_no")) for r in rows if r.get("cust_no")}
    cust_map = {}
    for c in Customer.objects.filter(cust_no__in=batch_cnos).only("pk", "cust_no"):
        cust_map[c.cust_no] = c

    setup_map = {}
    for s in CustomerAccountsSetup.objects.filter(is_active=True):
        setup_map[s.account_type] = s

    existing_lnos = set(LoanHistory.objects.values_list("loan_no", flat=True))

    return {"cust_map": cust_map, "setup_map": setup_map,
            "existing_lnos": existing_lnos}


def _validate_loan_history(row, ctx):
    errors = []
    cust_no = _pad_cust_no(row.get("cust_no"))

    customer = ctx.get("cust_map", {}).get(cust_no)
    if cust_no and not customer:
        errors.append(f"customer {cust_no} not found")
    if not cust_no:
        errors.append("cust_no is required")

    loan_type_key = _to_str(row.get("loan_type"))
    setup = ctx.get("setup_map", {}).get(loan_type_key)
    if loan_type_key and not setup:
        errors.append(f"loan_type '{loan_type_key}' not in CustomerAccountsSetup")
    if setup and not setup.is_loan_account:
        errors.append(f"'{loan_type_key}' is a savings product, not a loan product")

    try:
        loan_date = _to_date(row.get("loan_date"))
    except ValueError as e:
        errors.append(f"loan_date: {e}")
        loan_date = None
    if not loan_date:
        errors.append("loan_date is required")

    try:
        principal    = _to_decimal(row.get("principal"))
        installment  = _to_decimal(row.get("installment"))
        interest     = _to_decimal(row.get("interest_rate"), default=Decimal("12"))
        net          = _to_decimal(row.get("net_disbursed"), default=principal)
    except ValueError as e:
        errors.append(str(e))
        principal = installment = interest = net = ZERO

    if principal <= 0:
        errors.append("principal must be > 0")
    if installment <= 0:
        errors.append("installment must be > 0")

    try:
        loan_period = int(_to_str(row.get("loan_period")) or 0)
    except ValueError:
        errors.append("loan_period must be an integer")
        loan_period = 0
    if loan_period <= 0:
        errors.append("loan_period must be > 0")

    loan_no = _to_str(row.get("loan_no"))
    is_disbursed_raw = _to_str(row.get("is_disbursed")).lower()
    is_disbursed = is_disbursed_raw in ("true", "1", "yes", "y")

    cleaned = dict(
        loan_no=loan_no,
        customer=customer,
        loan_date=loan_date,
        loan_type=setup,
        principal=principal,
        installment=installment,
        loan_period=loan_period,
        interest_rate=interest,
        net_disbursed=net or principal,
        is_disbursed=is_disbursed,
    )
    return cleaned, errors


def _commit_loan_history(rows, opts, user):
    from loans.models import LoanHistory
    from django.utils import timezone
    from django.db.models import Max

    auto_gen = bool(opts.get("autogen_loan_no"))
    force_override = bool(opts.get("force_autogen"))
    username = getattr(user, "username", "system")
    now = timezone.now()

    # Pre-load existing loan_nos to skip dupes (1 query)
    existing_lnos = set(LoanHistory.objects.values_list("loan_no", flat=True))

    # For auto-generation: reserve a block of sequence numbers from Postgres.
    # We count how many rows need auto-gen first, then call nextval() once
    # per row — sequences are atomic, no race conditions.
    from django.db import connection as _conn

    # Separate into bulk-create (provided loan_no) and sequential (auto-gen)
    to_create = []
    needs_autogen = []
    skipped = 0
    problems = []

    for i, r in enumerate(rows, start=2):
        provided_no = r.pop("loan_no")

        if force_override:
            final_no = None  # needs auto-gen
        elif provided_no:
            final_no = provided_no
        elif auto_gen:
            final_no = None  # needs auto-gen
        else:
            problems.append(f"Row {i}: loan_no missing and auto-generate is OFF")
            continue

        if final_no and final_no in existing_lnos:
            skipped += 1
            continue

        obj = LoanHistory(
            loan_no=final_no,
            customer=r["customer"],
            loan_date=r["loan_date"],
            loan_type=r["loan_type"],
            principal=r["principal"],
            installment=r["installment"],
            loan_period=r["loan_period"],
            interest_rate=r["interest_rate"],
            net_disbursed=r["net_disbursed"],
            is_approved=True,
            approved_by=username,
            approved_at=now,
            is_disbursed=r["is_disbursed"],
            disbursed_at=now if r["is_disbursed"] else None,
            created_by=username,
        )

        if final_no:
            to_create.append(obj)
            existing_lnos.add(final_no)  # prevent batch-internal dupes
        else:
            needs_autogen.append(obj)

    # ── Bulk-create rows with provided loan_nos ─────────────────────
    created = 0
    if to_create:
        LoanHistory.objects.bulk_create(to_create, batch_size=BULK_BATCH)
        created += len(to_create)

    # ── Auto-gen rows: pull sequential numbers from Postgres sequences ──
    if needs_autogen:
        fast_autogen = []
        with _conn.cursor() as _cur:
            # Sync both sequences with max existing values to prevent
            # duplicate-key errors after imports with explicit loan_nos
            for prefix, seq in [('MOBI', 'loan_no_mobi_seq'), ('LN', 'loan_no_ln_seq')]:
                _cur.execute(
                    "SELECT MAX(CAST(REGEXP_REPLACE(loan_no, %s, '') AS INTEGER)) "
                    "FROM loans_loanhistory WHERE loan_no ~ %s",
                    [f'^{prefix}', f'^{prefix}\\d+$'],
                )
                max_existing = _cur.fetchone()[0] or 0
                _cur.execute(f"SELECT last_value FROM {seq}")
                current_seq = _cur.fetchone()[0] or 0
                if max_existing >= current_seq:
                    _cur.execute(f"SELECT setval('{seq}', %s)", [max_existing])

            for obj in needs_autogen:
                is_mobi = obj.loan_type.is_mobile_loan if obj.loan_type else False
                if is_mobi:
                    _cur.execute("SELECT nextval('loan_no_mobi_seq')")
                    obj.loan_no = f"MOBI{str(_cur.fetchone()[0]).zfill(6)}"
                else:
                    _cur.execute("SELECT nextval('loan_no_ln_seq')")
                    obj.loan_no = f"LN{str(_cur.fetchone()[0]).zfill(6)}"
                fast_autogen.append(obj)

        LoanHistory.objects.bulk_create(fast_autogen, batch_size=BULK_BATCH)
        created += len(fast_autogen)

    return dict(created=created, updated=0, skipped=skipped, problems=problems)


# ═══════════════════════════════════════════════════════════════════════
#  REGISTRY
# ═══════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════
#  7. LOAN GUARANTORS
# ═══════════════════════════════════════════════════════════════════════

GUARANTOR_COLUMNS = [
    Column("loan_no",           required=True,
           help="Existing loan number from LoanHistory.", example="LN000123"),
    Column("guarantor_cust_no", required=True,
           help="Member number of the guarantor (must already exist).", example="00045"),
    Column("amount",            required=True,
           help="Amount guaranteed by this member.", example="25000"),
]


def _ctx_guarantors(rows):
    from loans.models import LoanHistory, Guarantor
    from customers.models import Customer

    batch_lnos = {_to_str(r.get("loan_no")) for r in rows if r.get("loan_no")}
    batch_cnos = {_pad_cust_no(r.get("guarantor_cust_no")) for r in rows if r.get("guarantor_cust_no")}

    loan_map = {}
    for lh in LoanHistory.objects.filter(loan_no__in=batch_lnos).only("pk", "loan_no"):
        loan_map[lh.loan_no] = lh

    cust_map = {}
    for c in Customer.objects.filter(cust_no__in=batch_cnos).only("pk", "cust_no"):
        cust_map[c.cust_no] = c

    # Existing guarantor pairs: (loan_id, guarantor_cust_id)
    existing = set()
    loan_pks = [lh.pk for lh in loan_map.values()]
    if loan_pks:
        for g in Guarantor.objects.filter(loan_id__in=loan_pks).values_list(
            "loan_id", "guarantor_cust_id"
        ):
            existing.add(g)

    return {"loan_map": loan_map, "cust_map": cust_map, "existing_pairs": existing}


def _validate_guarantor(row, ctx):
    errors = []

    loan_no = _to_str(row.get("loan_no"))
    if not loan_no:
        errors.append("loan_no is required")
    loan = ctx.get("loan_map", {}).get(loan_no)
    if loan_no and not loan:
        errors.append(f"loan '{loan_no}' not found")

    gcno = _pad_cust_no(row.get("guarantor_cust_no"))
    if not gcno:
        errors.append("guarantor_cust_no is required")
    guarantor_cust = ctx.get("cust_map", {}).get(gcno)
    if gcno and not guarantor_cust:
        errors.append(f"guarantor member '{gcno}' not found")

    try:
        amount = _to_decimal(row.get("amount"))
    except ValueError as e:
        errors.append(str(e))
        amount = ZERO
    if amount <= 0:
        errors.append("amount must be > 0")

    cleaned = dict(
        loan=loan,
        guarantor_cust=guarantor_cust,
        amount=amount,
        _loan_pk=loan.pk if loan else None,
        _cust_pk=guarantor_cust.pk if guarantor_cust else None,
    )
    return cleaned, errors


def _commit_guarantors(rows, opts, user):
    from loans.models import Guarantor

    overwrite = bool(opts.get("overwrite_amount"))

    # Pre-load existing pairs once (already done in ctx, but reload fresh
    # for the commit since ctx was from validation time)
    loan_pks = {r["_loan_pk"] for r in rows if r.get("_loan_pk")}
    existing_map = {}   # (loan_id, cust_id) → Guarantor pk
    if loan_pks:
        for g in Guarantor.objects.filter(loan_id__in=loan_pks).only(
            "pk", "loan_id", "guarantor_cust_id"
        ):
            existing_map[(g.loan_id, g.guarantor_cust_id)] = g

    to_create = []
    to_update = []
    skipped = 0
    created = 0
    updated = 0

    seen = set()  # prevent intra-batch duplicates
    for r in rows:
        loan = r["loan"]
        cust = r["guarantor_cust"]
        if not loan or not cust:
            continue
        pair = (loan.pk, cust.pk)
        if pair in seen:
            skipped += 1
            continue
        seen.add(pair)

        existing = existing_map.get(pair)
        if existing and not overwrite:
            skipped += 1
            continue
        if existing and overwrite:
            existing.amount = r["amount"]
            to_update.append(existing)
        else:
            to_create.append(Guarantor(
                loan=loan, guarantor_cust=cust, amount=r["amount"]
            ))

    if to_create:
        Guarantor.objects.bulk_create(to_create, batch_size=BULK_BATCH)
        created = len(to_create)
    if to_update:
        Guarantor.objects.bulk_update(to_update, ["amount"], batch_size=BULK_BATCH)
        updated = len(to_update)

    return dict(created=created, updated=updated, skipped=skipped, problems=[])


# ═══════════════════════════════════════════════════════════════════════
#  8. LOAN COLLATERALS
# ═══════════════════════════════════════════════════════════════════════

_COLLATERAL_TYPES = {"land", "vehicle", "other"}

COLLATERAL_COLUMNS = [
    Column("loan_no",         required=True,
           help="Existing loan number from LoanHistory.", example="LN000123"),
    Column("owner_cust_no",   required=True,
           help="Member number of the asset owner (usually the borrower).", example="00116"),
    Column("collateral_type", required=True,
           help="Type: land | vehicle | other.", example="land"),
    Column("market_value",    required=True,
           help="Market valuation of the asset.", example="500000"),
    Column("forced_sale_value", required=True,
           help="Forced-sale / distress valuation.", example="350000"),
    Column("mortgage_value",  help="Mortgage value (optional).", example="400000"),
    Column("insurance_value", help="Insurance value (optional).", example="450000"),
    Column("title_deed_no",   help="Title deed number (land only).", example="LR/12345"),
    Column("location",        help="Location of asset (land/other).", example="Nakuru"),
    Column("size",            help="Size of land (e.g. 0.25 Ha).", example="0.25 Ha"),
    Column("registration_no", help="Vehicle registration number.", example="KDA 123A"),
    Column("chassis_no",      help="Vehicle chassis number.", example="JTDKN3DU1A0123456"),
    Column("model",           help="Vehicle model.", example="Toyota Probox 2015"),
    Column("description",     help="Free-text description of the asset.", example="Plot on Moi Road"),
]


def _ctx_collaterals(rows):
    from loans.models import LoanHistory
    from customers.models import Customer

    batch_lnos = {_to_str(r.get("loan_no")) for r in rows if r.get("loan_no")}
    batch_cnos = {_pad_cust_no(r.get("owner_cust_no")) for r in rows if r.get("owner_cust_no")}

    loan_map = {}
    for lh in LoanHistory.objects.filter(loan_no__in=batch_lnos).only("pk", "loan_no"):
        loan_map[lh.loan_no] = lh

    cust_map = {}
    for c in Customer.objects.filter(cust_no__in=batch_cnos).only("pk", "cust_no"):
        cust_map[c.cust_no] = c

    return {"loan_map": loan_map, "cust_map": cust_map}


def _validate_collateral(row, ctx):
    errors = []

    loan_no = _to_str(row.get("loan_no"))
    if not loan_no:
        errors.append("loan_no is required")
    loan = ctx.get("loan_map", {}).get(loan_no)
    if loan_no and not loan:
        errors.append(f"loan '{loan_no}' not found")

    ocno = _pad_cust_no(row.get("owner_cust_no"))
    if not ocno:
        errors.append("owner_cust_no is required")
    owner = ctx.get("cust_map", {}).get(ocno)
    if ocno and not owner:
        errors.append(f"owner member '{ocno}' not found")

    ctype = _to_str(row.get("collateral_type")).lower()
    if not ctype:
        errors.append("collateral_type is required")
    elif ctype not in _COLLATERAL_TYPES:
        errors.append(f"collateral_type '{ctype}' invalid; use one of {sorted(_COLLATERAL_TYPES)}")

    try:
        market = _to_decimal(row.get("market_value"))
        forced = _to_decimal(row.get("forced_sale_value"))
        mortgage = _to_decimal(row.get("mortgage_value"), default=None)
        insurance = _to_decimal(row.get("insurance_value"), default=None)
    except ValueError as e:
        errors.append(str(e))
        market = forced = ZERO
        mortgage = insurance = None

    if market <= 0:
        errors.append("market_value must be > 0")
    if forced <= 0:
        errors.append("forced_sale_value must be > 0")

    cleaned = dict(
        loan=loan,
        owner=owner,
        collateral_type=ctype,
        market_value=market,
        forced_sale_value=forced,
        mortgage_value=mortgage,
        insurance_value=insurance,
        title_deed_no=_to_str(row.get("title_deed_no")) or None,
        location=_to_str(row.get("location")) or None,
        size=_to_str(row.get("size")) or None,
        registration_no=_to_str(row.get("registration_no")) or None,
        chassis_no=_to_str(row.get("chassis_no")) or None,
        model=_to_str(row.get("model")) or None,
        description=_to_str(row.get("description")) or "",
    )
    return cleaned, errors


def _commit_collaterals(rows, opts, user):
    from loans.models import Collateral

    username = getattr(user, "username", "system")

    to_create = []
    for r in rows:
        loan = r.get("loan")
        owner = r.get("owner")
        if not loan or not owner:
            continue
        to_create.append(Collateral(
            loan=loan,
            owner=owner,
            collateral_type=r["collateral_type"],
            market_value=r["market_value"],
            forced_sale_value=r["forced_sale_value"],
            mortgage_value=r.get("mortgage_value"),
            insurance_value=r.get("insurance_value"),
            title_deed_no=r.get("title_deed_no"),
            location=r.get("location"),
            size=r.get("size"),
            registration_no=r.get("registration_no"),
            chassis_no=r.get("chassis_no"),
            model=r.get("model"),
            description=r.get("description", ""),
            created_by=username,
        ))

    if to_create:
        Collateral.objects.bulk_create(to_create, batch_size=BULK_BATCH)

    return dict(created=len(to_create), updated=0, skipped=0, problems=[])


# ═══════════════════════════════════════════════════════════════════════
#  9. LOAN CHARGE RECOVERIES  (charges-as-columns format)
# ═══════════════════════════════════════════════════════════════════════

# Base columns — charge-name columns are appended dynamically at import
# time (see _ctx_charge_recovery and the template download view).
CHARGE_RECOVERY_BASE_COLUMNS = [
    Column("loan_no", required=True,
           help="Existing loan number from LoanHistory.", example="LN000123"),
    Column("date", help="Date the charges were recovered (defaults to loan_date).",
           example="2024-05-01"),
]


def _get_charge_columns():
    """Return Column specs for every active LoanCharge (used by template download)."""
    from loans.models import LoanCharge
    return [
        Column(
            ch.name,
            help=f"{ch.get_charge_type_display()} charge — enter amount recovered.",
            example="500",
        )
        for ch in LoanCharge.objects.filter(is_active=True).order_by("name")
    ]


def _ctx_charge_recovery(rows):
    from loans.models import LoanHistory, LoanCharge, LoanChargeRecovery

    batch_lnos = {_to_str(r.get("loan_no")) for r in rows if r.get("loan_no")}
    loan_map = {}
    for lh in LoanHistory.objects.filter(loan_no__in=batch_lnos).only(
        "pk", "loan_no", "loan_date"
    ):
        loan_map[lh.loan_no] = lh

    charge_map = {}  # name (case-insensitive) → LoanCharge
    for ch in LoanCharge.objects.filter(is_active=True):
        charge_map[ch.name.strip().lower()] = ch

    # Existing recoveries to enable skip-duplicates
    existing = set(
        LoanChargeRecovery.objects
        .filter(loan__loan_no__in=batch_lnos)
        .values_list("loan__loan_no", "charge_id")
    )

    return {
        "loan_map": loan_map,
        "charge_map": charge_map,
        "existing_pairs": existing,
    }


def _validate_charge_recovery(row, ctx):
    errors = []

    loan_no = _to_str(row.get("loan_no"))
    if not loan_no:
        errors.append("loan_no is required")
    loan = ctx.get("loan_map", {}).get(loan_no)
    if loan_no and not loan:
        errors.append(f"loan '{loan_no}' not found")

    try:
        rec_date = _to_date(row.get("date"))
    except ValueError as e:
        errors.append(f"date: {e}")
        rec_date = None

    # Parse every charge column: any column not in (loan_no, date) is a charge name
    charge_map = ctx.get("charge_map", {})
    charges_parsed = []
    for col_name, raw_val in row.items():
        if col_name in ("loan_no", "date"):
            continue
        raw_str = _to_str(raw_val)
        if not raw_str:
            continue  # blank = no charge for this loan
        charge_obj = charge_map.get(col_name.strip().lower())
        if not charge_obj:
            errors.append(f"charge '{col_name}' not found in active LoanCharge records")
            continue
        try:
            amt = _to_decimal(raw_str)
        except ValueError as e:
            errors.append(f"{col_name}: {e}")
            continue
        if amt < 0:
            errors.append(f"{col_name}: amount cannot be negative")
            continue
        if amt == 0:
            continue
        charges_parsed.append((charge_obj, amt))

    # Default date to the loan's date
    if not rec_date and loan:
        rec_date = loan.loan_date

    cleaned = dict(
        loan=loan,
        loan_no=loan_no,
        date=rec_date,
        charges=charges_parsed,
    )
    return cleaned, errors


def _commit_charge_recovery(rows, opts, user):
    from loans.models import LoanChargeRecovery

    skip_existing = not bool(opts.get("overwrite_existing"))
    existing_pairs = set()

    # Re-derive existing pairs from the first row's context won't work here
    # since context is per-batch. Instead, gather from DB once.
    all_loan_nos = {r["loan_no"] for r in rows if r.get("loan_no")}
    if skip_existing and all_loan_nos:
        existing_pairs = set(
            LoanChargeRecovery.objects
            .filter(loan__loan_no__in=all_loan_nos)
            .values_list("loan__loan_no", "charge_id")
        )

    to_create = []
    skipped = 0
    for r in rows:
        loan = r.get("loan")
        if not loan:
            continue
        for charge_obj, amt in r.get("charges", []):
            pair = (loan.loan_no, charge_obj.id)
            if skip_existing and pair in existing_pairs:
                skipped += 1
                continue
            to_create.append(LoanChargeRecovery(
                loan=loan,
                charge=charge_obj,
                amount=amt,
                date=r.get("date"),
                reference=loan.loan_no,
                description=charge_obj.name,
            ))
            existing_pairs.add(pair)  # prevent intra-batch dupes

    if to_create:
        LoanChargeRecovery.objects.bulk_create(to_create, batch_size=BULK_BATCH)

    return dict(created=len(to_create), updated=0, skipped=skipped, problems=[])


# ═══════════════════════════════════════════════════════════════════════
#  REGISTRY
# ═══════════════════════════════════════════════════════════════════════

REGISTRY: Dict[str, ImportType] = {
    "customers": ImportType(
        slug="customers", title="Members / Customers", app_label="customers",
        icon="bi-people-fill",
        description=(
            "Import your existing SACCO membership register. Required fields are "
            "cust_no, full_name, and national_id. First/middle/last names are derived "
            "from full_name if left blank (and vice versa). Phone is auto-generated "
            "if blank so unique constraints still hold."
        ),
        columns=CUSTOMER_COLUMNS,
        validate_row=_validate_customer,
        commit_rows=_commit_customers,
        build_context=_ctx_customers,
        options=[
            dict(name="overwrite_existing", label="Overwrite if cust_no already exists",
                 help="If ON, existing members with the same cust_no are updated. "
                      "If OFF, they are skipped.", default=False),
        ],
    ),
    "next_of_kin": ImportType(
        slug="next_of_kin", title="Next of Kin", app_label="customers",
        icon="bi-person-heart",
        description=(
            "Attach next-of-kin records to existing members. Required fields are "
            "cust_no and kin_name. Duplicate (customer + kin_name) pairs are skipped."
        ),
        columns=NEXT_OF_KIN_COLUMNS,
        validate_row=_validate_nok,
        commit_rows=_commit_nok,
        build_context=_ctx_nok,
        options=[],
    ),
    "savings_transactions": ImportType(
        slug="savings_transactions", title="Savings Transactions",
        app_label="transactions", icon="bi-cash-stack",
        description=(
            "Import historical savings deposits and withdrawals. Each row hits "
            "SavingsTransaction. saving_type must match a Customer Accounts Setup "
            "product (its account_type)."
        ),
        columns=SAVINGS_TXN_COLUMNS,
        validate_row=_validate_savings_txn,
        commit_rows=_commit_savings_txn,
        build_context=_ctx_savings_txn,
        options=[
            dict(name="post_gl",
                 label="Also post double-entry GL journals",
                 help="If ON, every transaction posts a balanced pair to the ledger "
                      "using each product's configured GL/cash accounts. Leave OFF if "
                      "you're also importing SACCO account balances separately.",
                 default=False),
        ],
    ),
    "loan_transactions": ImportType(
        slug="loan_transactions", title="Loan Transactions",
        app_label="transactions", icon="bi-arrow-left-right",
        description=(
            "Import historical loan movements — disbursements, interest charges, "
            "and repayments. Requires an existing LoanHistory row keyed by loan_no."
        ),
        columns=LOAN_TXN_COLUMNS,
        validate_row=_validate_loan_txn,
        commit_rows=_commit_loan_txn,
        build_context=_ctx_loan_txn,
        options=[
            dict(name="post_gl",
                 label="Also post double-entry GL journals",
                 help="If ON, each row posts a balanced ledger pair to the loan "
                      "product's configured GL/cash accounts.", default=False),
        ],
    ),
    "sacco_balances": ImportType(
        slug="sacco_balances", title="Trial Balance / Opening Balances",
        app_label="accounting", icon="bi-bank",
        description=(
            "Upload a trial balance to align the system's GL with your company books "
            "as at a specific cutoff date. Each row represents one account's balance. "
            "Use debit_amount / credit_amount columns for explicit DR/CR placement, "
            "or the legacy 'balance' column (auto-placed on the account's normal side). "
            "All rows must share the same as_at_date. The system validates that your "
            "trial balance is in equilibrium (total debits = total credits) before "
            "posting — exactly like SAP / Dynamics 365 / QuickBooks. All accounts "
            "are posted as a single multi-leg journal entry with one shared reference "
            "for clean audit traceability."
        ),
        columns=SACCO_BALANCE_COLUMNS,
        validate_row=_validate_sacco_balance,
        commit_rows=_commit_sacco_balances,
        build_context=_ctx_sacco_balance,
        options=[
            dict(name="post_journal_entries",
                 label="Post GL journal entries (recommended)",
                 help="Posts all account balances as a single balanced journal entry "
                      "dated to your as_at_date. This is how the system's trial "
                      "balance aligns with company books. The system verifies "
                      "sum(debits) = sum(credits) before posting — an imbalanced TB "
                      "is rejected. Turn OFF only if you are importing the full "
                      "historical ledger separately.", default=True),
            dict(name="allow_imbalance",
                 label="Allow imbalance (post difference to equity)",
                 help="Only relevant when 'Post GL journal entries' is ON. "
                      "If your TB doesn't balance (e.g. partial import or "
                      "accounts still being set up), enable this to post the "
                      "difference to an Opening Balance / Equity account instead "
                      "of rejecting the import. The difference amount is shown "
                      "in the summary report.", default=False),
        ],
    ),
    "loan_history": ImportType(
        slug="loan_history", title="Loan History",
        app_label="loans", icon="bi-journal-text",
        description=(
            "Import existing loans (LoanHistory). Most SACCOs already have loan "
            "numbers — we keep them as-is. Toggle 'Auto-generate loan_no if blank' "
            "to let the system assign numbers to rows missing one. Toggle "
            "'Override provided loan numbers' to discard supplied numbers entirely "
            "and re-number using this system's convention (LNxxxxxx / MOBIxxxxxxxx)."
        ),
        columns=LOAN_HISTORY_COLUMNS,
        validate_row=_validate_loan_history,
        commit_rows=_commit_loan_history,
        build_context=_ctx_loan_history,
        options=[
            dict(name="autogen_loan_no",
                 label="Auto-generate loan_no when blank",
                 help="Rows with no loan_no get one assigned by the system.",
                 default=True),
            dict(name="force_autogen",
                 label="Override provided loan numbers (re-number everything)",
                 help="Discards any supplied loan_no and assigns fresh system numbers. "
                      "Use with caution.",
                 default=False),
        ],
    ),
    "guarantors": ImportType(
        slug="guarantors", title="Loan Guarantors",
        app_label="loans", icon="bi-person-check-fill",
        description=(
            "Import guarantor records for existing loans. Each row links an "
            "existing member (guarantor_cust_no) to an existing loan (loan_no) "
            "with a guaranteed amount. Both the loan and the guarantor member "
            "must already exist in the system. Duplicate (loan + guarantor) "
            "pairs are skipped automatically."
        ),
        columns=GUARANTOR_COLUMNS,
        validate_row=_validate_guarantor,
        commit_rows=_commit_guarantors,
        build_context=_ctx_guarantors,
        options=[
            dict(name="overwrite_amount",
                 label="Overwrite amount if guarantor already exists for this loan",
                 help="If ON, existing guarantor records are updated with the new amount. "
                      "If OFF, they are skipped.", default=False),
        ],
    ),
    "collaterals": ImportType(
        slug="collaterals", title="Loan Collaterals",
        app_label="loans", icon="bi-shield-lock-fill",
        description=(
            "Import collateral / security records for existing loans. Each row "
            "links an asset (land, vehicle, or other) to an existing loan. "
            "The loan and the asset owner must already exist. Collateral type "
            "determines which detail fields apply — land uses title_deed_no / "
            "location / size, vehicle uses registration_no / chassis_no / model."
        ),
        columns=COLLATERAL_COLUMNS,
        validate_row=_validate_collateral,
        commit_rows=_commit_collaterals,
        build_context=_ctx_collaterals,
        options=[],
    ),
    "charge_recoveries": ImportType(
        slug="charge_recoveries",
        title="Loan Charge Recoveries",
        app_label="loans",
        icon="bi-receipt-cutoff",
        description=(
            "Link historical charges to imported loans. Each row is one loan; "
            "each active LoanCharge name becomes a column header — enter the "
            "amount recovered under the relevant column. Leave a column blank "
            "if the loan did not attract that charge. The loan must already "
            "exist in LoanHistory. Download the template to get the current "
            "charge names as column headers."
        ),
        columns=CHARGE_RECOVERY_BASE_COLUMNS,  # charge cols appended at runtime
        validate_row=_validate_charge_recovery,
        commit_rows=_commit_charge_recovery,
        build_context=_ctx_charge_recovery,
        options=[
            dict(name="overwrite_existing",
                 label="Overwrite if charge already recorded for this loan",
                 help="If ON, existing charge recovery records for the same "
                      "(loan + charge) pair are duplicated. If OFF (default), "
                      "those pairs are skipped.",
                 default=False),
        ],
    ),
}


def get_import_type(slug: str) -> ImportType:
    if slug not in REGISTRY:
        raise KeyError(f"Unknown import type '{slug}'")
    return REGISTRY[slug]
