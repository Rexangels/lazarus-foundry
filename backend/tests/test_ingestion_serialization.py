"""Tests for apps.ingestion.serialization (fast: no Git, no database)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from apps.ingestion.evidence import FingerprintAllocator, SourceText, scan_line_markers
from apps.ingestion.serialization import cap_text, clean_text, to_json


class Color(StrEnum):
    RED = "RED"


@dataclass(frozen=True)
class Inner:
    when: datetime
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Outer:
    color: Color
    path: PureWindowsPath
    inner: Inner
    age: timedelta
    day: date
    names: frozenset[str]


def test_dataclasses_enums_dates_and_paths_become_plain_json() -> None:
    value = Outer(
        color=Color.RED,
        path=PureWindowsPath("paypulse\\retry.py"),
        inner=Inner(when=datetime(2021, 4, 6, 12, 0, tzinfo=UTC), tags=("a", "b")),
        age=timedelta(days=2, seconds=30),
        day=date(2021, 4, 6),
        names=frozenset({"b", "a"}),
    )

    result = to_json({Color.RED: value})

    assert result == {
        "RED": {
            "color": "RED",
            "path": "paypulse/retry.py",
            "inner": {"when": "2021-04-06T12:00:00+00:00", "tags": ["a", "b"]},
            "age": 172830.0,
            "day": "2021-04-06",
            "names": ["a", "b"],
        }
    }
    json.dumps(result)


def test_nul_characters_are_replaced_in_values_and_keys() -> None:
    assert to_json({"k\x00": ["a\x00b"]}) == {"k\ufffd": ["a\ufffdb"]}
    assert clean_text("plain") == "plain"


@pytest.mark.parametrize(
    ("value", "error", "where"),
    [
        ({"x": b"raw"}, TypeError, "value['x']"),
        ({"x": [Decimal("1.0")]}, TypeError, "value['x'][0]"),
        ({"x": float("nan")}, ValueError, "value['x']"),
        ({1: "one"}, TypeError, "value"),
    ],
)
def test_unsupported_values_are_rejected_with_their_location(value: object, error: type[Exception], where: str) -> None:
    with pytest.raises(error, match=f"^{re.escape(where)}:"):
        to_json(value)


def test_real_evidence_record_round_trips_through_json() -> None:
    source = SourceText("x = 1\n# TODO(priya): wire \x00 retries\n".encode())
    [marker] = scan_line_markers(source, "paypulse/a.py", FingerprintAllocator("paypulse/a.py"))

    result = to_json(marker)

    assert json.loads(json.dumps(result)) == result
    assert result["kind"] == "MARKER" and result["tag"] == "TODO" and result["owner"] == "priya"
    assert (result["line_start"], result["line_end"]) == (2, 2)
    assert "\x00" not in result["snippet"] and "\ufffd" in result["snippet"]


def test_cap_text_keeps_short_text_whole() -> None:
    assert cap_text(b"one\ntwo\n", 100) == ("one\ntwo\n", False)


def test_cap_text_cuts_at_the_last_line_break() -> None:
    assert cap_text(b"one\ntwo\nthree\n", 10) == ("one\ntwo\n", True)


def test_cap_text_hard_cut_never_splits_a_utf8_character() -> None:
    data = ("a" + "\u00e9" * 10).encode()  # 1 + 20 bytes, no line breaks
    text, truncated = cap_text(data, 4)  # 'a' + one 2-byte char + half of the next
    assert (text, truncated) == ("a\u00e9", True)


def test_cap_text_replaces_invalid_bytes_and_nul() -> None:
    assert cap_text(b"ok\xff\x00\n", 100) == ("ok\ufffd\ufffd\n", False)
    with pytest.raises(ValueError):
        cap_text(b"x", 0)


def test_pure_posix_path_is_kept() -> None:
    assert to_json(PurePosixPath("docs/a.md")) == "docs/a.md"
