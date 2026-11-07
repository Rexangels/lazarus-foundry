"""Tree-sitter extraction of code, comment, document and manifest evidence.

Pure service: nothing here touches the ORM, Git or the network.
``ASTParserService.parse_file(path, source_bytes)`` returns a frozen
``FileInventory``; ``parse_revision`` / ``parse_branch_changes`` feed it
bytes read from Git objects by ``GitForensicService``. Every record has the
evidence shape documented in ``apps.ingestion.evidence`` (file path, 1-based
inclusive lines, exact snippet, line-independent fingerprint), which is what
an ``EvidenceCitation`` quotes.

Grammars
    Python, the demo language, always uses the bundled ``tree-sitter-python``
    wheel. JavaScript/TypeScript/TSX, Go, Java, Ruby, Rust, C# and PHP are
    loaded from ``tree-sitter-language-pack``. Version 1.24.1 bundles no
    grammars: ``get_language(name)`` downloads the parser library on first
    use into a per-user cache (``tree_sitter_language_pack.cache_dir()``)
    and loads it from there afterwards; ``downloaded_languages()`` lists that
    cache without network access. Ingestion therefore uses only cached
    grammars unless ``settings.INGEST_ALLOW_GRAMMAR_DOWNLOAD`` is True. A
    grammar that is not cached, fails to download or fails to load is logged
    once and gives the file ``GRAMMAR_UNAVAILABLE`` (never an exception);
    such files, and files in languages without an extractor
    (``UNSUPPORTED_LANGUAGE``), still get line-based ``TODO``/``FIXME``
    markers. ``Language`` objects are cached per process, ``Parser`` objects
    per service (one service per thread: parsers are not thread-safe).

Python (full extraction)
    Module docstring; classes (bases), functions and methods (async,
    decorators, signature, docstring) under qualified names
    (``Outer.Inner.method``); decorated definitions start at their first
    decorator; imports (plain, ``from``, aliased, relative, wildcard,
    ``__future__``, including function-local imports); module-level
    ``UPPER_CASE`` constants; ``raise NotImplementedError`` stubs attributed
    to the enclosing function (whose ``is_stub`` is set); tests (``test*``
    functions and ``Test*`` classes/methods in ``test_*.py`` / ``*_test.py``,
    pytest's default collection rules); routes from ``@app.get("/x")``,
    ``@router.post(...)``, ``@app.route("/x", methods=[...])`` decorators.

Other languages (best effort)
    Classes, functions/methods and imports from per-language node-type maps
    (``LANGUAGE_SPECS``).

Markers
    ``TODO``/``FIXME``/``HACK``/``XXX`` come from real comment nodes when a
    grammar parsed the file (text inside strings never counts); directly
    following comment lines at the same column continue the note. A marker
    records its tag, text, owner (``TODO(alice)``), line(s) and enclosing
    symbol.

Robustness
    Vendored/generated paths, files over ``settings.INGEST_MAX_PARSE_BYTES``
    and binary files (NUL byte in the first 8 KiB) are skipped. Source is
    decoded as UTF-8 with replacement. Syntax errors never stop extraction:
    tree-sitter recovers, the outcome is ``PARSED_WITH_ERRORS`` and the error
    lines are recorded.
"""
from __future__ import annotations

import abc
import ast
import dataclasses
import inspect
import logging
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

import tree_sitter_python
from django.conf import settings
from tree_sitter import Language, Node, Parser, Tree

from apps.ingestion.doc_extractor import (
    DependencyRecord,
    DocClaim,
    DocHeading,
    ManifestError,
    extract_dependencies,
    extract_markdown,
    is_markdown_path,
    manifest_kind_for,
)
from apps.ingestion.evidence import (
    MARKER_IN_COMMENT_RE,
    CommentMarker,
    Evidence,
    EvidenceKind,
    FingerprintAllocator,
    MarkerDetection,
    SourceText,
    build_marker,
    clean_marker_text,
    normalize_repo_path,
    scan_line_markers,
)
from apps.ingestion.git_forensics import BranchReport, ChangeType, GitForensicService

logger = logging.getLogger(__name__)

PYTHON: str = "python"
BINARY_SNIFF_BYTES: int = 8192
MAX_ERROR_LINES: int = 20
MAX_SIGNATURE_CHARS: int = 500
MAX_DOCSTRING_CHARS: int = 2000

# Directory names whose contents are vendored or generated.
VENDORED_DIRECTORIES: frozenset[str] = frozenset({
    "node_modules", ".venv", "venv", "dist", "build", "__pycache__", "vendor", "site-packages",
    ".git", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".eggs",
})
LOCKFILE_NAMES: frozenset[str] = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "poetry.lock", "pipfile.lock", "pdm.lock", "uv.lock", "cargo.lock", "composer.lock",
    "gemfile.lock", "go.sum", "packages.lock.json",
})
GENERATED_SUFFIXES: tuple[str, ...] = (".min.js", ".min.css", ".min.mjs", ".js.map", ".css.map")


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
class ParseStatus(StrEnum):
    PARSED = "PARSED"
    PARSED_WITH_ERRORS = "PARSED_WITH_ERRORS"
    SKIPPED_VENDORED = "SKIPPED_VENDORED"
    SKIPPED_TOO_LARGE = "SKIPPED_TOO_LARGE"
    SKIPPED_BINARY = "SKIPPED_BINARY"
    SKIPPED_UNREADABLE = "SKIPPED_UNREADABLE"
    UNSUPPORTED_LANGUAGE = "UNSUPPORTED_LANGUAGE"
    GRAMMAR_UNAVAILABLE = "GRAMMAR_UNAVAILABLE"


SKIPPED_STATUSES: frozenset[ParseStatus] = frozenset({
    ParseStatus.SKIPPED_VENDORED,
    ParseStatus.SKIPPED_TOO_LARGE,
    ParseStatus.SKIPPED_BINARY,
    ParseStatus.SKIPPED_UNREADABLE,
})


@dataclass(frozen=True)
class ParseOutcome:
    """How one file was handled. ``language`` is None for skipped or unrecognized files."""

    file_path: str
    status: ParseStatus
    language: str | None
    reason: str
    size_bytes: int
    has_syntax_errors: bool = False
    error_lines: tuple[int, ...] = ()


@dataclass(frozen=True, kw_only=True)
class CodeSymbol(Evidence):
    """A module docstring, class, function, method or module-level constant.

    ``signature`` is the header with whitespace collapsed (``async def
    f(self, y) -> int``); the snippet keeps the exact text.
    """

    qualified_name: str
    parent: str | None
    language: str
    signature: str
    decorators: tuple[str, ...] = ()
    bases: tuple[str, ...] = ()
    is_async: bool = False
    is_test: bool = False
    is_stub: bool = False
    is_abstract: bool = False
    docstring: str | None = None


@dataclass(frozen=True)
class ImportedName:
    name: str
    alias: str | None = None


@dataclass(frozen=True, kw_only=True)
class ImportRecord(Evidence):
    """``module`` is dotted, without the leading dots of a relative import (``level`` counts them).

    A plain ``import a, b`` statement yields one record per module.
    """

    module: str
    names: tuple[ImportedName, ...]
    alias: str | None
    level: int
    enclosing_symbol: str | None
    language: str

    @property
    def is_relative(self) -> bool:
        return self.level > 0


@dataclass(frozen=True, kw_only=True)
class RouteRecord(Evidence):
    """An HTTP route declared by a decorator; lines run from the decorator to the ``def`` line."""

    methods: tuple[str, ...]
    path: str
    handler: str
    router: str


@dataclass(frozen=True, kw_only=True)
class StubRecord(Evidence):
    """A ``raise NotImplementedError`` site: evidence of a partial or abandoned feature."""

    enclosing_symbol: str | None
    exception: str
    message: str | None
    in_abstract_method: bool


@dataclass(frozen=True)
class FileInventory:
    outcome: ParseOutcome
    symbols: tuple[CodeSymbol, ...] = ()
    imports: tuple[ImportRecord, ...] = ()
    routes: tuple[RouteRecord, ...] = ()
    stubs: tuple[StubRecord, ...] = ()
    markers: tuple[CommentMarker, ...] = ()
    headings: tuple[DocHeading, ...] = ()
    claims: tuple[DocClaim, ...] = ()
    dependencies: tuple[DependencyRecord, ...] = ()

    @property
    def file_path(self) -> str:
        return self.outcome.file_path

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        """Every record in the file, ordered by position."""
        items: list[Evidence] = [
            *self.symbols, *self.imports, *self.routes, *self.stubs,
            *self.markers, *self.headings, *self.claims, *self.dependencies,
        ]
        return tuple(sorted(items, key=lambda item: (item.line_start, item.line_end)))

    def symbol(self, qualified_name: str) -> CodeSymbol | None:
        return next((item for item in self.symbols if item.qualified_name == qualified_name), None)


class InventoryScope(StrEnum):
    FULL_TREE = "FULL_TREE"  # every file at the revision
    BRANCH_DIFF = "BRANCH_DIFF"  # only files a branch added or changed since its merge-base


@dataclass(frozen=True)
class RevisionInventory:
    revision: str
    commit_sha: str
    scope: InventoryScope
    files: tuple[FileInventory, ...]

    def file(self, path: str) -> FileInventory | None:
        return next((item for item in self.files if item.file_path == path), None)

    def _flatten(self, attribute: str) -> tuple[Any, ...]:
        return tuple(record for item in self.files for record in getattr(item, attribute))

    @property
    def symbols(self) -> tuple[CodeSymbol, ...]:
        return self._flatten("symbols")

    @property
    def imports(self) -> tuple[ImportRecord, ...]:
        return self._flatten("imports")

    @property
    def routes(self) -> tuple[RouteRecord, ...]:
        return self._flatten("routes")

    @property
    def stubs(self) -> tuple[StubRecord, ...]:
        return self._flatten("stubs")

    @property
    def markers(self) -> tuple[CommentMarker, ...]:
        return self._flatten("markers")

    @property
    def headings(self) -> tuple[DocHeading, ...]:
        return self._flatten("headings")

    @property
    def claims(self) -> tuple[DocClaim, ...]:
        return self._flatten("claims")

    @property
    def dependencies(self) -> tuple[DependencyRecord, ...]:
        return self._flatten("dependencies")

    def symbol(self, qualified_name: str, file_path: str | None = None) -> CodeSymbol | None:
        return next(
            (
                item
                for item in self.symbols
                if item.qualified_name == qualified_name and (file_path is None or item.file_path == file_path)
            ),
            None,
        )

    @property
    def status_counts(self) -> dict[ParseStatus, int]:
        return dict(Counter(item.outcome.status for item in self.files))


# ---------------------------------------------------------------------------
# Languages and grammars
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LanguageSpec:
    """Node types for best-effort extraction in a non-Python grammar.

    Nodes in ``function_nodes`` become METHOD inside a class and FUNCTION
    elsewhere; ``method_nodes`` are always METHOD. Names come from the node's
    ``name`` field (nodes without one are skipped).
    """

    name: str
    class_nodes: frozenset[str]
    function_nodes: frozenset[str] = frozenset()
    method_nodes: frozenset[str] = frozenset()
    import_nodes: frozenset[str] = frozenset()
    comment_nodes: frozenset[str] = frozenset({"comment", "line_comment", "block_comment"})


def _spec(name: str, classes: str, functions: str = "", methods: str = "", imports: str = "") -> LanguageSpec:
    return LanguageSpec(
        name=name,
        class_nodes=frozenset(classes.split()),
        function_nodes=frozenset(functions.split()),
        method_nodes=frozenset(methods.split()),
        import_nodes=frozenset(imports.split()),
    )


_TS_CLASSES = "class_declaration abstract_class_declaration interface_declaration enum_declaration"
LANGUAGE_SPECS: Mapping[str, LanguageSpec] = {
    "javascript": _spec(
        "javascript", "class_declaration", "function_declaration generator_function_declaration",
        "method_definition", "import_statement",
    ),
    "typescript": _spec(
        "typescript", _TS_CLASSES, "function_declaration generator_function_declaration",
        "method_definition", "import_statement",
    ),
    "tsx": _spec(
        "tsx", _TS_CLASSES, "function_declaration generator_function_declaration",
        "method_definition", "import_statement",
    ),
    "go": _spec("go", "type_spec", "function_declaration", "method_declaration", "import_spec"),
    "java": _spec(
        "java", "class_declaration interface_declaration enum_declaration record_declaration", "",
        "method_declaration constructor_declaration", "import_declaration",
    ),
    "ruby": _spec("ruby", "class module", "method singleton_method"),
    "rust": _spec("rust", "struct_item enum_item trait_item union_item", "function_item", "", "use_declaration"),
    "csharp": _spec(
        "csharp", "class_declaration interface_declaration struct_declaration enum_declaration record_declaration",
        "", "method_declaration constructor_declaration", "using_directive",
    ),
    "php": _spec(
        "php", "class_declaration interface_declaration trait_declaration", "function_definition",
        "method_declaration", "namespace_use_declaration",
    ),
}

EXTENSION_LANGUAGES: Mapping[str, str] = {
    ".py": PYTHON, ".pyi": PYTHON, ".pyw": PYTHON,
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".go": "go", ".java": "java", ".rb": "ruby", ".rs": "rust", ".cs": "csharp", ".php": "php",
}


def language_for_path(path: str) -> str | None:
    """Extractor language for ``path`` by extension, or None if there is no extractor for it."""
    return EXTENSION_LANGUAGES.get(PurePosixPath(path).suffix.lower())


class GrammarUnavailableError(RuntimeError):
    """A grammar is not cached, could not be downloaded, or could not be loaded."""


GrammarLoader = Callable[[str], Language]

_PYTHON_LANGUAGE: Language | None = None
_PACK_LANGUAGES: dict[str, Language] = {}
_LANGUAGE_LOCK = threading.Lock()


def python_language() -> Language:
    """The bundled tree-sitter-python grammar (always available offline)."""
    global _PYTHON_LANGUAGE
    with _LANGUAGE_LOCK:
        if _PYTHON_LANGUAGE is None:
            _PYTHON_LANGUAGE = Language(tree_sitter_python.language())
        return _PYTHON_LANGUAGE


def cached_pack_languages() -> frozenset[str]:
    """Grammars already in tree-sitter-language-pack's local cache (no network access)."""
    import tree_sitter_language_pack as pack

    return frozenset(pack.downloaded_languages())


def load_pack_language(name: str, *, allow_download: bool) -> Language:
    """Load ``name`` from tree-sitter-language-pack; downloads only when ``allow_download``."""
    with _LANGUAGE_LOCK:
        cached = _PACK_LANGUAGES.get(name)
    if cached is not None:
        return cached
    try:
        import tree_sitter_language_pack as pack

        if not allow_download and name not in cached_pack_languages():
            raise GrammarUnavailableError(
                f"the {name} grammar is not in the local tree-sitter-language-pack cache ({pack.cache_dir()}) "
                "and INGEST_ALLOW_GRAMMAR_DOWNLOAD is off"
            )
        language = pack.get_language(name)
    except GrammarUnavailableError:
        raise
    except Exception as exc:  # ImportError, DownloadError, DynamicLoadError, LanguageNotFoundError, ...
        raise GrammarUnavailableError(f"could not load the {name} grammar: {type(exc).__name__}: {exc}") from exc
    if not isinstance(language, Language):
        raise GrammarUnavailableError(f"tree-sitter-language-pack returned {type(language).__name__} for {name}")
    with _LANGUAGE_LOCK:
        _PACK_LANGUAGES[name] = language
    return language


class GrammarRegistry:
    """Parsers per language; a failed grammar is logged once and remembered for the registry's lifetime."""

    def __init__(self, loader: GrammarLoader) -> None:
        self._loader = loader
        self._parsers: dict[str, Parser] = {}
        self._failures: dict[str, str] = {}

    def parser(self, language: str) -> Parser:
        """A parser for ``language``; raises ``GrammarUnavailableError`` (never for Python)."""
        existing = self._parsers.get(language)
        if existing is not None:
            return existing
        if language == PYTHON:
            parser = Parser(python_language())
        else:
            if language in self._failures:
                raise GrammarUnavailableError(self._failures[language])
            try:
                parser = Parser(self._loader(language))
            except Exception as exc:  # GrammarUnavailableError, or an ABI-incompatible grammar (ValueError)
                reason = str(exc) if isinstance(exc, GrammarUnavailableError) else (
                    f"could not create a {language} parser: {type(exc).__name__}: {exc}"
                )
                self._failures[language] = reason
                logger.warning("Grammar unavailable, falling back to line scanning: %s", reason)
                raise GrammarUnavailableError(reason) from exc
        self._parsers[language] = parser
        return parser


# ---------------------------------------------------------------------------
# Skip rules
# ---------------------------------------------------------------------------
def vendored_reason(path: str) -> str | None:
    """Why ``path`` is vendored/generated and must not be parsed, or None."""
    pure = PurePosixPath(path)
    for part in pure.parts[:-1]:
        if part in VENDORED_DIRECTORIES or part.endswith(".egg-info"):
            return f"inside vendored/generated directory {part}/"
    name = pure.name.lower()
    if name in LOCKFILE_NAMES:
        return "lockfile"
    if name.endswith(GENERATED_SUFFIXES):
        return "minified or generated asset"
    return None


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_SNIFF_BYTES]


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------
_COLLAPSE_RE = re.compile(r"\s+")
_CONSTANT_NAME_RE = re.compile(r"^_*[A-Z][A-Z0-9_]*$")
_COMMENT_PREFIX_RE = re.compile(r"^\s*(?:#+|//+|/\*+|\*+|--+|;+)?")
ROUTE_DECORATORS: frozenset[str] = frozenset({
    "get", "post", "put", "patch", "delete", "head", "options", "trace", "route", "api_route", "websocket",
})
ROUTE_OBJECT_HINTS: tuple[str, ...] = ("app", "router", "api", "bp", "blueprint")


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", "replace")


def _collapse(text: str, limit: int = MAX_SIGNATURE_CHARS) -> str:
    return _COLLAPSE_RE.sub(" ", text).strip()[:limit]


def _last_row(node: Node) -> int:
    """0-based row of the node's last character (a node ending at column 0 ends on the previous row)."""
    row, column = node.end_point
    return row - 1 if column == 0 and row > node.start_point[0] else row


def _end_line(node: Node, column: int) -> int:
    """1-based last line of a definition, ignoring trailing comments dedented to ``column`` or less.

    tree-sitter attaches a comment that follows a block to that block even
    when it is written at the outer indentation, which would stretch the
    definition over a comment that belongs to the next one.
    """
    current = node
    while True:
        chosen = None
        for child in reversed(current.children):
            if child.type == "comment" and child.start_point[1] <= column:
                continue
            chosen = child
            break
        if chosen is None:
            return _last_row(current) + 1
        if chosen.child_count == 0 or chosen.type in ("comment", "string"):
            return _last_row(chosen) + 1
        current = chosen


def _string_value(node: Node | None) -> str | None:
    """Value of a plain string literal node; None for f-strings, bytes and anything else."""
    if node is None or node.type != "string":
        return None
    try:
        value = ast.literal_eval(_text(node))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, str) else None


def _named(node: Node | None) -> list[Node]:
    return [] if node is None else [child for child in node.named_children if child.type != "comment"]


@dataclass(frozen=True)
class _Scope:
    qualified_name: str
    kind: EvidenceKind
    line_start: int
    line_end: int
    is_test_class: bool
    is_abstract: bool
    symbol_index: int


@dataclass
class _Comment:
    line: int
    column: int
    lines: list[str]  # comment text per source line, comment syntax removed
    standalone: bool
    scope: tuple[_Scope, ...]


def _innermost(scope: Sequence[_Scope], line: int) -> _Scope | None:
    return next((item for item in reversed(scope) if item.line_start <= line <= item.line_end), None)


def _is_python_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def module_name_for(path: str) -> str:
    """``paypulse/currency.py`` -> ``paypulse.currency``; ``pkg/__init__.py`` -> ``pkg``."""
    pure = PurePosixPath(path)
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__" and len(parts) > 1:
        parts.pop()
    return ".".join(parts)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class _Extractor(abc.ABC):
    """Shared state for walking one syntax tree."""

    def __init__(self, source: SourceText, file_path: str, language: str, allocate: FingerprintAllocator) -> None:
        self.source = source
        self.file_path = file_path
        self.language = language
        self.allocate = allocate
        self.symbols: list[CodeSymbol] = []
        self.imports: list[ImportRecord] = []
        self.routes: list[RouteRecord] = []
        self.stubs: list[StubRecord] = []
        self.comments: list[_Comment] = []
        self.error_lines: list[int] = []

    @abc.abstractmethod
    def run(self, root: Node) -> None:
        """Walk the tree from ``root`` and fill the record lists."""

    def snippet(self, line_start: int, line_end: int) -> tuple[str, bool]:
        return self.source.snippet(line_start, min(line_end, self.source.line_count))

    def note_error(self, node: Node) -> None:
        if (node.type == "ERROR" or node.is_missing) and len(self.error_lines) < MAX_ERROR_LINES:
            line = node.start_point[0] + 1
            if line not in self.error_lines:
                self.error_lines.append(line)

    def add_symbol(self, **fields: object) -> int:
        line_start = int(fields["line_start"])  # type: ignore[arg-type]
        line_end = int(fields["line_end"])  # type: ignore[arg-type]
        snippet, truncated = self.snippet(line_start, line_end)
        kind = fields["kind"]
        self.symbols.append(
            CodeSymbol(
                file_path=self.file_path,
                snippet=snippet,
                snippet_truncated=truncated,
                fingerprint=self.allocate(kind, str(fields["qualified_name"])),  # type: ignore[arg-type]
                language=self.language,
                **fields,  # type: ignore[arg-type]
            )
        )
        return len(self.symbols) - 1

    def collect_comment(self, node: Node, scope: tuple[_Scope, ...]) -> None:
        line, column = node.start_point[0] + 1, node.start_point[1]
        # Columns are byte offsets, so look at the line's bytes before the comment.
        data = self.source.data
        prefix = data[data.rfind(b"\n", 0, node.start_byte) + 1 : node.start_byte]
        raw_lines = _text(node).split("\n")
        cleaned = [_COMMENT_PREFIX_RE.sub("", raw.rstrip("\r"), count=1) for raw in raw_lines]
        self.comments.append(
            _Comment(line=line, column=column, lines=cleaned, standalone=not prefix.strip(), scope=scope)
        )

    def markers(self) -> list[CommentMarker]:
        markers: list[CommentMarker] = []
        comments = self.comments
        for index, comment in enumerate(comments):
            for offset, body in enumerate(comment.lines):
                match = MARKER_IN_COMMENT_RE.search(body)
                if match is None:
                    continue
                line_start = comment.line + offset
                line_end = line_start
                parts = [clean_marker_text(match["text"])]
                if len(comment.lines) == 1:
                    # A note continues over directly following standalone comments at the same column.
                    following = index + 1
                    while following < len(comments):
                        nxt = comments[following]
                        body_next = clean_marker_text(nxt.lines[0]) if len(nxt.lines) == 1 else ""
                        if not (
                            comment.standalone
                            and nxt.standalone
                            and nxt.line == line_end + 1
                            and nxt.column == comment.column
                            and body_next
                            and MARKER_IN_COMMENT_RE.search(nxt.lines[0]) is None
                        ):
                            break
                        parts.append(body_next)
                        line_end = nxt.line
                        following += 1
                else:
                    # Inside a block comment, following lines without a marker continue the note.
                    for extra in comment.lines[offset + 1 :]:
                        text = clean_marker_text(extra)
                        if not text or MARKER_IN_COMMENT_RE.search(extra):
                            break
                        parts.append(text)
                        line_end += 1
                enclosing = _innermost(comment.scope, line_start)
                markers.append(
                    build_marker(
                        self.source,
                        self.file_path,
                        self.allocate,
                        tag=match["tag"],
                        text=" ".join(part for part in parts if part),
                        owner=match["owner"],
                        line_start=line_start,
                        line_end=line_end,
                        enclosing_symbol=enclosing.qualified_name if enclosing else None,
                        detection=MarkerDetection.COMMENT_NODE,
                    )
                )
        return markers


class _PythonExtractor(_Extractor):
    def __init__(self, source: SourceText, file_path: str, allocate: FingerprintAllocator) -> None:
        super().__init__(source, file_path, PYTHON, allocate)
        self.is_test_file = _is_python_test_file(file_path)
        self.stub_symbol_indexes: set[int] = set()

    def run(self, root: Node) -> None:
        self._module_docstring(root)
        stack: list[tuple[Node, tuple[_Scope, ...]]] = [(child, ()) for child in reversed(root.children)]
        while stack:
            node, scope = stack.pop()
            self.note_error(node)
            kind = node.type
            if kind in ("decorated_definition", "function_definition", "class_definition"):
                definition = node.child_by_field_name("definition") if kind == "decorated_definition" else node
                if definition is not None and definition.type in ("function_definition", "class_definition"):
                    inner = self._definition(node, definition, scope)
                    if inner is not None:
                        children: list[tuple[Node, tuple[_Scope, ...]]] = []
                        if node != definition:  # decorators (and comments between them) stay outside
                            children.extend((child, scope) for child in node.children if child != definition)
                        # tree-sitter may hang a comment written after the header's ":" on the
                        # definition node instead of its body, so the whole definition is inner scope.
                        children.extend((part, inner) for part in definition.children)
                        stack.extend(reversed(children))
                        continue
            elif kind in ("import_statement", "import_from_statement", "future_import_statement"):
                self._import(node, scope)
                continue
            elif kind == "comment":
                self.collect_comment(node, scope)
                continue
            elif kind == "raise_statement":
                self._raise(node, scope)
            elif kind == "expression_statement" and not scope and node.parent is not None:
                self._constant(node)
            stack.extend((child, scope) for child in reversed(node.children))
        for index in self.stub_symbol_indexes:
            self.symbols[index] = dataclasses.replace(self.symbols[index], is_stub=True)

    # -- pieces -------------------------------------------------------------
    @staticmethod
    def _docstring_node(block: Node | None) -> Node | None:
        statements = _named(block)
        if not statements or statements[0].type != "expression_statement":
            return None
        parts = _named(statements[0])
        return parts[0] if len(parts) == 1 and parts[0].type == "string" else None

    def _docstring(self, block: Node | None) -> str | None:
        value = _string_value(self._docstring_node(block))
        return inspect.cleandoc(value)[:MAX_DOCSTRING_CHARS] if value is not None else None

    def _module_docstring(self, root: Node) -> None:
        node = self._docstring_node(root)
        value = _string_value(node)
        if node is None or value is None:
            return
        name = module_name_for(self.file_path)
        self.add_symbol(
            kind=EvidenceKind.MODULE_DOCSTRING,
            name=name,
            qualified_name=name,
            parent=None,
            signature="",
            line_start=node.start_point[0] + 1,
            line_end=_last_row(node) + 1,
            docstring=inspect.cleandoc(value)[:MAX_DOCSTRING_CHARS],
        )

    def _definition(self, outer: Node, definition: Node, scope: tuple[_Scope, ...]) -> tuple[_Scope, ...] | None:
        name = _text(definition.child_by_field_name("name"))
        if not name:
            return None
        parent = scope[-1] if scope else None
        qualified = f"{parent.qualified_name}.{name}" if parent else name
        is_class = definition.type == "class_definition"
        if is_class:
            kind = EvidenceKind.CLASS
        elif parent is not None and parent.kind is EvidenceKind.CLASS:
            kind = EvidenceKind.METHOD
        else:
            kind = EvidenceKind.FUNCTION
        decorator_nodes = [child for child in outer.children if child.type == "decorator"]
        decorators = tuple(_collapse(_text(node).lstrip("@")) for node in decorator_nodes)
        body = definition.child_by_field_name("body")
        # The header ends at the ":" token before the body (a comment after it is not signature).
        colon = next((child for child in reversed(definition.children) if child.type == ":"), None)
        header_end = colon.start_byte if colon is not None else (body.start_byte if body else definition.end_byte)
        header = self.source.data[definition.start_byte : header_end].decode("utf-8", "replace")
        signature = _collapse(header)
        bases = tuple(_collapse(_text(base)) for base in _named(definition.child_by_field_name("superclasses")))
        is_abstract = any(item.split("(")[0].endswith("abstractmethod") for item in decorators)

        if is_class:
            is_test = self.is_test_file and name.startswith("Test") and (parent is None or parent.is_test_class)
        elif kind is EvidenceKind.METHOD:
            is_test = self.is_test_file and name.startswith("test") and parent is not None and parent.is_test_class
        else:
            is_test = self.is_test_file and name.startswith("test") and parent is None

        line_start = outer.start_point[0] + 1
        line_end = _end_line(outer, outer.start_point[1])
        index = self.add_symbol(
            kind=kind,
            name=name,
            qualified_name=qualified,
            parent=parent.qualified_name if parent else None,
            signature=signature,
            line_start=line_start,
            line_end=line_end,
            decorators=decorators,
            bases=bases,
            is_async=any(child.type == "async" for child in definition.children),
            is_test=is_test,
            is_abstract=is_abstract,
            docstring=self._docstring(body),
        )
        if not is_class:
            for decorator in decorator_nodes:
                self._route(decorator, definition, qualified)
        new_scope = _Scope(
            qualified_name=qualified,
            kind=kind,
            line_start=line_start,
            line_end=line_end,
            is_test_class=is_class and is_test,
            is_abstract=is_abstract,
            symbol_index=index,
        )
        return (*scope, new_scope)

    def _route(self, decorator: Node, definition: Node, handler: str) -> None:
        parts = _named(decorator)
        call = parts[0] if parts else None
        if call is None or call.type != "call":
            return
        function = call.child_by_field_name("function")
        if function is None or function.type != "attribute":
            return
        attribute = _text(function.child_by_field_name("attribute"))
        router = _collapse(_text(function.child_by_field_name("object")))
        if attribute not in ROUTE_DECORATORS:
            return
        arguments = _named(call.child_by_field_name("arguments"))
        positional = [arg for arg in arguments if arg.type != "keyword_argument"]
        keywords = {
            _text(arg.child_by_field_name("name")): arg.child_by_field_name("value")
            for arg in arguments
            if arg.type == "keyword_argument"
        }
        path_node = positional[0] if positional else keywords.get("path") or keywords.get("rule")
        path = _string_value(path_node)
        if path is None:
            return
        last_name = router.rsplit(".", 1)[-1].lower()
        if not path.startswith("/") and not any(hint in last_name for hint in ROUTE_OBJECT_HINTS):
            return
        if attribute in ("route", "api_route"):
            methods_node = keywords.get("methods")
            methods = tuple(
                value.upper() for value in (_string_value(item) for item in _named(methods_node)) if value
            ) or ("GET",)
        elif attribute == "websocket":
            methods = ("WEBSOCKET",)
        else:
            methods = (attribute.upper(),)
        line_start = decorator.start_point[0] + 1
        line_end = definition.start_point[0] + 1
        snippet, truncated = self.snippet(line_start, line_end)
        self.routes.append(
            RouteRecord(
                kind=EvidenceKind.ROUTE,
                name=f"{'|'.join(methods)} {path}",
                file_path=self.file_path,
                line_start=line_start,
                line_end=line_end,
                snippet=snippet,
                snippet_truncated=truncated,
                fingerprint=self.allocate(EvidenceKind.ROUTE, f"{'|'.join(methods)} {path}"),
                methods=methods,
                path=path,
                handler=handler,
                router=router,
            )
        )

    def _import(self, node: Node, scope: tuple[_Scope, ...]) -> None:
        enclosing = scope[-1].qualified_name if scope else None
        line_start, line_end = node.start_point[0] + 1, _last_row(node) + 1

        def add(module: str, names: tuple[ImportedName, ...], alias: str | None, level: int, identity: str) -> None:
            snippet, truncated = self.snippet(line_start, line_end)
            self.imports.append(
                ImportRecord(
                    kind=EvidenceKind.IMPORT,
                    name="." * level + module,
                    file_path=self.file_path,
                    line_start=line_start,
                    line_end=line_end,
                    snippet=snippet,
                    snippet_truncated=truncated,
                    fingerprint=self.allocate(EvidenceKind.IMPORT, identity),
                    module=module,
                    names=names,
                    alias=alias,
                    level=level,
                    enclosing_symbol=enclosing,
                    language=PYTHON,
                )
            )

        def imported(item: Node) -> ImportedName:
            if item.type == "aliased_import":
                return ImportedName(
                    _text(item.child_by_field_name("name")), _text(item.child_by_field_name("alias")) or None
                )
            return ImportedName(_text(item))

        if node.type == "import_statement":
            for item in node.children_by_field_name("name"):
                entry = imported(item)
                add(entry.name, (), entry.alias, 0, f"import:{entry.name}")
            return
        if node.type == "future_import_statement":
            module, level = "__future__", 0
        else:
            module_node = node.child_by_field_name("module_name")
            if module_node is not None and module_node.type == "relative_import":
                prefix = next((child for child in module_node.children if child.type == "import_prefix"), None)
                level = _text(prefix).count(".")
                module = _text(next((child for child in module_node.children if child.type == "dotted_name"), None))
            else:
                module, level = _text(module_node), 0
        names = tuple(imported(item) for item in node.children_by_field_name("name"))
        if any(child.type == "wildcard_import" for child in node.children):
            names = (*names, ImportedName("*"))
        add(module, names, None, level, f"from:{level}:{module}")

    def _raise(self, node: Node, scope: tuple[_Scope, ...]) -> None:
        parts = _named(node)
        if not parts:
            return
        raised = parts[0]
        target = raised.child_by_field_name("function") if raised.type == "call" else raised
        if target is None:
            return
        if target.type == "attribute":
            target = target.child_by_field_name("attribute")
        if _text(target) != "NotImplementedError":
            return
        message = None
        if raised.type == "call":
            arguments = _named(raised.child_by_field_name("arguments"))
            message = _string_value(arguments[0]) if arguments else None
        owner = next((item for item in reversed(scope) if item.kind is not EvidenceKind.CLASS), None)
        owner = owner or (scope[-1] if scope else None)
        if owner is not None:
            self.stub_symbol_indexes.add(owner.symbol_index)
        line_start, line_end = node.start_point[0] + 1, _last_row(node) + 1
        snippet, truncated = self.snippet(line_start, line_end)
        enclosing = owner.qualified_name if owner else None
        self.stubs.append(
            StubRecord(
                kind=EvidenceKind.STUB,
                name=enclosing or "<module>",
                file_path=self.file_path,
                line_start=line_start,
                line_end=line_end,
                snippet=snippet,
                snippet_truncated=truncated,
                fingerprint=self.allocate(EvidenceKind.STUB, f"{enclosing or '<module>'}:NotImplementedError"),
                enclosing_symbol=enclosing,
                exception="NotImplementedError",
                message=message,
                in_abstract_method=bool(owner and owner.is_abstract),
            )
        )

    def _constant(self, node: Node) -> None:
        parts = _named(node)
        if len(parts) != 1 or parts[0].type != "assignment":
            return
        target = parts[0].child_by_field_name("left")
        if target is None or target.type != "identifier":
            return
        name = _text(target)
        if not _CONSTANT_NAME_RE.match(name):
            return
        self.add_symbol(
            kind=EvidenceKind.CONSTANT,
            name=name,
            qualified_name=name,
            parent=None,
            signature=_collapse(_text(parts[0]), 200),
            line_start=node.start_point[0] + 1,
            line_end=_last_row(node) + 1,
        )


class _GenericExtractor(_Extractor):
    def __init__(self, source: SourceText, file_path: str, spec: LanguageSpec, allocate: FingerprintAllocator) -> None:
        super().__init__(source, file_path, spec.name, allocate)
        self.spec = spec

    def run(self, root: Node) -> None:
        spec = self.spec
        definitions = spec.class_nodes | spec.function_nodes | spec.method_nodes
        stack: list[tuple[Node, tuple[_Scope, ...]]] = [(child, ()) for child in reversed(root.children)]
        while stack:
            node, scope = stack.pop()
            self.note_error(node)
            kind = node.type
            if kind in spec.comment_nodes:
                self.collect_comment(node, scope)
                continue
            if kind in spec.import_nodes:
                self._import(node, scope)
                continue
            if kind in definitions:
                inner = self._definition(node, scope)
                if inner is not None:
                    stack.extend((child, inner) for child in reversed(node.children))
                    continue
            stack.extend((child, scope) for child in reversed(node.children))

    def _definition(self, node: Node, scope: tuple[_Scope, ...]) -> tuple[_Scope, ...] | None:
        name = _text(node.child_by_field_name("name"))
        if not name:
            return None
        parent = scope[-1] if scope else None
        if node.type in self.spec.class_nodes:
            kind = EvidenceKind.CLASS
        elif node.type in self.spec.method_nodes or (parent is not None and parent.kind is EvidenceKind.CLASS):
            kind = EvidenceKind.METHOD
        else:
            kind = EvidenceKind.FUNCTION
        qualified = f"{parent.qualified_name}.{name}" if parent else name
        body = node.child_by_field_name("body")
        header_end = body.start_byte if body is not None else node.end_byte
        signature = _collapse(self.source.data[node.start_byte : header_end].decode("utf-8", "replace"))
        line_start, line_end = node.start_point[0] + 1, _last_row(node) + 1
        index = self.add_symbol(
            kind=kind,
            name=name,
            qualified_name=qualified,
            parent=parent.qualified_name if parent else None,
            signature=signature,
            line_start=line_start,
            line_end=line_end,
        )
        new_scope = _Scope(qualified, kind, line_start, line_end, False, False, index)
        return (*scope, new_scope)

    def _import(self, node: Node, scope: tuple[_Scope, ...]) -> None:
        source_node = node.child_by_field_name("source") or node.child_by_field_name("path")
        if source_node is not None:
            module = _text(source_node).strip("\"'`")
        else:
            module = re.sub(r"^(?:import|use|using)\s+", "", _collapse(_text(node))).rstrip(";").strip()
        line_start, line_end = node.start_point[0] + 1, _last_row(node) + 1
        snippet, truncated = self.snippet(line_start, line_end)
        self.imports.append(
            ImportRecord(
                kind=EvidenceKind.IMPORT,
                name=module,
                file_path=self.file_path,
                line_start=line_start,
                line_end=line_end,
                snippet=snippet,
                snippet_truncated=truncated,
                fingerprint=self.allocate(EvidenceKind.IMPORT, f"import:{module}"),
                module=module,
                names=(),
                alias=None,
                level=0,
                enclosing_symbol=scope[-1].qualified_name if scope else None,
                language=self.spec.name,
            )
        )


def extract_with_spec(file_path: str, source: bytes, language: Language, spec: LanguageSpec) -> FileInventory:
    """Best-effort extraction of ``source`` with ``language`` and the node types in ``spec``."""
    path = normalize_repo_path(file_path)
    text = SourceText(source)
    tree = Parser(language).parse(text.data)
    return _inventory_from_tree(_GenericExtractor(text, path, spec, FingerprintAllocator(path)), tree, len(source))


def _inventory_from_tree(extractor: _Extractor, tree: Tree, size: int) -> FileInventory:
    extractor.run(tree.root_node)
    has_errors = tree.root_node.has_error
    outcome = ParseOutcome(
        file_path=extractor.file_path,
        status=ParseStatus.PARSED_WITH_ERRORS if has_errors else ParseStatus.PARSED,
        language=extractor.language,
        reason="syntax errors; extracted what parsed" if has_errors else "",
        size_bytes=size,
        has_syntax_errors=has_errors,
        error_lines=tuple(sorted(extractor.error_lines)),
    )
    return FileInventory(
        outcome=outcome,
        symbols=tuple(extractor.symbols),
        imports=tuple(extractor.imports),
        routes=tuple(extractor.routes),
        stubs=tuple(extractor.stubs),
        markers=tuple(extractor.markers()),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class ASTParserService:
    """Turns ``(path, source_bytes)`` into a ``FileInventory``. Not thread-safe: use one per thread."""

    def __init__(
        self,
        *,
        max_parse_bytes: int | None = None,
        allow_grammar_download: bool | None = None,
        grammar_loader: GrammarLoader | None = None,
    ) -> None:
        self.max_parse_bytes: int = (
            max_parse_bytes if max_parse_bytes is not None else settings.INGEST_MAX_PARSE_BYTES
        )
        if self.max_parse_bytes <= 0:
            raise ValueError("max_parse_bytes must be positive")
        allow = (
            allow_grammar_download if allow_grammar_download is not None else settings.INGEST_ALLOW_GRAMMAR_DOWNLOAD
        )
        loader = grammar_loader or (lambda name: load_pack_language(name, allow_download=allow))
        self._grammars = GrammarRegistry(loader)

    def skip_before_read(self, path: str, size: int) -> ParseOutcome | None:
        """The skip outcome decidable from path and size alone (before reading the blob), or None."""
        reason = vendored_reason(path)
        if reason is not None:
            return ParseOutcome(path, ParseStatus.SKIPPED_VENDORED, None, reason, size)
        if size > self.max_parse_bytes:
            return ParseOutcome(
                path, ParseStatus.SKIPPED_TOO_LARGE, None, f"{size} bytes > {self.max_parse_bytes} byte limit", size
            )
        return None

    def parse_file(self, path: str, source: bytes) -> FileInventory:
        file_path = normalize_repo_path(path)
        size = len(source)
        skipped = self.skip_before_read(file_path, size)
        if skipped is not None:
            return FileInventory(outcome=skipped)
        if is_binary(source):
            return FileInventory(
                outcome=ParseOutcome(file_path, ParseStatus.SKIPPED_BINARY, None, "NUL byte in the first 8 KiB", size)
            )
        text = SourceText(source)
        allocate = FingerprintAllocator(file_path)

        if is_markdown_path(file_path):
            headings, claims = extract_markdown(text, file_path, allocate)
            return FileInventory(
                outcome=ParseOutcome(file_path, ParseStatus.PARSED, "markdown", "", size),
                headings=tuple(headings),
                claims=tuple(claims),
                markers=tuple(scan_line_markers(text, file_path, allocate)),
            )

        manifest = manifest_kind_for(file_path)
        if manifest is not None:
            markers = tuple(scan_line_markers(text, file_path, allocate))
            language = manifest.value.lower()
            try:
                dependencies = extract_dependencies(text, file_path, allocate, manifest)
            except ManifestError as exc:
                return FileInventory(
                    outcome=ParseOutcome(
                        file_path, ParseStatus.PARSED_WITH_ERRORS, language, str(exc), size, has_syntax_errors=True
                    ),
                    markers=markers,
                )
            return FileInventory(
                outcome=ParseOutcome(file_path, ParseStatus.PARSED, language, "", size),
                dependencies=tuple(dependencies),
                markers=markers,
            )

        language = language_for_path(file_path)
        if language is None:
            return FileInventory(
                outcome=ParseOutcome(
                    file_path, ParseStatus.UNSUPPORTED_LANGUAGE, None, "no extractor for this file type", size
                ),
                markers=tuple(scan_line_markers(text, file_path, allocate)),
            )
        try:
            parser = self._grammars.parser(language)
        except GrammarUnavailableError as exc:
            return FileInventory(
                outcome=ParseOutcome(file_path, ParseStatus.GRAMMAR_UNAVAILABLE, language, str(exc), size),
                markers=tuple(scan_line_markers(text, file_path, allocate)),
            )
        tree = parser.parse(text.data)
        extractor: _Extractor = (
            _PythonExtractor(text, file_path, allocate)
            if language == PYTHON
            else _GenericExtractor(text, file_path, LANGUAGE_SPECS[language], allocate)
        )
        return _inventory_from_tree(extractor, tree, size)


# ---------------------------------------------------------------------------
# Repository level
# ---------------------------------------------------------------------------
def _parse_entries(
    service: GitForensicService,
    parser: ASTParserService,
    commit_sha: str,
    entries: Iterable[tuple[str, int | None]],
) -> Iterator[FileInventory]:
    for path, size in entries:
        if size is not None:
            skipped = parser.skip_before_read(path, size)
            if skipped is not None:
                yield FileInventory(outcome=skipped)
                continue
        data = service.read_file_at(commit_sha, path)
        if data is None:
            yield FileInventory(
                outcome=ParseOutcome(path, ParseStatus.SKIPPED_UNREADABLE, None, "not a regular file", size or 0)
            )
            continue
        yield parser.parse_file(path, data)


FileCallback = Callable[[FileInventory], None]


def _notify(inventories: Iterable[FileInventory], on_file: FileCallback | None) -> Iterator[FileInventory]:
    for inventory in inventories:
        if on_file is not None:
            on_file(inventory)
        yield inventory


def parse_revision(
    service: GitForensicService,
    revision: str,
    *,
    parser: ASTParserService | None = None,
    on_file: FileCallback | None = None,
) -> RevisionInventory:
    """Parse every regular file at ``revision`` (read from Git objects; the work tree is untouched).

    ``on_file`` is called with each ``FileInventory`` as soon as it is parsed (for progress reporting).
    """
    parser = parser or ASTParserService()
    commit_sha = service.resolve_commit_sha(revision)
    files = tuple(_notify(_parse_entries(service, parser, commit_sha, service.list_files_at(commit_sha)), on_file))
    return RevisionInventory(revision=revision, commit_sha=commit_sha, scope=InventoryScope.FULL_TREE, files=files)


def parse_branch_changes(
    service: GitForensicService,
    branch: BranchReport,
    *,
    parser: ASTParserService | None = None,
    on_file: FileCallback | None = None,
) -> RevisionInventory:
    """Parse, at the branch tip, only the files the branch added or modified since its merge-base.

    Files the branch did not touch are identical to the merge-base version,
    so storing them again per branch would only duplicate evidence. ``on_file``
    works as in ``parse_revision``.
    """
    if branch.diff is None:
        raise ValueError(f"{branch.name} has no diff against the default branch; use parse_revision")
    parser = parser or ASTParserService()
    entries = [(item.path, None) for item in branch.diff.files if item.change_type is not ChangeType.DELETED]
    files = tuple(_notify(_parse_entries(service, parser, branch.head_sha, entries), on_file))
    return RevisionInventory(
        revision=branch.name, commit_sha=branch.head_sha, scope=InventoryScope.BRANCH_DIFF, files=files
    )
