"""Markdown claims and dependency manifests as evidence records (pure functions).

Markdown (``*.md``, ``*.markdown``)
    ATX and setext headings become ``DocHeading``. Checklist items
    (``- [x] PDF Invoice Export``, ``* [ ] ...``, ``1. [x] ...``) anywhere in
    the file, and plain bullets under a feature-like heading ("Features",
    "Roadmap", "Capabilities", "Highlights", including its sub-headings),
    become ``DocClaim`` records with their checked state. Fenced code blocks
    are skipped. An indented line directly below an item continues it.

Dependency manifests
    ``requirements*.txt`` / ``requirements*.in`` (and ``*.txt`` inside a
    ``requirements/`` directory), ``pyproject.toml`` (PEP 621 dependencies and
    optional dependencies, PEP 735 dependency groups, Poetry dependency
    tables), ``package.json`` (dependencies, devDependencies,
    peerDependencies, optionalDependencies) and ``setup.cfg``
    (``install_requires``, ``extras_require``, ``tests_require``) become
    ``DependencyRecord`` rows. Nothing is looked up on a package index.

The record shape and fingerprint rules are in ``apps.ingestion.evidence``.
"""
from __future__ import annotations

import configparser
import json
import re
import tomllib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from apps.ingestion.evidence import Allocate, Evidence, EvidenceKind, SourceText, normalize_text

MARKDOWN_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown"})
FEATURE_HEADING_RE: re.Pattern[str] = re.compile(
    r"\b(features?|roadmap|capabilities|highlights)\b", re.IGNORECASE
)


class ManifestError(ValueError):
    """A dependency manifest could not be parsed (invalid TOML/JSON/INI)."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class DocHeading(Evidence):
    level: int
    text: str


class ClaimStyle(StrEnum):
    CHECKLIST = "CHECKLIST"  # "- [x] ..." / "- [ ] ..."
    BULLET = "BULLET"  # a plain list item under a feature-like heading


@dataclass(frozen=True, kw_only=True)
class DocClaim(Evidence):
    """A feature claim. ``checked`` is True/False for checklist items and None for plain bullets.

    The fingerprint uses the normalized text only, so a claim keeps its
    fingerprint when its box is ticked or unticked on another revision.
    """

    text: str
    checked: bool | None
    style: ClaimStyle
    section: str | None
    in_feature_section: bool


class ManifestKind(StrEnum):
    REQUIREMENTS_TXT = "REQUIREMENTS_TXT"
    PYPROJECT_TOML = "PYPROJECT_TOML"
    PACKAGE_JSON = "PACKAGE_JSON"
    SETUP_CFG = "SETUP_CFG"


class Ecosystem(StrEnum):
    PYPI = "PYPI"
    NPM = "NPM"


@dataclass(frozen=True, kw_only=True)
class DependencyRecord(Evidence):
    """One declared dependency. ``pinned_version`` is set only for an exact pin (``==1.2.3``, npm ``1.2.3``).

    The fingerprint uses the normalized name and group, not the specifier, so
    a version bump between revisions matches the same dependency.
    """

    normalized_name: str
    specifier: str
    pinned_version: str | None
    manifest: ManifestKind
    ecosystem: Ecosystem
    group: str

    @property
    def line(self) -> int:
        return self.line_start


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_ATX_RE = re.compile(r"^\s{0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_ATX_CLOSING_RE = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
_SETEXT_RE = re.compile(r"^\s{0,3}(=+|-+)[ \t]*$")
_THEMATIC_BREAK_RE = re.compile(r"^\s{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_LIST_ITEM_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<bullet>[-*+]|\d{1,9}[.)])[ \t]+(?:\[(?P<box>[ xX])\][ \t]+)?(?P<text>\S.*?)[ \t]*$"
)
_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_EMPHASIS_RE = re.compile(r"[*_`~]")


def is_markdown_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in MARKDOWN_SUFFIXES


def normalize_claim_text(text: str) -> str:
    """Claim identity: link labels kept, emphasis/code marks dropped, whitespace collapsed, casefolded."""
    return normalize_text(_MARKDOWN_EMPHASIS_RE.sub("", _MARKDOWN_LINK_RE.sub(r"\1", text)))


@dataclass
class _OpenItem:
    line_start: int
    line_end: int
    indent: int
    text: str
    checked: bool | None
    style: ClaimStyle
    section: str | None
    in_feature_section: bool


def extract_markdown(
    source: SourceText, file_path: str, allocate: Allocate
) -> tuple[list[DocHeading], list[DocClaim]]:
    headings: list[DocHeading] = []
    claims: list[DocClaim] = []
    section: str | None = None
    feature_level: int | None = None
    fence: str | None = None
    item: _OpenItem | None = None
    paragraph_line: int | None = None  # previous line, if it was plain paragraph text

    def add_heading(level: int, text: str, line_start: int, line_end: int) -> None:
        nonlocal section, feature_level
        snippet, truncated = source.snippet(line_start, line_end)
        headings.append(
            DocHeading(
                kind=EvidenceKind.HEADING,
                name=text,
                file_path=file_path,
                line_start=line_start,
                line_end=line_end,
                snippet=snippet,
                snippet_truncated=truncated,
                fingerprint=allocate(EvidenceKind.HEADING, f"{level}:{normalize_claim_text(text)}"),
                level=level,
                text=text,
            )
        )
        section = text
        if FEATURE_HEADING_RE.search(text):
            feature_level = level if feature_level is None else min(feature_level, level)
        elif feature_level is not None and level <= feature_level:
            feature_level = None

    def close_item() -> None:
        nonlocal item
        if item is None:
            return
        snippet, truncated = source.snippet(item.line_start, item.line_end)
        claims.append(
            DocClaim(
                kind=EvidenceKind.DOC_CLAIM,
                name=item.text,
                file_path=file_path,
                line_start=item.line_start,
                line_end=item.line_end,
                snippet=snippet,
                snippet_truncated=truncated,
                fingerprint=allocate(EvidenceKind.DOC_CLAIM, normalize_claim_text(item.text)),
                text=item.text,
                checked=item.checked,
                style=item.style,
                section=item.section,
                in_feature_section=item.in_feature_section,
            )
        )
        item = None

    for number, line in enumerate(source.lines, start=1):
        if fence is not None:
            stripped = line.strip()
            if stripped.startswith(fence[0] * len(fence)) and set(stripped) <= {fence[0]}:
                fence = None
            continue
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            close_item()
            fence = fence_match.group(1)
            paragraph_line = None
            continue
        if not line.strip():
            close_item()
            paragraph_line = None
            continue
        atx = _ATX_RE.match(line)
        if atx:
            close_item()
            text = _ATX_CLOSING_RE.sub("", atx.group(2) or "").strip()
            add_heading(len(atx.group(1)), text, number, number)
            paragraph_line = None
            continue
        setext = _SETEXT_RE.match(line)
        if setext and paragraph_line is not None and item is None:
            add_heading(1 if setext.group(1)[0] == "=" else 2, source.line(paragraph_line).strip(), paragraph_line, number)
            paragraph_line = None
            continue
        if _THEMATIC_BREAK_RE.match(line):
            close_item()
            paragraph_line = None
            continue
        list_item = _LIST_ITEM_RE.match(line)
        if list_item:
            close_item()
            paragraph_line = None
            box = list_item["box"]
            in_features = feature_level is not None
            if box is None and not in_features:
                continue
            item = _OpenItem(
                line_start=number,
                line_end=number,
                indent=len(list_item["indent"].expandtabs(4)),
                text=list_item["text"],
                checked=None if box is None else box in "xX",
                style=ClaimStyle.BULLET if box is None else ClaimStyle.CHECKLIST,
                section=section,
                in_feature_section=in_features,
            )
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        if item is not None and indent > item.indent:
            item.text = f"{item.text} {line.strip()}"
            item.line_end = number
            continue
        close_item()
        paragraph_line = number
    close_item()
    return headings, claims


# ---------------------------------------------------------------------------
# Dependency manifests
# ---------------------------------------------------------------------------
# PEP 508 name, optional extras, then the version specifier / URL and markers.
_REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?P<extras>\[[^\]]*\])?\s*(?P<rest>.*)$"
)
_PIP_EXACT_PIN_RE = re.compile(r"^===?\s*(?P<version>[A-Za-z0-9][A-Za-z0-9.+!_-]*)$")
_POETRY_EXACT_PIN_RE = re.compile(r"^(?:===?|=)?\s*(?P<version>\d[A-Za-z0-9.+!_-]*)$")
_NPM_EXACT_PIN_RE = re.compile(r"^=?v?(?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")
_PIP_COMMENT_RE = re.compile(r"(?:^|\s)#.*$")
_PEP503_RE = re.compile(r"[-_.]+")


def manifest_kind_for(path: str) -> ManifestKind | None:
    pure = PurePosixPath(path)
    name = pure.name.lower()
    if name == "pyproject.toml":
        return ManifestKind.PYPROJECT_TOML
    if name == "package.json":
        return ManifestKind.PACKAGE_JSON
    if name == "setup.cfg":
        return ManifestKind.SETUP_CFG
    if re.fullmatch(r"requirements[\w.-]*\.(?:txt|in)", name) or (
        pure.parent.name.lower() == "requirements" and pure.suffix.lower() in {".txt", ".in"}
    ):
        return ManifestKind.REQUIREMENTS_TXT
    return None


def canonical_pypi_name(name: str) -> str:
    """PEP 503 normalized project name."""
    return _PEP503_RE.sub("-", name).lower()


def _split_requirement(text: str) -> tuple[str, str] | None:
    """``"requests[socks]>=2 ; python_version<'3.8'"`` -> ``("requests", ">=2")``; None if not PEP 508."""
    match = _REQUIREMENT_RE.match(text.strip())
    if match is None:
        return None
    rest = re.split(r"\s--", match["rest"], maxsplit=1)[0]  # trailing pip options such as --hash
    specifier = rest.split(";", 1)[0].strip()
    if specifier and specifier[0] not in "<>=!~@(":
        return None  # not a requirement (e.g. a bare path or URL)
    return match["name"], specifier.strip("()").strip()


def _pip_pin(specifier: str) -> str | None:
    match = _PIP_EXACT_PIN_RE.match(specifier)
    return match["version"] if match else None


class _Recorder:
    def __init__(self, source: SourceText, file_path: str, allocate: Allocate, manifest: ManifestKind) -> None:
        self.source = source
        self.file_path = file_path
        self.allocate = allocate
        self.manifest = manifest
        self.records: list[DependencyRecord] = []

    def add(
        self,
        name: str,
        specifier: str,
        pinned: str | None,
        group: str,
        line_start: int,
        line_end: int | None = None,
        ecosystem: Ecosystem = Ecosystem.PYPI,
    ) -> None:
        line_end = line_end or line_start
        normalized = canonical_pypi_name(name) if ecosystem is Ecosystem.PYPI else name.lower()
        snippet, truncated = self.source.snippet(line_start, line_end)
        self.records.append(
            DependencyRecord(
                kind=EvidenceKind.DEPENDENCY,
                name=name,
                file_path=self.file_path,
                line_start=line_start,
                line_end=line_end,
                snippet=snippet,
                snippet_truncated=truncated,
                fingerprint=self.allocate(EvidenceKind.DEPENDENCY, f"{ecosystem}:{group}:{normalized}"),
                normalized_name=normalized,
                specifier=specifier,
                pinned_version=pinned,
                manifest=self.manifest,
                ecosystem=ecosystem,
                group=group,
            )
        )


class _LineFinder:
    """Locates declarations in a manifest's text (tomllib/json/configparser report no positions)."""

    def __init__(self, source: SourceText) -> None:
        self.lines = source.lines
        self._used: set[int] = set()

    def header(self, pattern: str) -> int | None:
        regex = re.compile(pattern)
        return next((i for i, line in enumerate(self.lines, start=1) if regex.search(line)), None)

    def find(self, pattern: str, start: int = 1, *, stop_at_table: bool = False) -> int | None:
        """First unused line >= ``start`` matching ``pattern``; with ``stop_at_table`` stop at the next ``[table]``."""
        regex = re.compile(pattern)
        for number in range(max(start, 1), len(self.lines) + 1):
            line = self.lines[number - 1]
            if stop_at_table and number > start and re.match(r"^\s*\[", line):
                return None
            if number not in self._used and regex.search(line):
                self._used.add(number)
                return number
        return None


def _parse_requirements_txt(rec: _Recorder) -> None:
    lines = rec.source.lines
    index = 0
    while index < len(lines):
        first = index + 1
        logical = lines[index]
        while logical.rstrip().endswith("\\") and index + 1 < len(lines):
            logical = logical.rstrip()[:-1] + " " + lines[index + 1]
            index += 1
        last = index + 1
        index += 1
        text = _PIP_COMMENT_RE.sub("", logical).strip()
        if not text or text.startswith("-"):
            continue  # blank, comment, or a pip option (-r, -c, -e, --index-url, ...)
        parsed = _split_requirement(text)
        if parsed is None:
            continue
        name, specifier = parsed
        rec.add(name, specifier, _pip_pin(specifier), "main", first, last)


def _quoted(text: str) -> str:
    return rf"""["']{re.escape(text)}["']"""


def _parse_pyproject(rec: _Recorder) -> None:
    try:
        document = tomllib.loads("\n".join(rec.source.lines))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"invalid TOML: {exc}") from exc
    finder = _LineFinder(rec.source)

    def pep508_list(entries: object, group: str) -> None:
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, str):
                continue  # PEP 735 {include-group = ...}
            parsed = _split_requirement(entry)
            if parsed is None:
                continue
            line = finder.find(_quoted(entry)) or 1
            rec.add(parsed[0], parsed[1], _pip_pin(parsed[1]), group, line)

    project = document.get("project")
    if isinstance(project, Mapping):
        pep508_list(project.get("dependencies"), "main")
        optional = project.get("optional-dependencies")
        if isinstance(optional, Mapping):
            for group, entries in optional.items():
                pep508_list(entries, str(group))
    groups = document.get("dependency-groups")
    if isinstance(groups, Mapping):
        for group, entries in groups.items():
            pep508_list(entries, str(group))

    poetry = document.get("tool", {}).get("poetry") if isinstance(document.get("tool"), Mapping) else None
    if not isinstance(poetry, Mapping):
        return
    tables: list[tuple[str, object, str]] = [
        ("main", poetry.get("dependencies"), r"tool\.poetry\.dependencies"),
        ("dev", poetry.get("dev-dependencies"), r"tool\.poetry\.dev-dependencies"),
    ]
    poetry_groups = poetry.get("group")
    if isinstance(poetry_groups, Mapping):
        for group, table in poetry_groups.items():
            if isinstance(table, Mapping):
                tables.append(
                    (str(group), table.get("dependencies"), rf"tool\.poetry\.group\.{re.escape(str(group))}\.dependencies")
                )
    for group, table, header_pattern in tables:
        if not isinstance(table, Mapping):
            continue
        header = finder.header(rf"^\s*\[\s*{header_pattern}\s*\]") or 1
        for name, value in table.items():
            if str(name).lower() == "python":
                continue  # the interpreter constraint, not a package
            if isinstance(value, str):
                specifier = value.strip()
            elif isinstance(value, Mapping):
                specifier = str(value.get("version") or ", ".join(
                    f"{key}={value[key]}" for key in ("git", "path", "url", "branch", "tag", "rev") if key in value
                ))
            else:
                continue
            pin = _POETRY_EXACT_PIN_RE.match(specifier)
            line = finder.find(rf"""^\s*["']?{re.escape(str(name))}["']?\s*=""", header, stop_at_table=True) or header
            rec.add(str(name), specifier, pin["version"] if pin else None, group, line)


NPM_DEPENDENCY_GROUPS: tuple[str, ...] = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)


def _parse_package_json(rec: _Recorder) -> None:
    try:
        document = json.loads("\n".join(rec.source.lines))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ManifestError("package.json is not a JSON object")
    finder = _LineFinder(rec.source)
    for group in NPM_DEPENDENCY_GROUPS:
        table = document.get(group)
        if not isinstance(table, Mapping):
            continue
        header = finder.header(rf'"{group}"\s*:') or 1
        for name, value in table.items():
            if not isinstance(value, str):
                continue
            specifier = value.strip()
            pin = _NPM_EXACT_PIN_RE.match(specifier)
            line = finder.find(rf'"{re.escape(str(name))}"\s*:', header) or header
            rec.add(str(name), specifier, pin["version"] if pin else None, group, line, ecosystem=Ecosystem.NPM)


def _parse_setup_cfg(rec: _Recorder) -> None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string("\n".join(rec.source.lines))
    except configparser.Error as exc:
        raise ManifestError(f"invalid setup.cfg: {exc}") from exc
    finder = _LineFinder(rec.source)

    def requirement_lines(value: str) -> Iterator[str]:
        for raw in value.splitlines():
            text = _PIP_COMMENT_RE.sub("", raw).strip()
            if text:
                yield text

    def record(section: str, key: str, group: str) -> None:
        if not parser.has_option(section, key):
            return
        section_line = finder.header(rf"^\s*\[\s*{re.escape(section)}\s*\]") or 1
        key_line = finder.find(rf"^\s*{re.escape(key)}\s*[=:]", section_line, stop_at_table=True) or section_line
        for entry in requirement_lines(parser.get(section, key)):
            parsed = _split_requirement(entry)
            if parsed is None:
                continue
            line = finder.find(re.escape(entry), key_line, stop_at_table=True) or key_line
            rec.add(parsed[0], parsed[1], _pip_pin(parsed[1]), group, line)

    record("options", "install_requires", "main")
    record("options", "tests_require", "tests")
    if parser.has_section("options.extras_require"):
        for key in parser.options("options.extras_require"):
            record("options.extras_require", key, key)


_MANIFEST_PARSERS: Mapping[ManifestKind, Callable[[_Recorder], None]] = {
    ManifestKind.REQUIREMENTS_TXT: _parse_requirements_txt,
    ManifestKind.PYPROJECT_TOML: _parse_pyproject,
    ManifestKind.PACKAGE_JSON: _parse_package_json,
    ManifestKind.SETUP_CFG: _parse_setup_cfg,
}


def extract_dependencies(
    source: SourceText, file_path: str, allocate: Allocate, manifest: ManifestKind | None = None
) -> list[DependencyRecord]:
    """Dependencies declared by the manifest at ``file_path``; raises ``ManifestError`` if it doesn't parse."""
    kind = manifest or manifest_kind_for(file_path)
    if kind is None:
        raise ValueError(f"{file_path} is not a recognized dependency manifest")
    recorder = _Recorder(source, file_path, allocate, kind)
    _MANIFEST_PARSERS[kind](recorder)
    return recorder.records
