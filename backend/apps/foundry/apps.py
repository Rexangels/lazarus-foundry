from django.apps import AppConfig


class FoundryConfig(AppConfig):
    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.foundry"
    label: str = "foundry"
    verbose_name: str = "Autonomous Engineering Foundry"
