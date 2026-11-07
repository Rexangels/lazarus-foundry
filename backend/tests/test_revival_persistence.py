"""Database tests for apps.revival.services and its hand-off from apps.genome.tasks.

Every test here needs PostgreSQL (``django_db``) and is skipped by conftest.py when it is
unreachable. Broadcasting is replaced by a recorder (no Redis), and the model key is blank
(conftest), so plans and specs are deterministic.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.utils import timezone

from apps.genome import tasks as genome_tasks
from apps.genome.agents import GenomeReconcilerAgent
from apps.genome.models import IntentNode
from apps.genome.services import load_catalog, persist_genome
from apps.ingestion.persistence import persist_ingestion
from apps.ingestion.pipeline import IngestionResult
from apps.projects.models import Project, ProjectStatus
from apps.revival import services as revival_services
from apps.revival.models import RevivalStrategy, StrategySynthesis, StrategyType
from apps.revival.services import INTERNAL_ERROR_DETAIL, StaleGenomeError, generate_strategies
from apps.telemetry.events import TelemetryEventType
from tests.genome_helpers import MAIN, toy_ingestion_rows

pytestmark = pytest.mark.django_db

Events = list[tuple[TelemetryEventType, dict[str, Any]]]
SPEC_KEYS = {"spec_name", "requirements_md", "design_md", "tasks_md", "evidence_index", "mode", "generated_at"}


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> Events:
    recorded: Events = []

    def record(project_id: object, event: TelemetryEventType, payload: dict[str, Any]) -> None:
        recorded.append((event, payload))

    monkeypatch.setattr(genome_tasks, "broadcast_telemetry", record)
    monkeypatch.setattr(revival_services, "broadcast_telemetry", record)
    return recorded


def ingested_project(status: str = ProjectStatus.SYNTHESIZING_GENOME) -> Project:
    project = Project.objects.create(name="toy", status=status)
    branches, artifacts = toy_ingestion_rows()
    persist_ingestion(project, IngestionResult(MAIN, tuple(branches), tuple(artifacts), {"branch_count": 2}))
    project.refresh_from_db()
    return project


def project_with_genome() -> Project:
    project = ingested_project(status=ProjectStatus.AUTOPSY_COMPLETE)
    persist_genome(project, GenomeReconcilerAgent(api_key="").synthesize(load_catalog(project)))
    return project


def test_generate_strategies_persists_three_rows_with_breakdowns_and_specs() -> None:
    project = project_with_genome()

    generated = generate_strategies(project)

    rows = {row.strategy_type: row for row in RevivalStrategy.objects.filter(project=project)}
    assert set(rows) == set(StrategyType.values) and all(g.regenerated for g in generated.strategies)
    for kind, row in rows.items():
        assert row.synthesis_id == generated.synthesis.pk and not row.approved_by_human
        assert row.score_breakdown["score"] == row.feasibility_score
        assert row.score_breakdown["value"] == row.value_score and row.score_breakdown["risk"] == row.risk_score
        assert set(row.action_plan) == {"items", "in_scope", "deferred", "files", "extract_root", "risk_evidence"}
        assert row.is_actionable == bool(row.action_plan["items"])
        assert set(row.generated_kiro_specs) == SPEC_KEYS
        assert row.generated_kiro_specs["spec_name"] == f"lazarus-{kind.lower()}-toy"
        assert row.generated_kiro_specs["mode"] == "DETERMINISTIC"
        assert row.generated_kiro_specs["tasks_md"].startswith("# Implementation Plan")
    synthesis = StrategySynthesis.objects.get(project=project)
    assert synthesis.mode == "DETERMINISTIC" and len(synthesis.input_digest) == 64
    assert synthesis.genome_synthesis_id is not None and synthesis.preserved_strategy_types == []


def test_rerun_replaces_unapproved_rows_and_keeps_an_approved_one() -> None:
    project = project_with_genome()
    first = {g.strategy.strategy_type: g.strategy for g in generate_strategies(project).strategies}
    RevivalStrategy.objects.filter(pk=first["RESTORE"].pk).update(approved_by_human=True, approved_at=timezone.now())
    approved = RevivalStrategy.objects.get(pk=first["RESTORE"].pk)

    second = generate_strategies(project)

    rows = {row.strategy_type: row for row in RevivalStrategy.objects.filter(project=project)}
    assert len(rows) == 3
    assert rows["RESTORE"].pk == approved.pk and rows["RESTORE"].updated_at == approved.updated_at
    assert rows["RESTORE"].synthesis_id == first["RESTORE"].synthesis_id  # untouched
    assert rows["MODERNIZE"].pk != first["MODERNIZE"].pk and rows["EXTRACT"].pk != first["EXTRACT"].pk
    assert second.synthesis.preserved_strategy_types == ["RESTORE"]
    assert [(g.strategy.strategy_type, g.regenerated) for g in second.strategies] == [
        ("RESTORE", False), ("MODERNIZE", True), ("EXTRACT", True)]
    assert StrategySynthesis.objects.filter(project=project).count() == 2  # provenance history is kept


def test_a_newer_genome_makes_the_run_stale_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    project = project_with_genome()
    monkeypatch.setattr(revival_services, "_latest_genome_synthesis_id", lambda project: uuid.uuid4())

    with pytest.raises(StaleGenomeError):
        generate_strategies(project)

    assert not RevivalStrategy.objects.filter(project=project).exists()
    assert not StrategySynthesis.objects.filter(project=project).exists()


def test_genome_task_hands_off_to_strategy_generation(events: Events) -> None:
    project = ingested_project()

    outcome = genome_tasks.run_genome_synthesis(project.pk)

    project.refresh_from_db()
    assert outcome["status"] == "AUTOPSY_COMPLETE" and outcome["strategies"] == "GENERATED"
    assert project.status == ProjectStatus.AUTOPSY_COMPLETE and project.status_detail == ""
    assert RevivalStrategy.objects.filter(project=project).count() == 3
    kinds = [event for event, _ in events]
    complete = kinds.index(TelemetryEventType.GENOME_COMPLETE)
    assert kinds[complete + 1:] == [TelemetryEventType.STRATEGY_GENERATED] * 3
    payloads = [payload for event, payload in events if event is TelemetryEventType.STRATEGY_GENERATED]
    assert [p["strategy_type"] for p in payloads] == ["RESTORE", "MODERNIZE", "EXTRACT"]
    for payload in payloads:
        row = RevivalStrategy.objects.get(pk=payload["strategy_id"])
        assert payload["feasibility_score"] == row.feasibility_score
        assert (payload["value_score"], payload["debt_cost_score"], payload["risk_score"]) == (
            row.value_score, row.debt_cost_score, row.risk_score)
        assert payload["in_scope_count"] == len(row.action_plan["in_scope"])
        assert payload["deferred_count"] == len(row.action_plan["deferred"])
        assert payload["is_actionable"] == row.is_actionable and payload["regenerated"] is True


def test_a_strategy_failure_keeps_the_genome(events: Events, monkeypatch: pytest.MonkeyPatch) -> None:
    project = ingested_project()

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(revival_services, "plan_project", broken)

    outcome = genome_tasks.run_genome_synthesis(project.pk)

    project.refresh_from_db()
    assert outcome["status"] == "AUTOPSY_COMPLETE" and outcome["strategies"] == "FAILED"
    assert project.status == ProjectStatus.AUTOPSY_COMPLETE  # the genome is still valid
    assert project.status_detail == INTERNAL_ERROR_DETAIL and "planner exploded" not in project.status_detail
    assert IntentNode.objects.filter(project=project).exists()
    assert not RevivalStrategy.objects.filter(project=project).exists()
    assert events[-1] == (TelemetryEventType.ERROR, {"stage": "REVIVAL", "detail": INTERNAL_ERROR_DETAIL})
    assert TelemetryEventType.GENOME_COMPLETE in [event for event, _ in events]
