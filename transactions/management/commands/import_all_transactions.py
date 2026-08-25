import pandas as pd
from django.core.management.base import BaseCommand
from django.db import transaction
from loans.models import Guarantor,LoanHistory,Collateral
from customers.models import Customer
from transactions.models import (
    CustomerAccountsSetup, SavingsTransaction, LoanTransaction
)
class Command(BaseCommand):
    help = 'Complete import for Sacco master data'

    def add_arguments(self, parser):
        parser.add_argument('file_path', type=str)

    def handle(self, *args, **options):
        file_path = options['file_path']
        excel = pd.ExcelFile(file_path)
        cust_map = {c.cust_no: c for c in Customer.objects.all()}

        with transaction.atomic():
            # 1. Account Setup
            self.stdout.write("Importing Account Setup...")
            setup_objs = [CustomerAccountsSetup(**r) for r in excel.parse('AccountSetup').to_dict('records')]
            CustomerAccountsSetup.objects.bulk_create(setup_objs, ignore_conflicts=True)

            # 2. Loan History
            self.stdout.write("Importing Loans...")
            lh_df = excel.parse('LoanHistory')
            LoanHistory.objects.bulk_create([
                LoanHistory(customer=cust_map[r['cust_no']], **{k:v for k,v in r.items() if k != 'cust_no'}) 
                for r in lh_df.to_dict('records')
            ], batch_size=2000)
            
            loan_map = {l.loan_no: l for l in LoanHistory.objects.all()}

            # 3. Guarantors
            self.stdout.write("Importing Guarantors...")
            g_df = excel.parse('Guarantors')
            Guarantor.objects.bulk_create([
                Guarantor(loan=loan_map[r['loan_no']], guarantor_cust=cust_map[r['guarantor_cust_no']], amount=r['amount'])
                for r in g_df.to_dict('records')
            ], batch_size=2000)

            # 4. Collateral
            self.stdout.write("Importing Collateral...")
            c_df = excel.parse('Collateral')
            Collateral.objects.bulk_create([
                Collateral(loan=loan_map[r['loan_no']], owner=cust_map[r['owner_cust_no']], **{k:v for k,v in r.items() if k not in ['loan_no', 'owner_cust_no']})
                for r in c_df.to_dict('records')
            ], batch_size=2000)

            # 5. Loan Transactions (100k)
            self.stdout.write("Importing 100k Loan Transactions...")
            lt_df = excel.parse('LoanTransactions')
            LoanTransaction.objects.bulk_create([
                LoanTransaction(
                    loan_id=loan_map[r['loan_no']].id, # Use real ID from DB
                    **{k:v for k,v in r.items() if k != 'loan_id'}
                ) for r in lt_df.to_dict('records')
            ], batch_size=5000)

            # 6. Savings Transactions (100k)
            self.stdout.write("Importing 100k Savings Transactions...")
            st_df = excel.parse('SavingsTransactions')
            SavingsTransaction.objects.bulk_create([SavingsTransaction(**r) for r in st_df.to_dict('records')], batch_size=5000)

        self.stdout.write(self.style.SUCCESS("All systems green! Data import complete."))