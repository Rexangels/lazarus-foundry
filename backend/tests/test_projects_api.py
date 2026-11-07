"""Tests for the /api/projects/ endpoints.

Request validation happens before the view runs, so the 422 tests need no
database (pytest-django would fail them on any query). Tests that create or
read projects are marked ``django_db`` and are skipped by conftest.py when
PostgreSQL is unreachable.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.test import Client
from kombu.exceptions import OperationalError

from apps.ingestion.models import ArtifactRecord, ArtifactType, BranchSnapshot
from apps.ingestion.tasks import ingest_repository_task
from apps.projects.api import QUEUE_UNAVAILABLE_DETAIL
from apps.projects.models import Project, ProjectStatus

INGEST_URL: str = "/api/projects/ingest"


def post_ingest(client: Client, body: dict[str, Any]) -> Any:
    return client.post(INGEST_URL, data=body, content_type="application/json")


def error_messages(response: Any) -> str:
    return " | ".join(str(item.get("msg", "")) for item in response.json()["detail"])


# ---------------------------------------------------------------------------
# Validation (no database)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({}, "exactly one of 'repo_url' or 'demo'"),
        ({"repo_url": "https://github.com/acme/paypulse", "demo": "paypulse"}, "exactly one of"),
        ({"name": "only a name"}, "exactly one of"),
        ({"repo_url": "https://evil.example.com/acme/paypulse"}, "not in INGEST_ALLOWED_HOSTS"),
        ({"repo_url": "file:///C:/repos/paypulse"}, "only https://"),
        ({"repo_url": "C:\\repos\\paypulse"}, "backslashes"),
        ({"repo_url": "/srv/repos/paypulse"}, "only https://"),
        ({"repo_url": "git@github.com:acme/paypulse.git"}, "only https://"),
        ({"repo_url": "https://user:secret@github.com/acme/paypulse"}, "credentials"),
        ({"repo_url": "https://github.com/"}, "must name a repository path"),
        ({"demo": "another-demo"}, "paypulse"),
        ({"demo": "paypulse", "local_path": "C:/repos/paypulse"}, "Extra inputs are not permitted"),
        ({"demo": "paypulse", "name": "   "}, "name must not be blank"),
    ],
)
def test_invalid_ingest_requests_are_rejected_before_any_write(
    client: Client, body: dict[str, Any], reason: str
) -> None:
    response = post_ingest(client, body)

    assert response.status_code == 422
    assert reason in error_messages(response)


def test_project_routes_require_a_uuid(client: Client) -> None:
    assert client.get("/api/projects/not-a-uuid").status_code == 404


def test_ingest_endpoint_is_documented_in_openapi(client: Client) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]
    assert {"/api/projects/ingest", "/api/projects/{project_id}", "/api/projects/{project_id}/branches"} <= set(paths)
    assert {"202", "503"} <= set(paths["/api/projects/ingest"]["post"]["responses"])


# ---------------------------------------------------------------------------
# Creating and queueing (database)
# ---------------------------------------------------------------------------
@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records ``ingest_repository_task.delay`` calls instead of talking to Redis."""
    calls: list[str] = []
    monkeypatch.setattr(ingest_repository_task, "delay", lambda project_id: calls.append(project_id))
    return calls


@pytest.mark.django_db
def test_demo_ingest_creates_project_and_queues_after_commit(
    client: Client, queued: list[str], django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        response = post_ingest(client, {"demo": "paypulse"})
        assert queued == []  # nothing is sent before the transaction commits

    assert response.status_code == 202
    body = response.json()
    project = Project.objects.get(pk=body["project_id"])
    assert project.is_golden_demo and project.repo_url == ""
    assert project.status == ProjectStatus.DORMANT
    assert body == {
        "project_id": str(project.pk),
        "name": "PayPulse (Patient Zero)",
        "status": "DORMANT",
        "is_golden_demo": True,
        "telemetry_ws_path": f"/ws/projects/{project.pk}/telemetry/",
    }
    assert len(callbacks) == 1 and queued == [str(project.pk)]


@pytest.mark.django_db
def test_url_ingest_stores_the_validated_url_and_derives_a_name(
    client: Client, queued: list[str], django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        response = post_ingest(client, {"repo_url": "  https://github.com/acme/paypulse.git  "})

    assert response.status_code == 202
    project = Project.objects.get(pk=response.json()["project_id"])
    assert (project.repo_url, project.name, project.is_golden_demo) == (
        "https://github.com/acme/paypulse.git", "paypulse", False,
    )
    assert queued == [str(project.pk)]


@pytest.mark.django_db(transaction=True)
def test_queue_outage_marks_project_failed_and_returns_503(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broker_down(project_id: str) -> None:
        raise OperationalError("Error 10061 connecting to localhost:6379. Connection refused.")

    monkeypatch.setattr(ingest_repository_task, "delay", broker_down)

    response = post_ingest(client, {"demo": "paypulse", "name": "Patient Zero"})

    assert response.status_code == 503
    body = response.json()
    project = Project.objects.get(pk=body["project_id"])
    assert project.status == ProjectStatus.FAILED and project.status_detail == QUEUE_UNAVAILABLE_DETAIL
    assert body == {"detail": QUEUE_UNAVAILABLE_DETAIL, "project_id": str(project.pk), "status": "FAILED"}


# ---------------------------------------------------------------------------
# Reading (database)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_unknown_project_is_404(client: Client) -> None:
    assert client.get(f"/api/projects/{uuid4()}").status_code == 404
    assert client.get(f"/api/projects/{uuid4()}/branches").status_code == 404


@pytest.fixture
def ingested_project(db: None) -> Project:
    project = Project.objects.create(
        name="PayPulse",
        is_golden_demo=True,
        status=ProjectStatus.AUTOPSY_COMPLETE,
        default_branch="main",
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
        ingestion_summary={"file_status_counts": {"main": {"PARSED": 3, "UNSUPPORTED_LANGUAGE": 1}}},
    )
    BranchSnapshot.objects.create(
        project=project, name="feat/webhook-retry-engine", head_sha="b" * 40,
        last_commit_date=datetime(2021, 5, 1, tzinfo=UTC), is_abandoned=True,
        commits_ahead=4, commits_behind=5, diff_summary={"files_changed": 2},
    )
    BranchSnapshot.objects.create(
        project=project, name="main", head_sha="a" * 40, last_commit_date=datetime(2022, 11, 1, tzinfo=UTC),
        diff_summary={"is_default": True},
    )
    for artifact_type, path in (
        (ArtifactType.AST_MODULE, "paypulse/ledger.py"),
        (ArtifactType.AST_MODULE, "paypulse/retry.py"),
        (ArtifactType.TODO_MARKER, "paypulse/retry.py"),
    ):
        ArtifactRecord.objects.create(project=project, artifact_type=artifact_type, file_path=path, branch_name="main")
    return project


@pytest.mark.django_db
def test_project_detail_includes_status_and_ingestion_summary(client: Client, ingested_project: Project) -> None:
    response = client.get(f"/api/projects/{ingested_project.pk}")

    assert response.status_code == 200
    body = response.json()
    assert UUID(body["id"]) == ingested_project.pk
    assert (body["status"], body["status_detail"], body["default_branch"]) == ("AUTOPSY_COMPLETE", "", "main")
    assert body["ingested_at"].startswith("2026-01-01")
    assert body["telemetry_ws_path"] == f"/ws/projects/{ingested_project.pk}/telemetry/"
    assert body["ingestion"] == {
        "branch_count": 2,
        "abandoned_branch_count": 1,
        "artifact_counts": {
            "AST_MODULE": 2, "README_DOC": 0, "DEPENDENCY_MANIFEST": 0, "TODO_MARKER": 1, "COMMIT_LOG": 0,
        },
        "file_status_counts": {"main": {"PARSED": 3, "UNSUPPORTED_LANGUAGE": 1}},
    }


@pytest.mark.django_db
def test_branches_list_puts_the_default_branch_first(client: Client, ingested_project: Project) -> None:
    response = client.get(f"/api/projects/{ingested_project.pk}/branches")

    assert response.status_code == 200
    body = response.json()
    assert [branch["name"] for branch in body] == ["main", "feat/webhook-retry-engine"]
    assert body[0]["is_default"] is True and body[1]["is_default"] is False
    assert (body[1]["is_abandoned"], body[1]["commits_ahead"], body[1]["commits_behind"]) == (True, 4, 5)
    assert body[1]["diff_summary"] == {"files_changed": 2}
