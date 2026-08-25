from django.db import models
from decimal import Decimal
from accounts.models import CustomUser
from transactions.models import SavingsTransaction,LoanTransaction,CustomerAccountsSetup
from customers.models import Customer
class LoanLimitGraduation(models.Model):
    cust_no = models.CharField(max_length=40)
    full_name = models.CharField(max_length=255)
    loan_product = models.CharField(max_length=50)
    graduation_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    def __str__(self):
     return f"{self.cust_no} - {self.amount}"


import logging
from django.db import models
from django.db.models import Index

logger = logging.getLogger(__name__)

class LoanHistory(models.Model):
    loan_no = models.CharField(max_length=40, unique=True, editable=False, null=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, to_field='cust_no', db_column='cust_no')
    loan_date = models.DateField()
    principal = models.DecimalField(max_digits=12, decimal_places=2)
    installment = models.DecimalField(max_digits=12, decimal_places=2)
    loan_type = models.ForeignKey(CustomerAccountsSetup, on_delete=models.CASCADE)
    loan_period = models.IntegerField()
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, default=12.0)
    net_disbursed = models.DecimalField(max_digits=12, decimal_places=2)
    # Bridging / offset loan data — persisted so the appraisal can display
    # offset payoffs without re-querying the approval payload.
    total_offset_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    offset_data = models.JSONField(default=list, blank=True,
        help_text='[{"loan_no":"LN000123","amount":"50000.00"}, …]')
    # DEPRECATED: processing_fee, insurance_fee, other_charges removed.
    # All loan charges are now tracked via LoanChargeRecovery.
    created_by = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)

    # Status Fields (Highly queried)
    is_approved = models.BooleanField(default=False)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.CharField(max_length=50, null=True, blank=True)

    is_disbursed = models.BooleanField(default=False)
    disbursal_ref = models.CharField(max_length=40, unique=True, editable=False, null=True)
    disbursed_at = models.DateTimeField(null=True)

    is_restructured = models.BooleanField(default=False)
    restructured_at = models.DateTimeField(null=True)
    restructure_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    original_loan_summary = models.TextField(null=True)

    class Meta:
        verbose_name_plural = "Loan Histories"
        # ── PERFORMANCE INDEXES ──────────────────────────────────────
        indexes = [
            # 1. Pipeline Queue Index (Speeds up dashboard/processing lookups)
            models.Index(fields=['is_approved', 'is_disbursed'], name='idx_loan_pipeline'),

            # 2. Customer Activity Index (Optimizes fetching a member's specific running loans)
            models.Index(fields=['customer','loan_no', 'is_disbursed'], name='idx_cust_active_loans'),

            # 3. Chronological Reporting Index (Speeds up date filtering and ordering)
            models.Index(fields=['loan_date'], name='idx_loan_date_sort'),
        ]

    def _generate_loan_no(self):
        """
        Generate the next sequential loan number using a Postgres sequence.

        Before calling nextval(), sync the sequence with the maximum existing
        loan number in the table — this prevents duplicate-key errors after
        data imports insert records with explicit loan numbers.

        Sequences are atomic and operate outside transaction isolation,
        so concurrent requests always get unique numbers — no race conditions.
        """
        from django.db import connection

        is_mobi = self.loan_type.is_mobile_loan
        if is_mobi:
            prefix, digit_length, seq = 'MOBI', 6, 'loan_no_mobi_seq'
        else:
            prefix, digit_length, seq = 'LN', 6, 'loan_no_ln_seq'

        with connection.cursor() as cursor:
            # Sync the sequence with the highest existing loan number
            cursor.execute(
                "SELECT MAX(CAST(REGEXP_REPLACE(loan_no, %s, '') AS INTEGER)) "
                "FROM loans_loanhistory WHERE loan_no ~ %s",
                [f'^{prefix}', f'^{prefix}\\d+$'],
            )
            max_existing = cursor.fetchone()[0] or 0

            # Peek at the current sequence value; advance if behind
            cursor.execute(f"SELECT last_value FROM {seq}")
            current_seq = cursor.fetchone()[0] or 0
            if max_existing >= current_seq:
                cursor.execute(f"SELECT setval('{seq}', %s)", [max_existing])

            cursor.execute(f"SELECT nextval('{seq}')")
            next_no = cursor.fetchone()[0]

        return f"{prefix}{str(next_no).zfill(digit_length)}"

    def save(self, *args, **kwargs):
        if not self.loan_no:
            self.loan_no = self._generate_loan_no()
        super().save(*args, **kwargs)


    def get_savings_balance(self):
        # Aggregation logic for savings
        credits = SavingsTransaction.objects.filter(customer=self).aggregate(s=models.Sum('credit_amount'))['s'] or 0
        debits = SavingsTransaction.objects.filter(customer=self).aggregate(s=models.Sum('debit_amount'))['s'] or 0
        return Decimal(credits) - Decimal(debits)
# 2. ADDED: Custom method for your template to fetch the display property safely
    def get_loan_type_display(self):
        if self.loan_type:
            return self.loan_type.get_account_type_display()
        return "Unknown"

    # 3. FIXED: Removed the broken self.get_loan_type_display() call
    def __str__(self):
        return f"{self.loan_no} - {self.loan_type.account_name if self.loan_type else 'No Type'}"


class Guarantor(models.Model):
    loan = models.ForeignKey(LoanHistory, on_delete=models.CASCADE)
    guarantor_cust = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='guarantees_given')
    amount = models.DecimalField(max_digits=12, decimal_places=2)

# loans/models.py (or wherever LoanCharge is located)
from django.db import models

class LoanCharge(models.Model):
    LOAN_CHARGE_TYPES = [
        ('flat_amount', 'Flat Amount'),
        ('percentage', 'Percentage'),
    ]
    name = models.CharField(max_length=50, unique=True)
    description = models.CharField(max_length=150, blank=True, null=True)
    
    # ── CHANGED FROM ForeignKey TO ManyToManyField ──
    loan_products = models.ManyToManyField(
        'transactions.CustomerAccountsSetup', 
        blank=True,
        related_name='loan_charges',
        limit_choices_to={'is_loan_account': True}, # Limits options strictly to loan setups
        help_text="Select all loan products that attract this charge."
    )
    
    charge_type = models.CharField(max_length=20, choices=LOAN_CHARGE_TYPES)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    min_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)# must not be less this amount
    max_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)# must not be more than this amount
    is_bridging_fee = models.BooleanField(default=False)
    is_mandatory = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.get_charge_type_display()})"

class LoanChargeRecovery(models.Model):
    loan = models.ForeignKey(LoanHistory, on_delete=models.CASCADE)
    date = models.DateField(null=True, blank=True)
    reference = models.CharField(max_length=50, blank=True, null=True)
    description = models.CharField(max_length=150, blank=True, null=True)
    charge = models.ForeignKey(LoanCharge, on_delete=models.CASCADE, related_name='loan_charge_recoveries')
    amount = models.DecimalField(max_digits=12, decimal_places=2)

from django.db import models
from customers.models import Customer

# ... (Keep your existing LoanHistory and Guarantor models) ...

class Collateral(models.Model):
    COLLATERAL_TYPES = [
        ('land', 'Land / Title Deed'),
        ('vehicle', 'Motor Vehicle / Logbook'),
        ('other', 'Other Asset'),
    ]

    loan = models.ForeignKey('LoanHistory', on_delete=models.CASCADE, related_name='collaterals')
    owner = models.ForeignKey(Customer, on_delete=models.CASCADE, help_text="Owner of the asset (usually the borrower)")
    collateral_type = models.CharField(max_length=20, choices=COLLATERAL_TYPES)
    
    # Valuation Fields
    market_value = models.DecimalField(max_digits=12, decimal_places=2)
    forced_sale_value = models.DecimalField(max_digits=12, decimal_places=2)
    mortgage_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    insurance_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    
    # Specific Fields (Land)
    title_deed_no = models.CharField(max_length=100, blank=True, null=True)
    location = models.CharField(max_length=100, blank=True, null=True)
    size = models.CharField(max_length=50, blank=True, null=True, help_text="e.g. 0.05 Ha")
    
    # Specific Fields (Vehicle)
    registration_no = models.CharField(max_length=50, blank=True, null=True)
    chassis_no = models.CharField(max_length=100, blank=True, null=True)
    model = models.CharField(max_length=100, blank=True, null=True)
    
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=150, blank=True, null=True)

    def __str__(self):
        return f"{self.get_collateral_type_display()} - {self.market_value}"

from django.db import models
from customers.models import Customer

from django.db import models
from django.db.models import Index

class RunningLoanStat(models.Model):
    loan_no = models.CharField(max_length=50, unique=True)  # Automatically indexed by Django due to unique=True
    application_date = models.DateField()
    posting_date = models.DateField()
    repayment_start_date = models.DateField()

    last_repayment_date = models.DateTimeField(null=True, blank=True)
    next_repayment_date = models.DateField(null=True, blank=True)
    installments = models.IntegerField()
    repayment_end_date = models.DateField()
    cust_no = models.CharField(max_length=50)
    full_name = models.CharField(max_length=255)
    product_code = models.CharField(max_length=50)
    product_description = models.CharField(max_length=255, null=True)
    approved_amount = models.DecimalField(max_digits=14, decimal_places=2)
    loan_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    monthly_installment = models.DecimalField(max_digits=14, decimal_places=2)
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, default=12.0)
    repayment_method = models.CharField(max_length=50, default="Flat Rate")
    principle_paid = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    principle_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    interest_paid = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    interest_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    defaulted_days = models.IntegerField(default=0)
    total_arrears = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    principle_arrears = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    last_interest_charge = models.DateField(null=True, blank=True)
    interest_arrears = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    defaulted_installments = models.IntegerField(default=0)
    loan_classification = models.CharField(max_length=50)
    sales_person = models.CharField(max_length=100, blank=True, null=True)
    loan_account = models.CharField(max_length=50)
    disbursed = models.BooleanField(default=True)
    created_by = models.CharField(max_length=150)
    last_updated = models.DateTimeField(auto_now=True)
    loan_status = models.CharField(max_length=50, default="Active")

    class Meta:
        verbose_name_plural = "Running Loan Stats"
        # ── PERFORMANCE INDEXES ──────────────────────────────────────
        indexes = [
            # 1. Customer Tracking Index
            # Crucial because cust_no is a plain CharField here, NOT a ForeignKey.
            # Django does NOT auto-index plain text fields.
            models.Index(fields=['cust_no', 'product_code', 'loan_status'], name='idx_run_cust_status'),

            # 2. Billing & Penalty Cron Job Index
            # Speeds up daily interest recalculation and automated SMS/email generation loops
            models.Index(fields=['loan_status', 'next_repayment_date'], name='idx_run_billing_schedule'),

            # 3. Credit Risk & Arrears Reporting Index
            # Optimizes age analysis queries sorting out severe default parameters
            models.Index(fields=['loan_classification','loan_balance', 'total_arrears'], name='idx_run_risk_arrears'),
        ]

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

class InterestChargeBatch(models.Model):
    STATUS_CHOICES = (
        ('draft', 'Draft Preview'),
        ('posted', 'Posted / Finalized'),
    )

    loan_type = models.CharField(max_length=50)
    target_date = models.DateField()
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2)
    calc_method_used = models.CharField(max_length=50, default="reducing_balance")
    total_interest = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='draft')
    
    created_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name="interest_batches")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        # Prevent simultaneous open drafts for the exact same calculation profile
        unique_together = ('loan_type', 'target_date', 'status')

    def __str__(self):
        return f"{self.loan_type.upper()} Batch ({self.target_date}) - {self.get_status_display()}"


class InterestChargeDraftItem(models.Model):
    batch = models.ForeignKey(InterestChargeBatch, on_delete=models.CASCADE, related_name="draft_items")
    loan_id = models.IntegerField()  # References LoanHistory id
    loan_no = models.CharField(max_length=50)
    cust_no = models.CharField(max_length=50)
    customer_name = models.CharField(max_length=150)
    
    approved_amount = models.DecimalField(max_digits=15, decimal_places=2)
    outstanding_balance = models.DecimalField(max_digits=15, decimal_places=2)
    calculated_interest = models.DecimalField(max_digits=15, decimal_places=2)

    def __str__(self):
        return f"{self.loan_no} - Int: {self.calculated_interest}"

# ═══════════════════════════════════════════════════════════════════════════
#  LOAN DEFAULTER HISTORY
# ═══════════════════════════════════════════════════════════════════════════
class LoanDefaulterHistory(models.Model):
    """
    Snapshot table of loans that have ever fallen into arrears.

    Populated by a monthly Django-Q2 job (jobs.snapshot_defaulters). Each
    time a loan is observed in arrears (defaulted_days > threshold), a row
    is written; if the loan is already tracked, the row is refreshed with
    the HIGHEST arrears / days-past-due ever recorded — never overwritten
    downward. This mirrors how running_loans_stat is refreshed, but
    preserves a defaulter's *worst point* for appraisal purposes.

    Consumed by the loan-appraisal engine to answer
    "Has this applicant ever defaulted?" and — if yes — to display the
    last N distinct defaulted loans with the highest arrears observed.
    """
    cust_no        = models.CharField(max_length=50, db_index=True)
    loan_no        = models.CharField(max_length=50, db_index=True)
    product_name   = models.CharField(max_length=150, blank=True, null=True)
    product_code   = models.CharField(max_length=50,  blank=True, null=True)
    first_default_date = models.DateField()
    last_seen_date     = models.DateField()
    loan_arrears       = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    defaulted_days     = models.IntegerField(default=0)
    loan_classification = models.CharField(max_length=50, blank=True, null=True)
    is_resolved        = models.BooleanField(default=False)
    resolved_at        = models.DateTimeField(null=True, blank=True)
    created_at         = models.DateTimeField(auto_now_add=True)
    updated_at         = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Loan Defaulter History"
        # One authoritative row per (customer, loan) — snapshots update in place.
        constraints = [
            models.UniqueConstraint(
                fields=['cust_no', 'loan_no'],
                name='uniq_defaulter_cust_loan',
            ),
        ]
        indexes = [
            models.Index(fields=['cust_no', '-last_seen_date'],
                         name='idx_defaulter_cust_seen'),
            models.Index(fields=['is_resolved', 'defaulted_days'],
                         name='idx_defaulter_open'),
        ]

    def __str__(self):
        return f"{self.cust_no} · {self.loan_no} — {self.defaulted_days}d / {self.loan_arrears}"
