import json, os, sys, tempfile
from pathlib import Path
from uuid import UUID
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

tmp = Path(tempfile.mkdtemp())
repo = build_paypulse_fixture(tmp / "src" / "paypulse_dead_repo")
root = tmp / "ws"; root.mkdir()
with override_settings(WORKSPACE_ROOT=root):
    ws = clone_local_repository_into_workspace(repo.repo_path, root / "paypulse")
ing = run_ingestion(ws, emit=lambda e, p: None)
res = GenomeReconcilerAgent(api_key="").synthesize(catalog_from_rows(ing.artifacts, ing.branches))
proj = ProjectInput(UUID("00000000-0000-4000-8000-0000000000aa"), "PayPulse", "AUTOPSY_COMPLETE", "", ing.default_branch)
genome = inputs_from_features(proj, res.features, ing.branches)
plans = plan_strategies(genome, dependencies=dependencies_from_artifacts(ing.artifacts, ing.default_branch),
                        imports=import_graph_from_artifacts(ing.artifacts, ing.default_branch))
out = {p.strategy_type: action_plan_json(p) for p in plans}
Path(sys.argv[1]).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
