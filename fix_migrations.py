import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'nodicbslite.settings')
django.setup()
from django.db import connection
cursor = connection.cursor()
cursor.execute("DELETE FROM django_migrations WHERE app='loans' AND name='0002_initial'")
names = ['0002_initial','0002_alter_loanhistory_loan_type','0003_runningloanstat_last_repayment_date_and_more','0004_rename_member_no_runningloanstat_cust_no_and_more','0005_runningloanstat_loan_status_and_more','0006_interestchargebatch_interestchargedraftitem','0007_loancharge_loanchargerecovery','0008_loancharge_is_mandatory','0009_loanhistory_interest_rate']
[cursor.execute("INSERT INTO django_migrations (app, name, applied) VALUES ('loans', %s, NOW())", [n]) for n in names]
print('FIXED loans')
cursor.execute("SELECT app, name FROM django_migrations WHERE app IN ('accounting','accounts','customers','loans','administration') ORDER BY app, name")
[print(r[0] + ' | ' + r[1]) for r in cursor.fetchall()]
