"""Database side of System 3: plan, score, write the spec bundles and persist the three ``RevivalStrategy`` rows.

``generate_strategies(project)``
    1. Loads the latest genome (``apps.genome.services.load_genome_inputs``)
       and the default branch's dependency and import facts from the
       ingestion artifacts.
    2. Plans and scores ``RESTORE``, ``MODERNIZE`` and ``EXTRACT``
       (``planner.plan_strategies``, ``scoring.score_plans``).
    3. Builds each strategy's spec skeleton and validates its deterministic
       bundle; an invalid bundle raises ``SpecValidationError`` before
       anything is written.
    4. Lets ``SpecRefinementAgent`` rewrite the prose (never the structure);
       model failures fall back inside the agent.
    5. Persists, in one transaction under a row lock on the project: a
       ``StrategySynthesis`` provenance row and the new strategies.

Regeneration and approved strategies
    Unapproved strategies are always replaced (deleted and re-created), so
    the rows always match the latest genome. An approved strategy is never
    modified or deleted: it is the human-signed work order the Foundry runs.
    Its type is skipped on regeneration (the unique ``(project,
    strategy_type)`` row stays as approved, scores and specs included), the
    new ``StrategySynthesis.preserved_strategy_types`` lists it, and
    ``STRATEGY_GENERATED`` reports it with ``regenerated: false``. Its
    ``action_plan`` feature ids may then refer to a previous genome; that
    plan is a frozen snapshot by design.

Concurrency
    The persist step re-checks, under ``select_for_update`` on the project,
    that the project is still ``AUTOPSY_COMPLETE`` (else
    ``GenerationRefusedError``) and that the genome the plans were built from
    is still the latest ``GenomeSynthesis`` (else ``StaleGenomeError``; a
    newer genome run generates its own strategies). Either way it writes
    nothing.

Frozen after approval
    Generation runs only while the project is ``AUTOPSY_COMPLETE``. Once a
    strategy is approved (``STRATEGY_APPROVED`` and every later status),
    ``generate_strategies`` raises ``GenerationRefusedError`` before planning,
    and the persist step raises it too if an approval committed meanwhile.
    Nothing is written in either case.

``run_strategy_generation(project_id)`` wraps this for the genome task
(``apps.genome.tasks.on_genome_complete``): it never raises, emits
``STRATEGY_GENERATED`` per strategy, and on failure keeps the project at
``AUTOPSY_COMPLETE`` (the genome is intact and viewable, and re-synthesis
from ``AUTOPSY_COMPLETE`` retries generation; ``FAILED`` would force a full
re-ingestion), records a user-safe ``status_detail``, removes stale
unapproved strategies and emits ``ERROR`` with ``stage: "REVIVAL"``.

Approval (``approve_strategy``, called by ``POST .../strategies/{id}/approve``)
    One transaction under ``select_for_update`` on the project, then the
    strategy. It checks, in order: the strategy belongs to the project (else
    ``ApprovalError`` ``NOT_FOUND``); an already approved strategy is an
    idempotent success when the token matches (no writes, no events); no other
    strategy of the project is approved; the project is ``AUTOPSY_COMPLETE``;
    the token matches the current content (``apps.revival.approval``); the
    strategy is actionable. Success sets ``approved_by_human``,
    ``approved_at``, ``approved_token`` and ``approved_by``, moves the project
    to ``STRATEGY_APPROVED`` and creates a ``FoundryRun`` in ``ENVIRONMENT``
    with ``settings.SANDBOX_MODE``. The partial unique constraint
    ``revival_revivalstrategy_one_approved_per_project`` backs the
    one-approval rule in the database.

``on_strategy_approved(strategy, foundry_run)`` runs after that transaction
commits. It is the single place that decides what follows an approval, as
``apps.ingestion.tasks.on_ingestion_complete`` is for ingestion. Today it
logs and emits ``STRATEGY_APPROVED``; the ``FoundryRun`` row in
``ENVIRONMENT`` is the queryable "approved, Foundry queued" state. Task 6.2
makes it enqueue the Foundry pipeline for that run.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from apps.foundry.models import AgentStage, FoundryRun
from apps.genome.graph import GenomeInputs
from apps.genome.models import GenomeSynthesis
from apps.genome.services import load_genome_inputs
from apps.ingestion.models import ArtifactRecord, ArtifactType
from apps.projects.models import Project, ProjectStatus
from apps.revival.agents import SpecRefinementAgent, SpecRefinementResult
from apps.revival.approval import strategy_approval_token
from apps.revival.models import RevivalStrategy, StrategySynthesis, StrategyType
from apps.revival.planner import (
    StrategyPlan,
    dependencies_from_artifacts,
    import_graph_from_artifacts,
    plan_strategies,
)
from apps.revival.scoring import PlanScore, score_plans
from apps.revival.specs import (
    SpecBundle,
    SpecSkeleton,
    SpecValidationError,
    build_spec_skeleton,
    render_skeleton,
    validate_spec_bundle,
)
from apps.telemetry.broadcast import broadcast_telemetry
from apps.telemetry.events import TelemetryEventType

logger = logging.getLogger(__name__)

MAX_STATUS_DETAIL_CHARS: int = 2000
PLANNING_ARTIFACT_TYPES: tuple[str, ...] = (ArtifactType.DEPENDENCY_MANIFEST, ArtifactType.AST_MODULE)
PLANNING_ARTIFACT_FIELDS: tuple[str, ...] = ("id", "artifact_type", "file_path", "branch_name", "structured_metadata")
INTERNAL_ERROR_DETAIL: str = (
    "Revival strategies could not be generated because of an internal error; the genome is intact. "
    "The worker log has the details; re-run genome synthesis to retry."
)

Emit = Callable[[TelemetryEventType, dict[str, Any]], None]


class RevivalError(Exception):
    """A strategy-generation failure whose message is safe to show as ``Project.status_detail``."""


class StaleGenomeError(RevivalError):
    """The genome changed (or the project left ``AUTOPSY_COMPLETE``) while strategies were being generated."""


class GenerationRefusedError(RevivalError):
    """The project is past ``AUTOPSY_COMPLETE`` (a strategy is approved): its strategies are frozen."""


class ApprovalCode:
    NOT_FOUND = "NOT_FOUND"
    PROJECT_NOT_READY = "PROJECT_NOT_READY"
    STRATEGY_STALE = "STRATEGY_STALE"
    STRATEGY_NOT_ACTIONABLE = "STRATEGY_NOT_ACTIONABLE"
    ANOTHER_STRATEGY_APPROVED = "ANOTHER_STRATEGY_APPROVED"
    APPROVED_CONTENT_CHANGED = "APPROVED_CONTENT_CHANGED"


class ApprovalError(Exception):
    """An approval the gate refuses; ``code`` is an ``ApprovalCode``, ``detail`` is safe to show."""

    def __init__(self, code: str, detail: str, *, current_token: str | None = None,
                 approved_strategy_id: UUID | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.current_token = current_token
        self.approved_strategy_id = approved_strategy_id


@dataclass(frozen=True)
class ApprovalOutcome:
    strategy: RevivalStrategy
    foundry_run: FoundryRun | None  # None only for a row approved outside this service (no run exists)
    created: bool  # False: idempotent repeat, nothing written

@dataclass(frozen=True)
class GeneratedStrategy:
    strategy: RevivalStrategy
    regenerated: bool  # False: an approved row kept unchanged


@dataclass(frozen=True)
class GeneratedStrategies:
    synthesis: StrategySynthesis
    strategies: tuple[GeneratedStrategy, ...]  # RESTORE, MODERNIZE, EXTRACT


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def plan_project(project: Project, genome: GenomeInputs) -> tuple[StrategyPlan, ...]:
    """The three plans for ``genome``, with the default branch's dependencies and imports from ingestion."""
    default = project.default_branch
    artifacts = list(
        ArtifactRecord.objects.filter(project=project, branch_name=default, artifact_type__in=PLANNING_ARTIFACT_TYPES)
        .only(*PLANNING_ARTIFACT_FIELDS)
        .order_by("id")
    )
    return plan_strategies(
        genome,
        dependencies=dependencies_from_artifacts(artifacts, default),
        imports=import_graph_from_artifacts(artifacts, default),
    )


def build_skeletons(
    plans: Sequence[StrategyPlan], scores: Sequence[PlanScore], genome: GenomeInputs
) -> dict[StrategyType, SpecSkeleton]:
    """Deterministic skeletons; raises ``SpecValidationError`` if any rendered bundle is not a valid spec."""
    skeletons: dict[StrategyType, SpecSkeleton] = {}
    for plan, score in zip(plans, scores, strict=True):
        skeleton = build_spec_skeleton(plan, score, genome)
        validate_spec_bundle(render_skeleton(skeleton))
        skeletons[StrategyType(plan.strategy_type)] = skeleton
    return skeletons


def action_plan_json(plan: StrategyPlan) -> dict[str, Any]:
    """``RevivalStrategy.action_plan``: the Foundry's work order plus scope and deferrals."""
    body = plan.as_dict()
    return {
        "items": body["action_plan"],
        "in_scope": body["in_scope"],
        "deferred": body["deferred"],
        "files": body["files"],
        "extract_root": body["extract_root"],
        "risk_evidence": body["risk_evidence"],
    }


def generate_strategies(project: Project, *, agent: SpecRefinementAgent | None = None) -> GeneratedStrategies:
    """Plan, score, write and persist the project's three strategies (see the module docstring)."""
    if project.status != ProjectStatus.AUTOPSY_COMPLETE:
        raise GenerationRefusedError(
            f"Revival strategies can only be generated while the project is AUTOPSY_COMPLETE (it is {project.status})."
        )
    genome = load_genome_inputs(project)
    if not genome.features:
        raise RevivalError("Revival strategies could not be generated: the project has no genome yet.")
    plans = plan_project(project, genome)
    scores = score_plans(plans)
    skeletons = build_skeletons(plans, scores, genome)
    refinement = (agent or SpecRefinementAgent()).refine(skeletons)
    bundles = {kind: render_skeleton(refinement.skeletons[kind]) for kind in skeletons}
    for bundle in bundles.values():  # the agent validated refined bundles already; this is the last gate
        validate_spec_bundle(bundle)
    genome_synthesis_id = genome.synthesis.id if genome.synthesis is not None else None
    return persist_strategies(project, genome_synthesis_id, plans, scores, bundles, refinement)


def _latest_genome_synthesis_id(project: Project) -> UUID | None:
    return (
        GenomeSynthesis.objects.filter(project=project)
        .order_by("-created_at", "-started_at")
        .values_list("pk", flat=True)
        .first()
    )


def persist_strategies(
    project: Project,
    genome_synthesis_id: UUID | None,
    plans: Sequence[StrategyPlan],
    scores: Sequence[PlanScore],
    bundles: Mapping[StrategyType, SpecBundle],
    refinement: SpecRefinementResult,
) -> GeneratedStrategies:
    """Replace the unapproved strategies and record provenance, in one transaction (see the module docstring)."""
    generated_at = refinement.finished_at.isoformat()
    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        if locked.status != ProjectStatus.AUTOPSY_COMPLETE:
            raise GenerationRefusedError(
                f"the project moved to {locked.status} while strategies were being generated"
            )
        if _latest_genome_synthesis_id(locked) != genome_synthesis_id:
            raise StaleGenomeError("a newer genome was synthesized while strategies were being generated")
        existing = {row.strategy_type: row for row in RevivalStrategy.objects.select_for_update().filter(project=locked)}
        approved = {kind for kind, row in existing.items() if row.approved_by_human}
        RevivalStrategy.objects.filter(project=locked, approved_by_human=False).delete()
        synthesis = StrategySynthesis.objects.create(
            project=locked,
            genome_synthesis_id=genome_synthesis_id,
            mode=refinement.mode,
            model_id=refinement.model_id,
            started_at=refinement.started_at,
            finished_at=refinement.finished_at,
            input_digest=refinement.input_digest,
            validation_report=refinement.validation_report,
            raw_model_response=refinement.raw_response,
            preserved_strategy_types=sorted(approved),
        )
        rows: dict[str, GeneratedStrategy] = {}
        for plan, score in zip(plans, scores, strict=True):
            kind = StrategyType(plan.strategy_type)
            if kind in approved:
                rows[kind] = GeneratedStrategy(existing[kind], regenerated=False)
                continue
            row = RevivalStrategy.objects.create(
                project=locked,
                synthesis=synthesis,
                strategy_type=kind,
                title=plan.title[:255],
                summary=plan.summary,
                **score.model_fields(),
                score_breakdown=score.as_dict(),
                action_plan=action_plan_json(plan),
                is_actionable=plan.is_actionable,
                generated_kiro_specs=bundles[kind].as_storage(mode=str(refinement.mode.value), generated_at=generated_at),
            )
            rows[kind] = GeneratedStrategy(row, regenerated=True)
    ordered = tuple(rows[kind] for kind in StrategyType if kind in rows)
    return GeneratedStrategies(synthesis=synthesis, strategies=ordered)


# ---------------------------------------------------------------------------
# Lifecycle (called by apps.genome.tasks.on_genome_complete)
# ---------------------------------------------------------------------------
def telemetry_emitter(project_id: UUID) -> Emit:
    """An ``emit`` callback that broadcasts to the project's group and never raises."""

    def emit(event_type: TelemetryEventType, payload: dict[str, Any]) -> None:
        try:
            broadcast_telemetry(project_id, event_type, payload)
        except Exception:
            logger.exception("Could not broadcast %s for project %s.", event_type, project_id)

    return emit


def user_safe_error(exc: BaseException) -> str:
    if isinstance(exc, RevivalError):
        return str(exc)
    if isinstance(exc, SpecValidationError):
        return ("Revival strategies could not be generated: a recovery spec failed format validation. "
                "The genome is intact; the worker log has the details.")
    if isinstance(exc, DatabaseError):
        return "Saving the revival strategies failed because of a database error. The genome is intact."
    return INTERNAL_ERROR_DETAIL


def strategy_payload(generated: GeneratedStrategy, synthesis: StrategySynthesis) -> dict[str, Any]:
    row = generated.strategy
    plan = row.action_plan or {}
    specs = row.generated_kiro_specs or {}
    return {
        "strategy_id": str(row.pk),
        "strategy_type": row.strategy_type,
        "title": row.title,
        "feasibility_score": row.feasibility_score,
        "value_score": row.value_score,
        "debt_cost_score": row.debt_cost_score,
        "risk_score": row.risk_score,
        "in_scope_count": len(plan.get("in_scope") or ()),
        "deferred_count": len(plan.get("deferred") or ()),
        "item_count": len(plan.get("items") or ()),
        "is_actionable": row.is_actionable,
        "approved_by_human": row.approved_by_human,
        "regenerated": generated.regenerated,
        "spec_name": specs.get("spec_name", ""),
        "spec_mode": specs.get("mode", ""),
        "synthesis_id": str(synthesis.pk),
    }


def _record_failure(project: Project, detail: str) -> None:
    """Keep the status (the genome is valid), explain the failure, and drop strategies of an older genome.

    Only while the project is still ``AUTOPSY_COMPLETE``: once a strategy is approved, nothing is touched.
    """
    try:
        with transaction.atomic():
            locked = Project.objects.select_for_update().filter(pk=project.pk).first()
            if locked is None or locked.status != ProjectStatus.AUTOPSY_COMPLETE:
                return
            Project.objects.filter(pk=project.pk).update(
                status_detail=detail[:MAX_STATUS_DETAIL_CHARS], updated_at=timezone.now()
            )
            RevivalStrategy.objects.filter(project=project, approved_by_human=False).delete()
    except Exception:
        logger.exception("Could not record the strategy-generation failure of project %s.", project.pk)


def run_strategy_generation(
    project_id: UUID | str, *, agent: SpecRefinementAgent | None = None, emit: Emit | None = None
) -> dict[str, Any]:
    """Generate, persist and announce one project's strategies (see the module docstring). Never raises."""
    try:
        project = Project.objects.get(pk=project_id)
    except Project.DoesNotExist:
        logger.warning("Strategy generation requested for unknown project %s.", project_id)
        return {"project_id": str(project_id), "status": "NOT_FOUND"}
    if project.status != ProjectStatus.AUTOPSY_COMPLETE:
        logger.warning("Refusing to generate strategies for project %s: status is %s.", project.pk, project.status)
        return {"project_id": str(project.pk), "status": "REFUSED"}
    emit = emit or telemetry_emitter(project.pk)
    try:
        generated = generate_strategies(project, agent=agent)
    except StaleGenomeError as exc:
        logger.info("Strategies of project %s were superseded: %s", project.pk, exc)
        return {"project_id": str(project.pk), "status": "SUPERSEDED"}
    except GenerationRefusedError as exc:
        logger.info("Strategies of project %s are frozen: %s", project.pk, exc)
        return {"project_id": str(project.pk), "status": "REFUSED"}
    except Exception as exc:
        logger.exception("Strategy generation of project %s failed.", project.pk)
        detail = user_safe_error(exc)
        _record_failure(project, detail)
        emit(TelemetryEventType.ERROR, {"stage": "REVIVAL", "detail": detail})
        return {"project_id": str(project.pk), "status": "FAILED", "detail": detail}
    for strategy in generated.strategies:
        emit(TelemetryEventType.STRATEGY_GENERATED, strategy_payload(strategy, generated.synthesis))
    return {
        "project_id": str(project.pk),
        "status": "GENERATED",
        "mode": generated.synthesis.mode,
        "strategy_ids": [str(s.strategy.pk) for s in generated.strategies],
        "preserved_strategy_types": list(generated.synthesis.preserved_strategy_types),
    }


# ---------------------------------------------------------------------------
# Approval (called by apps.revival.api)
# ---------------------------------------------------------------------------
def approval_foundry_run(strategy: RevivalStrategy) -> FoundryRun | None:
    """The ``FoundryRun`` created when ``strategy`` was approved (its first run)."""
    return FoundryRun.objects.filter(strategy=strategy).order_by("created_at", "id").first()


def approve_strategy(
    project_id: UUID | str, strategy_id: UUID | str, token: str, *, approver: str = ""
) -> ApprovalOutcome:
    """Approve ``strategy_id`` if ``token`` matches its current content (see the module docstring).

    Raises ``ApprovalError``. ``on_strategy_approved`` runs after the transaction commits.
    """
    try:
        with transaction.atomic():
            project = Project.objects.select_for_update().filter(pk=project_id).first()
            if project is None:
                raise ApprovalError(ApprovalCode.NOT_FOUND, "Project not found.")
            strategy = RevivalStrategy.objects.select_for_update().filter(pk=strategy_id, project=project).first()
            if strategy is None:
                raise ApprovalError(ApprovalCode.NOT_FOUND, "Strategy not found in this project.")
            current = strategy_approval_token(strategy)
            if strategy.approved_by_human:
                return _repeat_approval(strategy, token, current)
            other = (
                RevivalStrategy.objects.filter(project=project, approved_by_human=True)
                .exclude(pk=strategy.pk)
                .values_list("pk", flat=True)
                .first()
            )
            if other is not None:
                raise ApprovalError(
                    ApprovalCode.ANOTHER_STRATEGY_APPROVED,
                    "Another strategy of this project is already approved; a project runs one approved strategy.",
                    approved_strategy_id=other,
                )
            if project.status != ProjectStatus.AUTOPSY_COMPLETE:
                raise ApprovalError(
                    ApprovalCode.PROJECT_NOT_READY,
                    f"Strategies can only be approved while the project is AUTOPSY_COMPLETE (it is {project.status}).",
                )
            if token != current:
                raise ApprovalError(
                    ApprovalCode.STRATEGY_STALE,
                    "The strategy changed since it was loaded; review the current version and approve again.",
                    current_token=current,
                )
            if not strategy.is_actionable:
                raise ApprovalError(
                    ApprovalCode.STRATEGY_NOT_ACTIONABLE,
                    "This strategy has no plan items, so there is nothing for the Foundry to run.",
                )
            now = timezone.now()
            strategy.approved_by_human = True
            strategy.approved_at = now
            strategy.approved_token = current
            strategy.approved_by = approver
            strategy.save(update_fields=["approved_by_human", "approved_at", "approved_token", "approved_by",
                                         "updated_at"])
            project.status = ProjectStatus.STRATEGY_APPROVED
            project.status_detail = ""
            project.save(update_fields=["status", "status_detail", "updated_at"])
            run = FoundryRun.objects.create(
                project=project, strategy=strategy, agent_stage=AgentStage.ENVIRONMENT,
                sandbox_mode=settings.SANDBOX_MODE,
            )
            transaction.on_commit(lambda: on_strategy_approved(strategy, run))
    except IntegrityError as exc:  # the one-approval constraint, if a writer bypassed the checks above
        raise ApprovalError(
            ApprovalCode.ANOTHER_STRATEGY_APPROVED,
            "Another strategy of this project is already approved; a project runs one approved strategy.",
        ) from exc
    return ApprovalOutcome(strategy=strategy, foundry_run=run, created=True)


def _repeat_approval(strategy: RevivalStrategy, token: str, current: str) -> ApprovalOutcome:
    """An already approved strategy: the same result for the same token, never a write."""
    if token != current:
        raise ApprovalError(
            ApprovalCode.STRATEGY_STALE,
            "The approved strategy differs from the version that was sent; reload it.",
            current_token=current,
        )
    if strategy.approved_token != current:
        raise ApprovalError(
            ApprovalCode.APPROVED_CONTENT_CHANGED,
            "The approved strategy's content no longer matches what was approved; it cannot be used.",
        )
    return ApprovalOutcome(strategy=strategy, foundry_run=approval_foundry_run(strategy), created=False)


def approved_payload(strategy: RevivalStrategy, foundry_run: FoundryRun) -> dict[str, Any]:
    return {
        "strategy_id": str(strategy.pk),
        "strategy_type": strategy.strategy_type,
        "title": strategy.title,
        "feasibility_score": strategy.feasibility_score,
        "approved_at": strategy.approved_at.isoformat() if strategy.approved_at else None,
        "approved_by": strategy.approved_by,
        "foundry_run_id": str(foundry_run.pk),
        "agent_stage": foundry_run.agent_stage,
        "sandbox_mode": foundry_run.sandbox_mode,
        "status": ProjectStatus.STRATEGY_APPROVED.value,
    }


def on_strategy_approved(strategy: RevivalStrategy, foundry_run: FoundryRun, *, emit: Emit | None = None) -> None:
    """Decide what follows a committed approval (see the module docstring). Never raises.

    Today: log and emit ``STRATEGY_APPROVED``. Task 6.2 enqueues the Foundry pipeline for ``foundry_run`` here.
    """
    try:
        logger.info(
            "Strategy %s (%s) of project %s approved; FoundryRun %s is queued in %s.",
            strategy.pk, strategy.strategy_type, strategy.project_id, foundry_run.pk, foundry_run.agent_stage,
        )
        (emit or telemetry_emitter(strategy.project_id))(
            TelemetryEventType.STRATEGY_APPROVED, approved_payload(strategy, foundry_run)
        )
    except Exception:
        logger.exception("Announcing the approval of strategy %s failed.", strategy.pk)
