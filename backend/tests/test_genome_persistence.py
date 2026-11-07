"""Database tests for apps.genome.services and apps.genome.tasks.

Every test here needs PostgreSQL (``django_db``) and is skipped by conftest.py
when it is unreachable. Broadcasting is replaced by a recorder (no Redis), and
the model key is blank (conftest), so synthesis is deterministic.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pytest_django import Settings as SettingsWrapper

from apps.genome import tasks as genome_tasks
from apps.genome.agents import GenomeReconcilerAgent
from apps.genome.models import ConflictRecord, EvidenceCitation, GenomeSynthesis, IntentNode, SynthesisMode
from apps.genome.scoring import UncitedIntentNodeError
from apps.genome.services import load_catalog, persist_genome
from apps.ingestion import tasks as ingestion_tasks
from apps.ingestion.models import ArtifactRecord
from apps.ingestion.persistence import persist_ingestion
from apps.ingestion.pipeline import IngestionResult
from apps.projects.models import Project, ProjectStatus
from apps.telemetry.events import TelemetryEventType
from tests.fixtures.build_paypulse_fixture import FixtureBuildResult
from tests.genome_helpers import MAIN, toy_ingestion_rows

pytestmark = pytest.mark.django_db

Events = list[tuple[TelemetryEventType, dict[str, Any]]]


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> Events:
    recorded: Events = []

    def record(project_id: object, event: TelemetryEventType, payload: dict[str, Any]) -> None:
        recorded.append((event, payload))

    monkeypatch.setattr(genome_tasks, "broadcast_telemetry", record)
    monkeypatch.setattr(ingestion_tasks, "broadcast_telemetry", record)
    return recorded


def ingested_project(status: str = ProjectStatus.SYNTHESIZING_GENOME) -> Project:
    project = Project.objects.create(name="toy", status=status)
    branches, artifacts = toy_ingestion_rows()
    persist_ingestion(project, IngestionResult(MAIN, tuple(branches), tuple(artifacts), {"branch_count": 2}))
    return project


def test_persist_genome_writes_rows_and_replaces_them_on_rerun() -> None:
    project = ingested_project()
    result = GenomeReconcilerAgent(api_key="").synthesize(load_catalog(project))

    first = persist_genome(project, result)
    first_ids = set(IntentNode.objects.filter(project=project).values_list("id", flat=True))
    second = persist_genome(project, result)

    nodes = IntentNode.objects.filter(project=project)
    assert nodes.count() == len(result.features) == second.synthesis.node_count
    assert first_ids.isdisjoint(nodes.values_list("id", flat=True))
    assert ConflictRecord.objects.filter(project=project).count() == result.conflict_count == 3
    assert all(node.citations.exists() for node in nodes)
    assert set(nodes.values_list("synthesis_id", flat=True)) == {second.synthesis.pk}
    assert GenomeSynthesis.objects.filter(project=project).count() == 2  # provenance history is kept
    assert first.synthesis.intent_nodes.count() == 0

    citation = EvidenceCitation.objects.filter(intent_node__project=project, line_start__isnull=False).first()
    assert citation is not None and citation.artifact is not None
    assert citation.artifact.project_id == project.pk and citation.snippet
    node = nodes.get(title="PDF export")
    assert node.score_breakdown["vital_status"] == "GHOST_FEATURE"
    assert node.score_breakdown["confidence"] == node.confidence_score


def test_a_node_without_citations_raises_and_writes_nothing() -> None:
    project = ingested_project()
    result = GenomeReconcilerAgent(api_key="").synthesize(load_catalog(project))
    broken = replace(result, features=(replace(result.features[0], citations=()), *result.features[1:]))

    with pytest.raises(UncitedIntentNodeError):
        persist_genome(project, broken)

    assert not IntentNode.objects.filter(project=project).exists()
    assert not GenomeSynthesis.objects.filter(project=project).exists()


def test_synthesis_task_records_provenance_and_completes(events: Events) -> None:
    project = ingested_project()

    outcome = genome_tasks.run_genome_synthesis(project.pk)

    project.refresh_from_db()
    assert outcome["status"] == "AUTOPSY_COMPLETE" and project.status == ProjectStatus.AUTOPSY_COMPLETE
    synthesis = GenomeSynthesis.objects.get(project=project)
    assert synthesis.mode == SynthesisMode.DETERMINISTIC and synthesis.model_id == ""
    assert len(synthesis.input_digest) == 64 and synthesis.catalog_size > 0
    assert synthesis.node_count == IntentNode.objects.filter(project=project).count()
    kinds = [event for event, _ in events]
    assert kinds.count(TelemetryEventType.GENOME_NODE_CREATED) == synthesis.node_count
    assert kinds.count(TelemetryEventType.CONFLICT_DETECTED) == synthesis.conflict_count
    complete = kinds.index(TelemetryEventType.GENOME_COMPLETE)
    assert events[complete][1]["mode"] == "DETERMINISTIC" and events[complete][1]["status"] == "AUTOPSY_COMPLETE"
    # on_genome_complete hands off to strategy generation (tests/test_revival_persistence.py).
    assert kinds[complete + 1:] == [TelemetryEventType.STRATEGY_GENERATED] * 3


def test_synthesis_failure_marks_the_project_failed(events: Events) -> None:
    project = Project.objects.create(name="empty", status=ProjectStatus.SYNTHESIZING_GENOME)

    outcome = genome_tasks.run_genome_synthesis(project.pk)

    project.refresh_from_db()
    assert outcome["status"] == "FAILED" and project.status == ProjectStatus.FAILED
    assert "no ingested branches" in project.status_detail
    assert events == [(TelemetryEventType.ERROR, {"stage": "GENOME", "detail": project.status_detail})]


@pytest.mark.parametrize("status", [ProjectStatus.DORMANT, ProjectStatus.INGESTING, ProjectStatus.FAILED])
def test_synthesis_refuses_projects_in_other_stages(status: str, events: Events) -> None:
    project = Project.objects.create(name="p", status=status)

    assert genome_tasks.run_genome_synthesis(project.pk)["status"] == "REFUSED"
    project.refresh_from_db()
    assert project.status == status and events == []


def test_on_ingestion_complete_runs_synthesis_inline_when_the_broker_is_down(
    events: Events, monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: Any
) -> None:
    project = ingested_project(status=ProjectStatus.INGESTING)

    def broker_down(*args: object) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(genome_tasks.synthesize_genome_task, "delay", broker_down)
    announced: list[str] = []

    # The test runs inside a transaction; execute the on_commit hand-off as a real commit would.
    with django_capture_on_commit_callbacks(execute=True):
        status = ingestion_tasks.on_ingestion_complete(project.pk, announce=announced.append)

    project.refresh_from_db()
    assert status == announced[0] == ProjectStatus.SYNTHESIZING_GENOME
    assert project.status == ProjectStatus.AUTOPSY_COMPLETE
    assert IntentNode.objects.filter(project=project).exists()


@pytest.mark.slow
def test_demo_status_transitions_end_to_end(
    events: Events,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
    tmp_path: Path,
    paypulse_repo: FixtureBuildResult,
    django_capture_on_commit_callbacks: Any,
) -> None:
    settings.WORKSPACE_ROOT = tmp_path / "workspaces"
    settings.WORKSPACE_ROOT.mkdir()
    settings.FIXTURE_ROOT = paypulse_repo.repo_path.parent
    seen: list[str] = []

    def worker(project_id: str) -> None:  # what the Celery worker would do with the queued task
        seen.append(Project.objects.get(pk=project_id).status)
        genome_tasks.run_genome_synthesis(project_id)

    monkeypatch.setattr(genome_tasks.synthesize_genome_task, "delay", worker)
    project = Project.objects.create(name="PayPulse", is_golden_demo=True)

    with django_capture_on_commit_callbacks(execute=True):
        result = ingestion_tasks.ingest_repository_task(str(project.pk))

    project.refresh_from_db()
    assert result["status"] == "SYNTHESIZING_GENOME" and seen == ["SYNTHESIZING_GENOME"]
    assert project.status == ProjectStatus.AUTOPSY_COMPLETE
    kinds = [event for event, _ in events]
    complete = kinds.index(TelemetryEventType.INGESTION_COMPLETE)
    assert events[complete][1]["status"] == "SYNTHESIZING_GENOME"
    assert complete < kinds.index(TelemetryEventType.GENOME_NODE_CREATED)
    genome_complete = kinds.index(TelemetryEventType.GENOME_COMPLETE)
    assert kinds[genome_complete + 1:] == [TelemetryEventType.STRATEGY_GENERATED] * 3
    assert project.status_detail == ""
    nodes = IntentNode.objects.filter(project=project)
    assert nodes.count() == 6
    assert nodes.filter(vital_status="GHOST_FEATURE", title="PDF Invoice Export").exists()
    assert ConflictRecord.objects.filter(project=project).count() == 3
    assert not EvidenceCitation.objects.filter(intent_node__project=project, artifact__isnull=True, line_start__isnull=False).exists()
    assert ArtifactRecord.objects.filter(project=project).exists()
