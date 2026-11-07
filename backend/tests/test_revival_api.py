"""Fast tests (no database) for the revival API: the approval token, the recommendation rule,
request validation, the spec-document allowlist and the OpenAPI document.

The database behavior of the same endpoints is in ``tests/test_revival_approval.py``.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.test import Client

from apps.revival.api import parse_approver
from apps.revival.approval import (
    RankedStrategy,
    approval_is_intact,
    recommended_strategy_id,
    strategy_approval_token,
)

STRATEGY_ID = "6f1c2a1e-0000-4000-8000-000000000001"
VALID_TOKEN = "a" * 64


def strategy(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "pk": STRATEGY_ID,
        "strategy_type": "RESTORE",
        "feasibility_score": 68.1,
        "value_score": 0.681,
        "debt_cost_score": 0.3333,
        "risk_score": 0.3,
        "action_plan": {
            "items": [{"id": "RESTORE-01", "kind": "MERGE_ABANDONED_BRANCH", "files_to_modify": ["a.py"]}],
            "in_scope": [{"feature_id": "f1", "feasibility": 0.68}],
            "deferred": [{"feature_id": "f2", "reason": "NEEDS_NEW_DEPENDENCY", "detail": "PDF"}],
            "files": ["a.py"],
            "extract_root": None,
            "risk_evidence": {"merge_distance": 5},
        },
        "generated_kiro_specs": {
            "spec_name": "lazarus-restore-toy",
            "requirements_md": "# Requirements\n",
            "design_md": "# Design\n",
            "tasks_md": "# Implementation Plan\n",
            "evidence_index": {"E1": {"ref": "x"}},
            "mode": "DETERMINISTIC",
            "generated_at": "2026-01-01T00:00:00+00:00",
        },
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def approve_url(project_id: object = None, strategy_id: object = None) -> str:
    return f"/api/projects/{project_id or uuid4()}/strategies/{strategy_id or uuid4()}/approve"


# ---------------------------------------------------------------------------
# Approval token
# ---------------------------------------------------------------------------
def test_token_is_a_deterministic_sha256_independent_of_key_order() -> None:
    base = strategy()
    reordered = strategy(
        action_plan=dict(reversed(list(copy.deepcopy(base.action_plan).items()))),
        generated_kiro_specs=dict(reversed(list(base.generated_kiro_specs.items()))),
    )

    token = strategy_approval_token(base)

    assert len(token) == 64 and int(token, 16) >= 0
    assert token == strategy_approval_token(strategy()) == strategy_approval_token(reordered)


def _changed_plan_item() -> dict[str, Any]:
    plan = copy.deepcopy(strategy().action_plan)
    plan["items"][0]["files_to_modify"] = ["a.py", "b.py"]
    return {"action_plan": plan}


def _changed_spec(key: str) -> dict[str, Any]:
    specs = dict(strategy().generated_kiro_specs)
    specs[key] += "- [ ] 9. One more task\n"
    return {"generated_kiro_specs": specs}


@pytest.mark.parametrize(
    "change",
    [
        {"pk": "6f1c2a1e-0000-4000-8000-000000000002"},
        {"strategy_type": "MODERNIZE"},
        {"feasibility_score": 68.2},
        {"value_score": 0.6811},
        {"debt_cost_score": 0.4},
        {"risk_score": 0.31},
        _changed_plan_item(),
        {"action_plan": {**strategy().action_plan, "deferred": []}},
        _changed_spec("requirements_md"),
        _changed_spec("design_md"),
        _changed_spec("tasks_md"),
    ],
    ids=["id", "type", "feasibility", "value", "debt", "risk", "plan-item", "deferred",
         "requirements", "design", "tasks"],
)
def test_token_changes_with_any_reviewed_content(change: dict[str, Any]) -> None:
    assert strategy_approval_token(strategy(**change)) != strategy_approval_token(strategy())


def test_approval_is_intact_only_for_the_matching_token() -> None:
    row = strategy()
    token = strategy_approval_token(row)
    assert approval_is_intact(row, token)
    assert not approval_is_intact(row, "")
    assert not approval_is_intact(strategy(risk_score=0.9), token)


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------
def ranked(kind: str, score: float, actionable: bool = True, pk: str | None = None) -> RankedStrategy:
    return RankedStrategy(pk or kind.lower(), kind, score, actionable)


def test_recommendation_is_the_highest_scoring_actionable_strategy() -> None:
    rows = [ranked("RESTORE", 68.1), ranked("MODERNIZE", 58.1), ranked("EXTRACT", 90.0, actionable=False)]
    assert recommended_strategy_id(rows) == "restore"


def test_recommendation_ties_go_to_strategy_order_then_id() -> None:
    assert recommended_strategy_id([ranked("EXTRACT", 60.0), ranked("MODERNIZE", 60.0)]) == "modernize"
    assert recommended_strategy_id([ranked("RESTORE", 60.0, pk="b"), ranked("RESTORE", 60.0, pk="a")]) == "a"


def test_no_actionable_strategy_means_no_recommendation() -> None:
    assert recommended_strategy_id([ranked("EXTRACT", 43.0, actionable=False)]) is None
    assert recommended_strategy_id([]) is None


# ---------------------------------------------------------------------------
# Request validation and the header gate (rejected before any query)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [{}, {"approval_token": ""}, {"approval_token": "   "}, {"approval_token": "a" * 63},
     {"approval_token": "g" * 64}, {"approval_token": VALID_TOKEN, "approved_by_human": True}],
    ids=["missing", "blank", "spaces", "short", "not-hex", "extra-field"],
)
def test_approve_rejects_a_malformed_body_with_422(client: Client, body: dict[str, Any]) -> None:
    response = client.post(approve_url(), body, content_type="application/json")
    assert response.status_code == 422


def test_approve_requires_the_approver_header_when_configured(client: Client, settings: Any) -> None:
    settings.REVIVAL_APPROVAL_REQUIRES_HEADER = True

    response = client.post(approve_url(), {"approval_token": VALID_TOKEN}, content_type="application/json")

    assert response.status_code == 403 and response.json()["code"] == "APPROVER_REQUIRED"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("   ", None), ("Ada Lovelace", "Ada Lovelace"), ("  ada  ", "ada"),
     ("x" * 256, None), ("evil\r\nX-Injected: 1", None)],
)
def test_parse_approver(raw: str | None, expected: str | None) -> None:
    assert parse_approver(raw) == expected


# ---------------------------------------------------------------------------
# Spec document allowlist
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "doc",
    ["../settings.py", "..%2F..%2Fconfig%2Fsettings.py", "%2e%2e", "..", "README.md", "REQUIREMENTS.md",
     "requirements_md", "tasks.md.bak", "evidence_index"],
)
def test_spec_download_rejects_names_outside_the_allowlist(client: Client, doc: str) -> None:
    response = client.get(f"/api/projects/{uuid4()}/strategies/{uuid4()}/specs/{doc}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------
def test_openapi_documents_the_strategy_routes(client: Client) -> None:
    document = client.get("/api/openapi.json").json()
    paths = document["paths"]
    base = "/api/projects/{project_id}/strategies"
    assert {"200", "404", "409"} <= set(paths[base]["get"]["responses"])
    approve = paths[base + "/{strategy_id}/approve"]["post"]
    assert {"200", "403", "404", "409"} <= set(approve["responses"])
    body_ref = approve["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    body_schema = document["components"]["schemas"][body_ref.rsplit("/", 1)[-1]]
    assert body_schema["required"] == ["approval_token"]
    assert any(p["name"] == "X-Lazarus-Approver" and p["in"] == "header" for p in approve["parameters"])
    spec = paths[base + "/{strategy_id}/specs/{doc}"]["get"]
    assert "text/markdown" in spec["responses"]["200"]["content"] and "404" in spec["responses"]
