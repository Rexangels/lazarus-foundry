"""Fast tests (no database) for ``apps.rebirth.builder`` on a small real Git repository.

The repository mirrors what a passed Foundry run leaves behind: a revival branch
``lazarus/restore-abcd1234`` with one committed merge (``merged.py``) and the
engineering edits uncommitted (``app.py`` modified, ``retry.py`` new). Its
``.gitignore`` ignores ``.kiro/``, so the exported specs must be force-added.
The DB side (``run_rebirth``, the endpoint) is in ``tests/test_rebirth_db.py``.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from apps.foundry.workspace import git, head_sha, read_text
from apps.projects.git_runner import run_git_command
from apps.rebirth.builder import (
    RebirthError,
    RebirthInputs,
    build_rebirth,
    pr_body_markdown,
    unified_diff,
    verification_facts,
)

BRANCH = "lazarus/restore-abcd1234"
SPEC_DIR = ".kiro/specs/lazarus-restore-toy"
SPEC_FILES = (f"{SPEC_DIR}/requirements.md", f"{SPEC_DIR}/design.md", f"{SPEC_DIR}/tasks.md")
FAILING_TEST = "tests/test_app.py::test_retry"


def commit_all(workspace: Path, message: str) -> str:
    git(workspace, "add", "-A")
    git(workspace, "commit", "-q", "--no-verify", "-m", message)
    return head_sha(workspace)


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, str]:
    return build_workspace(tmp_path)


def build_workspace(tmp_path: Path) -> tuple[Path, str]:
    """``(workspace, base_sha)`` in the state a passed Foundry run leaves."""
    root = tmp_path / "toy"
    (root / "tests").mkdir(parents=True)
    (root / ".gitignore").write_text(".kiro/\n__pycache__/\n", encoding="utf-8")
    (root / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_retry():\n    assert False\n", encoding="utf-8")
    run_git_command(["init", "-q", "-b", "main"], cwd=root)
    base = commit_all(root, "initial")
    git(root, "checkout", "-q", "-b", BRANCH)
    (root / "merged.py").write_text("MERGED = True\n", encoding="utf-8")
    commit_all(root, "merge feat/retry")
    (root / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (root / "retry.py").write_text("def delay_for(attempt):\n    return 2 ** attempt\n", encoding="utf-8")
    return root, base


def totals(tests: int, passed: int, failed: int = 0) -> dict[str, int]:
    return {"tests": tests, "passed": passed, "failed": failed, "errors": 0, "skipped": 0}


def inputs_for(base: str, **overrides: Any) -> RebirthInputs:
    report: dict[str, Any] = {
        "strategy_type": "RESTORE", "sandbox_mode": "subprocess", "max_healing_iterations": 3,
        "environment": {
            "base_sha": base, "python": {"actual": "3.13.5"},
            "sandbox": {"mode": "subprocess", "network_isolated": False},
            "install": {"resolution": "PINNED", "note": "the pinned requirements installed as written"},
            "baseline": {"exit_code": 1, "totals": totals(16, 15, 1), "failed": [FAILING_TEST]},
        },
        "architecture": {"merges": [{"item_id": "RESTORE-01", "branch": "feat/retry", "branch_head": "f" * 40,
                                     "merge_commit": "e" * 40, "files": ["merged.py"]}]},
        "verification": {"attempts": [{"iteration": 0, "passed": True, "full": {"exit_code": 0, "totals": totals(39, 39),
                                                                                "failed": []},
                                       "target_outcomes": {FAILING_TEST: "PASSED"}, "targets_run_separately": None}],
                         "healing": [], "iterations_used": 0},
        "failure": None, "revival_branch": BRANCH, "changed_files": ["app.py", "merged.py", "retry.py"],
    }
    fields: dict[str, Any] = {
        "project_id": "11111111-1111-4111-8111-111111111111", "project_name": "toy", "default_branch": "main",
        "foundry_run_id": "abcd1234-0000-4000-8000-000000000000", "report": report, "iterations_used": 0,
        "strategy_id": "22222222-2222-4222-8222-222222222222", "strategy_type": "RESTORE",
        "strategy_title": "Restore the retry engine", "strategy_summary": "Merge feat/retry and finish it.",
        "feasibility_score": 68.1, "approved_by": "dana", "approved_at": "2026-01-01T00:00:00+00:00",
        "action_plan": {
            "items": [
                {"id": "RESTORE-01", "kind": "MERGE_ABANDONED_BRANCH", "stage": "ARCHITECTURE",
                 "title": "Merge feat/retry into main", "branch": "feat/retry", "files_to_modify": [],
                 "files_to_create": ["merged.py"], "target_tests": [], "open_markers": ["merged.py:1: TODO jitter"]},
                {"id": "RESTORE-02", "kind": "FIX_FAILING_TEST", "stage": "ENGINEERING",
                 "title": "Make the retry test pass", "files_to_modify": ["app.py"], "files_to_create": ["retry.py"],
                 "target_tests": [FAILING_TEST]},
            ],
            "in_scope": [{"feature_id": "f1", "title": "Webhook retries", "vital_status": "ABANDONED_IN_PROGRESS"}],
            "deferred": [{"feature_id": "f2", "title": "PDF invoice export", "vital_status": "GHOST_FEATURE",
                          "reason": "NEEDS_NEW_DEPENDENCY", "detail": "needs a PDF rendering library",
                          "would_touch": ["invoices.py"], "external_requirements": [{"label": "a PDF library"}]}],
        },
        "kiro_specs": {"spec_name": "lazarus-restore-toy", "requirements_md": "# Requirements\n",
                       "design_md": "# Design\n", "tasks_md": "# Implementation Plan\n- [ ] 1. Merge\n"},
    }
    fields.update(overrides)
    return RebirthInputs(**fields)


def test_the_verified_tree_is_committed_with_the_specs_and_diffed_against_the_base(
    workspace: tuple[Path, str]
) -> None:
    root, base = workspace
    bundle = build_rebirth(root, inputs_for(base), generated_at="2026-01-02T00:00:00+00:00")

    assert git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() == BRANCH
    assert git(root, "status", "--porcelain").strip() == ""
    assert bundle.rebirth_commit_sha == head_sha(root) and bundle.base_sha == base
    assert bundle.committed_files == tuple(sorted(("app.py", "retry.py", *SPEC_FILES)))
    assert read_text(root, f"{SPEC_DIR}/tasks.md") == "# Implementation Plan\n- [ ] 1. Merge\n"
    assert bundle.exported_kiro_specs_path == SPEC_DIR and bundle.spec_files == SPEC_FILES

    assert {f.path: f.status for f in bundle.files} == {
        "app.py": "M", "merged.py": "A", "retry.py": "A", **{path: "A" for path in SPEC_FILES}}
    assert bundle.unified_diff_patch == git(root, "diff", "--no-renames", base, "HEAD")
    assert "+    return 42" in bundle.unified_diff_patch and "+++ b/retry.py" in bundle.unified_diff_patch
    assert not bundle.diff_truncated
    # The patch applies to the base commit.
    git(root, "checkout", "-q", "--detach", base)
    (root / "rebirth.patch").write_text(bundle.unified_diff_patch, encoding="utf-8")
    git(root, "apply", "--check", "rebirth.patch")


def test_the_continuity_memory_is_json_with_the_roadmap(workspace: tuple[Path, str]) -> None:
    root, base = workspace
    memory = build_rebirth(root, inputs_for(base)).continuity_memory

    assert json.loads(json.dumps(memory, allow_nan=False)) == memory
    assert memory["version"] == "lazarus-continuity-v1"
    assert memory["revival"]["base_sha"] == base and memory["revival"]["revival_branch"] == BRANCH
    assert memory["revival"]["spec_files"] == list(SPEC_FILES)
    assert memory["verification"]["before"] == totals(16, 15, 1)
    assert memory["verification"]["after"] == totals(39, 39)
    assert memory["verification"]["tests_fixed"] == [FAILING_TEST]
    assert [item["item_id"] for item in memory["completed"]] == ["RESTORE-01", "RESTORE-02"]
    assert memory["merged_branches"][0]["branch"] == "feat/retry"
    [step] = memory["roadmap"]
    assert step["title"] == "PDF invoice export" and step["status"] == "DEFERRED"
    assert step["external_requirements"] == ["a PDF library"] and step["would_touch"] == ["invoices.py"]
    assert memory["open_markers"] == ["merged.py:1: TODO jitter"] and memory["uncommitted_files"] == []


def test_the_pr_summary_reports_before_and_after(workspace: tuple[Path, str]) -> None:
    root, base = workspace
    bundle = build_rebirth(root, inputs_for(base))
    body = bundle.pr_body_markdown

    assert bundle.pr_title == "Lazarus RESTORE: Restore the retry engine"
    assert "| Before (main) | 16 | 15 | 1 | 0 | 0 |" in body
    assert f"| After ({BRANCH}) | 39 | 39 | 0 | 0 | 0 |" in body
    assert "Before: 15 of 16 tests passed, 1 failing. After: 39 of 39 passed, all passing." in body
    assert f"- `{FAILING_TEST}`" in body and "Self-healing iterations used: 0 of 3." in body
    assert f"## Files changed (6 files, +{bundle.additions} -{bundle.deletions})" in body
    assert "| `retry.py` | added |" in body and f"- `{SPEC_DIR}/design.md`" in body
    assert "**PDF invoice export** (NEEDS_NEW_DEPENDENCY): needs a PDF rendering library" in body


def test_building_twice_commits_nothing_new(workspace: tuple[Path, str]) -> None:
    root, base = workspace
    first = build_rebirth(root, inputs_for(base))
    second = build_rebirth(root, inputs_for(base))
    assert second.rebirth_commit_sha == first.rebirth_commit_sha and second.committed_files == ()
    assert second.unified_diff_patch == first.unified_diff_patch


def test_a_file_changed_outside_the_run_is_refused_and_unstaged(workspace: tuple[Path, str]) -> None:
    root, base = workspace
    before = head_sha(root)
    (root / "stray.txt").write_text("not verified\n", encoding="utf-8")
    with pytest.raises(RebirthError) as raised:
        build_rebirth(root, inputs_for(base))
    assert raised.value.code == "UNEXPECTED_CHANGES" and "stray.txt" in raised.value.detail
    assert head_sha(root) == before and git(root, "diff", "--cached", "--name-only").strip() == ""


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda r: r.update(failure={"stage": "VERIFICATION", "code": "TESTS_FAILING", "detail": "x"}),
         "RUN_NOT_VERIFIED"),
        (lambda r: r["verification"]["attempts"][-1].update(passed=False), "RUN_NOT_VERIFIED"),
        (lambda r: r.update(revival_branch=""), "RUN_INCOMPLETE"),
        (lambda r: r["environment"].update(base_sha="not-a-sha"), "RUN_INCOMPLETE"),
        (lambda r: r.update(revival_branch="lazarus/other-1"), "WORKSPACE_NOT_ON_REVIVAL_BRANCH"),
    ],
    ids=["failed", "not-passed", "no-branch", "no-base", "other-branch"],
)
def test_a_run_that_is_not_verified_or_incomplete_is_refused(workspace: tuple[Path, str], change: Any,
                                                             code: str) -> None:
    root, base = workspace
    inputs = inputs_for(base)
    report = copy.deepcopy(dict(inputs.report))
    change(report)
    with pytest.raises(RebirthError) as raised:
        build_rebirth(root, inputs_for(base, report=report))
    assert raised.value.code == code
    assert not (root / ".kiro").exists()


@pytest.mark.parametrize("name", ["../escape", "Lazarus Restore", "", "a/b"])
def test_an_invalid_spec_name_writes_nothing(workspace: tuple[Path, str], name: str) -> None:
    root, base = workspace
    specs = {**inputs_for(base).kiro_specs, "spec_name": name}
    with pytest.raises(RebirthError) as raised:
        build_rebirth(root, inputs_for(base, kiro_specs=specs))
    assert raised.value.code == "SPEC_INVALID" and not (root / ".kiro").exists()


def test_untrusted_titles_are_escaped_in_the_pr_body(workspace: tuple[Path, str]) -> None:
    _, base = workspace
    inputs = inputs_for(base, strategy_title="<script>alert(1)</script> | `x`")
    body = pr_body_markdown(inputs, verification_facts(inputs.report), base_sha=base, head="d" * 40,
                            revival_branch=BRANCH, spec_files=SPEC_FILES, files=(), diff_truncated=False)
    assert "<script>" not in body and "\\<script\\>" in body and "\\|" in body


def test_a_long_diff_is_cut_at_a_file_boundary(workspace: tuple[Path, str]) -> None:
    root, base = workspace
    head = build_rebirth(root, inputs_for(base)).rebirth_commit_sha
    patch, truncated = unified_diff(root, base, head, limit=200)
    assert truncated and "[lazarus] diff truncated at 200 characters" in patch
    assert patch.count("diff --git ") <= 1


def test_openapi_documents_the_rebirth_route(client: Client) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]
    assert {"200", "404", "409"} <= set(paths["/api/projects/{project_id}/rebirth"]["get"]["responses"])
