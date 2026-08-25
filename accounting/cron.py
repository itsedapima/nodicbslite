'''from apscheduler.schedulers.background import BackgroundScheduler
from .utils import send_scheduled_reports

def start_scheduler(scheduler=None):
    """Starts the scheduler only if it's not already running."""
    if scheduler is None:
        scheduler = BackgroundScheduler()

    if scheduler.running:
        print("Automated reports scheduler is already running. Skipping start.")
        return  # Avoid multiple instances

    #scheduler.add_job(send_scheduled_reports, 'cron', hour=10, minute=17)  # Runs daily at 10:30 AM
    scheduler.add_job(send_scheduled_reports, 'cron', minute='*')  # Runs every minute

    scheduler.start()

    print("Automated reports scheduler started successfully.")'''
