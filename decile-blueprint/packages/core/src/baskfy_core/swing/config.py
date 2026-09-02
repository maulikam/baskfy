"""Every threshold of the swing method, named once (docs/swing/04-business-rules.md).

The numbers are Kullamägi's own where he states one (ADR ≥ 3.5-4%, a 30-100%+ prior move,
2 weeks to 3 months of consolidation, a 10%+ gap on the highest volume in months, 0.25-1% risk
per trade, a 20-25% position cap, the 10/20-day trail) and the pack's stated choice where he
does not (docs/swing/DECISIONS-SW.md PACK.n). Nothing downstream compares to a literal: a
detector reads a field of one of these dataclasses, so recalibration is an edit here and a
diff in one place.

Units, stated once
------------------
* Every ``*_pct`` is a **percent** (``3.5`` means 3.5%), never a fraction.
* Every ``*_bars`` counts **trading days**.
* Every ``*_inr`` is **rupees**.
* ``*_adr`` multiples are dimensionless: "two ADRs above the 10-day MA".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class Setup(StrEnum):
    """The three setups. The value is the wire form (``sw_setup_daily.setup``)."""

    FLAG = "FLAG"
    EP = "EP"
    PARABOLIC_SHORT = "PARABOLIC_SHORT"


#: Setups the plan builder may turn into a BUY line. PARABOLIC_SHORT is never here: NSE cash
#: equities cannot be shorted for delivery, and MIS/F&O sit behind product gates that default
#: off (CLAUDE.md non-negotiable 5). docs/swing/02-scope-and-gating.md Track C.
TRADEABLE_SETUPS: Final[frozenset[Setup]] = frozenset({Setup.FLAG, Setup.EP})


@dataclass(frozen=True, slots=True)
class LiquidityConfig:
    """The universe a setup may come from (docs/swing/04 §1)."""

    #: ADR% = mean over ``adr_bars`` of ``(high / low - 1) x 100``. His floor is 3.5-4%.
    adr_bars: int = 20
    adr_min_pct: float = 3.5
    #: Average rupee turnover over ``turnover_bars``. ₹5 cr matches the desk's
    #: ``MIN_MEDIAN_DAILY_VALUE`` so the two books agree on what "liquid" means.
    turnover_bars: int = 20
    turnover_min_inr: float = 5e7
    #: Below this the tick is a large fraction of the stop distance; his "$1" floor, in rupees.
    price_min: float = 20.0


@dataclass(frozen=True, slots=True)
class FlagConfig:
    """Setup 1 — the flag / continuation breakout (docs/swing/04 §2)."""

    #: How far back the flagpole may start. ~3 months of bars.
    lookback_bars: int = 65
    #: The prior move: highest high of the window over the lowest low before it, minus one.
    flagpole_min_gain_pct: float = 30.0
    #: Bars since the pole's high — the consolidation length. Two weeks to about three months.
    base_min_bars: int = 10
    base_max_bars: int = 60
    #: The base may not retrace more than this from the pole high (a flag, not a collapse).
    base_max_depth_pct: float = 30.0
    #: Tightness: the range of the last ``tight_bars`` over the close, as a multiple of ADR%.
    tight_bars: int = 10
    tight_max_adr_multiple: float = 3.0
    #: Higher lows: the second half of the base may not undercut the first half's low.
    require_higher_lows: bool = True
    #: Not extended: the close sits within ``max_extension_adr`` ADRs of the 10-day MA.
    max_extension_adr: float = 2.0
    #: Surfing the MAs: close within this much below the 20-day MA is still "on" it.
    ma_tolerance_pct: float = 2.0
    #: The 20-day MA must be no lower than it was ``ma_rising_bars`` ago.
    ma_rising_bars: int = 5
    #: Volume dry-up: mean volume over the last ``dryup_bars`` ≤ ratio x mean over the base.
    dryup_bars: int = 10
    dryup_max_ratio: float = 0.85
    #: The pivot is the highest high of the last ``pivot_bars`` bars (capped by the base length).
    pivot_bars: int = 20
    #: A close above yesterday's pivot on ≥ this multiple of 50-day volume is BREAKOUT_TODAY.
    breakout_min_rvol: float = 1.5


@dataclass(frozen=True, slots=True)
class EpConfig:
    """Setup 2 — the episodic pivot (docs/swing/04 §3)."""

    #: The gap: today's open over yesterday's close, minus one.
    min_gap_pct: float = 10.0
    #: Relative volume: today's volume over the mean of the prior ``rvol_bars``.
    rvol_bars: int = 50
    min_rvol: float = 3.0
    #: The day must close green and in the upper part of its range — the gap held.
    min_close_position: float = 0.5
    #: Neglect: the return over the ``prior_bars`` before the gap may not exceed this.
    prior_bars: int = 60
    max_prior_gain_pct: float = 30.0
    #: An EP stays enterable for this many bars after the gap day.
    valid_bars: int = 3


@dataclass(frozen=True, slots=True)
class ParabolicConfig:
    """Setup 3 — the parabolic runner, detected for the record only (docs/swing/04 §4)."""

    min_gain_5_bars_pct: float = 50.0
    min_gain_10_bars_pct: float = 100.0
    min_up_streak: int = 3
    #: Extension above the 10-day MA in ADRs.
    min_extension_adr: float = 4.0


@dataclass(frozen=True, slots=True)
class SizingConfig:
    """Risk per trade decides survival (docs/swing/04 §5). Ceilings are system-only env."""

    risk_per_trade_pct: float = 0.5
    max_position_pct: float = 20.0
    max_open_positions: int = 8
    #: A position may not exceed this fraction of the name's average daily turnover.
    max_position_vs_turnover: float = 0.01
    #: Below this the brokerage dominates the edge (the desk's ``MIN_TRADE_VALUE``).
    min_trade_value_inr: float = 10_000.0


@dataclass(frozen=True, slots=True)
class StopConfig:
    """Stops and exits (docs/swing/04 §6)."""

    #: Partial sale into strength between these bars after entry, if the position is green.
    partial_earliest_bar: int = 3
    partial_latest_bar: int = 5
    #: The partial as a ratio of integers, so ``300 x 1/3`` is exactly 100 shares.
    partial_numerator: int = 1
    partial_denominator: int = 3
    #: Move the stop to breakeven once this many R is showing, or after the partial.
    breakeven_after_r: float = 1.0
    #: ADR% at or above which the fast (10-day) trail is used; slower names trail the 20-day.
    fast_trail_min_adr_pct: float = 6.0
    #: An entry stop may not sit further below the entry than this, whatever the LOD says.
    max_stop_distance_pct: float = 10.0


@dataclass(frozen=True, slots=True)
class OpeningRangeConfig:
    """The live monitor (docs/swing/04 §7)."""

    #: Candidate opening-range windows, in minutes from the 09:15 open.
    windows_minutes: tuple[int, ...] = (1, 5, 60)
    default_window_minutes: int = 5
    #: The break must clear the range high by this much (ticks are not a signal).
    break_buffer_pct: float = 0.1
    #: For an EP at the open: minimum gap, and the volume pace — volume so far against an
    #: average day's volume pro-rated to the minutes elapsed — the gap must be running at.
    live_min_gap_pct: float = 10.0
    live_min_volume_pace: float = 3.0
    #: Session bounds, IST, as ``(hour, minute)``. The monitor is idle outside them.
    session_open: tuple[int, int] = (9, 15)
    monitor_close: tuple[int, int] = (10, 45)


@dataclass(frozen=True, slots=True)
class MarketConfig:
    """Breadth, the gate, and progressive exposure (docs/swing/04 §8)."""

    #: His gauge: share of the universe up at least this much over the last month.
    strong_move_pct: float = 25.0
    green_min_pct_up: float = 5.0
    red_max_pct_up: float = 2.0
    #: The index must be above both its 10- and 20-day MAs for GREEN; below both is RED.
    index_ma_fast: int = 10
    index_ma_slow: int = 20
    #: Progressive exposure: tiers as (max open positions, max sleeve exposure %), lowest first.
    tiers: tuple[tuple[int, float], ...] = ((2, 25.0), (4, 50.0), (6, 75.0), (8, 100.0))
    #: Step up after this many closed trades with positive net R; step down on this streak.
    lookback_trades: int = 5
    step_down_loss_streak: int = 3


@dataclass(frozen=True, slots=True)
class SwingConfig:
    """Everything the engine can be recalibrated on without an edit."""

    liquidity: LiquidityConfig = field(default_factory=LiquidityConfig)
    flag: FlagConfig = field(default_factory=FlagConfig)
    ep: EpConfig = field(default_factory=EpConfig)
    parabolic: ParabolicConfig = field(default_factory=ParabolicConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    stops: StopConfig = field(default_factory=StopConfig)
    opening_range: OpeningRangeConfig = field(default_factory=OpeningRangeConfig)
    market: MarketConfig = field(default_factory=MarketConfig)

    #: The moving averages the detectors and the trail read.
    ma_fast: int = 10
    ma_slow: int = 20
    ma_trend: int = 50

    #: Bars of history a full detection needs. Callers may pass more; less is a ValueError.
    @property
    def bars_required(self) -> int:
        return (
            max(
                self.flag.lookback_bars + self.flag.base_max_bars,
                self.ep.prior_bars + self.ep.rvol_bars,
                self.ma_trend,
            )
            + 1
        )


DEFAULT_SWING_CONFIG: Final = SwingConfig()
