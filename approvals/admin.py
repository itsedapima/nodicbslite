"""approvals/admin.py — Admin for maker-checker approval requests."""

from django.contrib import admin
from .models import ApprovalRequest


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'action_type', 'maker', 'checker', 'status', 'created_at', 'actioned_at')
    list_filter = ('status', 'action_type')
    search_fields = ('maker__username', 'checker__username', 'maker_note')
    readonly_fields = ('created_at', 'actioned_at', 'payload')
    raw_id_fields = ('maker', 'checker')
    date_hierarchy = 'created_at'
