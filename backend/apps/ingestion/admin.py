from django.contrib import admin

from apps.ingestion.models import ArtifactRecord, BranchSnapshot


@admin.register(BranchSnapshot)
class BranchSnapshotAdmin(admin.ModelAdmin):
    list_display: tuple[str, ...] = (
        "name",
        "project",
        "head_sha",
        "last_commit_date",
        "is_abandoned",
        "commits_ahead",
        "commits_behind",
    )
    list_filter: tuple[str, ...] = ("is_abandoned", "project")
    list_select_related: tuple[str, ...] = ("project",)
    search_fields: tuple[str, ...] = ("name", "head_sha")
    readonly_fields: tuple[str, ...] = ("created_at", "updated_at")


@admin.register(ArtifactRecord)
class ArtifactRecordAdmin(admin.ModelAdmin):
    list_display: tuple[str, ...] = (
        "file_path",
        "artifact_type",
        "branch_name",
        "project",
        "has_embedding",
        "created_at",
    )
    list_filter: tuple[str, ...] = ("artifact_type", "branch_name", "project")
    list_select_related: tuple[str, ...] = ("project",)
    search_fields: tuple[str, ...] = ("file_path", "branch_name")
    # A 1536-float vector is not meaningfully editable in a form.
    exclude: tuple[str, ...] = ("embedding",)
    readonly_fields: tuple[str, ...] = ("created_at", "updated_at")

    @admin.display(boolean=True, description="Embedded")
    def has_embedding(self, obj: ArtifactRecord) -> bool:
        return obj.embedding is not None
