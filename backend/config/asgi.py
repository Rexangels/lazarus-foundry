"""ASGI entry point served by Daphne (``daphne config.asgi:application``).

HTTP is handled by Django. WebSockets go through origin validation against
``ALLOWED_HOSTS`` and the session/auth middleware, then to the telemetry route
``ws/projects/<uuid:project_id>/telemetry/``.
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Initialize Django (app registry) before importing anything that touches models.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from apps.telemetry.routing import websocket_urlpatterns  # noqa: E402

application: ProtocolTypeRouter = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(AuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
    }
)
