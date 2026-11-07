"""Sandbox inputs that must never be trusted: the child environment, synced folders, requirements.

Pure helpers used by ``apps.foundry.sandbox``; nothing here starts a process.

* The child environment is built from an allowlist. Nothing else is inherited
  from this process, so ``DATABASE_URL``, ``SECRET_KEY``, ``MODEL_API_KEY`` and
  any ``*_KEY`` / ``*_TOKEN`` / ``*_SECRET`` never reach sandboxed code, and
  caller overrides with such names are refused.
* ``synced_folder_root`` detects OneDrive-synced paths (slow or failing
  Docker Desktop bind mounts, files locked by sync).
* ``validate_requirements_file`` refuses requirements options that redirect
  where packages come from (index URLs, find-links, trusted hosts), editable
  or local-path installs and forced source builds.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)


class SandboxError(RuntimeError):
    """Base class for sandbox failures."""


class SandboxConfigurationError(SandboxError, ValueError):
    """Bad mode, Python version, run id, workspace, argument or environment override."""


class UnsafeRequirementsError(SandboxConfigurationError):
    """A requirements file or package spec would redirect or widen what the install may run."""


DOCKER_VENV: str = "/opt/lazarus-venv"
DOCKER_PATH: str = f"{DOCKER_VENV}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

SECRET_NAME_PATTERN: re.Pattern[str] = re.compile(
    r"(^|_)(KEY|KEYS|TOKEN|TOKENS|SECRET|SECRETS|PASSWORD|PASSWD|CREDENTIAL|CREDENTIALS|AUTH|DSN)(_|$)",
    re.IGNORECASE,
)
INFRASTRUCTURE_VARIABLES: frozenset[str] = frozenset({
    "DATABASE_URL", "REDIS_URL", "SECRET_KEY", "MODEL_API_KEY", "MODEL_BASE_URL",
    "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", "DJANGO_SETTINGS_MODULE",
})
# Inherited on Windows because Python, pip and the C runtime need them; none carries a secret.
WINDOWS_PASSTHROUGH_VARIABLES: tuple[str, ...] = (
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
)
FIXED_CHILD_ENV: Mapping[str, str] = {
    "LANG": "C.UTF-8",
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTHONIOENCODING": "utf-8",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
}
PROTECTED_VARIABLES: frozenset[str] = frozenset({
    "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR",
    "VIRTUAL_ENV", "SYSTEMROOT",
})
_ENV_NAME: re.Pattern[str] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_secret_variable(name: str) -> bool:
    return name.upper() in INFRASTRUCTURE_VARIABLES or SECRET_NAME_PATTERN.search(name) is not None


def validate_env_overrides(overrides: Mapping[str, str] | None) -> dict[str, str]:
    """Return ``overrides`` after refusing secret-looking, sandbox-managed or malformed entries."""
    checked: dict[str, str] = {}
    for name, value in (overrides or {}).items():
        if not isinstance(name, str) or not isinstance(value, str) or not _ENV_NAME.fullmatch(name):
            raise SandboxConfigurationError(f"invalid environment override {name!r}")
        if "\x00" in value:
            raise SandboxConfigurationError(f"environment override {name} contains a NUL character")
        if is_secret_variable(name):
            raise SandboxConfigurationError(
                f"environment override {name} looks like a secret; secrets never enter the sandbox"
            )
        if name.upper() in PROTECTED_VARIABLES:
            raise SandboxConfigurationError(f"environment override {name} is managed by the sandbox")
        checked[name] = value
    return checked


def venv_bin_dir(venv_dir: Path, *, windows: bool = os.name == "nt") -> Path:
    return venv_dir / ("Scripts" if windows else "bin")


def subprocess_environment(
    *,
    source: Mapping[str, str],
    home: Path,
    tmp: Path,
    venv_dir: Path | None = None,
    overrides: Mapping[str, str] | None = None,
    windows: bool = os.name == "nt",
) -> dict[str, str]:
    """The child environment for subprocess mode: an allowlist of ``source`` plus fixed values.

    Only ``PATH`` (and, on Windows, ``WINDOWS_PASSTHROUGH_VARIABLES``) come from
    ``source``. ``HOME``/``USERPROFILE``/``APPDATA``/``LOCALAPPDATA`` and the
    temp directories point into the run directory, so pip's cache and config
    and any user-site packages of the real account are not used.
    """
    upper_source = {key.upper(): value for key, value in source.items()}
    env: dict[str, str] = {}
    if windows:
        for name in WINDOWS_PASSTHROUGH_VARIABLES:
            if name in upper_source:
                env[name] = upper_source[name]
    path_entries = [str(venv_bin_dir(venv_dir, windows=windows))] if venv_dir is not None else []
    if upper_source.get("PATH"):
        path_entries.append(upper_source["PATH"])
    env["PATH"] = os.pathsep.join(path_entries) if path_entries else os.defpath
    env["HOME"] = str(home)
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(tmp)
    if windows:
        env["USERPROFILE"] = str(home)
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    env.update(FIXED_CHILD_ENV)
    if venv_dir is not None:
        env["VIRTUAL_ENV"] = str(venv_dir)
    env.update(validate_env_overrides(overrides))
    return env


def docker_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """The container environment: fixed values only, nothing from this process.

    Variables the image itself defines (``PYTHON_VERSION``, and ``GPG_KEY``, the
    public ID of the key that signed the CPython release) remain visible.
    """
    env: dict[str, str] = {
        "PATH": DOCKER_PATH, "HOME": "/tmp", "TMPDIR": "/tmp", "VIRTUAL_ENV": DOCKER_VENV,
        "PIP_NO_CACHE_DIR": "1", **FIXED_CHILD_ENV,
    }
    env.update(validate_env_overrides(overrides))
    return env


# ---------------------------------------------------------------------------
# Synced folders (OneDrive)
# ---------------------------------------------------------------------------
SYNCED_FOLDER_ENV_VARIABLES: tuple[str, ...] = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
_warned_synced_paths: set[str] = set()


def _normalized(path: str | Path) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def synced_folder_root(path: Path | str, environ: Mapping[str, str] | None = None) -> str | None:
    """The OneDrive folder containing ``path``, or None. Ignores case and separator style.

    Checks the ``OneDrive*`` environment variables Windows sets, then any path
    component named ``OneDrive`` or ``OneDrive - <organization>``.
    """
    source = os.environ if environ is None else environ
    normalized = _normalized(path)
    for name in SYNCED_FOLDER_ENV_VARIABLES:
        root = source.get(name, "")
        if root and (normalized == _normalized(root) or normalized.startswith(_normalized(root) + "/")):
            return root
    original_parts = str(path).replace("\\", "/").split("/")
    for index, part in enumerate(normalized.split("/")):
        if part == "onedrive" or part.startswith("onedrive - "):
            return "/".join(original_parts[: index + 1])
    return None


def warn_if_synced_folder(path: Path, label: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Log (once per path) a warning when ``path`` is inside a OneDrive-synced folder."""
    root = synced_folder_root(path, environ)
    if root is not None and str(path) not in _warned_synced_paths:
        _warned_synced_paths.add(str(path))
        logger.warning(
            "%s %s is inside the OneDrive-synced folder %s. Sync can lock or rewrite files while the "
            "sandbox runs, and Docker Desktop bind mounts of synced folders are slow or can fail. Point "
            "WORKSPACE_ROOT and SANDBOX_RUNS_ROOT at a non-synced path such as C:\\lazarus-data.",
            label, path, root,
        )
    return root


# ---------------------------------------------------------------------------
# Requirements validation
# ---------------------------------------------------------------------------
MAX_REQUIREMENTS_FILE_BYTES: int = 1024 * 1024
MAX_REQUIREMENTS_INCLUDE_DEPTH: int = 5
# The only options a requirements file may use. Index, find-links, trusted-host, editable and
# no-binary options are refused: they redirect where packages come from or force source builds.
ALLOWED_REQUIREMENT_OPTIONS: frozenset[str] = frozenset({"--pre", "--prefer-binary", "--only-binary", "--hash"})
INCLUDE_REQUIREMENT_OPTIONS: frozenset[str] = frozenset({"-r", "--requirement", "-c", "--constraint"})
_OPTION_LINE: re.Pattern[str] = re.compile(r"(-{1,2}[A-Za-z][A-Za-z0-9-]*)(?:[=\s]+(.*))?$")
_LOCAL_PATH_SPEC: re.Pattern[str] = re.compile(r"^(\.|/|\\|~|[A-Za-z]:[\\/])")
_COMMENT: re.Pattern[str] = re.compile(r"(?:^|\s)#")


def is_inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _check_requirement_line(line: str, where: str, include: Callable[[str], None]) -> None:
    if line.startswith("-"):
        match = _OPTION_LINE.match(line)
        option = match.group(1) if match else line.split()[0]
        value = ((match.group(2) if match else "") or "").strip()
        if option in INCLUDE_REQUIREMENT_OPTIONS:
            if not value or "://" in value:
                raise UnsafeRequirementsError(f"{where}: include {value!r} is not allowed")
            include(value)
        elif option not in ALLOWED_REQUIREMENT_OPTIONS:
            raise UnsafeRequirementsError(f"{where}: option {option} is not allowed in the sandbox")
    elif "://" in line or " @ " in line or _LOCAL_PATH_SPEC.match(line):
        raise UnsafeRequirementsError(f"{where}: URL and local-path requirements are not allowed")


def validate_requirements_file(workspace: Path, requirement_file: Path | str) -> PurePosixPath:
    """Check a requirements file and its ``-r``/``-c`` includes; return its workspace-relative path.

    Every file must resolve (symlinks followed) inside ``workspace``, be at most
    ``MAX_REQUIREMENTS_FILE_BYTES`` and pass ``_check_requirement_line``.
    """
    root = workspace.resolve()
    seen: set[Path] = set()

    def check(candidate: Path, depth: int) -> Path:
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if resolved == root or not is_inside(resolved, root):
            raise UnsafeRequirementsError(f"requirements file {candidate} is outside the workspace")
        relative = resolved.relative_to(root).as_posix()
        if not resolved.is_file():
            raise UnsafeRequirementsError(f"requirements file {relative} does not exist")
        if resolved.stat().st_size > MAX_REQUIREMENTS_FILE_BYTES:
            raise UnsafeRequirementsError(f"requirements file {relative} is too large")
        if resolved in seen:
            return resolved
        if depth > MAX_REQUIREMENTS_INCLUDE_DEPTH:
            raise UnsafeRequirementsError(f"requirements includes nest deeper than {MAX_REQUIREMENTS_INCLUDE_DEPTH}")
        seen.add(resolved)
        text = resolved.read_text(encoding="utf-8", errors="replace")
        for number, raw in enumerate(text.splitlines(), start=1):
            line = _COMMENT.split(raw, maxsplit=1)[0].strip().rstrip("\\").strip()
            if line:
                _check_requirement_line(
                    line, f"{relative}:{number}", lambda value: check(resolved.parent / value, depth + 1)
                )
        return resolved

    return PurePosixPath(check(Path(requirement_file), 0).relative_to(root).as_posix())


def validate_package_spec(spec: str) -> str:
    """A requirement such as ``pytest==7.4.4`` or ``requests[socks]>=2.25``: never an option, URL or path."""
    if (
        not isinstance(spec, str)
        or not spec.strip()
        or not spec.strip()[:1].isalnum()
        or any(token in spec for token in ("://", "@", "/", "\\", "\x00", "\n", "\r"))
    ):
        raise UnsafeRequirementsError(f"package spec {spec!r} is not allowed")
    return spec.strip()
