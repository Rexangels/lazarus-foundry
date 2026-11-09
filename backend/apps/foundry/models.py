"""One execution of the 4-agent Foundry pipeline for an approved strategy."""
import uuid

from django.db import models


class AgentStage(models.TextChoices):
    ENVIRONMENT = "ENVIRONMENT", "Environment"
    ARCHITECTURE = "ARCHITECTURE", "Architecture"
    ENGINEERING = "ENGINEERING", "Engineering"
    VERIFICATION = "VERIFICATION", "Verification"
    COMPLETE = "COMPLETE", "Complete"
    FAILED = "FAILED", "Failed"


class SandboxMode(models.TextChoices):
    # Values match settings.SANDBOX_MODES.
    SUBPROCESS = "subprocess", "Subprocess"
    DOCKER = "docker", "Docker"


class FoundryRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="foundry_runs"
    )
    strategy = models.ForeignKey(
        "revival.RevivalStrategy", on_delete=models.CASCADE, related_name="foundry_runs"
    )
    agent_stage = models.CharField(
        max_length=16, choices=AgentStage.choices, default=AgentStage.ENVIRONMENT
    )
    sandbox_mode = models.CharField(max_length=16, choices=SandboxMode.choices)
    before_test_output = models.TextField(blank=True)
    after_test_output = models.TextField(blank=True)
    tests_passed = models.BooleanField(default=False)
    iterations_used = models.PositiveSmallIntegerField(default=0)
    # The agents' reports (apps.foundry.agents.FoundryResult.report): environment audit and installs,
    # the architecture blueprint, engineering item results, every verification attempt and repair,
    # the revival branch, changed files and, on failure, {stage, code, detail}.
    report = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "agent_stage"], name="foundry_run_proj_stage_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(agent_stage__in=AgentStage.values),
                name="foundry_foundryrun_agent_stage_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(sandbox_mode__in=SandboxMode.values),
                name="foundry_foundryrun_sandbox_mode_valid",
            ),
        ]

    @property
    def is_finished(self) -> bool:
        return self.agent_stage in (AgentStage.COMPLETE, AgentStage.FAILED)

    def __str__(self) -> str:
        return f"FoundryRun {self.id} [{self.agent_stage}]"
