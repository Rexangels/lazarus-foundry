"""Database side of System 4: claim a ``FoundryRun``, run the 4-agent pipeline and persist the result.

``run_foundry(foundry_run_id)`` (never raises)
    1. Claim, in one transaction under ``select_for_update`` on the run and the
       project: the run must be unstarted (``ENVIRONMENT`` with an empty
       ``report``), the project ``STRATEGY_APPROVED`` and the strategy approved by
       a human with its approval intact (``approval_is_intact``: the approved
       token still matches the content). The claim moves the project to
       ``FOUNDRY_RUNNING``. A run that cannot be claimed is refused without
       changes; a broken approval fails the run and the project.
    2. ``FoundryPipeline.run``. Before every stage the strategy is re-read and
       its approval re-checked, and ``FoundryRun.agent_stage`` is updated.
    3. Persist ``before_test_output``, ``after_test_output``, ``tests_passed``,
       ``iterations_used``, ``report`` and the final stage (``COMPLETE`` or
       ``FAILED``). A failure sets the project ``FAILED`` with a user-safe
       ``status_detail``.
    4. ``on_foundry_complete(run)``: the single place that decides what follows
       a finished run. The rebirth artifact (Task 6.3) is created there; until
       then a passing run leaves the project ``FOUNDRY_RUNNING`` and logs.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.foundry.agents import Emit, FoundryContext, FoundryPipeline, FoundryResult, StageFailure
from apps.foundry.models import AgentStage, FoundryRun
from apps.projects.models import Project, ProjectStatus
from apps.revival.approval import approval_is_intact
from apps.revival.models import RevivalStrategy
from apps.telemetry.broadcast import broadcast_telemetry
from apps.telemetry.events import TelemetryEventType

logger = logging.getLogger(__name__)

MAX_STATUS_DETAIL_CHARS: int = 2000
APPROVAL_BROKEN_DETAIL: str = (
    "The approved strategy no longer matches the content that was approved, so the Foundry did not run it."
)

PipelineFactory = Callable[[Emit], FoundryPipeline]


def telemetry_emitter(project_id: UUID) -> Emit:
    def emit(event_type: TelemetryEventType, payload: dict[str, Any]) -> None:
        try:
            broadcast_telemetry(project_id, event_type, payload)
        except Exception:
            logger.exception("Could not broadcast %s for project %s.", event_type, project_id)

    return emit


def _approval_ok(strategy: RevivalStrategy) -> bool:
    return strategy.approved_by_human and approval_is_intact(strategy, strategy.approved_token)


def claim_foundry_run(foundry_run_id: UUID | str) -> tuple[FoundryRun | None, str]:
    """``(run, "CLAIMED")``, or ``(None, reason)`` when the run cannot start (see the module docstring)."""
    with transaction.atomic():
        run = FoundryRun.objects.select_for_update().filter(pk=foundry_run_id).first()
        if run is None:
            return None, "NOT_FOUND"
        project = Project.objects.select_for_update().get(pk=run.project_id)
        strategy = RevivalStrategy.objects.get(pk=run.strategy_id)
        if run.agent_stage != AgentStage.ENVIRONMENT or run.report:
            return None, "ALREADY_STARTED"
        if project.status != ProjectStatus.STRATEGY_APPROVED:
            return None, "PROJECT_NOT_READY"
        if not _approval_ok(strategy):
            run.agent_stage = AgentStage.FAILED
            run.report = {"failure": {"stage": AgentStage.ENVIRONMENT.value, "code": "APPROVAL_NOT_INTACT",
                                      "detail": APPROVAL_BROKEN_DETAIL}}
            run.save(update_fields=["agent_stage", "report", "updated_at"])
            project.status, project.status_detail = ProjectStatus.FAILED, APPROVAL_BROKEN_DETAIL
            project.save(update_fields=["status", "status_detail", "updated_at"])
            return None, "APPROVAL_NOT_INTACT"
        run.report = {"claimed_at": timezone.now().isoformat()}
        run.save(update_fields=["report", "updated_at"])
        project.status, project.status_detail = ProjectStatus.FOUNDRY_RUNNING, ""
        project.save(update_fields=["status", "status_detail", "updated_at"])
    run.project, run.strategy = project, strategy
    return run, "CLAIMED"


def stage_guard(run: FoundryRun) -> Callable[[AgentStage], None]:
    """``before_stage``: re-check the approval, then record the stage."""

    def before_stage(stage: AgentStage) -> None:
        strategy = RevivalStrategy.objects.get(pk=run.strategy_id)
        if not _approval_ok(strategy):
            raise StageFailure(stage, "APPROVAL_NOT_INTACT", APPROVAL_BROKEN_DETAIL)
        FoundryRun.objects.filter(pk=run.pk).update(agent_stage=stage, updated_at=timezone.now())

    return before_stage


def persist_result(run: FoundryRun, result: FoundryResult) -> None:
    claimed_at = (run.report or {}).get("claimed_at")
    with transaction.atomic():
        FoundryRun.objects.filter(pk=run.pk).update(
            agent_stage=result.final_stage, before_test_output=result.before_test_output,
            after_test_output=result.after_test_output, tests_passed=result.tests_passed,
            iterations_used=result.iterations_used,
            report={**result.report, "claimed_at": claimed_at, "finished_at": timezone.now().isoformat()},
            updated_at=timezone.now(),
        )
        if result.failure:
            detail = f"Foundry {result.failure['stage']} failed ({result.failure['code']}): {result.failure['detail']}"
            Project.objects.filter(pk=run.project_id).update(
                status=ProjectStatus.FAILED, status_detail=detail[:MAX_STATUS_DETAIL_CHARS], updated_at=timezone.now()
            )
    run.refresh_from_db()


def on_foundry_complete(run: FoundryRun, *, emit: Emit) -> None:
    """Decide what follows a finished run. Never raises. Task 6.3 creates the RebirthArtifact here."""
    try:
        logger.info("FoundryRun %s finished in %s (tests_passed=%s, iterations_used=%s).",
                    run.pk, run.agent_stage, run.tests_passed, run.iterations_used)
    except Exception:
        logger.exception("The hand-off after FoundryRun %s failed.", run.pk)


def run_foundry(
    foundry_run_id: UUID | str,
    *,
    emit: Emit | None = None,
    pipeline_factory: PipelineFactory | None = None,
) -> dict[str, Any]:
    """Claim, run and persist one Foundry run (see the module docstring). Never raises."""
    try:
        run, status = claim_foundry_run(foundry_run_id)
    except Exception:
        logger.exception("Claiming FoundryRun %s failed.", foundry_run_id)
        return {"foundry_run_id": str(foundry_run_id), "status": "FAILED"}
    if run is None:
        logger.warning("FoundryRun %s was not started: %s.", foundry_run_id, status)
        return {"foundry_run_id": str(foundry_run_id), "status": status}
    emit = emit or telemetry_emitter(run.project_id)
    try:
        pipeline = (pipeline_factory or (lambda e: FoundryPipeline(emit=e)))(emit)
        context = FoundryContext.from_action_plan(
            run.project, str(run.pk), run.strategy.strategy_type, run.strategy.action_plan or {},
            sandbox_mode=run.sandbox_mode,
        )
        result = pipeline.run(context, before_stage=stage_guard(run))
        persist_result(run, result)
    except Exception:
        logger.exception("FoundryRun %s failed outside the pipeline.", run.pk)
        detail = "The Foundry run failed because of an internal error; the worker log has the details."
        try:
            FoundryRun.objects.filter(pk=run.pk).update(agent_stage=AgentStage.FAILED, updated_at=timezone.now())
            Project.objects.filter(pk=run.project_id).update(status=ProjectStatus.FAILED, status_detail=detail,
                                                             updated_at=timezone.now())
        except Exception:
            logger.exception("Could not mark FoundryRun %s as FAILED.", run.pk)
        emit(TelemetryEventType.ERROR, {"stage": "FOUNDRY", "detail": detail, "foundry_run_id": str(run.pk)})
        return {"foundry_run_id": str(run.pk), "status": AgentStage.FAILED.value}
    on_foundry_complete(run, emit=emit)
    return {
        "foundry_run_id": str(run.pk), "status": run.agent_stage, "tests_passed": run.tests_passed,
        "iterations_used": run.iterations_used, "revival_branch": result.revival_branch,
    }
