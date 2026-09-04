# androidadminapi/views.py
# ═══════════════════════════════════════════════════════════════════════════
# NODiLite Admin API — Views for chama official mobile app
# ═══════════════════════════════════════════════════════════════════════════

import uuid
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import Sum, Q, Count
from django.utils import timezone
from django.utils.timezone import localtime
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
from accounting.models import SaccoAccount, JournalVoucher, JournalVoucherLine

from .serializers import (
    AdminUserSerializer, AdminLoginSerializer, ChamaInfoSerializer,
    CustomerListSerializer, CustomerDetailSerializer, MemberRegistrationSerializer,
    NextOfKinSerializer, AccountTypeSerializer,
    SavingsTransactionSerializer, LoanTransactionSerializer,
    RecordSavingsPaymentSerializer, RecordLoanPaymentSerializer,
    DisburseLoanSerializer, RecordFineSerializer,
    LoanHistorySerializer, GuarantorSerializer,
    MemberSearchSerializer, UnsettledLoanSerializer,
    CashAccountSerializer, JournalEntrySerializer,
    LoanApplicationSerializer,
    RequestOtpSerializer, VerifyOtpSerializer, ResetPasswordSerializer,
)

from .mpesa_service import (
    initiate_b2c_payment, initiate_stk_push,
    MpesaConfigError, MpesaApiError, format_phone,
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


def _get_chama_name():
    """Get chama name from ChamaInfo for login response."""
    try:
        info = ChamaInfo.objects.first()
        return info.chama_name if info else 'NODi Lite'
    except Exception:
        return 'NODi Lite'


def _generate_tr_ref(prefix='ADM'):
    """Generate a unique transaction reference."""
    ts = timezone.now().strftime('%Y%m%d%H%M%S')
    short_uuid = uuid.uuid4().hex[:6].upper()
    return f'{prefix}-{ts}-{short_uuid}'



def _parse_tr_date(data, field="tr_date"):
    """Parse a date string (YYYY-MM-DD) from request data.
    Returns a timezone-aware datetime if provided, otherwise timezone.now()."""
    raw = data.get(field, "").strip() if hasattr(data, "get") else ""
    if raw:
        try:
            d = datetime.strptime(raw, "%Y-%m-%d")
            return timezone.make_aware(d) if timezone.is_naive(d) else d
        except (ValueError, TypeError):
            pass
    return timezone.now()

# ════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION
# ════════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([AllowAny])
def admin_login_view(request):
    """POST /androidadminapi/auth/login/
    Officials login with username + password + device_id. Returns JWT tokens.
    Device locking: first login binds the user to that device_id.
    Subsequent logins from a different device_id are rejected."""
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

    # ── Device lock check ─────────────────────────────────────────────
    device_id = request.data.get('device_id', '').strip()
    if device_id:
        if user.device_id and user.device_id != device_id:
            return Response(
                {'error': 'This account is locked to another device. Contact your administrator.'},
                status=status.HTTP_403_FORBIDDEN
            )
        if not user.device_id:
            # First login — bind to this device
            user.device_id = device_id
            user.save(update_fields=['device_id'])

    refresh = RefreshToken.for_user(user)

    return Response({
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'username': user.username,
        'email': user.email,
        'role': user.role,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'chama_name': _get_chama_name(),
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


@api_view(['GET'])
@permission_classes([AllowAny])
def chama_name_public_view(request):
    """GET /androidadminapi/chama-name/  (no auth required)
    Returns only the chama name for display on the login screen."""
    info = ChamaInfo.objects.first()
    return Response({
        'chama_name': info.chama_name if info else '',
    })


# ════════════════════════════════════════════════════════════════════════════
# DASHBOARD / OVERVIEW
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_stats_view(request):
    from loans.models import RunningLoanStat
    """GET /androidadminapi/dashboard/stats/
    Returns chama-wide financial summary for the dashboard."""
    gate = _require_official(request.user)
    if gate:
        return gate

    today = date.today()
    first_of_month = today.replace(day=1)
    first_of_year = today.replace(month=1, day=1)

    # Member counts
    total_members = Customer.objects.count()
    active_members = Customer.objects.filter(customer_status='active').count()
    new_this_month = Customer.objects.filter(
        customer_status='active',
        reg_date__year=today.year,
        reg_date__month=today.month,
    ).count()

    # ── Deposits breakdown by savings account type ────────────────────
    # NOTE: SavingsTransaction.saving_type stores the account_type string
    # (e.g. 'share_capital', 'welfare_deposit') which maps to
    # CustomerAccountsSetup.account_type. Some older transactions may not
    # have account_code set, so we query by saving_type for completeness.
    savings_account_types = CustomerAccountsSetup.objects.filter(
        is_loan_account=False, is_active=True,
    ).order_by('account_code')

    deposits_breakdown = []
    total_deposits = Decimal('0')
    for acct in savings_account_types:
        credits = SavingsTransaction.objects.filter(
            saving_type=acct.account_type,
        ).aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
        debits = SavingsTransaction.objects.filter(
            saving_type=acct.account_type,
        ).aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
        balance = credits - debits
        total_deposits += balance
        deposits_breakdown.append({
            'account_code': acct.account_code,
            'account_name': acct.account_name,
            'balance': float(balance),
        })

    # ── Total loans outstanding ───────────────────────────────────────
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
    today_savings = SavingsTransaction.objects.filter(
        tr_date__date=today, credit_amount__gt=0,
    ).aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
    today_loan_payments = LoanTransaction.objects.filter(
        tr_date__date=today, credit_amount__gt=0,
    ).aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')

    # ── Income & Expenses (from GL ledger) ────────────────────────────
    from accounting.models import SaccoAccount, SaccoAccountsLedger

    def _gl_totals(account_group, from_date=None):
        accts = SaccoAccount.objects.filter(account_group=account_group)
        qs = SaccoAccountsLedger.objects.filter(sacco_account__in=accts)
        if from_date:
            qs = qs.filter(date__gte=from_date)
        debits = qs.aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
        credits = qs.aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
        if account_group == 'Income':
            return float(credits - debits)
        else:  # Expenditure
            return float(debits - credits)

    income_this_month = _gl_totals('Income', first_of_month)
    income_ytd = _gl_totals('Income', first_of_year)
    income_cumulative = _gl_totals('Income')

    expenses_this_month = _gl_totals('Expenditure', first_of_month)
    expenses_ytd = _gl_totals('Expenditure', first_of_year)
    expenses_cumulative = _gl_totals('Expenditure')

    return Response({
        'total_members': total_members,
        'active_members': active_members,
        'new_members_this_month': new_this_month,
        'total_deposits': float(total_deposits),
        'deposits_breakdown': deposits_breakdown,
        'total_loans_outstanding': float(total_loans),
        'active_loans': active_loans,
        'overdue_loans_count': RunningLoanStat.objects.filter(total_arrears__gt=0, loan_status='Active').count(),
        'pending_loan_approvals': pending_loans,
        'today_savings_collected': float(today_savings),
        'today_loan_payments': float(today_loan_payments),
        'today_total_collections': float(today_savings + today_loan_payments),
        'income': {
            'this_month': income_this_month,
            'ytd': income_ytd,
            'cumulative': income_cumulative,
        },
        'expenses': {
            'this_month': expenses_this_month,
            'ytd': expenses_ytd,
            'cumulative': expenses_cumulative,
        },
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


@api_view(['PUT'])
@permission_classes([IsAuthenticated])
def member_update_view(request, cust_no):
    """PUT /androidadminapi/members/<cust_no>/edit/
    Update editable fields for an existing member."""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    try:
        customer = Customer.objects.get(cust_no=padded)
    except Customer.DoesNotExist:
        return Response({'error': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

    data = request.data
    # Update allowed fields
    if 'first_name' in data:
        customer.first_name = data['first_name'].strip()
    if 'middle_name' in data:
        customer.middle_name = data['middle_name'].strip()
    if 'last_name' in data:
        customer.last_name = data['last_name'].strip()
    if 'phone' in data:
        customer.phone = data['phone'].strip()
    if 'national_id' in data:
        customer.national_id = data['national_id'].strip()
    if 'reg_email' in data:
        customer.reg_email = data['reg_email'].strip()
    if 'gender' in data and data['gender'] in ('Male', 'Female'):
        customer.gender = data['gender']
    if 'dob' in data:
        customer.dob = data['dob'] or None
    if 'postal_address' in data:
        customer.postal_address = data['postal_address'].strip()
    if 'town' in data:
        customer.town = data['town'].strip()
    if 'kra_pin' in data:
        customer.kra_pin = data['kra_pin'].strip()

    # Rebuild full_name
    parts = [customer.first_name or '', customer.middle_name or '', customer.last_name or '']
    customer.full_name = ' '.join(p for p in parts if p).strip()

    customer.save()

    return Response({
        'message': f'Member {customer.full_name} updated successfully.',
        'member': CustomerDetailSerializer(customer).data,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_accounts_view(request, cust_no):
    """GET /androidadminapi/members/<cust_no>/accounts/
    Returns all account balances for a member."""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    try:
        customer = Customer.objects.get(cust_no=padded)
    except Customer.DoesNotExist:
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
            'account_class': acct.account_type,
            'balance': float(balance),
        })

    return Response({
        'cust_no': padded,
        'member_name': customer.full_name,
        'accounts': accounts,
    })


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
    account_code = data['account_code']
    amount = Decimal(data['amount'])

    # Look up account by account_code
    try:
        acct = CustomerAccountsSetup.objects.get(account_code=account_code, is_active=True)
        saving_type = acct.account_type
    except CustomerAccountsSetup.DoesNotExist:
        return Response({'error': 'Invalid account type.'}, status=status.HTTP_400_BAD_REQUEST)

    tr_ref = _generate_tr_ref('SAV')

    tr_date_val = _parse_tr_date(request.data)
    txn = SavingsTransaction.objects.create(
        cust_no=cust_no,
        saving_type=saving_type,
        account_code=account_code,
        tr_date=tr_date_val,
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

    tr_date_val = _parse_tr_date(request.data)
    tr_ref = _generate_tr_ref('LPY')

    txn = LoanTransaction.objects.create(
        cust_no=cust_no,
        loan_id=loan.id,
        loan_no=loan_no,
        loan_type=loan_type,
        account_code=account_code,
        tr_date=tr_date_val,
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
    loan_type_code = data['loan_product']
    period = data['repayment_period']

    customer = Customer.objects.get(cust_no=cust_no)
    # Android sends account_code as loan_product; try both lookups
    try:
        loan_product = CustomerAccountsSetup.objects.get(
            account_code=loan_type_code, is_loan_account=True, is_active=True,
        )
    except CustomerAccountsSetup.DoesNotExist:
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

    tr_date_val = _parse_tr_date(request.data)
    # Record disbursement transaction
    tr_ref = _generate_tr_ref('DIS')
    LoanTransaction.objects.create(
        cust_no=cust_no,
        loan_id=loan.id,
        loan_no=loan.loan_no,
        loan_type=loan_type_code,
        account_code=loan_product.account_code,
        tr_date=tr_date_val,
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

    tr_date_val = _parse_tr_date(request.data)
    tr_ref = _generate_tr_ref('FIN')

    txn = SavingsTransaction.objects.create(
        cust_no=cust_no,
        saving_type=primary_savings.account_type,
        account_code=primary_savings.account_code,
        tr_date=tr_date_val,
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

        entries = []
        running = opening
        for txn in period_txns:
            debit = float(txn.debit_amount)
            credit = float(txn.credit_amount)
            if acct.is_loan_account:
                running = running + Decimal(str(debit)) - Decimal(str(credit))
            else:
                running = running + Decimal(str(credit)) - Decimal(str(debit))
            entries.append({
                'date': localtime(txn.tr_date).strftime('%d-%m-%Y'),
                'reference': txn.tr_ref,
                'description': txn.tr_desc,
                'debit': debit if debit > 0 else None,
                'credit': credit if credit > 0 else None,
                'balance': float(running),
            })

        accounts_data.append({
            'account_code': acct.account_type,
            'account_name': acct.account_name,
            'opening_balance': float(opening),
            'closing_balance': float(running),
            'entries': entries,
        })

    return Response({
        'cust_no': padded,
        'member_name': customer.full_name,
        'from_date': from_date,
        'to_date': to_date,
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

    # account_type may be an actual account_type (for savings) or a loan_no
    # (for loans, since customer_accounts_list_view returns loan_no as account_id)
    loan_no_filter = None
    try:
        acct = CustomerAccountsSetup.objects.get(account_type=account_type)
    except CustomerAccountsSetup.DoesNotExist:
        # Not a savings account_type — check if it's a loan number
        try:
            loan = LoanHistory.objects.select_related('loan_type').get(loan_no=account_type)
            acct = loan.loan_type
            loan_no_filter = account_type  # filter transactions by this loan_no
        except LoanHistory.DoesNotExist:
            return Response({'error': 'Invalid account type.'}, status=status.HTTP_404_NOT_FOUND)

    from_date = request.query_params.get('from_date', (date.today() - timedelta(days=365)).isoformat())
    to_date = request.query_params.get('to_date', date.today().isoformat())

    if acct.is_loan_account:
        # Use loan_no if available (specific loan), otherwise fall back to loan_type
        loan_filter = {'cust_no': padded}
        if loan_no_filter:
            loan_filter['loan_no'] = loan_no_filter
        else:
            loan_filter['loan_type'] = account_type

        before = LoanTransaction.objects.filter(
            **loan_filter, tr_date__date__lt=from_date)
        bf_d = before.aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
        bf_c = before.aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
        opening = bf_d - bf_c
        txns = LoanTransaction.objects.filter(
            **loan_filter,
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

    entries = []
    running = opening
    for txn in txns:
        d = float(txn.debit_amount)
        c = float(txn.credit_amount)
        if acct.is_loan_account:
            running += Decimal(str(d)) - Decimal(str(c))
        else:
            running += Decimal(str(c)) - Decimal(str(d))
        entries.append({
            'date': localtime(txn.tr_date).strftime('%d-%m-%Y'),
            'reference': txn.tr_ref,
            'description': txn.tr_desc,
            'debit': d if d > 0 else None,
            'credit': c if c > 0 else None,
            'balance': float(running),
        })

    return Response({
        'account_code': loan_no_filter or account_type,
        'account_name': f"{acct.account_name} - {loan_no_filter}" if loan_no_filter else acct.account_name,
        'opening_balance': float(opening),
        'closing_balance': float(running),
        'entries': entries,
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

    # Workflow: superuser can self-approve; others need at least manager approval
    created_by_username = getattr(loan, 'created_by', None) or ''
    is_own_loan_entry = str(created_by_username) == request.user.username
    if is_own_loan_entry and not request.user.is_superuser:
        if request.user.role not in ('admin', 'manager'):
            return Response(
                {'error': 'You cannot approve your own loan entry. A manager or admin must approve it.'},
                status=status.HTTP_403_FORBIDDEN,
            )

    loan.is_approved = True
    loan.approved_by = request.user.username
    loan.approved_at = timezone.now()
    loan.save(update_fields=['is_approved', 'approved_by', 'approved_at'])

    return Response({
        'message': f'Loan {loan_no} approved successfully.',
        'loan': LoanHistorySerializer(loan).data,
    })


# ════════════════════════════════════════════════════════════════════════════
# MEMBER SEARCH (live autocomplete)
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_search_view(request):
    """GET /androidadminapi/members/search/?q=john
    Returns up to 15 members matching name, phone, ID or cust_no."""
    gate = _require_official(request.user)
    if gate:
        return gate

    q = request.query_params.get('q', '').strip()
    if len(q) < 2:
        return Response([])

    qs = Customer.objects.filter(
        Q(full_name__icontains=q) |
        Q(first_name__icontains=q) |
        Q(last_name__icontains=q) |
        Q(cust_no__icontains=q) |
        Q(phone__icontains=q) |
        Q(national_id__icontains=q),

    ).order_by('full_name')[:15]

    return Response(MemberSearchSerializer(qs, many=True).data)


# ════════════════════════════════════════════════════════════════════════════
# UNSETTLED LOANS FOR A MEMBER
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_unsettled_loans_view(request, cust_no):
    """GET /androidadminapi/members/<cust_no>/unsettled-loans/
    Returns disbursed loans with outstanding balance > 0."""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    loans = LoanHistory.objects.filter(
        customer__cust_no=padded, is_disbursed=True,
    ).order_by('-loan_date')

    # Filter to only those with balance > 0
    result = []
    for loan in loans:
        debits = LoanTransaction.objects.filter(
            loan_no=loan.loan_no
        ).aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
        credits = LoanTransaction.objects.filter(
            loan_no=loan.loan_no
        ).aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
        balance = debits - credits
        if balance > Decimal('0.01'):
            result.append(loan)

    return Response(UnsettledLoanSerializer(result, many=True).data)


# ════════════════════════════════════════════════════════════════════════════
# CASH ACCOUNTS (payment source selection)
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def cash_accounts_view(request):
    """GET /androidadminapi/cash-accounts/
    Returns chama cash/bank accounts for payment source selection."""
    gate = _require_official(request.user)
    if gate:
        return gate

    accounts = SaccoAccount.objects.filter(is_cash_account=True).order_by('account_code')
    data = [{'account_code': a.account_code, 'account_name': a.account_name} for a in accounts]
    return Response(data)


# ════════════════════════════════════════════════════════════════════════════
# JOURNAL ENTRY (replaces fine — debit + credit)
# ════════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def journal_entry_view(request):
    """POST /androidadminapi/actions/journal-entry/
    Create a journal entry that debits one account and credits another.
    Used for fines, corrections, adjustments, etc."""
    gate = _require_official(request.user)
    if gate:
        return gate

    serializer = JournalEntrySerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    cust_no = data['cust_no']
    amount = Decimal(data['amount'])
    description = data['description']
    debit_acct_code = data['debit_account']
    credit_acct_code = data['credit_account']

    customer = Customer.objects.get(cust_no=cust_no)

    # Resolve debit account — could be a member product account or sacco GL
    debit_member_product = CustomerAccountsSetup.objects.filter(
        Q(account_code=debit_acct_code) | Q(account_type=debit_acct_code),
        is_active=True,
    ).first()
    debit_sacco = SaccoAccount.objects.filter(account_code=debit_acct_code).first()

    credit_member_product = CustomerAccountsSetup.objects.filter(
        Q(account_code=credit_acct_code) | Q(account_type=credit_acct_code),
        is_active=True,
    ).first()
    credit_sacco = SaccoAccount.objects.filter(account_code=credit_acct_code).first()

    if not (debit_member_product or debit_sacco):
        return Response({'error': f'Debit account {debit_acct_code} not found.'},
                        status=status.HTTP_400_BAD_REQUEST)
    if not (credit_member_product or credit_sacco):
        return Response({'error': f'Credit account {credit_acct_code} not found.'},
                        status=status.HTTP_400_BAD_REQUEST)

    tr_date_val = _parse_tr_date(request.data)
    tr_ref = _generate_tr_ref('JRN')

    # Create journal voucher
    voucher = JournalVoucher.objects.create(
        voucher_no=tr_ref,
        voucher_date=tr_date_val.date() if hasattr(tr_date_val, "date") else date.today(),
        description=f'{description} - Member {customer.full_name} ({cust_no})',
        status='posted',
        total_amount=amount,
        posted_at=timezone.now(),
        created_by=request.user,
    )

    # Debit line
    JournalVoucherLine.objects.create(
        voucher=voucher,
        description=f'DR: {description}',
        debit_amount=amount,
        credit_amount=Decimal('0'),
        entry_type='customer' if debit_member_product else 'sacco',
        customer=customer,
        member_product=debit_member_product,
        sacco_account=debit_sacco,
        member_account_ref=f'{debit_member_product.acc_initials}-{cust_no}' if debit_member_product else '',
    )

    # Credit line
    JournalVoucherLine.objects.create(
        voucher=voucher,
        description=f'CR: {description}',
        debit_amount=Decimal('0'),
        credit_amount=amount,
        entry_type='customer' if credit_member_product else 'sacco',
        customer=customer,
        member_product=credit_member_product,
        sacco_account=credit_sacco,
        member_account_ref=f'{credit_member_product.acc_initials}-{cust_no}' if credit_member_product else '',
    )

    # Also post the actual member transaction if it's a member product account
    if debit_member_product:
        if debit_member_product.is_loan_account:
            LoanTransaction.objects.create(
                cust_no=cust_no,
                loan_type=debit_member_product.account_type,
                account_code=debit_member_product.account_code,
                tr_date=tr_date_val, tr_ref=tr_ref,
                tr_desc=description,
                debit_amount=amount, credit_amount=Decimal('0'),
                created_by=request.user.username,
            )
        else:
            SavingsTransaction.objects.create(
                cust_no=cust_no,
                saving_type=debit_member_product.account_type,
                account_code=debit_member_product.account_code,
                tr_date=tr_date_val, tr_ref=tr_ref,
                tr_desc=description,
                debit_amount=amount, credit_amount=Decimal('0'),
                created_by=request.user.username,
            )

    if credit_member_product:
        if credit_member_product.is_loan_account:
            LoanTransaction.objects.create(
                cust_no=cust_no,
                loan_type=credit_member_product.account_type,
                account_code=credit_member_product.account_code,
                tr_date=tr_date_val, tr_ref=tr_ref,
                tr_desc=description,
                debit_amount=Decimal('0'), credit_amount=amount,
                created_by=request.user.username,
            )
        else:
            SavingsTransaction.objects.create(
                cust_no=cust_no,
                saving_type=credit_member_product.account_type,
                account_code=credit_member_product.account_code,
                tr_date=tr_date_val, tr_ref=tr_ref,
                tr_desc=description,
                debit_amount=Decimal('0'), credit_amount=amount,
                created_by=request.user.username,
            )

    return Response({
        'message': f'Journal {tr_ref}: DR {debit_acct_code} / CR {credit_acct_code} '
                   f'KES {amount:,.2f} for {customer.full_name}.',
        'tr_ref': tr_ref,
        'voucher_no': voucher.voucher_no,
    }, status=status.HTTP_201_CREATED)


# ════════════════════════════════════════════════════════════════════════════
# LOAN APPLICATION (send for approval — replaces direct disbursement)
# ════════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def loan_application_view(request):
    """POST /androidadminapi/actions/request-loan/
    Creates a loan request that goes to pending approval."""
    gate = _require_official(request.user)
    if gate:
        return gate

    serializer = LoanApplicationSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    cust_no = data['cust_no']
    amount = Decimal(data['amount'])
    loan_type_code = data['loan_product']
    period = data['repayment_period']
    purpose = data.get('purpose', '')

    customer = Customer.objects.get(cust_no=cust_no)

    # Resolve loan product
    try:
        loan_product = CustomerAccountsSetup.objects.get(
            Q(account_code=loan_type_code) | Q(account_type=loan_type_code),
            is_loan_account=True, is_active=True,
        )
    except CustomerAccountsSetup.DoesNotExist:
        return Response({'error': 'Invalid loan product.'}, status=status.HTTP_400_BAD_REQUEST)

    # Use submitted interest rate (p.m.) or default from product
    interest_rate_pm = data.get('interest_rate')
    if interest_rate_pm is None:
        interest_rate_pm = Decimal('1.0')  # default 1% p.m.
    else:
        interest_rate_pm = Decimal(str(interest_rate_pm))

    # interest_rate stored on model is annual (p.a.)
    interest_rate_pa = interest_rate_pm * Decimal('12')

    calc_method = getattr(loan_product, 'interest_calc_method', 'reducing_balance') or 'reducing_balance'
    rate = interest_rate_pm / Decimal('100')  # monthly rate as decimal

    # Calculate installment based on calc method
    upfront_interest = Decimal('0')
    if calc_method == 'flat_rate':
        # Flat rate: total interest = principal * monthly_rate * period
        upfront_interest = amount * rate * period
        installment = (amount + upfront_interest) / period
    elif calc_method == 'principal_flat_rate':
        upfront_interest = amount * rate * period
        installment = (amount / period) + (amount * rate)
    else:
        # Reducing balance (annuity formula)
        if rate > 0:
            rate_factor = (Decimal('1') + rate) ** int(period)
            installment = (amount * rate * rate_factor) / (rate_factor - Decimal('1'))
        else:
            installment = amount / period

    loan = LoanHistory(
        customer=customer,
        loan_date=date.today(),
        principal=amount,
        installment=installment.quantize(Decimal('0.01')),
        loan_type=loan_product,
        loan_period=period,
        interest_rate=interest_rate_pa,
        net_disbursed=Decimal('0'),
        is_approved=False,
        is_disbursed=False,
        created_by=request.user.username,
    )
    loan.save()

    return Response({
        'message': f'Loan {loan.loan_no} for {customer.full_name} submitted for approval. '
                   f'Amount: KES {amount:,.2f}, Period: {period} months.',
        'loan_no': loan.loan_no,
        'installment': float(installment.quantize(Decimal('0.01'))),
        'interest_rate_pm': float(interest_rate_pm),
        'interest_calc_method': calc_method,
        'upfront_interest': float(upfront_interest.quantize(Decimal('0.01'))),
        'loan': LoanHistorySerializer(loan).data,
    }, status=status.HTTP_201_CREATED)


# ════════════════════════════════════════════════════════════════════════════
# LOAN DETAIL, EDIT & GUARANTOR MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def loan_detail_view(request, loan_no):
    """GET /androidadminapi/loans/<loan_no>/
    Returns full loan details including guarantors and edit status."""
    gate = _require_official(request.user)
    if gate:
        return gate

    try:
        loan = LoanHistory.objects.select_related('customer', 'loan_type').get(loan_no=loan_no)
    except LoanHistory.DoesNotExist:
        return Response({'error': 'Loan not found.'}, status=status.HTTP_404_NOT_FOUND)

    guarantors = Guarantor.objects.filter(loan=loan).select_related('guarantor_cust')
    calc_method = getattr(loan.loan_type, 'interest_calc_method', 'reducing_balance') or 'reducing_balance'

    return Response({
        'loan': LoanHistorySerializer(loan).data,
        'interest_rate_pm': float((loan.interest_rate / Decimal('12')).quantize(Decimal('0.01'))),
        'interest_calc_method': calc_method,
        'is_editable': not loan.is_approved,
        'guarantors': GuarantorSerializer(guarantors, many=True).data,
    })


@api_view(['PUT'])
@permission_classes([IsAuthenticated])
def loan_edit_view(request, loan_no):
    """PUT /androidadminapi/loans/<loan_no>/edit/
    Edit a loan that has NOT yet been approved."""
    gate = _require_official(request.user)
    if gate:
        return gate

    try:
        loan = LoanHistory.objects.select_related('loan_type').get(loan_no=loan_no)
    except LoanHistory.DoesNotExist:
        return Response({'error': 'Loan not found.'}, status=status.HTTP_404_NOT_FOUND)

    if loan.is_approved:
        return Response({'error': 'Cannot edit a loan that has already been approved.'},
                        status=status.HTTP_400_BAD_REQUEST)

    data = request.data
    principal = Decimal(str(data.get('amount', loan.principal)))
    period = int(data.get('repayment_period', loan.loan_period))
    interest_rate_pm = Decimal(str(data.get('interest_rate_pm', (loan.interest_rate / Decimal('12')))))
    interest_rate_pa = interest_rate_pm * Decimal('12')

    # Optionally change loan product
    loan_type_code = data.get('loan_product')
    if loan_type_code:
        try:
            loan_product = CustomerAccountsSetup.objects.get(
                Q(account_code=loan_type_code) | Q(account_type=loan_type_code),
                is_loan_account=True, is_active=True,
            )
            loan.loan_type = loan_product
        except CustomerAccountsSetup.DoesNotExist:
            return Response({'error': 'Invalid loan product.'}, status=status.HTTP_400_BAD_REQUEST)

    calc_method = getattr(loan.loan_type, 'interest_calc_method', 'reducing_balance') or 'reducing_balance'
    rate = interest_rate_pm / Decimal('100')

    # Recalculate installment
    if calc_method == 'flat_rate':
        upfront_interest = principal * rate * period
        installment = (principal + upfront_interest) / period
    elif calc_method == 'principal_flat_rate':
        upfront_interest = principal * rate * period
        installment = (principal / period) + (principal * rate)
    else:
        if rate > 0:
            rate_factor = (Decimal('1') + rate) ** int(period)
            installment = (principal * rate * rate_factor) / (rate_factor - Decimal('1'))
        else:
            installment = principal / period

    loan.principal = principal
    loan.loan_period = period
    loan.interest_rate = interest_rate_pa
    loan.installment = installment.quantize(Decimal('0.01'))
    loan.save()

    return Response({
        'message': f'Loan {loan.loan_no} updated successfully.',
        'loan': LoanHistorySerializer(loan).data,
        'installment': float(loan.installment),
        'interest_rate_pm': float(interest_rate_pm),
        'interest_calc_method': calc_method,
    })


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def loan_guarantors_view(request, loan_no):
    """GET: list guarantors for a loan. POST: add a guarantor."""
    gate = _require_official(request.user)
    if gate:
        return gate

    try:
        loan = LoanHistory.objects.get(loan_no=loan_no)
    except LoanHistory.DoesNotExist:
        return Response({'error': 'Loan not found.'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        guarantors = Guarantor.objects.filter(loan=loan).select_related('guarantor_cust')
        return Response(GuarantorSerializer(guarantors, many=True).data)

    # POST — add guarantor
    cust_no = request.data.get('cust_no', '').strip()
    amount = request.data.get('amount', '0')

    if not cust_no:
        return Response({'error': 'Member cust_no is required.'}, status=status.HTTP_400_BAD_REQUEST)

    padded = cust_no.zfill(5) if cust_no.isdigit() else cust_no
    try:
        guarantor_cust = Customer.objects.get(cust_no=padded)
    except Customer.DoesNotExist:
        return Response({'error': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

    # Cannot guarantee own loan
    if guarantor_cust.cust_no == loan.customer.cust_no:
        return Response({'error': 'A member cannot guarantee their own loan.'},
                        status=status.HTTP_400_BAD_REQUEST)

    # Check not already guarantor on this loan
    if Guarantor.objects.filter(loan=loan, guarantor_cust=guarantor_cust).exists():
        return Response({'error': f'{guarantor_cust.full_name} is already a guarantor on this loan.'},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        amount_dec = Decimal(str(amount))
        if amount_dec <= 0:
            raise ValueError
    except (ValueError, Exception):
        return Response({'error': 'Invalid guarantee amount.'}, status=status.HTTP_400_BAD_REQUEST)

    g = Guarantor.objects.create(loan=loan, guarantor_cust=guarantor_cust, amount=amount_dec)

    return Response({
        'message': f'{guarantor_cust.full_name} added as guarantor for KES {amount_dec:,.2f}.',
        'guarantor': GuarantorSerializer(g).data,
    }, status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def loan_guarantor_delete_view(request, loan_no, pk):
    """DELETE /androidadminapi/loans/<loan_no>/guarantors/<pk>/
    Remove a guarantor — only allowed before loan is approved."""
    gate = _require_official(request.user)
    if gate:
        return gate

    try:
        loan = LoanHistory.objects.get(loan_no=loan_no)
    except LoanHistory.DoesNotExist:
        return Response({'error': 'Loan not found.'}, status=status.HTTP_404_NOT_FOUND)

    if loan.is_approved:
        return Response({'error': 'Cannot remove guarantors from an approved loan.'},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        g = Guarantor.objects.get(pk=pk, loan=loan)
    except Guarantor.DoesNotExist:
        return Response({'error': 'Guarantor not found.'}, status=status.HTTP_404_NOT_FOUND)

    name = g.guarantor_cust.full_name
    g.delete()
    return Response({'message': f'{name} removed as guarantor.'})


# ════════════════════════════════════════════════════════════════════════════
# LOAN CHARGES & DISBURSEMENT (with method + charges)
# ════════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def loan_charges_view(request, loan_no):
    """GET /androidadminapi/loans/<loan_no>/charges/
    Returns the charges applicable to this loan before disbursement."""
    gate = _require_official(request.user)
    if gate:
        return gate

    try:
        loan = LoanHistory.objects.get(loan_no=loan_no)
    except LoanHistory.DoesNotExist:
        return Response({'error': 'Loan not found.'}, status=status.HTTP_404_NOT_FOUND)

    if not loan.is_approved:
        return Response({'error': 'Loan not yet approved.'}, status=status.HTTP_400_BAD_REQUEST)
    if loan.is_disbursed:
        return Response({'error': 'Loan already disbursed.'}, status=status.HTTP_400_BAD_REQUEST)

    # Get charges for this loan product
    from loans.models import LoanCharge
    charges_qs = LoanCharge.objects.filter(
        loan_products=loan.loan_type, is_active=True,
    )

    charges = []
    total_charges = Decimal('0')
    for ch in charges_qs:
        if ch.charge_type == 'percentage':
            charge_amount = (ch.amount / Decimal('100') * loan.principal)
            if ch.min_amount and charge_amount < ch.min_amount:
                charge_amount = ch.min_amount
            if ch.max_amount and ch.max_amount > 0 and charge_amount > ch.max_amount:
                charge_amount = ch.max_amount
        else:
            charge_amount = ch.amount

        charge_amount = charge_amount.quantize(Decimal('0.01'))
        total_charges += charge_amount
        charges.append({
            'name': ch.name,
            'charge_type': ch.get_charge_type_display(),
            'rate': str(ch.amount),
            'amount': str(charge_amount),
            'is_mandatory': ch.is_mandatory,
        })

    net_disbursed = loan.principal - total_charges

    return Response({
        'loan_no': loan.loan_no,
        'member_name': loan.customer.full_name,
        'principal': str(loan.principal),
        'total_charges': str(total_charges),
        'net_disbursed': str(net_disbursed),
        'charges': charges,
    })



@api_view(["GET"])
@permission_classes([IsAuthenticated])
def approved_loans_list_view(request):
    """GET /androidadminapi/loans/approved-for-disbursement/
    Returns all approved but not-yet-disbursed loans."""
    gate = _require_official(request.user)
    if gate:
        return gate

    loans = LoanHistory.objects.filter(
        is_approved=True, is_disbursed=False,
    ).select_related("customer", "loan_type").order_by("-approved_at")

    data = []
    for loan in loans:
        data.append({
            "loan_no": loan.loan_no,
            "cust_no": loan.customer.cust_no,
            "member_name": loan.customer.full_name,
            "loan_product": loan.loan_type.account_name if loan.loan_type else "",
            "principal": str(loan.principal),
            "applied_at": loan.applied_at.isoformat() if hasattr(loan, "applied_at") and loan.applied_at else "",
            "approved_at": loan.approved_at.isoformat() if hasattr(loan, "approved_at") and loan.approved_at else "",
            "phone": loan.customer.mobile_no if hasattr(loan.customer, "mobile_no") else "",
        })

    return Response(data)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def loan_disburse_approved_view(request, loan_no):
    """POST /androidadminapi/loans/<loan_no>/disburse/
    Disburse an approved loan with method selection and charge deductions."""
    gate = _require_official(request.user)
    if gate:
        return gate

    if request.user.role not in ('admin', 'manager'):
        return Response({'error': 'Only managers/admins can disburse loans.'},
                        status=status.HTTP_403_FORBIDDEN)

    try:
        loan = LoanHistory.objects.get(loan_no=loan_no)
    except LoanHistory.DoesNotExist:
        return Response({'error': 'Loan not found.'}, status=status.HTTP_404_NOT_FOUND)

    if not loan.is_approved:
        return Response({'error': 'Loan not yet approved.'}, status=status.HTTP_400_BAD_REQUEST)
    if loan.is_disbursed:
        return Response({'error': 'Loan already disbursed.'}, status=status.HTTP_400_BAD_REQUEST)

    disbursement_method = request.data.get('disbursement_method', 'cash')
    cash_account_code = request.data.get('cash_account', '')

    # Calculate charges
    from loans.models import LoanCharge, LoanChargeRecovery
    charges_qs = LoanCharge.objects.filter(
        loan_products=loan.loan_type, is_active=True,
    )

    total_charges = Decimal('0')
    for ch in charges_qs:
        if ch.charge_type == 'percentage':
            charge_amount = (ch.amount / Decimal('100') * loan.principal)
            if ch.min_amount and charge_amount < ch.min_amount:
                charge_amount = ch.min_amount
            if ch.max_amount and ch.max_amount > 0 and charge_amount > ch.max_amount:
                charge_amount = ch.max_amount
        else:
            charge_amount = ch.amount
        charge_amount = charge_amount.quantize(Decimal('0.01'))
        total_charges += charge_amount

        # Record charge recovery
        LoanChargeRecovery.objects.create(
            loan=loan,
            date=date.today(),
            reference=f'{loan.loan_no}-CHG',
            description=ch.name,
            charge=ch,
            amount=charge_amount,
        )

    net_disbursed = loan.principal - total_charges

    # Mark loan as disbursed
    loan.is_disbursed = True
    loan.disbursed_at = timezone.now()
    tr_date_val = _parse_tr_date(request.data)
    loan.net_disbursed = net_disbursed
    loan.save(update_fields=['is_disbursed', 'disbursed_at', 'net_disbursed'])

    # Record disbursement transaction (loan debit)
    tr_ref = _generate_tr_ref('DIS')
    loan_type_code = loan.loan_type.account_type if loan.loan_type else ''
    account_code = loan.loan_type.account_code if loan.loan_type else ''

    LoanTransaction.objects.create(
        cust_no=loan.customer.cust_no,
        loan_id=loan.id,
        loan_no=loan.loan_no,
        loan_type=loan_type_code,
        account_code=account_code,
        tr_date=tr_date_val,
        tr_ref=tr_ref,
        tr_desc=f'Loan disbursement via {disbursement_method}',
        debit_amount=loan.principal,
        credit_amount=Decimal('0'),
        created_by=request.user.username,
    )

    # If charges exist, record charge transactions (loan credits)
    if total_charges > 0:
        chg_ref = _generate_tr_ref('CHG')
        LoanTransaction.objects.create(
            cust_no=loan.customer.cust_no,
            loan_id=loan.id,
            loan_no=loan.loan_no,
            loan_type=loan_type_code,
            account_code=account_code,
            tr_date=tr_date_val,
            tr_ref=chg_ref,
            tr_desc=f'Loan charges deducted at source',
            debit_amount=Decimal('0'),
            credit_amount=total_charges,
            created_by=request.user.username,
        )

    # ── M-Pesa B2C disbursement ─────────────────────────────────────────
    mpesa_result = None
    if disbursement_method == 'mpesa_b2c':
        phone = request.data.get('phone', '') or getattr(loan.customer, 'phone', '')
        if not phone:
            return Response(
                {'error': 'No phone number provided for M-Pesa disbursement.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            mpesa_result = initiate_b2c_payment(
                phone=phone,
                amount=net_disbursed,
                remarks=f'Loan {loan.loan_no} disbursement',
                occasion=loan.loan_no,
            )
        except MpesaConfigError as e:
            # Roll back: un-disburse so official can retry later
            loan.is_disbursed = False
            loan.disbursed_at = None
            loan.net_disbursed = None
            loan.save(update_fields=['is_disbursed', 'disbursed_at', 'net_disbursed'])
            return Response(
                {'error': str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except MpesaApiError as e:
            loan.is_disbursed = False
            loan.disbursed_at = None
            loan.net_disbursed = None
            loan.save(update_fields=['is_disbursed', 'disbursed_at', 'net_disbursed'])
            return Response(
                {'error': f'M-Pesa B2C failed: {e}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

    response_data = {
        'message': f'Loan {loan.loan_no} disbursed: KES {net_disbursed:,.2f} '
                   f'(Principal: {loan.principal:,.2f}, Charges: {total_charges:,.2f}) '
                   f'to {loan.customer.full_name} via {disbursement_method}.',
        'loan_no': loan.loan_no,
        'principal': str(loan.principal),
        'total_charges': str(total_charges),
        'net_disbursed': str(net_disbursed),
        'disbursement_method': disbursement_method,
        'tr_ref': tr_ref,
    }
    if mpesa_result:
        response_data['mpesa_conversation_id'] = mpesa_result.get('ConversationID', '')
        response_data['mpesa_response'] = mpesa_result.get('ResponseDescription', '')

    return Response(response_data, status=status.HTTP_201_CREATED)



# ════════════════════════════════════════════════════════════════════════════
# PHASE 3 — Account Selection, Statement Download, Inter-Account Transfer
# ════════════════════════════════════════════════════════════════════════════

from loans.models import RunningLoanStat


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def customer_accounts_list_view(request, cust_no):
    """GET /androidadminapi/members/<cust_no>/accounts-list/
    Lists savings account types + individual loan numbers for statement
    account selector. Supports ?include_zero=1 to include zero-balance accounts."""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    include_zero = request.GET.get('include_zero', '0') == '1'

    results = []

    # 1. Savings account types
    savings_accounts = CustomerAccountsSetup.objects.filter(
        is_active=True, is_loan_account=False
    ).values('account_code', 'account_name', 'account_type').distinct()

    for acc in savings_accounts:
        bal = SavingsTransaction.objects.filter(
            cust_no=padded, saving_type=acc['account_type']
        ).aggregate(
            balance=Sum('credit_amount', default=Decimal('0')) - Sum('debit_amount', default=Decimal('0'))
        )['balance'] or Decimal('0')
        if not include_zero and bal == 0:
            continue

        results.append({
            'account_id': acc['account_type'],
            'account_code': acc['account_code'],
            'account_name': acc['account_name'],
            'is_loan': False,
            'balance': float(bal),
        })

    # 2. Loan accounts — active loans only (exclude settled)
    settled_loan_nos = set(
        RunningLoanStat.objects.filter(
            cust_no=padded, loan_status='Settled'
        ).values_list('loan_no', flat=True)
    )

    active_loans = LoanHistory.objects.filter(
        customer__cust_no=padded
    ).select_related('loan_type').exclude(loan_no__in=settled_loan_nos)

    for loan in active_loans:
        loan_label = loan.loan_type.account_name if loan.loan_type else 'Loan'

        if not include_zero:
            bal = LoanTransaction.objects.filter(
                cust_no=padded, loan_no=loan.loan_no
            ).aggregate(
                balance=Sum('debit_amount', default=Decimal('0')) - Sum('credit_amount', default=Decimal('0'))
            )['balance'] or Decimal('0')
            if bal <= 0:
                continue

        results.append({
            'account_id': loan.loan_no,
            'account_code': loan.loan_no,
            'account_name': f'{loan_label} - {loan.loan_no}',
            'is_loan': True,
            'balance': float(bal),
        })

    return Response(results)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_accounts_detail_view(request, cust_no):
    """GET /androidadminapi/members/<cust_no>/accounts-detail/
    Returns individual savings balances (all, even zero) and loan balances (only with balance)."""
    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    try:
        customer = Customer.objects.get(cust_no=padded)
    except Customer.DoesNotExist:
        return Response({'error': 'Member not found'}, status=status.HTTP_404_NOT_FOUND)

    # All savings account types
    savings = []
    for acc in CustomerAccountsSetup.objects.filter(is_active=True, is_loan_account=False):
        bal = SavingsTransaction.objects.filter(
            cust_no=padded, saving_type=acc.account_type
        ).aggregate(
            balance=Sum('credit_amount', default=Decimal('0')) - Sum('debit_amount', default=Decimal('0'))
        )['balance'] or Decimal('0')
        savings.append({
            'account_name': acc.account_name,
            'account_code': acc.account_code,
            'balance': str(bal),
        })

    # Loans with balance only
    loans = []
    settled_loan_nos = set(
        RunningLoanStat.objects.filter(
            cust_no=padded, loan_status='Settled'
        ).values_list('loan_no', flat=True)
    )
    active_loans = LoanHistory.objects.filter(
        customer=customer
    ).select_related('loan_type').exclude(loan_no__in=settled_loan_nos)

    for loan in active_loans:
        bal = LoanTransaction.objects.filter(
            cust_no=padded, loan_no=loan.loan_no
        ).aggregate(
            balance=Sum('debit_amount', default=Decimal('0')) - Sum('credit_amount', default=Decimal('0'))
        )['balance'] or Decimal('0')
        if bal > 0:
            loan_label = loan.loan_type.account_name if loan.loan_type else 'Loan'
            loans.append({
                'loan_type': loan_label,
                'loan_no': loan.loan_no,
                'balance': str(bal),
            })

    return Response({
        'cust_no': padded,
        'member_name': customer.full_name,
        'savings': savings,
        'loans': loans,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def statement_download_view(request, cust_no, account_id):
    """GET /androidadminapi/statements/<cust_no>/<account_id>/download/
    Generates a PDF statement for a specific savings or loan account."""
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.units import mm
    from django.http import HttpResponse

    gate = _require_official(request.user)
    if gate:
        return gate

    padded = _pad_cust_no(cust_no)
    from_date = request.GET.get('from_date', '')
    to_date = request.GET.get('to_date', '')

    try:
        customer = Customer.objects.get(cust_no=padded)
    except Customer.DoesNotExist:
        return Response({'error': 'Member not found'}, status=status.HTTP_404_NOT_FOUND)

    # Determine if loan or savings
    is_loan = account_id.upper().startswith('LN') or account_id.upper().startswith('MOBI')

    if is_loan:
        account_display = f'Loan Statement - {account_id}'
        qs = LoanTransaction.objects.filter(cust_no=padded, loan_no=account_id)
    else:
        setup = CustomerAccountsSetup.objects.filter(account_type=account_id).first()
        account_display = f'Savings Statement - {setup.account_name if setup else account_id}'
        qs = SavingsTransaction.objects.filter(cust_no=padded, saving_type=account_id)

    if from_date:
        qs = qs.filter(tr_date__date__gte=from_date)
    if to_date:
        qs = qs.filter(tr_date__date__lte=to_date)

    qs = qs.order_by('tr_date', 'id')

    # Calculate opening balance
    if from_date:
        if is_loan:
            ob_qs = LoanTransaction.objects.filter(cust_no=padded, loan_no=account_id, tr_date__date__lt=from_date)
            ob = ob_qs.aggregate(b=Sum('debit_amount', default=Decimal('0')) - Sum('credit_amount', default=Decimal('0')))['b'] or Decimal('0')
        else:
            ob_qs = SavingsTransaction.objects.filter(cust_no=padded, saving_type=account_id, tr_date__date__lt=from_date)
            ob = ob_qs.aggregate(b=Sum('credit_amount', default=Decimal('0')) - Sum('debit_amount', default=Decimal('0')))['b'] or Decimal('0')
    else:
        ob = Decimal('0')

    # Build PDF
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=40, rightMargin=40, topMargin=30, bottomMargin=40)
    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle('Title', parent=styles['Normal'], fontSize=12, fontName='Helvetica-Bold',
                                  alignment=TA_CENTER, spaceAfter=6)
    meta_style = ParagraphStyle('Meta', parent=styles['Normal'], fontSize=8, alignment=TA_CENTER, spaceAfter=4)

    # Header
    try:
        chama = ChamaInfo.objects.first()
        chama_name = chama.chama_name if chama else 'SACCO'
    except Exception:
        chama_name = 'SACCO'

    story.append(Paragraph(chama_name.upper(), title_style))
    story.append(Paragraph(account_display, meta_style))
    story.append(Paragraph(f'Member: {customer.full_name} ({padded})', meta_style))

    period = ''
    if from_date:
        period += f'From: {from_date}'
    if to_date:
        period += f'  To: {to_date}'
    if period:
        story.append(Paragraph(period, meta_style))

    story.append(Spacer(1, 8 * mm))

    # Table
    header = ['Date', 'Reference', 'Description', 'Debit', 'Credit', 'Balance']
    data = [header]
    running = ob

    # Opening balance row
    data.append(['', '', 'Opening Balance', '', '', f'{ob:,.2f}'])

    for txn in qs:
        tr_date = localtime(txn.tr_date).strftime('%d-%m-%Y') if txn.tr_date else ''
        tr_ref = txn.tr_ref or ''
        tr_desc = txn.tr_desc or ''
        debit = getattr(txn, 'debit_amount', None) or Decimal('0')
        credit = getattr(txn, 'credit_amount', None) or Decimal('0')

        if is_loan:
            running = running + debit - credit
        else:
            running = running + credit - debit

        data.append([
            tr_date, tr_ref[:12], tr_desc[:25],
            f'{debit:,.2f}' if debit else '',
            f'{credit:,.2f}' if credit else '',
            f'{running:,.2f}',
        ])

    col_widths = [60, 70, 150, 65, 65, 70]
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (3, 0), (5, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.3, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    story.append(tbl)

    doc.build(story)
    buffer.seek(0)

    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    filename = f'statement_{padded}_{account_id}.pdf'
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def inter_account_transfer_view(request):
    """POST /androidadminapi/actions/inter-account-transfer/
    Transfer funds between member accounts (savings-to-savings or savings-to-loan)."""
    gate = _require_official(request.user)
    if gate:
        return gate

    from_cust = request.data.get('from_cust_no', '').strip()
    from_account = request.data.get('from_account', '').strip()
    to_cust = request.data.get('to_cust_no', '').strip()
    to_account = request.data.get('to_account', '').strip()
    amount_str = request.data.get('amount', '0')
    reference = request.data.get('reference', '')

    try:
        amount = Decimal(str(amount_str))
    except Exception:
        return Response({'error': 'Invalid amount'}, status=status.HTTP_400_BAD_REQUEST)

    if amount <= 0:
        return Response({'error': 'Amount must be greater than zero.'}, status=status.HTTP_400_BAD_REQUEST)
    if not from_cust or not to_cust:
        return Response({'error': 'Both source and destination members are required.'}, status=status.HTTP_400_BAD_REQUEST)
    if not from_account or not to_account:
        return Response({'error': 'Both source and destination accounts are required.'}, status=status.HTTP_400_BAD_REQUEST)

    from_padded = _pad_cust_no(from_cust)
    to_padded = _pad_cust_no(to_cust)

    # Resolve from_account — could be account_code or account_type
    from_setup = CustomerAccountsSetup.objects.filter(
        Q(account_code=from_account) | Q(account_type=from_account),
        is_active=True, is_loan_account=False,
    ).first()
    if not from_setup:
        return Response({'error': f'Source account "{from_account}" not found.'},
                        status=status.HTTP_400_BAD_REQUEST)
    from_saving_type = from_setup.account_type
    from_account_code = from_setup.account_code

    # Overdraw protection on source savings
    bal = SavingsTransaction.objects.filter(
        cust_no=from_padded, saving_type=from_saving_type
    ).aggregate(
        balance=Sum('credit_amount', default=Decimal('0')) - Sum('debit_amount', default=Decimal('0'))
    )['balance'] or Decimal('0')

    tr_date_val = _parse_tr_date(request.data)
    if bal < amount:
        return Response({
            'error': f'Insufficient funds. Current balance: KES {bal:,.2f}'
        }, status=status.HTTP_400_BAD_REQUEST)

    tr_ref = _generate_tr_ref('TRF')
    desc_suffix = f'{reference} | Ref: {tr_ref}'.strip(' | ') if reference else f'Ref: {tr_ref}'

    # Debit source savings
    SavingsTransaction.objects.create(
        cust_no=from_padded,
        saving_type=from_saving_type,
        account_code=from_account_code,
        tr_date=tr_date_val,
        tr_ref=tr_ref,
        tr_desc=f'Transfer to {to_padded}: {desc_suffix}',
        debit_amount=amount,
        credit_amount=Decimal('0'),
        created_by=request.user.username,
    )

    # Credit destination — check if loan or savings
    loan_record = LoanHistory.objects.filter(loan_no=to_account, customer__cust_no=to_padded).first()

    if loan_record:
        LoanTransaction.objects.create(
            cust_no=to_padded,
            loan_id=loan_record.id,
            loan_no=loan_record.loan_no,
            loan_type=loan_record.loan_type.account_type if loan_record.loan_type else '',
            account_code=loan_record.loan_type.account_code if loan_record.loan_type else '',
            tr_date=tr_date_val,
            tr_ref=tr_ref,
            tr_desc=f'Transfer from {from_padded}: {desc_suffix}',
            debit_amount=Decimal('0'),
            credit_amount=amount,
            created_by=request.user.username,
        )
    else:
        SavingsTransaction.objects.create(
            cust_no=to_padded,
            saving_type=to_account,
            tr_date=tr_date_val,
            tr_ref=tr_ref,
            tr_desc=f'Transfer from {from_padded}: {desc_suffix}',
            debit_amount=Decimal('0'),
            credit_amount=amount,
            created_by=request.user.username,
        )

    return Response({
        'message': f'Transfer of KES {amount:,.2f} completed. Ref: {tr_ref}',
        'tr_ref': tr_ref,
    }, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def sacco_accounts_view(request):
    """GET /androidadminapi/sacco-accounts/
    Returns all SaccoAccount GL entries for journal entry account selection."""
    gate = _require_official(request.user)
    if gate:
        return gate

    accounts = SaccoAccount.objects.all().order_by('account_code')
    data = [{
        'account_code': a.account_code,
        'account_name': a.account_name,
        'account_group': getattr(a, 'account_group', ''),
    } for a in accounts]

    return Response(data)


# ════════════════════════════════════════════════════════════════════════════
# PASSWORD RESET WITH OTP
# ════════════════════════════════════════════════════════════════════════════

import random
import hashlib

# Simple in-memory OTP store (in production, use cache/Redis)
_otp_store = {}


def _generate_otp():
    return f'{random.randint(100000, 999999)}'


@api_view(['POST'])
@permission_classes([AllowAny])
def request_otp_view(request):
    """POST /androidadminapi/auth/request-otp/
    Send OTP via SMS or email for password reset."""
    serializer = RequestOtpSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    identifier = serializer.validated_data['identifier'].strip()

    # Find user by username, email, or phone
    user = None
    try:
        if '@' in identifier:
            user = CustomUser.objects.get(email=identifier.lower())
        else:
            user = CustomUser.objects.filter(
                Q(username=identifier) | Q(phone=identifier)
            ).first()
    except CustomUser.DoesNotExist:
        pass

    if not user:
        # Don't reveal whether user exists
        return Response({'message': 'If the account exists, an OTP has been sent.'})

    otp = _generate_otp()
    key = hashlib.sha256(identifier.lower().encode()).hexdigest()
    _otp_store[key] = {
        'otp': otp,
        'user_id': user.id,
        'created': timezone.now(),
    }

    # Send via SMS — log to SMSLog, let the queue job deliver later
    sent_via = []
    if hasattr(user, 'phone') and user.phone:
        try:
            from sms.services import notify
            notify(
                user.phone,
                f'Your NODi Lite password reset code is: {otp}. '
                f'This code expires in 10 minutes.',
                created_by='password_reset',
                send_now=False,          # queued — picked up by SMS job
            )
            sent_via.append('sms')
        except Exception as e:
            logger.warning('OTP SMS failed for %s: %s', identifier, e)

    # Send via email — send immediately, no EmailLog
    if user.email:
        try:
            from django.core.mail import send_mail
            from django.conf import settings as django_settings
            chama = getattr(django_settings, 'CHAMA_DISPLAY_NAME', '') or \
                    getattr(django_settings, 'CHAMA_NAME', '') or 'NODi Lite'
            send_mail(
                f'{chama} — Password Reset Code',
                f'Hello {user.first_name or user.username},\n\n'
                f'Your password reset code is: {otp}\n\n'
                f'This code expires in 10 minutes.\n'
                f'If you did not request this, please ignore this email.\n\n'
                f'— {chama}',
                getattr(django_settings, 'DEFAULT_FROM_EMAIL', None),
                [user.email],
                fail_silently=False,
            )
            sent_via.append('email')
        except Exception as e:
            logger.warning('OTP email failed for %s: %s', identifier, e)

    return Response({
        'message': 'If the account exists, an OTP has been sent.',
        'sent_via': sent_via,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def verify_otp_view(request):
    """POST /androidadminapi/auth/verify-otp/
    Verify OTP is correct (before allowing password reset)."""
    serializer = VerifyOtpSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    identifier = serializer.validated_data['identifier'].strip()
    otp = serializer.validated_data['otp'].strip()

    key = hashlib.sha256(identifier.lower().encode()).hexdigest()
    stored = _otp_store.get(key)

    if not stored:
        return Response({'error': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)

    # Check expiry (10 minutes)
    elapsed = (timezone.now() - stored['created']).total_seconds()
    if elapsed > 600:
        del _otp_store[key]
        return Response({'error': 'OTP has expired. Please request a new one.'},
                        status=status.HTTP_400_BAD_REQUEST)

    if stored['otp'] != otp:
        return Response({'error': 'Invalid OTP.'}, status=status.HTTP_400_BAD_REQUEST)

    return Response({'message': 'OTP verified.', 'valid': True})


@api_view(['POST'])
@permission_classes([AllowAny])
def reset_password_view(request):
    """POST /androidadminapi/auth/reset-password/
    Reset password after OTP verification."""
    serializer = ResetPasswordSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    identifier = serializer.validated_data['identifier'].strip()
    otp = serializer.validated_data['otp'].strip()
    new_password = serializer.validated_data['new_password']

    key = hashlib.sha256(identifier.lower().encode()).hexdigest()
    stored = _otp_store.get(key)

    if not stored or stored['otp'] != otp:
        return Response({'error': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)

    elapsed = (timezone.now() - stored['created']).total_seconds()
    if elapsed > 600:
        del _otp_store[key]
        return Response({'error': 'OTP has expired.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = CustomUser.objects.get(id=stored['user_id'])
        user.set_password(new_password)
        user.save()
        del _otp_store[key]
        return Response({'message': 'Password reset successfully. You can now sign in.'})
    except CustomUser.DoesNotExist:
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)


# ════════════════════════════════════════════════════════════════════════════
# M-PESA STK PUSH — COLLECTIONS
# ════════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mpesa_collection_view(request):
    """POST /androidadminapi/actions/mpesa-collection/
    Initiate M-Pesa STK Push to collect payment from a member.

    Body: {cust_no, account_code, phone, amount, tr_date?}
    account_code can be a savings account or an unsettled loan account.
    """
    gate = _require_official(request.user)
    if gate:
        return gate

    cust_no = request.data.get('cust_no', '').strip()
    account_code = request.data.get('account_code', '').strip()
    phone = request.data.get('phone', '').strip()
    amount_str = request.data.get('amount', '0')

    if not cust_no:
        return Response({'error': 'Member number is required.'},
                        status=status.HTTP_400_BAD_REQUEST)
    if not account_code:
        return Response({'error': 'Account is required.'},
                        status=status.HTTP_400_BAD_REQUEST)
    if not phone:
        return Response({'error': 'Phone number is required.'},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        amount = Decimal(str(amount_str))
        if amount <= 0:
            raise ValueError
    except (ValueError, Exception):
        return Response({'error': 'A valid positive amount is required.'},
                        status=status.HTTP_400_BAD_REQUEST)

    padded = _pad_cust_no(cust_no)
    try:
        customer = Customer.objects.get(cust_no=padded)
    except Customer.DoesNotExist:
        return Response({'error': 'Member not found.'},
                        status=status.HTTP_404_NOT_FOUND)

    # Determine account reference for STK display
    try:
        acct = CustomerAccountsSetup.objects.get(account_code=account_code)
        acct_ref = acct.account_name[:12]
    except CustomerAccountsSetup.DoesNotExist:
        acct_ref = account_code[:12]

    try:
        result = initiate_stk_push(
            phone=phone,
            amount=amount,
            account_reference=acct_ref,
            transaction_desc='Collection',
        )
    except MpesaConfigError as e:
        return Response({'error': str(e)},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except MpesaApiError as e:
        return Response({'error': f'STK Push failed: {e}'},
                        status=status.HTTP_502_BAD_GATEWAY)

    return Response({
        'message': f'STK Push sent to {format_phone(phone)}. '
                   f'Member will receive a prompt to enter M-Pesa PIN '
                   f'for KES {amount:,.2f} payment.',
        'checkout_request_id': result.get('CheckoutRequestID', ''),
        'merchant_request_id': result.get('MerchantRequestID', ''),
        'customer_message': result.get('CustomerMessage', ''),
        'cust_no': padded,
        'account_code': account_code,
        'amount': str(amount),
        'phone': format_phone(phone),
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def mpesa_stk_callback_view(request):
    """POST /androidadminapi/mpesa/stk-callback/
    Safaricom STK Push result callback. This is called by Safaricom servers
    after the customer completes or cancels the STK prompt.
    """
    body = request.data.get('Body', {})
    stk_callback = body.get('stkCallback', {})
    result_code = stk_callback.get('ResultCode')
    result_desc = stk_callback.get('ResultDesc', '')
    merchant_request_id = stk_callback.get('MerchantRequestID', '')
    checkout_request_id = stk_callback.get('CheckoutRequestID', '')

    logger.info(
        "STK Callback: CheckoutRequestID=%s ResultCode=%s Desc=%s",
        checkout_request_id, result_code, result_desc,
    )

    if result_code == 0:
        # Successful payment — extract metadata
        metadata = stk_callback.get('CallbackMetadata', {}).get('Item', [])
        meta_dict = {}
        for item in metadata:
            meta_dict[item.get('Name', '')] = item.get('Value', '')

        amount = meta_dict.get('Amount', 0)
        mpesa_receipt = meta_dict.get('MpesaReceiptNumber', '')
        phone = meta_dict.get('PhoneNumber', '')

        logger.info(
            "STK Payment received: Receipt=%s Amount=%s Phone=%s",
            mpesa_receipt, amount, phone,
        )

        # Store the notification for processing (uses existing MpesaNotification model if available)
        try:
            from transactions.models import MpesaNotification
            MpesaNotification.objects.get_or_create(
                trans_id=mpesa_receipt,
                defaults={
                    'trans_type': 'STK_PUSH',
                    'trans_amount': Decimal(str(amount)),
                    'msisdn': str(phone),
                    'bill_ref_number': checkout_request_id,
                    'org_account_balance': Decimal('0'),
                    'trans_time': timezone.now().strftime('%Y%m%d%H%M%S'),
                },
            )
        except Exception as exc:
            logger.error("Failed to store STK callback notification: %s", exc)

    return Response({'ResultCode': 0, 'ResultDesc': 'Accepted'})


@api_view(['POST'])
@permission_classes([AllowAny])
def mpesa_b2c_result_view(request):
    """POST /androidadminapi/mpesa/b2c-result/
    Safaricom B2C result callback after funds are sent to customer.
    """
    result = request.data.get('Result', {})
    result_code = result.get('ResultCode')
    result_desc = result.get('ResultDesc', '')
    conversation_id = result.get('ConversationID', '')
    originator_id = result.get('OriginatorConversationID', '')

    logger.info(
        "B2C Result: ConversationID=%s ResultCode=%s Desc=%s",
        conversation_id, result_code, result_desc,
    )

    if result_code == 0:
        params = result.get('ResultParameters', {}).get('ResultParameter', [])
        param_dict = {}
        for p in params:
            param_dict[p.get('Key', '')] = p.get('Value', '')
        logger.info("B2C success params: %s", param_dict)

    return Response({'ResultCode': 0, 'ResultDesc': 'Accepted'})


@api_view(['POST'])
@permission_classes([AllowAny])
def mpesa_b2c_timeout_view(request):
    """POST /androidadminapi/mpesa/b2c-timeout/
    Safaricom B2C timeout callback when the request times out.
    """
    logger.warning("B2C Timeout callback received: %s", request.data)
    return Response({'ResultCode': 0, 'ResultDesc': 'Accepted'})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def overdue_loans_view(request):
    """GET /androidadminapi/dashboard/overdue-loans/
    Returns loans with arrears > 0 (overdue loans)."""
    gate = _require_official(request.user)
    if gate:
        return gate

    from loans.models import RunningLoanStat
    from customers.models import Customer

    overdue = RunningLoanStat.objects.filter(
        total_arrears__gt=0,
        loan_status='Active',
    ).order_by('-total_arrears')

    results = []
    for loan in overdue:
        # Get member phone from Customer
        phone = ''
        try:
            cust = Customer.objects.get(cust_no=loan.cust_no)
            phone = cust.phone or ''
        except Customer.DoesNotExist:
            pass

        results.append({
            'loan_no': loan.loan_no,
            'cust_no': loan.cust_no,
            'member_name': loan.full_name,
            'phone': phone,
            'arrears': float(loan.total_arrears),
            'defaulted_days': loan.defaulted_days,
            'loan_balance': float(loan.loan_balance),
        })

    return Response({
        'count': len(results),
        'loans': results,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def outstanding_loans_view(request):
    """GET /androidadminapi/dashboard/outstanding-loans/
    Returns all active loans with their balances."""
    gate = _require_official(request.user)
    if gate:
        return gate

    from loans.models import RunningLoanStat
    from customers.models import Customer

    active = RunningLoanStat.objects.filter(
        loan_status='Active',
        loan_balance__gt=0,
    ).order_by('-loan_balance')

    results = []
    for loan in active:
        phone = ''
        try:
            cust = Customer.objects.get(cust_no=loan.cust_no)
            phone = cust.phone or ''
        except Customer.DoesNotExist:
            pass

        results.append({
            'loan_no': loan.loan_no,
            'cust_no': loan.cust_no,
            'member_name': loan.full_name,
            'phone': phone,
            'principal': float(loan.approved_amount),
            'loan_balance': float(loan.loan_balance),
        })

    return Response({
        'count': len(results),
        'loans': results,
    })
