"""The Autopsy view's data: ``build_genome_graph`` turns a persisted genome into ``GenomeGraphOut``.

Pure: no ORM queries, no Git, no network. Inputs are the frozen dataclasses
below. ``*.from_model`` reads model instances whose relations are already
prefetched (``apps.genome.services.load_genome_inputs``), and
``inputs_from_features`` wraps an in-memory ``GenomeSynthesisResult`` /
reconciler output the same way, so both paths share one builder.

Graph shape (React Flow: nodes ``{id, type, position, data}``, edges ``{id, source, target, type, data}``)
    * ``feature`` node per ``IntentNode`` (id = the node id), at its stored
      ``graph_position`` (columns by vital status, rows by title).
    * ``branch`` node (id ``branch:<name>``) per non-default branch that a
      citation draws branch-only evidence from, plus every abandoned branch
      even when nothing cites it. One column right of the features, each node
      at the mean height of the features citing it, stacked without overlap.
    * ``document`` node (id ``document:<path>``) per documentation file that
      holds the claim side of a conflict. One column left of the features.
      Claims sit left, reality (branches) right.
    * There are no generic "artifact" nodes for files several features cite:
      in PayPulse README.md alone is cited by every claim-derived feature and
      the test modules by several, so such hubs would mostly add edges that
      the Evidence Drawer (``citations``) already lists per feature.
    * ``evidence`` edge (id ``evidence:<feature>-><branch node>``) when a
      feature cites branch-only evidence of that branch.
    * ``conflict`` edge (id ``conflict:<conflict id>``) per ``ConflictRecord``,
      from the feature to the document node of the contradicted claim. Its
      data names the claim (file, lines, citation id), so it is self-describing.
      The full record is in ``conflicts`` (the Conflict Inspector).

Claim resolution: a ``ConflictRecord.claim_source`` starts with the claim's
``path:line`` (or ``path:start-end``) location followed by ``: ``. The claim
is the feature's README/doc citation with that location; failing that, the
location is parsed from the text (the document node is still drawn, without a
citation id). A conflict whose claim cannot be located has no edge and keeps
``edge_id: null``.

Branch of a citation: the ``ArtifactRecord.branch_name`` it came from; when
the artifact row is gone (``SET_NULL``), the branch whose head is the
citation's ``commit_sha`` (the default branch wins ties). Branch-only means a
non-default branch.

Ordering: feature nodes by (x, y, title, id); branch and document nodes by
(y, name); evidence edges then conflict edges, both in feature order;
citations by ``EvidenceSourceType`` declaration order, then file, lines and
id; branches with the default first, then by name.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from apps.genome.models import EvidenceSourceType, VitalStatus
from apps.genome.reconciler import GRAPH_COLUMN_X, GRAPH_ROW_HEIGHT, GenomeFeature
from apps.genome.schemas import (
    BranchNode,
    BranchNodeData,
    BranchSummaryOut,
    CitationOut,
    ColorToken,
    ConflictEdge,
    ConflictEdgeData,
    ConflictOut,
    DocumentNode,
    DocumentNodeData,
    EvidenceEdge,
    EvidenceEdgeData,
    FeatureNode,
    FeatureNodeData,
    GenomeGraphOut,
    GenomeProjectOut,
    GraphPosition,
    ScoreBreakdownOut,
    SynthesisOut,
    SynthesisValidationOut,
)

GRAPH_COLUMN_GAP: int = 360
INPUT_DIGEST_PREFIX_CHARS: int = 12
BRANCH_NODE_PREFIX: str = "branch:"
DOCUMENT_NODE_PREFIX: str = "document:"
CLAIM_SOURCE_TYPES: frozenset[str] = frozenset({EvidenceSourceType.README_CLAIM, EvidenceSourceType.ISSUE_SPEC})
VITAL_COLOR_TOKENS: Mapping[str, ColorToken] = {
    VitalStatus.VERIFIED_WORKING: "alive",
    VitalStatus.ABANDONED_IN_PROGRESS: "dormant",
    VitalStatus.GHOST_FEATURE: "ghost",
}
_SOURCE_TYPE_ORDER: Mapping[str, int] = {value: index for index, value in enumerate(EvidenceSourceType.values)}
_LOCATION_RE = re.compile(r"^(?P<path>[^;\n]+?):(?P<start>\d+)(?:-(?P<end>\d+))?(?=: |;|$)")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProjectInput:
    id: UUID
    name: str
    status: str
    status_detail: str
    default_branch: str

    @classmethod
    def from_model(cls, project: Any) -> ProjectInput:
        return cls(project.pk, project.name, str(project.status), project.status_detail or "", project.default_branch)


@dataclass(frozen=True)
class SynthesisInput:
    id: UUID | None
    mode: str
    model_id: str
    started_at: datetime
    finished_at: datetime
    catalog_size: int
    node_count: int
    conflict_count: int
    input_digest: str
    validation_report: Mapping[str, Any]

    @classmethod
    def from_model(cls, synthesis: Any) -> SynthesisInput:
        """A ``GenomeSynthesis`` (``raw_model_response`` is never read and may be deferred)."""
        return cls(
            id=synthesis.pk,
            mode=str(synthesis.mode),
            model_id=synthesis.model_id,
            started_at=synthesis.started_at,
            finished_at=synthesis.finished_at,
            catalog_size=synthesis.catalog_size,
            node_count=synthesis.node_count,
            conflict_count=synthesis.conflict_count,
            input_digest=synthesis.input_digest,
            validation_report=synthesis.validation_report or {},
        )

    @classmethod
    def from_result(cls, result: Any) -> SynthesisInput:
        """An in-memory ``apps.genome.agents.GenomeSynthesisResult`` (not persisted, so no id)."""
        return cls(
            id=None,
            mode=str(result.mode),
            model_id=result.model_id,
            started_at=result.started_at,
            finished_at=result.finished_at,
            catalog_size=result.catalog_size,
            node_count=len(result.features),
            conflict_count=result.conflict_count,
            input_digest=result.input_digest,
            validation_report=result.validation_report or {},
        )


@dataclass(frozen=True)
class BranchInput:
    name: str
    head_sha: str
    last_commit_date: datetime | None
    is_default: bool
    is_merged: bool
    is_abandoned: bool
    commits_ahead: int
    commits_behind: int
    dormancy_days: float

    @classmethod
    def from_row(cls, row: Any, default_branch: str) -> BranchInput:
        """A ``BranchSnapshot`` or a pipeline ``BranchRow`` (same fields)."""
        summary: Mapping[str, Any] = row.diff_summary or {}
        return cls(
            name=row.name,
            head_sha=row.head_sha,
            last_commit_date=row.last_commit_date,
            is_default=row.name == default_branch,
            is_merged=bool(summary.get("is_merged", False)),
            is_abandoned=bool(row.is_abandoned),
            commits_ahead=int(row.commits_ahead),
            commits_behind=int(row.commits_behind),
            dormancy_days=float(summary.get("dormancy_days") or 0),
        )


@dataclass(frozen=True)
class CitationInput:
    id: str
    source_type: str
    file_path: str
    commit_sha: str
    line_start: int | None
    line_end: int | None
    snippet: str
    weight: float
    branch_name: str | None  # the source artifact's branch; None when unknown
    artifact_id: int | None = None
    signal_index: int | None = None


@dataclass(frozen=True)
class ConflictInput:
    id: str
    claim_source: str
    reality_source: str
    conflict_description: str
    resolution_recommendation: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class FeatureInput:
    id: str
    title: str
    category: str
    vital_status: str
    epistemic_status: str
    confidence_score: float
    reconstructed_intent: str
    actual_implementation_state: str
    graph_position: Mapping[str, Any]
    score_breakdown: Mapping[str, Any]
    citations: tuple[CitationInput, ...] = ()
    conflicts: tuple[ConflictInput, ...] = ()

    @classmethod
    def from_model(cls, node: Any) -> FeatureInput:
        """An ``IntentNode`` with ``citations`` and ``conflicts`` prefetched (``node.citations.all()`` must not query).

        Citations should carry an ``artifact_branch_name`` annotation (the
        source ``ArtifactRecord.branch_name``); without it the builder falls
        back to matching ``commit_sha`` against branch heads.
        """
        feature_id = str(node.pk)
        rows = sorted(node.citations.all(), key=lambda row: row.pk)
        breakdown: Mapping[str, Any] = node.score_breakdown or {}
        signal_types = [entry.get("source_type") for entry in breakdown.get("citations") or ()]
        # persist_genome writes citations in signal order, so pk order is signal order when the types line up.
        aligned = signal_types == [str(row.source_type) for row in rows]
        citations = tuple(
            CitationInput(
                id=str(row.pk),
                source_type=str(row.source_type),
                file_path=row.file_path,
                commit_sha=row.commit_sha,
                line_start=row.line_start,
                line_end=row.line_end,
                snippet=row.snippet,
                weight=float(row.weight),
                branch_name=getattr(row, "artifact_branch_name", None),
                artifact_id=row.artifact_id,
                signal_index=index if aligned else None,
            )
            for index, row in enumerate(rows)
        )
        conflicts = tuple(
            ConflictInput(
                id=str(record.pk),
                claim_source=record.claim_source,
                reality_source=record.reality_source,
                conflict_description=record.conflict_description,
                resolution_recommendation=record.resolution_recommendation,
                created_at=record.created_at,
            )
            for record in sorted(node.conflicts.all(), key=lambda record: record.pk)
        )
        return cls(
            id=feature_id,
            title=node.title,
            category=str(node.category),
            vital_status=str(node.vital_status),
            epistemic_status=str(node.epistemic_status),
            confidence_score=float(node.confidence_score),
            reconstructed_intent=node.reconstructed_intent,
            actual_implementation_state=node.actual_implementation_state,
            graph_position=node.graph_position or {},
            score_breakdown=breakdown,
            citations=citations,
            conflicts=conflicts,
        )

    @classmethod
    def from_genome_feature(cls, feature: GenomeFeature) -> FeatureInput:
        """An in-memory reconciler feature; ids are derived from its key (``feature:<key>``)."""
        feature_id = f"feature:{feature.key}"
        return cls(
            id=feature_id,
            title=feature.title,
            category=feature.category.value,
            vital_status=feature.vital_status.value,
            epistemic_status=feature.epistemic_status.value,
            confidence_score=feature.confidence,
            reconstructed_intent=feature.reconstructed_intent,
            actual_implementation_state=feature.actual_implementation_state,
            graph_position=dict(feature.graph_position),
            score_breakdown=feature.score_breakdown(),
            citations=tuple(
                CitationInput(
                    id=f"{feature_id}#c{index}",
                    source_type=citation.item.source_type.value,
                    file_path=citation.item.file_path,
                    commit_sha=citation.item.commit_sha,
                    line_start=citation.item.line_start,
                    line_end=citation.item.line_end,
                    snippet=citation.item.snippet,
                    weight=citation.weight,
                    branch_name=citation.item.branch_name,
                    artifact_id=citation.item.artifact_pk,
                    signal_index=index,
                )
                for index, citation in enumerate(feature.citations)
            ),
            conflicts=tuple(
                ConflictInput(
                    id=f"{feature_id}#k{index}",
                    claim_source=conflict.claim_source,
                    reality_source=conflict.reality_source,
                    conflict_description=conflict.conflict_description,
                    resolution_recommendation=conflict.resolution_recommendation,
                )
                for index, conflict in enumerate(feature.conflicts)
            ),
        )


@dataclass(frozen=True)
class GenomeInputs:
    project: ProjectInput
    synthesis: SynthesisInput | None
    features: tuple[FeatureInput, ...]
    branches: tuple[BranchInput, ...] = field(default_factory=tuple)


def inputs_from_features(
    project: ProjectInput,
    features: Iterable[GenomeFeature],
    branch_rows: Iterable[Any],
    synthesis: SynthesisInput | None = None,
) -> GenomeInputs:
    """``GenomeInputs`` for an in-memory genome (reconciler or agent output) and pipeline ``BranchRow``s."""
    return GenomeInputs(
        project=project,
        synthesis=synthesis,
        features=tuple(FeatureInput.from_genome_feature(feature) for feature in features),
        branches=tuple(BranchInput.from_row(row, project.default_branch) for row in branch_rows),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def color_token_for(vital_status: str) -> ColorToken:
    return VITAL_COLOR_TOKENS.get(vital_status, "neutral")


def location_of(file_path: str, line_start: int | None, line_end: int | None) -> str:
    """``path:start`` or ``path:start-end``, the format of ``CatalogItem.location`` and ``claim_source``."""
    if line_start is None:
        return file_path
    if line_end is None or line_end == line_start:
        return f"{file_path}:{line_start}"
    return f"{file_path}:{line_start}-{line_end}"


def _citation_sort_key(citation: CitationInput) -> tuple[Any, ...]:
    return (
        _SOURCE_TYPE_ORDER.get(citation.source_type, len(_SOURCE_TYPE_ORDER)),
        citation.file_path,
        citation.line_start is None,
        citation.line_start or 0,
        citation.line_end or 0,
        citation.signal_index if citation.signal_index is not None else 0,
        citation.id,
    )


def _position(feature: FeatureInput) -> tuple[float, float] | None:
    x, y = feature.graph_position.get("x"), feature.graph_position.get("y")
    if isinstance(x, int | float) and isinstance(y, int | float) and not isinstance(x, bool) and not isinstance(y, bool):
        return float(x), float(y)
    return None


def _feature_positions(features: Sequence[FeatureInput]) -> dict[str, tuple[float, float]]:
    """Stored positions; features without one go below their vital-status column (the reconciler's rule)."""
    positions: dict[str, tuple[float, float]] = {}
    column_bottom: dict[float, float] = defaultdict(lambda: -float(GRAPH_ROW_HEIGHT))
    for feature in features:
        stored = _position(feature)
        if stored is not None:
            positions[feature.id] = stored
            column_bottom[stored[0]] = max(column_bottom[stored[0]], stored[1])
    for feature in sorted(features, key=lambda f: (f.title.casefold(), f.id)):
        if feature.id in positions:
            continue
        x = float(GRAPH_COLUMN_X.get(VitalStatus(feature.vital_status), 0)) if feature.vital_status in VitalStatus.values else 0.0
        y = column_bottom[x] + GRAPH_ROW_HEIGHT
        column_bottom[x] = y
        positions[feature.id] = (x, y)
    return positions


def _stack(desired: Mapping[str, float], x: float) -> dict[str, tuple[float, float]]:
    """Place satellites at their desired heights, pushed down so no two are closer than a row."""
    placed: dict[str, tuple[float, float]] = {}
    next_free = -float("inf")
    for key, wanted in sorted(desired.items(), key=lambda entry: (entry[1], entry[0])):
        y = max(wanted, next_free)
        placed[key] = (x, round(y, 2))
        next_free = y + GRAPH_ROW_HEIGHT
    return placed


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _validation_summary(report: Mapping[str, Any]) -> SynthesisValidationOut:
    def count(key: str) -> int:
        value = report.get(key)
        return len(value) if isinstance(value, list | tuple) else 0

    reason = report.get("fallback_reason")
    return SynthesisValidationOut(
        dropped_citation_count=count("dropped_citation_ids"),
        unsupported_citation_count=count("unsupported_citation_ids"),
        rejected_feature_count=count("rejected_features"),
        dropped_conflict_count=count("dropped_conflicts"),
        vital_status_override_count=count("vital_status_overrides"),
        fallback_reason=str(reason) if reason else None,
    )


def _synthesis_out(synthesis: SynthesisInput | None) -> SynthesisOut | None:
    if synthesis is None:
        return None
    return SynthesisOut(
        id=synthesis.id,
        mode=synthesis.mode,
        model_id=synthesis.model_id,
        started_at=synthesis.started_at,
        finished_at=synthesis.finished_at,
        catalog_size=synthesis.catalog_size,
        node_count=synthesis.node_count,
        conflict_count=synthesis.conflict_count,
        input_digest_prefix=synthesis.input_digest[:INPUT_DIGEST_PREFIX_CHARS],
        validation=_validation_summary(synthesis.validation_report),
    )


@dataclass(frozen=True)
class _ClaimRef:
    file_path: str
    line_start: int | None
    line_end: int | None
    citation_id: str | None


def resolve_claim(conflict: ConflictInput, citations: Sequence[CitationInput], default_branch: str) -> _ClaimRef | None:
    """The contradicted claim of ``conflict`` (see the module docstring), or None."""
    source = conflict.claim_source
    for citation in sorted(citations, key=_citation_sort_key):
        if citation.source_type not in CLAIM_SOURCE_TYPES or citation.line_start is None:
            continue
        if citation.branch_name not in (None, default_branch):
            continue
        location = location_of(citation.file_path, citation.line_start, citation.line_end)
        if source == location or source.startswith(f"{location}: "):
            return _ClaimRef(citation.file_path, citation.line_start, citation.line_end, citation.id)
    match = _LOCATION_RE.match(source)
    if match is None:
        return None
    start = int(match["start"])
    end = int(match["end"]) if match["end"] else start
    return _ClaimRef(match["path"].strip(), start, end, None)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
def build_genome_graph(
    project: ProjectInput,
    synthesis: SynthesisInput | None,
    features: Sequence[FeatureInput],
    branches: Sequence[BranchInput],
) -> GenomeGraphOut:
    """The ``GET /api/projects/{project_id}/genome`` body (see the module docstring)."""
    default = project.default_branch
    branch_by_name = {branch.name: branch for branch in branches}
    head_to_branch: dict[str, str] = {}
    for branch in sorted(branches, key=lambda b: (b.name == default, b.name)):  # default last: it wins ties
        head_to_branch[branch.head_sha] = branch.name

    positions = _feature_positions(features)
    ordered = sorted(features, key=lambda f: (*positions[f.id], f.title.casefold(), f.id))
    xs = [positions[f.id][0] for f in ordered] or [0.0]
    ys = [positions[f.id][1] for f in ordered] or [0.0]

    # Citations, with each one's branch resolved.
    citations_out: dict[str, list[CitationOut]] = {}
    branch_citations: dict[str, dict[str, list[CitationOut]]] = defaultdict(lambda: defaultdict(list))
    for feature in ordered:
        rows: list[CitationOut] = []
        for citation in sorted(feature.citations, key=_citation_sort_key):
            branch_name = citation.branch_name or head_to_branch.get(citation.commit_sha)
            branch_only = branch_name is not None and branch_name != default and branch_name in branch_by_name
            row = CitationOut(
                id=citation.id,
                feature_id=feature.id,
                source_type=citation.source_type,
                file_path=citation.file_path,
                commit_sha=citation.commit_sha,
                line_start=citation.line_start,
                line_end=citation.line_end,
                snippet=citation.snippet,
                weight=citation.weight,
                branch_name=branch_name,
                is_branch_only=branch_only,
                artifact_id=citation.artifact_id,
                signal_index=citation.signal_index,
            )
            rows.append(row)
            if branch_only:
                branch_citations[branch_name][feature.id].append(row)  # type: ignore[index]
        citations_out[feature.id] = rows

    # Branch nodes: cited non-default branches plus every abandoned one.
    drawn = set(branch_citations) | {b.name for b in branches if b.is_abandoned and not b.is_default}
    desired_branch_y: dict[str, float] = {}
    below = max(ys) + GRAPH_ROW_HEIGHT
    for name in drawn:
        citing = branch_citations.get(name, {})
        desired_branch_y[name] = _mean([positions[fid][1] for fid in citing]) if citing else below
    branch_positions = _stack(desired_branch_y, max(xs) + GRAPH_COLUMN_GAP)

    # Conflicts and their claim documents.
    conflicts_out: list[ConflictOut] = []
    conflict_edges: list[ConflictEdge] = []
    document_features: dict[str, list[str]] = defaultdict(list)
    document_conflicts: dict[str, int] = defaultdict(int)
    for feature in ordered:
        for conflict in feature.conflicts:
            claim = resolve_claim(conflict, feature.citations, default)
            document_id = f"{DOCUMENT_NODE_PREFIX}{claim.file_path}" if claim else None
            edge_id = f"conflict:{conflict.id}" if claim else None
            if claim is not None and document_id is not None and edge_id is not None:
                document_conflicts[claim.file_path] += 1
                if feature.id not in document_features[claim.file_path]:
                    document_features[claim.file_path].append(feature.id)
                conflict_edges.append(
                    ConflictEdge(
                        id=edge_id,
                        source=feature.id,
                        target=document_id,
                        type="conflict",
                        data=ConflictEdgeData(
                            conflict_id=conflict.id,
                            claim_source=conflict.claim_source,
                            file_path=claim.file_path,
                            line_start=claim.line_start,
                            line_end=claim.line_end,
                            claim_citation_id=claim.citation_id,
                            color_token="conflict",
                        ),
                    )
                )
            conflicts_out.append(
                ConflictOut(
                    id=conflict.id,
                    feature_id=feature.id,
                    claim_source=conflict.claim_source,
                    reality_source=conflict.reality_source,
                    conflict_description=conflict.conflict_description,
                    resolution_recommendation=conflict.resolution_recommendation,
                    created_at=conflict.created_at,
                    claim_citation_id=claim.citation_id if claim else None,
                    claim_file_path=claim.file_path if claim else None,
                    claim_line_start=claim.line_start if claim else None,
                    claim_line_end=claim.line_end if claim else None,
                    document_node_id=document_id,
                    edge_id=edge_id,
                )
            )
    document_positions = _stack(
        {path: _mean([positions[fid][1] for fid in fids]) for path, fids in document_features.items()},
        min(xs) - GRAPH_COLUMN_GAP,
    )

    # Nodes.
    feature_nodes = [
        FeatureNode(
            id=feature.id,
            type="feature",
            position=GraphPosition(x=positions[feature.id][0], y=positions[feature.id][1]),
            data=FeatureNodeData(
                feature_id=feature.id,
                feature_key=feature.score_breakdown.get("feature_key"),
                title=feature.title,
                category=feature.category,
                vital_status=feature.vital_status,
                epistemic_status=feature.epistemic_status,
                confidence_score=feature.confidence_score,
                reconstructed_intent=feature.reconstructed_intent,
                actual_implementation_state=feature.actual_implementation_state,
                citation_count=len(feature.citations),
                conflict_count=len(feature.conflicts),
                color_token=color_token_for(feature.vital_status),
            ),
        )
        for feature in ordered
    ]
    branch_nodes = []
    for name, (x, y) in sorted(branch_positions.items(), key=lambda entry: (entry[1][1], entry[0])):
        branch = branch_by_name[name]
        citing = branch_citations.get(name, {})
        branch_nodes.append(
            BranchNode(
                id=f"{BRANCH_NODE_PREFIX}{name}",
                type="branch",
                position=GraphPosition(x=x, y=y),
                data=BranchNodeData(
                    name=name,
                    head_sha=branch.head_sha,
                    last_commit_date=branch.last_commit_date,
                    is_abandoned=branch.is_abandoned,
                    is_merged=branch.is_merged,
                    commits_ahead=branch.commits_ahead,
                    commits_behind=branch.commits_behind,
                    dormancy_days=branch.dormancy_days,
                    citation_count=sum(len(rows) for rows in citing.values()),
                    feature_ids=[f.id for f in ordered if f.id in citing],
                    color_token="dormant" if branch.is_abandoned else "neutral",
                ),
            )
        )
    document_nodes = [
        DocumentNode(
            id=f"{DOCUMENT_NODE_PREFIX}{path}",
            type="document",
            position=GraphPosition(x=x, y=y),
            data=DocumentNodeData(
                file_path=path,
                conflict_count=document_conflicts[path],
                feature_ids=list(document_features[path]),
                color_token="conflict",
            ),
        )
        for path, (x, y) in sorted(document_positions.items(), key=lambda entry: (entry[1][1], entry[0]))
    ]

    # Evidence edges, in feature order then branch name.
    evidence_edges = []
    for feature in ordered:
        for name in sorted(n for n, citing in branch_citations.items() if feature.id in citing):
            rows = branch_citations[name][feature.id]
            target = f"{BRANCH_NODE_PREFIX}{name}"
            evidence_edges.append(
                EvidenceEdge(
                    id=f"evidence:{feature.id}->{target}",
                    source=feature.id,
                    target=target,
                    type="evidence",
                    data=EvidenceEdgeData(
                        citation_count=len(rows),
                        source_types=sorted({row.source_type for row in rows}, key=lambda t: _SOURCE_TYPE_ORDER[t]),
                        color_token="dormant" if branch_by_name[name].is_abandoned else "neutral",
                    ),
                )
            )

    score_breakdowns = {
        feature.id: ScoreBreakdownOut.model_validate(
            {
                "confidence": feature.confidence_score,
                "epistemic_status": feature.epistemic_status,
                "vital_status": feature.vital_status,
                **feature.score_breakdown,
            }
        )
        for feature in ordered
    }
    branches_out = [
        BranchSummaryOut(
            name=branch.name,
            head_sha=branch.head_sha,
            last_commit_date=branch.last_commit_date,
            is_default=branch.is_default,
            is_merged=branch.is_merged,
            is_abandoned=branch.is_abandoned,
            commits_ahead=branch.commits_ahead,
            commits_behind=branch.commits_behind,
            dormancy_days=branch.dormancy_days,
            node_id=f"{BRANCH_NODE_PREFIX}{branch.name}" if branch.name in branch_positions else None,
        )
        for branch in sorted(branches, key=lambda b: (not b.is_default, b.name))
    ]
    return GenomeGraphOut(
        project=GenomeProjectOut(
            id=project.id,
            name=project.name,
            status=project.status,
            status_detail=project.status_detail,
            default_branch=project.default_branch,
        ),
        synthesis=_synthesis_out(synthesis),
        nodes=[*feature_nodes, *branch_nodes, *document_nodes],
        edges=[*evidence_edges, *conflict_edges],
        citations=citations_out,
        conflicts=conflicts_out,
        score_breakdowns=score_breakdowns,
        branches=branches_out,
    )


def build_genome_graph_from_inputs(inputs: GenomeInputs) -> GenomeGraphOut:
    return build_genome_graph(inputs.project, inputs.synthesis, inputs.features, inputs.branches)
