"""Write an ``IngestionResult`` to the database.

``persist_ingestion`` replaces the project's ``BranchSnapshot`` and
``ArtifactRecord`` rows in one transaction: the old rows are deleted and the
new ones bulk-created, so re-ingesting a project never duplicates rows and a
failure leaves the previous ingestion intact. Genome rows that cite old
artifacts (``EvidenceCitation.artifact``, ``on_delete=SET_NULL``) keep their
copied file path, lines and snippet.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.ingestion.models import ArtifactRecord, BranchSnapshot
from apps.ingestion.pipeline import IngestionResult
from apps.projects.models import Project

BULK_CREATE_BATCH_SIZE: int = 500


@dataclass(frozen=True)
class PersistedCounts:
    branches: int
    artifacts: int


def persist_ingestion(project: Project, result: IngestionResult) -> PersistedCounts:
    """Replace ``project``'s ingestion rows with ``result`` and record the default branch and summary.

    Updates ``project`` in place (``default_branch``, ``ingestion_summary``,
    ``ingested_at``). The project status is not changed here; see
    ``apps.ingestion.tasks.on_ingestion_complete``.
    """
    with transaction.atomic():
        BranchSnapshot.objects.filter(project=project).delete()
        ArtifactRecord.objects.filter(project=project).delete()
        BranchSnapshot.objects.bulk_create(
            [
                BranchSnapshot(
                    project=project,
                    name=row.name,
                    head_sha=row.head_sha,
                    last_commit_date=row.last_commit_date,
                    is_abandoned=row.is_abandoned,
                    commits_ahead=row.commits_ahead,
                    commits_behind=row.commits_behind,
                    diff_summary=row.diff_summary,
                )
                for row in result.branches
            ],
            batch_size=BULK_CREATE_BATCH_SIZE,
        )
        ArtifactRecord.objects.bulk_create(
            [
                ArtifactRecord(
                    project=project,
                    artifact_type=row.artifact_type,
                    file_path=row.file_path,
                    branch_name=row.branch_name,
                    raw_content=row.raw_content,
                    structured_metadata=row.structured_metadata,
                    embedding=None,
                )
                for row in result.artifacts
            ],
            batch_size=BULK_CREATE_BATCH_SIZE,
        )
        project.default_branch = result.default_branch
        project.ingestion_summary = result.summary
        project.ingested_at = timezone.now()
        project.save(update_fields=["default_branch", "ingestion_summary", "ingested_at", "updated_at"])
    return PersistedCounts(branches=len(result.branches), artifacts=len(result.artifacts))
