"""Raw forensic records extracted from a project's Git repository."""
from django.db import models
from pgvector.django import VectorField

EMBEDDING_DIMENSIONS: int = 1536


class ArtifactType(models.TextChoices):
    AST_MODULE = "AST_MODULE", "AST module"
    README_DOC = "README_DOC", "README / documentation"
    DEPENDENCY_MANIFEST = "DEPENDENCY_MANIFEST", "Dependency manifest"
    TODO_MARKER = "TODO_MARKER", "TODO / FIXME marker"
    COMMIT_LOG = "COMMIT_LOG", "Commit log"


class BranchSnapshot(models.Model):
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="branches"
    )
    name = models.CharField(max_length=255)
    # 64 chars covers both SHA-1 (40) and SHA-256 (64) object names.
    head_sha = models.CharField(max_length=64)
    last_commit_date = models.DateTimeField()
    is_abandoned = models.BooleanField(default=False)
    commits_ahead = models.PositiveIntegerField(default=0)
    commits_behind = models.PositiveIntegerField(default=0)
    diff_summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_commit_date", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "name"], name="ingestion_branchsnapshot_unique_project_name"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} @ {self.head_sha[:8]}"


class ArtifactRecord(models.Model):
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="artifacts"
    )
    artifact_type = models.CharField(max_length=32, choices=ArtifactType.choices)
    file_path = models.CharField(max_length=1024)
    branch_name = models.CharField(max_length=255)
    raw_content = models.TextField(blank=True)
    structured_metadata = models.JSONField(default=dict, blank=True)
    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["branch_name", "file_path", "id"]
        indexes = [
            # Django caps index names at 30 characters.
            models.Index(fields=["project", "artifact_type"], name="ingest_art_proj_type_idx"),
            models.Index(
                fields=["project", "branch_name", "file_path"], name="ingest_art_proj_path_idx"
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(artifact_type__in=ArtifactType.values),
                name="ingestion_artifactrecord_type_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.artifact_type} {self.branch_name}:{self.file_path}"
