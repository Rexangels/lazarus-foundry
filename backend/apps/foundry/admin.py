from django.contrib import admin

from apps.foundry.models import FoundryRun


@admin.register(FoundryRun)
class FoundryRunAdmin(admin.ModelAdmin):
    list_display: tuple[str, ...] = (
        "id",
        "project",
        "strategy",
        "agent_stage",
        "sandbox_mode",
        "tests_passed",
        "iterations_used",
        "created_at",
    )
    list_filter: tuple[str, ...] = ("agent_stage", "sandbox_mode", "tests_passed")
    list_select_related: tuple[str, ...] = ("project", "strategy")
    raw_id_fields: tuple[str, ...] = ("strategy",)
    readonly_fields: tuple[str, ...] = ("id", "created_at", "updated_at")
