# api/views.py

from rest_framework import viewsets, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from django.db.models import Sum
from decimal import Decimal

from accounts.models import CustomUser
from customers.models import Customer
from transactions.models import SavingsTransaction, LoanTransaction
from loans.models import LoanHistory

from .serializers import (
    UserSerializer, UserRegistrationSerializer, LoginSerializer,
    CustomerSerializer, SavingsTransactionSerializer, LoanTransactionSerializer,
    LoanHistorySerializer, BalanceSummarySerializer
)

# ============================================================================
# AUTHENTICATION VIEWS
# ============================================================================

@api_view(['POST'])
@permission_classes([AllowAny])
def register_view(request):
    """
    POST /api/auth/register/
    Register a new user
    """
    serializer = UserRegistrationSerializer(data=request.data)
    if serializer.is_valid():
        user = serializer.save(role='customer')  # Default role is customer
        
        # Create customer profile
        Customer.objects.create(user=user)
        
        return Response({
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'phone': user.phone,
            'role': user.role,
            'message': 'User registered successfully'
        }, status=status.HTTP_201_CREATED)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_view(request):
    """
    POST /api/auth/logout/
    Logout and optionally blacklist token
    """
    try:
        return Response(
            {'message': 'Logged out successfully'},
            status=status.HTTP_200_OK
        )
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def current_user_view(request):
    """
    GET /api/auth/user/
    Get current authenticated user details
    """
    serializer = UserSerializer(request.user)
    return Response(serializer.data)

# ============================================================================
# CUSTOMER VIEWS
# ============================================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def customer_profile_view(request):
    """
    GET /api/customers/me/
    Get customer profile for logged-in user
    """
    try:
        customer = Customer.objects.get(user=request.user)
        serializer = CustomerSerializer(customer)
        return Response(serializer.data)
    except Customer.DoesNotExist:
        return Response(
            {'error': 'Customer profile not found'},
            status=status.HTTP_404_NOT_FOUND
        )

# ============================================================================
# BALANCE VIEWS (Computed/Read-only)
# ============================================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def balance_summary_view(request):
    """
    GET /api/balances/summary/
    Get balance summary with recent transactions
    """
    try:
        customer = Customer.objects.get(user=request.user)
        cust_no = customer.cust_no

        # Calculate savings balance
        savings_credits = SavingsTransaction.objects.filter(
            cust_no=cust_no
        ).aggregate(total=Sum('credit_amount'))['total'] or Decimal('0')
        
        savings_debits = SavingsTransaction.objects.filter(
            cust_no=cust_no
        ).aggregate(total=Sum('debit_amount'))['total'] or Decimal('0')
        
        savings_balance = savings_credits - savings_debits

        # Calculate loan balance
        loan_debits = LoanTransaction.objects.filter(
            cust_no=cust_no
        ).aggregate(total=Sum('debit_amount'))['total'] or Decimal('0')
        
        loan_credits = LoanTransaction.objects.filter(
            cust_no=cust_no
        ).aggregate(total=Sum('credit_amount'))['total'] or Decimal('0')
        
        loan_balance = loan_debits - loan_credits

        total_balance = savings_balance - loan_balance

        # Get recent transactions
        savings_txns = SavingsTransaction.objects.filter(
            cust_no=cust_no
        ).order_by('-tr_date')[:10]
        
        loan_txns = LoanTransaction.objects.filter(
            cust_no=cust_no
        ).order_by('-tr_date')[:10]

        data = {
            'savings_balance': float(savings_balance),
            'loan_balance': float(loan_balance),
            'total_balance': float(total_balance),
            'savings_transactions': SavingsTransactionSerializer(savings_txns, many=True).data,
            'loan_transactions': LoanTransactionSerializer(loan_txns, many=True).data,
        }

        return Response(data)
    except Customer.DoesNotExist:
        return Response(
            {'error': 'Customer not found'},
            status=status.HTTP_404_NOT_FOUND
        )

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def savings_balance_view(request):
    """
    GET /api/balances/savings/
    Get savings balance only
    """
    try:
        customer = Customer.objects.get(user=request.user)
        
        credits = SavingsTransaction.objects.filter(
            cust_no=customer.cust_no
        ).aggregate(total=Sum('credit_amount'))['total'] or Decimal('0')
        
        debits = SavingsTransaction.objects.filter(
            cust_no=customer.cust_no
        ).aggregate(total=Sum('debit_amount'))['total'] or Decimal('0')
        
        balance = float(credits - debits)
        return Response({'savings_balance': balance})
    except Customer.DoesNotExist:
        return Response({'error': 'Customer not found'}, status=status.HTTP_404_NOT_FOUND)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def loan_balance_view(request):
    """
    GET /api/balances/loan/
    Get loan balance only
    """
    try:
        customer = Customer.objects.get(user=request.user)
        
        debits = LoanTransaction.objects.filter(
            cust_no=customer.cust_no
        ).aggregate(total=Sum('debit_amount'))['total'] or Decimal('0')
        
        credits = LoanTransaction.objects.filter(
            cust_no=customer.cust_no
        ).aggregate(total=Sum('credit_amount'))['total'] or Decimal('0')
        
        balance = float(debits - credits)
        return Response({'loan_balance': balance})
    except Customer.DoesNotExist:
        return Response({'error': 'Customer not found'}, status=status.HTTP_404_NOT_FOUND)

# ============================================================================
# TRANSACTION VIEWSETS (Read-only)
# ============================================================================

class SavingsTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/savings-transactions/
    List all savings transactions for authenticated user
    """
    serializer_class = SavingsTransactionSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        customer = Customer.objects.get(user=self.request.user)
        return SavingsTransaction.objects.filter(
            cust_no=customer.cust_no
        ).order_by('-tr_date')

class LoanTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/loan-transactions/
    List all loan transactions for authenticated user
    """
    serializer_class = LoanTransactionSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        customer = Customer.objects.get(user=self.request.user)
        return LoanTransaction.objects.filter(
            cust_no=customer.cust_no
        ).order_by('-tr_date')

class LoanHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/loan-history/
    List all loan history for authenticated user
    """
    serializer_class = LoanHistorySerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        customer = Customer.objects.get(user=self.request.user)
        return LoanHistory.objects.filter(
            customer=customer
        ).order_by('-loan_date')