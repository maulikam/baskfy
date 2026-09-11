"""Every threshold of TWT-1, named once (``docs/twt/04-business-rules.md``).

``04`` is the contract and this module is its executable half: **nothing downstream compares to a
literal**. A detector, a task, a router and a page each read a field of one of these dataclasses,
so a recalibration is an edit here plus an edit in ``04``, and a diff in one place.
``packages/core/tests/test_twt_no_literals.py`` scans the sleeve's source for a threshold written
as a number, and ``test_twt_docs_parity.py`` asserts the values below are the values ``04`` states.

Units, stated once (``04``'s own preamble)
------------------------------------------
* Every ``*_pct`` is a **percent** (``20.0`` means 20 %), never a fraction — except
  ``max_position_vs_turnover``, which is a **fraction** (``0.01``) because it multiplies money and
  the research's own ``Rules`` field was a percent that read as a bug every time it was quoted.
* Every ``*_bars`` / ``*_sessions`` counts **trading sessions on the run's own calendar**
  (:mod:`baskfy_core.twt.calendar` — thin sessions removed), never calendar days — except
  ``month_low_months_back``, which counts **calendar months**, because Chartink's monthly candle
  does.
* Every ``*_inr`` is **rupees**.

Money, prices and every multiplier that lands on a price are :class:`~decimal.Decimal` (house rule
9). Polars promotes a ``Decimal`` literal to the frame's ``Float64`` for a comparison, so the same
field serves the panel arithmetic and the desk's exact levels without a second spelling. The two
genuine floats are ``thin_session_min_share`` and ``rolling_min_share``, which are shares of a
count, and ``min_pct_above_dma``, which is handed verbatim to VBT-1's breadth config (``04`` §4.4).

What is deliberately absent
---------------------------
There is no ``target_pct``, no ``partial_*``, no ``max_hold``, no ``exit_close_below_sma`` and no
``exit_close_below_ema`` field. ``docs/twt/01`` §5 records that each was measured and each lowered
the result, and **a field that exists is a field somebody turns on**. There are also no trend
filters: ``01`` §4 measured them and they hurt — the 50/200-SMA pair costs 8.6 CAGR points here,
on the same signal that makes VBT-1 work.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final


class Gate(StrEnum):
    """The regime gate (``04`` §4.3). **Two values, not three.**

    The swing book's gate has an amber because its ladder uses the middle value to shrink
    exposure. This book has ten equal slots and no ladder: ``01`` §5 is a single threshold and the
    sensitivity table measures it as one.
    """

    OPEN = "OPEN"
    SHUT = "SHUT"


class SignalState(StrEnum):
    """What a row of ``tw_signal_daily`` is (``03`` §3).

    An entry event below the liquidity floor is stored as ``SCAN_ONLY``, never dropped: a system
    that stores only what it accepted cannot show a person what it passed over, and ``01`` §7's
    liquidity row is the argument for the floor this sleeve ships at.
    """

    SIGNAL = "SIGNAL"
    SCAN_ONLY = "SCAN_ONLY"


class EntryTiming(StrEnum):
    """``04`` §5.1. One member, and the enum exists to say that it is one member.

    ``01`` §5 measured the alternatives: a buy-stop at the 15-session base high returns 3.7 % CAGR
    and a limit at the signal close 11.6 %, against this rule's 20.9 %.
    """

    NEXT_OPEN = "NEXT_OPEN"


class RankKey(StrEnum):
    """How more signals than slots are ordered (``04`` §6.3, DECISIONS-TW **TW0.2**).

    The **signal session's own** rupee turnover. ``research/tight-close/STRATEGY.md`` §3 says
    "20-day turnover"; the code that produced every number in that note ranks by the signal day's
    own ``close_raw x volume``. The numbers are the fact and the prose is the stale half.
    """

    SIGNAL_TURNOVER = "SIGNAL_TURNOVER"


@dataclass(frozen=True, slots=True)
class DataConfig:
    """The universe, the calendar and the missing-bar tolerance (``04`` §1, §2)."""

    #: NSE cash equities. BE/BZ (trade-for-trade) stay in: every trade here is delivery anyway.
    #: SME series (SM/ST/SZ) are out. **Identical to ``docs/vbt/04`` §1 on purpose** — two sleeves
    #: drawing from different universes would make `01` §6's "zero shared trades" a statement
    #: about populations rather than about events (`04` §1.4, ``test_twt_neighbours.py``).
    instrument_type: str = "EQ"
    series_allowed: tuple[str, ...] = ("EQ", "BE", "BZ")
    #: A null series is a delisted name with no current listing row, and is **kept**: dropping
    #: today's list would drop the history of names later demoted — survivorship bias in reverse.
    keep_null_series: bool = True
    #: ETFs are out. The ``etf`` index universe is the authority; the two patterns only catch one
    #: that never made the list, and are narrow on purpose ("GOLD" would flag GOLDIAM).
    etf_universe_slug: str = "etf"
    etf_name_pattern: str = r"\bETF\b"
    etf_symbol_pattern: str = r"(BEES|ETF|IETF)$"

    #: THE THIN-SESSION RULE (`04` §2.1). A session whose traded-name count is below this share of
    #: the centred rolling median of that count **is not a trading session for this strategy** and
    #: is removed before any rolling statistic. Muhurat and special-Saturday sessions print about
    #: 200 names against about 1,900, and a single such column poisons every 50- and 200-session
    #: window that spans it.
    #:
    #: The rule is the contract; the six dates it finds on the 2017 -> history are a test's
    #: expectation. A seventh muhurat session in 2027 must be found by the rule, not by an edit.
    thin_session_min_share: float = 0.25
    thin_session_window_bars: int = 41
    thin_session_min_periods: int = 5

    #: THE MISSING-BAR TOLERANCE (`04` §2.2). A rolling window over n sessions is valid once it
    #: holds ``max(2, round(n x this))`` bars — the way a screener that only sees traded bars
    #: computes an average. Demanding a full window would blank every name with an occasional
    #: no-trade day and silently shrink the universe to the most liquid names, which is the job
    #: the liquidity floor does explicitly, later and on purpose.
    rolling_min_share: float = 0.90

    #: Sessions of history a full detection needs (`04` §2.3). 200 governs — the month-3 low needs
    #: at most about 85 sessions — but the 200-session average also needs 200 *valid* bars under
    #: §2.2 and the thin-session drops of §2.1 consume some. 260 is 200 + a quarter's slack + the
    #: six thin sessions. Callers may pass more; less is an error.
    bars_required: int = 260

    def min_samples(self, window_bars: int) -> int:
        """How many bars a ``window_bars`` window needs before it has a value (`04` §2.2)."""
        return max(2, round(window_bars * self.rolling_min_share))


@dataclass(frozen=True, slots=True)
class ScanConfig:
    """Chartink's five lines, read literally on the closed daily bar (``04`` §3.1).

    The comparison senses are part of the contract and are Chartink's own. Line 4 — *market cap >
    1* — is **not implemented**: it is a no-op in Chartink and a no-op here, and naming a field for
    it would invite somebody to give it a number.

    Line 1 reads ``close_raw``, the exchange print. A ₹28 name that a 1:2 split makes ₹56 in the
    adjusted series did not clear Chartink's line. Lines 2, 3 and 5 read the **adjusted** series,
    because they compare a price to another price of a different date, or a volume to an average
    of volumes, and a split between the two makes the raw comparison meaningless.
    """

    #: 1. ``close_raw > this`` — an exchange price, never the adjusted close.
    min_close_raw: Decimal = Decimal("30.0")
    #: 2. ``close >= this x month_low_back``.
    month_low_multiple: Decimal = Decimal("1.3")
    #: 2. how far back the monthly low is read, in **calendar months**. Measured alternatives, all
    #: worse against Chartink's export: two months back, four months back, and a rolling
    #: 63-session low.
    month_low_months_back: int = 3
    #: 3. ``(max(W) / min(W) - 1) x 100 <= this``. Chartink's own 3.01, not a rounded 3.
    tight_band_pct: Decimal = Decimal("3.01")
    #: 3. how many weekly closes are in ``W`` — the current (partial) week and the two before it.
    tight_weeks: int = 3
    #: 5. ``vol_sma >= this``. A share count, not money, and exact at this size.
    min_vol_sma: Decimal = Decimal("10000.0")
    #: 5. the volume average's window, which **includes the signal day** — Chartink's reading of
    #: ``Sma(Volume, 50)``.
    vol_sma_bars: int = 50


@dataclass(frozen=True, slots=True)
class BreadthConfig:
    """The regime gate (``04`` §4).

    A breadth reading of **the universe the book trades**, which is the lesson ``CLAUDE.md``
    records for the swing gate: ask the tape you actually trade. The arithmetic is VBT-1's —
    :mod:`baskfy_core.twt.breadth` calls it — but these two numbers are **spelled out here, not
    inherited**, so a VBT recalibration cannot silently move this sleeve's gate
    (``04`` §4.4, DECISIONS-TW **TW0.4**).

    Not to be confused with ``market_health_daily.pct_above_200dma``, which measures an **index's**
    point-in-time membership: a different measurement of a different population.
    """

    dma_bars: int = 200
    #: The gate is OPEN **strictly above** this and SHUT at or below it. 35-40 is the plateau:
    #: 30 -> 18.6 % CAGR at -31 %, 35 -> 20.3/-26, **40 -> 20.9/-24.7**, 45 -> 13.5/-26,
    #: 50 -> 14.4/-35, no gate at all -> 17.2/-43. A float because it is handed verbatim to
    #: VBT-1's own breadth config.
    min_pct_above_dma: float = 40.0
    #: ``pct_above_dma`` is reported and stored to this many decimal places (`04` §4.2), so the
    #: value the gate read and the value a page shows can never be two different numbers.
    pct_decimals: int = 4


@dataclass(frozen=True, slots=True)
class EntryConfig:
    """The entry event, the liquidity floor and how a crowded session is ordered (``04`` §3.4-3.5,
    §5.1, §6.3)."""

    #: ``04`` §5.1. Next session's open, at market, and there is no second member.
    entry: EntryTiming = EntryTiming.NEXT_OPEN
    #: ``04`` §3.4. The state is true today and was false on **each** of the previous this-many
    #: sessions. Measured: 10 -> 19.7 % CAGR at -27 %, 20 -> 15.8 % at -32 %.
    #:
    #: A name with fewer than this many sessions of **existing** history before the session has no
    #: entry event: "was false for five sessions" is a claim about five sessions that exist. The
    #: research's implementation seeds its counter at a large number and fires on a listing day;
    #: this sleeve does not (DECISIONS-TW **TW0.6**).
    entry_min_sessions_out: int = 5
    #: ``04`` §3.5. ₹5 crore on the mean of ``close_raw x volume`` over ``turnover_avg_bars``.
    #: **The research's headline used ₹2 crore.** This sleeve ships higher because the research ran
    #: at ₹10 lakh and this sleeve runs at ₹25 lakh: ten slots at ₹25 lakh is a ₹2.5 lakh line, and
    #: §6.2's 1 %-of-turnover cap does not stop binding until the name turns over ₹2.5 crore a day.
    #: A floor below the cap means the plan is routinely sized by the cap rather than by the
    #: strategy. It is also the better of the two in the research's own sensitivity table — 22.5 %
    #: CAGR at -27 % against 20.9 % at -24.7 %. DECISIONS-TW **TW0.3**.
    min_turnover_inr: Decimal = Decimal("50000000")
    #: The research's own floor, kept for **exactly one caller**: TW2's golden parameter set,
    #: because a golden that cannot reproduce the study is not a golden. It is never read by the
    #: detector, the plan or a page, and ``test_twt_no_literals.py`` asserts as much.
    research_min_turnover_inr: Decimal = Decimal("20000000")
    #: The window the liquidity floor and the 1 %-of-turnover cap both read (`04` §2.2 applies).
    turnover_avg_bars: int = 20
    #: ``04`` §6.3 and DECISIONS-TW **TW0.2**. Ranking matters: by nothing 15.8 %, by relative
    #: volume 17.2 %, by day-change 16.3 %, by turnover 20.9 %.
    rank_key: RankKey = RankKey.SIGNAL_TURNOVER


@dataclass(frozen=True, slots=True)
class SizingConfig:
    """Ten equal slots, and every cap named (``04`` §6)."""

    #: ``04`` §6.1. Eight slots returns 26.3 % CAGR at -27 % and fifteen returns 14.7 % at -29 %:
    #: ten is the useful number for this signal count, not a round one.
    max_slots: int = 10
    #: The per-position ceiling, a percent of sleeve equity. Above the slot size, so it binds only
    #: when equity has drifted.
    max_position_pct: Decimal = Decimal("12.5")
    #: A **fraction**, not a percent: 1 % of the name's 20-session average turnover. At ₹25 lakh
    #: over ten slots it binds until the name turns over ₹2.5 crore a day, which is the whole
    #: argument for the ₹5 crore floor above.
    max_position_vs_turnover: Decimal = Decimal("0.01")
    #: Below this the brokerage dominates the edge; a line that cannot clear it is skipped
    #: ``BELOW_MIN_TRADE_VALUE`` (the desk's own ``MIN_TRADE_VALUE``).
    min_trade_value_inr: Decimal = Decimal("10000")
    #: ``04`` §6.3. Counted as lines in this plan **plus** the session's already-confirmed or sent
    #: orders, whatever plan they came from, so a fourth confirm of an evening is a refusal.
    max_new_entries_per_session: int = 3
    #: ``04`` §6.4. Half a slot while the first live entries are being taken, applied **before**
    #: every cap so the line shown is the line sent. A ``DRY_RUN`` plan is full size: half size is
    #: a live-money discipline, and a paper plan that is not the plan is not a rehearsal.
    risk_multiplier_first_live: Decimal = Decimal("0.5")
    #: It counts **entries, not sessions**. The swing book and VBT-1 count sessions because they
    #: enter most days; this book enters about eighteen times a year, and a five-session allowance
    #: would be spent by a quiet week.
    first_live_entries: int = 10


@dataclass(frozen=True, slots=True)
class ExitConfig:
    """The two stops, and there are exactly two (``04`` §7).

    137 of the research's 164 exits are the trail. No target, no partial, no time stop, no
    moving-average exit: ``01`` §5 measured all four, and the 50-SMA exit is a *different strategy*
    with the same signal (543 trades, 15.6 % CAGR, -38.3 % drawdown).
    """

    #: ``04`` §7.1, measured from **the fill** — the exchange's price, not the cost-inclusive book
    #: entry. 15 % -> 21.6 % CAGR at -23 %, 30 % -> 19.4 % at -25 %, and **10 % breaks the
    #: strategy**: these names swing more than 12 % inside their own bases, so a tight stop
    #: converts the median trade into a stop-out. The band 15-30 is flat, which is why this is a
    #: bounded ``tw_config`` setting rather than a constant.
    stop_pct: Decimal = Decimal("20.0")
    #: ``04`` §7.2. **This is the exit.** Tighter is a cliff (15 % -> 9.6 % CAGR at -43 %); wider
    #: thins the book to nothing (30 % -> 15.5 % on 73 trades). 20-25 % is the plateau, which is
    #: why the ``tw_config`` bound on this one is a **floor** and not a ceiling (DECISIONS-TW
    #: **TW0.5**): tightening it is the failure mode.
    trail_pct: Decimal = Decimal("20.0")
    #: ``04`` §7.2's clamp. A trigger at or above the last traded price fires the moment it is
    #: armed, which on a GTT means selling the position at the next tick for no reason. The
    #: research code clamps for the same purpose and this sleeve reproduces it exactly so TW2's
    #: goldens can be a tick comparison.
    close_clamp_fraction: Decimal = Decimal("0.9999")
    #: The fallback used when the raw trigger already sits at or above the close.
    close_clamp_fallback: Decimal = Decimal("0.999")
    #: ``04`` §7.5. A position whose instrument prints no bar for this many consecutive sessions is
    #: closed at its last known close — a delisting or a suspension, and a book that carries such a
    #: line forever reports an equity it cannot realise.
    no_bar_sessions: int = 5
    #: ``04`` §10.6. A GTT fires a LIMIT order and Maulik uses market stops; this puts the resting
    #: limit 3 % under its trigger so it fills on the way down the way a market stop would. An
    #: additive keyword to ``place_gtt_stop``; the weekly book keeps the gateway's own 0.995 and
    #: never sees this one.
    gtt_limit_fraction: Decimal = Decimal("0.97")
    #: ``04`` §10.7, DECISIONS-TW **TW0.8**. The band the TWT route hands the gateway. The desk's
    #: own 8-12 % is the weekly book's; a 20 % stop is outside it, and a band that refuses this
    #: sleeve's own stop would make non-negotiable 4 unsatisfiable. The ceiling is 0.30 rather
    #: than 0.25 because ``tw_config.stop_pct``'s own ceiling is 25 % and a band equal to the
    #: setting's ceiling would refuse the ceiling itself on a tick-floor rounding.
    gtt_band_min_pct: Decimal = Decimal("0.005")
    gtt_band_max_pct: Decimal = Decimal("0.30")


@dataclass(frozen=True, slots=True)
class CostConfig:
    """What a round trip costs the backtest (``04`` §8)."""

    #: Charged on both sides in the backtest; the live book's costs are the broker's and are
    #: journalled, not modelled. Long holds make costs nearly irrelevant here: 40 bps a side gives
    #: 20.3 % and 60 bps gives 19.5 %.
    cost_bps_per_side: Decimal = Decimal("25.0")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """The backtest's own parameters, named so TW2 and TW9 cannot disagree about what they ran
    (``04`` §12)."""

    initial_capital_inr: Decimal = Decimal("1000000")
    start: dt.date = dt.date(2017, 10, 16)
    is_oos_split: dt.date = dt.date(2023, 1, 1)


@dataclass(frozen=True, slots=True)
class TwtConfig:
    """Everything the sleeve can be recalibrated on without an edit."""

    data: DataConfig = field(default_factory=DataConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    breadth: BreadthConfig = field(default_factory=BreadthConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    @property
    def deepest_window_bars(self) -> int:
        """The longest rolling window the sleeve reads, in sessions.

        ``DataConfig.bars_required`` is stated in ``04`` §2.3 rather than derived, because the
        slack it carries is an argument about thin sessions and the §2.2 tolerance rather than an
        arithmetic fact. This property is what a test compares it against: a window longer than
        the history a caller is told to load is a silently blank column.
        """
        return max(
            self.breadth.dma_bars,
            self.scan.vol_sma_bars,
            self.entry.turnover_avg_bars,
            self.entry.entry_min_sessions_out,
            self.exits.no_bar_sessions,
        )


DEFAULT_TWT_CONFIG: Final = TwtConfig()

#: The tick the exchange quotes NSE cash equities in, and the tick every level the desk sends is
#: snapped to. **Not a setting: it is the exchange's**, and it is the same number VBT-1 and the
#: swing book snap to — ``test_twt_neighbours.py`` asserts the two spellings agree.
TICK_INR: Final[str] = "0.05"

#: The tick the **research** simulator floored to (``research/volume-breakout/vbt/sim.py``'s
#: ``_tick``: ``floor(x * 100) / 100``, one paisa). It exists for exactly one caller, TW2's golden
#: parameter set, for the same reason ``EntryConfig.research_min_turnover_inr`` does: a golden that
#: cannot reproduce the study is not a golden, and the study's stops sit on paise. It is never read
#: by the detector, the plan or a page.
RESEARCH_TICK_INR: Final[str] = "0.01"

#: How many DRY_RUN sessions the sleeve must rehearse before real money is discussable.
#:
#: **Zero — there is no paper phase.** Maulik decided it on 11 Sep 2026, before the run started
#: (``docs/twt/02`` §3); the same call he made for VBT-1 that morning and for the swing book on
#: 2 Sep. ``tw_session`` still counts the sessions the machinery has run and the pages still show
#: the count, because "this sleeve has rehearsed three sessions" is worth knowing after it stops
#: being a condition. It is **information**, not a gate.
#:
#: Not a strategy parameter and not a field of :class:`TwtConfig` — nothing in the method reads it
#: — but the desk page, the web page and the settings view all print it, and three copies is two
#: too many.
DRY_RUN_SESSIONS_REQUIRED: Final[int] = 0
