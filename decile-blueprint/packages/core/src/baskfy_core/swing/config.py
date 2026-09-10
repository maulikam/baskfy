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

    #: ADR% = mean over ``adr_bars`` of ``(high / low - 1) x 100``. His screens use 5%+; 3.5-4%
    #: is the floor he names on stream. 4.0 here; ``sw_config`` lets the trader raise it.
    adr_bars: int = 20
    adr_min_pct: float = 4.0
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
    #: "Most of my positions are 10-20% of account size"; the hard ceiling he states is 30%
    #: overnight, which is the system-only env ceiling, not this default.
    max_position_pct: float = 20.0
    #: "Typically 5-10 positions; 15-20 in a good market; all cash in a bad one." The ladder
    #: (``MarketConfig.tiers``) climbs toward this; the plan takes the smaller of the two.
    max_open_positions: int = 10
    #: "You do not need to trade 50 things. 1, 2, 3 stocks per day." New entries per session.
    max_new_entries_per_session: int = 3
    #: A position may not exceed this fraction of the name's average daily turnover.
    max_position_vs_turnover: float = 0.01
    #: Below this the brokerage dominates the edge (the desk's ``MIN_TRADE_VALUE``).
    min_trade_value_inr: float = 10_000.0
    #: "Start small" (docs/swing/02 §3.5, STANDING-ANSWERS A9): the first live sessions plan at
    #: half the configured risk — ``risk_per_trade_pct x risk_multiplier_first_live`` before
    #: ``size_position`` — for ``first_live_sessions`` sessions, counted down by the evening job
    #: once a LIVE session closes (``sw_config.first_live_sessions_left``). Applied at plan
    #: time, never to a quantity at send time, so the line shown is the line sent; SELL and
    #: RAISE lines are never touched. A paper plan is full size.
    risk_multiplier_first_live: float = 0.5
    first_live_sessions: int = 5


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
    #: "Stop should not be wider than the ATR or ADR of the stock" - the width that actually
    #: binds on a normal day. The widest stop is ``min(max_stop_distance_pct, adr_pct x this)``.
    max_stop_adr_multiple: float = 1.0


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
    #: WHEN TRIGGERS STOP BEING EVALUATED — the whole cash session since 9 Sep 2026.
    #:
    #: This was ``(10, 45)`` and `opening_range.evaluate_trigger` still explains why: "he trades
    #: the first 60-90 minutes". Maulik asked for the opposite — *"build the auto execute outside
    #: monitor window anytime during trading time"* — so a name that breaks its opening range at
    #: 14:00 is now a trigger, where before it answered SESSION_OVER and nothing fired.
    #:
    #: A strategy change, not a bug fix, and his to make. What is unchanged is WHAT a trigger is:
    #: the break is still measured against the OPENING range (`windows_minutes`), still needs the
    #: buffer, still needs a FLAG above its daily pivot, and a locked circuit is still a lock. The
    #: window over which that test is applied is all that widened.
    monitor_close: tuple[int, int] = (15, 30)
    #: ...and when the session's housekeeping runs, which did NOT move with it.
    #:
    #: `monitor_close` used to serve two purposes, and extending it would have moved both. This is
    #: the second: A7/A8's chore — cancel any open remainder, and free the `PENDING_RANGE` slots
    #: that no range ever resolved so a lower-scored flag can use them. It belongs at 10:45
    #: because a gap whose range has not resolved by then is not going to, and holding its slot
    #: all afternoon starves the rest of the watchlist.
    #:
    #: Keeping them separate also avoids an inversion: the desk's clock runs the cutoff as soon
    #: as the strategy stops, so a single setting at 15:30 would have put the cutoff AFTER the
    #: 15:15 GTT sweep.
    pending_cutoff_at: tuple[int, int] = (10, 45)
    #: THE ENTRY CAP. ``min(trigger x (1 + entry_limit_buffer_pct / 100), range_high +
    #: entry_limit_max_adr x ADR)`` — half a percent of chase, and never more than a quarter of
    #: a normal day's range above the opening range it broke out of. It is the highest price
    #: this strategy will pay for a breakout, and it is derived from the setup, not from the
    #: broker's idea of a reasonable slip.
    entry_limit_buffer_pct: float = 0.5
    entry_limit_max_adr: float = 0.25

    #: HOW THE ENTRY IS SENT (Maulik, 4 Sep 2026 — supersedes STANDING-ANSWERS A8's order type,
    #: not its cap; STANDING-ANSWERS B16, "the later letter wins").
    #:
    #: A8 chose a resting *marketable LIMIT* at the cap above. On a breakout that is the wrong
    #: side of the trade-off and the live book showed it: a LIMIT that the tape runs past does
    #: not fill, and the name that was "in swing" is bought by everyone except us. What A8 was
    #: protecting against — paying an unbounded price on a thin book — is what Kite's
    #: **market protection** already does, and does at the exchange rather than in a resting
    #: order that the market can simply leave behind.
    #:
    #: So: ``MARKET``, with the protection percentage derived from *this* cap
    #: (`baskfy_core.swing.plan.market_protection_pct`) rather than from Zerodha's default 3 %.
    #: The strategy's own risk ceiling stays the binding one, and a price that has already run
    #: past the cap is refused rather than chased — which is the honest answer, not a worse fill.
    #: Set this back to ``"LIMIT"`` and A8's behaviour returns exactly, with no other edit.
    entry_order_type: str = "MARKET"
    #: Kite accepts a protection greater than 0 and up to 100 (``-1`` means "let Zerodha decide",
    #: which is the thing we are choosing not to do). Below the floor the number is noise and the
    #: order goes unprotected-but-capped by the refusal above it; the max is the most this
    #: strategy will ever hand the exchange, whatever the arithmetic says.
    market_protection_floor_pct: float = 0.05
    market_protection_max_pct: float = 3.0
    #: How long a confirm waits for the broker's fill before answering (seconds), and how often
    #: it asks — the orders endpoint at most twice a second (Kite's own ceiling is ten).
    fill_poll_seconds: float = 10.0
    fill_poll_interval_seconds: float = 0.5
    #: SW11 (STANDING-ANSWERS A4, B10). The range is built from the ``TickBus`` ticks inside the
    #: window; the historical minute candles are fetched once, ``range_reconcile_delay_minutes``
    #: after the window closed, only to reconcile the tick range (Zerodha: the historical API
    #: "was never built for polling during market hours"). The monitor's quote fallback — for a
    #: name the ticker has gone quiet on — asks Kite ``/quote`` at most once every
    #: ``quote_poll_min_seconds``; anything faster is a bug, not a setting to lower.
    range_reconcile_delay_minutes: int = 1
    quote_poll_min_seconds: float = 5.0
    #: A8: when the desk's clock sweeps the book for a filled quantity without a GTT and
    #: re-arms it (`SWING_GTT_MISSING_AT_1515` reads what is still naked after this).
    gtt_sweep_at: tuple[int, int] = (15, 15)


@dataclass(frozen=True, slots=True)
class WatchConfig:
    """The watchlist's own rules (docs/swing/04 §9.5, SW5).

    Two numbers, and both exist because a watchlist that is not pruned is not a watchlist. His
    weekend routine produces "a watchlist of a few dozen forming flags"; a list that only ever
    grows becomes a list nobody reads, and the names on it stop being the ones that were setting
    up *this week*.
    """

    #: A detected flag is watched automatically from this score up. Below it the setup is real but
    #: unremarkable, and every name that is merely real would fill the list. `04` §2.6's score is
    #: out of 100 and its median textbook flag scores in the low 60s.
    auto_watch_min_score: float = 60.0
    #: A flag stays on the list this many sessions without triggering. Two trading weeks: his
    #: bases run two weeks to three months, so a name that has not moved in ten sessions has not
    #: stopped being a base — but it has stopped being *this week's*, and the detector will find
    #: it again tomorrow if it still qualifies.
    flag_valid_bars: int = 10
    #: His funnel (docs/swing/07, STANDING-ANSWERS A14): the weekly focus list is the top
    #: ``auto_watch_top_n`` SETTING_UP flags by score (plus every EP); the daily focus — what
    #: is pushed and sits at the top of the desk page — is the top ``focus_top_n`` of those by
    #: score (plus every EP). The rest are watched, signalled and logged, never pushed.
    auto_watch_top_n: int = 20
    focus_top_n: int = 5
    #: A MANUAL row expires after this many sessions unless re-confirmed on the watchlist page:
    #: a two-week-old typed pivot is stale, and MANUAL levels are not refreshed premarket.
    manual_valid_bars: int = 10


@dataclass(frozen=True, slots=True)
class MarketConfig:
    """Breadth, the gate, and progressive exposure (docs/swing/04 §8)."""

    #: His gauge: share of the universe up at least this much over the last month.
    strong_move_pct: float = 25.0
    green_min_pct_up: float = 5.0
    red_max_pct_up: float = 2.0
    #: His index filter for longs: the 10-day MA above the 20-day. Below it, no new longs.
    index_ma_fast: int = 10
    index_ma_slow: int = 20
    #: Progressive exposure: tiers as (max open positions, max sleeve exposure %), lowest first.
    #: The top rung is his "typical" count; his 15-20 in a great market is the env ceiling.
    tiers: tuple[tuple[int, float], ...] = ((2, 25.0), (4, 50.0), (6, 75.0), (10, 100.0))
    #: Step up after this many closed trades with positive net R; step down on this streak.
    lookback_trades: int = 5
    step_down_loss_streak: int = 3
    #: "I try to contain them at 15-20%." A sleeve this far below its peak stops opening new
    #: positions until it has recovered to within ``resume_drawdown_pct`` of the peak.
    max_drawdown_pct: float = 15.0
    resume_drawdown_pct: float = 10.0


@dataclass(frozen=True, slots=True)
class SwingConfig:
    """Everything the engine can be recalibrated on without an edit."""

    liquidity: LiquidityConfig = field(default_factory=LiquidityConfig)
    flag: FlagConfig = field(default_factory=FlagConfig)
    ep: EpConfig = field(default_factory=EpConfig)
    parabolic: ParabolicConfig = field(default_factory=ParabolicConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    stops: StopConfig = field(default_factory=StopConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
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
