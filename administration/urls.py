from django.urls import path
from . import views

app_name = 'administration'

urlpatterns = [
    path("chama-info/edit/", views.edit_chama_info, name="edit_chama_info"),
    path("chama/details/", views.chama_details, name="chama_details"),
    path("user/profile/", views.user_profile, name="user_profile"),
    path('backups/', views.backup_dashboard, name='backup_dashboard'),
    path('backups/configs/', views.backup_config_list, name='backup_config_list'),
    path('backups/setup/', views.create_backup_settings, name='create_settings'),
    path('backups/settings/<int:pk>/edit/', views.update_backup_settings, name='update_settings'),
    path('backups/run/', views.trigger_manual_backup, name='trigger_backup'),
    path('backups/download/<int:pk>', views.download_backup, name='download_backup'),


    path('branches/', views.branch_list, name='branch_list'),
    path('branches/create/', views.branch_create, name='branch_create'),
    path('branches/<int:pk>/', views.branch_detail, name='branch_detail'),
    path('branches/<int:pk>/edit/', views.branch_update, name='branch_update'),
    path('branches/<int:pk>/delete/', views.branch_delete, name='branch_delete'),

    # ── Promotions (mobile app ads / updates) ──────────────────────────────
    path('promotions/',                        views.promotion_list,   name='promotion_list'),
    path('promotions/create/',                 views.promotion_create, name='promotion_create'),
    path('promotions/<int:pk>/edit/',          views.promotion_update, name='promotion_update'),
    path('promotions/<int:pk>/delete/',        views.promotion_delete, name='promotion_delete'),
    path('promotions/<int:pk>/toggle/',        views.promotion_toggle, name='promotion_toggle'),

    path(
        "notifications/",
        views.notification_management,
        name="notification_management",
    ),

    # ── Mobile Activities (B2C authorization per customer) ─────────────────
    path('mobile-activities/',                         views.mobile_activity_list,   name='mobile_activity_list'),
    path('mobile-activities/create/',                  views.mobile_activity_create, name='mobile_activity_create'),
    path('mobile-activities/search/',                  views.mobile_activity_customer_search, name='mobile_activity_search'),
    path('mobile-activities/<int:pk>/edit/',           views.mobile_activity_update, name='mobile_activity_update'),
    path('mobile-activities/<int:pk>/toggle/',         views.mobile_activity_toggle, name='mobile_activity_toggle'),
    path('mobile-activities/<int:pk>/delete/',         views.mobile_activity_delete, name='mobile_activity_delete'),
    path('mobile-activities/bulk/',                    views.mobile_activity_bulk,   name='mobile_activity_bulk'),

    # ── Bulk transactions ──
    path(
        "bulk-transactions/",
        views.bulk_transaction_upload,
        name="bulk_transaction_upload",
    ),
    path(
        "bulk-transactions/template/",
        views.download_bulk_template,
        name="download_bulk_template",
    ),
    path(
        "bulk-transactions/summary/",
        views.bulk_transaction_summary,
        name="bulk_transaction_summary",
    ),
    path(
        "bulk-transactions/summary/pdf/",
        views.bulk_transaction_summary_pdf,
        name="bulk_transaction_summary_pdf",
    ),
]

