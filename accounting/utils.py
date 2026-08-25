import datetime
import io
import logging
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.timezone import now
from django.conf import settings
from django.http import HttpRequest
from .models import AutomatedReport, AutomatedReportLog
from accounts.models import CustomUser

# Set up logging
logger = logging.getLogger(__name__)

_scheduler = None  # Singleton scheduler instance

def get_scheduler():
    """Returns a singleton scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler()
        #_scheduler.add_job(send_scheduled_reports, 'cron', hour=10, minute=30)  # Runs daily at 10:30
        _scheduler.add_job(send_scheduled_reports, 'interval', hours=3)  # Runs every 3 hours 
       #_scheduler.add_job(send_scheduled_reports, 'interval', minutes=1)  # Runs every minute for testing
        
    return _scheduler


def send_scheduled_reports():
    """Send automated MMF reports based on congregation presence."""
    today = now().day
    reports = AutomatedReport.objects.filter(day_of_month=today)

    if not reports.exists():
        logger.info("No scheduled reports for today.")
        return

    start_date, end_date = get_financial_dates()

    for report in reports:
        officials = AutomatedReportOfficial.objects.filter(automated_report=report)
        recipients = [official.official.email for official in officials if official.official.email]

        if not recipients:
            logger.info(f"Skipping report '{report.name}' due to no valid email recipients.")
            continue  # No recipients; move to the next report

        try:
            if report.congregation:
                # Generate MMF Statement for Congregation-based Reports
                request = HttpRequest()
                admin_user = CustomUser.objects.filter(is_admin=True).first()
                request.user = admin_user if admin_user else None
                request.GET = {
                    'congregation': report.congregation.id,
                    'start_date': start_date.strftime("%Y-%m-%d") if start_date else '',
                    'end_date': end_date.strftime("%Y-%m-%d"),
                }

                pdf_response = mmf_statement_pdf(request)
                pdf_filename = f"MMF_Statement_{report.congregation.name}.pdf"
                email_subject = f"MMF Statement - {report.congregation.name} as of {end_date}"

            else:
                # General MMF Report when no congregation is linked
                mmf_description = MMFDescription.objects.first()
                if not mmf_description:
                    logger.warning(f"No MMFDescription found for '{report.name}', skipping.")
                    continue  # No financial period available, so skip this report

                pdf_response = download_mmf_pdf(None, mmf_id=mmf_description.id)
                pdf_filename = f"{report.name}.pdf"
                email_subject = f"Automated Report - {report.name} as at {end_date}"

            pdf_buffer = io.BytesIO(pdf_response.content)

            # Email content
            email_body = render_to_string('accounting/email_report.html', {'report_name': report.name})
            plain_message = email_body.strip()

            email = EmailMultiAlternatives(
                email_subject,
                plain_message,
                settings.DEFAULT_FROM_EMAIL,
                recipients
            )
            email.attach_alternative(email_body, "text/html")
            email.attach(pdf_filename, pdf_buffer.getvalue(), "application/pdf")

            # Send Email
            email.send()
            logger.info(f"Report '{report.name}' sent successfully to {recipients}")

            # Log success
            AutomatedReportLog.objects.create(automated_report=report, status="success")

        except Exception as e:
            # Log failure
            AutomatedReportLog.objects.create(automated_report=report, status="failed")
            logger.error(f"Failed to send '{report.name}' report: {e}")
