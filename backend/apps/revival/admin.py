from django.contrib import admin

from apps.revival.models import RevivalStrategy, StrategySynthesis


@admin.register(RevivalStrategy)
class RevivalStrategyAdmin(admin.ModelAdmin):
    list_display: tuple[str, ...] = (
        "title",
        "strategy_type",
        "project",
        "feasibility_score",
        "value_score",
        "debt_cost_score",
        "risk_score",
        "is_actionable",
        "approved_by_human",
        "approved_at",
        "approved_by",
    )
    list_filter: tuple[str, ...] = ("strategy_type", "approved_by_human", "is_actionable", "project")
    list_select_related: tuple[str, ...] = ("project",)
    search_fields: tuple[str, ...] = ("title", "summary")
    # Approval goes through the API gate (apps.revival.services.approve_strategy), which records the token.
    readonly_fields: tuple[str, ...] = ("id", "synthesis", "approved_token", "approved_by", "created_at", "updated_at")


@admin.register(StrategySynthesis)
class StrategySynthesisAdmin(admin.ModelAdmin):
    list_display: tuple[str, ...] = ("project", "mode", "model_id", "started_at", "finished_at")
    list_filter: tuple[str, ...] = ("mode", "project")
    list_select_related: tuple[str, ...] = ("project",)
    readonly_fields: tuple[str, ...] = (
        "id", "project", "genome_synthesis", "mode", "model_id", "started_at", "finished_at", "input_digest",
        "validation_report", "raw_model_response", "preserved_strategy_types", "created_at",
    )
