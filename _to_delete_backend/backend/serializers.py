# androidadminapi/serializers.py
# ═══════════════════════════════════════════════════════════════════════════
# NODiLite Admin API — Serializers for chama official operations
# ═══════════════════════════════════════════════════════════════════════════

from rest_framework import serializers
from decimal import Decimal

from accounts.models import CustomUser
from customers.models import Customer, NextOfKin
from transactions.models import SavingsTransaction, LoanTransaction, CustomerAccountsSetup
from loans.models import LoanHistory, Guarantor, LoanCharge
from administration.models import ChamaInfo


# ════════════════════════════════════════════════════════════════════════════
# AUTH & USER
# ════════════════════════════════════════════════════════════════════════════

class AdminUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'phone', 'role']
        read_only_fields = ['id']


class AdminLoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        from django.contrib.auth import authenticate
        identifier = attrs['username'].strip()
        password = attrs['password']

        user = None
        try:
            if '@' in identifier:
                user = CustomUser.objects.get(email=identifier.lower())
            else:
                user = CustomUser.objects.get(username=identifier)
        except CustomUser.DoesNotExist:
            raise serializers.ValidationError('Invalid credentials.')

        # Only allow staff/admin roles — not customers
        ALLOWED_ROLES = ('admin', 'manager', 'loan_officer', 'accounts_clerk')
        if user.role not in ALLOWED_ROLES:
            raise serializers.ValidationError('Access denied. Officials only.')

        auth_user = authenticate(username=user.username, password=password)
        if not auth_user:
            raise serializers.ValidationError('Invalid credentials.')
        if not auth_user.is_active:
            raise serializers.ValidationError('Account disabled.')

        attrs['user'] = auth_user
        return attrs


# ════════════════════════════════════════════════════════════════════════════
# CHAMA INFO / BRANDING
# ════════════════════════════════════════════════════════════════════════════

class ChamaInfoSerializer(serializers.ModelSerializer):
    chama_logo_url = serializers.SerializerMethodField()

    class Meta:
        model = ChamaInfo
        fields = [
            'brand_name', 'chama_name', 'chama_address',
            'chama_contact', 'chama_location', 'chama_footer',
            'chama_logo_url',
        ]

    def get_chama_logo_url(self, obj):
        if obj.chama_logo:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.chama_logo.url)
            return obj.chama_logo.url
        return ''


# ════════════════════════════════════════════════════════════════════════════
# CUSTOMER / MEMBER
# ════════════════════════════════════════════════════════════════════════════

class CustomerListSerializer(serializers.ModelSerializer):
    """Compact serializer for member listing."""
    class Meta:
        model = Customer
        fields = [
            'id', 'cust_no', 'full_name', 'phone', 'national_id',
            'customer_type', 'customer_status', 'reg_date',
        ]
        read_only_fields = ['id', 'cust_no', 'reg_date']


class CustomerDetailSerializer(serializers.ModelSerializer):
    """Full member profile for detail views."""
    class Meta:
        model = Customer
        fields = [
            'id', 'cust_no', 'full_name', 'first_name', 'middle_name', 'last_name',
            'gender', 'marital_status', 'dob', 'phone', 'town',
            'postal_address', 'postal_code', 'home_address',
            'national_id', 'kra_pin', 'reg_email',
            'customer_type', 'customer_status', 'reg_date',
            'reg_fee_is_paid',
        ]
        read_only_fields = ['id', 'cust_no', 'reg_date']


class MemberRegistrationSerializer(serializers.Serializer):
    """Register a new chama member (official-side — no app login created)."""
    full_name = serializers.CharField(max_length=255)
    first_name = serializers.CharField(max_length=100, required=False, default='')
    middle_name = serializers.CharField(max_length=100, required=False, default='', allow_blank=True)
    last_name = serializers.CharField(max_length=100, required=False, default='')
    gender = serializers.ChoiceField(choices=[('Male', 'Male'), ('Female', 'Female')], required=False)
    dob = serializers.DateField(required=False, allow_null=True)
    phone = serializers.CharField(max_length=15)
    national_id = serializers.CharField(max_length=20, required=False, allow_blank=True)
    kra_pin = serializers.CharField(max_length=20, required=False, allow_blank=True)
    reg_email = serializers.EmailField(required=False, allow_blank=True)
    town = serializers.CharField(max_length=100, required=False, allow_blank=True)
    postal_address = serializers.CharField(max_length=255, required=False, allow_blank=True)
    customer_type = serializers.ChoiceField(
        choices=[
            ('adult_individual', 'Adult-Individual'),
            ('minor_individual', 'Minor-Individual'),
            ('group', 'Group'),
        ],
        default='adult_individual',
    )

    def validate_phone(self, value):
        value = value.strip()
        if Customer.objects.filter(phone=value).exists():
            raise serializers.ValidationError('Phone number already registered.')
        return value

    def validate_national_id(self, value):
        value = value.strip()
        if value and Customer.objects.filter(national_id=value).exists():
            raise serializers.ValidationError('National ID already registered.')
        return value


class NextOfKinSerializer(serializers.ModelSerializer):
    class Meta:
        model = NextOfKin
        fields = ['id', 'kin_name', 'gender', 'kin_relationship',
                  'kin_dob', 'kin_phone', 'kin_national_id']
        read_only_fields = ['id']


# ════════════════════════════════════════════════════════════════════════════
# ACCOUNT TYPES
# ════════════════════════════════════════════════════════════════════════════

class AccountTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerAccountsSetup
        fields = [
            'id', 'account_code', 'account_name', 'acc_initials',
            'account_type', 'is_loan_account', 'is_withdrawable',
            'max_loan_limit', 'max_repayment_period', 'interest_calc_method',
            'is_active',
        ]
        read_only_fields = ['id']


# ════════════════════════════════════════════════════════════════════════════
# TRANSACTIONS
# ════════════════════════════════════════════════════════════════════════════

class SavingsTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavingsTransaction
        fields = [
            'id', 'cust_no', 'saving_type', 'account_code', 'tr_date',
            'tr_ref', 'ext_ref', 'tr_desc', 'debit_amount', 'credit_amount',
            'created_by', 'created_at',
        ]
        read_only_fields = ['created_at', 'created_by']


class LoanTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanTransaction
        fields = [
            'id', 'cust_no', 'loan_id', 'loan_no', 'loan_type',
            'account_code', 'tr_date', 'tr_ref', 'ext_ref', 'tr_desc',
            'debit_amount', 'credit_amount', 'created_by', 'created_at',
        ]
        read_only_fields = ['created_at', 'created_by']


# ════════════════════════════════════════════════════════════════════════════
# ACTION REQUESTS
# ════════════════════════════════════════════════════════════════════════════

class RecordSavingsPaymentSerializer(serializers.Serializer):
    """Official records a savings deposit for a member."""
    cust_no = serializers.CharField(max_length=20)
    saving_type = serializers.CharField(max_length=50)
    amount = serializers.CharField(max_length=20)
    description = serializers.CharField(max_length=255, required=False, default='Admin deposit')
    ext_ref = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')

    def validate_amount(self, value):
        try:
            d = Decimal(value)
            if d <= 0:
                raise serializers.ValidationError('Amount must be positive.')
            return value
        except Exception:
            raise serializers.ValidationError('Invalid amount.')

    def validate_cust_no(self, value):
        value = value.strip()
        padded = value.zfill(5) if value.isdigit() else value
        if not Customer.objects.filter(cust_no=padded).exists():
            raise serializers.ValidationError('Member not found.')
        return padded


class RecordLoanPaymentSerializer(serializers.Serializer):
    """Official records a loan repayment for a member."""
    cust_no = serializers.CharField(max_length=20)
    loan_no = serializers.CharField(max_length=50)
    amount = serializers.CharField(max_length=20)
    description = serializers.CharField(max_length=255, required=False, default='Loan repayment')
    ext_ref = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')

    def validate_amount(self, value):
        try:
            d = Decimal(value)
            if d <= 0:
                raise serializers.ValidationError('Amount must be positive.')
            return value
        except Exception:
            raise serializers.ValidationError('Invalid amount.')

    def validate_cust_no(self, value):
        value = value.strip()
        padded = value.zfill(5) if value.isdigit() else value
        if not Customer.objects.filter(cust_no=padded).exists():
            raise serializers.ValidationError('Member not found.')
        return padded


class DisburseLoanSerializer(serializers.Serializer):
    """Official disburses a loan to a member."""
    cust_no = serializers.CharField(max_length=20)
    amount = serializers.CharField(max_length=20)
    loan_type = serializers.CharField(max_length=50)
    period = serializers.IntegerField(min_value=1)
    purpose = serializers.CharField(max_length=255, required=False, default='Loan disbursement')

    def validate_amount(self, value):
        try:
            d = Decimal(value)
            if d <= 0:
                raise serializers.ValidationError('Amount must be positive.')
            return value
        except Exception:
            raise serializers.ValidationError('Invalid amount.')

    def validate_cust_no(self, value):
        value = value.strip()
        padded = value.zfill(5) if value.isdigit() else value
        if not Customer.objects.filter(cust_no=padded).exists():
            raise serializers.ValidationError('Member not found.')
        return padded

    def validate_loan_type(self, value):
        if not CustomerAccountsSetup.objects.filter(
            account_type=value, is_loan_account=True, is_active=True
        ).exists():
            raise serializers.ValidationError('Invalid loan product.')
        return value


class RecordFineSerializer(serializers.Serializer):
    """Official imposes a fine on a member."""
    cust_no = serializers.CharField(max_length=20)
    amount = serializers.CharField(max_length=20)
    reason = serializers.CharField(max_length=255)

    def validate_amount(self, value):
        try:
            d = Decimal(value)
            if d <= 0:
                raise serializers.ValidationError('Amount must be positive.')
            return value
        except Exception:
            raise serializers.ValidationError('Invalid amount.')

    def validate_cust_no(self, value):
        value = value.strip()
        padded = value.zfill(5) if value.isdigit() else value
        if not Customer.objects.filter(cust_no=padded).exists():
            raise serializers.ValidationError('Member not found.')
        return padded


# ════════════════════════════════════════════════════════════════════════════
# LOANS
# ════════════════════════════════════════════════════════════════════════════

class LoanHistorySerializer(serializers.ModelSerializer):
    loan_type_display = serializers.SerializerMethodField()
    cust_no = serializers.CharField(source='customer.cust_no', read_only=True)
    member_name = serializers.CharField(source='customer.full_name', read_only=True)

    class Meta:
        model = LoanHistory
        fields = [
            'id', 'loan_no', 'cust_no', 'member_name', 'loan_date',
            'principal', 'installment', 'loan_type_display', 'loan_period',
            'interest_rate', 'net_disbursed', 'is_approved', 'approved_by',
            'is_disbursed', 'disbursed_at', 'created_by', 'created_at',
        ]
        read_only_fields = ['id', 'loan_no', 'created_at']

    def get_loan_type_display(self, obj):
        try:
            return obj.loan_type.account_name
        except Exception:
            return 'Loan'


class GuarantorSerializer(serializers.ModelSerializer):
    loan_no = serializers.CharField(source='loan.loan_no', read_only=True)
    guarantor_name = serializers.CharField(source='guarantor_cust.full_name', read_only=True)
    guarantor_cust_no = serializers.CharField(source='guarantor_cust.cust_no', read_only=True)

    class Meta:
        model = Guarantor
        fields = ['id', 'loan_no', 'guarantor_name', 'guarantor_cust_no', 'amount']
        read_only_fields = ['id']


# ════════════════════════════════════════════════════════════════════════════
# STATEMENTS
# ════════════════════════════════════════════════════════════════════════════

class StatementLineSerializer(serializers.Serializer):
    date = serializers.CharField()
    ref = serializers.CharField()
    description = serializers.CharField()
    debit = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    credit = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    balance = serializers.DecimalField(max_digits=14, decimal_places=2)


class AccountStatementSerializer(serializers.Serializer):
    account_type = serializers.CharField()
    account_name = serializers.CharField()
    period_from = serializers.CharField()
    period_to = serializers.CharField()
    balance_brought_forward = serializers.DecimalField(max_digits=14, decimal_places=2)
    lines = StatementLineSerializer(many=True)
    closing_balance = serializers.DecimalField(max_digits=14, decimal_places=2)


class MemberStatementSerializer(serializers.Serializer):
    cust_no = serializers.CharField()
    full_name = serializers.CharField()
    statement_date = serializers.CharField()
    period_from = serializers.CharField()
    period_to = serializers.CharField()
    accounts = AccountStatementSerializer(many=True)
