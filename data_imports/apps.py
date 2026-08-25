from django.apps import AppConfig


class DataImportsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "data_imports"
    verbose_name = "Bulk Data Imports"
