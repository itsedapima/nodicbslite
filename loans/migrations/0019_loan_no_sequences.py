"""
Create Postgres sequences for loan number generation.

Sequences are atomic and outside transaction isolation — no race conditions
when multiple requests try to create loans concurrently.
"""
from django.db import migrations


def _extract_max_number(cursor, prefix):
    """
    Find the highest numeric suffix across ALL loan_no variants for a prefix.

    Handles mixed formats (e.g. MOBI000107 and MOBI00000108) by extracting
    the integer from every matching row and taking the max.
    """
    cursor.execute(
        "SELECT MAX(CAST(SUBSTRING(loan_no FROM %s) AS INTEGER)) "
        "FROM loans_loanhistory "
        "WHERE loan_no LIKE %s",
        [len(prefix) + 1, f"{prefix}%"],
    )
    result = cursor.fetchone()[0]
    return result or 0


def create_sequences(apps, schema_editor):
    """Create sequences initialised to the current max loan number."""
    with schema_editor.connection.cursor() as cursor:
        mobi_max = _extract_max_number(cursor, 'MOBI')
        ln_max = _extract_max_number(cursor, 'LN')

        # Drop and recreate to guarantee the sequence starts at the right value,
        # even if a stale sequence already exists from a previous attempt.
        cursor.execute("DROP SEQUENCE IF EXISTS loan_no_mobi_seq;")
        cursor.execute(
            "CREATE SEQUENCE loan_no_mobi_seq START WITH %s;",
            [mobi_max + 1],
        )
        cursor.execute("DROP SEQUENCE IF EXISTS loan_no_ln_seq;")
        cursor.execute(
            "CREATE SEQUENCE loan_no_ln_seq START WITH %s;",
            [ln_max + 1],
        )


def drop_sequences(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DROP SEQUENCE IF EXISTS loan_no_mobi_seq;")
        cursor.execute("DROP SEQUENCE IF EXISTS loan_no_ln_seq;")


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0018_loanhistory_offset_data_and_more'),
    ]

    operations = [
        migrations.RunPython(create_sequences, drop_sequences),
    ]
