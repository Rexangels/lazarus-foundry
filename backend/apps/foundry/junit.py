"""Parse the JUnit XML that pytest writes inside the Foundry sandbox.

The report is produced by code from a repository we don't control, so it is
untrusted input:

* the file is read only if it is a regular file (not a symlink) inside the
  run's results directory, and at most ``MAX_JUNIT_BYTES``;
* it must be UTF-8 (pytest always writes UTF-8) and may not contain a
  ``<!DOCTYPE`` or ``<!ENTITY`` declaration, which rules out entity
  expansion ("billion laughs") and external entities. ``defusedxml`` is used
  for the parse when it is installed; otherwise ``xml.etree`` parses the
  already-checked bytes;
* at most ``MAX_CASES`` test cases and ``MAX_MESSAGE_CHARS`` per message are
  kept.

Both pytest JUnit families are handled: ``xunit2`` (the default since pytest
5.1, used by 6.x-8.x, ``<testsuites>`` root, no ``file`` attribute) and
``xunit1`` / pre-5.1 reports (``<testsuite>`` root, ``file`` and ``line``
attributes). Collection errors are the ``<testcase classname="" ...>`` entries
carrying ``<error message="collection failure">``.
"""
from __future__ import annotations

import os
import re
import stat
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

try:  # pragma: no cover - depends on the environment
    from defusedxml import ElementTree as _defused_element_tree
except ImportError:  # pragma: no cover - defusedxml is optional
    _defused_element_tree = None

MAX_JUNIT_BYTES: int = 10 * 1024 * 1024
MAX_MESSAGE_CHARS: int = 4000
MAX_CASES: int = 20000
COLLECTION_FAILURE_MESSAGE: str = "collection failure"
_FORBIDDEN_DECLARATION: re.Pattern[str] = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_XML_ENCODING: re.Pattern[str] = re.compile(r"""^\s*<\?xml[^>]*\bencoding\s*=\s*["']([^"']+)["']""")


class JUnitError(ValueError):
    """The JUnit report is missing, unsafe to read, too large or not valid JUnit XML."""


class TestOutcome(StrEnum):
    __test__ = False  # not a pytest test class

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class TestCaseResult:
    """One ``<testcase>``: ``nodeid`` is reconstructed as ``path.py::Class::name``."""

    __test__ = False

    nodeid: str
    classname: str
    name: str
    outcome: TestOutcome
    duration_seconds: float
    message: str
    message_truncated: bool
    is_collection_error: bool


@dataclass(frozen=True)
class TestTotals:
    __test__ = False

    tests: int
    passed: int
    failed: int
    errors: int
    skipped: int

    @classmethod
    def from_cases(cls, cases: tuple[TestCaseResult, ...]) -> TestTotals:
        def count(outcome: TestOutcome) -> int:
            return sum(1 for case in cases if case.outcome is outcome)

        return cls(
            tests=len(cases),
            passed=count(TestOutcome.PASSED),
            failed=count(TestOutcome.FAILED),
            errors=count(TestOutcome.ERROR),
            skipped=count(TestOutcome.SKIPPED),
        )


EMPTY_TOTALS: TestTotals = TestTotals(tests=0, passed=0, failed=0, errors=0, skipped=0)


@dataclass(frozen=True)
class JUnitReport:
    cases: tuple[TestCaseResult, ...]
    totals: TestTotals
    collection_error: bool
    cases_truncated: bool


EMPTY_REPORT: JUnitReport = JUnitReport(cases=(), totals=EMPTY_TOTALS, collection_error=False, cases_truncated=False)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def read_junit_file(path: Path, *, results_root: Path, max_bytes: int = MAX_JUNIT_BYTES) -> bytes:
    """Return the bytes of ``path``, a regular file directly under ``results_root`` (no symlinks)."""
    root = results_root.resolve()
    if path.is_symlink():
        raise JUnitError("the JUnit report is a symlink")
    if not path.exists():
        raise JUnitError("pytest wrote no JUnit report")
    if path.resolve().parent != root:
        raise JUnitError("the JUnit report is outside the results directory")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise JUnitError(f"the JUnit report could not be opened: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise JUnitError("the JUnit report is not a regular file")
        if info.st_size > max_bytes:
            raise JUnitError(f"the JUnit report is larger than {max_bytes} bytes")
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise JUnitError(f"the JUnit report is larger than {max_bytes} bytes")
    return data


def parse_junit_file(path: Path, *, results_root: Path, workspace: Path | None = None) -> JUnitReport:
    return parse_junit_bytes(read_junit_file(path, results_root=results_root), workspace=workspace)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _parse_xml(data: bytes) -> ElementTree.Element:
    if len(data) > MAX_JUNIT_BYTES:
        raise JUnitError(f"the JUnit report is larger than {MAX_JUNIT_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JUnitError("the JUnit report is not UTF-8") from exc
    declared = _XML_ENCODING.match(text.lstrip("\ufeff"))
    if declared and declared.group(1).lower().replace("_", "-") not in ("utf-8", "utf8"):
        raise JUnitError(f"the JUnit report declares an unsupported encoding ({declared.group(1)})")
    if _FORBIDDEN_DECLARATION.search(text):
        raise JUnitError("the JUnit report contains a DOCTYPE or ENTITY declaration")
    try:
        if _defused_element_tree is not None:  # pragma: no cover - depends on the environment
            return _defused_element_tree.fromstring(data, forbid_dtd=True)
        return ElementTree.fromstring(data)  # noqa: S314 - DOCTYPE/ENTITY rejected above
    except ElementTree.ParseError as exc:
        raise JUnitError(f"the JUnit report is not well-formed XML: {exc}") from exc
    except Exception as exc:  # defusedxml raises its own DefusedXmlException subclasses
        raise JUnitError(f"the JUnit report was rejected: {type(exc).__name__}") from exc


def _cap(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_MESSAGE_CHARS:
        return text, False
    return text[:MAX_MESSAGE_CHARS], True


def _float(value: str | None) -> float:
    try:
        number = float(value or 0.0)
    except ValueError:
        return 0.0
    return number if number == number and number >= 0 else 0.0  # NaN and negatives become 0


def _module_and_classes(dotted: str, workspace: Path | None) -> tuple[str, list[str]]:
    """Split ``tests.test_x.TestA`` into (``tests/test_x.py``, [``TestA``])."""
    parts = [part for part in dotted.split(".") if part]
    if not parts:
        return "", []
    if workspace is not None:
        for split in range(len(parts), 0, -1):
            candidate = "/".join(parts[:split]) + ".py"
            if (workspace / candidate).is_file():
                return candidate, parts[split:]
    classes: list[str] = []
    while len(parts) > 1 and parts[-1][:1].isupper():
        classes.insert(0, parts.pop())
    return "/".join(parts) + ".py", classes


def _nodeid(classname: str, name: str, file_attr: str, workspace: Path | None, collection_error: bool) -> str:
    if collection_error or not classname:
        # pytest names a collection error after the module ("tests.test_x" or "tests/test_x.py").
        if name.endswith(".py") or "/" in name:
            return name.replace("\\", "/")
        module, classes = _module_and_classes(name, workspace)
        return "::".join([module, *classes]) if module else name
    if file_attr:
        module = file_attr.replace("\\", "/")
        dotted_module = module.removesuffix(".py").replace("/", ".")
        rest = classname[len(dotted_module) + 1:] if classname.startswith(dotted_module + ".") else ""
        classes = [part for part in rest.split(".") if part]
    else:
        module, classes = _module_and_classes(classname, workspace)
    return "::".join([module, *classes, name])


def _case(element: ElementTree.Element, workspace: Path | None) -> TestCaseResult:
    classname = element.get("classname", "") or ""
    name = element.get("name", "") or ""
    failure = element.find("failure")
    error = element.find("error")
    skipped = element.find("skipped")
    # A failing test that also errors in teardown counts as failed, like pytest's own summary line.
    if failure is not None:
        outcome, detail = TestOutcome.FAILED, failure
    elif error is not None:
        outcome, detail = TestOutcome.ERROR, error
    elif skipped is not None:
        outcome, detail = TestOutcome.SKIPPED, skipped
    else:
        outcome, detail = TestOutcome.PASSED, None
    raw_message = ""
    if detail is not None:
        raw_message = detail.get("message") or (detail.text or "").strip()
    message, truncated = _cap(raw_message)
    is_collection_error = (
        error is not None
        and (error.get("message", "") == COLLECTION_FAILURE_MESSAGE or not classname)
    )
    return TestCaseResult(
        nodeid=_nodeid(classname, name, element.get("file", "") or "", workspace, is_collection_error),
        classname=classname,
        name=name,
        outcome=outcome,
        duration_seconds=_float(element.get("time")),
        message=message,
        message_truncated=truncated,
        is_collection_error=is_collection_error,
    )


def parse_junit_bytes(data: bytes, *, workspace: Path | None = None) -> JUnitReport:
    """Parse a pytest JUnit report. ``workspace`` (optional) resolves dotted class names to files."""
    root = _parse_xml(data)
    if root.tag not in ("testsuites", "testsuite"):
        raise JUnitError(f"the JUnit report has an unexpected root element <{root.tag}>")
    cases: list[TestCaseResult] = []
    truncated = False
    for element in root.iter("testcase"):
        if len(cases) >= MAX_CASES:
            truncated = True
            break
        cases.append(_case(element, workspace))
    frozen = tuple(cases)
    return JUnitReport(
        cases=frozen,
        totals=TestTotals.from_cases(frozen),
        collection_error=any(case.is_collection_error for case in frozen),
        cases_truncated=truncated,
    )


def report_as_dict(report: JUnitReport) -> dict[str, Any]:
    """Plain JSON for telemetry and ``FoundryRun`` logs."""
    return {
        "totals": asdict(report.totals),
        "collection_error": report.collection_error,
        "cases_truncated": report.cases_truncated,
        "cases": [
            {
                "nodeid": case.nodeid,
                "outcome": case.outcome.value,
                "duration_seconds": case.duration_seconds,
                "message": case.message,
                "message_truncated": case.message_truncated,
                "is_collection_error": case.is_collection_error,
            }
            for case in report.cases
        ],
    }
