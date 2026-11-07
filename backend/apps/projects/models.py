"""Root ``Project`` model that every other app hangs its records off."""
import uuid

from django.db import models


class ProjectStatus(models.TextChoices):
    DORMANT = "DORMANT", "Dormant"
    INGESTING = "INGESTING", "Ingesting"
    # Ingestion is committed and the Project Genome is being synthesized (apps.genome.tasks).
    SYNTHESIZING_GENOME = "SYNTHESIZING_GENOME", "Synthesizing genome"
    AUTOPSY_COMPLETE = "AUTOPSY_COMPLETE", "Autopsy complete"
    STRATEGY_APPROVED = "STRATEGY_APPROVED", "Strategy approved"
    FOUNDRY_RUNNING = "FOUNDRY_RUNNING", "Foundry running"
    REBORN = "REBORN", "Reborn"
    FAILED = "FAILED", "Failed"


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    # CharField rather than URLField: local paths and scp-style Git remotes
    # (git@host:org/repo.git) are valid sources but fail URL validation.
    repo_url = models.CharField(max_length=2048, blank=True)
    is_golden_demo = models.BooleanField(default=False)
    workspace_path = models.CharField(max_length=1024, blank=True)
    status = models.CharField(
        max_length=32, choices=ProjectStatus.choices, default=ProjectStatus.DORMANT
    )
    default_branch = models.CharField(max_length=255, default="main")
    # User-facing explanation of the current status (e.g. why ingestion FAILED).
    # Never holds tracebacks, Git stderr or server paths.
    status_detail = models.TextField(blank=True)
    # When the last successful ingestion was persisted.
    ingested_at = models.DateTimeField(null=True, blank=True)
    # Counts from the last successful ingestion (parse outcomes per scope,
    # artifacts per type, branches); see apps.ingestion.pipeline.
    ingestion_summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status"], name="projects_project_status_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=ProjectStatus.values),
                name="projects_project_status_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.status})"
