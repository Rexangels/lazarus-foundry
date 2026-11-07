"""Database side of genome synthesis: load the evidence catalog, persist a genome, load it for the API.

``load_genome_inputs`` reads the latest persisted genome for
``GET /api/projects/{project_id}/genome`` (``apps.genome.api``).

``persist_genome`` replaces the project's ``IntentNode`` / ``EvidenceCitation`` /
``ConflictRecord`` rows in one transaction and records a ``GenomeSynthesis``
provenance row (previous provenance rows are kept as history; their nodes
are gone). It refuses, before writing anything, a genome with a node that has
no citation (forensic-invariants.md section 1).

``EvidenceCitation`` fields are copied from the catalog item (file, commit,
lines, snippet, ``artifact`` = the ``ArtifactRecord`` it came from). Its
``weight`` is the item's term in C(f) (``SignalTerm.term``: authority weight x
match strength x temporal decay x rank factor), i.e. exactly what the citation
contributed to the score before the per-type cap.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import F, Prefetch

from apps.genome.agents import GenomeSynthesisResult
from apps.genome.catalog import EvidenceCatalog, catalog_from_rows
from apps.genome.graph import BranchInput, FeatureInput, GenomeInputs, ProjectInput, SynthesisInput
from apps.genome.models import ConflictRecord, EvidenceCitation, GenomeSynthesis, IntentNode
from apps.genome.reconciler import GenomeFeature
from apps.genome.scoring import UncitedIntentNodeError
from apps.ingestion.models import ArtifactRecord, BranchSnapshot
from apps.projects.models import Project

BULK_CREATE_BATCH_SIZE: int = 500
CATALOG_FIELDS: tuple[str, ...] = (
    "id", "artifact_type", "file_path", "branch_name", "raw_content", "structured_metadata",
)


class GenomeError(Exception):
    """A synthesis failure whose message is safe to show as ``Project.status_detail``."""


def load_catalog(project: Project) -> EvidenceCatalog:
    """The evidence catalog of ``project``'s last ingestion."""
    branches = list(BranchSnapshot.objects.filter(project=project))
    if not branches:
        raise GenomeError("The project has no ingested branches; ingest the repository first.")
    artifacts = ArtifactRecord.objects.filter(project=project).only(*CATALOG_FIELDS).order_by("id")
    try:
        return catalog_from_rows(artifacts.iterator(), branches, project.default_branch)
    except ValueError as exc:
        raise GenomeError("The ingested branches do not identify a default branch.") from exc


@dataclass(frozen=True)
class PersistedGenome:
    synthesis: GenomeSynthesis
    nodes: tuple[tuple[IntentNode, GenomeFeature], ...]
    conflicts: tuple[tuple[ConflictRecord, IntentNode], ...]


def validate_genome(features: tuple[GenomeFeature, ...]) -> None:
    """Raise before any write if a feature would break a forensic invariant or a DB constraint."""
    for feature in features:
        if not feature.citations:
            raise UncitedIntentNodeError(
                f"feature {feature.title!r} has no EvidenceCitation; refusing to persist the genome"
            )
        if not 0.0 <= feature.confidence <= 1.0:
            raise ValueError(f"feature {feature.title!r} has confidence {feature.confidence} outside [0, 1]")
        for citation in feature.citations:
            start, end = citation.item.line_start, citation.item.line_end
            if start is not None and end is not None and end < start:
                raise ValueError(f"citation {citation.item.id} has line_end {end} < line_start {start}")


def persist_genome(project: Project, result: GenomeSynthesisResult) -> PersistedGenome:
    """Replace ``project``'s genome rows with ``result`` (see the module docstring)."""
    validate_genome(result.features)
    with transaction.atomic():
        ConflictRecord.objects.filter(project=project).delete()
        IntentNode.objects.filter(project=project).delete()
        synthesis = GenomeSynthesis.objects.create(
            project=project,
            mode=result.mode,
            model_id=result.model_id,
            started_at=result.started_at,
            finished_at=result.finished_at,
            catalog_size=result.catalog_size,
            input_digest=result.input_digest,
            validation_report=result.validation_report,
            raw_model_response=result.raw_response,
            node_count=len(result.features),
            conflict_count=result.conflict_count,
        )
        wanted = {c.item.artifact_pk for f in result.features for c in f.citations if c.item.artifact_pk is not None}
        existing = set(
            ArtifactRecord.objects.filter(project=project, pk__in=wanted).values_list("pk", flat=True)
        ) if wanted else set()

        pairs = [
            (
                IntentNode(
                    project=project,
                    synthesis=synthesis,
                    title=feature.title,
                    category=feature.category,
                    vital_status=feature.vital_status,
                    epistemic_status=feature.epistemic_status,
                    confidence_score=feature.confidence,
                    reconstructed_intent=feature.reconstructed_intent,
                    actual_implementation_state=feature.actual_implementation_state,
                    graph_position=dict(feature.graph_position),
                    score_breakdown=feature.score_breakdown(),
                ),
                feature,
            )
            for feature in result.features
        ]
        IntentNode.objects.bulk_create([node for node, _ in pairs], batch_size=BULK_CREATE_BATCH_SIZE)

        citations = [
            EvidenceCitation(
                intent_node=node,
                artifact_id=citation.item.artifact_pk if citation.item.artifact_pk in existing else None,
                source_type=citation.item.source_type,
                file_path=citation.item.file_path,
                commit_sha=citation.item.commit_sha,
                line_start=citation.item.line_start,
                line_end=citation.item.line_end,
                snippet=citation.item.snippet,
                weight=citation.weight,
            )
            for node, feature in pairs
            for citation in feature.citations
        ]
        EvidenceCitation.objects.bulk_create(citations, batch_size=BULK_CREATE_BATCH_SIZE)

        conflicts = [
            (
                ConflictRecord(
                    project=project,
                    intent_node=node,
                    claim_source=conflict.claim_source,
                    reality_source=conflict.reality_source,
                    conflict_description=conflict.conflict_description,
                    resolution_recommendation=conflict.resolution_recommendation,
                ),
                node,
            )
            for node, feature in pairs
            for conflict in feature.conflicts
        ]
        ConflictRecord.objects.bulk_create([record for record, _ in conflicts], batch_size=BULK_CREATE_BATCH_SIZE)
    return PersistedGenome(synthesis=synthesis, nodes=tuple(pairs), conflicts=tuple(conflicts))


SYNTHESIS_SUMMARY_FIELDS: tuple[str, ...] = (
    "id", "project", "mode", "model_id", "started_at", "finished_at", "catalog_size",
    "node_count", "conflict_count", "input_digest", "validation_report",
)


def load_genome_inputs(project: Project) -> GenomeInputs:
    """The latest persisted genome of ``project`` as ``build_genome_graph`` inputs, in a constant number of queries.

    Queries: the latest ``GenomeSynthesis`` (without ``raw_model_response``),
    its ``IntentNode`` rows, their citations (annotated with the source
    artifact's branch; ``raw_content`` and the embedding are never loaded),
    their conflicts, and the project's branches. When no provenance row exists,
    every project node is returned (``persist_genome`` replaces all of them on
    each run, so they are one genome).
    """
    synthesis = (
        GenomeSynthesis.objects.filter(project=project)
        .only(*SYNTHESIS_SUMMARY_FIELDS)
        .order_by("-created_at", "-started_at")
        .first()
    )
    nodes = IntentNode.objects.filter(project=project)
    nodes = nodes.filter(synthesis=synthesis) if synthesis is not None else nodes
    nodes = nodes.prefetch_related(
        Prefetch(
            "citations",
            queryset=EvidenceCitation.objects.annotate(artifact_branch_name=F("artifact__branch_name")).order_by("id"),
        ),
        Prefetch("conflicts", queryset=ConflictRecord.objects.order_by("id")),
    )
    features = tuple(FeatureInput.from_model(node) for node in nodes)
    branches = tuple(
        BranchInput.from_row(row, project.default_branch) for row in BranchSnapshot.objects.filter(project=project)
    )
    return GenomeInputs(
        project=ProjectInput.from_model(project),
        synthesis=SynthesisInput.from_model(synthesis) if synthesis is not None else None,
        features=features,
        branches=branches,
    )
