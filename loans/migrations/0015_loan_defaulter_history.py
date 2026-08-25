"""
0015_loan_defaulter_history
============================
Adds LoanDefaulterHistory — a snapshot table populated by the monthly
job (`loans.jobs.snapshot_defaulters`) to answer the appraisal question
"Has this applicant ever defaulted?" with a defensible audit trail.

Rows are keyed by (cust_no, loan_no); the snapshot preserves the WORST
observed arrears / days-past-due for each loan (never overwritten
downward), so a loan cured today still shows its historical bad point
on future appraisals.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("loans", "0014_remove_loanhistory_idx_cust_active_loans_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="LoanDefaulterHistory",
            fields=[
                ("id", models.BigAutoField(
                    auto_created=True, primary_key=True,
                    serialize=False, verbose_name="ID")),
                ("cust_no",  models.CharField(db_index=True, max_length=50)),
                ("loan_no",  models.CharField(db_index=True, max_length=50)),
                ("product_name", models.CharField(blank=True, max_length=150, null=True)),
                ("product_code", models.CharField(blank=True, max_length=50,  null=True)),
                ("first_default_date", models.DateField()),
                ("last_seen_date",     models.DateField()),
                ("loan_arrears",   models.DecimalField(
                    decimal_places=2, default=0, max_digits=14)),
                ("defaulted_days", models.IntegerField(default=0)),
                ("loan_classification", models.CharField(
                    blank=True, max_length=50, null=True)),
                ("is_resolved", models.BooleanField(default=False)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at",  models.DateTimeField(auto_now_add=True)),
                ("updated_at",  models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name_plural": "Loan Defaulter History",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("cust_no", "loan_no"),
                        name="uniq_defaulter_cust_loan",
                    ),
                ],
                "indexes": [
                    models.Index(
                        fields=["cust_no", "-last_seen_date"],
                        name="idx_defaulter_cust_seen",
                    ),
                    models.Index(
                        fields=["is_resolved", "defaulted_days"],
                        name="idx_defaulter_open",
                    ),
                ],
            },
        ),
    ]
