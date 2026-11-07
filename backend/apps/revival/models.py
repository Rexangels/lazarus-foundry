"""Revival strategies and their bounded feasibility scores (forensic-invariants.md section 4)."""
import uuid

from django.db import models


class StrategyType(models.TextChoices):
    RESTORE = "RESTORE", "Restore"
    MODERNIZE = "MODERNIZE", "Modernize"
    EXTRACT = "EXTRACT", "Extract"


def _unit_interval(field: str) -> models.Q:
    """``0.0 <= field <= 1.0``."""
    return models.Q(**{f"{field}__gte": 0.0, f"{field}__lte": 1.0})


class RevivalStrategy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="strategies"
    )
    strategy_type = models.CharField(max_length=16, choices=StrategyType.choices)
    title = models.CharField(max_length=255)
    summary = models.TextField()
    # Score(S_k) in [0, 100]; V, D and R inputs in [0, 1].
    feasibility_score = models.FloatField()
    value_score = models.FloatField()
    debt_cost_score = models.FloatField()
    risk_score = models.FloatField()
    action_plan = models.JSONField(default=list, blank=True)
    generated_kiro_specs = models.JSONField(default=dict, blank=True)
    approved_by_human = models.BooleanField(default=False)
    approved_at = models.DateTimeField(null=True, blank=True)
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
