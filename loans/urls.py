from django.urls import path
from . import views
from .views import (
    LoanRestructureView, GuarantorOffloadDefaulterView,
    LoanRestructurePDFView, GuarantorOffloadPDFView,
)

app_name = 'loans'

urlpatterns = [
    path('', views.loan_dashboard, name='loan_dashboard'),
    path('collaterals/edit/<int:pk>/', views.edit_collateral, name='edit_collateral'),
    path('loan-details/<int:pk>/', views.view_loan_details, name='view_loan_details'),
    path('dispatch/', views.loan_dispatch, name='loan_dispatch'),
    path('dispatch/<int:pk>/edit/', views.loan_dispatch, name='edit_loan_dispatch'),
    path('guarantors/', views.view_guarantors, name='view_guarantors'),
    path('guarantors/add/<int:pk>/', views.add_guarantor, name='add_guarantor'),
    path('guarantors/replace/<int:pk>/', views.replace_guarantor, name='replace_guarantor'),
    path('api/guarantor/<int:pk>/update/', views.api_guarantor_update, name='api_guarantor_update'),
    path('api/guarantor/<int:pk>/delete/', views.api_guarantor_delete, name='api_guarantor_delete'),
    path('collaterals/add/<int:pk>/', views.add_collateral, name='add_collateral'),
    path('collaterals/', views.view_collaterals, name='view_collaterals'),
    path('collaterals/view/<int:pk>/', views.view_collateral_details, name='view_collateral_details'),
    path('details/<int:pk>/', views.view_loan_details, name='view_loan_details'),
    path("interest/charge/", views.interest_charge, name="interest_charge"),
     path("interest/charge/stream/<int:batch_id>/", views.interest_charge_stream, name="interest_charge_stream"),
    path('view-appraisal/<int:pk>/', views.view_appraisal, name='view_appraisal'),
    path('api/borrower-info/', views.api_borrower_info, name='api_borrower_info'),
    path('api/unsettled-loans/', views.customer_unsettled_loans, name='customer_unsettled_loans'),
    path('api/guarantor-info/', views.api_guarantor_info, name='api_guarantor_info'),
    path('api/guarantor-metrics/', views.api_guarantor_metrics, name='api_guarantor_metrics'),
    path('api/seach-customer/', views.search_customer_api, name='search_customer_api'),
    path('running-loans/', views.running_loans_dashboard, name='running_loans_dashboard'),
    path('running-loans/update/', views.trigger_running_loans_update, name='trigger_update'),
    path('running-loans/export/', views.export_running_loans_excel, name='export_excel'),
    path('interest/charge-interest/', views.interest_charge, name='interest_charge'),
    path('interest/batches/', views.interest_batch_list, name='interest_batch_list'),
    path('interest/batches/<int:batch_id>/', views.interest_batch_detail, name='interest_batch_detail'),

    path('charges/', views.loan_charge_list, name='loan_charge_list'),
    path('charges/add/', views.loan_charge_add, name='loan_charge_add'),
    path('charges/<int:pk>/edit/', views.loan_charge_edit, name='loan_charge_edit'),

    path('loan-limits/graduate/', views.trigger_loan_limit_graduation, name='trigger_loan_limit_graduation'),
    path('loan-limits/', views.loan_limits_list, name='loan_limits_list'),
    path('loan-limits/update/<int:pk>/', views.update_loan_limit_amount, name='update_loan_limit_amount'),
    path('api/v1/limits/<str:cust_no>/', views.api_get_customer_limit, name='api_customer_limit'),
    # Add this to your loans/urls.py urlpatterns list:
    path('api/product-charges/', views.api_product_charges, name='api_product_charges'),

    path('loan/<int:pk>/amortization/', views.loan_amortization_schedule, name='loan_amortization_schedule'),
    path('loan/<int:pk>/amortization/pdf/', views.loan_amortization_schedule_pdf, name='loan_amortization_pdf'),

    path('loan/<int:pk>/restructure/', LoanRestructureView.as_view(), name='restructure'),
    path('loan/<int:pk>/restructure/pdf/', LoanRestructurePDFView.as_view(), name='restructure_pdf'),
    path('loan/<int:pk>/guarantor-offload/', GuarantorOffloadDefaulterView.as_view(), name='guarantor_offload'),
    path('loan/<int:pk>/guarantor-offload/pdf/', GuarantorOffloadPDFView.as_view(), name='guarantor_offload_pdf'),
]


