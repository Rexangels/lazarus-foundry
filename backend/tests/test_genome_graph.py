"""Tests for apps.genome.graph (the GET /api/projects/{project_id}/genome body).

Fast tests build the graph from hand-built inputs and from the toy reconciler
genome (no Git, no database). The ``slow`` tests run the real PayPulse path:
``run_ingestion`` on a clone, catalog, deterministic synthesis, graph.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from django.test import Client, override_settings

from apps.genome.agents import GenomeReconcilerAgent, GenomeSynthesisResult
from apps.genome.catalog import catalog_from_rows
from apps.genome.graph import (
    BranchInput,
    CitationInput,
    ConflictInput,
    FeatureInput,
    ProjectInput,
    SynthesisInput,
    build_genome_graph,
    build_genome_graph_from_inputs,
    inputs_from_features,
)
from apps.genome.models import ConflictRecord, EvidenceCitation, IntentNode
from apps.genome.reconciler import reconcile
from apps.genome.schemas import GenomeGraphOut
from apps.ingestion.git_forensics import clone_local_repository_into_workspace
from apps.ingestion.pipeline import IngestionResult, run_ingestion
from tests.fixtures.build_paypulse_fixture import RETRY_BRANCH_NAME, FixtureBuildResult
from tests.genome_helpers import BRANCH, toy_catalog, toy_ingestion_rows

PROJECT_ID = UUID("00000000-0000-4000-8000-000000000001")
MAIN_HEAD = "a" * 40
BRANCH_HEAD = "b" * 40
WHEN = datetime(2023, 1, 1, tzinfo=UTC)
RAW_RESPONSE = "RAW-MODEL-REPLY-must-not-leak"
FULL_DIGEST = "d" * 64
FALLBACK_DETAIL = "fallback detail text must not leak"


# ---------------------------------------------------------------------------
# Hand-built inputs
# ---------------------------------------------------------------------------
def citation(cid: str, source_type: str, path: str, line: int | None, *, branch: str | None = "main",
             commit: str = MAIN_HEAD, snippet: str = "snippet", index: int = 0) -> CitationInput:
    return CitationInput(
        id=cid, source_type=source_type, file_path=path, commit_sha=commit, line_start=line, line_end=line,
        snippet=snippet, weight=0.5, branch_name=branch, artifact_id=None, signal_index=index,
    )


def breakdown(key: str, vital: str, epistemic: str, confidence: float) -> dict[str, Any]:
    return {
        "confidence": confidence, "z": 1.0, "evidence_total": 1.5, "prior_bias": 0.5, "conflict": epistemic == "CONFLICTED",
        "conflict_penalty": 0.4 if epistemic == "CONFLICTED" else 0.0,
        "contributions": [{"source_type": "README_CLAIM", "authority_weight": 0.35, "signal_count": 1, "uncapped": 0.21,
                           "cap": 0.6125, "contribution": 0.21, "capped": False,
                           "terms": [{"signal_index": 0, "rank": 1, "match_strength": 0.6, "age_days": 0.0, "decay": 1.0,
                                      "base_term": 0.21, "rank_factor": 1.0, "term": 0.21}]}],
        "epistemic_status": epistemic, "status_reason": "CONFLICT" if epistemic == "CONFLICTED" else "ABOVE_KNOWN_THRESHOLD",
        "parameters": {"prior_bias": 0.5}, "feature_key": key, "origin": "CLAIM", "draft_keys": [],
        "vital_status": vital, "vital_status_reason": "HEAD_IMPLEMENTATION", "matched_evidence_count": 3,
        "citations": [{"signal_index": 0, "catalog_id": "E1", "source_type": "README_CLAIM", "match_kind": "ANCHOR",
                       "match_strength": 0.6, "weight": 0.21}],
    }


def feature(fid: str, title: str, vital: str, epistemic: str, x: int, y: int, citations: tuple[CitationInput, ...],
            conflicts: tuple[ConflictInput, ...] = ()) -> FeatureInput:
    return FeatureInput(
        id=fid, title=title, category="CORE_FEATURE", vital_status=vital, epistemic_status=epistemic,
        confidence_score=0.5, reconstructed_intent=f"{title} intent", actual_implementation_state=f"{title} state",
        graph_position={"x": x, "y": y, "column": vital, "row": y // 180}, score_breakdown=breakdown(fid, vital, epistemic, 0.5),
        citations=citations, conflicts=conflicts,
    )


def conflict(cid: str, claim_source: str) -> ConflictInput:
    return ConflictInput(
        id=cid, claim_source=claim_source, reality_source="reality", conflict_description="description",
        resolution_recommendation="recommendation", created_at=WHEN,
    )


def hand_built() -> tuple[ProjectInput, SynthesisInput, list[FeatureInput], list[BranchInput]]:
    project = ProjectInput(PROJECT_ID, "Toy", "AUTOPSY_COMPLETE", "", "main")
    synthesis = SynthesisInput(
        id=UUID("00000000-0000-4000-8000-000000000002"), mode="MODEL_FALLBACK", model_id="muse-spark-1.3", started_at=WHEN, finished_at=WHEN,
        catalog_size=42, node_count=3, conflict_count=3, input_digest=FULL_DIGEST,
        validation_report={
            "fallback_reason": "MODEL_TIMEOUT", "fallback_detail": FALLBACK_DETAIL,
            "dropped_citation_ids": [{"feature": "x", "id": "E99"}, {"feature": "y", "id": "E98"}],
            "rejected_features": [{"feature": "#1", "reason": "EMPTY_TITLE"}],
            "vital_status_overrides": [{"feature": "x", "suggested": "VERIFIED_WORKING", "applied": "GHOST_FEATURE"}],
        },
    )
    alive = feature("f-alive", "Ledger", "VERIFIED_WORKING", "KNOWN", 0, 0, (
        citation("1", "README_CLAIM", "README.md", 3, index=1),
        citation("2", "AST_IMPLEMENTATION", "pkg/ledger.py", 10, index=0),
    ))
    dormant = feature("f-dormant", "Retries", "ABANDONED_IN_PROGRESS", "CONFLICTED", 360, 0, (
        citation("3", "README_CLAIM", "README.md", 4, index=0),
        citation("4", "UNMERGED_BRANCH_COMMIT", "pkg/retry.py", None, branch="feat/x", commit=BRANCH_HEAD, index=1),
        # The artifact row is gone: the branch is recovered from the commit SHA.
        citation("5", "TODO_COMMENT", "pkg/retry.py", 2, branch=None, commit=BRANCH_HEAD, index=2),
        citation("6", "AST_IMPLEMENTATION", "pkg/webhooks.py", 5, index=3),
    ), (conflict("c1", 'README.md:4: "[x] Retries"'),))
    ghost = feature("f-ghost", "PDF export", "GHOST_FEATURE", "CONFLICTED", 720, 0, (
        citation("7", "README_CLAIM", "README.md", 5, branch=None, commit="9" * 40, index=0),
    ), (
        conflict("c2", 'README.md:5: "[x] PDF export"'),
        conflict("c3", 'docs/spec.md:4-6: "PDF"'),  # no matching citation: parsed from the text
        conflict("c4", "an unlocatable claim"),
    ))
    branches = [
        BranchInput("main", MAIN_HEAD, WHEN, True, False, False, 0, 0, 0.0),
        BranchInput("feat/x", BRANCH_HEAD, WHEN, False, False, True, 4, 5, 543.0),
        BranchInput("feat/old", "c" * 40, WHEN, False, False, True, 1, 9, 900.0),
        BranchInput("feat/merged", "e" * 40, WHEN, False, True, False, 0, 3, 10.0),
    ]
    # Out of order on purpose: the builder sorts.
    return project, synthesis, [ghost, alive, dormant], branches


@pytest.fixture
def graph() -> GenomeGraphOut:
    return build_genome_graph(*hand_built())


def test_nodes_are_react_flow_shaped_and_ordered(graph: GenomeGraphOut) -> None:
    body = json.loads(graph.model_dump_json())
    assert [(n["id"], n["type"], n["position"]) for n in body["nodes"]] == [
        ("f-alive", "feature", {"x": 0.0, "y": 0.0}),
        ("f-dormant", "feature", {"x": 360.0, "y": 0.0}),
        ("f-ghost", "feature", {"x": 720.0, "y": 0.0}),
        ("branch:feat/x", "branch", {"x": 1080.0, "y": 0.0}),
        ("branch:feat/old", "branch", {"x": 1080.0, "y": 180.0}),  # abandoned, uncited: drawn below
        ("document:README.md", "document", {"x": -360.0, "y": 0.0}),
        ("document:docs/spec.md", "document", {"x": -360.0, "y": 180.0}),
    ]
    tokens = {n["id"]: n["data"]["color_token"] for n in body["nodes"]}
    assert tokens["f-alive"] == "alive" and tokens["f-dormant"] == "dormant" and tokens["f-ghost"] == "ghost"
    assert tokens["branch:feat/x"] == "dormant" and tokens["document:README.md"] == "conflict"
    ghost = body["nodes"][2]["data"]
    assert (ghost["citation_count"], ghost["conflict_count"], ghost["feature_key"]) == (1, 3, "f-ghost")
    branch = body["nodes"][3]["data"]
    assert (branch["citation_count"], branch["feature_ids"], branch["dormancy_days"]) == (2, ["f-dormant"], 543.0)
    assert body["nodes"][4]["data"]["feature_ids"] == []


def test_branch_nodes_only_for_cited_or_abandoned_branches(graph: GenomeGraphOut) -> None:
    drawn = {node.id for node in graph.nodes if node.type == "branch"}
    assert drawn == {"branch:feat/x", "branch:feat/old"}  # not main, not the merged uncited branch
    assert [(b.name, b.node_id) for b in graph.branches] == [
        ("main", None), ("feat/merged", None), ("feat/old", "branch:feat/old"), ("feat/x", "branch:feat/x"),
    ]


def test_edges_are_deterministic_and_conflicts_point_at_claim_documents(graph: GenomeGraphOut) -> None:
    body = json.loads(graph.model_dump_json())
    assert [(e["id"], e["source"], e["target"], e["type"]) for e in body["edges"]] == [
        ("evidence:f-dormant->branch:feat/x", "f-dormant", "branch:feat/x", "evidence"),
        ("conflict:c1", "f-dormant", "document:README.md", "conflict"),
        ("conflict:c2", "f-ghost", "document:README.md", "conflict"),
        ("conflict:c3", "f-ghost", "document:docs/spec.md", "conflict"),
    ]
    evidence = body["edges"][0]["data"]
    assert evidence == {"citation_count": 2, "source_types": ["UNMERGED_BRANCH_COMMIT", "TODO_COMMENT"],
                        "color_token": "dormant"}
    assert body["edges"][1]["data"]["claim_citation_id"] == "3"
    assert (body["edges"][3]["data"]["line_start"], body["edges"][3]["data"]["line_end"]) == (4, 6)
    by_id = {c["id"]: c for c in body["conflicts"]}
    assert [c["id"] for c in body["conflicts"]] == ["c1", "c2", "c3", "c4"]
    assert (by_id["c2"]["claim_citation_id"], by_id["c2"]["claim_line_start"], by_id["c2"]["edge_id"]) == ("7", 5, "conflict:c2")
    assert by_id["c3"]["claim_citation_id"] is None and by_id["c3"]["document_node_id"] == "document:docs/spec.md"
    assert by_id["c4"]["edge_id"] is None and by_id["c4"]["document_node_id"] is None  # listed, not drawn
    assert build_genome_graph(*hand_built()) == graph


def test_citations_are_keyed_by_feature_and_ordered_by_source_type(graph: GenomeGraphOut) -> None:
    assert list(graph.citations) == ["f-alive", "f-dormant", "f-ghost"] == list(graph.score_breakdowns)
    dormant = graph.citations["f-dormant"]
    assert [(c.source_type, c.branch_name, c.is_branch_only) for c in dormant] == [
        ("AST_IMPLEMENTATION", "main", False),
        ("UNMERGED_BRANCH_COMMIT", "feat/x", True),
        ("TODO_COMMENT", "feat/x", True),  # recovered from the commit SHA
        ("README_CLAIM", "main", False),
    ]
    assert graph.citations["f-ghost"][0].branch_name is None  # no artifact and no matching head
    breakdown_out = graph.score_breakdowns["f-dormant"]
    assert breakdown_out.prior_bias == 0.5 and breakdown_out.conflict_penalty == 0.4
    assert breakdown_out.status_reason == "CONFLICT" and breakdown_out.contributions[0].terms[0].term == 0.21


def test_synthesis_summary_and_nothing_raw_leaks(graph: GenomeGraphOut) -> None:
    text = graph.model_dump_json()
    body = json.loads(text)
    assert body["synthesis"]["input_digest_prefix"] == "d" * 12
    assert body["synthesis"]["validation"] == {
        "dropped_citation_count": 2, "unsupported_citation_count": 0, "rejected_feature_count": 1,
        "dropped_conflict_count": 0, "vital_status_override_count": 1, "fallback_reason": "MODEL_TIMEOUT",
    }
    assert FULL_DIGEST not in text and FALLBACK_DETAIL not in text and RAW_RESPONSE not in text

    def keys(node: Any) -> set[str]:
        if isinstance(node, dict):
            return set(node) | {k for value in node.values() for k in keys(value)}
        if isinstance(node, list):
            return {k for value in node for k in keys(value)}
        return set()

    assert not keys(body) & {"raw_content", "raw_model_response", "raw_response", "input_digest", "embedding"}


def test_response_validates_against_the_schema(graph: GenomeGraphOut) -> None:
    assert GenomeGraphOut.model_validate_json(graph.model_dump_json()) == graph
    project, _, features, branches = hand_built()
    empty = build_genome_graph(project, None, features, branches)
    assert empty.synthesis is None


def test_features_without_a_stored_position_get_the_reconciler_column() -> None:
    project, synthesis, features, branches = hand_built()
    moved = FeatureInput(**{**features[1].__dict__, "id": "f-new", "title": "Another", "graph_position": {}})
    graph = build_genome_graph(project, synthesis, [*features, moved], branches)
    position = next(node.position for node in graph.nodes if node.id == "f-new")
    assert (position.x, position.y) == (0.0, 180.0)  # below Ledger in the VERIFIED_WORKING column


def test_from_model_reads_prefetched_rows_without_queries() -> None:
    node = IntentNode(
        title="Retries", category="INTEGRATION", vital_status="ABANDONED_IN_PROGRESS", epistemic_status="CONFLICTED",
        confidence_score=0.79, reconstructed_intent="i", actual_implementation_state="s",
        graph_position={"x": 360, "y": 0}, score_breakdown=breakdown("claim:x", "ABANDONED_IN_PROGRESS", "CONFLICTED", 0.79),
    )
    row = EvidenceCitation(id=11, intent_node=node, source_type="README_CLAIM", file_path="README.md", commit_sha=MAIN_HEAD,
                           line_start=4, line_end=4, snippet="- [x] Retries", weight=0.21)
    row.artifact_branch_name = "main"  # the annotation load_genome_inputs adds
    record = ConflictRecord(id=5, intent_node=node, claim_source='README.md:4: "[x] Retries"', reality_source="r",
                            conflict_description="d", resolution_recommendation="rec")
    node._prefetched_objects_cache = {"citations": [row], "conflicts": [record]}  # what prefetch_related fills

    loaded = FeatureInput.from_model(node)  # pytest-django would fail this test on any query

    assert loaded.id == str(node.pk) and loaded.citations[0].branch_name == "main"
    assert loaded.citations[0].signal_index == 0  # aligned with score_breakdown["citations"]
    assert loaded.conflicts[0].id == "5"


def test_openapi_documents_the_genome_route(client: Client) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]
    assert {"200", "404", "409"} <= set(paths["/api/projects/{project_id}/genome"]["get"]["responses"])


# ---------------------------------------------------------------------------
# Toy reconciler genome (in memory)
# ---------------------------------------------------------------------------
def test_toy_genome_graph() -> None:
    branches, _ = toy_ingestion_rows()
    project = ProjectInput(PROJECT_ID, "toy", "AUTOPSY_COMPLETE", "", "main")
    graph = build_genome_graph_from_inputs(inputs_from_features(project, reconcile(toy_catalog()).features, branches))
    features = [node for node in graph.nodes if node.type == "feature"]
    assert len(features) == 6
    retries = next(node for node in features if node.data.title == "Webhook retries")
    assert any(edge.id == f"evidence:{retries.id}->branch:{BRANCH}" for edge in graph.edges)
    assert {c.feature_id for c in graph.conflicts} == {
        node.id for node in features if node.data.title in {"Webhook retries", "PDF export", "Currency conversion"}
    }
    assert all(edge.target == "document:README.md" for edge in graph.edges if edge.type == "conflict")


# ---------------------------------------------------------------------------
# PayPulse (real pipeline output, slow)
# ---------------------------------------------------------------------------
RETRIES = "Automated Exponential Backoff Webhook Retries"
CURRENCY = "Multi-currency conversion (USD, EUR, GBP) with daily ECB rates"
PDF = "PDF Invoice Export"
LEDGER = "Double-entry ledger with balanced, atomic postings"


@pytest.fixture(scope="module")
def paypulse_synthesis(
    paypulse_repo: FixtureBuildResult, tmp_path_factory: pytest.TempPathFactory
) -> tuple[IngestionResult, GenomeSynthesisResult]:
    root = tmp_path_factory.mktemp("genome_graph_workspaces")
    with override_settings(WORKSPACE_ROOT=root):
        workspace: Path = clone_local_repository_into_workspace(paypulse_repo.repo_path, root / "paypulse")
    ingestion = run_ingestion(workspace, emit=lambda event, payload: None)
    catalog = catalog_from_rows(ingestion.artifacts, ingestion.branches)
    return ingestion, GenomeReconcilerAgent(api_key="").synthesize(catalog)


@pytest.fixture(scope="module")
def paypulse_graph(paypulse_synthesis: tuple[IngestionResult, GenomeSynthesisResult]) -> GenomeGraphOut:
    ingestion, result = paypulse_synthesis
    project = ProjectInput(PROJECT_ID, "PayPulse", "AUTOPSY_COMPLETE", "", ingestion.default_branch)
    inputs = inputs_from_features(project, result.features, ingestion.branches, SynthesisInput.from_result(result))
    return build_genome_graph_from_inputs(inputs)


@pytest.mark.slow
def test_paypulse_graph(paypulse_graph: GenomeGraphOut) -> None:
    features = {node.data.title: node for node in paypulse_graph.nodes if node.type == "feature"}
    assert len(features) == 6
    retries = features[RETRIES]
    branch_id = f"branch:{RETRY_BRANCH_NAME}"
    assert any(e.type == "evidence" and e.source == retries.id and e.target == branch_id for e in paypulse_graph.edges)
    branch = next(node for node in paypulse_graph.nodes if node.id == branch_id)
    assert branch.data.is_abandoned and (branch.data.commits_ahead, branch.data.commits_behind) == (4, 5)
    assert retries.id in branch.data.feature_ids
    title_by_id = {node.id: title for title, node in features.items()}
    assert sorted(title_by_id[c.feature_id] for c in paypulse_graph.conflicts) == sorted([RETRIES, CURRENCY, PDF])
    assert all(c.edge_id and c.claim_file_path == "README.md" and c.claim_citation_id for c in paypulse_graph.conflicts)
    assert features[PDF].data.color_token == "ghost"
    ledger = features[LEDGER].data
    assert (ledger.color_token, ledger.epistemic_status, ledger.vital_status) == ("alive", "KNOWN", "VERIFIED_WORKING")
    assert paypulse_graph.synthesis is not None and paypulse_graph.synthesis.mode == "DETERMINISTIC"
    retry_citations = paypulse_graph.citations[retries.id]
    assert any(c.is_branch_only and c.branch_name == RETRY_BRANCH_NAME for c in retry_citations)
    assert all(c.snippet for c in retry_citations)


@pytest.mark.slow
def test_paypulse_graph_has_no_raw_content(
    paypulse_graph: GenomeGraphOut, paypulse_synthesis: tuple[IngestionResult, GenomeSynthesisResult]
) -> None:
    ingestion, result = paypulse_synthesis
    text = paypulse_graph.model_dump_json()
    assert result.input_digest not in text
    readme = next(row for row in ingestion.artifacts if row.file_path == "README.md")
    assert readme.raw_content not in text  # whole files are never sent, only cited snippets
    assert GenomeGraphOut.model_validate_json(text) == paypulse_graph
