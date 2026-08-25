import pandas as pd
from django.core.management.base import BaseCommand
from django.db import transaction
from loans.models import Guarantor,LoanHistory
from customers.models import Customer, CustomerEconomicActivity, NextOfKin, GroupOfficial, ChurchOfficial

class Command(BaseCommand):
    help = 'Imports 15,000 customers and their related data'

    def add_arguments(self, parser):
        parser.add_argument('file_path', type=str)

    def handle(self, *args, **options):
        file_path = options['file_path']
        df_dict = pd.read_excel(file_path, sheet_name=None) # Reads all sheets

        with transaction.atomic():
            # 1. Bulk Create Customers
            self.stdout.write("Importing Customers...")
            cust_objs = [Customer(**row) for row in df_dict['Customers'].to_dict('records')]
            Customer.objects.bulk_create(cust_objs, batch_size=2000)

            # Re-fetch for FK mapping (cust_no -> object)
            cust_map = {c.cust_no: c for c in Customer.objects.all()}

            # 2. Bulk Create Economic Activity
            self.stdout.write("Importing Economic Activities...")
            econ_objs = []
            for row in df_dict['EconomicActivity'].to_dict('records'):
                cust = cust_map.get(row.pop('cust_no'))
                if cust:
                    econ_objs.append(CustomerEconomicActivity(customer=cust, **row))
            CustomerEconomicActivity.objects.bulk_create(econ_objs, batch_size=2000)

            # 3. Bulk Create Next of Kin
            self.stdout.write("Importing Next of Kin...")
            kin_objs = []
            for row in df_dict['NextOfKin'].to_dict('records'):
                cust = cust_map.get(row.pop('cust_no'))
                if cust:
                    kin_objs.append(NextOfKin(customer=cust, **row))
            NextOfKin.objects.bulk_create(kin_objs, batch_size=2000)

            # 4. Bulk Create Officials
            self.stdout.write("Importing Group/Church Officials...")
            g_off = [GroupOfficial(customer=cust_map[r.pop('cust_no')], **r) for r in df_dict['GroupOfficials'].to_dict('records')]
            c_off = [ChurchOfficial(customer=cust_map[r.pop('cust_no')], **r) for r in df_dict['ChurchOfficials'].to_dict('records')]
            
            GroupOfficial.objects.bulk_create(g_off, batch_size=2000)
            ChurchOfficial.objects.bulk_create(c_off, batch_size=2000)

            self.stdout.write(self.style.SUCCESS(f"Finished! Imported {len(cust_objs)} customers."))