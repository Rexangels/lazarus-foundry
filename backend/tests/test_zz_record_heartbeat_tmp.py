"""TEMPORARY (Task 8.4 data recording; delete after use): record the PayPulse RESTORE rebirth for the mock.

Same chain as test_rebirth_paypulse.py, with the mock's ids, an emit callback that records the
Foundry telemetry, and a JSON dump into frontend/src/lib/mock/.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from django.test import override_settings
from pytest_django import Settings as SettingsWrapper

from apps.foundry.agents import EnvironmentAgent, FoundryContext, FoundryPipeline
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

FRONTEND_MOCK = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "mock"
MOCK_PROJECT_ID = "0b5e7d1a-6c2f-4e8a-9f3d-2a1c0de0a001"
MOCK_PROJECT_NAME = "PayPulse (Patient Zero)"
MOCK_STRATEGY_ID = "5a7c0e2b-8d4f-4a1b-9c3e-6f2d1b0a0101"
MOCK_RUN_ID = "7e1d2c3b-4a5f-4e6d-8c7b-9a0f1e2d3c4b"
MOCK_ARTIFACT_ID = "d4f6a8c0-2e1b-4c3d-9a5e-8b7c6d5e4f01"
APPROVED_AT = "2026-01-01T00:01:00+00:00"
CREATED_AT = "2026-01-01T00:01:31+00:00"
LINES_PER_BATCH = 12
HEAD_LINES = 45
TAIL_LINES = 15


def _feature_id_map(action_plan: dict[str, Any]) -> dict[str, str]:
    recorded = json.loads((FRONTEND_MOCK / "paypulse-strategies.json").read_text(encoding="utf-8"))
    restore = next(s for s in recorded if s["strategy_type"] == "RESTORE")
    mock_by_title = {e["title"]: e["feature_id"] for key in ("in_scope", "deferred")
                     for e in restore["action_plan"][key] if e.get("feature_id")}
    mapping: dict[str, str] = {}
    for key in ("in_scope", "deferred"):
        for entry in action_plan[key]:
            fid, title = entry.get("feature_id"), entry.get("title")
            if fid and title in mock_by_title:
                mapping[str(fid)] = mock_by_title[title]
    return mapping


def _scrub(value: Any, replacements: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        for old, new in replacements:
            value = re.sub(re.escape(old), lambda _m, new=new: new, value, flags=re.IGNORECASE)
        return value
    if isinstance(value, list):
        return [_scrub(v, replacements) for v in value]
    if isinstance(value, dict):
        return {_scrub(k, replacements): _scrub(v, replacements) for k, v in value.items()}
    return value


def _trim_logs(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group the SANDBOX_LOG lines of each command, keep the head and tail of long ones, re-batch them."""
    out: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        lines = pending["lines"]
        if len(lines) > HEAD_LINES + TAIL_LINES:
            lines = lines[:HEAD_LINES] + lines[-TAIL_LINES:]
        for start in range(0, len(lines), LINES_PER_BATCH):
            out.append({"event_type": "SANDBOX_LOG", "offset_ms": pending["offset_ms"],
                        "payload": {**pending["payload"], "lines": lines[start:start + LINES_PER_BATCH]}})
        pending = None

    for frame in frames:
        if frame["event_type"] == "SANDBOX_LOG":
            payload = frame["payload"]
            if pending is not None and pending["payload"]["command"] == payload["command"]:
                pending["lines"].extend(payload["lines"])
                continue
            flush()
            pending = {"offset_ms": frame["offset_ms"], "lines": list(payload["lines"]),
                       "payload": {k: v for k, v in payload.items() if k != "lines"}}
        else:
            flush()
            out.append(frame)
    flush()
    return out


def test_record_heartbeat(paypulse_repo: FixtureBuildResult, tmp_path: Path, settings: SettingsWrapper) -> None:
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
    genome = inputs_from_features(ProjectInput(MOCK_PROJECT_ID, MOCK_PROJECT_NAME, "AUTOPSY_COMPLETE", "",
                                               ingestion.default_branch),
                                  genome_result.features, ingestion.branches)
    plans = {plan.strategy_type: plan for plan in plan_strategies(
        genome, dependencies=dependencies_from_artifacts(ingestion.artifacts, ingestion.default_branch),
        imports=import_graph_from_artifacts(ingestion.artifacts, ingestion.default_branch))}
    plan = plans[StrategyType.RESTORE]
    score = score_plan(plan)
    specs = validate_spec_bundle(render_spec_bundle(plan, score, genome)).as_storage(
        mode="DETERMINISTIC", generated_at="2026-01-01T00:00:00+00:00")
    action_plan = action_plan_json(plan)

    frames: list[dict[str, Any]] = []
    started = time.monotonic()

    def emit(event: Any, payload: dict[str, Any]) -> None:
        frames.append({"event_type": str(getattr(event, "value", event)),
                       "offset_ms": round((time.monotonic() - started) * 1000),
                       "payload": json.loads(json.dumps(payload, default=str))})

    project = Project(id=MOCK_PROJECT_ID, name=MOCK_PROJECT_NAME, is_golden_demo=True, workspace_path=str(workspace),
                      default_branch=ingestion.default_branch)
    pipeline = FoundryPipeline(environment=EnvironmentAgent(install_network=False, wheelhouse=WHEELHOUSE), emit=emit)
    result = pipeline.run(FoundryContext.from_action_plan(project, MOCK_RUN_ID, "RESTORE", action_plan,
                                                          sandbox_mode="subprocess"))
    assert result.succeeded and result.tests_passed, result.failure

    bundle = build_rebirth(workspace, RebirthInputs(
        project_id=MOCK_PROJECT_ID, project_name=MOCK_PROJECT_NAME, default_branch=ingestion.default_branch,
        foundry_run_id=MOCK_RUN_ID, report=result.report, iterations_used=result.iterations_used,
        strategy_id=MOCK_STRATEGY_ID, strategy_type="RESTORE", strategy_title=plan.title,
        strategy_summary=plan.summary, feasibility_score=score.feasibility_score,
        action_plan=action_plan, kiro_specs=specs, approved_by="demo", approved_at=APPROVED_AT,
    ), generated_at=CREATED_AT)
    verification = bundle.continuity_memory.get("verification") or {}
    emit("REBIRTH_COMPLETE", {
        "rebirth_artifact_id": MOCK_ARTIFACT_ID, "foundry_run_id": MOCK_RUN_ID, "pr_title": bundle.pr_title,
        "revival_branch": bundle.revival_branch, "base_sha": bundle.base_sha,
        "rebirth_commit_sha": bundle.rebirth_commit_sha, "files_changed": len(bundle.files),
        "additions": bundle.additions, "deletions": bundle.deletions, "diff_truncated": bundle.diff_truncated,
        "exported_kiro_specs_path": bundle.exported_kiro_specs_path, "spec_files": list(bundle.spec_files),
        "before": verification.get("before"), "after": verification.get("after"), "preview_url": "",
    })

    rebirth = {
        "id": MOCK_ARTIFACT_ID,
        "project": {"id": MOCK_PROJECT_ID, "name": MOCK_PROJECT_NAME, "status": "REBORN", "status_detail": "",
                    "default_branch": ingestion.default_branch},
        "foundry_run_id": MOCK_RUN_ID, "strategy_id": MOCK_STRATEGY_ID, "strategy_type": "RESTORE",
        "revival_branch": bundle.revival_branch, "base_sha": bundle.base_sha,
        "rebirth_commit_sha": bundle.rebirth_commit_sha, "pr_title": bundle.pr_title,
        "pr_body_markdown": bundle.pr_body_markdown, "unified_diff_patch": bundle.unified_diff_patch,
        "files": [f.as_dict() for f in bundle.files], "exported_kiro_specs_path": bundle.exported_kiro_specs_path,
        "spec_files": list(bundle.spec_files), "continuity_memory": bundle.continuity_memory, "preview_url": "",
        "before_test_output": result.before_test_output, "after_test_output": result.after_test_output,
        "tests_passed": result.tests_passed, "iterations_used": result.iterations_used, "created_at": CREATED_AT,
        "telemetry_ws_path": f"/ws/projects/{MOCK_PROJECT_ID}/telemetry/",
    }

    targets: dict[str, str] = {}
    for base, label in ((Path.home(), "~"), (WHEELHOUSE, "<wheelhouse>"), (tmp_path, "<tmp>")):
        for form in (str(base), base.as_posix(), str(base.resolve()), base.resolve().as_posix()):
            targets[form] = label
    replacements: list[tuple[str, str]] = sorted(targets.items(), key=lambda kv: len(kv[0]), reverse=True)
    replacements += list(_feature_id_map(action_plan).items())
    rebirth = _scrub(rebirth, replacements)
    timeline = _scrub(_trim_logs(frames), replacements)

    (FRONTEND_MOCK / "paypulse-rebirth.json").write_text(json.dumps(rebirth, indent=1, ensure_ascii=False) + "\n",
                                                          encoding="utf-8", newline="\n")
    (FRONTEND_MOCK / "paypulse-foundry.json").write_text(json.dumps(timeline, indent=1, ensure_ascii=False) + "\n",
                                                          encoding="utf-8", newline="\n")
    print(  # noqa: T201
        f"\nRECORDED files={len(bundle.files)} +{bundle.additions} -{bundle.deletions} "
        f"before={verification.get('before')} after={verification.get('after')} "
        f"iterations={result.iterations_used}/{result.report.get('max_healing_iterations')} "
        f"frames={len(frames)} timeline={len(timeline)} "
        f"log_lines={sum(len(f['payload']['lines']) for f in timeline if f['event_type'] == 'SANDBOX_LOG')} "
        f"patch_chars={len(bundle.unified_diff_patch)} total_ms={frames[-1]['offset_ms']}"
    )
