"""Deterministic confidence score C(f) and epistemic status for genome features.

Pure module: no ORM queries, no network, no model calls. Task 4.2 matches
evidence to a feature and passes each match in as an ``EvidenceSignal``; this
module never guesses relevance. The formula and every constant are specified
in ``.kiro/steering/forensic-invariants.md`` sections 2 and 3:

    term_i  = w(type_i) * s_i * exp(-lambda * dt_i)
    A_t     = min(cap * w(t), sum_k rho**(k-1) * term_(k))   (terms of type t, largest first)
    z       = sum_t A_t - beta0 - gamma * Conflict(f)
    C(f)    = round(sigmoid(z), 4)

Status: CONFLICTED if Conflict(f); KNOWN if C >= 0.75 and a non-stub, non-test
AST implementation was observed on the default branch; INFERRED if C >= 0.45
and there is implementation-bearing evidence (any AST item, an unmerged-branch
commit, or a detailed spec); otherwise MISSING_CONTEXT.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from apps.genome.models import EpistemicStatus, EvidenceSourceType

# Match strengths s(f, e) in [0, 1]. The caller (Task 4.2) picks one per match.
MATCH_DIRECT_SYMBOL: float = 1.0  # the evidence is the feature's own symbol / AST node
MATCH_BRANCH_DIFF: float = 0.8  # the feature's code appears in an unmerged branch diff
MATCH_TEXT_MENTION: float = 0.6  # prose or a comment names the feature
# A symbol whose body only raises NotImplementedError: it fixes the name and the
# interface but proves no behavior, so it is worth half of a text mention.
MATCH_STUB: float = 0.3

# Authority weights w_i from forensic-invariants.md section 2 (unchanged).
DEFAULT_AUTHORITY_WEIGHTS: Mapping[EvidenceSourceType, float] = MappingProxyType(
    {
        EvidenceSourceType.AST_IMPLEMENTATION: 1.2,
        EvidenceSourceType.UNMERGED_BRANCH_COMMIT: 0.9,
        EvidenceSourceType.ISSUE_SPEC: 0.6,
        EvidenceSourceType.TODO_COMMENT: 0.45,
        EvidenceSourceType.README_CLAIM: 0.35,
    }
)

# Fixed order for per-type output, so breakdowns are identical for any signal order.
_TYPE_ORDER: tuple[EvidenceSourceType, ...] = tuple(EvidenceSourceType)


def _require_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a real number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return number


def _require_range(name: str, value: object, low: float, high: float) -> float:
    number = _require_number(name, value)
    if not low <= number <= high:
        raise ValueError(f"{name} must lie in [{low}, {high}], got {number!r}")
    return number


@dataclass(frozen=True)
class EvidenceSignal:
    """One piece of evidence already matched to one feature.

    ``age_days`` is the age, relative to the repository's latest commit, of the
    revision at which the evidence was observed. Evidence observed at the
    default branch's HEAD is current, so its age is 0 however old the file is;
    evidence found only on an unmerged branch carries that branch tip's
    dormancy.
    """

    source_type: EvidenceSourceType
    match_strength: float
    age_days: float = 0.0
    observed_on_default_branch: bool = True
    is_stub: bool = False
    is_test: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_type", EvidenceSourceType(self.source_type))
        object.__setattr__(
            self, "match_strength", _require_range("match_strength", self.match_strength, 0.0, 1.0)
        )
        age = _require_number("age_days", self.age_days)
        if age < 0.0:
            raise ValueError(f"age_days must be >= 0, got {age!r}")
        object.__setattr__(self, "age_days", age)
        for flag in ("observed_on_default_branch", "is_stub", "is_test"):
            if not isinstance(getattr(self, flag), bool):
                raise TypeError(f"{flag} must be a bool")

        if self.observed_on_default_branch and age != 0.0:
            raise ValueError("evidence observed at the default branch HEAD is current: age_days must be 0")
        if self.source_type is EvidenceSourceType.UNMERGED_BRANCH_COMMIT and self.observed_on_default_branch:
            raise ValueError("UNMERGED_BRANCH_COMMIT evidence cannot be observed on the default branch")
        if (self.is_stub or self.is_test) and self.source_type is not EvidenceSourceType.AST_IMPLEMENTATION:
            raise ValueError("is_stub and is_test apply only to AST_IMPLEMENTATION evidence")
        if self.is_stub and self.is_test:
            raise ValueError("a signal cannot be both a stub and a test")
        if self.is_stub and self.match_strength > MATCH_STUB:
            raise ValueError(f"a stub proves no behavior: match_strength must be <= {MATCH_STUB}")

    @property
    def is_default_branch_implementation(self) -> bool:
        """Real (non-stub, non-test) code present at the default branch HEAD."""
        return (
            self.source_type is EvidenceSourceType.AST_IMPLEMENTATION
            and self.observed_on_default_branch
            and not self.is_stub
            and not self.is_test
        )


@dataclass(frozen=True)
class ScoringParameters:
    """Every constant of C(f) and of the status rules. Use ``DEFAULT_PARAMETERS``."""

    authority_weights: Mapping[EvidenceSourceType, float] = field(
        default_factory=lambda: DEFAULT_AUTHORITY_WEIGHTS, hash=False
    )
    decay_lambda: float = 0.0015  # lambda, per day
    conflict_penalty: float = 0.40  # gamma
    prior_bias: float = 0.50  # beta0: an unevidenced feature is doubtful by default
    rank_decay: float = 0.5  # rho: k-th strongest item of a type counts rho**(k-1)
    per_type_cap: float = 1.75  # A_t <= per_type_cap * w(t)
    known_threshold: float = 0.75
    inferred_threshold: float = 0.45
    # An ISSUE_SPEC this strongly matched (it names the feature's concrete
    # symbols or interfaces) is a design, not a claim, and may support INFERRED.
    detailed_spec_min_match: float = MATCH_DIRECT_SYMBOL
    score_decimals: int = 4

    def __post_init__(self) -> None:
        weights: dict[EvidenceSourceType, float] = {}
        for key, value in self.authority_weights.items():
            weights[EvidenceSourceType(key)] = _require_number(f"authority_weights[{key}]", value)
        missing = [t.value for t in _TYPE_ORDER if t not in weights]
        if missing:
            raise ValueError(f"authority_weights is missing {missing}")
        for key, weight in weights.items():
            if weight <= 0.0:
                raise ValueError(f"authority_weights[{key.value}] must be > 0, got {weight!r}")
        object.__setattr__(self, "authority_weights", MappingProxyType(weights))

        if _require_number("decay_lambda", self.decay_lambda) < 0.0:
            raise ValueError("decay_lambda must be >= 0")
        if _require_number("conflict_penalty", self.conflict_penalty) <= 0.0:
            raise ValueError("conflict_penalty must be > 0 so that a conflict always lowers C(f)")
        if _require_number("prior_bias", self.prior_bias) <= 0.0:
            raise ValueError("prior_bias must be > 0 so that zero evidence scores below 0.5")
        rho = _require_number("rank_decay", self.rank_decay)
        if not 0.0 <= rho < 1.0:
            raise ValueError(f"rank_decay must lie in [0, 1), got {rho!r}")
        if _require_number("per_type_cap", self.per_type_cap) < 1.0:
            raise ValueError("per_type_cap must be >= 1 so a single full-strength item is never cut")
        inferred = _require_range("inferred_threshold", self.inferred_threshold, 0.0, 1.0)
        known = _require_range("known_threshold", self.known_threshold, 0.0, 1.0)
        if not 0.0 < inferred < known < 1.0:
            raise ValueError("thresholds must satisfy 0 < inferred_threshold < known_threshold < 1")
        _require_range("detailed_spec_min_match", self.detailed_spec_min_match, 0.0, 1.0)
        if isinstance(self.score_decimals, bool) or not isinstance(self.score_decimals, int):
            raise TypeError("score_decimals must be an int")
        if not 1 <= self.score_decimals <= 12:
            raise ValueError("score_decimals must lie in [1, 12]")

    def weight(self, source_type: EvidenceSourceType) -> float:
        return self.authority_weights[source_type]

    def as_dict(self) -> dict[str, Any]:
        return {
            "authority_weights": {t.value: self.authority_weights[t] for t in _TYPE_ORDER},
            "decay_lambda": self.decay_lambda,
            "conflict_penalty": self.conflict_penalty,
            "prior_bias": self.prior_bias,
            "rank_decay": self.rank_decay,
            "per_type_cap": self.per_type_cap,
            "known_threshold": self.known_threshold,
            "inferred_threshold": self.inferred_threshold,
            "detailed_spec_min_match": self.detailed_spec_min_match,
            "score_decimals": self.score_decimals,
        }


DEFAULT_PARAMETERS: ScoringParameters = ScoringParameters()


@dataclass(frozen=True)
class SignalTerm:
    """How one signal entered its type's sum."""

    signal_index: int  # position in the caller's signal sequence
    rank: int  # 1 = strongest item of its type
    match_strength: float
    age_days: float
    decay: float  # exp(-lambda * age_days)
    base_term: float  # w * s * decay
    rank_factor: float  # rho ** (rank - 1)
    term: float  # base_term * rank_factor

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_index": self.signal_index,
            "rank": self.rank,
            "match_strength": self.match_strength,
            "age_days": self.age_days,
            "decay": self.decay,
            "base_term": self.base_term,
            "rank_factor": self.rank_factor,
            "term": self.term,
        }


@dataclass(frozen=True)
class TypeContribution:
    """A_t: the bounded, diminishing-returns contribution of one source type."""

    source_type: EvidenceSourceType
    authority_weight: float
    signal_count: int
    uncapped: float
    cap: float
    contribution: float
    capped: bool
    terms: tuple[SignalTerm, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type.value,
            "authority_weight": self.authority_weight,
            "signal_count": self.signal_count,
            "uncapped": self.uncapped,
            "cap": self.cap,
            "contribution": self.contribution,
            "capped": self.capped,
            "terms": [term.as_dict() for term in self.terms],
        }


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """C(f) with every intermediate value, so a score can be explained."""

    confidence: float  # rounded C(f), the value to persist
    z: float
    evidence_total: float  # sum_t A_t
    prior_bias: float  # beta0, subtracted from z
    conflict: bool
    conflict_penalty: float  # gamma * Conflict(f), subtracted from z
    contributions: tuple[TypeContribution, ...]  # present types only, in EvidenceSourceType order

    @property
    def signal_count(self) -> int:
        return sum(c.signal_count for c in self.contributions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "z": self.z,
            "evidence_total": self.evidence_total,
            "prior_bias": self.prior_bias,
            "conflict": self.conflict,
            "conflict_penalty": self.conflict_penalty,
            "contributions": [c.as_dict() for c in self.contributions],
        }


class StatusReason(StrEnum):
    """Which rule decided the epistemic status."""

    CONFLICT = "CONFLICT"
    ABOVE_KNOWN_THRESHOLD = "ABOVE_KNOWN_THRESHOLD"
    NO_DEFAULT_BRANCH_IMPLEMENTATION = "NO_DEFAULT_BRANCH_IMPLEMENTATION"  # C >= known, capped to INFERRED
    ABOVE_INFERRED_THRESHOLD = "ABOVE_INFERRED_THRESHOLD"
    NO_IMPLEMENTATION_EVIDENCE = "NO_IMPLEMENTATION_EVIDENCE"  # C >= inferred, capped to MISSING_CONTEXT
    BELOW_INFERRED_THRESHOLD = "BELOW_INFERRED_THRESHOLD"


@dataclass(frozen=True)
class FeatureScore:
    breakdown: ConfidenceBreakdown
    status: EpistemicStatus
    status_reason: StatusReason

    @property
    def confidence(self) -> float:
        return self.breakdown.confidence

    def as_dict(self, params: ScoringParameters = DEFAULT_PARAMETERS) -> dict[str, Any]:
        return {
            **self.breakdown.as_dict(),
            "epistemic_status": self.status.value,
            "status_reason": self.status_reason.value,
            "parameters": params.as_dict(),
        }


class UncitedIntentNodeError(ValueError):
    """An IntentNode was scored without evidence (forensic-invariants.md section 1)."""


def _sigmoid(z: float) -> float:
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _type_contribution(
    source_type: EvidenceSourceType,
    indexed: Sequence[tuple[int, EvidenceSignal]],
    params: ScoringParameters,
) -> TypeContribution:
    weight = params.weight(source_type)
    bases = [
        (index, signal, math.exp(-params.decay_lambda * signal.age_days))
        for index, signal in indexed
    ]
    ranked = sorted(
        ((weight * s.match_strength * decay, index, s, decay) for index, s, decay in bases),
        key=lambda item: (-item[0], item[1]),
    )
    terms: list[SignalTerm] = []
    for rank, (base, index, signal, decay) in enumerate(ranked, start=1):
        factor = params.rank_decay ** (rank - 1)
        terms.append(
            SignalTerm(
                signal_index=index,
                rank=rank,
                match_strength=signal.match_strength,
                age_days=signal.age_days,
                decay=decay,
                base_term=base,
                rank_factor=factor,
                term=base * factor,
            )
        )
    uncapped = math.fsum(term.term for term in terms)
    cap = params.per_type_cap * weight
    return TypeContribution(
        source_type=source_type,
        authority_weight=weight,
        signal_count=len(terms),
        uncapped=uncapped,
        cap=cap,
        contribution=min(uncapped, cap),
        capped=uncapped > cap,
        terms=tuple(terms),
    )


def confidence_score(
    signals: Sequence[EvidenceSignal],
    *,
    conflict: bool,
    params: ScoringParameters = DEFAULT_PARAMETERS,
) -> ConfidenceBreakdown:
    """C(f) for one feature. An empty sequence returns the prior sigmoid(-beta0)."""
    if not isinstance(conflict, bool):
        raise TypeError("conflict must be a bool")
    by_type: dict[EvidenceSourceType, list[tuple[int, EvidenceSignal]]] = {}
    for index, signal in enumerate(signals):
        if not isinstance(signal, EvidenceSignal):
            raise TypeError(f"signals[{index}] must be an EvidenceSignal, got {type(signal).__name__}")
        by_type.setdefault(signal.source_type, []).append((index, signal))

    contributions = tuple(
        _type_contribution(source_type, by_type[source_type], params)
        for source_type in _TYPE_ORDER
        if source_type in by_type
    )
    evidence_total = math.fsum(c.contribution for c in contributions)
    penalty = params.conflict_penalty if conflict else 0.0
    z = math.fsum((evidence_total, -params.prior_bias, -penalty))
    confidence = min(1.0, max(0.0, round(_sigmoid(z), params.score_decimals)))
    return ConfidenceBreakdown(
        confidence=confidence,
        z=z,
        evidence_total=evidence_total,
        prior_bias=params.prior_bias,
        conflict=conflict,
        conflict_penalty=penalty,
        contributions=contributions,
    )


def _has_implementation_bearing_evidence(
    signals: Sequence[EvidenceSignal], params: ScoringParameters
) -> bool:
    for signal in signals:
        if signal.source_type in (
            EvidenceSourceType.AST_IMPLEMENTATION,
            EvidenceSourceType.UNMERGED_BRANCH_COMMIT,
        ):
            return True
        if (
            signal.source_type is EvidenceSourceType.ISSUE_SPEC
            and signal.match_strength >= params.detailed_spec_min_match
        ):
            return True
    return False


def _classify(
    breakdown: ConfidenceBreakdown,
    signals: Sequence[EvidenceSignal],
    *,
    conflict: bool,
    params: ScoringParameters,
) -> tuple[EpistemicStatus, StatusReason]:
    if breakdown.conflict != conflict:
        raise ValueError("conflict does not match the breakdown it was scored with")
    if breakdown.signal_count != len(signals):
        raise ValueError("signals do not match the breakdown they were scored with")
    if conflict:
        return EpistemicStatus.CONFLICTED, StatusReason.CONFLICT
    c = breakdown.confidence
    implementation_bearing = _has_implementation_bearing_evidence(signals, params)
    if c >= params.known_threshold:
        if any(s.is_default_branch_implementation for s in signals):
            return EpistemicStatus.KNOWN, StatusReason.ABOVE_KNOWN_THRESHOLD
        if implementation_bearing:
            return EpistemicStatus.INFERRED, StatusReason.NO_DEFAULT_BRANCH_IMPLEMENTATION
        return EpistemicStatus.MISSING_CONTEXT, StatusReason.NO_IMPLEMENTATION_EVIDENCE
    if c >= params.inferred_threshold:
        if implementation_bearing:
            return EpistemicStatus.INFERRED, StatusReason.ABOVE_INFERRED_THRESHOLD
        return EpistemicStatus.MISSING_CONTEXT, StatusReason.NO_IMPLEMENTATION_EVIDENCE
    return EpistemicStatus.MISSING_CONTEXT, StatusReason.BELOW_INFERRED_THRESHOLD


def classify_epistemic_status(
    breakdown: ConfidenceBreakdown,
    signals: Sequence[EvidenceSignal],
    *,
    conflict: bool,
    params: ScoringParameters = DEFAULT_PARAMETERS,
) -> EpistemicStatus:
    """Epistemic status from a breakdown and the signals it was computed from."""
    status, _ = _classify(breakdown, signals, conflict=conflict, params=params)
    return status


def score_feature(
    signals: Sequence[EvidenceSignal],
    *,
    conflict: bool,
    params: ScoringParameters = DEFAULT_PARAMETERS,
) -> FeatureScore:
    """C(f), its breakdown and the epistemic status. Accepts no evidence (returns the prior)."""
    signals = tuple(signals)
    breakdown = confidence_score(signals, conflict=conflict, params=params)
    status, reason = _classify(breakdown, signals, conflict=conflict, params=params)
    return FeatureScore(breakdown=breakdown, status=status, status_reason=reason)


def score_intent_node(
    signals: Sequence[EvidenceSignal],
    *,
    conflict: bool,
    params: ScoringParameters = DEFAULT_PARAMETERS,
) -> FeatureScore:
    """``score_feature`` for a node about to be persisted: refuses an empty evidence list."""
    signals = tuple(signals)
    if not signals:
        raise UncitedIntentNodeError(
            "an IntentNode needs at least one EvidenceCitation; refusing to score it without evidence"
        )
    return score_feature(signals, conflict=conflict, params=params)
