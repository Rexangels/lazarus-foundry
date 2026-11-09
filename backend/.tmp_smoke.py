import json, os, sys, tempfile, time
from pathlib import Path
from uuid import UUID, uuid4
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.test import override_settings
from apps.genome.agents import GenomeReconcilerAgent
from apps.genome.catalog import catalog_from_rows
from apps.genome.graph import ProjectInput, inputs_from_features
from apps.ingestion.git_forensics import clone_local_repository_into_workspace
from apps.ingestion.pipeline import run_ingestion
from apps.revival.planner import dependencies_from_artifacts, import_graph_from_artifacts, plan_strategies
from apps.revival.services import action_plan_json
from apps.projects.demo.paypulse import build_paypulse_fixture
from apps.projects.models import Project
from apps.foundry.agents import FoundryContext, FoundryPipeline, EnvironmentAgent
from config.paths import REPO_ROOT

network = "--network" in sys.argv
kind = next((a for a in sys.argv[1:] if not a.startswith("--")), "RESTORE")
tmp = Path(tempfile.mkdtemp())
repo = build_paypulse_fixture(tmp / "src" / "paypulse_dead_repo")
root = tmp / "ws"; root.mkdir()
with override_settings(WORKSPACE_ROOT=root, SANDBOX_RUNS_ROOT=tmp / "runs", MODEL_API_KEY=""):
    ws = clone_local_repository_into_workspace(repo.repo_path, root / "paypulse")
    ing = run_ingestion(ws, emit=lambda e, p: None)
    res = GenomeReconcilerAgent(api_key="").synthesize(catalog_from_rows(ing.artifacts, ing.branches))
    proj = ProjectInput(UUID("00000000-0000-4000-8000-0000000000aa"), "PayPulse", "AUTOPSY_COMPLETE", "", ing.default_branch)
    genome = inputs_from_features(proj, res.features, ing.branches)
    plans = {p.strategy_type: p for p in plan_strategies(genome, dependencies=dependencies_from_artifacts(ing.artifacts, ing.default_branch),
                                                         imports=import_graph_from_artifacts(ing.artifacts, ing.default_branch))}
    plan = action_plan_json(plans[kind])
    project = Project(id=uuid4(), name="PayPulse", is_golden_demo=True, workspace_path=str(ws), default_branch="main")
    events = []
    env = EnvironmentAgent(install_network=network, wheelhouse=REPO_ROOT / ".lazarus_data" / "wheelhouse")
    pipe = FoundryPipeline(environment=env, emit=lambda e, p: events.append((str(e), p)))
    ctx = FoundryContext.from_action_plan(project, str(uuid4()), kind, plan, sandbox_mode="subprocess")
    t = time.time()
    result = pipe.run(ctx, before_stage=lambda s: print("STAGE", s))
    print("elapsed", round(time.time() - t, 1))
    print("succeeded", result.succeeded, result.failure, "iterations", result.iterations_used, "branch", result.revival_branch)
    r = result.report
    print("python", json.dumps(r.get("environment", {}).get("python")))
    print("install", r.get("environment", {}).get("install", {}).get("resolution"))
    print("baseline", json.dumps(r.get("environment", {}).get("baseline")))
    print("engineering", json.dumps([(i["item_id"], i["status"], i["mode"]) for i in r.get("engineering", {}).get("items", [])]))
    for a in r["verification"]["attempts"]:
        print("attempt", json.dumps({k: a[k] for k in ("iteration", "passed", "reasons", "target_outcomes")}), json.dumps(a["full"]["totals"]))
    print("changed", r.get("changed_files"))
    print("events", [e for e, _ in events if e != "SANDBOX_LOG"], sum(1 for e, _ in events if e == "SANDBOX_LOG"))
    print("--- BEFORE (tail) ---\n" + result.before_test_output[-600:])
    print("--- AFTER (tail) ---\n" + result.after_test_output[-600:])
