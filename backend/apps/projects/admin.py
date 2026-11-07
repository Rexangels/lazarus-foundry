from django.contrib import admin

from apps.projects.models import Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display: tuple[str, ...] = (
        "name",
        "status",
        "is_golden_demo",
        "default_branch",
        "repo_url",
        "created_at",
        "updated_at",
    )
    list_filter: tuple[str, ...] = ("status", "is_golden_demo")
    search_fields: tuple[str, ...] = ("name", "repo_url")
    readonly_fields: tuple[str, ...] = ("id", "created_at", "updated_at")
