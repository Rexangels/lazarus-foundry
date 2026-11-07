"""Publish telemetry events to a project's WebSocket group.

Use :func:`broadcast_telemetry` from synchronous code (Celery tasks, services)
and :func:`abroadcast_telemetry` from coroutines. Calling the sync variant from
inside a running event loop raises ``RuntimeError`` (asgiref refuses it).

Failure policy:

* Programmer errors raise before anything is sent: an unknown ``event_type``
  (``ValueError``), a malformed ``project_id`` (``ValueError``/``TypeError``), or
  a payload that is not a JSON object of supported types (``TypeError``/``ValueError``).
  A missing ``CHANNEL_LAYERS["default"]`` raises ``ImproperlyConfigured``.
* Transport failures (Redis down, channel full, connection reset) are logged
  with a traceback and swallowed. Telemetry is best-effort and must never fail
  an ingestion or Foundry job; the dropped event is not retried.
"""
import logging
import math
from collections.abc import Mapping
from datetime import date, datetime, time
from enum import Enum
from pathlib import PurePath
from typing import Any
from uuid import UUID, uuid4

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from pydantic import JsonValue

from apps.telemetry.events import CHANNEL_MESSAGE_TYPE, TelemetryEventType, TelemetryFrame
from apps.telemetry.groups import telemetry_group_name

logger = logging.getLogger(__name__)


def to_json_compatible(value: Any, *, path: str = "payload") -> JsonValue:
    """Convert ``value`` into plain JSON types, or raise if it can't be represented.

    Conversions: ``UUID`` -> str, ``datetime``/``date``/``time`` -> ISO-8601 str,
    ``PurePath`` -> POSIX-style str, ``Enum`` -> its value, tuple -> list.
    Rejected: non-string mapping keys, NaN/infinite floats, and any other type
    (bytes, sets, Decimal, model instances, ...). ``path`` names the offending
    location in error messages, e.g. ``payload['files'][2]``.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return to_json_compatible(value.value, path=path)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path}: {value!r} is not representable in JSON")
        return float(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path}: mapping keys must be str, got {type(key).__name__} {key!r}")
            result[str(key)] = to_json_compatible(item, path=f"{path}[{key!r}]")
        return result
    if isinstance(value, (list, tuple)):
        return [to_json_compatible(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path}: {type(value).__name__} is not JSON-serializable")


def _parse_project_id(project_id: UUID | str) -> UUID:
    if isinstance(project_id, UUID):
        return project_id
    if isinstance(project_id, str):
        try:
            return UUID(project_id)
        except ValueError as exc:
            raise ValueError(f"project_id {project_id!r} is not a valid UUID") from exc
    raise TypeError(f"project_id must be a UUID or str, got {type(project_id).__name__}")


def _parse_event_type(event_type: TelemetryEventType | str) -> TelemetryEventType:
    try:
        return TelemetryEventType(event_type)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in TelemetryEventType)
        raise ValueError(f"Unknown telemetry event_type {event_type!r}; expected one of: {allowed}") from exc


def build_telemetry_frame(
    project_id: UUID | str,
    event_type: TelemetryEventType | str,
    payload: Mapping[str, Any],
) -> TelemetryFrame:
    """Validate inputs and build the frame that clients will receive."""
    if not isinstance(payload, Mapping):
        raise TypeError(f"payload must be a mapping, got {type(payload).__name__}")
    return TelemetryFrame(
        event_id=uuid4(),
        project_id=_parse_project_id(project_id),
        event_type=_parse_event_type(event_type),
        payload=to_json_compatible(payload),
        timestamp=timezone.now(),
    )


async def _deliver(frame: TelemetryFrame) -> None:
    channel_layer = get_channel_layer()
    if channel_layer is None:
        raise ImproperlyConfigured("CHANNEL_LAYERS has no 'default' layer; telemetry cannot be delivered.")
    message: dict[str, Any] = {
        "type": CHANNEL_MESSAGE_TYPE,
        "frame": frame.model_dump(mode="json"),
    }
    try:
        await channel_layer.group_send(telemetry_group_name(frame.project_id), message)
    except Exception:
        logger.exception(
            "Dropped telemetry event %s for project %s: channel layer delivery failed.",
            frame.event_type.value,
            frame.project_id,
        )


def broadcast_telemetry(
    project_id: UUID | str,
    event_type: TelemetryEventType | str,
    payload: Mapping[str, Any],
) -> None:
    """Send one telemetry event to every client watching ``project_id`` (sync callers)."""
    frame = build_telemetry_frame(project_id, event_type, payload)
    async_to_sync(_deliver)(frame)


async def abroadcast_telemetry(
    project_id: UUID | str,
    event_type: TelemetryEventType | str,
    payload: Mapping[str, Any],
) -> None:
    """Async variant of :func:`broadcast_telemetry` for code already in an event loop."""
    frame = build_telemetry_frame(project_id, event_type, payload)
    await _deliver(frame)
