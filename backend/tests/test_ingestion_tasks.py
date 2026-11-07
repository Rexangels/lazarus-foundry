"""Tests for apps.ingestion.tasks and apps.ingestion.persistence.

The helpers at the top need neither PostgreSQL nor Redis. Tests marked
``django_db`` are skipped by conftest.py when PostgreSQL is unreachable; the
end-to-end task test is also ``slow`` (it clones and parses PayPulse).
Broadcasting is replaced by a recorder so no Redis is needed.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from pytest_django import Settings as SettingsWrapper

from apps.genome import tasks as genome_tasks
from apps.ingestion import tasks
from apps.ingestion.git_forensics import CloneError, GitForensicsError, UnsafeRepositorySourceError, UnsafeWorkspaceError
from apps.ingestion.models import ArtifactRecord, ArtifactType, BranchSnapshot
from apps.ingestion.persistence import persist_ingestion
from apps.ingestion.pipeline import ArtifactRow, BranchRow, IngestionResult
from apps.projects.git_runner import GitTimeoutError
from apps.projects.models import Project, ProjectStatus
from apps.telemetry.events import TelemetryEventType
from tests.fixtures.build_paypulse_fixture import (
    PAYPULSE_FIXTURE_NAME,
    RETRY_BRANCH_NAME,
    RETRY_ENGINE_TODO_MARKER,
    FixtureBuildResult,
)

Events = list[tuple[TelemetryEventType, dict[str, Any]]]


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> Events:
    recorded: Events = []
    monkeypatch.setattr(
        tasks, "broadcast_telemetry", lambda project_id, event, payload: recorded.append((event, payload))
    )
    return recorded


@pytest.fixture
def workspace_root(settings: SettingsWrapper, tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    settings.WORKSPACE_ROOT = root
    return root


# ---------------------------------------------------------------------------
# Helpers (no database)
# ---------------------------------------------------------------------------
def test_task_is_registered_with_late_ack_and_time_limits() -> None:
    task = tasks.ingest_repository_task
    assert task.name == "ingestion.ingest_repository"
    assert task.acks_late is True
    assert task.soft_time_limit == settings.INGEST_TASK_SOFT_TIME_LIMIT_SECONDS
    assert task.time_limit == settings.INGEST_TASK_TIME_LIMIT_SECONDS > task.soft_time_limit


def _caused(exc: BaseException, cause: BaseException) -> BaseException:
    exc.__cause__ = cause
    return exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (UnsafeRepositorySourceError("only https:// repository URLs are accepted"), "rejected: only https://"),
        (CloneError("could not clone into C:\\secret\\ws: fatal: repository not found"), "could not be cloned"),
        (_caused(CloneError("x"), GitTimeoutError("timed out")), "timed out after"),
        (UnsafeWorkspaceError("workspace C:\\secret is not empty"), "workspace could not be prepared"),
        (GitForensicsError("`git rev-list` failed: C:\\secret"), "could not be analyzed"),
        (SoftTimeLimitExceeded(), "time limit"),
        (tasks.IngestionError("The PayPulse demo repository could not be prepared."), "PayPulse demo"),
        (RuntimeError("boom at C:\\secret"), "internal error"),
    ],
)
def test_user_safe_error_never_leaks_paths_or_git_output(exc: BaseException, expected: str) -> None:
    detail = tasks.user_safe_error(exc)
    assert expected in detail
    assert "secret" not in detail and "fatal" not in detail


def test_prepare_workspace_replaces_a_previous_workspace(workspace_root: Path) -> None:
    project = Project(id=uuid4(), name="p")
    old = workspace_root / str(project.pk)
    (old / ".git").mkdir(parents=True)
    (old / "stale.txt").write_text("old")

    prepared = tasks.prepare_workspace(project)

    assert prepared == old.resolve() and not prepared.exists()
    assert workspace_root.exists()


def test_prepare_workspace_refuses_a_redirecting_link(workspace_root: Path, tmp_path: Path) -> None:
    project = Project(id=uuid4(), name="p")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    try:
        os.symlink(victim, workspace_root / str(project.pk), target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or admin rights on Windows")

    with pytest.raises(UnsafeWorkspaceError):
        tasks.prepare_workspace(project)
    assert (victim / "keep.txt").read_text() == "keep"


def test_existing_demo_fixture_is_reused_not_rebuilt(settings: SettingsWrapper, tmp_path: Path) -> None:
    settings.FIXTURE_ROOT = tmp_path
    (tmp_path / PAYPULSE_FIXTURE_NAME / ".git").mkdir(parents=True)

    assert tasks.ensure_demo_fixture() == tmp_path / PAYPULSE_FIXTURE_NAME


@pytest.mark.slow
def test_missing_demo_fixture_is_built(settings: SettingsWrapper, tmp_path: Path) -> None:
    settings.FIXTURE_ROOT = tmp_path / "fixtures"

    path = tasks.ensure_demo_fixture()

    assert (path / ".git").is_dir() and (path / "paypulse" / "ledger.py").is_file()
    assert tasks.ensure_demo_fixture() == path  # second call reuses it


def test_telemetry_emitter_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object) -> None:
        raise ValueError("bad payload")

    monkeypatch.setattr(tasks, "broadcast_telemetry", broken)
    tasks.telemetry_emitter(uuid4())(TelemetryEventType.ERROR, {"detail": "x"})


# ---------------------------------------------------------------------------
# Persistence (database)
# ---------------------------------------------------------------------------
def synthetic_result(default_branch: str = "trunk", artifact_count: int = 3) -> IngestionResult:
    when = datetime(2022, 11, 1, tzinfo=UTC)
    branches = (
        BranchRow(default_branch, "a" * 40, when, False, 0, 0, {"is_default": True}),
        BranchRow("feat/x", "b" * 40, when, True, 2, 7, {"is_default": False, "files_changed": 1}),
    )
    artifacts = tuple(
        ArtifactRow(ArtifactType.AST_MODULE, f"pkg/m{index}.py", default_branch, "x = 1\n", {"symbols": []})
        for index in range(artifact_count)
    )
    return IngestionResult(default_branch, branches, artifacts, {"branch_count": 2})


@pytest.mark.django_db
def test_persist_writes_rows_and_reingest_replaces_them() -> None:
    project = Project.objects.create(name="p")

    persist_ingestion(project, synthetic_result(artifact_count=3))
    first_ids = set(ArtifactRecord.objects.filter(project=project).values_list("id", flat=True))
    counts = persist_ingestion(project, synthetic_result(artifact_count=2))

    project.refresh_from_db()
    assert (counts.branches, counts.artifacts) == (2, 2)
    assert BranchSnapshot.objects.filter(project=project).count() == 2
    assert ArtifactRecord.objects.filter(project=project).count() == 2
    assert first_ids.isdisjoint(ArtifactRecord.objects.filter(project=project).values_list("id", flat=True))
    assert project.default_branch == "trunk" and project.ingestion_summary == {"branch_count": 2}
    assert project.ingested_at is not None
    feat = BranchSnapshot.objects.get(project=project, name="feat/x")
    assert (feat.is_abandoned, feat.commits_ahead, feat.commits_behind) == (True, 2, 7)
    assert ArtifactRecord.objects.filter(project=project, embedding__isnull=False).count() == 0


@pytest.mark.django_db
def test_persist_does_not_touch_other_projects() -> None:
    mine, other = Project.objects.create(name="mine"), Project.objects.create(name="other")
    persist_ingestion(other, synthetic_result())

    persist_ingestion(mine, synthetic_result(artifact_count=1))

    assert ArtifactRecord.objects.filter(project=other).count() == 3


# ---------------------------------------------------------------------------
# Claiming and status transitions (database)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("status", [ProjectStatus.DORMANT, ProjectStatus.FAILED, ProjectStatus.AUTOPSY_COMPLETE])
def test_claim_moves_claimable_projects_to_ingesting(status: str) -> None:
    project = Project.objects.create(name="p", status=status, status_detail="old failure")

    claimed = tasks.claim_project_for_ingestion(project.pk)

    project.refresh_from_db()
    assert claimed is not None and claimed.pk == project.pk
    assert (project.status, project.status_detail) == (ProjectStatus.INGESTING, "")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [ProjectStatus.INGESTING, ProjectStatus.SYNTHESIZING_GENOME, ProjectStatus.FOUNDRY_RUNNING, ProjectStatus.REBORN],
)
def test_claim_refuses_projects_already_ingesting_or_further_along(status: str) -> None:
    project = Project.objects.create(name="p", status=status)

    assert tasks.claim_project_for_ingestion(project.pk) is None
    project.refresh_from_db()
    assert project.status == status


@pytest.mark.django_db
def test_task_refuses_a_concurrent_run_without_side_effects(events: Events, workspace_root: Path) -> None:
    project = Project.objects.create(name="p", status=ProjectStatus.INGESTING, is_golden_demo=True)

    assert tasks.ingest_repository_task(str(project.pk)) == {"project_id": str(project.pk), "status": "REFUSED"}
    project.refresh_from_db()
    assert project.status == ProjectStatus.INGESTING and events == []
    assert not any(workspace_root.iterdir())


@pytest.mark.django_db
def test_task_for_unknown_project_does_nothing(events: Events) -> None:
    missing = str(uuid4())
    assert tasks.ingest_repository_task(missing) == {"project_id": missing, "status": "NOT_FOUND"}
    assert events == []


@pytest.mark.django_db
def test_rejected_url_fails_the_project_with_a_safe_detail(events: Events, workspace_root: Path) -> None:
    # Only the API validates URLs up front; the task must re-check rows created any other way.
    project = Project.objects.create(name="evil", repo_url="https://evil.example.com/acme/paypulse")

    result = tasks.ingest_repository_task(str(project.pk))

    project.refresh_from_db()
    assert project.status == ProjectStatus.FAILED and result["status"] == "FAILED"
    assert "not in INGEST_ALLOWED_HOSTS" in project.status_detail
    assert [event for event, _ in events] == [TelemetryEventType.INGESTION_STARTED, TelemetryEventType.ERROR]
    assert events[-1][1] == {"stage": "INGESTION", "detail": project.status_detail}
    assert project.ingested_at is None


@pytest.mark.django_db
def test_on_ingestion_complete_hands_off_to_genome_synthesis(
    monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: Any
) -> None:
    project = Project.objects.create(name="p", status=ProjectStatus.INGESTING)
    queued: list[str] = []
    monkeypatch.setattr(genome_tasks.synthesize_genome_task, "delay", queued.append)
    announced: list[str] = []

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        status = tasks.on_ingestion_complete(project.pk, announce=announced.append)
        project.refresh_from_db()
        assert project.status == ProjectStatus.SYNTHESIZING_GENOME and queued == []  # only after commit

    assert status == announced[0] == ProjectStatus.SYNTHESIZING_GENOME
    assert len(callbacks) == 1 and queued == [str(project.pk)]


@pytest.mark.django_db
@pytest.mark.slow
def test_demo_ingestion_end_to_end_and_reingest(
    events: Events,
    workspace_root: Path,
    settings: SettingsWrapper,
    paypulse_repo: FixtureBuildResult,
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Any,
) -> None:
    settings.FIXTURE_ROOT = paypulse_repo.repo_path.parent  # reuse the session fixture, never rebuild it
    monkeypatch.setattr(genome_tasks, "broadcast_telemetry", tasks.broadcast_telemetry)  # the recorder
    # Run the queued genome task synchronously, as a worker would.
    monkeypatch.setattr(genome_tasks.synthesize_genome_task, "delay", genome_tasks.run_genome_synthesis)
    project = Project.objects.create(name="PayPulse", is_golden_demo=True)

    with django_capture_on_commit_callbacks(execute=True):
        result = tasks.ingest_repository_task(str(project.pk))

    project.refresh_from_db()
    assert result == {"project_id": str(project.pk), "status": "SYNTHESIZING_GENOME"}
    assert project.status == ProjectStatus.AUTOPSY_COMPLETE and project.status_detail == ""
    assert project.ingested_at is not None and project.default_branch == "main"
    assert Path(project.workspace_path) == (workspace_root / str(project.pk)).resolve()
    retry = BranchSnapshot.objects.get(project=project, name=RETRY_BRANCH_NAME)
    assert (retry.is_abandoned, retry.commits_ahead, retry.commits_behind) == (True, 4, 5)
    todo = ArtifactRecord.objects.get(
        project=project, artifact_type=ArtifactType.TODO_MARKER, branch_name=RETRY_BRANCH_NAME
    )
    assert todo.structured_metadata["snippet"].strip() == RETRY_ENGINE_TODO_MARKER
    kinds = [event for event, _ in events]
    assert kinds[0] is TelemetryEventType.INGESTION_STARTED and kinds[-1] is TelemetryEventType.GENOME_COMPLETE
    assert TelemetryEventType.ERROR not in kinds
    complete = next(payload for event, payload in events if event is TelemetryEventType.INGESTION_COMPLETE)
    assert complete["status"] == ProjectStatus.SYNTHESIZING_GENOME

    counts = (BranchSnapshot.objects.filter(project=project).count(), ArtifactRecord.objects.filter(project=project).count())
    with django_capture_on_commit_callbacks(execute=True):
        assert tasks.ingest_repository_task(str(project.pk))["status"] == "SYNTHESIZING_GENOME"
    project.refresh_from_db()
    assert project.status == ProjectStatus.AUTOPSY_COMPLETE
    assert counts == (
        BranchSnapshot.objects.filter(project=project).count(),
        ArtifactRecord.objects.filter(project=project).count(),
    )
