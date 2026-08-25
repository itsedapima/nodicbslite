import logging
import threading
from django.core.mail import EmailMessage
from django.conf import settings
from django.utils.timezone import now
from .models import StatementSchedule, StatementLog

# Adjust imports to your project structure
from customers.models import Customer 
from statements.utils import generate_statement_pdf_bytes 

logger = logging.getLogger(__name__)

def run_monthly_statements(force=False):
    """
    Background task to generate and email statements.
    If force=True, it bypasses the day_of_month check.
    """
    today = now().date()
    
    # 1. Schedule Validation
    schedule = StatementSchedule.objects.filter(is_active=True).first()
    
    if not force:
        # Check if today matches the scheduled day
        if not schedule or schedule.day_of_month != today.day:
            logger.info(f"Skipping: Today is not the scheduled day ({today.day}).")
            return
    
    # 2. Fetch customers with valid emails (using reg_email as per your model)
    members = Customer.objects.exclude(email__isnull=True).exclude(email="")

    if not members.exists():
        logger.warning("No members with valid emails found for statement distribution.")
        return

    logger.info(f"Starting statement distribution for {members.count()} members (Forced: {force}).")

    for member in members:
        try:
            # 3. Generate PDF bytes
            pdf_data = generate_statement_pdf_bytes(member)

            # 4. Attach and send the email
            email = EmailMessage(
                subject=f"Monthly Statement - {today.strftime('%B %Y')}",
                body=f"Dear {member.full_name},\n\nPlease find attached your consolidated statement for {today.strftime('%B %Y')}.\n\nThank you.",
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[member.reg_email],
            )
            email.attach(f"Statement_{member.cust_no}.pdf", pdf_data, "application/pdf")
            email.send()

            # 5. Log success to database
            StatementLog.objects.create(
                customer_name=member.full_name,
                email=member.email,
                status='sent'
            )
        except Exception as e:
            logger.error(f"Failed to send statement to {member.email}: {e}")
            # Log failure to database
            StatementLog.objects.create(
                customer_name=member.full_name,
                email=member.email,
                status='failed',
                error_message=str(e)
            )

    # 6. Update schedule metadata
    if schedule:
        schedule.last_run = today
        schedule.save()
        
    logger.info(f"Statement distribution completed on {today}.")
