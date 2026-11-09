"""Rebirth endpoint (mounted at ``/api/projects/`` in ``config.api``, next to the genome and revival routers).

``GET /api/projects/{project_id}/rebirth``
    200 ``RebirthOut``: the project's ``RebirthArtifact`` (unified diff against
    the default branch, PR title and Markdown body, the repository-relative
    ``.kiro/specs`` path and files, the Continuity Memory JSON, the preview
    URL) with the Foundry run's before/after ``pytest`` output.
    404 for an unknown project. 409 ``RebirthNotReadyOut``:
    ``REBIRTH_PENDING`` while the project has not reached a final state
    (``DORMANT`` through ``FOUNDRY_RUNNING``); ``REBIRTH_UNAVAILABLE`` when it
    is ``FAILED`` (``status_detail`` says why) or ``REBORN`` without an artifact.
    Two queries: the project, then the artifact with its Foundry run.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router

from apps.projects.api import ErrorOut, telemetry_ws_path
from apps.projects.models import Project, ProjectStatus
from apps.rebirth.models import RebirthArtifact
from apps.rebirth.schemas import RebirthFileOut, RebirthNotReadyOut, RebirthOut, RebirthProjectOut

router: Router = Router(tags=["rebirth"])

REBIRTH_FINAL_STATUSES: frozenset[str] = frozenset({ProjectStatus.REBORN, ProjectStatus.FAILED})


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def rebirth_out(project: Project, artifact: RebirthArtifact) -> RebirthOut:
    memory = _dict(artifact.continuity_memory)
    revival = _dict(memory.get("revival"))
    run = artifact.foundry_run
    return RebirthOut(
        id=artifact.pk,
        project=RebirthProjectOut(id=project.pk, name=project.name, status=ProjectStatus(project.status).value,
                                  status_detail=project.status_detail, default_branch=project.default_branch),
        foundry_run_id=run.pk,
        strategy_id=run.strategy_id,
        strategy_type=str(revival.get("strategy_type") or ""),
        revival_branch=str(revival.get("revival_branch") or ""),
        base_sha=str(revival.get("base_sha") or ""),
        rebirth_commit_sha=str(revival.get("rebirth_commit_sha") or ""),
        pr_title=artifact.pr_title,
        pr_body_markdown=artifact.pr_body_markdown,
        unified_diff_patch=artifact.unified_diff_patch,
        files=[RebirthFileOut(**{key: entry.get(key) for key in ("path", "status", "additions", "deletions")})
               for entry in memory.get("changed_files") or () if isinstance(entry, dict)],
        exported_kiro_specs_path=artifact.exported_kiro_specs_path,
        spec_files=[str(path) for path in revival.get("spec_files") or ()],
        continuity_memory=memory,
        preview_url=artifact.preview_url,
        before_test_output=run.before_test_output,
        after_test_output=run.after_test_output,
        tests_passed=run.tests_passed,
        iterations_used=run.iterations_used,
        created_at=artifact.created_at,
        telemetry_ws_path=telemetry_ws_path(project.pk),
    )


@router.get(
    "/{uuid:project_id}/rebirth",
    response={200: RebirthOut, 404: ErrorOut, 409: RebirthNotReadyOut},
    summary="The rebirth artifact: unified diff, PR summary, exported .kiro specs and Continuity Memory",
)
def get_rebirth(request: HttpRequest, project_id: UUID) -> Any:
    project = get_object_or_404(Project, pk=project_id)
    artifact = RebirthArtifact.objects.select_related("foundry_run").filter(project=project).first()
    if artifact is not None:
        return 200, rebirth_out(project, artifact)
    status = ProjectStatus(project.status).value
    pending = project.status not in REBIRTH_FINAL_STATUSES
    return 409, RebirthNotReadyOut(
        detail=(f"The rebirth artifact is not ready: the project is {status}. Follow the telemetry WebSocket "
                "and request it again after REBIRTH_COMPLETE.") if pending
        else f"No rebirth artifact exists: the project is {status}.",
        code="REBIRTH_PENDING" if pending else "REBIRTH_UNAVAILABLE",
        project_id=project.pk,
        status=status,
        status_detail=project.status_detail,
        telemetry_ws_path=telemetry_ws_path(project.pk),
    )
