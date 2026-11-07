"""Root Django Ninja API, mounted at ``/api/`` in ``config.urls``.

App routers are attached here as the apps are built (``api.add_router(...)``).
"""
from datetime import datetime, timezone
from typing import Literal

from django.http import HttpRequest
from ninja import NinjaAPI, Schema

API_VERSION: str = "0.1.0"
SERVICE_NAME: str = "lazarus-foundry"

api: NinjaAPI = NinjaAPI(
    title="Lazarus Foundry API",
    version=API_VERSION,
    description="AI software revival foundry: ingestion, genome, revival, foundry, rebirth.",
)


class HealthResponse(Schema):
    status: Literal["ok"]
    service: str
    version: str
    timestamp: datetime


@api.get("/health", response=HealthResponse, tags=["system"], summary="Liveness check")
def health(request: HttpRequest) -> HealthResponse:
    """Report that the API process is up. Does not touch PostgreSQL or Redis."""
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        version=API_VERSION,
        timestamp=datetime.now(timezone.utc),
    )
