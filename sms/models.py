"""
sms/models.py
--------------
Notification log tables for SMS and Email channels + Frequent Notifications
(reminders & marketing) engine.

Retry semantics:
  • Each row starts at attempts=0, status='pending'.
  • The queue worker increments `attempts` and stamps `last_attempt_at`
    on every delivery attempt.
  • If delivery fails and attempts < MAX_ATTEMPTS the row stays 'pending'
    for the next cycle (subject to a RETRY_COOLDOWN_HOURS cooldown).
  • Once attempts reaches MAX_ATTEMPTS the row is terminally marked 'failed'.

Celcom Africa fields (SMSLog only):
  • provider_message_id  — the provider's `messageid` returned on 200 OK.
                           Used later for DLR lookups.
  • provider_response_code — Celcom response-code (int); useful for triage.

FrequentNotification system:
  • FrequentNotification — template definitions for reminders & marketing.
  • MemberSnapshot       — lightweight cached snapshot of key member fields
                           rebuilt nightly by Django-Q2. Avoids expensive
                           joins during bulk SMS generation.
"""

from django.db import models
from django.utils import timezone


class _RetryMixin(models.Model):
    """Shared retry fields and logic for SMS / Email log tables."""

    MAX_ATTEMPTS = 3
    RETRY_COOLDOWN_HOURS = 12

    attempts = models.PositiveSmallIntegerField(
        default=0,
        help_text="Number of delivery attempts made so far.",
    )
    last_attempt_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp of the most recent delivery attempt.",
    )

    class Meta:
        abstract = True

    # ── helpers used by the queue workers ─────────────────────────────
    @property
    def can_retry(self) -> bool:
        """True if the row is eligible for another delivery attempt."""
        if self.status != 'pending' or self.attempts >= self.MAX_ATTEMPTS:
            return False
        if self.last_attempt_at is None:
            return True  # never attempted
        cooldown = timezone.timedelta(hours=self.RETRY_COOLDOWN_HOURS)
        return timezone.now() >= self.last_attempt_at + cooldown

    def record_success(self, provider_message_id: str | None = None,
                       provider_response_code: int | None = None):
        """
        Mark this row as delivered. SMSLog uses the provider_* arguments;
        EmailLog ignores them (extra kwargs are simply not persisted).
        """
        self.status = 'sent'
        self.sent_at = timezone.now()
        self.error_message = None
        self.attempts += 1
        self.last_attempt_at = timezone.now()

        update_fields = [
            'status', 'sent_at', 'error_message',
            'attempts', 'last_attempt_at',
        ]

        # Persist provider metadata on models that support it (SMSLog).
        if provider_message_id is not None and hasattr(self, 'provider_message_id'):
            self.provider_message_id = str(provider_message_id)[:64]
            update_fields.append('provider_message_id')
        if provider_response_code is not None and hasattr(self, 'provider_response_code'):
            self.provider_response_code = int(provider_response_code)
            update_fields.append('provider_response_code')

        self.save(update_fields=update_fields)

    def record_failure(self, error: str, provider_response_code: int | None = None):
        self.attempts += 1
        self.last_attempt_at = timezone.now()
        self.error_message = str(error)[:255] if isinstance(self, SMSLog) else str(error)[:1000]
        if self.attempts >= self.MAX_ATTEMPTS:
            self.status = 'failed'
        # else stays 'pending' — will be retried after cooldown

        update_fields = [
            'status', 'error_message', 'attempts', 'last_attempt_at',
        ]
        if provider_response_code is not None and hasattr(self, 'provider_response_code'):
            self.provider_response_code = int(provider_response_code)
            update_fields.append('provider_response_code')

        self.save(update_fields=update_fields)


# ═══════════════════════════════════════════════════════════════════════════
# SMS Log
# ═══════════════════════════════════════════════════════════════════════════

class SMSLog(_RetryMixin):
    SMS_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ]

    phone = models.CharField(max_length=20)
    message = models.TextField()
    status = models.CharField(max_length=10, choices=SMS_STATUS_CHOICES, default='pending')
    error_message = models.CharField(max_length=255, null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=20)

    # ── Celcom Africa metadata ──────────────────────────────────────────
    provider_message_id = models.CharField(
        max_length=64, null=True, blank=True, db_index=True,
        help_text="Provider (Celcom Africa) messageid returned on success. "
                  "Use with the DLR endpoint to query delivery status.",
    )
    provider_response_code = models.IntegerField(
        null=True, blank=True,
        help_text="Provider response-code from the last delivery attempt.",
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'SMS Log'
        verbose_name_plural = 'SMS Logs'

    def __str__(self):
        return f'{self.phone} - {self.created_at}'


# ═══════════════════════════════════════════════════════════════════════════
# Email Log
# ═══════════════════════════════════════════════════════════════════════════

class EmailLog(_RetryMixin):
    EMAIL_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ]

    recipient_to = models.TextField(help_text="Comma-separated list of recipient email addresses.")
    recipient_cc = models.TextField(null=True, blank=True, help_text="Comma-separated list of CC addresses.")
    recipient_bcc = models.TextField(null=True, blank=True, help_text="Comma-separated list of BCC addresses.")
    subject = models.CharField(max_length=255)
    message_body = models.TextField(help_text="Plain text or HTML content of the email.")
    is_html = models.BooleanField(default=False, help_text="Designates whether the body contains HTML markup.")
    status = models.CharField(max_length=10, choices=EMAIL_STATUS_CHOICES, default='pending')
    error_message = models.TextField(null=True, blank=True, help_text="Stores traceback or error logs if sending fails.")
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=50)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Email Log'
        verbose_name_plural = 'Email Logs'

    def __str__(self):
        return f"{self.subject[:30]}… → {self.recipient_to[:30]} ({self.status})"


# ═══════════════════════════════════════════════════════════════════════════
#  FREQUENT NOTIFICATIONS — Template-driven reminders & marketing
# ═══════════════════════════════════════════════════════════════════════════

class FrequentNotification(models.Model):
    """
    A reusable SMS template definition for reminders and marketing.

    Each row defines:
      - WHAT to send (category + message_template with placeholders)
      - WHEN to send (schedule day/time via Django-Q2)
      - WHO to send to (derived from category — the helper function
        selects the correct audience automatically)
      - ON/OFF toggle (is_active) so admins can pause without deleting

    Placeholders available in message_template:
      {first_name}        — customer first name
      {cust_no}           — member number
      {paybill}           — SACCO M-Pesa paybill
      {account_no}        — constructed deposit account number
      {loan_no}           — active loan number (loan categories only)
      {loan_name}         — loan product name
      {loan_balance}      — outstanding loan balance
      {arrears}           — total arrears amount
      {installment}       — monthly installment
      {eligible_amount}   — max eligible amount for the surfaced loan offer
      {loan_offer}        — name of the best-matched loan product on offer
      {offers_list}       — up to 2 offers formatted as
                            "Emergency Loan up to KES 150,000 and
                             Development Loan up to KES 90,000"
      {sacco_name}        — SACCO company name
      {min_balance}       — minimum required balance (Share Capital marketing)
      {balance}           — member's current balance in the target account
    """

    CATEGORY_CHOICES = [
        # ── Reminders ──
        ('savings_deposit_howto',    'Reminder: How to Pay Savings Deposit'),
        ('share_capital_howto',      'Reminder: How to Pay Share Capital'),
        ('loan_repayment_howto',     'Reminder: How to Pay Loan'),
        ('loan_arrears',            'Reminder: Loan Arrears Notice'),
        # ── Marketing ──
        ('loan_eligibility',        'Marketing: Loan Eligibility (best offer from pool)'),
        ('fixed_deposit_marketing', 'Marketing: Fixed Deposit Account'),
        ('share_capital_marketing', 'Marketing: Share Capital Below Minimum'),
        ('dormant_reactivation',    'Marketing: Dormant Account Reactivation'),
        ('happy_birthday',          'Marketing: Happy Birthday'),
        ('happy_holiday',           'Marketing: Happy Holiday'),
        # ── Deprecated (kept for backward compatibility) ──
        ('mobile_loan_eligibility', 'Marketing: Mobile Loan Eligibility (deprecated)'),
        ('normal_loan_eligibility', 'Marketing: Normal Loan Eligibility (deprecated)'),
    ]

    name = models.CharField(
        max_length=100, unique=True,
        help_text="Friendly label shown in admin, e.g. 'Monthly Savings Reminder'.",
    )
    category = models.CharField(
        max_length=40, choices=CATEGORY_CHOICES, db_index=True,
        help_text="Determines the audience and available placeholders.",
    )
    message_template = models.TextField(
        help_text=(
            "SMS body with placeholders: {first_name}, {paybill}, "
            "{account_no}, {loan_no}, {loan_balance}, {arrears}, "
            "{eligible_amount}, {sacco_name}, etc."
        ),
    )
    # ── Balance-based targeting (for marketing categories) ─────────
    balance_filter_min = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text=(
            "Optional: minimum account balance to target. "
            "For Fixed Deposit marketing: target members with savings ≥ this. "
            "For Share Capital marketing: overrides the product's min_balance."
        ),
    )
    balance_filter_max = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="Optional: maximum account balance to target.",
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Uncheck to pause this notification without deleting it.",
    )
    last_run_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp of the last successful generation run.",
    )
    last_run_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of SMS queued during the last run.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'name']
        verbose_name = 'Frequent Notification'
        verbose_name_plural = 'Frequent Notifications'

    def __str__(self):
        status = '✓' if self.is_active else '✗'
        return f"[{status}] {self.name} ({self.get_category_display()})"


# ═══════════════════════════════════════════════════════════════════════════
#  MEMBER SNAPSHOT — Lightweight cache for bulk SMS generation
# ═══════════════════════════════════════════════════════════════════════════

class MemberSnapshot(models.Model):
    """
    Pre-computed snapshot of key member fields needed for personalized
    SMS generation. Rebuilt nightly (or on-demand) by the management
    command `refresh_member_snapshots` and scheduled via Django-Q2.

    This avoids expensive multi-table joins (Customer + SavingsTransaction
    + RunningLoanStat + CustomerAccountsSetup) every time a bulk reminder
    or marketing job fires.

    Fields are intentionally denormalized — they're read-only cache rows,
    not authoritative ledger data.
    """

    cust_no = models.CharField(max_length=20, unique=True, db_index=True)
    first_name = models.CharField(max_length=100, blank=True)
    full_name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    customer_status = models.CharField(max_length=20, default='active')
    dob = models.DateField(null=True, blank=True)

    # ── Savings balances ──────────────────────────────────────────────
    savings_deposit_balance = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
    )
    share_capital_balance = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
    )

    fixed_deposit_balance = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
    )

    # ── Account numbers (constructed from CustomerAccountsSetup) ─────
    savings_deposit_account = models.CharField(max_length=30, blank=True)
    share_capital_account = models.CharField(max_length=30, blank=True)
    fixed_deposit_account = models.CharField(max_length=30, blank=True)

    # ── Active loans (comma-separated if multiple) ───────────────────
    has_active_loan = models.BooleanField(default=False)
    active_loan_nos = models.TextField(
        blank=True,
        help_text="Comma-separated active loan numbers.",
    )
    active_loan_names = models.TextField(
        blank=True,
        help_text="Comma-separated product descriptions.",
    )
    active_loan_balances = models.TextField(
        blank=True,
        help_text="Comma-separated outstanding balances.",
    )
    active_loan_arrears = models.TextField(
        blank=True,
        help_text="Comma-separated arrears amounts.",
    )
    active_loan_installments = models.TextField(
        blank=True,
        help_text="Comma-separated monthly installment amounts.",
    )
    active_loan_accounts = models.TextField(
        blank=True,
        help_text="Comma-separated loan account codes.",
    )
    active_loan_account_types = models.TextField(
        blank=True,
        help_text="Comma-separated account_type of each active loan "
                  "(e.g. normal_loan,mobile_loan). Used to suppress "
                  "cross-selling a product the member already holds.",
    )
    total_arrears = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
    )

    # ── Loan eligibility (pre-computed) ──────────────────────────────
    # Legacy boolean/amount fields kept for backward compatibility and
    # simple filtering. The authoritative source is `eligible_offers`.
    mobile_loan_eligible = models.BooleanField(default=False)
    mobile_loan_max_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
    )
    normal_loan_eligible = models.BooleanField(default=False)
    normal_loan_max_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
    )

    # ── Per-product eligible offers (authoritative) ──────────────────
    # A JSON list of loan products this member qualifies for AND does
    # not already hold, already filtered by the arrears/holding rules
    # at snapshot-build time. Capped to the top offers. Shape:
    #   [{"account_type": "emergency_loan",
    #     "account_code": "L03",
    #     "acc_initials": "EL",
    #     "name": "Emergency Loan",
    #     "is_mobile": false,
    #     "max_amount": "150000.00",
    #     "account_no": "00114EL"}, ...]
    eligible_offers = models.JSONField(
        default=list, blank=True,
        help_text="Loan products the member qualifies for and does NOT "
                  "already hold (top offers only). Authoritative source "
                  "for marketing eligibility SMS.",
    )
    has_any_offer = models.BooleanField(
        default=False, db_index=True,
        help_text="True if eligible_offers is non-empty. Cheap filter "
                  "for the marketing queue.",
    )

    # ── Meta ──────────────────────────────────────────────────────────
    refreshed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['cust_no']
        verbose_name = 'Member Snapshot'
        verbose_name_plural = 'Member Snapshots'
        indexes = [
            models.Index(fields=['customer_status', 'has_active_loan'],
                         name='idx_snap_status_loan'),
            models.Index(fields=['dob'], name='idx_snap_dob'),
            models.Index(fields=['customer_status', 'has_any_offer'],
                         name='idx_snap_offers'),
            models.Index(fields=['mobile_loan_eligible'], name='idx_snap_mobi_elig'),
            models.Index(fields=['normal_loan_eligible'], name='idx_snap_norm_elig'),
        ]

    def __str__(self):
        return f"Snapshot {self.cust_no} — {self.first_name} ({self.customer_status})"

    # ── Convenience: iterate active loans as dicts ────────────────────
    def iter_active_loans(self):
        """Yield dicts of {loan_no, loan_name, balance, arrears, installment, account}."""
        nos = [x.strip() for x in self.active_loan_nos.split(',') if x.strip()]
        names = [x.strip() for x in self.active_loan_names.split(',') if x.strip()]
        bals = [x.strip() for x in self.active_loan_balances.split(',') if x.strip()]
        arrs = [x.strip() for x in self.active_loan_arrears.split(',') if x.strip()]
        insts = [x.strip() for x in self.active_loan_installments.split(',') if x.strip()]
        accts = [x.strip() for x in self.active_loan_accounts.split(',') if x.strip()]
        for i, ln in enumerate(nos):
            yield {
                'loan_no': ln,
                'loan_name': names[i] if i < len(names) else '',
                'balance': bals[i] if i < len(bals) else '0',
                'arrears': arrs[i] if i < len(arrs) else '0',
                'installment': insts[i] if i < len(insts) else '0',
                'account': accts[i] if i < len(accts) else '',
            }
