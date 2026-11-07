"""Run the ``git`` executable in an isolated, reproducible environment.

Shared by the demo fixture builder (``apps.projects.demo``) and the ingestion
service (``apps.ingestion.git_forensics``). The module imports no Django
models, so any app can use it without import cycles.

Isolation rules applied to every command:

* every inherited ``GIT_*`` variable is removed, so a parent hook's
  ``GIT_DIR``/``GIT_INDEX_FILE``/``GIT_OBJECT_DIRECTORY`` can never redirect
  a command to another repository;
* system and global (user) Git config are disabled
  (``GIT_CONFIG_NOSYSTEM=1``, ``GIT_CONFIG_GLOBAL=<devnull>``), so user
  aliases, credential helpers, hooks paths and ``diff.external`` don't apply;
* Git never prompts (``GIT_TERMINAL_PROMPT=0``, ``GCM_INTERACTIVE=never``);
* commands run with a timeout.

``subprocess`` is used instead of GitPython here because GitPython can add
environment variables for its child processes but cannot remove inherited ones.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path

GIT_TIMEOUT_SECONDS: float = 60.0
# Environment variables that are always set for Git children.
ISOLATION_ENV: Mapping[str, str] = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    # Git Credential Manager (Windows) must never open a login window.
    "GCM_INTERACTIVE": "never",
}


class GitError(RuntimeError):
    """Base class for failures running Git."""


class GitNotFoundError(GitError):
    """The ``git`` executable is not installed or not on ``PATH``."""


class GitTimeoutError(GitError):
    """A Git command ran longer than its timeout and was killed."""


class GitCommandError(GitError):
    """A Git command exited with a non-zero status."""

    def __init__(self, args: Sequence[str], returncode: int, output: str) -> None:
        self.git_args: tuple[str, ...] = tuple(args)
        self.returncode: int = returncode
        self.output: str = output
        super().__init__(f"`git {' '.join(args)}` failed with exit code {returncode}: {output}")


@lru_cache(maxsize=1)
def git_executable() -> str:
    """Return the Git executable (honoring ``GIT_PYTHON_GIT_EXECUTABLE``) or raise ``GitNotFoundError``."""
    configured = os.environ.get("GIT_PYTHON_GIT_EXECUTABLE")
    found = shutil.which(configured) if configured else shutil.which("git")
    if found is None:
        raise GitNotFoundError("the `git` executable was not found on PATH; install Git")
    return found


def isolated_git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of ``os.environ`` without ``GIT_*`` variables, plus ``ISOLATION_ENV`` and ``extra``."""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(ISOLATION_ENV)
    if extra:
        env.update(extra)
    return env


def run_git_command(
    args: Sequence[str],
    *,
    cwd: Path,
    extra_env: Mapping[str, str] | None = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> str:
    """Run ``git <args>`` in ``cwd`` with the isolated environment and return stdout.

    Raises ``GitNotFoundError``, ``GitTimeoutError`` or ``GitCommandError``.
    Output is decoded as UTF-8 with replacement characters for invalid bytes.
    """
    command = [git_executable(), *args]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=isolated_git_env(extra_env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeoutError(f"`git {' '.join(args)}` timed out after {timeout:g} seconds") from exc
    except OSError as exc:
        raise GitError(f"`git {' '.join(args)}` could not run: {exc}") from exc
    if completed.returncode != 0:
        raise GitCommandError(args, completed.returncode, (completed.stderr or completed.stdout).strip())
    return completed.stdout


def pinned_repo_env(repo_path: Path) -> dict[str, str]:
    """``GIT_DIR``/``GIT_WORK_TREE`` for the non-bare repository at ``repo_path``."""
    return {"GIT_DIR": str(repo_path / ".git"), "GIT_WORK_TREE": str(repo_path)}


def run_git(
    repo_path: Path,
    *args: str,
    extra_env: Mapping[str, str] | None = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> str:
    """Run Git against the non-bare repository at ``repo_path`` and return stdout.

    ``GIT_DIR`` and ``GIT_WORK_TREE`` are pinned to ``repo_path`` so the
    command can never fall through to an enclosing repository.
    """
    env = pinned_repo_env(repo_path)
    if extra_env:
        env.update(extra_env)
    return run_git_command(args, cwd=repo_path, extra_env=env, timeout=timeout)


def _make_writable_and_retry(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
    """``shutil.rmtree`` error handler: clear the read-only bit Git sets on Windows, then retry."""
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    func(path)


def remove_tree(path: Path) -> None:
    """Delete ``path`` recursively, including the read-only object files inside ``.git``."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_make_writable_and_retry)
    else:  # pragma: no cover - the backend runs on Python 3.12+
        shutil.rmtree(path, onerror=lambda func, p, info: _make_writable_and_retry(func, p, info[1]))
