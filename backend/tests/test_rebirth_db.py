"""Database tests for ``apps.rebirth.services.run_rebirth`` and ``GET /api/projects/{project_id}/rebirth``.

Every test here needs PostgreSQL (``django_db``) and is skipped by conftest.py when it is
unreachable. The workspace is the small Git repository of ``tests/test_rebirth_builder.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from apps.foundry.models import AgentStage, FoundryRun
from apps.foundry.services import on_foundry_complete
from apps.projects.models import Project, ProjectStatus
from apps.rebirth.models import RebirthArtifact
from apps.rebirth.services import run_rebirth
from apps.revival.approval import strategy_approval_token
from apps.revival.models import RevivalStrategy
from apps.telemetry.events import TelemetryEventType
from tests.test_rebirth_builder import SPEC_DIR, build_workspace, inputs_for

pytestmark = pytest.mark.django_db

Events = list[tuple[TelemetryEventType, dict[str, Any]]]


def finished_run(workspace: Path, base: str, *, status: str = ProjectStatus.FOUNDRY_RUNNING) -> FoundryRun:
    toy = inputs_for(base)
    project = Project.objects.create(name="toy", status=status, workspace_path=str(workspace), is_golden_demo=True)
    strategy = RevivalStrategy.objects.create(
        project=project, strategy_type="RESTORE", title=toy.strategy_title, summary=toy.strategy_summary,
        feasibility_score=68.1, value_score=0.68, debt_cost_score=0.33, risk_score=0.3,
        action_plan=dict(toy.action_plan), is_actionable=True, generated_kiro_specs=dict(toy.kiro_specs),
    )
    strategy.approved_by_human, strategy.approved_at, strategy.approved_by = True, timezone.now(), "dana"
    strategy.approved_token = strategy_approval_token(strategy)
    strategy.save()
    return FoundryRun.objects.create(
        project=project, strategy=strategy, agent_stage=AgentStage.COMPLETE, sandbox_mode="subprocess",
        before_test_output="1 failed, 15 passed", after_test_output="39 passed", tests_passed=True,
        report=dict(toy.report),
    )


@pytest.fixture
def toy(tmp_path: Path, settings: Any) -> tuple[Path, str]:
    settings.WORKSPACE_ROOT = tmp_path
    return build_workspace(tmp_path)


def test_a_verified_run_gets_its_artifact_and_the_project_is_reborn(toy: tuple[Path, str], client: Client) -> None:
    root, base = toy
    run = finished_run(root, base)
    events: Events = []

    on_foundry_complete(run, emit=lambda event, payload: events.append((event, payload)))

    artifact = RebirthArtifact.objects.get(project=run.project)
    assert artifact.foundry_run_id == run.pk and artifact.exported_kiro_specs_path == SPEC_DIR
    assert "+++ b/retry.py" in artifact.unified_diff_patch and artifact.pr_title.startswith("Lazarus RESTORE:")
    assert artifact.continuity_memory["verification"]["after"]["passed"] == 39
    assert Project.objects.get(pk=run.project_id).status == ProjectStatus.REBORN
    [(event, payload)] = events
    assert event is TelemetryEventType.REBIRTH_COMPLETE and payload["rebirth_artifact_id"] == str(artifact.pk)

    response = client.get(f"/api/projects/{run.project_id}/rebirth")
    assert response.status_code == 200
    body = response.json()
    assert body["unified_diff_patch"] == artifact.unified_diff_patch and body["tests_passed"] is True
    assert body["spec_files"] == [f"{SPEC_DIR}/requirements.md", f"{SPEC_DIR}/design.md", f"{SPEC_DIR}/tasks.md"]
    assert {f["path"] for f in body["files"]} >= {"app.py", "merged.py", "retry.py"}
    assert body["before_test_output"] == "1 failed, 15 passed" and body["project"]["status"] == "REBORN"
    assert str(root) not in response.content.decode()


def test_a_broken_approval_fails_the_project_without_an_artifact(toy: tuple[Path, str]) -> None:
    root, base = toy
    run = finished_run(root, base)
    RevivalStrategy.objects.filter(pk=run.strategy_id).update(title="edited after approval")
    events: Events = []

    result = run_rebirth(run, emit=lambda event, payload: events.append((event, payload)))

    assert result["status"] == "FAILED" and not RebirthArtifact.objects.exists()
    project = Project.objects.get(pk=run.project_id)
    assert project.status == ProjectStatus.FAILED and "APPROVAL_NOT_INTACT" in project.status_detail
    assert [e for e, _ in events] == [TelemetryEventType.ERROR] and events[0][1]["stage"] == "REBIRTH"
    assert not (root / ".kiro").exists()


def test_a_run_that_did_not_pass_is_skipped(toy: tuple[Path, str]) -> None:
    root, base = toy
    run = finished_run(root, base)
    FoundryRun.objects.filter(pk=run.pk).update(agent_stage=AgentStage.FAILED, tests_passed=False)
    assert run_rebirth(run, emit=lambda e, p: None)["status"] == "RUN_NOT_VERIFIED"
    assert Project.objects.get(pk=run.project_id).status == ProjectStatus.FOUNDRY_RUNNING


@pytest.mark.parametrize(
    ("status", "code"),
    [(ProjectStatus.FOUNDRY_RUNNING, "REBIRTH_PENDING"), (ProjectStatus.AUTOPSY_COMPLETE, "REBIRTH_PENDING"),
     (ProjectStatus.FAILED, "REBIRTH_UNAVAILABLE")],
)
def test_without_an_artifact_the_endpoint_answers_409(client: Client, status: str, code: str) -> None:
    project = Project.objects.create(name="toy", status=status, status_detail="why")
    response = client.get(f"/api/projects/{project.pk}/rebirth")
    assert response.status_code == 409
    assert response.json()["code"] == code and response.json()["status_detail"] == "why"


def test_an_unknown_project_is_404(client: Client) -> None:
    assert client.get(f"/api/projects/{uuid4()}/rebirth").status_code == 404
