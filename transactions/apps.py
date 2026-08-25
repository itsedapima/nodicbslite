import os
from django.apps import AppConfig

class TransactionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "transactions"

    def ready(self):
        # Only run this code in the main worker process, not the reloader
        if os.environ.get('RUN_MAIN') == 'true':
            from apscheduler.schedulers.background import BackgroundScheduler
            from .jobs import post_mpesa_notifications
            
            scheduler = BackgroundScheduler()
            # The database warning often occurs because APScheduler's 
            # default job store checks the DB during initialization.
            scheduler.add_job(
                post_mpesa_notifications, 
                "interval", 
                minutes=3,
                #seconds =30,
                id="post_mpesa_notifications",  # Adding an ID prevents duplicates
                replace_existing=True
            )
            
            scheduler.start()
            print("Scheduler started successfully (Worker Process)")
