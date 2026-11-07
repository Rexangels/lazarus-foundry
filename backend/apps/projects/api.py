"""Project endpoints (mounted at ``/api/projects/`` in ``config.api``).

``POST /api/projects/ingest``
    Body: ``{"repo_url": "https://github.com/owner/repo", "name"?: str}`` or
    ``{"demo": "paypulse", "name"?: str}`` (exactly one source). The URL is
    checked with ``validate_remote_url`` before anything is written (422 with
    the reason); local paths and ``file://`` URLs are never accepted. Creates a
    ``DORMANT`` project, queues ``ingest_repository_task`` after the
    transaction commits and returns 202. When the queue (Redis) is unreachable
    the project is marked ``FAILED`` and the response is 503.
``GET /api/projects/{project_id}``
    Status, ``status_detail``, default branch, timestamps and the ingestion summary.
``GET /api/projects/{project_id}/branches``
    ``BranchSnapshot`` rows for the Autopsy's branch nodes, default branch first.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router, Schema
from pydantic import ConfigDict, Field, field_validator, model_validator

from apps.ingestion.git_forensics import validate_remote_url
from apps.ingestion.models import ArtifactRecord, ArtifactType, BranchSnapshot
from apps.ingestion.tasks import ingest_repository_task
from apps.projects.models import Project, ProjectStatus

logger = logging.getLogger(__name__)

router: Router = Router(tags=["projects"])

DEMO_PROJECT_NAMES: dict[str, str] = {"paypulse": "PayPulse (Patient Zero)"}
NAME_MAX_LENGTH: int = Project._meta.get_field("name").max_length or 255
REPO_URL_MAX_LENGTH: int = Project._meta.get_field("repo_url").max_length or 2048
QUEUE_UNAVAILABLE_DETAIL: str = (
    "Ingestion could not be queued because the task queue (Redis) is unavailable. "
    "Start Redis and the Celery worker, then submit the repository again."
)


def telemetry_ws_path(project_id: UUID) -> str:
    """Path of the project's telemetry WebSocket (see ``apps.telemetry.routing``)."""
    return f"/ws/projects/{project_id}/telemetry/"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class IngestRequest(Schema):
    model_config = ConfigDict(extra="forbid")

    repo_url: str | None = Field(default=None, max_length=REPO_URL_MAX_LENGTH)
    demo: Literal["paypulse"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)

    @field_validator("repo_url")
    @classmethod
    def _validate_repo_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # UnsafeRepositorySourceError is a ValueError: pydantic turns it into a 422 with its message.
        return validate_remote_url(value.strip())

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped

    @model_validator(mode="after")
    def _exactly_one_source(self) -> IngestRequest:
        if (self.repo_url is None) == (self.demo is None):
            raise ValueError("provide exactly one of 'repo_url' or 'demo'")
        return self


class IngestAccepted(Schema):
    project_id: UUID
    name: str
    status: str
    is_golden_demo: bool
    telemetry_ws_path: str


class ErrorOut(Schema):
    detail: str


class QueueUnavailable(Schema):
    detail: str
    project_id: UUID
    status: str


class IngestionSummaryOut(Schema):
    branch_count: int
    abandoned_branch_count: int
    artifact_counts: dict[str, int]
    file_status_counts: dict[str, dict[str, int]]


class ProjectOut(Schema):
    id: UUID
    name: str
    repo_url: str
    is_golden_demo: bool
    status: str
    status_detail: str
    default_branch: str
    created_at: datetime
    updated_at: datetime
    ingested_at: datetime | None
    telemetry_ws_path: str
    ingestion: IngestionSummaryOut


class BranchOut(Schema):
    name: str
    head_sha: str
    last_commit_date: datetime
    is_default: bool
    is_abandoned: bool
    commits_ahead: int
    commits_behind: int
    diff_summary: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def default_project_name(payload: IngestRequest) -> str:
    if payload.name:
        return payload.name
    if payload.demo is not None:
        return DEMO_PROJECT_NAMES[payload.demo]
    segments = [segment for segment in urlsplit(payload.repo_url or "").path.split("/") if segment]
    name = segments[-1].removesuffix(".git") if segments else ""
    return (name or "repository")[:NAME_MAX_LENGTH]


def queue_ingestion(project_id: UUID) -> None:
    """Send ``ingest_repository_task``; raises whatever the broker client raises."""
    ingest_repository_task.delay(str(project_id))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post(
    "/ingest",
    response={202: IngestAccepted, 503: QueueUnavailable},
    summary="Submit a repository URL or the PayPulse demo for ingestion",
)
def ingest_project(request: HttpRequest, payload: IngestRequest) -> tuple[int, Any]:
    enqueue_failures: list[BaseException] = []

    def enqueue() -> None:
        try:
            queue_ingestion(project.pk)
        except Exception as exc:  # broker down, misconfigured, ...
            logger.exception("Could not queue ingestion for project %s.", project.pk)
            enqueue_failures.append(exc)

    with transaction.atomic():
        project = Project.objects.create(
            name=default_project_name(payload),
            repo_url=payload.repo_url or "",
            is_golden_demo=payload.demo is not None,
        )
        # Runs after COMMIT so the worker always finds the row. In autocommit
        # (the normal request path) that is when this block exits.
        transaction.on_commit(enqueue)

    if enqueue_failures:
        Project.objects.filter(pk=project.pk).update(
            status=ProjectStatus.FAILED, status_detail=QUEUE_UNAVAILABLE_DETAIL, updated_at=timezone.now()
        )
        return 503, QueueUnavailable(
            detail=QUEUE_UNAVAILABLE_DETAIL, project_id=project.pk, status=ProjectStatus.FAILED.value
        )
    return 202, IngestAccepted(
        project_id=project.pk,
        name=project.name,
        status=ProjectStatus(project.status).value,
        is_golden_demo=project.is_golden_demo,
        telemetry_ws_path=telemetry_ws_path(project.pk),
    )


def _ingestion_summary(project: Project) -> IngestionSummaryOut:
    branch_counts = BranchSnapshot.objects.filter(project=project).aggregate(
        total=Count("id"), abandoned=Count("id", filter=Q(is_abandoned=True))
    )
    # order_by() drops the model ordering, which would otherwise join the GROUP BY.
    by_type = dict(
        ArtifactRecord.objects.filter(project=project)
        .order_by()
        .values("artifact_type")
        .annotate(count=Count("id"))
        .values_list("artifact_type", "count")
    )
    stored = project.ingestion_summary or {}
    return IngestionSummaryOut(
        branch_count=branch_counts["total"] or 0,
        abandoned_branch_count=branch_counts["abandoned"] or 0,
        artifact_counts={kind: int(by_type.get(kind, 0)) for kind in ArtifactType.values},
        file_status_counts=stored.get("file_status_counts") or {},
    )


@router.get("/{uuid:project_id}", response={200: ProjectOut, 404: ErrorOut}, summary="Project status and summary")
def get_project(request: HttpRequest, project_id: UUID) -> ProjectOut:
    project = get_object_or_404(Project, pk=project_id)
    return ProjectOut(
        id=project.pk,
        name=project.name,
        repo_url=project.repo_url,
        is_golden_demo=project.is_golden_demo,
        status=ProjectStatus(project.status).value,
        status_detail=project.status_detail,
        default_branch=project.default_branch,
        created_at=project.created_at,
        updated_at=project.updated_at,
        ingested_at=project.ingested_at,
        telemetry_ws_path=telemetry_ws_path(project.pk),
        ingestion=_ingestion_summary(project),
    )


@router.get(
    "/{uuid:project_id}/branches",
    response={200: list[BranchOut], 404: ErrorOut},
    summary="Branch snapshots (default branch first)",
)
def list_branches(request: HttpRequest, project_id: UUID) -> list[BranchOut]:
    project = get_object_or_404(Project, pk=project_id)
    snapshots = sorted(
        BranchSnapshot.objects.filter(project=project),
        key=lambda snapshot: snapshot.name != project.default_branch,  # stable: model ordering otherwise
    )
    return [
        BranchOut(
            name=snapshot.name,
            head_sha=snapshot.head_sha,
            last_commit_date=snapshot.last_commit_date,
            is_default=snapshot.name == project.default_branch,
            is_abandoned=snapshot.is_abandoned,
            commits_ahead=snapshot.commits_ahead,
            commits_behind=snapshot.commits_behind,
            diff_summary=snapshot.diff_summary,
        )
        for snapshot in snapshots
    ]
