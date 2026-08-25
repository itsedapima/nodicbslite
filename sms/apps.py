import os
from django.apps import AppConfig

class SmsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'sms'  # Replace with your actual app name

    def ready(self):
        # Clean! No more APScheduler background loops running inside the web process.
     pass