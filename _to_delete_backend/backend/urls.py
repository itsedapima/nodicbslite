# androidadminapi/urls.py
# ═══════════════════════════════════════════════════════════════════════════
# NODiLite Admin API — URL routing for chama official mobile app
# ═══════════════════════════════════════════════════════════════════════════

from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from . import views

app_name = 'androidadminapi'

urlpatterns = [
    # ── Authentication ────────────────────────────────────────────────
    path('auth/login/', views.admin_login_view),
    path('auth/login/refresh/', TokenRefreshView.as_view()),
    path('auth/logout/', views.admin_logout_view),
    path('auth/user/', views.admin_current_user_view),

    # ── Chama Branding ────────────────────────────────────────────────
    path('chama-info/', views.chama_info_view),

    # ── Dashboard / Overview ──────────────────────────────────────────
    path('dashboard/stats/', views.dashboard_stats_view),
    path('dashboard/recent-transactions/', views.recent_transactions_view),

    # ── Members ───────────────────────────────────────────────────────
    path('members/', views.member_list_view),
    path('members/register/', views.member_register_view),
    path('members/<str:cust_no>/', views.member_detail_view),
    path('members/<str:cust_no>/accounts/', views.member_accounts_view),
    path('members/<str:cust_no>/kins/', views.member_kins_view),
    path('members/<str:cust_no>/loans/', views.member_loans_view),

    # ── Account Types / Products ──────────────────────────────────────
    path('account-types/', views.account_types_view),
    path('account-types/savings/', views.savings_account_types_view),
    path('account-types/loans/', views.loan_account_types_view),

    # ── Transaction Recording (Official actions) ──────────────────────
    path('actions/record-savings/', views.record_savings_payment_view),
    path('actions/record-loan-payment/', views.record_loan_payment_view),
    path('actions/disburse-loan/', views.disburse_loan_view),
    path('actions/record-fine/', views.record_fine_view),

    # ── Transaction Lookup ────────────────────────────────────────────
    path('transactions/savings/', views.savings_transactions_view),
    path('transactions/loans/', views.loan_transactions_view),
    path('transactions/savings/<str:cust_no>/', views.member_savings_transactions_view),
    path('transactions/loans/<str:cust_no>/', views.member_loan_transactions_view),

    # ── Statements ────────────────────────────────────────────────────
    path('statements/<str:cust_no>/full/', views.member_full_statement_view),
    path('statements/<str:cust_no>/<str:account_type>/', views.member_account_statement_view),

    # ── Loans Management ──────────────────────────────────────────────
    path('loans/', views.loans_list_view),
    path('loans/pending/', views.loans_pending_view),
    path('loans/<str:loan_no>/approve/', views.loan_approve_view),
]
