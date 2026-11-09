"""Unit tests for the 4-agent Foundry pipeline (``apps/foundry/agents.py``).

Fast and offline: the sandbox is a scripted fake that returns real
``TestRunResult`` objects, the pipeline tests swap the first three agents for
recorders, and the model client is a fake. No database, no subprocess sandbox.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from apps.foundry.agents import (
    EngineeringAgent,
    EnvironmentAgent,
    FoundryContext,
    FoundryPipeline,
    RunState,
    StageFailure,
    VerificationAgent,
    WorkItem,
)
from apps.foundry.junit import JUnitReport, TestCaseResult, TestOutcome, TestTotals
from apps.foundry.models import AgentStage
from apps.foundry.recipes import RETRY_JITTER
from apps.foundry.sandbox import ExecResult, Phase, SandboxInfo, TestRunResult
from apps.foundry.workspace import git, read_text, write_text
from apps.projects.git_runner import run_git_command
from apps.telemetry.events import TelemetryEventType as Event

RUN_ID = "run-0001"
TARGET = "tests/test_webhooks.py::test_dispatch_retries_with_exponential_backoff"
OTHER = "tests/test_ledger.py::test_balanced_posting"

Events = list[tuple[Event, dict[str, Any]]]


# ---------------------------------------------------------------------------
# Builders and fakes
# ---------------------------------------------------------------------------
def case(nodeid: str, outcome: TestOutcome = TestOutcome.PASSED) -> TestCaseResult:
    return TestCaseResult(nodeid=nodeid, classname="", name=nodeid.rsplit("::", 1)[-1], outcome=outcome,
                          duration_seconds=0.01, message="assert 1 == 2" if outcome is TestOutcome.FAILED else "",
                          message_truncated=False, is_collection_error=False)


def execution(argv: Sequence[str], exit_code: int = 0, stdout: str = "") -> ExecResult:
    return ExecResult(argv=tuple(argv), phase=Phase.EXECUTE, exit_code=exit_code, stdout=stdout, stderr="",
                      stdout_truncated=False, stderr_truncated=False, duration_seconds=0.1, timed_out=False,
                      mode="subprocess", network_isolated=False, image=None, python_version="3.11.9")


def pytest_result(*cases: TestCaseResult) -> TestRunResult:
    failed = any(c.outcome in (TestOutcome.FAILED, TestOutcome.ERROR) for c in cases)
    summary = f"{sum(c.outcome is TestOutcome.PASSED for c in cases)} passed"
    return TestRunResult(
        execution=execution(("python", "-m", "pytest"), exit_code=1 if failed else 0, stdout=summary + "\n"),
        report=JUnitReport(cases=cases, totals=TestTotals.from_cases(cases), collection_error=False,
                           cases_truncated=False),
        junit_error=None, collection_error=False,
    )


PASSING = pytest_result(case(OTHER), case(TARGET))
FAILING = pytest_result(case(OTHER), case(TARGET, TestOutcome.FAILED))


class FakeSandbox:
    """Returns scripted ``run_pytest`` results (the last one repeats) and records every call."""

    mode = "subprocess"
    python_version = "3.11.9"
    info = SandboxInfo(mode="subprocess", run_id=RUN_ID, project_id="p", requested_python_version="3.11",
                       python_version="3.11.9", python_executable=None, image=None, network_isolated=False,
                       notes=("fake",))

    def __init__(self, results: Sequence[TestRunResult] = (PASSING,),
                 install_ok: Callable[[dict[str, Any]], bool] = lambda _kwargs: True) -> None:
        self.results = list(results)
        self.install_ok = install_ok
        self.pytest_calls: list[tuple[str, ...]] = []
        self.installs: list[dict[str, Any]] = []
        self.closed = False

    def create_environment(self) -> ExecResult:
        return execution(("python", "-m", "venv"))

    def install(self, requirements_files: Sequence[str] = (), **kwargs: Any) -> tuple[ExecResult, ...]:
        kwargs = {"requirements_files": list(requirements_files), **kwargs}
        kwargs.pop("on_output", None)
        self.installs.append(kwargs)
        return (execution(("python", "-m", "pip", "install"), exit_code=0 if self.install_ok(kwargs) else 1),)

    def run(self, argv: Sequence[str], **_kwargs: Any) -> ExecResult:
        return execution(argv, stdout="pytest 9.2.0\n")

    def run_pytest(self, test_ids: Sequence[str] | None = None, *,
                   on_output: Callable[[str, str], None] | None = None, **_kwargs: Any) -> TestRunResult:
        self.pytest_calls.append(tuple(test_ids or ()))
        if on_output is not None:
            on_output("stdout", "collected tests\n")
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def close(self) -> None:
        self.closed = True


def context(items: Sequence[WorkItem] = ()) -> FoundryContext:
    project = SimpleNamespace(id=uuid4(), workspace_path="", default_branch="main", is_golden_demo=True)
    return FoundryContext(project=project, run_id=RUN_ID, strategy_type="RESTORE", default_branch="main",
                          items=tuple(items), sandbox_mode="subprocess")


class Recorder:
    """Stands in for one of the first three agents and records when it ran."""

    def __init__(self, stage: AgentStage, calls: list[Any], setup: Callable[[RunState], None] = lambda _s: None,
                 error: Exception | None = None) -> None:
        self.stage, self.calls, self.setup, self.error = stage, calls, setup, error

    def run(self, ctx: FoundryContext, state: RunState, emit: Any) -> dict[str, Any]:
        self.calls.append(self.stage.value)
        if self.error is not None:
            raise self.error
        self.setup(state)
        return {"ran": self.stage.value}


class RecordingEngineering(Recorder):
    """An EngineeringAgent whose ``repair`` reports ``repair_status`` and can make the next run pass."""

    def __init__(self, calls: list[Any], sandbox: FakeSandbox, *, repair_status: str = "APPLIED",
                 heals_on: int | None = None) -> None:
        super().__init__(AgentStage.ENGINEERING, calls)
        self.sandbox, self.repair_status, self.heals_on = sandbox, repair_status, heals_on

    def repair(self, ctx: FoundryContext, state: RunState, attempt: Any, iteration: int) -> dict[str, Any]:
        self.calls.append(("REPAIR", iteration, attempt.passed))
        if self.heals_on == iteration:
            self.sandbox.results = [PASSING]
        files = [{"path": "paypulse/webhooks.py", "action": "modify"}] if self.repair_status == "APPLIED" else []
        return {"iteration": iteration, "mode": "DETERMINISTIC", "files": files, "status": self.repair_status}


def pipeline(calls: list[Any], sandbox: FakeSandbox, events: Events, *, baseline: TestRunResult = FAILING,
             engineering: Any | None = None, architecture_error: Exception | None = None,
             environment_error: Exception | None = None, **kwargs: Any) -> FoundryPipeline:
    def environment_setup(state: RunState) -> None:
        state.sandbox, state.baseline = sandbox, baseline  # type: ignore[assignment]

    def architecture_setup(state: RunState) -> None:
        state.targets, state.revival_branch = (TARGET,), "lazarus/restore-run0001"

    return FoundryPipeline(
        environment=Recorder(AgentStage.ENVIRONMENT, calls, environment_setup, environment_error),  # type: ignore[arg-type]
        architecture=Recorder(AgentStage.ARCHITECTURE, calls, architecture_setup, architecture_error),  # type: ignore[arg-type]
        engineering=engineering or RecordingEngineering(calls, sandbox),
        verification=VerificationAgent(),
        emit=lambda event, payload: events.append((event, payload)),
        **kwargs,
    )


def stage_events(events: Events) -> list[tuple[str, str]]:
    return [(p["stage"], p["status"]) for e, p in events if e is Event.AGENT_STAGE_CHANGED]


def kinds(events: Events) -> list[Event]:
    return [event for event, _ in events if event is not Event.SANDBOX_LOG]


# ---------------------------------------------------------------------------
# Pipeline: order, telemetry, self-healing
# ---------------------------------------------------------------------------
def test_the_four_agents_run_in_order_and_announce_each_stage() -> None:
    calls: list[Any] = []
    events: Events = []
    guarded: list[AgentStage] = []
    sandbox = FakeSandbox([PASSING])

    result = pipeline(calls, sandbox, events).run(context(), before_stage=guarded.append)

    assert calls == ["ENVIRONMENT", "ARCHITECTURE", "ENGINEERING"]
    assert sandbox.pytest_calls == [()]  # one full verification run, after engineering
    assert guarded == [AgentStage.ENVIRONMENT, AgentStage.ARCHITECTURE, AgentStage.ENGINEERING,
                       AgentStage.VERIFICATION]
    assert stage_events(events) == [
        ("ENVIRONMENT", "STARTED"), ("ENVIRONMENT", "COMPLETED"),
        ("ARCHITECTURE", "STARTED"), ("ARCHITECTURE", "COMPLETED"),
        ("ENGINEERING", "STARTED"), ("ENGINEERING", "COMPLETED"),
        ("VERIFICATION", "STARTED"), ("VERIFICATION", "COMPLETED"),
        ("COMPLETE", "COMPLETE"),
    ]
    [(_, verified)] = [(e, p) for e, p in events if e is Event.TEST_PASSED]
    assert verified["phase"] == "VERIFICATION" and verified["foundry_run_id"] == RUN_ID
    assert verified["target_outcomes"] == {TARGET: "PASSED"}
    logs = [p for e, p in events if e is Event.SANDBOX_LOG]
    assert logs and logs[0]["stage"] == "VERIFICATION" and logs[0]["lines"][0]["line"] == "collected tests"
    assert Event.ERROR not in kinds(events)
    assert result.succeeded and result.tests_passed and result.final_stage is AgentStage.COMPLETE
    assert result.iterations_used == 0 and result.failure is None
    assert result.revival_branch == "lazarus/restore-run0001"
    assert [key for key in ("environment", "architecture", "engineering") if key in result.report] == [
        "environment", "architecture", "engineering"]
    assert sandbox.closed


def test_a_failed_verification_is_repaired_and_verified_again() -> None:
    calls: list[Any] = []
    events: Events = []
    sandbox = FakeSandbox([FAILING])
    engineering = RecordingEngineering(calls, sandbox, heals_on=1)

    result = pipeline(calls, sandbox, events, engineering=engineering).run(context())

    assert calls == ["ENVIRONMENT", "ARCHITECTURE", "ENGINEERING", ("REPAIR", 1, False)]
    assert len(sandbox.pytest_calls) == 2
    assert result.succeeded and result.tests_passed and result.iterations_used == 1
    assert ("VERIFICATION", "SELF_HEALING") in stage_events(events)
    assert [e for e in kinds(events) if e in (Event.TEST_PASSED, Event.TEST_FAILED)] == [
        Event.TEST_FAILED, Event.TEST_PASSED]
    attempts = result.report["verification"]["attempts"]
    assert [(a["iteration"], a["passed"]) for a in attempts] == [(0, False), (1, True)]


def test_self_healing_stops_after_three_iterations_and_the_run_fails() -> None:
    calls: list[Any] = []
    events: Events = []
    sandbox = FakeSandbox([FAILING])

    result = pipeline(calls, sandbox, events).run(context())

    assert [call for call in calls if isinstance(call, tuple)] == [
        ("REPAIR", 1, False), ("REPAIR", 2, False), ("REPAIR", 3, False)]
    assert len(sandbox.pytest_calls) == 4  # the first verification plus one per repair
    assert not result.succeeded and not result.tests_passed
    assert result.final_stage is AgentStage.FAILED and result.iterations_used == 3
    assert result.failure is not None
    assert (result.failure["stage"], result.failure["code"]) == ("VERIFICATION", "TESTS_FAILING")
    assert "after 3 self-healing iteration(s)" in result.failure["detail"]
    assert TARGET in result.failure["detail"]
    healing = [p for e, p in events if e is Event.AGENT_STAGE_CHANGED and p["status"] == "SELF_HEALING"]
    assert [(p["iteration"], p["max_iterations"]) for p in healing] == [(1, 3), (2, 3), (3, 3)]
    assert stage_events(events)[-2:] == [("VERIFICATION", "FAILED"), ("FAILED", "FAILED")]
    [(_, error)] = [(e, p) for e, p in events if e is Event.ERROR]
    assert error["agent_stage"] == "VERIFICATION" and error["code"] == "TESTS_FAILING"
    assert result.after_test_output.startswith("1 passed")
    assert sandbox.closed


def test_a_repair_that_changes_nothing_ends_the_loop_early() -> None:
    calls: list[Any] = []
    sandbox = FakeSandbox([FAILING])
    engineering = RecordingEngineering(calls, sandbox, repair_status="NO_CHANGES")

    result = pipeline(calls, sandbox, [], engineering=engineering).run(context())

    assert [call for call in calls if isinstance(call, tuple)] == [("REPAIR", 1, False)]
    assert len(sandbox.pytest_calls) == 1
    assert not result.succeeded and result.iterations_used == 0
    assert result.report["verification"]["healing"][0]["status"] == "NO_CHANGES"


def test_the_healing_limit_is_configurable_but_never_above_three() -> None:
    calls: list[Any] = []
    sandbox = FakeSandbox([FAILING])

    result = pipeline(calls, sandbox, [], max_healing_iterations=0).run(context())

    assert not any(isinstance(call, tuple) for call in calls) and not result.succeeded
    with pytest.raises(ValueError):
        pipeline([], FakeSandbox(), [], max_healing_iterations=4)


def test_a_stage_failure_stops_the_later_agents() -> None:
    calls: list[Any] = []
    events: Events = []
    sandbox = FakeSandbox([PASSING])
    failure = StageFailure(AgentStage.ARCHITECTURE, "MERGE_FAILED", "restore-01: conflicts in paypulse/retry.py")

    result = pipeline(calls, sandbox, events, architecture_error=failure).run(context())

    assert calls == ["ENVIRONMENT", "ARCHITECTURE"] and sandbox.pytest_calls == []
    assert result.failure == {"stage": "ARCHITECTURE", "code": "MERGE_FAILED",
                              "detail": "restore-01: conflicts in paypulse/retry.py"}
    assert stage_events(events)[-2:] == [("ARCHITECTURE", "FAILED"), ("FAILED", "FAILED")]
    assert sandbox.closed


def test_the_approval_guard_runs_before_each_stage_and_can_stop_the_run() -> None:
    calls: list[Any] = []

    def guard(stage: AgentStage) -> None:
        if stage is AgentStage.ENGINEERING:
            raise StageFailure(stage, "APPROVAL_NOT_INTACT", "the approved strategy changed")

    result = pipeline(calls, FakeSandbox(), []).run(context(), before_stage=guard)

    assert calls == ["ENVIRONMENT", "ARCHITECTURE"]
    assert result.failure is not None and result.failure["code"] == "APPROVAL_NOT_INTACT"


def test_an_unexpected_error_fails_the_run_without_leaking_its_message() -> None:
    events: Events = []
    error = RuntimeError(r"C:\Users\someone\secret path")

    result = pipeline([], FakeSandbox(), events, environment_error=error).run(context())

    assert result.failure is not None
    assert (result.failure["stage"], result.failure["code"]) == ("ENVIRONMENT", "INTERNAL_ERROR")
    assert "secret" not in json.dumps(result.report) + json.dumps([p for _, p in events])


def test_telemetry_failures_never_fail_the_run() -> None:
    def broken_emit(_event: Event, _payload: dict[str, Any]) -> None:
        raise ConnectionError("redis is down")

    calls: list[Any] = []
    sandbox = FakeSandbox([PASSING])
    run = pipeline(calls, sandbox, [])
    run.emit = broken_emit

    assert run.run(context()).succeeded


# ---------------------------------------------------------------------------
# VerificationAgent
# ---------------------------------------------------------------------------
def test_verification_runs_uncollected_targets_separately_and_reports_regressions() -> None:
    sandbox = FakeSandbox([pytest_result(case(OTHER, TestOutcome.FAILED)), pytest_result(case(TARGET))])
    state = RunState(sandbox=sandbox, baseline=pytest_result(case(OTHER)), targets=(TARGET,))  # type: ignore[arg-type]
    events: Events = []

    attempt = VerificationAgent().run(context(), state, lambda e, p: events.append((e, p)), 0)

    assert sandbox.pytest_calls == [(), (TARGET,)]  # the target was not in the default test paths
    assert attempt.target_outcomes == {TARGET: "PASSED"}
    assert attempt.regressions == (OTHER,)
    assert not attempt.passed
    assert any("passed before now fail" in reason for reason in attempt.reasons)
    assert [e for e, _ in events if e is not Event.SANDBOX_LOG] == [Event.TEST_FAILED]


def test_verification_fails_when_a_target_never_ran() -> None:
    sandbox = FakeSandbox([pytest_result(case(OTHER))])
    state = RunState(sandbox=sandbox, targets=(TARGET,))  # type: ignore[arg-type]

    attempt = VerificationAgent().run(context(), state, lambda e, p: None, 0)

    assert attempt.target_outcomes == {TARGET: "NOT_RUN"} and not attempt.passed


# ---------------------------------------------------------------------------
# EngineeringAgent: deterministic path, model fallback, edit validation
# ---------------------------------------------------------------------------
RETRY_PATH = "paypulse/retry.py"
RETRY_STUB = (
    RETRY_JITTER.replacements[0].old
    + "\n\n@dataclass\nclass RetryPolicy:\n    base_delay: float = 0.5\n    jitter: bool = False\n\n"
    + "    def delay_for(self, attempt: int) -> float:\n        delay = self.base_delay * 2 ** attempt\n"
    + RETRY_JITTER.replacements[1].old
)
STUB_ITEM = WorkItem(id="restore-02", kind="COMPLETE_STUB", stage="ENGINEERING", title="Complete jitter",
                     files_to_modify=(RETRY_PATH,), target_tests=("tests/test_retry.py",))


class ExplodingClient:
    """Any use of the model client fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the model client was used ({name})")


def fake_client(reply: Callable[..., Any]) -> Any:
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=reply)))


def model_reply(edits: list[dict[str, str]]) -> Callable[..., Any]:
    def create(**_request: Any) -> Any:
        message = SimpleNamespace(content=json.dumps({"edits": edits, "notes": "done"}), refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

    return create


@pytest.fixture
def stub_state(tmp_path: Path) -> RunState:
    write_text(tmp_path, RETRY_PATH, RETRY_STUB)
    return RunState(workspace=tmp_path, protected_tests=frozenset({"tests/test_retry.py"}))


def test_without_a_model_key_the_deterministic_recipe_is_applied(stub_state: RunState) -> None:
    agent = EngineeringAgent(api_key="", client=ExplodingClient())
    ctx = context([STUB_ITEM])

    report = agent.run(ctx, stub_state, lambda e, p: None)

    assert not agent.model_enabled and report["model_enabled"] is False and report["model_id"] == ""
    [item] = report["items"]
    assert (item["status"], item["mode"], item["recipes"]) == ("APPLIED", "DETERMINISTIC",
                                                              ["paypulse.retry.full_jitter"])
    assert item["files"] == [{"path": RETRY_PATH, "action": "modify"}]
    text = read_text(stub_state.workspace, RETRY_PATH)  # type: ignore[arg-type]
    assert "random.uniform(0.0, delay)" in text and "NotImplementedError" not in text


def test_the_deterministic_repair_reapplies_only_missing_recipes(stub_state: RunState) -> None:
    agent = EngineeringAgent(api_key="", client=ExplodingClient())
    ctx = context([STUB_ITEM])
    agent.run(ctx, stub_state, lambda e, p: None)
    attempt = SimpleNamespace(passed=False)

    assert agent.repair(ctx, stub_state, attempt, 1)["status"] == "NO_CHANGES"  # type: ignore[arg-type]
    write_text(stub_state.workspace, RETRY_PATH, RETRY_STUB)  # type: ignore[arg-type]  # the fix was reverted
    repair = agent.repair(ctx, stub_state, attempt, 2)  # type: ignore[arg-type]
    assert repair["status"] == "APPLIED" and repair["recipes"] == ["paypulse.retry.full_jitter"]


def test_a_failing_model_call_falls_back_to_the_recipe_and_redacts_the_key(stub_state: RunState) -> None:
    def broken(**_request: Any) -> Any:
        raise RuntimeError("upstream rejected key sk-test-123")

    agent = EngineeringAgent(api_key="sk-test-123", client=fake_client(broken))

    report = agent.run(context([STUB_ITEM]), stub_state, lambda e, p: None)

    [item] = report["items"]
    assert (item["status"], item["mode"], item["fallback_reason"]) == ("APPLIED", "MODEL_FALLBACK", "MODEL_ERROR")
    assert "sk-test-123" not in json.dumps(report) and "sk-test-123" not in repr(agent)


def test_an_item_no_recipe_implements_is_unsupported_offline(tmp_path: Path) -> None:
    item = WorkItem(id="restore-09", kind="IMPLEMENT_FROM_SPEC", stage="ENGINEERING", title="PDF export",
                    files_to_create=("paypulse/pdf.py",))
    state = RunState(workspace=tmp_path)

    with pytest.raises(StageFailure) as raised:
        EngineeringAgent(api_key="").run(context([item]), state, lambda e, p: None)

    assert raised.value.code == "ITEMS_NOT_APPLIED" and "restore-09" in raised.value.detail
    assert state.reports["engineering"]["items"][0]["status"] == "UNSUPPORTED"


def test_model_edits_outside_the_item_or_to_protected_tests_are_rejected(stub_state: RunState) -> None:
    fixed = RETRY_STUB.replace("import time", "import random\nimport time")
    reply = model_reply([
        {"path": RETRY_PATH, "content": fixed},
        {"path": "tests/test_retry.py", "content": "def test_x():\n    assert True\n"},
        {"path": "paypulse/ledger.py", "content": "x = 1\n"},
        {"path": RETRY_PATH, "content": "duplicate"},
    ])
    agent = EngineeringAgent(api_key="sk-test", client=fake_client(reply))

    report = agent.run(context([STUB_ITEM]), stub_state, lambda e, p: None)

    [item] = report["items"]
    assert (item["status"], item["mode"]) == ("APPLIED", "MODEL")
    assert [r["reason"] for r in item["model"]["rejected_edits"]] == [
        "PROTECTED_TEST", "PATH_NOT_ALLOWED", "DUPLICATE_PATH"]
    assert read_text(stub_state.workspace, RETRY_PATH) == fixed  # type: ignore[arg-type]
    assert read_text(stub_state.workspace, "paypulse/ledger.py") is None  # type: ignore[arg-type]


def test_model_python_that_does_not_compile_falls_back_to_the_recipe(stub_state: RunState) -> None:
    agent = EngineeringAgent(api_key="sk-test", client=fake_client(model_reply(
        [{"path": RETRY_PATH, "content": "def broken(:\n"}])))

    [item] = agent.run(context([STUB_ITEM]), stub_state, lambda e, p: None)["items"]

    assert (item["mode"], item["fallback_reason"], item["status"]) == (
        "MODEL_FALLBACK", "MODEL_NO_USABLE_EDITS", "APPLIED")


# ---------------------------------------------------------------------------
# EnvironmentAgent: the install ladder and the baseline run
# ---------------------------------------------------------------------------
@pytest.fixture
def git_workspace(tmp_path: Path, settings: Any) -> Path:
    settings.WORKSPACE_ROOT = tmp_path
    workspace = tmp_path / "paypulse"
    workspace.mkdir()
    (workspace / "requirements.txt").write_text("pytest==6.2.4\nrequests==2.25.1\n", encoding="utf-8")
    (workspace / "setup.cfg").write_text("[options]\npython_requires = >=3.8\n", encoding="utf-8")
    run_git_command(["init", "-q", "-b", "main"], cwd=workspace)
    git(workspace, "add", "-A")
    git(workspace, "commit", "-q", "--no-verify", "-m", "initial")
    return workspace


def test_broken_pins_fall_back_to_the_test_runner_and_the_baseline_is_recorded(git_workspace: Path) -> None:
    sandbox = FakeSandbox([FAILING], install_ok=lambda kwargs: kwargs.get("packages") == ["pytest"])
    agent = EnvironmentAgent(sandbox_factory=lambda *a, **k: sandbox, install_network=True, wheelhouse="",
                             interpreter_available=lambda version: version == "3.11")
    ctx = context()
    ctx.project.workspace_path = str(git_workspace)
    state = RunState()
    events: Events = []

    report = agent.run(ctx, state, lambda e, p: events.append((e, p)))

    assert [(i["requirements_files"], i.get("packages")) for i in sandbox.installs] == [
        (["requirements.txt"], None), ([], ["pytest", "requests"]), ([], ["pytest"])]
    assert report["install"]["resolution"] == "TEST_RUNNER_ONLY"
    assert report["install"]["unresolved_packages"] == ["pytest", "requests"]
    assert report["python"]["requested"] == "3.11"
    assert report["baseline"]["totals"]["failed"] == 1 and state.baseline is FAILING
    assert state.base_sha and state.sandbox is sandbox
    [(event, payload)] = [(e, p) for e, p in events if e is not Event.SANDBOX_LOG]
    assert event is Event.TEST_FAILED and payload["phase"] == "BASELINE"


def test_a_dirty_workspace_is_refused(git_workspace: Path) -> None:
    (git_workspace / "stray.txt").write_text("x", encoding="utf-8")
    ctx = context()
    ctx.project.workspace_path = str(git_workspace)
    agent = EnvironmentAgent(sandbox_factory=lambda *a, **k: FakeSandbox(), install_network=True, wheelhouse="")

    with pytest.raises(StageFailure) as raised:
        agent.run(ctx, RunState(), lambda e, p: None)

    assert raised.value.code == "WORKSPACE_DIRTY"
