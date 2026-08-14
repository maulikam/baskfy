"""Momentum Regime Overlay — PURE domain engine.

Checkpoint 1 of the regime spec. This module is deliberately free of I/O: no Kite calls,
no SQLite, no clock reads, no config imports. Everything it needs arrives as arguments,
so live evaluation and historical replay call the SAME functions and, given identical
inputs, produce byte-identical decisions. That property is a live acceptance requirement,
not a nicety — it is what makes a persisted decision reproducible.

Adapters (index cache, breadth, persistence, rebalance integration, webview) are later
checkpoints and must depend on this module, never the reverse.

DESIGN INVARIANTS enforced here:
- The market signal and the portfolio's actual exposure are SEPARATE facts. A committed
  policy tier is never evidence that the cap was achieved; exposure_gap_pct carries that.
- UNKNOWN / stale / low-coverage inputs can never permit R1 and never enable new buys.
- UNKNOWN / stale inputs can never INDEPENDENTLY escalate a tier or cause forced selling.
- Book-composition weights describe the full-risk portfolio the strategy WANTS, never the
  residual already reduced by R2/R3/R4 — otherwise regime sells manufacture a largecap
  tilt that fakes a recovery signal the following week.
- Hysteresis compares each confirming close against THAT DAY's moving average.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable, Mapping, Sequence

ALGORITHM_VERSION = "regime/1.0.0"


# =====================================================================================
# typed values
# =====================================================================================
class RegimeTier(str, Enum):
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    R4 = "R4"

    @property
    def ordinal(self) -> int:
        """1 = most risk-on, 4 = most risk-off. Higher is more defensive."""
        return {"R1": 1, "R2": 2, "R3": 3, "R4": 4}[self.value]

    @classmethod
    def from_ordinal(cls, n: int) -> "RegimeTier":
        return {1: cls.R1, 2: cls.R2, 3: cls.R3, 4: cls.R4}[max(1, min(4, int(n)))]


class SignalState(str, Enum):
    ABOVE = "above"
    BELOW = "below"
    UNKNOWN = "unknown"


class NewBuyMode(str, Enum):
    FULL = "full"
    HALF = "half"
    BLOCKED = "blocked"


class ForcedAction(str, Enum):
    NONE = "none"
    TRIM_TO_CAP = "trim_to_cap"
    REDUCE_TO_CAP = "reduce_to_cap"
    CRASH_REDUCE = "crash_reduce"
    MANUAL_REVIEW = "manual_review"


class ExecutionStatus(str, Enum):
    NOT_PLANNED = "not_planned"
    PLANNED = "planned"
    PARTIAL = "partial"
    COMPLETED = "completed"
    FAILED = "failed"


class WeightSource(str, Enum):
    FULL_RISK_TARGET = "full_risk_target"
    LAST_R1_SNAPSHOT = "last_r1_snapshot"
    CONFIG_DEFAULT = "config_default"


class BootstrapPolicy(str, Enum):
    """What to do on the very first evaluation, when no prior policy tier exists."""
    OBSERVE_ONLY = "observe_only"        # adopt the tier for display; NEVER forced-sell
    ADOPT_CANDIDATE = "adopt_candidate"  # adopt and allow reconciliation immediately


class RegimeMode(str, Enum):
    OBSERVE = "observe"
    PROPOSE = "propose"
    ENFORCE = "enforce"


# =====================================================================================
# stable reason codes — the audit contract. Never rename; add new ones instead.
# =====================================================================================
class Reason:
    # data quality
    DATA_STALE = "DATA_STALE"
    DATA_INSUFFICIENT_WARMUP = "DATA_INSUFFICIENT_WARMUP"
    DATA_INDEX_MISSING = "DATA_INDEX_MISSING"
    SIGNAL_UNKNOWN = "SIGNAL_UNKNOWN"
    BREADTH_MISSING = "BREADTH_MISSING"
    BREADTH_STALE = "BREADTH_STALE"
    BREADTH_LOW_COVERAGE = "BREADTH_LOW_COVERAGE"
    BREADTH_NOT_USED = "BREADTH_NOT_USED"
    # classifier
    R4_BEARISH_STACK_AND_WEAK_BREADTH = "R4_BEARISH_STACK_AND_WEAK_BREADTH"
    R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH = "R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH"
    R3_H200_WEAK = "R3_H200_WEAK"
    R3_BREADTH_WEAK = "R3_BREADTH_WEAK"
    R3_SENTINEL_BELOW_200DMA = "R3_SENTINEL_BELOW_200DMA"
    R1_ALL_CONDITIONS_MET = "R1_ALL_CONDITIONS_MET"
    R2_DEFAULT = "R2_DEFAULT"
    # sentinel veto
    SENTINEL_BELOW_50DMA_VETO = "SENTINEL_BELOW_50DMA_VETO"
    SENTINEL_FLOOR_R2 = "SENTINEL_FLOOR_R2"
    SENTINEL_FLOOR_R3 = "SENTINEL_FLOOR_R3"
    SENTINEL_UNKNOWN = "SENTINEL_UNKNOWN"
    # recovery override
    RECOVERY_OVERRIDE_APPLIED = "RECOVERY_OVERRIDE_APPLIED"
    RECOVERY_OVERRIDE_UNCONFIRMED = "RECOVERY_OVERRIDE_UNCONFIRMED"
    RECOVERY_OVERRIDE_H20_TOO_WEAK = "RECOVERY_OVERRIDE_H20_TOO_WEAK"
    RECOVERY_OVERRIDE_BREADTH_UNUSABLE = "RECOVERY_OVERRIDE_BREADTH_UNUSABLE"
    # transitions
    TRANSITION_RISK_OFF_DIRECT = "TRANSITION_RISK_OFF_DIRECT"
    TRANSITION_RERISK_CAPPED = "TRANSITION_RERISK_CAPPED"
    TRANSITION_NONE = "TRANSITION_NONE"
    TRANSITION_HELD_STALE_DATA = "TRANSITION_HELD_STALE_DATA"
    BOOTSTRAP_NO_PRIOR_STATE = "BOOTSTRAP_NO_PRIOR_STATE"
    BOOTSTRAP_OBSERVE_ONLY = "BOOTSTRAP_OBSERVE_ONLY"
    # book weights
    BOOK_WEIGHTS_FULL_RISK_TARGET = "BOOK_WEIGHTS_FULL_RISK_TARGET"
    BOOK_WEIGHTS_LAST_R1_SNAPSHOT = "BOOK_WEIGHTS_LAST_R1_SNAPSHOT"
    BOOK_WEIGHTS_CONFIG_DEFAULT = "BOOK_WEIGHTS_CONFIG_DEFAULT"
    BOOK_CLASSIFICATION_COVERAGE_LOW = "BOOK_CLASSIFICATION_COVERAGE_LOW"
    # buys / exposure
    NEW_BUYS_FULL = "NEW_BUYS_FULL"
    NEW_BUYS_HALF = "NEW_BUYS_HALF"
    NEW_BUYS_BLOCKED_TIER = "NEW_BUYS_BLOCKED_TIER"
    NEW_BUYS_BLOCKED_SENTINEL = "NEW_BUYS_BLOCKED_SENTINEL"
    NEW_BUYS_BLOCKED_DATA = "NEW_BUYS_BLOCKED_DATA"
    EXPOSURE_WITHIN_CAP = "EXPOSURE_WITHIN_CAP"
    EXPOSURE_GAP_PENDING = "EXPOSURE_GAP_PENDING"
    EXPOSURE_RECONCILE_RETRY = "EXPOSURE_RECONCILE_RETRY"
    FORCED_SELL_SUPPRESSED_BOOTSTRAP = "FORCED_SELL_SUPPRESSED_BOOTSTRAP"


# Rules that represent an AFFIRMATIVE risk-off signal. R2_DEFAULT is deliberately absent:
# it records "R1 was not confirmed", which is not evidence of danger and must never be
# allowed to escalate a tier (and thus force selling) on the back of degraded inputs.
AFFIRMATIVE_RISK_OFF: frozenset[str] = frozenset({
    "R4_BEARISH_STACK_AND_WEAK_BREADTH", "R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH",
    "R3_H200_WEAK", "R3_BREADTH_WEAK", "R3_SENTINEL_BELOW_200DMA",
    "SENTINEL_FLOOR_R2", "SENTINEL_FLOOR_R3",
})


DISPLAY_REASONS: Mapping[str, str] = {
    Reason.DATA_STALE: "Index data is stale — holding the previous tier and blocking new buys.",
    Reason.DATA_INSUFFICIENT_WARMUP: "Not enough history to establish every moving average.",
    Reason.DATA_INDEX_MISSING: "A required structural index has no usable data.",
    Reason.SIGNAL_UNKNOWN: "One or more index/MA pairs have not yet confirmed a side.",
    Reason.BREADTH_MISSING: "No breadth reading supplied.",
    Reason.BREADTH_STALE: "Breadth reading is older than the staleness allowance.",
    Reason.BREADTH_LOW_COVERAGE: "Breadth coverage is below the configured minimum.",
    Reason.BREADTH_NOT_USED:
        "This configuration does not use breadth; breadth rules are inactive.",
    Reason.R4_BEARISH_STACK_AND_WEAK_BREADTH:
        "Bearish MA stack across most of the book with very weak breadth.",
    Reason.R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH:
        "Momentum sentinel below its 200-DMA with weak long-term health and breadth.",
    Reason.R3_H200_WEAK: "Long-term health (H200) at or below the R3 threshold.",
    Reason.R3_BREADTH_WEAK: "Breadth below the R3 threshold.",
    Reason.R3_SENTINEL_BELOW_200DMA: "Momentum sentinel confirmed below its 200-DMA.",
    Reason.R1_ALL_CONDITIONS_MET: "Health, breadth and the momentum sentinel all risk-on.",
    Reason.R2_DEFAULT: "No risk-off rule fired and R1 conditions were not all met.",
    Reason.SENTINEL_BELOW_50DMA_VETO:
        "Momentum sentinel below its 50-DMA — new entries vetoed.",
    Reason.SENTINEL_FLOOR_R2: "Sentinel 50-DMA veto floors the candidate tier at R2.",
    Reason.SENTINEL_FLOOR_R3: "Sentinel 200-DMA break floors the candidate tier at R3.",
    Reason.SENTINEL_UNKNOWN: "Momentum sentinel state is not yet confirmed.",
    Reason.RECOVERY_OVERRIDE_APPLIED: "Breadth recovery override lifted the tier to R2.",
    Reason.RECOVERY_OVERRIDE_UNCONFIRMED:
        "Recovery conditions met but not yet confirmed for long enough.",
    Reason.RECOVERY_OVERRIDE_H20_TOO_WEAK: "Short-term health below the recovery threshold.",
    Reason.RECOVERY_OVERRIDE_BREADTH_UNUSABLE:
        "Recovery override needs current, well-covered breadth.",
    Reason.TRANSITION_RISK_OFF_DIRECT: "Risk reduction applied directly to the candidate tier.",
    Reason.TRANSITION_RERISK_CAPPED: "Re-risking limited to one tier per weekly evaluation.",
    Reason.TRANSITION_NONE: "No tier change this evaluation.",
    Reason.TRANSITION_HELD_STALE_DATA:
        "Previous tier retained: inputs are degraded and no confirmed risk-off rule fired.",
    Reason.BOOTSTRAP_NO_PRIOR_STATE: "First evaluation — no prior policy tier exists.",
    Reason.BOOTSTRAP_OBSERVE_ONLY:
        "Bootstrap policy is observe-only: no forced selling from absent history.",
    Reason.BOOK_WEIGHTS_FULL_RISK_TARGET:
        "Book weights from the proposed full-risk target portfolio.",
    Reason.BOOK_WEIGHTS_LAST_R1_SNAPSHOT: "Book weights from the last committed R1 snapshot.",
    Reason.BOOK_WEIGHTS_CONFIG_DEFAULT: "Book weights fell back to configured defaults.",
    Reason.BOOK_CLASSIFICATION_COVERAGE_LOW:
        "Market-cap classification coverage below the configured minimum.",
    Reason.NEW_BUYS_FULL: "Full-sized new entries permitted.",
    Reason.NEW_BUYS_HALF: "New entries permitted at half size.",
    Reason.NEW_BUYS_BLOCKED_TIER: "New entries blocked by the policy tier.",
    Reason.NEW_BUYS_BLOCKED_SENTINEL: "New entries blocked by the momentum sentinel veto.",
    Reason.NEW_BUYS_BLOCKED_DATA: "New entries blocked by unusable or stale data.",
    Reason.EXPOSURE_WITHIN_CAP: "Actual equity exposure is within the policy cap.",
    Reason.EXPOSURE_GAP_PENDING: "Actual equity exposure exceeds the policy cap.",
    Reason.EXPOSURE_RECONCILE_RETRY:
        "Previous plan did not complete — reconciling without a new tier transition.",
    Reason.FORCED_SELL_SUPPRESSED_BOOTSTRAP:
        "Forced selling suppressed: no prior state to justify it.",
}


def describe(codes: Iterable[str]) -> list[str]:
    """Machine-readable codes -> human display text, preserving order."""
    return [DISPLAY_REASONS.get(c, c) for c in codes]


# =====================================================================================
# configuration (pure value object; app/config.py wiring is checkpoint 2)
# =====================================================================================
@dataclass(frozen=True)
class RegimeConfig:
    # --- indices -------------------------------------------------------------------
    # category -> exact index tradingsymbol. Categories key the book weights.
    structural_indices: Mapping[str, str] = field(default_factory=lambda: {
        "largecap": "NIFTY 50",
        "midcap": "NIFTY MIDCAP 150",
        "smallcap": "NIFTY SMLCAP 250",
    })
    index_aliases: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: {
        "NIFTY SMLCAP 250": ("NIFTY SMALLCAP 250",),
        "NIFTY 500 MOMENTUM 50": ("NIFTY500MOMENTM50",),
    })
    momentum_sentinel: str = "NIFTY 500 MOMENTUM 50"
    exchange: str = "NSE"
    segment: str = "INDICES"

    # --- moving averages / hysteresis ----------------------------------------------
    ma_lengths: tuple[int, ...] = (20, 50, 200)
    buffer_bps: int = 150          # stored in bps so 1.5 vs 0.015 can never be confused
    confirm_days: int = 3

    # --- book composition ----------------------------------------------------------
    default_book_weights: Mapping[str, float] = field(default_factory=lambda: {
        "smallcap": 0.50, "midcap": 0.30, "largecap": 0.20,
    })
    min_classification_coverage_pct: float = 80.0

    # --- thresholds (strategy contract — operators are exact) -----------------------
    r4_bearish_stack_min: float = 0.60     # >=
    r4_breadth_max: float = 30.0           # <
    r4_h200_max: float = 0.30              # <
    r3_h200_max: float = 0.50              # <=
    r3_breadth_max: float = 40.0           # <
    r1_h50_min: float = 0.80               # >=
    r1_h200_min: float = 0.80              # >=
    r1_breadth_min: float = 55.0           # >=
    recovery_breadth_min: float = 55.0     # >=
    recovery_h20_min: float = 0.50         # >=
    recovery_confirm_evaluations: int = 2

    # --- data quality ---------------------------------------------------------------
    min_breadth_coverage_pct: float = 90.0
    # When False, breadth is simply NOT PART OF THE MODEL: its rules never fire, and its
    # absence is not treated as degraded data. That distinction matters — "we don't use
    # breadth" is a different model from "breadth is broken", and conflating them makes a
    # breadth-less deployment freeze forever under the degraded-input lock.
    breadth_required: bool = True
    breadth_stale_days: int = 10
    index_stale_days: int = 5

    # --- exposure --------------------------------------------------------------------
    tier_exposure_pct: Mapping[str, float] = field(default_factory=lambda: {
        "R1": 100.0, "R2": 70.0, "R3": 40.0, "R4": 10.0,
    })
    exposure_tolerance_pct: float = 1.0

    # --- scheduling -------------------------------------------------------------------
    weekly_evaluation_weekday: int = 4     # Monday=0 ... Friday=4

    # --- policy ------------------------------------------------------------------------
    bootstrap_policy: BootstrapPolicy = BootstrapPolicy.OBSERVE_ONLY
    mode: RegimeMode = RegimeMode.OBSERVE
    algorithm_version: str = ALGORITHM_VERSION

    # ---------------------------------------------------------------------------------
    @property
    def buffer(self) -> float:
        """Buffer as a fraction (150 bps -> 0.015)."""
        return self.buffer_bps / 10_000.0

    def cap_for(self, tier: RegimeTier) -> float:
        return float(self.tier_exposure_pct[tier.value])

    def canonical(self) -> dict:
        """Deterministic, JSON-safe view used for the config hash."""
        return {
            "structural_indices": dict(sorted(self.structural_indices.items())),
            "index_aliases": {k: list(v) for k, v in sorted(self.index_aliases.items())},
            "momentum_sentinel": self.momentum_sentinel,
            "exchange": self.exchange, "segment": self.segment,
            "ma_lengths": list(self.ma_lengths),
            "buffer_bps": self.buffer_bps, "confirm_days": self.confirm_days,
            "default_book_weights": dict(sorted(self.default_book_weights.items())),
            "min_classification_coverage_pct": self.min_classification_coverage_pct,
            "r4_bearish_stack_min": self.r4_bearish_stack_min,
            "r4_breadth_max": self.r4_breadth_max, "r4_h200_max": self.r4_h200_max,
            "r3_h200_max": self.r3_h200_max, "r3_breadth_max": self.r3_breadth_max,
            "r1_h50_min": self.r1_h50_min, "r1_h200_min": self.r1_h200_min,
            "r1_breadth_min": self.r1_breadth_min,
            "recovery_breadth_min": self.recovery_breadth_min,
            "recovery_h20_min": self.recovery_h20_min,
            "recovery_confirm_evaluations": self.recovery_confirm_evaluations,
            "min_breadth_coverage_pct": self.min_breadth_coverage_pct,
            "breadth_required": self.breadth_required,
            "breadth_stale_days": self.breadth_stale_days,
            "index_stale_days": self.index_stale_days,
            "tier_exposure_pct": dict(sorted(self.tier_exposure_pct.items())),
            "exposure_tolerance_pct": self.exposure_tolerance_pct,
            "weekly_evaluation_weekday": self.weekly_evaluation_weekday,
            "bootstrap_policy": self.bootstrap_policy.value,
            "algorithm_version": self.algorithm_version,
            # NOTE: `mode` is deliberately excluded — observe/propose/enforce changes what
            # we DO with a decision, not what the decision IS. Including it would make the
            # same market state hash differently per rollout stage and break replay.
        }

    def config_hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    # ---------------------------------------------------------------------------------
    def validate(self) -> None:
        """Startup validation. Raises ConfigError listing every problem found."""
        errs: list[str] = []

        if list(self.ma_lengths) != sorted(set(self.ma_lengths)):
            errs.append(f"ma_lengths must be strictly increasing and unique: {self.ma_lengths}")
        if len(self.ma_lengths) < 1:
            errs.append("ma_lengths must not be empty")

        caps = [self.tier_exposure_pct.get(t.value) for t in
                (RegimeTier.R1, RegimeTier.R2, RegimeTier.R3, RegimeTier.R4)]
        if any(c is None for c in caps):
            errs.append("tier_exposure_pct must define R1, R2, R3 and R4")
        else:
            if any(caps[i] < caps[i + 1] for i in range(3)):
                errs.append(f"tier exposure caps must be non-increasing R1->R4: {caps}")
            if any(not (0.0 <= c <= 100.0) for c in caps):
                errs.append(f"tier exposure caps must be within 0-100: {caps}")

        for name, val in (("r4_breadth_max", self.r4_breadth_max),
                          ("r3_breadth_max", self.r3_breadth_max),
                          ("r1_breadth_min", self.r1_breadth_min),
                          ("recovery_breadth_min", self.recovery_breadth_min),
                          ("min_breadth_coverage_pct", self.min_breadth_coverage_pct),
                          ("min_classification_coverage_pct",
                           self.min_classification_coverage_pct)):
            if not (0.0 <= val <= 100.0):
                errs.append(f"{name} must be a percentage within 0-100, got {val}")

        for name, val in (("r4_bearish_stack_min", self.r4_bearish_stack_min),
                          ("r4_h200_max", self.r4_h200_max),
                          ("r3_h200_max", self.r3_h200_max),
                          ("r1_h50_min", self.r1_h50_min),
                          ("r1_h200_min", self.r1_h200_min),
                          ("recovery_h20_min", self.recovery_h20_min)):
            if not (0.0 <= val <= 1.0):
                errs.append(f"{name} is a weighted-health fraction within 0-1, got {val}")

        w = dict(self.default_book_weights)
        if any(v < 0 for v in w.values()):
            errs.append(f"default_book_weights must be non-negative: {w}")
        if sum(w.values()) <= 0:
            errs.append("default_book_weights must be normalizable (sum > 0)")
        missing = set(self.structural_indices) - set(w)
        if missing:
            errs.append(f"default_book_weights missing categories: {sorted(missing)}")

        if self.confirm_days < 1:
            errs.append(f"confirm_days must be at least 1, got {self.confirm_days}")
        if not (0 <= self.buffer_bps <= 1000):
            errs.append(f"buffer_bps must be within 0-1000 (0-10%), got {self.buffer_bps}")
        if self.recovery_confirm_evaluations < 1:
            errs.append("recovery_confirm_evaluations must be at least 1")
        if not (0 <= self.weekly_evaluation_weekday <= 6):
            errs.append("weekly_evaluation_weekday must be 0-6 (Mon-Sun)")

        if not self.structural_indices:
            errs.append("structural_indices must not be empty")
        if any(not str(v).strip() for v in self.structural_indices.values()):
            errs.append("structural index identifiers must be non-empty")
        if not str(self.momentum_sentinel).strip():
            errs.append("momentum_sentinel must be non-empty")

        if errs:
            raise ConfigError("invalid regime configuration:\n  - " + "\n  - ".join(errs))


class ConfigError(ValueError):
    pass


class AllocationError(ValueError):
    """Raised when position/weight/cap constraints are jointly infeasible."""


# =====================================================================================
# market data value objects
# =====================================================================================
@dataclass(frozen=True)
class Candle:
    date: dt.date
    open: float | None
    high: float | None
    low: float | None
    close: float
    is_final: bool = True


@dataclass(frozen=True)
class MaSignal:
    ma_length: int
    close: float
    ma: float | None
    distance_pct: float | None
    state: SignalState
    confirming_closes: int

    def as_dict(self) -> dict:
        return {"ma_length": self.ma_length, "close": self.close, "ma": self.ma,
                "distance_pct": self.distance_pct, "state": self.state.value,
                "confirming_closes": self.confirming_closes}


@dataclass(frozen=True)
class IndexSignals:
    index_name: str
    as_of_date: dt.date | None
    close: float | None
    signals: Mapping[int, MaSignal]
    is_stale: bool = False
    has_data: bool = True
    instrument_token: int | None = None

    def state(self, ma_length: int) -> SignalState:
        sig = self.signals.get(ma_length)
        return sig.state if sig else SignalState.UNKNOWN

    def is_above(self, ma_length: int) -> bool:
        return self.state(ma_length) is SignalState.ABOVE

    def is_below(self, ma_length: int) -> bool:
        return self.state(ma_length) is SignalState.BELOW

    @property
    def any_unknown(self) -> bool:
        return (not self.has_data) or any(
            s.state is SignalState.UNKNOWN for s in self.signals.values())

    def bearish_stack(self, lengths: Sequence[int] = (20, 50, 200)) -> bool:
        """MA20 < MA50 < MA200 AND close confirmed below MA20."""
        try:
            mas = [self.signals[n].ma for n in lengths]
        except KeyError:
            return False
        if any(m is None for m in mas):
            return False
        ordered = all(mas[i] < mas[i + 1] for i in range(len(mas) - 1))
        return bool(ordered and self.is_below(lengths[0]))

    def as_dict(self) -> dict:
        return {
            "index_name": self.index_name,
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
            "close": self.close, "is_stale": self.is_stale, "has_data": self.has_data,
            "instrument_token": self.instrument_token,
            "signals": {str(k): v.as_dict() for k, v in sorted(self.signals.items())},
            "bearish_stack": self.bearish_stack(),
        }


@dataclass(frozen=True)
class BreadthReading:
    as_of_date: dt.date
    pct_above_20dma: float
    eligible_count: int
    observed_count: int
    coverage_pct: float
    universe_id: str
    universe_hash: str
    audit_run_id: str
    calculation_version: str

    def is_usable(self, cfg: RegimeConfig, as_of: dt.date) -> tuple[bool, list[str]]:
        codes: list[str] = []
        if self.coverage_pct < cfg.min_breadth_coverage_pct:
            codes.append(Reason.BREADTH_LOW_COVERAGE)
        if (as_of - self.as_of_date).days > cfg.breadth_stale_days:
            codes.append(Reason.BREADTH_STALE)
        return (not codes), codes

    def as_dict(self) -> dict:
        return {"as_of_date": self.as_of_date.isoformat(),
                "pct_above_20dma": self.pct_above_20dma,
                "eligible_count": self.eligible_count,
                "observed_count": self.observed_count,
                "coverage_pct": self.coverage_pct,
                "universe_id": self.universe_id, "universe_hash": self.universe_hash,
                "audit_run_id": self.audit_run_id,
                "calculation_version": self.calculation_version}


@dataclass(frozen=True)
class BookWeights:
    weights: Mapping[str, float]
    source: WeightSource
    coverage_pct: float
    reason_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"weights": dict(sorted(self.weights.items())), "source": self.source.value,
                "coverage_pct": self.coverage_pct, "reason_codes": list(self.reason_codes)}


@dataclass(frozen=True)
class TargetPosition:
    """A line of the hypothetical FULL-RISK target book (pre-regime restriction)."""
    symbol: str
    category: str | None          # largecap / midcap / smallcap, or None if unclassified
    market_value: float


@dataclass(frozen=True)
class ExposureSnapshot:
    actual_equity_pct: float
    pending_buy_pct: float = 0.0
    pending_sell_pct: float = 0.0
    execution_status: ExecutionStatus = ExecutionStatus.NOT_PLANNED


@dataclass(frozen=True)
class PreviousRegime:
    policy_tier: RegimeTier
    scheduled_week_end: dt.date
    last_transition_date: dt.date | None = None
    recovery_confirmations: int = 0
    book_weights: BookWeights | None = None      # frozen snapshot for the week


# =====================================================================================
# the state this engine produces
# =====================================================================================
@dataclass(frozen=True)
class RegimeState:
    raw_candidate_tier: RegimeTier
    previous_policy_tier: RegimeTier | None
    policy_tier: RegimeTier
    transition_limited: bool

    target_equity_cap_pct: float
    actual_equity_pct: float
    exposure_gap_pct: float
    pending_sell_pct: float
    pending_buy_pct: float
    execution_status: ExecutionStatus

    new_buys: NewBuyMode
    forced_action: ForcedAction

    reason_codes: list[str]
    reasons: list[str]

    as_of_date: dt.date
    signal_session_date: dt.date
    scheduled_week_end: dt.date
    evaluation_id: str

    breadth_pct: float | None
    breadth_coverage_pct: float | None
    override_active: bool

    book_category_weights: Mapping[str, float]
    book_weight_source: WeightSource
    index_diagnostics: Mapping[str, dict]

    last_transition_date: dt.date | None
    next_evaluation_date: dt.date
    data_stale: bool
    manual_action_required: bool

    algorithm_version: str
    config_hash: str

    health: Mapping[int, float] = field(default_factory=dict)
    bearish_stack_weight: float = 0.0

    def as_dict(self) -> dict:
        return {
            "raw_candidate_tier": self.raw_candidate_tier.value,
            "previous_policy_tier": self.previous_policy_tier.value
            if self.previous_policy_tier else None,
            "policy_tier": self.policy_tier.value,
            "transition_limited": self.transition_limited,
            "target_equity_cap_pct": self.target_equity_cap_pct,
            "actual_equity_pct": self.actual_equity_pct,
            "exposure_gap_pct": self.exposure_gap_pct,
            "pending_sell_pct": self.pending_sell_pct,
            "pending_buy_pct": self.pending_buy_pct,
            "execution_status": self.execution_status.value,
            "new_buys": self.new_buys.value,
            "forced_action": self.forced_action.value,
            "reason_codes": list(self.reason_codes),
            "reasons": list(self.reasons),
            "as_of_date": self.as_of_date.isoformat(),
            "signal_session_date": self.signal_session_date.isoformat(),
            "scheduled_week_end": self.scheduled_week_end.isoformat(),
            "evaluation_id": self.evaluation_id,
            "breadth_pct": self.breadth_pct,
            "breadth_coverage_pct": self.breadth_coverage_pct,
            "override_active": self.override_active,
            "book_category_weights": dict(sorted(self.book_category_weights.items())),
            "book_weight_source": self.book_weight_source.value,
            "index_diagnostics": dict(self.index_diagnostics),
            "last_transition_date": self.last_transition_date.isoformat()
            if self.last_transition_date else None,
            "next_evaluation_date": self.next_evaluation_date.isoformat(),
            "data_stale": self.data_stale,
            "manual_action_required": self.manual_action_required,
            "algorithm_version": self.algorithm_version,
            "config_hash": self.config_hash,
            "health": {str(k): v for k, v in sorted(self.health.items())},
            "bearish_stack_weight": self.bearish_stack_weight,
        }


# =====================================================================================
# moving averages + Schmitt-trigger hysteresis  (the heart of the engine)
# =====================================================================================
def moving_average(closes: Sequence[float], length: int) -> list[float | None]:
    """Simple MA aligned to `closes`; None until `length` observations exist."""
    out: list[float | None] = []
    running = 0.0
    for i, c in enumerate(closes):
        running += c
        if i >= length:
            running -= closes[i - length]
        out.append(running / length if i >= length - 1 else None)
    return out


def confirmed_state_series(closes: Sequence[float], mas: Sequence[float | None], *,
                           buffer: float, confirm_days: int
                           ) -> list[tuple[SignalState, int]]:
    """Per-day (confirmed state, consecutive confirming closes) with hysteresis.

    Each of the last `confirm_days` closes is compared against THAT DAY'S OWN moving
    average — never all against the latest MA value, which would silently change the
    signal whenever the MA moved. Inside the buffer band, the previous state is retained;
    before any confirmation has occurred the state is UNKNOWN.
    """
    n = len(closes)
    above_flag = [False] * n
    below_flag = [False] * n
    for i in range(n):
        ma = mas[i] if i < len(mas) else None
        if ma is None:
            continue
        above_flag[i] = closes[i] > ma * (1.0 + buffer)
        below_flag[i] = closes[i] < ma * (1.0 - buffer)

    out: list[tuple[SignalState, int]] = []
    state = SignalState.UNKNOWN
    for i in range(n):
        if i + 1 >= confirm_days:
            window = range(i - confirm_days + 1, i + 1)
            if all(above_flag[k] for k in window):
                state = SignalState.ABOVE
            elif all(below_flag[k] for k in window):
                state = SignalState.BELOW
            # else: inside the buffer or mixed -> retain the previous state
        # trailing run on whichever side today sits
        run = 0
        if above_flag[i]:
            k = i
            while k >= 0 and above_flag[k]:
                run += 1
                k -= 1
        elif below_flag[i]:
            k = i
            while k >= 0 and below_flag[k]:
                run += 1
                k -= 1
        out.append((state, run))
    return out


def build_index_signals(index_name: str, candles: Sequence[Candle], cfg: RegimeConfig, *,
                        as_of: dt.date | None = None,
                        instrument_token: int | None = None) -> IndexSignals:
    """Pure candles -> confirmed signals. Unfinished candles are dropped, never consumed."""
    finals = [c for c in sorted(candles, key=lambda c: c.date) if c.is_final]
    if not finals:
        return IndexSignals(index_name=index_name, as_of_date=None, close=None,
                            signals={}, is_stale=True, has_data=False,
                            instrument_token=instrument_token)

    closes = [c.close for c in finals]
    last = finals[-1]
    signals: dict[int, MaSignal] = {}
    for length in cfg.ma_lengths:
        mas = moving_average(closes, length)
        states = confirmed_state_series(closes, mas, buffer=cfg.buffer,
                                        confirm_days=cfg.confirm_days)
        ma_last = mas[-1]
        state, run = states[-1]
        signals[length] = MaSignal(
            ma_length=length, close=last.close, ma=ma_last,
            distance_pct=None if ma_last in (None, 0) else (last.close / ma_last - 1.0) * 100.0,
            state=state, confirming_closes=run)

    ref = as_of or last.date
    stale = (ref - last.date).days > cfg.index_stale_days
    return IndexSignals(index_name=index_name, as_of_date=last.date, close=last.close,
                        signals=signals, is_stale=stale, has_data=True,
                        instrument_token=instrument_token)


# =====================================================================================
# book-composition weights
# =====================================================================================
def _normalise(weights: Mapping[str, float]) -> dict[str, float]:
    total = sum(v for v in weights.values() if v > 0)
    if total <= 0:
        raise ConfigError(f"book weights are not normalizable: {dict(weights)}")
    return {k: (v / total if v > 0 else 0.0) for k, v in weights.items()}


def resolve_book_weights(cfg: RegimeConfig, *,
                         full_risk_target: Sequence[TargetPosition] | None = None,
                         last_r1_snapshot: BookWeights | None = None) -> BookWeights:
    """Resolve the strategic book composition, in the spec's precedence order.

    CRITICAL: `full_risk_target` must be the portfolio the strategy WANTS at full risk,
    produced by scoring BEFORE any regime restriction. Passing regime-reduced holdings
    here would tilt next week's weights toward largecaps and manufacture a fake recovery.
    """
    codes: list[str] = []

    if full_risk_target:
        total = sum(max(p.market_value, 0.0) for p in full_risk_target)
        if total > 0:
            known = {c: 0.0 for c in cfg.structural_indices}
            classified = 0.0
            for p in full_risk_target:
                v = max(p.market_value, 0.0)
                if p.category in known:
                    known[p.category] += v
                    classified += v
            coverage = classified / total * 100.0
            if coverage >= cfg.min_classification_coverage_pct and classified > 0:
                return BookWeights(_normalise(known), WeightSource.FULL_RISK_TARGET,
                                   round(coverage, 2),
                                   (Reason.BOOK_WEIGHTS_FULL_RISK_TARGET,))
            codes.append(Reason.BOOK_CLASSIFICATION_COVERAGE_LOW)

    if last_r1_snapshot is not None and last_r1_snapshot.weights:
        return BookWeights(_normalise(last_r1_snapshot.weights),
                           WeightSource.LAST_R1_SNAPSHOT,
                           last_r1_snapshot.coverage_pct,
                           tuple(codes) + (Reason.BOOK_WEIGHTS_LAST_R1_SNAPSHOT,))

    return BookWeights(_normalise(cfg.default_book_weights), WeightSource.CONFIG_DEFAULT,
                       0.0, tuple(codes) + (Reason.BOOK_WEIGHTS_CONFIG_DEFAULT,))


# =====================================================================================
# weighted health
# =====================================================================================
def weighted_health(book: BookWeights, index_signals: Mapping[str, IndexSignals],
                    cfg: RegimeConfig, ma_length: int) -> float:
    """H_m = sum(category_weight x confirmed_above(category_index, MA_m)).

    UNKNOWN contributes zero — it is never treated as bullish. Note this is computed per
    MA length on each index's OWN series; there is deliberately no synthetic weighted
    price index to take averages of.
    """
    total = 0.0
    for category, index_name in cfg.structural_indices.items():
        w = float(book.weights.get(category, 0.0))
        sig = index_signals.get(index_name)
        if sig is not None and sig.is_above(ma_length):
            total += w
    return round(total, 10)


def optimistic_health(book: BookWeights, index_signals: Mapping[str, IndexSignals],
                      cfg: RegimeConfig, ma_length: int) -> float:
    """Health computed with every DEGRADED signal charitably treated as ABOVE.

    Used only to decide whether a risk-off escalation is genuine. If even the most
    favourable reading of missing/stale/UNKNOWN data still trips a risk-off rule, the
    danger is real; otherwise the escalation would be an artefact of bad data, and
    acting on it would mean selling because a feed broke.
    """
    total = 0.0
    for category, index_name in cfg.structural_indices.items():
        w = float(book.weights.get(category, 0.0))
        sig = index_signals.get(index_name)
        if sig is None or not sig.has_data or sig.is_stale:
            total += w
        elif sig.state(ma_length) in (SignalState.ABOVE, SignalState.UNKNOWN):
            total += w
    return round(total, 10)


def optimistic_sentinel(sentinel: IndexSignals | None) -> IndexSignals | None:
    """A degraded sentinel imposes no floor; a confirmed, fresh one still does."""
    if sentinel is None or not sentinel.has_data or sentinel.is_stale:
        return None
    return sentinel


def bearish_stack_weight(book: BookWeights, index_signals: Mapping[str, IndexSignals],
                         cfg: RegimeConfig) -> float:
    total = 0.0
    for category, index_name in cfg.structural_indices.items():
        sig = index_signals.get(index_name)
        if sig is not None and sig.bearish_stack(tuple(cfg.ma_lengths)):
            total += float(book.weights.get(category, 0.0))
    return round(total, 10)


# =====================================================================================
# candidate tier classifier
# =====================================================================================
def classify_candidate(*, health: Mapping[int, float], stack_weight: float,
                       breadth_pct: float | None, breadth_usable: bool,
                       sentinel: IndexSignals | None, cfg: RegimeConfig
                       ) -> tuple[RegimeTier, list[str]]:
    """Mutually exclusive rules, evaluated R4 -> R3 -> R1 -> else R2.

    Breadth conditions can only FIRE when breadth is usable. An unusable reading therefore
    cannot trigger a risk-off rule (no forced selling from bad data) and cannot satisfy
    R1's breadth requirement (no risk-on from bad data) — the asymmetry the spec demands.
    """
    codes: list[str] = []
    h20 = health.get(20, 0.0)
    h50 = health.get(50, 0.0)
    h200 = health.get(200, 0.0)
    b = breadth_pct if breadth_usable else None

    sentinel_below_200 = bool(sentinel and sentinel.is_below(200))
    sentinel_below_50 = bool(sentinel and sentinel.is_below(50))
    sentinel_above_50 = bool(sentinel and sentinel.is_above(50))

    # --- R4 ---------------------------------------------------------------------------
    if b is not None and stack_weight >= cfg.r4_bearish_stack_min and b < cfg.r4_breadth_max:
        codes.append(Reason.R4_BEARISH_STACK_AND_WEAK_BREADTH)
        return RegimeTier.R4, codes
    if (b is not None and sentinel_below_200 and h200 < cfg.r4_h200_max
            and b < cfg.r4_breadth_max):
        codes.append(Reason.R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH)
        return RegimeTier.R4, codes

    # --- R3 ---------------------------------------------------------------------------
    r3 = False
    if h200 <= cfg.r3_h200_max:
        codes.append(Reason.R3_H200_WEAK)
        r3 = True
    if b is not None and b < cfg.r3_breadth_max:
        codes.append(Reason.R3_BREADTH_WEAK)
        r3 = True
    if sentinel_below_200:
        codes.append(Reason.R3_SENTINEL_BELOW_200DMA)
        r3 = True
    if r3:
        return RegimeTier.R3, codes

    # --- R1 ---------------------------------------------------------------------------
    # When breadth is not part of the model it imposes NO constraint in either direction:
    # it cannot force risk-off above, and it must not block R1 here either. When breadth
    # IS required but unusable, `b is None` and R1 stays correctly unreachable.
    breadth_ok_for_r1 = (b is not None and b >= cfg.r1_breadth_min
                         ) if cfg.breadth_required else True
    if (h50 >= cfg.r1_h50_min and h200 >= cfg.r1_h200_min
            and breadth_ok_for_r1 and sentinel_above_50):
        codes.append(Reason.R1_ALL_CONDITIONS_MET)
        return RegimeTier.R1, codes

    # --- R2 ---------------------------------------------------------------------------
    codes.append(Reason.R2_DEFAULT)
    return RegimeTier.R2, codes


def apply_sentinel_floor(tier: RegimeTier, sentinel: IndexSignals | None,
                         ) -> tuple[RegimeTier, list[str]]:
    """The momentum sentinel can only make the tier MORE defensive, never less."""
    codes: list[str] = []
    if sentinel is None or not sentinel.has_data:
        codes.append(Reason.SENTINEL_UNKNOWN)
        return tier, codes
    out = tier
    if sentinel.is_below(50) and out.ordinal < RegimeTier.R2.ordinal:
        out = RegimeTier.R2
        codes.append(Reason.SENTINEL_FLOOR_R2)
    if sentinel.is_below(200) and out.ordinal < RegimeTier.R3.ordinal:
        out = RegimeTier.R3
        codes.append(Reason.SENTINEL_FLOOR_R3)
    if sentinel.is_below(50):
        codes.append(Reason.SENTINEL_BELOW_50DMA_VETO)
    return out, codes


def apply_recovery_override(tier: RegimeTier, *, health: Mapping[int, float],
                            breadth_pct: float | None, breadth_usable: bool,
                            confirmations: int, cfg: RegimeConfig
                            ) -> tuple[RegimeTier, bool, list[str]]:
    """Lift an R3/R4 candidate to no better than R2. Never to R1, never past the caps."""
    codes: list[str] = []
    if tier.ordinal < RegimeTier.R3.ordinal:
        return tier, False, codes
    if not breadth_usable or breadth_pct is None:
        codes.append(Reason.RECOVERY_OVERRIDE_BREADTH_UNUSABLE)
        return tier, False, codes
    if breadth_pct < cfg.recovery_breadth_min:
        return tier, False, codes
    if health.get(20, 0.0) < cfg.recovery_h20_min:
        codes.append(Reason.RECOVERY_OVERRIDE_H20_TOO_WEAK)
        return tier, False, codes
    if confirmations < cfg.recovery_confirm_evaluations:
        codes.append(Reason.RECOVERY_OVERRIDE_UNCONFIRMED)
        return tier, False, codes
    codes.append(Reason.RECOVERY_OVERRIDE_APPLIED)
    return RegimeTier.R2, True, codes


# =====================================================================================
# transitions
# =====================================================================================
def apply_transition(previous: RegimeTier | None, candidate: RegimeTier, cfg: RegimeConfig
                     ) -> tuple[RegimeTier, bool, list[str]]:
    """Asymmetric: risk-off is immediate, re-risking is one ordinal tier per week."""
    if previous is None:
        return candidate, False, [Reason.BOOTSTRAP_NO_PRIOR_STATE]
    if candidate.ordinal > previous.ordinal:
        return candidate, False, [Reason.TRANSITION_RISK_OFF_DIRECT]
    if candidate.ordinal < previous.ordinal:
        stepped = RegimeTier.from_ordinal(previous.ordinal - 1)
        limited = stepped is not candidate
        return stepped, limited, [Reason.TRANSITION_RERISK_CAPPED] if limited else []
    return previous, False, [Reason.TRANSITION_NONE]


def next_evaluation_date(after: dt.date, cfg: RegimeConfig) -> dt.date:
    """Next calendar date strictly after `after` falling on the evaluation weekday."""
    delta = (cfg.weekly_evaluation_weekday - after.weekday()) % 7
    return after + dt.timedelta(days=delta or 7)


def make_evaluation_id(scheduled_week_end: dt.date, signal_session_date: dt.date,
                       cfg: RegimeConfig) -> str:
    """Deterministic id — the same week + session + config always yields the same id.

    This is what makes a same-week re-run idempotent: the caller cannot accidentally mint
    a second evaluation for a week that has already been decided.
    """
    blob = "|".join([scheduled_week_end.isoformat(), signal_session_date.isoformat(),
                     cfg.algorithm_version, cfg.config_hash()])
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


# =====================================================================================
# new buys + forced action
# =====================================================================================
def resolve_new_buys(tier: RegimeTier, *, sentinel: IndexSignals | None,
                     data_ok: bool) -> tuple[NewBuyMode, list[str]]:
    if not data_ok:
        return NewBuyMode.BLOCKED, [Reason.NEW_BUYS_BLOCKED_DATA]
    if sentinel is not None and sentinel.is_below(50):
        return NewBuyMode.BLOCKED, [Reason.NEW_BUYS_BLOCKED_SENTINEL]
    if tier is RegimeTier.R1:
        return NewBuyMode.FULL, [Reason.NEW_BUYS_FULL]
    if tier is RegimeTier.R2:
        return NewBuyMode.HALF, [Reason.NEW_BUYS_HALF]
    return NewBuyMode.BLOCKED, [Reason.NEW_BUYS_BLOCKED_TIER]


def resolve_forced_action(tier: RegimeTier, gap_pct: float, cfg: RegimeConfig, *,
                          bootstrap_suppressed: bool) -> tuple[ForcedAction, list[str]]:
    """Driven by the EXPOSURE GAP, not by the transition date.

    This is why a failed or partial plan can be retried: the gap persists across weeks and
    is re-derived every invocation, so reconciliation never needs a fresh tier transition.
    """
    if gap_pct <= cfg.exposure_tolerance_pct:
        return ForcedAction.NONE, [Reason.EXPOSURE_WITHIN_CAP]
    if bootstrap_suppressed:
        return ForcedAction.NONE, [Reason.FORCED_SELL_SUPPRESSED_BOOTSTRAP]
    codes = [Reason.EXPOSURE_GAP_PENDING]
    if tier is RegimeTier.R4:
        return ForcedAction.CRASH_REDUCE, codes
    if tier is RegimeTier.R3:
        return ForcedAction.REDUCE_TO_CAP, codes
    if tier is RegimeTier.R2:
        return ForcedAction.TRIM_TO_CAP, codes
    return ForcedAction.NONE, codes


# =====================================================================================
# the single entry point
# =====================================================================================
def evaluate(*, as_of_date: dt.date, scheduled_week_end: dt.date,
             signal_session_date: dt.date,
             index_signals: Mapping[str, IndexSignals],
             sentinel: IndexSignals | None,
             breadth: BreadthReading | None,
             book: BookWeights,
             exposure: ExposureSnapshot,
             cfg: RegimeConfig,
             previous: PreviousRegime | None = None) -> RegimeState:
    """Pure regime evaluation. Identical inputs always yield an identical RegimeState."""
    codes: list[str] = []

    # --- data quality ------------------------------------------------------------------
    required = list(cfg.structural_indices.values())
    missing = [n for n in required
               if n not in index_signals or not index_signals[n].has_data]
    stale = [n for n in required if n in index_signals and index_signals[n].is_stale]
    unknown = [n for n in required
               if n in index_signals and index_signals[n].any_unknown]

    if missing:
        codes.append(Reason.DATA_INDEX_MISSING)
    if stale or (sentinel is not None and sentinel.is_stale):
        codes.append(Reason.DATA_STALE)
    if unknown or (sentinel is not None and sentinel.any_unknown):
        codes.append(Reason.SIGNAL_UNKNOWN)
    data_stale = bool(missing or stale)
    data_ok = not (missing or stale or unknown)

    breadth_usable = False
    breadth_pct = breadth.pct_above_20dma if breadth else None
    breadth_cov = breadth.coverage_pct if breadth else None
    if not cfg.breadth_required:
        codes.append(Reason.BREADTH_NOT_USED)
    elif breadth is None:
        codes.append(Reason.BREADTH_MISSING)
    else:
        breadth_usable, bcodes = breadth.is_usable(cfg, as_of_date)
        codes.extend(bcodes)
    # Only a REQUIRED-but-unusable reading degrades the inputs.
    if cfg.breadth_required and not breadth_usable:
        data_ok = False

    codes.extend(book.reason_codes)

    # --- health ---------------------------------------------------------------------
    health = {m: weighted_health(book, index_signals, cfg, m) for m in cfg.ma_lengths}
    stack_w = bearish_stack_weight(book, index_signals, cfg)

    # --- candidate tier ---------------------------------------------------------------
    candidate, ccodes = classify_candidate(
        health=health, stack_weight=stack_w, breadth_pct=breadth_pct,
        breadth_usable=breadth_usable, sentinel=sentinel, cfg=cfg)
    codes.extend(ccodes)

    candidate, scodes = apply_sentinel_floor(candidate, sentinel)
    codes.extend(scodes)

    confirmations = previous.recovery_confirmations if previous else 0
    candidate, override_active, rcodes = apply_recovery_override(
        candidate, health=health, breadth_pct=breadth_pct,
        breadth_usable=breadth_usable, confirmations=confirmations, cfg=cfg)
    codes.extend(rcodes)

    raw_candidate = candidate

    # An UNKNOWN structural signal must never be read as bullish enough for R1.
    if unknown and raw_candidate is RegimeTier.R1:
        raw_candidate = RegimeTier.R2

    # --- policy tier -------------------------------------------------------------------
    prev_tier = previous.policy_tier if previous else None
    degraded = bool(missing or stale or unknown
                    or (cfg.breadth_required and not breadth_usable))

    if degraded and prev_tier is not None:
        # Degraded inputs may never re-risk, and may only de-risk when the escalation
        # survives the optimistic test above. Reconciliation of the EXISTING cap still
        # runs every invocation, so an already-committed gap keeps being worked off.
        health_opt = {m: optimistic_health(book, index_signals, cfg, m)
                      for m in cfg.ma_lengths}
        sent_opt = optimistic_sentinel(sentinel)
        cand_opt, codes_opt = classify_candidate(
            health=health_opt, stack_weight=stack_w, breadth_pct=breadth_pct,
            breadth_usable=breadth_usable, sentinel=sent_opt, cfg=cfg)
        cand_opt, floor_opt = apply_sentinel_floor(cand_opt, sent_opt)
        codes_opt = list(codes_opt) + list(floor_opt)

        justified = (bool(set(codes_opt) & AFFIRMATIVE_RISK_OFF)
                     and cand_opt.ordinal > prev_tier.ordinal)
        if justified:
            policy_tier, transition_limited = cand_opt, False
            codes.append(Reason.TRANSITION_RISK_OFF_DIRECT)
        else:
            policy_tier, transition_limited = prev_tier, False
            codes.append(Reason.TRANSITION_HELD_STALE_DATA)
    else:
        policy_tier, transition_limited, tcodes = apply_transition(prev_tier, raw_candidate, cfg)
        codes.extend(tcodes)

    bootstrap = prev_tier is None
    bootstrap_suppressed = bootstrap and cfg.bootstrap_policy is BootstrapPolicy.OBSERVE_ONLY
    if bootstrap_suppressed:
        codes.append(Reason.BOOTSTRAP_OBSERVE_ONLY)

    # --- exposure ----------------------------------------------------------------------
    cap = cfg.cap_for(policy_tier)
    gap = round(exposure.actual_equity_pct - cap, 6)
    if exposure.execution_status in (ExecutionStatus.PARTIAL, ExecutionStatus.FAILED):
        codes.append(Reason.EXPOSURE_RECONCILE_RETRY)

    new_buys, nbcodes = resolve_new_buys(policy_tier, sentinel=sentinel, data_ok=data_ok)
    codes.extend(nbcodes)

    forced, fcodes = resolve_forced_action(policy_tier, gap, cfg,
                                           bootstrap_suppressed=bootstrap_suppressed)
    codes.extend(fcodes)

    manual = bool(bootstrap_suppressed and gap > cfg.exposure_tolerance_pct)

    # --- assemble -----------------------------------------------------------------------
    transitioned = prev_tier is not None and policy_tier is not prev_tier
    last_transition = (signal_session_date if transitioned
                       else (previous.last_transition_date if previous else None))

    seen: set[str] = set()
    ordered_codes = [c for c in codes if not (c in seen or seen.add(c))]

    return RegimeState(
        raw_candidate_tier=raw_candidate,
        previous_policy_tier=prev_tier,
        policy_tier=policy_tier,
        transition_limited=transition_limited,
        target_equity_cap_pct=cap,
        actual_equity_pct=exposure.actual_equity_pct,
        exposure_gap_pct=gap,
        pending_sell_pct=exposure.pending_sell_pct,
        pending_buy_pct=exposure.pending_buy_pct,
        execution_status=exposure.execution_status,
        new_buys=new_buys,
        forced_action=forced,
        reason_codes=ordered_codes,
        reasons=describe(ordered_codes),
        as_of_date=as_of_date,
        signal_session_date=signal_session_date,
        scheduled_week_end=scheduled_week_end,
        evaluation_id=make_evaluation_id(scheduled_week_end, signal_session_date, cfg),
        breadth_pct=breadth_pct,
        breadth_coverage_pct=breadth_cov,
        override_active=override_active,
        book_category_weights=dict(book.weights),
        book_weight_source=book.source,
        index_diagnostics={n: s.as_dict() for n, s in sorted(index_signals.items())}
        | ({sentinel.index_name: sentinel.as_dict()} if sentinel else {}),
        last_transition_date=last_transition,
        next_evaluation_date=next_evaluation_date(scheduled_week_end, cfg),
        data_stale=data_stale,
        manual_action_required=manual,
        algorithm_version=cfg.algorithm_version,
        config_hash=cfg.config_hash(),
        health=health,
        bearish_stack_weight=stack_w,
    )
