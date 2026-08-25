from django.contrib import admin
from django.utils.html import format_html
from .models import ChamaInfo, CompanyBranch, BackupLog, BackupConfiguration, Promotion


@admin.register(ChamaInfo)
class ChamaInfoAdmin(admin.ModelAdmin):
    list_display = ('chama_name', 'chama_contact', 'chama_location', 'created_at')
    search_fields = ('chama_name', 'chama_contact')

    def has_add_permission(self, request):
        # Singleton — allow add only if none exists yet
        return not ChamaInfo.objects.exists()


@admin.register(CompanyBranch)
class CompanyBranchAdmin(admin.ModelAdmin):
    list_display = ('name', 'branch_code', 'is_headquarters', 'is_active', 'city')
    list_filter = ('is_active', 'is_headquarters', 'country')
    search_fields = ('name', 'branch_code', 'city')


@admin.register(BackupLog)
class BackupLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'file_name', 'status', 'file_size')
    list_filter = ('status',)
    readonly_fields = ('timestamp',)


@admin.register(BackupConfiguration)
class BackupConfigurationAdmin(admin.ModelAdmin):
    list_display = ('email_recipient', 'interval_hours', 'is_active', 'last_run')
    list_filter = ('is_active',)


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = ('preview', 'title', 'promo_type', 'is_active', 'display_order', 'start_date', 'end_date')
    list_filter = ('promo_type', 'is_active')
    search_fields = ('title', 'subtitle', 'body')
    list_editable = ('is_active', 'display_order')
    fieldsets = (
        ('Content', {
            'fields': ('title', 'subtitle', 'body', 'image', 'promo_type'),
        }),
        ('Call to Action', {
            'fields': ('cta_label', 'cta_url'),
            'classes': ('collapse',),
        }),
        ('Display Settings', {
            'fields': ('is_active', 'display_order', 'start_date', 'end_date'),
        }),
    )

    def preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="height:38px;border-radius:4px;" />',
                obj.image.url,
            )
        return "—"
    preview.short_description = "Image"
