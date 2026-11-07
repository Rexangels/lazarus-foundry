"""Database tests for GET /api/projects/{project_id}/genome.

Every test here needs PostgreSQL (``django_db``) and is skipped by conftest.py
when it is unreachable. The genome is the toy repository's
(``tests/genome_helpers.py``), synthesized deterministically (blank model key).
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from django.test import Client

from apps.genome import tasks as genome_tasks
from apps.genome.models import GenomeSynthesis, IntentNode
from apps.ingestion.persistence import persist_ingestion
from apps.ingestion.pipeline import IngestionResult
from apps.projects.models import Project, ProjectStatus
from tests.genome_helpers import BRANCH, MAIN, toy_ingestion_rows

pytestmark = pytest.mark.django_db

MAX_GENOME_QUERIES: int = 8


def genome_url(project_id: object) -> str:
    return f"/api/projects/{project_id}/genome"


@pytest.fixture(autouse=True)
def _no_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(genome_tasks, "broadcast_telemetry", lambda *args: None)


def synthesized_project() -> Project:
    project = Project.objects.create(name="toy", status=ProjectStatus.SYNTHESIZING_GENOME)
    branches, artifacts = toy_ingestion_rows()
    persist_ingestion(project, IngestionResult(MAIN, tuple(branches), tuple(artifacts), {"branch_count": 2}))
    assert genome_tasks.run_genome_synthesis(project.pk)["status"] == "AUTOPSY_COMPLETE"
    project.refresh_from_db()
    return project


def test_genome_returns_the_persisted_graph(client: Client) -> None:
    project = synthesized_project()

    response = client.get(genome_url(project.pk))

    assert response.status_code == 200
    body = response.json()
    nodes = IntentNode.objects.filter(project=project)
    features = [node for node in body["nodes"] if node["type"] == "feature"]
    assert {node["id"] for node in features} == {str(pk) for pk in nodes.values_list("id", flat=True)}
    assert set(body["citations"]) == set(body["score_breakdowns"]) == {node["id"] for node in features}
    assert len(body["conflicts"]) == 3 and all(conflict["edge_id"] for conflict in body["conflicts"])
    assert body["project"]["status"] == "AUTOPSY_COMPLETE" and body["synthesis"]["mode"] == "DETERMINISTIC"
    retries = next(node for node in features if node["data"]["title"] == "Webhook retries")
    assert any(edge["id"] == f"evidence:{retries['id']}->branch:{BRANCH}" for edge in body["edges"])
    branch_citations = [c for c in body["citations"][retries["id"]] if c["is_branch_only"]]
    assert branch_citations and all(c["branch_name"] == BRANCH and c["artifact_id"] for c in branch_citations)
    assert all(c["signal_index"] is not None and c["snippet"] for rows in body["citations"].values() for c in rows)
    assert "raw_model_response" not in response.content.decode()


def test_genome_query_count_is_bounded(client: Client, django_assert_max_num_queries: Any) -> None:
    project = synthesized_project()

    with django_assert_max_num_queries(MAX_GENOME_QUERIES):
        response = client.get(genome_url(project.pk))

    assert response.status_code == 200


def test_unknown_project_is_404(client: Client) -> None:
    assert client.get(genome_url(uuid4())).status_code == 404


@pytest.mark.parametrize(
    "status", [ProjectStatus.DORMANT, ProjectStatus.INGESTING, ProjectStatus.SYNTHESIZING_GENOME]
)
def test_in_progress_projects_get_409_with_the_telemetry_path(client: Client, status: str) -> None:
    project = Project.objects.create(name="busy", status=status)

    response = client.get(genome_url(project.pk))

    assert response.status_code == 409
    body = response.json()
    assert (body["code"], body["status"]) == ("GENOME_PENDING", status)
    assert body["telemetry_ws_path"] == f"/ws/projects/{project.pk}/telemetry/"


def test_failed_project_without_genome_gets_409_with_the_detail(client: Client) -> None:
    project = Project.objects.create(name="broken", status=ProjectStatus.FAILED, status_detail="Clone failed.")

    response = client.get(genome_url(project.pk))

    assert response.status_code == 409
    assert response.json()["code"] == "GENOME_UNAVAILABLE" and response.json()["status_detail"] == "Clone failed."


def test_only_the_latest_synthesis_is_returned(client: Client) -> None:
    project = synthesized_project()
    first = GenomeSynthesis.objects.get(project=project)
    assert genome_tasks.run_genome_synthesis(project.pk)["status"] == "AUTOPSY_COMPLETE"  # re-synthesis
    latest = GenomeSynthesis.objects.exclude(pk=first.pk).get(project=project)
    # A leftover node of the older run must not appear.
    IntentNode.objects.create(
        project=project, synthesis=first, title="stale", category="CORE_FEATURE", vital_status="GHOST_FEATURE",
        epistemic_status="MISSING_CONTEXT", confidence_score=0.4, reconstructed_intent="", actual_implementation_state="",
    )

    body = client.get(genome_url(project.pk)).json()

    assert body["synthesis"]["id"] == str(latest.pk)
    feature_ids = {node["id"] for node in body["nodes"] if node["type"] == "feature"}
    assert feature_ids == {str(pk) for pk in latest.intent_nodes.values_list("id", flat=True)}
    assert all(node["data"]["title"] != "stale" for node in body["nodes"] if node["type"] == "feature")
