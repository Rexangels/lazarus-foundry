"""Celery application for Lazarus Foundry.

On Windows, run the worker with ``celery -A config worker -l info --pool=solo``;
the default prefork pool relies on ``fork()``, which Windows does not provide.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app: Celery = Celery("lazarus_foundry")

# All Celery options are read from Django settings prefixed with ``CELERY_``.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Discover ``tasks.py`` modules in every installed app.
app.autodiscover_tasks()
