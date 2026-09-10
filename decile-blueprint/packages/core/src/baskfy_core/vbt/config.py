"""Every threshold of VBT-1, named once (``docs/vbt/04-business-rules.md``).

The numbers are the research note's where it states one — the five Chartink lines, the six trend
filters, the 40% breadth gate, the three-session limit, the 12% stop, the 21-EMA exit, ten slots,
three entries a session, 1% of turnover, 25 bps a side — and this pack's stated choice where it
does not (``docs/vbt/DECISIONS-VB.md`` PACK.n). **Nothing downstream compares to a literal:** a
detector, a task, a router and a page each read a field of one of these dataclasses, so a
recalibration is an edit here and a diff in one place.

Units, stated once
------------------
* Every ``*_pct`` is a **percent** (``6.5`` means 6.5%), never a fraction.
* Every ``*_bars`` / ``*_sessions`` counts **trading sessions on the run's own calendar**
  (:mod:`baskfy_core.vbt.calendar` — thin sessions removed), never calendar days.
* Every ``*_inr`` is **rupees**.

What is deliberately absent
---------------------------
There is no ``target_r``, ``partial_*`` or ``max_hold`` field: ``docs/vbt/04`` §6.3 records that
each was tested and each lowered the result, and **a field that exists is a field somebody turns
on**. For the same reason ``TrendConfig`` has exactly six filters — the 50-DMA and the ADR ceiling
that ``grid2.py`` still applies are not fields here (DECISIONS-VB VB0.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class Gate(StrEnum):
    """The breadth gate. Two values, not three (DECISIONS-VB VB0.4).

    The swing book's gate has an amber because its ladder uses the middle value to shrink
    exposure. VBT-1 has no ladder: ``docs/vbt/04`` §4.2 is a single threshold, and the
    sensitivity table measures it as one.
    """

    OPEN = "OPEN"
    SHUT = "SHUT"


class SignalState(StrEnum):
    """What a row of ``vb_signal_daily`` is.

    ``SCAN_ONLY`` rows are stored on purpose (DECISIONS-VB PACK.6): the ablation table is the
    whole argument for the six filters, and a system that stores only what it accepted cannot
    show a person what it rejected.
    """

    SIGNAL = "SIGNAL"
    SCAN_ONLY = "SCAN_ONLY"


class TrendFilter(StrEnum):
    """The six filters of ``docs/vbt/04`` §3.2, by the letters STRATEGY §3 gives them."""

    A_ABOVE_200DMA = "A"
    B_TWENTY_DAY_HIGH = "B"
    C_NOT_EXTENDED = "C"
    D_STRONG_CLOSE = "D"
    E_CHANGE_CEILING = "E"
    F_TURNOVER_FLOOR = "F"


@dataclass(frozen=True, slots=True)
class DataConfig:
    """The universe, the calendar and the missing-bar tolerance (``docs/vbt/04`` §1, §2)."""

    #: NSE cash equities. BE/BZ (trade-for-trade) stay in: every trade here is delivery anyway,
    #: and dropping today's BE list would drop the history of names that were *later* demoted —
    #: a survivorship bias in reverse. SME (SM/ST/SZ) is out.
    instrument_type: str = "EQ"
    series_allowed: tuple[str, ...] = ("EQ", "BE", "BZ")
    #: A null series is a delisted name with no current listing row, and is kept.
    keep_null_series: bool = True
    #: ETFs are out. The ``etf`` index universe is the authority; the two patterns only catch one
    #: that never made the list, and are narrow on purpose ("GOLD" would flag GOLDIAM).
    etf_universe_slug: str = "etf"
    etf_name_pattern: str = r"\bETF\b"
    etf_symbol_pattern: str = r"(BEES|ETF|IETF)$"

    #: THE THIN-SESSION RULE (`04` §2.1). A session whose traded-name count is below this share
    #: of the centred rolling median of that count is not a trading session for this strategy.
    #: Muhurat and special-Saturday sessions print ~200 names against ~1,900, and a single such
    #: column poisons every 50- and 200-session window that spans it — which is why the 200-DMA
    #: filter "vanished" for most of 2024-25 on the research's first run.
    #:
    #: The rule is the contract; the six dates it finds on the 2017→ history are a test's
    #: expectation. A seventh muhurat session in 2027 must be found by the rule, not by an edit.
    thin_session_min_share: float = 0.25
    thin_session_window_bars: int = 41
    thin_session_min_periods: int = 5

    #: THE MISSING-BAR TOLERANCE (`04` §2.2). A rolling window over n sessions is valid once it
    #: holds ``max(2, round(n x this))`` bars — the way a screener that only sees traded bars
    #: computes an average. Demanding a full window would blank every name with an occasional
    #: no-trade day and silently shrink the universe to the most liquid names, which is the job
    #: filter F does explicitly, later and on purpose.
    rolling_min_share: float = 0.90

    def min_samples(self, window_bars: int) -> int:
        """How many bars a ``window_bars`` window needs before it has a value."""
        return max(2, round(window_bars * self.rolling_min_share))


@dataclass(frozen=True, slots=True)
class ScanConfig:
    """Chartink's five lines, read literally on the closed daily bar (``docs/vbt/04`` §3.1).

    The comparison senses are part of the contract and are Chartink's own: lines 1 and 5 are
    strict ``>``, lines 3 and 4 are ``>=``.
    """

    #: 1. ``volume > SMA(volume, vol_sma_bars) x vol_mult``. The SMA **includes** the signal day.
    vol_mult: float = 3.0
    vol_sma_bars: int = 50
    #: 2. ``close_raw > this`` — an **exchange price**, never the adjusted close. A ₹28 name that
    #: a 1:2 split makes ₹56 in the adjusted series did not clear Chartink's line.
    min_close_raw_inr: float = 30.0
    #: 3. ``change_pct >= this``.
    min_change_pct: float = 6.5
    #: 4. ``vol_sma >= this``.
    min_vol_sma: float = 25_000.0
    #: 5. ``volume > this``.
    min_volume: float = 50_000.0


@dataclass(frozen=True, slots=True)
class TrendConfig:
    """The six filters that turn a candidate generator into a strategy (``docs/vbt/04`` §3.2).

    Without them the same 32,929 signals, traded exactly this way, return 0.8% a year at a 48%
    drawdown. With them, 6,293 signals return 18.2% at 27.9%. The ablation is in
    ``docs/vbt/01-method.md`` §3; **B, the 20-day-high breakout, is the one the book can least do
    without** (removing it costs 9.2 CAGR points).
    """

    #: A. ``close > SMA(close, dma_bars)`` — below its 200-day average the same bar loses money
    #: in both halves of the history.
    dma_bars: int = 200
    #: B. ``close > highest high of the prior breakout_high_bars sessions``, **today excluded**
    #: (including today, the rule could never be true).
    breakout_high_bars: int = 20
    #: C. ``ret_20_pct < this`` (strict) — not already extended into the spike.
    ret_bars: int = 20
    max_ret_20_pct: float = 25.0
    #: D. ``(close - low) / (high - low) >= this`` — the bar closed strong.
    min_close_position: float = 0.6
    #: E. ``change_pct <= this`` — excludes circuit plays and the parabolic prints.
    max_change_pct: float = 15.0
    #: F. ``mean(close_raw x volume) over turnover_bars >= this`` — ₹2 crore. The thin tail is
    #: where the losses live, and it is the tail a ₹10-lakh book cannot exit.
    turnover_bars: int = 20
    min_turnover_avg_inr: float = 2e7


@dataclass(frozen=True, slots=True)
class BreadthConfig:
    """The regime gate (``docs/vbt/04`` §4).

    A breadth reading of **the universe the book trades**, which is the lesson CLAUDE.md records
    for the swing gate: ask the tape you actually trade. Index-based gates (Midcap 150 over its
    50-DMA, the 10-day over the 20-day) were tested and do not help here.
    """

    dma_bars: int = 200
    #: The gate is OPEN above this (strict ``>``), SHUT at or below it. 35-45 is a plateau:
    #: 30 → 15.6% CAGR at -45% (2018 gets in), 35 → 18.6/-33, **40 → 18.2/-27.9**, 45 → 15.4/-31,
    #: 50 → 10.9/-30. Changing it is a strategy change with a DECISIONS-VB entry, not a tuning
    #: pass — and no gate at all is the same CAGR at **twice** the drawdown.
    min_pct_above_dma: float = 40.0


@dataclass(frozen=True, slots=True)
class EntryConfig:
    """The pullback entry (``docs/vbt/04`` §7). *"Do not chase the open."*"""

    #: The limit is the signal bar's close. Not its high, not a buffer above it, not the next
    #: open — a next-open entry is worth 9.6% a year against this rule's 18.2%, because the open
    #: gap (+1% on the median signal) is exactly the part of the move that reverts.
    limit_at: str = "SIGNAL_CLOSE"
    #: THE PARAMETER WITH A CLIFF. The order works for this many sessions after the signal: it
    #: may fill on t+1, t+2 or t+3, and at the close of t+3 it is cancelled. Two sessions →
    #: 11.4% CAGR, three → 18.2%, five → 17.1%. Two is too short for the pullback to arrive.
    valid_sessions: int = 3
    #: Require the low to trade this far *through* the limit before a fill is assumed. 0.25
    #: changes CAGR by 0.02 pt, which is why the default is 0 and the field exists anyway: it is
    #: the honest knob for anyone who thinks the backtest's fills are optimistic.
    fill_through_pct: float = 0.0


@dataclass(frozen=True, slots=True)
class SizingConfig:
    """Ten equal slots, and every cap named (``docs/vbt/04`` §5)."""

    #: Ten slots, 10% of equity each. Eight cost 2.7 CAGR points and fifteen cost 5.3 — ten is
    #: the useful number for this signal count, not a round one.
    max_slots: int = 10
    #: The per-position ceiling. Above the slot size, so it binds only when equity has drifted.
    max_position_pct: float = 12.5
    #: "1, 2, 3 a day" — counted as lines in this plan **plus** the session's confirmed or sent
    #: orders, whatever plan they came from, so a fourth confirm of an evening is a refusal.
    max_new_entries_per_session: int = 3
    #: Never more than 1% of the name's 20-day average turnover. It binds nowhere at ₹10 lakh
    #: and will at ₹1 crore; it is in from day one so that day is a line on a page.
    max_position_vs_turnover: float = 0.01
    #: Below this the brokerage dominates the edge (the desk's own ``MIN_TRADE_VALUE``).
    min_trade_value_inr: float = 10_000.0
    #: "Start small" (`02` §3.5): the first live sessions plan at half a slot, applied **at plan
    #: time** before every cap, so the line shown is the line sent. A paper plan is full size.
    risk_multiplier_first_live: float = 0.5
    first_live_sessions: int = 5


@dataclass(frozen=True, slots=True)
class ExitConfig:
    """The stop and the exit (``docs/vbt/04`` §6). The EMA is the exit; the stop is insurance."""

    #: The disaster stop, measured from **the fill**. 10% → 16.4% CAGR, 15% → 16.6%: insurance
    #: either way, which is why this one number is a bounded ``vb_config`` setting.
    stop_pct: float = 12.0
    #: A GTT fires a LIMIT order; this puts the resting limit 3% under its trigger so it fills on
    #: the way down the way a market stop would. Additive keyword to ``place_gtt_stop``; the
    #: weekly book keeps the gateway's own value and never sees this one.
    gtt_limit_fraction: float = 0.97
    #: The band the VBT route hands the gateway. The desk's 8-12% is the weekly book's.
    gtt_band_min_pct: float = 0.005
    gtt_band_max_pct: float = 0.15
    #: THE WORKING EXIT: a close below this EMA sells at the next open. 688 of the 761 trades
    #: left this way; 62 hit the stop. A 10-EMA is far too tight (6.3% CAGR) and a 50-SMA holds
    #: longer for the same CAGR at a deeper drawdown.
    trail_ema_bars: int = 21
    #: A held name that stops printing is written off at its last close after this many blank
    #: sessions. On the live book it is an alert as well as an exit line.
    no_bar_tolerance_sessions: int = 5


@dataclass(frozen=True, slots=True)
class CostConfig:
    """What a round trip costs the backtest (``docs/vbt/04`` §8)."""

    #: 25 bps a side — STT, exchange and brokerage charges, and slippage. **Used by the backtest
    #: only**; a live fill's cost is whatever the broker charged and is read from the journal.
    #: 40 bps → 15.5% CAGR and 60 bps → 11.9%, which is what scale does to this strategy.
    cost_pct_per_side: float = 0.25


@dataclass(frozen=True, slots=True)
class VbtConfig:
    """Everything the sleeve can be recalibrated on without an edit."""

    data: DataConfig = field(default_factory=DataConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    breadth: BreadthConfig = field(default_factory=BreadthConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    costs: CostConfig = field(default_factory=CostConfig)

    @property
    def bars_required(self) -> int:
        """Sessions of history a full detection needs. Callers may pass more; less is an error."""
        return (
            max(
                self.trend.dma_bars,
                self.breadth.dma_bars,
                self.scan.vol_sma_bars,
                self.exits.trail_ema_bars,
                self.trend.breakout_high_bars + 1,
                self.trend.ret_bars + 1,
                self.trend.turnover_bars,
            )
            + 1
        )


DEFAULT_VBT_CONFIG: Final = VbtConfig()

#: The tick the exchange quotes NSE cash equities in, and the tick every level the desk sends is
#: snapped to. Not a setting: it is the exchange's.
TICK_INR: Final[str] = "0.05"
