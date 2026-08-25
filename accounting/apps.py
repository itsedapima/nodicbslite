'''from django.apps import AppConfig

class AccountingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'accounting'

    def ready(self):
        """Ensure scheduler starts only after Django is fully initialized."""
        from .utils import get_scheduler  # Import inside ready() to avoid circular import
        scheduler = get_scheduler()
        if not scheduler.running:
            scheduler.start()
            print("Automated reports scheduler started successfully.")
'''