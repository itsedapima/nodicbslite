# loans/jobs.py
import logging
from .utils import update_running_loans_stats

logger = logging.getLogger(__name__)

def run_loan_stats_update():
    """
    Task worker designed for Django Q2 scheduler.
    Refreshes RunningLoanStat records across the portfolio.
    """
    logger.info("Django Q2: Starting scheduled loan statistics recalculation task...")
    
    try:
        # Executes the optimized utility script
        records_updated = update_running_loans_stats(
            cust_no=None,           # Process all customers
            batch_size=1000,        # Secure performance batch sizing
            disbursed_only=True    # Process all loans per your script defaults
        )
        
        summary_message = f"Success: Recalculated and upserted statistics for {records_updated} loan(s)."
        logger.info(summary_message)
        
        # This return string saves directly into the Django Q successful task logs UI
        return summary_message

    except Exception as e:
        error_message = f"Critical Failure during scheduled loan stats run: {str(e)}"
        logger.exception(error_message)
        
        # Propagating the error ensures Django Q marks this task as 'Failed' with a full traceback
        raise Exception(error_message)


# ═══════════════════════════════════════════════════════════════════════════
#  DEFAULTER HISTORY SNAPSHOT
# ═══════════════════════════════════════════════════════════════════════════
def snapshot_defaulters():
    """
    Monthly Django-Q2 job — freezes a defaulter snapshot from
    RunningLoanStat into LoanDefaulterHistory.

    RUN AFTER run_loan_stats_update — this reads the freshest arrears
    figures produced there.

    Semantics
    ---------
    For every loan currently in arrears (defaulted_days >= DEFAULTER_MIN_DAYS),
    upsert a row in LoanDefaulterHistory keyed by (cust_no, loan_no):

      * If the row is NEW → record first_default_date = today.
      * If the row EXISTS →
          - loan_arrears / defaulted_days advance only if the observed
            value is HIGHER than the stored one (worst-point preservation);
          - last_seen_date is refreshed;
          - loan_classification / product_name refresh from the current
            RunningLoanStat.

    Loans whose RunningLoanStat.loan_balance has fallen to zero are
    marked is_resolved=True (kept for audit — still visible on appraisal).
    """
    from decimal import Decimal
    from django.conf import settings
    from django.db import transaction
    from django.utils import timezone
    from loans.models import LoanDefaulterHistory, RunningLoanStat

    min_days = int(getattr(settings, 'DEFAULTER_MIN_DAYS', 1))
    today    = timezone.now().date()
    logger.info("Django Q2: Starting defaulter snapshot (min_days=%s, date=%s)",
                min_days, today)

    upserts = resolves = 0

    # ── 1. Sweep loans currently in arrears ───────────────────────────
    arrears_qs = (
        RunningLoanStat.objects
        .filter(defaulted_days__gte=min_days)
        .only('loan_no', 'cust_no', 'product_code', 'product_description',
              'total_arrears', 'defaulted_days', 'loan_classification')
    )

    for stat in arrears_qs.iterator(chunk_size=1000):
        try:
            with transaction.atomic():
                row, created = LoanDefaulterHistory.objects.get_or_create(
                    cust_no=stat.cust_no,
                    loan_no=stat.loan_no,
                    defaults={
                        'product_code':        stat.product_code or '',
                        'product_name':        stat.product_description or '',
                        'first_default_date':  today,
                        'last_seen_date':      today,
                        'loan_arrears':        stat.total_arrears or Decimal('0'),
                        'defaulted_days':      stat.defaulted_days or 0,
                        'loan_classification': stat.loan_classification or '',
                    },
                )
                if not created:
                    # Preserve WORST-observed figures — never write down.
                    dirty = False
                    cur_arr  = stat.total_arrears or Decimal('0')
                    cur_days = stat.defaulted_days or 0
                    if cur_arr > row.loan_arrears:
                        row.loan_arrears = cur_arr; dirty = True
                    if cur_days > row.defaulted_days:
                        row.defaulted_days = cur_days; dirty = True
                    if stat.loan_classification and \
                       stat.loan_classification != row.loan_classification:
                        row.loan_classification = stat.loan_classification
                        dirty = True
                    if stat.product_description and not row.product_name:
                        row.product_name = stat.product_description; dirty = True
                    if stat.product_code and not row.product_code:
                        row.product_code = stat.product_code; dirty = True
                    row.last_seen_date = today
                    row.is_resolved    = False
                    row.resolved_at    = None
                    row.save(update_fields=[
                        'loan_arrears', 'defaulted_days', 'loan_classification',
                        'product_name', 'product_code',
                        'last_seen_date', 'is_resolved', 'resolved_at',
                    ] if dirty else ['last_seen_date', 'is_resolved', 'resolved_at'])
                upserts += 1
        except Exception:
            logger.exception("Failed to snapshot defaulter for %s / %s",
                             stat.cust_no, stat.loan_no)

    # ── 2. Mark cleared loans as resolved ────────────────────────────
    cleared_qs = (
        LoanDefaulterHistory.objects
        .filter(is_resolved=False)
        .values_list('id', 'loan_no')
    )
    open_loan_nos = set(
        RunningLoanStat.objects
        .filter(loan_balance__gt=0)
        .values_list('loan_no', flat=True)
    )
    to_close = [row_id for row_id, ln in cleared_qs if ln not in open_loan_nos]
    if to_close:
        LoanDefaulterHistory.objects.filter(id__in=to_close).update(
            is_resolved=True, resolved_at=timezone.now(),
        )
        resolves = len(to_close)

    summary = (f"Defaulter snapshot done: {upserts} upserts, "
               f"{resolves} resolved.")
    logger.info(summary)
    return summary