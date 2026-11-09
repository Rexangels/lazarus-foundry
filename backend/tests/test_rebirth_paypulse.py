"""End to end on PayPulse (slow; no database): RESTORE plan -> 4-agent Foundry -> rebirth bundle.

A private clone of the fixture is ingested, the deterministic genome and the
RESTORE plan and spec bundle are built, the Foundry runs in subprocess mode
offline (pytest from the local wheelhouse, ``LAZARUS_SANDBOX_WHEELHOUSE``,
default ``.lazarus_data/wheelhouse``) on the deterministic engineering path,
and ``build_rebirth`` turns the verified workspace into the rebirth bundle.
Run with ``-s`` to see the evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from django.test import override_settings
from pytest_django import Settings as SettingsWrapper

from apps.foundry.agents import EnvironmentAgent, FoundryContext, FoundryPipeline
from apps.foundry.workspace import current_branch, git, is_clean, read_text
from apps.genome.agents import GenomeReconcilerAgent
from apps.genome.catalog import catalog_from_rows
from apps.genome.graph import ProjectInput, inputs_from_features
from apps.ingestion.git_forensics import clone_local_repository_into_workspace
from apps.ingestion.pipeline import run_ingestion
from apps.projects.models import Project
from apps.rebirth.builder import RebirthInputs, build_rebirth
from apps.revival.models import StrategyType
from apps.revival.planner import dependencies_from_artifacts, import_graph_from_artifacts, plan_strategies
from apps.revival.scoring import score_plan
from apps.revival.services import action_plan_json
from apps.revival.specs import render_spec_bundle, validate_spec_bundle
from tests.fixtures.build_paypulse_fixture import FixtureBuildResult
from tests.test_foundry_sandbox_paypulse import WHEELHOUSE

pytestmark = pytest.mark.slow

CHANGED = {"paypulse/currency.py", "paypulse/retry.py", "paypulse/webhooks.py", "tests/test_currency.py",
           "tests/test_retry.py"}
SPEC_DIR = ".kiro/specs/lazarus-restore-paypulse"


def test_paypulse_restore_is_reborn(paypulse_repo: FixtureBuildResult, tmp_path: Path,
                                    settings: SettingsWrapper) -> None:
    if not any(WHEELHOUSE.glob("pytest-*.whl")):
        pytest.skip(f"no pytest wheel in {WHEELHOUSE}")
    root = tmp_path / "workspaces"
    root.mkdir()
    settings.WORKSPACE_ROOT = root
    settings.SANDBOX_RUNS_ROOT = tmp_path / "runs"
    settings.SANDBOX_MODE = "subprocess"
    settings.SANDBOX_COMMAND_TIMEOUT_SECONDS = 300
    settings.SANDBOX_INSTALL_TIMEOUT_SECONDS = 600
    with override_settings(WORKSPACE_ROOT=root):
        workspace = clone_local_repository_into_workspace(paypulse_repo.repo_path, root / "paypulse")

    ingestion = run_ingestion(workspace, emit=lambda event, payload: None)
    genome_result = GenomeReconcilerAgent(api_key="").synthesize(catalog_from_rows(ingestion.artifacts, ingestion.branches))
    project_id = uuid4()
    genome = inputs_from_features(ProjectInput(project_id, "PayPulse", "AUTOPSY_COMPLETE", "", ingestion.default_branch),
                                  genome_result.features, ingestion.branches)
    plans = {plan.strategy_type: plan for plan in plan_strategies(
        genome, dependencies=dependencies_from_artifacts(ingestion.artifacts, ingestion.default_branch),
        imports=import_graph_from_artifacts(ingestion.artifacts, ingestion.default_branch))}
    plan = plans[StrategyType.RESTORE]
    score = score_plan(plan)
    specs = validate_spec_bundle(render_spec_bundle(plan, score, genome)).as_storage(
        mode="DETERMINISTIC", generated_at="2026-01-01T00:00:00+00:00")
    action_plan = action_plan_json(plan)

    project = Project(id=project_id, name="PayPulse", is_golden_demo=True, workspace_path=str(workspace),
                      default_branch=ingestion.default_branch)
    run_id = str(uuid4())
    pipeline = FoundryPipeline(environment=EnvironmentAgent(install_network=False, wheelhouse=WHEELHOUSE))
    result = pipeline.run(FoundryContext.from_action_plan(project, run_id, "RESTORE", action_plan,
                                                          sandbox_mode="subprocess"))
    assert result.succeeded and result.tests_passed, result.failure

    bundle = build_rebirth(workspace, RebirthInputs(
        project_id=str(project_id), project_name="PayPulse", default_branch=ingestion.default_branch,
        foundry_run_id=run_id, report=result.report, iterations_used=result.iterations_used,
        strategy_id=str(uuid4()), strategy_type="RESTORE", strategy_title=plan.title, strategy_summary=plan.summary,
        feasibility_score=score.feasibility_score,
        action_plan=action_plan, kiro_specs=specs, approved_by="e2e",
    ))

    paths = {f.path for f in bundle.files}
    memory = bundle.continuity_memory
    print(  # noqa: T201 - evidence, shown with -s
        f"\nrevival branch: {bundle.revival_branch} base={bundle.base_sha[:12]} commit={bundle.rebirth_commit_sha[:12]}"
        f"\ndiff files: {[(f.path, f.status, f.additions, f.deletions) for f in bundle.files]}"
        f"\ndiff: {len(bundle.unified_diff_patch)} chars, {bundle.unified_diff_patch.count('diff --git ')} file sections"
        f"\nspecs written: {[(p, len(read_text(workspace, p) or '')) for p in bundle.spec_files]}"
        f"\ncontinuity memory: {len(json.dumps(memory))} bytes, keys={sorted(memory)}"
        f"\n  verification={json.dumps(memory['verification']['before'])} -> "
        f"{json.dumps(memory['verification']['after'])}, fixed={memory['verification']['tests_fixed']}"
        f"\n  roadmap={[(s['title'], s['reason']) for s in memory['roadmap']]}"
        f"\nPR title: {bundle.pr_title}\n--- PR body ---\n{bundle.pr_body_markdown}"
    )
    assert paths == CHANGED | set(bundle.spec_files)
    assert bundle.exported_kiro_specs_path == SPEC_DIR
    assert bundle.spec_files == tuple(f"{SPEC_DIR}/{doc}" for doc in ("requirements.md", "design.md", "tasks.md"))
    for path, key in zip(bundle.spec_files, ("requirements_md", "design_md", "tasks_md"), strict=True):
        assert read_text(workspace, path) == specs[key]
    assert bundle.unified_diff_patch == git(workspace, "diff", "--no-renames", bundle.base_sha, "HEAD")
    assert current_branch(workspace) == bundle.revival_branch and is_clean(workspace)
    assert json.loads(json.dumps(memory, allow_nan=False)) == memory
    assert memory["verification"]["before"] == {"tests": 16, "passed": 15, "failed": 1, "errors": 0, "skipped": 0}
    assert memory["verification"]["after"]["tests"] == memory["verification"]["after"]["passed"] == 39
    body = bundle.pr_body_markdown
    assert f"| Before ({ingestion.default_branch}) | 16 | 15 | 1 | 0 | 0 |" in body
    assert f"| After ({bundle.revival_branch}) | 39 | 39 | 0 | 0 | 0 |" in body
    assert "Before: 15 of 16 tests passed, 1 failing. After: 39 of 39 passed, all passing." in body
