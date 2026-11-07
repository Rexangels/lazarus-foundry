from django.apps import AppConfig


class GenomeConfig(AppConfig):
    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.genome"
    label: str = "genome"
    verbose_name: str = "Project Genome"
