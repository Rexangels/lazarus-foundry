"""Git and file operations the Foundry agents run on a project's workspace (no ORM, no sandbox).

Every Git call goes through ``apps.projects.git_runner.run_git`` (scrubbed
``GIT_*`` environment, no system/global config, no prompts, timeouts). Commits
use a fixed Lazarus identity and ``--no-verify``, so no hook from the work tree
runs in the backend process.

Every path an agent writes is a repository-relative POSIX path checked by
``safe_relative_path``: no absolute or drive paths, no ``..``, nothing under
``.git``, and the resolved target (symlinks included) must stay inside the
workspace.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from apps.projects.git_runner import GitCommandError, run_git

LAZARUS_GIT_IDENTITY: tuple[str, ...] = (
    "-c", "user.name=Lazarus Foundry",
    "-c", "user.email=foundry@lazarus.invalid",
    "-c", "commit.gpgsign=false",
)
REVIVAL_BRANCH_PREFIX: str = "lazarus/"
MAX_AGENT_FILE_BYTES: int = 512 * 1024
_BRANCH_COMPONENT: re.Pattern[str] = re.compile(r"[^a-z0-9._-]+")


class WorkspaceError(RuntimeError):
    """A workspace operation was refused or failed; the message is safe to show."""


class UnsafePathError(WorkspaceError):
    """An agent tried to touch a path outside the workspace, under ``.git`` or through a symlink."""


@dataclass(frozen=True)
class MergeOutcome:
    branch: str
    ref: str
    head_sha: str
    merge_commit_sha: str
    files: tuple[str, ...]


def git(workspace: Path, *args: str, timeout: float = 120.0) -> str:
    return run_git(workspace, *LAZARUS_GIT_IDENTITY, *args, timeout=timeout)


def head_sha(workspace: Path) -> str:
    return git(workspace, "rev-parse", "HEAD").strip()


def current_branch(workspace: Path) -> str:
    return git(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip()


def status_porcelain(workspace: Path) -> str:
    return git(workspace, "status", "--porcelain", "--untracked-files=all")


def is_clean(workspace: Path) -> bool:
    return status_porcelain(workspace).strip() == ""


def resolve_ref(workspace: Path, name: str) -> str | None:
    """The full ref of branch ``name``: the local branch, else ``origin``'s remote-tracking branch."""
    for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
        try:
            git(workspace, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        except GitCommandError:
            continue
        return ref
    return None


def revival_branch_name(strategy_type: str, run_id: str) -> str:
    """``lazarus/<strategy>-<first 8 chars of the run id>``, e.g. ``lazarus/restore-1a2b3c4d``."""
    kind = _BRANCH_COMPONENT.sub("-", strategy_type.lower()).strip("-.") or "revival"
    suffix = _BRANCH_COMPONENT.sub("", run_id.lower())[:8] or "run"
    return f"{REVIVAL_BRANCH_PREFIX}{kind}-{suffix}"


def create_branch(workspace: Path, name: str, start: str) -> None:
    git(workspace, "check-ref-format", "--branch", name)
    git(workspace, "checkout", "-q", "-B", name, start)


def merge_branch(workspace: Path, branch: str, *, allowed_files: Iterable[str], message: str) -> MergeOutcome:
    """Merge ``branch`` with a merge commit, only if it changes exactly ``allowed_files``.

    The merge is staged with ``--no-commit`` first; a conflict, or a change set
    that differs from the planned files, aborts it and leaves HEAD unchanged.
    """
    ref = resolve_ref(workspace, branch)
    if ref is None:
        raise WorkspaceError(f"branch {branch!r} does not exist in the workspace")
    branch_head = git(workspace, "rev-parse", f"{ref}^{{commit}}").strip()
    try:
        git(workspace, "merge", "--no-ff", "--no-commit", "--no-edit", ref)
    except GitCommandError as exc:
        conflicted = git(workspace, "diff", "--name-only", "--diff-filter=U").split()
        _abort_merge(workspace)
        detail = f"conflicts in {', '.join(conflicted)}" if conflicted else exc.output[:500]
        raise WorkspaceError(f"merging {branch} failed: {detail}") from exc
    staged = tuple(sorted(git(workspace, "diff", "--cached", "--name-only", "HEAD").split()))
    planned = tuple(sorted(set(allowed_files)))
    if not staged:
        _abort_merge(workspace)
        raise WorkspaceError(f"merging {branch} changes nothing; it may already be merged")
    if staged != planned:
        _abort_merge(workspace)
        raise WorkspaceError(
            f"merging {branch} would change {', '.join(staged)}, but the approved plan names {', '.join(planned)}"
        )
    git(workspace, "commit", "-q", "--no-verify", "-m", message)
    return MergeOutcome(branch=branch, ref=ref, head_sha=branch_head, merge_commit_sha=head_sha(workspace),
                        files=staged)


def _abort_merge(workspace: Path) -> None:
    try:
        git(workspace, "merge", "--abort")
    except GitCommandError:
        git(workspace, "reset", "-q", "--merge")


def changed_files(workspace: Path, base: str) -> tuple[str, ...]:
    """Files that differ between ``base`` and the work tree, untracked files included."""
    tracked = git(workspace, "diff", "--name-only", base).split("\n")
    untracked = git(workspace, "ls-files", "--others", "--exclude-standard").split("\n")
    return tuple(sorted({name.strip() for name in (*tracked, *untracked) if name.strip()}))


def safe_relative_path(workspace: Path, relative: str) -> Path:
    """The absolute path of ``relative`` inside ``workspace``, or ``UnsafePathError``."""
    if not isinstance(relative, str) or not relative or "\x00" in relative or "\\" in relative:
        raise UnsafePathError(f"invalid path {relative!r}")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or re.match(r"^[A-Za-z]:", relative) or any(part in ("", ".", "..") for part in
                                                                         relative.split("/")):
        raise UnsafePathError(f"path {relative!r} must be a plain repository-relative path")
    if posix.parts[0].lower() == ".git":
        raise UnsafePathError(f"path {relative!r} is inside .git")
    root = workspace.resolve()
    target = root.joinpath(*posix.parts)
    probe = root
    for part in posix.parts:
        probe = probe / part
        if probe.is_symlink():
            raise UnsafePathError(f"path {relative!r} goes through a symlink")
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise UnsafePathError(f"path {relative!r} resolves outside the workspace")
    return target


def read_text(workspace: Path, relative: str) -> str | None:
    """The file's UTF-8 text, or None when it does not exist."""
    path = safe_relative_path(workspace, relative)
    if not path.is_file():
        return None
    return path.read_bytes().decode("utf-8", errors="replace")


def write_text(workspace: Path, relative: str, content: str) -> None:
    """Write ``content`` (UTF-8, line endings as given) to ``relative``, creating parent directories."""
    data = content.encode("utf-8")
    if len(data) > MAX_AGENT_FILE_BYTES:
        raise WorkspaceError(f"{relative} would be {len(data)} bytes; the limit is {MAX_AGENT_FILE_BYTES}")
    path = safe_relative_path(workspace, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.lazarus-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def copy_file(workspace: Path, source: str, destination: str) -> None:
    """Copy ``source`` to ``destination`` byte for byte; never overwrites."""
    src = safe_relative_path(workspace, source)
    dst = safe_relative_path(workspace, destination)
    if not src.is_file():
        raise WorkspaceError(f"{source} does not exist in the workspace")
    if dst.exists():
        raise WorkspaceError(f"{destination} already exists; copies never overwrite")
    data = src.read_bytes()
    if len(data) > MAX_AGENT_FILE_BYTES:
        raise WorkspaceError(f"{source} is larger than {MAX_AGENT_FILE_BYTES} bytes")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)


def file_part(test_id: str) -> str:
    """``tests/test_x.py::test_y`` -> ``tests/test_x.py``."""
    return test_id.split("::", 1)[0]


def existing_files(workspace: Path, paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(path for path in paths if read_text(workspace, path) is not None)
