"""Tests for apps.genome.reconciler.

The fast tests use the synthetic toy repository in ``tests/genome_helpers.py``.
The ``slow`` tests build the catalog from the real pipeline output on a PayPulse
clone and assert the expected PayPulse genome (no database, no network).
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest
from django.test import override_settings

from apps.genome.agents import GenomeReconcilerAgent
from apps.genome.catalog import CatalogItem, CatalogKind, EvidenceCatalog, catalog_from_rows
from apps.genome.models import EpistemicStatus, EvidenceSourceType, IntentCategory, SynthesisMode, VitalStatus
from apps.genome.reconciler import (
    GRAPH_COLUMN_X,
    MAX_CITATIONS_PER_SOURCE_TYPE,
    FeatureOrigin,
    GenomeFeature,
    MatchKind,
    ReconciledGenome,
    VitalReason,
    classify_vital,
    reconcile,
)
from apps.genome.scoring import MATCH_STUB
from apps.ingestion.git_forensics import clone_local_repository_into_workspace
from apps.ingestion.pipeline import run_ingestion
from apps.projects.demo.paypulse import CURRENCY_PY, README_V2, WEBHOOKS_PY_V2
from tests.fixtures.build_paypulse_fixture import RETRY_BRANCH_NAME, RETRY_ENGINE_TODO_MARKER, FixtureBuildResult
from tests.genome_helpers import BRANCH, toy_catalog


def feature_titled(genome: ReconciledGenome, title: str) -> GenomeFeature:
    return next(feature for feature in genome.features if feature.title == title)


def item(source_type: EvidenceSourceType, *, on_default: bool = True, stub: bool = False, test: bool = False,
         abstract: bool = False) -> CatalogItem:
    return CatalogItem(
        id="E1", kind=CatalogKind.STUB if stub else CatalogKind.SYMBOL, source_type=source_type, name="x", text="",
        file_path="x.py", branch_name="main" if on_default else "feat/x", commit_sha="a" * 40, line_start=1,
        line_end=1, snippet="x", snippet_truncated=False, fingerprint="f", age_days=0.0 if on_default else 10.0,
        observed_on_default_branch=on_default, artifact_pk=None, artifact_index=0, is_stub=stub, is_test=test,
        in_abstract_method=abstract,
    )


AST = EvidenceSourceType.AST_IMPLEMENTATION
BRANCH_COMMIT = EvidenceSourceType.UNMERGED_BRANCH_COMMIT


# ---------------------------------------------------------------------------
# Vital status rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([item(AST)], (VitalStatus.VERIFIED_WORKING, VitalReason.HEAD_IMPLEMENTATION)),
        ([item(AST), item(AST, stub=True, abstract=True)], (VitalStatus.VERIFIED_WORKING, VitalReason.HEAD_IMPLEMENTATION)),
        ([item(AST), item(AST, stub=True)], (VitalStatus.ABANDONED_IN_PROGRESS, VitalReason.HEAD_STUB)),
        ([item(AST, stub=True)], (VitalStatus.ABANDONED_IN_PROGRESS, VitalReason.HEAD_STUB)),
        ([item(BRANCH_COMMIT, on_default=False)], (VitalStatus.ABANDONED_IN_PROGRESS, VitalReason.BRANCH_ONLY)),
        ([item(AST, test=True)], (VitalStatus.ABANDONED_IN_PROGRESS, VitalReason.TESTS_ONLY)),
        ([item(EvidenceSourceType.README_CLAIM), item(EvidenceSourceType.TODO_COMMENT)],
         (VitalStatus.GHOST_FEATURE, VitalReason.NO_IMPLEMENTATION)),
        ([], (VitalStatus.GHOST_FEATURE, VitalReason.NO_IMPLEMENTATION)),
    ],
)
def test_vital_status_rules(items: list[CatalogItem], expected: tuple[VitalStatus, VitalReason]) -> None:
    assert classify_vital(items) == expected


def test_real_code_plus_branch_work_is_still_verified() -> None:
    assert classify_vital([item(AST), item(BRANCH_COMMIT, on_default=False)])[0] is VitalStatus.VERIFIED_WORKING


# ---------------------------------------------------------------------------
# Toy repository
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def toy() -> tuple[EvidenceCatalog, ReconciledGenome]:
    catalog = toy_catalog()
    return catalog, reconcile(catalog)


def test_toy_features_and_vital_statuses(toy: tuple[EvidenceCatalog, ReconciledGenome]) -> None:
    _, genome = toy
    table = {feature.title: (feature.origin, feature.vital_status, feature.epistemic_status) for feature in genome.features}
    assert table == {
        "Ledger postings": (FeatureOrigin.CLAIM, VitalStatus.VERIFIED_WORKING, EpistemicStatus.KNOWN),
        "Webhook retries": (FeatureOrigin.CLAIM, VitalStatus.ABANDONED_IN_PROGRESS, EpistemicStatus.CONFLICTED),
        "PDF export": (FeatureOrigin.CLAIM, VitalStatus.GHOST_FEATURE, EpistemicStatus.CONFLICTED),
        "Currency conversion": (FeatureOrigin.CLAIM, VitalStatus.ABANDONED_IN_PROGRESS, EpistemicStatus.CONFLICTED),
        "Dark mode": (FeatureOrigin.CLAIM, VitalStatus.GHOST_FEATURE, EpistemicStatus.MISSING_CONTEXT),
        "pkg.webhooks module": (FeatureOrigin.MODULE, VitalStatus.VERIFIED_WORKING, EpistemicStatus.KNOWN),
    }


def test_branch_is_explained_by_the_matching_claim(toy: tuple[EvidenceCatalog, ReconciledGenome]) -> None:
    _, genome = toy
    retries = feature_titled(genome, "Webhook retries")
    kinds = {(c.item.kind, c.item.branch_name, c.match_kind) for c in retries.citations}
    assert (CatalogKind.BRANCH_FILE, BRANCH, MatchKind.BRANCH_DIFF) in kinds
    assert (CatalogKind.MARKER, BRANCH, MatchKind.SAME_FILE) in kinds
    assert (CatalogKind.MARKER, "main", MatchKind.TEXT_MENTION) in kinds  # the webhooks.py FIXME, by text
    assert not any(f.origin is FeatureOrigin.BRANCH for f in genome.features)
    # The dispatcher module shares one word with the claim, too few to absorb it.
    assert all(c.item.file_path != "pkg/webhooks.py" or c.item.kind is CatalogKind.MARKER for c in retries.citations)


def test_module_ownership_tests_and_stubs(toy: tuple[EvidenceCatalog, ReconciledGenome]) -> None:
    _, genome = toy
    ledger = feature_titled(genome, "Ledger postings")
    assert {(c.item.name, c.match_kind) for c in ledger.citations} >= {
        ("Ledger", MatchKind.OWN_CODE), ("test_post", MatchKind.OWN_TEST), ("Ledger postings", MatchKind.ANCHOR),
    }
    currency = feature_titled(genome, "Currency conversion")
    stubs = [c for c in currency.citations if c.item.is_stub]
    assert stubs and all(c.match_strength == MATCH_STUB for c in stubs)
    assert any(c.item.kind is CatalogKind.MARKER for c in currency.citations)  # same-file TODO


def test_conflicts_only_for_checked_claims_that_are_not_verified(toy: tuple[EvidenceCatalog, ReconciledGenome]) -> None:
    _, genome = toy
    conflicted = {feature.title for feature in genome.features if feature.conflicts}
    assert conflicted == {"Webhook retries", "PDF export", "Currency conversion"}

    pdf = feature_titled(genome, "PDF export").conflicts[0]
    assert pdf.claim_source == 'README.md:5: "[x] PDF export"'
    assert pdf.reality_source == f"no implementation found on main at {'a' * 12} or on any unmerged branch"
    retries = feature_titled(genome, "Webhook retries").conflicts[0]
    assert f"unmerged branch {BRANCH} (pkg/retry.py)" in retries.reality_source
    assert "rebase feat/webhook-retries onto main" in retries.resolution_recommendation
    currency = feature_titled(genome, "Currency conversion").conflicts[0]
    assert currency.reality_source.startswith("convert at pkg/currency.py:9 on main")


def test_scores_weights_and_breakdown_are_consistent(toy: tuple[EvidenceCatalog, ReconciledGenome]) -> None:
    _, genome = toy
    for feature in genome.features:
        assert feature.citations, feature.title
        assert 0.0 <= feature.confidence <= 1.0
        breakdown = feature.score_breakdown()
        assert [c["catalog_id"] for c in breakdown["citations"]] == [c.item.id for c in feature.citations]
        assert breakdown["vital_status"] == feature.vital_status.value
        assert all(citation.weight > 0 for citation in feature.citations)
        for source in EvidenceSourceType:
            cited = [c for c in feature.citations if c.item.source_type is source]
            assert len([c for c in cited if not c.item.is_stub]) <= MAX_CITATIONS_PER_SOURCE_TYPE


def test_layout_columns_by_vital_status_and_rows_by_title(toy: tuple[EvidenceCatalog, ReconciledGenome]) -> None:
    _, genome = toy
    for feature in genome.features:
        assert feature.graph_position["x"] == GRAPH_COLUMN_X[feature.vital_status]
    ghosts = [f for f in genome.features if f.vital_status is VitalStatus.GHOST_FEATURE]
    assert [f.title for f in ghosts] == ["Dark mode", "PDF export"]
    assert [f.graph_position["y"] for f in ghosts] == [0, 180]
    assert reconcile(toy_catalog()).features == genome.features  # deterministic


def test_category_heuristic(toy: tuple[EvidenceCatalog, ReconciledGenome]) -> None:
    _, genome = toy
    assert feature_titled(genome, "Webhook retries").category is IntentCategory.INTEGRATION
    assert feature_titled(genome, "Ledger postings").category is IntentCategory.CORE_FEATURE


# ---------------------------------------------------------------------------
# PayPulse (real pipeline output)
# ---------------------------------------------------------------------------
RETRIES = "Automated Exponential Backoff Webhook Retries"
CURRENCY = "Multi-currency conversion (USD, EUR, GBP) with daily ECB rates"
PDF = "PDF Invoice Export"
LEDGER = "Double-entry ledger with balanced, atomic postings"
INVOICES = "Invoices with line items, tax and a draft/issued/paid/void workflow"
WEBHOOKS_MODULE_KEY = "module:paypulse/webhooks.py"


def line_number(text: str, needle: str) -> int:
    return next(number for number, line in enumerate(text.split("\n"), start=1) if needle in line)


@pytest.fixture(scope="module")
def paypulse_catalog(paypulse_repo: FixtureBuildResult, tmp_path_factory: pytest.TempPathFactory) -> EvidenceCatalog:
    root = tmp_path_factory.mktemp("genome_workspaces")
    with override_settings(WORKSPACE_ROOT=root):
        workspace: Path = clone_local_repository_into_workspace(paypulse_repo.repo_path, root / "paypulse")
    result = run_ingestion(workspace, emit=lambda event, payload: None)
    return catalog_from_rows(result.artifacts, result.branches)


@pytest.fixture(scope="module")
def paypulse_genome(paypulse_catalog: EvidenceCatalog) -> ReconciledGenome:
    return reconcile(paypulse_catalog)


@pytest.mark.slow
def test_paypulse_genome_table(paypulse_genome: ReconciledGenome) -> None:
    table = {
        feature.key if feature.origin is FeatureOrigin.MODULE else feature.title: (
            feature.vital_status, feature.epistemic_status, bool(feature.conflicts)
        )
        for feature in paypulse_genome.features
    }
    assert table == {
        LEDGER: (VitalStatus.VERIFIED_WORKING, EpistemicStatus.KNOWN, False),
        INVOICES: (VitalStatus.VERIFIED_WORKING, EpistemicStatus.KNOWN, False),
        WEBHOOKS_MODULE_KEY: (VitalStatus.VERIFIED_WORKING, EpistemicStatus.KNOWN, False),
        RETRIES: (VitalStatus.ABANDONED_IN_PROGRESS, EpistemicStatus.CONFLICTED, True),
        CURRENCY: (VitalStatus.ABANDONED_IN_PROGRESS, EpistemicStatus.CONFLICTED, True),
        PDF: (VitalStatus.GHOST_FEATURE, EpistemicStatus.CONFLICTED, True),
    }


@pytest.mark.slow
def test_paypulse_retry_feature_cites_the_branch_diff_and_both_markers(
    paypulse_genome: ReconciledGenome, paypulse_repo: FixtureBuildResult
) -> None:
    retries = feature_titled(paypulse_genome, RETRIES)
    by_kind = {(c.item.kind, c.item.file_path, c.item.branch_name): c for c in retries.citations}

    diff = by_kind[(CatalogKind.BRANCH_FILE, "paypulse/retry.py", RETRY_BRANCH_NAME)]
    assert diff.item.source_type is EvidenceSourceType.UNMERGED_BRANCH_COMMIT
    assert diff.item.commit_sha == paypulse_repo.branch_heads[RETRY_BRANCH_NAME]
    assert "+++ b/paypulse/retry.py" in diff.item.snippet

    todo = by_kind[(CatalogKind.MARKER, "paypulse/retry.py", RETRY_BRANCH_NAME)].item
    assert todo.snippet.strip() == RETRY_ENGINE_TODO_MARKER
    assert (todo.line_start, todo.line_end) == (60, 60)
    assert todo.source_type is EvidenceSourceType.TODO_COMMENT and todo.age_days >= 90
    assert todo.commit_sha == paypulse_repo.branch_heads[RETRY_BRANCH_NAME]

    fixme = by_kind[(CatalogKind.MARKER, "paypulse/webhooks.py", "main")].item
    first = line_number(WEBHOOKS_PY_V2, "# FIXME: single attempt only.")
    assert (fixme.line_start, fixme.line_end) == (first, first + 1)
    assert fixme.snippet == "\n".join(WEBHOOKS_PY_V2.split("\n")[first - 1 : first + 1])
    assert fixme.commit_sha == paypulse_repo.head_sha and fixme.age_days == 0.0

    [conflict] = retries.conflicts
    assert conflict.claim_source == f'README.md:{line_number(README_V2, RETRIES)}: "[x] {RETRIES}"'
    assert f"exists only on unmerged branch {RETRY_BRANCH_NAME} (paypulse/retry.py, tests/test_retry.py)" in (
        conflict.reality_source
    )


@pytest.mark.slow
def test_paypulse_currency_feature_cites_the_stub_and_markers(paypulse_genome: ReconciledGenome) -> None:
    currency = feature_titled(paypulse_genome, CURRENCY)
    stub = next(c.item for c in currency.citations if c.item.kind is CatalogKind.STUB)
    line = line_number(CURRENCY_PY, "raise NotImplementedError")
    assert (stub.file_path, stub.line_start, stub.line_end) == ("paypulse/currency.py", line, line)
    assert stub.snippet == CURRENCY_PY.split("\n")[line - 1]
    markers = {c.item.marker_tag: c.item for c in currency.citations if c.item.kind is CatalogKind.MARKER}
    assert set(markers) == {"TODO", "FIXME"}
    todo_line = line_number(CURRENCY_PY, "# TODO: fetch daily FX rates")
    assert (markers["TODO"].line_start, markers["TODO"].snippet) == (todo_line, CURRENCY_PY.split("\n")[todo_line - 1])
    assert currency.conflicts[0].reality_source.startswith(f"convert at paypulse/currency.py:{line} on main")


@pytest.mark.slow
def test_paypulse_readme_claims_and_the_ghost_pdf_export(paypulse_genome: ReconciledGenome) -> None:
    readme_lines = README_V2.split("\n")
    for title in (LEDGER, INVOICES, CURRENCY, RETRIES, PDF):
        anchor = next(c.item for c in feature_titled(paypulse_genome, title).citations if c.match_kind is MatchKind.ANCHOR)
        number = line_number(README_V2, f"- [x] {title}")
        assert (anchor.file_path, anchor.line_start, anchor.line_end) == ("README.md", number, number)
        assert anchor.snippet == readme_lines[number - 1]
        assert anchor.source_type is EvidenceSourceType.README_CLAIM

    pdf = feature_titled(paypulse_genome, PDF)
    assert {(c.item.source_type, c.item.file_path) for c in pdf.citations} == {
        (EvidenceSourceType.README_CLAIM, "README.md"),
        (EvidenceSourceType.ISSUE_SPEC, "docs/architecture-notes.md"),
    }
    spec = next(c.item for c in pdf.citations if c.item.source_type is EvidenceSourceType.ISSUE_SPEC)
    assert spec.snippet.startswith("## PDF invoice export\n")
    [conflict] = pdf.conflicts
    assert conflict.reality_source.startswith("no implementation found on main at ")


@pytest.mark.slow
def test_paypulse_scores_and_offline_agent(paypulse_catalog: EvidenceCatalog, paypulse_genome: ReconciledGenome) -> None:
    pdf = feature_titled(paypulse_genome, PDF)
    # README claim + spec mention, conflicted: sigmoid(0.21 + 0.36 - 0.5 - 0.4), the 4.1 calibration value.
    assert pdf.confidence == round(1 / (1 + math.exp(-(0.21 + 0.36 - 0.9))), 4) == 0.4182
    result = GenomeReconcilerAgent(api_key="").synthesize(paypulse_catalog)
    assert result.mode is SynthesisMode.DETERMINISTIC and result.model_id == ""
    assert [f.key for f in result.features] == [f.key for f in paypulse_genome.features]
    assert len(result.input_digest) == 64
