from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    # path('signup/', views.signup_view, name='signup'),
    path('user/signup/', views.signup_view_contact_admin, name='signup_contact_admin'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('activate/<uidb64>/<token>/', views.activate, name='activate'),

    # Password reset
    path('password_reset/', views.password_reset_request_view, name='password_reset'),
    path('reset/<uidb64>/<token>/', views.password_reset_confirm_view, name='password_reset_confirm'),

    # OTP-only login flow
    path('otp-login/', views.otp_login_request_view, name='otp_login_request'),
    path('otp-login/verify/', views.otp_login_verify_view, name='otp_login_verify'),

    # Admin user management
    path('add_user/', views.add_user, name='add_user'),
    path('view_users/', views.view_users, name='view_users'),
    path('edit_user/<int:user_id>/', views.edit_user, name='edit_user'),
    path('unlock_user/<int:user_id>/', views.unlock_user, name='unlock_user'),
]
