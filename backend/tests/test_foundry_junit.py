"""Tests for apps.foundry.junit: pytest JUnit XML from the sandbox (untrusted input). No DB, no Docker."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from apps.foundry.junit import (
    MAX_JUNIT_BYTES,
    MAX_MESSAGE_CHARS,
    JUnitError,
    TestOutcome,
    parse_junit_bytes,
    parse_junit_file,
    report_as_dict,
)

# pytest 8 (xunit2): <testsuites name="pytest tests">, no file/line attributes.
PYTEST8_REPORT = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="5"
 time="0.050" timestamp="2025-01-01T00:00:00.000000+00:00" hostname="h">
<testcase classname="tests.test_webhooks" name="test_dispatch_retries_with_exponential_backoff" time="0.002">
<failure message="TypeError: WebhookDispatcher.__init__() got an unexpected keyword argument 'max_attempts'">
E   TypeError: unexpected keyword argument</failure></testcase>
<testcase classname="tests.test_ledger.TestPosting" name="test_balanced[2-2]" time="0.001" />
<testcase classname="tests.test_ledger" name="test_later" time="0.000">
<skipped type="pytest.skip" message="not today">tests/test_ledger.py:10: not today</skipped></testcase>
<testcase classname="tests.test_invoices" name="test_cleanup" time="0.001">
<error message="failed on teardown with &quot;RuntimeError: boom&quot;">RuntimeError: boom</error></testcase>
<testcase classname="tests.test_invoices" name="test_total" time="0.004" />
</testsuite></testsuites>
"""

# pytest 6.2 (xunit2 was already the default): <testsuites> root without a name.
PYTEST6_REPORT = b"""<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" errors="0"
 failures="1" skipped="0" tests="2" time="0.410" timestamp="2021-07-22T10:00:00.000000" hostname="h">
<testcase classname="tests.test_webhooks" name="test_dispatch_retries_with_exponential_backoff" time="0.003">
<failure message="TypeError: unexpected keyword argument 'max_attempts'">trace</failure></testcase>
<testcase classname="tests.test_webhooks" name="test_signature_is_stable" time="0.001"/>
</testsuite></testsuites>"""

# pytest < 5.1, or junit_family=xunit1: a bare <testsuite> root with file and line attributes.
LEGACY_REPORT = b"""<?xml version="1.0" encoding="utf-8"?><testsuite errors="0" failures="0" name="pytest"
 skipped="0" tests="1" time="0.1"><testcase classname="tests.test_ledger.TestPosting"
 file="tests/test_ledger.py" line="12" name="test_post" time="0.01"></testcase></testsuite>"""

COLLECTION_ERROR_REPORT = b"""<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest"
 errors="1" failures="0" skipped="0" tests="1" time="0.2"><testcase classname="" name="tests.test_broken"
 time="0.000"><error message="collection failure">ImportError: No module named 'paypulse.pdf'</error>
</testcase></testsuite></testsuites>"""


def test_pytest8_report_outcomes_nodeids_and_totals() -> None:
    report = parse_junit_bytes(PYTEST8_REPORT)
    by_id = {case.nodeid: case for case in report.cases}
    failed = by_id["tests/test_webhooks.py::test_dispatch_retries_with_exponential_backoff"]
    assert failed.outcome is TestOutcome.FAILED and "max_attempts" in failed.message
    assert by_id["tests/test_ledger.py::TestPosting::test_balanced[2-2]"].outcome is TestOutcome.PASSED
    assert by_id["tests/test_ledger.py::test_later"].outcome is TestOutcome.SKIPPED
    assert by_id["tests/test_invoices.py::test_cleanup"].outcome is TestOutcome.ERROR
    assert by_id["tests/test_invoices.py::test_total"].duration_seconds == pytest.approx(0.004)
    totals = report.totals
    assert (totals.tests, totals.passed, totals.failed, totals.errors, totals.skipped) == (5, 2, 1, 1, 1)
    assert not report.collection_error and not report.cases_truncated


def test_pytest6_report_parses_the_same_way() -> None:
    report = parse_junit_bytes(PYTEST6_REPORT)
    assert [(case.nodeid, case.outcome) for case in report.cases] == [
        ("tests/test_webhooks.py::test_dispatch_retries_with_exponential_backoff", TestOutcome.FAILED),
        ("tests/test_webhooks.py::test_signature_is_stable", TestOutcome.PASSED),
    ]


def test_legacy_report_uses_the_file_attribute() -> None:
    [case] = parse_junit_bytes(LEGACY_REPORT).cases
    assert case.nodeid == "tests/test_ledger.py::TestPosting::test_post"


def test_collection_error_is_flagged_with_the_module_as_nodeid() -> None:
    report = parse_junit_bytes(COLLECTION_ERROR_REPORT)
    [case] = report.cases
    assert report.collection_error and case.is_collection_error
    assert case.outcome is TestOutcome.ERROR and case.nodeid == "tests/test_broken.py"
    assert report_as_dict(report)["totals"]["errors"] == 1


def test_workspace_files_decide_where_the_module_ends(tmp_path: Path) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_api.py").write_text("")
    xml = (b'<testsuites><testsuite><testcase classname="tests.unit.test_api.Outer.inner" name="test_x"/>'
           b"</testsuite></testsuites>")
    [case] = parse_junit_bytes(xml, workspace=tmp_path).cases
    assert case.nodeid == "tests/unit/test_api.py::Outer::inner::test_x"


def test_long_messages_are_capped() -> None:
    xml = (b'<testsuites><testsuite><testcase classname="t.test_a" name="test_b"><failure message="'
           + b"x" * (MAX_MESSAGE_CHARS + 100) + b'"/></testcase></testsuite></testsuites>')
    [case] = parse_junit_bytes(xml).cases
    assert len(case.message) == MAX_MESSAGE_CHARS and case.message_truncated


BILLION_LAUGHS = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">
<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]><testsuites>&lol2;</testsuites>"""
EXTERNAL_ENTITY = b"""<?xml version="1.0"?><!DOCTYPE t [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<testsuites><testsuite><testcase classname="a.test_b" name="test_c"><failure message="&xxe;"/></testcase>
</testsuite></testsuites>"""


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (BILLION_LAUGHS, "DOCTYPE or ENTITY"),
        (EXTERNAL_ENTITY, "DOCTYPE or ENTITY"),
        (b"<testsuites><!ENTITY x 'y'></testsuites>", "DOCTYPE or ENTITY"),
        (b" " * (MAX_JUNIT_BYTES + 1), "larger than"),
        ('<?xml version="1.0" encoding="utf-16"?><testsuites/>'.encode("utf-16"), "not UTF-8"),
        (b'<?xml version="1.0" encoding="latin-1"?><testsuites/>', "unsupported encoding"),
        (b"<testsuites><testsuite>", "not well-formed"),
        (b"<html><body/></html>", "unexpected root"),
    ],
    ids=["billion-laughs", "external-entity", "bare-entity", "oversized", "utf16", "latin1", "truncated", "html"],
)
def test_unsafe_or_invalid_reports_are_rejected(data: bytes, reason: str) -> None:
    with pytest.raises(JUnitError, match=reason):
        parse_junit_bytes(data)


def test_file_reader_accepts_a_report_in_the_results_directory(tmp_path: Path) -> None:
    (tmp_path / "junit.xml").write_bytes(PYTEST6_REPORT)
    assert parse_junit_file(tmp_path / "junit.xml", results_root=tmp_path).totals.tests == 2


def test_file_reader_rejects_missing_and_outside_reports(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    with pytest.raises(JUnitError, match="no JUnit report"):
        parse_junit_file(results / "junit.xml", results_root=results)
    (tmp_path / "junit.xml").write_bytes(PYTEST6_REPORT)
    with pytest.raises(JUnitError, match="outside the results directory"):
        parse_junit_file(tmp_path / "junit.xml", results_root=results)


def test_file_reader_refuses_a_symlinked_report(tmp_path: Path) -> None:
    secret = tmp_path / "host-secret.xml"
    secret.write_bytes(PYTEST6_REPORT)
    results = tmp_path / "results"
    results.mkdir()
    try:
        os.symlink(secret, results / "junit.xml")
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or admin rights on Windows")
    with pytest.raises(JUnitError, match="symlink"):
        parse_junit_file(results / "junit.xml", results_root=results)
