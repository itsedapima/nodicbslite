# customers/scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler
from .utils import update_running_loans_stats

def start_scheduler():
    scheduler = BackgroundScheduler()
    # Run every hour
    scheduler.add_job(update_running_loans_stats, 'interval', hours=1, id='stats_update', replace_existing=True)
    scheduler.start()