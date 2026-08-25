"""
sms/sms_job.py — DEPRECATED
============================
This module previously contained a serial-modem based SMS queue processor.
All queue processing is now handled by sms/tasks.py (process_sms_queue)
which uses the Celcom Africa HTTP API via sms/utils.SMSGateway.

This file is kept as a redirect so any stale imports don't crash.
"""
import logging
import warnings

logger = logging.getLogger(__name__)


def process_sms_queue():
    """Redirect to the canonical queue processor in sms.tasks."""
    warnings.warn(
        "sms.sms_job.process_sms_queue() is deprecated. "
        "Use sms.tasks.process_sms_queue() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    from .tasks import process_sms_queue as _process
    return _process()
