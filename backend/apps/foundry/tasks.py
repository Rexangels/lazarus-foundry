"""Celery entry point for System 4: ``run_foundry_task(foundry_run_id)``.

Enqueued by ``apps.revival.services.on_strategy_approved`` after an approval
commits. ``acks_late`` with no automatic retries: a run edits the workspace,
so it is never repeated behind the user's back (the claim also refuses a run
that already started).
"""
from __future__ import annotations

from typing import Any

from celery import shared_task
from django.conf import settings

from apps.foundry.services import run_foundry


@shared_task(
    name="foundry.run_foundry",
    acks_late=True,
    soft_time_limit=settings.FOUNDRY_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=settings.FOUNDRY_TASK_TIME_LIMIT_SECONDS,
)
def run_foundry_task(foundry_run_id: str) -> dict[str, Any]:
    """Celery wrapper around ``run_foundry``."""
    return run_foundry(foundry_run_id)
