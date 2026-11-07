"""Ingestion pipeline: turn a cloned workspace into the rows Task 4 cites.

Pure: no ORM access, no Celery, no network. ``run_ingestion(workspace, emit=...)``
runs ``GitForensicService.analyze()``, parses the default branch's full tree
(``parse_revision``) and, for every unmerged branch (abandoned or not), only the
files that branch added or changed (``parse_branch_changes``). It returns an
``IngestionResult`` of plain ``BranchRow``/``ArtifactRow`` dataclasses whose JSON
payloads are already serialized by ``apps.ingestion.serialization.to_json``;
``apps.ingestion.persistence.persist_ingestion`` writes them.

Artifact granularity (one ``ArtifactRow`` per ...)
    ``AST_MODULE``
        code file per scope (Python, or another language whose grammar was
        unavailable; ``outcome.status`` says which). Metadata: the file's
        symbols, imports, routes and stubs as evidence records, the
        ``ParseOutcome``, the commit SHA and the fingerprints of its markers.
    ``README_DOC``
        Markdown file per scope (headings and claims).
    ``DEPENDENCY_MANIFEST``
        manifest per scope (dependency records).
    ``TODO_MARKER``
        ``TODO``/``FIXME``/``HACK``/``XXX`` marker in any file type, so each
        marker is its own citation target (metadata = the marker record plus
        the commit SHA).
    ``COMMIT_LOG``
        non-default branch: unique commits, merge-base, ahead/behind, dates
        and per-file diff stats; ``raw_content`` is the branch's combined
        unified diff.

    A scope is the default branch (full tree) or one unmerged branch (its
    diff files at the branch tip); ``branch_name`` names it. File rows keep
    the file text in ``raw_content``, capped at
    ``settings.INGEST_MAX_STORED_CONTENT_BYTES`` (``raw_content_truncated``
    in metadata). Skipped (vendored, too large, binary, unreadable) files and
    files without an extractor get no file row; their counts per status are in
    ``summary["file_status_counts"]``. ``embedding`` is left NULL (no
    embedding provider yet).

Telemetry (through ``emit``)
    ``BRANCH_DISCOVERED`` once per branch, default branch first, right after
    the Git analysis; then ``AST_PARSED`` in batches of
    ``AST_PARSED_BATCH_SIZE`` non-skipped files per scope while parsing runs
    (the last batch of a scope may be smaller; skipped files are only
    counted). ``INGESTION_STARTED``, ``INGESTION_COMPLETE`` and ``ERROR`` are
    sent by ``apps.ingestion.tasks``, which knows when rows are committed.
    Payloads never contain file content.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from django.conf import settings

from apps.ingestion.ast_parser import (
    SKIPPED_STATUSES,
    ASTParserService,
    FileInventory,
    InventoryScope,
    RevisionInventory,
    parse_branch_changes,
    parse_revision,
)
from apps.ingestion.doc_extractor import is_markdown_path, manifest_kind_for
from apps.ingestion.evidence import UTF8_BOM
from apps.ingestion.git_forensics import BranchReport, GitForensicService, RepositoryReport
from apps.ingestion.models import ArtifactType
from apps.ingestion.serialization import JsonValue, cap_text, to_json
from apps.telemetry.events import TelemetryEventType

AST_PARSED_BATCH_SIZE: int = 25
# BranchSnapshot.diff_summary lists at most this many commits (COMMIT_LOG keeps them all).
DIFF_SUMMARY_MAX_COMMITS: int = 50
METADATA_SCHEMA_VERSION: int = 1
SECONDS_PER_DAY: int = 86400

Emit = Callable[[TelemetryEventType, dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BranchRow:
    """One ``BranchSnapshot`` to write."""

    name: str
    head_sha: str
    last_commit_date: datetime
    is_abandoned: bool
    commits_ahead: int
    commits_behind: int
    diff_summary: dict[str, JsonValue]


@dataclass(frozen=True)
class ArtifactRow:
    """One ``ArtifactRecord`` to write (``embedding`` stays NULL)."""

    artifact_type: ArtifactType
    file_path: str
    branch_name: str
    raw_content: str
    structured_metadata: dict[str, JsonValue]


@dataclass(frozen=True)
class IngestionResult:
    default_branch: str
    branches: tuple[BranchRow, ...]
    artifacts: tuple[ArtifactRow, ...]
    summary: dict[str, JsonValue]

    def artifacts_of(self, artifact_type: ArtifactType, branch_name: str | None = None) -> tuple[ArtifactRow, ...]:
        return tuple(
            row
            for row in self.artifacts
            if row.artifact_type == artifact_type and (branch_name is None or row.branch_name == branch_name)
        )

    def branch(self, name: str) -> BranchRow:
        return next(row for row in self.branches if row.name == name)


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------
def _dormancy_days(branch: BranchReport) -> int:
    return int(branch.dormancy.total_seconds() // SECONDS_PER_DAY)


def branch_row(branch: BranchReport, report: RepositoryReport) -> BranchRow:
    listed = branch.unique_commits[:DIFF_SUMMARY_MAX_COMMITS]
    diff = branch.diff
    summary: dict[str, Any] = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "ref": branch.ref,
        "is_default": branch.is_default,
        "is_merged": branch.is_merged,
        "merge_base_sha": branch.merge_base_sha,
        "merge_base_date": branch.merge_base_date,
        "dormancy_days": _dormancy_days(branch),
        "files_changed": len(diff.files) if diff else 0,
        "additions": diff.total_additions if diff else 0,
        "deletions": diff.total_deletions if diff else 0,
        "unique_commit_count": branch.commits_ahead,
        "commits": [
            {
                "sha": commit.sha,
                "subject": commit.subject,
                "author_name": commit.author_name,
                "committed_at": commit.committed_at,
            }
            for commit in listed
        ],
        "commits_truncated": branch.unique_commits_truncated or len(branch.unique_commits) > len(listed),
    }
    if branch.is_default:
        summary["total_commit_count"] = report.default_branch.commit_count
        summary["first_commit_date"] = report.default_branch.first_commit_date
    return BranchRow(
        name=branch.name,
        head_sha=branch.head_sha,
        last_commit_date=branch.last_commit_date,
        is_abandoned=branch.is_abandoned,
        commits_ahead=branch.commits_ahead,
        commits_behind=branch.commits_behind,
        diff_summary=to_json(summary, path="diff_summary"),  # type: ignore[arg-type]
    )


def branch_discovered_payload(branch: BranchReport) -> dict[str, Any]:
    return {
        "name": branch.name,
        "head_sha": branch.head_sha,
        "last_commit_date": branch.last_commit_date.isoformat(),
        "is_default": branch.is_default,
        "is_merged": branch.is_merged,
        "is_abandoned": branch.is_abandoned,
        "commits_ahead": branch.commits_ahead,
        "commits_behind": branch.commits_behind,
    }


def commit_log_row(branch: BranchReport, max_stored_bytes: int) -> ArtifactRow:
    diff = branch.diff
    files = diff.files if diff else ()
    combined = "".join(item.patch if item.patch.endswith("\n") else f"{item.patch}\n" for item in files if item.patch)
    raw_content, truncated = cap_text(combined.encode("utf-8"), max_stored_bytes)
    metadata: dict[str, Any] = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "branch": branch.name,
        "ref": branch.ref,
        "head_sha": branch.head_sha,
        "last_commit_date": branch.last_commit_date,
        "is_merged": branch.is_merged,
        "is_abandoned": branch.is_abandoned,
        "commits_ahead": branch.commits_ahead,
        "commits_behind": branch.commits_behind,
        "merge_base_sha": branch.merge_base_sha,
        "merge_base_date": branch.merge_base_date,
        "dormancy_days": _dormancy_days(branch),
        "commits": branch.unique_commits,
        "commits_truncated": branch.unique_commits_truncated,
        "diff": {
            "base_sha": diff.base_sha if diff else None,
            "head_sha": diff.head_sha if diff else branch.head_sha,
            "files_changed": len(files),
            "additions": diff.total_additions if diff else 0,
            "deletions": diff.total_deletions if diff else 0,
            "files": [
                {
                    "path": item.path,
                    "old_path": item.old_path,
                    "change_type": item.change_type,
                    "additions": item.additions,
                    "deletions": item.deletions,
                    "is_binary": item.is_binary,
                    "patch_truncated": item.patch_truncated,
                    "patch_size_bytes": item.patch_size_bytes,
                }
                for item in files
            ],
        },
        "raw_content_truncated": truncated,
    }
    return ArtifactRow(
        artifact_type=ArtifactType.COMMIT_LOG,
        # A commit log belongs to a branch, not a file.
        file_path="",
        branch_name=branch.name,
        raw_content=raw_content,
        structured_metadata=to_json(metadata, path="commit_log"),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def file_artifact_type(inventory: FileInventory) -> ArtifactType | None:
    """The file-level artifact type for ``inventory``, or None when the file gets no file row."""
    if inventory.outcome.status in SKIPPED_STATUSES:
        return None
    path = inventory.file_path
    if is_markdown_path(path):
        return ArtifactType.README_DOC
    if manifest_kind_for(path) is not None:
        return ArtifactType.DEPENDENCY_MANIFEST
    if inventory.outcome.language is not None:
        return ArtifactType.AST_MODULE
    return None  # UNSUPPORTED_LANGUAGE: only its markers are kept


def _file_metadata(
    inventory: FileInventory, artifact_type: ArtifactType, revision: RevisionInventory, truncated: bool
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "commit_sha": revision.commit_sha,
        "scope": revision.scope,
        "outcome": inventory.outcome,
        "raw_content_truncated": truncated,
        "marker_fingerprints": [marker.fingerprint for marker in inventory.markers],
    }
    if artifact_type == ArtifactType.AST_MODULE:
        metadata.update(
            language=inventory.outcome.language,
            symbols=inventory.symbols,
            imports=inventory.imports,
            routes=inventory.routes,
            stubs=inventory.stubs,
        )
    elif artifact_type == ArtifactType.README_DOC:
        metadata.update(headings=inventory.headings, claims=inventory.claims)
    else:
        metadata.update(manifest=manifest_kind_for(inventory.file_path), dependencies=inventory.dependencies)
    return metadata


def file_rows(
    service: GitForensicService, revision: RevisionInventory, inventory: FileInventory, max_stored_bytes: int
) -> list[ArtifactRow]:
    """The file row (if any) and one TODO_MARKER row per marker for one parsed file."""
    rows: list[ArtifactRow] = []
    branch_name = revision.revision
    artifact_type = file_artifact_type(inventory)
    if artifact_type is not None:
        data = service.read_file_at(revision.commit_sha, inventory.file_path) or b""
        raw_content, truncated = cap_text(data.removeprefix(UTF8_BOM), max_stored_bytes)
        metadata = _file_metadata(inventory, artifact_type, revision, truncated)
        rows.append(
            ArtifactRow(
                artifact_type=artifact_type,
                file_path=inventory.file_path,
                branch_name=branch_name,
                raw_content=raw_content,
                structured_metadata=to_json(metadata, path=inventory.file_path),  # type: ignore[arg-type]
            )
        )
    for marker in inventory.markers:
        metadata = {
            **to_json(marker, path=f"{inventory.file_path}:marker"),  # type: ignore[dict-item]
            "schema_version": METADATA_SCHEMA_VERSION,
            "commit_sha": revision.commit_sha,
            "scope": revision.scope.value,
        }
        rows.append(
            ArtifactRow(
                artifact_type=ArtifactType.TODO_MARKER,
                file_path=inventory.file_path,
                branch_name=branch_name,
                raw_content=str(metadata["snippet"]),
                structured_metadata=metadata,
            )
        )
    return rows


class _ParseProgress:
    """Counts parse outcomes for one scope and emits ``AST_PARSED`` in batches while parsing runs."""

    def __init__(self, emit: Emit, branch_name: str, scope: InventoryScope, batch_size: int) -> None:
        self.emit = emit
        self.branch_name = branch_name
        self.scope = scope
        self.batch_size = batch_size
        self.status_counts: Counter[str] = Counter()
        self._files: list[dict[str, Any]] = []
        self._skipped = 0
        self._batches = 0
        self._processed = 0

    def on_file(self, inventory: FileInventory) -> None:
        status = inventory.outcome.status
        self.status_counts[status.value] += 1
        self._processed += 1
        if status in SKIPPED_STATUSES:
            self._skipped += 1
        else:
            self._files.append(
                {
                    "path": inventory.file_path,
                    "status": status.value,
                    "language": inventory.outcome.language,
                    "symbols": len(inventory.symbols),
                    "markers": len(inventory.markers),
                }
            )
        if len(self._files) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._files and not self._skipped:
            return
        self._batches += 1
        self.emit(
            TelemetryEventType.AST_PARSED,
            {
                "branch": self.branch_name,
                "scope": self.scope.value,
                "batch": self._batches,
                "files": self._files,
                "skipped": self._skipped,
                "files_processed": self._processed,
            },
        )
        self._files = []
        self._skipped = 0


def _ingest_scope(
    service: GitForensicService,
    parse: Callable[..., RevisionInventory],
    target: str | BranchReport,
    *,
    branch_name: str,
    scope: InventoryScope,
    parser: ASTParserService,
    emit: Emit,
    batch_size: int,
    max_stored_bytes: int,
) -> tuple[list[ArtifactRow], Mapping[str, int]]:
    progress = _ParseProgress(emit, branch_name, scope, batch_size)
    revision = parse(service, target, parser=parser, on_file=progress.on_file)
    progress.flush()
    rows: list[ArtifactRow] = []
    for inventory in revision.files:
        rows.extend(file_rows(service, revision, inventory, max_stored_bytes))
    return rows, dict(progress.status_counts)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_ingestion(
    workspace: Path,
    *,
    emit: Emit,
    parser: ASTParserService | None = None,
    batch_size: int = AST_PARSED_BATCH_SIZE,
    max_stored_bytes: int | None = None,
) -> IngestionResult:
    """Analyze and parse the Git work tree at ``workspace`` (see the module docstring)."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    max_bytes = max_stored_bytes if max_stored_bytes is not None else settings.INGEST_MAX_STORED_CONTENT_BYTES
    parser = parser or ASTParserService()
    with GitForensicService(workspace) as service:
        report = service.analyze()
        for branch in report.branches:
            emit(TelemetryEventType.BRANCH_DISCOVERED, branch_discovered_payload(branch))

        default = report.branch(report.default_branch.name)
        common = {"parser": parser, "emit": emit, "batch_size": batch_size, "max_stored_bytes": max_bytes}
        artifacts, default_counts = _ingest_scope(
            service, parse_revision, default.name, branch_name=default.name, scope=InventoryScope.FULL_TREE, **common
        )
        file_status_counts: dict[str, Mapping[str, int]] = {default.name: default_counts}
        for branch in report.branches:
            if branch.is_default:
                continue
            if not branch.is_merged:
                rows, counts = _ingest_scope(
                    service,
                    parse_branch_changes,
                    branch,
                    branch_name=branch.name,
                    scope=InventoryScope.BRANCH_DIFF,
                    **common,
                )
                artifacts.extend(rows)
                file_status_counts[branch.name] = counts
            artifacts.append(commit_log_row(branch, max_bytes))

    branches = tuple(branch_row(branch, report) for branch in report.branches)
    artifact_counts = Counter(row.artifact_type.value for row in artifacts)
    summary: dict[str, Any] = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "default_branch": report.default_branch.name,
        "default_branch_head_sha": report.default_branch.head_sha,
        "default_branch_commit_count": report.default_branch.commit_count,
        "first_commit_date": report.default_branch.first_commit_date,
        "last_commit_date": report.default_branch.last_commit_date,
        "latest_commit_date": report.latest_commit_date,
        "branch_count": len(report.branches),
        "unmerged_branch_count": len(report.unmerged_branches),
        "abandoned_branch_count": len(report.abandoned_branches),
        "abandoned_branches": [branch.name for branch in report.abandoned_branches],
        "file_status_counts": file_status_counts,
        "artifact_counts": {kind: artifact_counts.get(kind, 0) for kind in ArtifactType.values},
        "raw_content_truncated_count": sum(
            1 for row in artifacts if row.structured_metadata.get("raw_content_truncated") is True
        ),
    }
    return IngestionResult(
        default_branch=report.default_branch.name,
        branches=branches,
        artifacts=tuple(artifacts),
        summary=to_json(summary, path="summary"),  # type: ignore[arg-type]
    )


def completion_payload(summary: Mapping[str, Any]) -> dict[str, Any]:
    """The small ``INGESTION_COMPLETE`` payload derived from an ingestion summary."""
    totals: Counter[str] = Counter()
    for counts in (summary.get("file_status_counts") or {}).values():
        totals.update(counts)
    return {
        "default_branch": summary.get("default_branch"),
        "branch_count": summary.get("branch_count", 0),
        "unmerged_branch_count": summary.get("unmerged_branch_count", 0),
        "abandoned_branch_count": summary.get("abandoned_branch_count", 0),
        "artifact_counts": dict(summary.get("artifact_counts") or {}),
        "file_status_counts": dict(totals),
    }
