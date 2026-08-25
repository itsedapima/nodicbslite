import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from faker import Faker

# Import your models here - update the app names as necessary
from transactions.models import (
    SavingsTransaction, LoanTransaction, BulkUploadQueue, 
    MpesaNotification, PostedMpesaNotification, CustomerAccountsSetup,
    InterestBatch, InterestDetail, DividendDeclaration, 
    DividendDistribution, DividendSlipItem
)
# Assuming 'customers' is the app name for the Customer model
from customers.models import Customer 
from django.contrib.auth import get_user_model

User = get_user_model()
fake = Faker()

class Command(BaseCommand):
    help = "Generates 200,000 rows of dummy data for each model spanning 5 years."

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=200000, help='Number of rows per model')

    def handle(self, *args, **options):
        count = options['count']
        batch_size = 5000  # Number of rows to commit at once
        
        self.stdout.write(self.style.SUCCESS(f"Starting data generation ({count} rows per table)..."))

        # 0. Setup Prerequisites
        user, _ = User.objects.get_or_create(username='admin', defaults={'is_admin': True})
        
        # Ensure we have some Customers if they don't exist (using user's range 1-50,000)
        customer_ids = list(Customer.objects.values_list('id', flat=True)[:10000])
        if not customer_ids:
            self.stdout.write("No customers found. Creating 5000 placeholder customers...")
            customers = [Customer(cust_no=i, full_name=fake.name()) for i in range(1, 5001)]
            Customer.objects.bulk_create(customers)
            customer_ids = list(Customer.objects.values_list('id', flat=True))

        # 1. Customer Accounts Setup
        self._generate_account_setup()

        # 2. Savings & Loan Transactions
        self._generate_transactions(SavingsTransaction, count, batch_size, is_loan=False)
        self._generate_transactions(LoanTransaction, count, batch_size, is_loan=True)

        # 3. Mpesa Notifications
        self._generate_mpesa(count, batch_size)

        # 4. Bulk Upload Queue
        self._generate_bulk_queue(count, batch_size, customer_ids, user)

        # 5. Interest & Dividend Tables (Relational)
        self._generate_interest_data(count, batch_size)
        self._generate_dividend_data(count, batch_size, customer_ids)

        self.stdout.write(self.style.SUCCESS("Successfully populated database!"))

    def _get_random_date(self):
        """Returns a random date within the last 5 years."""
        days_back = random.randint(0, 365 * 5)
        return timezone.now() - timedelta(days=days_back)

    def _generate_account_setup(self):
        if CustomerAccountsSetup.objects.exists():
            return
        setups = [
            CustomerAccountsSetup(account_code="S01", account_name="Share Capital", acc_initials="SC", account_type="share_capital"),
            CustomerAccountsSetup(account_code="S02", account_name="Welfare Deposit", acc_initials="WD", account_type="welfare_deposit"),
            CustomerAccountsSetup(account_code="L01", account_name="Normal Loan", acc_initials="NL", account_type="normal_loan", is_loan_account=True),
        ]
        CustomerAccountsSetup.objects.bulk_create(setups)

    def _generate_transactions(self, model_class, total, batch, is_loan=False):
        self.stdout.write(f"Generating {total} rows for {model_class.__name__}...")
        objs = []
        for i in range(total):
            tr_date = self._get_random_date()
            data = {
                'cust_no': random.randint(1, 50000),
                'tr_date': tr_date,
                'tr_ref': f"REF-{fake.unique.bothify(text='??####')}",
                'ext_ref': fake.bothify(text='EXT-####'),
                'tr_desc': fake.sentence(nb_words=5),
                'debit_amount': Decimal(random.randint(0, 10000)),
                'credit_amount': Decimal(random.randint(0, 10000)),
                'created_by': 'system_gen'
            }
            if is_loan:
                data.update({
                    'loan_id': random.randint(1, 30000),
                    'loan_no': f"LN-{random.randint(1000, 99999)}",
                    'loan_type': random.choice(['normal_loan', 'repsi_loan'])
                })
            else:
                data['saving_type'] = random.choice(['share_capital', 'welfare_deposit', 'seed_deposit'])
            
            objs.append(model_class(**data))
            if len(objs) >= batch:
                model_class.objects.bulk_create(objs)
                objs = []
        model_class.objects.bulk_create(objs)

    def _generate_mpesa(self, total, batch):
        self.stdout.write(f"Generating {total} rows for MpesaNotification...")
        objs = []
        for i in range(total):
            m = MpesaNotification(
                transaction_type="PayBill",
                trans_id=fake.unique.bothify(text='?#?#?#?#?#').upper(),
                trans_time=self._get_random_date(),
                trans_amount=Decimal(random.randint(10, 50000)),
                business_shortcode="123456",
                bill_ref_number=str(random.randint(1, 50000)),
                msisdn=fake.phone_number()[:15],
                first_name=fake.first_name()
            )
            objs.append(m)
            if len(objs) >= batch:
                MpesaNotification.objects.bulk_create(objs)
                objs = []
        MpesaNotification.objects.bulk_create(objs)

    def _generate_bulk_queue(self, total, batch, customer_ids, user):
        self.stdout.write(f"Generating {total} rows for BulkUploadQueue...")
        objs = []
        for i in range(total):
            objs.append(BulkUploadQueue(
                date=self._get_random_date().date(),
                customer_id=random.choice(customer_ids),
                account_type=random.choice(['SC', 'WD', 'MS']),
                amount=Decimal(random.randint(100, 10000)),
                session_key=fake.uuid4(),
                status='processed',
                created_by=user
            ))
            if len(objs) >= batch:
                BulkUploadQueue.objects.bulk_create(objs)
                objs = []
        BulkUploadQueue.objects.bulk_create(objs)

    def _generate_interest_data(self, total, batch):
        self.stdout.write(f"Generating InterestBatch and {total} Details...")
        # Create 100 batches to distribute the 200k details
        batches = [
            InterestBatch(
                batch_no=f"BATCH-{i}",
                saving_type=random.choice(["share_capital", "welfare_deposit"]),
                cut_off_date=self._get_random_date().date(),
                created_by="admin"
            ) for i in range(100)
        ]
        InterestBatch.objects.bulk_create(batches)
        batch_objects = list(InterestBatch.objects.all())

        objs = []
        for i in range(total):
            objs.append(InterestDetail(
                batch=random.choice(batch_objects),
                cust_no=random.randint(1, 50000),
                member_name=fake.name(),
                weighted_avg_balance=Decimal(random.randint(1000, 100000)),
                net_payout=Decimal(random.randint(10, 5000)),
                is_posted=True
            ))
            if len(objs) >= batch:
                InterestDetail.objects.bulk_create(objs)
                objs = []
        InterestDetail.objects.bulk_create(objs)

    def _generate_dividend_data(self, total, batch, customer_ids):
        self.stdout.write(f"Generating DividendDeclaration and {total} Distributions...")
        decl = DividendDeclaration.objects.create(
            title="Annual Dividend 2025",
            start_date="2025-01-01",
            end_date="2025-12-31",
            total_profit=5000000,
            created_by="admin"
        )
        
        objs = []
        for i in range(total):
            objs.append(DividendDistribution(
                declaration=decl,
                customer_id=random.choice(customer_ids),
                net_dividend=Decimal(random.randint(100, 20000))
            ))
            if len(objs) >= batch:
                DividendDistribution.objects.bulk_create(objs)
                objs = []
        DividendDistribution.objects.bulk_create(objs)