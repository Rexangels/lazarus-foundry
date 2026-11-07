from django.apps import AppConfig


class IngestionConfig(AppConfig):
    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.ingestion"
    label: str = "ingestion"
    verbose_name: str = "Artifact Ingestion"
