"""Pure helpers for the EnvironmentAgent: manifest audit, Python version choice and pin rewriting.

Nothing here runs a command. ``audit_manifests`` reads the workspace's
top-level ``requirements*.txt``, ``setup.cfg`` and ``pyproject.toml``;
``choose_python_version`` picks the sandbox interpreter from the project's
``python_requires`` and what is allowed (and, in subprocess mode, installed);
``rewrite_pin`` changes one requirement line to ``name==version``.
"""
from __future__ import annotations

import configparser
import re
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name

MAX_MANIFEST_BYTES: int = 256 * 1024
_REQUIREMENTS_FILE: re.Pattern[str] = re.compile(r"^requirements[\w.\-]*\.txt$", re.IGNORECASE)
_COMMENT: re.Pattern[str] = re.compile(r"(?:^|\s)#")
_PACKAGE_NAME: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class EnvironmentPlanError(ValueError):
    """The manifests or the allowed interpreters rule out every sandbox environment."""


@dataclass(frozen=True)
class RequirementLine:
    file: str
    line: int
    name: str
    specifier: str
    exact_pin: str | None  # the version of an ``==`` pin

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManifestAudit:
    requirement_files: tuple[str, ...]
    requirements: tuple[RequirementLine, ...]
    requires_python: str | None
    requires_python_source: str | None
    notes: tuple[str, ...]

    @property
    def package_names(self) -> tuple[str, ...]:
        """Requirement names in file order, de-duplicated (the relaxed, unpinned install)."""
        return tuple(dict.fromkeys(requirement.name for requirement in self.requirements))

    @property
    def declares_pytest(self) -> bool:
        return any(canonicalize_name(r.name) == "pytest" for r in self.requirements)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requirement_files": list(self.requirement_files),
            "requirements": [requirement.as_dict() for requirement in self.requirements],
            "exact_pins": [f"{r.name}{r.specifier}" for r in self.requirements if r.exact_pin],
            "requires_python": self.requires_python,
            "requires_python_source": self.requires_python_source,
            "notes": list(self.notes),
        }


def _read_small(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def requirement_files(workspace: Path) -> tuple[str, ...]:
    """Top-level ``requirements*.txt`` files, ``requirements.txt`` first."""
    names = [
        entry.name for entry in workspace.iterdir()
        if _REQUIREMENTS_FILE.match(entry.name) and entry.is_file() and not entry.is_symlink()
    ]
    return tuple(sorted(names, key=lambda name: (name.lower() != "requirements.txt", name.lower())))


def parse_requirement_lines(file: str, text: str) -> tuple[list[RequirementLine], list[str]]:
    found: list[RequirementLine] = []
    notes: list[str] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = _COMMENT.split(raw, maxsplit=1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            notes.append(f"{file}:{number}: could not parse {line[:80]!r}")
            continue
        specs = list(requirement.specifier)
        pin = specs[0].version if len(specs) == 1 and specs[0].operator in ("==", "===") else None
        found.append(RequirementLine(file, number, requirement.name, str(requirement.specifier), pin))
    return found, notes


def requires_python(workspace: Path) -> tuple[str | None, str | None, list[str]]:
    notes: list[str] = []
    setup_cfg = _read_small(workspace / "setup.cfg")
    if setup_cfg is not None:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(setup_cfg)
            value = parser.get("options", "python_requires", fallback="").strip()
        except configparser.Error as exc:
            notes.append(f"setup.cfg could not be parsed: {type(exc).__name__}")
            value = ""
        if value:
            return value, "setup.cfg", notes
    pyproject = _read_small(workspace / "pyproject.toml")
    if pyproject is not None:
        try:
            value = str((tomllib.loads(pyproject).get("project") or {}).get("requires-python") or "").strip()
        except tomllib.TOMLDecodeError as exc:
            notes.append(f"pyproject.toml could not be parsed: {exc}")
            value = ""
        if value:
            return value, "pyproject.toml", notes
    return None, None, notes


def audit_manifests(workspace: Path) -> ManifestAudit:
    files = requirement_files(workspace)
    requirements: list[RequirementLine] = []
    notes: list[str] = []
    for name in files:
        text = _read_small(workspace / name)
        if text is None:
            notes.append(f"{name} was skipped (too large or not a regular file)")
            continue
        found, problems = parse_requirement_lines(name, text)
        requirements += found
        notes += problems
    python_spec, source, python_notes = requires_python(workspace)
    return ManifestAudit(files, tuple(requirements), python_spec, source, tuple(notes + python_notes))


def satisfies(version: str, spec: str | None) -> bool:
    """Whether some release of ``X.Y`` satisfies ``spec`` (an unparsable spec allows everything)."""
    if not spec:
        return True
    try:
        specifier = SpecifierSet(spec)
    except InvalidSpecifier:
        return True
    return any(specifier.contains(f"{version}.{patch}", prereleases=True) for patch in (0, 99))


def choose_python_version(
    spec: str | None, *, allowed: Sequence[str], default: str, available: Callable[[str], bool] | None = None
) -> tuple[str, str]:
    """``(version, reason)``: the default when it fits, else the newest allowed (and available) one."""
    candidates = [version for version in allowed if satisfies(version, spec)]
    if not candidates:
        raise EnvironmentPlanError(
            f"python_requires {spec!r} excludes every allowed sandbox Python ({', '.join(allowed)})"
        )
    newest_first = sorted(candidates, key=lambda v: tuple(int(p) for p in v.split(".")), reverse=True)
    wanted = f"python_requires {spec}" if spec else "no python_requires"
    is_available = available or (lambda _version: True)
    if default in candidates and is_available(default):
        return default, f"{wanted}; SANDBOX_DEFAULT_PYTHON {default} satisfies it"
    for version in newest_first:
        if is_available(version):
            return version, f"{wanted}; {version} is the newest allowed interpreter available here"
    fallback = default if default in candidates else newest_first[0]
    return fallback, f"{wanted}; no allowed interpreter was found, so {fallback} is requested"


def valid_package_name(name: str) -> bool:
    return bool(_PACKAGE_NAME.match(name or ""))


def rewrite_pin(text: str, *, line_number: int, name: str, version: str) -> str:
    """``text`` with requirement line ``line_number`` (1-based) pinned to ``name==version``.

    Extras, markers and a trailing comment are kept. Raises ``ValueError`` when
    the line does not hold a requirement for ``name``.
    """
    lines = text.split("\n")
    if not 1 <= line_number <= len(lines):
        raise ValueError(f"line {line_number} does not exist")
    raw = lines[line_number - 1]
    parts = _COMMENT.split(raw, maxsplit=1)
    body = parts[0].strip()
    comment = raw[len(parts[0]):].rstrip() if len(parts) > 1 else ""
    requirement = Requirement(body)
    if canonicalize_name(requirement.name) != canonicalize_name(name):
        raise ValueError(f"line {line_number} names {requirement.name}, not {name}")
    extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
    marker = f"; {requirement.marker}" if requirement.marker else ""
    lines[line_number - 1] = f"{requirement.name}{extras}=={version}{marker}{(' ' + comment.strip()) if comment else ''}"
    return "\n".join(lines)
