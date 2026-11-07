"""Bounded revival feasibility score Score(S_k) and its V, D and R inputs.

Pure module: no ORM queries, no network, no model calls. The formula and the
derivation of every input are specified in
``.kiro/steering/forensic-invariants.md`` section 4:

    Score(S_k) = round(100 * (alpha * V + beta * (1 - D) + delta * (1 - R)), 1)

* ``V`` (``value_ratio``): the projected fraction of features that are
  ``VERIFIED_WORKING`` after the plan. Currently verified features count 1;
  each in-scope target feature counts its feasibility (the weakest of its
  plan items' feasibilities), so a speculative item is never a sure win. For
  ``EXTRACT`` it is the extracted cluster's share of the currently verified
  features.
* ``D`` (``debt_cost``): ``min(1, |files_to_modify U files_to_create| / 15)``.
* ``R`` (``strategy_risk``): the per-type base (``RESTORE`` 0.25,
  ``MODERNIZE`` 0.60, ``EXTRACT`` 0.35) plus named evidence factors
  (``RiskFactor``), each ``min(cap, per_unit * count)``, clamped to [0, 1].
  With no such evidence R is exactly the base.

V, D and R are rounded to ``input_decimals`` (4) before the formula, so the
stored ``value_score`` / ``debt_cost_score`` / ``risk_score`` reproduce the
stored ``feasibility_score`` exactly. Rounding is monotone, so the score stays
non-decreasing in V and non-increasing in D and R.

``score_plan`` ties ``apps.revival.planner.StrategyPlan`` to the formula and
returns a JSON-ready breakdown for the Defibrillator view.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from apps.revival.models import StrategyType

if TYPE_CHECKING:  # pragma: no cover - typing only; the planner imports this module
    from apps.revival.planner import StrategyPlan

WEIGHT_SUM_TOLERANCE: float = 1e-9
FORMULA: str = "round(100 * (alpha * V + beta * (1 - D) + delta * (1 - R)), 1)"


def _require_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a real number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return number


def _require_unit(name: str, value: object) -> float:
    number = _require_number(name, value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must lie in [0, 1], got {number!r}")
    return number


def _require_count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")
    return value


def _require_decimals(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if not 0 <= value <= 12:
        raise ValueError(f"{name} must lie in [0, 12]")
    return value


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
class RiskFactor(StrEnum):
    """Evidence that raises a strategy's risk above its base (forensic-invariants.md section 4)."""

    # Sum of commits_behind of the branches the plan merges: the further the
    # default branch moved on, the more the merge can conflict or regress.
    MERGE_DISTANCE = "MERGE_DISTANCE"
    # Exact pins the plan upgrades: each upgrade is a possible API break.
    PINNED_UPGRADES = "PINNED_UPGRADES"
    # Files outside the extracted cluster that its imports drag along.
    EXTRACT_BOUNDARY = "EXTRACT_BOUNDARY"


@dataclass(frozen=True)
class RiskFactorRule:
    """``adjustment = min(cap, per_unit * count)`` for the strategy types in ``applies_to``."""

    per_unit: float
    cap: float
    applies_to: frozenset[StrategyType]

    def __post_init__(self) -> None:
        if _require_number("per_unit", self.per_unit) < 0.0:
            raise ValueError("per_unit must be >= 0 (evidence never lowers risk below the base)")
        _require_unit("cap", self.cap)
        object.__setattr__(self, "applies_to", frozenset(StrategyType(t) for t in self.applies_to))
        if not self.applies_to:
            raise ValueError("applies_to must name at least one strategy type")


DEFAULT_BASE_RISK: Mapping[StrategyType, float] = MappingProxyType(
    {StrategyType.RESTORE: 0.25, StrategyType.MODERNIZE: 0.60, StrategyType.EXTRACT: 0.35}
)
DEFAULT_RISK_RULES: Mapping[RiskFactor, RiskFactorRule] = MappingProxyType(
    {
        RiskFactor.MERGE_DISTANCE: RiskFactorRule(
            per_unit=0.01, cap=0.15, applies_to=frozenset({StrategyType.RESTORE, StrategyType.MODERNIZE})
        ),
        RiskFactor.PINNED_UPGRADES: RiskFactorRule(
            per_unit=0.025, cap=0.20, applies_to=frozenset({StrategyType.MODERNIZE})
        ),
        RiskFactor.EXTRACT_BOUNDARY: RiskFactorRule(
            per_unit=0.05, cap=0.20, applies_to=frozenset({StrategyType.EXTRACT})
        ),
    }
)


@dataclass(frozen=True)
class RevivalScoringParameters:
    """Every constant of Score(S_k) and of its V, D, R derivations. Use ``DEFAULT_PARAMETERS``."""

    value_weight: float = 0.50  # alpha
    debt_weight: float = 0.30  # beta
    risk_weight: float = 0.20  # delta
    debt_file_budget: float = 15.0  # D = min(1, files / budget)
    base_risk: Mapping[StrategyType, float] = field(default_factory=lambda: DEFAULT_BASE_RISK, hash=False)
    risk_rules: Mapping[RiskFactor, RiskFactorRule] = field(default_factory=lambda: DEFAULT_RISK_RULES, hash=False)
    input_decimals: int = 4  # V, D and R are rounded to this before the formula
    score_decimals: int = 1

    def __post_init__(self) -> None:
        weights = [
            _require_unit(name, getattr(self, name)) for name in ("value_weight", "debt_weight", "risk_weight")
        ]
        if abs(sum(weights) - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise ValueError(f"value_weight + debt_weight + risk_weight must equal 1.0, got {sum(weights)!r}")
        if _require_number("debt_file_budget", self.debt_file_budget) <= 0.0:
            raise ValueError("debt_file_budget must be > 0")
        base: dict[StrategyType, float] = {}
        for key, value in self.base_risk.items():
            base[StrategyType(key)] = _require_unit(f"base_risk[{key}]", value)
        missing = [t.value for t in StrategyType if t not in base]
        if missing:
            raise ValueError(f"base_risk is missing {missing}")
        object.__setattr__(self, "base_risk", MappingProxyType(base))
        rules: dict[RiskFactor, RiskFactorRule] = {}
        for key, rule in self.risk_rules.items():
            if not isinstance(rule, RiskFactorRule):
                raise TypeError(f"risk_rules[{key}] must be a RiskFactorRule")
            rules[RiskFactor(key)] = rule
        object.__setattr__(self, "risk_rules", MappingProxyType(rules))
        _require_decimals("input_decimals", self.input_decimals)
        _require_decimals("score_decimals", self.score_decimals)

    def as_dict(self) -> dict[str, Any]:
        return {
            "value_weight": self.value_weight,
            "debt_weight": self.debt_weight,
            "risk_weight": self.risk_weight,
            "debt_file_budget": self.debt_file_budget,
            "base_risk": {t.value: self.base_risk[t] for t in StrategyType},
            "risk_rules": {
                factor.value: {
                    "per_unit": rule.per_unit,
                    "cap": rule.cap,
                    "applies_to": sorted(t.value for t in rule.applies_to),
                }
                for factor, rule in sorted(self.risk_rules.items(), key=lambda entry: entry[0].value)
            },
            "input_decimals": self.input_decimals,
            "score_decimals": self.score_decimals,
            "formula": FORMULA,
        }


DEFAULT_PARAMETERS: RevivalScoringParameters = RevivalScoringParameters()


# ---------------------------------------------------------------------------
# Score(S_k)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrategyScore:
    """Score(S_k) with the rounded inputs it was computed from and each weighted term."""

    score: float
    value: float  # V, rounded
    debt_cost: float  # D, rounded
    risk: float  # R, rounded
    value_term: float  # alpha * V
    debt_term: float  # beta * (1 - D)
    risk_term: float  # delta * (1 - R)
    parameters: RevivalScoringParameters = field(default=DEFAULT_PARAMETERS, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        def points(term: float) -> float:
            return round(100.0 * term, 4)

        p = self.parameters
        return {
            "score": self.score,
            "formula": FORMULA,
            "value": self.value,
            "debt_cost": self.debt_cost,
            "risk": self.risk,
            "weights": {"value": p.value_weight, "debt": p.debt_weight, "risk": p.risk_weight},
            # Contribution of each term in score points (they sum to the score before rounding).
            "terms": {
                "value": {"weight": p.value_weight, "input": self.value, "term": self.value_term,
                          "points": points(self.value_term)},
                "debt": {"weight": p.debt_weight, "input": round(1.0 - self.debt_cost, 12),
                         "term": self.debt_term, "points": points(self.debt_term)},
                "risk": {"weight": p.risk_weight, "input": round(1.0 - self.risk, 12),
                         "term": self.risk_term, "points": points(self.risk_term)},
            },
        }


def score_revival_strategy(
    v: float, d: float, r: float, params: RevivalScoringParameters = DEFAULT_PARAMETERS
) -> StrategyScore:
    """Score(S_k) for value V, debt cost D and risk R, each in [0, 1]. Always in [0, 100]."""
    value = round(_require_unit("V", v), params.input_decimals)
    debt = round(_require_unit("D", d), params.input_decimals)
    risk = round(_require_unit("R", r), params.input_decimals)
    value_term = params.value_weight * value
    debt_term = params.debt_weight * (1.0 - debt)
    risk_term = params.risk_weight * (1.0 - risk)
    raw = 100.0 * (value_term + debt_term + risk_term)
    # The weights sum to 1 and every input is in [0, 1], so raw is in [0, 100] up to float error.
    score = round(min(100.0, max(0.0, raw)), params.score_decimals)
    return StrategyScore(score, value, debt, risk, value_term, debt_term, risk_term, params)


# ---------------------------------------------------------------------------
# V, D and R
# ---------------------------------------------------------------------------
class ValueMode(StrEnum):
    PROJECTED_VERIFIED = "PROJECTED_VERIFIED"  # RESTORE, MODERNIZE
    EXTRACT_SHARE = "EXTRACT_SHARE"  # EXTRACT


@dataclass(frozen=True)
class ValueTarget:
    """A feature the plan aims to bring to VERIFIED_WORKING, weighted by its feasibility."""

    feature_id: str
    title: str
    feasibility: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "feasibility", _require_unit("feasibility", self.feasibility))


@dataclass(frozen=True)
class ValueBasis:
    """What V is computed from (see ``value_ratio``)."""

    mode: ValueMode
    total_features: int
    verified_feature_ids: tuple[str, ...]
    targets: tuple[ValueTarget, ...] = ()
    extracted_feature_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", ValueMode(self.mode))
        total = _require_count("total_features", self.total_features)
        verified = set(self.verified_feature_ids)
        if len(verified) != len(self.verified_feature_ids):
            raise ValueError("verified_feature_ids must be unique")
        if len(verified) > total:
            raise ValueError("more verified features than features")
        target_ids = [target.feature_id for target in self.targets]
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("a feature can be a value target only once")
        if verified & set(target_ids):
            raise ValueError("a verified feature cannot also be a value target")
        if len(verified) + len(target_ids) > total:
            raise ValueError("more verified features and targets than features")
        extracted = set(self.extracted_feature_ids)
        if len(extracted) != len(self.extracted_feature_ids):
            raise ValueError("extracted_feature_ids must be unique")
        if not extracted <= verified:
            raise ValueError("only verified features can be extracted")
        if self.mode is ValueMode.EXTRACT_SHARE and self.targets:
            raise ValueError("EXTRACT_SHARE has no value targets")
        if self.mode is ValueMode.PROJECTED_VERIFIED and self.extracted_feature_ids:
            raise ValueError("PROJECTED_VERIFIED extracts nothing")


def value_ratio(basis: ValueBasis) -> float:
    """V in [0, 1].

    ``PROJECTED_VERIFIED``: ``(verified + sum(target feasibility)) / total``.
    ``EXTRACT_SHARE``: ``extracted / verified``. Either is 0 when its denominator is 0.
    """
    if basis.mode is ValueMode.EXTRACT_SHARE:
        verified = len(basis.verified_feature_ids)
        return len(basis.extracted_feature_ids) / verified if verified else 0.0
    if basis.total_features == 0:
        return 0.0
    projected = len(basis.verified_feature_ids) + sum(target.feasibility for target in basis.targets)
    return min(1.0, projected / basis.total_features)


def debt_cost(file_count: int, params: RevivalScoringParameters = DEFAULT_PARAMETERS) -> float:
    """D = min(1, files / debt_file_budget), files = |files_to_modify U files_to_create|."""
    return min(1.0, _require_count("file_count", file_count) / params.debt_file_budget)


@dataclass(frozen=True)
class RiskAdjustment:
    factor: RiskFactor
    count: int
    per_unit: float
    cap: float
    adjustment: float  # min(cap, per_unit * count)


@dataclass(frozen=True)
class RiskAssessment:
    strategy_type: StrategyType
    base: float
    adjustments: tuple[RiskAdjustment, ...]
    risk: float  # clamp(base + sum(adjustments), 0, 1), unrounded

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_type": self.strategy_type.value,
            "base": self.base,
            "factors": [
                {"factor": a.factor.value, "count": a.count, "per_unit": a.per_unit, "cap": a.cap,
                 "adjustment": a.adjustment}
                for a in self.adjustments
            ],
            "risk": self.risk,
        }


def strategy_risk(
    strategy_type: StrategyType | str,
    evidence: Mapping[RiskFactor | str, int] | None = None,
    params: RevivalScoringParameters = DEFAULT_PARAMETERS,
) -> RiskAssessment:
    """R: the type's base plus every applicable factor's ``min(cap, per_unit * count)``, clamped to [0, 1].

    Every factor that applies to ``strategy_type`` is reported (count 0 when
    absent), so the breakdown always lists them. A positive count for a factor
    that does not apply to the type is a caller error.
    """
    kind = StrategyType(strategy_type)
    counts: dict[RiskFactor, int] = {}
    for key, value in (evidence or {}).items():
        factor = RiskFactor(key)
        counts[factor] = _require_count(f"evidence[{factor.value}]", value)
    adjustments: list[RiskAdjustment] = []
    for factor in RiskFactor:
        rule = params.risk_rules.get(factor)
        count = counts.get(factor, 0)
        if rule is None or kind not in rule.applies_to:
            if count:
                raise ValueError(f"risk factor {factor.value} does not apply to {kind.value}")
            continue
        adjustments.append(
            RiskAdjustment(factor, count, rule.per_unit, rule.cap, round(min(rule.cap, rule.per_unit * count), 12))
        )
    base = params.base_risk[kind]
    risk = min(1.0, max(0.0, base + sum(a.adjustment for a in adjustments)))
    return RiskAssessment(kind, base, tuple(adjustments), risk)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanScore:
    """Score(S_k) of one ``StrategyPlan`` with how V, D and R were derived."""

    strategy_type: StrategyType
    score: StrategyScore
    value_basis: ValueBasis
    files: tuple[str, ...]
    risk: RiskAssessment

    @property
    def feasibility_score(self) -> float:
        return self.score.score

    def model_fields(self) -> dict[str, float]:
        """The ``RevivalStrategy`` score columns."""
        return {
            "feasibility_score": self.score.score,
            "value_score": self.score.value,
            "debt_cost_score": self.score.debt_cost,
            "risk_score": self.score.risk,
        }

    def as_dict(self) -> dict[str, Any]:
        basis = self.value_basis
        p = self.score.parameters
        return {
            "strategy_type": self.strategy_type.value,
            **self.score.as_dict(),
            "value_derivation": {
                "mode": basis.mode.value,
                "total_features": basis.total_features,
                "verified_feature_ids": list(basis.verified_feature_ids),
                "targets": [
                    {"feature_id": t.feature_id, "title": t.title, "feasibility": t.feasibility} for t in basis.targets
                ],
                "extracted_feature_ids": list(basis.extracted_feature_ids),
                "unrounded": value_ratio(basis),
            },
            "debt_derivation": {
                "file_count": len(self.files),
                "file_budget": p.debt_file_budget,
                "files": list(self.files),
                "unrounded": debt_cost(len(self.files), p),
            },
            "risk_derivation": self.risk.as_dict(),
            "parameters": p.as_dict(),
        }


def score_plan(plan: StrategyPlan, params: RevivalScoringParameters = DEFAULT_PARAMETERS) -> PlanScore:
    """Score one planner ``StrategyPlan``: V from its value basis, D from its files, R from its risk evidence."""
    files = tuple(plan.files)
    risk = strategy_risk(plan.strategy_type, plan.risk_evidence, params)
    value = value_ratio(plan.value_basis)
    score = score_revival_strategy(value, debt_cost(len(files), params), risk.risk, params)
    return PlanScore(StrategyType(plan.strategy_type), score, plan.value_basis, files, risk)


def score_plans(
    plans: Sequence[StrategyPlan], params: RevivalScoringParameters = DEFAULT_PARAMETERS
) -> tuple[PlanScore, ...]:
    return tuple(score_plan(plan, params) for plan in plans)
