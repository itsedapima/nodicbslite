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
    path('chama-name/', views.chama_name_public_view),

    # ── Dashboard / Overview ──────────────────────────────────────────
    path('dashboard/stats/', views.dashboard_stats_view),
    path('dashboard/recent-transactions/', views.recent_transactions_view),

    # ── Members ───────────────────────────────────────────────────────
    path('members/', views.member_list_view),
    path('members/register/', views.member_register_view),
    path('members/search/', views.member_search_view),
    path('members/<str:cust_no>/unsettled-loans/', views.member_unsettled_loans_view),
    path('members/<str:cust_no>/accounts-list/', views.customer_accounts_list_view),
    path('members/<str:cust_no>/accounts-detail/', views.member_accounts_detail_view),
    path('members/<str:cust_no>/', views.member_detail_view),
    path('members/<str:cust_no>/edit/', views.member_update_view),
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
    path('statements/<str:cust_no>/<str:account_id>/download/', views.statement_download_view),
    path('statements/<str:cust_no>/<str:account_type>/', views.member_account_statement_view),

    # ── Loans Management ──────────────────────────────────────────────
    path('loans/', views.loans_list_view),
    path('loans/pending/', views.loans_pending_view),
    path('loans/<str:loan_no>/approve/', views.loan_approve_view),

    # -- Cash Accounts (payment source) ------------------------------------
    path('cash-accounts/', views.cash_accounts_view),

    # -- Journal Entry -----------------------------------------------------
    path('actions/journal-entry/', views.journal_entry_view),

    # -- Loan Application & Disbursement -----------------------------------
    path('actions/request-loan/', views.loan_application_view),
    path("loans/approved-for-disbursement/", views.approved_loans_list_view),
    path('loans/<str:loan_no>/detail/', views.loan_detail_view),
    path('loans/<str:loan_no>/edit/', views.loan_edit_view),
    path('loans/<str:loan_no>/guarantors/', views.loan_guarantors_view),
    path('loans/<str:loan_no>/guarantors/<int:pk>/', views.loan_guarantor_delete_view),
    path('loans/<str:loan_no>/charges/', views.loan_charges_view),
    path('loans/<str:loan_no>/disburse/', views.loan_disburse_approved_view),

    # -- Inter-Account Transfer --------------------------------------------
    path('actions/inter-account-transfer/', views.inter_account_transfer_view),

    # -- Sacco GL Accounts -------------------------------------------------
    path('sacco-accounts/', views.sacco_accounts_view),

    # -- Password Reset OTP ------------------------------------------------
    path('auth/request-otp/', views.request_otp_view),
    path('auth/verify-otp/', views.verify_otp_view),
    path('auth/reset-password/', views.reset_password_view),

    # -- M-Pesa Integration ------------------------------------------------
    path('actions/mpesa-collection/', views.mpesa_collection_view),
    path('mpesa/stk-callback/', views.mpesa_stk_callback_view),
    path('mpesa/b2c-result/', views.mpesa_b2c_result_view),
    path('mpesa/b2c-timeout/', views.mpesa_b2c_timeout_view),
]
