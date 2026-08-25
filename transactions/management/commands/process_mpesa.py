"""
Management command to process M-Pesa notifications manually.

Usage:
    python manage.py process_mpesa              # process pending
    python manage.py process_mpesa --diagnose   # show what's stuck and why
"""

import logging
from django.core.management.base import BaseCommand
from django.db.models import Q

from transactions.models import MpesaNotification
from transactions.jobs import post_mpesa_notifications, BILL_REF_PATTERN


class Command(BaseCommand):
    help = "Process pending M-Pesa notifications (or diagnose stuck ones)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--diagnose",
            action="store_true",
            help="Don't process -- just show pending/stuck notifications and why",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Max rows to show in diagnose mode (default 20)",
        )

    def handle(self, *args, **options):
        # Force logging to console so you see everything
        logging.basicConfig(level=logging.DEBUG, force=True)
        logger = logging.getLogger("transactions.jobs")
        logger.setLevel(logging.DEBUG)

        if options["diagnose"]:
            self._diagnose(options["limit"])
        else:
            self._process()

    def _process(self):
        pending = MpesaNotification.objects.filter(posted=False).count()
        self.stdout.write(f"\nPending notifications: {pending}")

        if pending == 0:
            self.stdout.write(self.style.WARNING("Nothing to process.\n"))
            return

        self.stdout.write(f"Processing up to 200...\n")
        result = post_mpesa_notifications()
        self.stdout.write(self.style.SUCCESS(f"\nDone. Processed: {result}\n"))

        # Show remaining
        still_pending = MpesaNotification.objects.filter(posted=False).count()
        if still_pending:
            self.stdout.write(self.style.WARNING(
                f"{still_pending} still pending (run again or check --diagnose)\n"
            ))

    def _diagnose(self, limit):
        total     = MpesaNotification.objects.count()
        posted    = MpesaNotification.objects.filter(posted=True).count()
        pending   = MpesaNotification.objects.filter(posted=False).count()
        w_errors  = MpesaNotification.objects.filter(
            posted=False
        ).exclude(
            Q(last_error__isnull=True) | Q(last_error="")
        ).count()

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"  M-PESA NOTIFICATION DIAGNOSTICS")
        self.stdout.write(f"{'='*60}")
        self.stdout.write(f"  Total notifications:  {total}")
        self.stdout.write(f"  Already posted:       {posted}")
        self.stdout.write(f"  Pending (unposted):   {pending}")
        self.stdout.write(f"  Pending WITH errors:  {w_errors}")
        self.stdout.write(f"{'='*60}\n")

        if pending == 0:
            self.stdout.write(self.style.SUCCESS("Nothing pending!\n"))
            return

        # Show pending rows with detail
        rows = (
            MpesaNotification.objects
            .filter(posted=False)
            .order_by("id")[:limit]
        )

        for n in rows:
            ref = (n.bill_ref_number or "").strip().upper()
            match = BILL_REF_PATTERN.match(ref)
            status = "VALID" if match else "BAD REF"

            self.stdout.write(f"\n  id={n.id}  trans_id={n.trans_id}")
            self.stdout.write(f"    bill_ref_number : '{n.bill_ref_number}'")
            self.stdout.write(f"    parsed          : {status}")
            if match:
                cust, suffix = match.groups()
                self.stdout.write(f"    customer_no     : {cust}")
                self.stdout.write(f"    suffix          : {suffix}")
            self.stdout.write(f"    trans_amount    : {n.trans_amount}")
            self.stdout.write(f"    last_error      : {n.last_error or '(none)'}")

        if pending > limit:
            self.stdout.write(
                f"\n  ... and {pending - limit} more. "
                f"Use --limit {pending} to see all.\n"
            )
        self.stdout.write("")
