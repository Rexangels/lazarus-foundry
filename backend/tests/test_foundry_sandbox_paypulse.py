"""Real sandbox runs on a private clone of the PayPulse fixture (slow; no database).

* Subprocess mode, offline: a modern pytest from a local wheelhouse
  (``LAZARUS_SANDBOX_WHEELHOUSE``, default ``.lazarus_data/wheelhouse``; fill it
  once with ``pip download pytest --only-binary=:all: -d .lazarus_data/wheelhouse``).
* Subprocess mode, networked (``network``): PayPulse's own pins
  (``pytest==6.2.4``, ``requests==2.25.1``) from PyPI, per installed Python.
* Docker mode (``docker`` + ``network``): ``python:3.9-slim`` and
  ``python:3.11-slim``, plus the isolation probes. Skipped when the daemon is
  unreachable.

On ``main`` PayPulse has 16 tests and exactly one fails:
``test_dispatch_retries_with_exponential_backoff`` (the abandoned retry engine).
"""
from __future__ import annotations

import json
import os
import socket
import textwrap
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import pytest
from django.test import override_settings
from pytest_django import Settings as SettingsWrapper

from apps.foundry.junit import TestOutcome
from apps.foundry.sandbox import (
    LABEL_RUN,
    Sandbox,
    TestRunResult,
    docker_client,
    docker_unavailable_reason,
    get_sandbox,
)
from apps.foundry.sandbox_env import subprocess_environment
from apps.foundry.sandbox_process import resolve_interpreter
from apps.ingestion.git_forensics import clone_local_repository_into_workspace
from apps.projects.git_runner import run_git
from apps.projects.models import Project
from config.paths import REPO_ROOT
from tests.fixtures.build_paypulse_fixture import FixtureBuildResult
from tests.test_foundry_sandbox import PLANTED_SECRETS

pytestmark = pytest.mark.slow

BACKOFF_TEST = "tests/test_webhooks.py::test_dispatch_retries_with_exponential_backoff"
WHEELHOUSE = Path(os.environ.get("LAZARUS_SANDBOX_WHEELHOUSE", REPO_ROOT / ".lazarus_data" / "wheelhouse"))


@lru_cache(maxsize=1)
def pypi_unreachable_reason() -> str | None:
    try:
        socket.create_connection(("pypi.org", 443), timeout=5).close()
    except OSError as exc:
        return f"PyPI is unreachable ({type(exc).__name__}: {exc}); this test installs packages from it"
    return None


@lru_cache(maxsize=1)
def docker_skip_reason() -> str | None:
    return docker_unavailable_reason() or pypi_unreachable_reason()


def require_network() -> None:
    reason = pypi_unreachable_reason()
    if reason:
        pytest.skip(reason)


def require_docker() -> None:
    reason = docker_skip_reason()
    if reason:
        pytest.skip(reason)


@pytest.fixture(scope="module")
def paypulse_workspace(paypulse_repo: FixtureBuildResult, tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("sandbox_workspaces")
    with override_settings(WORKSPACE_ROOT=root):
        return clone_local_repository_into_workspace(paypulse_repo.repo_path, root / "paypulse")


@pytest.fixture
def project(paypulse_workspace: Path, settings: SettingsWrapper, tmp_path: Path) -> Project:
    settings.WORKSPACE_ROOT = paypulse_workspace.parent
    settings.SANDBOX_RUNS_ROOT = tmp_path / "runs"
    settings.SANDBOX_COMMAND_TIMEOUT_SECONDS = 300
    settings.SANDBOX_INSTALL_TIMEOUT_SECONDS = 900
    return Project(id=uuid4(), name="PayPulse", is_golden_demo=True, workspace_path=str(paypulse_workspace))


def assert_paypulse_main_result(result: TestRunResult) -> None:
    """16 tests on main, 15 pass and the backoff spec fails; the report parsed cleanly."""
    output = result.execution.stdout + result.execution.stderr
    assert result.exit_code == 1 and not result.timed_out, output[-3000:]
    assert result.junit_error is None and not result.collection_error
    totals = result.totals
    assert (totals.tests, totals.passed, totals.failed, totals.errors, totals.skipped) == (16, 15, 1, 0, 0)
    assert result.nodeids(TestOutcome.FAILED) == (BACKOFF_TEST,)
    assert "unexpected keyword argument 'max_attempts'" in output


def assert_workspace_untouched(workspace: Path) -> None:
    """The venv, caches and the JUnit report live outside the workspace, so Git sees no change."""
    assert run_git(workspace, "status", "--porcelain", "--ignored") == ""


def report(label: str, box: Sandbox, result: TestRunResult) -> None:
    totals = result.totals
    print(  # noqa: T201 - shown with -s / -rA as evidence for the EnvironmentAgent
        f"\n[{label}] mode={box.mode} python={box.python_version} image={box.info.image} "
        f"exit={result.exit_code} tests={totals.tests} passed={totals.passed} failed={totals.failed} "
        f"errors={totals.errors} duration={result.execution.duration_seconds}s"
    )


def installed_interpreter(version: str) -> bool:
    env = subprocess_environment(source=os.environ, home=Path.home(), tmp=Path.home())
    return resolve_interpreter(version, env=env).matches(version)


def test_subprocess_offline_run_with_a_modern_pytest(project: Project, paypulse_workspace: Path) -> None:
    if not any(WHEELHOUSE.glob("pytest-*.whl")):
        pytest.skip(f"no pytest wheel in {WHEELHOUSE}; run `pip download pytest --only-binary=:all: -d {WHEELHOUSE}`")
    with get_sandbox(project, uuid4(), python_version="3.13", mode="subprocess") as box:
        assert box.create_environment().ok
        installs = box.install(packages=["pytest"], network=False, find_links=WHEELHOUSE)
        assert [r.ok for r in installs] == [True], installs[-1].stdout[-2000:] + installs[-1].stderr[-2000:]
        assert "--no-index" in installs[0].argv
        result = box.run_pytest(timeout=300)
        report("subprocess offline", box, result)
    assert_paypulse_main_result(result)
    assert result.execution.network_isolated is False
    assert_workspace_untouched(paypulse_workspace)


@pytest.mark.network
@pytest.mark.parametrize("version", ["3.13", "3.10"])
def test_subprocess_run_with_paypulse_pinned_requirements(
    version: str, project: Project, paypulse_workspace: Path
) -> None:
    require_network()
    if not installed_interpreter(version):
        pytest.skip(f"Python {version} is not installed here (py -{version} / python{version})")
    with get_sandbox(project, uuid4(), python_version=version, mode="subprocess") as box:
        setup = box.create_environment()
        assert setup.ok and setup.python_version is not None and setup.python_version.startswith(version + ".")
        [install] = box.install(["requirements.txt"])
        assert install.ok, install.stdout[-2000:] + install.stderr[-2000:]
        version_check = box.run(["python", "-m", "pytest", "--version"], timeout=120)
        assert "pytest 6.2.4" in version_check.stdout + version_check.stderr
        result = box.run_pytest(timeout=300)
        report(f"subprocess pinned {version}", box, result)
    assert_paypulse_main_result(result)
    assert_workspace_untouched(paypulse_workspace)


# ---------------------------------------------------------------------------
# Docker mode (needs Docker Desktop and Docker Hub / PyPI)
# ---------------------------------------------------------------------------
ISOLATION_PROBE = textwrap.dedent("""
    import json, os, socket
    found = {"uid": os.getuid(), "gid": os.getgid(), "env": sorted(os.environ)}
    try:
        socket.create_connection(("pypi.org", 443), timeout=5).close()
        found["network"] = "connected"
    except OSError as exc:
        found["network"] = type(exc).__name__
    try:
        open("/var/tmp/lazarus-probe", "w").close()
        found["rootfs"] = "writable"
    except OSError as exc:
        found["rootfs"] = exc.errno
    print(json.dumps(found))
""")
EROFS: int = 30


@pytest.mark.docker
@pytest.mark.network
@pytest.mark.parametrize("version", ["3.9", "3.11"])
def test_docker_run_with_paypulse_pinned_requirements(
    version: str, project: Project, paypulse_workspace: Path, settings: SettingsWrapper
) -> None:
    require_docker()
    with get_sandbox(project, uuid4(), python_version=version, mode="docker") as box:
        setup = box.create_environment()
        assert setup.ok, setup.stdout[-2000:] + setup.stderr[-2000:]
        assert box.image == f"python:{version}-slim" and setup.python_version.startswith(version + ".")
        [install] = box.install(["requirements.txt"])
        assert install.ok and not install.network_isolated, install.stdout[-2000:] + install.stderr[-2000:]
        result = box.run_pytest(timeout=300)
        report(f"docker pinned {version}", box, result)
    assert result.execution.network_isolated is True
    assert_paypulse_main_result(result)
    assert_workspace_untouched(paypulse_workspace)


@pytest.mark.docker
@pytest.mark.network
def test_docker_execute_phase_is_isolated_and_cleaned_up(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_docker()
    for name, value in PLANTED_SECRETS.items():
        monkeypatch.setenv(name, value)
    run_id = str(uuid4())
    with get_sandbox(project, run_id, python_version="3.11", mode="docker") as box:
        assert box.create_environment().ok
        probe = box.run(["python", "-c", ISOLATION_PROBE], timeout=120)
        assert probe.ok, probe.stderr[-2000:]
        found = json.loads(probe.stdout.strip().splitlines()[-1])
    assert (found["uid"], found["gid"]) == (1000, 1000)
    assert found["network"] != "connected"  # gaierror / OSError: there is no network at all
    assert found["rootfs"] == EROFS
    assert not set(found["env"]) & set(PLANTED_SECRETS)

    client = docker_client()
    labelled = {"label": [f"{LABEL_RUN}={run_id}"]}
    assert client.containers.list(all=True, filters=labelled) == []
    assert client.volumes.list(filters=labelled) == []
    assert client.networks.list(filters=labelled) == []
