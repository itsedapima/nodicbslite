# sms/admin.py
from django.contrib import admin
from django.utils.html import format_html
from .models import SMSLog, EmailLog, FrequentNotification, MemberSnapshot


@admin.register(SMSLog)
class SMSLogAdmin(admin.ModelAdmin):
    list_display = ('phone', 'status', 'attempts', 'created_by', 'last_attempt_at', 'sent_at', 'created_at')
    list_filter = ('status', 'attempts', 'created_at')
    search_fields = ('phone', 'message', 'created_by')
    readonly_fields = ('created_at', 'attempts', 'last_attempt_at')
    ordering = ('-created_at',)


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = (
        'short_subject', 'short_recipient', 'status', 'attempts',
        'is_html', 'created_by', 'last_attempt_at', 'sent_at', 'created_at',
    )
    list_filter = ('status', 'attempts', 'is_html', 'created_at')
    search_fields = ('recipient_to', 'subject', 'message_body', 'created_by')
    readonly_fields = ('created_at', 'attempts', 'last_attempt_at')
    ordering = ('-created_at',)

    @admin.display(description='Subject')
    def short_subject(self, obj):
        return obj.subject[:40] + '\u2026' if len(obj.subject) > 40 else obj.subject

    @admin.display(description='To')
    def short_recipient(self, obj):
        return obj.recipient_to[:40] + '\u2026' if len(obj.recipient_to) > 40 else obj.recipient_to


# ═══════════════════════════════════════════════════════════════════════════
#  FREQUENT NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════════════════

@admin.register(FrequentNotification)
class FrequentNotificationAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'category_badge', 'is_active_badge',
        'last_run_at', 'last_run_count', 'updated_at',
    )
    list_filter = ('category', 'is_active')
    search_fields = ('name', 'message_template')
    readonly_fields = ('last_run_at', 'last_run_count', 'created_at', 'updated_at')
    fieldsets = (
        (None, {
            'fields': ('name', 'category', 'is_active'),
        }),
        ('Message Template', {
            'fields': ('message_template',),
            'description': (
                '<strong>Available placeholders:</strong> '
                '<code>{first_name}</code>, <code>{cust_no}</code>, '
                '<code>{paybill}</code>, <code>{account_no}</code>, '
                '<code>{loan_no}</code>, <code>{loan_name}</code>, '
                '<code>{loan_balance}</code>, <code>{arrears}</code>, '
                '<code>{installment}</code>, <code>{eligible_amount}</code>, '
                '<code>{sacco_name}</code>'
            ),
        }),
        ('Run History', {
            'fields': ('last_run_at', 'last_run_count', 'created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Category')
    def category_badge(self, obj):
        colors = {
            'savings_deposit_howto':    '#0078D4',
            'share_capital_howto':      '#0078D4',
            'loan_repayment_howto':     '#0078D4',
            'loan_arrears':             '#D13438',
            'loan_eligibility':         '#107C10',
            'mobile_loan_eligibility':  '#107C10',
            'normal_loan_eligibility':  '#107C10',
            'dormant_reactivation':     '#C19C00',
            'happy_birthday':           '#881798',
            'happy_holiday':            '#881798',
        }
        color = colors.get(obj.category, '#605E5C')
        return format_html(
            '<span style="background:{}; color:#fff; padding:2px 8px; '
            'border-radius:3px; font-size:11px;">{}</span>',
            color, obj.get_category_display(),
        )

    @admin.display(description='Active', boolean=True)
    def is_active_badge(self, obj):
        return obj.is_active


# ═══════════════════════════════════════════════════════════════════════════
#  MEMBER SNAPSHOTS (read-only admin view)
# ═══════════════════════════════════════════════════════════════════════════

@admin.register(MemberSnapshot)
class MemberSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        'cust_no', 'first_name', 'phone', 'customer_status',
        'savings_deposit_balance', 'has_active_loan',
        'total_arrears', 'has_any_offer', 'offer_count',
        'refreshed_at',
    )
    list_filter = (
        'customer_status', 'has_active_loan', 'has_any_offer',
        'mobile_loan_eligible', 'normal_loan_eligible',
    )
    search_fields = ('cust_no', 'first_name', 'full_name', 'phone')
    readonly_fields = [f.name for f in MemberSnapshot._meta.get_fields()]
    ordering = ('cust_no',)

    @admin.display(description='Offers')
    def offer_count(self, obj):
        offers = obj.eligible_offers or []
        if not offers:
            return '—'
        names = ', '.join(o.get('name', '?') for o in offers)
        return f"{len(offers)}: {names}"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
