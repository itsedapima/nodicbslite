from django.urls import path
from . import views
from . import jv_views  # NEW — multi-line Journal Voucher views (see note below)

app_name = 'accounting'

urlpatterns = [
    path('ledger_list/', views.ledger_list, name='ledger_list'),
    path('ledger_transactions/<int:ledger_id>/', views.ledger_transactions, name='ledger_transactions'),
    path('accounts/', views.sacco_account_list, name='sacco_account_list'),
    path('accounts/add/', views.sacco_account_create, name='sacco_account_create'),
    path('accounts/edit/<int:pk>/', views.sacco_account_update, name='sacco_account_update'),
    path("sacco-accounts/<int:account_id>/delete/", views.sacco_account_delete, name="sacco_account_delete"),
    path('income/add/', views.income_create, name='income_create'),
    path('expense/add/', views.expense_create, name='expense_create'),
    path('income-expenditure-report/', views.income_expenditure_report, name='income_expenditure_report'),
    path('expense-report/<int:account_id>/', views.individual_expense_report, name='individual_expense_report'),
    path('expense-report/<int:account_id>/download/', views.individual_expense_report_pdf, name='individual_expense_report_pdf'),
    path('expense-accounts/all/', views.expense_accounts_list, name='expense_accounts_list'),
    path('individual-expense-report/view/', views.individual_expense_report_view, name='individual_expense_report_view'),
    path('individual-expense-report/pdf/', views.generate_individual_expense_report_pdf, name='generate_individual_expense_report_pdf'),

    # ── Retained from nodicbslite (not present in nodicbs's urls.py) ──
    path("reconciliation-report/filter/", views.reconciliation_report_filter, name="reconciliation_report_filter"),
    path('reconciliation-report/', views.reconciliation_report, name='reconciliation_report'),
    path('reconciliation-report/pdf/', views.reconciliation_report_pdf, name='reconciliation_report_pdf'),
    path('reconciliation-report/pdf/<int:account_id>/', views.reconciliation_report_pdf, name='reconciliation_report_pdf'),
    path('reconciliation-report/pdf/<int:account_id>/<str:start_date>/<str:end_date>/', views.reconciliation_report_pdf, name='reconciliation_report_pdf'),

    path('incomes/', views.income_list, name='income_list'),
    path('expenses/', views.expense_list, name='expense_list'),
    path('income/<int:income_id>/', views.view_income, name='view_income'),
    path('expense/<int:expense_id>/', views.view_expense, name='view_expense'),
    path('incomes/edit/<int:income_id>/', views.edit_income, name='edit_income'),
    path('expenses/edit/<int:expense_id>/', views.edit_expense, name='edit_expense'),
    path('incomes/reconcile/<int:income_id>/', views.reconcile_income, name='reconcile_income'),
    path('expenses/reconcile/<int:expense_id>/', views.reconcile_expense, name='reconcile_expense'),
    path("income-report/all/", views.income_report_view, name="income_report_all"),
    path("expense-report/all/", views.expense_report_view, name="expense_report_all"),

    # Export routes
    path("income-report/pdf/", views.export_income_pdf, name="export_income_pdf"),
    path("expense-report/pdf/", views.export_expense_pdf, name="export_expense_pdf"),
    path("income-report/excel/", views.export_income_excel, name="export_income_excel"),
    path("expense-report/excel/", views.export_expense_excel, name="export_expense_excel"),
    path("income-expenditure-summary/", views.income_expenditure_summary, name="income_expenditure_summary"),
    path("income-expenditure-summary/pdf/", views.download_income_expenditure_summary_pdf, name="download_income_expenditure_summary_pdf"),
    path("income-expenditure-summary/excel/", views.download_income_expenditure_summary_excel, name="download_income_expenditure_summary_excel"),

    # ── Retained from nodicbslite (not present in nodicbs's urls.py) ──
    path('automated-reports/', views.automated_reports_list, name='automated_reports_list'),
    path('automated-reports/create/', views.automated_report_create, name='automated_report_create'),
    path('automated-reports/delete/<int:report_id>/', views.automated_report_delete, name='automated_report_delete'),
    path('automated-reports/edit/<int:report_id>/', views.automated_report_edit, name='automated_report_edit'),

    path('customer-search-ajax/', views.customer_search_list_ajax, name='customer_search_list_ajax'),
    # NOTE: legacy single-line 'journal-voucher/add/' route removed in nodicbs in favour
    # of the flexible multi-line jv/* routes below — mirrored here.

    # ─── Flexible Journal Voucher (multi-line, D365 grid) ────────────
    # Uses jv_service.py for validation/posting (GL + subledger) and
    # jv_pdf.py (reportlab) for PDF generation. Synced from nodicbs.
    path('jv/',                        jv_views.jv_list,              name='jv_list'),
    path('jv/new/',                    jv_views.jv_new,               name='jv_new'),
    path('jv/validate/',               jv_views.jv_validate,          name='jv_validate'),
    path('jv/post/',                   jv_views.jv_post,              name='jv_post'),
    path('jv/upload/',                 jv_views.jv_upload,            name='jv_upload'),
    path('jv/template.csv',            jv_views.jv_template,          name='jv_template'),
    path('jv/lookup/customer/',        jv_views.jv_lookup_customer,   name='jv_lookup_customer'),
    path('jv/lookup/sacco/',           jv_views.jv_lookup_sacco,      name='jv_lookup_sacco'),
    path('jv/autosave/',               jv_views.jv_autosave,          name='jv_autosave'),
    path('jv/drafts/',                 jv_views.jv_draft_history,     name='jv_draft_history'),
    path('jv/drafts/<str:session_key>/load/', jv_views.jv_draft_load, name='jv_draft_load'),
    path('jv/drafts/<str:session_key>/delete/', jv_views.jv_draft_delete, name='jv_draft_delete'),
    path('jv/<int:pk>/',               jv_views.jv_detail,            name='jv_detail'),
    path('jv/<int:pk>/pdf/',           jv_views.jv_pdf_view,          name='jv_pdf'),

    path('api/get-customer-accounts/', views.get_customer_accounts_api, name='get_customer_accounts_api'),
    path('reg-fee/config/', views.reg_fee_config, name='reg_fee_config'),
    path('reg-fee/recover/', views.recover_registration_fees, name='recover_registration_fees'),

    # ─── Bankers cheques / financial-profile APIs ──────────────────
    # TODO: Implement these view functions in views.py when needed.
    # path('bankers-cheques/create/', views.bankers_cheque_create, name='bankers_cheque_create'),
    # path('api/v1/customer-financial-profile/', views.get_customer_financial_profile_api, name='api_customer_profile'),
    # path('api/v1/estimate-charges/', views.estimate_withdrawal_charges_api, name='api_estimate_charges'),

    # ── Paginated Statement Logs Browser ──────────────────────────────
    path('reconciliation/mpesa/uploads/', views.view_uploaded_records, name='view_uploaded_records'),

    # ──────────────────────────────────────────────────────────────
    # M-Pesa Reconciliation
    # Existing lite routes kept; extended nodicbs routes commented out
    # until view functions are implemented in views.py.
    # ──────────────────────────────────────────────────────────────
    path('mpesa-reconciliation/',                    views.mpesa_reconciliation,       name='mpesa_reconciliation'),
    path('mpesa-reconciliation/push/',              views.mpesa_reconciliation_push,  name='mpesa_reconciliation_push'),

    # Retained legacy export route from nodicbslite for backwards compatibility.
    path('reconciliation/mpesa/export/', views.export_unmatched_excel, name='export_unmatched_excel'),
]
