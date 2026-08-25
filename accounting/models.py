from django.db import models
from accounts.models import CustomUser  # Import the CustomUser model
from customers.models import Customer
from decimal import Decimal
from django.core.validators import FileExtensionValidator
from django.conf import settings


class SaccoAccount(models.Model):
    account_code = models.CharField(max_length=10, unique=True)
    account_name = models.CharField(max_length=255)
    account_group = models.CharField(max_length=50)  # Income, Expenditure, etc.
    is_cash_account = models.BooleanField(
        default=False,
        help_text="Mark if this is a cash/bank account for cash book tracking",
    )
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='created_sacco_accounts')
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='updated_sacco_accounts')
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        if not self.pk and user:
            self.created_by = user
        if user:
            self.updated_by = user
        # Auto-detect cash accounts
        if not self.pk and self.account_code:
            cash_prefixes = ('900-600', '900-601', '900-602', '900-603')
            if any(self.account_code.startswith(p) for p in cash_prefixes):
                self.is_cash_account = True
        super().save(*args, **kwargs)

    @property
    def normal_balance_side(self):
        """Return 'debit' or 'credit' based on account group."""
        debit_groups = {
            'Income', 'Fixed Asset', 'Fixed Assets',
            'Current Asset', 'Current Assets',
        }
        if self.account_group == 'Expenditure':
            return 'debit'
        if self.account_group in debit_groups:
            return 'debit'
        return 'credit'

    def __str__(self):
        return f"{self.account_code} - {self.account_name}"


class SaccoAccountBalance(models.Model):
    sacco_account = models.OneToOneField(SaccoAccount, on_delete=models.CASCADE, related_name='balance')
    balance = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    def __str__(self):
        return f"{self.sacco_account.account_name} - Balance: {self.balance}"


from django.db import models
from decimal import Decimal
import os

def upload_to(instance, filename):
    return os.path.join('uploads/', filename)

class SaccoIncome(models.Model):
    RECONCILIATION_STATUS = [
        ('unreconciled', 'Unreconciled'),
        ('reconciled', 'Reconciled'),
    ]
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE,blank=True, null=True, related_name='customer_incomes')
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    description = models.TextField()
    sacco_account = models.ForeignKey('SaccoAccount', on_delete=models.CASCADE, related_name='incomes')
    income_date = models.DateTimeField(null=True)
    date_added = models.DateTimeField(auto_now_add=True)
    reconciliation_status = models.CharField(max_length=15, choices=RECONCILIATION_STATUS, default='unreconciled')
    reconciliation_account = models.ForeignKey('SaccoAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='reconciled_incomes')
    reference = models.CharField(max_length=100, blank=True, null=True) 
    cheque_number = models.CharField(max_length=100, blank=True, null=True)  # Cheque number
    income_file = models.FileField(upload_to=upload_to, blank=True, null=True)  # Upload invoice or receipt
    reconciliation_reference = models.CharField(max_length=100, blank=True, null=True)  # Ref for reconciliation
    reconciliation_file = models.FileField(upload_to=upload_to, blank=True, null=True, validators=[
        FileExtensionValidator(allowed_extensions=['pdf', 'jpg', 'jpeg', 'png'])
    ])
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='created_incomes')
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='updated_incomes')
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.amount = Decimal(self.amount) if isinstance(self.amount, float) else self.amount
        
        # Ensure created_by is set only when the instance is new
        if not self.pk and 'user' in kwargs:
            self.created_by = kwargs.pop('user')

        # Ensure updated_by is always set when saving
        if 'user' in kwargs:
            self.updated_by = kwargs.pop('user')

        super().save(*args, **kwargs)
    
    def save(self, *args, **kwargs):
        self.amount = Decimal(self.amount) if isinstance(self.amount, float) else self.amount
        super().save(*args, **kwargs)

        if self.reconciliation_status == 'reconciled' and self.reconciliation_account:
            balance, created = SaccoAccountBalance.objects.get_or_create(sacco_account=self.reconciliation_account)
            balance.balance += self.amount  
            balance.save()

    def __str__(self):
        return f"{self.amount} - {self.sacco_account.account_name} ({self.reconciliation_status})"

class SaccoExpense(models.Model):
    RECONCILIATION_STATUS = [
        ('unreconciled', 'Unreconciled'),
        ('reconciled', 'Reconciled'),
    ]
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, blank=True, null=True,related_name='customer_expenses')
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    description = models.TextField()
    sacco_account = models.ForeignKey('SaccoAccount', on_delete=models.CASCADE, related_name='expenses')
    expense_date = models.DateTimeField(null=True)
    date_added = models.DateTimeField(auto_now_add=True)
    reconciliation_status = models.CharField(max_length=15, choices=RECONCILIATION_STATUS, default='unreconciled')
    reconciliation_account = models.ForeignKey('SaccoAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='reconciled_expenses')
    reference = models.CharField(max_length=100, blank=True, null=True)  
    cheque_number = models.CharField(max_length=100, blank=True, null=True)  # Cheque number
    expense_invoice = models.CharField(max_length=100, blank=True, null=True)  # Invoice number
    expense_file = models.FileField(upload_to=upload_to, blank=True, null=True)  # Upload invoice or receipt
    reconciliation_reference = models.CharField(max_length=100, blank=True, null=True)  # Ref for reconciliation
    reconciliation_file = models.FileField(upload_to=upload_to, blank=True, null=True, validators=[
        FileExtensionValidator(allowed_extensions=['pdf', 'jpg', 'jpeg', 'png']),
    
    ])
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='created_expenses')
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='updated_expenses')
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.amount = Decimal(self.amount) if isinstance(self.amount, float) else self.amount
        
        # Ensure created_by is set only when the instance is new
        if not self.pk and 'user' in kwargs:
            self.created_by = kwargs.pop('user')

        # Ensure updated_by is always set when saving
        if 'user' in kwargs:
            self.updated_by = kwargs.pop('user')

        super().save(*args, **kwargs)
    

    def save(self, *args, **kwargs):
        self.amount = Decimal(self.amount) if isinstance(self.amount, float) else self.amount
        super().save(*args, **kwargs)

        if self.reconciliation_status == 'reconciled' and self.reconciliation_account:
            balance, created = SaccoAccountBalance.objects.get_or_create(sacco_account=self.reconciliation_account)
            balance.balance -= self.amount  
            balance.save()

    def __str__(self):
        return f"{self.amount} - {self.sacco_account.account_name} ({self.reconciliation_status})"




# Ledger Model
class SaccoAccountsLedger(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, blank=True, null=True,related_name='customer_sacco_account_ledgers')
    sacco_account = models.ForeignKey(SaccoAccount, on_delete=models.CASCADE)
    date = models.DateField(auto_now_add=True)
    reference = models.CharField(max_length=100)
    external_reference = models.CharField(max_length=100,null=True)
    description = models.TextField()
    amount = models.DecimalField(max_digits=15, decimal_places=2, null=True)
    debit_amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    credit_amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='created_sacco_accounts_ledgers')
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='updated_sacco_accounts_ledgers')
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.amount = Decimal(self.amount) if isinstance(self.amount, float) else self.amount
        
        # Ensure created_by is set only when the instance is new
        if not self.pk and 'user' in kwargs:
            self.created_by = kwargs.pop('user')

        # Ensure updated_by is always set when saving
        if 'user' in kwargs:
            self.updated_by = kwargs.pop('user')

        super().save(*args, **kwargs)
    def __str__(self):
        return f"Transaction {self.tr_ref} - {self.sacco_account.account_name}"





class AutomatedReport(models.Model):
    name = models.CharField(max_length=255, unique=True)
    day_of_month = models.IntegerField(
    )  # Restrict values to valid days of a month

    def __str__(self):
        return f"{self.name} (Day: {self.day_of_month})"



class AutomatedReportLog(models.Model):
    STATUS_CHOICES = [
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]

    automated_report = models.ForeignKey(AutomatedReport, on_delete=models.CASCADE, related_name="logs")
    sent_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)

    def __str__(self):
        return f"{self.automated_report.name} - {self.status} ({self.sent_at})"
    
from django.db import models

class RegistrationFeeConfig(models.Model):
    """Stores how much each category pays for registration."""
    CATEGORY_CHOICES = [
        ("adult_individual", "Adult-Individual"),
        ("minor_individual", "Minor-Individual"),
        ("group", "Group"),
        ("church", "Church"),
    ]
    category = models.CharField(max_length=40, choices=CATEGORY_CHOICES, unique=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.get_category_display()} - {self.amount}"
    
from django.db import models

class CoreBankingRecord(models.Model):
    date = models.DateField(db_index=True)
    document_no = models.CharField(max_length=100, db_index=True)
    description = models.TextField(blank=True, null=True)
    member_no = models.CharField(max_length=50, blank=True, null=True)
    account = models.CharField(max_length=100)
    # NOTE: As per system specification, debit_amount holds deposits, credit_amount holds charges
    debit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    credit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f"CBS-{self.document_no} ({self.account})"


class MpesaRecord(models.Model):
    receipt_no = models.CharField(max_length=100, unique=True, db_index=True)
    completion_time = models.DateTimeField(db_index=True)
    details = models.TextField(blank=True, null=True)
    credit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    debit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    account = models.CharField(max_length=100, blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-completion_time']

    def __str__(self):
        return f"MPESA-{self.receipt_no}"


# ════════════════════════════════════════════════════════════════════════
#  JOURNAL VOUCHER — multi-line D365-style grid
# ════════════════════════════════════════════════════════════════════════

class JournalVoucher(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('posted', 'Posted'),
        ('rejected', 'Rejected'),
    ]
    voucher_no = models.CharField(max_length=30, unique=True)
    voucher_date = models.DateField()
    description = models.TextField()
    status = models.CharField(
        max_length=15, choices=STATUS_CHOICES, default='draft',
    )
    total_amount = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='journal_vouchers_approved',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True,
        on_delete=models.SET_NULL,
        related_name='journal_vouchers_created',
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.voucher_no

    @property
    def is_balanced(self):
        """True when sum(debits) == sum(credits) across all lines."""
        from django.db.models import Sum
        agg = self.lines.aggregate(dr=Sum('debit_amount'), cr=Sum('credit_amount'))
        dr = agg['dr'] or Decimal('0')
        cr = agg['cr'] or Decimal('0')
        return (dr - cr).copy_abs() <= Decimal('0.01')


class JournalVoucherLine(models.Model):
    ENTRY_TYPE_CHOICES = [
        ('sacco', 'SACCO GL Account'),
        ('customer', 'Customer Member Account'),
    ]
    voucher = models.ForeignKey(
        JournalVoucher, on_delete=models.CASCADE, related_name='lines',
    )
    description = models.CharField(max_length=255, blank=True)
    debit_amount = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
    )
    credit_amount = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
    )
    entry_type = models.CharField(
        max_length=10, choices=ENTRY_TYPE_CHOICES, default='sacco',
    )
    member_account_ref = models.CharField(
        max_length=50, blank=True,
        help_text=(
            "For customer lines: the specific member product account. "
            "Savings/deposits: '<acc_initials>-<cust_no>'. "
            "Loans: the loan_no (e.g. LN000037)."
        ),
    )
    member_loan_no = models.CharField(max_length=40, blank=True)
    customer = models.ForeignKey(
        'customers.Customer', null=True, blank=True,
        on_delete=models.SET_NULL,
    )
    member_product = models.ForeignKey(
        'transactions.CustomerAccountsSetup', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='jv_lines',
    )
    sacco_account = models.ForeignKey(
        SaccoAccount, null=True, blank=True, on_delete=models.PROTECT,
    )

    def __str__(self):
        return f"Line {self.pk} - {self.voucher.voucher_no}"


class JournalVoucherDraft(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('saved', 'Saved'),
        ('posted', 'Posted'),
    ]
    session_key = models.CharField(
        max_length=100, unique=True, db_index=True,
    )
    label = models.CharField(max_length=200, blank=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='draft',
    )
    voucher_date = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True)
    grid_data = models.JSONField(default=list)
    line_count = models.PositiveIntegerField(default=0)
    total_debit = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
    )
    total_credit = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
    )

    class Meta:
        verbose_name = 'Journal Voucher Draft'
        ordering = ['-updated_at']

    def __str__(self):
        return f"Draft {self.session_key}"

    @property
    def is_posted(self):
        return self.status == 'posted'

    @property
    def is_balanced(self):
        return (self.total_debit - self.total_credit).copy_abs() <= Decimal('0.01')
