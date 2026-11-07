from django.apps import AppConfig


class TelemetryConfig(AppConfig):
    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.telemetry"
    label: str = "telemetry"
    verbose_name: str = "Real-Time Telemetry"
