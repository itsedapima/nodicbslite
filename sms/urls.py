# sms/urls.py

from django.urls import path
from . import views

app_name = 'sms'

urlpatterns = [
    # ── Existing ──────────────────────────────────────────────────────
    path('bulk-sms/', views.bulk_sms, name='bulk_sms'),
    path('view/', views.view_sms, name='view_sms'),
    path('bulk/', views.bulk_sms, name='bulk_sms_alt'),
    path('search-customers/', views.search_customers, name='search_customers'),

    # ── Frequent Notifications (Reminders & Marketing) ────────────────
    path('notifications/', views.frequent_notifications_list, name='frequent_notifications'),
    path('notifications/create/', views.frequent_notification_create, name='frequent_notification_create'),
    path('notifications/<int:pk>/edit/', views.frequent_notification_edit, name='frequent_notification_edit'),
    path('notifications/<int:pk>/toggle/', views.frequent_notification_toggle, name='frequent_notification_toggle'),
    path('notifications/<int:pk>/delete/', views.frequent_notification_delete, name='frequent_notification_delete'),
    path('notifications/<int:pk>/run/', views.frequent_notification_run, name='frequent_notification_run'),
    path('notifications/<int:pk>/preview/', views.frequent_notification_preview, name='frequent_notification_preview'),
]
