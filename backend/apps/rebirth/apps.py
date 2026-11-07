from django.apps import AppConfig


class RebirthConfig(AppConfig):
    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.rebirth"
    label: str = "rebirth"
    verbose_name: str = "Product Rebirth"
