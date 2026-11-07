"""Celery entry point for System 1: ``ingest_repository_task(project_id)``.

Lifecycle
    1. Claim the project under ``select_for_update``: allowed from
       ``DORMANT``, ``FAILED`` or ``AUTOPSY_COMPLETE`` (re-ingest); refused
       (no changes, logged) when it is ``INGESTING``, ``SYNTHESIZING_GENOME``
       (the genome task is reading the artifacts) or further along.
       The claim sets ``INGESTING`` and clears ``status_detail``.
    2. Prepare ``WORKSPACE_ROOT/<project_id>``. A previous workspace is
       removed only when the path resolves to exactly that directory inside
       ``WORKSPACE_ROOT`` (a symlink or junction there is refused).
    3. Clone: the demo builds the PayPulse fixture under ``FIXTURE_ROOT`` if it
       is missing (never ``force``) and clones it with
       ``clone_local_repository_into_workspace``; any other project clones
       ``repo_url`` with ``clone_into_workspace`` (https + host allowlist).
    4. ``run_ingestion`` -> ``persist_ingestion`` (one transaction) ->
       ``on_ingestion_complete``, the single place that decides what follows
       ingestion: status ``SYNTHESIZING_GENOME``, then ``synthesize_genome_task``
       is enqueued after commit (run inline if the broker refuses it). The
       task's return value carries that hand-off status; the genome task
       later sets ``AUTOPSY_COMPLETE`` or ``FAILED``.
    Any exception sets ``FAILED`` with a user-safe ``status_detail``, emits
    ``ERROR`` and logs the traceback. The task never retries: a bad URL or a
    failed clone is the user's to fix, not a transient fault.

Telemetry: ``INGESTION_STARTED`` after the claim, the pipeline's
``BRANCH_DISCOVERED``/``AST_PARSED``, then ``INGESTION_COMPLETE`` once rows
and status are committed (or ``ERROR``), before any genome event.
Broadcasting never fails the job.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.genome.tasks import run_genome_synthesis, synthesize_genome_task
from apps.ingestion.git_forensics import (
    CloneError,
    GitForensicsError,
    UnsafeRepositorySourceError,
    UnsafeWorkspaceError,
    clone_into_workspace,
    clone_local_repository_into_workspace,
    resolve_workspace_dir,
)
from apps.ingestion.persistence import persist_ingestion
from apps.ingestion.pipeline import Emit, completion_payload, run_ingestion
from apps.projects.demo.history import FixtureBuildError, FixtureExistsError
from apps.projects.demo.paypulse import PAYPULSE_FIXTURE_NAME, build_paypulse_fixture
from apps.projects.git_runner import GitNotFoundError, GitTimeoutError, remove_tree
from apps.projects.models import Project, ProjectStatus
from apps.telemetry.broadcast import broadcast_telemetry
from apps.telemetry.events import TelemetryEventType

logger = logging.getLogger(__name__)

CLAIMABLE_STATUSES: frozenset[str] = frozenset({
    ProjectStatus.DORMANT,
    ProjectStatus.FAILED,
    ProjectStatus.AUTOPSY_COMPLETE,
})
MAX_STATUS_DETAIL_CHARS: int = 2000
INTERNAL_ERROR_DETAIL: str = "Ingestion failed because of an internal error; the worker log has the details."


class IngestionError(Exception):
    """A failure whose message is safe to show to the user as ``status_detail``."""


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------
def claim_project_for_ingestion(project_id: UUID | str) -> Project | None:
    """Atomically move the project to ``INGESTING``; None (and no change) when it can't be claimed.

    Raises ``Project.DoesNotExist`` for an unknown id.
    """
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=project_id)
        if project.status not in CLAIMABLE_STATUSES:
            logger.warning("Refusing to ingest project %s: status is %s.", project.pk, project.status)
            return None
        project.status = ProjectStatus.INGESTING
        project.status_detail = ""
        project.save(update_fields=["status", "status_detail", "updated_at"])
    return project


def mark_failed(project_id: UUID | str, detail: str) -> None:
    Project.objects.filter(pk=project_id).update(
        status=ProjectStatus.FAILED,
        status_detail=detail[:MAX_STATUS_DETAIL_CHARS],
        updated_at=timezone.now(),
    )


def start_genome_synthesis(project_id: UUID | str) -> None:
    """Enqueue ``synthesize_genome_task``; if the broker refuses, synthesize inline instead.

    This already runs in a Celery worker, so running the (bounded) synthesis
    here is better than leaving the project stuck in ``SYNTHESIZING_GENOME``.
    ``run_genome_synthesis`` never raises: it marks the project ``FAILED`` itself.
    """
    try:
        synthesize_genome_task.delay(str(project_id))
    except Exception:
        logger.exception("Could not enqueue genome synthesis for project %s; running it inline.", project_id)
        run_genome_synthesis(project_id)


def on_ingestion_complete(project_id: UUID | str, *, announce: Callable[[str], None] | None = None) -> str:
    """Decide what runs after a successful ingestion and return the project's new status.

    This is the only place that knows the next stage: the project moves to
    ``SYNTHESIZING_GENOME``, ``announce(status)`` reports it (the task emits
    ``INGESTION_COMPLETE`` there, so that event precedes every genome event),
    and genome synthesis starts once the surrounding transaction commits
    (immediately under autocommit). ``synthesize_genome_task`` sets
    ``AUTOPSY_COMPLETE`` or ``FAILED``.
    """
    status = ProjectStatus.SYNTHESIZING_GENOME
    Project.objects.filter(pk=project_id).update(status=status, status_detail="", updated_at=timezone.now())
    if announce is not None:
        announce(status)
    transaction.on_commit(lambda: start_genome_synthesis(project_id))
    return status


# ---------------------------------------------------------------------------
# Workspace and source
# ---------------------------------------------------------------------------
def workspace_dir_for(project: Project) -> Path:
    return Path(settings.WORKSPACE_ROOT).expanduser().resolve() / str(project.pk)


def prepare_workspace(project: Project) -> Path:
    """Return an empty ``WORKSPACE_ROOT/<project_id>``, deleting a previous workspace there if needed."""
    expected = workspace_dir_for(project)
    if expected.exists() or expected.is_symlink():
        resolved = expected.resolve()
        # Anything that redirects (symlink, junction) could point at another project or outside the root.
        if resolved != expected or expected.is_symlink():
            raise UnsafeWorkspaceError(f"refusing to replace {expected}: it does not resolve to itself ({resolved})")
        if not resolved.is_dir():
            raise UnsafeWorkspaceError(f"refusing to replace {expected}: it is not a directory")
        remove_tree(resolved)
    return resolve_workspace_dir(expected)


def demo_fixture_path() -> Path:
    return Path(settings.FIXTURE_ROOT).expanduser() / PAYPULSE_FIXTURE_NAME


def ensure_demo_fixture() -> Path:
    """The PayPulse fixture under ``FIXTURE_ROOT``, built first if it is missing (never rebuilt)."""
    destination = demo_fixture_path()
    if (destination / ".git").is_dir():
        return destination
    try:
        build_paypulse_fixture(destination, force=False)
    except FixtureExistsError:
        # Another worker may have built it meanwhile; anything else at that path is not ours to replace.
        if not (destination / ".git").is_dir():
            raise
    return destination


def clone_source(project: Project, workspace: Path) -> Path:
    if project.is_golden_demo:
        try:
            return clone_local_repository_into_workspace(ensure_demo_fixture(), workspace)
        except (FixtureBuildError, UnsafeRepositorySourceError, CloneError) as exc:
            raise IngestionError("The PayPulse demo repository could not be prepared.") from exc
    if not project.repo_url:
        raise IngestionError("The project has no repository URL to ingest.")
    return clone_into_workspace(project.repo_url, workspace)


def user_safe_error(exc: BaseException) -> str:
    """A ``status_detail`` for ``exc`` that never includes Git output, server paths or tracebacks."""
    if isinstance(exc, IngestionError):
        return str(exc)
    if isinstance(exc, UnsafeRepositorySourceError):
        # validate_remote_url's messages describe only the submitted URL.
        return f"The repository URL was rejected: {exc}"
    if isinstance(exc, CloneError):
        if isinstance(exc.__cause__, GitTimeoutError):
            return f"Cloning the repository timed out after {settings.INGEST_CLONE_TIMEOUT_SECONDS} seconds."
        if isinstance(exc.__cause__, GitNotFoundError):
            return "Git is not installed on the ingestion worker."
        return "The repository could not be cloned. Check that the URL is correct and the repository is public."
    if isinstance(exc, UnsafeWorkspaceError):
        return "The project workspace could not be prepared safely."
    if isinstance(exc, GitForensicsError):
        return "The repository's Git history could not be analyzed."
    if isinstance(exc, SoftTimeLimitExceeded):
        return f"Ingestion exceeded its time limit of {settings.INGEST_TASK_SOFT_TIME_LIMIT_SECONDS} seconds."
    if isinstance(exc, DatabaseError):
        return "Saving the ingestion results failed because of a database error."
    return INTERNAL_ERROR_DETAIL


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------
def telemetry_emitter(project_id: UUID) -> Emit:
    """An ``emit`` callback that broadcasts to the project's group and never raises."""

    def emit(event_type: TelemetryEventType, payload: dict[str, Any]) -> None:
        try:
            broadcast_telemetry(project_id, event_type, payload)
        except Exception:
            logger.exception("Could not broadcast %s for project %s.", event_type, project_id)

    return emit


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------
@shared_task(
    name="ingestion.ingest_repository",
    acks_late=True,
    soft_time_limit=settings.INGEST_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=settings.INGEST_TASK_TIME_LIMIT_SECONDS,
)
def ingest_repository_task(project_id: str) -> dict[str, Any]:
    """Clone, analyze, parse and persist one project (see the module docstring)."""
    try:
        project = claim_project_for_ingestion(project_id)
    except Project.DoesNotExist:
        logger.warning("Ingestion requested for unknown project %s.", project_id)
        return {"project_id": str(project_id), "status": "NOT_FOUND"}
    if project is None:
        return {"project_id": str(project_id), "status": "REFUSED"}

    emit = telemetry_emitter(project.pk)
    emit(
        TelemetryEventType.INGESTION_STARTED,
        {
            "name": project.name,
            "source": "demo" if project.is_golden_demo else "repo_url",
            "repo_url": None if project.is_golden_demo else project.repo_url,
        },
    )
    try:
        workspace = prepare_workspace(project)
        project.workspace_path = str(workspace)
        project.save(update_fields=["workspace_path", "updated_at"])
        clone_source(project, workspace)
        result = run_ingestion(workspace, emit=emit)
        persist_ingestion(project, result)

        def announce(status: str) -> None:
            emit(
                TelemetryEventType.INGESTION_COMPLETE,
                {**completion_payload(result.summary), "status": ProjectStatus(status).value},
            )

        # The status handed to the next stage; the genome task moves it on from there.
        status = on_ingestion_complete(project.pk, announce=announce)
    except Exception as exc:
        logger.exception("Ingestion of project %s failed.", project.pk)
        detail = user_safe_error(exc)
        try:
            mark_failed(project.pk, detail)
        except Exception:
            logger.exception("Could not mark project %s as FAILED.", project.pk)
        emit(TelemetryEventType.ERROR, {"stage": "INGESTION", "detail": detail})
        return {"project_id": str(project.pk), "status": ProjectStatus.FAILED.value, "detail": detail}
    return {"project_id": str(project.pk), "status": ProjectStatus(status).value}
