from django.urls import path
from .import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.dashboard_view, name='dashboard'),
    path("user-profile/", views.user_profile, name="user_profile"),
    path('dashboard/savings/', views.savings_list_view, name='savings_list'),
    path('dashboard/loans/', views.loans_list_view, name='loans_list'),
    path('reports/savings-monthly/', views.savings_monthly_report, name='savings_monthly_report'),
    # Add your mpesa_pending_list path here as well
]
 

