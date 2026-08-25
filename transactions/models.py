from django.db import models
from django.conf import settings
from decimal import Decimal, ROUND_HALF_UP
from django.db import models
from django.core.exceptions import ValidationError

# transactions/models.py
class SavingsTransaction(models.Model):
    cust_no = models.CharField(max_length=20,db_index=True)
    saving_type = models.CharField(max_length=50)  # e.g. fixed_deposit, savings_deposit, share_capital
    account_code = models.CharField(max_length=50, null=True, blank=True)
    tr_date = models.DateTimeField()
    tr_ref = models.CharField(max_length=50, db_index=True)
    ext_ref = models.CharField(max_length=50, db_index=True, null=True)#external refernce
    tr_desc = models.CharField(max_length=255, blank=True)
    debit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_by = models.CharField(max_length=150, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            # Balance queries: SUM(credit-debit) WHERE cust_no=X AND saving_type=Y
            models.Index(fields=['cust_no', 'saving_type'], name='idx_sav_cust_type'),
            # Dividend calculation: GROUP BY cust_no, month WHERE saving_type=X AND tr_date<=Y
            models.Index(fields=['saving_type', 'tr_date'], name='idx_sav_type_date'),
            # Combined for dividend calc opening balance query
            models.Index(fields=['saving_type', 'tr_date', 'cust_no'], name='idx_sav_type_date_cust'),
        ]

class LoanTransaction(models.Model):
    cust_no = models.CharField(max_length=20,db_index=True)
    loan_id = models.PositiveIntegerField(db_index=True)
    loan_no = models.CharField(max_length=50,db_index=True, null=True)  # e.g. LN000001
    loan_type = models.CharField(max_length=50)  # e.g. normal_loan
    account_code = models.CharField(max_length=50, null=True, blank=True)
    tr_date = models.DateTimeField()
    tr_ref = models.CharField(max_length=50, db_index=True)
    ext_ref = models.CharField(max_length=50, db_index=True, null=True)#external reference
    tr_desc = models.CharField(max_length=255, blank=True)
    debit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_by = models.CharField(max_length=150, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

from django.db import models
from django.conf import settings

class BulkUploadQueue(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processed', 'Processed'),
        ('failed', 'Failed'),
    ]

    date = models.DateField()
    customer = models.ForeignKey('customers.Customer', on_delete=models.CASCADE)
    saving_type = models.CharField(max_length=50)  # Handles savings e.g savings_deposit.
    account_code = models.CharField(max_length=50, null=True, blank=True)
    loan_id = models.PositiveIntegerField(db_index=True)
    loan_no = models.CharField(max_length=50,db_index=True, null=True)  # e.g. LN000001
    loan_type = models.CharField(max_length=50,  null=True)  # e.g. normal_loan
    is_loan = models.BooleanField(default=False)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.CharField(max_length=255, blank=True, null=True)

    # Audit & Process fields
    session_key = models.CharField(max_length=100, db_index=True)  # Groups rows for the current user's batch
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True)
    bank_account = models.ForeignKey('accounting.SaccoAccount', on_delete=models.CASCADE,null=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    error_message = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.customer.cust_no} - {self.account_type} - {self.amount}"
from django.db import models
from django.utils import timezone


class BulkTransactionBatch(models.Model):
    """
    Tracks a bulk transaction session end-to-end: from first keystroke
    (autosave) through review, posting, or abandonment.

    The `grid_data` JSONField stores the full grid state so the user can
    close the browser and resume later with all their work intact.
    """
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("saved", "Saved"),
        ("posted", "Posted"),
        ("failed", "Failed"),
    ]

    session_key = models.CharField(max_length=100, unique=True, db_index=True)
    label = models.CharField(
        max_length=200, blank=True,
        help_text="Optional user-given name, e.g. 'June deposits batch 2'",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    grid_data = models.JSONField(
        default=list,
        help_text="Full grid state — array of row dicts for resume",
    )
    bank_account_id = models.PositiveIntegerField(null=True, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name="bulk_batches",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Bulk Transaction Batch"
        verbose_name_plural = "Bulk Transaction Batches"

    def __str__(self):
        label = self.label or f"Batch {self.session_key[:8]}"
        return f"[{self.get_status_display()}] {label} — {self.row_count} rows, KES {self.total_amount:,.2f}"

    @property
    def is_posted(self):
        return self.status == "posted"


class MpesaNotification(models.Model):
    transaction_type = models.CharField(max_length=50)
    trans_id = models.CharField(max_length=30, unique=True) # Matches statement 'receipt_no'
    trans_time = models.DateTimeField()
    trans_amount = models.DecimalField(max_digits=12, decimal_places=2)
    business_shortcode = models.CharField(max_length=20)
    bill_ref_number = models.CharField(max_length=100, blank=True, null=True)
    invoice_number = models.CharField(max_length=100, blank=True, null=True)
    org_account_balance = models.CharField(max_length=50, blank=True, null=True)
    third_party_trans_id = models.CharField(max_length=50, blank=True, null=True)
    msisdn = models.CharField(max_length=200)
    first_name = models.CharField(max_length=50, blank=True, null=True)

    received_at = models.DateTimeField(default=timezone.now)
    posted = models.BooleanField(default=False)
    last_error = models.TextField(blank=True, null=True)


    def __str__(self):
        return f"{self.trans_id} - {self.trans_amount}"


class QuarantinedMpesaPayload(models.Model):
    """
    Stores M-Pesa callback payloads that arrived from non-whitelisted IPs.

    These are NOT posted to MpesaNotification and will never hit the
    processing pipeline. Staff can inspect them in Django admin to:
      * spot legitimate Safaricom IP changes that need whitelisting
      * investigate potential injection/fraud attempts
      * recover real payments that were quarantined by a stale whitelist

    After review, staff can mark a record as reviewed or promote it
    to MpesaNotification if it turns out to be genuine.
    """
    REVIEW_CHOICES = [
        ("pending", "Pending Review"),
        ("legitimate", "Legitimate — Promoted"),
        ("suspicious", "Suspicious — Rejected"),
    ]

    source_ip = models.GenericIPAddressField(help_text="IP that sent this payload")
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    raw_body = models.TextField(help_text="Full raw request body (JSON)")
    user_agent = models.CharField(max_length=300, blank=True)
    xff_header = models.CharField(
        max_length=500, blank=True,
        help_text="X-Forwarded-For header value",
    )
    request_path = models.CharField(max_length=200, blank=True)

    # Extracted fields for quick scanning in admin (may be blank if payload is junk)
    trans_id = models.CharField(max_length=30, blank=True, db_index=True)
    trans_amount = models.CharField(max_length=30, blank=True)
    msisdn = models.CharField(max_length=200, blank=True)
    bill_ref_number = models.CharField(max_length=100, blank=True)

    review_status = models.CharField(
        max_length=20, choices=REVIEW_CHOICES, default="pending", db_index=True,
    )
    reviewed_by = models.CharField(max_length=150, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_at"]
        verbose_name = "Quarantined M-Pesa Payload"
        verbose_name_plural = "Quarantined M-Pesa Payloads"

    def __str__(self):
        return f"[{self.review_status}] {self.source_ip} — {self.trans_id or 'no-txn-id'} @ {self.received_at:%Y-%m-%d %H:%M}"


class PostedMpesaNotification(models.Model):
    mpesa_notification = models.OneToOneField(MpesaNotification, on_delete=models.CASCADE, related_name='posting_details')
    customer_no = models.CharField(max_length=20) # Normalized string format
    account_type = models.CharField(max_length=30)
    posted_at = models.DateTimeField(default=timezone.now)
    is_reconciled = models.BooleanField(default=False)
    def __str__(self):
        return f"Posted {self.customer_no} - {self.account_type} - {self.mpesa_notification.trans_id}"



from django.db import models

class CustomerAccountsSetup(models.Model):
    account_code = models.CharField(max_length=10, unique=True)   # e.g. S01, L01
    account_name = models.CharField(max_length=100)               # e.g. Share Capital
    acc_initials = models.CharField(max_length=10, unique=True)   # e.g. SC, MS, NL, EL
    account_type = models.CharField(max_length=50, unique=True)# e.g normal_loan
    interest_calc_method = models.CharField(max_length=100, null=True, default="reducing_balance")#e.g reducing_balance, flat_rate
    is_withdrawable = models.BooleanField(default=False)
    is_loan_account = models.BooleanField(default=False)
    is_mobile_loan = models.BooleanField(default=False)
    max_loan_limit = models.DecimalField(max_digits=12, decimal_places=2, default=50000.00)
    min_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    max_repayment_period = models.PositiveBigIntegerField(default=6)#repayment period in months
    access_on_channels = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # ── ELIGIBILITY & GUARANTEE MULTIPLIERS ─────────────────────────
    # Configure once per product; consumed by the loan-appraisal engine
    # and the add-guarantor form to enforce policy uniformly.
    #
    # guarantee_multiplier
    #   For SAVINGS products only. How many times its face value this
    #   deposit balance can secure as a guarantee for another member's
    #   loan. e.g. 3 → a KES 10 000 deposit balance can guarantee up to
    #   KES 30 000 of outstanding loans.
    #
    # loan_multiplier
    #   For SAVINGS products only. Multiplier used when computing this
    #   member's OWN maximum borrowing capacity from this deposit type.
    #   e.g. 5 → a KES 20 000 deposit lets the member borrow up to
    #   KES 100 000 in loans whose base_deposits include this product.
    #
    # base_deposits
    #   For LOAN products only. The savings products a member MUST hold
    #   in order to qualify for this loan. Their balances are summed and
    #   multiplied by each product's loan_multiplier to derive the
    #   member's eligible ceiling on this loan product.
    guarantee_multiplier = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('3.00'),
        help_text=(
            "Savings only: how many times its face value this deposit "
            "may guarantee (e.g. 3 → KES 10k can guarantee up to KES 30k)."
        ),
    )
    loan_multiplier = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('3.00'),
        help_text=(
            "Savings only: multiplier used to derive the owner's own "
            "borrowing capacity from this deposit balance (e.g. 5 → "
            "KES 20k gives eligibility up to KES 100k)."
        ),
    )
    base_deposits = models.ManyToManyField(
        'self', symmetrical=False, blank=True,
        related_name='eligible_loan_products',
        limit_choices_to={'is_loan_account': False, 'is_active': True},
        help_text=(
            "Loan products only: the savings products a member must "
            "hold to qualify for this loan. Their balances are combined "
            "with each product's loan_multiplier to compute eligibility."
        ),
    )

    # ── GL Account Linkage ─────────────────────────────────────────
    # These three FKs tell the system exactly which GL accounts to hit
    # when a member transacts on this product. Configure once in Admin,
    # and every deposit / withdrawal / disbursement / repayment reads
    # them automatically — no hardcoded mapping needed.
    #
    # Example for savings_deposit:
    #   sacco_gl_account   → 900-802000  Member Deposits (liability)
    #   sacco_cash_account → 900-601001  Coop Bank (asset)
    #   sacco_interest_account → (blank)
    #
    # Example for normal_loan:
    #   sacco_gl_account   → 900-630011  Repsi Loan (asset)
    #   sacco_cash_account → 900-601001  Coop Bank (asset)
    #   sacco_interest_account → 900-110000  Normal Loan Interest (income)

    sacco_gl_account = models.ForeignKey(
        'accounting.SaccoAccount',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='linked_products',
        verbose_name='GL Balance Account',
        help_text=(
            "The GL account where this product's balance lives. "
            "Savings → a liability (e.g. 900-802000 Member Deposits). "
            "Loans → an asset (e.g. 900-630011 Repsi Loan)."
        ),
    )

    sacco_interest_account = models.ForeignKey(
        'accounting.SaccoAccount',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='interest_products',
        verbose_name='GL Interest Income Account',
        help_text=(
            "For loan products: the GL income account for interest "
            "(e.g. 900-110000 Normal Loan Interest). "
            "Leave blank for savings products."
        ),
    )

    sacco_cash_account = models.ForeignKey(
        'accounting.SaccoAccount',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='cash_products',
        verbose_name='GL Cash/Bank Account',
        help_text=(
            "The cash/bank GL account to hit on the other side of the "
            "journal. Usually 900-601001 Coop Bank. If blank, defaults "
            "to GL.CASH (900-601001)."
        ),
    )

    def get_gl_code(self):
        """Return the primary GL account code, or None if not linked."""
        return self.sacco_gl_account.account_code if self.sacco_gl_account_id else None

    def get_interest_gl_code(self):
        """Return the interest income GL account code, or None."""
        return self.sacco_interest_account.account_code if self.sacco_interest_account_id else None

    def get_cash_gl_code(self):
        """Return the cash/bank GL account code, or the system default."""
        if self.sacco_cash_account_id:
            return self.sacco_cash_account.account_code
        from accounting.services import GL
        return GL.CASH  # Coop Bank default
    def get_account_type_display(self):
        """
        Dynamically returns a clean, human-readable name without
        relying on hardcoded choices.
        """
        if self.account_name:
            return self.account_name

        # Fallback: converts 'normal_loan' -> 'Normal Loan'
        return self.account_type.replace('_', ' ').title()
    def __str__(self):
        gl = f" → {self.sacco_gl_account.account_code}" if self.sacco_gl_account_id else ""
        return f"{self.account_code} - {self.account_name}{gl}"

from django.db import models
from decimal import Decimal

from django.db import models
from decimal import Decimal

class DividendBatch(models.Model):
    SAVING_TYPES = [
        ("share_capital", "Share Capital"),
        ("welfare_deposit", "Welfare Deposit"),
        ("seed_deposit", "Seed Deposit"),
    ]
    batch_no = models.CharField(max_length=50, unique=True)
    saving_type = models.CharField(max_length=50, choices=SAVING_TYPES)

    # New Fields
    amount_to_share = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    wht_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0) # e.g., 15.00 for 15%
    processing_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0) # e.g., 100.00

    cut_off_date = models.DateField()

    # Accumulated Totals
    total_gross = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    total_tax = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    total_fees = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    total_net_payout = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    is_posted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=150)

    def __str__(self):
        return f"{self.batch_no} - {self.saving_type} (Total Share: {self.amount_to_share})"

class DividendDetail(models.Model):
    batch = models.ForeignKey(DividendBatch, on_delete=models.CASCADE, related_name='details')
    cust_no = models.CharField(max_length=20)
    member_name = models.CharField(max_length=255)
    weighted_avg_balance = models.DecimalField(max_digits=14, decimal_places=2)

    # Breakdown Fields
    gross_interest = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    withholding_tax = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    processing_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_payout = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    is_posted = models.BooleanField(default=False)

    class Meta:
        indexes = [
            # Batch posting: filter unposted details per batch
            models.Index(fields=['batch', 'is_posted'], name='idx_divdetail_batch_posted'),
            # Slip lookup by member
            models.Index(fields=['cust_no', 'batch'], name='idx_divdetail_cust_batch'),
        ]

    def __str__(self):
        return f"{self.cust_no} - Net: {self.net_payout}"


from django.db import models

class DividendDeclaration(models.Model):
    STATUS_CHOICES = (
        ('DRAFT', 'Draft'),
        ('POSTED', 'Posted'),
    )

    title = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    total_profit = models.DecimalField(max_digits=12, decimal_places=2)

    # --- New Deduction Fields ---
    withholding_tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=15.00, help_text="Enter as a percentage, e.g., 15 for 15%")
    processing_fee = models.DecimalField(max_digits=10, decimal_places=2, default=100.00, help_text="Flat fee deducted per member payout")

    saving_type = models.CharField(max_length=50, default='share_capital')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='DRAFT')
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.title} - {self.status}"



class DividendSlipItem(models.Model):
    detail = models.ForeignKey(DividendDetail, on_delete=models.CASCADE, related_name='slip_items')
    period_date = models.DateField()  # e.g., 31-01-2025
    savings_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    ratio = models.DecimalField(max_digits=5, decimal_places=4, default=0) # Prorata weight
    weighted_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gross_interest = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    wht_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    fee_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_interest = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        ordering = ['period_date']


class DividendTaskProgress(models.Model):
    """
    Tracks the progress of async dividend calculation / posting jobs.
    Polled by the UI to show real-time progress bars.
    """
    TASK_TYPES = [
        ('calculation', 'Calculation'),
        ('posting', 'Posting'),
    ]
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    batch = models.ForeignKey(
        DividendBatch, on_delete=models.CASCADE,
        related_name='task_progress', null=True, blank=True,
    )
    task_type = models.CharField(max_length=20, choices=TASK_TYPES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued')
    total_items = models.PositiveIntegerField(default=0)
    processed_items = models.PositiveIntegerField(default=0)
    current_chunk = models.PositiveIntegerField(default=0)
    total_chunks = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=150, blank=True)

    # Calculation-specific: store params so the background task can resume
    saving_type = models.CharField(max_length=50, blank=True)
    calc_params = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.task_type} — {self.status} ({self.processed_items}/{self.total_items})"

    @property
    def progress_percent(self):
        if self.total_items == 0:
            return 0
        return round((self.processed_items / self.total_items) * 100, 1)


class WithdrawalChargeBand(models.Model):
    from_amount = models.DecimalField(max_digits=12, decimal_places=2)
    to_amount = models.DecimalField(max_digits=12, decimal_places=2)
    customer_charge = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["from_amount"]
        verbose_name = "Withdrawal Charge Band"
        verbose_name_plural = "Withdrawal Charge Bands"

    def __str__(self):
        return f"{self.from_amount:,.0f} – {self.to_amount:,.0f} → KES {self.customer_charge:,.2f}"

    def clean(self):
        if self.from_amount and self.to_amount and self.from_amount > self.to_amount:
            raise ValidationError("The 'From Amount' cannot be greater than the 'To Amount'.")

    @classmethod
    def lookup(cls, amount):
        return cls.objects.filter(
            is_active=True,
            from_amount__lte=amount,
            to_amount__gte=amount
        ).first()


class TransactionCharge(models.Model):
    METHODS = [("flat", "Flat Amount (KES)"), ("percentage", "Percentage")]
    TARGETS = [("on_principal", "On withdrawal amount"), ("on_charge", "On the band fee")]

    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=30, unique=True)
    charge_method = models.CharField(max_length=20, choices=METHODS)
    rate = models.DecimalField(max_digits=12, decimal_places=4)
    applies_to = models.CharField(max_length=30, choices=TARGETS, default="on_charge")
    sacco_account = models.ForeignKey(
        "accounting.SaccoAccount", on_delete=models.PROTECT, null=True, blank=True
    )
    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Transaction Charge"
        verbose_name_plural = "Transaction Charges"

    def __str__(self):
        unit = "%" if self.charge_method == "percentage" else "KES"
        return f"{self.name} ({self.code}) — {self.rate} {unit}"

    def compute(self, base_amount):
        base = Decimal(str(base_amount or 0))
        if self.charge_method == "flat":
            value = self.rate
        else:
            value = base * self.rate / Decimal("100")
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_withdrawal_charges(amount):
    """
    Calculate all charges for a withdrawal.
    """
    amount = Decimal(str(amount))

    # 1. Band fee lookup
    band = WithdrawalChargeBand.lookup(amount)
    if not band:
        # Fallback to zero, or alternatively raise a ValidationError if bands are mandatory
        band_fee = Decimal("0.00")
    else:
        band_fee = Decimal(str(band.customer_charge))

    # 2. Extra charges computation
    extras = []
    total_extras = Decimal("0.00")

    # Prefetching active charges
    active_charges = TransactionCharge.objects.filter(is_active=True)

    for tc in active_charges:
        base = amount if tc.applies_to == "on_principal" else band_fee
        charge = tc.compute(base)

        extras.append({
            "name": tc.name,
            "code": tc.code,
            "amount": charge,
        })
        total_extras += charge

    total_charges = band_fee + total_extras

    return {
        "band_fee": band_fee,
        "extra_charges": extras,
        "total_charges": total_charges,
        "net_amount": amount,
        "gross_debit": amount + total_charges,
    }
