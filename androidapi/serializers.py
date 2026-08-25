# api/serializers.py

from rest_framework import serializers
from django.contrib.auth import authenticate
from accounts.models import CustomUser
from customers.models import Customer
from transactions.models import SavingsTransaction, LoanTransaction
from loans.models import LoanHistory

# ============================================================================
# USER SERIALIZERS
# ============================================================================

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'phone', 'role']
        read_only_fields = ['id']

class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=6)
    password_confirm = serializers.CharField(write_only=True, required=True)

    class Meta:
        model = CustomUser
        fields = ['username', 'email', 'password', 'password_confirm', 
                  'first_name', 'last_name', 'phone']

    def validate(self, attrs):
        if attrs['password'] != attrs.pop('password_confirm'):
            raise serializers.ValidationError({"password": "Passwords do not match"})
        return attrs

    def create(self, validated_data):
        user = CustomUser.objects.create_user(**validated_data)
        return user

class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField()

    def validate(self, attrs):
        user = authenticate(
            username=attrs.get('username'),
            password=attrs.get('password')
        )
        if not user:
            raise serializers.ValidationError("Invalid credentials")
        attrs['user'] = user
        return attrs

# ============================================================================
# CUSTOMER SERIALIZERS
# ============================================================================

class CustomerSerializer(serializers.ModelSerializer):
    user_details = UserSerializer(source='user', read_only=True)
    
    class Meta:
        model = Customer
        fields = ['id', 'cust_no', 'user_details', 'created_at']
        read_only_fields = ['created_at']

# ============================================================================
# TRANSACTION SERIALIZERS
# ============================================================================

class SavingsTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavingsTransaction
        fields = [
            'id', 'cust_no', 'saving_type', 'tr_date', 'tr_ref', 'ext_ref',
            'tr_desc', 'debit_amount', 'credit_amount', 'created_by', 'created_at'
        ]
        read_only_fields = ['created_at', 'created_by']

class LoanTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanTransaction
        fields = [
            'id', 'cust_no', 'loan_id', 'loan_no', 'loan_type', 'tr_date',
            'tr_ref', 'ext_ref', 'tr_desc', 'debit_amount', 'credit_amount',
            'created_by', 'created_at'
        ]
        read_only_fields = ['created_at', 'created_by']

# ============================================================================
# LOAN SERIALIZERS
# ============================================================================

class LoanHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanHistory
        fields = [
            'id', 'loan_no', 'cust_no', 'loan_date', 'principal', 'installment',
            'loan_type', 'loan_period', 'interest_rate', 'net_disbursed',
            'processing_fee', 'insurance_fee', 'other_charges',
            'created_by', 'created_at'
        ]
        read_only_fields = ['loan_no', 'created_at', 'created_by']

# ============================================================================
# BALANCE SERIALIZERS (Read-only - computed)
# ============================================================================

class BalanceSummarySerializer(serializers.Serializer):
    savings_balance = serializers.DecimalField(max_digits=14, decimal_places=2)
    loan_balance = serializers.DecimalField(max_digits=14, decimal_places=2)
    total_balance = serializers.DecimalField(max_digits=14, decimal_places=2)
    savings_transactions = SavingsTransactionSerializer(many=True, read_only=True)
    loan_transactions = LoanTransactionSerializer(many=True, read_only=True)