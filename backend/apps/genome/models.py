"""Project Genome records.

The "every IntentNode has at least one EvidenceCitation" invariant
(forensic-invariants.md section 1) spans two tables and cannot be a database
constraint; the genome service enforces it when it persists nodes.
"""
import uuid

from django.db import models


class IntentCategory(models.TextChoices):
    CORE_FEATURE = "CORE_FEATURE", "Core feature"
    ARCHITECTURE = "ARCHITECTURE", "Architecture"
    INTEGRATION = "INTEGRATION", "Integration"
    DATA_MODEL = "DATA_MODEL", "Data model"


class VitalStatus(models.TextChoices):
    VERIFIED_WORKING = "VERIFIED_WORKING", "Verified working"
    ABANDONED_IN_PROGRESS = "ABANDONED_IN_PROGRESS", "Abandoned in progress"
    GHOST_FEATURE = "GHOST_FEATURE", "Ghost feature"


class EpistemicStatus(models.TextChoices):
    KNOWN = "KNOWN", "Known"
    INFERRED = "INFERRED", "Inferred"
    CONFLICTED = "CONFLICTED", "Conflicted"
    MISSING_CONTEXT = "MISSING_CONTEXT", "Missing context"


class EvidenceSourceType(models.TextChoices):
    AST_IMPLEMENTATION = "AST_IMPLEMENTATION", "AST implementation"
    UNMERGED_BRANCH_COMMIT = "UNMERGED_BRANCH_COMMIT", "Unmerged branch commit"
    ISSUE_SPEC = "ISSUE_SPEC", "Issue / spec"
    TODO_COMMENT = "TODO_COMMENT", "TODO comment"
    README_CLAIM = "README_CLAIM", "README claim"


class SynthesisMode(models.TextChoices):
    DETERMINISTIC = "DETERMINISTIC", "Deterministic (no model configured)"
    MODEL = "MODEL", "Model-refined"
    MODEL_FALLBACK = "MODEL_FALLBACK", "Deterministic after a model failure"


class GenomeSynthesis(models.Model):
    """Provenance of one genome synthesis run (apps.genome.agents / services).

    Never stores the API key or request headers: ``input_digest`` is the
    SHA-256 of the request payload (model, messages, response format) and
    ``raw_model_response`` the model's reply text, capped.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="genome_syntheses"
    )
    mode = models.CharField(max_length=32, choices=SynthesisMode.choices)
    model_id = models.CharField(max_length=255, blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()
    catalog_size = models.PositiveIntegerField(default=0)
    input_digest = models.CharField(max_length=64)
    # Dropped citation ids, rejected features, vital-status overrides, fallback reason.
    validation_report = models.JSONField(default=dict, blank=True)
    raw_model_response = models.TextField(blank=True)
    node_count = models.PositiveIntegerField(default=0)
    conflict_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name_plural = "genome syntheses"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(mode__in=SynthesisMode.values),
                name="genome_genomesynthesis_mode_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.mode} genome for {self.project_id} at {self.started_at:%Y-%m-%d %H:%M}"


class IntentNode(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="intent_nodes"
    )
    # The run that produced this node; SET_NULL keeps nodes if old provenance is purged.
    synthesis = models.ForeignKey(
        GenomeSynthesis, on_delete=models.SET_NULL, null=True, blank=True, related_name="intent_nodes"
    )
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=32, choices=IntentCategory.choices)
    vital_status = models.CharField(max_length=32, choices=VitalStatus.choices)
    epistemic_status = models.CharField(max_length=32, choices=EpistemicStatus.choices)
    confidence_score = models.FloatField()
    reconstructed_intent = models.TextField()
    actual_implementation_state = models.TextField()
    graph_position = models.JSONField(default=dict, blank=True)
    # FeatureScore.as_dict() plus the vital-status rule and how each citation entered C(f).
    score_breakdown = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-confidence_score", "title"]
        indexes = [
            models.Index(fields=["project", "vital_status"], name="genome_node_proj_vital_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(confidence_score__gte=0.0, confidence_score__lte=1.0),
                name="genome_intentnode_confidence_score_0_1",
            ),
            models.CheckConstraint(
                condition=models.Q(category__in=IntentCategory.values),
                name="genome_intentnode_category_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(vital_status__in=VitalStatus.values),
                name="genome_intentnode_vital_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(epistemic_status__in=EpistemicStatus.values),
                name="genome_intentnode_epistemic_status_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} [{self.vital_status}, C={self.confidence_score:.2f}]"


class EvidenceCitation(models.Model):
    intent_node = models.ForeignKey(
        IntentNode, on_delete=models.CASCADE, related_name="citations"
    )
    # SET_NULL keeps the citation (file, lines, snippet) if the raw artifact is purged.
    artifact = models.ForeignKey(
        "ingestion.ArtifactRecord",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="citations",
    )
    source_type = models.CharField(max_length=32, choices=EvidenceSourceType.choices)
    file_path = models.CharField(max_length=1024)
    # Optional for working-tree files that have no commit.
    commit_sha = models.CharField(max_length=64, blank=True)
    line_start = models.IntegerField(null=True, blank=True)
    line_end = models.IntegerField(null=True, blank=True)
    snippet = models.TextField()
    weight = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["file_path", "line_start", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(line_start__isnull=True)
                    | models.Q(line_end__isnull=True)
                    | models.Q(line_end__gte=models.F("line_start"))
                ),
                name="genome_evidencecitation_line_end_gte_line_start",
            ),
            models.CheckConstraint(
                condition=models.Q(source_type__in=EvidenceSourceType.values),
                name="genome_evidencecitation_source_type_valid",
            ),
        ]

    def __str__(self) -> str:
        if self.line_start is None:
            return f"{self.source_type} {self.file_path}"
        end = self.line_end if self.line_end is not None else self.line_start
        return f"{self.source_type} {self.file_path}:{self.line_start}-{end}"


class ConflictRecord(models.Model):
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="conflicts"
    )
    intent_node = models.ForeignKey(
        IntentNode, on_delete=models.CASCADE, related_name="conflicts"
    )
    claim_source = models.TextField()
    reality_source = models.TextField()
    conflict_description = models.TextField()
    resolution_recommendation = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Conflict on {self.intent_node_id}: {self.claim_source} vs {self.reality_source}"
