import os, sys, tempfile
from pathlib import Path
from uuid import uuid4
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.test import override_settings
from apps.ingestion.git_forensics import clone_local_repository_into_workspace
from apps.projects.demo.paypulse import build_paypulse_fixture
from apps.projects.models import Project
from apps.foundry.sandbox import get_sandbox
from apps.foundry.workspace import copy_file
from config.paths import REPO_ROOT

tmp = Path(tempfile.mkdtemp())
repo = build_paypulse_fixture(tmp / "src" / "paypulse_dead_repo")
root = tmp / "ws"; root.mkdir()
with override_settings(WORKSPACE_ROOT=root, SANDBOX_RUNS_ROOT=tmp / "runs"):
    ws = clone_local_repository_into_workspace(repo.repo_path, root / "paypulse")
    x = "extracted/paypulse-ledger-invoices"
    for f in ["paypulse/__init__.py", "paypulse/ledger.py", "tests/__init__.py", "tests/test_ledger.py"]:
        copy_file(ws, f, f"{x}/{f}")
    project = Project(id=uuid4(), name="P", is_golden_demo=True, workspace_path=str(ws))
    with get_sandbox(project, str(uuid4()), python_version="3.13", mode="subprocess") as box:
        box.create_environment()
        box.install(packages=["pytest"], network=False, find_links=REPO_ROOT / ".lazarus_data" / "wheelhouse")
        r = box.run_pytest([f"{x}/tests/test_ledger.py"])
        print(r.exit_code, r.junit_error, [c.nodeid for c in r.cases][:3])
        print(r.execution.stdout[-1500:])
        print((box.results_dir / "junit.xml").read_text()[:800] if (box.results_dir / "junit.xml").exists() else "no junit")
