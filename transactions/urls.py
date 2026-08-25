from django.urls import path
from . import views, c2bintegrations

app_name = "transactions"

urlpatterns = [
    path('post/', views.post_transaction, name='post_transaction_empty'),
    path('post/<int:pk>/', views.post_transaction, name='post_transaction_with_pk'),

    path('bulk-post/', views.bulk_post_transaction, name='bulk_post_transaction'),
    path('bulk/save/', views.save_bulk_queue, name='save_bulk_queue'),
    path('bulk/post/', views.process_bulk_post, name='process_bulk_post'),
    path("c2b/mpesa/notify/", c2bintegrations.mpesa_integration, name="notify"),
    # ... your existing urls ...
    path("mpesa/notifications/unposted/", views.unposted_notifications, name="unposted_notifications"),
    path("mpesa/notifications/posted/", views.posted_notifications, name="posted_notifications"),
    path("mpesa/notifications/update/<int:pk>/", views.update_notification, name="update_notification"),

    # --- AJAX Endpoints for Customer Lookup ---
    path('api/customers/search/', views.search_customers, name='api_search_customers'),
    path('api/customers/by_cust_no/', views.customer_by_cust_no, name='api_customer_by_cust_no'),
    path('ajax/get-customer-loans/', views.get_customer_loans, name='ajax_get_loans'),
    
    # --- Account Setup Views (as provided in your initial code) ---
    path('accounts/', views.account_list, name='account_list'),
    path('accounts/create/', views.account_create, name='account_create'),
    path('accounts/<int:pk>/update/', views.account_update, name='account_update'),
    path('accounts/<int:pk>/delete/', views.account_delete, name='account_delete'),

    path('transfer/', views.inter_account_transfer, name='inter_account_transfer'),
    path('api/search-customers/', views.search_customer_api, name='search_customer_api'),

    # ... your other urls
    path('api/customers/search/', views.customer_search_ajax, name='customer_search_ajax'),
    path('api/get-accounts/', views.get_customer_accounts_api, name='get_customer_accounts_api'),
    # 1. Initial Form: Select saving_type, rate, and cut-off date
    path('interest/calculate/', views.calculate_interest, name='calculate_interest'),

    # 2. Review Page: Displays the list of members and earned interest before posting
    path('interest/review/<int:batch_id>/', views.interest_review, name='interest_review'),

    # 3. Execution: The "Bulk Update" that posts to Ledger and SavingsTransaction
    #path('interest/post/<int:batch_id>/', views.post_interest_batch, name='post_interest_batch'),

    path('interest/batch/<int:batch_id>/post-unified/', views.post_interest_batch, name='post_interest_batch_unified'),
    path('interest/detail/<int:detail_id>/post-individual/', views.post_interest_individual, name='post_interest_individual'),


    # 4. History: List of all previous interest batches (Posted or Draft)
    path('interest/history/', views.interest_history, name='interest_history'),
    
    # 5. Optional: Delete a draft batch if calculations were wrong
    path('interest/delete/<int:batch_id>/', views.delete_interest_batch, name='delete_interest_batch'),
    path('dividend-slip/search/', views.search_dividend_slip, name='search_dividend_slip'),
path('dividend-slip/preview/<int:detail_id>/', views.preview_dividend_slip, name='preview_dividend_slip'),
path('dividend-slip/download/<int:detail_id>/', views.download_dividend_slip_pdf, name='download_dividend_slip_pdf'),
]


