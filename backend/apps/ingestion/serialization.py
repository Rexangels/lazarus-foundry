"""Convert ingestion results into values PostgreSQL ``jsonb``/``text`` columns accept.

``to_json`` is the one serializer for every ``ArtifactRecord.structured_metadata``,
``BranchSnapshot.diff_summary`` and ``Project.ingestion_summary`` payload:

* dataclasses (evidence records, ``ParseOutcome``, ``CommitInfo``, ...) become
  dicts of their fields, recursively;
* ``Enum`` -> its value, ``datetime``/``date``/``time`` -> ISO-8601 string,
  ``timedelta`` -> seconds (float), ``PurePath`` -> POSIX string,
  tuple/list -> list, ``set``/``frozenset`` -> sorted list;
* NUL characters in strings become U+FFFD: PostgreSQL rejects ``\\u0000`` in
  both ``text`` and ``jsonb``, and source files can contain NUL bytes past the
  binary sniffing window;
* anything else (bytes, Decimal, model instances, NaN/infinite floats,
  non-string mapping keys) raises ``TypeError``/``ValueError`` naming the path.
"""
from __future__ import annotations

import codecs
import dataclasses
import math
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import PurePath
from typing import Any

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

NUL: str = "\x00"
REPLACEMENT_CHARACTER: str = "\ufffd"


def clean_text(text: str) -> str:
    """``text`` with NUL characters replaced, so PostgreSQL can store it."""
    return text.replace(NUL, REPLACEMENT_CHARACTER) if NUL in text else text


def to_json(value: Any, *, path: str = "value") -> JsonValue:
    """Return ``value`` as plain JSON types (see the module docstring), or raise."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return to_json(value.value, path=path)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path}: {value!r} is not representable in JSON")
        return float(value)
    if isinstance(value, str):
        return clean_text(str(value))
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, PurePath):
        return clean_text(value.as_posix())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_json(getattr(value, field.name), path=f"{path}.{field.name}")
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if isinstance(key, Enum):
                key = key.value
            if not isinstance(key, str):
                raise TypeError(f"{path}: mapping keys must be str, got {type(key).__name__} {key!r}")
            result[clean_text(key)] = to_json(item, path=f"{path}[{key!r}]")
        return result
    if isinstance(value, (list, tuple)):
        return [to_json(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, (set, frozenset)):
        items = [to_json(item, path=f"{path}{{}}") for item in value]
        return sorted(items, key=repr)
    raise TypeError(f"{path}: {type(value).__name__} is not JSON-serializable")


def cap_text(data: bytes, max_bytes: int) -> tuple[str, bool]:
    """Decode ``data`` as UTF-8 (with replacement), keeping at most ``max_bytes`` bytes.

    The cut falls at the last line break within the limit (or at the limit
    when the first line alone is longer), never inside a UTF-8 sequence.
    Returns ``(text, truncated)``; the text is NUL-free.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if len(data) <= max_bytes:
        return clean_text(data.decode("utf-8", errors="replace")), False
    cut = data.rfind(b"\n", 0, max_bytes)
    kept = data[: cut + 1] if cut >= 0 else data[:max_bytes]
    # final=False holds back an incomplete multi-byte sequence at a hard cut.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    return clean_text(decoder.decode(kept, final=False)), True
