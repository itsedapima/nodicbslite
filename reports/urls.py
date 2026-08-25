from django.urls import path

from . import views
from customers.reports import members_listing, member_balances_listing
from transactions.reports import (
    cashier_statement,
    payments_summary_monthly,
    mpesa_payments,
    savings_breakdown_matrix,
)
from loans.reports import (
    loans_register,
    interest_paid_report,
    loan_book,
    mobile_loans_report,
)

app_name = "reports"

urlpatterns = [
    # Dashboard
    path("", views.reports_dashboard, name="reports_dashboard"),

    # Members
    path("members/", members_listing, name="members_listing"),
    path("member-balances/", member_balances_listing, name="member_balances"),

    # Transactions
    path("cashier-statement/", cashier_statement, name="cashier_statement"),
    path("payments-summary/", payments_summary_monthly, name="payments_summary"),
    path("mpesa-payments/", mpesa_payments, name="mpesa_payments"),
    path("savings-matrix/", savings_breakdown_matrix, name="savings_matrix"),

    # Loans
    path("loans-register/", loans_register, name="loans_register"),
    path("interest-income/", interest_paid_report, name="interest_paid"),
    path("loan-book/", loan_book, name="loan_book"),
    path("mobile-loans/", mobile_loans_report, name="mobile_loans"),

    # Export
    path("export-excel/", views.export_to_excel, name="export_to_excel"),
]
