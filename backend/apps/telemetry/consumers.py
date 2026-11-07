"""Server-push WebSocket consumer for ``ws/projects/<uuid:project_id>/telemetry/``.

Connection lifecycle:

1. The URL route's ``uuid`` converter guarantees a valid ``project_id``;
   any other path never reaches this consumer.
2. When ``settings.TELEMETRY_REQUIRE_EXISTING_PROJECT`` is true (the default),
   the project must exist in the database. Unknown projects are accepted and
   then immediately closed with code 4404. Accepting first is deliberate: a
   close before the handshake completes becomes an HTTP 403 and browsers only
   report code 1006, so the client could not tell "no such project" (stop
   reconnecting) from a network failure (retry). Set the setting to false to
   skip the lookup, e.g. for clients that subscribe before the project row is
   committed.
3. The socket joins the project's group and receives every frame published by
   ``broadcast_telemetry`` / ``abroadcast_telemetry`` until it disconnects.

If the database or channel layer fails during the handshake, the socket is
closed with 1011 (internal error) and the failure is logged.

Clients may send ``{"type": "ping"}`` and get ``{"type": "pong"}``. Any other
text message gets an ``{"type": "error", ...}`` frame and the connection stays
open. Binary frames are closed with 1003 (unsupported data).
"""
import logging
from typing import Any, Final
from uuid import UUID

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.db import DatabaseError

from apps.projects.models import Project
from apps.telemetry.events import FRAME_TYPE_ERROR, FRAME_TYPE_PONG
from apps.telemetry.groups import telemetry_group_name

logger = logging.getLogger(__name__)

CLOSE_UNSUPPORTED_DATA: Final[int] = 1003
CLOSE_INTERNAL_ERROR: Final[int] = 1011
CLOSE_PROJECT_NOT_FOUND: Final[int] = 4404

PING_MESSAGE_TYPE: Final[str] = "ping"
SERVER_PUSH_ONLY_DETAIL: Final[str] = (
    'This channel is server-push only; the only accepted client message is {"type": "ping"}.'
)


@database_sync_to_async
def project_exists(project_id: UUID) -> bool:
    return Project.objects.filter(pk=project_id).exists()


class TelemetryConsumer(AsyncJsonWebsocketConsumer):
    group_name: str | None = None

    async def connect(self) -> None:
        project_id: UUID = self.scope["url_route"]["kwargs"]["project_id"]

        if settings.TELEMETRY_REQUIRE_EXISTING_PROJECT:
            try:
                exists = await project_exists(project_id)
            except DatabaseError:
                logger.exception("Telemetry connect for project %s: project lookup failed.", project_id)
                await self._accept_and_close(CLOSE_INTERNAL_ERROR)
                return
            if not exists:
                await self._accept_and_close(CLOSE_PROJECT_NOT_FOUND)
                return

        if self.channel_layer is None:
            logger.error("Telemetry connect for project %s: no channel layer is configured.", project_id)
            await self._accept_and_close(CLOSE_INTERNAL_ERROR)
            return

        group_name = telemetry_group_name(project_id)
        try:
            await self.channel_layer.group_add(group_name, self.channel_name)
        except Exception:
            logger.exception("Telemetry connect for project %s: joining group %s failed.", project_id, group_name)
            await self._accept_and_close(CLOSE_INTERNAL_ERROR)
            return
        self.group_name = group_name
        await self.accept()

    async def disconnect(self, code: int) -> None:
        if self.group_name is None:
            return
        try:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        except Exception:
            logger.exception("Telemetry disconnect: leaving group %s failed.", self.group_name)
        finally:
            self.group_name = None

    async def receive(self, text_data: str | None = None, bytes_data: bytes | None = None, **kwargs: Any) -> None:
        if text_data is None:
            await self.close(code=CLOSE_UNSUPPORTED_DATA)
            return
        try:
            content = await self.decode_json(text_data)
        except ValueError:
            await self._send_error("Message is not valid JSON.")
            return
        await self.receive_json(content, **kwargs)

    async def receive_json(self, content: Any, **kwargs: Any) -> None:
        if isinstance(content, dict) and content.get("type") == PING_MESSAGE_TYPE:
            await self.send_json({"type": FRAME_TYPE_PONG})
            return
        await self._send_error(SERVER_PUSH_ONLY_DETAIL)

    async def telemetry_event(self, message: dict[str, Any]) -> None:
        """Handle ``telemetry.event`` messages from the group: forward the frame as-is."""
        await self.send_json(message["frame"])

    async def _send_error(self, detail: str) -> None:
        await self.send_json({"type": FRAME_TYPE_ERROR, "detail": detail})

    async def _accept_and_close(self, code: int) -> None:
        await self.accept()
        await self.close(code=code)
