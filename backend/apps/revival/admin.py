from django.contrib import admin

from apps.revival.models import RevivalStrategy


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
        "approved_by_human",
        "approved_at",
    )
    list_filter: tuple[str, ...] = ("strategy_type", "approved_by_human", "project")
    list_select_related: tuple[str, ...] = ("project",)
    search_fields: tuple[str, ...] = ("title", "summary")
    readonly_fields: tuple[str, ...] = ("id", "created_at", "updated_at")
