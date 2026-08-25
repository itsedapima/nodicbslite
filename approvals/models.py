"""
approvals/models.py
--------------------
Generic maker-checker approval table.
Links to any Django model via GenericForeignKey (ContentType framework).
"""

from django.db import models
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType


class ApprovalRequest(models.Model):

    ACTION_CHOICES = [
        ('loan_disburse',   'Loan Disbursement'),
        ('cheque_issue',    "Banker's Cheque Issue"),
        ('bulk_post',       'Bulk Transaction Post'),
        ('interest_post',   'Interest Batch Post'),
        ('dividend_post',   'Dividend Posting'),
        ('journal_voucher', 'Journal Voucher'),
        ('income_entry',    'Income Entry'),
        ('expense_entry',   'Expense Entry'),
        ('member_exit',     'Member Exit'),
        ('account_setup',   'Account Setup Change'),
        ('fund_transfer',   'Inter-Account Fund Transfer'),
    ]

    STATUS_CHOICES = [
        ('pending',   'Pending Approval'),
        ('approved',  'Approved'),
        ('rejected',  'Rejected'),
        ('cancelled', 'Cancelled'),
    ]

    action_type = models.CharField(max_length=50, choices=ACTION_CHOICES, db_index=True)
    status      = models.CharField(max_length=20, choices=STATUS_CHOICES,
                                   default='pending', db_index=True)

    # ── Generic link to the object awaiting approval ───────────────────────
    content_type   = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, null=True, blank=True
    )
    object_id      = models.PositiveIntegerField(null=True, blank=True)
    content_object = GenericForeignKey('content_type', 'object_id')

    # ── People ─────────────────────────────────────────────────────────────
    maker = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='approval_requests_made',
    )
    checker = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='approval_requests_checked',
    )

    # ── Notes ──────────────────────────────────────────────────────────────
    maker_note   = models.TextField(blank=True)
    checker_note = models.TextField(blank=True)

    # ── Timestamps ─────────────────────────────────────────────────────────
    created_at  = models.DateTimeField(auto_now_add=True)
    actioned_at = models.DateTimeField(null=True, blank=True)

    # ── Payload snapshot (JSON) for audit / display ─────────────────────
    payload = models.JSONField(default=dict,
        help_text="Snapshot of submitted data for audit trail.")

    class Meta:
        ordering = ['-created_at']
        verbose_name        = 'Approval Request'
        verbose_name_plural = 'Approval Requests'
        indexes = [
            models.Index(fields=['status', 'action_type']),
            models.Index(fields=['maker']),
        ]

    def __str__(self):
        return (
            f"{self.get_action_type_display()} "
            f"by {self.maker.get_full_name() or self.maker.username} "
            f"— {self.get_status_display()}"
        )

    @property
    def is_pending(self):
        return self.status == 'pending'

    @property
    def is_approved(self):
        return self.status == 'approved'
