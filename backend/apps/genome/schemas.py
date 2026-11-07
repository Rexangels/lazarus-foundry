"""Genome schemas: the reasoning model's structured output, and the genome API response.

1. ``ModelGenomeResponse`` and friends: what the reasoning model may return.
2. ``GenomeGraphOut`` and friends (bottom of the file): the body of
   ``GET /api/projects/{project_id}/genome``, built by
   ``apps.genome.graph.build_genome_graph``. ``frontend/src/lib/types.ts``
   mirrors these classes (design.md, "Genome API contract").

Structured-output contract between ``GenomeReconcilerAgent`` and the reasoning model
-------------------------------------------------------------------------------------
The model may only propose grouping, titles, categories, prose and conflicts,
and it may only reference evidence by catalog id (``E17``). The schema has no
field for a score, an epistemic status, a line number or a snippet; the
server derives those from the catalog and ``apps.genome.scoring``.
``suggested_vital_status`` is advisory and is overridden by the evidence rules.

``strict_response_schema()`` is the JSON Schema sent as
``response_format={"type": "json_schema", "json_schema": {..., "strict": true}}``.
It satisfies Meta Model API's strict subset (modeled on OpenAI's): the root is
a plain object, every object has ``additionalProperties: false`` and lists all
its keys in ``required``, there is no ``oneOf``/``allOf``, ``$ref`` is inlined,
and the nesting depth stays far below 10. Length limits are not expressed in
the schema (their support in strict mode is not documented); the server caps
every string instead.
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from ninja import Schema
from pydantic import BaseModel, ConfigDict, Field

SCHEMA_NAME: str = "project_genome_refinement"

CategoryName = Literal["CORE_FEATURE", "ARCHITECTURE", "INTEGRATION", "DATA_MODEL"]
VitalStatusName = Literal["VERIFIED_WORKING", "ABANDONED_IN_PROGRESS", "GHOST_FEATURE"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConflict(_Strict):
    claim_citation_ids: list[str] = Field(
        description="Catalog ids of documentation items (README claims, doc sections) that make the claim."
    )
    reality_citation_ids: list[str] = Field(
        description="Catalog ids of code, branch or TODO items showing what the repository actually contains."
    )
    description: str = Field(description="One or two sentences: what is claimed and what the evidence shows.")
    resolution_recommendation: str = Field(description="A concrete next step to resolve the contradiction.")


class ModelFeature(_Strict):
    draft_keys: list[str] = Field(
        description="Keys of the draft features this feature refines or merges; empty for a new feature."
    )
    title: str = Field(description="Short product-level feature name.")
    category: CategoryName
    suggested_vital_status: VitalStatusName = Field(
        description="Your reading of the evidence. The server recomputes the status from the cited evidence."
    )
    reconstructed_intent: str = Field(description="What the software was meant to do, from the evidence.")
    actual_implementation_state: str = Field(description="What actually exists, citing files and branches.")
    citation_ids: list[str] = Field(description="Catalog ids supporting this feature. Ids only, never text.")
    conflicts: list[ModelConflict] = Field(description="Contradictions between documentation and code.")


class ModelGenomeResponse(_Strict):
    features: list[ModelFeature]
    notes: str = Field(description="Optional remarks about the analysis; empty string if none.")


def _inline(node: Any, defs: dict[str, Any], *, is_properties: bool = False) -> Any:
    """Replace ``$ref`` with the definition and drop ``$defs`` and Pydantic's ``title`` annotations."""
    if isinstance(node, dict):
        if is_properties:  # keys are field names (one of them is "title"), values are schemas
            return {key: _inline(value, defs) for key, value in node.items()}
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            merged = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
            return _inline(merged, defs)
        return {
            key: _inline(value, defs, is_properties=key == "properties")
            for key, value in node.items()
            if key != "$defs" and not (key == "title" and isinstance(value, str))
        }
    if isinstance(node, list):
        return [_inline(value, defs) for value in node]
    return node


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """``model`` as a self-contained strict-mode JSON Schema (also used by ``apps.revival.schemas``)."""
    schema = model.model_json_schema()
    inlined = _inline(schema, schema.get("$defs", {}))
    _enforce_strict(inlined)
    return inlined


def strict_response_schema() -> dict[str, Any]:
    """``ModelGenomeResponse`` as a self-contained strict-mode JSON Schema."""
    return strict_json_schema(ModelGenomeResponse)


def _enforce_strict(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        for value in node.values():
            _enforce_strict(value)
    elif isinstance(node, list):
        for value in node:
            _enforce_strict(value)


def response_format() -> dict[str, Any]:
    """The ``response_format`` argument for ``chat.completions.create``."""
    return {
        "type": "json_schema",
        "json_schema": {"name": SCHEMA_NAME, "schema": strict_response_schema(), "strict": True},
    }


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id}/genome (apps.genome.api, built by apps.genome.graph)
#
# React Flow-ready: every node is {id, type, position, data} and every edge
# {id, source, target, type, data}. Node and edge ids are deterministic.
# Colors are tokens; the frontend maps them to the product palette.
# ---------------------------------------------------------------------------
EpistemicStatusName = Literal["KNOWN", "INFERRED", "CONFLICTED", "MISSING_CONTEXT"]
EvidenceSourceTypeName = Literal[
    "AST_IMPLEMENTATION", "UNMERGED_BRANCH_COMMIT", "ISSUE_SPEC", "TODO_COMMENT", "README_CLAIM"
]
SynthesisModeName = Literal["DETERMINISTIC", "MODEL", "MODEL_FALLBACK"]
# alive = VERIFIED_WORKING, dormant = ABANDONED_IN_PROGRESS or an abandoned branch,
# ghost = GHOST_FEATURE, conflict = a contradicted claim, neutral = anything else.
ColorToken = Literal["alive", "dormant", "ghost", "conflict", "neutral"]
GenomeNotReadyCode = Literal["GENOME_PENDING", "GENOME_UNAVAILABLE"]


class GraphPosition(Schema):
    x: float
    y: float


class GenomeProjectOut(Schema):
    id: UUID
    name: str
    status: str
    status_detail: str
    default_branch: str


class SynthesisValidationOut(Schema):
    """Counts from ``GenomeSynthesis.validation_report`` (never its free-text details)."""

    dropped_citation_count: int
    unsupported_citation_count: int
    rejected_feature_count: int
    dropped_conflict_count: int
    vital_status_override_count: int
    fallback_reason: str | None


class SynthesisOut(Schema):
    """The latest ``GenomeSynthesis``. The raw model response and the full input digest are never sent."""

    id: UUID | None
    mode: SynthesisModeName
    model_id: str
    started_at: datetime
    finished_at: datetime
    catalog_size: int
    node_count: int
    conflict_count: int
    input_digest_prefix: str
    validation: SynthesisValidationOut


class FeatureNodeData(Schema):
    feature_id: str
    feature_key: str | None  # reconciler key, stable across re-synthesis (the node id is not)
    title: str
    category: CategoryName
    vital_status: VitalStatusName
    epistemic_status: EpistemicStatusName
    confidence_score: float
    reconstructed_intent: str
    actual_implementation_state: str
    citation_count: int
    conflict_count: int
    color_token: ColorToken


class FeatureNode(Schema):
    id: str  # the IntentNode id
    type: Literal["feature"]
    position: GraphPosition
    data: FeatureNodeData


class BranchNodeData(Schema):
    name: str
    head_sha: str
    last_commit_date: datetime | None
    is_abandoned: bool
    is_merged: bool
    commits_ahead: int
    commits_behind: int
    dormancy_days: float
    citation_count: int  # citations of branch-only evidence from this branch
    feature_ids: list[str]  # features citing it (empty for an uncited abandoned branch)
    color_token: ColorToken


class BranchNode(Schema):
    id: str  # "branch:<name>"
    type: Literal["branch"]
    position: GraphPosition
    data: BranchNodeData


class DocumentNodeData(Schema):
    file_path: str
    conflict_count: int
    feature_ids: list[str]
    color_token: ColorToken


class DocumentNode(Schema):
    id: str  # "document:<file_path>"
    type: Literal["document"]
    position: GraphPosition
    data: DocumentNodeData


GenomeNodeOut = Annotated[FeatureNode | BranchNode | DocumentNode, Field(discriminator="type")]


class EvidenceEdgeData(Schema):
    citation_count: int
    source_types: list[EvidenceSourceTypeName]
    color_token: ColorToken


class EvidenceEdge(Schema):
    id: str  # "evidence:<feature id>-><branch node id>"
    source: str
    target: str
    type: Literal["evidence"]
    data: EvidenceEdgeData


class ConflictEdgeData(Schema):
    conflict_id: str
    claim_source: str
    file_path: str
    line_start: int | None
    line_end: int | None
    claim_citation_id: str | None
    color_token: ColorToken


class ConflictEdge(Schema):
    id: str  # "conflict:<conflict id>"
    source: str
    target: str
    type: Literal["conflict"]
    data: ConflictEdgeData


GenomeEdgeOut = Annotated[EvidenceEdge | ConflictEdge, Field(discriminator="type")]


class CitationOut(Schema):
    """One ``EvidenceCitation`` (the Evidence Drawer). ``snippet`` is the exact cited text."""

    id: str
    feature_id: str
    source_type: EvidenceSourceTypeName
    file_path: str
    commit_sha: str
    line_start: int | None
    line_end: int | None
    snippet: str
    weight: float
    branch_name: str | None
    is_branch_only: bool
    artifact_id: int | None
    signal_index: int | None  # joins ScoreTermOut.signal_index / ScoreCitationOut.signal_index


class ConflictOut(Schema):
    """One ``ConflictRecord`` (the Conflict Inspector), with the claim it was resolved to."""

    id: str
    feature_id: str
    claim_source: str
    reality_source: str
    conflict_description: str
    resolution_recommendation: str
    created_at: datetime | None
    claim_citation_id: str | None
    claim_file_path: str | None
    claim_line_start: int | None
    claim_line_end: int | None
    document_node_id: str | None
    edge_id: str | None


class ScoreTermOut(Schema):
    signal_index: int
    rank: int
    match_strength: float
    age_days: float
    decay: float
    base_term: float
    rank_factor: float
    term: float


class ScoreContributionOut(Schema):
    source_type: EvidenceSourceTypeName
    authority_weight: float
    signal_count: int
    uncapped: float
    cap: float
    contribution: float
    capped: bool
    terms: list[ScoreTermOut] = []


class ScoreCitationOut(Schema):
    signal_index: int
    catalog_id: str
    source_type: EvidenceSourceTypeName
    match_kind: str
    match_strength: float
    weight: float


class ScoreBreakdownOut(Schema):
    """``IntentNode.score_breakdown``: how C(f) = sigmoid(z) was reached (forensic-invariants.md section 2)."""

    confidence: float
    z: float | None = None
    evidence_total: float | None = None
    prior_bias: float | None = None
    conflict: bool = False
    conflict_penalty: float | None = None
    contributions: list[ScoreContributionOut] = []
    epistemic_status: EpistemicStatusName
    status_reason: str | None = None
    vital_status: VitalStatusName
    vital_status_reason: str | None = None
    feature_key: str | None = None
    origin: str | None = None
    matched_evidence_count: int | None = None
    citations: list[ScoreCitationOut] = []
    parameters: dict[str, Any] = {}


class BranchSummaryOut(Schema):
    name: str
    head_sha: str
    last_commit_date: datetime | None
    is_default: bool
    is_merged: bool
    is_abandoned: bool
    commits_ahead: int
    commits_behind: int
    dormancy_days: float
    node_id: str | None  # the branch node, when the graph draws one


class GenomeGraphOut(Schema):
    project: GenomeProjectOut
    synthesis: SynthesisOut | None
    nodes: list[GenomeNodeOut]
    edges: list[GenomeEdgeOut]
    citations: dict[str, list[CitationOut]]  # keyed by feature id
    conflicts: list[ConflictOut]
    score_breakdowns: dict[str, ScoreBreakdownOut]  # keyed by feature id
    branches: list[BranchSummaryOut]


class GenomeNotReadyOut(Schema):
    """409: there is no genome to show yet (or the pipeline failed before producing one)."""

    detail: str
    code: GenomeNotReadyCode
    project_id: UUID
    status: str
    status_detail: str
    telemetry_ws_path: str
