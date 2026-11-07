"""Lazarus Foundry Django project package.

Importing the Celery app here ensures it is loaded whenever Django starts,
so ``@shared_task`` decorators in the apps bind to it.
"""
from .celery import app as celery_app

__all__ = ("celery_app",)
