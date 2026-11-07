"""The shared evidence record shape for ingestion extractors.

Every item the extractors find (a symbol, an import, a route, a stub, a
comment marker, a README claim, a dependency pin) is a frozen ``Evidence``
subclass carrying the fields an ``EvidenceCitation`` needs:

``kind``, ``name``
    What the item is and a short human-readable label.
``file_path``
    Repository-relative POSIX path.
``line_start``, ``line_end``
    1-based, inclusive. Lines are separated by ``\\n`` only, exactly as
    tree-sitter counts rows, so CRLF files and non-ASCII text number the
    same way Git and editors do. (``str.splitlines`` also splits on ``\\r``,
    form feeds and Unicode separators, which would shift line numbers.)
``snippet``, ``snippet_truncated``
    The exact source text of those lines (each line's ``\\r\\n`` / ``\\n``
    terminator normalized to ``\\n``, decoded as UTF-8 with replacement),
    never paraphrased. Long spans keep their first lines, cut at a line
    boundary, and set ``snippet_truncated``; ``line_end`` still marks the
    real end.
``fingerprint``
    ``sha256(kind, file_path, identity)``, where the identity is the
    qualified name (code), the module (imports), the method and path
    (routes) or normalized text (markers, doc claims). Line numbers are never
    part of it, so the same item matches across revisions when unrelated
    lines move. Repeats of one identity in a file get ``#2``, ``#3``, ... in
    document order.

Nothing here touches the ORM, Git or the network.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

MAX_SNIPPET_CHARS: int = 4000
MAX_SNIPPET_LINES: int = 80
FINGERPRINT_HEX_CHARS: int = 32
UTF8_BOM: bytes = b"\xef\xbb\xbf"

_WINDOWS_DRIVE: re.Pattern[str] = re.compile(r"^[A-Za-z]:")


class EvidenceKind(StrEnum):
    MODULE_DOCSTRING = "MODULE_DOCSTRING"
    CLASS = "CLASS"
    FUNCTION = "FUNCTION"
    METHOD = "METHOD"
    CONSTANT = "CONSTANT"
    IMPORT = "IMPORT"
    ROUTE = "ROUTE"
    STUB = "STUB"
    MARKER = "MARKER"
    HEADING = "HEADING"
    DOC_CLAIM = "DOC_CLAIM"
    DEPENDENCY = "DEPENDENCY"


@dataclass(frozen=True, kw_only=True)
class Evidence:
    """Fields shared by every extracted record (see the module docstring)."""

    kind: EvidenceKind
    name: str
    file_path: str
    line_start: int
    line_end: int
    snippet: str
    snippet_truncated: bool
    fingerprint: str


def normalize_repo_path(path: str) -> str:
    """Return ``path`` as a clean repository-relative POSIX path, or raise ``ValueError``."""
    text = path.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or _WINDOWS_DRIVE.match(text) or ".." in pure.parts or "\0" in text:
        raise ValueError(f"not a repository-relative path: {path!r}")
    return str(pure)


def normalize_text(text: str) -> str:
    """Collapse whitespace and casefold: the identity used for text fingerprints."""
    return " ".join(text.split()).casefold()


def make_fingerprint(kind: EvidenceKind | str, file_path: str, identity: str) -> str:
    payload = "\x1f".join((str(kind), file_path, identity)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:FINGERPRINT_HEX_CHARS]


class FingerprintAllocator:
    """Fingerprints for one file; a repeated (kind, identity) gets ``#2``, ``#3``, ... in call order."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self._seen: Counter[tuple[str, str]] = Counter()

    def __call__(self, kind: EvidenceKind, identity: str) -> str:
        key = (str(kind), identity)
        self._seen[key] += 1
        occurrence = self._seen[key]
        return make_fingerprint(kind, self.file_path, identity if occurrence == 1 else f"{identity}#{occurrence}")


class SourceText:
    """A file's bytes split into ``\\n``-separated lines, the way tree-sitter numbers rows.

    A leading UTF-8 BOM is dropped from ``data`` (row numbers are unchanged).
    """

    __slots__ = ("data", "lines")

    def __init__(self, data: bytes) -> None:
        if data.startswith(UTF8_BOM):
            data = data[len(UTF8_BOM):]
        self.data: bytes = data
        raw = data.split(b"\n") if data else []
        if len(raw) > 1 and raw[-1] == b"":
            raw.pop()  # the final terminator does not start a new line
        # A multi-byte UTF-8 sequence never contains 0x0A, so per-line decoding is exact.
        self.lines: list[str] = [line.removesuffix(b"\r").decode("utf-8", "replace") for line in raw]

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def line(self, number: int) -> str:
        """Text of 1-based line ``number`` without its terminator."""
        return self.lines[number - 1]

    def snippet(
        self,
        line_start: int,
        line_end: int,
        *,
        max_chars: int = MAX_SNIPPET_CHARS,
        max_lines: int = MAX_SNIPPET_LINES,
    ) -> tuple[str, bool]:
        """Exact text of lines ``line_start..line_end`` (inclusive), capped; returns (text, truncated)."""
        if not 1 <= line_start <= line_end <= self.line_count:
            raise ValueError(f"lines {line_start}-{line_end} outside 1-{self.line_count}")
        selected = self.lines[line_start - 1 : line_end]
        truncated = len(selected) > max_lines
        kept: list[str] = []
        size = 0
        for line in selected[:max_lines]:
            extra = len(line) + (1 if kept else 0)
            if size + extra > max_chars:
                if not kept:
                    kept.append(line[:max_chars])
                truncated = True
                break
            kept.append(line)
            size += extra
        return "\n".join(kept), truncated


# ---------------------------------------------------------------------------
# Comment markers (TODO / FIXME / HACK / XXX)
# ---------------------------------------------------------------------------
class MarkerTag(StrEnum):
    TODO = "TODO"
    FIXME = "FIXME"
    HACK = "HACK"
    XXX = "XXX"


class MarkerDetection(StrEnum):
    COMMENT_NODE = "COMMENT_NODE"  # found in a tree-sitter comment node (strings never count)
    LINE_SCAN = "LINE_SCAN"  # regex fallback for files without a grammar


@dataclass(frozen=True, kw_only=True)
class CommentMarker(Evidence):
    """A ``TODO``/``FIXME``/``HACK``/``XXX`` note. ``text`` follows the tag (continuation comment lines joined)."""

    tag: MarkerTag
    text: str
    owner: str | None
    enclosing_symbol: str | None
    detection: MarkerDetection


_TAGS: str = "|".join(tag.value for tag in MarkerTag)
# Applied to comment text with the comment syntax already removed.
MARKER_IN_COMMENT_RE: re.Pattern[str] = re.compile(
    rf"\b(?P<tag>{_TAGS})\b(?:\((?P<owner>[^)\n]*)\))?\s*[:\-]?\s*(?P<text>.*)$"
)
# Fallback for unparsed files: a tag right after a common comment leader...
_LINE_MARKER_RE: re.Pattern[str] = re.compile(
    rf"(?:#|//|/\*|<!--|--|;|%|\*)\s*(?P<tag>{_TAGS})\b(?:\((?P<owner>[^)\n]*)\))?\s*[:\-]?\s*(?P<text>.*)$"
)
# ...or a tag followed by a colon at the start of a line.
_LINE_START_MARKER_RE: re.Pattern[str] = re.compile(
    rf"^\s*(?P<tag>{_TAGS})(?:\((?P<owner>[^)\n]*)\))?:\s*(?P<text>.*)$"
)
_COMMENT_CLOSERS: re.Pattern[str] = re.compile(r"\s*(?:\*/|-->)\s*$")

Allocate = Callable[[EvidenceKind, str], str]


def clean_marker_text(text: str) -> str:
    return _COMMENT_CLOSERS.sub("", text).strip()


def build_marker(
    source: SourceText,
    file_path: str,
    allocate: Allocate,
    *,
    tag: str,
    text: str,
    owner: str | None,
    line_start: int,
    line_end: int,
    enclosing_symbol: str | None,
    detection: MarkerDetection,
) -> CommentMarker:
    marker_tag = MarkerTag(tag)
    snippet, truncated = source.snippet(line_start, line_end)
    owner = (owner or "").strip() or None
    return CommentMarker(
        kind=EvidenceKind.MARKER,
        name=f"{marker_tag}: {text}" if text else str(marker_tag),
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        snippet=snippet,
        snippet_truncated=truncated,
        fingerprint=allocate(EvidenceKind.MARKER, f"{marker_tag}:{normalize_text(text)}"),
        tag=marker_tag,
        text=text,
        owner=owner,
        enclosing_symbol=enclosing_symbol,
        detection=detection,
    )


def scan_line_markers(source: SourceText, file_path: str, allocate: Allocate) -> list[CommentMarker]:
    """Line-based marker fallback for files that have no syntax tree (one marker per line at most)."""
    markers: list[CommentMarker] = []
    for number, line in enumerate(source.lines, start=1):
        match = _LINE_MARKER_RE.search(line) or _LINE_START_MARKER_RE.match(line)
        if match is None:
            continue
        markers.append(
            build_marker(
                source,
                file_path,
                allocate,
                tag=match["tag"],
                text=clean_marker_text(match["text"]),
                owner=match["owner"],
                line_start=number,
                line_end=number,
                enclosing_symbol=None,
                detection=MarkerDetection.LINE_SCAN,
            )
        )
    return markers
