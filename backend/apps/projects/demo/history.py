"""A data-driven engine that turns typed commit specs into a real Git history.

``Identity``, ``CommitSpec`` and ``BranchSpec`` describe commits and branches;
``init_repository``, ``apply_commit`` and ``build_branch`` write them with
byte-for-byte reproducible SHAs. ``apps.projects.demo.paypulse`` uses it to
build the "Patient Zero" demo repository; tests use it for throwaway repos.

Determinism rules enforced by the engine:

* Git runs through ``apps.projects.git_runner``: system and global config are
  disabled and every inherited ``GIT_*`` variable is removed, so the user's
  config, hooks and a parent hook's ``GIT_DIR``/``GIT_INDEX_FILE`` can never
  leak in.
* Author/committer identities and dates come from the spec via
  ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` in Git's raw ``<epoch> <+hhmm>`` format.
* Files are written as UTF-8 bytes with ``\\n`` line endings, and the repo is
  configured with ``core.autocrlf=false``, so Windows never introduces CRLF.
* Commit messages are passed verbatim through a message file.

Every failure (including Git errors) is raised as ``FixtureBuildError``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from apps.projects import git_runner
from apps.projects.git_runner import GitError, remove_tree
from config.paths import REPO_ROOT

# The only place inside the Lazarus repository where fixture repos may live.
# It is gitignored, so a nested .git there is never part of our tracked tree.
LAZARUS_DATA_DIR: Path = REPO_ROOT / ".lazarus_data"

# Written inside a built repository's .git directory (so it never affects SHAs).
# Its presence is what allows force=True to delete an existing destination.
FIXTURE_MARKER_NAME: str = "lazarus-fixture"
COMMIT_MESSAGE_FILE_NAME: str = "LAZARUS_COMMIT_MSG"
GIT_TIMEOUT_SECONDS: float = git_runner.GIT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class FixtureBuildError(RuntimeError):
    """Raised when a fixture repository cannot be built."""


class FixtureExistsError(FixtureBuildError):
    """Raised when the destination already exists and ``force`` is False."""


class UnsafeDestinationError(FixtureBuildError):
    """Raised when building or deleting at the destination could damage other data."""


# ---------------------------------------------------------------------------
# Typed specs and results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Identity:
    """A Git author or committer."""

    name: str
    email: str

    def __post_init__(self) -> None:
        for value in (self.name, self.email):
            if not value or any(char in value for char in "<>\r\n"):
                raise ValueError(f"invalid Git identity component: {value!r}")

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware, got {value!r}")


@dataclass(frozen=True)
class CommitSpec:
    """One commit: the files it writes or deletes, who made it, and when.

    ``writes`` maps POSIX-style repo-relative paths to full file contents
    (``\\n`` line endings only). ``deletes`` lists repo-relative paths to remove.
    The committer and commit date default to the author and author date.
    """

    message: str
    author: Identity
    authored_at: datetime
    writes: Mapping[str, str] = field(default_factory=dict)
    deletes: tuple[str, ...] = ()
    committer: Identity | None = None
    committed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.message or self.message != self.message.strip() or "\r" in self.message:
            raise ValueError("commit message must be non-empty, trimmed and use \\n newlines")
        if not self.writes and not self.deletes:
            raise ValueError(f"commit {self.subject!r} changes no files")
        _require_aware(self.authored_at, "authored_at")
        if self.committed_at is not None:
            _require_aware(self.committed_at, "committed_at")
        for path, content in self.writes.items():
            if "\r" in content:
                raise ValueError(f"{path} contains a carriage return; fixture files must use \\n")

    @property
    def subject(self) -> str:
        return self.message.splitlines()[0]

    @property
    def effective_committer(self) -> Identity:
        return self.committer or self.author

    @property
    def effective_committed_at(self) -> datetime:
        return self.committed_at or self.authored_at


@dataclass(frozen=True)
class BranchSpec:
    """A branch and the commits to add to it, in order.

    With ``base=None`` the commits go onto the currently checked-out branch,
    which must be named ``name`` (this is how the default branch is built).
    Otherwise ``name`` is created from ``base`` (any Git revision, e.g.
    ``"main~3"`` or a SHA) and checked out before committing.
    """

    name: str
    commits: tuple[CommitSpec, ...]
    base: str | None = None

    def __post_init__(self) -> None:
        if not self.commits:
            raise ValueError(f"branch {self.name!r} has no commits")


@dataclass(frozen=True)
class CommitRecord:
    """A commit as it was actually written to the repository."""

    sha: str
    branch: str
    message: str
    author: Identity
    authored_at: datetime
    committer: Identity
    committed_at: datetime

    @property
    def subject(self) -> str:
        return self.message.splitlines()[0]


# ---------------------------------------------------------------------------
# Destination safety
# ---------------------------------------------------------------------------
def _nearest_enclosing_work_tree(path: Path) -> Path | None:
    """Return the closest strict ancestor of ``path`` that contains a ``.git`` entry."""
    for ancestor in path.parents:
        if (ancestor / ".git").exists():
            return ancestor
    return None


def assert_safe_destination(destination: Path) -> Path:
    """Return the resolved ``destination`` or raise ``UnsafeDestinationError``.

    Refused destinations:

    * filesystem roots, the home directory and any ancestor of it;
    * the Lazarus repository itself or any directory containing it;
    * anything inside the Lazarus repository other than a strict subdirectory
      of the gitignored ``.lazarus_data/`` (so no nested ``.git`` ever lands in
      a tracked folder such as ``backend/``);
    * anything inside some other Git work tree.
    """
    resolved = Path(destination).expanduser().resolve()
    repo_root = REPO_ROOT.resolve()
    data_dir = LAZARUS_DATA_DIR.resolve()
    home = Path.home().resolve()

    if resolved.parent == resolved:
        raise UnsafeDestinationError(f"refusing to use a filesystem root as a fixture destination: {resolved}")
    if resolved == home or home.is_relative_to(resolved):
        raise UnsafeDestinationError(f"refusing to use the home directory or one of its ancestors: {resolved}")
    if repo_root.is_relative_to(resolved):
        raise UnsafeDestinationError(
            f"refusing to use {resolved}: it is or contains the Lazarus repository ({repo_root})"
        )

    if resolved.is_relative_to(repo_root):
        if resolved == data_dir or not resolved.is_relative_to(data_dir):
            raise UnsafeDestinationError(
                f"refusing to create a nested Git repository at {resolved}: inside the Lazarus "
                f"repository, fixtures may only be built under a subdirectory of {data_dir}"
            )
        enclosing = _nearest_enclosing_work_tree(resolved)
        if enclosing is not None and enclosing != repo_root:
            raise UnsafeDestinationError(f"refusing to nest a fixture inside the Git work tree at {enclosing}")
        return resolved

    enclosing = _nearest_enclosing_work_tree(resolved)
    if enclosing is not None:
        raise UnsafeDestinationError(f"refusing to nest a fixture inside the Git work tree at {enclosing}")
    return resolved


def prepare_destination(destination: Path, *, force: bool) -> Path:
    """Validate ``destination`` and leave it as a fresh, empty directory.

    An existing destination raises ``FixtureExistsError`` unless ``force`` is
    True. With ``force``, a non-empty destination is deleted only when it
    carries the Lazarus fixture marker, so an arbitrary directory is never wiped.
    """
    resolved = assert_safe_destination(destination)
    if resolved.exists() or resolved.is_symlink():
        if not force:
            raise FixtureExistsError(f"{resolved} already exists; pass force=True to rebuild it")
        if not resolved.is_dir() or resolved.is_symlink():
            raise UnsafeDestinationError(f"refusing to replace {resolved}: it is not a directory")
        if any(resolved.iterdir()):
            if not (resolved / ".git" / FIXTURE_MARKER_NAME).is_file():
                raise UnsafeDestinationError(
                    f"refusing to delete {resolved}: it is not empty and was not built by Lazarus "
                    f"(no .git/{FIXTURE_MARKER_NAME} marker)"
                )
        remove_tree(resolved)
    resolved.mkdir(parents=True)
    return resolved


# ---------------------------------------------------------------------------
# Git plumbing (errors re-raised as FixtureBuildError)
# ---------------------------------------------------------------------------
def _run_git(args: Sequence[str], *, cwd: Path, extra_env: Mapping[str, str] | None = None) -> str:
    try:
        return git_runner.run_git_command(args, cwd=cwd, extra_env=extra_env, timeout=GIT_TIMEOUT_SECONDS)
    except GitError as exc:
        raise FixtureBuildError(str(exc)) from exc


def run_git(repo_path: Path, *args: str, extra_env: Mapping[str, str] | None = None) -> str:
    """Run Git against the repository at ``repo_path`` and return stdout.

    ``GIT_DIR`` and ``GIT_WORK_TREE`` are pinned to ``repo_path`` so the
    command can never fall through to an enclosing repository.
    """
    try:
        return git_runner.run_git(repo_path, *args, extra_env=extra_env, timeout=GIT_TIMEOUT_SECONDS)
    except GitError as exc:
        raise FixtureBuildError(str(exc)) from exc


def _git_date(value: datetime) -> str:
    """Format an aware datetime in Git's raw ``<unix seconds> <+hhmm>`` form."""
    offset = value.utcoffset()
    assert offset is not None  # guaranteed by CommitSpec validation
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"{int(value.timestamp())} {sign}{hours:02d}{mins:02d}"


def _repo_file(repo_path: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or ":" in relative
        or pure.is_absolute()
        or ".." in pure.parts
        or pure.parts[0] == ".git"
    ):
        raise FixtureBuildError(f"invalid repo-relative fixture path: {relative!r}")
    return repo_path.joinpath(*pure.parts)


def init_repository(repo_path: Path, *, default_branch: str, identity: Identity) -> None:
    """Initialize an empty repository at ``repo_path`` with deterministic local config."""
    _run_git(["init", "--quiet", f"--initial-branch={default_branch}", str(repo_path)], cwd=repo_path)
    (repo_path / ".git" / FIXTURE_MARKER_NAME).write_bytes(b"built-by: lazarus-foundry apps.projects.demo\n")
    local_config: tuple[tuple[str, str], ...] = (
        ("user.name", identity.name),
        ("user.email", identity.email),
        ("core.autocrlf", "false"),
        ("core.safecrlf", "false"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
        ("gc.auto", "0"),
    )
    for key, value in local_config:
        run_git(repo_path, "config", key, value)


def current_branch(repo_path: Path) -> str:
    return run_git(repo_path, "symbolic-ref", "--short", "HEAD").strip()


def checkout(repo_path: Path, revision: str) -> None:
    run_git(repo_path, "checkout", "--quiet", revision)


def apply_commit(repo_path: Path, spec: CommitSpec) -> CommitRecord:
    """Apply ``spec``'s file changes to the work tree and commit them on the current branch."""
    for relative in spec.deletes:
        target = _repo_file(repo_path, relative)
        try:
            target.unlink()
        except FileNotFoundError as exc:
            raise FixtureBuildError(f"commit {spec.subject!r} deletes missing file {relative}") from exc
    for relative, content in spec.writes.items():
        target = _repo_file(repo_path, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Bytes, not text mode: no platform newline translation.
        target.write_bytes(content.encode("utf-8"))

    run_git(repo_path, "add", "--all")

    message_file = repo_path / ".git" / COMMIT_MESSAGE_FILE_NAME
    message_file.write_bytes((spec.message + "\n").encode("utf-8"))
    committer = spec.effective_committer
    committed_at = spec.effective_committed_at
    try:
        run_git(
            repo_path,
            "commit",
            "--quiet",
            "--no-verify",
            "--no-gpg-sign",
            "--cleanup=verbatim",
            f"--file={message_file}",
            extra_env={
                "GIT_AUTHOR_NAME": spec.author.name,
                "GIT_AUTHOR_EMAIL": spec.author.email,
                "GIT_AUTHOR_DATE": _git_date(spec.authored_at),
                "GIT_COMMITTER_NAME": committer.name,
                "GIT_COMMITTER_EMAIL": committer.email,
                "GIT_COMMITTER_DATE": _git_date(committed_at),
            },
        )
    finally:
        message_file.unlink(missing_ok=True)

    return CommitRecord(
        sha=run_git(repo_path, "rev-parse", "HEAD").strip(),
        branch=current_branch(repo_path),
        message=spec.message,
        author=spec.author,
        authored_at=spec.authored_at,
        committer=committer,
        committed_at=committed_at,
    )


def build_branch(repo_path: Path, branch: BranchSpec) -> tuple[CommitRecord, ...]:
    """Create (or continue) ``branch`` and apply its commits in order."""
    if branch.base is None:
        active = current_branch(repo_path)
        if active != branch.name:
            raise FixtureBuildError(
                f"branch spec {branch.name!r} has no base but {active!r} is checked out"
            )
    else:
        run_git(repo_path, "checkout", "--quiet", "-b", branch.name, branch.base)
    return tuple(apply_commit(repo_path, commit) for commit in branch.commits)


__all__ = [
    "COMMIT_MESSAGE_FILE_NAME",
    "FIXTURE_MARKER_NAME",
    "GIT_TIMEOUT_SECONDS",
    "LAZARUS_DATA_DIR",
    "BranchSpec",
    "CommitRecord",
    "CommitSpec",
    "FixtureBuildError",
    "FixtureExistsError",
    "Identity",
    "UnsafeDestinationError",
    "apply_commit",
    "assert_safe_destination",
    "build_branch",
    "checkout",
    "current_branch",
    "init_repository",
    "prepare_destination",
    "remove_tree",
    "run_git",
]
