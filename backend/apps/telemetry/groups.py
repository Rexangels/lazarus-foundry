"""Channel-layer group naming shared by the consumer and the broadcaster."""
from typing import Final
from uuid import UUID

GROUP_PREFIX: Final[str] = "telemetry.project."


def telemetry_group_name(project_id: UUID) -> str:
    """Return the Channels group for one project's telemetry stream.

    Uses the 32-character hex form of the UUID, so the result is always
    ``telemetry.project.<hex>`` (50 ASCII characters), within Channels' rule of
    alphanumerics, hyphens, underscores and periods, shorter than 100 characters.
    """
    if not isinstance(project_id, UUID):
        raise TypeError(f"project_id must be a uuid.UUID, got {type(project_id).__name__}")
    return f"{GROUP_PREFIX}{project_id.hex}"
