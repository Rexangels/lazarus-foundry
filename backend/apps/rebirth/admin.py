from django.contrib import admin

from apps.rebirth.models import RebirthArtifact


@admin.register(RebirthArtifact)
class RebirthArtifactAdmin(admin.ModelAdmin):
    list_display: tuple[str, ...] = (
        "pr_title",
        "project",
        "foundry_run",
        "preview_url",
        "exported_kiro_specs_path",
        "created_at",
    )
    list_filter: tuple[str, ...] = ("created_at",)
    list_select_related: tuple[str, ...] = ("project", "foundry_run")
    search_fields: tuple[str, ...] = ("pr_title", "pr_body_markdown")
    raw_id_fields: tuple[str, ...] = ("foundry_run",)
    readonly_fields: tuple[str, ...] = ("id", "created_at")
