# statements/urls.py
from django.urls import path
from . import views

app_name = "statements"

urlpatterns = [
    path("", views.panel, name="panel"),                     # UI with search and preview area
    path("customer-search/", views.customer_search_api, name="customer_search_api"),
    path("preview/", views.preview, name="preview"),
    path("download/", views.download, name="download"),
    path("full-statement/schedule/", views.statement_dashboard, name="statement_dashboard"),
    path('full-statement/<int:pk>/', views.full_statement, name='full_statement'),
    path('api/get-customer-accounts/', views.get_customer_accounts_api, name='get_customer_accounts'),
    path('customer/<int:pk>/full/', views.full_statement, name='full_statement'),
    path('customer/<int:pk>/full/pdf/', views.download_full_statement_pdf, name='full_statement_pdf'),
    path('statements/run-manual/', views.trigger_manual_statements, name='trigger_statements'),
    path('verify/<str:hash_value>/', views.verify_statement, name='verify_statement'),
]
