"""approvals/urls.py — Simple maker-checker URLs."""

from django.urls import path
from . import views

app_name = 'approvals'

urlpatterns = [
    path('', views.approval_list, name='list'),
    path('<int:pk>/', views.approval_detail, name='detail'),
    path('<int:pk>/action/', views.approval_action, name='action'),
    path('<int:pk>/cancel/', views.approval_cancel, name='cancel'),
]
