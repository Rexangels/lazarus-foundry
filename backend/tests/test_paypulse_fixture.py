"""Tests for the PayPulse dead-repo fixture builder and its management command (no database needed)."""
from __future__ import annotations

import io
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import git
import pytest
from django.core.management import CommandError, call_command

from config.paths import BACKEND_DIR, REPO_ROOT
from tests.fixtures.build_paypulse_fixture import (
    DEFAULT_BRANCH,
    LAZARUS_DATA_DIR,
    MAIN_COMMITS,
    MARCUS,
    PAYPULSE_FIXTURE_NAME,
    PRIYA,
    RETRY_BRANCH_BASE_INDEX,
    RETRY_BRANCH_COMMITS,
    RETRY_BRANCH_NAME,
    RETRY_ENGINE_TODO_MARKER,
    FixtureBuildResult,
    FixtureExistsError,
    UnsafeDestinationError,
    assert_safe_destination,
    build_paypulse_fixture,
    checkout,
    run_git,
)

FIXTURE_PYTEST_TIMEOUT_SECONDS: int = 120
MANAGE_PY_TIMEOUT_SECONDS: int = 180
# The branch forks from main on 2021-07-22 and main's last commit is 2023-01-18
# (545 days later), so ~18 months of dormancy is at most ~545 days here.
MIN_RETRY_BRANCH_DORMANCY: timedelta = timedelta(days=540)
# Main's tests/test_webhooks.py, copied into a checkout of the retry branch.
MAIN_WEBHOOK_TESTS_COPY: str = "tests/test_webhooks_from_main.py"
UNREACHABLE_DATABASE_URL: str = "postgres://lazarus:lazarus@127.0.0.1:1/unreachable?connect_timeout=1"


@pytest.fixture
def repo(paypulse_repo: FixtureBuildResult) -> Iterator[git.Repo]:
    with git.Repo(paypulse_repo.repo_path) as repository:
        yield repository


@pytest.fixture(scope="module")
def second_build(tmp_path_factory: pytest.TempPathFactory) -> FixtureBuildResult:
    """An independent build, compared against the session build and then checked out on the branch."""
    return build_paypulse_fixture(tmp_path_factory.mktemp("paypulse_second") / PAYPULSE_FIXTURE_NAME)


@pytest.fixture(scope="module")
def retry_checkout(second_build: FixtureBuildResult) -> Path:
    """The second build's work tree on the retry branch, plus a copy of main's webhook tests."""
    repo_path = second_build.repo_path
    main_webhook_tests = run_git(repo_path, "show", f"{DEFAULT_BRANCH}:tests/test_webhooks.py")
    checkout(repo_path, RETRY_BRANCH_NAME)
    (repo_path / MAIN_WEBHOOK_TESTS_COPY).write_bytes(main_webhook_tests.encode("utf-8"))
    return repo_path


def _tree_blobs(repository: git.Repo, revision: str) -> dict[str, bytes]:
    return {
        str(item.path): item.data_stream.read()
        for item in repository.commit(revision).tree.traverse()
        if isinstance(item, git.Blob)
    }


def _isolated_env(**overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"  # no pytest-django / pytest-asyncio in the target repo
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(overrides)
    return env


def _run_fixture_pytest(repo_path: Path, *test_files: str) -> subprocess.CompletedProcess[str]:
    """Run the fixture's own tests with our interpreter, isolated from this project's plugins."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *test_files],
        cwd=repo_path,
        env=_isolated_env(),
        capture_output=True,
        text=True,
        timeout=FIXTURE_PYTEST_TIMEOUT_SECONDS,
        check=False,
    )


def _run_manage_py(*args: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(BACKEND_DIR / "manage.py"), *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=MANAGE_PY_TIMEOUT_SECONDS,
        check=False,
    )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_builds_main_history_matching_the_spec(paypulse_repo: FixtureBuildResult, repo: git.Repo) -> None:
    assert not repo.bare
    assert repo.active_branch.name == DEFAULT_BRANCH == paypulse_repo.default_branch
    assert {head.name for head in repo.heads} == {DEFAULT_BRANCH, RETRY_BRANCH_NAME}
    assert set(paypulse_repo.branches) == {DEFAULT_BRANCH, RETRY_BRANCH_NAME}
    assert paypulse_repo.branches[DEFAULT_BRANCH] == paypulse_repo.main_commits

    commits = list(repo.iter_commits(DEFAULT_BRANCH, reverse=True))
    assert len(commits) == len(MAIN_COMMITS) == len(paypulse_repo.main_commits)
    for commit, spec, record in zip(commits, MAIN_COMMITS, paypulse_repo.main_commits, strict=True):
        assert commit.hexsha == record.sha
        assert commit.message == spec.message + "\n"
        assert (commit.author.name, commit.author.email) == (spec.author.name, spec.author.email)
        assert (commit.committer.name, commit.committer.email) == (spec.author.name, spec.author.email)
        assert commit.authored_datetime == spec.authored_at
        assert commit.authored_datetime.utcoffset() == spec.authored_at.utcoffset()
        assert commit.committed_datetime == spec.authored_at
        assert record.branch == DEFAULT_BRANCH

    assert repo.head.commit.hexsha == paypulse_repo.head_sha == paypulse_repo.main_commits[-1].sha
    assert {spec.author for spec in MAIN_COMMITS} == {PRIYA, MARCUS}
    assert commits[0].authored_datetime.year == 2021
    assert commits[-1].authored_datetime.year == 2023
    dates = [commit.authored_datetime for commit in commits]
    assert dates == sorted(dates)


@pytest.mark.slow
def test_build_leaves_main_checked_out_and_clean(paypulse_repo: FixtureBuildResult, repo: git.Repo) -> None:
    assert repo.active_branch.name == DEFAULT_BRANCH
    assert not repo.is_dirty(untracked_files=True)
    assert run_git(paypulse_repo.repo_path, "status", "--porcelain") == ""
    assert not (paypulse_repo.repo_path / "paypulse" / "retry.py").exists()


@pytest.mark.slow
def test_retry_branch_history_matching_the_spec(paypulse_repo: FixtureBuildResult, repo: git.Repo) -> None:
    commits = list(repo.iter_commits(f"{DEFAULT_BRANCH}..{RETRY_BRANCH_NAME}", reverse=True))
    records = paypulse_repo.retry_branch_commits
    assert 3 <= len(commits) == len(RETRY_BRANCH_COMMITS) == len(records) <= 5
    for commit, spec, record in zip(commits, RETRY_BRANCH_COMMITS, records, strict=True):
        assert commit.hexsha == record.sha
        assert commit.message == spec.message + "\n"
        assert (commit.author.name, commit.author.email) == (spec.author.name, spec.author.email)
        assert commit.authored_datetime == spec.authored_at
        assert record.branch == RETRY_BRANCH_NAME
    assert paypulse_repo.branch_heads[RETRY_BRANCH_NAME] == repo.heads[RETRY_BRANCH_NAME].commit.hexsha
    assert {spec.author for spec in RETRY_BRANCH_COMMITS} <= {PRIYA, MARCUS}
    dates = [commit.authored_datetime for commit in commits]
    assert dates == sorted(dates)


@pytest.mark.slow
def test_retry_branch_is_unmerged_and_diverged(paypulse_repo: FixtureBuildResult, repo: git.Repo) -> None:
    for branch in (DEFAULT_BRANCH, RETRY_BRANCH_NAME):
        assert all(len(commit.parents) <= 1 for commit in repo.iter_commits(branch)), branch
    assert not repo.is_ancestor(RETRY_BRANCH_NAME, DEFAULT_BRANCH)

    counts = run_git(
        paypulse_repo.repo_path, "rev-list", "--left-right", "--count", f"{DEFAULT_BRANCH}...{RETRY_BRANCH_NAME}"
    )
    behind, ahead = (int(value) for value in counts.split())
    assert behind == len(MAIN_COMMITS) - 1 - RETRY_BRANCH_BASE_INDEX > 0
    assert ahead == len(RETRY_BRANCH_COMMITS) > 0

    [merge_base] = repo.merge_base(DEFAULT_BRANCH, RETRY_BRANCH_NAME)
    assert merge_base.hexsha == paypulse_repo.main_commits[RETRY_BRANCH_BASE_INDEX].sha
    assert merge_base.authored_datetime.year == 2021
    assert 5 <= merge_base.authored_datetime.month <= 8
    assert merge_base.authored_datetime < repo.commit(RETRY_BRANCH_NAME).authored_datetime


@pytest.mark.slow
def test_retry_branch_was_abandoned_long_before_main_went_dormant(repo: git.Repo) -> None:
    branch_last = repo.commit(RETRY_BRANCH_NAME).committed_datetime
    main_last = repo.commit(DEFAULT_BRANCH).committed_datetime
    assert main_last - branch_last >= MIN_RETRY_BRANCH_DORMANCY


@pytest.mark.slow
def test_no_committed_blob_contains_crlf(repo: git.Repo) -> None:
    offenders = {
        f"{commit.hexsha[:8]}:{item.path}"
        for branch in (DEFAULT_BRANCH, RETRY_BRANCH_NAME)
        for commit in repo.iter_commits(branch)
        for item in commit.tree.traverse()
        if isinstance(item, git.Blob) and b"\r" in item.data_stream.read()
    }
    assert offenders == set()


# ---------------------------------------------------------------------------
# Forensic contradictions planted on main
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_readme_claims_features_that_are_missing_or_partial(repo: git.Repo) -> None:
    blobs = _tree_blobs(repo, DEFAULT_BRANCH)
    readme = blobs["README.md"].decode("utf-8")
    for claim in (
        "Multi-currency conversion",
        "Automated Exponential Backoff Webhook Retries",
        "PDF Invoice Export",
    ):
        assert claim in readme

    # Ghost feature: documented, but no PDF/export code exists anywhere.
    assert "PDF invoice export" in blobs["docs/architecture-notes.md"].decode("utf-8")
    assert not any("pdf" in path.lower() or "export" in path.lower() for path in blobs)
    assert not any(b"pdf" in data.lower() for path, data in blobs.items() if path.endswith(".py"))

    # Partial features carry markers for later AST/TODO extraction.
    currency = blobs["paypulse/currency.py"].decode("utf-8")
    assert "raise NotImplementedError" in currency
    assert "# TODO:" in currency
    assert "# FIXME:" in blobs["paypulse/webhooks.py"].decode("utf-8")
    assert "pytest==6.2.4" in blobs["requirements.txt"].decode("utf-8")
    assert "requests==2.25.1" in blobs["requirements.txt"].decode("utf-8")


@pytest.mark.slow
def test_fixture_webhook_test_fails_on_main(paypulse_repo: FixtureBuildResult) -> None:
    completed = _run_fixture_pytest(paypulse_repo.repo_path, "tests/test_webhooks.py")
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "1 failed, 3 passed" in output
    assert "test_dispatch_retries_with_exponential_backoff" in output


@pytest.mark.slow
def test_fixture_ledger_and_invoice_tests_pass(paypulse_repo: FixtureBuildResult) -> None:
    completed = _run_fixture_pytest(paypulse_repo.repo_path, "tests/test_ledger.py", "tests/test_invoices.py")
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "12 passed" in output


# ---------------------------------------------------------------------------
# Forensic contradictions planted on feat/webhook-retry-engine
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_todo_marker_is_only_on_the_retry_branch(repo: git.Repo) -> None:
    marker = RETRY_ENGINE_TODO_MARKER.encode("utf-8")
    branch_hits = {
        path: data.count(marker) for path, data in _tree_blobs(repo, RETRY_BRANCH_NAME).items() if marker in data
    }
    assert branch_hits == {"paypulse/retry.py": 1}
    assert not any(marker in data for data in _tree_blobs(repo, DEFAULT_BRANCH).values())


@pytest.mark.slow
def test_retry_branch_leaves_jitter_and_dispatcher_wiring_unfinished(
    paypulse_repo: FixtureBuildResult, repo: git.Repo
) -> None:
    branch = _tree_blobs(repo, RETRY_BRANCH_NAME)
    retry = branch["paypulse/retry.py"].decode("utf-8")
    assert 'raise NotImplementedError("backoff jitter is not implemented yet")' in retry
    assert "random" not in retry
    for done in ("class RetryPolicy", "max_delay", "def call_with_retry", "def is_retryable_status"):
        assert done in retry

    # The dispatcher on the branch is exactly the one it forked from: no retry wiring.
    fork_point = _tree_blobs(repo, paypulse_repo.main_commits[RETRY_BRANCH_BASE_INDEX].sha)
    assert branch["paypulse/webhooks.py"] == fork_point["paypulse/webhooks.py"]
    assert b"retry" not in branch["paypulse/webhooks.py"].lower()


@pytest.mark.slow
def test_retry_branch_own_tests_pass(retry_checkout: Path) -> None:
    completed = _run_fixture_pytest(retry_checkout, "tests/test_retry.py", "tests/test_webhooks.py")
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "20 passed" in output


@pytest.mark.slow
def test_main_backoff_spec_still_fails_on_retry_branch(retry_checkout: Path) -> None:
    completed = _run_fixture_pytest(retry_checkout, MAIN_WEBHOOK_TESTS_COPY)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "1 failed, 3 passed" in output
    assert "test_dispatch_retries_with_exponential_backoff" in output
    assert "unexpected keyword argument 'max_attempts'" in output


@pytest.mark.slow
def test_jitter_raises_on_retry_branch(retry_checkout: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "from paypulse.retry import RetryPolicy; RetryPolicy(jitter=True).delay_for(2)"],
        cwd=retry_checkout,
        env=_isolated_env(),
        capture_output=True,
        text=True,
        timeout=FIXTURE_PYTEST_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 1
    assert "NotImplementedError: backoff jitter is not implemented yet" in completed.stderr


# ---------------------------------------------------------------------------
# Determinism, rebuild and safety
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_same_inputs_produce_identical_shas(
    paypulse_repo: FixtureBuildResult, second_build: FixtureBuildResult
) -> None:
    for name, records in paypulse_repo.branches.items():
        assert [record.sha for record in second_build.branches[name]] == [record.sha for record in records], name
    assert second_build.branch_heads == paypulse_repo.branch_heads
    assert second_build.head_sha == paypulse_repo.head_sha


@pytest.mark.slow
def test_existing_destination_requires_force(tmp_path: Path) -> None:
    destination = tmp_path / "paypulse_dead_repo"
    first = build_paypulse_fixture(destination)
    stray = destination / "stray.txt"
    stray.write_text("left over", encoding="utf-8")

    with pytest.raises(FixtureExistsError):
        build_paypulse_fixture(destination)
    assert stray.exists()

    rebuilt = build_paypulse_fixture(destination, force=True)
    assert rebuilt.head_sha == first.head_sha
    assert not stray.exists()


@pytest.mark.slow
def test_force_refuses_to_delete_a_directory_it_did_not_build(tmp_path: Path) -> None:
    destination = tmp_path / "someone_elses_data"
    destination.mkdir()
    keep = destination / "important.txt"
    keep.write_text("do not delete", encoding="utf-8")

    with pytest.raises(UnsafeDestinationError):
        build_paypulse_fixture(destination, force=True)
    assert keep.read_text(encoding="utf-8") == "do not delete"


@pytest.mark.parametrize(
    "destination",
    [
        REPO_ROOT / "backend" / "paypulse_dead_repo",
        REPO_ROOT / "paypulse_dead_repo",
        REPO_ROOT,
        LAZARUS_DATA_DIR,
        Path.home(),
        Path(Path.home().anchor),
    ],
    ids=["inside-backend", "inside-repo-root", "repo-root", "lazarus-data-itself", "home", "filesystem-root"],
)
def test_unsafe_destinations_are_refused(destination: Path) -> None:
    with pytest.raises(UnsafeDestinationError):
        assert_safe_destination(destination)


def test_refused_destination_is_never_created() -> None:
    destination = REPO_ROOT / "backend" / "paypulse_guard_probe"
    with pytest.raises(UnsafeDestinationError):
        build_paypulse_fixture(destination)
    assert not destination.exists()


def test_destination_under_lazarus_data_is_allowed() -> None:
    destination = LAZARUS_DATA_DIR / "fixtures" / "paypulse_dead_repo"
    assert assert_safe_destination(destination) == destination.resolve()


# ---------------------------------------------------------------------------
# Management command: python manage.py seed_paypulse_fixture
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_seed_command_builds_then_requires_force(paypulse_repo: FixtureBuildResult, tmp_path: Path) -> None:
    destination = tmp_path / PAYPULSE_FIXTURE_NAME
    out = io.StringIO()
    call_command("seed_paypulse_fixture", destination=str(destination), stdout=out)
    summary = out.getvalue()
    assert f"Built PayPulse fixture at {destination.resolve()}" in summary
    for name, sha in paypulse_repo.branch_heads.items():
        assert name in summary
        assert sha in summary
    assert f"{len(MAIN_COMMITS)} commits" in summary
    assert f"{len(RETRY_BRANCH_COMMITS)} commits not on {DEFAULT_BRANCH} (unmerged)" in summary
    assert "PayPulse fixture ready." in summary

    with pytest.raises(CommandError, match="--force"):
        call_command("seed_paypulse_fixture", destination=str(destination), stdout=io.StringIO())

    # --force through the real CLI, with an unreachable database: seeding never touches it.
    completed = _run_manage_py(
        "seed_paypulse_fixture", "--destination", str(destination), "--force",
        DATABASE_URL=UNREACHABLE_DATABASE_URL,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert paypulse_repo.head_sha in completed.stdout
    assert "PayPulse fixture ready." in completed.stdout
    with git.Repo(destination) as rebuilt:
        assert rebuilt.head.commit.hexsha == paypulse_repo.head_sha
        assert rebuilt.heads[RETRY_BRANCH_NAME].commit.hexsha == paypulse_repo.branch_heads[RETRY_BRANCH_NAME]


def test_seed_command_rejects_unsafe_destination() -> None:
    destination = REPO_ROOT / "backend" / "paypulse_command_probe"
    with pytest.raises(CommandError, match="refusing"):
        call_command("seed_paypulse_fixture", destination=str(destination), stdout=io.StringIO())
    assert not destination.exists()
