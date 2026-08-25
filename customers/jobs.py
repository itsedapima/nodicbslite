# customers/jobs.py
import logging
from .utils import update_customer_statistics as _update_customer_statistics

logger = logging.getLogger(__name__)


def update_customer_statistics():
    """
    Django-Q2 scheduled entry point.

    Delegates to customers.utils.update_customer_statistics which:
      1. Flips Customer.customer_status  active <-> dormant  based on
         90-day transaction activity.
      2. Aggregates the post-update counts into the CustomerStats singleton.

    This ensures the scheduled job keeps Customer.customer_status in sync,
    which downstream consumers (MemberSnapshot, SMS notifications) rely on.
    """
    logger.info("Django Q2: Starting customer statistics update...")
    _update_customer_statistics()
    logger.info("Django Q2: Customer statistics update complete.")