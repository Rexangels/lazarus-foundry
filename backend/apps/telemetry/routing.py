"""WebSocket URL patterns for the telemetry stream (mounted in ``config/asgi.py``)."""
from django.urls import URLPattern, path

from apps.telemetry.consumers import TelemetryConsumer

websocket_urlpatterns: list[URLPattern] = [
    path(
        "ws/projects/<uuid:project_id>/telemetry/",
        TelemetryConsumer.as_asgi(),
        name="project-telemetry",
    ),
]
