from django.contrib import admin
from django.utils.html import format_html

from .models import (
    SavingsTransaction,
    LoanTransaction,
    BulkUploadQueue,
    BulkTransactionBatch,
    MpesaNotification,
    PostedMpesaNotification,
    QuarantinedMpesaPayload,
    CustomerAccountsSetup,
    DividendBatch,
    DividendDetail,
    DividendDeclaration,
    DividendSlipItem,
    WithdrawalChargeBand,
    TransactionCharge,
)


# ══════════════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════

class ReadOnlyAuditMixin:
    """Audit columns are written by the app, never typed by hand."""

    def save_model(self, request, obj, form, change):
        if not change and hasattr(obj, 'created_by') and not obj.created_by:
            obj.created_by = request.user.username
        super().save_model(request, obj, form, change)


def _money(value):
    return f"{value:,.2f}" if value is not None else "—"


def _pill(text, color):
    return format_html(
        '<span style="background:{}1a;color:{};padding:2px 8px;'
        'border-radius:10px;font-size:11px;font-weight:600;">{}</span>',
        color, color, text,
    )


# ══════════════════════════════════════════════════════════════════════
#  PRODUCT SETUP  (Chart of Products)
# ══════════════════════════════════════════════════════════════════════

@admin.register(CustomerAccountsSetup)
class CustomerAccountsSetupAdmin(admin.ModelAdmin):
    list_display = (
        'account_code', 'account_name', 'acc_initials', 'kind',
        'channels_pill', 'is_withdrawable', 'is_active', 'gl_link',
    )
    list_display_links = ('account_code', 'account_name')
    list_editable = ('is_withdrawable', 'is_active')
    list_filter = (
        'access_on_channels', 'is_active', 'is_loan_account',
        'is_mobile_loan', 'is_withdrawable', 'account_type',
    )
    search_fields = ('account_code', 'account_name', 'acc_initials', 'account_type')
    ordering = ('account_code',)
    list_per_page = 50
    save_on_top = True
    readonly_fields = ('created_at',)
    autocomplete_fields = ('sacco_gl_account', 'sacco_interest_account', 'sacco_cash_account')
    filter_horizontal = ('base_deposits',)

    actions = ('enable_channels', 'disable_channels', 'activate', 'deactivate')

    fieldsets = (
        ('Product Identity', {
            'fields': (
                'account_code', 'account_name', 'acc_initials', 'account_type',
            ),
        }),
        ('Product Configuration', {
            'fields': (
                'is_loan_account', 'is_mobile_loan', 'is_withdrawable',
                'access_on_channels', 'is_active',
                'interest_calc_method', 'min_balance',
                'max_loan_limit', 'max_repayment_period',
            ),
        }),
        ('Eligibility & Guarantee Multipliers', {
            'classes': ('collapse',),
            'description': (
                'Savings products: set <b>loan_multiplier</b> (own borrowing power) '
                'and <b>guarantee_multiplier</b> (how much of another member&rsquo;s loan '
                'this balance can secure).<br>'
                'Loan products: set <b>base_deposits</b> — the savings products a member '
                'must hold to qualify.'
            ),
            'fields': ('guarantee_multiplier', 'loan_multiplier', 'base_deposits'),
        }),
        ('GL Account Linkage', {
            'description': (
                'Link this product to the Chart of Accounts. Every deposit, withdrawal, '
                'disbursement and repayment reads these to post a balanced journal entry.'
            ),
            'fields': (
                'sacco_gl_account', 'sacco_interest_account', 'sacco_cash_account',
            ),
        }),
        ('Audit', {
            'classes': ('collapse',),
            'fields': ('created_at',),
        }),
    )

    # ── computed columns ──────────────────────────────────────────

    @admin.display(description='Kind', ordering='is_loan_account')
    def kind(self, obj):
        if obj.is_loan_account:
            label = 'Mobile Loan' if obj.is_mobile_loan else 'Loan'
            return _pill(label, '#d01b22')
        return _pill('Savings', '#1a4dad')

    @admin.display(description='Channels', ordering='access_on_channels', boolean=False)
    def channels_pill(self, obj):
        if obj.access_on_channels:
            return _pill('Enabled', '#0f7b3d')
        return _pill('Disabled', '#8a8a8a')

    @admin.display(description='GL Linkage')
    def gl_link(self, obj):
        gl = obj.sacco_gl_account.account_code if obj.sacco_gl_account_id else None
        cash = obj.sacco_cash_account.account_code if obj.sacco_cash_account_id else '900-601001*'
        if not gl:
            return _pill('Not linked', '#d01b22')
        return format_html('<code>{}</code> / <code>{}</code>', gl, cash)

    # ── bulk actions ──────────────────────────────────────────────

    @admin.action(description='Enable channel access on selected products')
    def enable_channels(self, request, queryset):
        n = queryset.update(access_on_channels=True)
        self.message_user(request, f"Channel access enabled on {n} product(s).")

    @admin.action(description='Disable channel access on selected products')
    def disable_channels(self, request, queryset):
        n = queryset.update(access_on_channels=False)
        self.message_user(request, f"Channel access disabled on {n} product(s).")

    @admin.action(description='Activate selected products')
    def activate(self, request, queryset):
        n = queryset.update(is_active=True)
        self.message_user(request, f"{n} product(s) activated.")

    @admin.action(description='Deactivate selected products')
    def deactivate(self, request, queryset):
        n = queryset.update(is_active=False)
        self.message_user(request, f"{n} product(s) deactivated.")


# ══════════════════════════════════════════════════════════════════════
#  TRANSACTIONS
# ══════════════════════════════════════════════════════════════════════

@admin.register(SavingsTransaction)
class SavingsTransactionAdmin(ReadOnlyAuditMixin, admin.ModelAdmin):
    list_display = (
        'tr_date', 'cust_no', 'saving_type', 'tr_ref',
        'debit', 'credit', 'leg', 'tr_desc',
    )
    list_filter = ('saving_type', 'tr_date')
    search_fields = ('cust_no', 'tr_ref', 'ext_ref', 'account_code', 'tr_desc')
    date_hierarchy = 'tr_date'
    ordering = ('-tr_date', '-id')
    list_per_page = 50
    list_select_related = True
    readonly_fields = ('created_at', 'created_by')

    fieldsets = (
        ('Identifiers', {'fields': ('cust_no', 'tr_date', 'tr_ref', 'ext_ref')}),
        ('Details', {'fields': ('saving_type', 'account_code', 'tr_desc')}),
        ('Amounts', {'fields': ('debit_amount', 'credit_amount')}),
        ('Audit', {'classes': ('collapse',), 'fields': ('created_by', 'created_at')}),
    )

    @admin.display(description='Debit', ordering='debit_amount')
    def debit(self, obj):
        if obj.debit_amount:
            return format_html('<span style="color:#d01b22;font-weight:600">{}</span>', _money(obj.debit_amount))
        return '—'

    @admin.display(description='Credit', ordering='credit_amount')
    def credit(self, obj):
        if obj.credit_amount:
            return format_html('<span style="color:#0f7b3d;font-weight:600">{}</span>', _money(obj.credit_amount))
        return '—'

    @admin.display(description='Leg')
    def leg(self, obj):
        return _pill('DR', '#d01b22') if obj.debit_amount else _pill('CR', '#0f7b3d')


@admin.register(LoanTransaction)
class LoanTransactionAdmin(ReadOnlyAuditMixin, admin.ModelAdmin):
    list_display = (
        'tr_date', 'cust_no', 'loan_no', 'loan_type', 'tr_ref',
        'debit', 'credit', 'leg',
    )
    list_filter = ('loan_type', 'tr_date')
    search_fields = ('cust_no', 'loan_no', 'tr_ref', 'ext_ref', 'account_code')
    date_hierarchy = 'tr_date'
    ordering = ('-tr_date', '-id')
    list_per_page = 50
    readonly_fields = ('created_at', 'created_by')

    fieldsets = (
        ('Identifiers', {'fields': ('cust_no', 'loan_no', 'loan_id', 'tr_date', 'tr_ref', 'ext_ref')}),
        ('Details', {'fields': ('loan_type', 'account_code', 'tr_desc')}),
        ('Amounts', {'fields': ('debit_amount', 'credit_amount')}),
        ('Audit', {'classes': ('collapse',), 'fields': ('created_by', 'created_at')}),
    )

    @admin.display(description='Debit', ordering='debit_amount')
    def debit(self, obj):
        if obj.debit_amount:
            return format_html('<span style="color:#d01b22;font-weight:600">{}</span>', _money(obj.debit_amount))
        return '—'

    @admin.display(description='Credit', ordering='credit_amount')
    def credit(self, obj):
        if obj.credit_amount:
            return format_html('<span style="color:#0f7b3d;font-weight:600">{}</span>', _money(obj.credit_amount))
        return '—'

    @admin.display(description='Leg')
    def leg(self, obj):
        return _pill('DR', '#d01b22') if obj.debit_amount else _pill('CR', '#0f7b3d')


@admin.register(BulkUploadQueue)
class BulkUploadQueueAdmin(admin.ModelAdmin):
    list_display = (
        'date', 'customer', 'kind', 'account_code', 'loan_no',
        'amount_fmt', 'status_pill', 'created_by', 'created_at',
    )
    list_filter = ('status', 'is_loan', 'date', 'created_at')
    search_fields = ('customer__cust_no', 'loan_no', 'description', 'session_key')
    date_hierarchy = 'date'
    ordering = ('-created_at',)
    list_per_page = 50
    autocomplete_fields = ('customer', 'created_by', 'bank_account')
    readonly_fields = ('created_at', 'processed_at', 'error_message')

    @admin.display(description='Type', ordering='is_loan')
    def kind(self, obj):
        return _pill('Loan', '#d01b22') if obj.is_loan else _pill('Savings', '#1a4dad')

    @admin.display(description='Amount', ordering='amount')
    def amount_fmt(self, obj):
        return _money(obj.amount)

    @admin.display(description='Status', ordering='status')
    def status_pill(self, obj):
        colors = {
            'pending': '#b06f00',
            'processed': '#0f7b3d',
            'failed': '#d01b22',
            'error': '#d01b22',
        }
        value = (obj.status or '').lower()
        return _pill(obj.status or '—', colors.get(value, '#8a8a8a'))


# ══════════════════════════════════════════════════════════════════════
#  M-PESA
# ══════════════════════════════════════════════════════════════════════

@admin.register(MpesaNotification)
class MpesaNotificationAdmin(admin.ModelAdmin):
    list_display = (
        'trans_id', 'trans_time', 'amount_fmt', 'bill_ref_number',
        'msisdn', 'first_name', 'posted_pill',
    )
    list_filter = ('posted', 'transaction_type', 'trans_time', 'received_at')
    search_fields = ('trans_id', 'bill_ref_number', 'msisdn', 'first_name', 'third_party_trans_id')
    date_hierarchy = 'trans_time'
    ordering = ('-received_at',)
    list_per_page = 50
    readonly_fields = ('received_at',)

    actions = ('mark_unposted',)

    @admin.display(description='Amount', ordering='trans_amount')
    def amount_fmt(self, obj):
        return _money(obj.trans_amount)

    @admin.display(description='Posted', ordering='posted')
    def posted_pill(self, obj):
        if obj.posted:
            return _pill('Posted', '#0f7b3d')
        if obj.last_error:
            return _pill('Error', '#d01b22')
        return _pill('Pending', '#b06f00')

    @admin.action(description='Re-queue: mark selected as UNPOSTED')
    def mark_unposted(self, request, queryset):
        n = queryset.update(posted=False, last_error='')
        self.message_user(request, f"{n} notification(s) re-queued for posting.")


@admin.register(PostedMpesaNotification)
class PostedMpesaNotificationAdmin(admin.ModelAdmin):
    list_display = ('mpesa_notification', 'customer_no', 'account_type', 'reconciled_pill', 'posted_at')
    list_filter = ('is_reconciled', 'account_type', 'posted_at')
    search_fields = ('customer_no', 'mpesa_notification__trans_id')
    date_hierarchy = 'posted_at'
    ordering = ('-posted_at',)
    list_per_page = 50
    autocomplete_fields = ('mpesa_notification',)

    @admin.display(description='Reconciled', ordering='is_reconciled')
    def reconciled_pill(self, obj):
        return _pill('Yes', '#0f7b3d') if obj.is_reconciled else _pill('No', '#b06f00')


@admin.register(QuarantinedMpesaPayload)
class QuarantinedMpesaPayloadAdmin(admin.ModelAdmin):
    list_display = (
        'received_at', 'source_ip', 'trans_id', 'amount_fmt',
        'msisdn', 'bill_ref_number', 'review_pill',
    )
    list_filter = ('review_status', 'received_at')
    search_fields = ('source_ip', 'trans_id', 'msisdn', 'bill_ref_number', 'raw_body')
    date_hierarchy = 'received_at'
    ordering = ('-received_at',)
    list_per_page = 50
    readonly_fields = (
        'source_ip', 'received_at', 'raw_body', 'user_agent',
        'xff_header', 'request_path', 'trans_id', 'trans_amount',
        'msisdn', 'bill_ref_number',
    )

    fieldsets = (
        ('Payload Info', {
            'fields': (
                'source_ip', 'received_at', 'request_path',
                'user_agent', 'xff_header',
            ),
        }),
        ('Extracted Fields', {
            'fields': ('trans_id', 'trans_amount', 'msisdn', 'bill_ref_number'),
        }),
        ('Raw Body', {
            'fields': ('raw_body',),
            'classes': ('collapse',),
        }),
        ('Review', {
            'fields': ('review_status', 'reviewed_by', 'reviewed_at', 'review_notes'),
        }),
    )

    actions = ('mark_suspicious', 'mark_legitimate')

    @admin.display(description='Amount')
    def amount_fmt(self, obj):
        return obj.trans_amount or '—'

    @admin.display(description='Status', ordering='review_status')
    def review_pill(self, obj):
        colors_map = {
            'pending': '#b06f00',
            'legitimate': '#0f7b3d',
            'suspicious': '#d01b22',
        }
        return _pill(obj.get_review_status_display(), colors_map.get(obj.review_status, '#666'))

    @admin.action(description='Mark selected as Suspicious')
    def mark_suspicious(self, request, queryset):
        from django.utils.timezone import now
        n = queryset.filter(review_status='pending').update(
            review_status='suspicious',
            reviewed_by=str(request.user),
            reviewed_at=now(),
        )
        self.message_user(request, f"{n} payload(s) marked as suspicious.")

    @admin.action(description='Mark selected as Legitimate')
    def mark_legitimate(self, request, queryset):
        from django.utils.timezone import now
        n = queryset.filter(review_status='pending').update(
            review_status='legitimate',
            reviewed_by=str(request.user),
            reviewed_at=now(),
        )
        self.message_user(request, f"{n} payload(s) marked as legitimate.")


# ══════════════════════════════════════════════════════════════════════
#  BULK TRANSACTION BATCHES
# ══════════════════════════════════════════════════════════════════════

@admin.register(BulkTransactionBatch)
class BulkTransactionBatchAdmin(admin.ModelAdmin):
    list_display = (
        'session_key_short', 'label', 'status_pill', 'row_count',
        'total_fmt', 'created_by', 'updated_at',
    )
    list_filter = ('status', 'created_at')
    search_fields = ('session_key', 'label', 'created_by__username')
    date_hierarchy = 'created_at'
    ordering = ('-updated_at',)
    list_per_page = 50
    readonly_fields = (
        'session_key', 'grid_data', 'row_count', 'total_amount',
        'created_by', 'created_at', 'updated_at', 'posted_at',
    )

    @admin.display(description='Session')
    def session_key_short(self, obj):
        return obj.session_key[:12] + '…' if len(obj.session_key) > 12 else obj.session_key

    @admin.display(description='Total')
    def total_fmt(self, obj):
        return f"KES {obj.total_amount:,.2f}"

    @admin.display(description='Status', ordering='status')
    def status_pill(self, obj):
        colors = {
            'draft': '#b06f00', 'saved': '#0c5460',
            'posted': '#0f7b3d', 'failed': '#d01b22',
        }
        return _pill(obj.get_status_display(), colors.get(obj.status, '#666'))


# ══════════════════════════════════════════════════════════════════════
#  CHARGES
# ══════════════════════════════════════════════════════════════════════

@admin.register(WithdrawalChargeBand)
class WithdrawalChargeBandAdmin(admin.ModelAdmin):
    list_display = ('band', 'charge_fmt', 'is_active', 'updated_at')
    list_editable = ('is_active',)
    list_filter = ('is_active',)
    search_fields = ('from_amount', 'to_amount')
    ordering = ('from_amount',)

    @admin.display(description='Amount Band (KES)', ordering='from_amount')
    def band(self, obj):
        return f"{_money(obj.from_amount)} – {_money(obj.to_amount)}"

    @admin.display(description='Customer Charge', ordering='customer_charge')
    def charge_fmt(self, obj):
        return f"KES {_money(obj.customer_charge)}"


@admin.register(TransactionCharge)
class TransactionChargeAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'charge_method', 'rate_fmt', 'applies_to', 'sacco_account', 'is_active')
    list_editable = ('is_active',)
    list_filter = ('charge_method', 'applies_to', 'is_active')
    search_fields = ('name', 'code', 'description')
    ordering = ('name',)
    autocomplete_fields = ('sacco_account',)

    @admin.display(description='Rate / Value', ordering='rate')
    def rate_fmt(self, obj):
        if obj.charge_method == 'percentage':
            return f"{obj.rate}%"
        return f"KES {_money(obj.rate)}"


# ══════════════════════════════════════════════════════════════════════
#  DIVIDENDS
# ══════════════════════════════════════════════════════════════════════

class DividendDetailInline(admin.TabularInline):
    model = DividendDetail
    extra = 0
    can_delete = False
    fields = (
        'cust_no', 'member_name', 'weighted_avg_balance',
        'gross_interest', 'withholding_tax', 'processing_fee',
        'net_payout', 'is_posted',
    )
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(DividendBatch)
class DividendBatchAdmin(admin.ModelAdmin):
    list_display = (
        'batch_no', 'saving_type', 'cut_off_date', 'share_fmt',
        'net_fmt', 'posted_pill', 'created_by', 'created_at',
    )
    list_filter = ('is_posted', 'saving_type', 'cut_off_date')
    search_fields = ('batch_no', 'saving_type', 'created_by')
    date_hierarchy = 'cut_off_date'
    ordering = ('-created_at',)
    inlines = (DividendDetailInline,)
    readonly_fields = (
        'total_gross', 'total_tax', 'total_fees',
        'total_net_payout', 'created_at', 'created_by',
    )

    fieldsets = (
        ('Batch', {'fields': ('batch_no', 'saving_type', 'cut_off_date', 'is_posted')}),
        ('Declaration', {'fields': ('amount_to_share', 'wht_rate', 'processing_fee')}),
        ('Computed Totals', {
            'description': 'Populated by the dividend engine. Read-only.',
            'fields': ('total_gross', 'total_tax', 'total_fees', 'total_net_payout'),
        }),
        ('Audit', {'classes': ('collapse',), 'fields': ('created_by', 'created_at')}),
    )

    @admin.display(description='To Share', ordering='amount_to_share')
    def share_fmt(self, obj):
        return _money(obj.amount_to_share)

    @admin.display(description='Net Payout', ordering='total_net_payout')
    def net_fmt(self, obj):
        return _money(obj.total_net_payout)

    @admin.display(description='Status', ordering='is_posted')
    def posted_pill(self, obj):
        return _pill('Posted', '#0f7b3d') if obj.is_posted else _pill('Draft', '#b06f00')


@admin.register(DividendDetail)
class DividendDetailAdmin(admin.ModelAdmin):
    list_display = (
        'batch', 'cust_no', 'member_name', 'wab_fmt',
        'gross_fmt', 'net_fmt', 'is_posted',
    )
    list_filter = ('is_posted', 'batch')
    search_fields = ('cust_no', 'member_name', 'batch__batch_no')
    ordering = ('-batch', 'cust_no')
    list_select_related = ('batch',)
    autocomplete_fields = ('batch',)

    @admin.display(description='Weighted Avg Bal', ordering='weighted_avg_balance')
    def wab_fmt(self, obj):
        return _money(obj.weighted_avg_balance)

    @admin.display(description='Gross', ordering='gross_interest')
    def gross_fmt(self, obj):
        return _money(obj.gross_interest)

    @admin.display(description='Net', ordering='net_payout')
    def net_fmt(self, obj):
        return _money(obj.net_payout)


@admin.register(DividendDeclaration)
class DividendDeclarationAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'saving_type', 'start_date', 'end_date',
        'profit_fmt', 'status', 'created_by', 'posted_at',
    )
    list_filter = ('status', 'saving_type', 'start_date')
    search_fields = ('title', 'saving_type', 'created_by')
    date_hierarchy = 'start_date'
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'posted_at', 'created_by')

    @admin.display(description='Total Profit', ordering='total_profit')
    def profit_fmt(self, obj):
        return _money(obj.total_profit)


@admin.register(DividendSlipItem)
class DividendSlipItemAdmin(admin.ModelAdmin):
    list_display = (
        'detail', 'period_date', 'savings_amount',
        'ratio', 'weighted_balance', 'net_interest',
    )
    list_filter = ('period_date',)
    search_fields = ('detail__cust_no', 'detail__member_name')
    date_hierarchy = 'period_date'
    ordering = ('-period_date',)
    list_select_related = ('detail',)
    autocomplete_fields = ('detail',)
