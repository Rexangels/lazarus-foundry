"""Revival strategies, their bounded feasibility scores (forensic-invariants.md section 4) and their provenance."""
import uuid

from django.db import models

from apps.genome.models import SynthesisMode


class StrategyType(models.TextChoices):
    RESTORE = "RESTORE", "Restore"
    MODERNIZE = "MODERNIZE", "Modernize"
    EXTRACT = "EXTRACT", "Extract"


def _unit_interval(field: str) -> models.Q:
    """``0.0 <= field <= 1.0``."""
    return models.Q(**{f"{field}__gte": 0.0, f"{field}__lte": 1.0})


class StrategySynthesis(models.Model):
    """Provenance of one strategy-generation run (apps.revival.services / agents).

    ``mode`` says how the spec bundles' prose was written: ``DETERMINISTIC``
    (no model key), ``MODEL`` (model prose validated and applied) or
    ``MODEL_FALLBACK`` (the model failed; ``validation_report`` has the
    reason). Plans, scores and the bundles' structure are always
    deterministic. Never stores the API key or request headers:
    ``input_digest`` is the SHA-256 of the request payload and
    ``raw_model_response`` the reply text, capped.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="strategy_syntheses"
    )
    # The genome the plans were built from; SET_NULL keeps provenance if old genome provenance is purged.
    genome_synthesis = models.ForeignKey(
        "genome.GenomeSynthesis", on_delete=models.SET_NULL, null=True, blank=True, related_name="strategy_syntheses"
    )
    mode = models.CharField(max_length=32, choices=SynthesisMode.choices)
    model_id = models.CharField(max_length=255, blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()
    input_digest = models.CharField(max_length=64)
    # Applied and dropped prose per bundle, fallback reason, preserved approved strategies.
    validation_report = models.JSONField(default=dict, blank=True)
    raw_model_response = models.TextField(blank=True)
    # Strategy types whose approved row was kept unchanged instead of regenerated.
    preserved_strategy_types = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name_plural = "strategy syntheses"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(mode__in=SynthesisMode.values),
                name="revival_strategysynthesis_mode_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.mode} strategies for {self.project_id} at {self.started_at:%Y-%m-%d %H:%M}"


class RevivalStrategy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="strategies"
    )
    # The run that produced this row; SET_NULL keeps the strategy if old provenance is purged.
    synthesis = models.ForeignKey(
        StrategySynthesis, on_delete=models.SET_NULL, null=True, blank=True, related_name="strategies"
    )
    strategy_type = models.CharField(max_length=16, choices=StrategyType.choices)
    title = models.CharField(max_length=255)
    summary = models.TextField()
    # Score(S_k) in [0, 100]; V, D and R inputs in [0, 1].
    feasibility_score = models.FloatField()
    value_score = models.FloatField()
    debt_cost_score = models.FloatField()
    risk_score = models.FloatField()
    # apps.revival.scoring.PlanScore.as_dict(): weights, terms, V/D/R derivations, parameters.
    score_breakdown = models.JSONField(default=dict, blank=True)
    # {"items": [PlanItem...], "in_scope", "deferred", "files", "extract_root", "risk_evidence"}.
    action_plan = models.JSONField(default=dict, blank=True)
    # False when the plan has no items (e.g. EXTRACT with nothing verified).
    is_actionable = models.BooleanField(default=False)
    # {"spec_name", "requirements_md", "design_md", "tasks_md", "evidence_index", "mode", "generated_at"}.
    generated_kiro_specs = models.JSONField(default=dict, blank=True)
    approved_by_human = models.BooleanField(default=False)
    approved_at = models.DateTimeField(null=True, blank=True)
    # apps.revival.approval.strategy_approval_token() of the content the human approved. Later stages
    # recompute it to prove the plan, scores and specs were not altered after approval.
    approved_token = models.CharField(max_length=64, blank=True)
    # The X-Lazarus-Approver header value (settings.REVIVAL_APPROVAL_REQUIRES_HEADER); blank when not sent.
    approved_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "revival strategies"
        ordering = ["-feasibility_score", "strategy_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "strategy_type"],
                name="revival_revivalstrategy_unique_project_strategy_type",
            ),
            # One approved strategy per project: the Foundry runs exactly one human-signed work order.
            models.UniqueConstraint(
                fields=["project"],
                condition=models.Q(approved_by_human=True),
                name="revival_revivalstrategy_one_approved_per_project",
            ),
            models.CheckConstraint(
                condition=models.Q(feasibility_score__gte=0.0, feasibility_score__lte=100.0),
                name="revival_revivalstrategy_feasibility_score_0_100",
            ),
            models.CheckConstraint(
                condition=_unit_interval("value_score"),
                name="revival_revivalstrategy_value_score_0_1",
            ),
            models.CheckConstraint(
                condition=_unit_interval("debt_cost_score"),
                name="revival_revivalstrategy_debt_cost_score_0_1",
            ),
            models.CheckConstraint(
                condition=_unit_interval("risk_score"),
                name="revival_revivalstrategy_risk_score_0_1",
            ),
            models.CheckConstraint(
                condition=models.Q(approved_by_human=False) | models.Q(approved_at__isnull=False),
                name="revival_revivalstrategy_approval_has_timestamp",
            ),
            models.CheckConstraint(
                condition=models.Q(strategy_type__in=StrategyType.values),
                name="revival_revivalstrategy_strategy_type_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.strategy_type}: {self.title} ({self.feasibility_score:.1f})"
