import uuid
from datetime import timedelta

from django.db import models
from django.utils.timezone import now


class StatementSchedule(models.Model):
    day_of_month = models.PositiveIntegerField(default=28, help_text="Day of the month to send statements (1-28)")
    is_active = models.BooleanField(default=True)
    last_run = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"Monthly Statement on Day {self.day_of_month}"


class StatementLog(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    customer_name = models.CharField(max_length=255)
    email = models.EmailField()
    status = models.CharField(max_length=20, choices=[('sent', 'Sent'), ('failed', 'Failed')])
    error_message = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ['-timestamp']


def _default_expiry():
    return now() + timedelta(days=30)


class StatementHash(models.Model):
    """
    Stores a verification hash for every generated statement.
    QR code on the PDF links to /statements/verify/<hash_value>/
    Records auto-expire after 30 days.
    """
    hash_value = models.CharField(max_length=64, unique=True, db_index=True)
    cust_no = models.CharField(max_length=20)
    customer_name = models.CharField(max_length=255)
    account_code = models.CharField(max_length=50)
    statement_type = models.CharField(
        max_length=20,
        choices=[("single", "Single Account"), ("consolidated", "Consolidated")],
        default="single",
    )
    generated_by = models.CharField(max_length=150, blank=True)
    generated_at = models.DateTimeField(default=now)
    expires_at = models.DateTimeField(default=_default_expiry)

    class Meta:
        ordering = ["-generated_at"]

    def __str__(self):
        return f"Stmt {self.account_code} – {self.customer_name} ({self.hash_value[:10]}…)"

    @property
    def is_expired(self):
        return now() > self.expires_at