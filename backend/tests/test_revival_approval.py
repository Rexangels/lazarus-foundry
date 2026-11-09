"""Database tests for the strategy read API, the human approval gate and the spec downloads.

Every test here needs PostgreSQL (``django_db``) and is skipped by conftest.py when it is
unreachable. The strategies are the toy repository's (``tests/genome_helpers.py``), generated
deterministically (blank model key). Broadcasting is replaced by a recorder (no Redis).
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from apps.foundry.models import AgentStage, FoundryRun
from apps.genome.agents import GenomeReconcilerAgent
from apps.genome.services import load_catalog, persist_genome
from apps.ingestion.persistence import persist_ingestion
from apps.ingestion.pipeline import IngestionResult
from apps.projects.models import Project, ProjectStatus
from apps.revival import services as revival_services
from apps.revival.approval import strategy_approval_token
from apps.revival.models import RevivalStrategy, StrategyType
from apps.revival.services import GenerationRefusedError, generate_strategies, run_strategy_generation
from apps.telemetry.events import TelemetryEventType
from tests.genome_helpers import MAIN, toy_ingestion_rows

pytestmark = pytest.mark.django_db

Events = list[tuple[TelemetryEventType, dict[str, Any]]]
MAX_STRATEGY_QUERIES: int = 5


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Foundry runs sent to the broker (recorded instead of reaching Redis)."""
    sent: list[str] = []
    monkeypatch.setattr(revival_services, "queue_foundry_run", lambda run_id: sent.append(str(run_id)))
    return sent


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch, queued: list[str]) -> Events:
    recorded: Events = []

    def record(project_id: object, event: TelemetryEventType, payload: dict[str, Any]) -> None:
        recorded.append((event, payload))

    monkeypatch.setattr(revival_services, "broadcast_telemetry", record)
    return recorded


def project_with_strategies() -> Project:
    project = Project.objects.create(name="toy", status=ProjectStatus.AUTOPSY_COMPLETE)
    branches, artifacts = toy_ingestion_rows()
    persist_ingestion(project, IngestionResult(MAIN, tuple(branches), tuple(artifacts), {"branch_count": 2}))
    project.refresh_from_db()
    persist_genome(project, GenomeReconcilerAgent(api_key="").synthesize(load_catalog(project)))
    generate_strategies(project)
    project.refresh_from_db()
    return project


def rows(project: Project) -> dict[str, RevivalStrategy]:
    return {row.strategy_type: row for row in RevivalStrategy.objects.filter(project=project)}


def strategies_url(project_id: object) -> str:
    return f"/api/projects/{project_id}/strategies"


def approve(client: Client, row: RevivalStrategy, token: str | None = None, project_id: object = None,
            approver: str | None = None) -> Any:
    return client.post(
        f"/api/projects/{project_id or row.project_id}/strategies/{row.pk}/approve",
        {"approval_token": token or strategy_approval_token(row)},
        content_type="application/json",
        headers={"X-Lazarus-Approver": approver} if approver is not None else {},
    )


# ---------------------------------------------------------------------------
# GET /strategies
# ---------------------------------------------------------------------------
def test_strategies_are_returned_in_order_with_tokens_and_specs(client: Client) -> None:
    project = project_with_strategies()
    stored = rows(project)

    response = client.get(strategies_url(project.pk))

    assert response.status_code == 200
    body = response.json()
    assert [s["strategy_type"] for s in body["strategies"]] == ["RESTORE", "MODERNIZE", "EXTRACT"]
    for item in body["strategies"]:
        row = stored[item["strategy_type"]]
        assert item["feasibility_score"] == row.feasibility_score and item["risk_score"] == row.risk_score
        assert item["score_breakdown"] == row.score_breakdown
        assert item["action_plan"]["items"] == row.action_plan["items"]
        assert item["action_plan"]["deferred"] == row.action_plan["deferred"]
        assert item["spec"]["tasks_md"] == row.generated_kiro_specs["tasks_md"]
        assert item["spec"]["document_urls"]["design_md"].endswith(f"/strategies/{row.pk}/specs/design.md")
        assert item["approval"]["approval_token"] == strategy_approval_token(row)
        assert item["approval"]["approved_by_human"] is False and item["approval"]["approval_intact"] is None
    actionable = [row for row in stored.values() if row.is_actionable]
    best = max(actionable, key=lambda row: row.feasibility_score)
    assert body["recommended_strategy_id"] == str(best.pk) and body["approved_strategy_id"] is None
    assert body["synthesis"]["mode"] == "DETERMINISTIC" and body["project"]["status"] == "AUTOPSY_COMPLETE"
    assert "raw_model_response" not in response.content.decode() and "input_digest" not in body["synthesis"]


def test_strategies_query_count_is_bounded(client: Client, django_assert_max_num_queries: Any) -> None:
    project = project_with_strategies()

    with django_assert_max_num_queries(MAX_STRATEGY_QUERIES):
        response = client.get(strategies_url(project.pk))

    assert response.status_code == 200


def test_unknown_project_is_404(client: Client) -> None:
    assert client.get(strategies_url(uuid4())).status_code == 404


@pytest.mark.parametrize("status", [ProjectStatus.DORMANT, ProjectStatus.INGESTING, ProjectStatus.SYNTHESIZING_GENOME])
def test_in_progress_projects_get_strategies_pending(client: Client, status: str) -> None:
    project = Project.objects.create(name="busy", status=status)

    response = client.get(strategies_url(project.pk))

    assert response.status_code == 409
    assert (response.json()["code"], response.json()["status"]) == ("STRATEGIES_PENDING", status)


def test_failed_generation_gets_strategies_unavailable_with_the_detail(client: Client) -> None:
    project = Project.objects.create(
        name="toy", status=ProjectStatus.AUTOPSY_COMPLETE, status_detail="Revival strategies could not be generated."
    )

    response = client.get(strategies_url(project.pk))

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "STRATEGIES_UNAVAILABLE"
    assert body["status_detail"] == "Revival strategies could not be generated."
    assert body["telemetry_ws_path"] == f"/ws/projects/{project.pk}/telemetry/"


# ---------------------------------------------------------------------------
# POST /approve
# ---------------------------------------------------------------------------
def test_approval_locks_in_the_strategy_and_queues_the_foundry_after_commit(
    client: Client, events: Events, queued: list[str], settings: Any, django_capture_on_commit_callbacks: Any
) -> None:
    project = project_with_strategies()
    restore = rows(project)["RESTORE"]
    token = strategy_approval_token(restore)

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        response = approve(client, restore, token)
        assert events == [] and queued == []  # nothing is announced or queued before the commit

    assert response.status_code == 200
    body = response.json()
    restore.refresh_from_db()
    project.refresh_from_db()
    run = FoundryRun.objects.get(strategy=restore)
    assert restore.approved_by_human and restore.approved_at is not None and restore.approved_token == token
    assert restore.approved_by == ""
    assert project.status == ProjectStatus.STRATEGY_APPROVED
    assert (run.project_id, run.agent_stage, run.sandbox_mode) == (project.pk, AgentStage.ENVIRONMENT,
                                                                   settings.SANDBOX_MODE)
    assert body["foundry_run_id"] == str(run.pk) and body["already_approved"] is False
    assert body["project_status"] == "STRATEGY_APPROVED"
    assert body["strategy"]["approval"]["approval_intact"] is True
    assert body["telemetry_ws_path"] == f"/ws/projects/{project.pk}/telemetry/"

    for callback in callbacks:
        callback()
    [(event, payload)] = events
    assert event is TelemetryEventType.STRATEGY_APPROVED
    assert payload["strategy_id"] == str(restore.pk) and payload["strategy_type"] == "RESTORE"
    assert payload["foundry_run_id"] == str(run.pk)
    assert queued == [str(run.pk)]


def test_re_approving_the_same_strategy_is_idempotent(
    client: Client, events: Events, django_capture_on_commit_callbacks: Any
) -> None:
    project = project_with_strategies()
    restore = rows(project)["RESTORE"]
    with django_capture_on_commit_callbacks(execute=True):
        first = approve(client, restore).json()
    restore.refresh_from_db()
    events.clear()

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        response = approve(client, restore)

    assert response.status_code == 200
    body = response.json()
    assert body["already_approved"] is True and body["foundry_run_id"] == first["foundry_run_id"]
    assert body["strategy"]["approval"]["approved_at"] == first["strategy"]["approval"]["approved_at"]
    assert FoundryRun.objects.filter(strategy=restore).count() == 1
    assert callbacks == [] and events == []


def test_a_stale_token_is_409_with_the_current_token(client: Client) -> None:
    project = project_with_strategies()
    restore = rows(project)["RESTORE"]
    old_token = strategy_approval_token(restore)
    plan = dict(restore.action_plan, files=[*restore.action_plan["files"], "late/change.py"])
    RevivalStrategy.objects.filter(pk=restore.pk).update(action_plan=plan)
    restore.refresh_from_db()

    response = approve(client, restore, old_token)

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "STRATEGY_STALE" and body["current_approval_token"] == strategy_approval_token(restore)
    restore.refresh_from_db()
    assert not restore.approved_by_human and not FoundryRun.objects.exists()


def test_a_non_actionable_strategy_cannot_be_approved(client: Client) -> None:
    project = project_with_strategies()
    extract = rows(project)["EXTRACT"]
    RevivalStrategy.objects.filter(pk=extract.pk).update(is_actionable=False)

    response = approve(client, extract)

    assert response.status_code == 409 and response.json()["code"] == "STRATEGY_NOT_ACTIONABLE"


def test_only_one_strategy_per_project_can_be_approved(client: Client) -> None:
    project = project_with_strategies()
    stored = rows(project)
    assert approve(client, stored["RESTORE"]).status_code == 200

    response = approve(client, stored["MODERNIZE"])

    assert response.status_code == 409
    assert response.json()["code"] == "ANOTHER_STRATEGY_APPROVED"
    assert response.json()["approved_strategy_id"] == str(stored["RESTORE"].pk)
    # The database enforces the rule even when the service is bypassed.
    with pytest.raises(IntegrityError), transaction.atomic():
        RevivalStrategy.objects.filter(pk=stored["MODERNIZE"].pk).update(
            approved_by_human=True, approved_at=timezone.now()
        )


def test_a_project_that_is_not_autopsy_complete_cannot_approve(client: Client) -> None:
    project = project_with_strategies()
    Project.objects.filter(pk=project.pk).update(status=ProjectStatus.FAILED)

    response = approve(client, rows(project)["RESTORE"])

    assert response.status_code == 409 and response.json()["code"] == "PROJECT_NOT_READY"


def test_a_strategy_of_another_project_is_404(client: Client) -> None:
    project = project_with_strategies()
    other = Project.objects.create(name="other", status=ProjectStatus.AUTOPSY_COMPLETE)

    response = approve(client, rows(project)["RESTORE"], project_id=other.pk)

    assert response.status_code == 404
    assert approve(client, rows(project)["RESTORE"], project_id=uuid4()).status_code == 404
    assert not RevivalStrategy.objects.filter(project=project, approved_by_human=True).exists()


def test_the_approver_header_is_required_and_stored_when_configured(client: Client, settings: Any) -> None:
    settings.REVIVAL_APPROVAL_REQUIRES_HEADER = True
    project = project_with_strategies()
    restore = rows(project)["RESTORE"]

    assert approve(client, restore).status_code == 403
    response = approve(client, restore, approver="Ada Lovelace")

    assert response.status_code == 200 and response.json()["strategy"]["approval"]["approved_by"] == "Ada Lovelace"
    restore.refresh_from_db()
    assert restore.approved_by == "Ada Lovelace"


def test_strategy_generation_refuses_after_approval(client: Client) -> None:
    project = project_with_strategies()
    assert approve(client, rows(project)["RESTORE"]).status_code == 200
    before = {kind: (row.pk, row.updated_at) for kind, row in rows(project).items()}
    project.refresh_from_db()

    assert run_strategy_generation(project.pk)["status"] == "REFUSED"
    with pytest.raises(GenerationRefusedError):
        generate_strategies(project)

    assert {kind: (row.pk, row.updated_at) for kind, row in rows(project).items()} == before
    assert set(before) == set(StrategyType.values)


# ---------------------------------------------------------------------------
# GET /specs/{doc}
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("doc", ["requirements.md", "design.md", "tasks.md"])
def test_spec_documents_download_as_markdown(client: Client, doc: str) -> None:
    project = project_with_strategies()
    restore = rows(project)["RESTORE"]
    url = f"/api/projects/{project.pk}/strategies/{restore.pk}/specs/{doc}"

    response = client.get(url)

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/markdown")
    spec_name = restore.generated_kiro_specs["spec_name"]
    assert response["Content-Disposition"] == f'inline; filename="{spec_name}-{doc}"'
    assert response.content.decode() == restore.generated_kiro_specs[doc.replace(".md", "_md")]
    assert client.get(url + "?download=true")["Content-Disposition"].startswith("attachment;")
    assert client.get(f"/api/projects/{uuid4()}/strategies/{restore.pk}/specs/{doc}").status_code == 404
