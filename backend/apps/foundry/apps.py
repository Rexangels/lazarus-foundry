import logging
from typing import Any

from django.apps import AppConfig

logger = logging.getLogger(__name__)


def sweep_sandboxes_on_worker_ready(**_kwargs: Any) -> None:
    """Remove sandbox containers, volumes and run directories left behind by a crashed worker."""
    from apps.foundry.sandbox import sweep_orphaned_sandboxes

    try:
        report = sweep_orphaned_sandboxes()
    except Exception:  # the sweep must never stop the worker from starting
        logger.exception("sandbox orphan sweep failed")
        return
    if any((report.containers, report.volumes, report.networks, report.run_dirs)):
        logger.info("sandbox orphan sweep removed %s", report)


class FoundryConfig(AppConfig):
    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.foundry"
    label: str = "foundry"
    verbose_name: str = "Autonomous Engineering Foundry"

    def ready(self) -> None:
        from celery.signals import worker_ready

        # Fires only in a Celery worker process, once it is ready to take tasks.
        worker_ready.connect(sweep_sandboxes_on_worker_ready, weak=False, dispatch_uid="lazarus-sandbox-sweep")
