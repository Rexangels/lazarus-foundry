"""The PayPulse RESTORE recovery spec on the real pipeline (slow: builds and clones the Git fixture).

``run_ingestion`` on a clone, catalog, deterministic genome, ``inputs_from_features``,
``plan_strategies``, ``score_plan``, then ``render_spec_bundle``. Run with ``-s`` to see the
RESTORE ``tasks.md``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.test import override_settings

from apps.genome.agents import GenomeReconcilerAgent
from apps.genome.catalog import catalog_from_rows
from apps.genome.graph import GenomeInputs, ProjectInput, inputs_from_features
from apps.ingestion.git_forensics import clone_local_repository_into_workspace
from apps.ingestion.pipeline import run_ingestion
from apps.revival.models import StrategyType
from apps.revival.planner import dependencies_from_artifacts, import_graph_from_artifacts, plan_strategies
from apps.revival.scoring import score_plan
from apps.revival.specs import SpecBundle, render_spec_bundle, validate_spec_bundle
from tests.fixtures.build_paypulse_fixture import RETRY_BRANCH_NAME, FixtureBuildResult
from tests.test_revival_planner import CURRENCY, PDF, PROJECT, RETRIES

pytestmark = pytest.mark.slow

TARGET_TEST = "tests/test_webhooks.py::test_dispatch_retries_with_exponential_backoff"


@pytest.fixture(scope="module")
def paypulse_bundles(
    paypulse_repo: FixtureBuildResult, tmp_path_factory: pytest.TempPathFactory
) -> dict[StrategyType, SpecBundle]:
    root = tmp_path_factory.mktemp("revival_spec_workspaces")
    with override_settings(WORKSPACE_ROOT=root):
        workspace: Path = clone_local_repository_into_workspace(paypulse_repo.repo_path, root / "paypulse")
    ingestion = run_ingestion(workspace, emit=lambda event, payload: None)
    result = GenomeReconcilerAgent(api_key="").synthesize(catalog_from_rows(ingestion.artifacts, ingestion.branches))
    project = ProjectInput(PROJECT.id, "PayPulse", "AUTOPSY_COMPLETE", "", ingestion.default_branch)
    genome: GenomeInputs = inputs_from_features(project, result.features, ingestion.branches)
    planned = plan_strategies(
        genome,
        dependencies=dependencies_from_artifacts(ingestion.artifacts, ingestion.default_branch),
        imports=import_graph_from_artifacts(ingestion.artifacts, ingestion.default_branch),
    )
    return {plan.strategy_type: render_spec_bundle(plan, score_plan(plan), genome) for plan in planned}


def test_paypulse_restore_spec(paypulse_bundles: dict[StrategyType, SpecBundle]) -> None:
    restore = validate_spec_bundle(paypulse_bundles[StrategyType.RESTORE])
    print("\n" + restore.tasks_md)
    assert restore.spec_name == "lazarus-restore-paypulse"

    titles = re.findall(r"^### Requirement \d+: (.+)$", restore.requirements_md, flags=re.M)
    assert len(titles) == 3 and set(titles) == {RETRIES, CURRENCY, "Verification"} and titles[-1] == "Verification"
    assert f"THE test `{TARGET_TEST}` SHALL pass without being edited" in restore.requirements_md

    tasks = dict(re.findall(r"^- \[ \] (\d+)\. (.+)$", restore.tasks_md, flags=re.M))
    assert f"Merge {RETRY_BRANCH_NAME} into main" in tasks.values()
    assert "Complete delay_for in paypulse/retry.py" in tasks.values()
    assert f"Make {TARGET_TEST} pass" in tasks.values()
    assert "Wire the implementation into `paypulse/webhooks.py`" in restore.tasks_md
    assert f"Target tests: `{TARGET_TEST}`" in restore.tasks_md
    order = {title: int(number) for number, title in tasks.items()}
    assert order[f"Merge {RETRY_BRANCH_NAME} into main"] < order["Complete delay_for in paypulse/retry.py"] \
        < order[f"Make {TARGET_TEST} pass"]

    out_of_scope = restore.design_md.split("## Out of Scope", 1)[1]
    assert f"**{PDF}** (GHOST_FEATURE): NEEDS_NEW_DEPENDENCY." in out_of_scope
    assert "a PDF rendering library" in out_of_scope and "a QR code library" in out_of_scope
    assert "**Multi-currency conversion" in out_of_scope and "Partial deferral" in out_of_scope


def test_paypulse_modernize_and_extract_specs_validate(paypulse_bundles: dict[StrategyType, SpecBundle]) -> None:
    for kind in (StrategyType.MODERNIZE, StrategyType.EXTRACT):
        validate_spec_bundle(paypulse_bundles[kind])
    assert "### Requirement 3: Current dependencies" in paypulse_bundles[StrategyType.MODERNIZE].requirements_md
    assert "`extracted/paypulse-ledger-invoices`" in paypulse_bundles[StrategyType.EXTRACT].requirements_md
