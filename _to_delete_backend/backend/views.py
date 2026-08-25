# androidadminapi/views.py
# ═══════════════════════════════════════════════════════════════════════════
# NODiLite Admin API — Views for chama official mobile app
# ═══════════════════════════════════════════════════════════════════════════

import uuid
import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum, Q, Count
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import CustomUser
from customers.models import Customer, NextOfKin
from transactions.models import SavingsTransaction, LoanTransaction, CustomerAccountsSetup
from loans.models import LoanHistory, Guarantor
from administration.models import ChamaInfo

from .serializers import (
    AdminUserSerializer, AdminLoginSerializer, ChamaInfoSerializer,
    CustomerListSerializer, CustomerDetailSerializer, MemberRegistrationSerializer,
    NextOfKinSerializer, AccountTypeSerializer,
    SavingsTransactionSerializer, LoanTransactionSerializer,
    RecordSavingsPaymentSerializer, RecordLoanPaymentSerializer,
    DisburseLoanSerializer, RecordFineSerializer,
    LoanHistorySerializer, GuarantorSerializer,
)

logger = logging.getLogger(__name__)

# ── Role gate: only chama officials ──────────────────────────────────────
OFFICIAL_ROLES = ('admin', 'manager', 'loan_officer', 'accounts_clerk')


def _require_official(user):
    """Returns error Response if user isn't a chama official, else None."""
    if user.role not in OFFICIAL_ROLES:
        return Response(
            {'error': 'Access denied. Officials only.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def _pad_cust_no(raw: str) -> str:
    s = raw.strip()
    return s.zfill(5) if s.isdigit() else s


def _generate_tr_ref(prefix='ADM'):
    """Generate a unique transaction reference."""
    ts = timezone.now().strftime('%Y%m%d%H%M%S')
    short_uuid = uuid.uuid4().hex[:6].upper()
    return f'{prefix}-{ts}-{short_uuid}'


# ════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION
# ════════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([AllowAny])
def admin_login_view(request):
    """POST /androidadminapi/auth/login/
    Officials login with username + password. Returns JWT tokens."""
    serializer = AdminLoginSerializer(data=request.data)
    if not serializer.is_valid():
        errors = serializer.errors
        msg = ''
        if 'non_field_errors' in errors:
            msg = errors['non_field_errors'][0]
        else:
            msg = str(errors)
        return Response({'error': msg}, status=status.HTTP_401_UNAUTHORIZED)

    user = serializer.validated_data['user']
    refresh = RefreshToken.for_user(user)

    return Response({
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'username': user.username,
        'email': user.email,
        'role': user.role,
        'first_name': user.first_name,
        'last_name': user.last_name,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def admin_logout_view(request):
    """POST /androidadminapi/auth/logout/"""
    try:
        refresh = request.data.get('refresh', '')
        if refresh:
            token = RefreshToken(refresh)
            token.blacklist()
    except Exception:
        pass
    return Response({'message': 'Logged out successfully.'})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def admin_current_user_view(request):
    """GET /androidadminapi/auth/user/"""
    gate = _require_official(request.user)
    if gate:
        return gate
    return Response(AdminUserSerializer(request.user).data)


# ════════════════════════════════════════════════════════════════════════════
# CHAMA BRANDING
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def chama_info_view(request):
    """GET /androidadminapi/chama-info/"""
    info = ChamaInfo.objects.first()
    if not info:
        return Response({
            'brand_name': 'NODi Lite',
            'chama_name': '',
            'chama_address': '',
            'chama_contact': '',
            'chama_location': '',
            'chama_footer': '',
            'chama_logo_url': '',
        })
    return Response(ChamaInfoSerializer(info, context={'request': request}).data)


# ════════════════════════════════════════════════════════════════════════════
# DASHBOARD / OVERVIEW
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_stats_view(request):
    """GET /androidadminapi/dashboard/stats/
    Returns chama-wide financial summary for the dashboard."""
    gate = _require_official(request.user)
    if gate:
        return gate

    # Member counts
    total_members = Customer.objects.filter(customer_status='active').count()
    new_this_month = Customer.objects.filter(
        customer_status='active',
        reg_date__year=date.today().year,
        reg_date__month=date.today().month,
    ).count()

    # Total savings (all members)
    sav_credits = SavingsTransaction.objects.aggregate(
        t=Sum('credit_amount'))['t'] or Decimal('0')
    sav_debits = SavingsTransaction.objects.aggregate(
        t=Sum('debit_amount'))['t'] or Decimal('0')
    total_savings = sav_credits - sav_debits

    # Total loans outstanding
    loan_debits = LoanTransaction.objects.aggregate(
        t=Sum('debit_amount'))['t'] or Decimal('0')
    loan_credits = LoanTransaction.objects.aggregate(
        t=Sum('credit_amount'))['t'] or Decimal('0')
    total_loans = loan_debits - loan_credits

    # Active loans count
    active_loans = LoanHistory.objects.filter(
        is_disbursed=True,
    ).count()

    # Pending approvals
    pending_loans = LoanHistory.objects.filter(
        is_approved=False, is_disbursed=False,
    ).count()

    # Today's collections
    today = date.today()
    today_savings = SavingsTransaction.objects.filter(
        tr_date__date=today, credit_amount__gt=0,
    ).aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
    today_loan_payments = LoanTransaction.objects.filter(
        tr_date__date=today, credit_amount__gt=0,
    ).aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')

    return Response({
        'total_members': total_members,
        'new_members_this_month': new_this_month,
        'total_savings': float(total_savings),
        'total_loans_outstanding': float(total_loans),
        'active_loans': active_loans,
        'pending_loan_approvals': pending_loans,
        'today_savings_collected': float(today_savings),
        'today_loan_payments': float(today_loan_payments),
        'today_total_collections': float(today_savings + today_loan_payments),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def recent_transactions_view(request):
    """GET /androidadminapi/dashboard/recent-transactions/
    Returns the 20 most recent transactions across all members."""
    gate = _require_official(request.user)
    if gate:
        return gate

    limit = int(request.query_params.get('limit', 20))

    savings = SavingsTransaction.objects.order_by('-created_at')[:limit]
    loans = LoanTransaction.objects.order_by('-created_at')[:limit]

    return Response({
        'savings': SavingsTransactionSerializer(savings, many=True).data,
        'loans': LoanTransactionSerializer(loans, many=True).data,
    })


# ════════════════════════════════════════════════════════════════════════════
# MEMBERS
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_list_view(request):
    """GET /androidadminapi/members/?search=&status=active"""
    gate = _require_official(request.user)
    if gate:
        return gate

    qs = Customer.objects.all().order_by('cust_no')

    # Filters
    status_filter = request.query_params.get('status', '')
    if status_filter:
        qs = qs.filter(customer_status=status_filter)

    search = request.query_params.get('search', '').strip()
    if search:
        qs = qs.filter(
            Q(full_name__icontains=search) |
            Q(cust_no__icontains=search) |
            Q(phone__icontains=search) |
            Q(national_id__icontains=search)
        )

    # Pagination
    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 50))
    start = (page - 1) * page_size
    end = start + page_size

    total = qs.count()
    members = qs[start:end]

    return Response({
        'count': total,
        'page': page,
        'page_size': page_size,
        'results': CustomerListSerializer(members, many=True).data,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def member_register_view(request):
    """POST /androidadminapi/members/register/
    Register a new chama member."""
    gate = _require_official(request.user)
    if gate:
        return gate

    serializer = MemberRegistrationSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    customer = Customer(
        full_name=data['full_name'],
        first_name=data.get('first_name', ''),
        middle_name=data.get('middle_name', ''),
        last_name=data.get('last_name', ''),
        gender=data.get('gender'),
        dob=data.get('dob'),
        phone=data['phone'],
        national_id=data.get('national_id', ''),
        kra_pin=data.get('kra_pin', ''),
        reg_email=data.get('reg_email', ''),
        town=data.get('town', ''),
        postal_address=data.get('postal_address', ''),
        customer_type=data.get('customer_type', 'adult_individual'),
        customer_status='active',
        registered_by=request.user,
    )
    customer.save()

    return Response({
        'message': f'Member {customer.full_name} registered successfully.',
        'cust_no': customer.cust_no,
        'member': CustomerDetailSerializer(customer).data,
    }, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_detail_view(request, cust_no):
    """GET /androidadminapi/members/<cust_no>/"""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    try:
        customer = Customer.objects.get(cust_no=padded)
    except Customer.DoesNotExist:
        return Response({'error': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

    # Calculate balances for this member
    sav_credits = SavingsTransaction.objects.filter(
        cust_no=padded).aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
    sav_debits = SavingsTransaction.objects.filter(
        cust_no=padded).aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
    savings_balance = sav_credits - sav_debits

    loan_debits = LoanTransaction.objects.filter(
        cust_no=padded).aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
    loan_credits = LoanTransaction.objects.filter(
        cust_no=padded).aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
    loan_balance = loan_debits - loan_credits

    data = CustomerDetailSerializer(customer).data
    data['savings_balance'] = float(savings_balance)
    data['loan_balance'] = float(loan_balance)
    data['net_balance'] = float(savings_balance - loan_balance)

    return Response(data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_accounts_view(request, cust_no):
    """GET /androidadminapi/members/<cust_no>/accounts/
    Returns all account balances for a member."""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    if not Customer.objects.filter(cust_no=padded).exists():
        return Response({'error': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

    account_types = CustomerAccountsSetup.objects.filter(is_active=True)
    accounts = []

    for acct in account_types:
        if acct.is_loan_account:
            debits = LoanTransaction.objects.filter(
                cust_no=padded, loan_type=acct.account_type,
            ).aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
            credits = LoanTransaction.objects.filter(
                cust_no=padded, loan_type=acct.account_type,
            ).aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
            balance = debits - credits
        else:
            credits = SavingsTransaction.objects.filter(
                cust_no=padded, saving_type=acct.account_type,
            ).aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
            debits = SavingsTransaction.objects.filter(
                cust_no=padded, saving_type=acct.account_type,
            ).aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
            balance = credits - debits

        accounts.append({
            'account_code': acct.account_code,
            'account_name': acct.account_name,
            'account_type': acct.account_type,
            'is_loan': acct.is_loan_account,
            'balance': float(balance),
        })

    return Response({'cust_no': padded, 'accounts': accounts})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_kins_view(request, cust_no):
    """GET /androidadminapi/members/<cust_no>/kins/"""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    try:
        customer = Customer.objects.get(cust_no=padded)
    except Customer.DoesNotExist:
        return Response({'error': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

    kins = NextOfKin.objects.filter(customer=customer)
    return Response(NextOfKinSerializer(kins, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_loans_view(request, cust_no):
    """GET /androidadminapi/members/<cust_no>/loans/"""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    try:
        customer = Customer.objects.get(cust_no=padded)
    except Customer.DoesNotExist:
        return Response({'error': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

    loans = LoanHistory.objects.filter(customer=customer).order_by('-loan_date')
    return Response(LoanHistorySerializer(loans, many=True).data)


# ════════════════════════════════════════════════════════════════════════════
# ACCOUNT TYPES
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def account_types_view(request):
    """GET /androidadminapi/account-types/"""
    gate = _require_official(request.user)
    if gate:
        return gate
    qs = CustomerAccountsSetup.objects.filter(is_active=True)
    return Response(AccountTypeSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def savings_account_types_view(request):
    """GET /androidadminapi/account-types/savings/"""
    gate = _require_official(request.user)
    if gate:
        return gate
    qs = CustomerAccountsSetup.objects.filter(is_active=True, is_loan_account=False)
    return Response(AccountTypeSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def loan_account_types_view(request):
    """GET /androidadminapi/account-types/loans/"""
    gate = _require_official(request.user)
    if gate:
        return gate
    qs = CustomerAccountsSetup.objects.filter(is_active=True, is_loan_account=True)
    return Response(AccountTypeSerializer(qs, many=True).data)


# ════════════════════════════════════════════════════════════════════════════
# TRANSACTION RECORDING (OFFICIAL ACTIONS)
# ════════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def record_savings_payment_view(request):
    """POST /androidadminapi/actions/record-savings/
    Official records a savings deposit for a member."""
    gate = _require_official(request.user)
    if gate:
        return gate

    serializer = RecordSavingsPaymentSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    cust_no = data['cust_no']
    saving_type = data['saving_type']
    amount = Decimal(data['amount'])

    # Look up account code
    try:
        acct = CustomerAccountsSetup.objects.get(account_type=saving_type, is_active=True)
        account_code = acct.account_code
    except CustomerAccountsSetup.DoesNotExist:
        return Response({'error': 'Invalid account type.'}, status=status.HTTP_400_BAD_REQUEST)

    tr_ref = _generate_tr_ref('SAV')

    txn = SavingsTransaction.objects.create(
        cust_no=cust_no,
        saving_type=saving_type,
        account_code=account_code,
        tr_date=timezone.now(),
        tr_ref=tr_ref,
        ext_ref=data.get('ext_ref', ''),
        tr_desc=data.get('description', 'Admin deposit'),
        debit_amount=Decimal('0'),
        credit_amount=amount,
        created_by=request.user.username,
    )

    return Response({
        'message': f'KES {amount:,.2f} deposited to {acct.account_name} for member {cust_no}.',
        'tr_ref': tr_ref,
        'transaction': SavingsTransactionSerializer(txn).data,
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def record_loan_payment_view(request):
    """POST /androidadminapi/actions/record-loan-payment/
    Official records a loan repayment for a member."""
    gate = _require_official(request.user)
    if gate:
        return gate

    serializer = RecordLoanPaymentSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    cust_no = data['cust_no']
    loan_no = data['loan_no'].strip()
    amount = Decimal(data['amount'])

    # Verify loan exists
    try:
        loan = LoanHistory.objects.get(loan_no=loan_no, customer__cust_no=cust_no)
    except LoanHistory.DoesNotExist:
        return Response({'error': 'Loan not found for this member.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        acct = loan.loan_type
        account_code = acct.account_code
        loan_type = acct.account_type
    except Exception:
        account_code = ''
        loan_type = ''

    tr_ref = _generate_tr_ref('LPY')

    txn = LoanTransaction.objects.create(
        cust_no=cust_no,
        loan_id=loan.id,
        loan_no=loan_no,
        loan_type=loan_type,
        account_code=account_code,
        tr_date=timezone.now(),
        tr_ref=tr_ref,
        ext_ref=data.get('ext_ref', ''),
        tr_desc=data.get('description', 'Loan repayment'),
        debit_amount=Decimal('0'),
        credit_amount=amount,
        created_by=request.user.username,
    )

    return Response({
        'message': f'KES {amount:,.2f} loan repayment recorded for {loan_no}.',
        'tr_ref': tr_ref,
        'transaction': LoanTransactionSerializer(txn).data,
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def disburse_loan_view(request):
    """POST /androidadminapi/actions/disburse-loan/
    Official disburses a loan to a member."""
    gate = _require_official(request.user)
    if gate:
        return gate

    # Only managers and admins can disburse
    if request.user.role not in ('admin', 'manager'):
        return Response(
            {'error': 'Only managers/admins can disburse loans.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    serializer = DisburseLoanSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    cust_no = data['cust_no']
    amount = Decimal(data['amount'])
    loan_type_code = data['loan_type']
    period = data['period']

    customer = Customer.objects.get(cust_no=cust_no)
    loan_product = CustomerAccountsSetup.objects.get(
        account_type=loan_type_code, is_loan_account=True, is_active=True,
    )

    # Calculate interest & installment
    interest_rate = Decimal('12.0')  # Default; real systems pull from product config
    monthly_rate = interest_rate / Decimal('12') / Decimal('100')
    if loan_product.interest_calc_method == 'flat_rate':
        total_interest = amount * interest_rate / Decimal('100') * period / Decimal('12')
        installment = (amount + total_interest) / period
    else:
        # Reducing balance
        if monthly_rate > 0:
            installment = amount * monthly_rate / (1 - (1 + monthly_rate) ** (-period))
        else:
            installment = amount / period

    net_disbursed = amount  # Charges would be deducted in production

    loan = LoanHistory(
        customer=customer,
        loan_date=date.today(),
        principal=amount,
        installment=installment.quantize(Decimal('0.01')),
        loan_type=loan_product,
        loan_period=period,
        interest_rate=interest_rate,
        net_disbursed=net_disbursed,
        is_approved=True,
        approved_by=request.user.username,
        approved_at=timezone.now(),
        is_disbursed=True,
        disbursed_at=timezone.now(),
        created_by=request.user.username,
    )
    loan.save()

    # Record disbursement transaction
    tr_ref = _generate_tr_ref('DIS')
    LoanTransaction.objects.create(
        cust_no=cust_no,
        loan_id=loan.id,
        loan_no=loan.loan_no,
        loan_type=loan_type_code,
        account_code=loan_product.account_code,
        tr_date=timezone.now(),
        tr_ref=tr_ref,
        tr_desc=f'Loan disbursement - {data.get("purpose", "")}',
        debit_amount=amount,
        credit_amount=Decimal('0'),
        created_by=request.user.username,
    )

    return Response({
        'message': f'Loan {loan.loan_no} disbursed: KES {amount:,.2f} to member {cust_no}.',
        'loan_no': loan.loan_no,
        'principal': float(amount),
        'installment': float(installment.quantize(Decimal('0.01'))),
        'period': period,
        'net_disbursed': float(net_disbursed),
        'tr_ref': tr_ref,
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def record_fine_view(request):
    """POST /androidadminapi/actions/record-fine/
    Official imposes a fine on a member (debit against savings)."""
    gate = _require_official(request.user)
    if gate:
        return gate

    serializer = RecordFineSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    cust_no = data['cust_no']
    amount = Decimal(data['amount'])
    reason = data['reason']

    # Fines go as a debit on the primary savings account
    primary_savings = CustomerAccountsSetup.objects.filter(
        is_active=True, is_loan_account=False,
    ).first()

    if not primary_savings:
        return Response({'error': 'No savings account type configured.'}, status=status.HTTP_400_BAD_REQUEST)

    tr_ref = _generate_tr_ref('FIN')

    txn = SavingsTransaction.objects.create(
        cust_no=cust_no,
        saving_type=primary_savings.account_type,
        account_code=primary_savings.account_code,
        tr_date=timezone.now(),
        tr_ref=tr_ref,
        tr_desc=f'Fine: {reason}',
        debit_amount=amount,
        credit_amount=Decimal('0'),
        created_by=request.user.username,
    )

    return Response({
        'message': f'Fine of KES {amount:,.2f} recorded for member {cust_no}. Reason: {reason}',
        'tr_ref': tr_ref,
        'transaction': SavingsTransactionSerializer(txn).data,
    }, status=status.HTTP_201_CREATED)


# ════════════════════════════════════════════════════════════════════════════
# TRANSACTION LOOKUP
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def savings_transactions_view(request):
    """GET /androidadminapi/transactions/savings/?date=&page="""
    gate = _require_official(request.user)
    if gate:
        return gate

    qs = SavingsTransaction.objects.all().order_by('-tr_date')

    date_filter = request.query_params.get('date', '')
    if date_filter:
        qs = qs.filter(tr_date__date=date_filter)

    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 50))
    start = (page - 1) * page_size

    return Response({
        'count': qs.count(),
        'results': SavingsTransactionSerializer(qs[start:start + page_size], many=True).data,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def loan_transactions_view(request):
    """GET /androidadminapi/transactions/loans/?date=&page="""
    gate = _require_official(request.user)
    if gate:
        return gate

    qs = LoanTransaction.objects.all().order_by('-tr_date')

    date_filter = request.query_params.get('date', '')
    if date_filter:
        qs = qs.filter(tr_date__date=date_filter)

    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 50))
    start = (page - 1) * page_size

    return Response({
        'count': qs.count(),
        'results': LoanTransactionSerializer(qs[start:start + page_size], many=True).data,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_savings_transactions_view(request, cust_no):
    """GET /androidadminapi/transactions/savings/<cust_no>/"""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    qs = SavingsTransaction.objects.filter(cust_no=padded).order_by('-tr_date')
    return Response({
        'count': qs.count(),
        'results': SavingsTransactionSerializer(qs[:100], many=True).data,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_loan_transactions_view(request, cust_no):
    """GET /androidadminapi/transactions/loans/<cust_no>/"""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    qs = LoanTransaction.objects.filter(cust_no=padded).order_by('-tr_date')
    return Response({
        'count': qs.count(),
        'results': LoanTransactionSerializer(qs[:100], many=True).data,
    })


# ════════════════════════════════════════════════════════════════════════════
# STATEMENTS
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_full_statement_view(request, cust_no):
    """GET /androidadminapi/statements/<cust_no>/full/?from_date=&to_date="""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    try:
        customer = Customer.objects.get(cust_no=padded)
    except Customer.DoesNotExist:
        return Response({'error': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

    from_date = request.query_params.get('from_date', '')
    to_date = request.query_params.get('to_date', '')

    if not from_date:
        from_date = (date.today() - timedelta(days=365)).isoformat()
    if not to_date:
        to_date = date.today().isoformat()

    account_types = CustomerAccountsSetup.objects.filter(is_active=True)
    accounts_data = []

    for acct in account_types:
        if acct.is_loan_account:
            # Get all loan transactions for this type
            before_period = LoanTransaction.objects.filter(
                cust_no=padded, loan_type=acct.account_type,
                tr_date__date__lt=from_date,
            )
            bf_debit = before_period.aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
            bf_credit = before_period.aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
            opening = bf_debit - bf_credit

            period_txns = LoanTransaction.objects.filter(
                cust_no=padded, loan_type=acct.account_type,
                tr_date__date__gte=from_date, tr_date__date__lte=to_date,
            ).order_by('tr_date')
        else:
            before_period = SavingsTransaction.objects.filter(
                cust_no=padded, saving_type=acct.account_type,
                tr_date__date__lt=from_date,
            )
            bf_credit = before_period.aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
            bf_debit = before_period.aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
            opening = bf_credit - bf_debit

            period_txns = SavingsTransaction.objects.filter(
                cust_no=padded, saving_type=acct.account_type,
                tr_date__date__gte=from_date, tr_date__date__lte=to_date,
            ).order_by('tr_date')

        if not period_txns.exists() and opening == 0:
            continue

        lines = []
        running = opening
        for txn in period_txns:
            debit = float(txn.debit_amount)
            credit = float(txn.credit_amount)
            if acct.is_loan_account:
                running = running + Decimal(str(debit)) - Decimal(str(credit))
            else:
                running = running + Decimal(str(credit)) - Decimal(str(debit))
            lines.append({
                'date': txn.tr_date.strftime('%Y-%m-%d'),
                'ref': txn.tr_ref,
                'description': txn.tr_desc,
                'debit': debit if debit > 0 else None,
                'credit': credit if credit > 0 else None,
                'balance': float(running),
            })

        accounts_data.append({
            'account_type': acct.account_type,
            'account_name': acct.account_name,
            'period_from': from_date,
            'period_to': to_date,
            'balance_brought_forward': float(opening),
            'lines': lines,
            'closing_balance': float(running),
        })

    # Chama info for header
    chama = ChamaInfo.objects.first()
    chama_data = None
    if chama:
        chama_data = {
            'chama_name': chama.chama_name,
            'chama_address': chama.chama_address,
            'chama_contact': chama.chama_contact,
        }

    return Response({
        'chama_info': chama_data,
        'cust_no': padded,
        'full_name': customer.full_name,
        'statement_date': date.today().isoformat(),
        'period_from': from_date,
        'period_to': to_date,
        'accounts': accounts_data,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_account_statement_view(request, cust_no, account_type):
    """GET /androidadminapi/statements/<cust_no>/<account_type>/"""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    try:
        customer = Customer.objects.get(cust_no=padded)
    except Customer.DoesNotExist:
        return Response({'error': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        acct = CustomerAccountsSetup.objects.get(account_type=account_type)
    except CustomerAccountsSetup.DoesNotExist:
        return Response({'error': 'Invalid account type.'}, status=status.HTTP_404_NOT_FOUND)

    from_date = request.query_params.get('from_date', (date.today() - timedelta(days=365)).isoformat())
    to_date = request.query_params.get('to_date', date.today().isoformat())

    if acct.is_loan_account:
        before = LoanTransaction.objects.filter(
            cust_no=padded, loan_type=account_type, tr_date__date__lt=from_date)
        bf_d = before.aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
        bf_c = before.aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
        opening = bf_d - bf_c
        txns = LoanTransaction.objects.filter(
            cust_no=padded, loan_type=account_type,
            tr_date__date__gte=from_date, tr_date__date__lte=to_date,
        ).order_by('tr_date')
    else:
        before = SavingsTransaction.objects.filter(
            cust_no=padded, saving_type=account_type, tr_date__date__lt=from_date)
        bf_c = before.aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
        bf_d = before.aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
        opening = bf_c - bf_d
        txns = SavingsTransaction.objects.filter(
            cust_no=padded, saving_type=account_type,
            tr_date__date__gte=from_date, tr_date__date__lte=to_date,
        ).order_by('tr_date')

    lines = []
    running = opening
    for txn in txns:
        d = float(txn.debit_amount)
        c = float(txn.credit_amount)
        if acct.is_loan_account:
            running += Decimal(str(d)) - Decimal(str(c))
        else:
            running += Decimal(str(c)) - Decimal(str(d))
        lines.append({
            'date': txn.tr_date.strftime('%Y-%m-%d'),
            'ref': txn.tr_ref,
            'description': txn.tr_desc,
            'debit': d if d > 0 else None,
            'credit': c if c > 0 else None,
            'balance': float(running),
        })

    return Response({
        'cust_no': padded,
        'full_name': customer.full_name,
        'account_type': account_type,
        'account_name': acct.account_name,
        'period_from': from_date,
        'period_to': to_date,
        'balance_brought_forward': float(opening),
        'lines': lines,
        'closing_balance': float(running),
    })


# ════════════════════════════════════════════════════════════════════════════
# LOANS MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def loans_list_view(request):
    """GET /androidadminapi/loans/?status=disbursed&page="""
    gate = _require_official(request.user)
    if gate:
        return gate

    qs = LoanHistory.objects.all().order_by('-loan_date')

    loan_status = request.query_params.get('status', '')
    if loan_status == 'disbursed':
        qs = qs.filter(is_disbursed=True)
    elif loan_status == 'pending':
        qs = qs.filter(is_approved=False, is_disbursed=False)
    elif loan_status == 'approved':
        qs = qs.filter(is_approved=True, is_disbursed=False)

    search = request.query_params.get('search', '').strip()
    if search:
        qs = qs.filter(
            Q(loan_no__icontains=search) |
            Q(customer__full_name__icontains=search) |
            Q(customer__cust_no__icontains=search)
        )

    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 50))
    start = (page - 1) * page_size

    return Response({
        'count': qs.count(),
        'results': LoanHistorySerializer(qs[start:start + page_size], many=True).data,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def loans_pending_view(request):
    """GET /androidadminapi/loans/pending/"""
    gate = _require_official(request.user)
    if gate:
        return gate

    qs = LoanHistory.objects.filter(
        is_approved=False, is_disbursed=False,
    ).order_by('-created_at')

    return Response({
        'count': qs.count(),
        'results': LoanHistorySerializer(qs, many=True).data,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def loan_approve_view(request, loan_no):
    """POST /androidadminapi/loans/<loan_no>/approve/"""
    gate = _require_official(request.user)
    if gate:
        return gate

    if request.user.role not in ('admin', 'manager', 'loan_officer'):
        return Response(
            {'error': 'Insufficient permissions to approve loans.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        loan = LoanHistory.objects.get(loan_no=loan_no)
    except LoanHistory.DoesNotExist:
        return Response({'error': 'Loan not found.'}, status=status.HTTP_404_NOT_FOUND)

    if loan.is_approved:
        return Response({'error': 'Loan already approved.'}, status=status.HTTP_400_BAD_REQUEST)

    loan.is_approved = True
    loan.approved_by = request.user.username
    loan.approved_at = timezone.now()
    loan.save(update_fields=['is_approved', 'approved_by', 'approved_at'])

    return Response({
        'message': f'Loan {loan_no} approved successfully.',
        'loan': LoanHistorySerializer(loan).data,
    })
