import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'nodicbslite.settings')
django.setup()
from django.db import connection
cursor = connection.cursor()
cursor.execute("DELETE FROM django_migrations WHERE app='accounting' AND name='0002_initial'")
acct_names = ['0002_initial','0002_saccoaccountsledger_external_reference','0003_alter_saccoaccountsledger_amount','0004_alter_registrationfeeconfig_amount_and_more','0005_corebankingrecord_mpesarecord','0006_saccoaccountsledger_customer_saccoexpense_customer_and_more','0007_saccoexpense_expense_reference_and_more','0008_rename_expense_reference_saccoexpense_reference_and_more']
[cursor.execute("INSERT INTO django_migrations (app, name, applied) VALUES ('accounting', %s, NOW())", [n]) for n in acct_names]
print('FIXED accounting')
accts_names = ['0002_customuser_cbs_customer_alter_customuser_role','0003_remove_customuser_cbs_customer_customuser_cust_no_and_more']
[cursor.execute("INSERT INTO django_migrations (app, name, applied) VALUES ('accounts', %s, NOW())", [n]) for n in accts_names]
print('FIXED accounts')
cursor.execute("SELECT app, name FROM django_migrations WHERE app IN ('accounting','accounts') ORDER BY app, name")
[print(r[0] + ' | ' + r[1]) for r in cursor.fetchall()]
