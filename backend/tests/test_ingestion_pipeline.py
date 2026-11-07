"""Tests for apps.ingestion.pipeline on a PayPulse clone (no database, no network).

These clone the session PayPulse fixture into a temporary WORKSPACE_ROOT, so
they are marked ``slow``.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from django.test import override_settings

from apps.ingestion.git_forensics import clone_local_repository_into_workspace
from apps.ingestion.models import ArtifactType
from apps.ingestion.pipeline import (
    AST_PARSED_BATCH_SIZE,
    IngestionResult,
    completion_payload,
    run_ingestion,
)
from apps.projects.git_runner import run_git
from apps.telemetry.events import TelemetryEventType
from tests.fixtures.build_paypulse_fixture import (
    DEFAULT_BRANCH,
    RETRY_BRANCH_BASE_INDEX,
    RETRY_BRANCH_COMMITS,
    RETRY_BRANCH_NAME,
    RETRY_ENGINE_TODO_MARKER,
    FixtureBuildResult,
)

pytestmark = pytest.mark.slow

Events = list[tuple[TelemetryEventType, dict[str, Any]]]
TELEMETRY_FILE_KEYS: frozenset[str] = frozenset({"path", "status", "language", "symbols", "markers"})


def line_of(text: str, needle: str) -> int:
    return next(number for number, line in enumerate(text.split("\n"), start=1) if needle in line)


def last_version(path: str) -> str:
    return next(commit.writes[path] for commit in reversed(RETRY_BRANCH_COMMITS) if path in commit.writes)


@pytest.fixture(scope="module")
def paypulse_workspace(paypulse_repo: FixtureBuildResult, tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("pipeline_workspaces")
    with override_settings(WORKSPACE_ROOT=root):
        return clone_local_repository_into_workspace(paypulse_repo.repo_path, root / "paypulse")


@pytest.fixture(scope="module")
def ingestion(paypulse_workspace: Path) -> tuple[IngestionResult, Events]:
    events: Events = []
    result = run_ingestion(paypulse_workspace, emit=lambda event, payload: events.append((event, payload)))
    return result, events


@pytest.fixture(scope="module")
def result(ingestion: tuple[IngestionResult, Events]) -> IngestionResult:
    return ingestion[0]


def main_python_files(paypulse_repo: FixtureBuildResult) -> set[str]:
    listed = run_git(paypulse_repo.repo_path, "ls-tree", "-r", "--name-only", DEFAULT_BRANCH).split("\n")
    return {path for path in listed if path.endswith(".py")}


def test_branch_rows_cover_main_and_the_abandoned_retry_branch(
    result: IngestionResult, paypulse_repo: FixtureBuildResult
) -> None:
    assert result.default_branch == DEFAULT_BRANCH
    assert [row.name for row in result.branches] == [DEFAULT_BRANCH, RETRY_BRANCH_NAME]

    main = result.branch(DEFAULT_BRANCH)
    assert main.head_sha == paypulse_repo.head_sha
    assert (main.is_abandoned, main.commits_ahead, main.commits_behind) == (False, 0, 0)
    assert main.diff_summary["is_default"] is True
    assert main.diff_summary["total_commit_count"] == len(paypulse_repo.main_commits)

    retry = result.branch(RETRY_BRANCH_NAME)
    assert retry.head_sha == paypulse_repo.branch_heads[RETRY_BRANCH_NAME]
    assert (retry.is_abandoned, retry.commits_ahead, retry.commits_behind) == (True, 4, 5)
    summary = retry.diff_summary
    assert summary["merge_base_sha"] == paypulse_repo.main_commits[RETRY_BRANCH_BASE_INDEX].sha
    assert summary["is_default"] is False and summary["is_merged"] is False
    assert summary["files_changed"] == 2 and summary["additions"] > 0
    assert [commit["sha"] for commit in summary["commits"]] == [c.sha for c in paypulse_repo.retry_branch_commits]
    assert summary["dormancy_days"] >= 90


def test_ast_module_rows_for_main_and_the_branch_diff(
    result: IngestionResult, paypulse_repo: FixtureBuildResult
) -> None:
    main_rows = result.artifacts_of(ArtifactType.AST_MODULE, DEFAULT_BRANCH)
    assert {row.file_path for row in main_rows} == main_python_files(paypulse_repo)

    ledger = next(row for row in main_rows if row.file_path == "paypulse/ledger.py")
    metadata = ledger.structured_metadata
    assert metadata["commit_sha"] == paypulse_repo.head_sha and metadata["scope"] == "FULL_TREE"
    assert metadata["outcome"]["status"] == "PARSED" and metadata["language"] == "python"
    post = next(s for s in metadata["symbols"] if s["qualified_name"] == "Ledger.post_transaction")
    assert post["kind"] == "METHOD" and len(post["fingerprint"]) == 32
    assert ledger.raw_content.split("\n")[post["line_start"] - 1].lstrip().startswith("def post_transaction(")
    assert metadata["raw_content_truncated"] is False

    branch_rows = result.artifacts_of(ArtifactType.AST_MODULE, RETRY_BRANCH_NAME)
    assert {row.file_path for row in branch_rows} == {"paypulse/retry.py", "tests/test_retry.py"}
    retry = next(row for row in branch_rows if row.file_path == "paypulse/retry.py")
    assert retry.structured_metadata["scope"] == "BRANCH_DIFF"
    assert retry.structured_metadata["commit_sha"] == paypulse_repo.branch_heads[RETRY_BRANCH_NAME]
    assert retry.raw_content == last_version("paypulse/retry.py")
    [stub] = retry.structured_metadata["stubs"]
    assert stub["enclosing_symbol"] == "RetryPolicy.delay_for"


def test_marker_rows_are_individual_citation_targets(
    result: IngestionResult, paypulse_repo: FixtureBuildResult
) -> None:
    [todo] = result.artifacts_of(ArtifactType.TODO_MARKER, RETRY_BRANCH_NAME)
    retry_py = last_version("paypulse/retry.py")
    metadata = todo.structured_metadata
    assert todo.file_path == "paypulse/retry.py"
    assert metadata["line_start"] == metadata["line_end"] == line_of(retry_py, RETRY_ENGINE_TODO_MARKER)
    assert metadata["snippet"].strip() == RETRY_ENGINE_TODO_MARKER == todo.raw_content.strip()
    assert metadata["tag"] == "TODO" and metadata["text"] == "complete backoff jitter and wire into dispatcher"
    assert metadata["enclosing_symbol"] == "RetryPolicy.delay_for"
    assert metadata["commit_sha"] == paypulse_repo.branch_heads[RETRY_BRANCH_NAME]
    assert len(metadata["fingerprint"]) == 32
    retry_module = next(
        row for row in result.artifacts_of(ArtifactType.AST_MODULE, RETRY_BRANCH_NAME)
        if row.file_path == "paypulse/retry.py"
    )
    assert retry_module.structured_metadata["marker_fingerprints"] == [metadata["fingerprint"]]

    main_rows = result.artifacts_of(ArtifactType.TODO_MARKER, DEFAULT_BRANCH)
    main_markers = {(row.file_path, row.structured_metadata["tag"]) for row in main_rows}
    assert main_markers >= {
        ("paypulse/currency.py", "TODO"),
        ("paypulse/currency.py", "FIXME"),
        ("paypulse/webhooks.py", "FIXME"),
    }
    assert all(row.structured_metadata["commit_sha"] == paypulse_repo.head_sha for row in main_rows)
    assert not any(RETRY_ENGINE_TODO_MARKER in row.raw_content for row in main_rows)


def test_docs_manifest_and_commit_log_rows(result: IngestionResult, paypulse_repo: FixtureBuildResult) -> None:
    readme = next(row for row in result.artifacts_of(ArtifactType.README_DOC, DEFAULT_BRANCH) if row.file_path == "README.md")
    claims = {claim["text"]: claim for claim in readme.structured_metadata["claims"]}
    assert claims["PDF Invoice Export"]["checked"] is True
    assert claims["Automated Exponential Backoff Webhook Retries"]["section"] == "Features"
    assert readme.raw_content.startswith("# PayPulse")

    manifest = next(
        row for row in result.artifacts_of(ArtifactType.DEPENDENCY_MANIFEST) if row.file_path == "requirements.txt"
    )
    pins = {dep["name"]: dep["pinned_version"] for dep in manifest.structured_metadata["dependencies"]}
    assert pins == {"pytest": "6.2.4", "requests": "2.25.1"}
    assert manifest.structured_metadata["manifest"] == "REQUIREMENTS_TXT"

    [commit_log] = result.artifacts_of(ArtifactType.COMMIT_LOG)
    assert commit_log.branch_name == RETRY_BRANCH_NAME and commit_log.file_path == ""
    metadata = commit_log.structured_metadata
    assert (metadata["commits_ahead"], metadata["commits_behind"]) == (4, 5)
    assert metadata["merge_base_sha"] == paypulse_repo.main_commits[RETRY_BRANCH_BASE_INDEX].sha
    assert [c["sha"] for c in metadata["commits"]] == [c.sha for c in paypulse_repo.retry_branch_commits]
    assert {item["path"] for item in metadata["diff"]["files"]} == {"paypulse/retry.py", "tests/test_retry.py"}
    assert "+++ b/paypulse/retry.py" in commit_log.raw_content
    assert RETRY_ENGINE_TODO_MARKER in commit_log.raw_content


def test_every_payload_is_json_serializable(result: IngestionResult) -> None:
    for row in result.artifacts:
        json.dumps(row.structured_metadata)
        assert "\x00" not in row.raw_content
        assert row.artifact_type in ArtifactType.values
    for branch in result.branches:
        json.dumps(branch.diff_summary)
    json.dumps(result.summary)


def test_summary_counts_match_the_rows(result: IngestionResult, paypulse_repo: FixtureBuildResult) -> None:
    summary = result.summary
    assert (summary["branch_count"], summary["unmerged_branch_count"], summary["abandoned_branch_count"]) == (2, 1, 1)
    assert summary["abandoned_branches"] == [RETRY_BRANCH_NAME]
    for kind in ArtifactType.values:
        assert summary["artifact_counts"][kind] == len(result.artifacts_of(ArtifactType(kind)))
    listed = run_git(paypulse_repo.repo_path, "ls-tree", "-r", "--name-only", DEFAULT_BRANCH).split()
    assert sum(summary["file_status_counts"][DEFAULT_BRANCH].values()) == len(listed)
    assert summary["file_status_counts"][RETRY_BRANCH_NAME] == {"PARSED": 2}

    payload = completion_payload(summary)
    assert payload["branch_count"] == 2 and payload["abandoned_branch_count"] == 1
    assert sum(payload["file_status_counts"].values()) == len(listed) + 2


def test_telemetry_order_and_payload_shape(ingestion: tuple[IngestionResult, Events]) -> None:
    result, events = ingestion
    kinds = [event for event, _ in events]
    assert kinds[:2] == [TelemetryEventType.BRANCH_DISCOVERED] * 2
    assert set(kinds[2:]) == {TelemetryEventType.AST_PARSED}
    assert [payload["name"] for _, payload in events[:2]] == [DEFAULT_BRANCH, RETRY_BRANCH_NAME]
    retry = events[1][1]
    assert (retry["is_abandoned"], retry["commits_ahead"], retry["commits_behind"]) == (True, 4, 5)

    parsed = [payload for event, payload in events if event is TelemetryEventType.AST_PARSED]
    branches = [payload["branch"] for payload in parsed]
    assert branches == sorted(branches, key=lambda name: name != DEFAULT_BRANCH)  # main's batches first
    assert parsed[-1]["branch"] == RETRY_BRANCH_NAME and parsed[-1]["scope"] == "BRANCH_DIFF"
    assert {entry["path"] for entry in parsed[-1]["files"]} == {"paypulse/retry.py", "tests/test_retry.py"}
    for payload in parsed:
        assert len(payload["files"]) <= AST_PARSED_BATCH_SIZE
        assert all(set(entry) == TELEMETRY_FILE_KEYS for entry in payload["files"])
    for _, payload in events:
        assert len(json.dumps(payload)) < 8192  # small frames, never file content
    main_counts = result.summary["file_status_counts"][DEFAULT_BRANCH]
    assert max(p["files_processed"] for p in parsed if p["branch"] == DEFAULT_BRANCH) == sum(main_counts.values())


def test_ast_parsed_is_batched(paypulse_workspace: Path, result: IngestionResult) -> None:
    events: Events = []
    run_ingestion(paypulse_workspace, emit=lambda event, payload: events.append((event, payload)), batch_size=2)

    main_batches = [
        payload for event, payload in events
        if event is TelemetryEventType.AST_PARSED and payload["branch"] == DEFAULT_BRANCH
    ]
    reported = sum(len(payload["files"]) for payload in main_batches)
    assert reported == sum(
        count for status, count in result.summary["file_status_counts"][DEFAULT_BRANCH].items()
        if not status.startswith("SKIPPED")
    )
    assert len(main_batches) == math.ceil(reported / 2)
    assert [payload["batch"] for payload in main_batches] == list(range(1, len(main_batches) + 1))
