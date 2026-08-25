# androidadminapi/serializers.py
# ═══════════════════════════════════════════════════════════════════════════
# NODiLite Admin API — Serializers for chama official operations
# Field names match the Android (Kotlin) data models exactly.
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
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = ChamaInfo
        fields = [
            'brand_name', 'chama_name', 'chama_address',
            'chama_contact', 'chama_location', 'chama_footer',
            'logo_url',
        ]

    def get_logo_url(self, obj):
        if obj.chama_logo:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.chama_logo.url)
            return obj.chama_logo.url
        return ''


# ════════════════════════════════════════════════════════════════════════════
# CUSTOMER / MEMBER  (field names match Android MemberSummary / MemberDetail)
# ════════════════════════════════════════════════════════════════════════════

class CustomerListSerializer(serializers.ModelSerializer):
    """Maps to Android MemberSummary model."""
    phone_no = serializers.CharField(source='phone', read_only=True)
    status = serializers.CharField(source='customer_status', read_only=True)
    date_registered = serializers.DateField(source='reg_date', read_only=True)

    class Meta:
        model = Customer
        fields = [
            'cust_no', 'first_name', 'last_name',
            'phone_no', 'status', 'date_registered',
        ]


class CustomerDetailSerializer(serializers.ModelSerializer):
    """Maps to Android MemberDetail model."""
    other_names = serializers.CharField(source='middle_name', read_only=True, default='')
    id_no = serializers.CharField(source='national_id', read_only=True, default='')
    phone_no = serializers.CharField(source='phone', read_only=True, default='')
    email = serializers.EmailField(source='reg_email', read_only=True, default='')
    date_of_birth = serializers.DateField(source='dob', read_only=True)
    status = serializers.CharField(source='customer_status', read_only=True)
    date_registered = serializers.DateField(source='reg_date', read_only=True)

    class Meta:
        model = Customer
        fields = [
            'cust_no', 'first_name', 'last_name', 'other_names',
            'gender', 'id_no', 'phone_no', 'email',
            'postal_address', 'date_of_birth', 'date_registered',
            'customer_type', 'status',
        ]


class MemberRegistrationSerializer(serializers.Serializer):
    """Register a new chama member (official-side)."""
    full_name = serializers.CharField(max_length=255, required=False, default='')
    first_name = serializers.CharField(max_length=100)
    middle_name = serializers.CharField(max_length=100, required=False, default='', allow_blank=True)
    last_name = serializers.CharField(max_length=100)
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
    """Maps to Android NextOfKin model."""
    full_name = serializers.CharField(source='kin_name', read_only=True)
    relationship = serializers.CharField(source='kin_relationship', read_only=True)
    phone_no = serializers.CharField(source='kin_phone', read_only=True)
    id_no = serializers.CharField(source='kin_national_id', read_only=True)

    class Meta:
        model = NextOfKin
        fields = ['full_name', 'relationship', 'phone_no', 'id_no']


# ════════════════════════════════════════════════════════════════════════════
# ACCOUNT TYPES  (maps to Android AccountType model)
# ════════════════════════════════════════════════════════════════════════════

class AccountTypeSerializer(serializers.ModelSerializer):
    account_class = serializers.CharField(source='account_type', read_only=True)
    interest_calc_method = serializers.CharField(read_only=True)

    class Meta:
        model = CustomerAccountsSetup
        fields = ['account_code', 'account_name', 'account_class', 'interest_calc_method']


# ════════════════════════════════════════════════════════════════════════════
# TRANSACTIONS  (maps to Android SavingsTransaction / LoanTransaction models)
# ════════════════════════════════════════════════════════════════════════════

class SavingsTransactionSerializer(serializers.ModelSerializer):
    member_name = serializers.SerializerMethodField()
    description = serializers.CharField(source='tr_desc', read_only=True, default='')

    class Meta:
        model = SavingsTransaction
        fields = [
            'id', 'cust_no', 'member_name', 'saving_type', 'account_code',
            'tr_date', 'tr_ref', 'description',
            'debit_amount', 'credit_amount',
        ]

    def get_member_name(self, obj):
        try:
            c = Customer.objects.filter(cust_no=obj.cust_no).first()
            return c.full_name if c else ''
        except Exception:
            return ''


class LoanTransactionSerializer(serializers.ModelSerializer):
    member_name = serializers.SerializerMethodField()
    description = serializers.CharField(source='tr_desc', read_only=True, default='')

    class Meta:
        model = LoanTransaction
        fields = [
            'id', 'cust_no', 'member_name', 'loan_id', 'loan_no',
            'loan_type', 'account_code', 'tr_date', 'tr_ref',
            'description', 'debit_amount', 'credit_amount',
        ]

    def get_member_name(self, obj):
        try:
            c = Customer.objects.filter(cust_no=obj.cust_no).first()
            return c.full_name if c else ''
        except Exception:
            return ''


# ════════════════════════════════════════════════════════════════════════════
# ACTION REQUESTS
# ════════════════════════════════════════════════════════════════════════════

class RecordSavingsPaymentSerializer(serializers.Serializer):
    cust_no = serializers.CharField(max_length=20)
    account_code = serializers.CharField(max_length=50)
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
    cust_no = serializers.CharField(max_length=20)
    account_code = serializers.CharField(max_length=50)
    loan_no = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
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
    cust_no = serializers.CharField(max_length=20)
    amount = serializers.CharField(max_length=20)
    loan_product = serializers.CharField(max_length=50)
    repayment_period = serializers.IntegerField(min_value=1)
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

    def validate_loan_product(self, value):
        from django.db.models import Q
        if not CustomerAccountsSetup.objects.filter(
            Q(account_code=value) | Q(account_type=value),
            is_loan_account=True, is_active=True,
        ).exists():
            raise serializers.ValidationError('Invalid loan product.')
        return value


class RecordFineSerializer(serializers.Serializer):
    cust_no = serializers.CharField(max_length=20)
    amount = serializers.CharField(max_length=20)
    reason = serializers.CharField(max_length=255, required=False, default='Fine')

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
# LOANS  (maps to Android LoanEntry model)
# ════════════════════════════════════════════════════════════════════════════

class LoanHistorySerializer(serializers.ModelSerializer):
    cust_no = serializers.CharField(source='customer.cust_no', read_only=True)
    member_name = serializers.CharField(source='customer.full_name', read_only=True)
    loan_type = serializers.SerializerMethodField()
    loan_type_display = serializers.SerializerMethodField()
    amount_applied = serializers.DecimalField(source='principal', max_digits=14, decimal_places=2, read_only=True)
    amount_approved = serializers.DecimalField(source='principal', max_digits=14, decimal_places=2, read_only=True)
    repayment_period = serializers.IntegerField(source='loan_period', read_only=True)
    date_applied = serializers.DateField(source='loan_date', read_only=True)
    date_approved = serializers.SerializerMethodField()
    date_disbursed = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()
    interest_calc_method = serializers.SerializerMethodField()

    class Meta:
        model = LoanHistory
        fields = [
            'id', 'loan_no', 'cust_no', 'member_name',
            'loan_type', 'loan_type_display',
            'amount_applied', 'amount_approved',
            'repayment_period', 'installment', 'interest_rate',
            'interest_calc_method',
            'date_applied', 'is_approved', 'is_disbursed',
            'date_approved', 'date_disbursed', 'balance',
        ]

    def get_loan_type(self, obj):
        try:
            return obj.loan_type.account_type if obj.loan_type else ''
        except Exception:
            return ''

    def get_interest_calc_method(self, obj):
        try:
            return obj.loan_type.interest_calc_method if obj.loan_type else 'reducing_balance'
        except Exception:
            return 'reducing_balance'

    def get_loan_type_display(self, obj):
        try:
            return obj.loan_type.account_name if obj.loan_type else 'Loan'
        except Exception:
            return 'Loan'

    def get_date_approved(self, obj):
        if hasattr(obj, 'approved_at') and obj.approved_at:
            return obj.approved_at.strftime('%Y-%m-%d')
        return None

    def get_date_disbursed(self, obj):
        if obj.disbursed_at:
            return obj.disbursed_at.strftime('%Y-%m-%d')
        return None

    def get_balance(self, obj):
        """Calculate outstanding loan balance."""
        try:
            from django.db.models import Sum
            debits = LoanTransaction.objects.filter(
                loan_no=obj.loan_no
            ).aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
            credits = LoanTransaction.objects.filter(
                loan_no=obj.loan_no
            ).aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
            return str(debits - credits)
        except Exception:
            return '0'


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
    reference = serializers.CharField(source='ref')
    description = serializers.CharField()
    debit = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    credit = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    balance = serializers.DecimalField(max_digits=14, decimal_places=2)


class AccountStatementSerializer(serializers.Serializer):
    account_code = serializers.CharField(source='account_type')
    account_name = serializers.CharField()
    opening_balance = serializers.DecimalField(
        source='balance_brought_forward', max_digits=14, decimal_places=2)
    closing_balance = serializers.DecimalField(max_digits=14, decimal_places=2)
    entries = StatementLineSerializer(source='lines', many=True)


class MemberStatementSerializer(serializers.Serializer):
    cust_no = serializers.CharField()
    member_name = serializers.CharField(source='full_name')
    from_date = serializers.CharField(source='period_from')
    to_date = serializers.CharField(source='period_to')
    accounts = AccountStatementSerializer(many=True)


# ════════════════════════════════════════════════════════════════════════════
# MEMBER SEARCH (autocomplete)
# ════════════════════════════════════════════════════════════════════════════

class MemberSearchSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    phone = serializers.CharField(read_only=True)

    class Meta:
        model = Customer
        fields = ['cust_no', 'full_name', 'phone', 'national_id']


# ════════════════════════════════════════════════════════════════════════════
# UNSETTLED LOANS
# ════════════════════════════════════════════════════════════════════════════

class UnsettledLoanSerializer(serializers.ModelSerializer):
    loan_type = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()
    principal = serializers.DecimalField(max_digits=14, decimal_places=2)
    loan_date = serializers.DateField(read_only=True)

    class Meta:
        model = LoanHistory
        fields = ['loan_no', 'loan_type', 'principal', 'balance', 'loan_date']

    def get_loan_type(self, obj):
        try:
            return obj.loan_type.account_name if obj.loan_type else 'Loan'
        except Exception:
            return 'Loan'

    def get_balance(self, obj):
        from django.db.models import Sum
        debits = LoanTransaction.objects.filter(
            loan_no=obj.loan_no
        ).aggregate(t=Sum('debit_amount'))['t'] or Decimal('0')
        credits = LoanTransaction.objects.filter(
            loan_no=obj.loan_no
        ).aggregate(t=Sum('credit_amount'))['t'] or Decimal('0')
        return str(debits - credits)


# ════════════════════════════════════════════════════════════════════════════
# CASH ACCOUNTS (payment sources)
# ════════════════════════════════════════════════════════════════════════════

class CashAccountSerializer(serializers.Serializer):
    account_code = serializers.CharField()
    account_name = serializers.CharField()


# ════════════════════════════════════════════════════════════════════════════
# JOURNAL ENTRY
# ════════════════════════════════════════════════════════════════════════════

class JournalEntrySerializer(serializers.Serializer):
    cust_no = serializers.CharField(max_length=20)
    debit_account = serializers.CharField(max_length=50)
    credit_account = serializers.CharField(max_length=50)
    amount = serializers.CharField(max_length=20)
    description = serializers.CharField(max_length=255)
    payment_source = serializers.CharField(max_length=50, required=False, default='')

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
# LOAN APPLICATION (request for approval)
# ════════════════════════════════════════════════════════════════════════════

class LoanApplicationSerializer(serializers.Serializer):
    cust_no = serializers.CharField(max_length=20)
    amount = serializers.CharField(max_length=20)
    loan_product = serializers.CharField(max_length=50)
    repayment_period = serializers.IntegerField(min_value=1)
    interest_rate = serializers.DecimalField(max_digits=5, decimal_places=2, required=False)
    purpose = serializers.CharField(max_length=255, required=False, default='')

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

    def validate_loan_product(self, value):
        from django.db.models import Q
        if not CustomerAccountsSetup.objects.filter(
            Q(account_code=value) | Q(account_type=value),
            is_loan_account=True, is_active=True,
        ).exists():
            raise serializers.ValidationError('Invalid loan product.')
        return value


# ════════════════════════════════════════════════════════════════════════════
# PASSWORD RESET OTP
# ════════════════════════════════════════════════════════════════════════════

class RequestOtpSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=255,
        help_text='Username, email, or phone number')

class VerifyOtpSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=255)
    otp = serializers.CharField(max_length=6)

class ResetPasswordSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=255)
    otp = serializers.CharField(max_length=6)
    new_password = serializers.CharField(min_length=6)
