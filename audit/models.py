"""
audit/models.py
----------------
A persistent audit trail for security and operational events.

Why this exists alongside EmailLog:
  • EmailLog tracks message delivery to admin. If the admin email is
    mis-configured, the row still lands here so the security event
    itself is never lost.
  • Auditors expect a single table to query when investigating an
    incident — they should not have to grep `subject` strings out of
    an email log.
"""
from django.conf import settings
from django.db import models


class SecurityEvent(models.Model):
    """
    One row per security-sensitive thing that happened in the system.

    Examples:
      • LOGIN_SUCCESS / LOGIN_FAILED
      • PASSWORD_RESET_REQUESTED / PASSWORD_RESET_COMPLETED
      • USER_CREATED / USER_DEACTIVATED / ROLE_CHANGED
      • LARGE_TRANSACTION (≥ threshold)
      • DISBURSEMENT_APPROVED / DISBURSEMENT_REJECTED
      • MEMBER_EXIT_APPROVED
      • UNAUTHORIZED_ACCESS_ATTEMPT
    """
    SEVERITY_CHOICES = [
        ('info',     'Info'),
        ('warning',  'Warning'),
        ('critical', 'Critical'),
    ]

    event       = models.CharField(max_length=64, db_index=True)
    severity    = models.CharField(max_length=10, choices=SEVERITY_CHOICES,
                                   default='info', db_index=True)
    actor       = models.CharField(max_length=150, blank=True, null=True,
                                   help_text="Username or 'system' / 'anonymous'.")
    actor_user  = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='security_events',
    )
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    user_agent  = models.CharField(max_length=255, blank=True, null=True)
    object_ref  = models.CharField(max_length=120, blank=True, null=True,
                                   help_text="Free-form ref to the object affected, e.g. 'Customer 1024' or 'Loan LN-0042'.")
    details     = models.TextField(blank=True, null=True)
    email_sent  = models.BooleanField(default=False,
                                      help_text="True if an admin email log row was created for this event.")
    created_at  = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Security Event"
        verbose_name_plural = "Security Events"
        indexes = [
            models.Index(fields=['event', 'created_at']),
            models.Index(fields=['actor', 'created_at']),
        ]

    def __str__(self):
        return f"{self.event} by {self.actor or 'system'} at {self.created_at:%Y-%m-%d %H:%M}"
