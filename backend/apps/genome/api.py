"""Genome endpoint (mounted at ``/api/projects/`` in ``config.api``, next to ``apps.projects.api``).

``GET /api/projects/{project_id}/genome``
    200 ``GenomeGraphOut``: the latest synthesis's React Flow graph, evidence
    citations, conflicts and score breakdowns (``apps.genome.graph``).
    404 for an unknown project.
    409 ``GenomeNotReadyOut`` when there is nothing to show yet:
    ``GENOME_PENDING`` while the project is ``DORMANT``, ``INGESTING`` or
    ``SYNTHESIZING_GENOME`` (even if an older genome is still stored, since
    it is about to be replaced), ``GENOME_UNAVAILABLE`` when no genome exists
    in any other status (``FAILED`` included, with its ``status_detail``). A
    ``FAILED`` project that still has a genome (synthesis failed on a re-run)
    returns that genome; ``project.status`` says it failed.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router

from apps.genome.graph import build_genome_graph_from_inputs
from apps.genome.schemas import GenomeGraphOut, GenomeNotReadyOut
from apps.genome.services import load_genome_inputs
from apps.projects.api import ErrorOut, telemetry_ws_path
from apps.projects.models import Project, ProjectStatus

router: Router = Router(tags=["genome"])

GENOME_PENDING_STATUSES: frozenset[str] = frozenset({
    ProjectStatus.DORMANT,
    ProjectStatus.INGESTING,
    ProjectStatus.SYNTHESIZING_GENOME,
})


def _not_ready(project: Project, code: str, detail: str) -> GenomeNotReadyOut:
    return GenomeNotReadyOut(
        detail=detail,
        code=code,
        project_id=project.pk,
        status=ProjectStatus(project.status).value,
        status_detail=project.status_detail,
        telemetry_ws_path=telemetry_ws_path(project.pk),
    )


@router.get(
    "/{uuid:project_id}/genome",
    response={200: GenomeGraphOut, 404: ErrorOut, 409: GenomeNotReadyOut},
    summary="Project Genome graph, evidence citations, conflicts and confidence breakdowns",
)
def get_genome(request: HttpRequest, project_id: UUID) -> Any:
    project = get_object_or_404(Project, pk=project_id)
    status = ProjectStatus(project.status).value
    if project.status in GENOME_PENDING_STATUSES:
        return 409, _not_ready(
            project,
            "GENOME_PENDING",
            f"The genome is not ready: the project is {status}. Follow the telemetry WebSocket "
            "and request the genome again after GENOME_COMPLETE.",
        )
    inputs = load_genome_inputs(project)
    if not inputs.features:
        detail = (
            f"No genome is available: the project is {status}."
            if project.status != ProjectStatus.FAILED
            else "No genome is available: the project FAILED before a genome was synthesized."
        )
        return 409, _not_ready(project, "GENOME_UNAVAILABLE", detail)
    return 200, build_genome_graph_from_inputs(inputs)
