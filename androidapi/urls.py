# api/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from . import views

# Router for ViewSets
router = DefaultRouter()
router.register(r'savings-transactions', views.SavingsTransactionViewSet, 
                basename='savings-transaction')
router.register(r'loan-transactions', views.LoanTransactionViewSet, 
                basename='loan-transaction')
router.register(r'loan-history', views.LoanHistoryViewSet, 
                basename='loan-history')

app_name = 'api'

urlpatterns = [
    # Authentication endpoints
    path('auth/register/', views.register_view, name='register'),
    path('auth/login/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/login/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/logout/', views.logout_view, name='logout'),
    path('auth/user/', views.current_user_view, name='current-user'),
    
    # Customer endpoints
    path('customers/me/', views.customer_profile_view, name='customer-profile'),
    
    # Balance endpoints
    path('balances/summary/', views.balance_summary_view, name='balance-summary'),
    path('balances/savings/', views.savings_balance_view, name='savings-balance'),
    path('balances/loan/', views.loan_balance_view, name='loan-balance'),
    
    # Transaction endpoints (via router)
    path('', include(router.urls)),
]