from django.contrib import admin

from apps.genome.models import ConflictRecord, EvidenceCitation, GenomeSynthesis, IntentNode


@admin.register(GenomeSynthesis)
class GenomeSynthesisAdmin(admin.ModelAdmin):
    list_display: tuple[str, ...] = (
        "project",
        "mode",
        "model_id",
        "node_count",
        "conflict_count",
        "catalog_size",
        "started_at",
        "finished_at",
    )
    list_filter: tuple[str, ...] = ("mode",)
    list_select_related: tuple[str, ...] = ("project",)
    readonly_fields: tuple[str, ...] = ("id", "created_at", "input_digest", "validation_report", "raw_model_response")


class EvidenceCitationInline(admin.TabularInline):
    model = EvidenceCitation
    extra: int = 0
    fields: tuple[str, ...] = (
        "source_type",
        "file_path",
        "commit_sha",
        "line_start",
        "line_end",
        "snippet",
        "weight",
        "artifact",
    )
    raw_id_fields: tuple[str, ...] = ("artifact",)


@admin.register(IntentNode)
class IntentNodeAdmin(admin.ModelAdmin):
    list_display: tuple[str, ...] = (
        "title",
        "project",
        "category",
        "vital_status",
        "epistemic_status",
        "confidence_score",
        "updated_at",
    )
    list_filter: tuple[str, ...] = ("vital_status", "epistemic_status", "category", "project")
    list_select_related: tuple[str, ...] = ("project",)
    search_fields: tuple[str, ...] = ("title", "reconstructed_intent")
    readonly_fields: tuple[str, ...] = ("id", "created_at", "updated_at")
    inlines = [EvidenceCitationInline]


@admin.register(EvidenceCitation)
class EvidenceCitationAdmin(admin.ModelAdmin):
    list_display: tuple[str, ...] = (
        "file_path",
        "source_type",
        "line_start",
        "line_end",
        "commit_sha",
        "weight",
        "intent_node",
    )
    list_filter: tuple[str, ...] = ("source_type",)
    list_select_related: tuple[str, ...] = ("intent_node",)
    search_fields: tuple[str, ...] = ("file_path", "commit_sha", "snippet")
    raw_id_fields: tuple[str, ...] = ("intent_node", "artifact")


@admin.register(ConflictRecord)
class ConflictRecordAdmin(admin.ModelAdmin):
    list_display: tuple[str, ...] = (
        "intent_node",
        "project",
        "claim_source",
        "reality_source",
        "created_at",
    )
    list_filter: tuple[str, ...] = ("project",)
    list_select_related: tuple[str, ...] = ("intent_node", "project")
    search_fields: tuple[str, ...] = ("conflict_description", "claim_source", "reality_source")
    raw_id_fields: tuple[str, ...] = ("intent_node",)
