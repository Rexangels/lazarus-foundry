"""ASGI entry point served by Daphne (``daphne config.asgi:application``).

HTTP is handled by Django. The ``websocket`` protocol route for telemetry
(``ws/projects/<uuid:project_id>/telemetry/``) is added in Task 1.4.
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Initialize Django (app registry) before importing anything that touches models.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter  # noqa: E402

application: ProtocolTypeRouter = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
    }
)
