import random
from datetime import timedelta
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from faker import Faker

# Replace 'loans' and 'customers' with your actual app names
from loans.models import LoanHistory, Guarantor, Collateral
from transactions.models import CustomerAccountsSetup
from customers.models import Customer

fake = Faker()

class Command(BaseCommand):
    help = "Generates 30,000 rows of Loan History, Guarantors, and Collateral"

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=30000)

    def handle(self, *args, **options):
        count = options['count']
        batch_size = 2000
        
        self.stdout.write(self.style.SUCCESS(f"Fetching prerequisites..."))
        
        # 1. Get Prerequisites
        customers = list(Customer.objects.all())
        if not customers:
            self.stdout.write(self.style.ERROR("No customers found. Please run customer generation first."))
            return

        loan_types = list(CustomerAccountsSetup.objects.filter(is_loan_account=True))
        if not loan_types:
            self.stdout.write(self.style.ERROR("No Loan Account types found in CustomerAccountsSetup."))
            return

        # Determine the starting point for LN numbers
        last_loan = LoanHistory.objects.order_by('id').last()
        current_ln_seq = 1
        if last_loan and last_loan.loan_no:
            try:
                current_ln_seq = int(last_loan.loan_no[2:]) + 1
            except ValueError:
                pass

        # 2. Generate Loan History
        self.stdout.write(f"Generating {count} Loan History records...")
        
        created_loans = []
        # We need to save loans in batches to get IDs back for Guarantors/Collateral
        for i in range(count):
            principal = Decimal(random.randint(50000, 2000000))
            # Simulating some fees
            proc_fee = principal * Decimal('0.015') # 1.5%
            ins_fee = principal * Decimal('0.005')  # 0.5%
            
            loan = LoanHistory(
                loan_no=f'LN{current_ln_seq:06d}',
                customer=random.choice(customers),
                loan_date=fake.date_between(start_date='-5y', end_date='today'),
                principal=principal,
                installment=principal / Decimal(random.choice([12, 24, 36, 48])),
                loan_type=random.choice(loan_types),
                loan_period=random.choice([12, 24, 36, 48, 60]),
                net_disbursed=principal - proc_fee - ins_fee,
                processing_fee=proc_fee,
                insurance_fee=ins_fee,
                other_charges=0,
                created_by='admin_gen'
            )
            created_loans.append(loan)
            current_ln_seq += 1

            if len(created_loans) >= batch_size:
                LoanHistory.objects.bulk_create(created_loans)
                created_loans = []
        
        LoanHistory.objects.bulk_create(created_loans)
        
        # Re-fetch the newly created loans to link Guarantors and Collateral
        # We take the most recent 'count' records
        recent_loans = list(LoanHistory.objects.order_by('-id')[:count])

        # 3. Generate Guarantors
        self.stdout.write("Generating Guarantors for loans...")
        guarantors_to_create = []
        for loan in recent_loans:
            # Each loan gets 1 to 3 guarantors
            num_guarantors = random.randint(1, 3)
            possible_guarantors = random.sample(customers, num_guarantors)
            
            for g_cust in possible_guarantors:
                if g_cust.cust_no != loan.customer.cust_no: # Don't guarantee yourself
                    guarantors_to_create.append(Guarantor(
                        loan=loan,
                        guarantor_cust=g_cust,
                        amount=loan.principal / Decimal(num_guarantors)
                    ))
            
            if len(guarantors_to_create) >= batch_size:
                Guarantor.objects.bulk_create(guarantors_to_create)
                guarantors_to_create = []
        Guarantor.objects.bulk_create(guarantors_to_create)

        # 4. Generate Collateral
        self.stdout.write("Generating Collateral for loans...")
        collaterals_to_create = []
        for loan in recent_loans:
            # 60% of loans have collateral
            if random.random() > 0.4:
                c_type = random.choice(['land', 'vehicle', 'other'])
                market_val = loan.principal * Decimal('1.5')
                
                collateral = Collateral(
                    loan=loan,
                    owner=loan.customer,
                    collateral_type=c_type,
                    market_value=market_val,
                    forced_sale_value=market_val * Decimal('0.7'),
                    created_by='admin_gen'
                )
                
                if c_type == 'land':
                    collateral.title_deed_no = f"TITLE/{fake.bothify('??/####')}"
                    collateral.location = fake.city()
                    collateral.size = "0.05 Ha"
                elif c_type == 'vehicle':
                    collateral.registration_no = fake.bothify('K?? ###?')
                    collateral.chassis_no = fake.bothify('CHAS#######')
                    collateral.model = random.choice(['Toyota Hilux', 'Isuzu NPR', 'Nissan NP200'])
                
                collaterals_to_create.append(collateral)

            if len(collaterals_to_create) >= batch_size:
                Collateral.objects.bulk_create(collaterals_to_create)
                collaterals_to_create = []
        Collateral.objects.bulk_create(collaterals_to_create)

        self.stdout.write(self.style.SUCCESS(f"Successfully generated loan data!"))