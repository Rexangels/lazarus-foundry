"""Strategy endpoints (mounted at ``/api/projects/`` in ``config.api``, next to the genome router).

``GET /api/projects/{project_id}/strategies``
    200 ``StrategiesOut``: the project's strategies in ``RESTORE``,
    ``MODERNIZE``, ``EXTRACT`` order with scores, breakdowns, action plans,
    spec bundles, approval state and each strategy's ``approval_token``; the
    ``recommended_strategy_id``, the ``approved_strategy_id`` and the latest
    ``StrategySynthesis`` summary (never the raw model reply).
    404 for an unknown project. 409 ``StrategiesNotReadyOut``:
    ``STRATEGIES_PENDING`` while the project is ``DORMANT``, ``INGESTING`` or
    ``SYNTHESIZING_GENOME``; ``STRATEGIES_UNAVAILABLE`` when no strategy
    exists in any other status (e.g. generation failed; ``status_detail`` says
    why).

``POST /api/projects/{project_id}/strategies/{strategy_id}/approve``
    Body ``{"approval_token": "<64 hex>"}`` (422 when missing or malformed).
    The human approval gate: ``apps.revival.services.approve_strategy`` does
    every check and write in one locked transaction. 200 ``ApprovalOut`` (also
    for an idempotent repeat), 403 ``APPROVER_REQUIRED`` when
    ``settings.REVIVAL_APPROVAL_REQUIRES_HEADER`` is on and the
    ``X-Lazarus-Approver`` header is missing or invalid, 404 for an unknown
    project or a strategy of another project, 409 ``ApprovalErrorOut``.

    The API has no authentication yet: without the header setting anyone who
    can reach the server can approve. The header is a minimal gate for a
    hosted demo, not authentication.

``GET /api/projects/{project_id}/strategies/{strategy_id}/specs/{doc}``
    One spec document as ``text/markdown`` with a ``Content-Disposition``
    filename (``inline``; ``?download=true`` gives ``attachment``). ``doc`` must
    be ``requirements.md``, ``design.md`` or ``tasks.md``; anything else is 404.
"""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from ninja import Router

from apps.projects.api import ErrorOut, telemetry_ws_path
from apps.projects.models import Project, ProjectStatus
from apps.revival.approval import (
    SPEC_DOCUMENTS,
    STRATEGY_ORDER,
    RankedStrategy,
    approval_is_intact,
    recommended_strategy_id,
    strategy_approval_token,
)
from apps.revival.models import RevivalStrategy, StrategySynthesis
from apps.revival.schemas import (
    ActionPlanOut,
    ApprovalErrorOut,
    ApprovalOut,
    ApprovalStateOut,
    ApproveRequest,
    ApproverRequiredOut,
    SpecBundleOut,
    SpecDocumentLinksOut,
    StrategiesNotReadyOut,
    StrategiesOut,
    StrategyOut,
    StrategyProjectOut,
    StrategySynthesisOut,
    StrategySynthesisValidationOut,
)
from apps.revival.services import ApprovalCode, ApprovalError, approve_strategy

router: Router = Router(tags=["revival"])

STRATEGIES_PENDING_STATUSES: frozenset[str] = frozenset({
    ProjectStatus.DORMANT,
    ProjectStatus.INGESTING,
    ProjectStatus.SYNTHESIZING_GENOME,
})
APPROVER_HEADER: str = "X-Lazarus-Approver"
APPROVER_MAX_LENGTH: int = RevivalStrategy._meta.get_field("approved_by").max_length or 255
SAFE_FILENAME = re.compile(r"[^a-z0-9-]+")
APPROVE_OPENAPI_EXTRA: dict[str, Any] = {
    "parameters": [{
        "name": APPROVER_HEADER,
        "in": "header",
        "required": False,
        "schema": {"type": "string", "maxLength": APPROVER_MAX_LENGTH},
        "description": "Approver name, stored as approved_by. Required when REVIVAL_APPROVAL_REQUIRES_HEADER is on.",
    }],
}
SPEC_OPENAPI_EXTRA: dict[str, Any] = {
    "responses": {
        200: {
            "description": "The spec document as Markdown.",
            "content": {"text/markdown": {"schema": {"type": "string"}}},
        },
    },
}


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def spec_document_url(project_id: UUID, strategy_id: UUID, doc: str) -> str:
    return f"/api/projects/{project_id}/strategies/{strategy_id}/specs/{doc}"


def _ordered(rows: list[RevivalStrategy]) -> list[RevivalStrategy]:
    return sorted(rows, key=lambda row: (STRATEGY_ORDER.get(row.strategy_type, len(STRATEGY_ORDER)), str(row.pk)))


def strategy_out(row: RevivalStrategy, *, recommended_id: str | None) -> StrategyOut:
    plan = row.action_plan if isinstance(row.action_plan, dict) else {}
    specs = row.generated_kiro_specs if isinstance(row.generated_kiro_specs, dict) else {}
    return StrategyOut(
        id=row.pk,
        strategy_type=row.strategy_type,
        title=row.title,
        summary=row.summary,
        feasibility_score=row.feasibility_score,
        value_score=row.value_score,
        debt_cost_score=row.debt_cost_score,
        risk_score=row.risk_score,
        score_breakdown=row.score_breakdown or {},
        is_actionable=row.is_actionable,
        is_recommended=recommended_id == str(row.pk),
        action_plan=ActionPlanOut(
            items=plan.get("items") or [],
            in_scope=plan.get("in_scope") or [],
            deferred=plan.get("deferred") or [],
            files=plan.get("files") or [],
            extract_root=plan.get("extract_root"),
            risk_evidence=plan.get("risk_evidence") or {},
        ),
        spec=SpecBundleOut(
            spec_name=str(specs.get("spec_name") or ""),
            requirements_md=str(specs.get("requirements_md") or ""),
            design_md=str(specs.get("design_md") or ""),
            tasks_md=str(specs.get("tasks_md") or ""),
            evidence_index=specs.get("evidence_index") or {},
            mode=str(specs.get("mode") or ""),
            generated_at=str(specs.get("generated_at") or ""),
            document_urls=SpecDocumentLinksOut(**{
                key: spec_document_url(row.project_id, row.pk, doc) for doc, key in SPEC_DOCUMENTS.items()
            }),
        ),
        approval=ApprovalStateOut(
            approved_by_human=row.approved_by_human,
            approved_at=row.approved_at,
            approved_by=row.approved_by,
            approval_token=strategy_approval_token(row),
            approval_intact=approval_is_intact(row, row.approved_token) if row.approved_by_human else None,
        ),
        synthesis_id=row.synthesis_id,
        created_at=row.created_at,
    )


def validation_summary(report: Any) -> StrategySynthesisValidationOut:
    report = report if isinstance(report, dict) else {}
    bundles = report.get("bundles") if isinstance(report.get("bundles"), dict) else {}
    applied = sum(
        int(value)
        for counts in bundles.values() if isinstance(counts, dict)
        for value in counts.values() if isinstance(value, (bool, int))
    )
    reason = report.get("fallback_reason")
    return StrategySynthesisValidationOut(
        fallback_reason=str(reason) if reason else None,
        refined_bundle_count=len(bundles),
        applied_prose_count=applied,
        dropped_prose_count=len(report.get("dropped") or ()),
    )


def synthesis_out(synthesis: StrategySynthesis | None) -> StrategySynthesisOut | None:
    if synthesis is None:
        return None
    return StrategySynthesisOut(
        id=synthesis.pk,
        mode=synthesis.mode,
        model_id=synthesis.model_id,
        started_at=synthesis.started_at,
        finished_at=synthesis.finished_at,
        preserved_strategy_types=list(synthesis.preserved_strategy_types or ()),
        validation=validation_summary(synthesis.validation_report),
    )


def parse_approver(raw: str | None) -> str | None:
    """The stripped approver name, or None when blank, too long or not printable."""
    name = (raw or "").strip()
    if not name or len(name) > APPROVER_MAX_LENGTH or not name.isprintable():
        return None
    return name


def _not_ready(project: Project, code: str, detail: str) -> StrategiesNotReadyOut:
    return StrategiesNotReadyOut(
        detail=detail,
        code=code,
        project_id=project.pk,
        status=ProjectStatus(project.status).value,
        status_detail=project.status_detail,
        telemetry_ws_path=telemetry_ws_path(project.pk),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get(
    "/{uuid:project_id}/strategies",
    response={200: StrategiesOut, 404: ErrorOut, 409: StrategiesNotReadyOut},
    summary="Revival strategies with scores, plans, spec bundles and approval tokens",
)
def list_strategies(request: HttpRequest, project_id: UUID) -> Any:
    project = get_object_or_404(Project, pk=project_id)
    status = ProjectStatus(project.status).value
    if project.status in STRATEGIES_PENDING_STATUSES:
        return 409, _not_ready(
            project,
            "STRATEGIES_PENDING",
            f"The strategies are not ready: the project is {status}. Follow the telemetry WebSocket "
            "and request them again after STRATEGY_GENERATED.",
        )
    rows = _ordered(list(RevivalStrategy.objects.filter(project=project)))
    if not rows:
        return 409, _not_ready(project, "STRATEGIES_UNAVAILABLE", f"No revival strategies exist: the project is {status}.")
    synthesis = (
        StrategySynthesis.objects.filter(project=project)
        .defer("raw_model_response")
        .order_by("-created_at", "-started_at")
        .first()
    )
    recommended = recommended_strategy_id(
        RankedStrategy(str(row.pk), row.strategy_type, row.feasibility_score, row.is_actionable) for row in rows
    )
    approved = next((row.pk for row in rows if row.approved_by_human), None)
    return 200, StrategiesOut(
        project=StrategyProjectOut(
            id=project.pk, name=project.name, status=status, status_detail=project.status_detail,
            default_branch=project.default_branch,
        ),
        synthesis=synthesis_out(synthesis),
        recommended_strategy_id=UUID(recommended) if recommended else None,
        approved_strategy_id=approved,
        telemetry_ws_path=telemetry_ws_path(project.pk),
        strategies=[strategy_out(row, recommended_id=recommended) for row in rows],
    )


@router.post(
    "/{uuid:project_id}/strategies/{uuid:strategy_id}/approve",
    response={200: ApprovalOut, 403: ApproverRequiredOut, 404: ErrorOut, 409: ApprovalErrorOut},
    summary="Human approval gate: approve exactly the reviewed strategy and queue the Foundry",
    openapi_extra=APPROVE_OPENAPI_EXTRA,
)
def approve(request: HttpRequest, project_id: UUID, strategy_id: UUID, payload: ApproveRequest) -> Any:
    approver = parse_approver(request.headers.get(APPROVER_HEADER))
    if settings.REVIVAL_APPROVAL_REQUIRES_HEADER and approver is None:
        return 403, ApproverRequiredOut(
            detail=f"Approval requires the {APPROVER_HEADER} header with a printable name of at most "
                   f"{APPROVER_MAX_LENGTH} characters.",
            code="APPROVER_REQUIRED",
        )
    try:
        outcome = approve_strategy(project_id, strategy_id, payload.approval_token, approver=approver or "")
    except ApprovalError as exc:
        if exc.code == ApprovalCode.NOT_FOUND:
            return 404, ErrorOut(detail=exc.detail)
        return 409, ApprovalErrorOut(
            detail=exc.detail,
            code=exc.code,
            project_id=project_id,
            strategy_id=strategy_id,
            current_approval_token=exc.current_token,
            approved_strategy_id=exc.approved_strategy_id,
        )
    strategy = outcome.strategy
    project_status = Project.objects.filter(pk=project_id).values_list("status", flat=True).first() or ""
    recommended = recommended_strategy_id(
        RankedStrategy(str(pk), kind, score, actionable)
        for pk, kind, score, actionable in RevivalStrategy.objects.filter(project_id=project_id).values_list(
            "pk", "strategy_type", "feasibility_score", "is_actionable"
        )
    )
    return 200, ApprovalOut(
        strategy=strategy_out(strategy, recommended_id=recommended),
        foundry_run_id=outcome.foundry_run.pk if outcome.foundry_run else None,
        project_status=project_status,
        already_approved=not outcome.created,
        telemetry_ws_path=telemetry_ws_path(project_id),
    )


@router.get(
    "/{uuid:project_id}/strategies/{uuid:strategy_id}/specs/{doc}",
    response={404: ErrorOut},
    summary="One spec document of a strategy as text/markdown (requirements.md, design.md or tasks.md)",
    openapi_extra=SPEC_OPENAPI_EXTRA,
)
def download_spec(request: HttpRequest, project_id: UUID, strategy_id: UUID, doc: str, download: bool = False) -> Any:
    key = SPEC_DOCUMENTS.get(doc)
    if key is None:  # checked before any query: only the three allowlisted names are ever served
        return 404, ErrorOut(detail=f"Unknown spec document; expected one of {', '.join(SPEC_DOCUMENTS)}.")
    specs = (
        RevivalStrategy.objects.filter(pk=strategy_id, project_id=project_id)
        .values_list("generated_kiro_specs", flat=True)
        .first()
    )
    text = specs.get(key) if isinstance(specs, dict) else None
    if not text:
        return 404, ErrorOut(detail="Spec document not found.")
    stem = SAFE_FILENAME.sub("-", str(specs.get("spec_name") or "").lower()).strip("-") or "lazarus-spec"
    response = HttpResponse(str(text), content_type="text/markdown; charset=utf-8")
    disposition = "attachment" if download else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{stem}-{doc}"'
    return response
