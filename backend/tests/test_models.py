"""ORM model and migration tests for the seven Lazarus Foundry apps.

Schema, choices, constraint and migration-graph tests read Django metadata and
migration files only, so they run without PostgreSQL. Tests marked
``django_db`` exercise real constraints and are skipped by conftest.py when
PostgreSQL is unreachable.
"""
from datetime import datetime, timezone

import pytest
from django.apps import apps
from django.db import IntegrityError, models, transaction
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddField, CreateModel
from django.db.migrations.state import ProjectState
from pgvector.django import VectorExtension, VectorField

from apps.foundry.models import AgentStage, FoundryRun, SandboxMode
from apps.genome.models import (
    ConflictRecord,
    EpistemicStatus,
    EvidenceCitation,
    EvidenceSourceType,
    IntentCategory,
    IntentNode,
    VitalStatus,
)
from apps.ingestion.models import ArtifactRecord, ArtifactType, BranchSnapshot
from apps.projects.models import Project, ProjectStatus
from apps.rebirth.models import RebirthArtifact
from apps.revival.models import RevivalStrategy, StrategyType

APP_LABELS: tuple[str, ...] = (
    "projects",
    "ingestion",
    "genome",
    "revival",
    "foundry",
    "rebirth",
    "telemetry",
)
PGVECTOR_MIGRATION: tuple[str, str] = ("ingestion", "0001_enable_pgvector")
INGESTION_MODELS_MIGRATION: tuple[str, str] = ("ingestion", "0002_branchsnapshot_artifactrecord")


# ---------------------------------------------------------------------------
# App registry
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label", APP_LABELS)
def test_app_is_installed_under_apps_package(label: str) -> None:
    config = apps.get_app_config(label)
    assert config.name == f"apps.{label}"


def test_telemetry_has_no_models() -> None:
    assert list(apps.get_app_config("telemetry").get_models()) == []


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------
FIELD_SPECS: list[tuple[type[models.Model], str, type[models.Field], bool]] = [
    # (model, field name, field class, null)
    (Project, "id", models.UUIDField, False),
    (Project, "name", models.CharField, False),
    (Project, "repo_url", models.CharField, False),
    (Project, "is_golden_demo", models.BooleanField, False),
    (Project, "workspace_path", models.CharField, False),
    (Project, "status", models.CharField, False),
    (Project, "default_branch", models.CharField, False),
    (Project, "status_detail", models.TextField, False),
    (Project, "ingested_at", models.DateTimeField, True),
    (Project, "ingestion_summary", models.JSONField, False),
    (Project, "created_at", models.DateTimeField, False),
    (Project, "updated_at", models.DateTimeField, False),
    (BranchSnapshot, "project", models.ForeignKey, False),
    (BranchSnapshot, "name", models.CharField, False),
    (BranchSnapshot, "head_sha", models.CharField, False),
    (BranchSnapshot, "last_commit_date", models.DateTimeField, False),
    (BranchSnapshot, "is_abandoned", models.BooleanField, False),
    (BranchSnapshot, "commits_ahead", models.IntegerField, False),
    (BranchSnapshot, "commits_behind", models.IntegerField, False),
    (BranchSnapshot, "diff_summary", models.JSONField, False),
    (ArtifactRecord, "project", models.ForeignKey, False),
    (ArtifactRecord, "artifact_type", models.CharField, False),
    (ArtifactRecord, "file_path", models.CharField, False),
    (ArtifactRecord, "branch_name", models.CharField, False),
    (ArtifactRecord, "raw_content", models.TextField, False),
    (ArtifactRecord, "structured_metadata", models.JSONField, False),
    (ArtifactRecord, "embedding", VectorField, True),
    (IntentNode, "id", models.UUIDField, False),
    (IntentNode, "project", models.ForeignKey, False),
    (IntentNode, "title", models.CharField, False),
    (IntentNode, "category", models.CharField, False),
    (IntentNode, "vital_status", models.CharField, False),
    (IntentNode, "epistemic_status", models.CharField, False),
    (IntentNode, "confidence_score", models.FloatField, False),
    (IntentNode, "reconstructed_intent", models.TextField, False),
    (IntentNode, "actual_implementation_state", models.TextField, False),
    (IntentNode, "graph_position", models.JSONField, False),
    (EvidenceCitation, "intent_node", models.ForeignKey, False),
    (EvidenceCitation, "artifact", models.ForeignKey, True),
    (EvidenceCitation, "source_type", models.CharField, False),
    (EvidenceCitation, "file_path", models.CharField, False),
    (EvidenceCitation, "commit_sha", models.CharField, False),
    (EvidenceCitation, "line_start", models.IntegerField, True),
    (EvidenceCitation, "line_end", models.IntegerField, True),
    (EvidenceCitation, "snippet", models.TextField, False),
    (EvidenceCitation, "weight", models.FloatField, False),
    (ConflictRecord, "project", models.ForeignKey, False),
    (ConflictRecord, "intent_node", models.ForeignKey, False),
    (ConflictRecord, "claim_source", models.TextField, False),
    (ConflictRecord, "reality_source", models.TextField, False),
    (ConflictRecord, "conflict_description", models.TextField, False),
    (ConflictRecord, "resolution_recommendation", models.TextField, False),
    (RevivalStrategy, "id", models.UUIDField, False),
    (RevivalStrategy, "project", models.ForeignKey, False),
    (RevivalStrategy, "strategy_type", models.CharField, False),
    (RevivalStrategy, "title", models.CharField, False),
    (RevivalStrategy, "summary", models.TextField, False),
    (RevivalStrategy, "feasibility_score", models.FloatField, False),
    (RevivalStrategy, "value_score", models.FloatField, False),
    (RevivalStrategy, "debt_cost_score", models.FloatField, False),
    (RevivalStrategy, "risk_score", models.FloatField, False),
    (RevivalStrategy, "action_plan", models.JSONField, False),
    (RevivalStrategy, "generated_kiro_specs", models.JSONField, False),
    (RevivalStrategy, "approved_by_human", models.BooleanField, False),
    (RevivalStrategy, "approved_at", models.DateTimeField, True),
    (FoundryRun, "id", models.UUIDField, False),
    (FoundryRun, "project", models.ForeignKey, False),
    (FoundryRun, "strategy", models.ForeignKey, False),
    (FoundryRun, "agent_stage", models.CharField, False),
    (FoundryRun, "sandbox_mode", models.CharField, False),
    (FoundryRun, "before_test_output", models.TextField, False),
    (FoundryRun, "after_test_output", models.TextField, False),
    (FoundryRun, "tests_passed", models.BooleanField, False),
    (FoundryRun, "iterations_used", models.IntegerField, False),
    (RebirthArtifact, "id", models.UUIDField, False),
    (RebirthArtifact, "project", models.OneToOneField, False),
    (RebirthArtifact, "foundry_run", models.ForeignKey, False),
    (RebirthArtifact, "unified_diff_patch", models.TextField, False),
    (RebirthArtifact, "pr_title", models.CharField, False),
    (RebirthArtifact, "pr_body_markdown", models.TextField, False),
    (RebirthArtifact, "exported_kiro_specs_path", models.CharField, False),
    (RebirthArtifact, "continuity_memory", models.JSONField, False),
    (RebirthArtifact, "preview_url", models.CharField, False),
    (RebirthArtifact, "created_at", models.DateTimeField, False),
]


@pytest.mark.parametrize(
    ("model", "field_name", "field_class", "null"),
    FIELD_SPECS,
    ids=[f"{m.__name__}.{f}" for m, f, _, _ in FIELD_SPECS],
)
def test_field_matches_design(
    model: type[models.Model], field_name: str, field_class: type[models.Field], null: bool
) -> None:
    field = model._meta.get_field(field_name)
    assert isinstance(field, field_class)
    assert field.null is null


@pytest.mark.parametrize(
    "model", [Project, IntentNode, RevivalStrategy, FoundryRun, RebirthArtifact]
)
def test_uuid_primary_keys(model: type[models.Model]) -> None:
    pk = model._meta.pk
    assert isinstance(pk, models.UUIDField)
    assert pk.name == "id"
    assert pk.has_default()


@pytest.mark.parametrize("removed", ["line_range", "quote_or_symbol"])
def test_evidence_citation_has_no_legacy_fields(removed: str) -> None:
    names = {f.name for f in EvidenceCitation._meta.get_fields()}
    assert removed not in names


def test_embedding_is_1536_dimensional_vector() -> None:
    field = ArtifactRecord._meta.get_field("embedding")
    assert isinstance(field, VectorField)
    assert field.dimensions == 1536
    assert field.blank is True


def test_rebirth_preview_url_default() -> None:
    assert RebirthArtifact._meta.get_field("preview_url").default == "http://localhost:8010"


@pytest.mark.parametrize(
    ("model", "field_name", "target", "related_name"),
    [
        (EvidenceCitation, "intent_node", IntentNode, "citations"),
        (EvidenceCitation, "artifact", ArtifactRecord, "citations"),
        (ConflictRecord, "intent_node", IntentNode, "conflicts"),
        (ConflictRecord, "project", Project, "conflicts"),
        (BranchSnapshot, "project", Project, "branches"),
        (ArtifactRecord, "project", Project, "artifacts"),
        (IntentNode, "project", Project, "intent_nodes"),
        (RevivalStrategy, "project", Project, "strategies"),
        (FoundryRun, "project", Project, "foundry_runs"),
        (FoundryRun, "strategy", RevivalStrategy, "foundry_runs"),
        (RebirthArtifact, "project", Project, "rebirth_artifact"),
        (RebirthArtifact, "foundry_run", FoundryRun, "rebirth_artifacts"),
    ],
)
def test_relations(
    model: type[models.Model], field_name: str, target: type[models.Model], related_name: str
) -> None:
    field = model._meta.get_field(field_name)
    assert field.related_model is target
    assert field.remote_field.related_name == related_name


# ---------------------------------------------------------------------------
# Choices (values copied from design.md / forensic-invariants.md)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("model", "field_name", "expected"),
    [
        (
            Project,
            "status",
            [
                "DORMANT",
                "INGESTING",
                "SYNTHESIZING_GENOME",
                "AUTOPSY_COMPLETE",
                "STRATEGY_APPROVED",
                "FOUNDRY_RUNNING",
                "REBORN",
                "FAILED",
            ],
        ),
        (
            ArtifactRecord,
            "artifact_type",
            ["AST_MODULE", "README_DOC", "DEPENDENCY_MANIFEST", "TODO_MARKER", "COMMIT_LOG"],
        ),
        (IntentNode, "category", ["CORE_FEATURE", "ARCHITECTURE", "INTEGRATION", "DATA_MODEL"]),
        (
            IntentNode,
            "vital_status",
            ["VERIFIED_WORKING", "ABANDONED_IN_PROGRESS", "GHOST_FEATURE"],
        ),
        (IntentNode, "epistemic_status", ["KNOWN", "INFERRED", "CONFLICTED", "MISSING_CONTEXT"]),
        (
            EvidenceCitation,
            "source_type",
            [
                "AST_IMPLEMENTATION",
                "UNMERGED_BRANCH_COMMIT",
                "ISSUE_SPEC",
                "TODO_COMMENT",
                "README_CLAIM",
            ],
        ),
        (RevivalStrategy, "strategy_type", ["RESTORE", "MODERNIZE", "EXTRACT"]),
        (
            FoundryRun,
            "agent_stage",
            ["ENVIRONMENT", "ARCHITECTURE", "ENGINEERING", "VERIFICATION", "COMPLETE", "FAILED"],
        ),
        (FoundryRun, "sandbox_mode", ["subprocess", "docker"]),
    ],
)
def test_choices_match_design(
    model: type[models.Model], field_name: str, expected: list[str]
) -> None:
    field = model._meta.get_field(field_name)
    assert [value for value, _ in field.choices] == expected


def test_sandbox_mode_choices_match_settings() -> None:
    from django.conf import settings

    assert tuple(SandboxMode.values) == settings.SANDBOX_MODES


def test_foundry_run_is_finished_property() -> None:
    assert FoundryRun(agent_stage=AgentStage.COMPLETE).is_finished
    assert FoundryRun(agent_stage=AgentStage.FAILED).is_finished
    assert not FoundryRun(agent_stage=AgentStage.VERIFICATION).is_finished


# ---------------------------------------------------------------------------
# Constraints declared in Meta
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("model", "constraint_name"),
    [
        (Project, "projects_project_status_valid"),
        (BranchSnapshot, "ingestion_branchsnapshot_unique_project_name"),
        (ArtifactRecord, "ingestion_artifactrecord_type_valid"),
        (IntentNode, "genome_intentnode_confidence_score_0_1"),
        (IntentNode, "genome_intentnode_category_valid"),
        (IntentNode, "genome_intentnode_vital_status_valid"),
        (IntentNode, "genome_intentnode_epistemic_status_valid"),
        (EvidenceCitation, "genome_evidencecitation_line_end_gte_line_start"),
        (EvidenceCitation, "genome_evidencecitation_source_type_valid"),
        (RevivalStrategy, "revival_revivalstrategy_unique_project_strategy_type"),
        (RevivalStrategy, "revival_revivalstrategy_feasibility_score_0_100"),
        (RevivalStrategy, "revival_revivalstrategy_value_score_0_1"),
        (RevivalStrategy, "revival_revivalstrategy_debt_cost_score_0_1"),
        (RevivalStrategy, "revival_revivalstrategy_risk_score_0_1"),
        (RevivalStrategy, "revival_revivalstrategy_approval_has_timestamp"),
        (RevivalStrategy, "revival_revivalstrategy_strategy_type_valid"),
        (FoundryRun, "foundry_foundryrun_agent_stage_valid"),
        (FoundryRun, "foundry_foundryrun_sandbox_mode_valid"),
    ],
)
def test_constraint_declared(model: type[models.Model], constraint_name: str) -> None:
    names = {c.name for c in model._meta.constraints}
    assert constraint_name in names


# ---------------------------------------------------------------------------
# Migrations (read from disk; no database connection)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def loader() -> MigrationLoader:
    return MigrationLoader(None, ignore_no_migrations=True)


def test_enable_pgvector_migration_has_exactly_one_vector_extension(
    loader: MigrationLoader,
) -> None:
    migration = loader.disk_migrations[PGVECTOR_MIGRATION]
    assert len(migration.operations) == 1
    assert isinstance(migration.operations[0], VectorExtension)
    assert migration.dependencies == []


def test_ingestion_models_migration_depends_on_pgvector(loader: MigrationLoader) -> None:
    migration = loader.disk_migrations[INGESTION_MODELS_MIGRATION]
    assert PGVECTOR_MIGRATION in migration.dependencies


def _adds_vector_field(operation: object) -> bool:
    if isinstance(operation, CreateModel):
        return any(isinstance(field, VectorField) for _, field in operation.fields)
    if isinstance(operation, AddField):
        return isinstance(operation.field, VectorField)
    return False


def test_every_vector_field_migration_runs_after_pgvector(loader: MigrationLoader) -> None:
    vector_migrations = [
        key
        for key, migration in loader.disk_migrations.items()
        if any(_adds_vector_field(op) for op in migration.operations)
    ]
    assert INGESTION_MODELS_MIGRATION in vector_migrations
    for key in vector_migrations:
        # forwards_plan lists every migration that must be applied before `key`.
        assert PGVECTOR_MIGRATION in loader.graph.forwards_plan(key)[:-1], key


def test_migrations_cover_all_model_changes(loader: MigrationLoader) -> None:
    """Equivalent of ``makemigrations --check`` without touching the database."""
    autodetector = MigrationAutodetector(
        loader.project_state(), ProjectState.from_apps(apps)
    )
    changes = autodetector.changes(graph=loader.graph)
    assert changes == {}, f"Missing migrations for: {sorted(changes)}"


# ---------------------------------------------------------------------------
# Database-backed tests (skipped when PostgreSQL is unreachable)
# ---------------------------------------------------------------------------
COMMIT_DATE: datetime = datetime(2021, 6, 1, tzinfo=timezone.utc)


def make_project(name: str = "paypulse") -> Project:
    return Project.objects.create(name=name, repo_url="/tmp/lazarus_fixtures/paypulse_dead_repo")


def make_node(project: Project, confidence: float = 0.8) -> IntentNode:
    return IntentNode.objects.create(
        project=project,
        title="Exponential backoff webhook retries",
        category=IntentCategory.CORE_FEATURE,
        vital_status=VitalStatus.ABANDONED_IN_PROGRESS,
        epistemic_status=EpistemicStatus.INFERRED,
        confidence_score=confidence,
        reconstructed_intent="Retry failed webhooks with exponential backoff and jitter.",
        actual_implementation_state="80% implemented on feat/webhook-retry-engine.",
    )


def make_citation(
    node: IntentNode, line_start: int | None = 10, line_end: int | None = 24
) -> EvidenceCitation:
    return EvidenceCitation.objects.create(
        intent_node=node,
        source_type=EvidenceSourceType.UNMERGED_BRANCH_COMMIT,
        file_path="paypulse/webhooks/retry.py",
        commit_sha="a" * 40,
        line_start=line_start,
        line_end=line_end,
        snippet="def schedule_retry(attempt: int) -> float:",
        weight=0.9,
    )


def make_strategy(
    project: Project,
    strategy_type: str = StrategyType.RESTORE,
    feasibility: float = 82.5,
) -> RevivalStrategy:
    return RevivalStrategy.objects.create(
        project=project,
        strategy_type=strategy_type,
        title="Restore the retry engine",
        summary="Merge feat/webhook-retry-engine and finish jitter.",
        feasibility_score=feasibility,
        value_score=0.8,
        debt_cost_score=0.2,
        risk_score=0.25,
    )


def make_run(project: Project, strategy: RevivalStrategy) -> FoundryRun:
    return FoundryRun.objects.create(
        project=project, strategy=strategy, sandbox_mode=SandboxMode.SUBPROCESS
    )


def make_rebirth(project: Project, run: FoundryRun) -> RebirthArtifact:
    return RebirthArtifact.objects.create(
        project=project,
        foundry_run=run,
        unified_diff_patch="--- a/x\n+++ b/x\n",
        pr_title="Resurrect webhook retries",
        pr_body_markdown="## Summary",
    )


@pytest.mark.django_db
def test_full_object_graph_and_related_names() -> None:
    project = make_project()
    branch = BranchSnapshot.objects.create(
        project=project, name="feat/webhook-retry-engine", head_sha="b" * 40,
        last_commit_date=COMMIT_DATE, is_abandoned=True, commits_ahead=7,
    )
    artifact = ArtifactRecord.objects.create(
        project=project, artifact_type=ArtifactType.AST_MODULE,
        file_path="paypulse/webhooks/retry.py", branch_name=branch.name,
    )
    node = make_node(project)
    citation = make_citation(node)
    citation.artifact = artifact
    citation.save()
    conflict = ConflictRecord.objects.create(
        project=project, intent_node=node, claim_source="README.md:12",
        reality_source="paypulse/webhooks/dispatcher.py",
        conflict_description="README claims retries ship; main has no retry code.",
    )
    strategy = make_strategy(project)
    run = make_run(project, strategy)
    rebirth = make_rebirth(project, run)

    project.refresh_from_db()
    assert project.status == ProjectStatus.DORMANT
    assert list(project.branches.all()) == [branch]
    assert list(project.artifacts.all()) == [artifact]
    assert list(project.intent_nodes.all()) == [node]
    assert list(node.citations.all()) == [citation]
    assert list(artifact.citations.all()) == [citation]
    assert list(node.conflicts.all()) == [conflict]
    assert list(project.conflicts.all()) == [conflict]
    assert list(project.strategies.all()) == [strategy]
    assert list(strategy.foundry_runs.all()) == [run]
    assert project.rebirth_artifact == rebirth
    assert rebirth.preview_url == "http://localhost:8010"
    assert run.agent_stage == AgentStage.ENVIRONMENT


@pytest.mark.django_db
def test_embedding_round_trips_1536_floats() -> None:
    project = make_project()
    vector = [0.001 * i for i in range(1536)]
    artifact = ArtifactRecord.objects.create(
        project=project, artifact_type=ArtifactType.README_DOC,
        file_path="README.md", branch_name="main", embedding=vector,
    )
    artifact.refresh_from_db()
    assert len(artifact.embedding) == 1536
    assert artifact.embedding[1535] == pytest.approx(1.535, rel=1e-5)


@pytest.mark.django_db
def test_deleting_artifact_keeps_citation() -> None:
    project = make_project()
    artifact = ArtifactRecord.objects.create(
        project=project, artifact_type=ArtifactType.TODO_MARKER,
        file_path="paypulse/webhooks/retry.py", branch_name="main",
    )
    citation = make_citation(make_node(project))
    citation.artifact = artifact
    citation.save()
    artifact.delete()
    citation.refresh_from_db()
    assert citation.artifact is None


@pytest.mark.django_db
@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidence_score_outside_unit_interval_is_rejected(confidence: float) -> None:
    project = make_project()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_node(project, confidence=confidence)


@pytest.mark.django_db
@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_confidence_score_bounds_are_inclusive(confidence: float) -> None:
    assert make_node(make_project(), confidence=confidence).pk is not None


@pytest.mark.django_db
def test_line_end_before_line_start_is_rejected() -> None:
    node = make_node(make_project())
    with pytest.raises(IntegrityError), transaction.atomic():
        make_citation(node, line_start=20, line_end=19)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("line_start", "line_end"), [(5, 5), (None, None), (5, None), (None, 5)]
)
def test_valid_or_partial_line_ranges_are_accepted(
    line_start: int | None, line_end: int | None
) -> None:
    node = make_node(make_project())
    assert make_citation(node, line_start=line_start, line_end=line_end).pk is not None


@pytest.mark.django_db
@pytest.mark.parametrize("feasibility", [-0.1, 100.1])
def test_feasibility_score_outside_0_100_is_rejected(feasibility: float) -> None:
    project = make_project()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_strategy(project, feasibility=feasibility)


@pytest.mark.django_db
def test_strategy_type_is_unique_per_project() -> None:
    project = make_project()
    make_strategy(project, StrategyType.RESTORE)
    make_strategy(project, StrategyType.MODERNIZE)
    make_strategy(make_project("other"), StrategyType.RESTORE)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_strategy(project, StrategyType.RESTORE)


@pytest.mark.django_db
def test_approval_requires_timestamp() -> None:
    strategy = make_strategy(make_project())
    strategy.approved_by_human = True
    with pytest.raises(IntegrityError), transaction.atomic():
        strategy.save()
    strategy.approved_at = datetime.now(timezone.utc)
    strategy.save()


@pytest.mark.django_db
def test_branch_name_is_unique_per_project() -> None:
    project = make_project()
    BranchSnapshot.objects.create(
        project=project, name="main", head_sha="c" * 40, last_commit_date=COMMIT_DATE
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        BranchSnapshot.objects.create(
            project=project, name="main", head_sha="d" * 40, last_commit_date=COMMIT_DATE
        )


@pytest.mark.django_db
def test_negative_commit_counts_are_rejected() -> None:
    project = make_project()
    with pytest.raises(IntegrityError), transaction.atomic():
        BranchSnapshot.objects.create(
            project=project, name="main", head_sha="e" * 40,
            last_commit_date=COMMIT_DATE, commits_behind=-1,
        )


@pytest.mark.django_db
def test_project_has_at_most_one_rebirth_artifact() -> None:
    project = make_project()
    run = make_run(project, make_strategy(project))
    make_rebirth(project, run)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_rebirth(project, run)


@pytest.mark.django_db
def test_invalid_enum_value_is_rejected_by_database() -> None:
    project = make_project()
    with pytest.raises(IntegrityError), transaction.atomic():
        Project.objects.filter(pk=project.pk).update(status="UNDEAD")
