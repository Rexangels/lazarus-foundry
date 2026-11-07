"""Pure rules of the human approval gate (no database): the approval token and the recommendation.

Approval token
    ``approval_token(...)`` is the SHA-256 (hex) of a canonical JSON document
    holding exactly what the Defibrillator shows for review: the strategy id
    and type, the four score columns, the whole ``action_plan`` and the three
    spec documents. Canonical means sorted keys, no whitespace, UTF-8 and
    ``allow_nan=False``, so a JSONB round trip (which reorders keys) gives the
    same token. ``GET .../strategies`` returns it, ``POST .../approve`` must
    send it back, and the stored ``approved_token`` lets later stages prove
    that the approved content was not altered (``approval_is_intact``).

    The spec's ``evidence_index`` is not hashed separately: every footnote a
    document uses is defined inside that document's Markdown, which is hashed.

Recommendation
    ``recommended_strategy_id`` picks the highest ``feasibility_score`` among
    actionable strategies; ties go to the earlier type in ``RESTORE``,
    ``MODERNIZE``, ``EXTRACT`` order, then to the smaller id. ``None`` when no
    strategy is actionable.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from apps.revival.models import StrategyType

TOKEN_VERSION: str = "lazarus-approval-v1"
TOKEN_LENGTH: int = 64
SCORE_FIELDS: tuple[str, ...] = ("feasibility_score", "value_score", "debt_cost_score", "risk_score")
# Spec document file name -> key in RevivalStrategy.generated_kiro_specs. Also the download allowlist.
SPEC_DOCUMENTS: dict[str, str] = {
    "requirements.md": "requirements_md",
    "design.md": "design_md",
    "tasks.md": "tasks_md",
}
STRATEGY_ORDER: dict[str, int] = {kind.value: index for index, kind in enumerate(StrategyType)}


class ApprovableStrategy(Protocol):
    """The ``RevivalStrategy`` attributes the token covers."""

    pk: Any
    strategy_type: str
    feasibility_score: float
    value_score: float
    debt_cost_score: float
    risk_score: float
    action_plan: Any
    generated_kiro_specs: Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def approval_token(
    *,
    strategy_id: str,
    strategy_type: str,
    scores: Mapping[str, float],
    action_plan: Mapping[str, Any],
    specs: Mapping[str, Any],
) -> str:
    """SHA-256 hex of the reviewed content (see the module docstring)."""
    document = {
        "version": TOKEN_VERSION,
        "strategy_id": str(strategy_id),
        "strategy_type": str(strategy_type),
        "scores": {name: float(scores[name]) for name in SCORE_FIELDS},
        "action_plan": action_plan,
        "specs": {name: str(specs.get(key) or "") for name, key in SPEC_DOCUMENTS.items()},
    }
    return hashlib.sha256(canonical_json(document)).hexdigest()


def strategy_approval_token(strategy: ApprovableStrategy) -> str:
    return approval_token(
        strategy_id=str(strategy.pk),
        strategy_type=str(strategy.strategy_type),
        scores={name: getattr(strategy, name) for name in SCORE_FIELDS},
        action_plan=strategy.action_plan or {},
        specs=strategy.generated_kiro_specs or {},
    )


def approval_is_intact(strategy: ApprovableStrategy, approved_token: str) -> bool:
    """True when ``approved_token`` is set and still matches the strategy's current content."""
    return bool(approved_token) and approved_token == strategy_approval_token(strategy)


@dataclass(frozen=True)
class RankedStrategy:
    id: str
    strategy_type: str
    feasibility_score: float
    is_actionable: bool


def recommended_strategy_id(strategies: Iterable[RankedStrategy]) -> str | None:
    candidates = [s for s in strategies if s.is_actionable]
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda s: (-s.feasibility_score, STRATEGY_ORDER.get(s.strategy_type, len(STRATEGY_ORDER)), s.id),
    )
    return best.id
