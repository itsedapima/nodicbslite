# loans/admin.py
import logging
from datetime import date
from decimal import Decimal

from django.contrib import admin
from django.forms import ModelForm, ValidationError as FormValidationError
from django.utils.safestring import mark_safe

from .models import (
    Collateral, Guarantor, LoanCharge, LoanChargeRecovery,
    LoanDefaulterHistory, LoanHistory,
)
from transactions.models import CustomerAccountsSetup

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
#  LOAN CHARGE ADMIN
# ══════════════════════════════════════════════════════════════════════

@admin.register(LoanCharge)
class LoanChargeAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'charge_type', 'amount', 'is_mandatory',
        'is_active', 'display_loan_products',
    )
    list_filter = ('charge_type', 'is_mandatory', 'is_active', 'loan_products')
    search_fields = ('name', 'description')
    filter_horizontal = ('loan_products',)

    def display_loan_products(self, obj):
        products = obj.loan_products.all()
        if not products:
            return mark_safe('<span style="color: #999;">None</span>')
        return ", ".join(p.acc_initials for p in products)
    display_loan_products.short_description = "Applied Products"


# ══════════════════════════════════════════════════════════════════════
#  LOAN CHARGE RECOVERY ADMIN  (standalone + inline)
# ══════════════════════════════════════════════════════════════════════

class LoanChargeRecoveryInline(admin.TabularInline):
    """Inline on LoanHistory — view/add charge recoveries."""
    model = LoanChargeRecovery
    extra = 0
    fields = ('charge', 'amount', 'date', 'reference', 'description')
    readonly_fields = ('date', 'reference')

    def get_extra(self, request, obj=None, **kwargs):
        return 0 if obj else 1


@admin.register(LoanChargeRecovery)
class LoanChargeRecoveryAdmin(admin.ModelAdmin):
    """Standalone admin for browsing / filtering all charge recoveries."""
    list_display = (
        'loan', 'charge', 'amount', 'date', 'reference', 'description',
    )
    list_filter = ('charge', 'date')
    search_fields = (
        'loan__loan_no', 'charge__name', 'reference', 'description',
    )
    readonly_fields = ('loan', 'charge', 'amount', 'date', 'reference', 'description')
    list_select_related = ('loan', 'charge')
    ordering = ('-date',)

    def has_add_permission(self, request):
        # Charges are created during loan disbursement or via data imports
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ══════════════════════════════════════════════════════════════════════
#  CUSTOMER ACCOUNTS SETUP  (with LoanCharge inline)
# ══════════════════════════════════════════════════════════════════════

class LoanChargeInline(admin.TabularInline):
    model = LoanCharge.loan_products.through
    extra = 0
    verbose_name = "Associated Loan Charge Setup"
    verbose_name_plural = "Associated Loan Charge Setups"


admin.site.unregister(CustomerAccountsSetup)

@admin.register(CustomerAccountsSetup)
class CustomerAccountsSetupAdmin(admin.ModelAdmin):
    list_display = (
        'account_code', 'account_name', 'account_type',
        'is_loan_account', 'guarantee_multiplier',
        'loan_multiplier', 'is_active',
    )
    list_filter = ('account_type', 'is_loan_account', 'is_active')
    list_editable = ('guarantee_multiplier', 'loan_multiplier')
    search_fields = ('account_code', 'account_name', 'acc_initials')
    filter_horizontal = ('base_deposits',)
    inlines = [LoanChargeInline]

    fieldsets = (
        (None, {
            'fields': (
                'account_code', 'account_name', 'acc_initials',
                'account_type', 'interest_calc_method',
            ),
        }),
        ('Product Flags', {
            'fields': (
                'is_withdrawable', 'is_loan_account', 'is_mobile_loan',
                'is_active',
            ),
        }),
        ('Loan Product Limits', {
            'fields': ('max_loan_limit', 'min_balance', 'max_repayment_period'),
        }),
        ('Multipliers & Eligibility', {
            'description': (
                'For SAVINGS products: set guarantee_multiplier and '
                'loan_multiplier. For LOAN products: select the base_deposits '
                'that qualify a member for this loan.'
            ),
            'fields': (
                'guarantee_multiplier', 'loan_multiplier', 'base_deposits',
            ),
        }),
        ('GL Linkage', {
            'fields': (
                'sacco_gl_account', 'sacco_interest_account',
                'sacco_cash_account',
            ),
        }),
    )


# ══════════════════════════════════════════════════════════════════════
#  LOAN DEFAULTER HISTORY  (read-only audit)
# ══════════════════════════════════════════════════════════════════════

@admin.register(LoanDefaulterHistory)
class LoanDefaulterHistoryAdmin(admin.ModelAdmin):
    list_display = (
        'cust_no', 'loan_no', 'product_name', 'loan_arrears',
        'defaulted_days', 'loan_classification', 'is_resolved',
        'first_default_date', 'last_seen_date',
    )
    list_filter = ('is_resolved', 'loan_classification')
    search_fields = ('cust_no', 'loan_no', 'product_name')
    readonly_fields = (
        'cust_no', 'loan_no', 'product_name', 'product_code',
        'first_default_date', 'last_seen_date', 'loan_arrears',
        'defaulted_days', 'loan_classification', 'is_resolved',
        'resolved_at', 'created_at', 'updated_at',
    )
    ordering = ('-last_seen_date',)


# ══════════════════════════════════════════════════════════════════════
#  FORMS WITH VALIDATION
# ══════════════════════════════════════════════════════════════════════

class GuarantorAdminForm(ModelForm):
    class Meta:
        model = Guarantor
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()
        loan = cleaned_data.get('loan')
        guarantor_cust = cleaned_data.get('guarantor_cust')
        amount = cleaned_data.get('amount')

        if not loan:
            raise FormValidationError("Loan is required.")
        if not guarantor_cust:
            raise FormValidationError("Guarantor customer is required.")
        if not amount:
            raise FormValidationError("Amount is required.")
        if amount <= 0:
            raise FormValidationError("Guarantee amount must be greater than 0.")
        if amount > loan.principal:
            raise FormValidationError(
                f"Guarantee amount ({amount}) cannot exceed loan principal ({loan.principal})."
            )
        if guarantor_cust.cust_no == loan.customer.cust_no:
            raise FormValidationError(
                "Guarantor cannot be the same person as the loan borrower."
            )
        return cleaned_data


class LoanHistoryAdminForm(ModelForm):
    class Meta:
        model = LoanHistory
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()

        if not cleaned_data.get('customer'):
            raise FormValidationError("Customer is required.")
        if not cleaned_data.get('loan_date'):
            raise FormValidationError("Loan date is required.")
        if not cleaned_data.get('principal'):
            raise FormValidationError("Principal amount is required.")
        if not cleaned_data.get('installment'):
            raise FormValidationError("Installment amount is required.")
        if not cleaned_data.get('loan_type'):
            raise FormValidationError("Loan type is required.")
        if cleaned_data.get('loan_period') is None:
            raise FormValidationError("Loan period is required.")

        principal = cleaned_data.get('principal')
        installment = cleaned_data.get('installment')

        if principal <= 0:
            raise FormValidationError("Principal must be greater than 0.")
        if installment <= 0:
            raise FormValidationError("Installment must be greater than 0.")

        loan_period = cleaned_data.get('loan_period')
        total_installments = principal / installment if installment else 0
        if total_installments > loan_period:
            raise FormValidationError(
                f"Installment amount too low. With principal {principal} and "
                f"installment {installment}, you'd need {total_installments:.0f} periods "
                f"but loan period is only {loan_period}."
            )

        interest_rate = cleaned_data.get('interest_rate')
        if interest_rate and (interest_rate < 0 or interest_rate > 100):
            raise FormValidationError("Interest rate must be between 0 and 100.")

        loan_date = cleaned_data.get('loan_date')
        if loan_date and loan_date > date.today():
            raise FormValidationError("Loan date cannot be in the future.")

        is_approved = cleaned_data.get('is_approved')
        if is_approved and not cleaned_data.get('approved_at'):
            raise FormValidationError("Approved date required when loan is approved.")
        if is_approved and not cleaned_data.get('approved_by'):
            raise FormValidationError("Approved by required when loan is approved.")

        is_disbursed = cleaned_data.get('is_disbursed')
        if is_disbursed and not cleaned_data.get('disbursed_at'):
            raise FormValidationError("Disbursal date required when loan is disbursed.")
        if is_disbursed and not is_approved:
            raise FormValidationError("Loan must be approved before disbursement.")

        return cleaned_data


# ══════════════════════════════════════════════════════════════════════
#  INLINES
# ══════════════════════════════════════════════════════════════════════

class GuarantorInline(admin.TabularInline):
    model = Guarantor
    form = GuarantorAdminForm
    extra = 1
    fields = ('guarantor_cust', 'amount')

    def get_extra(self, request, obj=None, **kwargs):
        return 0 if obj else 1


# ══════════════════════════════════════════════════════════════════════
#  LOAN HISTORY ADMIN
# ══════════════════════════════════════════════════════════════════════

@admin.register(LoanHistory)
class LoanHistoryAdmin(admin.ModelAdmin):
    form = LoanHistoryAdminForm
    inlines = [GuarantorInline, LoanChargeRecoveryInline]

    list_display = (
        'loan_no', 'customer', 'principal', 'loan_type',
        'loan_date', 'approval_status', 'disbursal_status',
    )
    list_filter = (
        'is_approved', 'is_disbursed', 'is_restructured',
        'loan_type', 'loan_date', 'created_at',
    )
    search_fields = (
        'loan_no', 'customer__full_name', 'customer__cust_no', 'disbursal_ref',
    )
    readonly_fields = (
        'loan_no', 'disbursal_ref', 'created_at', 'created_by',
        'approved_at', 'disbursed_at', 'restructured_at',
    )

    fieldsets = (
        ('Loan Details', {
            'fields': ('loan_no', 'customer', 'loan_date', 'loan_type', 'loan_period'),
        }),
        ('Financial Terms', {
            'fields': ('principal', 'installment', 'interest_rate', 'net_disbursed'),
        }),
        ('Approval', {
            'fields': ('is_approved', 'approved_at', 'approved_by'),
            'classes': ('collapse',),
        }),
        ('Disbursement', {
            'fields': ('is_disbursed', 'disbursal_ref', 'disbursed_at'),
            'classes': ('collapse',),
        }),
        ('Restructuring', {
            'fields': (
                'is_restructured', 'restructured_at', 'restructure_fee',
                'original_loan_summary',
            ),
            'classes': ('collapse',),
        }),
        ('Audit', {
            'fields': ('created_by', 'created_at'),
            'classes': ('collapse',),
        }),
    )

    def approval_status(self, obj):
        if obj.is_approved:
            return mark_safe(
                '<span style="background-color: #4caf50; color: white; '
                'padding: 3px 8px; border-radius: 3px; font-weight: bold;">'
                '✓ Approved</span>'
            )
        return mark_safe(
            '<span style="background-color: #ff9800; color: white; '
            'padding: 3px 8px; border-radius: 3px; font-weight: bold;">'
            '⏳ Pending</span>'
        )
    approval_status.short_description = 'Approval'

    def disbursal_status(self, obj):
        if obj.is_disbursed:
            return mark_safe(
                '<span style="background-color: #2196f3; color: white; '
                'padding: 3px 8px; border-radius: 3px; font-weight: bold;">'
                '✓ Disbursed</span>'
            )
        return mark_safe(
            '<span style="background-color: #ccc; color: #333; '
            'padding: 3px 8px; border-radius: 3px; font-weight: bold;">'
            '- Not Disbursed</span>'
        )
    disbursal_status.short_description = 'Disbursal'

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user.username
        super().save_model(request, obj, form, change)


# ══════════════════════════════════════════════════════════════════════
#  GUARANTOR ADMIN
# ══════════════════════════════════════════════════════════════════════

@admin.register(Guarantor)
class GuarantorAdmin(admin.ModelAdmin):
    form = GuarantorAdminForm

    list_display = ('loan', 'guarantor_cust', 'amount')
    list_filter = ('loan__loan_date', 'loan__loan_type')
    search_fields = (
        'loan__loan_no', 'guarantor_cust__full_name', 'guarantor_cust__cust_no',
    )

    fieldsets = (
        ('Loan & Guarantor', {
            'fields': ('loan', 'guarantor_cust'),
        }),
        ('Amount', {
            'fields': ('amount',),
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('loan', 'guarantor_cust')
