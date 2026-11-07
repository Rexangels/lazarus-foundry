"""The evidence catalog: every citable item from one project's ingestion, with a stable id.

Pure: no ORM queries, no Git, no network. ``build_catalog`` takes the rows
ingestion persisted (``ArtifactRecord`` / ``BranchSnapshot`` from the database,
or the identical ``ArtifactRow`` / ``BranchRow`` from ``run_ingestion``) wrapped
as ``ArtifactInput`` / ``BranchInput``, and returns an ``EvidenceCatalog``.

Every ``CatalogItem`` copies its file path, commit SHA, lines and snippet from
ingestion data. Nothing here (and nothing downstream) takes a line or a
snippet from model output: the reconciler and the model refer to items only by
``CatalogItem.id``.

Items and their ``EvidenceSourceType``
    =====================================  ==========================  =========================
    Ingestion record                        On the default branch       Only on an unmerged branch
    =====================================  ==========================  =========================
    AST symbol / route / stub               AST_IMPLEMENTATION          UNMERGED_BRANCH_COMMIT
    TODO/FIXME/HACK/XXX marker              TODO_COMMENT                TODO_COMMENT (branch age)
    Doc claim in a README                   README_CLAIM                UNMERGED_BRANCH_COMMIT
    Doc claim or section in other docs      ISSUE_SPEC                  UNMERGED_BRANCH_COMMIT
    Dependency pin                          ISSUE_SPEC                  UNMERGED_BRANCH_COMMIT
    Branch diff file / branch commit        (n/a)                       UNMERGED_BRANCH_COMMIT
    =====================================  ==========================  =========================

    * Module docstrings and imports are not items (they prove no behavior);
      module docstrings are kept on ``ModuleInfo`` for matching and titles.
      README headings are not items either (they are page structure); other
      docs' headings become ``DOC_SECTION`` items whose snippet is the
      section's exact text (heading line to the line before the next heading).
    * Symbols in test files (``tests/`` directories, ``test_*.py``,
      ``*_test.py``, ``conftest.py``) are ``is_test``.
    * A branch record whose fingerprint also exists on the default branch is
      not branch-only and is skipped (the default-branch item covers it); the
      branch's ``BRANCH_FILE`` diff item still records the change.
    * Merged branches contribute nothing: their commits are on the default branch.
    * ``age_days`` is 0 at the default branch's HEAD and the branch tip's
      dormancy (``BranchSnapshot.diff_summary["dormancy_days"]``) otherwise,
      per forensic-invariants.md section 2.

Ids are ``E1``, ``E2``, ... assigned after sorting by (default branch first,
then branch name), file path, first line, kind and fingerprint, so the same
ingestion always yields the same ids regardless of row order.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from apps.genome.models import EvidenceSourceType
from apps.ingestion.evidence import SourceText
from apps.ingestion.models import ArtifactType

DIFF_HEADER_OLD: str = "--- "
DIFF_HEADER_NEW: str = "+++ "
DEV_NULL: str = "/dev/null"


class CatalogKind(StrEnum):
    SYMBOL = "SYMBOL"  # class, function, method, constant (tests included, flagged is_test)
    ROUTE = "ROUTE"
    STUB = "STUB"  # a ``raise NotImplementedError`` site
    MARKER = "MARKER"
    DOC_CLAIM = "DOC_CLAIM"
    DOC_SECTION = "DOC_SECTION"
    DEPENDENCY = "DEPENDENCY"
    BRANCH_FILE = "BRANCH_FILE"  # one file in an unmerged branch's diff
    BRANCH_COMMIT = "BRANCH_COMMIT"  # one commit unique to an unmerged branch


_KIND_ORDER: dict[CatalogKind, int] = {kind: index for index, kind in enumerate(CatalogKind)}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ArtifactInput:
    """One ingestion artifact (an ``ArtifactRecord`` or a pipeline ``ArtifactRow``)."""

    artifact_type: ArtifactType
    file_path: str
    branch_name: str
    raw_content: str
    structured_metadata: Mapping[str, Any]
    pk: int | None = None  # ArtifactRecord primary key; None for pipeline rows

    @classmethod
    def from_row(cls, row: Any) -> ArtifactInput:
        """Wrap an ``ArtifactRecord`` or an ``ArtifactRow`` (both have the same fields)."""
        return cls(
            artifact_type=ArtifactType(row.artifact_type),
            file_path=row.file_path,
            branch_name=row.branch_name,
            raw_content=row.raw_content or "",
            structured_metadata=row.structured_metadata or {},
            pk=getattr(row, "pk", None),
        )


@dataclass(frozen=True)
class BranchInfo:
    name: str
    head_sha: str
    is_default: bool
    is_merged: bool
    is_abandoned: bool
    dormancy_days: float
    commits_ahead: int
    commits_behind: int
    last_commit_date: str

    @classmethod
    def from_row(cls, row: Any) -> BranchInfo:
        """Wrap a ``BranchSnapshot`` or a pipeline ``BranchRow``."""
        summary: Mapping[str, Any] = row.diff_summary or {}
        last = row.last_commit_date
        return cls(
            name=row.name,
            head_sha=row.head_sha,
            is_default=bool(summary.get("is_default", False)),
            is_merged=bool(summary.get("is_merged", False)),
            is_abandoned=bool(row.is_abandoned),
            dormancy_days=float(summary.get("dormancy_days") or 0),
            commits_ahead=int(row.commits_ahead),
            commits_behind=int(row.commits_behind),
            last_commit_date=last.isoformat() if hasattr(last, "isoformat") else str(last),
        )


@dataclass(frozen=True)
class RepositoryContext:
    default_branch: str
    head_sha: str
    branches: tuple[BranchInfo, ...]

    def branch(self, name: str) -> BranchInfo | None:
        return next((item for item in self.branches if item.name == name), None)

    @property
    def unmerged_branches(self) -> tuple[BranchInfo, ...]:
        return tuple(item for item in self.branches if not item.is_default and not item.is_merged)

    @property
    def short_sha(self) -> str:
        return self.head_sha[:12]

    @classmethod
    def build(cls, branches: Iterable[BranchInfo], default_branch: str | None = None) -> RepositoryContext:
        rows = tuple(sorted(branches, key=lambda item: (not item.is_default, item.name)))
        default = next((item for item in rows if item.is_default), None)
        if default is None and default_branch is not None:
            default = next((item for item in rows if item.name == default_branch), None)
        if default is None:
            raise ValueError("the ingestion has no default branch")
        return cls(default_branch=default.name, head_sha=default.head_sha, branches=rows)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CatalogItem:
    """One citable evidence record. Every location field is copied from ingestion data."""

    id: str
    kind: CatalogKind
    source_type: EvidenceSourceType
    name: str  # short label: qualified name, claim text, heading, commit subject, ...
    text: str  # matching text: docstring, marker text, claim text, heading, commit subject
    file_path: str  # "" for a branch commit
    branch_name: str
    commit_sha: str
    line_start: int | None
    line_end: int | None
    snippet: str
    snippet_truncated: bool
    fingerprint: str
    age_days: float
    observed_on_default_branch: bool
    artifact_pk: int | None
    artifact_index: int  # position of the source artifact in the input sequence
    is_stub: bool = False
    is_test: bool = False
    symbol_kind: str | None = None  # CLASS / FUNCTION / METHOD / CONSTANT for symbols
    qualified_name: str | None = None
    parent: str | None = None
    enclosing_symbol: str | None = None  # markers and stubs
    in_abstract_method: bool = False
    checked: bool | None = None  # doc claims
    in_feature_section: bool = False
    section: str | None = None
    marker_tag: str | None = None
    change_type: str | None = None  # branch files

    @property
    def location(self) -> str:
        """``path:start-end`` (or just the path, or the branch for commits)."""
        where = self.file_path or f"branch {self.branch_name}"
        if self.line_start is None:
            return where
        if self.line_end is None or self.line_end == self.line_start:
            return f"{where}:{self.line_start}"
        return f"{where}:{self.line_start}-{self.line_end}"

    @property
    def is_claim_side(self) -> bool:
        """Documentation that asserts what the software does (the claim side of a conflict)."""
        return self.kind in (CatalogKind.DOC_CLAIM, CatalogKind.DOC_SECTION) and self.observed_on_default_branch

    @property
    def is_reality_side(self) -> bool:
        """Code, branch work or markers: what the repository actually contains."""
        return self.source_type in (
            EvidenceSourceType.AST_IMPLEMENTATION,
            EvidenceSourceType.UNMERGED_BRANCH_COMMIT,
            EvidenceSourceType.TODO_COMMENT,
        )

    @property
    def is_head_implementation(self) -> bool:
        """Real (non-stub, non-test) code at the default branch HEAD."""
        return (
            self.source_type is EvidenceSourceType.AST_IMPLEMENTATION
            and self.observed_on_default_branch
            and not self.is_stub
            and not self.is_test
        )

    @property
    def is_head_stub(self) -> bool:
        """A non-abstract ``NotImplementedError`` stub at HEAD (abstract methods are legitimate)."""
        return (
            self.source_type is EvidenceSourceType.AST_IMPLEMENTATION
            and self.observed_on_default_branch
            and self.is_stub
            and not self.in_abstract_method
        )


@dataclass(frozen=True)
class ModuleInfo:
    """A code file in one scope; not citable, used to group symbols and name module features."""

    file_path: str
    branch_name: str
    module_name: str
    docstring: str | None
    is_test_file: bool
    observed_on_default_branch: bool


@dataclass(frozen=True)
class EvidenceCatalog:
    context: RepositoryContext
    items: tuple[CatalogItem, ...]
    modules: tuple[ModuleInfo, ...] = ()
    _by_id: Mapping[str, CatalogItem] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_id", {item.id: item for item in self.items})

    def __len__(self) -> int:
        return len(self.items)

    def get(self, item_id: str) -> CatalogItem | None:
        return self._by_id.get(item_id)

    def __getitem__(self, item_id: str) -> CatalogItem:
        return self._by_id[item_id]

    def __contains__(self, item_id: object) -> bool:
        return item_id in self._by_id

    def find(self, **criteria: Any) -> tuple[CatalogItem, ...]:
        """Items whose attributes equal every keyword argument (test and debugging helper)."""
        return tuple(
            item for item in self.items if all(getattr(item, key) == value for key, value in criteria.items())
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_test_path(path: str) -> bool:
    """Files whose symbols are tests or test helpers."""
    pure = PurePosixPath(path)
    name = pure.name
    if name == "conftest.py" or name.startswith("test_") or pure.stem.endswith("_test"):
        return True
    return any(part in ("tests", "test", "__tests__", "spec", "specs") for part in pure.parts[:-1])


def is_readme_path(path: str) -> bool:
    return PurePosixPath(path).name.casefold().startswith("readme")


def module_name_for(path: str) -> str:
    pure = PurePosixPath(path)
    parts = list(pure.with_suffix("").parts) if pure.suffix else list(pure.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or pure.stem


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _section_snippet(raw: str, line_start: int, next_heading: int | None) -> tuple[int, str, bool]:
    """(line_end, exact text, truncated) of the section starting at heading ``line_start``."""
    source = SourceText(raw.encode("utf-8"))
    if not 1 <= line_start <= source.line_count:
        return line_start, "", False
    end = (next_heading - 1) if next_heading is not None else source.line_count
    end = min(max(end, line_start), source.line_count)
    while end > line_start and not source.line(end).strip():
        end -= 1
    text, truncated = source.snippet(line_start, end)
    return end, text, truncated


def split_patches(combined: str) -> dict[str, str]:
    """Per-file unified patches from a COMMIT_LOG's combined diff, keyed by path (new path, or old for deletions)."""
    lines = combined.split("\n")
    starts: list[tuple[int, str]] = []
    for index in range(len(lines) - 1):
        old, new = lines[index], lines[index + 1]
        if old.startswith(DIFF_HEADER_OLD) and new.startswith(DIFF_HEADER_NEW):
            new_path = new[len(DIFF_HEADER_NEW):].strip()
            old_path = old[len(DIFF_HEADER_OLD):].strip()
            chosen = new_path if new_path != DEV_NULL else old_path
            if chosen.startswith(("a/", "b/")):
                chosen = chosen[2:]
            starts.append((index, chosen))
    patches: dict[str, str] = {}
    for position, (index, path) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        patches.setdefault(path, "\n".join(lines[index:end]).rstrip("\n"))
    return patches


def _capped(text: str) -> tuple[str, bool]:
    if not text:
        return "", False
    source = SourceText(text.encode("utf-8"))
    if source.line_count == 0:
        return "", False
    return source.snippet(1, source.line_count)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
class _Builder:
    def __init__(self, context: RepositoryContext) -> None:
        self.context = context
        self.default = context.default_branch
        self.pending: list[dict[str, Any]] = []
        self.modules: list[ModuleInfo] = []
        self.head_fingerprints: set[str] = set()

    # Scope -------------------------------------------------------------
    def scope_of(self, artifact: ArtifactInput) -> tuple[bool, float] | None:
        """(observed_on_default_branch, age_days), or None for scopes that contribute nothing."""
        if artifact.branch_name == self.default:
            return True, 0.0
        branch = self.context.branch(artifact.branch_name)
        if branch is None or branch.is_merged or branch.is_default:
            return None
        return False, branch.dormancy_days

    def source_for(self, head_type: EvidenceSourceType, on_default: bool) -> EvidenceSourceType:
        return head_type if on_default else EvidenceSourceType.UNMERGED_BRANCH_COMMIT

    def add(self, artifact: ArtifactInput, index: int, on_default: bool, age: float, **fields: Any) -> None:
        fingerprint = str(fields.get("fingerprint") or "")
        if not on_default and fingerprint and fingerprint in self.head_fingerprints:
            return  # the same record exists at HEAD; this is not branch-only evidence
        self.pending.append(
            {
                "branch_name": artifact.branch_name,
                "observed_on_default_branch": on_default,
                "age_days": 0.0 if on_default else float(age),
                "artifact_pk": artifact.pk,
                "artifact_index": index,
                **fields,
            }
        )

    # Records -----------------------------------------------------------
    def ast_module(self, artifact: ArtifactInput, index: int, on_default: bool, age: float) -> None:
        meta = artifact.structured_metadata
        path = artifact.file_path
        commit = str(meta.get("commit_sha") or "")
        in_test_file = is_test_path(path)
        docstring = None
        for symbol in meta.get("symbols") or ():
            if symbol.get("kind") == "MODULE_DOCSTRING":
                docstring = symbol.get("docstring") or symbol.get("snippet")
                continue
            is_test = bool(symbol.get("is_test")) or in_test_file
            self.add(
                artifact,
                index,
                on_default,
                age,
                kind=CatalogKind.SYMBOL,
                source_type=self.source_for(EvidenceSourceType.AST_IMPLEMENTATION, on_default),
                name=str(symbol.get("qualified_name") or symbol.get("name") or ""),
                text=str(symbol.get("docstring") or ""),
                file_path=path,
                commit_sha=commit,
                line_start=_int_or_none(symbol.get("line_start")),
                line_end=_int_or_none(symbol.get("line_end")),
                snippet=str(symbol.get("snippet") or ""),
                snippet_truncated=bool(symbol.get("snippet_truncated")),
                fingerprint=str(symbol.get("fingerprint") or ""),
                is_stub=bool(symbol.get("is_stub")) and not is_test,
                is_test=is_test,
                symbol_kind=str(symbol.get("kind") or ""),
                qualified_name=symbol.get("qualified_name"),
                parent=symbol.get("parent"),
                in_abstract_method=bool(symbol.get("is_abstract")),
            )
        for route in meta.get("routes") or ():
            self.add(
                artifact,
                index,
                on_default,
                age,
                kind=CatalogKind.ROUTE,
                source_type=self.source_for(EvidenceSourceType.AST_IMPLEMENTATION, on_default),
                name=str(route.get("name") or ""),
                text=f"{' '.join(route.get('methods') or ())} {route.get('path') or ''} {route.get('handler') or ''}",
                file_path=path,
                commit_sha=commit,
                line_start=_int_or_none(route.get("line_start")),
                line_end=_int_or_none(route.get("line_end")),
                snippet=str(route.get("snippet") or ""),
                snippet_truncated=bool(route.get("snippet_truncated")),
                fingerprint=str(route.get("fingerprint") or ""),
                is_test=in_test_file,
                qualified_name=route.get("handler"),
            )
        for stub in meta.get("stubs") or ():
            if in_test_file:
                continue  # a test raising NotImplementedError is not a missing feature
            self.add(
                artifact,
                index,
                on_default,
                age,
                kind=CatalogKind.STUB,
                source_type=self.source_for(EvidenceSourceType.AST_IMPLEMENTATION, on_default),
                name=str(stub.get("enclosing_symbol") or stub.get("name") or ""),
                text=str(stub.get("message") or ""),
                file_path=path,
                commit_sha=commit,
                line_start=_int_or_none(stub.get("line_start")),
                line_end=_int_or_none(stub.get("line_end")),
                snippet=str(stub.get("snippet") or ""),
                snippet_truncated=bool(stub.get("snippet_truncated")),
                fingerprint=str(stub.get("fingerprint") or ""),
                is_stub=True,
                enclosing_symbol=stub.get("enclosing_symbol"),
                qualified_name=stub.get("enclosing_symbol"),
                in_abstract_method=bool(stub.get("in_abstract_method")),
            )
        self.modules.append(
            ModuleInfo(
                file_path=path,
                branch_name=artifact.branch_name,
                module_name=module_name_for(path),
                docstring=docstring,
                is_test_file=in_test_file,
                observed_on_default_branch=on_default,
            )
        )

    def marker(self, artifact: ArtifactInput, index: int, on_default: bool, age: float) -> None:
        meta = artifact.structured_metadata
        self.add(
            artifact,
            index,
            on_default,
            age,
            kind=CatalogKind.MARKER,
            # Markers stay TODO_COMMENT on a branch too; their branch age carries the dormancy.
            source_type=EvidenceSourceType.TODO_COMMENT,
            name=str(meta.get("name") or ""),
            text=str(meta.get("text") or ""),
            file_path=artifact.file_path,
            commit_sha=str(meta.get("commit_sha") or ""),
            line_start=_int_or_none(meta.get("line_start")),
            line_end=_int_or_none(meta.get("line_end")),
            snippet=str(meta.get("snippet") or artifact.raw_content),
            snippet_truncated=bool(meta.get("snippet_truncated")),
            fingerprint=str(meta.get("fingerprint") or ""),
            is_test=is_test_path(artifact.file_path),
            enclosing_symbol=meta.get("enclosing_symbol"),
            marker_tag=meta.get("tag"),
        )

    def document(self, artifact: ArtifactInput, index: int, on_default: bool, age: float) -> None:
        meta = artifact.structured_metadata
        path = artifact.file_path
        commit = str(meta.get("commit_sha") or "")
        readme = is_readme_path(path)
        claim_type = EvidenceSourceType.README_CLAIM if readme else EvidenceSourceType.ISSUE_SPEC
        for claim in meta.get("claims") or ():
            self.add(
                artifact,
                index,
                on_default,
                age,
                kind=CatalogKind.DOC_CLAIM,
                source_type=self.source_for(claim_type, on_default),
                name=str(claim.get("text") or claim.get("name") or ""),
                text=str(claim.get("text") or ""),
                file_path=path,
                commit_sha=commit,
                line_start=_int_or_none(claim.get("line_start")),
                line_end=_int_or_none(claim.get("line_end")),
                snippet=str(claim.get("snippet") or ""),
                snippet_truncated=bool(claim.get("snippet_truncated")),
                fingerprint=str(claim.get("fingerprint") or ""),
                checked=claim.get("checked"),
                in_feature_section=bool(claim.get("in_feature_section")),
                section=claim.get("section"),
            )
        if readme:
            return  # README headings are page structure, not evidence
        headings = sorted(meta.get("headings") or (), key=lambda item: item.get("line_start") or 0)
        for position, heading in enumerate(headings):
            start = _int_or_none(heading.get("line_start"))
            if start is None:
                continue
            following = _int_or_none(headings[position + 1].get("line_start")) if position + 1 < len(headings) else None
            end, snippet, truncated = _section_snippet(artifact.raw_content, start, following)
            if not snippet:
                end, snippet, truncated = start, str(heading.get("snippet") or ""), False
            self.add(
                artifact,
                index,
                on_default,
                age,
                kind=CatalogKind.DOC_SECTION,
                source_type=self.source_for(EvidenceSourceType.ISSUE_SPEC, on_default),
                name=str(heading.get("text") or heading.get("name") or ""),
                text=str(heading.get("text") or ""),
                file_path=path,
                commit_sha=commit,
                line_start=start,
                line_end=end,
                snippet=snippet,
                snippet_truncated=truncated,
                fingerprint=str(heading.get("fingerprint") or ""),
            )

    def manifest(self, artifact: ArtifactInput, index: int, on_default: bool, age: float) -> None:
        meta = artifact.structured_metadata
        for dependency in meta.get("dependencies") or ():
            self.add(
                artifact,
                index,
                on_default,
                age,
                kind=CatalogKind.DEPENDENCY,
                source_type=self.source_for(EvidenceSourceType.ISSUE_SPEC, on_default),
                name=str(dependency.get("name") or ""),
                text=str(dependency.get("normalized_name") or dependency.get("name") or ""),
                file_path=artifact.file_path,
                commit_sha=str(meta.get("commit_sha") or ""),
                line_start=_int_or_none(dependency.get("line_start")),
                line_end=_int_or_none(dependency.get("line_end")),
                snippet=str(dependency.get("snippet") or ""),
                snippet_truncated=bool(dependency.get("snippet_truncated")),
                fingerprint=str(dependency.get("fingerprint") or ""),
            )

    def commit_log(self, artifact: ArtifactInput, index: int, on_default: bool, age: float) -> None:
        if on_default:
            return
        meta = artifact.structured_metadata
        head_sha = str(meta.get("head_sha") or "")
        diff = meta.get("diff") or {}
        patches = split_patches(artifact.raw_content)
        for item in diff.get("files") or ():
            path = str(item.get("path") or "")
            change = str(item.get("change_type") or "")
            additions, deletions = item.get("additions") or 0, item.get("deletions") or 0
            snippet, truncated = _capped(patches.get(path, ""))
            if not snippet:
                snippet = f"{change} {path} (+{additions} -{deletions})"
            self.add(
                artifact,
                index,
                on_default,
                age,
                kind=CatalogKind.BRANCH_FILE,
                source_type=EvidenceSourceType.UNMERGED_BRANCH_COMMIT,
                name=f"{change.lower() or 'changed'} {path} on {artifact.branch_name}",
                text=path,
                file_path=path,
                commit_sha=head_sha,
                line_start=None,
                line_end=None,
                snippet=snippet,
                snippet_truncated=truncated or bool(item.get("patch_truncated")),
                fingerprint=f"branch-file:{artifact.branch_name}:{path}",
                is_test=is_test_path(path),
                change_type=change,
            )
        for commit in meta.get("commits") or ():
            subject = str(commit.get("subject") or "")
            self.add(
                artifact,
                index,
                on_default,
                age,
                kind=CatalogKind.BRANCH_COMMIT,
                source_type=EvidenceSourceType.UNMERGED_BRANCH_COMMIT,
                name=subject,
                text=subject,
                file_path="",
                commit_sha=str(commit.get("sha") or ""),
                line_start=None,
                line_end=None,
                snippet=subject,
                snippet_truncated=False,
                fingerprint=f"branch-commit:{commit.get('sha')}",
            )

    def finish(self) -> EvidenceCatalog:
        branch_rank = {branch.name: rank for rank, branch in enumerate(self.context.branches)}

        def order(entry: dict[str, Any]) -> tuple[Any, ...]:
            return (
                0 if entry["observed_on_default_branch"] else 1,
                branch_rank.get(entry["branch_name"], len(branch_rank)),
                entry["branch_name"],
                entry["file_path"],
                entry["line_start"] if entry["line_start"] is not None else 0,
                _KIND_ORDER[entry["kind"]],
                entry["fingerprint"],
                entry["name"],
            )

        items = tuple(
            CatalogItem(id=f"E{number}", **entry)
            for number, entry in enumerate(sorted(self.pending, key=order), start=1)
        )
        modules = tuple(sorted(self.modules, key=lambda m: (not m.observed_on_default_branch, m.branch_name, m.file_path)))
        return EvidenceCatalog(context=self.context, items=items, modules=modules)


_HANDLERS = {
    ArtifactType.AST_MODULE: "ast_module",
    ArtifactType.TODO_MARKER: "marker",
    ArtifactType.README_DOC: "document",
    ArtifactType.DEPENDENCY_MANIFEST: "manifest",
    ArtifactType.COMMIT_LOG: "commit_log",
}


def build_catalog(artifacts: Sequence[ArtifactInput], context: RepositoryContext) -> EvidenceCatalog:
    """The catalog of every citable record in ``artifacts`` (see the module docstring)."""
    builder = _Builder(context)
    scoped: list[tuple[int, ArtifactInput, bool, float]] = []
    for index, artifact in enumerate(artifacts):
        scope = builder.scope_of(artifact)
        if scope is not None:
            scoped.append((index, artifact, *scope))
    # HEAD first, so branch records can be checked against HEAD fingerprints.
    for index, artifact, on_default, _ in scoped:
        if on_default:
            builder.head_fingerprints.update(_fingerprints(artifact))
    for index, artifact, on_default, age in sorted(scoped, key=lambda entry: not entry[2]):
        getattr(builder, _HANDLERS[artifact.artifact_type])(artifact, index, on_default, age)
    return builder.finish()


def _fingerprints(artifact: ArtifactInput) -> set[str]:
    meta = artifact.structured_metadata
    found: set[str] = set()
    if artifact.artifact_type is ArtifactType.TODO_MARKER:
        found.add(str(meta.get("fingerprint") or ""))
    for key in ("symbols", "routes", "stubs", "claims", "headings", "dependencies"):
        for record in meta.get(key) or ():
            found.add(str(record.get("fingerprint") or ""))
    found.discard("")
    return found


def catalog_from_rows(artifacts: Iterable[Any], branches: Iterable[Any], default_branch: str | None = None) -> EvidenceCatalog:
    """``build_catalog`` for ORM rows or pipeline rows (``ArtifactRecord``/``ArtifactRow`` and ``BranchSnapshot``/``BranchRow``)."""
    context = RepositoryContext.build((BranchInfo.from_row(row) for row in branches), default_branch)
    return build_catalog([ArtifactInput.from_row(row) for row in artifacts], context)
