"""Database side of System 5: create the ``RebirthArtifact`` of a verified Foundry run.

``run_rebirth(run, *, emit)`` (never raises) is called by
``apps.foundry.services.on_foundry_complete`` after a run is persisted.

1. Only a run that finished ``COMPLETE`` with ``tests_passed`` qualifies, while
   its project is ``FOUNDRY_RUNNING``; anything else is skipped without changes.
2. The strategy's approval is re-checked (``approval_is_intact``): the exported
   ``.kiro/specs`` must be exactly the bundle the human approved.
3. ``apps.rebirth.builder.build_rebirth`` exports the specs into the workspace,
   commits the verified tree on the revival branch and builds the diff, the
   Continuity Memory and the PR summary. This runs outside any transaction.
4. One transaction under ``select_for_update`` on the project re-checks the
   status, writes the ``RebirthArtifact`` (one per project: an existing row is
   replaced) and moves the project to ``REBORN``.
5. ``REBIRTH_COMPLETE`` telemetry. A failure sets the project ``FAILED`` with a
   user-safe ``status_detail`` and emits ``ERROR`` with ``stage: "REBIRTH"``;
   the ``FoundryRun`` stays ``COMPLETE`` because its verification did pass.
"""
from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.foundry.agents import Emit, safe_emit
from apps.foundry.models import AgentStage, FoundryRun
from apps.ingestion.git_forensics import UnsafeWorkspaceError, resolve_workspace_dir
from apps.projects.models import Project, ProjectStatus
from apps.rebirth.builder import RebirthBundle, RebirthError, RebirthInputs, build_rebirth
from apps.rebirth.models import DEFAULT_PREVIEW_URL, RebirthArtifact
from apps.revival.approval import approval_is_intact
from apps.revival.models import RevivalStrategy
from apps.telemetry.events import TelemetryEventType

logger = logging.getLogger(__name__)

MAX_STATUS_DETAIL_CHARS: int = 2000


def rebirth_inputs(project: Project, strategy: RevivalStrategy, run: FoundryRun) -> RebirthInputs:
    return RebirthInputs(
        project_id=str(project.pk), project_name=project.name, default_branch=project.default_branch,
        repo_url=project.repo_url, foundry_run_id=str(run.pk), report=run.report or {},
        iterations_used=run.iterations_used, strategy_id=str(strategy.pk), strategy_type=strategy.strategy_type,
        strategy_title=strategy.title, strategy_summary=strategy.summary,
        feasibility_score=strategy.feasibility_score, action_plan=strategy.action_plan or {},
        kiro_specs=strategy.generated_kiro_specs or {}, approved_by=strategy.approved_by,
        approved_at=strategy.approved_at.isoformat() if strategy.approved_at else None,
        preview_url=DEFAULT_PREVIEW_URL,
    )


def create_rebirth_artifact(run: FoundryRun) -> tuple[RebirthArtifact | None, RebirthBundle | None, str]:
    """``(artifact, bundle, "CREATED")``, ``(None, None, reason)`` when skipped; raises ``RebirthError``."""
    run = FoundryRun.objects.select_related("project", "strategy").get(pk=run.pk)
    project, strategy = run.project, run.strategy
    if run.agent_stage != AgentStage.COMPLETE or not run.tests_passed:
        return None, None, "RUN_NOT_VERIFIED"
    if project.status != ProjectStatus.FOUNDRY_RUNNING:
        return None, None, "PROJECT_NOT_READY"
    if not (strategy.approved_by_human and approval_is_intact(strategy, strategy.approved_token)):
        raise RebirthError("APPROVAL_NOT_INTACT",
                           "the approved strategy no longer matches the content that was approved")
    try:
        workspace = resolve_workspace_dir(project.workspace_path, require_empty=False)
    except UnsafeWorkspaceError as exc:
        raise RebirthError("WORKSPACE_INVALID", "the project workspace is missing or unsafe") from exc
    bundle = build_rebirth(workspace, rebirth_inputs(project, strategy, run))
    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        if locked.status != ProjectStatus.FOUNDRY_RUNNING:
            return None, bundle, "PROJECT_NOT_READY"
        artifact, _ = RebirthArtifact.objects.update_or_create(
            project=locked,
            defaults={
                "foundry_run": run, "unified_diff_patch": bundle.unified_diff_patch, "pr_title": bundle.pr_title,
                "pr_body_markdown": bundle.pr_body_markdown,
                "exported_kiro_specs_path": bundle.exported_kiro_specs_path,
                "continuity_memory": bundle.continuity_memory, "preview_url": DEFAULT_PREVIEW_URL,
            },
        )
        locked.status, locked.status_detail = ProjectStatus.REBORN, ""
        locked.save(update_fields=["status", "status_detail", "updated_at"])
    return artifact, bundle, "CREATED"


def rebirth_payload(artifact: RebirthArtifact, bundle: RebirthBundle) -> dict[str, Any]:
    verification = bundle.continuity_memory.get("verification") or {}
    return {
        "rebirth_artifact_id": str(artifact.pk), "foundry_run_id": str(artifact.foundry_run_id),
        "pr_title": artifact.pr_title, "revival_branch": bundle.revival_branch,
        "base_sha": bundle.base_sha, "rebirth_commit_sha": bundle.rebirth_commit_sha,
        "files_changed": len(bundle.files), "additions": bundle.additions, "deletions": bundle.deletions,
        "diff_truncated": bundle.diff_truncated, "exported_kiro_specs_path": bundle.exported_kiro_specs_path,
        "spec_files": list(bundle.spec_files), "before": verification.get("before"),
        "after": verification.get("after"), "preview_url": artifact.preview_url,
    }


def _record_failure(run: FoundryRun, detail: str) -> None:
    Project.objects.filter(pk=run.project_id, status=ProjectStatus.FOUNDRY_RUNNING).update(
        status=ProjectStatus.FAILED, status_detail=detail[:MAX_STATUS_DETAIL_CHARS], updated_at=timezone.now()
    )


def run_rebirth(run: FoundryRun, *, emit: Emit) -> dict[str, Any]:
    """Create the run's ``RebirthArtifact`` (see the module docstring). Never raises."""
    try:
        artifact, bundle, status = create_rebirth_artifact(run)
    except RebirthError as exc:
        detail = f"Rebirth failed ({exc.code}): {exc.detail}"
        logger.warning("Rebirth of FoundryRun %s failed: %s", run.pk, detail)
    except Exception:
        logger.exception("Rebirth of FoundryRun %s failed.", run.pk)
        detail = "Rebirth failed because of an internal error; the worker log has the details."
    else:
        if artifact is None or bundle is None:
            logger.info("Rebirth of FoundryRun %s skipped: %s.", run.pk, status)
            return {"foundry_run_id": str(run.pk), "status": status}
        safe_emit(emit, TelemetryEventType.REBIRTH_COMPLETE, rebirth_payload(artifact, bundle))
        return {"foundry_run_id": str(run.pk), "status": status, "rebirth_artifact_id": str(artifact.pk)}
    try:
        _record_failure(run, detail)
    except Exception:
        logger.exception("Could not record the rebirth failure of FoundryRun %s.", run.pk)
    safe_emit(emit, TelemetryEventType.ERROR, {"stage": "REBIRTH", "detail": detail, "foundry_run_id": str(run.pk)})
    return {"foundry_run_id": str(run.pk), "status": "FAILED", "detail": detail}
