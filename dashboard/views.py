from datetime import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Sum, Q, Count
from django.db.models.functions import Coalesce, TruncMonth
from django.shortcuts import render, redirect

from accounts.models import CustomUser
from administration.models import ChamaInfo

from customers.models import Customer
from transactions.models import (
    MpesaNotification, SavingsTransaction,
    LoanTransaction, DividendBatch,
    CustomerAccountsSetup,
)

def user_profile(request):
    user = request.user
    return render(request, "dashboard/user_profile.html", {"user": user})


# ═══════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════

@login_required
def dashboard_view(request):
    # --- 1. Basic Stats ---
    total_members = Customer.objects.count()

    aggregation = MpesaNotification.objects.filter(posted=False).aggregate(total=Sum('trans_amount'))
    total_pending = aggregation['total'] or 0

    # --- 2. Loan Portfolio Analysis ---
    loan_stats = LoanTransaction.objects.aggregate(
        principal=Coalesce(Sum('debit_amount', filter=Q(tr_desc='Principal Disbursement')), Decimal(0)),
        interest_charged=Coalesce(Sum('debit_amount', filter=Q(tr_desc='Upfront Interest Charge')), Decimal(0)),
        repaid=Coalesce(Sum('credit_amount'), Decimal(0)),
        total_debits=Coalesce(Sum('debit_amount'), Decimal(0)),
    )

    running_loan_balance = loan_stats['total_debits'] - loan_stats['repaid']

    # --- 3. Seed Deposits ---
    seed_stats = SavingsTransaction.objects.filter(saving_type='seed_deposit').aggregate(
        net_seed=Coalesce(Sum('credit_amount') - Sum('debit_amount'), Decimal(0)),
    )
    total_seed = seed_stats['net_seed']

    # --- 4. Loans Liquidity ---
    # = deposits (all savings credits - debits, EXCLUDING share_capital)
    #   + loan credits (repayments, fines, etc.)
    #   - loans disbursed (principal)
    deposits_excl_share = SavingsTransaction.objects.exclude(
        saving_type='share_capital'
    ).aggregate(
        net=Coalesce(Sum('credit_amount') - Sum('debit_amount'), Decimal(0)),
    )['net']
    loans_liquidity = deposits_excl_share + loan_stats['repaid'] - loan_stats['principal']

    # --- 5. Savings & Performance ---
    savings_query = SavingsTransaction.objects.values("saving_type").annotate(
        balance=Sum("credit_amount") - Sum("debit_amount"),
    )
    savings_data = []
    total_deposits = Decimal(0)
    share_capital_total = Decimal(0)
    for item in savings_query:
        bal = item['balance'] or Decimal(0)
        stype = item['saving_type'] or ''
        savings_data.append({
            'raw_type': stype,
            'display_name': stype.replace('_', ' ').title(),
            'balance': bal,
            'is_share': stype == 'share_capital',
        })
        total_deposits += bal
        if stype == 'share_capital':
            share_capital_total += bal

    savings_data.sort(key=lambda x: x['balance'], reverse=True)

    perf_stats = DividendBatch.objects.aggregate(
        income=Coalesce(Sum('total_fees'), Decimal(0)),
        expense=Coalesce(Sum('total_net_payout'), Decimal(0)),
    )

    # --- 6. Account Profiles & GL Mapping ---
    account_configs = CustomerAccountsSetup.objects.select_related(
        'sacco_gl_account'
    ).filter(is_active=True).order_by('account_code')

    total_mpesa_volume = MpesaNotification.objects.aggregate(
        v=Coalesce(Sum('trans_amount'), Decimal(0))
    )['v'] or Decimal(0)

    # --- 7. Loan Portfolio table data (like Savings Breakdown) ---
    loan_total = loan_stats['total_debits']  # sum of all debits as denominator
    def _pct(part, whole):
        if whole and whole > 0:
            return int(round(part / whole * 100))
        return None

    loan_portfolio_data = [
        {
            'label': 'Principal Disbursed',
            'amount': loan_stats['principal'],
            'color': '',
            'pct': _pct(loan_stats['principal'], loan_total),
            'bar_color': '#0E2B4D',
        },
        {
            'label': 'Interest Charged',
            'amount': loan_stats['interest_charged'],
            'color': '',
            'pct': _pct(loan_stats['interest_charged'], loan_total),
            'bar_color': '#6c757d',
        },
        {
            'label': 'Total Repaid',
            'amount': loan_stats['repaid'],
            'color': ' text-success',
            'pct': _pct(loan_stats['repaid'], loan_total),
            'bar_color': '#198754',
        },
        {
            'label': 'Outstanding Balance',
            'amount': running_loan_balance,
            'color': ' text-danger',
            'pct': _pct(abs(running_loan_balance), loan_total) if running_loan_balance else None,
            'bar_color': '#dc3545',
        },
    ]

    # --- 8. Income & M-Pesa table data ---
    income_total = perf_stats['income'] + perf_stats['expense'] + total_mpesa_volume
    income_data = [
        {
            'label': 'Fee Income',
            'amount': perf_stats['income'],
            'color': ' text-success',
            'pct': _pct(perf_stats['income'], income_total),
            'bar_color': '#198754',
        },
        {
            'label': 'Net Payouts',
            'amount': perf_stats['expense'],
            'color': '',
            'pct': _pct(perf_stats['expense'], income_total),
            'bar_color': '#6c757d',
        },
        {
            'label': 'Total M-Pesa Volume',
            'amount': total_mpesa_volume,
            'color': '',
            'pct': _pct(total_mpesa_volume, income_total),
            'bar_color': '#0dcaf0',
        },
    ]

    context = {
        "total_members": total_members,
        "total_pending": total_pending,
        "total_principal": loan_stats['principal'],
        "total_interest": loan_stats['interest_charged'],
        "total_repaid": loan_stats['repaid'],
        "running_loan_balance": running_loan_balance,
        "total_seed": total_seed,
        "loans_liquidity": loans_liquidity,
        "savings_data": savings_data,
        "total_deposits": total_deposits,
        "share_capital_total": share_capital_total,
        "account_configs": account_configs,
        "total_income_fees": perf_stats['income'],
        "total_expenses_interest": perf_stats['expense'],
        "total_mpesa_volume": total_mpesa_volume,
        "loan_portfolio_data": loan_portfolio_data,
        "income_data": income_data,
    }
    return render(request, "dashboard/dashboard.html", context)


# ═══════════════════════════════════════════════════════════════════════════
# DRILL-DOWN VIEWS
# ═══════════════════════════════════════════════════════════════════════════

@login_required
def savings_list_view(request):
    saving_type = request.GET.get('type')
    transactions = SavingsTransaction.objects.all().order_by('-tr_date')
    if saving_type:
        transactions = transactions.filter(saving_type=saving_type)

    context = {
        'transactions': transactions,
        'title': saving_type.replace('_', ' ').title() if saving_type else "All Savings",
    }
    return render(request, "dashboard/transaction_list.html", context)


@login_required
def loans_list_view(request):
    loan_type = request.GET.get('type')
    description = request.GET.get('desc')
    transactions = LoanTransaction.objects.all().order_by('-tr_date')

    if loan_type:
        transactions = transactions.filter(loan_type=loan_type)
    if description:
        transactions = transactions.filter(tr_desc=description)

    context = {
        'transactions': transactions,
        'title': f"Loan Records: {description if description else 'All'}",
    }
    return render(request, "dashboard/transaction_list.html", context)


# ═══════════════════════════════════════════════════════════════════════════
# SAVINGS MONTHLY REPORT
# ═══════════════════════════════════════════════════════════════════════════

@login_required
def savings_monthly_report(request):
    from dateutil.relativedelta import relativedelta
    from .utils import export_to_excel

    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')
    saving_type = request.GET.get('saving_type', 'share_capital')

    if not start_date_str or not end_date_str:
        start_date = datetime(datetime.now().year, 1, 1).date()
        end_date = datetime(datetime.now().year, 12, 31).date()
    else:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    months = []
    current = start_date.replace(day=1)
    while current <= end_date:
        months.append(current)
        current += relativedelta(months=1)

    members_queryset = Customer.objects.all().order_by('cust_no')

    is_export = request.GET.get('export') == 'excel'

    if is_export:
        members = members_queryset
        page_obj = None
    else:
        paginator = Paginator(members_queryset, 100)
        page_number = request.GET.get('page', 1)
        try:
            page_obj = paginator.page(page_number)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)

        members = page_obj.object_list

    tx_data = SavingsTransaction.objects.filter(
        tr_date__date__range=[start_date, end_date],
        saving_type=saving_type,
        cust_no__in=members.values_list('cust_no', flat=True),
    ).annotate(
        month=TruncMonth('tr_date'),
    ).values('cust_no', 'month').annotate(
        monthly_net=Sum('credit_amount') - Sum('debit_amount'),
    )

    savings_lookup = {
        (tx['cust_no'], tx['month'].date().replace(day=1)): tx['monthly_net']
        for tx in tx_data
    }

    report_rows = []
    for member in members:
        row = {
            'cust_no': member.cust_no,
            'full_name': f"{member.first_name} {member.last_name}",
            'monthly_values': [],
            'row_total': 0,
        }
        for m_date in months:
            amount = savings_lookup.get((member.cust_no, m_date), 0)
            row['monthly_values'].append(amount)
            row['row_total'] += amount
        report_rows.append(row)

    if is_export:
        headers = ['Cust No', 'Full Name'] + [m.strftime('%b %Y') for m in months] + ['Total']
        excel_data = []
        for row in report_rows:
            data_row = [row['cust_no'], row['full_name']] + row['monthly_values'] + [row['row_total']]
            excel_data.append(data_row)
        filename = f"{saving_type}_Report_{datetime.now().strftime('%Y%m%d')}"
        return export_to_excel(filename, headers, excel_data)

    context = {
        'months': [m.strftime('%b %Y') for m in months],
        'report_rows': report_rows,
        'page_obj': page_obj,
        'start_date': start_date.strftime('%Y-%m-%d'),
        'end_date': end_date.strftime('%Y-%m-%d'),
        'saving_type': saving_type,
        'title': f"Monthly {saving_type.replace('_', ' ').title()} Report",
    }
    return render(request, "dashboard/savings_monthly_report.html", context)
