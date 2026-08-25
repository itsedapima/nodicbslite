"""
Management command to register the monthly defaulter history snapshot
with Django-Q2's scheduler. Run once after deployment:

    python manage.py setup_defaulter_snapshot
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Register the monthly LoanDefaulterHistory snapshot schedule in Django-Q2."

    def handle(self, *args, **options):
        from django_q.models import Schedule

        name = "snapshot_defaulters_monthly"

        if Schedule.objects.filter(name=name).exists():
            self.stdout.write(self.style.WARNING(
                f"Schedule '{name}' already exists — skipping."
            ))
            return

        Schedule.objects.create(
            name=name,
            func="loans.jobs.snapshot_defaulters",
            schedule_type=Schedule.MONTHLY,
            repeats=-1,  # run forever
        )
        self.stdout.write(self.style.SUCCESS(
            f"✅ Registered monthly schedule '{name}'.\n"
            f"   The job reads RunningLoanStat and upserts into\n"
            f"   LoanDefaulterHistory. Run run_loan_stats_update FIRST\n"
            f"   so the data is fresh.\n"
            f"   To run it manually now:\n"
            f"     python manage.py shell -c "
            f"\"from loans.jobs import snapshot_defaulters; snapshot_defaulters()\""
        ))
