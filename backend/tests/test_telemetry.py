"""Telemetry WebSocket routing, consumer and broadcaster tests.

The channel layer is swapped for Channels' ``InMemoryChannelLayer`` (the
documented test backend) so these run without Redis. Production settings keep
``channels_redis``; see ``test_health.test_redis_backs_celery_and_channels``.
Only the tests marked ``django_db`` need PostgreSQL.
"""
import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import UUID, uuid4

import pytest
from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.layers import InMemoryChannelLayer
from channels.testing import WebsocketCommunicator
from pytest_django import Settings

from apps.projects.models import Project
from apps.telemetry.broadcast import abroadcast_telemetry, broadcast_telemetry, build_telemetry_frame
from apps.telemetry.consumers import CLOSE_PROJECT_NOT_FOUND, CLOSE_UNSUPPORTED_DATA, SERVER_PUSH_ONLY_DETAIL
from apps.telemetry.events import TelemetryEventType
from apps.telemetry.groups import telemetry_group_name
from config.asgi import application

ALLOWED_ORIGIN: tuple[bytes, bytes] = (b"origin", b"http://localhost")
CHANNELS_GROUP_NAME = re.compile(r"^[A-Za-z0-9._-]{1,99}$")
IN_MEMORY_LAYER: dict[str, dict[str, Any]] = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


class FailingChannelLayer(InMemoryChannelLayer):
    """Behaves like a channel layer whose transport (e.g. Redis) is down."""

    async def group_send(self, group: str, message: dict[str, Any]) -> None:
        raise ConnectionError("Error 10061 connecting to localhost:6379. Connection refused.")


def telemetry_path(project_id: UUID | str) -> str:
    return f"/ws/projects/{project_id}/telemetry/"


def communicator_for(project_id: UUID | str, origin: tuple[bytes, bytes] = ALLOWED_ORIGIN) -> WebsocketCommunicator:
    return WebsocketCommunicator(application, telemetry_path(project_id), headers=[origin])


@pytest.fixture
def in_memory_layer(settings: Settings) -> Iterator[None]:
    settings.CHANNEL_LAYERS = IN_MEMORY_LAYER
    settings.TELEMETRY_REQUIRE_EXISTING_PROJECT = False
    yield


# ---------------------------------------------------------------------------
# Group naming
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("project_id", [UUID(int=0), UUID(int=2**128 - 1), uuid4()])
def test_group_name_is_valid_for_channels(project_id: UUID) -> None:
    name = telemetry_group_name(project_id)

    assert name == f"telemetry.project.{project_id.hex}"
    assert CHANNELS_GROUP_NAME.fullmatch(name)
    assert len(name) < 100


def test_group_names_differ_per_project_and_reject_non_uuid() -> None:
    assert telemetry_group_name(uuid4()) != telemetry_group_name(uuid4())
    with pytest.raises(TypeError):
        telemetry_group_name(str(uuid4()))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Broadcast delivery over the real ASGI application
# ---------------------------------------------------------------------------
async def test_sync_broadcast_reaches_connected_client(in_memory_layer: None) -> None:
    project_id = uuid4()
    communicator = communicator_for(project_id)
    connected, _ = await communicator.connect()
    assert connected
    try:
        # Sync code (a Celery task) runs outside the event loop; sync_to_async models that.
        await sync_to_async(broadcast_telemetry)(project_id, TelemetryEventType.AST_PARSED, {"file": "ledger.py"})
        frame = await communicator.receive_json_from(timeout=2)
    finally:
        await communicator.disconnect()

    assert frame["type"] == "telemetry"
    assert frame["project_id"] == str(project_id)
    assert frame["event_type"] == "AST_PARSED"
    assert frame["payload"] == {"file": "ledger.py"}
    assert UUID(frame["event_id"])
    assert datetime.fromisoformat(frame["timestamp"]).tzinfo is not None


async def test_async_broadcast_accepts_string_ids_and_event_types(in_memory_layer: None) -> None:
    project_id = uuid4()
    communicator = communicator_for(project_id)
    connected, _ = await communicator.connect()
    assert connected
    try:
        await abroadcast_telemetry(str(project_id), "SANDBOX_LOG", {"line": "collected 4 items"})
        frame = await communicator.receive_json_from(timeout=2)
    finally:
        await communicator.disconnect()

    assert frame["event_type"] == "SANDBOX_LOG"
    assert frame["payload"] == {"line": "collected 4 items"}


async def test_project_groups_are_isolated(in_memory_layer: None) -> None:
    project_a, project_b = uuid4(), uuid4()
    client_a, client_b = communicator_for(project_a), communicator_for(project_b)
    assert (await client_a.connect())[0]
    assert (await client_b.connect())[0]
    try:
        await abroadcast_telemetry(project_a, TelemetryEventType.TEST_PASSED, {"passed": 4})
        frame = await client_a.receive_json_from(timeout=2)
        assert frame["project_id"] == str(project_a)
        assert await client_b.receive_nothing(timeout=0.2)
    finally:
        await client_a.disconnect()
        await client_b.disconnect()


async def test_payload_is_coerced_to_json_end_to_end(in_memory_layer: None) -> None:
    project_id, node_id = uuid4(), uuid4()
    when = datetime(2021, 3, 4, 5, 6, 7, tzinfo=UTC)
    communicator = communicator_for(project_id)
    assert (await communicator.connect())[0]
    try:
        await abroadcast_telemetry(
            project_id,
            TelemetryEventType.GENOME_NODE_CREATED,
            {"node_id": node_id, "at": when, "path": Path("apps") / "ledger.py"},
        )
        frame = await communicator.receive_json_from(timeout=2)
    finally:
        await communicator.disconnect()

    assert frame["payload"] == {"node_id": str(node_id), "at": "2021-03-04T05:06:07+00:00", "path": "apps/ledger.py"}


# ---------------------------------------------------------------------------
# Routing and client messages
# ---------------------------------------------------------------------------
async def test_invalid_uuid_path_does_not_route(in_memory_layer: None) -> None:
    communicator = communicator_for("not-a-uuid")
    with pytest.raises(ValueError, match="No route found"):
        await communicator.connect()


async def test_foreign_origin_is_rejected(in_memory_layer: None) -> None:
    communicator = communicator_for(uuid4(), origin=(b"origin", b"http://evil.example"))
    connected, _ = await communicator.connect()
    assert not connected


async def test_ping_gets_pong_and_other_messages_get_error(in_memory_layer: None) -> None:
    communicator = communicator_for(uuid4())
    assert (await communicator.connect())[0]
    try:
        await communicator.send_json_to({"type": "ping"})
        assert await communicator.receive_json_from(timeout=2) == {"type": "pong"}

        await communicator.send_json_to({"type": "subscribe", "project_id": "x"})
        assert await communicator.receive_json_from(timeout=2) == {"type": "error", "detail": SERVER_PUSH_ONLY_DETAIL}

        await communicator.send_to(text_data="{not json")
        error = await communicator.receive_json_from(timeout=2)
        assert error["type"] == "error"

        # The connection survives bad client input.
        await communicator.send_json_to({"type": "ping"})
        assert await communicator.receive_json_from(timeout=2) == {"type": "pong"}
    finally:
        await communicator.disconnect()


async def test_binary_frames_close_with_unsupported_data(in_memory_layer: None) -> None:
    communicator = communicator_for(uuid4())
    assert (await communicator.connect())[0]
    await communicator.send_to(bytes_data=b"\x00\x01")
    output = await communicator.receive_output(timeout=2)
    assert output == {"type": "websocket.close", "code": CLOSE_UNSUPPORTED_DATA}
    await communicator.disconnect()


# ---------------------------------------------------------------------------
# Input validation (programmer errors raise)
# ---------------------------------------------------------------------------
def test_unknown_event_type_raises(in_memory_layer: None) -> None:
    with pytest.raises(ValueError, match="Unknown telemetry event_type 'AST_PARSE'"):
        broadcast_telemetry(uuid4(), "AST_PARSE", {})


async def test_unknown_event_type_raises_async(in_memory_layer: None) -> None:
    with pytest.raises(ValueError, match="Unknown telemetry event_type"):
        await abroadcast_telemetry(uuid4(), "TEST_SKIPPED", {})


def test_invalid_project_id_raises(in_memory_layer: None) -> None:
    with pytest.raises(ValueError, match="not a valid UUID"):
        broadcast_telemetry("project-1", TelemetryEventType.ERROR, {})


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"blob": b"\x00"}, TypeError),
        ({"branches": {"main", "feat"}}, TypeError),
        ({"nested": [{"score": float("nan")}]}, ValueError),
        ({"score": float("inf")}, ValueError),
        ({1: "non-string key"}, TypeError),
        ({"model": object()}, TypeError),
        (["not", "a", "mapping"], TypeError),
    ],
)
def test_unserializable_payload_raises(in_memory_layer: None, payload: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        broadcast_telemetry(uuid4(), TelemetryEventType.ERROR, payload)


def test_unserializable_payload_error_names_location() -> None:
    with pytest.raises(TypeError, match=r"payload\['files'\]\[1\]: bytes"):
        build_telemetry_frame(uuid4(), TelemetryEventType.AST_PARSED, {"files": ["ok.py", b"bad"]})


def test_json_coercion_of_common_types() -> None:
    node_id = uuid4()
    payload = {
        "node_id": node_id,
        "at": datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        "day": date(2021, 6, 1),
        "posix_path": Path("apps") / "genome" / "agents.py",
        "windows_path": PureWindowsPath(r"C:\work\paypulse\ledger.py"),
        "stage": TelemetryEventType.SANDBOX_LOG,
        "lines": (1, 2.5, True, None),
        "nested": {"ids": [node_id]},
    }

    frame = build_telemetry_frame(uuid4(), TelemetryEventType.GENOME_NODE_CREATED, payload)
    dumped = frame.model_dump(mode="json")

    assert dumped["payload"] == {
        "node_id": str(node_id),
        "at": "2024-01-02T03:04:05+00:00",
        "day": "2021-06-01",
        "posix_path": "apps/genome/agents.py",
        "windows_path": "C:/work/paypulse/ledger.py",
        "stage": "SANDBOX_LOG",
        "lines": [1, 2.5, True, None],
        "nested": {"ids": [str(node_id)]},
    }
    assert json.loads(json.dumps(dumped)) == dumped


# ---------------------------------------------------------------------------
# Transport failures are logged, never raised
# ---------------------------------------------------------------------------
@pytest.fixture
def failing_layer(settings: Settings) -> Iterator[None]:
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "tests.test_telemetry.FailingChannelLayer"}}
    yield


def test_sync_transport_failure_is_logged_not_raised(failing_layer: None, caplog: pytest.LogCaptureFixture) -> None:
    project_id = uuid4()
    with caplog.at_level(logging.ERROR, logger="apps.telemetry.broadcast"):
        broadcast_telemetry(project_id, TelemetryEventType.INGESTION_STARTED, {"branch": "main"})

    [record] = [r for r in caplog.records if r.name == "apps.telemetry.broadcast"]
    assert "INGESTION_STARTED" in record.getMessage()
    assert str(project_id) in record.getMessage()
    assert record.exc_info is not None and isinstance(record.exc_info[1], ConnectionError)


async def test_async_transport_failure_is_logged_not_raised(
    failing_layer: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="apps.telemetry.broadcast"):
        await abroadcast_telemetry(uuid4(), TelemetryEventType.ERROR, {"detail": "boom"})

    assert any(r.name == "apps.telemetry.broadcast" and r.exc_info for r in caplog.records)


def test_programmer_errors_still_raise_when_transport_is_down(failing_layer: None) -> None:
    with pytest.raises(ValueError):
        broadcast_telemetry(uuid4(), "NOT_AN_EVENT", {})


# ---------------------------------------------------------------------------
# Project existence check (needs PostgreSQL; skipped otherwise)
# ---------------------------------------------------------------------------
@pytest.fixture
def existence_check_layer(settings: Settings) -> Iterator[None]:
    settings.CHANNEL_LAYERS = IN_MEMORY_LAYER
    settings.TELEMETRY_REQUIRE_EXISTING_PROJECT = True
    yield


@pytest.mark.django_db(transaction=True)
async def test_unknown_project_is_closed_with_4404(existence_check_layer: None) -> None:
    communicator = communicator_for(uuid4())
    connected, _ = await communicator.connect()
    assert connected  # accepted so the close code reaches the browser
    output = await communicator.receive_output(timeout=2)
    assert output == {"type": "websocket.close", "code": CLOSE_PROJECT_NOT_FOUND}
    await communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_existing_project_connects_and_receives(existence_check_layer: None) -> None:
    project = await database_sync_to_async(Project.objects.create)(name="PayPulse")
    communicator = communicator_for(project.id)
    assert (await communicator.connect())[0]
    try:
        await abroadcast_telemetry(project.id, TelemetryEventType.INGESTION_COMPLETE, {"artifacts": 12})
        frame = await communicator.receive_json_from(timeout=2)
    finally:
        await communicator.disconnect()

    assert frame["project_id"] == str(project.id)
    assert frame["payload"] == {"artifacts": 12}
