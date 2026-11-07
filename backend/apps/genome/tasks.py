"""Celery entry point for System 2: ``synthesize_genome_task(project_id)``.

Lifecycle (``run_genome_synthesis``, also called inline by
``apps.ingestion.tasks.start_genome_synthesis`` when the task cannot be enqueued)
    1. Claim the project under ``select_for_update``: allowed from
       ``SYNTHESIZING_GENOME`` (set by ``on_ingestion_complete``) or
       ``AUTOPSY_COMPLETE`` (re-synthesis, e.g. after configuring a model key);
       refused without changes otherwise. The claim sets ``SYNTHESIZING_GENOME``.
    2. ``load_catalog`` -> ``GenomeReconcilerAgent().synthesize`` (model
       failures fall back inside the agent) -> ``persist_genome`` and the status
       ``AUTOPSY_COMPLETE``, in one transaction.
    3. Telemetry after commit: ``GENOME_NODE_CREATED`` per node,
       ``CONFLICT_DETECTED`` per conflict, then ``GENOME_COMPLETE``.
    4. ``on_genome_complete``: the only place that decides what follows a
       genome. It generates the revival strategies inline, in this worker
       (``apps.revival.services.run_strategy_generation``), which emits
       ``STRATEGY_GENERATED`` per strategy. A strategy failure never touches
       the genome: the project stays ``AUTOPSY_COMPLETE`` with a
       ``status_detail`` and an ``ERROR`` (``stage: "REVIVAL"``).
    Any exception in steps 1-3 sets ``FAILED`` with a user-safe ``status_detail``,
    emits ``ERROR`` (``stage: "GENOME"``) and logs the traceback. No automatic retries.

Why inline rather than a chained Celery task: deterministic generation takes
milliseconds and reads the genome just committed; running it here needs no
second enqueue (and no broker-down fallback like ``start_genome_synthesis``),
and the task returns only once the project's strategies are ready. With a
model key the prose refinement adds at most one bounded model request.
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from typing import Any
from uuid import UUID

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.genome.agents import GenomeReconcilerAgent
from apps.genome.scoring import UncitedIntentNodeError
from apps.genome.services import GenomeError, PersistedGenome, load_catalog, persist_genome
from apps.projects.models import Project, ProjectStatus
from apps.revival.agents import SpecRefinementAgent
from apps.revival.services import run_strategy_generation
from apps.telemetry.broadcast import broadcast_telemetry
from apps.telemetry.events import TelemetryEventType

logger = logging.getLogger(__name__)

GENOME_CLAIMABLE_STATUSES: frozenset[str] = frozenset({
    ProjectStatus.SYNTHESIZING_GENOME,
    ProjectStatus.AUTOPSY_COMPLETE,
})
MAX_STATUS_DETAIL_CHARS: int = 2000
INTERNAL_ERROR_DETAIL: str = "Genome synthesis failed because of an internal error; the worker log has the details."

Emit = Callable[[TelemetryEventType, dict[str, Any]], None]


def telemetry_emitter(project_id: UUID) -> Emit:
    """An ``emit`` callback that broadcasts to the project's group and never raises."""

    def emit(event_type: TelemetryEventType, payload: dict[str, Any]) -> None:
        try:
            broadcast_telemetry(project_id, event_type, payload)
        except Exception:
            logger.exception("Could not broadcast %s for project %s.", event_type, project_id)

    return emit


def claim_project_for_genome(project_id: UUID | str) -> Project | None:
    """Atomically move the project to ``SYNTHESIZING_GENOME``; None (no change) if it can't be claimed."""
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=project_id)
        if project.status not in GENOME_CLAIMABLE_STATUSES:
            logger.warning("Refusing to synthesize the genome of project %s: status is %s.", project.pk, project.status)
            return None
        project.status = ProjectStatus.SYNTHESIZING_GENOME
        project.status_detail = ""
        project.save(update_fields=["status", "status_detail", "updated_at"])
    return project


def user_safe_error(exc: BaseException) -> str:
    if isinstance(exc, GenomeError):
        return str(exc)
    if isinstance(exc, UncitedIntentNodeError):
        return "The genome was not saved: a feature had no evidence citation."
    if isinstance(exc, SoftTimeLimitExceeded):
        return f"Genome synthesis exceeded its time limit of {settings.GENOME_TASK_SOFT_TIME_LIMIT_SECONDS} seconds."
    if isinstance(exc, DatabaseError):
        return "Saving the genome failed because of a database error."
    return INTERNAL_ERROR_DETAIL


def _emit_results(emit: Emit, persisted: PersistedGenome) -> dict[str, Any]:
    for node, feature in persisted.nodes:
        emit(
            TelemetryEventType.GENOME_NODE_CREATED,
            {
                "node_id": str(node.pk),
                "title": node.title,
                "category": feature.category.value,
                "vital_status": feature.vital_status.value,
                "epistemic_status": feature.epistemic_status.value,
                "confidence": feature.confidence,
                "citation_count": len(feature.citations),
                "graph_position": dict(feature.graph_position),
            },
        )
    for record, node in persisted.conflicts:
        emit(
            TelemetryEventType.CONFLICT_DETECTED,
            {
                "node_id": str(node.pk),
                "title": node.title,
                "claim_source": record.claim_source,
                "reality_source": record.reality_source,
            },
        )
    synthesis = persisted.synthesis
    features = [feature for _, feature in persisted.nodes]
    payload = {
        "synthesis_id": str(synthesis.pk),
        "mode": synthesis.mode,
        "model_id": synthesis.model_id,
        "node_count": len(features),
        "conflict_count": len(persisted.conflicts),
        "vital_status_counts": dict(Counter(f.vital_status.value for f in features)),
        "epistemic_status_counts": dict(Counter(f.epistemic_status.value for f in features)),
        "fallback_reason": synthesis.validation_report.get("fallback_reason"),
        "status": ProjectStatus.AUTOPSY_COMPLETE.value,
    }
    emit(TelemetryEventType.GENOME_COMPLETE, payload)
    return payload


def on_genome_complete(
    project_id: UUID | str, *, emit: Emit, strategy_agent: SpecRefinementAgent | None = None
) -> dict[str, Any]:
    """Decide what runs after a committed genome: revival strategy generation, inline. Never raises."""
    try:
        return run_strategy_generation(project_id, agent=strategy_agent, emit=emit)
    except Exception:  # e.g. the database went away; the committed genome must not be reported as failed
        logger.exception("Strategy generation of project %s could not run.", project_id)
        return {"project_id": str(project_id), "status": "FAILED"}


def run_genome_synthesis(
    project_id: UUID | str,
    *,
    agent: GenomeReconcilerAgent | None = None,
    emit: Emit | None = None,
    strategy_agent: SpecRefinementAgent | None = None,
) -> dict[str, Any]:
    """Synthesize, persist and announce one project's genome, then hand off (see the module docstring). Never raises."""
    try:
        project = claim_project_for_genome(project_id)
    except Project.DoesNotExist:
        logger.warning("Genome synthesis requested for unknown project %s.", project_id)
        return {"project_id": str(project_id), "status": "NOT_FOUND"}
    if project is None:
        return {"project_id": str(project_id), "status": "REFUSED"}

    emit = emit or telemetry_emitter(project.pk)
    try:
        catalog = load_catalog(project)
        result = (agent or GenomeReconcilerAgent()).synthesize(catalog)
        with transaction.atomic():
            persisted = persist_genome(project, result)
            Project.objects.filter(pk=project.pk).update(
                status=ProjectStatus.AUTOPSY_COMPLETE, status_detail="", updated_at=timezone.now()
            )
    except Exception as exc:
        logger.exception("Genome synthesis of project %s failed.", project.pk)
        detail = user_safe_error(exc)
        try:
            Project.objects.filter(pk=project.pk).update(
                status=ProjectStatus.FAILED, status_detail=detail[:MAX_STATUS_DETAIL_CHARS], updated_at=timezone.now()
            )
        except Exception:
            logger.exception("Could not mark project %s as FAILED.", project.pk)
        emit(TelemetryEventType.ERROR, {"stage": "GENOME", "detail": detail})
        return {"project_id": str(project.pk), "status": ProjectStatus.FAILED.value, "detail": detail}

    summary = _emit_results(emit, persisted)
    strategies = on_genome_complete(project.pk, emit=emit, strategy_agent=strategy_agent)
    return {
        "project_id": str(project.pk),
        "status": ProjectStatus.AUTOPSY_COMPLETE.value,
        "mode": summary["mode"],
        "node_count": summary["node_count"],
        "conflict_count": summary["conflict_count"],
        "strategies": strategies["status"],
    }


@shared_task(
    name="genome.synthesize_genome",
    acks_late=True,
    soft_time_limit=settings.GENOME_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=settings.GENOME_TASK_TIME_LIMIT_SECONDS,
)
def synthesize_genome_task(project_id: str) -> dict[str, Any]:
    """Celery wrapper around ``run_genome_synthesis``."""
    return run_genome_synthesis(project_id)
