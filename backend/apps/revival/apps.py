from django.apps import AppConfig


class RevivalConfig(AppConfig):
    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.revival"
    label: str = "revival"
    verbose_name: str = "Revival Intelligence"
