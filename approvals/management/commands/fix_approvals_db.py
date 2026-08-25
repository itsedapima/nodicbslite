# approvals/management/commands/fix_approvals_db.py
"""
One-time fix: drops the old N-approver approvals tables and resets
the migration state so the new maker-checker 0001_initial can run fresh.

Usage:
    python manage.py fix_approvals_db

After running this, run:
    python manage.py migrate approvals
"""

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Drop old approvals tables and reset migration state for maker-checker rebuild."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            # 1. Drop old tables (CASCADE handles FK constraints)
            old_tables = [
                'approvals_approvalvote',
                'approvals_approvalconfig',
                'approvals_approvalrequest',
            ]
            for table in old_tables:
                cursor.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = %s", [table]
                )
                if cursor.fetchone():
                    cursor.execute(f'DROP TABLE "{table}" CASCADE')
                    self.stdout.write(self.style.WARNING(f"  Dropped table: {table}"))
                else:
                    self.stdout.write(f"  Table {table} does not exist, skipping.")

            # 2. Remove ALL approvals migration records so 0001 can run fresh
            cursor.execute(
                "DELETE FROM django_migrations WHERE app = 'approvals'"
            )
            deleted = cursor.rowcount
            self.stdout.write(self.style.WARNING(
                f"  Removed {deleted} approvals migration record(s) from django_migrations."
            ))

        self.stdout.write(self.style.SUCCESS(
            "\nDone! Now run:\n"
            "    python manage.py migrate approvals\n"
        ))
