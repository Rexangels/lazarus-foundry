"""Git forensics for ingested repositories: workspace cloning and branch analysis.

Pure service: nothing here touches the ORM. ``GitForensicService`` returns
frozen dataclasses (``RepositoryReport``, ``BranchReport``, ``CommitInfo``,
``FileDiff``, ...) that ``ingest_repository_task`` persists as
``BranchSnapshot`` / ``ArtifactRecord`` rows.

Cloning (untrusted input)
    ``clone_into_workspace`` accepts only ``https://`` URLs on
    ``settings.INGEST_ALLOWED_HOSTS``. Local repositories (the demo fixture and
    tests) go through ``clone_local_repository_into_workspace``, never through
    the URL path, and are cloned with ``--no-local`` so the workspace shares no
    object hardlinks with the source. Both run Git through
    ``apps.projects.git_runner`` (scrubbed ``GIT_*`` environment, no system or
    user config, no prompts, a timeout), with ``--no-recurse-submodules``,
    ``core.symlinks=false`` and full history. The workspace must be a new or
    empty directory strictly inside ``settings.WORKSPACE_ROOT``; a failed clone
    removes it.

Branches
    Remote-tracking branches (``refs/remotes/origin/*`` except ``origin/HEAD``)
    and local branches, de-duplicated by short name; the local branch wins
    when both exist. The default branch comes from ``origin/HEAD``, then
    ``main``, then ``master``, then the alphabetically first branch.

Abandonment
    Dormant repositories are judged on their own timeline, not the wall
    clock: ``is_abandoned = not is_merged and (repo_latest_commit_date -
    branch_last_commit_date) >= settings.ABANDONED_BRANCH_MIN_AGE_DAYS``.
    Dates are committer dates; the repository's latest commit is the newest
    branch tip across all branches.

Reads use GitPython (``git.Repo``) with the repository's ``GIT_DIR`` and work
tree pinned and system/user config disabled. Files are read from Git objects
(``ls-tree`` / ``cat-file``), so the work tree stays on the default branch.
"""
from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import TracebackType
from urllib.parse import urlsplit

import git
from django.conf import settings

from apps.projects import git_runner
from apps.projects.git_runner import GitError, remove_tree

REMOTE_NAME: str = "origin"
FALLBACK_DEFAULT_BRANCHES: tuple[str, ...] = ("main", "master")
# Upper bound on the commits listed per branch (commits_ahead is always exact).
DEFAULT_MAX_BRANCH_COMMITS: int = 1000
# Regular files only: symlinks (120000) and submodules (160000) are skipped.
REGULAR_FILE_MODES: frozenset[str] = frozenset({"100644", "100755"})
_REGULAR_FILE_TYPE_BITS: int = 0o100000
_FILE_TYPE_MASK: int = 0o170000
EMPTY_TREE_SHAS: Mapping[str, str] = {
    "sha1": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "sha256": "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",
}
# Persisted into every cloned workspace's .git/config.
CLONE_CONFIG: tuple[str, ...] = ("core.symlinks=false", "core.autocrlf=false")
# Only https may be used by a remote clone (including redirects); never file://.
REMOTE_PROTOCOL_CONFIG: tuple[str, ...] = (
    "protocol.allow=never",
    "protocol.https.allow=always",
    "protocol.file.allow=never",
)
_FORBIDDEN_URL_CHARS: re.Pattern[str] = re.compile(r"[\s\x00-\x1f\x7f\\]")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class GitForensicsError(RuntimeError):
    """Base class for ingestion Git failures."""


class UnsafeRepositorySourceError(GitForensicsError, ValueError):
    """A repository URL or local source path was rejected before running Git."""


class UnsafeWorkspaceError(GitForensicsError, ValueError):
    """A workspace directory is outside ``WORKSPACE_ROOT``, not empty, or overlaps the source."""


class CloneError(GitForensicsError):
    """``git clone`` failed or timed out (the partial workspace has been removed)."""


class UnknownRevisionError(GitForensicsError, LookupError):
    """A revision does not name a commit in the repository."""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
class ChangeType(StrEnum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
    RENAMED = "RENAMED"
    COPIED = "COPIED"


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    author_name: str
    author_email: str
    authored_at: datetime
    committer_name: str
    committer_email: str
    committed_at: datetime
    subject: str
    parent_shas: tuple[str, ...]


@dataclass(frozen=True)
class FileDiff:
    """One file changed by a branch, relative to its merge-base with the default branch.

    ``path`` is the new path (the old path for deletions). ``patch`` is a
    unified diff for this file (``---``/``+++`` headers plus hunks), capped at
    ``max_patch_bytes`` of hunk text at a line boundary; ``patch_truncated``
    records the cut and ``patch_size_bytes`` the full hunk size. Binary files
    carry no patch and no line counts.
    """

    path: str
    old_path: str | None
    change_type: ChangeType
    additions: int
    deletions: int
    is_binary: bool
    patch: str
    patch_truncated: bool
    patch_size_bytes: int


@dataclass(frozen=True)
class BranchDiff:
    """What a branch added since it forked: ``git diff <merge-base> <branch>`` (three-dot semantics)."""

    base_sha: str
    head_sha: str
    files: tuple[FileDiff, ...]

    @property
    def total_additions(self) -> int:
        return sum(item.additions for item in self.files)

    @property
    def total_deletions(self) -> int:
        return sum(item.deletions for item in self.files)

    def file(self, path: str) -> FileDiff | None:
        return next((item for item in self.files if item.path == path), None)


@dataclass(frozen=True)
class BranchReport:
    """Forensic facts about one branch.

    For the default branch: ``is_default`` and ``is_merged`` are True, the
    counts are 0, and ``merge_base_*``/``diff`` are None. ``dormancy`` is the
    repository's latest commit date minus this branch's last commit date.
    """

    name: str
    ref: str
    head_sha: str
    last_commit_date: datetime
    is_default: bool
    is_merged: bool
    is_abandoned: bool
    commits_ahead: int
    commits_behind: int
    merge_base_sha: str | None
    merge_base_date: datetime | None
    dormancy: timedelta
    unique_commits: tuple[CommitInfo, ...]
    unique_commits_truncated: bool
    diff: BranchDiff | None


@dataclass(frozen=True)
class DefaultBranchFacts:
    name: str
    head_sha: str
    commit_count: int
    first_commit_date: datetime
    last_commit_date: datetime


@dataclass(frozen=True)
class RepositoryReport:
    """Everything ``GitForensicService.analyze`` learned; ``branches`` lists the default branch first."""

    repo_path: Path
    default_branch: DefaultBranchFacts
    latest_commit: CommitInfo
    abandoned_min_age: timedelta
    branches: tuple[BranchReport, ...]

    @property
    def latest_commit_date(self) -> datetime:
        return self.latest_commit.committed_at

    @property
    def unmerged_branches(self) -> tuple[BranchReport, ...]:
        return tuple(branch for branch in self.branches if not branch.is_merged)

    @property
    def abandoned_branches(self) -> tuple[BranchReport, ...]:
        return tuple(branch for branch in self.branches if branch.is_abandoned)

    def branch(self, name: str) -> BranchReport:
        for branch in self.branches:
            if branch.name == name:
                return branch
        raise KeyError(name)


@dataclass(frozen=True)
class BranchRef:
    """A branch as found in the ref store: short name, full ref name and tip SHA."""

    name: str
    ref: str
    sha: str


# ---------------------------------------------------------------------------
# Source and workspace validation
# ---------------------------------------------------------------------------
def validate_remote_url(url: str, allowed_hosts: Iterable[str] | None = None) -> str:
    """Return ``url`` if it is a plain ``https://<allowed host>/<path>`` URL, else raise.

    Rejected: any scheme but https (``file://``, ``ssh://``, ``git://``,
    ``ext::``, scp-style ``git@host:path``), anything starting with ``-``
    (option injection), whitespace/control characters/backslashes,
    credentials, explicit ports, query strings, fragments, ``.``/``..`` path
    segments, and hosts not in ``allowed_hosts`` (default
    ``settings.INGEST_ALLOWED_HOSTS``, exact match, case-insensitive).
    """
    if not isinstance(url, str) or not url:
        raise UnsafeRepositorySourceError("repository URL must be a non-empty string")
    if url.startswith("-"):
        raise UnsafeRepositorySourceError("repository URL must not start with '-' (looks like a Git option)")
    if _FORBIDDEN_URL_CHARS.search(url):
        raise UnsafeRepositorySourceError("repository URL contains whitespace, control characters or backslashes")
    if "::" in url:
        raise UnsafeRepositorySourceError("Git transport syntax ('<transport>::<address>') is not allowed")
    if url[:8].lower() != "https://":
        raise UnsafeRepositorySourceError("only https:// repository URLs are accepted")
    if "?" in url or "#" in url:
        raise UnsafeRepositorySourceError("repository URL must not contain a query string or fragment")

    parts = urlsplit(url)
    if "@" in parts.netloc:
        raise UnsafeRepositorySourceError("repository URL must not contain credentials")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeRepositorySourceError("repository URL has an invalid port") from exc
    host = (parts.hostname or "").lower()
    if port is not None or parts.netloc.lower() != host:
        raise UnsafeRepositorySourceError("repository URL must not specify a port")
    configured = allowed_hosts if allowed_hosts is not None else settings.INGEST_ALLOWED_HOSTS
    allowed = {item.strip().lower() for item in configured}
    if host not in allowed:
        raise UnsafeRepositorySourceError(
            f"host {host or '<empty>'!r} is not in INGEST_ALLOWED_HOSTS ({', '.join(sorted(allowed)) or 'none'})"
        )
    segments = parts.path.strip("/").split("/")
    if not parts.path.strip("/") or any(segment in ("", ".", "..") for segment in segments):
        raise UnsafeRepositorySourceError("repository URL must name a repository path such as /owner/repo")
    return url


def resolve_workspace_dir(workspace_dir: Path | str, *, require_empty: bool = True) -> Path:
    """Return the resolved workspace directory, which must be strictly inside ``settings.WORKSPACE_ROOT``.

    Relative paths are taken relative to ``WORKSPACE_ROOT``. Symlinks are
    resolved before the check, so a link pointing outside the root is
    rejected. With ``require_empty`` (cloning), the directory may not exist
    yet; if it does, it must be empty. Without it (the Foundry sandbox), it
    must be an existing directory.
    """
    root = Path(settings.WORKSPACE_ROOT).expanduser().resolve()
    candidate = Path(workspace_dir).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise UnsafeWorkspaceError(f"workspace {resolved} must be a subdirectory of WORKSPACE_ROOT ({root})")
    if not require_empty:
        if not resolved.is_dir():
            raise UnsafeWorkspaceError(f"workspace {resolved} does not exist or is not a directory")
        return resolved
    if resolved.exists() or resolved.is_symlink():
        if not resolved.is_dir():
            raise UnsafeWorkspaceError(f"workspace {resolved} exists and is not a directory")
        if any(resolved.iterdir()):
            raise UnsafeWorkspaceError(f"workspace {resolved} already exists and is not empty")
    return resolved


def _is_git_repository(path: Path) -> bool:
    return (path / ".git").exists() or ((path / "HEAD").is_file() and (path / "objects").is_dir())


# ---------------------------------------------------------------------------
# Cloning
# ---------------------------------------------------------------------------
def _clone_timeout(timeout: float | None) -> float:
    return float(timeout if timeout is not None else settings.INGEST_CLONE_TIMEOUT_SECONDS)


def _clone(args: list[str], source: str, destination: Path, timeout: float) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        git_runner.run_git_command([*args, "--", source, str(destination)], cwd=destination.parent, timeout=timeout)
        if not (destination / ".git").is_dir():
            raise CloneError(f"git clone finished but {destination} is not a Git work tree")
    except BaseException as exc:
        with contextlib.suppress(OSError):
            if destination.exists():
                remove_tree(destination)
        if isinstance(exc, GitError):
            raise CloneError(f"could not clone into {destination}: {exc}") from exc
        raise
    return destination


def clone_into_workspace(
    source: str,
    workspace_dir: Path | str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    timeout: float | None = None,
) -> Path:
    """Clone the user-supplied https ``source`` URL into ``workspace_dir`` and return the workspace path.

    ``source`` is validated by ``validate_remote_url``; ``workspace_dir`` by
    ``resolve_workspace_dir``. Only the https protocol is enabled for the
    clone. Raises ``UnsafeRepositorySourceError``, ``UnsafeWorkspaceError`` or
    ``CloneError``.
    """
    url = validate_remote_url(source, allowed_hosts)
    destination = resolve_workspace_dir(workspace_dir)
    clone_args: list[str] = []
    for item in (*REMOTE_PROTOCOL_CONFIG, *CLONE_CONFIG):
        clone_args += ["-c", item]
    clone_args += ["clone", "--quiet", "--no-recurse-submodules"]
    for item in (*CLONE_CONFIG, "protocol.file.allow=never"):
        clone_args += ["--config", item]
    return _clone(clone_args, url, destination, _clone_timeout(timeout))


def clone_local_repository_into_workspace(
    source_path: Path | str, workspace_dir: Path | str, *, timeout: float | None = None
) -> Path:
    """Clone a trusted local repository (the demo fixture, tests) into ``workspace_dir``.

    Uses ``--no-local`` so objects are copied through the pack protocol
    instead of hardlinked. The workspace must not be, contain, or sit inside
    the source repository.
    """
    try:
        source = Path(source_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise UnsafeRepositorySourceError(f"local repository {source_path} does not exist") from exc
    if not source.is_dir() or not _is_git_repository(source):
        raise UnsafeRepositorySourceError(f"{source} is not a Git repository")
    destination = resolve_workspace_dir(workspace_dir)
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise UnsafeWorkspaceError(f"workspace {destination} overlaps the source repository {source}")
    clone_args: list[str] = []
    for item in CLONE_CONFIG:
        clone_args += ["-c", item]
    clone_args += ["clone", "--quiet", "--no-local", "--no-recurse-submodules"]
    for item in CLONE_CONFIG:
        clone_args += ["--config", item]
    return _clone(clone_args, str(source), destination, _clone_timeout(timeout))


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def _decode(value: str | bytes) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _commit_info(commit: git.Commit) -> CommitInfo:
    message = _decode(commit.message)
    return CommitInfo(
        sha=commit.hexsha,
        author_name=str(commit.author.name or ""),
        author_email=str(commit.author.email or ""),
        authored_at=commit.authored_datetime,
        committer_name=str(commit.committer.name or ""),
        committer_email=str(commit.committer.email or ""),
        committed_at=commit.committed_datetime,
        subject=message.splitlines()[0] if message.strip() else "",
        parent_shas=tuple(parent.hexsha for parent in commit.parents),
    )


def _validate_repo_path(path: str) -> str:
    pure = PurePosixPath(path)
    if not path or "\\" in path or "\0" in path or pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"invalid repository-relative path: {path!r}")
    return pure.as_posix()


def _change_type(diff: git.Diff) -> ChangeType:
    if diff.new_file:
        return ChangeType.ADDED
    if diff.deleted_file:
        return ChangeType.DELETED
    if diff.renamed_file:
        return ChangeType.RENAMED
    if diff.copied_file:
        return ChangeType.COPIED
    return ChangeType.MODIFIED


class GitForensicService:
    """Read-only forensic analysis of one repository (normally a workspace clone).

    Use as a context manager (or call ``close``) so GitPython's persistent
    ``git cat-file`` processes are stopped; on Windows they otherwise keep the
    repository's files locked.
    """

    def __init__(
        self,
        repo_path: Path | str,
        *,
        abandoned_min_age: timedelta | None = None,
        max_patch_bytes: int | None = None,
        max_branch_commits: int = DEFAULT_MAX_BRANCH_COMMITS,
    ) -> None:
        path = Path(repo_path).expanduser().resolve()
        try:
            self._repo = git.Repo(path)
        except (git.InvalidGitRepositoryError, git.NoSuchPathError) as exc:
            raise GitForensicsError(f"{path} is not a Git repository") from exc
        environment = dict(git_runner.ISOLATION_ENV)
        environment["GIT_DIR"] = str(self._repo.git_dir)
        if self._repo.working_tree_dir is not None:
            environment["GIT_WORK_TREE"] = str(self._repo.working_tree_dir)
        # Reads must never take the index lock (e.g. a status refresh).
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        self._repo.git.update_environment(**environment)

        self.repo_path: Path = Path(self._repo.working_tree_dir or self._repo.git_dir)
        self.abandoned_min_age: timedelta = (
            abandoned_min_age
            if abandoned_min_age is not None
            else timedelta(days=settings.ABANDONED_BRANCH_MIN_AGE_DAYS)
        )
        self.max_patch_bytes: int = max_patch_bytes if max_patch_bytes is not None else settings.INGEST_MAX_PATCH_BYTES
        if self.max_patch_bytes <= 0 or max_branch_commits <= 0:
            raise ValueError("max_patch_bytes and max_branch_commits must be positive")
        self.max_branch_commits: int = max_branch_commits
        self._branches: dict[str, BranchRef] | None = None
        self._origin_head: str | None = None

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self._repo.close()

    def __enter__(self) -> GitForensicService:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- low level --------------------------------------------------------
    def _git(self, command: str, *args: str) -> str:
        """Run ``git <command> <args>`` through GitPython (its configured executable and environment)."""
        try:
            return str(getattr(self._repo.git, command.replace("-", "_"))(*args))
        except git.GitCommandError as exc:
            raise GitForensicsError(
                f"`git {command} {' '.join(args)}` failed: {_decode(exc.stderr).strip()}"
            ) from exc

    def _load_branches(self) -> dict[str, BranchRef]:
        if self._branches is not None:
            return self._branches
        remote_prefix = f"refs/remotes/{REMOTE_NAME}/"
        output = self._git(
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(objecttype)%00%(symref)",
            "refs/heads",
            remote_prefix.rstrip("/"),
        )
        local: dict[str, BranchRef] = {}
        remote: dict[str, BranchRef] = {}
        for line in output.split("\n"):
            if not line:
                continue
            refname, sha, object_type, symref = line.split("\0")
            if refname.startswith("refs/heads/"):
                if object_type == "commit":
                    name = refname.removeprefix("refs/heads/")
                    local[name] = BranchRef(name=name, ref=refname, sha=sha)
            elif refname.startswith(remote_prefix):
                name = refname.removeprefix(remote_prefix)
                if name == "HEAD":
                    if symref.startswith(remote_prefix):
                        self._origin_head = symref.removeprefix(remote_prefix)
                elif not symref and object_type == "commit":
                    remote[name] = BranchRef(name=name, ref=refname, sha=sha)
        # The local branch wins: in a fresh clone both point at the same commit.
        self._branches = {name: (local.get(name) or remote[name]) for name in sorted({*local, *remote})}
        return self._branches

    @property
    def branches(self) -> Mapping[str, BranchRef]:
        """Short branch name -> ``BranchRef``, sorted by name."""
        return dict(self._load_branches())

    def default_branch(self) -> str:
        """``origin/HEAD``'s branch, else ``main``, else ``master``, else the first branch by name."""
        branches = self._load_branches()
        if self._origin_head is not None and self._origin_head in branches:
            return self._origin_head
        for name in FALLBACK_DEFAULT_BRANCHES:
            if name in branches:
                return name
        if not branches:
            raise GitForensicsError(f"{self.repo_path} has no branches")
        return next(iter(branches))

    def _resolve_commit(self, revision: str) -> git.Commit:
        """Resolve a branch short name (local or remote-tracking) or any Git revision to a commit."""
        if not isinstance(revision, str) or not revision or revision.startswith("-") or any(
            char in revision for char in "\0\r\n"
        ):
            raise UnknownRevisionError(f"invalid revision: {revision!r}")
        branch = self._load_branches().get(revision)
        try:
            return self._repo.commit(branch.ref if branch else revision)
        except (ValueError, LookupError, git.BadName, git.BadObject, git.GitCommandError) as exc:
            raise UnknownRevisionError(f"{revision!r} does not name a commit") from exc

    def resolve_commit_sha(self, revision: str) -> str:
        """Full SHA of the commit that ``revision`` (a branch short name or any Git revision) names."""
        return self._resolve_commit(revision).hexsha

    # -- files at a revision ---------------------------------------------
    def read_file_at(self, revision: str, path: str) -> bytes | None:
        """Return the bytes of regular file ``path`` at ``revision``, or None if it isn't one there.

        Reads the Git object directly; the work tree is not touched.
        """
        relative = _validate_repo_path(path)
        commit = self._resolve_commit(revision)
        try:
            item = commit.tree / relative
        except KeyError:
            return None
        if not isinstance(item, git.Blob) or (item.mode & _FILE_TYPE_MASK) != _REGULAR_FILE_TYPE_BITS:
            return None
        return bytes(item.data_stream.read())

    def list_files_at(self, revision: str) -> list[tuple[str, int]]:
        """Every regular file at ``revision`` as ``(path, size_in_bytes)``, in Git's path order."""
        commit = self._resolve_commit(revision)
        output = self._git("ls-tree", "-r", "-l", "-z", "--full-tree", commit.hexsha)
        files: list[tuple[str, int]] = []
        for entry in output.split("\0"):
            if not entry:
                continue
            meta, path = entry.split("\t", 1)
            mode, object_type, _sha, size = meta.split()
            if object_type == "blob" and mode in REGULAR_FILE_MODES:
                files.append((path, int(size)))
        return files

    # -- diffs ------------------------------------------------------------
    def _empty_tree(self) -> git.Tree:
        object_format = self._git("rev-parse", "--show-object-format").strip() or "sha1"
        return git.Tree(self._repo, bytes.fromhex(EMPTY_TREE_SHAS[object_format]))

    def _numstat(self, base_sha: str, head_sha: str) -> dict[str, tuple[int, int] | None]:
        """Path -> (additions, deletions), or None for binary files. Renames are keyed by the new path."""
        output = self._git("diff-tree", "-r", "-M", "--numstat", "-z", "--no-color", base_sha, head_sha)
        tokens = output.split("\0")
        stats: dict[str, tuple[int, int] | None] = {}
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if not token:
                continue
            additions, deletions, path = token.split("\t", 2)
            if not path:  # rename/copy: the old and new paths follow as separate tokens
                path = tokens[index + 1]
                index += 2
            stats[path] = None if additions == "-" else (int(additions), int(deletions))
        return stats

    def _file_patch(self, diff: git.Diff, path: str) -> tuple[str, bool, int]:
        body = diff.diff if isinstance(diff.diff, bytes) else _decode(diff.diff or "").encode("utf-8")
        size = len(body)
        truncated = size > self.max_patch_bytes
        if truncated:
            cut = body.rfind(b"\n", 0, self.max_patch_bytes)
            body = body[: cut + 1] if cut >= 0 else body[: self.max_patch_bytes]
        old = f"a/{diff.a_path}" if diff.a_path and not diff.new_file else "/dev/null"
        new = f"b/{path}" if not diff.deleted_file else "/dev/null"
        header = f"--- {old}\n+++ {new}\n"
        return header + body.decode("utf-8", errors="replace"), truncated, size

    def diff_between(self, base: git.Commit | git.Tree, head: git.Commit) -> BranchDiff:
        """Per-file diff from ``base`` to ``head`` (rename detection on, binary files without patches)."""
        stats = self._numstat(base.hexsha, head.hexsha)
        files: list[FileDiff] = []
        for diff in base.diff(head, create_patch=True):
            path = str(diff.a_path if diff.deleted_file else diff.b_path)
            change_type = _change_type(diff)
            counts = stats.get(path)
            is_binary = path in stats and counts is None
            if is_binary:
                patch, truncated, size = "", False, 0
            else:
                patch, truncated, size = self._file_patch(diff, path)
            files.append(
                FileDiff(
                    path=path,
                    old_path=str(diff.a_path) if change_type in (ChangeType.RENAMED, ChangeType.COPIED) else None,
                    change_type=change_type,
                    additions=counts[0] if counts else 0,
                    deletions=counts[1] if counts else 0,
                    is_binary=is_binary,
                    patch=patch,
                    patch_truncated=truncated,
                    patch_size_bytes=size,
                )
            )
        files.sort(key=lambda item: item.path)
        return BranchDiff(base_sha=base.hexsha, head_sha=head.hexsha, files=tuple(files))

    # -- branches ---------------------------------------------------------
    def _branch_report(self, branch: BranchRef, default: BranchRef, repo_latest: datetime) -> BranchReport:
        head = self._repo.commit(branch.sha)
        last_commit_date = head.committed_datetime
        dormancy = repo_latest - last_commit_date
        if branch.name == default.name:
            return BranchReport(
                name=branch.name,
                ref=branch.ref,
                head_sha=branch.sha,
                last_commit_date=last_commit_date,
                is_default=True,
                is_merged=True,
                is_abandoned=False,
                commits_ahead=0,
                commits_behind=0,
                merge_base_sha=None,
                merge_base_date=None,
                dormancy=dormancy,
                unique_commits=(),
                unique_commits_truncated=False,
                diff=None,
            )

        behind, ahead = (
            int(value)
            for value in self._git("rev-list", "--left-right", "--count", f"{default.ref}...{branch.ref}").split()
        )
        merge_bases = self._repo.merge_base(default.ref, branch.ref)
        merge_base = merge_bases[0] if merge_bases else None
        is_merged = self._repo.is_ancestor(branch.sha, default.sha)
        listed = list(self._repo.iter_commits(f"{default.ref}..{branch.ref}", max_count=self.max_branch_commits))
        listed.reverse()  # oldest first
        diff = self.diff_between(merge_base if merge_base is not None else self._empty_tree(), head)
        return BranchReport(
            name=branch.name,
            ref=branch.ref,
            head_sha=branch.sha,
            last_commit_date=last_commit_date,
            is_default=False,
            is_merged=is_merged,
            is_abandoned=not is_merged and dormancy >= self.abandoned_min_age,
            commits_ahead=ahead,
            commits_behind=behind,
            merge_base_sha=merge_base.hexsha if merge_base is not None else None,
            merge_base_date=merge_base.committed_datetime if merge_base is not None else None,
            dormancy=dormancy,
            unique_commits=tuple(_commit_info(commit) for commit in listed),
            unique_commits_truncated=ahead > len(listed),
            diff=diff,
        )

    def latest_commit(self) -> CommitInfo:
        """The newest branch tip (by committer date) across all branches."""
        tips = [self._repo.commit(branch.sha) for branch in self._load_branches().values()]
        if not tips:
            raise GitForensicsError(f"{self.repo_path} has no branches")
        return _commit_info(max(tips, key=lambda commit: commit.committed_datetime))

    def default_branch_facts(self) -> DefaultBranchFacts:
        default = self._load_branches()[self.default_branch()]
        roots = self._git("rev-list", "--max-parents=0", default.ref).split()
        return DefaultBranchFacts(
            name=default.name,
            head_sha=default.sha,
            commit_count=int(self._git("rev-list", "--count", default.ref).strip()),
            first_commit_date=min(self._repo.commit(sha).committed_datetime for sha in roots),
            last_commit_date=self._repo.commit(default.sha).committed_datetime,
        )

    def inspect_branch(self, name: str) -> BranchReport:
        """Forensic report for one branch, compared with the default branch."""
        branches = self._load_branches()
        if name not in branches:
            raise UnknownRevisionError(f"no branch named {name!r}")
        default = branches[self.default_branch()]
        return self._branch_report(branches[name], default, self.latest_commit().committed_at)

    def analyze(self) -> RepositoryReport:
        """Inspect every branch; the default branch comes first, the rest by name."""
        branches = self._load_branches()
        default_name = self.default_branch()
        default = branches[default_name]
        latest = self.latest_commit()
        ordered = [default, *(branch for name, branch in branches.items() if name != default_name)]
        return RepositoryReport(
            repo_path=self.repo_path,
            default_branch=self.default_branch_facts(),
            latest_commit=latest,
            abandoned_min_age=self.abandoned_min_age,
            branches=tuple(self._branch_report(branch, default, latest.committed_at) for branch in ordered),
        )
