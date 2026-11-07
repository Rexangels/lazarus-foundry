"""Tests for apps.genome.scoring: C(f) and the epistemic status (fast: no database)."""
from __future__ import annotations

import json
import math
from collections.abc import Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from apps.genome.models import EpistemicStatus, EvidenceSourceType
from apps.genome.scoring import (
    DEFAULT_PARAMETERS,
    MATCH_BRANCH_DIFF,
    MATCH_DIRECT_SYMBOL,
    MATCH_STUB,
    MATCH_TEXT_MENTION,
    ConfidenceBreakdown,
    EvidenceSignal,
    ScoringParameters,
    StatusReason,
    UncitedIntentNodeError,
    classify_epistemic_status,
    confidence_score,
    score_feature,
    score_intent_node,
)

AST = EvidenceSourceType.AST_IMPLEMENTATION
BRANCH = EvidenceSourceType.UNMERGED_BRANCH_COMMIT
SPEC = EvidenceSourceType.ISSUE_SPEC
TODO = EvidenceSourceType.TODO_COMMENT
README = EvidenceSourceType.README_CLAIM

RETRY_BRANCH_DORMANCY_DAYS = 543.0  # feat/webhook-retry-engine vs the repo's latest commit
FAST = settings(max_examples=60, deadline=None)


def head(source_type: EvidenceSourceType, strength: float, **flags: bool) -> EvidenceSignal:
    """Evidence observed at the default branch HEAD (age 0)."""
    return EvidenceSignal(source_type, strength, **flags)


def on_branch(source_type: EvidenceSourceType, strength: float, age: float = RETRY_BRANCH_DORMANCY_DAYS) -> EvidenceSignal:
    """Evidence found only on an unmerged branch tip that is ``age`` days dormant."""
    return EvidenceSignal(source_type, strength, age_days=age, observed_on_default_branch=False)


def sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


# --- Calibration table ---------------------------------------------------------------

LEDGER = [
    head(AST, MATCH_DIRECT_SYMBOL),  # class Ledger
    head(AST, MATCH_DIRECT_SYMBOL),  # Ledger.post_transaction
    head(AST, MATCH_DIRECT_SYMBOL, is_test=True),  # tests/test_ledger.py
    head(AST, MATCH_DIRECT_SYMBOL, is_test=True),
    head(AST, MATCH_DIRECT_SYMBOL, is_test=True),
    head(README, MATCH_TEXT_MENTION),
]
WEBHOOK_RETRIES = [
    head(README, MATCH_TEXT_MENTION),  # README "[x] Automated Exponential Backoff Webhook Retries"
    head(TODO, MATCH_TEXT_MENTION),  # FIXME in paypulse/webhooks.py at HEAD
    on_branch(BRANCH, MATCH_BRANCH_DIFF),  # retry engine in the branch diff
    on_branch(TODO, MATCH_TEXT_MENTION),  # "TODO: complete backoff jitter ..." on the branch
]
PDF_EXPORT = [head(README, MATCH_TEXT_MENTION), head(SPEC, MATCH_TEXT_MENTION)]
MULTI_CURRENCY = [
    head(README, MATCH_TEXT_MENTION),
    head(AST, MATCH_STUB, is_stub=True),
    head(TODO, MATCH_TEXT_MENTION),
    head(TODO, MATCH_TEXT_MENTION),  # the FIXME
]

CALIBRATION: list[tuple[str, list[EvidenceSignal], bool, EpistemicStatus, float]] = [
    ("paypulse_ledger", LEDGER, False, EpistemicStatus.KNOWN, sigmoid(2.1 + 0.21 - 0.5)),
    ("single_ast_symbol", [head(AST, MATCH_DIRECT_SYMBOL)], False, EpistemicStatus.INFERRED, sigmoid(1.2 - 0.5)),
    (
        "unmerged_branch_only",
        [on_branch(BRANCH, MATCH_BRANCH_DIFF), on_branch(TODO, MATCH_TEXT_MENTION)],
        False,
        EpistemicStatus.INFERRED,
        sigmoid((0.9 * 0.8 + 0.45 * 0.6) * math.exp(-0.0015 * 543) - 0.5),
    ),
    ("one_todo", [head(TODO, MATCH_TEXT_MENTION)], False, EpistemicStatus.MISSING_CONTEXT, sigmoid(0.27 - 0.5)),
    (
        "ten_todos",
        [head(TODO, MATCH_TEXT_MENTION)] * 10,
        False,
        EpistemicStatus.MISSING_CONTEXT,
        sigmoid(0.27 * (2 - 0.5**9) - 0.5),
    ),
    ("readme_only", [head(README, MATCH_TEXT_MENTION)], False, EpistemicStatus.MISSING_CONTEXT, sigmoid(0.21 - 0.5)),
    ("zero_evidence", [], False, EpistemicStatus.MISSING_CONTEXT, sigmoid(-0.5)),
    (
        "paypulse_webhook_retries",
        WEBHOOK_RETRIES,
        True,
        EpistemicStatus.CONFLICTED,
        sigmoid(0.21 + 0.27 + 0.5 * 0.27 * math.exp(-0.8145) + 0.72 * math.exp(-0.8145) - 0.5 - 0.4),
    ),
    ("paypulse_pdf_export", PDF_EXPORT, True, EpistemicStatus.CONFLICTED, sigmoid(0.21 + 0.36 - 0.5 - 0.4)),
    (
        "paypulse_multi_currency",
        MULTI_CURRENCY,
        True,
        EpistemicStatus.CONFLICTED,
        sigmoid(0.21 + 0.36 + 0.27 + 0.135 - 0.5 - 0.4),
    ),
]


@pytest.mark.parametrize(
    ("signals", "conflict", "status", "expected_c"),
    [case[1:] for case in CALIBRATION],
    ids=[case[0] for case in CALIBRATION],
)
def test_calibration_table(
    signals: list[EvidenceSignal], conflict: bool, status: EpistemicStatus, expected_c: float
) -> None:
    result = score_feature(signals, conflict=conflict)

    assert result.status is status
    assert result.confidence == round(expected_c, 4)


def test_calibration_hits_the_threshold_bands() -> None:
    scores = {name: score_feature(signals, conflict=conflict) for name, signals, conflict, _, _ in CALIBRATION}

    assert scores["paypulse_ledger"].confidence >= 0.75
    assert 0.45 <= scores["single_ast_symbol"].confidence < 0.75
    assert 0.45 <= scores["unmerged_branch_only"].confidence < 0.75
    for name in ("one_todo", "readme_only", "zero_evidence"):
        assert scores[name].confidence < 0.45
    # Ten TODOs add up past 0.45 but carry no implementation, so the gate holds them back.
    assert scores["ten_todos"].confidence >= 0.45
    assert scores["ten_todos"].status_reason is StatusReason.NO_IMPLEMENTATION_EVIDENCE


def test_every_status_is_reachable() -> None:
    reached = {score_feature(signals, conflict=conflict).status for _, signals, conflict, _, _ in CALIBRATION}
    assert reached == set(EpistemicStatus)


@pytest.mark.parametrize("signals", [WEBHOOK_RETRIES, PDF_EXPORT, MULTI_CURRENCY, LEDGER])
def test_conflict_is_computed_and_lowers_c(signals: list[EvidenceSignal]) -> None:
    with_conflict = score_feature(signals, conflict=True)
    without = score_feature(signals, conflict=False)

    assert with_conflict.status is EpistemicStatus.CONFLICTED
    assert with_conflict.confidence < without.confidence
    assert with_conflict.breakdown.z == pytest.approx(without.breakdown.z - 0.4)


# --- Formula unit checks ---------------------------------------------------------------


def test_zero_evidence_returns_the_prior() -> None:
    breakdown = confidence_score([], conflict=False)

    assert breakdown.z == -0.5
    assert breakdown.confidence == 0.3775  # 1 / (1 + e^0.5) = 0.37754
    assert breakdown.contributions == ()


def test_single_term_matches_hand_computation() -> None:
    breakdown = confidence_score([head(AST, MATCH_DIRECT_SYMBOL)], conflict=False)

    [ast] = breakdown.contributions
    assert ast.contribution == 1.2 and ast.cap == pytest.approx(2.1)
    assert breakdown.z == pytest.approx(0.7)
    assert breakdown.confidence == 0.6682  # 1 / (1 + e^-0.7) = 0.66819


def test_temporal_decay_uses_branch_dormancy() -> None:
    breakdown = confidence_score([on_branch(BRANCH, MATCH_BRANCH_DIFF)], conflict=False)

    [term] = breakdown.contributions[0].terms
    assert term.decay == pytest.approx(math.exp(-0.0015 * 543))  # 0.44286
    assert term.term == pytest.approx(0.9 * 0.8 * 0.442861, abs=1e-6)


def test_same_type_items_have_geometric_diminishing_returns_and_a_cap() -> None:
    three = confidence_score([head(AST, MATCH_DIRECT_SYMBOL)] * 3, conflict=False).contributions[0]
    four = confidence_score([head(AST, MATCH_DIRECT_SYMBOL)] * 4, conflict=False).contributions[0]

    assert [t.rank_factor for t in three.terms] == [1.0, 0.5, 0.25]
    assert three.uncapped == pytest.approx(2.1) and not three.capped
    assert four.uncapped == pytest.approx(2.25) and four.capped
    assert four.contribution == pytest.approx(2.1)


def test_strongest_item_of_a_type_counts_fully() -> None:
    signals = [head(TODO, 0.2), head(TODO, 1.0), head(TODO, 0.6)]
    [todo] = confidence_score(signals, conflict=False).contributions

    assert [(t.signal_index, t.rank) for t in todo.terms] == [(1, 1), (2, 2), (0, 3)]
    assert todo.contribution == pytest.approx(0.45 * (1.0 + 0.5 * 0.6 + 0.25 * 0.2))


def test_breakdown_is_json_serializable_and_explains_the_score() -> None:
    result = score_feature(WEBHOOK_RETRIES, conflict=True)
    payload = result.as_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["epistemic_status"] == "CONFLICTED" and payload["status_reason"] == "CONFLICT"
    assert payload["prior_bias"] == 0.5 and payload["conflict_penalty"] == 0.4
    assert [c["source_type"] for c in payload["contributions"]] == [
        "UNMERGED_BRANCH_COMMIT",
        "TODO_COMMENT",
        "README_CLAIM",
    ]
    assert payload["parameters"]["authority_weights"]["AST_IMPLEMENTATION"] == 1.2


# --- Status gates ----------------------------------------------------------------------


def test_known_needs_real_code_on_the_default_branch() -> None:
    branch_heavy = [on_branch(BRANCH, MATCH_BRANCH_DIFF, age=0.0)] * 4 + [on_branch(AST, MATCH_DIRECT_SYMBOL, age=0.0)] * 3
    tests_only = [head(AST, MATCH_DIRECT_SYMBOL, is_test=True)] * 3 + [head(README, MATCH_TEXT_MENTION)]

    for signals in (branch_heavy, tests_only):
        result = score_feature(signals, conflict=False)
        assert result.confidence >= 0.75
        assert result.status is EpistemicStatus.INFERRED
        assert result.status_reason is StatusReason.NO_DEFAULT_BRANCH_IMPLEMENTATION


def test_a_detailed_spec_supports_inferred_but_a_mention_does_not() -> None:
    detailed = score_feature([head(SPEC, MATCH_DIRECT_SYMBOL)], conflict=False)
    mention = score_feature([head(SPEC, MATCH_TEXT_MENTION)], conflict=False)

    assert detailed.confidence == 0.525 and detailed.status is EpistemicStatus.INFERRED  # sigmoid(0.1)
    assert mention.confidence == 0.4651 and mention.status is EpistemicStatus.MISSING_CONTEXT  # sigmoid(-0.14)
    assert mention.status_reason is StatusReason.NO_IMPLEMENTATION_EVIDENCE


def test_a_stub_is_implementation_bearing_but_not_known() -> None:
    result = score_feature([head(AST, MATCH_STUB, is_stub=True)] * 3 + [head(AST, MATCH_DIRECT_SYMBOL, is_test=True)], conflict=False)
    assert result.status is EpistemicStatus.INFERRED


def test_classifier_rejects_a_mismatched_breakdown() -> None:
    breakdown = confidence_score(LEDGER, conflict=False)

    assert classify_epistemic_status(breakdown, LEDGER, conflict=False) is EpistemicStatus.KNOWN
    with pytest.raises(ValueError, match="conflict"):
        classify_epistemic_status(breakdown, LEDGER, conflict=True)
    with pytest.raises(ValueError, match="signals"):
        classify_epistemic_status(breakdown, LEDGER[:1], conflict=False)


def test_persisted_nodes_need_evidence() -> None:
    with pytest.raises(UncitedIntentNodeError):
        score_intent_node([], conflict=False)
    assert score_intent_node(LEDGER, conflict=False).status is EpistemicStatus.KNOWN


# --- Validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"source_type": AST, "match_strength": 1.5}, ValueError),
        ({"source_type": AST, "match_strength": -0.1}, ValueError),
        ({"source_type": AST, "match_strength": float("nan")}, ValueError),
        ({"source_type": AST, "match_strength": True}, TypeError),
        ({"source_type": "NOT_A_TYPE", "match_strength": 1.0}, ValueError),
        ({"source_type": AST, "match_strength": 1.0, "age_days": 3.0}, ValueError),  # HEAD evidence is age 0
        ({"source_type": AST, "match_strength": 1.0, "age_days": -1.0, "observed_on_default_branch": False}, ValueError),
        ({"source_type": BRANCH, "match_strength": 0.8}, ValueError),  # branch commits are never on HEAD
        ({"source_type": TODO, "match_strength": 0.6, "is_stub": True}, ValueError),
        ({"source_type": README, "match_strength": 0.6, "is_test": True}, ValueError),
        ({"source_type": AST, "match_strength": 0.3, "is_stub": True, "is_test": True}, ValueError),
        ({"source_type": AST, "match_strength": 1.0, "is_stub": True}, ValueError),  # stubs cap at MATCH_STUB
        ({"source_type": AST, "match_strength": 1.0, "observed_on_default_branch": 1}, TypeError),
    ],
)
def test_signal_validation(kwargs: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        EvidenceSignal(**kwargs)  # type: ignore[arg-type]


def test_signal_coerces_source_type_strings() -> None:
    assert EvidenceSignal("README_CLAIM", 0.6).source_type is README  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"authority_weights": {AST: 1.2}},
        {"authority_weights": {**DEFAULT_PARAMETERS.authority_weights, TODO: 0.0}},
        {"decay_lambda": -0.001},
        {"conflict_penalty": 0.0},
        {"prior_bias": 0.0},
        {"rank_decay": 1.0},
        {"per_type_cap": 0.9},
        {"known_threshold": 0.4},
        {"inferred_threshold": 0.0},
        {"detailed_spec_min_match": 1.2},
        {"score_decimals": 0},
        {"decay_lambda": float("inf")},
    ],
)
def test_parameter_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        ScoringParameters(**kwargs)  # type: ignore[arg-type]


def test_custom_parameters_are_honored() -> None:
    params = ScoringParameters(prior_bias=1.0)
    assert confidence_score([], conflict=False, params=params).confidence == 0.2689  # sigmoid(-1)


# --- Property-based tests --------------------------------------------------------------


@st.composite
def signals_st(draw: st.DrawFn, types: Sequence[EvidenceSourceType] = tuple(EvidenceSourceType)) -> EvidenceSignal:
    source_type = draw(st.sampled_from(types))
    on_default = source_type is not BRANCH and draw(st.booleans())
    age = 0.0 if on_default else draw(st.floats(0.0, 3650.0, allow_nan=False))
    flag = draw(st.sampled_from(["none", "stub", "test"])) if source_type is AST else "none"
    high = MATCH_STUB if flag == "stub" else 1.0
    return EvidenceSignal(
        source_type,
        draw(st.floats(0.0, high, allow_nan=False)),
        age_days=age,
        observed_on_default_branch=on_default,
        is_stub=flag == "stub",
        is_test=flag == "test",
    )


SIGNAL_LISTS = st.lists(signals_st(), max_size=25)


@FAST
@given(SIGNAL_LISTS, st.booleans())
def test_property_c_is_strictly_inside_unit_interval(signals: list[EvidenceSignal], conflict: bool) -> None:
    """**Validates: Requirements 2.2**"""
    assert 0.0 < confidence_score(signals, conflict=conflict).confidence < 1.0


@FAST
@given(SIGNAL_LISTS, st.booleans())
def test_property_scoring_is_deterministic(signals: list[EvidenceSignal], conflict: bool) -> None:
    """**Validates: Requirements 2.2**"""
    first = score_feature(signals, conflict=conflict).as_dict()
    second = score_feature(list(signals), conflict=conflict).as_dict()
    assert first == second
    json.dumps(first)


def _summary(breakdown: ConfidenceBreakdown) -> tuple[object, ...]:
    return (
        breakdown.confidence,
        breakdown.z,
        tuple((c.source_type, c.signal_count, c.contribution) for c in breakdown.contributions),
    )


@FAST
@given(SIGNAL_LISTS.flatmap(lambda xs: st.tuples(st.just(xs), st.permutations(xs))), st.booleans())
def test_property_signal_order_does_not_matter(
    pair: tuple[list[EvidenceSignal], list[EvidenceSignal]], conflict: bool
) -> None:
    """**Validates: Requirements 2.2**"""
    original, shuffled = pair
    a = score_feature(original, conflict=conflict)
    b = score_feature(shuffled, conflict=conflict)
    assert _summary(a.breakdown) == _summary(b.breakdown)
    assert a.status is b.status


@FAST
@given(SIGNAL_LISTS, signals_st(), st.booleans())
def test_property_adding_evidence_never_lowers_c(
    signals: list[EvidenceSignal], extra: EvidenceSignal, conflict: bool
) -> None:
    """**Validates: Requirements 2.2**"""
    before = confidence_score(signals, conflict=conflict)
    after = confidence_score([*signals, extra], conflict=conflict)
    assert after.z >= before.z
    assert after.confidence >= before.confidence


@FAST
@given(SIGNAL_LISTS)
def test_property_conflict_lowers_c_and_is_conflicted(signals: list[EvidenceSignal]) -> None:
    """**Validates: Requirements 2.2, 2.4**"""
    with_conflict = score_feature(signals, conflict=True)
    without = score_feature(signals, conflict=False)
    assert with_conflict.confidence < without.confidence
    assert with_conflict.status is EpistemicStatus.CONFLICTED


@FAST
@given(
    SIGNAL_LISTS,
    st.sampled_from(tuple(EvidenceSourceType)),
    st.floats(0.0, 1.0, allow_nan=False),
    st.floats(0.0, 2000.0, allow_nan=False),
    st.floats(0.0, 2000.0, allow_nan=False),
    st.booleans(),
)
def test_property_older_evidence_never_raises_c(
    signals: list[EvidenceSignal],
    source_type: EvidenceSourceType,
    strength: float,
    age: float,
    extra_age: float,
    conflict: bool,
) -> None:
    """**Validates: Requirements 2.2**"""
    younger = on_branch(source_type, strength, age=age)
    older = on_branch(source_type, strength, age=age + extra_age)
    assert (
        confidence_score([*signals, older], conflict=conflict).confidence
        <= confidence_score([*signals, younger], conflict=conflict).confidence
    )


@FAST
@given(st.sampled_from(tuple(EvidenceSourceType)), st.integers(1, 60))
def test_property_per_type_cap_bounds_any_count(source_type: EvidenceSourceType, count: int) -> None:
    """**Validates: Requirements 2.2**"""
    signal = on_branch(source_type, 1.0, age=0.0) if source_type is BRANCH else head(source_type, 1.0)
    breakdown = confidence_score([signal] * count, conflict=False)
    cap = DEFAULT_PARAMETERS.per_type_cap * DEFAULT_PARAMETERS.weight(source_type)

    [contribution] = breakdown.contributions
    assert contribution.contribution <= cap
    assert breakdown.confidence <= round(sigmoid(cap - DEFAULT_PARAMETERS.prior_bias), 4)


@FAST
@given(st.lists(signals_st(types=(TODO, README)), min_size=1, max_size=40))
def test_property_todo_and_readme_only_stay_missing_context(signals: list[EvidenceSignal]) -> None:
    """**Validates: Requirements 2.2, 2.3**"""
    assert score_feature(signals, conflict=False).status is EpistemicStatus.MISSING_CONTEXT
