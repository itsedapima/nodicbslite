"""
administration/jobs.py — Django-Q2 Scheduled Backup Job
========================================================
This module is the Django-Q2 entry-point.  It delegates all heavy lifting
to administration.utils so there is a single backup engine.

Scheduler configuration:
  Function: administration.jobs.perform_database_backup
  (This respects the interval gate — safe to schedule every minute/hour)

For on-demand backups from the admin UI, views.py calls
  administration.utils.perform_database_backup_now()
which skips the interval check entirely.
"""

import logging
from .utils import perform_database_backup as _scheduled_backup

logger = logging.getLogger(__name__)


def perform_database_backup():
    """
    Django-Q2 scheduled entry-point.

    Delegates to utils.perform_database_backup() which enforces the
    interval gate (only runs if enough time has elapsed since last_run).

    Returns:
        tuple: (success: bool, message: str)
    """
    logger.info("[BACKUP JOB] Django-Q2 scheduler triggered backup check.")
    return _scheduled_backup()
