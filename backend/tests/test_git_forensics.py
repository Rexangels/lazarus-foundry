"""Tests for apps.ingestion.git_forensics (no database, no network)."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import git
import pytest
from django.test import override_settings
from pytest_django.fixtures import Settings as SettingsWrapper

from apps.ingestion.git_forensics import (
    ChangeType,
    CloneError,
    GitForensicService,
    RepositoryReport,
    UnknownRevisionError,
    UnsafeRepositorySourceError,
    UnsafeWorkspaceError,
    clone_into_workspace,
    clone_local_repository_into_workspace,
    resolve_workspace_dir,
    validate_remote_url,
)
from apps.projects.git_runner import run_git_command
from tests.fixtures.build_paypulse_fixture import (
    DEFAULT_BRANCH,
    MAIN_COMMITS,
    RETRY_BRANCH_BASE_INDEX,
    RETRY_BRANCH_COMMITS,
    RETRY_BRANCH_NAME,
    RETRY_ENGINE_TODO_MARKER,
    BranchSpec,
    CommitSpec,
    FixtureBuildResult,
    Identity,
    build_branch,
    checkout,
    init_repository,
    run_git,
)

TESTER: Identity = Identity("Forensic Tester", "tester@example.com")
RETRY_FILES: frozenset[str] = frozenset({"paypulse/retry.py", "tests/test_retry.py"})


# ---------------------------------------------------------------------------
# PayPulse, cloned into a temporary WORKSPACE_ROOT
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def paypulse_clone(paypulse_repo: FixtureBuildResult, tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("workspaces")
    with override_settings(WORKSPACE_ROOT=root):
        return clone_local_repository_into_workspace(paypulse_repo.repo_path, root / "paypulse")


@pytest.fixture
def service(paypulse_clone: Path) -> Iterator[GitForensicService]:
    with GitForensicService(paypulse_clone) as forensic:
        yield forensic


@pytest.fixture(scope="module")
def paypulse_report(paypulse_clone: Path) -> RepositoryReport:
    with GitForensicService(paypulse_clone) as forensic:
        return forensic.analyze()


@pytest.mark.slow
def test_clone_is_independent_with_full_history(paypulse_repo: FixtureBuildResult, paypulse_clone: Path) -> None:
    assert not (paypulse_clone / ".git" / "objects" / "info" / "alternates").exists()
    with git.Repo(paypulse_clone) as clone:
        assert clone.active_branch.name == DEFAULT_BRANCH
        assert clone.head.commit.hexsha == paypulse_repo.head_sha
        assert len(list(clone.iter_commits(DEFAULT_BRANCH))) == len(MAIN_COMMITS)
        assert clone.git.config("--local", "core.symlinks") == "false"
        # Only the default branch is local; the retry branch exists as origin/<name>.
        assert {head.name for head in clone.heads} == {DEFAULT_BRANCH}
        assert f"origin/{RETRY_BRANCH_NAME}" in {ref.name for ref in clone.remotes.origin.refs}


@pytest.mark.slow
def test_default_branch_and_repo_timeline(
    paypulse_repo: FixtureBuildResult, paypulse_report: RepositoryReport
) -> None:
    facts = paypulse_report.default_branch
    assert facts.name == DEFAULT_BRANCH
    assert facts.head_sha == paypulse_repo.head_sha
    assert facts.commit_count == len(MAIN_COMMITS)
    assert facts.first_commit_date == MAIN_COMMITS[0].authored_at
    assert facts.last_commit_date == MAIN_COMMITS[-1].authored_at
    assert paypulse_report.latest_commit.sha == paypulse_repo.head_sha
    assert paypulse_report.latest_commit_date == MAIN_COMMITS[-1].authored_at

    assert [branch.name for branch in paypulse_report.branches] == [DEFAULT_BRANCH, RETRY_BRANCH_NAME]
    main = paypulse_report.branch(DEFAULT_BRANCH)
    assert main.is_default and main.is_merged and not main.is_abandoned
    assert (main.commits_ahead, main.commits_behind, main.diff) == (0, 0, None)


@pytest.mark.slow
def test_retry_branch_is_unmerged_abandoned_and_diverged(
    paypulse_repo: FixtureBuildResult, paypulse_clone: Path, paypulse_report: RepositoryReport
) -> None:
    branch = paypulse_report.branch(RETRY_BRANCH_NAME)
    assert branch.ref == f"refs/remotes/origin/{RETRY_BRANCH_NAME}"
    assert branch.head_sha == paypulse_repo.branch_heads[RETRY_BRANCH_NAME]
    assert not branch.is_default
    assert not branch.is_merged
    assert branch.is_abandoned
    assert paypulse_report.abandoned_branches == (branch,)
    assert (branch.commits_ahead, branch.commits_behind) == (4, 5)

    main_minus_5 = run_git(paypulse_clone, "rev-parse", f"{DEFAULT_BRANCH}~5").strip()
    assert branch.merge_base_sha == main_minus_5 == paypulse_repo.main_commits[RETRY_BRANCH_BASE_INDEX].sha
    assert branch.merge_base_date == MAIN_COMMITS[RETRY_BRANCH_BASE_INDEX].authored_at

    assert [commit.sha for commit in branch.unique_commits] == [
        record.sha for record in paypulse_repo.retry_branch_commits
    ]
    assert [commit.subject for commit in branch.unique_commits] == [spec.subject for spec in RETRY_BRANCH_COMMITS]
    assert not branch.unique_commits_truncated
    assert branch.last_commit_date == RETRY_BRANCH_COMMITS[-1].authored_at
    assert branch.dormancy == MAIN_COMMITS[-1].authored_at - RETRY_BRANCH_COMMITS[-1].authored_at
    assert branch.dormancy >= paypulse_report.abandoned_min_age == timedelta(days=90)


@pytest.mark.slow
def test_retry_branch_diff_adds_the_retry_engine(paypulse_report: RepositoryReport) -> None:
    diff = paypulse_report.branch(RETRY_BRANCH_NAME).diff
    assert diff is not None
    assert {item.path for item in diff.files} == RETRY_FILES
    for item in diff.files:
        assert item.change_type is ChangeType.ADDED
        assert item.additions > 0 and item.deletions == 0
        assert not item.is_binary and not item.patch_truncated
        assert item.patch.startswith(f"--- /dev/null\n+++ b/{item.path}\n@@ ")

    retry = diff.file("paypulse/retry.py")
    assert retry is not None
    marker_lines = [line for line in retry.patch.splitlines() if RETRY_ENGINE_TODO_MARKER in line]
    assert len(marker_lines) == 1 and marker_lines[0].startswith("+")
    assert diff.total_additions == sum(item.additions for item in diff.files)


@pytest.mark.slow
def test_patch_is_capped_and_truncation_recorded(paypulse_clone: Path, paypulse_report: RepositoryReport) -> None:
    full = paypulse_report.branch(RETRY_BRANCH_NAME).diff
    assert full is not None
    with GitForensicService(paypulse_clone, max_patch_bytes=300) as forensic:
        capped = forensic.inspect_branch(RETRY_BRANCH_NAME).diff
    assert capped is not None
    for item in capped.files:
        original = full.file(item.path)
        assert original is not None
        assert item.patch_truncated
        assert item.patch_size_bytes == original.patch_size_bytes > 300
        body = item.patch.split("\n", 2)[2]  # drop the ---/+++ header lines
        assert len(body.encode("utf-8")) <= 300 and body.endswith("\n")
        assert original.patch.startswith(item.patch)
        assert (item.additions, item.deletions) == (original.additions, original.deletions)


@pytest.mark.slow
def test_read_file_at_reads_branch_versions_without_checkout(
    paypulse_clone: Path, service: GitForensicService
) -> None:
    content = service.read_file_at(RETRY_BRANCH_NAME, "paypulse/retry.py")
    assert content is not None
    assert RETRY_ENGINE_TODO_MARKER.encode("utf-8") in content
    assert service.read_file_at(DEFAULT_BRANCH, "paypulse/retry.py") is None
    assert service.read_file_at(RETRY_BRANCH_NAME, "paypulse") is None  # a directory, not a file

    branch_files = dict(service.list_files_at(RETRY_BRANCH_NAME))
    assert branch_files["paypulse/retry.py"] == len(content)
    assert RETRY_FILES <= branch_files.keys()
    assert not RETRY_FILES & dict(service.list_files_at(DEFAULT_BRANCH)).keys()

    # The work tree never left the default branch.
    assert run_git(paypulse_clone, "symbolic-ref", "--short", "HEAD").strip() == DEFAULT_BRANCH
    assert run_git(paypulse_clone, "status", "--porcelain") == ""
    assert not (paypulse_clone / "paypulse" / "retry.py").exists()

    for bad in ("--output=pwned", "no-such-branch"):
        with pytest.raises(UnknownRevisionError):
            service.read_file_at(bad, "paypulse/retry.py")


# ---------------------------------------------------------------------------
# A throwaway repository with merged, stale and recent branches (no origin)
# ---------------------------------------------------------------------------
def _commit(message: str, when: datetime, path: str, content: str) -> CommitSpec:
    return CommitSpec(message=message, author=TESTER, authored_at=when, writes={path: content})


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def branchy_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("branchy") / "repo"
    repo.mkdir()
    init_repository(repo, default_branch="main", identity=TESTER)
    build_branch(repo, BranchSpec("main", (_commit("Initial commit", _utc(2024, 1, 1), "app.py", "v = 1\n"),)))
    build_branch(
        repo,
        BranchSpec("feature/done", (_commit("Add feature", _utc(2024, 1, 5), "feature.py", "done = True\n"),), "main"),
    )
    checkout(repo, "main")
    merged_at = _utc(2024, 1, 10).isoformat()
    run_git(
        repo, "merge", "--no-ff", "--no-edit", "--quiet", "feature/done",
        extra_env={"GIT_AUTHOR_DATE": merged_at, "GIT_COMMITTER_DATE": merged_at},
    )
    build_branch(repo, BranchSpec("main", (_commit("Bump version", _utc(2024, 6, 1), "app.py", "v = 2\n"),)))
    # Forks from the merge commit; last touched 141 days before the repo's newest commit.
    build_branch(
        repo, BranchSpec("stale", (_commit("Stale work", _utc(2024, 1, 12), "stale.py", "x = 1\n"),), "main~1")
    )
    # Forks from main's tip; last touched 12 days before the newest commit.
    build_branch(repo, BranchSpec("wip", (_commit("Recent work", _utc(2024, 5, 20), "wip.py", "y = 1\n"),), "main"))
    checkout(repo, "main")
    return repo


@pytest.mark.slow
def test_merged_stale_and_recent_branches_are_classified(branchy_repo: Path) -> None:
    with GitForensicService(branchy_repo) as forensic:
        report = forensic.analyze()

    # No origin/HEAD here, so detection falls back to "main".
    assert report.default_branch.name == "main"
    assert report.latest_commit_date == _utc(2024, 6, 1)
    assert [branch.name for branch in report.branches] == ["main", "feature/done", "stale", "wip"]

    merged = report.branch("feature/done")
    assert merged.is_merged and not merged.is_abandoned
    assert merged.commits_ahead == 0
    assert merged.dormancy >= timedelta(days=90)  # old, but merged work is never "abandoned"

    stale = report.branch("stale")
    assert not stale.is_merged and stale.is_abandoned
    assert (stale.commits_ahead, stale.commits_behind) == (1, 1)
    assert stale.dormancy == timedelta(days=141)

    wip = report.branch("wip")
    assert not wip.is_merged and not wip.is_abandoned
    assert (wip.commits_ahead, wip.commits_behind) == (1, 0)
    assert wip.diff is not None and [item.path for item in wip.diff.files] == ["wip.py"]

    assert {branch.name for branch in report.unmerged_branches} == {"stale", "wip"}
    assert [branch.name for branch in report.abandoned_branches] == ["stale"]


# ---------------------------------------------------------------------------
# Untrusted input: URLs and workspace paths (fast, no Git)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/repos/paypulse",
        "file:///tmp/paypulse",
        "ssh://git@github.com/octo/paypulse.git",
        "git://github.com/octo/paypulse.git",
        "git@github.com:octo/paypulse.git",
        "http://github.com/octo/paypulse.git",
        "ext::sh -c touch% /tmp/pwned",
        "ext::git-remote-pwn",
        "-uhttps://github.com/octo/paypulse.git",
        "--upload-pack=touch /tmp/pwned",
        "https://evil.example.com/octo/paypulse.git",
        "https://github.com.evil.example/octo/paypulse.git",
        "https://user:token@github.com/octo/paypulse.git",
        "https://github.com@evil.example/octo/paypulse.git",
        "https://github.com:8443/octo/paypulse.git",
        "https://github.com/",
        "https://github.com/octo/../paypulse",
        "https://github.com/octo/paypulse.git?ref=main",
        " https://github.com/octo/paypulse.git",
        "https://github.com/octo/paypulse.git\n--upload-pack=touch",
        "C:\\repos\\paypulse",
        "/srv/repos/paypulse",
        "",
    ],
)
def test_untrusted_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeRepositorySourceError):
        validate_remote_url(url, allowed_hosts=["github.com"])


@pytest.mark.parametrize(
    "url",
    ["https://github.com/octo/paypulse.git", "https://GitLab.com/group/sub/project", "https://bitbucket.org/a/b"],
)
def test_https_urls_on_allowed_hosts_are_accepted(url: str) -> None:
    assert validate_remote_url(url) == url


def test_allowed_hosts_come_from_settings(settings: SettingsWrapper) -> None:
    settings.INGEST_ALLOWED_HOSTS = ["git.example.org"]
    assert validate_remote_url("https://git.example.org/team/repo.git")
    with pytest.raises(UnsafeRepositorySourceError, match="INGEST_ALLOWED_HOSTS"):
        validate_remote_url("https://github.com/octo/paypulse.git")


def test_clone_into_workspace_rejects_before_running_git(settings: SettingsWrapper, tmp_path: Path) -> None:
    settings.WORKSPACE_ROOT = tmp_path / "ws"
    for source in ("file:///tmp/paypulse", str(tmp_path), "--upload-pack=touch /tmp/pwned"):
        with pytest.raises(UnsafeRepositorySourceError):
            clone_into_workspace(source, tmp_path / "ws" / "project")
    assert not (tmp_path / "ws").exists()


def test_workspace_must_stay_inside_workspace_root(settings: SettingsWrapper, tmp_path: Path) -> None:
    root = tmp_path / "ws"
    settings.WORKSPACE_ROOT = root
    assert resolve_workspace_dir("project-1") == (root / "project-1").resolve()
    assert resolve_workspace_dir(root / "a" / "b") == (root / "a" / "b").resolve()
    for escape in (tmp_path / "elsewhere", root / ".." / "escape", "../escape", root, tmp_path):
        with pytest.raises(UnsafeWorkspaceError):
            resolve_workspace_dir(escape)

    occupied = root / "occupied"
    occupied.mkdir(parents=True)
    (occupied / "file.txt").write_text("taken", encoding="utf-8")
    with pytest.raises(UnsafeWorkspaceError, match="not empty"):
        resolve_workspace_dir(occupied)


def test_local_clone_rejects_non_repositories_and_overlapping_workspaces(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    root = tmp_path / "ws"
    settings.WORKSPACE_ROOT = root
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    with pytest.raises(UnsafeRepositorySourceError):
        clone_local_repository_into_workspace(plain_dir, root / "a")
    with pytest.raises(UnsafeRepositorySourceError):
        clone_local_repository_into_workspace(tmp_path / "missing", root / "a")

    source = root / "source"
    source.mkdir(parents=True)
    run_git_command(["init", "--quiet", str(source)], cwd=tmp_path)
    with pytest.raises(UnsafeWorkspaceError):
        clone_local_repository_into_workspace(source, source / "inner")
    with pytest.raises(UnsafeWorkspaceError):
        clone_local_repository_into_workspace(source, source)
    assert not (source / "inner").exists()


def test_failed_clone_removes_the_workspace(settings: SettingsWrapper, tmp_path: Path) -> None:
    settings.WORKSPACE_ROOT = tmp_path / "ws"
    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)  # looks like a repository, but Git can't read it
    destination = tmp_path / "ws" / "project"
    with pytest.raises(CloneError):
        clone_local_repository_into_workspace(broken, destination)
    assert not destination.exists()
