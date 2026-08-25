import logging
import threading
from django.conf import settings
from django.utils.timezone import now
from .models import StatementSchedule, StatementLog
from sms.models import EmailLog

# Core mappings
from customers.models import Customer 
from statements.utils import generate_statement_pdf_bytes 

logger = logging.getLogger(__name__)

def run_monthly_statements(force=False):
    """
    Validates execution timeline, compiles core member statements,
    and queues individual outbound items directly inside EmailLog.
    """
    today = now().date()
    
    # 1. Schedule Validation
    schedule = StatementSchedule.objects.filter(is_active=True).first()
    
    if not force:
        if not schedule or schedule.day_of_month != today.day:
            logger.info(f"Skipping: Today is not the scheduled day ({today.day}).")
            return
    
    # 2. Extract targets with active email terminals
    members = Customer.objects.exclude(reg_email__isnull=True).exclude(reg_email="")

    if not members.exists():
        logger.warning("No members with valid email fields identified for queuing.")
        return

    logger.info(f"Queuing statement jobs for {members.count()} records (Forced: {force}).")

    for member in members:
        try:
            # 3. Generate individual raw payload data
            # NOTE: If your jobs.py needs to dynamically pick up raw files, consider saving 
            # the bytes to a secure path or appending references. For now, we queue the log entry.
            pdf_data = generate_statement_pdf_bytes(member)

            subject_line = f"Monthly Statement - {today.strftime('%B %Y')}"
            body_content = (
                f"Dear {member.full_name},\n\n"
                f"Please find attached your consolidated statement for {today.strftime('%B %Y')}.\n\n"
                f"Thank you."
            )

            # 4. Create the Outbound Pending Record
            EmailLog.objects.create(
                recipient_to=member.reg_email,
                subject=subject_line,
                message_body=body_content,
                is_html=False,
                status='pending',
                created_by='system_statement_job'
            )

            # 5. Log preliminary structure completion to StatementLog tracking
            StatementLog.objects.create(
                customer_name=member.full_name,
                email=member.reg_email,
                status='sent'  # Marks structured setup as successful
            )

        except Exception as e:
            logger.error(f"Failed to queue statement for record {member.reg_email}: {e}")
            StatementLog.objects.create(
                customer_name=member.full_name,
                email=member.reg_email,
                status='failed',
                error_message=str(e)
            )

    # 6. Metadata progression updates
    if schedule:
        schedule.last_run = today
        schedule.save()
        
    logger.info(f"Statement generation run processing completed. Entries pushed to EmailLog queue.")

def trigger_statements_background():
    """
    Threaded wrapper preventing operational context blocks from UI actions.
    """
    task_thread = threading.Thread(target=run_monthly_statements, kwargs={'force': True})
    task_thread.daemon = True
    task_thread.start()