"""Tests for apps.revival.planner (the three strategy work orders) and score_plan.

Fast tests plan a hand-built genome (``FeatureInput``/``CitationInput``, no Git,
no database) and random genomes (Hypothesis). The ``slow`` test runs the real
PayPulse path: ``run_ingestion`` on a clone, catalog, deterministic synthesis,
``inputs_from_features``, then ``plan_strategies``.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from django.test import override_settings
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apps.foundry.models import AgentStage
from apps.genome.agents import GenomeReconcilerAgent
from apps.genome.catalog import catalog_from_rows
from apps.genome.graph import BranchInput, CitationInput, FeatureInput, GenomeInputs, ProjectInput, inputs_from_features
from apps.ingestion.git_forensics import clone_local_repository_into_workspace
from apps.ingestion.models import ArtifactType
from apps.ingestion.pipeline import ArtifactRow, run_ingestion
from apps.revival.models import StrategyType
from apps.revival.planner import (
    DeferralReason,
    DependencyInput,
    ImportGraph,
    PlanItemKind,
    PlanStage,
    StrategyPlan,
    dependencies_from_artifacts,
    import_graph_from_artifacts,
    plan_strategies,
)
from apps.revival.scoring import score_plan
from tests.fixtures.build_paypulse_fixture import RETRY_BRANCH_NAME, FixtureBuildResult

PROJECT = ProjectInput(UUID("00000000-0000-4000-8000-0000000000aa"), "Toy", "AUTOPSY_COMPLETE", "", "main")
MAIN_SHA = "a" * 40
RETRY_SHA = "b" * 40
ACTIVE_SHA = "c" * 40
WHEN = datetime(2023, 1, 1, tzinfo=UTC)
BRANCHES = (
    BranchInput("main", MAIN_SHA, WHEN, True, False, False, 0, 0, 0.0),
    BranchInput("feat/retry", RETRY_SHA, WHEN, False, False, True, 3, 5, 300.0),
    BranchInput("feat/active", ACTIVE_SHA, WHEN, False, False, False, 1, 0, 2.0),
)
ADDED_RETRY = "--- /dev/null\n+++ b/pkg/retry.py\n@@ -0,0 +1,3 @@\n+def jitter():\n+    # TODO: finish jitter\n+    raise NotImplementedError\n"
ADDED_RETRY_TEST = "--- /dev/null\n+++ b/tests/test_retry.py\n@@ -0,0 +1,2 @@\n+def test_retry():\n+    assert True\n"


# ---------------------------------------------------------------------------
# Hand-built genome
# ---------------------------------------------------------------------------
def cite(fid: str, n: int, source: str, path: str, line: int | None, snippet: str, match: str, *,
         end: int | None = None, branch: str = "main", commit: str = MAIN_SHA) -> tuple[CitationInput, str]:
    return CitationInput(
        id=f"{fid}#c{n}", source_type=source, file_path=path, commit_sha=commit, line_start=line,
        line_end=end if end is not None else line, snippet=snippet, weight=0.5, branch_name=branch,
        artifact_id=None, signal_index=n,
    ), match


def feature(fid: str, title: str, vital: str, confidence: float, cited: Sequence[tuple[CitationInput, str]]) -> FeatureInput:
    citations = tuple(c for c, _ in cited)
    return FeatureInput(
        id=fid, title=title, category="CORE_FEATURE", vital_status=vital,
        epistemic_status="KNOWN" if vital == "VERIFIED_WORKING" else "CONFLICTED", confidence_score=confidence,
        reconstructed_intent="", actual_implementation_state="", graph_position={"x": 0, "y": 0},
        score_breakdown={
            "feature_key": f"key:{fid}",
            "citations": [
                {"signal_index": c.signal_index, "source_type": c.source_type, "match_kind": kind} for c, kind in cited
            ],
        },
        citations=citations,
    )


AST, BRANCH, SPEC, TODO, README = (
    "AST_IMPLEMENTATION", "UNMERGED_BRANCH_COMMIT", "ISSUE_SPEC", "TODO_COMMENT", "README_CLAIM",
)


def toy_features() -> list[FeatureInput]:
    return [
        feature("ledger", "Ledger", "VERIFIED_WORKING", 0.86, [
            cite("ledger", 0, README, "README.md", 3, "- [x] Ledger", "ANCHOR"),
            cite("ledger", 1, AST, "pkg/ledger.py", 1, "class Ledger:\n    pass", "OWN_CODE", end=20),
            cite("ledger", 2, AST, "tests/test_ledger.py", 1, "def test_post():\n    pass", "OWN_TEST", end=3),
        ]),
        feature("invoices", "Invoices", "VERIFIED_WORKING", 0.85, [
            cite("invoices", 0, AST, "pkg/invoices.py", 1, "class Invoice:\n    pass", "OWN_CODE", end=30),
            cite("invoices", 1, AST, "tests/test_invoices.py", 1, "def test_total():\n    pass", "OWN_TEST", end=4),
        ]),
        feature("hooks", "Hooks", "VERIFIED_WORKING", 0.90, [
            cite("hooks", 0, AST, "pkg/hooks.py", 1, "class Dispatcher:\n    pass", "OWN_CODE", end=40),
        ]),
        feature("retry", "Retries", "ABANDONED_IN_PROGRESS", 0.79, [
            cite("retry", 0, README, "README.md", 4, "- [x] Retries", "ANCHOR"),
            cite("retry", 1, AST, "tests/test_hooks.py", 5, "def test_hooks_retry():\n    assert False", "TEXT_MENTION", end=9),
            cite("retry", 2, TODO, "pkg/hooks.py", 30, "# FIXME: retries live on feat/retry", "TEXT_MENTION"),
            cite("retry", 3, BRANCH, "pkg/retry.py", None, ADDED_RETRY, "BRANCH_DIFF", branch="feat/retry", commit=RETRY_SHA),
            cite("retry", 4, BRANCH, "tests/test_retry.py", None, ADDED_RETRY_TEST, "BRANCH_DIFF", branch="feat/retry",
                 commit=RETRY_SHA),
            cite("retry", 5, BRANCH, "pkg/retry.py", 1, "def jitter():\n    raise NotImplementedError", "STUB", end=3,
                 branch="feat/retry", commit=RETRY_SHA),
            cite("retry", 6, TODO, "pkg/retry.py", 2, "# TODO: finish jitter", "SAME_FILE", branch="feat/retry",
                 commit=RETRY_SHA),
        ]),
        feature("currency", "Currency conversion with live ECB rates", "ABANDONED_IN_PROGRESS", 0.88, [
            cite("currency", 0, README, "README.md", 5, "- [x] Currency conversion with live ECB rates", "ANCHOR"),
            cite("currency", 1, AST, "pkg/currency.py", 1, "def normalize(code):\n    return code", "OWN_CODE", end=3),
            cite("currency", 2, AST, "pkg/currency.py", 5, "def convert(a, b):\n    raise NotImplementedError", "STUB", end=9),
            cite("currency", 3, TODO, "pkg/currency.py", 8, "# TODO: fetch ECB rates", "SAME_FILE"),
        ]),
        feature("pdf", "PDF export", "GHOST_FEATURE", 0.42, [
            cite("pdf", 0, README, "README.md", 6, "- [x] PDF export", "ANCHOR"),
            cite("pdf", 1, SPEC, "docs/design.md", 3, "## PDF export\nRendered by `pkg/export/pdf.py` with ReportLab.",
                 "TEXT_MENTION", end=5),
        ]),
        feature("csv", "CSV export", "GHOST_FEATURE", 0.42, [
            cite("csv", 0, README, "README.md", 7, "- [x] CSV export", "ANCHOR"),
            cite("csv", 1, SPEC, "docs/design.md", 7, "## CSV export\nWrite invoices through `pkg/export/csv_export.py`.",
                 "TEXT_MENTION", end=9),
        ]),
        feature("dark", "Dark mode", "GHOST_FEATURE", 0.43, [
            cite("dark", 0, README, "README.md", 8, "- [ ] Dark mode", "ANCHOR"),
        ]),
        feature("sync", "Sync", "ABANDONED_IN_PROGRESS", 0.5, [
            cite("sync", 0, BRANCH, "pkg/sync.py", 1, "def sync():\n    pass", "BRANCH_DIFF", end=2, branch="feat/active",
                 commit=ACTIVE_SHA),
        ]),
    ]


def dep(name: str, specifier: str, pinned: str | None, line: int) -> DependencyInput:
    return DependencyInput(name, name, specifier, pinned, "requirements.txt", line, line, f"{name}{specifier}",
                           "PYPI", "REQUIREMENTS_TXT")


DEPENDENCIES = (dep("pytest", "==6.2.4", "6.2.4", 1), dep("requests", "==2.25.1", "2.25.1", 2), dep("click", ">=8", None, 3))
IMPORTS = ImportGraph({
    "pkg/invoices.py": frozenset({"pkg/ledger.py", "pkg/currency.py"}),
    "pkg/ledger.py": frozenset(),
    "pkg/currency.py": frozenset(),
    "pkg/hooks.py": frozenset(),
    "pkg/__init__.py": frozenset(),
    "tests/test_invoices.py": frozenset({"pkg/invoices.py", "pkg/ledger.py"}),
})


def plans(features: Sequence[FeatureInput] | None = None, *, imports: ImportGraph | None = IMPORTS,
          dependencies: Sequence[DependencyInput] = DEPENDENCIES) -> dict[StrategyType, StrategyPlan]:
    genome = GenomeInputs(PROJECT, None, tuple(features if features is not None else toy_features()), BRANCHES)
    return {plan.strategy_type: plan for plan in plan_strategies(genome, dependencies=dependencies, imports=imports)}


def items(plan: StrategyPlan, kind: PlanItemKind, feature_id: str | None = None) -> list[Any]:
    return [i for i in plan.action_plan if i.kind is kind and (feature_id is None or i.feature_id == feature_id)]


def deferred(plan: StrategyPlan) -> dict[tuple[str, str], Any]:
    return {(d.feature_id, d.reason.value): d for d in plan.deferred}


@pytest.fixture(scope="module")
def toy() -> dict[StrategyType, StrategyPlan]:
    return plans()


# ---------------------------------------------------------------------------
# RESTORE
# ---------------------------------------------------------------------------
def test_abandoned_branch_becomes_a_merge_with_its_diff_files(toy: dict[StrategyType, StrategyPlan]) -> None:
    restore = toy[StrategyType.RESTORE]
    [merge] = items(restore, PlanItemKind.MERGE_ABANDONED_BRANCH)
    assert (merge.feature_id, merge.branch, merge.stage) == ("retry", "feat/retry", PlanStage.ARCHITECTURE)
    assert merge.files_to_create == ("pkg/retry.py", "tests/test_retry.py") and merge.files_to_modify == ()
    assert merge.target_tests == ("tests/test_retry.py",)
    assert {"retry#c3", "retry#c4", "retry#c5"} <= set(merge.evidence_ids)
    assert [(f.name, f.value) for f in merge.feasibility_factors] == [("BASE", 0.85), ("MERGE_DISTANCE", 0.8)]
    assert merge.item_feasibility == 0.68


def test_stubs_become_complete_stub_items(toy: dict[StrategyType, StrategyPlan]) -> None:
    restore = toy[StrategyType.RESTORE]
    [merge] = items(restore, PlanItemKind.MERGE_ABANDONED_BRANCH)
    [branch_stub] = items(restore, PlanItemKind.COMPLETE_STUB, "retry")
    assert branch_stub.files_to_modify == ("pkg/retry.py",) and branch_stub.depends_on == (merge.id,)
    assert branch_stub.open_markers == ("pkg/retry.py:2: TODO: finish jitter",)
    [head_stub] = items(restore, PlanItemKind.COMPLETE_STUB, "currency")
    assert head_stub.title == "Complete `convert` in pkg/currency.py" and head_stub.branch is None
    assert set(head_stub.evidence_ids) == {"currency#c2", "currency#c3"}
    assert [f.name for f in head_stub.feasibility_factors] == ["BASE", "OPEN_MARKERS", "EXTERNAL_SERVICE"]
    assert [r.capability for r in head_stub.external_requirements] == ["live_fx_rates"]


def test_head_tests_of_a_feature_without_head_code_are_fixed_by_wiring(toy: dict[StrategyType, StrategyPlan]) -> None:
    restore = toy[StrategyType.RESTORE]
    [fix] = items(restore, PlanItemKind.FIX_FAILING_TEST)
    assert fix.target_tests == ("tests/test_hooks.py::test_hooks_retry",)
    assert fix.files_to_modify == ("pkg/hooks.py",)  # the module under test and the FIXME's file
    assert "tests/test_hooks.py" not in fix.files  # target tests are never edited
    assert set(fix.evidence_ids) == {"retry#c1", "retry#c2"}
    merge, stub = items(restore, PlanItemKind.MERGE_ABANDONED_BRANCH)[0], items(restore, PlanItemKind.COMPLETE_STUB, "retry")[0]
    assert fix.depends_on == (merge.id, stub.id)


def test_features_without_target_tests_get_add_tests(toy: dict[StrategyType, StrategyPlan]) -> None:
    restore = toy[StrategyType.RESTORE]
    [currency_tests] = items(restore, PlanItemKind.ADD_TESTS, "currency")
    assert currency_tests.files_to_create == ("tests/test_currency.py",)
    assert not items(restore, PlanItemKind.ADD_TESTS, "retry")  # the merged branch brings tests/test_retry.py


def test_ghost_deferrals_and_spec_implementation(toy: dict[StrategyType, StrategyPlan]) -> None:
    restore = toy[StrategyType.RESTORE]
    reasons = deferred(restore)
    assert ("dark", "NO_DETAILED_SPEC") in reasons
    pdf = reasons[("pdf", "NEEDS_NEW_DEPENDENCY")]
    assert {r.capability for r in pdf.external_requirements} == {"pdf_rendering"}
    assert pdf.would_touch == ("pkg/export/pdf.py",) and pdf.evidence and not pdf.partial
    [spec] = items(restore, PlanItemKind.IMPLEMENT_FROM_SPEC)
    assert (spec.feature_id, spec.files_to_create) == ("csv", ("pkg/export/csv_export.py",))
    assert items(restore, PlanItemKind.ADD_TESTS, "csv")[0].files_to_create == ("tests/test_csv_export.py",)


def test_external_service_is_a_partial_deferral_and_active_branches_are_left_alone(
    toy: dict[StrategyType, StrategyPlan],
) -> None:
    restore = toy[StrategyType.RESTORE]
    reasons = deferred(restore)
    live = reasons[("currency", "NEEDS_EXTERNAL_SERVICE")]
    assert live.partial and {r.capability for r in live.external_requirements} == {"live_fx_rates"}
    scope = {entry.feature_id: entry for entry in restore.in_scope}
    assert scope["currency"].partial and not scope["retry"].partial
    sync = reasons[("sync", "BRANCH_STILL_ACTIVE")]
    assert not sync.partial and "sync" not in scope
    assert set(scope) == {"retry", "currency", "csv"}
    assert scope["retry"].feasibility == min(i.item_feasibility for i in restore.action_plan if i.feature_id == "retry")


def test_pinned_dependencies_are_upgraded_only_by_modernize(toy: dict[StrategyType, StrategyPlan]) -> None:
    assert not items(toy[StrategyType.RESTORE], PlanItemKind.UPGRADE_DEPENDENCY)
    assert not items(toy[StrategyType.EXTRACT], PlanItemKind.UPGRADE_DEPENDENCY)
    modernize = toy[StrategyType.MODERNIZE]
    upgrades = items(modernize, PlanItemKind.UPGRADE_DEPENDENCY)
    assert [u.dependency.name for u in upgrades] == ["pytest", "requests"]  # click is not pinned
    assert all(u.requires_resolution and u.dependency.target_version is None for u in upgrades)
    assert upgrades[0].evidence_ids == ("dependency:requirements.txt:1",)
    assert [i.id for i in modernize.action_plan[:2]] == [u.id for u in upgrades]  # ENVIRONMENT runs first
    restore_kinds = [(i.kind, i.feature_id, i.title) for i in toy[StrategyType.RESTORE].action_plan]
    assert [(i.kind, i.feature_id, i.title) for i in modernize.action_plan[2:]] == restore_kinds


# ---------------------------------------------------------------------------
# EXTRACT
# ---------------------------------------------------------------------------
def test_extract_picks_the_highest_value_import_cluster(toy: dict[StrategyType, StrategyPlan]) -> None:
    extract = toy[StrategyType.EXTRACT]
    modules = items(extract, PlanItemKind.EXTRACT_MODULE)
    assert [m.feature_id for m in modules] == ["ledger", "invoices"]  # 0.86 + 0.85 beats Hooks' 0.90
    assert extract.extract_root == "extracted/pkg-ledger-invoices"
    assert extract.value_basis.extracted_feature_ids == ("ledger", "invoices")
    root = extract.extract_root
    assert {f"{root}/pyproject.toml", f"{root}/pkg/__init__.py", f"{root}/pkg/ledger.py",
            f"{root}/tests/test_ledger.py"} == set(modules[0].files_to_create)
    assert f"{root}/pkg/currency.py" in modules[1].files_to_create  # pulled in by invoices' import
    assert [f.name for f in modules[1].feasibility_factors] == ["BASE", "BOUNDARY_LEAK"]
    assert dict(extract.risk_evidence) == {"EXTRACT_BOUNDARY": 1}
    [boundary] = items(extract, PlanItemKind.ADD_TESTS)
    assert boundary.files_to_create == (f"{root}/tests/test_public_api.py",)
    assert boundary.depends_on == (modules[0].id, modules[1].id)
    assert ("hooks", "OUTSIDE_SELECTED_CLUSTER") in deferred(extract)


def test_extract_without_an_import_graph_takes_the_single_best_feature() -> None:
    extract = plans(imports=None)[StrategyType.EXTRACT]
    assert [m.feature_id for m in items(extract, PlanItemKind.EXTRACT_MODULE)] == ["hooks"]
    assert dict(extract.risk_evidence) == {"EXTRACT_BOUNDARY": 0}


def test_extract_with_nothing_verified_is_empty_but_explained() -> None:
    features = [f for f in toy_features() if f.vital_status != "VERIFIED_WORKING"]
    extract = plans(features)[StrategyType.EXTRACT]
    assert not extract.is_actionable and extract.extract_root is None
    assert {d.reason for d in extract.deferred} == {DeferralReason.NOT_VERIFIED_WORKING}
    assert score_plan(extract).score.value == 0.0


# ---------------------------------------------------------------------------
# Invariants, scoring, ordering
# ---------------------------------------------------------------------------
def test_every_item_cites_evidence_and_stages_match_the_foundry(toy: dict[StrategyType, StrategyPlan]) -> None:
    assert set(PlanStage) <= set(AgentStage.values)
    for plan in toy.values():
        stages = [i.stage for i in plan.action_plan]
        assert stages == sorted(stages, key=list(PlanStage).index)
        for index, item in enumerate(plan.action_plan, start=1):
            assert item.evidence and item.id == f"{plan.strategy_type.value.lower()}-{index:02d}"
            assert 0.0 <= item.item_feasibility <= 1.0
        json.dumps(plan.as_dict())


def test_scores_differ_for_evidence_reasons(toy: dict[StrategyType, StrategyPlan]) -> None:
    scores = {kind: score_plan(plan) for kind, plan in toy.items()}
    restore, modernize = scores[StrategyType.RESTORE], scores[StrategyType.MODERNIZE]
    assert restore.score.value == modernize.score.value  # same features restored
    assert modernize.score.debt_cost > restore.score.debt_cost  # + requirements.txt
    assert restore.risk.risk == pytest.approx(0.30)  # 0.25 + 5 commits behind * 0.01
    assert modernize.risk.risk == pytest.approx(0.70)  # 0.60 + 0.05 + 2 pins * 0.025
    assert scores[StrategyType.EXTRACT].score.value == pytest.approx(2 / 3, abs=1e-4)
    for result in scores.values():
        body = result.as_dict()
        json.dumps(body)
        assert body["debt_derivation"]["file_count"] == len(toy[result.strategy_type].files)
        fields = result.model_fields()
        assert 0 <= fields["feasibility_score"] <= 100
        assert all(0 <= fields[k] <= 1 for k in ("value_score", "debt_cost_score", "risk_score"))


def test_planning_is_deterministic_and_order_independent() -> None:
    first = plans()
    shuffled = plans(list(reversed(toy_features())))
    for kind in StrategyType:
        assert first[kind].as_dict() == shuffled[kind].as_dict()


def test_a_feature_without_citations_is_refused() -> None:
    empty = FeatureInput("x", "X", "CORE_FEATURE", "GHOST_FEATURE", "MISSING_CONTEXT", 0.4, "", "", {}, {}, ())
    with pytest.raises(ValueError, match="no citations"):
        plans([empty])


def test_ingestion_facts_are_read_from_artifact_rows() -> None:
    rows = [
        ArtifactRow(ArtifactType.DEPENDENCY_MANIFEST, "requirements.txt", "main", "", {"manifest": "REQUIREMENTS_TXT",
            "dependencies": [{"name": "Requests", "normalized_name": "requests", "specifier": "==2.25.1",
                              "pinned_version": "2.25.1", "line_start": 2, "line_end": 2, "snippet": "Requests==2.25.1",
                              "ecosystem": "PYPI"}]}),
        ArtifactRow(ArtifactType.AST_MODULE, "pkg/invoices.py", "main", "", {"imports": [
            {"module": "pkg.ledger", "level": 0, "names": []},
            {"module": "", "level": 1, "names": [{"name": "currency"}]},
            {"module": "requests", "level": 0, "names": []},
            {"module": "json", "level": 0, "names": []},
        ]}),
        ArtifactRow(ArtifactType.AST_MODULE, "pkg/ledger.py", "main", "", {"imports": []}),
        ArtifactRow(ArtifactType.AST_MODULE, "pkg/currency.py", "main", "", {"imports": []}),
        ArtifactRow(ArtifactType.AST_MODULE, "pkg/other.py", "feat/x", "", {"imports": [{"module": "pkg.ledger"}]}),
    ]
    [requests] = dependencies_from_artifacts(rows, "main")
    assert (requests.normalized_name, requests.pinned_version, requests.line_start) == ("requests", "2.25.1", 2)
    graph = import_graph_from_artifacts(rows, "main")
    assert graph.internal["pkg/invoices.py"] == {"pkg/ledger.py", "pkg/currency.py"}
    assert graph.external["pkg/invoices.py"] == {"requests"}
    assert "pkg/other.py" not in graph.files  # branch rows are not HEAD
    assert graph.closure(["pkg/invoices.py"]) == {"pkg/invoices.py", "pkg/ledger.py", "pkg/currency.py"}


# ---------------------------------------------------------------------------
# Properties over random genomes
# ---------------------------------------------------------------------------
CODE_PATHS = ("pkg/a.py", "pkg/b.py", "pkg/c.py")
TEST_PATHS = ("tests/test_a.py", "tests/test_b.py")
WORDS = ("ledger", "pdf", "ecb rates", "invoice", "stripe", "csv", "retry", "qr code")


@st.composite
def random_citation(draw: st.DrawFn, fid: str, n: int) -> tuple[CitationInput, str]:
    kind = draw(st.sampled_from(("claim", "spec", "code", "stub", "test", "marker", "branch_file", "branch_stub", "dep")))
    branch = draw(st.sampled_from(("feat/retry", "feat/active", "feat/gone")))
    sha = {"feat/retry": RETRY_SHA, "feat/active": ACTIVE_SHA}.get(branch, "d" * 40)
    code, test, word = draw(st.sampled_from(CODE_PATHS)), draw(st.sampled_from(TEST_PATHS)), draw(st.sampled_from(WORDS))
    line = draw(st.integers(1, 200))
    if kind == "claim":
        return cite(fid, n, README, "README.md", line, f"- [x] {word}", "ANCHOR")
    if kind == "spec":
        return cite(fid, n, SPEC, "docs/design.md", line, f"## {word}\nSee `{code}` and `pkg/new/{word.split()[0]}.py`.",
                    "TEXT_MENTION", end=line + 3)
    if kind == "code":
        return cite(fid, n, AST, code, line, "def run():\n    return 1", "OWN_CODE", end=line + 5)
    if kind == "stub":
        return cite(fid, n, AST, code, line, "def run():\n    raise NotImplementedError", "STUB", end=line + 2)
    if kind == "test":
        return cite(fid, n, AST, test, line, "def test_run():\n    assert run()", draw(st.sampled_from(("OWN_TEST", "TEXT_MENTION"))))
    if kind == "marker":
        return cite(fid, n, TODO, code, line, f"# TODO: {word}", "SAME_FILE")
    if kind == "branch_file":
        patch = draw(st.sampled_from((f"--- /dev/null\n+++ b/{code}\n+x = 1\n", f"--- a/{code}\n+++ b/{code}\n-x\n+y\n")))
        return cite(fid, n, BRANCH, draw(st.sampled_from((code, test))), None, patch, "BRANCH_DIFF", branch=branch, commit=sha)
    if kind == "branch_stub":
        return cite(fid, n, BRANCH, code, line, "def run():\n    raise NotImplementedError", "STUB", end=line + 1,
                    branch=branch, commit=sha)
    return cite(fid, n, SPEC, "requirements.txt", 1, "pytest==6.2.4", "TEXT_MENTION")


@st.composite
def random_genome(draw: st.DrawFn) -> list[FeatureInput]:
    features = []
    for index in range(draw(st.integers(1, 5))):
        fid = f"f{index}"
        count = draw(st.integers(1, 6))
        cited = [draw(random_citation(fid, n)) for n in range(count)]
        vital = draw(st.sampled_from(("VERIFIED_WORKING", "ABANDONED_IN_PROGRESS", "GHOST_FEATURE")))
        confidence = draw(st.floats(0.3, 0.99))
        features.append(feature(fid, f"Feature {draw(st.sampled_from(WORDS))} {index}", vital, confidence, cited))
    return features


PLAN_PROPERTIES = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@PLAN_PROPERTIES
@given(random_genome(), st.booleans())
def test_every_plan_item_cites_evidence(features: list[FeatureInput], with_imports: bool) -> None:
    """**Validates: Requirements 3.1, 2.3**"""
    graph = ImportGraph({"pkg/a.py": frozenset({"pkg/b.py"}), "pkg/b.py": frozenset(), "pkg/c.py": frozenset()})
    for plan in plans(features, imports=graph if with_imports else None).values():
        known = {i.id for i in plan.action_plan}
        for item in plan.action_plan:
            assert len(item.evidence) >= 1
            assert set(item.depends_on) <= known
        for entry in plan.deferred:
            assert len(entry.evidence) >= 1


@PLAN_PROPERTIES
@given(random_genome(), st.booleans())
def test_value_debt_and_risk_stay_in_the_unit_interval(features: list[FeatureInput], with_deps: bool) -> None:
    """**Validates: Requirements 3.2**"""
    for plan in plans(features, dependencies=DEPENDENCIES if with_deps else ()).values():
        result = score_plan(plan)
        assert 0.0 <= result.score.value <= 1.0
        assert 0.0 <= result.score.debt_cost <= 1.0
        assert 0.0 <= result.score.risk <= 1.0
        assert 0.0 <= result.score.score <= 100.0


# ---------------------------------------------------------------------------
# PayPulse (real pipeline output, slow)
# ---------------------------------------------------------------------------
RETRIES = "Automated Exponential Backoff Webhook Retries"
CURRENCY = "Multi-currency conversion (USD, EUR, GBP) with daily ECB rates"
PDF = "PDF Invoice Export"
LEDGER = "Double-entry ledger with balanced, atomic postings"
INVOICES = "Invoices with line items, tax and a draft/issued/paid/void workflow"


@pytest.fixture(scope="module")
def paypulse_plans(
    paypulse_repo: FixtureBuildResult, tmp_path_factory: pytest.TempPathFactory
) -> dict[StrategyType, StrategyPlan]:
    root = tmp_path_factory.mktemp("revival_workspaces")
    with override_settings(WORKSPACE_ROOT=root):
        workspace: Path = clone_local_repository_into_workspace(paypulse_repo.repo_path, root / "paypulse")
    ingestion = run_ingestion(workspace, emit=lambda event, payload: None)
    result = GenomeReconcilerAgent(api_key="").synthesize(catalog_from_rows(ingestion.artifacts, ingestion.branches))
    project = ProjectInput(PROJECT.id, "PayPulse", "AUTOPSY_COMPLETE", "", ingestion.default_branch)
    genome = inputs_from_features(project, result.features, ingestion.branches)
    planned = plan_strategies(
        genome,
        dependencies=dependencies_from_artifacts(ingestion.artifacts, ingestion.default_branch),
        imports=import_graph_from_artifacts(ingestion.artifacts, ingestion.default_branch),
    )
    return {plan.strategy_type: plan for plan in planned}


def by_title(plan: StrategyPlan, title: str, kind: PlanItemKind) -> list[Any]:
    feature_ids = {e.feature_id for e in plan.in_scope if e.title == title}
    return [i for i in plan.action_plan if i.kind is kind and i.feature_id in feature_ids]


@pytest.mark.slow
def test_paypulse_restore(paypulse_plans: dict[StrategyType, StrategyPlan]) -> None:
    restore = paypulse_plans[StrategyType.RESTORE]
    [merge] = by_title(restore, RETRIES, PlanItemKind.MERGE_ABANDONED_BRANCH)
    assert merge.branch == RETRY_BRANCH_NAME
    assert merge.files_to_create == ("paypulse/retry.py", "tests/test_retry.py")
    [jitter] = by_title(restore, RETRIES, PlanItemKind.COMPLETE_STUB)
    assert jitter.title == "Complete `delay_for` in paypulse/retry.py" and jitter.depends_on == (merge.id,)
    [fix] = by_title(restore, RETRIES, PlanItemKind.FIX_FAILING_TEST)
    assert fix.target_tests == ("tests/test_webhooks.py::test_dispatch_retries_with_exponential_backoff",)
    assert fix.files_to_modify == ("paypulse/webhooks.py",)  # the wiring
    [convert] = by_title(restore, CURRENCY, PlanItemKind.COMPLETE_STUB)
    assert convert.files_to_modify == ("paypulse/currency.py",)
    assert [r.capability for r in convert.external_requirements] == ["live_fx_rates"]
    assert by_title(restore, CURRENCY, PlanItemKind.ADD_TESTS)[0].files_to_create == ("tests/test_currency.py",)
    reasons = {(d.title, d.reason) for d in restore.deferred}
    assert reasons == {(PDF, DeferralReason.NEEDS_NEW_DEPENDENCY), (CURRENCY, DeferralReason.NEEDS_EXTERNAL_SERVICE)}
    assert restore.files == ("paypulse/currency.py", "paypulse/retry.py", "paypulse/webhooks.py",
                             "tests/test_currency.py", "tests/test_retry.py")
    result = score_plan(restore)
    assert (result.score.value, result.score.debt_cost, result.score.risk, result.score.score) == (0.681, 0.3333, 0.3, 68.1)


@pytest.mark.slow
def test_paypulse_modernize_and_extract(paypulse_plans: dict[StrategyType, StrategyPlan]) -> None:
    modernize = paypulse_plans[StrategyType.MODERNIZE]
    upgrades = items(modernize, PlanItemKind.UPGRADE_DEPENDENCY)
    assert [(u.dependency.name, u.dependency.current_specifier) for u in upgrades] == [
        ("pytest", "==6.2.4"), ("requests", "==2.25.1"),
    ]
    m = score_plan(modernize).score
    assert (m.value, m.debt_cost, m.risk, m.score) == (0.681, 0.4, 0.7, 58.1)

    extract = paypulse_plans[StrategyType.EXTRACT]
    titles = [e.title for e in extract.in_scope]
    assert titles == [LEDGER, INVOICES]
    assert extract.extract_root == "extracted/paypulse-ledger-invoices"
    assert f"{extract.extract_root}/paypulse/currency.py" in extract.files  # invoices imports currency
    x = score_plan(extract).score
    assert (x.value, x.debt_cost, x.risk, x.score) == (0.6667, 0.6, 0.4, 57.3)
    for plan in paypulse_plans.values():
        assert all(item.evidence for item in plan.action_plan)
        json.dumps(plan.as_dict())
        json.dumps(score_plan(plan).as_dict())
