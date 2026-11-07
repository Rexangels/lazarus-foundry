"""WSGI entry point (HTTP only). The primary server is Daphne via ``config.asgi``."""
import os

from django.core.handlers.wsgi import WSGIHandler
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application: WSGIHandler = get_wsgi_application()
