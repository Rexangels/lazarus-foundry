"""Tests for apps.revival.scoring: Score(S_k) and its V, D, R derivations (forensic-invariants.md section 4)."""
from __future__ import annotations

import json
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from apps.revival.models import StrategyType
from apps.revival.scoring import (
    DEFAULT_PARAMETERS,
    RevivalScoringParameters,
    RiskFactor,
    RiskFactorRule,
    ValueBasis,
    ValueMode,
    ValueTarget,
    debt_cost,
    score_revival_strategy,
    strategy_risk,
    value_ratio,
)

FAST = settings(max_examples=80, deadline=None)
UNIT = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


# ---------------------------------------------------------------------------
# Formula
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("v", "d", "r", "expected"),
    [
        (1.0, 0.0, 0.0, 100.0),
        (0.0, 1.0, 1.0, 0.0),
        (0.5, 0.5, 0.5, 50.0),
        # 100 * (0.5 * 0.681 + 0.3 * (1 - 0.3333) + 0.2 * (1 - 0.30)) = 34.05 + 20.001 + 14.0 = 68.051
        (0.681, 0.3333, 0.30, 68.1),
        # 100 * (0.5 * 2/3 + 0.3 * 0.4 + 0.2 * 0.6) = 33.335 + 12 + 12 (V rounded to 0.6667)
        (2 / 3, 0.6, 0.4, 57.3),
    ],
)
def test_formula_hand_checks(v: float, d: float, r: float, expected: float) -> None:
    assert score_revival_strategy(v, d, r).score == expected


def test_inputs_are_rounded_before_the_formula_and_the_terms_reproduce_the_score() -> None:
    score = score_revival_strategy(2 / 3, 0.53333333, 0.4)
    assert (score.value, score.debt_cost, score.risk) == (0.6667, 0.5333, 0.4)
    assert score.value_term == pytest.approx(0.5 * 0.6667)
    assert score.debt_term == pytest.approx(0.3 * (1 - 0.5333))
    assert score.risk_term == pytest.approx(0.2 * 0.6)
    assert score.score == round(100 * (score.value_term + score.debt_term + score.risk_term), 1)
    body = score.as_dict()
    json.dumps(body)
    assert body["terms"]["value"]["points"] == pytest.approx(33.335)
    assert body["weights"] == {"value": 0.5, "debt": 0.3, "risk": 0.2}


@pytest.mark.parametrize("bad", [-0.01, 1.01, math.nan, math.inf])
def test_inputs_outside_the_unit_interval_are_rejected(bad: float) -> None:
    for args in ((bad, 0.5, 0.5), (0.5, bad, 0.5), (0.5, 0.5, bad)):
        with pytest.raises(ValueError):
            score_revival_strategy(*args)


@pytest.mark.parametrize("bad", [True, "0.5", None])
def test_non_numbers_are_rejected(bad: object) -> None:
    with pytest.raises(TypeError):
        score_revival_strategy(bad, 0.5, 0.5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
def test_default_parameters_match_the_steering_file() -> None:
    p = DEFAULT_PARAMETERS
    assert (p.value_weight, p.debt_weight, p.risk_weight, p.debt_file_budget) == (0.5, 0.3, 0.2, 15.0)
    assert dict(p.base_risk) == {StrategyType.RESTORE: 0.25, StrategyType.MODERNIZE: 0.60, StrategyType.EXTRACT: 0.35}
    json.dumps(p.as_dict())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"value_weight": 0.6},  # weights no longer sum to 1
        {"value_weight": -0.1, "debt_weight": 0.9},
        {"debt_file_budget": 0},
        {"base_risk": {StrategyType.RESTORE: 0.25, StrategyType.MODERNIZE: 0.6}},  # EXTRACT missing
        {"base_risk": {StrategyType.RESTORE: 1.5, StrategyType.MODERNIZE: 0.6, StrategyType.EXTRACT: 0.35}},
        {"score_decimals": 13},
    ],
)
def test_invalid_parameters_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        RevivalScoringParameters(**kwargs)  # type: ignore[arg-type]


def test_invalid_risk_rules_are_rejected() -> None:
    with pytest.raises(ValueError):
        RiskFactorRule(per_unit=-0.1, cap=0.2, applies_to=frozenset({StrategyType.RESTORE}))
    with pytest.raises(ValueError):
        RiskFactorRule(per_unit=0.1, cap=1.2, applies_to=frozenset({StrategyType.RESTORE}))
    with pytest.raises(ValueError):
        RiskFactorRule(per_unit=0.1, cap=0.2, applies_to=frozenset())


# ---------------------------------------------------------------------------
# V, D, R
# ---------------------------------------------------------------------------
def test_value_ratio_weights_targets_by_feasibility() -> None:
    basis = ValueBasis(
        ValueMode.PROJECTED_VERIFIED, 6, ("a", "b", "c"),
        targets=(ValueTarget("d", "D", 0.68), ValueTarget("e", "E", 0.4061)),
    )
    assert value_ratio(basis) == pytest.approx((3 + 0.68 + 0.4061) / 6)
    assert value_ratio(ValueBasis(ValueMode.PROJECTED_VERIFIED, 0, ())) == 0.0


def test_extract_value_is_the_share_of_verified_features() -> None:
    basis = ValueBasis(ValueMode.EXTRACT_SHARE, 6, ("a", "b", "c"), extracted_feature_ids=("a", "b"))
    assert value_ratio(basis) == pytest.approx(2 / 3)
    assert value_ratio(ValueBasis(ValueMode.EXTRACT_SHARE, 2, ())) == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"verified_feature_ids": ("a",), "targets": (ValueTarget("a", "A", 0.5),)},  # verified and a target
        {"verified_feature_ids": ("a",), "extracted_feature_ids": ("z",), "mode": ValueMode.EXTRACT_SHARE},
        {"verified_feature_ids": ("a", "b", "c"), "total_features": 2},
        {"verified_feature_ids": (), "targets": (ValueTarget("x", "X", 0.5), ValueTarget("x", "X", 0.5))},
    ],
)
def test_inconsistent_value_bases_are_rejected(kwargs: dict[str, object]) -> None:
    arguments: dict[str, object] = {"mode": ValueMode.PROJECTED_VERIFIED, "total_features": 4, **kwargs}
    with pytest.raises(ValueError):
        ValueBasis(**arguments)  # type: ignore[arg-type]


def test_debt_cost_saturates_at_the_file_budget() -> None:
    assert debt_cost(0) == 0.0
    assert debt_cost(5) == pytest.approx(1 / 3)
    assert debt_cost(15) == 1.0 and debt_cost(40) == 1.0
    with pytest.raises(ValueError):
        debt_cost(-1)


def test_risk_is_the_base_when_the_evidence_is_neutral() -> None:
    for kind, base in ((StrategyType.RESTORE, 0.25), (StrategyType.MODERNIZE, 0.60), (StrategyType.EXTRACT, 0.35)):
        assessment = strategy_risk(kind)
        assert assessment.risk == base and all(a.adjustment == 0 for a in assessment.adjustments)


def test_risk_factors_add_capped_adjustments() -> None:
    restore = strategy_risk(StrategyType.RESTORE, {RiskFactor.MERGE_DISTANCE: 5})
    assert restore.risk == pytest.approx(0.30)
    assert strategy_risk(StrategyType.RESTORE, {"MERGE_DISTANCE": 500}).risk == pytest.approx(0.40)  # cap 0.15
    modernize = strategy_risk(StrategyType.MODERNIZE, {"MERGE_DISTANCE": 5, "PINNED_UPGRADES": 2})
    assert modernize.risk == pytest.approx(0.70)
    assert [a.factor for a in modernize.adjustments] == [RiskFactor.MERGE_DISTANCE, RiskFactor.PINNED_UPGRADES]
    assert strategy_risk(StrategyType.EXTRACT, {"EXTRACT_BOUNDARY": 1}).risk == pytest.approx(0.40)
    json.dumps(modernize.as_dict())
    with pytest.raises(ValueError):
        strategy_risk(StrategyType.EXTRACT, {"PINNED_UPGRADES": 1})  # does not apply to EXTRACT


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------
@FAST
@given(UNIT, UNIT, UNIT)
def test_score_is_bounded(v: float, d: float, r: float) -> None:
    """**Validates: Requirements 3.2**"""
    assert 0.0 <= score_revival_strategy(v, d, r).score <= 100.0


@FAST
@given(UNIT, UNIT, UNIT, UNIT)
def test_score_is_monotone_in_each_input(a: float, b: float, x: float, y: float) -> None:
    """Non-decreasing in V, non-increasing in D and in R. **Validates: Requirements 3.2**"""
    low, high = min(a, b), max(a, b)
    assert score_revival_strategy(low, x, y).score <= score_revival_strategy(high, x, y).score
    assert score_revival_strategy(x, low, y).score >= score_revival_strategy(x, high, y).score
    assert score_revival_strategy(x, y, low).score >= score_revival_strategy(x, y, high).score


@FAST
@given(
    st.sampled_from(list(StrategyType)),
    st.integers(0, 400), st.integers(0, 50), st.integers(0, 30), st.integers(0, 60),
)
def test_risk_and_debt_stay_in_the_unit_interval(
    kind: StrategyType, behind: int, pins: int, boundary: int, files: int
) -> None:
    """**Validates: Requirements 3.2**"""
    evidence = {
        RiskFactor.MERGE_DISTANCE: behind if kind is not StrategyType.EXTRACT else 0,
        RiskFactor.PINNED_UPGRADES: pins if kind is StrategyType.MODERNIZE else 0,
        RiskFactor.EXTRACT_BOUNDARY: boundary if kind is StrategyType.EXTRACT else 0,
    }
    risk = strategy_risk(kind, evidence)
    assert DEFAULT_PARAMETERS.base_risk[kind] <= risk.risk <= 1.0
    assert 0.0 <= debt_cost(files) <= 1.0
