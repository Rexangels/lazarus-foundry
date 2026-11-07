"""The single rebirth bundle produced for a project once verification passes."""
import uuid

from django.db import models

DEFAULT_PREVIEW_URL: str = "http://localhost:8010"


class RebirthArtifact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.OneToOneField(
        "projects.Project", on_delete=models.CASCADE, related_name="rebirth_artifact"
    )
    foundry_run = models.ForeignKey(
        "foundry.FoundryRun", on_delete=models.CASCADE, related_name="rebirth_artifacts"
    )
    unified_diff_patch = models.TextField()
    pr_title = models.CharField(max_length=255)
    pr_body_markdown = models.TextField()
    exported_kiro_specs_path = models.CharField(max_length=1024, blank=True)
    continuity_memory = models.JSONField(default=dict, blank=True)
    preview_url = models.CharField(max_length=512, default=DEFAULT_PREVIEW_URL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Rebirth of {self.project_id}: {self.pr_title}"
