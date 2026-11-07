"""The ``.kiro/specs/<spec_name>/`` recovery bundle of one revival strategy, and its format validator.

Pure: no ORM, no Git, no network, no model calls. The bundle is what the
resurrected repository inherits and what the Foundry executes, so it is a
valid Kiro spec in the same format as this repository's own
``.kiro/specs/core-resurrection-engine/*.md``:

* ``requirements.md``: ``## Introduction``, then ``### Requirement N: <title>``
  blocks with a ``**User Story:**`` line and numbered EARS acceptance
  criteria (``WHEN``/``IF``/``WHILE``/``WHERE`` ... ``THE <system> SHALL`` ...).
  One requirement per in-scope feature, one for project-wide dependency
  upgrades when the plan has any, and a final cross-cutting "Verification"
  requirement.
* ``design.md``: Overview (with the Score(S_k) breakdown), Change Set (one
  section per plan item), Merge Plan, Verification Strategy, Risks and Out of
  Scope (every deferred feature with its reason).
* ``tasks.md``: ``- [ ] 1. ...`` tasks with ``  - [ ] 1.1 ...`` sub-tasks,
  one task per plan item in ``depends_on`` order, each naming its files and
  target tests and ending with a ``Verify:`` sub-task, then a final
  verification task.

Evidence footnotes
    Every requirement, task, change-set entry and deferral cites its evidence
    as Markdown footnotes (``[^E3]``). Labels are assigned in order of first
    use and are the same in all three files; each file defines the footnotes
    it uses, and ``SpecBundle.evidence_index`` maps every label to the plan's
    ``EvidenceRef`` (citation id, file, lines, branch, artifact id, and the
    cited snippet when the genome has it). This carries the zero-uncited-claims
    invariant (forensic-invariants.md section 1) into the exported spec.

Untrusted text
    Titles, rationales, constraints and snippets come from the repository and
    may contain Markdown. ``inline_text`` flattens them to one line, strips
    leading list/heading/checkbox markers, drops backticks and escapes
    backslashes, brackets, angle brackets and pipes, so they can never open a
    code span, a footnote, a checkbox, a list item or HTML. Only the renderer
    writes code spans (file paths, test ids, branches, commands) and footnote
    references.

Structure versus prose
    ``build_spec_skeleton`` fixes the structure: requirement and task ids,
    acceptance criteria, files, tests and evidence labels. The only free text
    is the introduction, each requirement's user story, the design overview
    and each task's description (``SpecSkeleton.with_prose``). The optional
    model refinement (``apps.revival.agents``) may rewrite exactly those, and
    ``render_skeleton`` re-renders the Markdown from the skeleton.

``validate_spec_bundle`` checks the format of all three files and raises
``SpecValidationError`` (listing every problem) when it is broken.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from apps.genome.graph import BranchInput, CitationInput, FeatureInput, GenomeInputs
from apps.genome.models import EvidenceSourceType, VitalStatus
from apps.revival.models import StrategyType
from apps.revival.planner import (
    EvidenceKind,
    EvidenceRef,
    PlanItem,
    PlanItemKind,
    RequirementKind,
    StrategyPlan,
    is_manifest_path,
)
from apps.revival.scoring import PlanScore

FULL_SUITE_COMMAND: str = "python -m pytest"
MAX_SELF_HEALING_ITERATIONS: int = 3  # Requirement 4.2
SPEC_NAME_PREFIX: str = "lazarus"
SPEC_NAME_MAX_CHARS: int = 64
EVIDENCE_SNIPPET_CHARS: int = 300
CRITERION_EVIDENCE_LIMIT: int = 3
SPEC_QUOTE_CHARS: int = 160
MAX_SPEC_CRITERIA_PER_FEATURE: int = 2
LOW_FEASIBILITY: float = 0.7

DESIGN_SECTIONS: tuple[str, ...] = (
    "Overview", "Change Set", "Merge Plan", "Verification Strategy", "Risks", "Out of Scope",
)
EARS_KEYWORDS: tuple[str, ...] = ("WHEN", "IF", "WHILE", "WHERE", "THE")
# Extensions that make a code-span or prose token a file reference.
FILE_EXTENSIONS: frozenset[str] = frozenset({
    "py", "pyi", "md", "rst", "txt", "toml", "cfg", "ini", "json", "yaml", "yml", "lock",
    "js", "jsx", "ts", "tsx", "mjs", "cjs", "go", "rs", "java", "kt", "rb", "php", "cs",
    "c", "h", "cpp", "hpp", "sh", "html", "css", "scss", "sql", "xml",
})

_ESCAPED = re.compile(r"([\\\[\]<>|])")
_WHITESPACE = re.compile(r"\s+")
_LEADING_MARKERS = re.compile(r"^(?:[-*+>#]+\s*|\d+[.)]\s+|\[[ xX~-]?\]\s*)+")
_BACKTICKED = re.compile(r"`([^`]+)`")
_SLUG = re.compile(r"[^a-z0-9]+")
_FILE_TOKEN = re.compile(r"^(?P<path>[\w.\-/]+\.(?P<ext>[A-Za-z0-9]{1,5}))(?::\d+(?:-\d+)?)?$")
_PROSE_FILE = re.compile(r"(?<![\w.\-/])[\w.\-/]*[\w\-]\.(?P<ext>[A-Za-z0-9]{1,5})(?![\w/])")


class SpecValidationError(ValueError):
    """A spec bundle breaks the Kiro spec format; ``problems`` lists every issue found."""

    def __init__(self, spec_name: str, problems: Sequence[str]) -> None:
        self.spec_name = spec_name
        self.problems = tuple(problems)
        super().__init__(f"spec {spec_name!r} is invalid: " + "; ".join(self.problems[:10]))


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def inline_text(text: str | None, limit: int | None = None) -> str:
    """One line of untrusted text, safe to embed in Markdown (see the module docstring)."""
    # Backticks are dropped rather than escaped: planner titles quote symbols as `name`, and plain text reads better.
    flat = str(text or "").replace("`", "")
    flat = "".join(ch for ch in _WHITESPACE.sub(" ", flat) if ch.isprintable()).strip()
    flat = _LEADING_MARKERS.sub("", flat).strip()
    if limit is not None and len(flat) > limit:
        cut = flat[: max(1, limit - 1)]
        if " " in cut[limit // 2:]:
            cut = cut[: cut.rindex(" ")]
        flat = cut.rstrip() + "…"
    return _ESCAPED.sub(r"\\\1", flat)


def code(text: str) -> str:
    """A code span written by the renderer (backticks inside become apostrophes)."""
    return "`" + " ".join(str(text).replace("`", "'").split()) + "`"


def codes(values: Iterable[str]) -> str:
    return ", ".join(code(value) for value in values)


def slugify(text: str, fallback: str = "project") -> str:
    return _SLUG.sub("-", text.lower()).strip("-") or fallback


def spec_name_for(strategy_type: StrategyType | str, project_name: str) -> str:
    """``lazarus-<strategy>-<project slug>``, at most ``SPEC_NAME_MAX_CHARS`` characters."""
    name = f"{SPEC_NAME_PREFIX}-{StrategyType(strategy_type).value.lower()}-{slugify(project_name)}"
    return name[:SPEC_NAME_MAX_CHARS].rstrip("-")


# (Not named test_*: test modules that import them must not collect them.)
def node_file(test_id: str) -> str:
    """``tests/test_x.py::test_y`` -> ``tests/test_x.py``."""
    return test_id.split("::", 1)[0]


def scenario_of(test_id: str) -> str:
    """``tests/test_x.py::test_dispatch_retries`` -> ``dispatch retries``."""
    name = test_id.rsplit("::", 1)[-1] if "::" in test_id else PurePosixPath(test_id).stem
    return re.sub(r"^test_?", "", name).replace("_", " ").strip() or name


def location_of(path: str, start: int | None, end: int | None) -> str:
    if start is None:
        return path
    return f"{path}:{start}" if end in (None, start) else f"{path}:{start}-{end}"


def prose_file_mentions(text: str) -> list[str]:
    """Path-like tokens with a known file extension in free text (``README.md``, ``pkg/a.py``)."""
    return [m.group(0) for m in _PROSE_FILE.finditer(text) if m.group("ext").lower() in FILE_EXTENSIONS]


def unknown_file_mentions(text: str, known_files: Collection[str]) -> list[str]:
    """File mentions in ``text`` that are neither a known path nor a known path's trailing segments."""
    unknown = []
    for token in prose_file_mentions(text.replace("\\", "")):
        if token in known_files or any(path.endswith("/" + token) for path in known_files):
            continue
        unknown.append(token)
    return unknown


def code_span_files(markdown: str) -> list[str]:
    """File paths referenced inside code spans (test ids and ``path:line`` suffixes reduced to the path)."""
    found = []
    for span in _BACKTICKED.findall(markdown):
        for token in span.split():
            match = _FILE_TOKEN.match(token.split("::", 1)[0])
            if match and match.group("ext").lower() in FILE_EXTENSIONS:
                found.append(match.group("path"))
    return found


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EvidenceEntry:
    """One footnote: a plan ``EvidenceRef`` plus the cited snippet when the genome has it."""

    label: str
    ref: str
    kind: str
    source_type: str
    file_path: str
    line_start: int | None
    line_end: int | None
    branch_name: str | None
    citation_id: str | None
    artifact_id: int | None
    snippet: str = ""

    @property
    def location(self) -> str:
        return location_of(self.file_path, self.line_start, self.line_end)

    def footnote(self) -> str:
        where = code(self.location) if self.file_path else "(no file)"
        parts = [self.source_type]
        if self.branch_name:
            parts.append(f"on {code(self.branch_name)}")
        parts.append(f"citation {code(self.citation_id)}" if self.citation_id else f"ref {code(self.ref)}")
        return f"[^{self.label}]: {where} · " + " · ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "ref": self.ref, "kind": self.kind, "source_type": self.source_type,
            "file_path": self.file_path, "line_start": self.line_start, "line_end": self.line_end,
            "location": self.location, "branch_name": self.branch_name, "citation_id": self.citation_id,
            "artifact_id": self.artifact_id, "snippet": self.snippet,
        }


@dataclass(frozen=True)
class Criterion:
    text: str  # Markdown; starts with an EARS keyword
    evidence: tuple[str, ...] = ()  # footnote labels


@dataclass(frozen=True)
class SpecRequirement:
    id: str  # "1", "2", ...
    title: str  # Markdown-safe
    user_story: str  # Markdown-safe prose (rewritable)
    criteria: tuple[Criterion, ...]
    feature_id: str | None = None
    item_ids: tuple[str, ...] = ()

    @property
    def evidence(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(label for c in self.criteria for label in c.evidence))


@dataclass(frozen=True)
class SpecTask:
    id: str  # "1", "2", ...; sub-task ids are "<id>.<n>"
    title: str  # Markdown-safe
    description: str  # Markdown-safe prose (rewritable)
    details: tuple[str, ...]  # Markdown detail bullets
    subtasks: tuple[str, ...]  # the last one starts with "Verify:"
    evidence: tuple[str, ...]
    item_id: str | None = None
    requirement_ids: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    target_tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecSkeleton:
    """Everything a bundle is rendered from. Structure is fixed; only four kinds of prose are rewritable."""

    strategy_type: StrategyType
    spec_name: str
    title: str  # Markdown-safe
    introduction: str  # prose
    design_overview: str  # prose
    overview_facts: tuple[str, ...]  # Markdown bullets under the overview
    requirements: tuple[SpecRequirement, ...]
    tasks: tuple[SpecTask, ...]
    design_sections: Mapping[str, tuple[str, ...]]  # section -> Markdown lines
    evidence: tuple[EvidenceEntry, ...]
    known_files: frozenset[str]

    def with_prose(
        self,
        *,
        introduction: str | None = None,
        design_overview: str | None = None,
        user_stories: Mapping[str, str] | None = None,
        task_descriptions: Mapping[str, str] | None = None,
    ) -> SpecSkeleton:
        """A copy with the given (already sanitized) prose; ids not in the skeleton are ignored."""
        stories = user_stories or {}
        descriptions = task_descriptions or {}
        return replace(
            self,
            introduction=introduction or self.introduction,
            design_overview=design_overview or self.design_overview,
            requirements=tuple(replace(r, user_story=stories.get(r.id) or r.user_story) for r in self.requirements),
            tasks=tuple(replace(t, description=descriptions.get(t.id) or t.description) for t in self.tasks),
        )


@dataclass(frozen=True)
class SpecBundle:
    spec_name: str
    requirements_md: str
    design_md: str
    tasks_md: str
    evidence_index: Mapping[str, Mapping[str, Any]]  # footnote label -> EvidenceEntry.as_dict()
    known_files: frozenset[str] = field(default_factory=frozenset, compare=False)

    @property
    def documents(self) -> dict[str, str]:
        return {"requirements.md": self.requirements_md, "design.md": self.design_md, "tasks.md": self.tasks_md}

    def as_storage(self, *, mode: str, generated_at: str) -> dict[str, Any]:
        """``RevivalStrategy.generated_kiro_specs``."""
        return {
            "spec_name": self.spec_name,
            "requirements_md": self.requirements_md,
            "design_md": self.design_md,
            "tasks_md": self.tasks_md,
            "evidence_index": {label: dict(entry) for label, entry in self.evidence_index.items()},
            "mode": mode,
            "generated_at": generated_at,
        }


# ---------------------------------------------------------------------------
# Skeleton
# ---------------------------------------------------------------------------
class _Evidence:
    """Assigns footnote labels (``E1``, ``E2``, ...) in order of first use."""

    def __init__(self, citations: Mapping[str, CitationInput]) -> None:
        self.citations = citations
        self.entries: dict[str, EvidenceEntry] = {}

    def label(self, ref: EvidenceRef) -> str:
        entry = self.entries.get(ref.ref)
        if entry is None:
            citation = self.citations.get(ref.citation_id or ref.ref)
            entry = EvidenceEntry(
                label=f"E{len(self.entries) + 1}", ref=ref.ref, kind=EvidenceKind(ref.kind).value,
                source_type=ref.source_type, file_path=ref.file_path, line_start=ref.line_start,
                line_end=ref.line_end, branch_name=ref.branch_name, citation_id=ref.citation_id,
                artifact_id=ref.artifact_id, snippet=citation.snippet[:EVIDENCE_SNIPPET_CHARS] if citation else "",
            )
            self.entries[ref.ref] = entry
        return entry.label

    def labels(self, refs: Iterable[EvidenceRef], limit: int | None = None) -> tuple[str, ...]:
        """Labels of ``refs`` (deduplicated); only the first ``limit`` are registered and returned."""
        found: list[str] = []
        for ref in refs:
            if limit is not None and len(found) >= limit:
                break
            label = self.label(ref)
            if label not in found:
                found.append(label)
        return tuple(found)


def refs_markdown(labels: Iterable[str]) -> str:
    return " ".join(f"[^{label}]" for label in labels)


def citation_ref(citation: CitationInput) -> EvidenceRef:
    return EvidenceRef(
        ref=citation.id, kind=EvidenceKind.CITATION, source_type=citation.source_type, file_path=citation.file_path,
        line_start=citation.line_start, line_end=citation.line_end, branch_name=citation.branch_name,
        citation_id=citation.id, artifact_id=citation.artifact_id,
    )


def ordered_items(items: Sequence[PlanItem]) -> list[PlanItem]:
    """Plan items in ``depends_on`` order: the first item (in plan order) whose dependencies are done goes next."""
    remaining = list(items)
    known = {item.id for item in items}
    done: set[str] = set()
    ordered: list[PlanItem] = []
    while remaining:
        ready = next(
            (i for i in remaining if all(dep in done or dep not in known for dep in i.depends_on)), remaining[0]
        )
        remaining.remove(ready)
        done.add(ready.id)
        ordered.append(ready)
    return ordered


def _symbols(item: PlanItem) -> list[str]:
    """Symbol names the planner put in backticks in the item title (``Complete `convert` in ...``)."""
    return _BACKTICKED.findall(item.title)


def _claim_text(citation: CitationInput) -> str:
    first = citation.snippet.strip().splitlines()[0] if citation.snippet.strip() else ""
    return re.sub(r"^\s*[-*+]\s*(\[[ xX]\]\s*)?", "", first).strip()


def _spec_quote(citation: CitationInput) -> str:
    lines = [line.strip() for line in citation.snippet.splitlines()]
    body = [line for line in lines if line and not line.startswith("#")]
    return " ".join(body) if body else (lines[0] if lines else "")


class _SkeletonBuilder:
    def __init__(self, plan: StrategyPlan, score: PlanScore, genome: GenomeInputs) -> None:
        self.plan = plan
        self.score = score
        self.project = genome.project.name or "the project"
        self.system = f"{inline_text(self.project)} system"
        self.default = genome.project.default_branch or "main"
        self.features: dict[str, FeatureInput] = {f.id: f for f in genome.features}
        self.branches: dict[str, BranchInput] = {b.name: b for b in genome.branches}
        self.evidence = _Evidence({c.id: c for f in genome.features for c in f.citations})
        self.items = ordered_items(plan.action_plan)
        self.task_of = {item.id: str(index) for index, item in enumerate(self.items, start=1)}

    def known_files(self) -> frozenset[str]:
        files: set[str] = set(self.plan.files)
        refs: list[EvidenceRef] = []
        for item in self.plan.action_plan:
            files.update(node_file(t) for t in item.target_tests)
            files.update(path for copy in item.copies for path in (copy.source, copy.destination))
            refs.extend(item.evidence)
        for entry in self.plan.deferred:
            files.update(entry.would_touch)
            refs.extend(entry.evidence)
        files.update(ref.file_path for ref in refs)
        scope = {entry.feature_id for entry in self.plan.in_scope}
        files.update(c.file_path for f in self.features.values() if f.id in scope for c in f.citations)
        return frozenset(path for path in files if path)

    # Requirements ------------------------------------------------------------
    def item_criteria(self, item: PlanItem, feature: FeatureInput | None, stated: set[str]) -> list[Criterion]:
        labels = self.evidence.labels(item.evidence, CRITERION_EVIDENCE_LIMIT)
        kind = item.kind
        out: list[Criterion] = []
        if kind is PlanItemKind.MERGE_ABANDONED_BRANCH:
            out.append(Criterion(
                f"WHEN {code(item.branch or '')} is merged into {code(self.default)}, THE {self.system} SHALL contain "
                f"{codes(item.files)} and SHALL NOT change files outside the branch diff.", labels))
        elif kind is PlanItemKind.COMPLETE_STUB:
            marker = (f", and SHALL resolve the marker \"{inline_text(item.open_markers[0], 160).rstrip('.')}\""
                      if item.open_markers else "")
            out.append(Criterion(
                f"WHEN {codes(_symbols(item)) or 'the stub'} in {codes(item.files_to_modify)} is called, THE "
                f"{self.system} SHALL return a result instead of raising NotImplementedError and SHALL keep the "
                f"public signature unchanged{marker}.", labels))
        elif kind is PlanItemKind.FIX_FAILING_TEST:
            for test in item.target_tests:
                stated.add(test)
                out.append(Criterion(
                    f"WHEN the scenario \"{inline_text(scenario_of(test))}\" runs, THE test {code(test)} SHALL pass "
                    f"without being edited, through changes to {codes(item.files) or 'the module under test'}.", labels))
        elif kind is PlanItemKind.IMPLEMENT_FROM_SPEC:
            out.append(Criterion(
                f"THE {self.system} SHALL implement {inline_text(feature.title if feature else item.title, 200)} in "
                f"{codes(item.files) or 'the module the ArchitectureAgent chooses'}, as the cited design section "
                "describes and nothing more.", labels))
        elif kind is PlanItemKind.ADD_TESTS:
            out.append(Criterion(
                f"THE tests in {codes(item.files)} SHALL cover the changed code and SHALL run offline and "
                "deterministically.", labels))
        elif kind is PlanItemKind.EXTRACT_MODULE:
            tests = codes(item.target_tests) or "(none were copied)"
            out.append(Criterion(
                f"WHEN {code(self.plan.extract_root or '')} is installed on its own, THE copied tests {tests} SHALL "
                "pass without the rest of the repository, and the original files SHALL stay unchanged.", labels))
        for requirement in item.external_requirements:
            if requirement.kind is RequirementKind.EXTERNAL_SERVICE:
                text = (f"IF {inline_text(requirement.label)} is unreachable, THEN THE {self.system} SHALL work "
                        "against an injectable interface, and its tests SHALL NOT use the network.")
            else:
                text = (f"IF none of {codes(requirement.candidates)} is declared, THEN THE EnvironmentAgent SHALL "
                        f"declare one before {inline_text(item.title, 200)} runs.")
            out.append(Criterion(text, labels))
        return out

    def feature_criteria(self, feature_id: str, items: Sequence[PlanItem]) -> list[Criterion]:
        feature = self.features.get(feature_id)
        criteria: list[Criterion] = []
        claims = [c for c in feature.citations if c.source_type == EvidenceSourceType.README_CLAIM
                  and c.line_start is not None] if feature else []
        for claim in claims[:1]:
            criteria.append(Criterion(
                f"THE {self.system} SHALL provide {inline_text(_claim_text(claim) or feature.title, 200)}, as "
                f"{code(location_of(claim.file_path, claim.line_start, claim.line_end))} claims.",
                self.evidence.labels([citation_ref(claim)])))
        stated: set[str] = set()
        for item in items:
            for criterion in self.item_criteria(item, feature, stated):
                if all(c.text != criterion.text for c in criteria):
                    criteria.append(criterion)
        for item in items:
            for test in item.target_tests:
                if test not in stated:
                    stated.add(test)
                    criteria.append(Criterion(f"THE test {code(test)} SHALL pass.",
                                              self.evidence.labels(item.evidence, 1)))
        specs = [c for c in feature.citations if c.source_type == EvidenceSourceType.ISSUE_SPEC
                 and c.line_start is not None and not is_manifest_path(c.file_path)] if feature else []
        for spec in specs[:MAX_SPEC_CRITERIA_PER_FEATURE]:
            criteria.append(Criterion(
                f"THE {self.system} SHALL behave as the design note "
                f"{code(location_of(spec.file_path, spec.line_start, spec.line_end))} describes "
                f"(\"{inline_text(_spec_quote(spec), SPEC_QUOTE_CHARS)}\").",
                self.evidence.labels([citation_ref(spec)])))
        return criteria

    def user_story(self, feature: FeatureInput | None, title: str, items: Sequence[PlanItem]) -> str:
        who = f"As a maintainer of {inline_text(self.project)}"
        if self.plan.strategy_type is StrategyType.EXTRACT:
            return (f"{who}, I want {title} copied into {code(self.plan.extract_root or 'a standalone package')}, "
                    "so that it can be reused and tested without the rest of the repository.")
        if feature is not None and feature.vital_status == VitalStatus.GHOST_FEATURE:
            return f"{who}, I want {title} built from its design notes, so that the documented feature exists in the code."
        claim = next((c for c in feature.citations if c.source_type == EvidenceSourceType.README_CLAIM), None) \
            if feature else None
        branches = sorted({item.branch for item in items if item.branch})
        if claim is not None and _claim_text(claim):
            benefit = f"the claim \"{inline_text(_claim_text(claim), 160)}\" is true again"
        elif branches:
            benefit = f"the work left on {codes(branches)} is not lost"
        else:
            benefit = "it works as its evidence describes"
        return f"{who}, I want {title} finished in the current stack, so that {benefit}."

    def requirements(self) -> tuple[SpecRequirement, ...]:
        requirements: list[SpecRequirement] = []
        for entry in self.plan.in_scope:
            items = [item for item in self.items if item.feature_id == entry.feature_id]
            title = inline_text(entry.title, 200)
            criteria = self.feature_criteria(entry.feature_id, items)
            if not any(c.evidence for c in criteria):  # every requirement cites evidence
                criteria.append(Criterion(f"THE {self.system} SHALL keep the cited evidence true.",
                                          self.evidence.labels((r for i in items for r in i.evidence), 1)))
            requirements.append(SpecRequirement(
                id=str(len(requirements) + 1), title=title,
                user_story=self.user_story(self.features.get(entry.feature_id), title, items),
                criteria=tuple(criteria), feature_id=entry.feature_id, item_ids=tuple(i.id for i in items),
            ))
        upgrades = [item for item in self.items if item.feature_id is None]
        if upgrades:
            criteria = [
                Criterion(
                    f"WHEN {code(dep.name)} is upgraded from {code(dep.current_specifier or dep.current_version)} in "
                    f"{code(dep.file_path)}, THE EnvironmentAgent SHALL pin the newest release that is compatible "
                    "with the sandbox runtime and passes the test suite.",
                    self.evidence.labels(item.evidence, CRITERION_EVIDENCE_LIMIT))
                for item in upgrades if (dep := item.dependency) is not None
            ] or [Criterion(f"THE {self.system} SHALL install with current dependencies.",
                            self.evidence.labels((r for i in upgrades for r in i.evidence), 1))]
            requirements.append(SpecRequirement(
                id=str(len(requirements) + 1), title="Current dependencies",
                user_story=(f"As a maintainer of {inline_text(self.project)}, I want the exact dependency pins "
                            "upgraded, so that the project installs and runs on a current runtime."),
                criteria=tuple(criteria), item_ids=tuple(i.id for i in upgrades),
            ))
        requirements.append(self.verification_requirement(str(len(requirements) + 1)))
        return tuple(requirements)

    def all_target_tests(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(t for item in self.items for t in item.target_tests))

    def verification_labels(self) -> tuple[str, ...]:
        limit = CRITERION_EVIDENCE_LIMIT
        tested = [item for item in self.items if item.target_tests]
        return (self.evidence.labels((r for i in tested for r in i.evidence), limit)
                or self.evidence.labels((r for i in self.items for r in i.evidence), limit)
                or self.evidence.labels((r for d in self.plan.deferred for r in d.evidence), limit))

    def verification_requirement(self, number: str) -> SpecRequirement:
        criteria = [
            Criterion(f"WHEN the Foundry finishes, THE test suite ({code(FULL_SUITE_COMMAND)}) SHALL exit with code 0.",
                      self.verification_labels()),
            Criterion("THE tests that passed before the revival SHALL still pass after it."),
        ]
        tests = self.all_target_tests()
        if tests:
            criteria.append(Criterion(f"THE target tests {codes(tests)} SHALL pass."))
        criteria.append(Criterion(
            "IF a target test fails, THEN THE VerificationAgent SHALL repair the implementation, never the test, "
            f"within at most {MAX_SELF_HEALING_ITERATIONS} self-healing iterations."))
        return SpecRequirement(
            id=number, title="Verification",
            user_story=("As the reviewer approving this revival, I want the whole test suite to pass, so that the "
                        "revival provably works and breaks nothing that worked before."),
            criteria=tuple(criteria),
        )

    # Tasks -------------------------------------------------------------------
    def primary_action(self, item: PlanItem) -> str:
        kind = item.kind
        if kind is PlanItemKind.MERGE_ABANDONED_BRANCH:
            return f"Merge {code(item.branch or '')} into {code(self.default)}, bringing in {codes(item.files)}"
        if kind is PlanItemKind.COMPLETE_STUB:
            return (f"Implement the body of {codes(_symbols(item)) or 'the stub'} in {codes(item.files_to_modify)} "
                    "and keep its signature")
        if kind is PlanItemKind.FIX_FAILING_TEST:
            return (f"Wire the implementation into {codes(item.files_to_modify) or 'the module under test'} until "
                    f"{codes(item.target_tests)} pass; never edit those tests")
        if kind is PlanItemKind.IMPLEMENT_FROM_SPEC:
            return f"Implement the cited design section in {codes(item.files) or 'the module the ArchitectureAgent chooses'}"
        if kind is PlanItemKind.UPGRADE_DEPENDENCY and item.dependency is not None:
            return (f"Resolve the newest compatible release of {code(item.dependency.name)} in the sandbox and pin it "
                    f"in {code(item.dependency.file_path)}")
        if kind is PlanItemKind.EXTRACT_MODULE:
            return (f"Copy {len(item.copies)} file(s) unchanged into {code(self.plan.extract_root or '')} and create "
                    f"{codes(item.files_to_create)}")
        return f"Write {codes(item.files)}"

    def task_for(self, item: PlanItem, requirement: str | None) -> SpecTask:
        labels = self.evidence.labels(item.evidence)
        details = [f"Plan item {code(item.id)} · {item.kind.value} · {item.stage.value} stage · effort "
                   f"{item.effort.value} · feasibility {item.item_feasibility:g}"]
        if requirement:
            details.append(f"Requirement: {requirement}")
        if item.branch:
            details.append(f"Branch: {code(item.branch)}")
        depends = [self.task_of[dep] for dep in item.depends_on if dep in self.task_of]
        if depends:
            details.append("Depends on: task " + ", ".join(depends))
        if item.files_to_modify:
            details.append(f"Files to modify: {codes(item.files_to_modify)}")
        if item.files_to_create:
            details.append(f"Files to create: {codes(item.files_to_create)}")
        details.append(f"Target tests: {codes(item.target_tests)}" if item.target_tests
                       else f"Target tests: none; the full suite {code(FULL_SUITE_COMMAND)} is the check")
        details.append(f"Evidence: {refs_markdown(labels)}")
        subtasks = [self.primary_action(item)]
        subtasks += [f"Constraint: {inline_text(text, 400)}" for text in item.constraints]
        subtasks += [f"Resolve or remove the marker \"{inline_text(marker, 200)}\"" for marker in item.open_markers]
        command = " ".join([FULL_SUITE_COMMAND, *item.target_tests])
        subtasks.append(f"Verify: run {code(command)} and confirm it exits with code 0")
        return SpecTask(
            id=self.task_of[item.id], title=inline_text(item.title, 200), description=inline_text(item.rationale, 800),
            details=tuple(details), subtasks=tuple(subtasks), evidence=labels, item_id=item.id,
            requirement_ids=(requirement,) if requirement else (), files=item.files, target_tests=item.target_tests,
        )

    def tasks(self, requirements: Sequence[SpecRequirement]) -> tuple[SpecTask, ...]:
        requirement_of = {item_id: r.id for r in requirements for item_id in r.item_ids}
        tasks = [self.task_for(item, requirement_of.get(item.id)) for item in self.items]
        verification = requirements[-1]
        tests = self.all_target_tests()
        subtasks = ["Compare with the baseline run captured before task 1: every test that passed before still passes"]
        if tests:
            subtasks.append(f"Confirm the target tests pass: {codes(tests)}")
        subtasks.append(f"Verify: run {code(FULL_SUITE_COMMAND)} and confirm it exits with code 0")
        tasks.append(SpecTask(
            id=str(len(tasks) + 1), title="Verify the revival",
            description=("Run the whole suite in the sandbox after the change set; repair the implementation, never "
                         f"a test, within at most {MAX_SELF_HEALING_ITERATIONS} self-healing iterations."),
            details=(f"Requirement: {verification.id}", f"Evidence: {refs_markdown(verification.evidence)}"),
            subtasks=tuple(subtasks), evidence=verification.evidence, requirement_ids=(verification.id,),
            target_tests=tests,
        ))
        return tuple(tasks)

    # Design ------------------------------------------------------------------
    def overview_facts(self) -> tuple[str, ...]:
        s, p, basis = self.score.score, self.score.score.parameters, self.score.value_basis
        facts = [
            f"Strategy: {code(self.plan.strategy_type.value)}; spec "
            f"{code(spec_name_for(self.plan.strategy_type, self.project))}",
            f"Feasibility score: **{s.score:g}** = round(100 × ({p.value_weight:g} × {s.value:g} + {p.debt_weight:g} × "
            f"(1 − {s.debt_cost:g}) + {p.risk_weight:g} × (1 − {s.risk:g})), {p.score_decimals})",
        ]
        if basis.extracted_feature_ids or self.plan.strategy_type is StrategyType.EXTRACT:
            facts.append(f"Value V = {s.value:g}: {len(basis.extracted_feature_ids)} of "
                         f"{len(basis.verified_feature_ids)} verified feature(s) extracted")
        else:
            targets = "; ".join(f"{inline_text(t.title, 120)} ({t.feasibility:g})" for t in basis.targets) or "none"
            facts.append(f"Value V = {s.value:g}: {len(basis.verified_feature_ids)} verified of "
                         f"{basis.total_features} feature(s), plus targets weighted by feasibility: {targets}")
        facts.append(f"Debt D = {s.debt_cost:g}: {len(self.score.files)} file(s) against a budget of "
                     f"{p.debt_file_budget:g}")
        factors = "".join(f" + {a.factor.value} {a.count} × {a.per_unit:g} (cap {a.cap:g}) = {a.adjustment:g}"
                          for a in self.score.risk.adjustments)
        facts.append(f"Risk R = {s.risk:g}: base {self.score.risk.base:g}{factors}")
        return tuple(facts)

    def change_set(self) -> tuple[str, ...]:
        lines: list[str] = []
        for item in self.items:
            factors = " × ".join(f"{f.name} {f.value:g}" for f in item.feasibility_factors)
            lines += ["", f"### {item.id}: {inline_text(item.title, 200)}", "",
                      f"- {item.kind.value} · {item.stage.value} stage · effort {item.effort.value} · task "
                      f"{self.task_of[item.id]} · feasibility {item.item_feasibility:g} = {factors}",
                      f"- Rationale: {inline_text(item.rationale, 800)} {refs_markdown(self.evidence.labels(item.evidence))}"]
            if item.files_to_modify:
                lines.append(f"- Files to modify: {codes(item.files_to_modify)}")
            if item.files_to_create:
                lines.append(f"- Files to create: {codes(item.files_to_create)}")
            lines += [f"- Copy {code(c.source)} → {code(c.destination)}" for c in item.copies]
            if item.target_tests:
                lines.append(f"- Target tests (never edited): {codes(item.target_tests)}")
            if item.depends_on:
                lines.append(f"- Depends on: {codes(item.depends_on)}")
            if item.dependency is not None:
                dep = item.dependency
                lines.append(f"- Dependency: {code(dep.name)} {code(dep.current_specifier or dep.current_version)} at "
                             f"{code(location_of(dep.file_path, dep.line_start, None))}; the target version is resolved "
                             "in the sandbox")
            lines += [f"- External requirement: {inline_text(r.label)} ({r.kind.value})" for r in item.external_requirements]
            lines += [f"- Open marker: {inline_text(m, 200)}" for m in item.open_markers]
            lines += [f"- Constraint: {inline_text(c, 400)}" for c in item.constraints]
        return tuple(lines[1:]) or ("This strategy has no plan items.",)

    def merge_plan(self) -> tuple[str, ...]:
        merges = [item for item in self.items if item.kind is PlanItemKind.MERGE_ABANDONED_BRANCH]
        if not merges:
            return (f"This strategy merges no branch; all work happens on {code(self.default)}.",)
        lines: list[str] = []
        for item in merges:
            branch = self.branches.get(item.branch or "")
            later = [self.task_of[i.id] for i in self.items if item.id in i.depends_on]
            lines += ["", f"### Merge {code(item.branch or '')} into {code(self.default)}", ""]
            if branch is not None:
                lines.append(f"- Head {code(branch.head_sha[:12])}: {branch.commits_ahead} commit(s) ahead, "
                             f"{branch.commits_behind} behind, dormant {branch.dormancy_days:g} days"
                             + (", abandoned" if branch.is_abandoned else ""))
            lines.append(f"- Brings in {codes(item.files)} "
                         f"{refs_markdown(self.evidence.labels(item.evidence, CRITERION_EVIDENCE_LIMIT))}")
            lines.append(f"- Runs as task {self.task_of[item.id]} in the {item.stage.value} stage"
                         + (f", before task {', '.join(later)}, which depend on it" if later else ""))
            lines += [f"- Conflict policy: {inline_text(c, 400)}" for c in item.constraints]
        return tuple(lines[1:])

    def verification_strategy(self) -> tuple[str, ...]:
        tests = self.all_target_tests()
        lines = [
            f"- Baseline: before task 1 the VerificationAgent runs {code(FULL_SUITE_COMMAND)} in the sandbox and "
            "records which tests pass.",
            f"- After the change set {code(FULL_SUITE_COMMAND)} must exit with code 0, and every test that passed at "
            "the baseline must still pass.",
        ]
        if tests:
            lines.append(f"- Target tests: {codes(tests)}. They define the expected behavior and are never edited.")
        lines += [
            f"- A failing run is repaired in the implementation, at most {MAX_SELF_HEALING_ITERATIONS} self-healing "
            "iterations; after that the strategy is reported as not verified.",
            "- The sandbox has no network: tests use fakes and injectable interfaces.",
        ]
        return tuple(lines)

    def risks(self) -> tuple[str, ...]:
        risk = self.score.risk
        lines = [f"- Risk R = {self.score.score.risk:g}: base {risk.base:g} for {self.plan.strategy_type.value}."]
        lines += [f"- {a.factor.value}: {a.count} × {a.per_unit:g}, capped at {a.cap:g}, adds {a.adjustment:g}."
                  for a in risk.adjustments]
        for item in self.items:
            if item.item_feasibility < LOW_FEASIBILITY:
                factors = ", ".join(f"{f.name} {f.value:g} ({inline_text(f.detail, 160)})"
                                    for f in item.feasibility_factors if f.name != "BASE")
                lines.append(f"- {code(item.id)} has feasibility {item.item_feasibility:g}"
                             + (f": {factors}." if factors else "."))
        if any(item.requires_resolution for item in self.items):
            lines.append("- Upgraded versions are resolved in the sandbox; an upgrade that breaks the suite falls back "
                         "to the newest release that passes it.")
        markers = sum(len(item.open_markers) for item in self.items)
        if markers:
            lines.append(f"- {markers} open TODO/FIXME marker(s) remain in the touched files.")
        return tuple(lines)

    def out_of_scope(self) -> tuple[str, ...]:
        if not self.plan.deferred:
            return ("Nothing is deferred.",)
        lines = []
        for entry in self.plan.deferred:
            detail = inline_text(entry.detail, 600).rstrip(".")
            text = (f"- **{inline_text(entry.title, 200)}** ({entry.vital_status}): {entry.reason.value}. "
                    f"{detail[:1].upper()}{detail[1:]}.")
            if entry.would_touch:
                text += f" Would touch {codes(entry.would_touch)}."
            if entry.partial:
                text += " Partial deferral: the rest of the feature is in scope."
            lines.append(f"{text} {refs_markdown(self.evidence.labels(entry.evidence, CRITERION_EVIDENCE_LIMIT))}")
        return tuple(lines)

    def introduction(self) -> str:
        return (f"{inline_text(self.plan.title, 200)}. {inline_text(self.plan.summary, 1200)} This recovery spec was "
                f"generated by Lazarus Foundry from the {inline_text(self.project)} genome. Every requirement and "
                "task cites its evidence in footnotes.")

    def design_overview(self) -> str:
        s = self.score.score
        return (f"The {self.plan.strategy_type.value} strategy for {inline_text(self.project)} scores {s.score:g} of "
                f"100. It changes {len(self.score.files)} file(s) through {len(self.items)} plan item(s) and defers "
                f"{len(self.plan.deferred)} item(s); the sections below list every change with its evidence.")

    def build(self) -> SpecSkeleton:
        requirements = self.requirements()
        tasks = self.tasks(requirements)
        sections = {
            "Change Set": self.change_set(),
            "Merge Plan": self.merge_plan(),
            "Verification Strategy": self.verification_strategy(),
            "Risks": self.risks(),
            "Out of Scope": self.out_of_scope(),
        }
        return SpecSkeleton(
            strategy_type=StrategyType(self.plan.strategy_type),
            spec_name=spec_name_for(self.plan.strategy_type, self.project),
            title=inline_text(self.plan.title, 200),
            introduction=self.introduction(),
            design_overview=self.design_overview(),
            overview_facts=self.overview_facts(),
            requirements=requirements,
            tasks=tasks,
            design_sections=MappingProxyType(sections),
            evidence=tuple(self.evidence.entries.values()),
            known_files=self.known_files(),
        )


def build_spec_skeleton(plan: StrategyPlan, plan_score: PlanScore, genome_inputs: GenomeInputs) -> SpecSkeleton:
    """The bundle's structure and deterministic prose for one plan (see the module docstring)."""
    if StrategyType(plan_score.strategy_type) is not StrategyType(plan.strategy_type):
        raise ValueError(f"score is for {plan_score.strategy_type}, plan is {plan.strategy_type}")
    return _SkeletonBuilder(plan, plan_score, genome_inputs).build()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_FOOTNOTE_REF = re.compile(r"\[\^([A-Za-z0-9_-]+)\](?!:)")
_FOOTNOTE_DEF = re.compile(r"^\[\^([A-Za-z0-9_-]+)\]:", re.M)


def _with_footnotes(lines: list[str], entries: Mapping[str, EvidenceEntry]) -> str:
    body = "\n".join(lines).rstrip() + "\n"
    used = sorted(set(_FOOTNOTE_REF.findall(body)), key=lambda label: (len(label), label))
    if used:
        body += "\n" + "\n".join(entries[label].footnote() for label in used if label in entries) + "\n"
    return body


def _criterion_line(number: int, criterion: Criterion) -> str:
    refs = refs_markdown(criterion.evidence)
    return f"{number}. {criterion.text}" + (f" {refs}" if refs else "")


def render_requirements(skeleton: SpecSkeleton, entries: Mapping[str, EvidenceEntry]) -> str:
    lines = [f"# Requirements — {skeleton.title}", "", "## Introduction", "", skeleton.introduction, "",
             "## Requirements"]
    for requirement in skeleton.requirements:
        lines += ["", f"### Requirement {requirement.id}: {requirement.title}", "",
                  f"**User Story:** {requirement.user_story}", "", "#### Acceptance Criteria", ""]
        lines += [_criterion_line(n, c) for n, c in enumerate(requirement.criteria, start=1)]
    return _with_footnotes(lines, entries)


def render_design(skeleton: SpecSkeleton, entries: Mapping[str, EvidenceEntry]) -> str:
    lines = [f"# Design — {skeleton.title}", "", "## Overview", "", skeleton.design_overview, ""]
    lines += [f"- {fact}" for fact in skeleton.overview_facts]
    for section in DESIGN_SECTIONS[1:]:
        lines += ["", f"## {section}", "", *skeleton.design_sections.get(section, ())]
    return _with_footnotes(lines, entries)


def render_tasks(skeleton: SpecSkeleton, entries: Mapping[str, EvidenceEntry]) -> str:
    lines = [f"# Implementation Plan — {skeleton.title}"]
    for task in skeleton.tasks:
        lines += ["", f"- [ ] {task.id}. {task.title}", f"  - {task.description}"]
        lines += [f"  - {detail}" for detail in task.details]
        lines += [f"  - [ ] {task.id}.{n} {text}" for n, text in enumerate(task.subtasks, start=1)]
    return _with_footnotes(lines, entries)


def render_skeleton(skeleton: SpecSkeleton) -> SpecBundle:
    entries = {entry.label: entry for entry in skeleton.evidence}
    return SpecBundle(
        spec_name=skeleton.spec_name,
        requirements_md=render_requirements(skeleton, entries),
        design_md=render_design(skeleton, entries),
        tasks_md=render_tasks(skeleton, entries),
        evidence_index=MappingProxyType({label: entry.as_dict() for label, entry in entries.items()}),
        known_files=skeleton.known_files,
    )


def render_spec_bundle(plan: StrategyPlan, plan_score: PlanScore, genome_inputs: GenomeInputs) -> SpecBundle:
    """The deterministic bundle of one plan. Pure and deterministic: the same inputs give the same text."""
    return render_skeleton(build_spec_skeleton(plan, plan_score, genome_inputs))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
_REQUIREMENT_HEADING = re.compile(r"^### Requirement (?P<n>\d+): \S")
_CRITERION = re.compile(r"^(?P<n>\d+)\. (?P<text>.+)$")
_ANY_CHECKBOX = re.compile(r"^\s*[-*+] \[.?\]")
_TASK = re.compile(r"^- \[[ x~-]\] (?P<n>\d+)\. \S")
_SUBTASK = re.compile(r"^  - \[[ x~-]\] (?P<n>\d+)\.(?P<m>\d+) (?P<text>\S.*)$")


def _blocks(lines: Sequence[str], start: re.Pattern[str]) -> list[list[str]]:
    """Consecutive line groups that each begin with a line matching ``start`` (footnote definitions excluded)."""
    blocks: list[list[str]] = []
    for line in lines:
        if start.match(line):
            blocks.append([line])
        elif blocks and not _FOOTNOTE_DEF.match(line):
            blocks[-1].append(line)
    return blocks


def _is_ears(text: str) -> bool:
    keyword = text.split(" ", 1)[0]
    if keyword not in EARS_KEYWORDS or " SHALL " not in f" {text} ":
        return False
    return keyword != "IF" or " THEN " in text


def _requirements_problems(text: str) -> list[str]:
    lines = text.splitlines()
    problems: list[str] = []
    if not lines or not lines[0].startswith("# Requirements"):
        problems.append("requirements.md: must start with a '# Requirements' heading")
    for heading in ("## Introduction", "## Requirements"):
        if heading not in lines:
            problems.append(f"requirements.md: missing '{heading}'")
    blocks = _blocks(lines, _REQUIREMENT_HEADING)
    if not blocks:
        problems.append("requirements.md: no '### Requirement N: <title>' sections")
    titles = []
    for expected, block in enumerate(blocks, start=1):
        number = int(_REQUIREMENT_HEADING.match(block[0]).group("n"))  # type: ignore[union-attr]
        where = f"requirements.md: Requirement {number}"
        titles.append(block[0].split(": ", 1)[1].strip())
        if number != expected:
            problems.append(f"{where}: numbered {number}, expected {expected}")
        story = [line for line in block if line.startswith("**User Story:** ")]
        if len(story) != 1 or not re.match(r"^\*\*User Story:\*\* As .+, I want .+", story[0]):
            problems.append(f"{where}: needs exactly one '**User Story:** As ..., I want ...' line")
        if "#### Acceptance Criteria" not in block:
            problems.append(f"{where}: missing '#### Acceptance Criteria'")
        criteria = [m for m in (_CRITERION.match(line) for line in block) if m]
        if not criteria:
            problems.append(f"{where}: no acceptance criteria")
        for index, match in enumerate(criteria, start=1):
            if int(match.group("n")) != index:
                problems.append(f"{where}: criterion numbered {match.group('n')}, expected {index}")
            if not _is_ears(match.group("text")):
                problems.append(f"{where}.{index}: not an EARS criterion (WHEN/IF ... THEN/WHILE/WHERE/THE ... SHALL)")
        if not _FOOTNOTE_REF.search("\n".join(block)):
            problems.append(f"{where}: cites no evidence footnote")
    if blocks and "Verification" not in titles:
        problems.append("requirements.md: missing the 'Verification' requirement")
    return problems


def _tasks_problems(text: str) -> list[str]:
    lines = text.splitlines()
    problems: list[str] = []
    if not lines or not lines[0].startswith("# Implementation Plan"):
        problems.append("tasks.md: must start with a '# Implementation Plan' heading")
    for number, line in enumerate(lines, start=1):
        if _ANY_CHECKBOX.match(line) and not (_TASK.match(line) or _SUBTASK.match(line)):
            problems.append(f"tasks.md:{number}: malformed checkbox line {line.strip()[:60]!r}")
    blocks = _blocks(lines, _TASK)
    if not blocks:
        problems.append("tasks.md: no '- [ ] N. <task>' lines")
    for expected, block in enumerate(blocks, start=1):
        number = int(_TASK.match(block[0]).group("n"))  # type: ignore[union-attr]
        where = f"tasks.md: task {number}"
        if number != expected:
            problems.append(f"{where}: numbered {number}, expected {expected}")
        subtasks = [m for m in (_SUBTASK.match(line) for line in block) if m]
        if not subtasks:
            problems.append(f"{where}: has no sub-tasks")
        for index, match in enumerate(subtasks, start=1):
            if (int(match.group("n")), int(match.group("m"))) != (number, index):
                problems.append(f"{where}: sub-task {match.group('n')}.{match.group('m')}, expected {number}.{index}")
        if subtasks and not subtasks[-1].group("text").startswith("Verify:"):
            problems.append(f"{where}: the last sub-task must be a 'Verify:' step")
        if not _FOOTNOTE_REF.search("\n".join(block)):
            problems.append(f"{where}: cites no evidence footnote")
    return problems


def _design_problems(text: str) -> list[str]:
    lines = text.splitlines()
    problems: list[str] = []
    if not lines or not lines[0].startswith("# Design"):
        problems.append("design.md: must start with a '# Design' heading")
    positions = [lines.index(f"## {s}") if f"## {s}" in lines else -1 for s in DESIGN_SECTIONS]
    for section, position in zip(DESIGN_SECTIONS, positions, strict=True):
        if position < 0:
            problems.append(f"design.md: missing '## {section}'")
    found = [p for p in positions if p >= 0]
    if found != sorted(found):
        problems.append("design.md: sections are out of order (" + ", ".join(DESIGN_SECTIONS) + ")")
    return problems


def _reference_problems(name: str, text: str, evidence_index: Mapping[str, Any], known_files: Collection[str]) -> list[str]:
    problems: list[str] = []
    defined = set(_FOOTNOTE_DEF.findall(text))
    for label in dict.fromkeys(_FOOTNOTE_REF.findall(text)):
        if label not in evidence_index:
            problems.append(f"{name}: footnote [^{label}] is not in the evidence index")
        elif label not in defined:
            problems.append(f"{name}: footnote [^{label}] has no definition")
    for label in sorted(defined - set(evidence_index)):
        problems.append(f"{name}: footnote definition [^{label}] is not in the evidence index")
    for path in dict.fromkeys(code_span_files(text)):
        if path not in known_files:
            problems.append(f"{name}: references unknown file {path!r}")
    return problems


def spec_bundle_problems(bundle: SpecBundle, known_files: Collection[str] | None = None) -> list[str]:
    """Every format problem of ``bundle`` (empty when valid). ``known_files`` defaults to the bundle's own list."""
    known = bundle.known_files if known_files is None else frozenset(known_files)
    problems = []
    if not bundle.spec_name or slugify(bundle.spec_name) != bundle.spec_name:
        problems.append(f"spec name {bundle.spec_name!r} is not a kebab-case slug")
    problems += _requirements_problems(bundle.requirements_md)
    problems += _design_problems(bundle.design_md)
    problems += _tasks_problems(bundle.tasks_md)
    for name, text in bundle.documents.items():
        problems += _reference_problems(name, text, bundle.evidence_index, known)
    return problems


def validate_spec_bundle(bundle: SpecBundle, known_files: Collection[str] | None = None) -> SpecBundle:
    """Return ``bundle`` if it is a valid Kiro spec; raise ``SpecValidationError`` listing every problem otherwise."""
    problems = spec_bundle_problems(bundle, known_files)
    if problems:
        raise SpecValidationError(bundle.spec_name, problems)
    return bundle
