import io
from io import BytesIO
import pandas as pd
import openpyxl
from itertools import zip_longest
from decimal import Decimal
from datetime import datetime
from reportlab.lib.styles import getSampleStyleSheet
from transactions.utils import make_tr_ref

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.utils.timezone import make_aware, is_aware, make_naive
from django.utils.dateparse import parse_date
from django.template.loader import render_to_string
from django.db.models import Sum, Q

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.utils import simpleSplit
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
)
from reportlab.pdfgen import canvas

from xhtml2pdf import pisa

# Models
from .models import SaccoAccount, SaccoAccountsLedger,SaccoIncome, SaccoExpense, SaccoAccountBalance

from accounting.models import SaccoIncome, SaccoExpense
from administration.models import ChamaInfo
from accounts.models import CustomUser

# Forms
from .forms import (
    SaccoAccountForm, IncomeForm, ExpenseForm, IncomeReconciliationForm,
    ExpenseReconciliationForm
)
from administration.utils import add_company_info
from accounts.models import CustomUser

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render, redirect
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
# Make sure to import your SaccoAccountsLedger model
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render, redirect
from .models import SaccoIncome, SaccoExpense, SaccoAccountsLedger
from .forms import IncomeForm, ExpenseForm

def draw_page_header(canvas, doc, title):
    """ Draws the header on each PDF page """
    canvas.saveState()  # Ensure state is maintained across pages
    width, height = landscape(A4)

    # Get sacco Info
    company = ChamaInfo.objects.first()
    company_name = company.company_name if company else "Your Organization"
    company_address = company.company_address if company else "sacco Address"
    company_contact = company.company_contact if company else "sacco Contact"

    # Draw sacco Info
    canvas.setFont("Helvetica-Bold", 14)
    canvas.drawCentredString(width / 2, height - 40, company_name)
    canvas.setFont("Helvetica", 10)
    canvas.drawCentredString(width / 2, height - 55, company_address)
    canvas.drawCentredString(width / 2, height - 70, f"Contact: {company_contact}")

    # Report Title (Group or Congregation)
    canvas.setFont("Helvetica-Bold", 12)
    canvas.drawCentredString(width / 2, height - 90, title)

    # Draw a line under the header
    canvas.line(50, height - 100, width - 50, height - 100)
    canvas.restoreState()  # Restore state after drawing


@login_required
def sacco_account_list(request):
    if not request.user.is_superuser:
        messages.warning(request, "You don't have permission to view accounts.")
        return redirect('dashboard:dashboard')
    
    # Use select_related to fetch the OneToOne balance in the same query
    accounts = SaccoAccount.objects.select_related('balance').all().order_by('account_code')
    
    # Optional: Calculate total balance for a summary card
    total_balance = sum(acc.balance.balance for acc in accounts if hasattr(acc, 'balance'))

    return render(request, 'accounting/sacco_account_list.html', {
        'accounts': accounts,
        'total_balance': total_balance
    })

@login_required
def sacco_account_create(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    if request.method == 'POST':
        form = SaccoAccountForm(request.POST)
        if form.is_valid():
            account = form.save(commit=False)
            account.created_by = request.user
            account.save()
            messages.success(request, "sacco Account created successfully!")
            return redirect('accounting:sacco_account_list')
    else:
        form = SaccoAccountForm()
    return render(request, 'accounting/sacco_account_form.html', {'form': form})

@login_required
def sacco_account_update(request, pk):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    account = get_object_or_404(SaccoAccount, pk=pk)
    if request.method == 'POST':
        form = SaccoAccountForm(request.POST, instance=account)
        if form.is_valid():
            account = form.save(commit=False)
            account.updated_by = request.user
            account.save()
            messages.success(request, "sacco Account updated successfully!")
            return redirect('accounting:sacco_account_list')
    else:
        form = SaccoAccountForm(instance=account)
    return render(request, 'accounting/sacco_account_form.html', {'form': form})

@login_required
def sacco_account_delete(request, account_id):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    account = get_object_or_404(SaccoAccount, id=account_id)
    if request.method == "POST":
        account.delete()
        messages.success(request, "sacco account deleted successfully.")
        return redirect("accounting:sacco_account_list")
    return render(request, "accounting/sacco_account_confirm_delete.html", {"account": account})


@login_required
def income_create(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to access this section.")
        return redirect('administration:administration')
    
    if request.method == 'POST':
        form = IncomeForm(request.POST, request.FILES)
        if form.is_valid():
            income = form.save(commit=False)
            income.created_by = request.user
            income.save()
            # Add entry to saccoAccountsLedger
            SaccoAccountsLedger.objects.create(
                sacco_account=income.sacco_account,
                reference="Other Income",
                description=income.description,
                credit_amount=income.amount,
                amount=income.amount,
                created_by=request.user
            )

            messages.success(request, "Income recorded successfully!")
            return redirect('accounting:income_list')
    else:
        form = IncomeForm()

    return render(request, 'accounting/income_form.html', {'form': form})

@login_required
def expense_create(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to access this section.")
        return redirect('administration:administration')
    
    if request.method == 'POST':
        form = ExpenseForm(request.POST, request.FILES)
        if form.is_valid():
            expense = form.save(commit=False)
            expense.created_by = request.user
            expense.save()
      
            # Add entry to saccoAccountsLedger
            SaccoAccountsLedger.objects.create(
                sacco_account=expense.sacco_account,
                reference="Expense",
                description=expense.description,
                debit_amount=expense.amount,
                amount=expense.amount,
                created_by=request.user
            )

            messages.success(request, "Expense recorded successfully!")
            return redirect('accounting:expense_list')
    else:
        form = ExpenseForm()

    return render(request, 'accounting/expense_form.html', {'form': form})

@login_required
def edit_income(request, income_id):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    income = get_object_or_404(SaccoIncome, id=income_id)
    if request.method == "POST":
        form = IncomeForm(request.POST, request.FILES, instance=income)
        if form.is_valid():
            income = form.save(commit=False)
            income.updated_by = request.user
            income.save()
            messages.success(request, "Income successfully updated.")
            return redirect('accounting:income_list')
    else:
        form = IncomeForm(instance=income)
    return render(request, 'accounting/edit_income.html', {'form': form, 'title': "Income"})

@login_required
def edit_expense(request, expense_id):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    expense = get_object_or_404(SaccoExpense, id=expense_id)
    if request.method == "POST":
        form = ExpenseForm(request.POST, request.FILES, instance=expense)
        if form.is_valid():
            expense = form.save(commit=False)
            expense.updated_by = request.user
            expense.save()
            messages.success(request, "Expense successfully updated.")
            return redirect('accounting:expense_list')
    else:
        form = ExpenseForm(instance=expense)
    return render(request, 'accounting/edit_expense.html', {'form': form, 'title': "Expense"})


@login_required
def ledger_list(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to this section.")
        return redirect('administration:administration')

    # .select_related optimization prevents N+1 query performance bottleneck
    qs = SaccoAccountsLedger.objects.all().select_related('sacco_account').order_by('-id')

    # Date filtering
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    if start_date:
        qs = qs.filter(date__gte=start_date)
    if end_date:
        qs = qs.filter(date__lte=end_date)

    # Account filtering
    account_id = request.GET.get('account')
    if account_id:
        qs = qs.filter(sacco_account_id=account_id)

    # Show 50 entries per page
    paginator = Paginator(qs, 50)
    page = request.GET.get('page')

    try:
        ledger_entries = paginator.page(page)
    except PageNotAnInteger:
        ledger_entries = paginator.page(1)
    except EmptyPage:
        ledger_entries = paginator.page(paginator.num_pages)

    return render(request, 'accounting/ledgerlist.html', {'ledger_entries': ledger_entries})

@login_required
def ledger_transactions(request, ledger_id):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    ledger = get_object_or_404(SaccoAccount, id=ledger_id)
    transactions = SaccoAccountsLedger.objects.filter(sacco_account=ledger)
    return render(request, 'accounting/ledger_transactions.html', {'ledger': ledger, 'transactions': transactions})


def make_naive(dt):
    """ Convert timezone-aware datetime to naive (timezone-unaware). """
    if dt and is_aware(dt):  
        return dt.replace(tzinfo=None)  
    return dt  # If already naive, return as is
@login_required
def income_expenditure_report(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')

    # Get filter parameters
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    export_format = request.GET.get('export')  # PDF or Excel
    show_filtered = request.GET.get('filter')  # Detect if filter button was clicked

    # Convert to datetime and make timezone-aware
    if start_date:
        start_date = make_aware(datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        end_date = make_aware(datetime.strptime(end_date, "%Y-%m-%d"))

    # Query data
    if start_date and end_date:
        incomes = list(SaccoIncome.objects.filter(income_date__range=[start_date, end_date]))
        expenses = list(SaccoExpense.objects.filter(expense_date__range=[start_date, end_date]))
    else:
        incomes = list(SaccoIncome.objects.all().order_by('-id')[:100])
        expenses = list(SaccoExpense.objects.all().order_by('-id')[:100])

    # Calculate totals safely
    total_income = sum(i.amount for i in incomes if i.amount is not None)
    total_expenses = sum(e.amount for e in expenses if e.amount is not None)
    net_balance = total_income - total_expenses  

    user = request.user

    context = {
        'incomes': incomes,
        'expenses': expenses,
        "total_income": total_income,
        "total_expenses": total_expenses,
        'net_balance': net_balance,
        'start_date': start_date,
        'end_date': end_date,
        'user': user,
    }

    # Handle PDF export (Exclude placeholders)
    if export_format == 'pdf':
        pdf_context = {
            'incomes': [i for i in incomes if i.amount != Decimal("0.00")],  # Remove empty records
            'expenses': [e for e in expenses if e.amount != Decimal("0.00")],  # Remove empty records
            "total_income": total_income,
            "total_expenses": total_expenses,
            'net_balance': net_balance,
            'start_date': start_date,
            'end_date': end_date,
            'user': user,
        }
        return export_income_expenditure_pdf(request, pdf_context)

    # Handle Excel export (Keep all records)
    if export_format == 'excel':
        return export_income_expenditure_excel(request, context)

    # If "Filter" was clicked, render the PDF-styled template with filtered data
    if show_filtered:
        return render(request, 'accounting/income_expenditure_report_pdf.html', context)

    # Render normal template
    return render(request, 'accounting/income_expenditure_report.html', context)

from datetime import datetime
from django.utils import timezone

# Ensure all dates are timezone-aware and replace None values
def safe_datetime(date_value):
    if date_value is None:
        return timezone.make_aware(datetime(1900, 1, 1))  # Default fallback
    if timezone.is_naive(date_value):  # Convert naive datetime to aware
        return timezone.make_aware(date_value)
    return date_value  # Already aware, return as-is



@login_required
def export_income_expenditure_pdf(request, context):
    """
    Generate a PDF report for Income & Expenditure, ensuring transactions are listed by date 
    and incomes/expenses appear on the same row when they occur on the same day.
    """
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=40, rightMargin=40, topMargin=50, bottomMargin=30)
    
    width, height = landscape(A4)
    elements = []  

    add_sacco_info(elements, width)
    
    # Title and Date Range
    title = "Income & Expenditure Report"
    date_range = (f"From: {context['start_date'].strftime('%B %d, %Y')} "
                  f"To: {context['end_date'].strftime('%B %d, %Y')}") if context.get("start_date") and context.get("end_date") else ""

    elements.append(Table([[title]], colWidths=[width - 80], style=[
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        
    ]))

    if date_range:
        elements.append(Table([[date_range]], colWidths=[width - 80], style=[
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 15),
        ]))
    
    # Table Headers
    table_data = [["Date", "Account", "Description", "Debit (KES)", "Credit (KES)"]]
    col_widths = [90, 150, 320, 85, 85]  

    # Merge incomes and expenses into a single list and sort by date
    transactions = []
    for income in context["incomes"]:
        transactions.append({
            "date": income.income_date,
            "account": income.sacco_account if hasattr(income, 'sacco_account') else "Other Income",
            "description": income.description,
            "credit": income.amount,
            "debit": None
        })
    for expense in context["expenses"]:
        transactions.append({
            "date": expense.expense_date,
            "account": expense.sacco_account if hasattr(expense, 'sacco_account') else "Other Expenses",
            "description": expense.description,
            "credit": None,
            "debit": expense.amount
        })
    



      # Sort transactions by date
    transactions.sort(key=lambda x: safe_datetime(x["date"]))



    # Wrap text function
    def wrap_text(text, max_width):
        return "\n".join(simpleSplit(text, "Helvetica", 10, max_width)) if text else "N/A"

    # Fill table data
    for entry in transactions:
        table_data.append([
            entry["date"].strftime('%b. %d, %Y') if entry["date"] else "-",
            entry["account"],
            wrap_text(entry["description"], 240),
            f"{entry['debit']:,.2f}" if entry["debit"] else "-",
            f"{entry['credit']:,.2f}" if entry["credit"] else "-"
        ])

    # Add Total and Net Income rows
    table_data.append(["", "", "Total", f"{context['total_expenses']:,.2f}", f"{context['total_income']:,.2f}"])
    table_data.append(["", "", "Net Income", "", f"{context['net_balance']:,.2f}"])

    # Create Table
    table = Table(table_data, colWidths=col_widths, repeatRows=1)

    # Style Table
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -2), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -2), (-1, -1), colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))

    elements.append(table)
    doc.build(elements)
    
    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"income_expenditure_report_{timestamp}.pdf"
    buffer.seek(0)
    response = HttpResponse(buffer, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response




def make_naive(dt):
    """Convert timezone-aware datetime to naive (timezone-unaware)."""
    if dt and is_aware(dt):  
        return dt.replace(tzinfo=None)  
    return dt  # If already naive, return as is

@login_required
def export_income_expenditure_excel(request, context):
    """
    Convert the already rendered HTML data into an Excel file, sorted by date.
    """
    # Collect transactions from both incomes and expenses
    transactions = []

    for income in context['incomes']:
        transactions.append({
            "description": income.description,
            "amount": income.amount,
            "date": make_naive(income.income_date),
            "type": "income"
        })

    for expense in context['expenses']:
        transactions.append({
            "description": expense.description,
            "amount": expense.amount,
            "date": make_naive(expense.expense_date),
            "type": "expense"
        })

    # Sort all transactions by date
    transactions.sort(key=lambda x: x["date"])

    # Reformat data for DataFrame
    data = []
    income_entry = {}
    
    for transaction in transactions:
        if transaction["type"] == "income":
            income_entry = transaction
        else:  # Expense
            data.append([
                income_entry.get("description", ""), 
                income_entry.get("amount", ""), 
                income_entry.get("date", ""),  
                transaction["description"], 
                transaction["amount"], 
                transaction["date"]
            ])
            income_entry = {}  # Reset income entry after pairing

    # Handle any remaining unpaired income
    if income_entry:
        data.append([
            income_entry.get("description", ""), 
            income_entry.get("amount", ""), 
            income_entry.get("date", ""),  
            "", "", ""  # Empty expense columns
        ])

    # Convert to DataFrame
    df = pd.DataFrame(data, columns=[
        'Income Description', 'Income Amount', 'Income Date',
        'Expense Description', 'Expense Amount', 'Expense Date'
    ])

    # Add totals at the end
    df.loc[len(df)] = ['Total', context['total_income'], '', 'Total', context['total_expenses'], '']
    df.loc[len(df)] = ['Net Balance', context['net_balance'], '', '', '', '']

    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"income_expenditure_report_{timestamp}.xlsx"

    # Prepare HTTP response
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    # Write to Excel
    df.to_excel(response, index=False, engine="openpyxl")
    return response



@login_required
def income_report_view(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    start_date = request.GET.get("start_date", "").strip()
    end_date = request.GET.get("end_date", "").strip()
    export_format = request.GET.get('export')  # PDF or Excel
    if start_date:
        start_date = make_aware(datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        end_date = make_aware(datetime.strptime(end_date, "%Y-%m-%d"))
    # Query data
    if start_date and end_date:
        incomes = list(SaccoIncome.objects.filter(income_date__range=[start_date, end_date]))
    else:
        incomes = list(SaccoIncome.objects.all().order_by('-id')[:100])


    # Fetch last 100 records by default
    if not start_date or not end_date:
        incomes = SaccoIncome.objects.order_by("-id")[:100]
    else:
        if start_date > end_date:
            start_date, end_date = end_date, start_date  # Swap if incorrect order
        incomes = SaccoIncome.objects.filter(income_date__range=[start_date, end_date]).order_by("id")

    # Compute running balance
    running_balance = 0
    for income in incomes:
        running_balance += income.amount
        income.running_balance = running_balance

    context = {"incomes": incomes, "start_date": start_date, "end_date": end_date}
        # Handle PDF export
    if export_format == 'pdf':
        return export_income_pdf(request, context)

    # Handle Excel export
    if export_format == 'excel':
        return export_income_excel(request, context)
    return render(request, "accounting/income_report.html", context)

@login_required
def expense_report_view(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    start_date = request.GET.get("start_date", "").strip()
    end_date = request.GET.get("end_date", "").strip()
    export_format = request.GET.get('export')  # PDF or Excel
    if start_date:
        start_date = make_aware(datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        end_date = make_aware(datetime.strptime(end_date, "%Y-%m-%d"))
    # Query data
    if start_date and end_date:
        expenses = list(SaccoExpense.objects.filter(expense_date__range=[start_date, end_date]))
    else:
        expenses = list(SaccoExpense.objects.all().order_by('-id')[:100])


    # Fetch last 100 records by default
    if not start_date or not end_date:
        expenses = SaccoExpense.objects.order_by("-id")[:100]
    else:
        if start_date > end_date:
            start_date, end_date = end_date, start_date  # Swap if incorrect order
        incomes = SaccoExpense.objects.filter(expense_date__range=[start_date, end_date]).order_by("id")

    # Compute running balance
    running_balance = 0
    for expense in expenses:
        running_balance -= expense.amount
        expense.running_balance = running_balance

    context = {"expenses": expenses, "start_date": start_date, "end_date": end_date}
        # Handle PDF export
    if export_format == 'pdf':
        return export_expense_pdf(request, context)

    # Handle Excel export
    if export_format == 'excel':
        return export_expense_excel(request, context)
    return render(request, "accounting/expense_report.html", context)




from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Image, Spacer
from django.http import HttpResponse
from io import BytesIO
from datetime import datetime

@login_required
def export_expense_pdf(request, context):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4))
    width, height = landscape(A4)
    elements = []
    add_sacco_info(elements, width)

    # Report Title & Date Range
    report_timestamp = datetime.now().strftime("%Y-%m-%d, %H:%M:%S")
    title = f"Expenses Report as at {report_timestamp}"
    start_date = context.get("start_date")
    end_date = context.get("end_date")
    date_range = f"From: {start_date.strftime('%B %d, %Y')} To: {end_date.strftime('%B %d, %Y')}" if start_date and end_date else ""

    # Title Styling
    elements.append(Table([[title]], colWidths=[width - 80], style=[
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))

    if date_range:
        elements.append(Table([[date_range]], colWidths=[width - 80], style=[
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 15),
        ]))

    # Initialize table data with headers
    data = [["Date", "Account", "Description", "Amount (KES)", "Running Balance"]]
    running_balance = 0.0

    # Sort expenses by date 
    expenses = sorted(context.get("expenses", []), key=lambda x: x.expense_date)

    for expense in expenses:
        try:
            # Safely extract and format values
            expense_date = expense.expense_date.strftime('%b. %d, %Y') if expense.expense_date else "N/A"
            sacco_account = str(expense.sacco_account) if expense.sacco_account else "N/A"
            description = str(expense.description) if expense.description else "N/A"
            amount = float(getattr(expense, "amount", 0.0))  # Ensures amount is a valid float
            running_balance -= amount  # Update running balance

            # Append row to table data
            data.append([
                expense_date,
                sacco_account,
                description,
                f"{amount:,.2f}",
                f"{running_balance:,.2f}"
            ])
        except Exception as e:
            print(f"Error processing expense entry: {e}")  # Debugging

    # Expense Data Table
    table = Table(data, colWidths=[80, 150, 300, 75, 85])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("WORDWRAP", (1, 0), (2, -1)),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
    ]))
    elements.append(table)

    doc.build(elements)

    # Generate filename with timestamp
    filename = f"expense_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    buffer.seek(0)
    response = HttpResponse(buffer, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    return response



from io import BytesIO
from decimal import Decimal
from datetime import datetime
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Image, Spacer
from administration.models import ChamaInfo


@login_required
def export_income_pdf(request, context):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4))
    width, height = landscape(A4)
    elements = []

    add_sacco_info(elements, width)

    # Title and Date Range
    report_timestamp = datetime.now().strftime("%Y-%m-%d, %H:%M:%S")
    title = f"Incomes Report as at {report_timestamp}"
    date_range = ""

    if context.get("start_date") and context.get("end_date"):
        date_range = f"From: {context['start_date'].strftime('%B %d, %Y')} To: {context['end_date'].strftime('%B %d, %Y')}"

    elements.append(Table([[title]], colWidths=[width - 80], style=[
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))

    if date_range:
        elements.append(Table([[date_range]], colWidths=[width - 80], style=[
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 15),
        ]))

    # Sort incomes by date
    incomes = sorted(context.get("incomes", []), key=lambda x: x.income_date)

    # Compute running balance
    running_balance = Decimal(0)
    data = [["Date", "Reference", "Description", "Amount (KES)", "Running Balance"]]

    for income in incomes:
        try:
            date_str = income.income_date.strftime('%b. %d, %Y') if income.income_date else "N/A"
            reference = str(income.income_reference) if income.income_reference else "N/A"
            description = str(income.description) if income.description else "N/A"
            amount = Decimal(income.amount) if isinstance(income.amount, (Decimal, float, int)) else Decimal(0)
            
            running_balance += amount

            data.append([
                date_str,
                reference,
                description,
                f"{amount:,.2f}",
                f"{running_balance:,.2f}"
            ])
        except Exception as e:
            print(f"ERROR processing income: {e}")

    # Table of data
    table = Table(data, colWidths=[80, 150, 300, 75, 85])

    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("WORDWRAP", (1, 0), (2, -1)),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))

    elements.append(table)

    # Build PDF
    doc.build(elements)

    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"income_report_{timestamp}.pdf"

    buffer.seek(0)
    response = HttpResponse(buffer, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response

@login_required
def export_income_excel(request, context):
    incomes = context.get("incomes", [])

    if not incomes:
        return HttpResponse("No data available to export", content_type="text/plain")

    # Sort incomes by date (oldest to newest)
    sorted_incomes = sorted(incomes, key=lambda income: make_naive(income.income_date) if income.income_date else datetime.min)

    # Convert sorted incomes to a list of dictionaries for DataFrame
    data = [
        {
            "Transaction Date": make_naive(income.income_date).strftime("%Y-%m-%d") if income.income_date else "",
            "Account": income.sacco_account,  # Adjust according to your model field
            "Description": income.description,
            "Amount (KES)": income.amount,
            "Running Balance": income.running_balance  # Now included
        }
        for income in sorted_incomes
    ]

    df = pd.DataFrame(data)

    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"income_report_{timestamp}.xlsx"

    # Prepare response for Excel download
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    df.to_excel(response, index=False, engine="openpyxl")
    return response

@login_required
def export_expense_excel(request, context):
    expenses = context.get("expenses", [])

    if not expenses:
        return HttpResponse("No data available to export", content_type="text/plain")

    # Sort expenses by date (oldest to newest)
    sorted_expenses = sorted(expenses, key=lambda expense: make_naive(expense.expense_date) if expense.expense_date else datetime.min)

    # Convert sorted expenses to a list of dictionaries for DataFrame
    data = [
        {
            "Transaction Date": make_naive(expense.expense_date).strftime("%Y-%m-%d") if expense.expense_date else "",
            "Account": expense.sacco_account,  # Adjust according to your model field
            "Description": expense.description,
            "Amount (KES)": expense.amount,
            "Running Balance": expense.running_balance  # Now included
        }
        for expense in sorted_expenses
    ]

    df = pd.DataFrame(data)

    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"expense_report_{timestamp}.xlsx"

    # Prepare response for Excel download
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    df.to_excel(response, index=False, engine="openpyxl")
    return response


# List of incomes with search and last 100 matching records
@login_required
def income_list(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    query = request.GET.get('q', '')  # Get the search query from the URL

    incomes = SaccoIncome.objects.all().order_by('-id')  # Fetch all records, ordered by date

    if query:
        incomes = incomes.filter(
            Q(description__icontains=query) |
            Q(income_reference__icontains=query) |
            Q(amount__icontains=query) |
            Q(date_added__icontains=query)
        )

    incomes = incomes[:100]  # Limit results to the last 100 matching records

    return render(request, 'accounting/income_list.html', {'incomes': incomes, 'query': query})

# List of expenses with search and last 100 matching records
@login_required
def expense_list(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    query = request.GET.get('q', '')  # Get the search query from the URL

    expenses = SaccoExpense.objects.all().order_by('-id')  # Fetch all records, ordered by date

    if query:
        expenses = expenses.filter(
            Q(description__icontains=query) |
            Q(cheque_number__icontains=query) |
            Q(id__icontains=query) |
            Q(amount__icontains=query) |
            Q(expense_date__icontains=query)
        )

    expenses = expenses[:100]  # Limit results to the last 100 matching records

    return render(request, 'accounting/expense_list.html', {'expenses': expenses, 'query': query})


# View detailed income
@login_required
def view_income(request, income_id):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    income = get_object_or_404(SaccoIncome, id=income_id)
    return render(request, 'accounting/view_income.html', {'income': income})

# View detailed expense
@login_required
def view_expense(request, expense_id):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    expense = get_object_or_404(SaccoExpense, id=expense_id)
    return render(request, 'accounting/view_expense.html', {'expense': expense})


@login_required
def reconcile_income(request, income_id):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    income = get_object_or_404(SaccoIncome, id=income_id)

    if income.reconciliation_status == 'reconciled':
        messages.warning(request, "This income is already reconciled.")
        return redirect('accounting:income_list')

    if request.method == "POST":
        form = IncomeReconciliationForm(request.POST, request.FILES, instance=income)
        if form.is_valid():
            form.save()
            messages.success(request, "Income successfully reconciled.")
            return redirect('accounting:income_list')
    else:
        form = IncomeReconciliationForm(instance=income)

    return render(request, 'accounting/reconcile_income.html', {'form': form, 'income': income})

@login_required
def reconcile_expense(request, expense_id):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    expense = get_object_or_404(SaccoExpense, id=expense_id)

    if expense.reconciliation_status == 'reconciled':
        messages.warning(request, "This expense is already reconciled.")
        return redirect('accounting:expense_list')

    if request.method == "POST":
        form = ExpenseReconciliationForm(request.POST, request.FILES, instance=expense)
        if form.is_valid():
            form.save()
            messages.success(request, "Expense successfully reconciled.")
            return redirect('accounting:expense_list')
    else:
        form = ExpenseReconciliationForm(instance=expense)

    return render(request, 'accounting/reconcile_expense.html', {'form': form, 'expense': expense})



############################################################################################################
'''Automatic sending Reports'''
#############################################################################################################
from django.shortcuts import render, redirect
from .models import AutomatedReport
from .forms import AutomatedReportForm

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from .models import AutomatedReport
@login_required
def automated_reports_list(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    reports = AutomatedReport.objects.all()[:100]  # Restrict to 100 reports
    return render(request, 'accounting/automated_reports_list.html', {'reports': reports})
@login_required
def automated_report_delete(request, report_id):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    report = get_object_or_404(AutomatedReport, id=report_id)
    
    if request.method == 'POST':
        report.delete()
        messages.success(request, "Automated report deleted successfully.")
        return redirect('accounting:automated_reports_list')

    return render(request, 'accounting/automated_report_confirm_delete.html', {'report': report})


from django.shortcuts import render, redirect, get_object_or_404
from .forms import AutomatedReportForm,AutomatedReportFormEdit
from .models import AutomatedReport
@login_required
def automated_report_create(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    if request.method == "POST":
        form = AutomatedReportForm(request.POST)
        if form.is_valid():
            report = form.save()
            return redirect('accounting:automated_report_officials', report_id=report.id)
    else:
        form = AutomatedReportForm()
    return render(request, 'accounting/automated_report_form.html', {'form': form})
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from .models import AutomatedReport
from .forms import AutomatedReportForm
@login_required
def automated_report_edit(request, report_id):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    report = get_object_or_404(AutomatedReport, id=report_id)

    if request.method == "POST":
        form = AutomatedReportFormEdit(request.POST, instance=report)
        if form.is_valid():
            form.save()
            messages.success(request, "Report updated successfully.")
            return redirect('accounting:automated_reports_list')
    else:
        form = AutomatedReportFormEdit(instance=report)

    return render(request, 'accounting/automated_report_edit.html', {'form': form, 'report': report})


###########################################################################
"""Reports """
##############################################################################
from django.shortcuts import render
from .models import SaccoAccount
@login_required
def expense_accounts_list(request):
    # Filter only expense accounts
    accounts = SaccoAccount.objects.filter(account_group="Expenditure")  
    return render(request, 'accounting/expense_accounts_list.html', {'accounts': accounts})


from django.shortcuts import render, get_object_or_404
from django.db.models import Sum
from .models import SaccoExpense, SaccoAccount
@login_required
def individual_expense_report(request, account_id):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    account = get_object_or_404(saccoAccount, id=account_id)
    expenses = SaccoExpense.objects.filter(sacco_account=account)

    total_expense = expenses.aggregate(Sum('amount'))['amount__sum'] or 0

    context = {
        'account': account,
        'expenses': expenses,
        'total_expense': total_expense
    }
    return render(request, 'accounting/individual_expense_report.html', context)
from django.shortcuts import get_object_or_404
from django.http import HttpResponse
from django.template.loader import get_template
from xhtml2pdf import pisa
from .models import SaccoAccount, SaccoExpense
@login_required
def individual_expense_report_pdf(request, account_id):
    # Get account and related expenses
    account = get_object_or_404(SaccoAccount, id=account_id)
    expenses = SaccoExpense.objects.filter(sacco_account=account)
    total_expense = sum(exp.amount for exp in expenses)

    # Load template
    template_path = 'accounting/individual_expense_report_pdf.html'
    context = {'account': account, 'expenses': expenses, 'total_expense': total_expense}
    template = get_template(template_path)
    html = template.render(context)

    # Generate PDF
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="expense_report_{account.id}.pdf"'
    pisa_status = pisa.CreatePDF(html, dest=response)

    if pisa_status.err:
        return HttpResponse('We had some errors generating the PDF')

    return response
from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse
from .models import SaccoExpense, SaccoAccount
from .forms import AccountFilterForm
import io
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
@login_required
def individual_expense_report_view(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    form = AccountFilterForm(request.GET or None)
    expenses = None
    total_expense = 0

    if form.is_valid():
        account = form.cleaned_data.get('account')
        start_date = form.cleaned_data.get('start_date')
        end_date = form.cleaned_data.get('end_date')

        expenses = SaccoExpense.objects.filter(sacco_account=account)
        if start_date:
            expenses = expenses.filter(expense_date__gte=start_date)
        if end_date:
            expenses = expenses.filter(expense_date__lte=end_date)

        total_expense = sum(expense.amount for expense in expenses)

    return render(request, 'accounting/individual_expense_report.html', {'form': form, 'expenses': expenses, 'total_expense': total_expense})

import io
from django.http import HttpResponse
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from .models import SaccoExpense
from .forms import AccountFilterForm

@login_required
def generate_individual_expense_report_pdf(request):
    form = AccountFilterForm(request.GET or None)

    if form.is_valid():
        account = form.cleaned_data.get("account")
        start_date = form.cleaned_data.get("start_date")
        end_date = form.cleaned_data.get("end_date")

        expenses = SaccoExpense.objects.filter(sacco_account=account)
        if start_date:
            expenses = expenses.filter(expense_date__gte=start_date)
        if end_date:
            expenses = expenses.filter(expense_date__lte=end_date)

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4)
        elements = []

        # **Add sacco Info (Header)**
        add_sacco_info(elements, A4[0])  # Correctly passing width

        # **Title**
        styles = getSampleStyleSheet()
        title = Paragraph(f"<b>Expense Report for {account.account_name}</b>", styles["Title"])
        elements.append(title)
        elements.append(Spacer(1, 12))

        # **Table Headers & Data**
        data = [["Date", "Description", "Amount"]]
        for expense in expenses:
            data.append([
                expense.expense_date.strftime("%Y-%m-%d"),
                expense.description,
                f"{expense.amount:.2f}",
            ])

        # **Table Formatting**
        table = Table(data, colWidths=[100, 300, 100])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
        ]))

        elements.append(table)
        elements.append(Spacer(1, 12))

        # **Total Expenses**
        total_amount = sum(expense.amount for expense in expenses)
        total_paragraph = Paragraph(f"<b>Total Expenses: {total_amount:.2f}</b>", styles["Normal"])
        elements.append(total_paragraph)

        # **Build PDF**
        doc.build(elements)
        buffer.seek(0)

        response = HttpResponse(buffer, content_type="application/pdf")
        response["Content-Disposition"] = 'attachment; filename="expense_report.pdf"'
        return response

    return HttpResponse("Invalid Form Submission", status=400)


from django import forms
from django.shortcuts import render
from django.http import HttpResponse
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from .models import SaccoIncome, SaccoExpense, SaccoAccount
import io
from.forms import ReconciliationFilterForm

from django.shortcuts import render, get_object_or_404
from .models import SaccoIncome, SaccoExpense, SaccoAccount
from .forms import ReconciliationFilterForm

from django.shortcuts import render
from .forms import ReconciliationFilterForm
from .models import SaccoIncome, SaccoExpense
from django.db.models import F, Value, CharField

@login_required
def reconciliation_report_filter(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    form = ReconciliationFilterForm(request.GET or None)
    return render(request, "accounting/reconciliation_report_form.html", {"form": form})

from django.db.models import F, Value, CharField
from datetime import datetime
@login_required
def reconciliation_report(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    form = ReconciliationFilterForm(request.GET or None)
    transactions = []
    total_income, total_expenses = 0, 0
    account = None
    start_date, end_date = None, None

    if form.is_valid():
        account = form.cleaned_data.get("account")
        start_date = form.cleaned_data.get("start_date")
        end_date = form.cleaned_data.get("end_date")

        incomes = SaccoIncome.objects.filter(reconciliation_account=account)
        expenses = SaccoExpense.objects.filter(reconciliation_account=account)

        if start_date:
            incomes = incomes.filter(income_date__gte=start_date)
            expenses = expenses.filter(expense_date__gte=start_date)
        if end_date:
            incomes = incomes.filter(income_date__lte=end_date)
            expenses = expenses.filter(expense_date__lte=end_date)

        # Prepare transactions list
        income_data = incomes.values(
            date=F('income_date'),
            congregation_name=F('congregation__name'),
            congregation_group_name=F('congregation_group__name'),
            description_name=F('description'),
            amount_name=F('amount'),
            transaction_type=Value('Deposit', output_field=CharField())
        )

        expense_data = expenses.values(
            date=F('expense_date'),
            congregation_name=F('congregation__name'),
            congregation_group_name=F('congregation_group__name'),
            description_name=F('description'),
            amount_name=F('amount'),
            transaction_type=Value('Expense', output_field=CharField())
        )

        transactions = list(income_data) + list(expense_data)

        # Handle None dates by assigning a default value
        for transaction in transactions:
            if transaction["date"] is None:
                transaction["date"] = datetime.min  # Assign earliest possible date

        # Sort transactions by date
        transactions.sort(key=lambda x: x["date"])

        # Calculate totals
        total_income = sum(t["amount_name"] for t in transactions if t["transaction_type"] == "Deposit")
        total_expenses = sum(t["amount_name"] for t in transactions if t["transaction_type"] == "Expense")

    balance = total_income - total_expenses

    return render(request, "accounting/reconciliation_report.html", {
        "form": form,
        "account": account,
        "start_date": start_date,
        "end_date": end_date,
        "transactions": transactions,
        "total_income": total_income,
        "total_expenses": total_expenses,
        "balance": balance
    })

from django.http import HttpResponse
import io
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from .models import SaccoIncome, SaccoExpense, SaccoAccount
from datetime import datetime

def is_valid_date(date_str):
    """Check if a date string is valid."""
    try:
        if date_str and date_str.lower() != "none":  # Ensure it's not None or 'None'
            datetime.fromisoformat(date_str)  # Validate format
            return True
    except ValueError:
        pass
    return False

import io
from django.shortcuts import get_object_or_404
from django.http import HttpResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, portrait
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer


import io
from django.shortcuts import get_object_or_404
from django.http import HttpResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, portrait
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from datetime import datetime

from .models import SaccoAccount, SaccoIncome, SaccoExpense

def format_description(instance, is_expense=False):
    """Formats description, excluding sacco_account for incomes, but including it for expenses."""
    parts = [
        getattr(instance, "congregation", None),
        getattr(instance, "congregation_group", None),
        instance.description
    ]

    if is_expense:
        parts.append(getattr(instance, "sacco_account", None))  # Only include for expenses

    full_description = " - ".join(str(part) for part in parts if part)  # Remove None values

    # Limit description length to 60 characters (cut at word boundary)
    if len(full_description) > 60:
        return full_description[:55].rsplit(" ", 1)[0] + "..."  # Avoid cutting words mid-way

    return full_description
@login_required
def reconciliation_report_pdf(request, account_id, start_date, end_date):
    account = get_object_or_404(SaccoAccount, id=account_id)
    incomes = SaccoIncome.objects.filter(reconciliation_account=account)
    expenses = SaccoExpense.objects.filter(reconciliation_account=account)

    if is_valid_date(start_date):
        incomes = incomes.filter(income_date__gte=start_date)
        expenses = expenses.filter(expense_date__gte=start_date)

    if is_valid_date(end_date):
        incomes = incomes.filter(income_date__lte=end_date)
        expenses = expenses.filter(expense_date__lte=end_date)

    # Merge and format transactions
    transactions = [
        {
            "date": income.income_date,
            "description": format_description(income, is_expense=False),  # No sacco account for incomes
            "reference": income.income_reference,
            "amount": income.amount
        }
        for income in incomes
    ] + [
        {
            "date": expense.expense_date,
            "description": format_description(expense, is_expense=True),  # Include sacco account for expenses
            "reference": expense.cheque_number,
            "amount": -expense.amount
        }
        for expense in expenses
    ]

    # Handle None dates by assigning a default value
    for transaction in transactions:
        if transaction["date"] is None:
            transaction["date"] = datetime.min  # Assign earliest possible date

    # Sort transactions by date
    transactions.sort(key=lambda x: x["date"])

    # Calculate totals
    total_income = sum(t["amount"] for t in transactions if t["amount"] > 0)
    total_expenses = sum(-t["amount"] for t in transactions if t["amount"] < 0)
    balance = total_income - total_expenses

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    elements = []
    width, height = portrait(A4)

    # Add sacco Info at the top of the first page
    add_sacco_info(elements, width)
    styles = getSampleStyleSheet()

    elements.append(Paragraph(f"<b>Reconciliation Report for {account.account_name}</b>", styles["Title"]))
    elements.append(Spacer(1, 12))

    # Create table data
    data = [["Date", "Ref", "Description", "Amount (KES)"]]
    for transaction in transactions:
        data.append([
            transaction["date"].strftime("%Y-%m-%d"),
            transaction["reference"],
            transaction["description"],
            f"{transaction['amount']:,.2f}"
        ])

    # Define table styling
    table = Table(data, colWidths=[70, 50, 300, 75])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("GRID", (0, 0), (-1, -1), 1, colors.black)
    ]))
    
    elements.append(table)
    elements.append(Spacer(1, 12))

    # Summary section
    elements.append(Paragraph(f"<b>Total Income: {total_income:,.2f}</b>", styles["Normal"]))
    elements.append(Paragraph(f"<b>Total Expenses: {total_expenses:,.2f}</b>", styles["Normal"]))
    elements.append(Paragraph(f"<b>Balance: {balance:,.2f}</b>", styles["Normal"]))

    doc.build(elements)
    buffer.seek(0)

    response = HttpResponse(buffer, content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="reconciliation_report.pdf"'
    return response



####################################################################
"""Income and expenditure summary"""
###################################################################
from django.shortcuts import render
from django.db.models import Sum
from .models import SaccoIncome, SaccoExpense  # Adjust model names as per your project
import datetime
import pandas as pd
from django.http import HttpResponse

from django.shortcuts import render
from django.db.models import Sum
from .models import SaccoIncome, SaccoExpense, SaccoAccount
import datetime
@login_required

def income_expenditure_summary(request):
    if not request.user.is_admin:
        messages.warning(request, "You don't have permission to the section.")
        return redirect('administration:administration')
    # Get filter dates from request
    start_date = request.GET.get("start_date")
    end_date = request.GET.get("end_date")

    # Convert to date format (handle invalid formats)
    if start_date:
        try:
            start_date = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            start_date = None

    if end_date:
        try:
            end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            end_date = None

    # Get all incomes and expenses
    incomes = SaccoIncome.objects.all()
    expenses = SaccoExpense.objects.all()

    # Apply date filters
    if start_date:
        incomes = incomes.filter(income_date__gte=start_date)
        expenses = expenses.filter(expense_date__gte=start_date)
    if end_date:
        incomes = incomes.filter(income_date__lte=end_date)
        expenses = expenses.filter(expense_date__lte=end_date)

    # Summarize Income by saccoAccount (with account details)
    income_summary = (
        incomes.values("sacco_account__account_code", "sacco_account__account_name")
        .annotate(total_amount=Sum("amount"))
        .order_by("sacco_account__account_code")
    )

    # Summarize Expenses by saccoAccount (with account details)
    expense_summary = (
        expenses.values("sacco_account__account_code", "sacco_account__account_name")
        .annotate(total_amount=Sum("amount"))
        .order_by("sacco_account__account_code")
    )

    # Calculate total income, expense, and net balance
    total_income = incomes.aggregate(Sum("amount"))["amount__sum"] or 0
    total_expense = expenses.aggregate(Sum("amount"))["amount__sum"] or 0
    net_balance = total_income - total_expense

    context = {
        "income_summary": income_summary,
        "expense_summary": expense_summary,
        "total_income": total_income,
        "total_expense": total_expense,
        "net_balance": net_balance,
        "start_date": start_date,
        "end_date": end_date,
    }

    return render(request, "accounting/income_expenditure_summary.html", context)

import pandas as pd
from django.http import HttpResponse
from django.db.models import Sum

import pandas as pd
from django.http import HttpResponse
from django.db.models import Sum
@login_required
def download_income_expenditure_summary_excel(request):
    start_date = request.GET.get("start_date")
    end_date = request.GET.get("end_date")

    incomes = SaccoIncome.objects.select_related("sacco_account").all()
    expenses = SaccoExpense.objects.select_related("sacco_account").all()

    if start_date:
        incomes = incomes.filter(income_date__gte=start_date)
        expenses = expenses.filter(expense_date__gte=start_date)
    if end_date:
        incomes = incomes.filter(income_date__lte=end_date)
        expenses = expenses.filter(expense_date__lte=end_date)

    # Summarize Income
    income_summary = (
        incomes.values("sacco_account__account_code", "sacco_account__account_name")
        .annotate(total_amount=Sum("amount"))
        .order_by("sacco_account__account_code")
    )

    # Summarize Expenses
    expense_summary = (
        expenses.values("sacco_account__account_code", "sacco_account__account_name")
        .annotate(total_amount=Sum("amount"))
        .order_by("sacco_account__account_code")
    )

    # Create DataFrames for Income and Expenses separately
    df_income = pd.DataFrame({
        "Account Code": [i["sacco_account__account_code"] for i in income_summary],
        "Account Name": [i["sacco_account__account_name"] for i in income_summary],
        "Income Amount (KES)": [i["total_amount"] for i in income_summary],
        "Expense Amount (KES)": [None] * len(income_summary)  # Empty for now
    })

    df_expense = pd.DataFrame({
        "Account Code": [e["sacco_account__account_code"] for e in expense_summary],
        "Account Name": [e["sacco_account__account_name"] for e in expense_summary],
        "Income Amount (KES)": [None] * len(expense_summary),  # Empty for now
        "Expense Amount (KES)": [e["total_amount"] for e in expense_summary]
    })

    # Add subheading rows for Income and Expenses
    income_heading = pd.DataFrame({
        "Account Code": ["--"],
        "Account Name": ["INCOME SUMMARY"],
        "Income Amount (KES)": [None],
        "Expense Amount (KES)": [None]
    })

    expense_heading = pd.DataFrame({
        "Account Code": ["--"],
        "Account Name": ["EXPENSE SUMMARY"],
        "Income Amount (KES)": [None],
        "Expense Amount (KES)": [None]
    })

    # Combine everything with headings
    df_summary = pd.concat([income_heading, df_income, expense_heading, df_expense], ignore_index=True)

    # Create Excel response
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = 'attachment; filename="income_expenditure_summary.xlsx"'

    # Write to single sheet
    with pd.ExcelWriter(response, engine="xlsxwriter") as writer:
        df_summary.to_excel(writer, sheet_name="Summary", index=False, startrow=0)

    return response


import io
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.db.models import Sum
from reportlab.lib.pagesizes import A4, portrait
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from datetime import datetime
@login_required
def download_income_expenditure_summary_pdf(request):
    start_date = request.GET.get("start_date")
    end_date = request.GET.get("end_date")

    incomes = SaccoIncome.objects.select_related("sacco_account").all()
    expenses = SaccoExpense.objects.select_related("sacco_account").all()

    if start_date:
        incomes = incomes.filter(income_date__gte=start_date)
        expenses = expenses.filter(expense_date__gte=start_date)
    if end_date:
        incomes = incomes.filter(income_date__lte=end_date)
        expenses = expenses.filter(expense_date__lte=end_date)

    # Summarize Income
    income_summary = (
        incomes.values("sacco_account__account_code", "sacco_account__account_name")
        .annotate(total_amount=Sum("amount"))
        .order_by("sacco_account__account_code")
    )

    # Summarize Expenses
    expense_summary = (
        expenses.values("sacco_account__account_code", "sacco_account__account_name")
        .annotate(total_amount=Sum("amount"))
        .order_by("sacco_account__account_code")
    )

    total_income = sum(i["total_amount"] for i in income_summary)
    total_expense = sum(e["total_amount"] for e in expense_summary)
    net_balance = total_income - total_expense

    # Create PDF Buffer
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=portrait(A4), rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    elements = []
    styles = getSampleStyleSheet()
    width, height = portrait(A4)

    # Add sacco Info at the top of the first page
    add_sacco_info(elements, width)
    # Title
    elements.append(Paragraph("<b>Income & Expenditure Summary</b>", styles["Title"]))
    elements.append(Spacer(1, 12))

    # Date Range
    today = datetime.now().strftime("%Y-%m-%d")
    elements.append(Paragraph(f"Generated on: {today}", styles["Normal"]))
    if start_date or end_date:
        elements.append(Paragraph(f"Period: {start_date or 'Start'} to {end_date or 'End'}", styles["Normal"]))
    elements.append(Spacer(1, 12))

    # Summary Section
    summary_data = [
        ["Total Income (KES)", f"{total_income:,.2f}"],
        ["Total Expense (KES)", f"{total_expense:,.2f}"],
        ["Net Balance (KES)", f"{net_balance:,.2f}"],
    ]
    summary_table = Table(summary_data, colWidths=[250, 150])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 1, colors.black)
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 20))

    # Income Table
    elements.append(Paragraph("<b>Income Summary</b>", styles["Heading2"]))
    elements.append(Spacer(1, 6))
    income_data = [["Account Code", "Account Name", "Amount (KES)"]]
    for income in income_summary:
        income_data.append([
            income["sacco_account__account_code"],
            income["sacco_account__account_name"],
            f"{income['total_amount']:,.2f}"
        ])
    income_table = Table(income_data, colWidths=[100, 300, 100])
    income_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("GRID", (0, 0), (-1, -1), 1, colors.black)
    ]))
    elements.append(income_table)
    elements.append(Spacer(1, 20))

    # Expense Table
    elements.append(Paragraph("<b>Expense Summary</b>", styles["Heading2"]))
    elements.append(Spacer(1, 6))
    expense_data = [["Account Code", "Account Name", "Amount (KES)"]]
    for expense in expense_summary:
        expense_data.append([
            expense["sacco_account__account_code"],
            expense["sacco_account__account_name"],
            f"{expense['total_amount']:,.2f}"
        ])
    expense_table = Table(expense_data, colWidths=[100, 300, 100])
    expense_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("GRID", (0, 0), (-1, -1), 1, colors.black)
    ]))
    elements.append(expense_table)

    # Build PDF
    doc.build(elements)
    buffer.seek(0)

    response = HttpResponse(buffer, content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="income_expenditure_summary.pdf"'
    return response
########################################################################
'''journal vouchers'''
#######################################################################
from django.http import JsonResponse
from django.db.models import Q
from django.shortcuts import render
from django.db.models import Q
from customers.models import Customer # Replace with your actual Member/Customer model name

def customer_search_list_ajax(request):
    query = request.GET.get('q', '').strip()
    results = []
    
    if len(query) >= 2:
        # Searching across multiple fields for better UX
        results = Customer.objects.filter(
            Q(cust_no__icontains=query) | 
            Q(full_name__icontains=query) | 
            Q(national_id__icontains=query) # or whatever your National ID field is called
        ).only('cust_no', 'full_name', 'national_id')[:10]
        
    return render(request, 'accounting/includes/customer_search_results.html', {'results': results})
    
    # Return as a list of dicts
    return JsonResponse(results, safe=False)
from django.shortcuts import render, redirect
from django.db import transaction
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Q, Sum
from django.utils import timezone
from django.views.decorators.http import require_GET

from .forms import JournalVoucherForm
from transactions.models import SavingsTransaction, LoanTransaction, CustomerAccountsSetup
from .models import (
    SaccoAccount, SaccoAccountsLedger, SaccoAccountBalance
   
)
# Assuming these models exist based on your architecture
from customers.models import Customer
from loans.models import LoanHistory 

from django.db import transaction
from django.utils import timezone
from django.contrib import messages
from django.shortcuts import render, redirect
from decimal import Decimal
from .models import (
   
    SaccoAccountsLedger, SaccoAccountBalance, SaccoIncome, SaccoExpense
)
from .forms import JournalVoucherForm

@transaction.atomic
def journal_voucher_create(request):
    """Handles JVS"""
    if request.method == 'POST':
        form = JournalVoucherForm(request.POST)
            
        if form.is_valid():
            data = form.cleaned_data
            
            date = data.get('date')
            target_acc = data.get('target_account')
            cust_no = data.get('cust_no')
            amount = data.get('amount')
            direction = data.get('transaction_direction')
            sacco_account_obj = data.get('sacco_account')
            
            # Safety check just in case the browser sent an empty field
            if not target_acc:
                messages.error(request, "Target account is missing. Please re-select the customer.")
                return render(request, 'accounting/journal_voucher_form.html', {'form': form})
            
            user_staff = request.user if request.user.is_authenticated else "System"
            user_instance = request.user if request.user.is_authenticated else None
            is_debit_customer = (direction == 'debit_customer')

            cust_debit = amount if is_debit_customer else 0
            cust_credit = 0 if is_debit_customer else amount
            sacco_debit = 0 if is_debit_customer else amount
            sacco_credit = amount if is_debit_customer else 0

            try:
                # Find matching customer profile instance
                customer_obj = Customer.objects.filter(cust_no=cust_no).first()

                # A. EFFECT CUSTOMER SIDE
                is_loan = target_acc.startswith('LN')
                jv_ref = make_tr_ref('journal_voucher')
                
                if is_loan:
                    loan_info = LoanHistory.objects.get(loan_no=target_acc)
                    
                    LoanTransaction.objects.create(
                        cust_no=cust_no,
                        loan_id=loan_info.id,
                        loan_no=target_acc,
                        loan_type=loan_info.loan_type,
                        tr_date=date if date else timezone.now(),
                        tr_ref=jv_ref,
                        ext_ref=data.get('reference'),
                        tr_desc=data.get('description'),
                        debit_amount=cust_debit,
                        credit_amount=cust_credit,
                        created_by=user_staff
                    )
                else:
                    SavingsTransaction.objects.create(
                        cust_no=cust_no,
                        saving_type=target_acc,
                        tr_date=date if date else timezone.now(),
                        tr_ref=jv_ref,
                        ext_ref=data.get('reference'),
                        tr_desc=data.get('description'),
                        debit_amount=cust_debit,
                        credit_amount=cust_credit,
                        created_by=user_staff
                    )

                # B. EFFECT SACCO LEDGER SIDE
                SaccoAccountsLedger.objects.create(
                    customer=customer_obj,  # Explicitly tracking customer link inside the Ledger
                    sacco_account=sacco_account_obj,
                    external_reference=data.get('reference'),
                    reference=jv_ref,
                    description=data.get('description'),
                    amount=amount,
                    debit_amount=sacco_debit,
                    credit_amount=sacco_credit,
                    created_by=user_instance
                )

                # C. UPDATE SACCO BALANCE
                balance_obj, _ = SaccoAccountBalance.objects.get_or_create(sacco_account=sacco_account_obj)
                if is_debit_customer:
                    balance_obj.balance += amount
                else:
                    balance_obj.balance -= amount
                balance_obj.save()

                # D. RECORD SYSTEM INCOME OR EXPENSE TRACKING
                transaction_date = date if date else timezone.now()
                
                if is_debit_customer:
                    # Debit Customer -> Credit Sacco (Income generated/received value)
                    SaccoIncome.objects.create(
                        customer=customer_obj,
                        amount=amount,
                        description=data.get('description') or f"JV Income posting via : {jv_ref}",
                        sacco_account=sacco_account_obj,
                        income_date=transaction_date,
                        income_reference=jv_ref,
                        reconciliation_status='unreconciled',
                        created_by=user_instance
                    )
                else:
                    # Credit Customer -> Debit Sacco (Expense incurred/funds released)
                    SaccoExpense.objects.create(
                        customer=customer_obj,
                        amount=amount,
                        description=data.get('description') or f"JV Expense posting via : {jv_ref}",
                        sacco_account=sacco_account_obj,
                        expense_date=transaction_date,
                        expense_reference=jv_ref,
                        reconciliation_status='unreconciled',
                        created_by=user_instance
                    )

                messages.success(request, f"Journal Voucher {jv_ref} posted and auxiliary modules updated successfully.")
                return redirect('accounting:journal_voucher_create') 

            except Exception as e:
                messages.error(request, f"Critical Processing Error: {str(e)}")
    else:
        form = JournalVoucherForm()

    return render(request, 'accounting/journal_voucher_form.html', {'form': form})


# -----------------------------------------
# 2. AJAX LIVE SEARCH
# -----------------------------------------
@require_GET
def customer_search_list_ajax(request):
    query = request.GET.get('q', '').strip()
    results = []
    
    if len(query) >= 2:
        # Adjust 'Member' and fields to match your actual customer profile model
        results = Customer.objects.filter(
            Q(cust_no__icontains=query) | 
            Q(full_name__icontains=query) | 
            Q(national_id__icontains=query)
        ).only('cust_no', 'full_name', 'national_id')[:10]
        
    return render(request, 'accounting/includes/customer_search_results.html', {'results': results})


# -----------------------------------------
# 3. API: FETCH ACCOUNTS WITH BALANCES
# -----------------------------------------
@require_GET
def get_customer_accounts_api(request):
    """Fetches savings/loan accounts and calculates real-time balances."""
    cust_no = request.GET.get("cust_no")
    if not cust_no:
        return JsonResponse({"results": []})

    results = []

    # 1. Fetch Savings Accounts & Calculate Balances
    savings_accounts = CustomerAccountsSetup.objects.filter(
        is_active=True, is_loan_account=False
    ).values('account_code', 'account_name', 'account_type').distinct()

    for acc in savings_accounts:
        # Calculate Balance: Sum(Credit) - Sum(Debit)
        totals = SavingsTransaction.objects.filter(
            cust_no=cust_no, saving_type=acc['account_type']
        ).aggregate(total_credit=Sum('credit_amount'), total_debit=Sum('debit_amount'))
        
        bal = (totals['total_credit'] or 0) - (totals['total_debit'] or 0)
        
        results.append({
            "id": acc['account_type'], 
            "text": f"{acc['account_code']} - {acc['account_name']} (Bal: {bal:,.2f})"
        })

    # 2. Fetch Loan Accounts & Calculate Balances
    active_loan_nos = LoanTransaction.objects.filter(
        cust_no=cust_no
    ).exclude(loan_no__isnull=True).values_list('loan_no', flat=True).distinct()

    loans = LoanHistory.objects.filter(loan_no__in=active_loan_nos).only('loan_no', 'loan_type')

    for ln in loans:
        # Calculate Loan Balance: Sum(Debit) - Sum(Credit)
        totals = LoanTransaction.objects.filter(
            cust_no=cust_no, loan_no=ln.loan_no
        ).aggregate(total_credit=Sum('credit_amount'), total_debit=Sum('debit_amount'))
        
        bal = (totals['total_debit'] or 0) - (totals['total_credit'] or 0)
        loan_label = ln.get_loan_type_display() if hasattr(ln, 'get_loan_type_display') else ln.loan_type
        
        results.append({
            "id": ln.loan_no, 
            "text": f"{ln.loan_no} - {loan_label} (Bal: {bal:,.2f})"
        })

    return JsonResponse({"results": results})

import datetime
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from .models import RegistrationFeeConfig
@login_required
def reg_fee_config(request):
    categories = [
        ("adult_individual", "Adult-Individual"),
        ("minor_individual", "Minor-Individual"),
        ("group", "Group"),
        ("church", "Church"),
    ]
    
    if request.method == "POST":
        cat = request.POST.get('category')
        amt = request.POST.get('amount')
        config, created = RegistrationFeeConfig.objects.update_or_create(
            category=cat, 
            defaults={'amount': amt}
        )
        messages.success(request, f"Updated {cat} fee to {amt}")
        return redirect('accounting:reg_fee_config')

    # Get existing configs as a dict for easy template access
    existing_configs = {c.category: c.amount for c in RegistrationFeeConfig.objects.all()}
    return render(request, 'accounting/reg_config.html', {
        'categories': categories,
        'existing_configs': existing_configs
    })
from django.shortcuts import render, redirect
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger

from .models import RegistrationFeeConfig, SaccoAccount, SaccoAccountsLedger, SaccoIncome
from customers.models import Customer
from transactions.models import SavingsTransaction
from sms.models import SMSLog
from transactions.utils import make_tr_ref

@transaction.atomic
def recover_registration_fees(request):
    """Iterates through unpaid members and recovers fees from savings_deposit"""
    if request.method == "POST":
        # 1. Map Fees
        fee_map = {cfg.category: cfg.amount for cfg in RegistrationFeeConfig.objects.all()}
        
        if not fee_map:
            messages.warning(request, "No Registration Fee configurations found. Please set them up first.")
            return redirect('accounting:reg_fee_config')

        # 2. Get the Income Account
        income_account = SaccoAccount.objects.filter(account_code="900-124000").first()
        if not income_account:
            messages.error(request, "Ledger Account 900-124000 not found.")
            return redirect('accounting:recover_registration_fees')

        # Process ALL unpaid customers globally on POST batch trigger
        unpaid_customers = Customer.objects.filter(reg_fee_is_paid=False)
        recovered_total = 0
        count = 0
        
        timestamp = timezone.now().strftime('%Y%m%d%H%M%S')
        user_instance = request.user if request.user.is_authenticated else None
        creator_string = request.user.username[:20] if request.user.is_authenticated else "System"

        for customer in unpaid_customers:
            fee_amount = fee_map.get(customer.customer_type, 0)
            if fee_amount <= 0: 
                continue

            # 3. Check balance
            bal_agg = SavingsTransaction.objects.filter(
                cust_no=customer.cust_no, 
                saving_type='share_capital'
            ).aggregate(bal=Sum('credit_amount') - Sum('debit_amount'))
            
            balance = bal_agg['bal'] or 0

            if balance >= fee_amount:
                tr_ref = f"REG-{customer.cust_no}-{timestamp}"

                # A. DEBIT MEMBER
                SavingsTransaction.objects.create(
                    cust_no=customer.cust_no,
                    saving_type='share_capital',
                    tr_date=timezone.now(),
                    tr_ref=tr_ref,
                    tr_desc=f"Registration Fee Recovery - {customer.full_name}",
                    debit_amount=fee_amount,
                    credit_amount=0,
                    created_by=user_instance
                )

                # B. CREDIT SACCO LEDGER
                SaccoAccountsLedger.objects.create(
                    sacco_account=income_account,
                    reference=tr_ref,
                    description=f"Membership Fee Recovery: {customer.full_name}",
                    amount=fee_amount,
                    credit_amount=fee_amount,
                    debit_amount=0,
                    created_by=user_instance
                )

                # C. UPDATE SACCO BALANCE
                balance_obj, _ = SaccoAccountBalance.objects.get_or_create(sacco_account=income_account)
                balance_obj.balance += fee_amount
                balance_obj.save()

                # D. RECORD TO INCOME MODEL
                SaccoIncome.objects.create(
                    customer=customer,
                    amount=fee_amount,
                    description=f"Registration Fee Recovery for {customer.full_name}",
                    sacco_account=income_account,
                    income_date=timezone.now(),
                    reference=tr_ref,
                    reconciliation_status='unreconciled',
                    created_by=user_instance
                )

                # E. STAGE OUTBOUND SMS LOG
                member_phone = getattr(customer, 'mobile', getattr(customer, 'phone', ''))
                if member_phone:
                    sms_message = (
                        f"Dear {customer.full_name}, KES {fee_amount:,.2f} has been recovered "
                        f"from your Deposits as Registration Fee. Ref: {tr_ref}."
                    )
                    SMSLog.objects.create(
                        phone=member_phone,
                        message=sms_message,
                        status="pending",
                        created_by=creator_string
                    )

                # F. UPDATE CUSTOMER STATUS
                customer.reg_fee_is_paid = True
                customer.save()

                recovered_total += fee_amount
                count += 1

        if count > 0:
            messages.success(request, f"Successfully recovered KES {recovered_total:,.2f} from {count} members.")
        else:
            messages.info(request, "Process completed, but no members had sufficient funds for recovery.")
            
        return redirect('accounting:recover_registration_fees')

    # ----------------==========================
    # PAGINATED GET REQUEST
    # ----------------==========================
    customer_list = Customer.objects.filter(reg_fee_is_paid=False).order_by('cust_no')
    
    # Set the number of rows displayed per page (e.g., 20)
    paginator = Paginator(customer_list, 20) 
    page_id = request.GET.get('page', 1)

    try:
        pending_page = paginator.page(page_id)
    except PageNotAnInteger:
        pending_page = paginator.page(1)
    except EmptyPage:
        pending_page = paginator.page(paginator.num_pages)

    return render(request, 'accounting/reg_recovery.html', {'pending': pending_page})

import pandas as pd
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import HttpResponse
from django.utils.dateparse import parse_date
from .models import CoreBankingRecord, MpesaRecord

def mpesa_reconciliation(request):
    """
    Three-section M-Pesa reconciliation (ported from nodicbs).

    Compares MpesaNotification (C2B API records) vs MpesaRecord (uploaded
    Safaricom statement). Three sections:

    A) STATEMENT ONLY — on M-Pesa statement but not in system notifications.
       Rows can be pushed into MpesaNotification via mpesa_reconciliation_push.
    B) SYSTEM ONLY   — in MpesaNotification but not on statement.
       For investigation (reversed / duplicate / failed at Safaricom).
    C) MATCHED        — present in both (info only).

    Also handles POST uploads of the Safaricom M-Pesa statement Excel file
    to populate MpesaRecord.
    """
    from transactions.models import MpesaNotification

    # ── Handle M-Pesa statement upload (POST) ──────────────────────
    if request.method == "POST":
        action = request.POST.get('action')
        if action == 'upload_mpesa':
            mpesa_file = request.FILES.get('mpesa_file')
            if not mpesa_file:
                messages.error(request, "Please select an M-Pesa Excel file.")
                return redirect('accounting:mpesa_reconciliation')
            try:
                df = pd.read_excel(mpesa_file)
                df.columns = df.columns.str.strip()
                records = []
                for _, row in df.iterrows():
                    records.append(MpesaRecord(
                        receipt_no=str(row['ReceiptNo']).strip(),
                        completion_time=pd.to_datetime(row['CompletionTime']),
                        details=row.get('Details'),
                        credit_amount=row.get('CreditAmount', 0.00),
                        debit_amount=row.get('DebitAmount', 0.00),
                        account=(str(row['Account']).strip()
                                 if pd.notna(row.get('Account')) else None),
                    ))
                MpesaRecord.objects.bulk_create(records, ignore_conflicts=True)
                messages.success(
                    request,
                    f"Successfully uploaded {len(records)} M-Pesa statement records.",
                )
            except Exception as e:
                messages.error(request, f"Error processing M-Pesa file: {e}")
        return redirect('accounting:mpesa_reconciliation')

    # ── GET — build reconciliation view ────────────────────────────
    from reports.utils import get_date_range
    start_str = request.GET.get('start_date')
    end_str = request.GET.get('end_date')

    if start_str and end_str:
        from datetime import datetime as _dt
        try:
            start = _dt.strptime(start_str, '%Y-%m-%d').date()
            end = _dt.strptime(end_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            start, end = get_date_range(request, default_days=30)
    else:
        start, end = get_date_range(request, default_days=30)

    notifs = (MpesaNotification.objects
              .filter(trans_time__date__gte=start, trans_time__date__lte=end)
              .only("trans_id", "trans_time", "trans_amount",
                    "bill_ref_number", "first_name", "posted"))
    records = (MpesaRecord.objects
               .filter(completion_time__date__gte=start,
                       completion_time__date__lte=end)
               .only("receipt_no", "completion_time", "credit_amount",
                     "account", "details"))

    notif_ids = {n.trans_id.strip().upper(): n for n in notifs}
    record_ids = {r.receipt_no.strip().upper(): r for r in records}

    matched_keys = set(notif_ids) & set(record_ids)
    stmt_only_keys = set(record_ids) - set(notif_ids)
    system_only_keys = set(notif_ids) - set(record_ids)

    section = (request.GET.get("section") or "stmt_only").lower()
    if section not in ("matched", "stmt_only", "system_only"):
        section = "stmt_only"

    ZERO = Decimal("0.00")

    def _money(v):
        try:
            return f"{Decimal(str(v)):,.2f}"
        except Exception:
            return str(v)

    if section == "stmt_only":
        table_headers = ["", "Receipt No", "Date", "Amount", "Account", "Notes"]
        table_rows = []
        total = ZERO
        for k in sorted(stmt_only_keys):
            r = record_ids[k]
            amt = r.credit_amount or ZERO
            total += amt
            table_rows.append({
                'receipt': r.receipt_no,
                'date': (r.completion_time.strftime("%Y-%m-%d %H:%M")
                         if r.completion_time else ""),
                'amount': f"{amt:,.2f}",
                'amount_raw': float(amt),
                'account': r.account or "",
                'notes': (r.details or "")[:80],
            })
        section_title = "Statement Only (Pushable)"
        section_badge = "bg-warning text-dark"
    elif section == "system_only":
        table_headers = ["Trans ID", "Date", "Amount", "Bill Ref",
                         "Sender", "Posted?"]
        table_rows = []
        total = ZERO
        for k in sorted(system_only_keys):
            n = notif_ids[k]
            amt = n.trans_amount or ZERO
            total += amt
            table_rows.append({
                'receipt': n.trans_id,
                'date': (n.trans_time.strftime("%Y-%m-%d %H:%M")
                         if n.trans_time else ""),
                'amount': f"{amt:,.2f}",
                'amount_raw': float(amt),
                'account': n.bill_ref_number or "",
                'notes': n.first_name or "",
                'posted': "Yes" if n.posted else "No",
            })
        section_title = "System Only (Investigate)"
        section_badge = "bg-danger"
    else:
        table_headers = ["Trans ID", "Date", "Amount", "Bill Ref"]
        table_rows = []
        total = ZERO
        for k in sorted(matched_keys):
            n = notif_ids[k]
            amt = n.trans_amount or ZERO
            total += amt
            table_rows.append({
                'receipt': n.trans_id,
                'date': (n.trans_time.strftime("%Y-%m-%d %H:%M")
                         if n.trans_time else ""),
                'amount': f"{amt:,.2f}",
                'amount_raw': float(amt),
                'account': n.bill_ref_number or "",
                'notes': "",
            })
        section_title = "Matched"
        section_badge = "bg-success"

    context = {
        'start_date': start,
        'end_date': end,
        'section': section,
        'section_title': section_title,
        'section_badge': section_badge,
        'table_headers': table_headers,
        'table_rows': table_rows,
        'total': _money(total),
        'row_count': len(table_rows),
        'section_counts': {
            'matched': len(matched_keys),
            'stmt_only': len(stmt_only_keys),
            'system_only': len(system_only_keys),
        },
        'notif_count': len(notif_ids),
        'record_count': len(record_ids),
    }
    return render(request, 'accounting/mpesa_reconciliation.html', context)


@login_required
def mpesa_reconciliation_push(request):
    """
    POST endpoint: push selected MpesaRecord rows into MpesaNotification so
    they can be posted to member accounts like normal C2B notifications.
    """
    from transactions.models import MpesaNotification

    if request.method != "POST":
        return redirect("accounting:mpesa_reconciliation")

    receipt_ids = request.POST.getlist("receipts")
    if not receipt_ids:
        messages.warning(request, "No records selected.")
        return redirect("accounting:mpesa_reconciliation")

    records = MpesaRecord.objects.filter(receipt_no__in=receipt_ids)
    pushed = 0
    skipped = 0
    ZERO = Decimal("0.00")
    for r in records:
        if MpesaNotification.objects.filter(trans_id=r.receipt_no).exists():
            skipped += 1
            continue
        MpesaNotification.objects.create(
            transaction_type="PAYBILL",
            trans_id=r.receipt_no,
            trans_time=r.completion_time,
            trans_amount=r.credit_amount or ZERO,
            business_shortcode="RECON",
            bill_ref_number=r.account or "",
            msisdn="",
            first_name=(r.details or "")[:50],
            posted=False,
        )
        pushed += 1

    messages.success(
        request,
        f"Pushed {pushed} record(s) to M-PESA notifications; "
        f"{skipped} already existed.",
    )
    qs = request.POST.get("redirect_query", "")
    return redirect(f"/accounting/mpesa-reconciliation/?{qs}")


def export_unmatched_excel(request):
    """Generates and streams an Excel sheet containing M-Pesa entries absent from Core Banking records"""
    match_date_str = request.GET.get('match_date')
    if not match_date_str:
        messages.error(request, "Cannot export file: Target matching date parameter is missing.")
        return redirect('accounting:mpesa_reconciliation')

    match_date = parse_date(match_date_str)
    if not match_date:
        messages.error(request, "Cannot export file: Invalid date format provided.")
        return redirect('accounting:mpesa_reconciliation')

    cbs_doc_numbers = CoreBankingRecord.objects.filter(date=match_date).values_list('document_no', flat=True)
    unmatched_records = MpesaRecord.objects.filter(completion_time__date=match_date).exclude(receipt_no__in=cbs_doc_numbers)

    if not unmatched_records.exists():
        messages.warning(request, f"No unmatched records found to export for {match_date_str}.")
        return redirect('accounting:mpesa_reconciliation')

    data = []
    for rec in unmatched_records:
        data.append({
            'ReceiptNo': rec.receipt_no,
            'CompletionTime': rec.completion_time.replace(tzinfo=None),
            'Details': rec.details,
            'CreditAmount': rec.credit_amount,
            'DebitAmount': rec.debit_amount,
            'Account': rec.account
        })

    df = pd.DataFrame(data)
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename=Unmatched_Mpesa_{match_date_str}.xlsx'
    
    with pd.ExcelWriter(response, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Unmatched')

    return response

from django.shortcuts import render
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.utils.dateparse import parse_date
from .models import CoreBankingRecord, MpesaRecord

def view_uploaded_records(request):
    """Filters and displays uploaded Core Banking or Mpesa statement entries with pagination fallback"""
    record_type = request.GET.get('record_type', 'mpesa')
    selected_date_str = request.GET.get('selected_date', '').strip()
    
    records = []
    is_cbs = (record_type == 'core_banking')

    if is_cbs:
        # Core Banking path
        queryset = CoreBankingRecord.objects.all().order_by('-date', '-id')
        if selected_date_str:
            selected_date = parse_date(selected_date_str)
            if selected_date:
                queryset = queryset.filter(date=selected_date)
    else:
        # M-Pesa path (Default)
        queryset = MpesaRecord.objects.all().order_by('-completion_time', '-id')
        if selected_date_str:
            selected_date = parse_date(selected_date_str)
            if selected_date:
                queryset = queryset.filter(completion_time__date=selected_date)
        else:
            # Default fallback: strictly isolate to the last 100 records if no specific date is provided
            queryset = queryset[:100]

    # Paginate results - 20 records per page
    paginator = Paginator(queryset, 20)
    page_number = request.GET.get('page', 1)
    
    try:
        page_obj = paginator.get_page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    context = {
        'page_obj': page_obj,
        'record_type': record_type,
        'selected_date': selected_date_str,
        'is_cbs': is_cbs,
    }
    return render(request, 'accounting/view_uploaded_records.html', context)