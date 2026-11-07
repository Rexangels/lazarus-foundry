"""Telemetry event vocabulary and the wire format of frames pushed to clients.

Every server-to-client WebSocket message is a JSON object with a ``type``
discriminator:

``{"type": "telemetry", ...}``
    A :class:`TelemetryFrame`, produced by ``broadcast_telemetry``.
``{"type": "pong"}``
    Reply to a client ``{"type": "ping"}`` keep-alive.
``{"type": "error", "detail": "..."}``
    The client sent something the server does not accept. The connection stays open.

Telemetry frames carry no global sequence number. Events are produced by several
processes (Celery workers, Daphne), so a per-process counter would suggest an
ordering guarantee that does not exist. Clients order frames by ``timestamp``
and de-duplicate by ``event_id``.
"""
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime, JsonValue

# Channels message type used inside the channel layer. Channels dispatches it to
# ``TelemetryConsumer.telemetry_event`` (dots become underscores).
CHANNEL_MESSAGE_TYPE: Final[str] = "telemetry.event"

FRAME_TYPE_TELEMETRY: Final[str] = "telemetry"
FRAME_TYPE_PONG: Final[str] = "pong"
FRAME_TYPE_ERROR: Final[str] = "error"


class TelemetryEventType(StrEnum):
    """Every event the backend may stream to the Command Center."""

    # System 1: ingestion
    INGESTION_STARTED = "INGESTION_STARTED"
    BRANCH_DISCOVERED = "BRANCH_DISCOVERED"
    AST_PARSED = "AST_PARSED"
    INGESTION_COMPLETE = "INGESTION_COMPLETE"
    # System 2: genome
    GENOME_NODE_CREATED = "GENOME_NODE_CREATED"
    CONFLICT_DETECTED = "CONFLICT_DETECTED"
    GENOME_COMPLETE = "GENOME_COMPLETE"
    # System 3: revival
    STRATEGY_GENERATED = "STRATEGY_GENERATED"
    STRATEGY_APPROVED = "STRATEGY_APPROVED"
    # System 4: foundry
    AGENT_STAGE_CHANGED = "AGENT_STAGE_CHANGED"
    SANDBOX_LOG = "SANDBOX_LOG"
    TEST_PASSED = "TEST_PASSED"
    TEST_FAILED = "TEST_FAILED"
    # System 5: rebirth
    REBIRTH_COMPLETE = "REBIRTH_COMPLETE"
    # Any stage
    ERROR = "ERROR"


class TelemetryFrame(Schema):
    """One telemetry event as delivered to WebSocket clients.

    Serialized with ``model_dump(mode="json")``: ``event_id`` and ``project_id``
    become UUID strings, ``timestamp`` an ISO-8601 UTC string.
    """

    type: Literal["telemetry"] = FRAME_TYPE_TELEMETRY
    event_id: UUID
    project_id: UUID
    event_type: TelemetryEventType
    payload: dict[str, JsonValue]
    timestamp: AwareDatetime
