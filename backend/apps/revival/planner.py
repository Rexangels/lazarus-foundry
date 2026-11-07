"""Revival planner: one evidence-grounded work order per strategy (``RESTORE``, ``MODERNIZE``, ``EXTRACT``).

Pure: no ORM queries, no Git, no network. The input is the genome as
``apps.genome.graph.GenomeInputs`` (persisted via ``FeatureInput.from_model``
or in memory via ``inputs_from_features``), the ``BranchInput`` rows, and two
optional facts read from ingestion artifacts: the declared dependencies
(``dependencies_from_artifacts``) and the internal import graph
(``import_graph_from_artifacts``).

``plan_strategies`` returns three ``StrategyPlan``s. A plan's ``action_plan``
is the Foundry's work order (Task 6): ordered ``PlanItem``s, each tied to the
``FoundryRun.agent_stage`` that executes it, the files it touches, the tests
that must pass afterwards, its dependencies on earlier items and the evidence
that justifies it. Every item cites at least one piece of evidence, as every
genome node does (forensic-invariants.md section 1): a genome citation id, or
for dependency pins the manifest line.

Reading citations
    Each citation's role comes from ingestion data only: its source type, its
    file (test files by ``is_test_path``, manifests by name), its branch, its
    lines and the reconciler's match kind (``score_breakdown["citations"]``,
    joined on ``signal_index``). Stubs are ``STUB`` matches or code whose
    snippet raises ``NotImplementedError`` (``@abstractmethod`` excluded);
    branch diff files are ``UNMERGED_BRANCH_COMMIT`` citations with a file and
    no lines, and their change type and size are read from the cited patch.

Strategies
    * ``RESTORE`` finishes ``ABANDONED_IN_PROGRESS`` features in the current
      stack: merge each abandoned branch carrying the feature's code
      (``MERGE_ABANDONED_BRANCH``), complete each non-abstract stub
      (``COMPLETE_STUB``, after the merge for branch stubs), and, when the
      feature has no code of its own at HEAD, make its HEAD tests pass
      (``FIX_FAILING_TEST``: those tests are presumed failing and are never
      edited; the files to change are the modules under test and the HEAD
      files holding the feature's markers). A feature left without any target
      test gets ``ADD_TESTS``. A ``GHOST_FEATURE`` is in scope only with a
      design document (an ``ISSUE_SPEC`` citation that is not a manifest) and
      no unmet external requirement; it then gets ``IMPLEMENT_FROM_SPEC`` plus
      ``ADD_TESTS``. Everything else is listed in ``deferred`` with a reason.
    * ``MODERNIZE`` is ``RESTORE`` plus one ``UPGRADE_DEPENDENCY`` per exact
      pin on the default branch. No target version is chosen here
      (``requires_resolution=True``): the Foundry's EnvironmentAgent resolves
      it in the sandbox.
    * ``EXTRACT`` copies the highest-value cluster of ``VERIFIED_WORKING``
      features into a standalone package under ``extracted/<slug>/``
      (``EXTRACT_MODULE`` per feature, then one ``ADD_TESTS`` boundary test).
      Clusters are the connected components of verified features under the
      import graph (one feature per cluster without it), ranked by the sum of
      C(f), then citation count, then fewer files dragged in from outside the
      cluster. Import-closure files outside the cluster are copied too and
      raise R (``RiskFactor.EXTRACT_BOUNDARY``).

External requirements
    ``EXTERNAL_CAPABILITIES`` is an explicit table of capabilities a feature's
    prose can demand (PDF rendering, QR codes, live FX rates, email, payment
    gateways, object storage). The title and the claim, spec and marker
    snippets the feature cites are matched against each capability's trigger
    words. A third-party dependency is unmet when none of the packages that
    provide it is declared; an external service is always unmet, because the
    sandbox has no network. A ghost with an unmet requirement is deferred. An
    abandoned feature stays in scope, its items carry the requirement as a
    constraint and a feasibility factor, and an external service's live part
    is listed as a partial deferral.

Item feasibility (``PlanItem.item_feasibility``) is the product of named
``FeasibilityFactor``s (``PlannerParameters``): the kind's base, merge distance
``1 / (1 + commits_behind / merge_behind_scale)``, open markers in the item's
files ``max(floor, factor ** n)``, external service, new dependency, and one
boundary-leak factor per outside file for an extract. A feature's feasibility
is the lowest of its items' (the weakest link decides whether it ends up
working); ``apps.revival.scoring.value_ratio`` uses it to weight V.

Ordering is deterministic: stage (ENVIRONMENT, ARCHITECTURE, ENGINEERING),
then feature (title, id; extract: cluster rank), then kind, then file. Item
ids are ``<strategy>-NN`` in that order, and ``depends_on`` only names earlier
items.
"""
from __future__ import annotations

import math
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from apps.genome.catalog import is_test_path
from apps.genome.graph import BranchInput, CitationInput, FeatureInput, GenomeInputs, ProjectInput
from apps.genome.models import EvidenceSourceType, VitalStatus
from apps.ingestion.models import ArtifactType
from apps.ingestion.serialization import to_json
from apps.revival.models import StrategyType
from apps.revival.scoring import RiskFactor, ValueBasis, ValueMode, ValueTarget

FEASIBILITY_DECIMALS: int = 4


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
class PlanItemKind(StrEnum):
    MERGE_ABANDONED_BRANCH = "MERGE_ABANDONED_BRANCH"
    COMPLETE_STUB = "COMPLETE_STUB"
    FIX_FAILING_TEST = "FIX_FAILING_TEST"
    IMPLEMENT_FROM_SPEC = "IMPLEMENT_FROM_SPEC"
    UPGRADE_DEPENDENCY = "UPGRADE_DEPENDENCY"
    EXTRACT_MODULE = "EXTRACT_MODULE"
    ADD_TESTS = "ADD_TESTS"


class PlanStage(StrEnum):
    """The ``apps.foundry.models.AgentStage`` that executes an item (VERIFICATION runs every target test)."""

    ENVIRONMENT = "ENVIRONMENT"
    ARCHITECTURE = "ARCHITECTURE"
    ENGINEERING = "ENGINEERING"


KIND_STAGE: Mapping[PlanItemKind, PlanStage] = MappingProxyType(
    {
        PlanItemKind.UPGRADE_DEPENDENCY: PlanStage.ENVIRONMENT,
        PlanItemKind.MERGE_ABANDONED_BRANCH: PlanStage.ARCHITECTURE,
        PlanItemKind.EXTRACT_MODULE: PlanStage.ARCHITECTURE,
        PlanItemKind.COMPLETE_STUB: PlanStage.ENGINEERING,
        PlanItemKind.IMPLEMENT_FROM_SPEC: PlanStage.ENGINEERING,
        PlanItemKind.FIX_FAILING_TEST: PlanStage.ENGINEERING,
        PlanItemKind.ADD_TESTS: PlanStage.ENGINEERING,
    }
)
_STAGE_ORDER: Mapping[PlanStage, int] = {stage: index for index, stage in enumerate(PlanStage)}
_KIND_ORDER: Mapping[PlanItemKind, int] = {
    kind: index
    for index, kind in enumerate(
        (
            PlanItemKind.UPGRADE_DEPENDENCY,
            PlanItemKind.MERGE_ABANDONED_BRANCH,
            PlanItemKind.EXTRACT_MODULE,
            PlanItemKind.COMPLETE_STUB,
            PlanItemKind.IMPLEMENT_FROM_SPEC,
            PlanItemKind.FIX_FAILING_TEST,
            PlanItemKind.ADD_TESTS,
        )
    )
}


class Effort(StrEnum):
    S = "S"
    M = "M"
    L = "L"


class DeferralReason(StrEnum):
    NO_DETAILED_SPEC = "NO_DETAILED_SPEC"  # a ghost with no design document
    NEEDS_NEW_DEPENDENCY = "NEEDS_NEW_DEPENDENCY"  # a ghost needing an undeclared third-party package
    NEEDS_EXTERNAL_SERVICE = "NEEDS_EXTERNAL_SERVICE"  # needs a live service the sandbox cannot reach
    BRANCH_STILL_ACTIVE = "BRANCH_STILL_ACTIVE"  # the feature's code is on an unmerged branch that is not abandoned
    NO_ACTIONABLE_EVIDENCE = "NO_ACTIONABLE_EVIDENCE"  # nothing in the evidence says what to change
    OUTSIDE_SELECTED_CLUSTER = "OUTSIDE_SELECTED_CLUSTER"  # EXTRACT: verified, but in a lower-value cluster
    NOT_VERIFIED_WORKING = "NOT_VERIFIED_WORKING"  # EXTRACT: only verified features are extracted


class RequirementKind(StrEnum):
    THIRD_PARTY_DEPENDENCY = "THIRD_PARTY_DEPENDENCY"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"


class EvidenceKind(StrEnum):
    CITATION = "CITATION"  # an EvidenceCitation of the genome
    DEPENDENCY = "DEPENDENCY"  # a dependency pin in a manifest (DEPENDENCY_MANIFEST artifact)


@dataclass(frozen=True)
class ExternalCapability:
    key: str
    kind: RequirementKind
    label: str
    triggers: tuple[str, ...]  # lower-case words or phrases, matched on word boundaries
    satisfied_by: tuple[str, ...] = ()  # normalized package names that provide it (dependencies only)


EXTERNAL_CAPABILITIES: tuple[ExternalCapability, ...] = (
    ExternalCapability(
        "pdf_rendering", RequirementKind.THIRD_PARTY_DEPENDENCY, "a PDF rendering library",
        ("pdf", "reportlab", "weasyprint"),
        ("reportlab", "weasyprint", "fpdf", "fpdf2", "xhtml2pdf", "pdfkit", "borb"),
    ),
    ExternalCapability(
        "qr_codes", RequirementKind.THIRD_PARTY_DEPENDENCY, "a QR code library",
        ("qr code", "qr codes", "qrcode"), ("qrcode", "segno"),
    ),
    ExternalCapability(
        "live_fx_rates", RequirementKind.EXTERNAL_SERVICE, "a live FX rate feed",
        ("ecb", "exchange rate", "exchange rates", "fx rate", "fx rates", "forex", "openexchangerates"),
    ),
    ExternalCapability(
        "email_delivery", RequirementKind.EXTERNAL_SERVICE, "an email delivery service",
        ("smtp", "sendgrid", "mailgun", "postmark", "amazon ses"),
    ),
    ExternalCapability(
        "payment_gateway", RequirementKind.EXTERNAL_SERVICE, "a payment gateway",
        ("stripe", "paypal", "braintree", "adyen"),
    ),
    ExternalCapability(
        "object_storage", RequirementKind.EXTERNAL_SERVICE, "cloud object storage",
        ("s3 bucket", "amazon s3", "google cloud storage", "azure blob"),
    ),
)

_MANIFEST_NAME = re.compile(r"^(requirements[\w.\-]*\.txt|pyproject\.toml|package\.json|setup\.cfg|pipfile)$", re.I)
_TEST_DEF = re.compile(r"^(?P<indent>[ \t]*)(?:async[ \t]+)?def[ \t]+(?P<name>test\w*)", re.M)
_DEF = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+(?P<name>\w+)", re.M)
_STAT_FALLBACK = re.compile(r"^(?P<change>[A-Z_]+) .*\(\+(?P<add>\d+) -(?P<del>\d+)\)\s*$")
_SPEC_PATH = re.compile(r"(?<![\w/.:-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_-]+\.[A-Za-z0-9]{1,5})(?![\w/])")
_NORMALIZE_NAME = re.compile(r"[-_.]+")


def is_manifest_path(path: str) -> bool:
    return bool(path) and bool(_MANIFEST_NAME.match(PurePosixPath(path).name))


def normalize_package_name(name: str) -> str:
    """PEP 503 normalization (``Foo_Bar`` -> ``foo-bar``)."""
    return _NORMALIZE_NAME.sub("-", name).strip().lower()


def _trigger_pattern(trigger: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(trigger)}(?![a-z0-9])")


_TRIGGERS: Mapping[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    capability.key: tuple((trigger, _trigger_pattern(trigger)) for trigger in capability.triggers)
    for capability in EXTERNAL_CAPABILITIES
}


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
def _unit(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise TypeError(f"{name} must be a finite real number")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{name} must lie in [0, 1], got {value!r}")
    return float(value)


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int, got {value!r}")
    return value


DEFAULT_BASE_FEASIBILITY: Mapping[PlanItemKind, float] = MappingProxyType(
    {
        PlanItemKind.MERGE_ABANDONED_BRANCH: 0.85,  # the code exists; the merge and its review remain
        PlanItemKind.COMPLETE_STUB: 0.75,  # the interface is fixed; the body is new code
        PlanItemKind.FIX_FAILING_TEST: 0.80,  # a test states exactly what must hold
        PlanItemKind.IMPLEMENT_FROM_SPEC: 0.50,  # only prose: everything is new code
        PlanItemKind.UPGRADE_DEPENDENCY: 0.90,  # mechanical; breakage is priced in R, not here
        PlanItemKind.EXTRACT_MODULE: 0.85,  # verified code is copied, then made standalone
        PlanItemKind.ADD_TESTS: 0.90,
    }
)


@dataclass(frozen=True)
class PlannerParameters:
    """Every constant of item feasibility and effort. Use ``DEFAULT_PLANNER_PARAMETERS``."""

    base_feasibility: Mapping[PlanItemKind, float] = field(
        default_factory=lambda: DEFAULT_BASE_FEASIBILITY, hash=False
    )
    merge_behind_scale: float = 20.0  # MERGE_DISTANCE factor = 1 / (1 + commits_behind / scale)
    open_marker_factor: float = 0.95  # per open TODO/FIXME in the item's files ...
    open_marker_floor: float = 0.80  # ... but never below this
    external_service_factor: float = 0.60  # the live part cannot be built or tested in the sandbox
    new_dependency_factor: float = 0.70  # an undeclared package must be chosen and added
    boundary_leak_factor: float = 0.90  # per file an extract drags in from outside its cluster
    effort_small_files: int = 2
    effort_small_lines: int = 80
    effort_medium_files: int = 6
    effort_medium_lines: int = 400
    extract_root: str = "extracted"
    default_test_dir: str = "tests"

    def __post_init__(self) -> None:
        base: dict[PlanItemKind, float] = {}
        for key, value in self.base_feasibility.items():
            base[PlanItemKind(key)] = _unit(f"base_feasibility[{key}]", value)
        missing = [kind.value for kind in PlanItemKind if kind not in base]
        if missing:
            raise ValueError(f"base_feasibility is missing {missing}")
        object.__setattr__(self, "base_feasibility", MappingProxyType(base))
        scale = self.merge_behind_scale
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
            raise ValueError("merge_behind_scale must be a finite number > 0")
        for name in ("open_marker_factor", "open_marker_floor", "external_service_factor",
                     "new_dependency_factor", "boundary_leak_factor"):
            _unit(name, getattr(self, name))
        for name in ("effort_small_files", "effort_small_lines", "effort_medium_files", "effort_medium_lines"):
            _positive_int(name, getattr(self, name))
        if self.effort_small_files > self.effort_medium_files or self.effort_small_lines > self.effort_medium_lines:
            raise ValueError("small effort limits must not exceed medium ones")
        for name in ("extract_root", "default_test_dir"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value.startswith("/") or ".." in value.split("/"):
                raise ValueError(f"{name} must be a relative POSIX path")

    def as_dict(self) -> dict[str, Any]:
        return to_json(
            {
                "base_feasibility": {kind.value: self.base_feasibility[kind] for kind in PlanItemKind},
                "merge_behind_scale": self.merge_behind_scale,
                "open_marker_factor": self.open_marker_factor,
                "open_marker_floor": self.open_marker_floor,
                "external_service_factor": self.external_service_factor,
                "new_dependency_factor": self.new_dependency_factor,
                "boundary_leak_factor": self.boundary_leak_factor,
                "effort": {
                    "small_files": self.effort_small_files, "small_lines": self.effort_small_lines,
                    "medium_files": self.effort_medium_files, "medium_lines": self.effort_medium_lines,
                },
            }
        )  # type: ignore[return-value]


DEFAULT_PLANNER_PARAMETERS: PlannerParameters = PlannerParameters()


# ---------------------------------------------------------------------------
# Optional ingestion facts
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DependencyInput:
    """One declared dependency on the default branch (a ``DEPENDENCY_MANIFEST`` record)."""

    name: str
    normalized_name: str
    specifier: str
    pinned_version: str | None
    file_path: str
    line_start: int | None
    line_end: int | None
    snippet: str
    ecosystem: str = ""
    manifest: str = ""
    group: str | None = None
    artifact_id: int | None = None

    @property
    def is_pinned(self) -> bool:
        return bool(self.pinned_version)


def dependencies_from_artifacts(artifacts: Iterable[Any], default_branch: str) -> tuple[DependencyInput, ...]:
    """Default-branch dependencies from ``ArtifactRecord`` / ``ArtifactRow`` / ``ArtifactInput`` rows."""
    found: list[DependencyInput] = []
    for row in artifacts:
        if ArtifactType(row.artifact_type) is not ArtifactType.DEPENDENCY_MANIFEST or row.branch_name != default_branch:
            continue
        meta = row.structured_metadata or {}
        for record in meta.get("dependencies") or ():
            name = str(record.get("name") or "")
            if not name:
                continue
            found.append(
                DependencyInput(
                    name=name,
                    normalized_name=normalize_package_name(str(record.get("normalized_name") or name)),
                    specifier=str(record.get("specifier") or ""),
                    pinned_version=record.get("pinned_version") or None,
                    file_path=str(record.get("file_path") or row.file_path),
                    line_start=record.get("line_start"),
                    line_end=record.get("line_end"),
                    snippet=str(record.get("snippet") or ""),
                    ecosystem=str(record.get("ecosystem") or ""),
                    manifest=str(record.get("manifest") or meta.get("manifest") or ""),
                    group=record.get("group"),
                    artifact_id=getattr(row, "pk", None),
                )
            )
    return tuple(sorted(found, key=lambda d: (d.file_path, d.line_start or 0, d.normalized_name)))


@dataclass(frozen=True)
class ImportGraph:
    """Default-branch Python imports: repository files each file imports, and its third-party top-level modules."""

    internal: Mapping[str, frozenset[str]]
    external: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @property
    def files(self) -> frozenset[str]:
        return frozenset(self.internal)

    def closure(self, start: Iterable[str]) -> frozenset[str]:
        seen: set[str] = set()
        stack = sorted(set(start))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(sorted(self.internal.get(current, frozenset()) - seen))
        return frozenset(seen)


def _module_names(path: str) -> tuple[str, ...]:
    """Dotted names a Python file can be imported as (``src/`` layouts included)."""
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return ()
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return ()
    names = [".".join(parts)]
    if parts[0] == "src" and len(parts) > 1:
        names.append(".".join(parts[1:]))
    return tuple(names)


def import_graph_from_artifacts(artifacts: Iterable[Any], default_branch: str) -> ImportGraph:
    """The import graph of the default branch's ``AST_MODULE`` rows (absolute, relative and ``from pkg import mod``)."""
    modules: dict[str, list[Mapping[str, Any]]] = {}
    for row in artifacts:
        if ArtifactType(row.artifact_type) is not ArtifactType.AST_MODULE or row.branch_name != default_branch:
            continue
        if not str(row.file_path).endswith(".py"):
            continue
        modules[row.file_path] = list((row.structured_metadata or {}).get("imports") or ())
    by_name: dict[str, str] = {}
    for path in sorted(modules):
        for name in _module_names(path):
            by_name.setdefault(name, path)
    stdlib = frozenset(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    internal: dict[str, frozenset[str]] = {}
    external: dict[str, frozenset[str]] = {}
    for path, records in modules.items():
        own = _module_names(path)
        package = own[0].split(".") if own else []
        if not path.endswith("__init__.py"):
            package = package[:-1]
        targets: set[str] = set()
        third_party: set[str] = set()
        for record in records:
            module = str(record.get("module") or "")
            level = int(record.get("level") or 0)
            if level:
                base = package[: len(package) - (level - 1)] if level - 1 <= len(package) else []
                module = ".".join([*base, module] if module else base)
            if not module:
                continue
            candidates = [module] + [f"{module}.{name.get('name')}" for name in record.get("names") or () if name.get("name")]
            resolved = [by_name[c] for c in candidates if c in by_name]
            if resolved:
                targets.update(resolved)
            elif level == 0:
                top = module.split(".")[0]
                if top not in stdlib and top not in {n.split(".")[0] for n in by_name}:
                    third_party.add(top)
        targets.discard(path)
        internal[path] = frozenset(targets)
        external[path] = frozenset(third_party)
    return ImportGraph(MappingProxyType(internal), MappingProxyType(external))


# ---------------------------------------------------------------------------
# Plan records
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EvidenceRef:
    """What justifies a plan item: a genome citation, or a manifest line for dependency pins."""

    ref: str  # the citation id, or "dependency:<file>:<line>"
    kind: EvidenceKind
    source_type: str
    file_path: str
    line_start: int | None
    line_end: int | None
    branch_name: str | None = None
    citation_id: str | None = None
    artifact_id: int | None = None


@dataclass(frozen=True)
class FeasibilityFactor:
    name: str  # BASE, MERGE_DISTANCE, OPEN_MARKERS, EXTERNAL_SERVICE, NEW_DEPENDENCY, BOUNDARY_LEAK
    value: float
    detail: str


@dataclass(frozen=True)
class ExternalRequirement:
    capability: str
    kind: RequirementKind
    label: str
    matched_terms: tuple[str, ...]
    evidence: tuple[str, ...]  # citation ids whose text matched ("title" for the feature title)
    candidates: tuple[str, ...] = ()  # packages that would provide it (dependencies only)


@dataclass(frozen=True)
class DependencyChange:
    name: str
    ecosystem: str
    manifest: str
    file_path: str
    line_start: int | None
    current_specifier: str
    current_version: str
    target_version: str | None = None  # always None: the EnvironmentAgent resolves it


@dataclass(frozen=True)
class FileCopy:
    source: str
    destination: str


@dataclass(frozen=True)
class PlanItem:
    """One step of the Foundry's work order."""

    id: str
    kind: PlanItemKind
    stage: PlanStage
    feature_id: str | None  # None for project-wide items (dependency upgrades)
    feature_key: str | None
    title: str
    rationale: str
    files_to_modify: tuple[str, ...]
    files_to_create: tuple[str, ...]
    target_tests: tuple[str, ...]  # "path" or "path::test_name"; must pass after the item and are never edited
    branch: str | None
    evidence: tuple[EvidenceRef, ...]
    effort: Effort
    item_feasibility: float
    feasibility_factors: tuple[FeasibilityFactor, ...]
    depends_on: tuple[str, ...] = ()
    requires_resolution: bool = False
    dependency: DependencyChange | None = None
    external_requirements: tuple[ExternalRequirement, ...] = ()
    open_markers: tuple[str, ...] = ()  # "path:line: <marker text>" left in the item's files
    constraints: tuple[str, ...] = ()
    copies: tuple[FileCopy, ...] = ()  # files copied verbatim before editing (EXTRACT)
    changed_lines: int = 0  # the size estimate behind ``effort``

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError(f"plan item {self.id} cites no evidence")
        _unit("item_feasibility", self.item_feasibility)
        if set(self.files_to_modify) & set(self.files_to_create):
            raise ValueError(f"plan item {self.id} both modifies and creates the same file")
        if KIND_STAGE[self.kind] is not self.stage:
            raise ValueError(f"plan item {self.id}: {self.kind} runs in {KIND_STAGE[self.kind]}")

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(ref.ref for ref in self.evidence)

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(sorted({*self.files_to_modify, *self.files_to_create}))


@dataclass(frozen=True)
class ScopeEntry:
    """A feature the plan targets, with its feasibility (the weakest of its items')."""

    feature_id: str
    feature_key: str | None
    title: str
    vital_status: str
    feasibility: float
    item_ids: tuple[str, ...]
    partial: bool = False  # part of the feature is deferred (see ``deferred``)


@dataclass(frozen=True)
class DeferredEntry:
    feature_id: str
    feature_key: str | None
    title: str
    vital_status: str
    reason: DeferralReason
    detail: str
    evidence: tuple[EvidenceRef, ...]
    external_requirements: tuple[ExternalRequirement, ...] = ()
    would_touch: tuple[str, ...] = ()
    partial: bool = False  # the rest of the feature is in scope

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError(f"deferral of {self.feature_id} cites no evidence")


@dataclass(frozen=True)
class StrategyPlan:
    strategy_type: StrategyType
    title: str
    summary: str
    action_plan: tuple[PlanItem, ...]
    in_scope: tuple[ScopeEntry, ...]
    deferred: tuple[DeferredEntry, ...]
    value_basis: ValueBasis
    risk_evidence: Mapping[str, int]
    extract_root: str | None = None

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for item in self.action_plan:
            if item.id in seen:
                raise ValueError(f"duplicate plan item id {item.id}")
            missing = [dep for dep in item.depends_on if dep not in seen]
            if missing:
                raise ValueError(f"plan item {item.id} depends on later or unknown items {missing}")
            seen.add(item.id)
        object.__setattr__(self, "risk_evidence", MappingProxyType(dict(self.risk_evidence)))

    @property
    def files(self) -> tuple[str, ...]:
        """``files_to_modify U files_to_create`` over every item (the D input)."""
        return tuple(sorted({path for item in self.action_plan for path in item.files}))

    @property
    def is_actionable(self) -> bool:
        return bool(self.action_plan)

    def action_plan_json(self) -> list[Any]:
        """``RevivalStrategy.action_plan``: the Foundry's work order as plain JSON."""
        return to_json(list(self.action_plan), path="action_plan")  # type: ignore[return-value]

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_type": self.strategy_type.value,
            "title": self.title,
            "summary": self.summary,
            "action_plan": self.action_plan_json(),
            "in_scope": to_json(list(self.in_scope), path="in_scope"),
            "deferred": to_json(list(self.deferred), path="deferred"),
            "files": list(self.files),
            "extract_root": self.extract_root,
            "risk_evidence": dict(self.risk_evidence),
        }


# ---------------------------------------------------------------------------
# Reading citations
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Cited:
    citation: CitationInput
    branch: str | None  # resolved branch; None when unknown (treated as the default branch)
    on_default: bool
    match_kind: str | None

    @property
    def id(self) -> str:
        return self.citation.id

    @property
    def path(self) -> str:
        return self.citation.file_path

    @property
    def source_type(self) -> str:
        return self.citation.source_type

    @property
    def is_code_source(self) -> bool:
        return self.source_type in (EvidenceSourceType.AST_IMPLEMENTATION, EvidenceSourceType.UNMERGED_BRANCH_COMMIT)

    @property
    def is_branch_file(self) -> bool:
        return (
            self.source_type == EvidenceSourceType.UNMERGED_BRANCH_COMMIT
            and bool(self.path)
            and self.citation.line_start is None
        )

    @property
    def is_branch_commit(self) -> bool:
        return self.source_type == EvidenceSourceType.UNMERGED_BRANCH_COMMIT and not self.path

    @property
    def is_symbol(self) -> bool:
        return self.is_code_source and bool(self.path) and self.citation.line_start is not None

    @property
    def is_test(self) -> bool:
        return self.is_symbol and is_test_path(self.path)

    @property
    def is_stub(self) -> bool:
        """The reconciler's ``STUB`` match; without match kinds, a function or raise site raising NotImplementedError.

        A class whose body contains a stub method is not itself a stub.
        """
        if not self.is_symbol or self.is_test or "abstractmethod" in self.citation.snippet:
            return False
        if self.match_kind is not None:
            return self.match_kind == "STUB"
        head = next(
            (line.strip() for line in self.citation.snippet.splitlines() if line.strip() and not line.strip().startswith("@")),
            "",
        )
        return "NotImplementedError" in self.citation.snippet and head.startswith(("def ", "async def ", "raise "))

    @property
    def is_code(self) -> bool:
        return self.is_symbol and not self.is_test and not self.is_stub

    @property
    def is_marker(self) -> bool:
        return self.source_type == EvidenceSourceType.TODO_COMMENT

    @property
    def is_dependency(self) -> bool:
        return self.source_type == EvidenceSourceType.ISSUE_SPEC and is_manifest_path(self.path)

    @property
    def is_spec(self) -> bool:
        return self.source_type == EvidenceSourceType.ISSUE_SPEC and not is_manifest_path(self.path) and self.on_default

    @property
    def is_claim(self) -> bool:
        return self.source_type == EvidenceSourceType.README_CLAIM

    @property
    def span(self) -> int:
        start, end = self.citation.line_start, self.citation.line_end
        if start is None:
            return 0
        return max(1, (end if end is not None else start) - start + 1)

    def ref(self) -> EvidenceRef:
        c = self.citation
        return EvidenceRef(
            ref=c.id, kind=EvidenceKind.CITATION, source_type=c.source_type, file_path=c.file_path,
            line_start=c.line_start, line_end=c.line_end, branch_name=self.branch, citation_id=c.id,
            artifact_id=c.artifact_id,
        )

    def marker_text(self) -> str:
        first = next((line.strip() for line in self.citation.snippet.splitlines() if line.strip()), "")
        first = first.lstrip("#/*; -").strip()
        where = f"{self.path}:{self.citation.line_start}" if self.citation.line_start is not None else self.path
        return f"{where}: {first}"


def test_node_id(cited: _Cited) -> str:
    """``path::name`` for a module-level test function, else the test file."""
    match = _TEST_DEF.search(cited.citation.snippet)
    if match and not match["indent"]:
        return f"{cited.path}::{match['name']}"
    return cited.path


def _symbol_name(cited: _Cited) -> str | None:
    match = _DEF.search(cited.citation.snippet)
    return match["name"] if match else None


def _patch_stats(snippet: str) -> tuple[str, int]:
    """(change type, changed lines) of a branch diff file citation, read from its patch (or its stat line)."""
    lines = snippet.splitlines()
    fallback = _STAT_FALLBACK.match(lines[0]) if lines else None
    if fallback:
        return fallback["change"], int(fallback["add"]) + int(fallback["del"])
    change = "MODIFIED"
    if any(line.startswith("--- /dev/null") for line in lines[:4]):
        change = "ADDED"
    elif any(line.startswith("+++ /dev/null") for line in lines[:4]):
        change = "DELETED"
    changed = sum(
        1 for line in lines
        if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---"))
    )
    return change, changed


class _FeatureView:
    """One feature's citations, sorted into roles."""

    def __init__(self, feature: FeatureInput, default_branch: str, head_to_branch: Mapping[str, str]) -> None:
        if not feature.citations:
            raise ValueError(f"feature {feature.id} has no citations (forensic-invariants.md section 1)")
        self.feature = feature
        kinds: dict[int, str] = {}
        for entry in feature.score_breakdown.get("citations") or ():
            index = entry.get("signal_index")
            if isinstance(index, int) and entry.get("match_kind"):
                kinds[index] = str(entry["match_kind"])
        cited: list[_Cited] = []
        for citation in feature.citations:
            branch = citation.branch_name or head_to_branch.get(citation.commit_sha)
            on_default = citation.source_type != EvidenceSourceType.UNMERGED_BRANCH_COMMIT and branch in (None, default_branch)
            kind = kinds.get(citation.signal_index) if citation.signal_index is not None else None
            cited.append(_Cited(citation, branch, on_default, kind))
        self.cited = tuple(sorted(cited, key=lambda c: (c.path, c.citation.line_start or 0, c.citation.line_end or 0, c.id)))

    @property
    def id(self) -> str:
        return self.feature.id

    @property
    def key(self) -> str | None:
        value = self.feature.score_breakdown.get("feature_key")
        return str(value) if value else None

    @property
    def title(self) -> str:
        return self.feature.title

    def head(self) -> tuple[_Cited, ...]:
        return tuple(c for c in self.cited if c.on_default)

    def branch_only(self) -> tuple[_Cited, ...]:
        return tuple(c for c in self.cited if not c.on_default and c.branch)

    @property
    def has_head_code(self) -> bool:
        return any(c.is_code for c in self.head())

    def head_tests(self) -> tuple[_Cited, ...]:
        return tuple(c for c in self.head() if c.is_test)

    def head_stubs(self) -> tuple[_Cited, ...]:
        return tuple(c for c in self.head() if c.is_stub)

    def head_markers(self) -> tuple[_Cited, ...]:
        return tuple(c for c in self.head() if c.is_marker and not is_test_path(c.path))

    def own_code_files(self) -> tuple[str, ...]:
        own = {c.path for c in self.head() if c.is_code and c.match_kind == "OWN_CODE"}
        if not own and not any(c.match_kind for c in self.cited):
            own = {c.path for c in self.head() if c.is_code}
        return tuple(sorted(own))

    def own_test_files(self) -> tuple[str, ...]:
        own = {c.path for c in self.head() if c.is_test and c.match_kind == "OWN_TEST"}
        if not own and not any(c.match_kind for c in self.cited):
            own = {c.path for c in self.head() if c.is_test}
        return tuple(sorted(own))

    def specs(self) -> tuple[_Cited, ...]:
        return tuple(c for c in self.cited if c.is_spec)

    def claims(self) -> tuple[_Cited, ...]:
        return tuple(c for c in self.cited if c.is_claim)

    def grounding(self) -> tuple[EvidenceRef, ...]:
        """Evidence for a deferral or exclusion: the claims and specs, else every citation."""
        chosen = [c for c in self.cited if c.is_claim or c.is_spec] or list(self.cited)
        return tuple(c.ref() for c in chosen)

    def prose(self) -> list[tuple[str, str]]:
        """(evidence id, lower-case text) the external-requirement triggers are matched against."""
        texts = [("title", self.title.lower())]
        texts.extend((c.id, c.citation.snippet.lower()) for c in self.cited if c.is_claim or c.is_spec or c.is_marker)
        return texts


def external_requirements(view: _FeatureView, declared: frozenset[str]) -> tuple[ExternalRequirement, ...]:
    """Unmet ``EXTERNAL_CAPABILITIES`` the feature's prose demands (see the module docstring)."""
    texts = view.prose()
    found: list[ExternalRequirement] = []
    for capability in EXTERNAL_CAPABILITIES:
        terms: set[str] = set()
        evidence: list[str] = []
        for evidence_id, text in texts:
            hits = [trigger for trigger, pattern in _TRIGGERS[capability.key] if pattern.search(text)]
            if hits:
                terms.update(hits)
                evidence.append(evidence_id)
        if not terms:
            continue
        if capability.kind is RequirementKind.THIRD_PARTY_DEPENDENCY and declared & set(capability.satisfied_by):
            continue
        found.append(
            ExternalRequirement(
                capability=capability.key, kind=capability.kind, label=capability.label,
                matched_terms=tuple(sorted(terms)), evidence=tuple(evidence), candidates=capability.satisfied_by,
            )
        )
    return tuple(found)


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------
@dataclass
class _Draft:
    key: str
    kind: PlanItemKind
    rank: int
    view: _FeatureView | None
    title: str
    rationale: str
    evidence: list[EvidenceRef]
    factors: list[FeasibilityFactor]
    files_to_modify: set[str] = field(default_factory=set)
    files_to_create: set[str] = field(default_factory=set)
    target_tests: list[str] = field(default_factory=list)
    branch: str | None = None
    lines: int = 0
    depends_on: list[str] = field(default_factory=list)
    requires_resolution: bool = False
    dependency: DependencyChange | None = None
    external_requirements: tuple[ExternalRequirement, ...] = ()
    open_markers: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    copies: list[FileCopy] = field(default_factory=list)
    sort_hint: str = ""

    @property
    def feasibility(self) -> float:
        value = 1.0
        for factor in self.factors:
            value *= factor.value
        return round(min(1.0, max(0.0, value)), FEASIBILITY_DECIMALS)


def _dedupe_refs(refs: Iterable[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    seen: dict[str, EvidenceRef] = {}
    for ref in refs:
        seen.setdefault(ref.ref, ref)
    return tuple(seen.values())


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


class _Planner:
    def __init__(
        self,
        genome: GenomeInputs,
        branches: Sequence[BranchInput],
        dependencies: Sequence[DependencyInput],
        imports: ImportGraph | None,
        params: PlannerParameters,
    ) -> None:
        self.project: ProjectInput = genome.project
        self.default = genome.project.default_branch
        self.params = params
        self.branches = {branch.name: branch for branch in branches}
        head_to_branch: dict[str, str] = {}
        for branch in sorted(branches, key=lambda b: (b.name == self.default, b.name)):  # default wins ties
            head_to_branch[branch.head_sha] = branch.name
        features = sorted(genome.features, key=lambda f: (f.title.casefold(), f.id))
        ids = [feature.id for feature in features]
        if len(set(ids)) != len(ids):
            raise ValueError("feature ids must be unique")
        self.views = tuple(_FeatureView(feature, self.default, head_to_branch) for feature in features)
        self.rank = {view.id: index for index, view in enumerate(self.views)}
        self.dependencies = tuple(dependencies)
        self.declared = frozenset(dep.normalized_name for dep in self.dependencies)
        self.imports = imports
        self.requirements = {view.id: external_requirements(view, self.declared) for view in self.views}
        code: set[str] = set(imports.files if imports else ())
        tests: set[str] = set()
        for view in self.views:
            for cited in view.head():
                if cited.is_symbol:
                    (tests if cited.is_test else code).add(cited.path)
        self.known_code = frozenset(path for path in code if not is_test_path(path))
        self.known_tests = frozenset(tests | {path for path in code if is_test_path(path)})
        self.known_files = frozenset(
            self.known_code | self.known_tests | {c.path for v in self.views for c in v.head() if c.path}
        )
        self.verified = tuple(v for v in self.views if v.feature.vital_status == VitalStatus.VERIFIED_WORKING)

    # Helpers -------------------------------------------------------------
    def test_dir(self) -> str:
        parents = sorted(str(PurePosixPath(path).parent) for path in self.known_tests)
        if not parents:
            return self.params.default_test_dir
        counts: dict[str, int] = defaultdict(int)
        for parent in parents:
            counts[parent] += 1
        return min(counts, key=lambda parent: (-counts[parent], parent))

    def module_under_test(self, test_path: str) -> str | None:
        stem = PurePosixPath(test_path).stem
        if stem.startswith("test_"):
            stem = stem[len("test_"):]
        elif stem.endswith("_test"):
            stem = stem[: -len("_test")]
        candidates = sorted(path for path in self.known_code if PurePosixPath(path).stem == stem)
        if not candidates:
            return None
        imported = self.imports.internal.get(test_path, frozenset()) if self.imports else frozenset()
        return min(candidates, key=lambda path: (path not in imported, len(PurePosixPath(path).parts), path))

    def open_marker_factor(self, count: int) -> FeasibilityFactor:
        value = max(self.params.open_marker_floor, self.params.open_marker_factor ** count)
        return FeasibilityFactor("OPEN_MARKERS", round(value, 6), f"{count} open TODO/FIXME marker(s) in the item's files")

    def requirement_factors(self, requirements: Sequence[ExternalRequirement]) -> list[FeasibilityFactor]:
        factors: list[FeasibilityFactor] = []
        services = [r for r in requirements if r.kind is RequirementKind.EXTERNAL_SERVICE]
        packages = [r for r in requirements if r.kind is RequirementKind.THIRD_PARTY_DEPENDENCY]
        if services:
            factors.append(FeasibilityFactor(
                "EXTERNAL_SERVICE", self.params.external_service_factor,
                "needs " + ", ".join(r.label for r in services) + "; the sandbox has no network",
            ))
        if packages:
            factors.append(FeasibilityFactor(
                "NEW_DEPENDENCY", self.params.new_dependency_factor,
                "needs " + ", ".join(r.label for r in packages) + " that no manifest declares",
            ))
        return factors

    def requirement_constraints(self, requirements: Sequence[ExternalRequirement]) -> list[str]:
        notes: list[str] = []
        for requirement in requirements:
            if requirement.kind is RequirementKind.EXTERNAL_SERVICE:
                notes.append(
                    f"No network in the sandbox: implement against an injectable interface for {requirement.label} "
                    "with an offline test double; the live integration is deferred."
                )
            else:
                notes.append(
                    f"Needs {requirement.label} (one of {', '.join(requirement.candidates)}); add it to the manifest "
                    "and let the EnvironmentAgent resolve the version."
                )
        return notes

    def base(self, kind: PlanItemKind) -> FeasibilityFactor:
        return FeasibilityFactor("BASE", self.params.base_feasibility[kind], f"base feasibility of {kind.value}")

    # RESTORE ---------------------------------------------------------------
    def restore(self) -> tuple[list[_Draft], list[DeferredEntry], dict[str, int]]:
        drafts: list[_Draft] = []
        deferred: list[DeferredEntry] = []
        merged: dict[str, int] = {}
        for view in self.views:
            status = view.feature.vital_status
            if status == VitalStatus.ABANDONED_IN_PROGRESS:
                feature_drafts, feature_deferred = self.restore_abandoned(view, merged)
            elif status == VitalStatus.GHOST_FEATURE:
                feature_drafts, feature_deferred = self.restore_ghost(view)
            else:
                continue
            drafts.extend(feature_drafts)
            deferred.extend(feature_deferred)
        return drafts, deferred, merged

    def restore_abandoned(self, view: _FeatureView, merged: dict[str, int]) -> tuple[list[_Draft], list[DeferredEntry]]:
        rank = self.rank[view.id]
        requirements = self.requirements[view.id]
        drafts: list[_Draft] = []
        deferred: list[DeferredEntry] = []
        by_branch: dict[str, list[_Cited]] = defaultdict(list)
        for cited in view.branch_only():
            by_branch[str(cited.branch)].append(cited)
        merge_keys: dict[str, str] = {}
        branch_markers: dict[str, list[_Cited]] = {}
        for name in sorted(by_branch):
            items = by_branch[name]
            branch = self.branches.get(name)
            if branch is None or branch.is_default or branch.is_merged:
                continue
            if not any(c.is_branch_file or c.is_symbol or c.is_branch_commit for c in items):
                continue
            if not branch.is_abandoned:
                deferred.append(DeferredEntry(
                    view.id, view.key, view.title, view.feature.vital_status, DeferralReason.BRANCH_STILL_ACTIVE,
                    f"part of the work is on {name}, which is unmerged but not abandoned; it is left to its owner",
                    tuple(c.ref() for c in items), partial=True,
                ))
                continue
            created: set[str] = set()
            modified: set[str] = set()
            lines = 0
            for cited in items:
                if cited.is_branch_file:
                    change, changed = _patch_stats(cited.citation.snippet)
                    (created if change == "ADDED" else modified).add(cited.path)
                    lines += changed
            if not created and not modified:  # no diff file cited: the files of the cited branch code
                modified = {c.path for c in items if c.is_symbol}
                lines = sum(c.span for c in items if c.is_symbol)
            files = created | modified
            key = f"merge:{view.id}:{name}"
            merge_keys[name] = key
            branch_markers[name] = [c for c in items if c.is_marker]
            merged[name] = branch.commits_behind
            behind_factor = 1.0 / (1.0 + branch.commits_behind / self.params.merge_behind_scale)
            drafts.append(_Draft(
                key=key, kind=PlanItemKind.MERGE_ABANDONED_BRANCH, rank=rank, view=view,
                title=f"Merge {name} into {self.default}",
                rationale=(
                    f"{view.title} is implemented only on {name}: abandoned {branch.dormancy_days:g} days ago, "
                    f"{branch.commits_ahead} commit(s) ahead and {branch.commits_behind} behind {self.default}. "
                    f"Merging it brings {len(files)} file(s) onto {self.default}."
                ),
                evidence=[c.ref() for c in items],
                factors=[self.base(PlanItemKind.MERGE_ABANDONED_BRANCH), FeasibilityFactor(
                    "MERGE_DISTANCE", round(behind_factor, 6),
                    f"{branch.commits_behind} commit(s) behind {self.default}",
                )],
                files_to_modify=modified, files_to_create=created,
                target_tests=sorted(path for path in files if is_test_path(path)),
                branch=name, lines=lines, sort_hint=name,
                constraints=[f"Resolve merge conflicts in favor of {self.default}'s current behavior; "
                             "the merge must not change files outside its diff."],
            ))
        stub_keys: list[str] = []
        regression = [test_node_id(c) for c in view.head_tests()] if view.has_head_code else []
        # Stubs: at HEAD, and on branches this plan merges.
        stub_groups: dict[tuple[str | None, str], list[_Cited]] = defaultdict(list)
        for cited in view.head_stubs():
            stub_groups[(None, cited.path)].append(cited)
        for name in merge_keys:
            for cited in by_branch[name]:
                if cited.is_stub:
                    stub_groups[(name, cited.path)].append(cited)
        stubbed_files: set[tuple[str | None, str]] = set(stub_groups)
        for (branch_name, path), stubs in sorted(stub_groups.items(), key=lambda e: (e[0][0] or "", e[0][1])):
            if branch_name is None:
                markers = [c for c in view.head_markers() if c.path == path]
            else:
                markers = [c for c in branch_markers.get(branch_name, []) if c.path == path]
            names = sorted({n for n in (_symbol_name(c) for c in stubs) if n})
            label = ", ".join(f"`{n}`" for n in names) or f"the stub at {stubs[0].path}:{stubs[0].citation.line_start}"
            where = f"on {branch_name} (after the merge)" if branch_name else f"at {self.default} HEAD"
            key = f"stub:{view.id}:{branch_name or ''}:{path}"
            stub_keys.append(key)
            targets = regression if branch_name is None else [
                p for p in sorted(c.path for c in by_branch[branch_name] if c.is_branch_file) if is_test_path(p)
            ]
            factors = [self.base(PlanItemKind.COMPLETE_STUB)]
            if markers:
                factors.append(self.open_marker_factor(len(markers)))
            factors.extend(self.requirement_factors(requirements))
            drafts.append(_Draft(
                key=key, kind=PlanItemKind.COMPLETE_STUB, rank=rank, view=view,
                title=f"Complete {label} in {path}",
                rationale=(
                    f"{label} in {path} {where} raises NotImplementedError, so {view.title} cannot work. "
                    "The signature stays; the body is implemented."
                ),
                evidence=[c.ref() for c in (*stubs, *markers)],
                factors=factors, files_to_modify={path}, target_tests=list(targets), branch=branch_name,
                lines=max(c.span for c in stubs), sort_hint=path,
                depends_on=[merge_keys[branch_name]] if branch_name else [],
                external_requirements=requirements,
                open_markers=[c.marker_text() for c in markers],
                constraints=["Keep the public signature unchanged.", *self.requirement_constraints(requirements)],
            ))
        # Markers on a merged branch outside stubbed files stay open on the merge item.
        for draft in drafts:
            if draft.kind is PlanItemKind.MERGE_ABANDONED_BRANCH and draft.branch:
                open_markers = [c for c in branch_markers.get(draft.branch, []) if (draft.branch, c.path) not in stubbed_files]
                if open_markers:
                    draft.factors.append(self.open_marker_factor(len(open_markers)))
                    draft.open_markers = [c.marker_text() for c in open_markers]
                if not stub_keys and requirements:
                    draft.factors.extend(self.requirement_factors(requirements))
                    draft.external_requirements = requirements
                    draft.constraints.extend(self.requirement_constraints(requirements))
        # HEAD tests of a feature with no code at HEAD are presumed failing.
        if view.head_tests() and not view.has_head_code:
            tests = view.head_tests()
            modules = {m for m in (self.module_under_test(c.path) for c in tests) if m}
            markers = [c for c in view.head_markers() if c.path in self.known_code or c.path in modules]
            modules |= {c.path for c in markers}
            unresolved = sorted({c.path for c in tests if self.module_under_test(c.path) is None})
            constraints = ["Never edit the target tests: they define the expected behavior."]
            if unresolved:
                constraints.append("No module under test was found for " + ", ".join(unresolved) +
                                   "; the ArchitectureAgent locates it.")
            node_ids = sorted(_unique(test_node_id(c) for c in tests))
            drafts.append(_Draft(
                key=f"fix:{view.id}", kind=PlanItemKind.FIX_FAILING_TEST, rank=rank, view=view,
                title=f"Make {', '.join(node_ids)} pass",
                rationale=(
                    f"{len(node_ids)} test(s) at {self.default} HEAD exercise {view.title}, which has no implementation "
                    f"of its own at HEAD, so they are presumed failing. They pass once the work is wired into "
                    f"{', '.join(sorted(modules)) or 'the module under test'}."
                ),
                evidence=[c.ref() for c in (*tests, *markers)],
                factors=[self.base(PlanItemKind.FIX_FAILING_TEST), *(self.requirement_factors(requirements) if not stub_keys and not merge_keys else [])],
                files_to_modify=modules, target_tests=node_ids,
                lines=sum(c.span for c in tests) + sum(c.span for c in markers), sort_hint="fix",
                depends_on=[*merge_keys.values(), *stub_keys],
                open_markers=[c.marker_text() for c in markers], constraints=constraints,
            ))
        if not drafts:
            if deferred:  # e.g. only an active branch: the whole feature is deferred for that reason
                return drafts, [replace(entry, partial=False) for entry in deferred]
            deferred.append(DeferredEntry(
                view.id, view.key, view.title, view.feature.vital_status, DeferralReason.NO_ACTIONABLE_EVIDENCE,
                "the evidence shows the feature is incomplete but names no stub, abandoned branch or test to act on",
                view.grounding(),
            ))
            return drafts, deferred
        if not any(draft.target_tests for draft in drafts):
            drafts.append(self.add_tests_for(view, rank, [d for d in drafts if d.kind is PlanItemKind.COMPLETE_STUB] or drafts))
        services = [r for r in requirements if r.kind is RequirementKind.EXTERNAL_SERVICE]
        if services:
            matched = {evidence_id for r in services for evidence_id in r.evidence}
            refs = tuple(c.ref() for c in view.cited if c.id in matched)
            deferred.append(DeferredEntry(
                view.id, view.key, view.title, view.feature.vital_status, DeferralReason.NEEDS_EXTERNAL_SERVICE,
                "the live integration with " + ", ".join(r.label for r in services) + " is deferred: the sandbox has "
                "no network. The rest of the feature is restored against an injectable interface.",
                refs or view.grounding(), external_requirements=tuple(services), partial=True,
            ))
        return drafts, deferred

    def add_tests_for(self, view: _FeatureView, rank: int, covered: Sequence[_Draft]) -> _Draft:
        sources = sorted({path for draft in covered for path in (*draft.files_to_modify, *draft.files_to_create)
                          if not is_test_path(path) and path.endswith(".py")})
        directory = self.test_dir()
        files = sorted({f"{directory}/test_{PurePosixPath(path).stem}.py" for path in sources}) or [
            f"{directory}/test_{re.sub(r'[^a-z0-9]+', '_', view.title.lower()).strip('_')[:40]}.py"
        ]
        existing = {path for path in files if path in self.known_files}
        evidence = [ref for draft in covered for ref in draft.evidence if ref.source_type in (
            EvidenceSourceType.AST_IMPLEMENTATION, EvidenceSourceType.UNMERGED_BRANCH_COMMIT, EvidenceSourceType.ISSUE_SPEC,
        )] or [ref for draft in covered for ref in draft.evidence]
        return _Draft(
            key=f"tests:{view.id}", kind=PlanItemKind.ADD_TESTS, rank=rank, view=view,
            title=f"Add tests for {view.title}",
            rationale=f"No test targets {view.title}, so the Foundry could not verify the change. "
                      f"New tests cover {', '.join(sources) or 'the new code'}.",
            evidence=list(_dedupe_refs(evidence)), factors=[self.base(PlanItemKind.ADD_TESTS)],
            files_to_modify=existing, files_to_create=set(files) - existing, target_tests=files,
            sort_hint="tests", depends_on=[draft.key for draft in covered],
            constraints=["Tests run offline and deterministically (no network, no sleeps, no wall clock)."],
        )

    def restore_ghost(self, view: _FeatureView) -> tuple[list[_Draft], list[DeferredEntry]]:
        specs = view.specs()
        status = view.feature.vital_status
        if not specs:
            return [], [DeferredEntry(
                view.id, view.key, view.title, status, DeferralReason.NO_DETAILED_SPEC,
                "only claims describe this feature; no design document says how it should work",
                view.grounding(),
            )]
        named = sorted({m for c in specs for m in _SPEC_PATH.findall(c.citation.snippet) if "://" not in m})
        requirements = self.requirements[view.id]
        if requirements:
            services = [r for r in requirements if r.kind is RequirementKind.EXTERNAL_SERVICE]
            reason = DeferralReason.NEEDS_EXTERNAL_SERVICE if services else DeferralReason.NEEDS_NEW_DEPENDENCY
            parts = []
            for r in requirements:
                if r.kind is RequirementKind.THIRD_PARTY_DEPENDENCY:
                    parts.append(f"{r.label} (none of {', '.join(r.candidates)} is declared)")
                else:
                    parts.append(f"{r.label} (unreachable from the sandbox)")
            return [], [DeferredEntry(
                view.id, view.key, view.title, status, reason,
                "building it from the spec needs " + "; ".join(parts) + ", outside the current stack",
                view.grounding(), external_requirements=requirements, would_touch=tuple(named),
            )]
        rank = self.rank[view.id]
        modify = {p for p in named if p in self.known_files}
        create = set(named) - modify
        constraints = ["Implement only what the cited spec describes."]
        if not named:
            constraints.append("The spec names no files; the ArchitectureAgent chooses the module.")
        draft = _Draft(
            key=f"spec:{view.id}", kind=PlanItemKind.IMPLEMENT_FROM_SPEC, rank=rank, view=view,
            title=f"Implement {view.title} from its spec",
            rationale=f"{view.title} has no code anywhere, but {len(specs)} design section(s) describe it.",
            evidence=[c.ref() for c in (*specs, *view.claims())], factors=[self.base(PlanItemKind.IMPLEMENT_FROM_SPEC)],
            files_to_modify=modify, files_to_create=create, lines=sum(c.span for c in specs), sort_hint="spec",
            constraints=constraints,
        )
        return [draft, self.add_tests_for(view, rank, [draft])], []

    # MODERNIZE -------------------------------------------------------------
    def upgrades(self) -> list[_Draft]:
        drafts: list[_Draft] = []
        cited_at: dict[tuple[str, int | None], list[EvidenceRef]] = defaultdict(list)
        for view in self.views:
            for cited in view.cited:
                if cited.is_dependency:
                    cited_at[(cited.path, cited.citation.line_start)].append(cited.ref())
        seen: set[tuple[str, str]] = set()
        for dep in self.dependencies:
            if not dep.is_pinned or (dep.file_path, dep.normalized_name) in seen:
                continue
            seen.add((dep.file_path, dep.normalized_name))
            ref = EvidenceRef(
                ref=f"dependency:{dep.file_path}:{dep.line_start}", kind=EvidenceKind.DEPENDENCY,
                source_type=EvidenceSourceType.ISSUE_SPEC.value, file_path=dep.file_path, line_start=dep.line_start,
                line_end=dep.line_end, branch_name=self.default, artifact_id=dep.artifact_id,
            )
            drafts.append(_Draft(
                key=f"upgrade:{dep.file_path}:{dep.normalized_name}", kind=PlanItemKind.UPGRADE_DEPENDENCY, rank=-1,
                view=None, title=f"Upgrade {dep.name} (pinned {dep.specifier or dep.pinned_version})",
                rationale=f"{dep.file_path}:{dep.line_start} pins {dep.name} to exactly {dep.pinned_version}. "
                          "An exact pin from a dormant project is the first thing to break on a current runtime.",
                evidence=[ref, *cited_at.get((dep.file_path, dep.line_start), [])],
                factors=[self.base(PlanItemKind.UPGRADE_DEPENDENCY)], files_to_modify={dep.file_path}, lines=1,
                sort_hint=f"{dep.file_path}:{dep.line_start or 0:06d}:{dep.normalized_name}",
                requires_resolution=True,
                dependency=DependencyChange(
                    name=dep.name, ecosystem=dep.ecosystem, manifest=dep.manifest, file_path=dep.file_path,
                    line_start=dep.line_start, current_specifier=dep.specifier, current_version=str(dep.pinned_version),
                ),
                constraints=["Do not choose the version here: the EnvironmentAgent resolves the newest release "
                             "compatible with the sandbox runtime and the test suite, then pins it."],
            ))
        return drafts

    # EXTRACT ---------------------------------------------------------------
    def clusters(self) -> list[tuple[_FeatureView, ...]]:
        verified = list(self.verified)
        parent = {view.id: view.id for view in verified}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        if self.imports:
            owner = {path: view.id for view in verified for path in view.own_code_files()}
            for view in verified:
                for path in view.own_code_files():
                    for target in self.imports.internal.get(path, frozenset()):
                        other = owner.get(target)
                        if other and other != view.id:
                            a, b = find(view.id), find(other)
                            if a != b:
                                parent[max(a, b)] = min(a, b)
        groups: dict[str, list[_FeatureView]] = defaultdict(list)
        for view in verified:
            groups[find(view.id)].append(view)
        ordered = [
            tuple(sorted(members, key=lambda v: (-v.feature.confidence_score, v.title.casefold(), v.id)))
            for members in groups.values()
        ]
        return sorted(ordered, key=self.cluster_key)

    def outside_files(self, cluster: Sequence[_FeatureView]) -> tuple[str, ...]:
        if not self.imports:
            return ()
        inside = {p for v in cluster for p in (*v.own_code_files(), *v.own_test_files())}
        return tuple(sorted(p for p in self.imports.closure(inside) - inside if not is_test_path(p)))

    def cluster_key(self, cluster: Sequence[_FeatureView]) -> tuple[Any, ...]:
        value = round(sum(v.feature.confidence_score for v in cluster), FEASIBILITY_DECIMALS)
        citations = sum(len(v.feature.citations) for v in cluster)
        return (-value, -citations, len(self.outside_files(cluster)), cluster[0].title.casefold(), cluster[0].id)

    def extract(self) -> tuple[list[_Draft], list[DeferredEntry], tuple[str, ...], str | None, int]:
        clusters = self.clusters()
        deferred: list[DeferredEntry] = []
        if not clusters:
            for view in self.views:
                deferred.append(DeferredEntry(
                    view.id, view.key, view.title, view.feature.vital_status, DeferralReason.NOT_VERIFIED_WORKING,
                    "only VERIFIED_WORKING features are extracted, and the genome has none", view.grounding(),
                ))
            return [], deferred, (), None, 0
        chosen = [v for v in clusters[0] if v.own_code_files()]
        if not chosen:
            chosen = list(clusters[0])
        chosen_ids = {v.id for v in chosen}
        chosen_value = round(sum(v.feature.confidence_score for v in chosen), FEASIBILITY_DECIMALS)
        cluster_value = {
            v.id: round(sum(m.feature.confidence_score for m in cluster), FEASIBILITY_DECIMALS)
            for cluster in clusters for v in cluster
        }
        for view in self.views:
            if view.id in chosen_ids:
                continue
            if view.feature.vital_status == VitalStatus.VERIFIED_WORKING:
                deferred.append(DeferredEntry(
                    view.id, view.key, view.title, view.feature.vital_status, DeferralReason.OUTSIDE_SELECTED_CLUSTER,
                    f"verified, but its cluster is worth less: sum of C(f) {cluster_value[view.id]} "
                    f"against {chosen_value} for the selected cluster",
                    view.grounding(),
                ))
            else:
                deferred.append(DeferredEntry(
                    view.id, view.key, view.title, view.feature.vital_status, DeferralReason.NOT_VERIFIED_WORKING,
                    f"only VERIFIED_WORKING features are extracted; this one is {view.feature.vital_status}",
                    view.grounding(),
                ))
        code_files = [p for v in chosen for p in v.own_code_files()]
        tops = {PurePosixPath(p).parts[0] for p in code_files if len(PurePosixPath(p).parts) > 1}
        package = sorted(tops)[0] if len(tops) == 1 else None
        stems = _unique(PurePosixPath(p).stem for p in code_files)
        slug = "-".join([*([package] if package else []), *stems])[:80] or "extract"
        root = f"{self.params.extract_root}/{slug}"
        owners = {path: v for v in self.views for path in v.own_code_files()}
        drafts: list[_Draft] = []
        assigned: set[str] = set()
        all_copied_tests: list[str] = []
        for position, view in enumerate(chosen):
            sources = [*view.own_code_files(), *view.own_test_files()]
            outside: list[str] = []
            if self.imports:
                reach = self.imports.closure(sources)
                inside = {p for v in chosen for p in (*v.own_code_files(), *v.own_test_files())}
                outside = sorted(p for p in reach - inside - assigned if not is_test_path(p))
                assigned.update(outside)
            copies = [FileCopy(path, f"{root}/{path}") for path in sorted({*sources, *outside})]
            create = {copy.destination for copy in copies}
            if position == 0:
                create.add(f"{root}/pyproject.toml")
                package_dirs = sorted({str(PurePosixPath(p).parent) for p in (*code_files, *(
                    p for v in chosen for p in v.own_test_files())) if str(PurePosixPath(p).parent) != "."})
                for directory in package_dirs:
                    init = f"{directory}/__init__.py"
                    if init in self.known_files or (self.imports and init in self.imports.files):
                        copies.append(FileCopy(init, f"{root}/{init}"))
                        create.add(f"{root}/{init}")
                    elif not is_test_path(init):
                        create.add(f"{root}/{init}")
            copied_tests = sorted(c.destination for c in copies if is_test_path(c.source) and not c.source.endswith("__init__.py"))
            all_copied_tests.extend(copied_tests)
            factors = [self.base(PlanItemKind.EXTRACT_MODULE)]
            constraints = [
                "Copy, do not move: the original repository keeps working unchanged.",
                "Keep import paths unchanged inside the package so the copied tests run as they are.",
            ]
            if outside:
                factors.append(FeasibilityFactor(
                    "BOUNDARY_LEAK", round(self.params.boundary_leak_factor ** len(outside), 6),
                    f"{len(outside)} file(s) outside the cluster are pulled in by imports",
                ))
                for path in outside:
                    owner = owners.get(path)
                    note = f" (owned by '{owner.title}', {owner.feature.vital_status})" if owner else ""
                    constraints.append(f"{path}{note} comes along because the cluster imports it; copy it unchanged "
                                       "and keep only what the cluster uses.")
            externals = sorted({m for p in sources for m in (self.imports.external.get(p, ()) if self.imports else ())})
            if position == 0 or externals:
                constraints.append("pyproject.toml declares " + (", ".join(externals) if externals else
                                   "no third-party runtime dependencies") + " for these modules.")
            evidence = [c.ref() for c in view.head() if (c.is_code and c.path in view.own_code_files())
                        or (c.is_test and c.path in view.own_test_files())] or list(view.grounding())
            drafts.append(_Draft(
                key=f"extract:{view.id}", kind=PlanItemKind.EXTRACT_MODULE, rank=position, view=view,
                title=f"Extract {', '.join(view.own_code_files()) or view.title} into {root}",
                rationale=(
                    f"{view.title} is VERIFIED_WORKING (C(f) {round(view.feature.confidence_score, 4)}) and belongs to "
                    f"the highest-value cluster of verified features ({', '.join(v.title for v in chosen)})."
                ),
                evidence=evidence, factors=factors, files_to_create=create, target_tests=copied_tests,
                lines=sum(c.span for c in view.head() if c.is_code and c.path in view.own_code_files()),
                sort_hint=f"{position:03d}", depends_on=[drafts[0].key] if drafts else [],
                copies=sorted(copies, key=lambda c: c.destination), constraints=constraints,
            ))
        boundary_test = f"{root}/tests/test_public_api.py"
        drafts.append(_Draft(
            key="extract:tests", kind=PlanItemKind.ADD_TESTS, rank=len(chosen), view=chosen[0],
            title=f"Add a boundary test for {root}",
            rationale="The extract must import and run on its own: a boundary test imports every extracted module in "
                      "a clean environment without the rest of the repository.",
            evidence=list(_dedupe_refs(ref for d in drafts for ref in d.evidence if ref.source_type ==
                                       EvidenceSourceType.AST_IMPLEMENTATION and not is_test_path(ref.file_path)))
            or list(drafts[0].evidence),
            factors=[self.base(PlanItemKind.ADD_TESTS)], files_to_create={boundary_test},
            target_tests=sorted({*all_copied_tests, boundary_test}), sort_hint="tests",
            depends_on=[d.key for d in drafts],
            constraints=["Install the extracted package (pip install -e) in the sandbox before running its tests."],
        ))
        return drafts, deferred, tuple(v.id for v in chosen), root, len(assigned)


def _finalize(strategy: StrategyType, drafts: Sequence[_Draft], params: PlannerParameters) -> tuple[PlanItem, ...]:
    ordered = sorted(
        drafts,
        key=lambda d: (_STAGE_ORDER[KIND_STAGE[d.kind]], d.rank, _KIND_ORDER[d.kind], d.sort_hint, d.key),
    )
    ids = {draft.key: f"{strategy.value.lower()}-{index:02d}" for index, draft in enumerate(ordered, start=1)}
    items: list[PlanItem] = []
    for draft in ordered:
        files = len({*draft.files_to_modify, *draft.files_to_create})
        if files <= params.effort_small_files and draft.lines <= params.effort_small_lines:
            effort = Effort.S
        elif files <= params.effort_medium_files and draft.lines <= params.effort_medium_lines:
            effort = Effort.M
        else:
            effort = Effort.L
        view = draft.view
        items.append(PlanItem(
            id=ids[draft.key],
            kind=draft.kind,
            stage=KIND_STAGE[draft.kind],
            feature_id=view.id if view else None,
            feature_key=view.key if view else None,
            title=draft.title,
            rationale=draft.rationale,
            files_to_modify=tuple(sorted(draft.files_to_modify)),
            files_to_create=tuple(sorted(draft.files_to_create - draft.files_to_modify)),
            target_tests=_unique(draft.target_tests),
            branch=draft.branch,
            evidence=_dedupe_refs(draft.evidence),
            effort=effort,
            item_feasibility=draft.feasibility,
            feasibility_factors=tuple(draft.factors),
            depends_on=tuple(ids[key] for key in _unique(draft.depends_on)),
            requires_resolution=draft.requires_resolution,
            dependency=draft.dependency,
            external_requirements=tuple(draft.external_requirements),
            open_markers=_unique(draft.open_markers),
            constraints=_unique(draft.constraints),
            copies=tuple(draft.copies),
            changed_lines=draft.lines,
        ))
    return tuple(items)


def _scope(planner: _Planner, items: Sequence[PlanItem], deferred: Sequence[DeferredEntry]) -> tuple[ScopeEntry, ...]:
    by_feature: dict[str, list[PlanItem]] = defaultdict(list)
    for item in items:
        if item.feature_id:
            by_feature[item.feature_id].append(item)
    partial = {entry.feature_id for entry in deferred if entry.partial}
    entries = []
    for view in planner.views:
        feature_items = by_feature.get(view.id)
        if not feature_items:
            continue
        entries.append(ScopeEntry(
            feature_id=view.id, feature_key=view.key, title=view.title, vital_status=str(view.feature.vital_status),
            feasibility=min(item.item_feasibility for item in feature_items),
            item_ids=tuple(item.id for item in feature_items), partial=view.id in partial,
        ))
    return tuple(entries)


def _count(items: Sequence[PlanItem], kind: PlanItemKind) -> int:
    return sum(1 for item in items if item.kind is kind)


def plan_strategies(
    genome: GenomeInputs,
    branches: Sequence[BranchInput] | None = None,
    *,
    dependencies: Sequence[DependencyInput] = (),
    imports: ImportGraph | None = None,
    params: PlannerParameters = DEFAULT_PLANNER_PARAMETERS,
) -> tuple[StrategyPlan, StrategyPlan, StrategyPlan]:
    """The ``RESTORE``, ``MODERNIZE`` and ``EXTRACT`` plans for one genome (see the module docstring)."""
    planner = _Planner(genome, genome.branches if branches is None else branches, dependencies, imports, params)
    name = planner.project.name or "the project"
    total = len(planner.views)
    verified_ids = tuple(v.id for v in planner.verified)

    restore_drafts, restore_deferred, merged = planner.restore()
    restore_items = _finalize(StrategyType.RESTORE, restore_drafts, params)
    restore_scope = _scope(planner, restore_items, restore_deferred)
    restore_basis = ValueBasis(
        ValueMode.PROJECTED_VERIFIED, total, verified_ids,
        targets=tuple(ValueTarget(e.feature_id, e.title, e.feasibility) for e in restore_scope),
    )
    merge_distance = sum(merged.values())

    def restore_summary(items: Sequence[PlanItem]) -> str:
        parts = [
            f"merge {_count(items, PlanItemKind.MERGE_ABANDONED_BRANCH)} abandoned branch(es)",
            f"complete {_count(items, PlanItemKind.COMPLETE_STUB)} stub(s)",
            f"fix {_count(items, PlanItemKind.FIX_FAILING_TEST)} failing test group(s)",
            f"implement {_count(items, PlanItemKind.IMPLEMENT_FROM_SPEC)} feature(s) from spec",
        ]
        return (f"Targets {len(restore_scope)} of {total} feature(s): " + ", ".join(parts) +
                f". {len(restore_deferred)} deferral(s).")

    restore = StrategyPlan(
        StrategyType.RESTORE, f"Restore {name} in its current stack", restore_summary(restore_items),
        restore_items, restore_scope, tuple(restore_deferred), restore_basis,
        {RiskFactor.MERGE_DISTANCE.value: merge_distance},
    )

    upgrades = planner.upgrades()
    modernize_items = _finalize(StrategyType.MODERNIZE, [*restore_drafts, *upgrades], params)
    modernize_scope = _scope(planner, modernize_items, restore_deferred)
    modernize = StrategyPlan(
        StrategyType.MODERNIZE, f"Restore {name} and upgrade its pinned dependencies",
        restore_summary(modernize_items) + f" Upgrade {len(upgrades)} exact pin(s), versions resolved in the sandbox.",
        modernize_items, modernize_scope, tuple(restore_deferred),
        ValueBasis(ValueMode.PROJECTED_VERIFIED, total, verified_ids,
                   targets=tuple(ValueTarget(e.feature_id, e.title, e.feasibility) for e in modernize_scope)),
        {RiskFactor.MERGE_DISTANCE.value: merge_distance, RiskFactor.PINNED_UPGRADES.value: len(upgrades)},
    )

    extract_drafts, extract_deferred, extracted, root, outside = planner.extract()
    extract_items = _finalize(StrategyType.EXTRACT, extract_drafts, params)
    titles = [v.title for v in planner.views if v.id in set(extracted)]
    extract = StrategyPlan(
        StrategyType.EXTRACT,
        f"Extract {name}'s verified core into {root}" if root else f"Nothing in {name} can be extracted yet",
        (f"Copies {len(extracted)} of {len(verified_ids)} verified feature(s) ({'; '.join(titles)}) into a "
         f"standalone package with their tests; {outside} file(s) come along through imports.")
        if root else "The genome has no VERIFIED_WORKING feature to extract.",
        extract_items, _scope(planner, extract_items, ()), tuple(extract_deferred),
        ValueBasis(ValueMode.EXTRACT_SHARE, total, verified_ids, extracted_feature_ids=extracted),
        {RiskFactor.EXTRACT_BOUNDARY.value: outside}, extract_root=root,
    )
    return restore, modernize, extract
