"""
sms/jobs.py
------------
Django-Q2 schedule entry points.

Register these in your Django-Q2 schedule (settings.py or admin):

    # ── Existing queue workers ────────────────────────────────────────
    Schedule.objects.create(
        func='sms.jobs.run_sms_queue',
        minutes=1, repeats=-1,
        name='SMS-Queue-Worker',
    )
    Schedule.objects.create(
        func='sms.jobs.run_email_queue',
        minutes=1, repeats=-1,
        name='Email-Queue-Worker',
    )

    # ── NEW: Nightly snapshot rebuild ─────────────────────────────────
    Schedule.objects.create(
        func='sms.jobs.refresh_snapshots',
        schedule_type=Schedule.DAILY,
        repeats=-1,
        name='Nightly-Member-Snapshot-Refresh',
    )

    # ── NEW: Reminder/Marketing jobs (examples) ──────────────────────
    # Daily birthday wishes
    Schedule.objects.create(
        func='sms.jobs.run_notification_by_category',
        kwargs='{"category": "happy_birthday"}',
        schedule_type=Schedule.DAILY,
        repeats=-1,
        name='Daily-Birthday-SMS',
    )

    # Weekly arrears reminder
    Schedule.objects.create(
        func='sms.jobs.run_notification_by_id',
        kwargs='{"notification_id": 4}',    # PK of your arrears template
        schedule_type=Schedule.WEEKLY,
        repeats=-1,
        name='Weekly-Arrears-Reminder',
    )

Retry policy (handled inside tasks.py):
  • Each pending SMS/email is attempted up to 3 times.
  • Between attempts there is a 12-hour cooldown, so even though the
    job runs every minute, a row that just failed won't be retried
    until 12 hours later.
  • After 3 failed attempts the row is terminally marked 'failed'.
"""

import logging

logger = logging.getLogger(__name__)


def run_sms_queue():
    """Django-Q2 hook — process pending SMS queue."""
    from .tasks import process_sms_queue
    return process_sms_queue()


def run_email_queue():
    """Django-Q2 hook — process pending email queue."""
    from .tasks import process_email_queue
    return process_email_queue()


# ═══════════════════════════════════════════════════════════════════════════
#  NEW: Snapshot refresh + notification runners
# ═══════════════════════════════════════════════════════════════════════════

def refresh_snapshots():
    """
    Django-Q2 hook — rebuild MemberSnapshot table.
    Schedule DAILY (e.g. 2:00 AM before any reminder jobs fire).
    """
    from .management.commands.refresh_member_snapshots import refresh_all_snapshots
    return refresh_all_snapshots()


def run_notification_by_id(notification_id: int):
    """
    Django-Q2 hook — fire a specific FrequentNotification by PK.
    Use this when you want precise control over which template fires.

    Schedule from Django admin → Django-Q2 → Scheduled Tasks:
        func:   sms.jobs.run_notification_by_id
        kwargs: {"notification_id": 5}
    """
    from .notification_helpers import run_notification
    return run_notification(notification_id=notification_id)


def run_notification_by_category(category: str):
    """
    Django-Q2 hook — fire ALL active notifications in a category.

    Schedule from Django admin → Django-Q2 → Scheduled Tasks:
        func:   sms.jobs.run_notification_by_category
        kwargs: {"category": "loan_arrears"}
    """
    from .notification_helpers import run_notification
    return run_notification(category=category)
