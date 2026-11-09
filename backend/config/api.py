"""Root Django Ninja API, mounted at ``/api/`` in ``config.urls``.

App routers are attached here as the apps are built (``api.add_router(...)``).
"""
from datetime import datetime, timezone
from typing import Literal

from django.http import HttpRequest
from ninja import NinjaAPI, Schema

from apps.genome.api import router as genome_router
from apps.projects.api import router as projects_router
from apps.rebirth.api import router as rebirth_router
from apps.revival.api import router as revival_router

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


api.add_router("/projects/", projects_router)
# Same prefix: /api/projects/{project_id}/genome lives in the genome app.
api.add_router("/projects/", genome_router)
# .../strategies, .../strategies/{strategy_id}/approve and .../specs/{doc} live in the revival app.
api.add_router("/projects/", revival_router)
# .../rebirth lives in the rebirth app.
api.add_router("/projects/", rebirth_router)
