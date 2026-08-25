from django.contrib import admin
from .models import (
    SaccoAccount, SaccoAccountBalance, SaccoAccountsLedger,
    SaccoIncome, SaccoExpense, RegistrationFeeConfig,
    JournalVoucher, JournalVoucherLine,
)


@admin.register(SaccoAccount)
class SaccoAccountAdmin(admin.ModelAdmin):
    list_display = ('account_code', 'account_name', 'account_group', 'balance_display')
    list_filter = ('account_group',)
    search_fields = ('account_code', 'account_name')  # required for autocomplete_fields
    ordering = ('account_code',)

    def balance_display(self, obj):
        try:
            return f"{obj.balance.balance:,.2f}"
        except Exception:
            return '—'
    balance_display.short_description = 'Balance'


@admin.register(SaccoAccountBalance)
class SaccoAccountBalanceAdmin(admin.ModelAdmin):
    list_display = ('sacco_account', 'balance')
    search_fields = ('sacco_account__account_code', 'sacco_account__account_name')
    readonly_fields = ('sacco_account', 'balance')


@admin.register(SaccoAccountsLedger)
class SaccoAccountsLedgerAdmin(admin.ModelAdmin):
    list_display = ('date', 'reference', 'sacco_account', 'description_short',
                    'debit_amount', 'credit_amount')
    list_filter = ('sacco_account__account_group', 'date')
    search_fields = ('reference', 'external_reference', 'description',
                     'sacco_account__account_code')
    date_hierarchy = 'date'
    readonly_fields = ('date', 'created_by', 'updated_by')

    def description_short(self, obj):
        return (obj.description or '')[:60]
    description_short.short_description = 'Description'

    def has_add_permission(self, request):
        return False  # Ledger rows are created by services, not admin


@admin.register(SaccoIncome)
class SaccoIncomeAdmin(admin.ModelAdmin):
    list_display = ('income_date', 'sacco_account', 'amount', 'reconciliation_status', 'reference')
    list_filter = ('reconciliation_status', 'sacco_account')
    search_fields = ('reference', 'description')


@admin.register(SaccoExpense)
class SaccoExpenseAdmin(admin.ModelAdmin):
    list_display = ('expense_date', 'sacco_account', 'amount', 'reconciliation_status', 'reference')
    list_filter = ('reconciliation_status', 'sacco_account')
    search_fields = ('reference', 'description')


@admin.register(RegistrationFeeConfig)
class RegistrationFeeConfigAdmin(admin.ModelAdmin):
    list_display = ('category', 'amount', 'updated_at')


# ═══════════════════════════════════════════════════════════════════════
#  Journal Vouchers
# ═══════════════════════════════════════════════════════════════════════

class JournalVoucherLineInline(admin.TabularInline):
    model = JournalVoucherLine
    extra = 2
    autocomplete_fields = ('sacco_account', 'customer')


@admin.register(JournalVoucher)
class JournalVoucherAdmin(admin.ModelAdmin):
    list_display = ('voucher_no', 'voucher_date', 'status',
                    'total_amount', 'created_by', 'created_at')
    list_filter = ('status', 'voucher_date')
    search_fields = ('voucher_no', 'description')
    date_hierarchy = 'voucher_date'
    inlines = [JournalVoucherLineInline]
    readonly_fields = ('created_at', 'approved_at', 'posted_at')
