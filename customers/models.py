"""
customers/models.py — Production-ready Customer model
=====================================================
cust_no is a zero-padded CharField that satisfies a DB-level CHECK constraint:
  • Exactly 5 digits   : "00001" to "99999"
  • Or 6-20 digits     : "100000"+ (first digit non-zero)

save() auto-generates the next sequential cust_no with:
  • BigIntegerField cast (no overflow up to 9.2 quintillion)
  • Retry-on-collision  (handles concurrent inserts gracefully)
  • isinstance guard    (survives int/str/None input)
  • Minimum-value check (rejects cust_no "00000")
"""

import logging
from django.conf import settings
from django.db import models, transaction, IntegrityError
from django.db.models import BigIntegerField, Q
from django.db.models.functions import Cast
from administration.models import CompanyBranch

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════
#  CUSTOMER
# ════════════════════════════════════════════════════════════════════════

class Customer(models.Model):
    CUSTOMER_TYPE_CHOICES = [
        ("adult_individual", "Adult-Individual"),
        ("minor_individual", "Minor-Individual"),
        ("group", "Group"),
        ("church", "Church"),
    ]

    CUSTOMER_STATUS_CHOICES = [
        ("active", "Active"),
        ("dormant", "Dormant"),
        ("exited", "Exited"),
        ("deceased", "Deceased"),
        ("suspended", "Suspended"),
    ]

    # ── Auth link ─────────────────────────────────────────────────────
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="customer_profile",
        null=True, blank=True,
    )

    # ── Member number (auto-generated, zero-padded) ──────────────────
    cust_no = models.CharField(
        max_length=20, unique=True, editable=False, blank=True,
        help_text="Auto-generated zero-padded member number (e.g. 00001).",
    )

    # ── Demographics ─────────────────────────────────────────────────
    branch = models.ForeignKey(
        CompanyBranch, on_delete=models.CASCADE,
        blank=True, null=True, related_name="branches",
    )
    customer_type = models.CharField(
        max_length=40, choices=CUSTOMER_TYPE_CHOICES, default="adult_individual",
    )
    full_name = models.CharField(max_length=255)
    first_name = models.CharField(max_length=100, blank=True, null=True)
    middle_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    gender = models.CharField(
        max_length=10, choices=[("Male", "Male"), ("Female", "Female")],
        blank=True, null=True,
    )
    marital_status = models.CharField(max_length=20, choices=[("Married", "Married"), ("Single", "Single"),("Separated", "Separated"),("Un-known", "Un-known")],
                                      blank=True, null=True,
                                      )
    dob = models.DateField(blank=True, null=True)

    # ── Contact ──────────────────────────────────────────────────────
    postal_address = models.CharField(max_length=255, blank=True, null=True)
    postal_code = models.CharField(max_length=10, blank=True, null=True)
    phone = models.CharField(max_length=15, blank=True, null=True)
    town = models.CharField(max_length=100, blank=True, null=True)
    home_address = models.CharField(max_length=255, blank=True, null=True)

    # ── Identity ─────────────────────────────────────────────────────
    national_id = models.CharField(max_length=20, null=True, blank=True)
    kra_pin = models.CharField(max_length=20, blank=True, null=True)

    # ── Registration ─────────────────────────────────────────────────
    reg_date = models.DateField(auto_now_add=True)
    reg_email = models.EmailField(blank=True, null=True)
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="registered_customers",
    )
    reg_fee_is_paid = models.BooleanField(default=False)
    default_notifications_setting = models.BooleanField(default=False)
    temp_notifications_setting = models.BooleanField(blank=True, null=True)
    # ── Status ───────────────────────────────────────────────────────
    is_treasury = models.BooleanField(default=False)
    customer_status = models.CharField(
        max_length=20, choices=CUSTOMER_STATUS_CHOICES, default="active",
    )
    exit_date = models.DateField(null=True, blank=True)
    death_date = models.DateField(null=True, blank=True)
    exit_reason = models.TextField(null=True, blank=True)
    is_reactivated = models.BooleanField(default=False)
    reactivation_date = models.DateField(null=True, blank=True)
    reactivation_reason = models.TextField(null=True, blank=True)
    updated_by = models.CharField(max_length=150, blank=True, null=True)
    created_by = models.CharField(max_length=150, blank=True, null=True)

    class Meta:
        ordering = ['cust_no']
        verbose_name = "Customer"
        verbose_name_plural = "Customers"
        indexes = [
            models.Index(fields=['customer_status'], name='idx_customer_status'),
            models.Index(fields=['phone'], name='idx_customer_phone'),
            models.Index(fields=['national_id'], name='idx_customer_nid'),
            models.Index(fields=['reg_email'], name='idx_customer_email'),
        ]
        constraints = [
            # DB-LEVEL CHECK: cust_no must be exactly 5 digits (00001-99999)
            # OR 6-20 digits starting with non-zero (100000+).
            # This rejects: "", "0", "1000", "abc", "00000" is allowed by
            # regex but blocked by save() logic.
            models.CheckConstraint(
                condition=(
                    Q(cust_no__regex=r'^[0-9]{5}$')
                    | Q(cust_no__regex=r'^[1-9][0-9]{5,19}$')
                ),
                name='strict_cust_no_padding_check',
            ),
        ]

    # ──────────────────────────────────────────────────────────────────
    #  save() — auto-generate cust_no with production-grade safety
    # ──────────────────────────────────────────────────────────────────
    #
    #  1. BigIntegerField cast  → no overflow (supports up to 9.2×10¹⁸)
    #  2. select_for_update()   → serializes concurrent MAX queries
    #  3. Retry on collision    → if two inserts race past the lock,
    #                             the loser retries with a fresh MAX
    #  4. isinstance guard      → handles int / str / None gracefully
    #  5. Minimum-value check   → rejects cust_no 0 ("00000")
    #
    MAX_RETRIES = 5

    def save(self, *args, **kwargs):
        # ── Normalize input type ─────────────────────────────────────
        # Handles: Customer(cust_no=1000), Customer(cust_no="1000"), etc.
        if self.cust_no is not None and not isinstance(self.cust_no, str):
            self.cust_no = str(self.cust_no)

        if not self.cust_no:
            # ── AUTO-GENERATE with retry ─────────────────────────────
            self._auto_generate_cust_no(*args, **kwargs)
        else:
            # ── MANUAL / EXISTING cust_no — enforce padding ──────────
            self._save_with_padding(*args, **kwargs)

    def _auto_generate_cust_no(self, *args, **kwargs):
        """Compute next sequential cust_no and save. Retries on collision."""
        for attempt in range(self.MAX_RETRIES):
            try:
                with transaction.atomic():
                    last = (
                        Customer.objects
                        .select_for_update()
                        .filter(cust_no__regex=r'^[0-9]+$')
                        .annotate(cust_int=Cast('cust_no', output_field=BigIntegerField()))
                        .order_by('-cust_int')
                        .first()
                    )
                    next_number = (last.cust_int + 1) if last else 1

                    # Reject zero — "00000" passes regex but is not a valid member
                    if next_number < 1:
                        next_number = 1

                    self.cust_no = str(next_number).zfill(5)
                    super().save(*args, **kwargs)
                    return  # ← success, exit retry loop

            except IntegrityError as e:
                # Unique constraint violation = another process grabbed this number
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning(
                        f"[Customer.save] cust_no collision on attempt {attempt + 1}, "
                        f"retrying... ({e})"
                    )
                    self.pk = None          # clear PK so Django does INSERT not UPDATE
                    self.cust_no = ''       # reset so next iteration re-computes
                    continue
                else:
                    logger.error(
                        f"[Customer.save] Failed to generate unique cust_no "
                        f"after {self.MAX_RETRIES} attempts."
                    )
                    raise

    def _save_with_padding(self, *args, **kwargs):
        """Save with an explicitly provided cust_no — pad if numeric."""
        if self.cust_no.isdigit():
            numeric_val = int(self.cust_no)
            if numeric_val < 1:
                raise ValueError(
                    f"cust_no must be >= 1, got '{self.cust_no}' (numeric value {numeric_val})."
                )
            self.cust_no = str(numeric_val).zfill(5)
        super().save(*args, **kwargs)

    # ──────────────────────────────────────────────────────────────────
    #  Utility properties
    # ──────────────────────────────────────────────────────────────────

    @property
    def cust_no_int(self):
        """Return the numeric value of cust_no (for transaction tables
        that still use PositiveIntegerField)."""
        try:
            return int(self.cust_no)
        except (ValueError, TypeError):
            return 0

    @property
    def display_name(self):
        """Full name with fallback."""
        parts = [self.first_name, self.middle_name, self.last_name]
        name = ' '.join(p for p in parts if p)
        return name or self.full_name or f"Member #{self.cust_no}"

    def __str__(self):
        return f"{self.cust_no}-{self.full_name}"

    def __repr__(self):
        return f"<Customer {self.cust_no} '{self.full_name}'>"


# ════════════════════════════════════════════════════════════════════════
#  CUSTOMER ECONOMIC ACTIVITY
# ════════════════════════════════════════════════════════════════════════

class CustomerEconomicActivity(models.Model):
    EMPLOYMENT_CHOICES = [
        ("self_employed", "Self-Employed"),
        ("employed", "Employed"),
    ]

    customer = models.OneToOneField(
        Customer, on_delete=models.CASCADE, primary_key=True,
        related_name="economic_activity",
    )
    employment_status = models.CharField(max_length=20, choices=EMPLOYMENT_CHOICES)
    economic_activity = models.CharField(max_length=255, blank=True, null=True)
    profession = models.CharField(max_length=255, blank=True, null=True)
    monthly_income = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        verbose_name = "Customer Economic Activity"
        verbose_name_plural = "Customer Economic Activities"

    def __str__(self):
        return f"Economic Activity for {self.customer.full_name}"


# ════════════════════════════════════════════════════════════════════════
#  NEXT OF KIN
# ════════════════════════════════════════════════════════════════════════

class NextOfKin(models.Model):
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="kins",
    )
    kin_name = models.CharField(max_length=255)
    gender = models.CharField(
        max_length=10, choices=[("Male", "Male"), ("Female", "Female")],
        blank=True, null=True,
    )
    kin_relationship = models.CharField(
        max_length=100, choices=[("Son", "Son"), ("Daughter", "Daughter"),("Parent", "Parent"),("Spouse", "Spouse"),("Other", "Other")],
        )
    kin_dob = models.DateField(blank=True, null=True)
    kin_phone = models.CharField(max_length=15, blank=True, null=True)
    kin_national_id = models.CharField(max_length=20, blank=True, null=True)

    class Meta:
        verbose_name = "Next of Kin"
        verbose_name_plural = "Next of Kin"

    def __str__(self):
        return f"{self.kin_name} ({self.kin_relationship}) — {self.customer.cust_no}"


# ════════════════════════════════════════════════════════════════════════
#  GROUP OFFICIAL
# ════════════════════════════════════════════════════════════════════════

class GroupOfficial(models.Model):
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="groupofficials",
    )
    name = models.CharField(max_length=255)
    designation = models.CharField(max_length=100)
    phone = models.CharField(max_length=15, blank=True, null=True)
    national_id = models.CharField(max_length=20, blank=True, null=True)

    class Meta:
        verbose_name = "Group Official"
        verbose_name_plural = "Group Officials"

    def __str__(self):
        return f"{self.name} ({self.designation}) — {self.customer.cust_no}"


# ════════════════════════════════════════════════════════════════════════
#  CHURCH OFFICIAL
# ════════════════════════════════════════════════════════════════════════

class ChurchOfficial(models.Model):
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="churchofficials",
    )
    name = models.CharField(max_length=255)
    designation = models.CharField(max_length=100)
    phone = models.CharField(max_length=15, blank=True, null=True)
    national_id = models.CharField(max_length=20, blank=True, null=True)

    class Meta:
        verbose_name = "Church Official"
        verbose_name_plural = "Church Officials"

    def __str__(self):
        return f"{self.name} ({self.designation}) — {self.customer.cust_no}"


# ════════════════════════════════════════════════════════════════════════
#  CUSTOMER STATISTICS (cached aggregate)
# ════════════════════════════════════════════════════════════════════════

class CustomerStats(models.Model):
    total_members = models.IntegerField(default=0)
    churches = models.IntegerField(default=0)
    groups = models.IntegerField(default=0)
    male = models.IntegerField(default=0)
    female = models.IntegerField(default=0)
    age_18_35 = models.IntegerField(default=0)
    age_35_50 = models.IntegerField(default=0)
    age_50_60 = models.IntegerField(default=0)
    age_above_70 = models.IntegerField(default=0)
    active = models.IntegerField(default=0)
    dormant = models.IntegerField(default=0)
    deceased = models.IntegerField(default=0)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Customer Statistics"
        verbose_name_plural = "Customer Statistics"

    def __str__(self):
        return f"Stats as of {self.last_updated}: {self.total_members} members"