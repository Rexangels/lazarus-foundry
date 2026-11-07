"""Fast tests for apps.foundry.sandbox: policy, environment allowlist, Docker hardening, timeouts.

No database, no Docker daemon and no network: Docker mode runs against
``tests.foundry_fakes.FakeDockerClient``, which records the SDK calls.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from uuid import uuid4

import pytest
from pytest_django import Settings as SettingsWrapper

from apps.foundry import sandbox_env
from apps.foundry.junit import TestOutcome
from apps.foundry.sandbox import (
    LABEL_ROLE,
    LABEL_RUN,
    DockerSandbox,
    Phase,
    SandboxConfigurationError,
    SandboxStateError,
    SubprocessSandbox,
    UnsafeRequirementsError,
    UntrustedSandboxError,
    get_sandbox,
    sweep_orphaned_sandboxes,
)
from apps.foundry.sandbox_env import (
    docker_environment,
    subprocess_environment,
    synced_folder_root,
    validate_package_spec,
    validate_requirements_file,
    warn_if_synced_folder,
)
from apps.projects.models import Project
from tests.foundry_fakes import FakeContainer, FakeDockerClient, FakeOutcome, default_handler
from tests.test_foundry_junit import PYTEST6_REPORT

PLANTED_SECRETS: dict[str, str] = {
    "DATABASE_URL": "postgres://planted:planted@db/planted",
    "REDIS_URL": "redis://planted:6379/0",
    "SECRET_KEY": "planted-django-secret",
    "MODEL_API_KEY": "planted-model-key",
    "LAZARUS_FAKE_TOKEN": "planted-token",
    "LAZARUS_FAKE_SECRET": "planted-secret",
    "AWS_SECRET_ACCESS_KEY": "planted-aws",
    "GITHUB_TOKEN": "planted-github",
}


@pytest.fixture
def sandbox_settings(settings: SettingsWrapper, tmp_path: Path) -> SettingsWrapper:
    settings.WORKSPACE_ROOT = tmp_path / "workspaces"
    settings.WORKSPACE_ROOT.mkdir()
    settings.SANDBOX_RUNS_ROOT = tmp_path / "runs"
    settings.SANDBOX_MODE = "subprocess"
    settings.SANDBOX_ALLOW_SUBPROCESS_FOR_UNTRUSTED = False
    settings.SANDBOX_DOCKER_IMAGE_TEMPLATE = "python:{version}-slim"
    settings.SANDBOX_PYTHON_VERSIONS = ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13"]
    settings.SANDBOX_DEFAULT_PYTHON = "3.11"
    settings.SANDBOX_MEMORY_LIMIT = "1g"
    settings.SANDBOX_CPUS = 1.0
    settings.SANDBOX_PIDS_LIMIT = 256
    settings.SANDBOX_MAX_OUTPUT_BYTES = 1024 * 1024
    settings.SANDBOX_COMMAND_TIMEOUT_SECONDS = 60
    settings.SANDBOX_INSTALL_TIMEOUT_SECONDS = 60
    settings.SANDBOX_PREVIEW_PORT = 8010
    return settings


@pytest.fixture
def workspace(sandbox_settings: SettingsWrapper) -> Path:
    path = Path(sandbox_settings.WORKSPACE_ROOT) / "project"
    (path / "tests").mkdir(parents=True)
    (path / "requirements.txt").write_text("pytest==8.3.2\n-r requirements-extra.txt\n", encoding="utf-8")
    (path / "requirements-extra.txt").write_text("requests==2.32.3 --hash=sha256:abc\n", encoding="utf-8")
    return path


def make_project(workspace: Path, *, golden: bool) -> Project:
    return Project(id=uuid4(), name="p", is_golden_demo=golden, workspace_path=str(workspace))


def docker_sandbox(workspace: Path, client: FakeDockerClient, **kwargs: object) -> DockerSandbox:
    box = get_sandbox(make_project(workspace, golden=False), uuid4(), mode="docker",
                      docker_client_factory=lambda: client, **kwargs)  # type: ignore[arg-type]
    assert isinstance(box, DockerSandbox)
    return box


# ---------------------------------------------------------------------------
# Factory policy
# ---------------------------------------------------------------------------
def test_subprocess_mode_refuses_untrusted_projects(workspace: Path, sandbox_settings: SettingsWrapper) -> None:
    run_id = uuid4()
    with pytest.raises(UntrustedSandboxError, match="golden demo"):
        get_sandbox(make_project(workspace, golden=False), run_id)
    assert not (Path(sandbox_settings.SANDBOX_RUNS_ROOT) / str(run_id)).exists()


def test_subprocess_mode_is_allowed_for_the_golden_demo(workspace: Path) -> None:
    run_id = uuid4()
    with get_sandbox(make_project(workspace, golden=True), run_id, python_version="3.10") as box:
        assert isinstance(box, SubprocessSandbox)
        assert box.workspace == workspace.resolve() and box.requested_python_version == "3.10"
        assert box.info.network_isolated is False and any("no network isolation" in n for n in box.info.notes)
        assert box.results_dir.is_dir() and not box.run_dir.is_relative_to(workspace)
        run_dir = box.run_dir
    assert not run_dir.exists()
    with pytest.raises(SandboxStateError):
        box.run([sys.executable, "-c", "pass"])


def test_untrusted_subprocess_needs_the_explicit_flag_and_warns(
    workspace: Path, sandbox_settings: SettingsWrapper, caplog: pytest.LogCaptureFixture
) -> None:
    sandbox_settings.SANDBOX_ALLOW_SUBPROCESS_FOR_UNTRUSTED = True
    with caplog.at_level(logging.WARNING, logger="apps.foundry.sandbox"):
        with get_sandbox(make_project(workspace, golden=False), uuid4()) as box:
            assert isinstance(box, SubprocessSandbox)
    assert "NO network, filesystem or process isolation" in caplog.text


def test_docker_mode_is_chosen_when_configured(workspace: Path, sandbox_settings: SettingsWrapper) -> None:
    sandbox_settings.SANDBOX_MODE = "docker"
    client = FakeDockerClient()
    for version, image in ((None, "python:3.11-slim"), ("3.9", "python:3.9-slim")):
        with get_sandbox(make_project(workspace, golden=False), uuid4(), python_version=version,
                         docker_client_factory=lambda: client) as box:
            assert isinstance(box, DockerSandbox) and box.image == image
            assert box.info.network_isolated is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"python_version": "2.7"}, "not allowed"),
        ({"python_version": "3.11; rm -rf /"}, "not allowed"),
        ({"mode": "chroot"}, "sandbox mode"),
    ],
)
def test_factory_rejects_bad_arguments(workspace: Path, kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(SandboxConfigurationError, match=message):
        get_sandbox(make_project(workspace, golden=True), uuid4(), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(SandboxConfigurationError, match="run id"):
        get_sandbox(make_project(workspace, golden=True), "../escape")


def test_workspace_must_be_inside_workspace_root(sandbox_settings: SettingsWrapper, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(SandboxConfigurationError, match="WORKSPACE_ROOT"):
        get_sandbox(make_project(outside, golden=True), uuid4())
    with pytest.raises(SandboxConfigurationError, match="no workspace"):
        get_sandbox(Project(id=uuid4(), name="p", is_golden_demo=True), uuid4())


def test_runs_root_must_not_overlap_workspace_root(workspace: Path, sandbox_settings: SettingsWrapper) -> None:
    sandbox_settings.SANDBOX_RUNS_ROOT = Path(sandbox_settings.WORKSPACE_ROOT) / "runs"
    with pytest.raises(SandboxConfigurationError, match="must not overlap"):
        get_sandbox(make_project(workspace, golden=True), uuid4())


# ---------------------------------------------------------------------------
# Child environment: secrets never enter the sandbox
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
def test_subprocess_environment_is_an_allowlist(windows: bool, tmp_path: Path) -> None:
    source = {**PLANTED_SECRETS, "Path": "C:\\bin", "SystemRoot": "C:\\Windows", "PYTHONPATH": "/host",
              "HTTP_PROXY": "http://user:pw@proxy", "PYTEST_ADDOPTS": "-x", "USERNAME": "danie"}
    env = subprocess_environment(source=source, home=tmp_path / "home", tmp=tmp_path / "tmp",
                                 venv_dir=tmp_path / "venv", windows=windows)
    assert not set(env) & (set(PLANTED_SECRETS) | {"PYTHONPATH", "HTTP_PROXY", "PYTEST_ADDOPTS", "USERNAME"})
    assert env["PATH"].startswith(str(tmp_path / "venv" / ("Scripts" if windows else "bin")))
    assert env["PATH"].endswith("C:\\bin") and env["HOME"] == str(tmp_path / "home")
    assert env["TEMP"] == env["TMP"] == env["TMPDIR"] == str(tmp_path / "tmp")
    assert (env["PYTHONHASHSEED"], env["PYTHONDONTWRITEBYTECODE"], env["PIP_DISABLE_PIP_VERSION_CHECK"]) == (
        "0", "1", "1")
    assert env["VIRTUAL_ENV"] == str(tmp_path / "venv")
    assert ("SYSTEMROOT" in env) is windows and ("USERPROFILE" in env) is windows


def test_docker_environment_uses_nothing_from_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in PLANTED_SECRETS.items():
        monkeypatch.setenv(name, value)
    env = docker_environment({"PAYPULSE_MODE": "test"})
    assert set(env) == {"PATH", "HOME", "TMPDIR", "VIRTUAL_ENV", "PIP_NO_CACHE_DIR", "PAYPULSE_MODE",
                        *sandbox_env.FIXED_CHILD_ENV}
    assert env["PATH"].startswith("/opt/lazarus-venv/bin:")


@pytest.mark.parametrize("name", ["DATABASE_URL", "SECRET_KEY", "MY_API_KEY", "GH_TOKEN", "DB_PASSWORD",
                                  "SENTRY_DSN", "PATH", "HOME", "VIRTUAL_ENV", "BAD-NAME"])
def test_secret_protected_or_malformed_overrides_are_refused(name: str, tmp_path: Path) -> None:
    with pytest.raises(SandboxConfigurationError):
        docker_environment({name: "x"})
    with pytest.raises(SandboxConfigurationError):
        subprocess_environment(source={}, home=tmp_path, tmp=tmp_path, overrides={name: "x"})


def test_subprocess_child_cannot_see_planted_secrets(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in PLANTED_SECRETS.items():
        monkeypatch.setenv(name, value)
    probe = "import json, os; print(json.dumps(dict(os.environ)))"
    with get_sandbox(make_project(workspace, golden=True), uuid4()) as box:
        result = box.run([sys.executable, "-c", probe], env_overrides={"PAYPULSE_MODE": "test"}, timeout=60)
    assert result.ok, result.stderr
    child = json.loads(result.stdout)
    assert not set(child) & set(PLANTED_SECRETS)
    assert not any(value in result.stdout for value in PLANTED_SECRETS.values())
    assert child["PAYPULSE_MODE"] == "test" and child["PYTHONHASHSEED"] == "0"
    assert result.network_isolated is False and result.mode == "subprocess" and result.phase is Phase.EXECUTE


# ---------------------------------------------------------------------------
# Docker mode against the SDK test double
# ---------------------------------------------------------------------------
def _pytest_writes_junit(container: FakeContainer) -> FakeOutcome:
    if container.role == "execute" and "pytest" in container.command:
        [results] = [mount for mount in container.kwargs["mounts"] if mount["Target"] == "/lazarus-results"]
        (Path(results["Source"]) / "junit.xml").write_bytes(PYTEST6_REPORT)
        return FakeOutcome(exit_code=1, stdout=b"F.\n1 failed, 1 passed\n")
    return default_handler(container)


def _mounts(container: FakeContainer) -> dict[str, tuple[str, str, bool]]:
    return {m["Target"]: (m["Type"], m["Source"], m["ReadOnly"]) for m in container.kwargs["mounts"]}


def test_docker_phases_apply_every_hardening_option(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in PLANTED_SECRETS.items():
        monkeypatch.setenv(name, value)
    client = FakeDockerClient(_pytest_writes_junit)
    with docker_sandbox(workspace, client) as box:
        setup = box.create_environment()
        [install] = box.install(["requirements.txt"])
        tests = box.run_pytest(timeout=30)
        workspace_dir, results_dir, run_id = str(box.workspace), str(box.results_dir), box.run_id
    assert setup.ok and setup.python_version == "3.11.9" and setup.image == "python:3.11-slim"
    assert install.ok and install.network_isolated is False
    assert tests.exit_code == 1 and tests.execution.network_isolated is True
    assert tests.nodeids(TestOutcome.FAILED) == ("tests/test_webhooks.py::test_dispatch_retries_with_exponential_backoff",)
    assert [c.role for c in client.containers.items] == ["init", "setup", "install", "execute"]

    for container in client.containers.items:
        kw = container.kwargs
        assert container.image == "python:3.11-slim" and container.removed
        assert kw["cap_drop"] == ["ALL"] and kw["security_opt"] == ["no-new-privileges:true"]
        assert kw["read_only"] is True and kw["privileged"] is False and kw["init"] is True
        assert "size=" in kw["tmpfs"]["/tmp"] and "nosuid" in kw["tmpfs"]["/tmp"]
        assert kw["mem_limit"] == kw["memswap_limit"] == "1g"
        assert kw["nano_cpus"] == 1_000_000_000 and kw["pids_limit"] == 256
        assert kw["labels"][LABEL_RUN] == run_id and kw["ipc_mode"] == "private"
        assert not {"volumes", "ports", "devices", "pid_mode", "userns_mode"} & set(kw)
        assert not set(kw["environment"]) & set(PLANTED_SECRETS)
        for kind, source, _ in _mounts(container).values():
            assert "docker.sock" not in source
            assert kind == "volume" or source in (workspace_dir, results_dir)

    init, setup_c, install_c, execute = client.containers.items
    assert (init.kwargs["user"], init.kwargs["cap_add"], init.kwargs["network_mode"]) == ("0:0", ["CHOWN"], "none")
    assert init.command == ["chown", "1000:1000", "/opt/lazarus-venv"]
    assert set(_mounts(init)) == set(_mounts(setup_c)) == {"/opt/lazarus-venv"}
    assert setup_c.kwargs["network_mode"] == "none" and "cap_add" not in setup_c.kwargs
    assert install_c.kwargs["network_mode"] == "bridge"
    assert _mounts(install_c) == {"/workspace": ("bind", workspace_dir, True),
                                  "/opt/lazarus-venv": ("volume", f"lazarus-venv-{run_id}", False)}
    assert "--only-binary=:all:" in install_c.command and "/workspace/requirements.txt" in install_c.command
    assert execute.kwargs["network_mode"] == "none"
    assert _mounts(execute) == {"/workspace": ("bind", workspace_dir, False),
                                "/lazarus-results": ("bind", results_dir, False),
                                "/opt/lazarus-venv": ("volume", f"lazarus-venv-{run_id}", True)}
    assert "--junitxml=/lazarus-results/junit.xml" in execute.command
    assert all(c.kwargs["user"] == "1000:1000" for c in (setup_c, install_c, execute))


def test_docker_preview_service_is_published_on_loopback_only(workspace: Path) -> None:
    client = FakeDockerClient(lambda c: FakeOutcome(hang=True) if c.role == "service" else default_handler(c))
    with docker_sandbox(workspace, client) as box:
        box.create_environment()
        service = box.start_service(["python", "-m", "http.server"])
        assert service.url == "http://127.0.0.1:8010" and service.network_isolated is False
        [container] = client.created("service")
        kw = container.kwargs
        assert kw["ports"] == {"8010/tcp": ("127.0.0.1", 8010)}
        assert kw["network_mode"] == f"lazarus-net-{box.run_id}" != "bridge"
        assert kw["environment"]["PORT"] == "8010" and kw["cap_drop"] == ["ALL"] and kw["read_only"] is True
        [network] = client.networks.items
        assert network.labels[LABEL_RUN] == box.run_id and network.labels[LABEL_ROLE] == "service"
        assert service.stop().phase is Phase.SERVICE and container.killed and container.removed


def test_docker_close_and_sweep_remove_labelled_resources(workspace: Path, tmp_path: Path) -> None:
    client = FakeDockerClient()
    box = docker_sandbox(workspace, client)
    box.create_environment()
    leftover = client.containers.create("python:3.11-slim", labels=box.labels("execute"))  # a crashed run's
    [volume] = client.volumes.items
    assert volume.name == f"lazarus-venv-{box.run_id}" and volume.labels[LABEL_RUN] == box.run_id
    box.close()
    assert leftover.removed and volume.removed and not box.run_dir.exists()

    orphan = client.volumes.create(name="lazarus-venv-old", labels={"lazarus.sandbox": "true"})
    unrelated = client.volumes.create(name="pgdata", labels={})
    stale_dir = tmp_path / "runs" / "0b6c1d5e-old-run"
    stale_dir.mkdir(parents=True)
    report = sweep_orphaned_sandboxes(client, runs_root=tmp_path / "runs")
    assert orphan.removed and not unrelated.removed and not stale_dir.exists()
    assert (report.volumes, report.run_dirs) == (1, 1)


def test_docker_timeout_kills_the_container_and_flags_it(workspace: Path) -> None:
    client = FakeDockerClient(lambda c: FakeOutcome(hang=True) if c.role == "execute" else default_handler(c))
    with docker_sandbox(workspace, client) as box:
        box.create_environment()
        started = time.monotonic()
        result = box.run(["python", "-c", "while True: pass"], timeout=0.5)
    [container] = client.created("execute")
    assert result.timed_out and not result.ok and result.exit_code == 137
    assert container.killed and container.removed and time.monotonic() - started < 15


def test_docker_pulls_a_missing_image(workspace: Path) -> None:
    client = FakeDockerClient(image_present=False)
    with docker_sandbox(workspace, client, python_version="3.9") as box:
        assert box.create_environment().ok
    assert client.images.pulled == ["python:3.9-slim"]


# ---------------------------------------------------------------------------
# Subprocess mode: timeouts, output caps, argv
# ---------------------------------------------------------------------------
SPAWN_GRANDCHILD = textwrap.dedent("""
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(sys.argv[1], "w") as handle:
        handle.write(str(child.pid))
    print("started", flush=True)
    time.sleep(120)
""")


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                                 check=False).stdout
        return str(pid) in listing
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_subprocess_timeout_kills_the_whole_process_tree(workspace: Path, tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    lines: list[tuple[str, str]] = []
    with get_sandbox(make_project(workspace, golden=True), uuid4()) as box:
        started = time.monotonic()
        result = box.run([sys.executable, "-c", SPAWN_GRANDCHILD, str(pid_file)], timeout=3,
                         on_output=lambda stream, line: lines.append((stream, line)))
    assert result.timed_out and not result.ok and time.monotonic() - started < 30
    assert ("stdout", "started") in lines and "started" in result.stdout
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while _process_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _process_alive(grandchild)


def test_output_is_capped_per_stream_and_streaming_stops_at_the_cap(
    workspace: Path, sandbox_settings: SettingsWrapper
) -> None:
    sandbox_settings.SANDBOX_MAX_OUTPUT_BYTES = 100
    script = "import sys\nfor i in range(1000): print(f'line {i}')\nsys.stderr.write('oops\\n')"
    lines: list[tuple[str, str]] = []
    with get_sandbox(make_project(workspace, golden=True), uuid4()) as box:
        result = box.run([sys.executable, "-c", script], timeout=60, on_output=lambda s, l: lines.append((s, l)))
    assert result.ok and len(result.stdout.encode()) <= 100 and result.stdout.startswith("line 0")
    assert result.stdout_truncated and not result.stderr_truncated and result.stderr.strip() == "oops"
    stdout_lines = [line for stream, line in lines if stream == "stdout"]
    assert stdout_lines[0] == "line 0" and stdout_lines[-1] == "[lazarus] stdout truncated at 100 bytes"
    assert len(stdout_lines) < 30 and ("stderr", "oops") in lines


def test_argv_must_be_a_list_and_test_ids_cannot_be_options(workspace: Path) -> None:
    with get_sandbox(make_project(workspace, golden=True), uuid4()) as box:
        with pytest.raises(SandboxConfigurationError, match="never a shell string"):
            box.run("python -c 'print(1)'")  # type: ignore[arg-type]
        with pytest.raises(SandboxStateError, match="create_environment"):
            box.run(["python", "-c", "pass"])
        box._environment_ready = True  # skip the venv; only argument checks run below
        with pytest.raises(SandboxConfigurationError, match="not options"):
            box.run_pytest(["--rootdir=/"])
        with pytest.raises(SandboxConfigurationError, match="timeout"):
            box.run([sys.executable, "-c", "pass"], timeout=0)


# ---------------------------------------------------------------------------
# Requirements validation
# ---------------------------------------------------------------------------
def test_requirements_files_and_their_includes_are_accepted(workspace: Path) -> None:
    assert str(validate_requirements_file(workspace, "requirements.txt")) == "requirements.txt"
    assert validate_package_spec(" pytest==7.4.4 ") == "pytest==7.4.4"
    assert validate_package_spec("requests[socks]>=2.25; python_version >= '3.8'").startswith("requests")


@pytest.mark.parametrize(
    "content",
    [
        "--index-url https://evil.example/simple\npytest",
        "-i https://evil.example/simple",
        "--extra-index-url=https://evil.example/simple",
        "--trusted-host evil.example",
        "-f https://evil.example/wheels",
        "-e .",
        "--no-binary :all:",
        "git+https://github.com/acme/pkg.git#egg=pkg",
        "pkg @ https://evil.example/pkg.whl",
        "./vendor/pkg",
        "-r ../../outside.txt",
        "-r https://evil.example/req.txt",
    ],
)
def test_requirements_that_redirect_or_widen_the_install_are_refused(workspace: Path, content: str) -> None:
    (workspace / "bad.txt").write_text(content + "\n", encoding="utf-8")
    with pytest.raises(UnsafeRequirementsError):
        validate_requirements_file(workspace, "bad.txt")


@pytest.mark.parametrize("spec", ["-e .", "--index-url=x", "https://x/y.whl", "../pkg", "pkg @ file:///x", ""])
def test_unsafe_package_specs_are_refused(spec: str) -> None:
    with pytest.raises(UnsafeRequirementsError):
        validate_package_spec(spec)


def test_requirements_outside_the_workspace_are_refused(workspace: Path, tmp_path: Path) -> None:
    (tmp_path / "host.txt").write_text("pytest\n", encoding="utf-8")
    with pytest.raises(UnsafeRequirementsError, match="outside the workspace"):
        validate_requirements_file(workspace, tmp_path / "host.txt")


# ---------------------------------------------------------------------------
# OneDrive detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("path", "environ", "expected"),
    [
        (r"C:\Users\danie\OneDrive\Desktop\lazarus\ws", {}, "C:/Users/danie/OneDrive"),
        (r"c:\users\danie\onedrive - Contoso\ws", {}, "c:/users/danie/onedrive - Contoso"),
        (r"D:\Sync\Work\ws", {"OneDrive": r"D:\Sync"}, r"D:\Sync"),
        (r"d:\sync\work\ws", {"OneDriveCommercial": r"D:\Sync\Work"}, r"D:\Sync\Work"),
        (r"C:\lazarus-data\workspaces", {"OneDrive": r"C:\Users\danie\OneDrive"}, None),
        ("/home/danie/lazarus/workspaces", {}, None),
        (r"C:\Users\danie\OneDriveBackup\ws", {}, None),
    ],
)
def test_synced_folder_detection(path: str, environ: dict[str, str], expected: str | None) -> None:
    assert synced_folder_root(path, environ) == expected


def test_synced_folder_warning_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    path = Path(rf"C:\Users\someone\OneDrive\probe-{uuid4()}")
    with caplog.at_level(logging.WARNING, logger="apps.foundry.sandbox_env"):
        assert warn_if_synced_folder(path, "WORKSPACE_ROOT", {}) is not None
        warn_if_synced_folder(path, "WORKSPACE_ROOT", {})
    assert caplog.text.count("OneDrive-synced folder") == 1 and "non-synced path" in caplog.text
