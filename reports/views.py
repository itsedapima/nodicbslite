"""
reports/views.py
================
Reports dashboard + legacy export endpoint.
Individual report views live in their respective apps:
  - customers/reports.py   (members_listing, member_balances)
  - transactions/reports.py (cashier_statement, payments_summary, mpesa_payments, savings_matrix)
  - loans/reports.py        (loans_register, interest_paid, loan_book, mobile_loans)
URL routing is centralized in reports/urls.py.
"""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from .utils import export_html_table_to_excel


@login_required
def reports_dashboard(request):
    """Landing page linking to all available reports."""
    return render(request, "reports/dashboard.html")


@require_POST
def export_to_excel(request):
    """Receives HTML table content via POST request and returns an Excel file."""
    try:
        data = json.loads(request.body)
        html_content = data.get('table_html')
        file_name = data.get('file_name', 'exported_data')

        if not html_content:
            return HttpResponse("No table content provided.", status=400)

        excel_file = export_html_table_to_excel(html_content, file_name)

        if excel_file:
            response = HttpResponse(
                excel_file,
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            response['Content-Disposition'] = f'attachment; filename="{file_name}.xlsx"'
            return response
        else:
            return HttpResponse("Failed to create Excel file.", status=500)

    except json.JSONDecodeError:
        return HttpResponse("Invalid JSON in request body.", status=400)
    except Exception as e:
        return HttpResponse(f"An unexpected error occurred: {e}", status=500)
