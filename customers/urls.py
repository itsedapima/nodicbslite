from django.urls import path
from . import views,utils

app_name = "customers"

urlpatterns = [
        # 1. URL for the main page that renders the list_customers.html template
    path('', views.list_customers, name='list_customers'),
    # 2. URL for the API endpoint used by JavaScript
    path("customer-search/", utils.customer_search_api, name="customer_search_api"),
    path('search_api/', views.search_customer_api, name='search_customer_api'),
        # ... your existing URL patterns ...
    path('add/', views.add_customer_stepper, name='add_customer_stepper'),
    path('add/<int:step>/', views.add_customer_stepper, name='add_customer_stepper'),
    # Note: Ensure the "add/" URL is at the top of the list
    path("<int:pk>/edit/", views.edit_customer_stepper, name="edit_customer_stepper"),
    path("<int:pk>/edit/<int:step>/", views.edit_customer_stepper, name="edit_customer_stepper"),
    # 3. Organization Registration (Handles 'group' and 'church' dynamically)
    path('register/<str:org_type>/', views.register_organization, name='register_org'),
    path('<int:pk>/edit-org/', views.edit_organization, name='edit_organization'),
    path("search/", views.search_customer, name="search_customer"),
    path("search_customer_api/", views.search_customer_api, name="search_customer_api"),
    path("<int:customer_id>/kins/", views.list_next_of_kin, name="list_next_of_kin"),
    path("<int:customer_id>/kins/add/", views.add_next_of_kin, name="add_next_of_kin"),
    path('next-of-kin/edit/<int:pk>/', views.edit_next_of_kin, name='edit_next_of_kin'),
    path("kins/<int:pk>/edit/", views.edit_next_of_kin, name="edit_next_of_kin"),
    # In your app's urls.py (e.g., customers/urls.py)
    # ... existing paths ...
    path('search/', views.search_customer, name='search_customer_view'),
    path('search_api/', views.search_customer_api, name='search_customer_api'),
    # New profile path: uses the customer ID
    path('<int:customer_id>/profile/', views.customer_profile, name='customer_profile'), 
    path('search-ajax/', views.member_search, name='search_ajax'),
    path('api/search-for-reactivation/', views.member_search_for_reactivation, name='member_search_for_reactivation'),
    path('validate-exit/<int:cust_no>/', views.validate_member_exit, name='validate_exit'),
    path('process-exit/', views.process_member_exit, name='process_exit'),
    path('exit-member/', views.exit_member_page, name='exit_member_page'),
    path('validate-reactivation/<str:cust_no>/', views.validate_member_reactivation, name='validate_member_reactivation'),
    path('process-reactivation/', views.process_member_reactivation, name='process_member_reactivation'),
    path('stats/', views.dashboard_stats, name='dashboard_stats'),
    path('stats/update/', views.trigger_stats_update, name='trigger_update'),
]

