from django.contrib import admin
from .models import SecurityEvent


@admin.register(SecurityEvent)
class SecurityEventAdmin(admin.ModelAdmin):
    list_display  = ('created_at', 'event', 'severity', 'actor',
                     'ip_address', 'object_ref', 'email_sent')
    list_filter   = ('severity', 'event', 'email_sent', 'created_at')
    search_fields = ('event', 'actor', 'object_ref', 'details', 'ip_address')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):  # Audit rows are system-written
        return False
