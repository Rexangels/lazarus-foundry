"""Bodies of ``GET /api/projects/{project_id}/rebirth`` (``frontend/src/lib/types.ts`` mirrors them)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from ninja import Schema

RebirthNotReadyCode = Literal["REBIRTH_PENDING", "REBIRTH_UNAVAILABLE"]


class RebirthProjectOut(Schema):
    id: UUID
    name: str
    status: str
    status_detail: str
    default_branch: str


class RebirthFileOut(Schema):
    path: str
    status: str  # A, M, D or T
    additions: int | None  # None for a binary file
    deletions: int | None


class RebirthOut(Schema):
    id: UUID
    project: RebirthProjectOut
    foundry_run_id: UUID
    strategy_id: UUID
    strategy_type: str
    revival_branch: str
    base_sha: str
    rebirth_commit_sha: str
    pr_title: str
    pr_body_markdown: str
    unified_diff_patch: str
    files: list[RebirthFileOut]
    exported_kiro_specs_path: str
    spec_files: list[str]
    continuity_memory: dict[str, Any]
    preview_url: str
    before_test_output: str
    after_test_output: str
    tests_passed: bool
    iterations_used: int
    created_at: datetime
    telemetry_ws_path: str


class RebirthNotReadyOut(Schema):
    """409: there is no rebirth artifact yet (``REBIRTH_PENDING``) or there will be none (``REBIRTH_UNAVAILABLE``)."""

    detail: str
    code: RebirthNotReadyCode
    project_id: UUID
    status: str
    status_detail: str
    telemetry_ws_path: str
