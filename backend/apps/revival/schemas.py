"""Revival schemas: the reasoning model's structured output, and the strategy API bodies.

1. ``ModelSpecRefinement`` and friends: what the reasoning model may return.
2. ``StrategiesOut``, ``ApproveRequest``, ``ApprovalOut`` and friends (bottom of the
   file): the bodies of ``apps.revival.api``. ``frontend/src/lib/types.ts``
   mirrors them (design.md, "Revival API contract").

Structured-output contract between ``SpecRefinementAgent`` and the reasoning model
-------------------------------------------------------------------------------------
The model may only rewrite prose of the deterministic spec skeletons
(``apps.revival.specs.SpecSkeleton``): each bundle's introduction and design
overview, each requirement's user story and each task's description, keyed by
the skeleton's requirement and task ids. The schema has no field for a
requirement, a task, an acceptance criterion, a file, a test or an evidence
label, so the model cannot add or remove any of them; the server re-renders
the Markdown from the skeleton. An empty string keeps the deterministic text.

``strict_response_schema()`` satisfies the same strict subset as the genome
schema (``apps.genome.schemas``): plain object root, ``additionalProperties:
false`` and every key ``required`` on every object, no ``oneOf``/``allOf``,
``$ref`` inlined, depth 3. Lengths are capped on the server.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from ninja import Schema
from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.genome.schemas import strict_json_schema
from apps.revival.approval import TOKEN_LENGTH

SCHEMA_NAME: str = "revival_spec_prose"

StrategyTypeName = Literal["RESTORE", "MODERNIZE", "EXTRACT"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelUserStory(_Strict):
    requirement_id: str = Field(description="A requirement id from the skeleton, e.g. \"1\".")
    user_story: str = Field(description="One sentence: 'As a <role>, I want <goal>, so that <benefit>.'")


class ModelTaskDescription(_Strict):
    task_id: str = Field(description="A task id from the skeleton, e.g. \"3\".")
    description: str = Field(description="One or two plain sentences on what the task does and why.")


class ModelSpecProse(_Strict):
    strategy_type: StrategyTypeName
    introduction: str = Field(description="Plain-text introduction of the recovery spec; empty keeps the draft.")
    design_overview: str = Field(description="Plain-text overview of the design; empty keeps the draft.")
    user_stories: list[ModelUserStory]
    task_descriptions: list[ModelTaskDescription]


class ModelSpecRefinement(_Strict):
    bundles: list[ModelSpecProse]
    notes: str = Field(description="Optional remarks; empty string if none.")


def strict_response_schema() -> dict[str, Any]:
    return strict_json_schema(ModelSpecRefinement)


def response_format() -> dict[str, Any]:
    """The ``response_format`` argument for ``chat.completions.create``."""
    return {
        "type": "json_schema",
        "json_schema": {"name": SCHEMA_NAME, "schema": strict_response_schema(), "strict": True},
    }


# ---------------------------------------------------------------------------
# Strategy API (apps.revival.api)
# ---------------------------------------------------------------------------
SynthesisModeName = Literal["DETERMINISTIC", "MODEL", "MODEL_FALLBACK"]
StrategiesNotReadyCode = Literal["STRATEGIES_PENDING", "STRATEGIES_UNAVAILABLE"]
ApprovalErrorCode = Literal[
    "PROJECT_NOT_READY",
    "STRATEGY_STALE",
    "STRATEGY_NOT_ACTIONABLE",
    "ANOTHER_STRATEGY_APPROVED",
    "APPROVED_CONTENT_CHANGED",
]


class StrategyProjectOut(Schema):
    id: UUID
    name: str
    status: str
    status_detail: str
    default_branch: str


class StrategySynthesisValidationOut(Schema):
    """Counts from ``StrategySynthesis.validation_report`` (never its free-text details)."""

    fallback_reason: str | None
    refined_bundle_count: int
    applied_prose_count: int
    dropped_prose_count: int


class StrategySynthesisOut(Schema):
    """The latest ``StrategySynthesis``. The raw model response and the input digest are never sent."""

    id: UUID
    mode: SynthesisModeName
    model_id: str
    started_at: datetime
    finished_at: datetime
    preserved_strategy_types: list[str]
    validation: StrategySynthesisValidationOut


class ActionPlanOut(Schema):
    """``RevivalStrategy.action_plan``. Items are PlanItems, ``in_scope``/``deferred`` the planner's entries
    (each deferral has ``reason`` and ``detail``); their shapes are in design.md, "PlanItem"."""

    items: list[dict[str, Any]]
    in_scope: list[dict[str, Any]]
    deferred: list[dict[str, Any]]
    files: list[str]
    extract_root: str | None
    risk_evidence: dict[str, Any]


class SpecDocumentLinksOut(Schema):
    requirements_md: str
    design_md: str
    tasks_md: str


class SpecBundleOut(Schema):
    spec_name: str
    requirements_md: str
    design_md: str
    tasks_md: str
    evidence_index: dict[str, dict[str, Any]]
    mode: str
    generated_at: str
    document_urls: SpecDocumentLinksOut  # text/markdown downloads


class ApprovalStateOut(Schema):
    approved_by_human: bool
    approved_at: datetime | None
    approved_by: str
    # Token of the current content; send it back to approve exactly what was reviewed.
    approval_token: str
    # Approved strategies only: whether the content still matches the token stored at approval.
    approval_intact: bool | None


class StrategyOut(Schema):
    id: UUID
    strategy_type: StrategyTypeName
    title: str
    summary: str
    feasibility_score: float
    value_score: float
    debt_cost_score: float
    risk_score: float
    score_breakdown: dict[str, Any]  # apps.revival.scoring.PlanScore.as_dict()
    is_actionable: bool
    is_recommended: bool
    action_plan: ActionPlanOut
    spec: SpecBundleOut
    approval: ApprovalStateOut
    synthesis_id: UUID | None
    created_at: datetime


class StrategiesOut(Schema):
    project: StrategyProjectOut
    synthesis: StrategySynthesisOut | None
    recommended_strategy_id: UUID | None
    approved_strategy_id: UUID | None
    telemetry_ws_path: str
    strategies: list[StrategyOut]  # RESTORE, MODERNIZE, EXTRACT


class StrategiesNotReadyOut(Schema):
    """409: there are no strategies to show yet (or generation failed)."""

    detail: str
    code: StrategiesNotReadyCode
    project_id: UUID
    status: str
    status_detail: str
    telemetry_ws_path: str


class ApproveRequest(Schema):
    model_config = ConfigDict(extra="forbid")

    approval_token: str = Field(min_length=TOKEN_LENGTH, max_length=TOKEN_LENGTH, pattern=r"^[0-9a-f]+$")

    @field_validator("approval_token", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class ApprovalOut(Schema):
    strategy: StrategyOut
    foundry_run_id: UUID | None
    project_status: str
    already_approved: bool  # True: an idempotent repeat, nothing changed
    telemetry_ws_path: str


class ApprovalErrorOut(Schema):
    detail: str
    code: ApprovalErrorCode
    project_id: UUID
    strategy_id: UUID
    # STRATEGY_STALE: the token of the current content, so the client can refresh and re-review.
    current_approval_token: str | None
    approved_strategy_id: UUID | None  # ANOTHER_STRATEGY_APPROVED


class ApproverRequiredOut(Schema):
    detail: str
    code: Literal["APPROVER_REQUIRED"]
