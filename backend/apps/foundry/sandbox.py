"""Foundry sandbox: create an environment, install dependencies and run tests for a workspace.

The Foundry agents (Task 6.2) use one ``Sandbox`` per ``FoundryRun``::

    with get_sandbox(project, run.id, python_version="3.9") as sandbox:
        sandbox.create_environment()
        sandbox.install(["requirements.txt"])
        baseline = sandbox.run_pytest(timeout=600)

The sandbox runs code from repositories we don't control; the security model
is in design.md ("Foundry: sandbox").

* Docker mode is the only mode for untrusted repositories: a fresh hardened
  container per command (uid 1000:1000, ``cap_drop=ALL``, no-new-privileges,
  read-only root fs, ``/tmp`` tmpfs, memory/CPU/PID limits), mounting only the
  workspace, the run's results directory and a per-run venv volume. The
  install phase has network (``bridge``); the execute phase has none.
* Subprocess mode has no isolation and is allowed only for the golden demo,
  unless ``SANDBOX_ALLOW_SUBPROCESS_FOR_UNTRUSTED`` is set.
* In both modes the child environment is an allowlist (``sandbox_env``), every
  command is an argv list (never a shell) and every command has a timeout.
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
import math
import os
import re
import shutil
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

import docker
import requests
from django.conf import settings
from docker.errors import DockerException, ImageNotFound
from docker.types import LogConfig, Mount

from apps.foundry.junit import EMPTY_REPORT, JUnitError, JUnitReport, TestCaseResult, TestOutcome, TestTotals
from apps.foundry.junit import parse_junit_file
from apps.foundry.sandbox_env import (
    DOCKER_VENV,
    SandboxConfigurationError,
    SandboxError,
    UnsafeRequirementsError,
    docker_environment,
    is_inside,
    subprocess_environment,
    validate_package_spec,
    validate_requirements_file,
    venv_bin_dir,
    warn_if_synced_folder,
)
from apps.foundry.sandbox_process import (
    INTERPRETER_PROBE_TIMEOUT_SECONDS,
    READER_JOIN_SECONDS,
    VERSION_PROBE,
    OutputCallback,
    OutputSink,
    ProcessOutcome,
    SandboxUnavailableError,
    attach_readers,
    resolve_interpreter,
    run_process,
    spawn,
    start_reader,
    stop_process,
)
from apps.ingestion.git_forensics import UnsafeWorkspaceError, resolve_workspace_dir
from apps.projects.git_runner import remove_tree

if TYPE_CHECKING:
    from apps.projects.models import Project

logger = logging.getLogger(__name__)

__all__ = [
    "DockerSandbox", "ExecResult", "OutputCallback", "Phase", "Sandbox", "SandboxConfigurationError",
    "SandboxError", "SandboxInfo", "SandboxStateError", "SandboxUnavailableError", "ServiceHandle",
    "SubprocessSandbox", "TestRunResult", "UnsafeRequirementsError", "UntrustedSandboxError",
    "cleanup_docker_run", "docker_client", "docker_unavailable_reason", "get_sandbox",
    "sweep_orphaned_sandboxes",
]

MODE_SUBPROCESS: str = "subprocess"
MODE_DOCKER: str = "docker"

# Docker layout and hardening.
DOCKER_WORKSPACE: str = "/workspace"
DOCKER_RESULTS: str = "/lazarus-results"
DOCKER_USER: str = "1000:1000"
DOCKER_ROOT_USER: str = "0:0"
DOCKER_TMPFS: Mapping[str, str] = {"/tmp": "rw,nosuid,nodev,size=512m"}
DOCKER_LOG_MAX_SIZE: str = "20m"
DOCKER_INSTALL_NETWORK: str = "bridge"
DOCKER_NO_NETWORK: str = "none"
DOCKER_HOSTNAME: str = "lazarus-sandbox"
DOCKER_CLIENT_TIMEOUT_SECONDS: int = 60
DOCKER_INIT_TIMEOUT_SECONDS: float = 120.0
# Extra time `container.wait` gets after the command timeout while the kill takes effect.
DOCKER_WAIT_GRACE_SECONDS: float = 30.0

LABEL_SANDBOX: str = "lazarus.sandbox"
LABEL_RUN: str = "lazarus.foundry_run"
LABEL_PROJECT: str = "lazarus.project"
LABEL_ROLE: str = "lazarus.role"

JUNIT_FILE_NAME: str = "junit.xml"
PYTEST_EXIT_INTERRUPTED: int = 2
RUN_ID_PATTERN: re.Pattern[str] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_COLLECTION_ERROR_OUTPUT: re.Pattern[str] = re.compile(r"errors? during collection|ERROR collecting")


class UntrustedSandboxError(SandboxError, PermissionError):
    """Subprocess mode was requested for a repository that is not the trusted golden demo."""


class SandboxStateError(SandboxError):
    """The call is not valid in the sandbox's current state (no environment yet, or closed)."""


class Phase(StrEnum):
    SETUP = "setup"
    INSTALL = "install"
    EXECUTE = "execute"
    SERVICE = "service"


@dataclass(frozen=True)
class ExecResult:
    """One command. ``stdout`` and ``stderr`` are each capped at ``SANDBOX_MAX_OUTPUT_BYTES``.

    ``exit_code`` is what the process or container returned. After a timeout it
    is the killed process's code (137 in Docker); ``timed_out`` is authoritative.
    ``network_isolated`` is False for every subprocess-mode command.
    """

    argv: tuple[str, ...]
    phase: Phase
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_seconds: float
    timed_out: bool
    mode: str
    network_isolated: bool
    image: str | None
    python_version: str | None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class TestRunResult:
    """A ``run_pytest`` call: the command and its parsed JUnit report (empty when unreadable)."""

    __test__ = False  # not a pytest test class

    execution: ExecResult
    report: JUnitReport
    junit_error: str | None
    collection_error: bool

    @property
    def exit_code(self) -> int:
        return self.execution.exit_code

    @property
    def timed_out(self) -> bool:
        return self.execution.timed_out

    @property
    def cases(self) -> tuple[TestCaseResult, ...]:
        return self.report.cases

    @property
    def totals(self) -> TestTotals:
        return self.report.totals

    @property
    def succeeded(self) -> bool:
        return self.execution.ok and not self.collection_error

    def nodeids(self, outcome: TestOutcome) -> tuple[str, ...]:
        return tuple(case.nodeid for case in self.cases if case.outcome is outcome)


@dataclass(frozen=True)
class SandboxInfo:
    """What the sandbox is, stated plainly for telemetry and the FoundryRun log."""

    mode: str
    run_id: str
    project_id: str
    requested_python_version: str
    python_version: str | None
    python_executable: str | None
    image: str | None
    network_isolated: bool  # for the execute phase
    notes: tuple[str, ...]


@dataclass(frozen=True)
class CleanupReport:
    containers: int = 0
    volumes: int = 0
    networks: int = 0
    run_dirs: int = 0


def _check_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise SandboxConfigurationError("argv must be a non-empty list of strings (never a shell string)")
    checked = tuple(argv)
    if any(not isinstance(arg, str) or "\x00" in arg for arg in checked):
        raise SandboxConfigurationError("argv items must be strings without NUL characters")
    return checked


def _check_timeout(timeout: float | None, default: float) -> float:
    value = float(default if timeout is None else timeout)
    if not math.isfinite(value) or value <= 0:
        raise SandboxConfigurationError(f"timeout must be a positive number of seconds, got {timeout!r}")
    return value


# ---------------------------------------------------------------------------
# Sandbox base
# ---------------------------------------------------------------------------
class Sandbox(ABC):
    """One isolated environment for one Foundry run. Use it as a context manager; not thread-safe."""

    mode: ClassVar[str]

    def __init__(
        self,
        *,
        project_id: str,
        run_id: str,
        workspace: Path,
        run_dir: Path,
        python_version: str,
        max_output_bytes: int,
        command_timeout: float,
        install_timeout: float,
    ) -> None:
        self.project_id = project_id
        self.run_id = run_id
        self.workspace = workspace
        self.run_dir = run_dir
        self.results_dir = run_dir / "results"
        self.requested_python_version = python_version
        self.python_version: str | None = None
        self.python_executable: str | None = None
        self.max_output_bytes = max_output_bytes
        self.command_timeout = command_timeout
        self.install_timeout = install_timeout
        self._environment_ready = False
        self._closed = False
        self._services: list[ServiceHandle] = []

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def prepare(self) -> None:
        """(Re)create the run directory; ``get_sandbox`` calls this."""
        if self.run_dir.exists():
            remove_tree(self.run_dir)
        self.results_dir.mkdir(parents=True)

    @property
    def environment_ready(self) -> bool:
        return self._environment_ready

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> None:
        if self._closed:
            raise SandboxStateError("the sandbox is closed")

    def _require_environment(self) -> None:
        self._require_open()
        if not self._environment_ready:
            raise SandboxStateError("call create_environment() first")

    def close(self) -> None:
        """Stop services and remove every resource of this run (containers, volumes, run dir). Idempotent."""
        if self._closed:
            return
        self._closed = True
        for service in list(self._services):
            with contextlib.suppress(Exception):
                service.stop()
        try:
            self._cleanup()
        finally:
            if self.run_dir.exists():
                try:
                    remove_tree(self.run_dir)
                except OSError:
                    logger.warning("could not remove the sandbox run directory %s", self.run_dir, exc_info=True)

    # -- implemented per mode -------------------------------------------------
    @property
    @abstractmethod
    def info(self) -> SandboxInfo: ...

    @abstractmethod
    def _create_environment(self) -> ExecResult: ...

    @abstractmethod
    def _run(
        self, argv: tuple[str, ...], *, phase: Phase, timeout: float, env_overrides: Mapping[str, str] | None,
        on_output: OutputCallback | None, network: bool,
    ) -> ExecResult: ...

    @abstractmethod
    def _sandbox_path(self, host_path: Path) -> str:
        """``host_path`` (in the workspace or results directory) as the sandboxed command sees it."""

    @abstractmethod
    def _start_service(
        self, argv: tuple[str, ...], *, host_port: int, env_overrides: Mapping[str, str] | None,
        on_output: OutputCallback | None,
    ) -> ServiceHandle: ...

    @abstractmethod
    def _cleanup(self) -> None: ...

    def _find_links_path(self, find_links: Path) -> str:
        raise SandboxConfigurationError(f"find_links is not supported in {self.mode} mode")

    # -- public API ---------------------------------------------------------
    def create_environment(self) -> ExecResult:
        """Create the sandbox virtualenv for ``requested_python_version``. Check ``ExecResult.ok``."""
        self._require_open()
        if self._environment_ready:
            raise SandboxStateError("the environment already exists")
        return self._create_environment()

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        """Run ``argv`` in the execute phase (Docker: no network); ``python``/``pip`` mean the sandbox venv's.

        The working directory is the workspace. ``on_output(stream, line)`` gets
        each output line as it arrives (the caller batches for SANDBOX_LOG).
        """
        self._require_open()
        return self._run(
            _check_argv(argv), phase=Phase.EXECUTE, timeout=_check_timeout(timeout, self.command_timeout),
            env_overrides=env_overrides, on_output=on_output, network=False,
        )

    def install(
        self,
        requirements_files: Sequence[Path | str] = (),
        *,
        packages: Sequence[str] = (),
        commands: Sequence[Sequence[str]] = (),
        network: bool = True,
        allow_source_builds: bool = False,
        find_links: Path | None = None,
        timeout: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> tuple[ExecResult, ...]:
        """Install dependencies into the sandbox venv (the Docker install phase).

        ``requirements_files`` (workspace-relative, checked by
        ``validate_requirements_file``) and ``packages`` (requirement specs) go
        to one ``pip install``. It is wheels-only unless ``allow_source_builds``,
        because a source build runs the package's own build code with network
        access. ``commands`` are extra argv lists run in the same phase.
        ``network=False`` installs offline (``--no-index``; in Docker also no
        network). ``find_links`` is a local wheel directory (subprocess mode only).
        """
        self._require_environment()
        limit = _check_timeout(timeout, self.install_timeout)
        files = [validate_requirements_file(self.workspace, item) for item in requirements_files]
        specs = [validate_package_spec(spec) for spec in packages]
        argvs: list[tuple[str, ...]] = []
        if files or specs:
            pip = ["python", "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
            if not allow_source_builds:
                pip.append("--only-binary=:all:")
            if not network:
                pip.append("--no-index")
            if find_links is not None:
                pip += ["--find-links", self._find_links_path(find_links)]
            for relative in files:
                pip += ["-r", self._sandbox_path(self.workspace / relative)]
            argvs.append((*pip, *specs))
        argvs += [_check_argv(command) for command in commands]
        results: list[ExecResult] = []
        for argv in argvs:
            result = self._run(
                argv, phase=Phase.INSTALL, timeout=limit, env_overrides=None, on_output=on_output, network=network
            )
            results.append(result)
            if not result.ok:
                break
        return tuple(results)

    def run_pytest(
        self,
        test_ids: Sequence[str] | None = None,
        *,
        timeout: float | None = None,
        extra_args: Sequence[str] = (),
        env_overrides: Mapping[str, str] | None = None,
        on_output: OutputCallback | None = None,
    ) -> TestRunResult:
        """``python -m pytest -p no:cacheprovider --junitxml=<results>/junit.xml -q [test_ids]``, parsed."""
        self._require_environment()
        ids = tuple(test_ids or ())
        if any(not isinstance(item, str) or not item or item.startswith("-") or "\x00" in item for item in ids):
            raise SandboxConfigurationError("test ids must be paths or node ids, not options")
        extras = _check_argv(extra_args) if extra_args else ()
        junit = self.results_dir / JUNIT_FILE_NAME
        if junit.is_symlink() or junit.is_file():
            junit.unlink()
        elif junit.exists():
            remove_tree(junit)
        argv = (
            "python", "-m", "pytest", "-p", "no:cacheprovider",
            f"--junitxml={self._sandbox_path(junit)}", "-q", *extras, *ids,
        )
        execution = self.run(argv, timeout=timeout, env_overrides=env_overrides, on_output=on_output)
        junit_error: str | None = None
        try:
            report = parse_junit_file(junit, results_root=self.results_dir, workspace=self.workspace)
        except JUnitError as exc:
            report, junit_error = EMPTY_REPORT, str(exc)
        collection_error = report.collection_error or (
            execution.exit_code == PYTEST_EXIT_INTERRUPTED
            and _COLLECTION_ERROR_OUTPUT.search(execution.stdout + execution.stderr) is not None
        )
        return TestRunResult(execution=execution, report=report, junit_error=junit_error,
                             collection_error=collection_error)

    def start_service(
        self,
        argv: Sequence[str],
        *,
        host_port: int | None = None,
        env_overrides: Mapping[str, str] | None = None,
        on_output: OutputCallback | None = None,
    ) -> ServiceHandle:
        """Start a preview server published on ``127.0.0.1:<host_port>`` only (default SANDBOX_PREVIEW_PORT).

        The command gets ``PORT`` and ``HOST`` and must listen on them. Docker
        cannot publish a port from a ``network_mode="none"`` container, so a
        service runs on a per-run bridge network and HAS outbound network access
        (the documented exception). Stop it with ``ServiceHandle.stop()``;
        ``close()`` stops it too.
        """
        self._require_environment()
        port = int(settings.SANDBOX_PREVIEW_PORT if host_port is None else host_port)
        if not 1 <= port <= 65535:
            raise SandboxConfigurationError(f"invalid preview port {port}")
        handle = self._start_service(_check_argv(argv), host_port=port, env_overrides=env_overrides,
                                     on_output=on_output)
        self._services.append(handle)
        return handle

    def _forget_service(self, handle: ServiceHandle) -> None:
        with contextlib.suppress(ValueError):
            self._services.remove(handle)


class ServiceHandle:
    """A running preview service. ``stop()`` kills it and returns its captured output (idempotent)."""

    def __init__(
        self, *, sandbox: Sandbox, argv: tuple[str, ...], url: str, host_port: int, network_isolated: bool,
        stopper: Callable[[], int], sink: OutputSink, readers: Sequence[threading.Thread], image: str | None,
    ) -> None:
        self.argv = argv
        self.url = url
        self.host_port = host_port
        self.network_isolated = network_isolated
        self._sandbox = sandbox
        self._stopper = stopper
        self._sink = sink
        self._readers = list(readers)
        self._image = image
        self._started = time.monotonic()
        self._result: ExecResult | None = None

    def stop(self) -> ExecResult:
        if self._result is not None:
            return self._result
        exit_code = self._stopper()
        for reader in self._readers:
            reader.join(READER_JOIN_SECONDS)
        self._sink.finish()
        self._result = ExecResult(
            argv=self.argv, phase=Phase.SERVICE, exit_code=exit_code,
            stdout=self._sink.stdout.text(), stderr=self._sink.stderr.text(),
            stdout_truncated=self._sink.stdout.truncated, stderr_truncated=self._sink.stderr.truncated,
            duration_seconds=round(time.monotonic() - self._started, 3), timed_out=False,
            mode=self._sandbox.mode, network_isolated=self.network_isolated, image=self._image,
            python_version=self._sandbox.python_version,
        )
        self._sandbox._forget_service(self)
        return self._result


# ---------------------------------------------------------------------------
# Subprocess mode (trusted fixtures only)
# ---------------------------------------------------------------------------
class SubprocessSandbox(Sandbox):
    """A venv in the run directory (outside the workspace). NO network, filesystem or process isolation."""

    mode: ClassVar[str] = MODE_SUBPROCESS
    NOTES: ClassVar[tuple[str, ...]] = (
        "subprocess mode: commands run directly on the host as the backend's user",
        "no network isolation: installs and tests can reach the network",
        "no filesystem isolation: commands can read and write whatever the backend's user can",
        "processes that leave the command's process tree are not tracked",
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.venv_dir = self.run_dir / "venv"
        self.home_dir = self.run_dir / "home"
        self.tmp_dir = self.run_dir / "tmp"

    def prepare(self) -> None:
        super().prepare()
        for directory in (self.home_dir / "AppData" / "Roaming", self.home_dir / "AppData" / "Local", self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def venv_python(self) -> Path:
        return venv_bin_dir(self.venv_dir) / ("python.exe" if os.name == "nt" else "python")

    @property
    def info(self) -> SandboxInfo:
        return SandboxInfo(
            mode=self.mode, run_id=self.run_id, project_id=self.project_id,
            requested_python_version=self.requested_python_version, python_version=self.python_version,
            python_executable=self.python_executable, image=None, network_isolated=False, notes=self.NOTES,
        )

    def child_environment(self, overrides: Mapping[str, str] | None = None) -> dict[str, str]:
        return subprocess_environment(
            source=os.environ, home=self.home_dir, tmp=self.tmp_dir,
            venv_dir=self.venv_dir if self._environment_ready else None, overrides=overrides,
        )

    def _sandbox_path(self, host_path: Path) -> str:
        return str(host_path)

    def _find_links_path(self, find_links: Path) -> str:
        directory = Path(find_links).resolve()
        if not directory.is_dir():
            raise SandboxConfigurationError(f"find_links directory {directory} does not exist")
        return str(directory)

    def _resolve_argv(self, argv: tuple[str, ...], env: Mapping[str, str]) -> list[str]:
        first, rest = argv[0], list(argv[1:])
        if first in ("python", "pip"):
            self._require_environment()
            return [str(self.venv_python), *(["-m", "pip"] if first == "pip" else []), *rest]
        if os.path.isabs(first) or "/" in first or "\\" in first:
            return [first, *rest]
        found = shutil.which(first, path=env.get("PATH"))
        if found is None:
            raise SandboxConfigurationError(f"{first!r} was not found on the sandbox PATH")
        return [found, *rest]

    def _result(self, argv: tuple[str, ...], phase: Phase, outcome: ProcessOutcome) -> ExecResult:
        return ExecResult(
            argv=argv, phase=phase, exit_code=outcome.exit_code, stdout=outcome.stdout, stderr=outcome.stderr,
            stdout_truncated=outcome.stdout_truncated, stderr_truncated=outcome.stderr_truncated,
            duration_seconds=outcome.duration_seconds, timed_out=outcome.timed_out, mode=self.mode,
            network_isolated=False, image=None, python_version=self.python_version,
        )

    def _spawn(
        self, argv: tuple[str, ...], resolved: Sequence[str], *, phase: Phase, timeout: float,
        env: Mapping[str, str], on_output: OutputCallback | None,
    ) -> ExecResult:
        outcome = run_process(resolved, cwd=self.workspace, env=env, timeout=timeout,
                              max_output_bytes=self.max_output_bytes, on_output=on_output)
        return self._result(argv, phase, outcome)

    def _create_environment(self) -> ExecResult:
        env = self.child_environment()
        choice = resolve_interpreter(self.requested_python_version, env=env)
        if not choice.matches(self.requested_python_version):
            logger.warning(
                "Python %s is not available; the subprocess sandbox uses Python %s (%s) instead",
                self.requested_python_version, choice.version, choice.source,
            )
        result = self._spawn(
            ("python", "-m", "venv", "<run>/venv"), [str(choice.executable), "-m", "venv", str(self.venv_dir)],
            phase=Phase.SETUP, timeout=self.install_timeout, env=env, on_output=None,
        )
        if not result.ok:
            return dataclasses.replace(result, python_version=choice.version)
        self._environment_ready = True
        probe = self._spawn(
            ("python", "-c", VERSION_PROBE), [str(self.venv_python), "-c", VERSION_PROBE], phase=Phase.SETUP,
            timeout=INTERPRETER_PROBE_TIMEOUT_SECONDS, env=self.child_environment(), on_output=None,
        )
        lines = probe.stdout.strip().splitlines()
        self.python_version = lines[-1].strip() if probe.ok and lines else choice.version
        self.python_executable = str(self.venv_python)
        return dataclasses.replace(result, python_version=self.python_version)

    def _run(
        self, argv: tuple[str, ...], *, phase: Phase, timeout: float, env_overrides: Mapping[str, str] | None,
        on_output: OutputCallback | None, network: bool,
    ) -> ExecResult:
        env = self.child_environment(env_overrides)
        return self._spawn(argv, self._resolve_argv(argv, env), phase=phase, timeout=timeout, env=env,
                           on_output=on_output)

    def _start_service(
        self, argv: tuple[str, ...], *, host_port: int, env_overrides: Mapping[str, str] | None,
        on_output: OutputCallback | None,
    ) -> ServiceHandle:
        env = self.child_environment({**(env_overrides or {}), "PORT": str(host_port), "HOST": "127.0.0.1"})
        sink = OutputSink(self.max_output_bytes, on_output)
        process = spawn(self._resolve_argv(argv, env), cwd=self.workspace, env=env)
        readers = attach_readers(process, sink)

        def stopper() -> int:
            return process.returncode if process.poll() is not None else stop_process(process)

        return ServiceHandle(
            sandbox=self, argv=argv, url=f"http://127.0.0.1:{host_port}", host_port=host_port,
            network_isolated=False, stopper=stopper, sink=sink, readers=readers, image=None,
        )

    def _cleanup(self) -> None:
        """Nothing lives outside the run directory, which ``close()`` removes."""


# ---------------------------------------------------------------------------
# Docker mode (the only mode for untrusted repositories)
# ---------------------------------------------------------------------------
def docker_client(timeout: int = DOCKER_CLIENT_TIMEOUT_SECONDS) -> docker.DockerClient:
    """A client for the local Docker daemon; ``SandboxUnavailableError`` when it can't be reached."""
    try:
        client = docker.from_env(timeout=timeout)
        client.ping()
    except DockerException as exc:
        raise SandboxUnavailableError(
            "the Docker daemon is not reachable; start Docker Desktop (README, 'Docker sandbox')"
        ) from exc
    return client


def docker_unavailable_reason() -> str | None:
    """Why Docker can't be used here, or None when the daemon answers a ping."""
    try:
        docker_client(timeout=10).close()
    except SandboxUnavailableError as exc:
        cause = exc.__cause__
        return f"{exc} ({type(cause).__name__}: {cause})" if cause else str(exc)
    return None


def _label_filter(**labels: str) -> dict[str, list[str]]:
    return {"label": [f"{key}={value}" for key, value in labels.items()]}


def _remove_labelled(client: Any, filters: Mapping[str, list[str]]) -> CleanupReport:
    containers = volumes = networks = 0
    for container in client.containers.list(all=True, filters=dict(filters)):
        with contextlib.suppress(DockerException):
            container.remove(force=True)
            containers += 1
    # Volumes after containers: a volume in use by a container can't be removed.
    for volume in client.volumes.list(filters=dict(filters)):
        with contextlib.suppress(DockerException):
            volume.remove(force=True)
            volumes += 1
    for network in client.networks.list(filters=dict(filters)):
        with contextlib.suppress(DockerException):
            network.remove()
            networks += 1
    return CleanupReport(containers=containers, volumes=volumes, networks=networks)


def cleanup_docker_run(client: Any, run_id: str) -> CleanupReport:
    """Remove every container, volume and network labelled ``lazarus.foundry_run=<run_id>``."""
    return _remove_labelled(client, _label_filter(**{LABEL_SANDBOX: "true", LABEL_RUN: run_id}))


def sweep_orphaned_sandboxes(client: Any | None = None, *, runs_root: Path | None = None) -> CleanupReport:
    """Remove all labelled sandbox resources and all run directories left by crashed runs.

    Meant for worker startup, when no Foundry run can be active (the Windows
    worker runs ``--pool=solo``). Docker is swept when a client is passed or
    ``SANDBOX_MODE`` is ``docker``; an unreachable daemon is logged, not raised.
    """
    report = CleanupReport()
    if client is None and settings.SANDBOX_MODE == MODE_DOCKER:
        try:
            client = docker_client(timeout=10)
        except SandboxUnavailableError:
            logger.warning("sandbox orphan sweep skipped Docker: the daemon is not reachable")
    if client is not None:
        try:
            report = _remove_labelled(client, _label_filter(**{LABEL_SANDBOX: "true"}))
        except DockerException:
            logger.warning("sandbox orphan sweep could not list Docker resources", exc_info=True)
    root = Path(runs_root if runs_root is not None else settings.SANDBOX_RUNS_ROOT)
    run_dirs = 0
    if root.is_dir():
        for entry in root.iterdir():
            if entry.is_dir() and not entry.is_symlink() and RUN_ID_PATTERN.fullmatch(entry.name):
                with contextlib.suppress(OSError):
                    remove_tree(entry)
                    run_dirs += 1
    return dataclasses.replace(report, run_dirs=run_dirs)


class DockerSandbox(Sandbox):
    """A fresh hardened container per command; the venv lives in a per-run named volume."""

    mode: ClassVar[str] = MODE_DOCKER
    NOTES: ClassVar[tuple[str, ...]] = (
        "docker mode: uid 1000:1000, all capabilities dropped, no-new-privileges, read-only root filesystem",
        "execute phase: no network (network_mode=none); the venv volume is mounted read-only",
        "install phase: default bridge network (the package index, but also the host and the LAN)",
        "preview services run on a per-run bridge network with outbound access, published on 127.0.0.1",
    )

    def __init__(
        self, *, client: Any, image: str, memory_limit: str, cpus: float, pids_limit: int, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.client = client
        self.image = image
        self.memory_limit = memory_limit
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.volume_name = f"lazarus-venv-{self.run_id}"
        self.network_name = f"lazarus-net-{self.run_id}"
        self._network_created = False

    def prepare(self) -> None:
        super().prepare()
        if os.name != "nt":
            # On Linux the container's uid 1000 must be able to write the JUnit report. The parent
            # run directory stays private to the backend's user.
            self.results_dir.chmod(0o777)  # noqa: S103

    @property
    def info(self) -> SandboxInfo:
        return SandboxInfo(
            mode=self.mode, run_id=self.run_id, project_id=self.project_id,
            requested_python_version=self.requested_python_version, python_version=self.python_version,
            python_executable=self.python_executable, image=self.image, network_isolated=True, notes=self.NOTES,
        )

    def labels(self, role: str) -> dict[str, str]:
        return {LABEL_SANDBOX: "true", LABEL_RUN: self.run_id, LABEL_PROJECT: self.project_id, LABEL_ROLE: role}

    def _sandbox_path(self, host_path: Path) -> str:
        resolved = host_path.resolve()
        for root, target in ((self.workspace.resolve(), DOCKER_WORKSPACE), (self.results_dir.resolve(), DOCKER_RESULTS)):
            if is_inside(resolved, root):
                relative = resolved.relative_to(root).as_posix()
                return target if relative == "." else f"{target}/{relative}"
        raise SandboxConfigurationError(f"{host_path} is not inside the workspace or the results directory")

    def mounts(self, phase: Phase) -> list[Mount]:
        """setup: venv rw. install: workspace ro + venv rw. execute/service: workspace rw + results rw + venv ro."""
        venv = Mount(DOCKER_VENV, self.volume_name, type="volume",
                     read_only=phase in (Phase.EXECUTE, Phase.SERVICE))
        if phase is Phase.SETUP:
            return [venv]
        workspace = Mount(DOCKER_WORKSPACE, str(self.workspace), type="bind", read_only=phase is Phase.INSTALL)
        if phase is Phase.INSTALL:
            return [workspace, venv]
        return [workspace, Mount(DOCKER_RESULTS, str(self.results_dir), type="bind", read_only=False), venv]

    def container_kwargs(
        self,
        *,
        role: str,
        phase: Phase,
        argv: Sequence[str],
        environment: Mapping[str, str],
        network_mode: str,
        user: str = DOCKER_USER,
        cap_add: Sequence[str] | None = None,
        ports: Mapping[str, tuple[str, int]] | None = None,
    ) -> dict[str, Any]:
        """Every ``containers.create`` argument besides the image. The hardening is not optional."""
        kwargs: dict[str, Any] = {
            "command": list(argv),
            "user": user,
            "working_dir": "/tmp" if phase is Phase.SETUP else DOCKER_WORKSPACE,
            "environment": dict(environment),
            "labels": self.labels(role),
            "network_mode": network_mode,
            "mounts": self.mounts(phase),
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "read_only": True,
            "tmpfs": dict(DOCKER_TMPFS),
            "mem_limit": self.memory_limit,
            "memswap_limit": self.memory_limit,
            "nano_cpus": int(self.cpus * 1_000_000_000),
            "pids_limit": self.pids_limit,
            "privileged": False,
            "init": True,
            "ipc_mode": "private",
            "hostname": DOCKER_HOSTNAME,
            "stdin_open": False,
            "tty": False,
            "log_config": LogConfig(type="json-file", config={"max-size": DOCKER_LOG_MAX_SIZE, "max-file": "1"}),
        }
        if cap_add:
            kwargs["cap_add"] = list(cap_add)
        if ports:
            kwargs["ports"] = dict(ports)
        return kwargs

    # -- container execution ------------------------------------------------
    def _create_container(self, kwargs: Mapping[str, Any]) -> Any:
        try:
            return self.client.containers.create(self.image, **kwargs)
        except DockerException as exc:
            raise SandboxError(f"could not create the sandbox container: {exc}") from exc

    def _start_container(self, container: Any) -> None:
        try:
            container.start()
        except DockerException as exc:
            raise SandboxError(
                f"could not start the sandbox container: {exc}. On Docker Desktop, check that the workspace "
                "drive is shared (Settings > Resources > File sharing)."
            ) from exc

    def _log_readers(self, container_id: str, sink: OutputSink) -> list[threading.Thread]:
        readers = []
        for capture, stdout, stderr in ((sink.stdout, True, False), (sink.stderr, False, True)):
            try:
                stream = self.client.api.logs(container_id, stdout=stdout, stderr=stderr, stream=True, follow=True)
            except DockerException:
                logger.warning("could not attach to the sandbox container's %s", capture.name, exc_info=True)
                continue
            readers.append(start_reader(stream, capture))
        return readers

    def _result(
        self, argv: tuple[str, ...], phase: Phase, *, exit_code: int, sink: OutputSink, started: float,
        timed_out: bool, network_mode: str,
    ) -> ExecResult:
        return ExecResult(
            argv=argv, phase=phase, exit_code=exit_code, stdout=sink.stdout.text(), stderr=sink.stderr.text(),
            stdout_truncated=sink.stdout.truncated, stderr_truncated=sink.stderr.truncated,
            duration_seconds=round(time.monotonic() - started, 3), timed_out=timed_out, mode=self.mode,
            network_isolated=network_mode == DOCKER_NO_NETWORK, image=self.image, python_version=self.python_version,
        )

    def _run_container(
        self, argv: tuple[str, ...], *, role: str, phase: Phase, timeout: float, environment: Mapping[str, str],
        network_mode: str, on_output: OutputCallback | None, user: str = DOCKER_USER,
        cap_add: Sequence[str] | None = None,
    ) -> ExecResult:
        """Create, start and wait for one container; kill it at ``timeout``; always remove it."""
        kwargs = self.container_kwargs(role=role, phase=phase, argv=argv, environment=environment,
                                       network_mode=network_mode, user=user, cap_add=cap_add)
        sink = OutputSink(self.max_output_bytes, on_output)
        started = time.monotonic()
        container = self._create_container(kwargs)
        timed_out = threading.Event()

        def kill() -> None:
            try:
                container.kill()
            except DockerException:
                return  # it already exited
            timed_out.set()

        try:
            self._start_container(container)
            readers = self._log_readers(container.id, sink)
            timer = threading.Timer(timeout, kill)
            timer.daemon = True
            timer.start()
            try:
                exit_code = int(container.wait(timeout=timeout + DOCKER_WAIT_GRACE_SECONDS).get("StatusCode", -1))
            except (requests.exceptions.RequestException, DockerException):
                kill()  # the daemon did not report an exit in time: treat it as a timeout
                timed_out.set()
                exit_code = -1
            finally:
                timer.cancel()
            for reader in readers:
                reader.join(READER_JOIN_SECONDS)
        finally:
            with contextlib.suppress(DockerException):
                container.remove(force=True)
        sink.finish()
        return self._result(argv, phase, exit_code=exit_code, sink=sink, started=started,
                            timed_out=timed_out.is_set(), network_mode=network_mode)

    def _ensure_image(self) -> None:
        try:
            self.client.images.get(self.image)
        except ImageNotFound:
            logger.info("pulling sandbox image %s", self.image)
            try:
                self.client.images.pull(self.image)
            except DockerException as exc:
                raise SandboxUnavailableError(f"could not pull the sandbox image {self.image}: {exc}") from exc
        except DockerException as exc:
            raise SandboxUnavailableError(f"could not inspect the sandbox image {self.image}: {exc}") from exc

    def _create_environment(self) -> ExecResult:
        self._ensure_image()
        try:
            self.client.volumes.create(name=self.volume_name, driver="local", labels=self.labels("venv"))
        except DockerException as exc:
            raise SandboxError(f"could not create the venv volume: {exc}") from exc
        environment = docker_environment()
        # A new named volume is root-owned and the slim images have no user 1000. Hand the volume
        # to 1000:1000 as root with CAP_CHOWN only, offline, without the workspace mounted.
        chown = self._run_container(
            ("chown", DOCKER_USER, DOCKER_VENV), role="init", phase=Phase.SETUP,
            timeout=DOCKER_INIT_TIMEOUT_SECONDS, environment=environment, network_mode=DOCKER_NO_NETWORK,
            on_output=None, user=DOCKER_ROOT_USER, cap_add=["CHOWN"],
        )
        if not chown.ok:
            return chown
        script = f"import platform, venv; venv.create({DOCKER_VENV!r}, with_pip=True); print(platform.python_version())"
        result = self._run_container(
            ("python", "-c", script), role="setup", phase=Phase.SETUP, timeout=self.install_timeout,
            environment=environment, network_mode=DOCKER_NO_NETWORK, on_output=None,
        )
        if result.ok:
            lines = result.stdout.strip().splitlines()
            self.python_version = lines[-1].strip() if lines else None
            self.python_executable = f"{DOCKER_VENV}/bin/python"
            self._environment_ready = True
        return dataclasses.replace(result, python_version=self.python_version)

    def _run(
        self, argv: tuple[str, ...], *, phase: Phase, timeout: float, env_overrides: Mapping[str, str] | None,
        on_output: OutputCallback | None, network: bool,
    ) -> ExecResult:
        if argv[0] in ("python", "pip"):
            self._require_environment()
        command = ("python", "-m", "pip", *argv[1:]) if argv[0] == "pip" else argv
        network_mode = DOCKER_INSTALL_NETWORK if phase is Phase.INSTALL and network else DOCKER_NO_NETWORK
        return self._run_container(command, role=phase.value, phase=phase, timeout=timeout,
                                   environment=docker_environment(env_overrides), network_mode=network_mode,
                                   on_output=on_output)

    def _start_service(
        self, argv: tuple[str, ...], *, host_port: int, env_overrides: Mapping[str, str] | None,
        on_output: OutputCallback | None,
    ) -> ServiceHandle:
        # Documented exception: a published port needs a network, so services use a per-run bridge
        # network (isolated from other containers, but with outbound access) instead of "none".
        if not self._network_created:
            try:
                self.client.networks.create(self.network_name, driver="bridge", labels=self.labels("service"))
            except DockerException as exc:
                raise SandboxError(f"could not create the preview network: {exc}") from exc
            self._network_created = True
        container_port = int(settings.SANDBOX_PREVIEW_PORT)
        environment = docker_environment({**(env_overrides or {}), "PORT": str(container_port), "HOST": "0.0.0.0"})
        command = ("python", "-m", "pip", *argv[1:]) if argv[0] == "pip" else argv
        kwargs = self.container_kwargs(
            role=Phase.SERVICE.value, phase=Phase.SERVICE, argv=command, environment=environment,
            network_mode=self.network_name, ports={f"{container_port}/tcp": ("127.0.0.1", host_port)},
        )
        sink = OutputSink(self.max_output_bytes, on_output)
        container = self._create_container(kwargs)
        try:
            self._start_container(container)
        except SandboxError:
            with contextlib.suppress(DockerException):
                container.remove(force=True)
            raise
        readers = self._log_readers(container.id, sink)

        def stopper() -> int:
            with contextlib.suppress(DockerException):
                container.kill()
            try:
                exit_code = int(container.wait(timeout=DOCKER_WAIT_GRACE_SECONDS).get("StatusCode", -1))
            except (requests.exceptions.RequestException, DockerException):
                exit_code = -1
            with contextlib.suppress(DockerException):
                container.remove(force=True)
            return exit_code

        return ServiceHandle(
            sandbox=self, argv=argv, url=f"http://127.0.0.1:{host_port}", host_port=host_port,
            network_isolated=False, stopper=stopper, sink=sink, readers=readers, image=self.image,
        )

    def _cleanup(self) -> None:
        try:
            cleanup_docker_run(self.client, self.run_id)
        except DockerException:
            logger.warning("could not remove the Docker resources of sandbox run %s", self.run_id, exc_info=True)


# ---------------------------------------------------------------------------
# Factory and policy
# ---------------------------------------------------------------------------
def check_run_id(run_id: UUID | str) -> str:
    value = str(run_id)
    if not RUN_ID_PATTERN.fullmatch(value):
        raise SandboxConfigurationError(f"run id {value!r} must match {RUN_ID_PATTERN.pattern}")
    return value


def check_python_version(python_version: str | None) -> str:
    version = settings.SANDBOX_DEFAULT_PYTHON if python_version is None else str(python_version)
    if version not in settings.SANDBOX_PYTHON_VERSIONS:
        raise SandboxConfigurationError(
            f"Python {version} is not allowed; SANDBOX_PYTHON_VERSIONS is {', '.join(settings.SANDBOX_PYTHON_VERSIONS)}"
        )
    return version


def check_subprocess_policy(project: Project) -> None:
    """Subprocess mode runs code unisolated, so only the golden demo may use it by default."""
    if project.is_golden_demo:
        return
    if not settings.SANDBOX_ALLOW_SUBPROCESS_FOR_UNTRUSTED:
        raise UntrustedSandboxError(
            "subprocess mode has no isolation and is only allowed for the golden demo repository; "
            "set SANDBOX_MODE=docker (or, on a throwaway machine, SANDBOX_ALLOW_SUBPROCESS_FOR_UNTRUSTED=True)"
        )
    logger.warning(
        "SANDBOX_ALLOW_SUBPROCESS_FOR_UNTRUSTED is set: project %s's code will run on this machine with NO "
        "network, filesystem or process isolation",
        project.pk,
    )


def runs_root() -> Path:
    """``SANDBOX_RUNS_ROOT``, which must not overlap ``WORKSPACE_ROOT`` (checked again at use)."""
    root = Path(settings.SANDBOX_RUNS_ROOT).expanduser().resolve()
    workspaces = Path(settings.WORKSPACE_ROOT).expanduser().resolve()
    if is_inside(root, workspaces) or is_inside(workspaces, root):
        raise SandboxConfigurationError(f"SANDBOX_RUNS_ROOT ({root}) must not overlap WORKSPACE_ROOT ({workspaces})")
    return root


def get_sandbox(
    project: Project,
    run_id: UUID | str,
    *,
    python_version: str | None = None,
    mode: str | None = None,
    docker_client_factory: Callable[[], Any] | None = None,
) -> Sandbox:
    """The sandbox for ``project``'s workspace and one Foundry run, with the policy applied.

    ``mode`` defaults to ``SANDBOX_MODE``; ``python_version`` to
    ``SANDBOX_DEFAULT_PYTHON`` and must be in ``SANDBOX_PYTHON_VERSIONS``.
    Subprocess mode for a project that is not the golden demo raises
    ``UntrustedSandboxError`` unless ``SANDBOX_ALLOW_SUBPROCESS_FOR_UNTRUSTED``.
    The workspace (``project.workspace_path``) must resolve inside
    ``WORKSPACE_ROOT``. ``docker_client_factory`` replaces ``docker_client``
    (tests pass a test double).
    """
    selected = settings.SANDBOX_MODE if mode is None else mode
    if selected not in settings.SANDBOX_MODES:
        raise SandboxConfigurationError(f"sandbox mode must be one of {settings.SANDBOX_MODES}, got {selected!r}")
    version = check_python_version(python_version)
    run = check_run_id(run_id)
    if selected == MODE_SUBPROCESS:
        check_subprocess_policy(project)
    if not project.workspace_path:
        raise SandboxConfigurationError("the project has no workspace yet; ingest it first")
    try:
        workspace = resolve_workspace_dir(project.workspace_path, require_empty=False)
    except UnsafeWorkspaceError as exc:
        raise SandboxConfigurationError(str(exc)) from exc
    root = runs_root()
    warn_if_synced_folder(workspace, "The sandbox workspace")
    warn_if_synced_folder(root, "SANDBOX_RUNS_ROOT")
    common: dict[str, Any] = {
        "project_id": str(project.pk), "run_id": run, "workspace": workspace, "run_dir": root / run,
        "python_version": version, "max_output_bytes": int(settings.SANDBOX_MAX_OUTPUT_BYTES),
        "command_timeout": float(settings.SANDBOX_COMMAND_TIMEOUT_SECONDS),
        "install_timeout": float(settings.SANDBOX_INSTALL_TIMEOUT_SECONDS),
    }
    sandbox: Sandbox
    if selected == MODE_DOCKER:
        client = (docker_client_factory or docker_client)()
        sandbox = DockerSandbox(
            client=client, image=settings.SANDBOX_DOCKER_IMAGE_TEMPLATE.format(version=version),
            memory_limit=settings.SANDBOX_MEMORY_LIMIT, cpus=float(settings.SANDBOX_CPUS),
            pids_limit=int(settings.SANDBOX_PIDS_LIMIT), **common,
        )
    else:
        sandbox = SubprocessSandbox(**common)
    sandbox.prepare()
    return sandbox
