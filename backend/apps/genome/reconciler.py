"""Deterministic genome reconciler: features, evidence matching, vital status and conflicts.

Pure: works on an ``EvidenceCatalog`` only (no ORM, no network, no model). It is
both the offline genome (no ``MODEL_API_KEY``) and the draft the reasoning model
refines; the model path re-enters ``finalize_features`` so every rule below
also decides the model's output.

1. Discover features (``discover_features``)
    * One per README feature claim (checklist items anywhere, checked or not,
      and plain bullets under a "Features"-style heading). The claim is the
      feature's ``ANCHOR`` citation.
    * Each unmerged branch is *explained* by the claim its name matches best
      (``tokens.text_match`` on the branch name without its ``feat/`` prefix);
      otherwise it becomes its own feature, titled from the branch name. The
      owner gets every branch-only item of that branch.
    * Each default-branch code module (non-test file with symbols) goes to the
      claim whose stems it shares best: at least one stem shared with the
      module's identifiers (file path and symbol names) and at least
      ``required_overlap(claim)`` shared overall (identifiers plus docstrings).
      Identifier overlap counts double. Unclaimed modules become module
      features. A module belongs to one feature; its symbols are ``OWN_CODE``,
      its stubs ``STUB`` and its markers ``SAME_FILE``.
    * Test files attach to the owner of the module they test (``test_x.py`` ->
      ``x.py``) as ``OWN_TEST``; test functions also attach by name to any
      feature they match (``TEXT_MENTION``).
    * Default-branch markers, doc sections, doc claims outside READMEs and
      dependencies attach to every feature their text matches (``TEXT_MENTION``).

2. Match strengths come from ``apps.genome.scoring``: ``OWN_CODE``/``OWN_TEST``
   -> ``MATCH_DIRECT_SYMBOL``, ``BRANCH_DIFF`` -> ``MATCH_BRANCH_DIFF``, prose,
   comments, commit subjects and the anchor claim -> ``MATCH_TEXT_MENTION``,
   any ``NotImplementedError`` stub -> ``MATCH_STUB``.

3. Citations: per source type, the ``MAX_CITATIONS_PER_SOURCE_TYPE`` strongest
   matches are cited (ties: more stems shared with the feature, then diff
   files and classes before methods, dunder methods and exception classes
   last, then catalog order), with up to ``MAX_TEST_CITATIONS_PER_SOURCE_TYPE``
   of those places kept for tests, plus every stub. Past three full-strength
   items a type's contribution is capped (forensic-invariants.md section 2),
   so the cut changes C(f) by at most the rank-decayed tail, and keeping the
   stubs means the cited evidence decides the vital status exactly as the
   full match set would. C(f) is scored on the cited items only, so every
   scored signal is a citation.

4. Vital status (``classify_vital``), from the cited items:
    * ``VERIFIED_WORKING``: real (non-stub, non-test) code at the default
      branch HEAD and no non-abstract stub at HEAD.
    * ``ABANDONED_IN_PROGRESS``: a stub at HEAD, or work only on an unmerged
      branch, or only tests at HEAD.
    * ``GHOST_FEATURE``: no code, stub, test or branch work anywhere.

5. Conflicts (Requirement 2.4): a cited default-branch doc claim that asserts
   completion (``[x]``, or a plain bullet under a feature heading) on a feature
   that is not ``VERIFIED_WORKING`` yields one ``ConflictResult``. Its
   ``claim_source`` cites the claim (``file:line``), its ``reality_source``
   what the repository shows (branch and diff files, stub locations, or "no
   implementation found on <default branch> at <sha>"), and its description
   and recommendation are fixed templates. ``[ ]`` claims are plans, not
   contradictions.

6. Score every feature with ``score_intent_node`` (conflict -> ``CONFLICTED``)
   and lay out ``graph_position``: one column per vital status, rows by title.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from apps.genome.catalog import CatalogItem, CatalogKind, EvidenceCatalog, ModuleInfo, RepositoryContext
from apps.genome.models import EpistemicStatus, EvidenceSourceType, IntentCategory, VitalStatus
from apps.genome.scoring import (
    DEFAULT_PARAMETERS,
    MATCH_BRANCH_DIFF,
    MATCH_DIRECT_SYMBOL,
    MATCH_STUB,
    MATCH_TEXT_MENTION,
    EvidenceSignal,
    FeatureScore,
    score_intent_node,
)
from apps.genome.tokens import path_tokens, required_overlap, stem, text_match, tokens, tokens_of

MAX_CITATIONS_PER_SOURCE_TYPE: int = 8
MAX_TEST_CITATIONS_PER_SOURCE_TYPE: int = 3
MAX_TITLE_CHARS: int = 120
MAX_PROSE_CHARS: int = 2000
MAX_SOURCE_CHARS: int = 1000
GRAPH_COLUMN_X: Mapping[VitalStatus, int] = {
    VitalStatus.VERIFIED_WORKING: 0,
    VitalStatus.ABANDONED_IN_PROGRESS: 360,
    VitalStatus.GHOST_FEATURE: 720,
}
GRAPH_ROW_HEIGHT: int = 180
BRANCH_PREFIXES: tuple[str, ...] = ("feat/", "feature/", "fix/", "bugfix/", "hotfix/", "chore/", "wip/", "exp/")

_CATEGORY_WORDS: tuple[tuple[IntentCategory, frozenset[str]], ...] = tuple(
    (category, frozenset(stem(word) for word in words))
    for category, words in (
        (
            IntentCategory.INTEGRATION,
            ("webhook", "api", "http", "rest", "graphql", "grpc", "integration", "export", "import", "pdf",
             "csv", "email", "sms", "oauth", "sso", "sync", "feed", "notification", "slack", "stripe"),
        ),
        (IntentCategory.DATA_MODEL, ("model", "schema", "migration", "database", "table", "orm", "entity")),
        (
            IntentCategory.ARCHITECTURE,
            ("architecture", "framework", "plugin", "cache", "queue", "worker", "pipeline", "infrastructure",
             "configuration", "deployment"),
        ),
    )
)


class FeatureOrigin(StrEnum):
    CLAIM = "CLAIM"  # a README feature claim
    BRANCH = "BRANCH"  # an unmerged branch no claim explains
    MODULE = "MODULE"  # a default-branch code module no claim covers
    MODEL = "MODEL"  # proposed by the reasoning model


class MatchKind(StrEnum):
    ANCHOR = "ANCHOR"  # the claim the feature was discovered from
    OWN_CODE = "OWN_CODE"  # symbol or route in a module the feature owns
    OWN_TEST = "OWN_TEST"  # test of a module the feature owns
    STUB = "STUB"  # NotImplementedError stub
    BRANCH_DIFF = "BRANCH_DIFF"  # code or diff file on an unmerged branch the feature owns
    SAME_FILE = "SAME_FILE"  # marker inside a file the feature owns
    TEXT_MENTION = "TEXT_MENTION"  # prose, comment, commit subject or test name names the feature


_MATCH_STRENGTH: Mapping[MatchKind, float] = {
    MatchKind.ANCHOR: MATCH_TEXT_MENTION,
    MatchKind.OWN_CODE: MATCH_DIRECT_SYMBOL,
    MatchKind.OWN_TEST: MATCH_DIRECT_SYMBOL,
    MatchKind.STUB: MATCH_STUB,
    MatchKind.BRANCH_DIFF: MATCH_BRANCH_DIFF,
    MatchKind.SAME_FILE: MATCH_TEXT_MENTION,
    MatchKind.TEXT_MENTION: MATCH_TEXT_MENTION,
}
# Lower ranks are cited first among equally strong matches.
_KIND_RANK: Mapping[str, int] = {
    "BRANCH_FILE": 0, "CLASS": 1, "FUNCTION": 2, "ROUTE": 2, "STUB": 2, "METHOD": 3, "CONSTANT": 5,
}


def match_strength(item: CatalogItem, kind: MatchKind) -> float:
    """s(f, e) for ``item`` matched as ``kind``. A stub never proves more than ``MATCH_STUB``."""
    if item.is_stub:
        return MATCH_STUB
    return _MATCH_STRENGTH[kind]


class VitalReason(StrEnum):
    HEAD_IMPLEMENTATION = "HEAD_IMPLEMENTATION"
    HEAD_STUB = "HEAD_STUB"
    BRANCH_ONLY = "BRANCH_ONLY"
    TESTS_ONLY = "TESTS_ONLY"
    NO_IMPLEMENTATION = "NO_IMPLEMENTATION"


def classify_vital(items: Iterable[CatalogItem]) -> tuple[VitalStatus, VitalReason]:
    """Vital status from a feature's evidence (rule 4 of the module docstring)."""
    items = tuple(items)
    head_code = any(item.is_head_implementation for item in items)
    head_stub = any(item.is_head_stub for item in items)
    if head_code and not head_stub:
        return VitalStatus.VERIFIED_WORKING, VitalReason.HEAD_IMPLEMENTATION
    if head_stub:
        return VitalStatus.ABANDONED_IN_PROGRESS, VitalReason.HEAD_STUB
    if any(item.source_type is EvidenceSourceType.UNMERGED_BRANCH_COMMIT for item in items):
        return VitalStatus.ABANDONED_IN_PROGRESS, VitalReason.BRANCH_ONLY
    if any(
        item.source_type is EvidenceSourceType.AST_IMPLEMENTATION and item.observed_on_default_branch for item in items
    ):
        return VitalStatus.ABANDONED_IN_PROGRESS, VitalReason.TESTS_ONLY
    return VitalStatus.GHOST_FEATURE, VitalReason.NO_IMPLEMENTATION


def asserts_completion(item: CatalogItem) -> bool:
    """A default-branch doc claim that says the feature exists: ``[x]`` or a plain bullet under a feature heading."""
    return (
        item.kind is CatalogKind.DOC_CLAIM
        and item.observed_on_default_branch
        and (item.checked is True or (item.checked is None and item.in_feature_section))
    )


def item_match_tokens(item: CatalogItem) -> frozenset[str]:
    """The stems an item is matched on: identifiers and path for code, the text for everything else."""
    if item.kind in (CatalogKind.SYMBOL, CatalogKind.ROUTE, CatalogKind.STUB):
        return tokens_of([item.qualified_name or item.name, item.text if item.kind is CatalogKind.ROUTE else None])
    if item.kind is CatalogKind.BRANCH_FILE:
        return path_tokens(item.file_path)
    return tokens(item.text or item.name)


def cap_text(text: str, limit: int) -> str:
    """``text`` with whitespace collapsed, cut at ``limit`` characters (ellipsis when cut)."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "\u2026"


# ---------------------------------------------------------------------------
# Feature specs (mutable while being discovered or refined)
# ---------------------------------------------------------------------------
@dataclass
class ConflictSpec:
    """A conflict proposed by the model, already validated against the catalog."""

    claim_ids: tuple[str, ...]
    reality_ids: tuple[str, ...]
    description: str
    resolution_recommendation: str


@dataclass
class FeatureSpec:
    key: str
    origin: FeatureOrigin
    title: str
    match_tokens: frozenset[str]
    matches: dict[str, MatchKind] = field(default_factory=dict)
    anchor_id: str | None = None
    branch_names: list[str] = field(default_factory=list)
    module_paths: list[str] = field(default_factory=list)
    order: int = 0
    # Set by the model path; None means "use the deterministic template".
    category: IntentCategory | None = None
    reconstructed_intent: str | None = None
    actual_implementation_state: str | None = None
    model_conflicts: list[ConflictSpec] = field(default_factory=list)
    suggested_vital_status: VitalStatus | None = None
    draft_keys: tuple[str, ...] = ()

    def attach(self, item: CatalogItem, kind: MatchKind) -> None:
        """Match ``item`` as ``kind``, keeping the stronger kind if it is already matched."""
        current = self.matches.get(item.id)
        if current is None or match_strength(item, kind) > match_strength(item, current):
            self.matches[item.id] = kind


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CitationResult:
    item: CatalogItem
    match_kind: MatchKind
    match_strength: float
    weight: float  # the item's term in C(f) (SignalTerm.term)


@dataclass(frozen=True)
class ConflictResult:
    claim_source: str
    reality_source: str
    conflict_description: str
    resolution_recommendation: str
    claim_item_ids: tuple[str, ...]
    reality_item_ids: tuple[str, ...]
    origin: str  # "RULE" or "MODEL"


@dataclass(frozen=True)
class GenomeFeature:
    key: str
    origin: FeatureOrigin
    title: str
    category: IntentCategory
    vital_status: VitalStatus
    vital_reason: VitalReason
    score: FeatureScore
    reconstructed_intent: str
    actual_implementation_state: str
    citations: tuple[CitationResult, ...]
    conflicts: tuple[ConflictResult, ...]
    matched_count: int
    graph_position: Mapping[str, Any] = field(default_factory=dict)
    draft_keys: tuple[str, ...] = ()

    @property
    def epistemic_status(self) -> EpistemicStatus:
        return self.score.status

    @property
    def confidence(self) -> float:
        return self.score.confidence

    def citation_for(self, item_id: str) -> CitationResult | None:
        return next((citation for citation in self.citations if citation.item.id == item_id), None)

    def score_breakdown(self) -> dict[str, Any]:
        """``FeatureScore.as_dict()`` plus how each citation entered it (stored on ``IntentNode.score_breakdown``)."""
        return {
            **self.score.as_dict(),
            "feature_key": self.key,
            "origin": self.origin.value,
            "draft_keys": list(self.draft_keys),
            "vital_status": self.vital_status.value,
            "vital_status_reason": self.vital_reason.value,
            "matched_evidence_count": self.matched_count,
            "citations": [
                {
                    "signal_index": index,
                    "catalog_id": citation.item.id,
                    "source_type": citation.item.source_type.value,
                    "match_kind": citation.match_kind.value,
                    "match_strength": citation.match_strength,
                    "weight": citation.weight,
                }
                for index, citation in enumerate(self.citations)
            ],
        }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _humanize_branch(name: str) -> str:
    short = name
    for prefix in BRANCH_PREFIXES:
        if short.startswith(prefix):
            short = short[len(prefix):]
            break
    words = " ".join(short.replace("/", " ").replace("-", " ").replace("_", " ").split())
    return words[:1].upper() + words[1:] if words else name


def _branch_tokens(name: str) -> frozenset[str]:
    return tokens(_humanize_branch(name))


def _summary_line(text: str | None) -> str | None:
    if not text:
        return None
    first = " ".join(text.strip().split("\n\n", 1)[0].split())
    sentence = first.split(". ", 1)[0].rstrip(".")
    return sentence or None


def _best(
    candidates: Sequence[FeatureSpec], score: Any
) -> FeatureSpec | None:
    """The candidate with the highest ``score(candidate)`` tuple (None scores are ineligible); ties keep order."""
    best: tuple[Any, ...] | None = None
    chosen: FeatureSpec | None = None
    for candidate in candidates:
        value = score(candidate)
        if value is None:
            continue
        ranked = (*value, -candidate.order)
        if best is None or ranked > best:
            best, chosen = ranked, candidate
    return chosen


class _Discovery:
    def __init__(self, catalog: EvidenceCatalog) -> None:
        self.catalog = catalog
        self.context: RepositoryContext = catalog.context
        self.features: list[FeatureSpec] = []
        self.by_file: dict[tuple[str, str], list[CatalogItem]] = defaultdict(list)
        for item in catalog.items:
            if item.file_path:
                self.by_file[(item.branch_name, item.file_path)].append(item)

    def add_feature(self, spec: FeatureSpec) -> FeatureSpec:
        taken = {feature.key for feature in self.features}
        base, suffix = spec.key, 2
        while spec.key in taken:
            spec.key, suffix = f"{base}#{suffix}", suffix + 1
        spec.order = len(self.features)
        self.features.append(spec)
        return spec

    def claims(self) -> list[FeatureSpec]:
        found: list[FeatureSpec] = []
        for item in self.catalog.items:
            if (
                item.source_type is EvidenceSourceType.README_CLAIM
                and item.kind is CatalogKind.DOC_CLAIM
                and (item.checked is not None or item.in_feature_section)
            ):
                spec = self.add_feature(
                    FeatureSpec(
                        # The claim's fingerprint ignores line numbers, so the key survives re-ingestion.
                        key=f"claim:{item.fingerprint or item.id}",
                        origin=FeatureOrigin.CLAIM,
                        title=cap_text(item.text or item.name, MAX_TITLE_CHARS),
                        match_tokens=tokens(item.text or item.name),
                        anchor_id=item.id,
                    )
                )
                spec.attach(item, MatchKind.ANCHOR)
                found.append(spec)
        return found

    def branches(self, claims: Sequence[FeatureSpec]) -> None:
        for branch in self.context.unmerged_branches:
            items = [
                item for item in self.catalog.items
                if item.branch_name == branch.name and not item.observed_on_default_branch
            ]
            if not items:
                continue
            branch_tokens = _branch_tokens(branch.name)

            def explains(claim: FeatureSpec) -> tuple[int, float] | None:
                if not text_match(branch_tokens, claim.match_tokens):
                    return None
                shared = len(branch_tokens & claim.match_tokens)
                return shared, shared / max(1, len(claim.match_tokens))

            owner = _best(claims, explains)
            if owner is None:
                owner = self.add_feature(
                    FeatureSpec(
                        key=f"branch:{branch.name}",
                        origin=FeatureOrigin.BRANCH,
                        title=cap_text(_humanize_branch(branch.name), MAX_TITLE_CHARS),
                        match_tokens=branch_tokens,
                    )
                )
            owner.branch_names.append(branch.name)
            for item in items:
                owner.attach(item, self._branch_kind(item))

    @staticmethod
    def _branch_kind(item: CatalogItem) -> MatchKind:
        if item.kind is CatalogKind.STUB or item.is_stub:
            return MatchKind.STUB
        if item.kind in (CatalogKind.SYMBOL, CatalogKind.ROUTE, CatalogKind.BRANCH_FILE):
            return MatchKind.BRANCH_DIFF
        if item.kind is CatalogKind.MARKER:
            return MatchKind.SAME_FILE
        return MatchKind.TEXT_MENTION

    def code_modules(self) -> list[tuple[ModuleInfo, list[CatalogItem]]]:
        modules = []
        for module in self.catalog.modules:
            if not module.observed_on_default_branch or module.is_test_file:
                continue
            code = [
                item for item in self.by_file.get((module.branch_name, module.file_path), [])
                if item.kind in (CatalogKind.SYMBOL, CatalogKind.ROUTE, CatalogKind.STUB)
            ]
            if code:
                modules.append((module, code))
        return modules

    def modules(self, claims: Sequence[FeatureSpec]) -> dict[str, FeatureSpec]:
        owners: dict[str, FeatureSpec] = {}
        for module, code in self.code_modules():
            identifiers = path_tokens(module.file_path) | tokens_of(item.qualified_name or item.name for item in code)
            docs = tokens_of([module.docstring, *(item.text for item in code if item.kind is CatalogKind.SYMBOL)])
            bag = identifiers | docs

            def covers(claim: FeatureSpec) -> tuple[int, float] | None:
                ident_shared = claim.match_tokens & identifiers
                shared = claim.match_tokens & bag
                if not ident_shared or len(shared) < required_overlap(claim.match_tokens):
                    return None
                score = 2 * len(ident_shared) + len(shared - ident_shared)
                return score, len(shared) / max(1, len(claim.match_tokens))

            owner = _best(claims, covers)
            if owner is None:
                owner = self.add_feature(self._module_feature(module, code))
            owner.module_paths.append(module.file_path)
            owners[module.file_path] = owner
            for item in self.by_file.get((module.branch_name, module.file_path), []):
                if item.kind is CatalogKind.STUB or item.is_stub:
                    owner.attach(item, MatchKind.STUB)
                elif item.kind in (CatalogKind.SYMBOL, CatalogKind.ROUTE):
                    owner.attach(item, MatchKind.OWN_CODE)
                elif item.kind is CatalogKind.MARKER:
                    owner.attach(item, MatchKind.SAME_FILE)
        return owners

    def _module_feature(self, module: ModuleInfo, code: Sequence[CatalogItem]) -> FeatureSpec:
        top = [item for item in code if item.kind is CatalogKind.SYMBOL and item.parent is None]
        classes = [item for item in top if item.symbol_kind == "CLASS"]
        named = classes or [item for item in top if item.symbol_kind == "FUNCTION"]
        title = _summary_line(module.docstring) or f"{module.module_name} module"
        return FeatureSpec(
            key=f"module:{module.file_path}",
            origin=FeatureOrigin.MODULE,
            title=cap_text(title, MAX_TITLE_CHARS),
            match_tokens=path_tokens(module.file_path) | tokens_of(item.name for item in named),
        )

    def tests(self, owners: Mapping[str, FeatureSpec]) -> None:
        by_stem: dict[str, list[str]] = defaultdict(list)
        for path in owners:
            by_stem[PurePosixPath(path).stem].append(path)
        for module in self.catalog.modules:
            if not module.observed_on_default_branch or not module.is_test_file:
                continue
            stem_name = PurePosixPath(module.file_path).stem
            target = stem_name.removeprefix("test_").removesuffix("_test")
            candidates = sorted(by_stem.get(target, []))
            if not candidates:
                continue
            owner = owners[candidates[0]]
            for item in self.by_file.get((module.branch_name, module.file_path), []):
                if item.kind in (CatalogKind.SYMBOL, CatalogKind.ROUTE):
                    owner.attach(item, MatchKind.OWN_TEST)
                elif item.kind is CatalogKind.MARKER:
                    owner.attach(item, MatchKind.SAME_FILE)
        for item in self.catalog.items:
            if not (item.observed_on_default_branch and item.is_test and item.kind is CatalogKind.SYMBOL):
                continue
            name = (item.qualified_name or item.name).rsplit(".", 1)[-1]
            if not name.startswith("test"):
                continue
            name_tokens = tokens(name)
            for feature in self.features:
                if text_match(name_tokens, feature.match_tokens):
                    feature.attach(item, MatchKind.TEXT_MENTION)

    def mentions(self) -> None:
        for item in self.catalog.items:
            if not item.observed_on_default_branch:
                continue
            if item.kind not in (CatalogKind.MARKER, CatalogKind.DOC_SECTION, CatalogKind.DEPENDENCY, CatalogKind.DOC_CLAIM):
                continue
            if item.source_type is EvidenceSourceType.README_CLAIM:
                continue  # README claims are features of their own
            item_tokens = item_match_tokens(item)
            for feature in self.features:
                if text_match(item_tokens, feature.match_tokens):
                    feature.attach(item, MatchKind.TEXT_MENTION)

    def run(self) -> list[FeatureSpec]:
        claims = self.claims()
        self.branches(claims)
        owners = self.modules(claims)
        self.tests(owners)
        self.mentions()
        return [feature for feature in self.features if feature.matches]


def discover_features(catalog: EvidenceCatalog) -> list[FeatureSpec]:
    """Candidate features with their matched evidence (rule 1 of the module docstring)."""
    return _Discovery(catalog).run()


def relevance(item: CatalogItem, feature_tokens: frozenset[str]) -> MatchKind | None:
    """How ``item`` would match a feature described by ``feature_tokens``, or None if it does not.

    Used to vet evidence the model attaches outside the draft: code must share
    ``required_overlap`` stems with the feature through its identifiers or
    path, everything else through its text.
    """
    if not feature_tokens:
        return None
    if item.kind in (CatalogKind.SYMBOL, CatalogKind.ROUTE, CatalogKind.STUB):
        candidate = item_match_tokens(item) | path_tokens(item.file_path)
        if not text_match(candidate, feature_tokens):
            return None
        if item.kind is CatalogKind.STUB or item.is_stub:
            return MatchKind.STUB
        if not item.observed_on_default_branch:
            return MatchKind.BRANCH_DIFF
        return MatchKind.OWN_TEST if item.is_test else MatchKind.OWN_CODE
    if item.kind is CatalogKind.BRANCH_FILE:
        candidate = path_tokens(item.file_path) | _branch_tokens(item.branch_name)
        return MatchKind.BRANCH_DIFF if text_match(candidate, feature_tokens) else None
    if item.kind is CatalogKind.BRANCH_COMMIT:
        candidate = tokens(item.text) | _branch_tokens(item.branch_name)
        return MatchKind.TEXT_MENTION if text_match(candidate, feature_tokens) else None
    return MatchKind.TEXT_MENTION if text_match(item_match_tokens(item), feature_tokens) else None


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------
def _decay(item: CatalogItem) -> float:
    return math.exp(-DEFAULT_PARAMETERS.decay_lambda * item.age_days)


def _is_noise_symbol(item: CatalogItem) -> bool:
    """Dunder methods and exception classes: real code, but rarely what a feature is about."""
    last = (item.qualified_name or item.name).rsplit(".", 1)[-1]
    return (
        (last.startswith("__") and last.endswith("__"))
        or (item.symbol_kind == "CLASS" and last.endswith(("Error", "Exception")))
        or (item.is_test and item.kind is CatalogKind.SYMBOL and not last.startswith("test"))  # fixtures, helpers
    )


def _rank_key(item: CatalogItem, kind: MatchKind, feature_tokens: frozenset[str]) -> tuple[Any, ...]:
    term = match_strength(item, kind) * _decay(item)
    kind_name = item.kind.value if item.kind in (CatalogKind.BRANCH_FILE, CatalogKind.STUB, CatalogKind.ROUTE) else (
        item.symbol_kind or ""
    )
    kind_rank = 4 if _is_noise_symbol(item) else _KIND_RANK.get(kind_name, 6)
    shared = 0 if _is_noise_symbol(item) else len(item_match_tokens(item) & feature_tokens)
    return (-term, -shared, kind_rank, _id_number(item.id))


def _id_number(item_id: str) -> int:
    digits = item_id.lstrip("E")
    return int(digits) if digits.isdigit() else 0


def select_citations(
    catalog: EvidenceCatalog,
    feature: FeatureSpec,
    per_type: int = MAX_CITATIONS_PER_SOURCE_TYPE,
    test_slots: int = MAX_TEST_CITATIONS_PER_SOURCE_TYPE,
) -> list[tuple[CatalogItem, MatchKind]]:
    """The cited subset of ``feature.matches`` (rule 3 of the module docstring), in catalog order.

    Within a source type, up to ``test_slots`` of the ``per_type`` places go to
    tests when the type also has non-test items, so a module's citations show
    both its code and the tests that exercise it. Every stub is cited.
    """
    grouped: dict[EvidenceSourceType, list[tuple[CatalogItem, MatchKind]]] = defaultdict(list)
    for item_id, kind in feature.matches.items():
        item = catalog.get(item_id)
        if item is not None:
            grouped[item.source_type].append((item, kind))
    chosen: dict[str, tuple[CatalogItem, MatchKind]] = {}
    for entries in grouped.values():
        entries.sort(key=lambda entry: _rank_key(entry[0], entry[1], feature.match_tokens))
        code = [entry for entry in entries if not entry[0].is_test]
        tests = [entry for entry in entries if entry[0].is_test]
        if code and tests:
            kept_tests = tests[: min(test_slots, len(tests), max(0, per_type - 1))]
            kept = code[: per_type - len(kept_tests)]
            kept += kept_tests
            if len(kept) < per_type:
                kept += [entry for entry in tests if entry not in kept][: per_type - len(kept)]
        else:
            kept = entries[:per_type]
        for item, kind in kept:
            chosen[item.id] = (item, kind)
        for item, kind in entries:
            if item.is_stub:
                chosen[item.id] = (item, kind)
    return sorted(chosen.values(), key=lambda entry: _id_number(entry[0].id))


def to_signal(item: CatalogItem, kind: MatchKind) -> EvidenceSignal:
    is_ast = item.source_type is EvidenceSourceType.AST_IMPLEMENTATION
    return EvidenceSignal(
        source_type=item.source_type,
        match_strength=match_strength(item, kind),
        age_days=0.0 if item.observed_on_default_branch else item.age_days,
        observed_on_default_branch=item.observed_on_default_branch,
        is_stub=is_ast and item.is_stub and not item.is_test,
        is_test=is_ast and item.is_test,
    )


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _list(values: Sequence[str], limit: int = 4) -> str:
    unique = list(dict.fromkeys(values))
    shown = ", ".join(unique[:limit])
    return f"{shown} and {len(unique) - limit} more" if len(unique) > limit else shown


class _Facts:
    """What a feature's matched evidence shows, for the prose and conflict templates."""

    def __init__(self, catalog: EvidenceCatalog, items: Sequence[CatalogItem]) -> None:
        self.context = catalog.context
        self.head_code = [item for item in items if item.is_head_implementation]
        self.head_tests = [
            item for item in items
            if item.source_type is EvidenceSourceType.AST_IMPLEMENTATION and item.observed_on_default_branch and item.is_test
        ]
        self.head_stubs = [item for item in items if item.is_head_stub and item.kind is CatalogKind.STUB] or [
            item for item in items if item.is_head_stub
        ]
        self.branch_items = [item for item in items if item.source_type is EvidenceSourceType.UNMERGED_BRANCH_COMMIT]
        self.branch_stubs = [item for item in self.branch_items if item.kind is CatalogKind.STUB]
        self.markers = [item for item in items if item.kind is CatalogKind.MARKER]
        self.specs = [item for item in items if item.kind is CatalogKind.DOC_SECTION and item.observed_on_default_branch]
        self.claims = [item for item in items if item.kind is CatalogKind.DOC_CLAIM and item.observed_on_default_branch]
        self.branches = list(dict.fromkeys(item.branch_name for item in self.branch_items))

    def branch_files(self, branch: str) -> list[str]:
        files = [item.file_path for item in self.branch_items if item.branch_name == branch and item.kind is CatalogKind.BRANCH_FILE]
        return files or [item.file_path for item in self.branch_items if item.branch_name == branch and item.file_path]

    def branch_phrase(self, branch: str) -> str:
        info = self.context.branch(branch)
        files = self.branch_files(branch)
        detail = f" ({_list(files)})" if files else ""
        if info is None:
            return f"unmerged branch {branch}{detail}"
        return (
            f"unmerged branch {branch}{detail}, dormant {int(info.dormancy_days)} days, "
            f"{info.commits_ahead} commits ahead and {info.commits_behind} behind {self.context.default_branch}"
        )


def _intent(feature: FeatureSpec, catalog: EvidenceCatalog, facts: _Facts) -> str:
    anchor = catalog.get(feature.anchor_id) if feature.anchor_id else None
    parts: list[str] = []
    if anchor is not None:
        state = {True: "a completed feature", False: "a planned feature"}.get(anchor.checked, "a feature")  # type: ignore[arg-type]
        section = f' under "{anchor.section}"' if anchor.section else ""
        parts.append(f'{anchor.location} lists "{anchor.text}" as {state}{section}.')
    elif feature.origin is FeatureOrigin.BRANCH and feature.branch_names:
        branch = feature.branch_names[0]
        commits = [item for item in facts.branch_items if item.kind is CatalogKind.BRANCH_COMMIT and item.branch_name == branch]
        info = catalog.context.branch(branch)
        last = f', last commit "{commits[-1].text}"' if commits else ""
        when = f" on {info.last_commit_date[:10]}" if info else ""
        parts.append(f"Unmerged branch {branch} was building {feature.title.lower()} ({_plural(len(commits), 'commit')}{last}{when}).")
    elif feature.origin is FeatureOrigin.MODULE and feature.module_paths:
        path = feature.module_paths[0]
        module = next((m for m in catalog.modules if m.file_path == path and m.observed_on_default_branch), None)
        names = [item.name for item in facts.head_code if item.file_path == path and item.parent is None]
        name = module.module_name if module else path
        provides = f" provides {_list(names)}" if names else ""
        parts.append(f"{name} ({path}){provides}.")
        summary = _summary_line(module.docstring) if module else None
        if summary:
            parts.append(f'Its docstring says: "{summary}."')
    else:
        parts.append(f"{feature.title}.")
    for spec in facts.specs[:2]:
        parts.append(f'{spec.location} describes it in the section "{spec.name}".')
    if feature.origin is not FeatureOrigin.BRANCH:
        for branch in facts.branches:
            parts.append(f"Unmerged branch {branch} was building it.")
    return cap_text(" ".join(parts), MAX_PROSE_CHARS)


def _state(vital: VitalStatus, facts: _Facts) -> str:
    context = facts.context
    where = f"{context.default_branch} at {context.short_sha}"
    parts: list[str] = []
    if facts.head_code:
        files = [item.file_path for item in facts.head_code]
        parts.append(f"{_plural(len(facts.head_code), 'implementation symbol')} on {where} in {_list(files)}")
    if facts.head_tests:
        parts.append(f"{_plural(len(facts.head_tests), 'test')} on {context.default_branch}")
    for stub in facts.head_stubs[:3]:
        parts.append(f"{stub.name} at {stub.location} raises NotImplementedError")
    for branch in facts.branches:
        parts.append(f"work on {facts.branch_phrase(branch)}")
    for stub in facts.branch_stubs[:2]:
        parts.append(f"{stub.name} at {stub.location} on {stub.branch_name} still raises NotImplementedError")
    if facts.markers:
        parts.append(f"{_plural(len(facts.markers), 'open TODO/FIXME marker')} ({_list([m.location for m in facts.markers], 3)})")
    lead = {
        VitalStatus.VERIFIED_WORKING: "Implemented",
        VitalStatus.ABANDONED_IN_PROGRESS: "Partially implemented",
        VitalStatus.GHOST_FEATURE: f"Not implemented: no code on {where} or on any unmerged branch",
    }[vital]
    body = "; ".join(parts)
    text = f"{lead}: {body}." if body and vital is not VitalStatus.GHOST_FEATURE else (
        f"{lead}; {body}." if body else f"{lead}."
    )
    return cap_text(text, MAX_PROSE_CHARS)


def _reality(vital: VitalStatus, facts: _Facts) -> tuple[str, tuple[str, ...]]:
    context = facts.context
    where = f"{context.default_branch} at {context.short_sha}"
    parts: list[str] = []
    ids: list[str] = []
    for stub in facts.head_stubs[:3]:
        parts.append(f"{stub.name} at {stub.location} on {where} raises NotImplementedError")
        ids.append(stub.id)
    for branch in facts.branches:
        parts.append(f"the implementation exists only on {facts.branch_phrase(branch)}")
        ids.extend(item.id for item in facts.branch_items if item.branch_name == branch and item.kind is CatalogKind.BRANCH_FILE)
    for stub in facts.branch_stubs[:2]:
        parts.append(f"{stub.name} at {stub.location} on {stub.branch_name} still raises NotImplementedError")
        ids.append(stub.id)
    if vital is VitalStatus.ABANDONED_IN_PROGRESS and not parts and facts.head_tests:
        parts.append(f"only tests reference it on {where} ({_list([t.location for t in facts.head_tests], 3)})")
        ids.extend(item.id for item in facts.head_tests[:3])
    if vital is VitalStatus.GHOST_FEATURE or not parts:
        parts.append(f"no implementation found on {where} or on any unmerged branch")
    ids.extend(marker.id for marker in facts.markers[:3])
    return cap_text("; ".join(parts), MAX_SOURCE_CHARS), tuple(dict.fromkeys(ids))


def _recommendation(vital: VitalStatus, facts: _Facts, claim: CatalogItem) -> str:
    default = facts.context.default_branch
    if facts.branches:
        branch = facts.branches[0]
        return (
            f"Restore: rebase {branch} onto {default}, finish its open stubs and TODOs, wire it into the code on "
            f"{default} and run the tests; until it lands, untick the claim at {claim.location}."
        )
    if facts.head_stubs:
        stub = facts.head_stubs[0]
        return (
            f"Implement {stub.name} ({stub.location}) in place of the NotImplementedError and add tests, "
            f"or mark the claim at {claim.location} as not done."
        )
    if vital is VitalStatus.GHOST_FEATURE:
        return f"Implement the feature with tests, or remove or untick the claim at {claim.location}."
    return f"Finish the implementation and its tests, or untick the claim at {claim.location}."


def _rule_conflict(feature: FeatureSpec, vital: VitalStatus, facts: _Facts, cited: Sequence[CatalogItem]) -> ConflictResult | None:
    if vital is VitalStatus.VERIFIED_WORKING:
        return None
    claims = [item for item in cited if asserts_completion(item)]
    if not claims:
        return None
    claims.sort(key=lambda item: (item.id != feature.anchor_id, _id_number(item.id)))
    claim = claims[0]
    reality, reality_ids = _reality(vital, facts)
    box = "[x] " if claim.checked else ""
    return ConflictResult(
        claim_source=cap_text(f'{claim.location}: "{box}{claim.text}"', MAX_SOURCE_CHARS),
        reality_source=reality,
        conflict_description=cap_text(
            f'{claim.location} presents "{claim.text}" as done, but {reality}.', MAX_PROSE_CHARS
        ),
        resolution_recommendation=cap_text(_recommendation(vital, facts, claim), MAX_PROSE_CHARS),
        claim_item_ids=tuple(item.id for item in claims),
        reality_item_ids=reality_ids,
        origin="RULE",
    )


def _model_conflict(spec: ConflictSpec, catalog: EvidenceCatalog) -> ConflictResult:
    claims = [catalog[item_id] for item_id in spec.claim_ids]
    reality = [catalog[item_id] for item_id in spec.reality_ids]
    return ConflictResult(
        claim_source=cap_text("; ".join(f'{item.location}: "{item.name}"' for item in claims), MAX_SOURCE_CHARS),
        reality_source=cap_text("; ".join(f"{item.location} ({item.name})" for item in reality), MAX_SOURCE_CHARS),
        conflict_description=cap_text(spec.description, MAX_PROSE_CHARS),
        resolution_recommendation=cap_text(spec.resolution_recommendation, MAX_PROSE_CHARS),
        claim_item_ids=tuple(spec.claim_ids),
        reality_item_ids=tuple(spec.reality_ids),
        origin="MODEL",
    )


def categorize(feature_tokens: frozenset[str]) -> IntentCategory:
    for category, words in _CATEGORY_WORDS:
        if feature_tokens & words:
            return category
    return IntentCategory.CORE_FEATURE


def finalize_feature(catalog: EvidenceCatalog, feature: FeatureSpec) -> GenomeFeature:
    """Cite, classify, detect conflicts and score one feature. Raises ``UncitedIntentNodeError`` without evidence."""
    selected = select_citations(catalog, feature)
    cited = [item for item, _ in selected]
    matched = [catalog[item_id] for item_id in feature.matches if item_id in catalog]
    vital, reason = classify_vital(cited)
    facts = _Facts(catalog, matched)
    conflicts: list[ConflictResult] = []
    rule = _rule_conflict(feature, vital, facts, cited)
    extra = list(feature.model_conflicts)
    if rule is not None:
        # A model conflict on the same claim only rewrites the rule conflict's prose;
        # its sources stay the deterministic citations of the claim and of reality.
        same = next((spec for spec in extra if set(spec.claim_ids) & set(rule.claim_item_ids)), None)
        if same is not None:
            extra.remove(same)
            rule = replace(
                rule,
                conflict_description=cap_text(same.description, MAX_PROSE_CHARS) or rule.conflict_description,
                resolution_recommendation=cap_text(same.resolution_recommendation, MAX_PROSE_CHARS)
                or rule.resolution_recommendation,
                origin="RULE+MODEL",
            )
        conflicts.append(rule)
    conflicts.extend(_model_conflict(spec, catalog) for spec in extra)
    signals = [to_signal(item, kind) for item, kind in selected]
    score = score_intent_node(signals, conflict=bool(conflicts))
    terms = {term.signal_index: term.term for contribution in score.breakdown.contributions for term in contribution.terms}
    citations = tuple(
        CitationResult(item=item, match_kind=kind, match_strength=match_strength(item, kind), weight=terms[index])
        for index, (item, kind) in enumerate(selected)
    )
    category = feature.category or categorize(feature.match_tokens | tokens(feature.title))
    # Model prose about the implementation assumed its own vital status; if the
    # evidence says otherwise, the deterministic description replaces it.
    state = feature.actual_implementation_state
    if feature.suggested_vital_status is not None and feature.suggested_vital_status is not vital:
        state = None
    return GenomeFeature(
        key=feature.key,
        origin=feature.origin,
        title=cap_text(feature.title, MAX_TITLE_CHARS),
        category=category,
        vital_status=vital,
        vital_reason=reason,
        score=score,
        reconstructed_intent=cap_text(feature.reconstructed_intent or _intent(feature, catalog, facts), MAX_PROSE_CHARS),
        actual_implementation_state=cap_text(state or _state(vital, facts), MAX_PROSE_CHARS),
        citations=citations,
        conflicts=tuple(conflicts),
        matched_count=len(matched),
        draft_keys=feature.draft_keys,
    )


def layout(features: Sequence[GenomeFeature]) -> tuple[GenomeFeature, ...]:
    """Assign ``graph_position``: columns by vital status, rows by title; returns the features in that order."""
    ordered = sorted(
        features,
        key=lambda f: (GRAPH_COLUMN_X[f.vital_status], f.title.casefold(), f.key),
    )
    rows: dict[VitalStatus, int] = defaultdict(int)
    placed: list[GenomeFeature] = []
    for feature in ordered:
        row = rows[feature.vital_status]
        rows[feature.vital_status] += 1
        position = {
            "x": GRAPH_COLUMN_X[feature.vital_status],
            "y": row * GRAPH_ROW_HEIGHT,
            "column": feature.vital_status.value,
            "row": row,
        }
        placed.append(replace(feature, graph_position=position))
    return tuple(placed)


def finalize_features(catalog: EvidenceCatalog, features: Iterable[FeatureSpec]) -> tuple[GenomeFeature, ...]:
    return layout([finalize_feature(catalog, feature) for feature in features])


@dataclass(frozen=True)
class ReconciledGenome:
    """The deterministic genome and the specs it came from (the model's starting point)."""

    specs: tuple[FeatureSpec, ...]
    features: tuple[GenomeFeature, ...]

    def feature(self, key: str) -> GenomeFeature:
        return next(feature for feature in self.features if feature.key == key)


def reconcile(catalog: EvidenceCatalog) -> ReconciledGenome:
    """The deterministic genome for ``catalog``."""
    specs = tuple(discover_features(catalog))
    return ReconciledGenome(specs=specs, features=finalize_features(catalog, specs))
