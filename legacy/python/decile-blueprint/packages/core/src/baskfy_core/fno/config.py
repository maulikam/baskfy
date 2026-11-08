"""Every number of the FO run, named once (``docs/fno/04-business-rules.md``).

``04`` is the numerical contract: each default below is the number printed there, each bound is
the range printed beside it, and ``test_fno_config.py`` asserts both. **Nothing downstream
compares to a literal**: the scan, the plan builders, the desk's monitor and the re-test engine
read a field of one of these frozen dataclasses.

Units, stated once
------------------
* Every ``*_pct`` is a **percent** (``1.0`` means 1 %), never a fraction.
* Every ``*_inr`` is rupees, a ``Decimal``. Every ``*_r`` is in R (a ``Decimal``).
* Every time is IST wall-clock; the pure core takes ``now`` as an argument (law 1).

What is deliberately absent
---------------------------
* **An auto-execute field**, for any sleeve (``02`` Track B: no auto-executed entry, ever). The
  one auto flag in the F&O book, F3's ``BASKFY_FNO_F3_AUTO_EXIT`` (M.5, Maulik's, an exit only),
  is the desk's env and never a config field. A field that exists is a field somebody turns on.
* **Lot sizes, strike steps and expiry dates.** They come from the NFO master (``04`` §1).
* **The env ceilings as config fields.** :class:`FnoCeilings` carries ``02``'s defaults so the
  pure core can be called with them; the values in force are system-only env read by the
  services (core reads no env). A setting may sit below them, never above:
  :func:`ceiling_violations` says which one does.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final

#: F1's capital (Maulik, M.2, 25 Sep 2026, raising M.1's ₹10 lakh: at 1 % that was ₹10,000, below
#: every NIFTY and BANKNIFTY lot's max loss in the last year; ``04`` §3), seeded by FO2.
F1_SEED_CAPITAL_INR: Final = Decimal("2500000")
#: F2's capital (``01`` §1b: Q1 was asked for F1 only). Paper runs one lot; live refuses.
F2_SEED_CAPITAL_INR: Final = Decimal("0")
#: The first UDiFF bhavcopy session: the file carries the underlying's price from here (``04`` §4).
UDIFF_FIRST_SESSION: Final = dt.date(2024, 7, 8)


class FoSleeve(StrEnum):
    """The ``fo_sleeve`` codes (``01`` §4)."""

    F1N = "F1N"
    F1B = "F1B"
    F2 = "F2"
    #: F3, the directional index credit spread (M.5): NIFTY weekly, BANKNIFTY monthly.
    F3N = "F3N"
    F3B = "F3B"


class FoSleeveGroup(StrEnum):
    """How flags group the sleeves (``01`` §4): ``F1N`` and ``F1B`` are one flag, ``F1``."""

    F1 = "F1"
    F2 = "F2"
    F3 = "F3"


def group_of(sleeve: FoSleeve) -> FoSleeveGroup:
    if sleeve is FoSleeve.F2:
        return FoSleeveGroup.F2
    if sleeve in (FoSleeve.F3N, FoSleeve.F3B):
        return FoSleeveGroup.F3
    return FoSleeveGroup.F1


def f1_sleeve_for(underlying: str) -> FoSleeve:
    """``F1N`` for NIFTY, ``F1B`` for BANKNIFTY; anything else is not an F1 underlying."""
    if underlying == "NIFTY":
        return FoSleeve.F1N
    if underlying == "BANKNIFTY":
        return FoSleeve.F1B
    raise ValueError(f"{underlying!r} is not an F1 underlying (04 §1)")


def f3_sleeve_for(underlying: str) -> FoSleeve:
    """``F3N`` for NIFTY (weekly), ``F3B`` for BANKNIFTY (monthly); M.5's third answer."""
    if underlying == "NIFTY":
        return FoSleeve.F3N
    if underlying == "BANKNIFTY":
        return FoSleeve.F3B
    raise ValueError(f"{underlying!r} is not an F3 underlying (04 §11)")


class Structure(StrEnum):
    """``fo_plan.structure`` (``03`` §4)."""

    IRON_CONDOR = "IRON_CONDOR"
    FUTURE = "FUTURE"
    #: F3's short option with its far wing (M.5, ``04`` §11).
    CREDIT_SPREAD = "CREDIT_SPREAD"


class PlanKind(StrEnum):
    """``fo_plan.kind`` (``03`` §4)."""

    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ROLL = "ROLL"
    #: F3's pyramid (``04`` §11): more lots onto the open spread, never a new position.
    ADD = "ADD"


class ScanState(StrEnum):
    """``fo_scan.state`` (``04`` §8). The last four are F2's only."""

    NOT_ENTRY_DAY = "NOT_ENTRY_DAY"
    CANDIDATE = "CANDIDATE"
    SKIPPED_EVENT = "SKIPPED_EVENT"
    PAUSED = "PAUSED"
    OPEN_POSITION = "OPEN_POSITION"
    NO_DATA = "NO_DATA"
    NO_SIGNAL = "NO_SIGNAL"
    BLOCKED_BAN = "BLOCKED_BAN"
    BLOCKED_REGIME = "BLOCKED_REGIME"
    BLOCKED_CAPACITY = "BLOCKED_CAPACITY"


class PlanState(StrEnum):
    """``fo_plan.state`` (``04`` §8). Every non-happy state carries its reason in words."""

    ISSUED = "ISSUED"
    CONFIRMED = "CONFIRMED"
    FILLING = "FILLING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    LAPSED = "LAPSED"
    REJECTED_STRUCTURE = "REJECTED_STRUCTURE"
    REJECTED_LIQUIDITY = "REJECTED_LIQUIDITY"
    REJECTED_SIZE = "REJECTED_SIZE"
    REJECTED_MARGIN = "REJECTED_MARGIN"
    REJECTED_COST = "REJECTED_COST"
    NO_SLEEVE_CAPITAL = "NO_SLEEVE_CAPITAL"
    #: ``04`` §7: a pause stops new entries; the desk writes the refused plan with its reason
    #: (FO10.7) so the paper checklist sees a skip, not a cycle the desk missed.
    REJECTED_PAUSED = "REJECTED_PAUSED"
    ABANDONED_PARTIAL = "ABANDONED_PARTIAL"


def _between[T: (int, Decimal, float)](name: str, value: T, low: T, high: T) -> None:
    if not low <= value <= high:
        raise ValueError(f"{name} = {value} is outside its bounds {low}-{high} (04)")


@dataclass(frozen=True, slots=True)
class FnoCeilings:
    """``02`` Track B "Ceilings": the system-only env bounds, as the pure core receives them.

    ``BASKFY_FNO_RISK_PER_TRADE_INR_MAX``, ``…_RISK_PCT_MAX``, ``…_MAX_OPEN_POSITIONS_MAX``,
    ``…_MAX_PER_UNDERLYING_MAX`` and ``…_BOOK_MONTHLY_LOSS_INR_MAX``. The services read the env;
    the core is handed the values (law 1).
    """

    risk_per_trade_inr_max: Decimal = Decimal("25000")
    risk_pct_max: Decimal = Decimal("1.0")
    max_open_positions_max: int = 10
    max_per_underlying_max: int = 1
    book_monthly_loss_inr_max: Decimal = Decimal("75000")

    def __post_init__(self) -> None:
        if self.risk_per_trade_inr_max <= 0 or self.book_monthly_loss_inr_max <= 0:
            raise ValueError("a rupee ceiling must be positive")
        if self.risk_pct_max <= 0 or self.max_open_positions_max < 1:
            raise ValueError("a ceiling of zero is a flag, not a ceiling")
        if self.max_per_underlying_max < 1:
            raise ValueError("max per underlying must be at least one")


@dataclass(frozen=True, slots=True)
class CommonConfig:
    """Fields every FO sleeve shares (``04`` §1, §3)."""

    #: ``fo_hard_exit_before_expiry``: flat by 15:00 on session ``E - n``. **Never zero** (``02``
    #: §2.2): no position is ever held into its expiry day.
    hard_exit_before_expiry: int = 1
    hard_exit_time: dt.time = dt.time(15, 0)
    #: ``fo_max_lots`` (``04`` §3): default 2, ceiling 10.
    max_lots: int = 2
    max_lots_ceiling: int = 10
    #: The plan's lifetime after issue (``04`` §1: ``issued + 30 min``).
    plan_ttl_minutes: int = 30
    market_open: dt.time = dt.time(9, 15)
    #: The last moment an entry window may end (``04`` §1: "inside 09:15-15:00").
    latest_window_end: dt.time = dt.time(15, 0)

    def __post_init__(self) -> None:
        _between("fo_hard_exit_before_expiry", self.hard_exit_before_expiry, 1, 5)
        _between("fo_max_lots", self.max_lots, 1, self.max_lots_ceiling)
        _between("fo_max_lots_ceiling", self.max_lots_ceiling, 1, 10)
        if self.plan_ttl_minutes <= 0:
            raise ValueError("a plan must live for a positive number of minutes")


@dataclass(frozen=True, slots=True)
class F1Config:
    """F1, the index monthly condor (``04`` §1-§3, §7)."""

    underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY")
    #: The index underlyings the pack admits; the API also checks them against the NFO master.
    index_underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY")
    entry_sessions_before: int = 15
    plan_time: dt.time = dt.time(9, 20)
    entry_window_end: dt.time = dt.time(10, 30)
    loss_close_mult: Decimal = Decimal("1.5")
    profit_take_pct: Decimal = Decimal("50")
    short_sigma: Decimal = Decimal("1.0")
    wing_sigma: Decimal = Decimal("0.5")
    min_short_oi_lots: int = 500
    max_spread_pct: Decimal = Decimal("5")
    max_open_per_underlying: int = 1
    max_cost_share_pct: Decimal = Decimal("25")
    risk_per_trade_pct: Decimal = Decimal("1.0")
    #: Research slippage per leg per crossing for the index options (``RESEARCH.md`` method).
    slippage_pct: Decimal = Decimal("0.5")
    slippage_min_inr: Decimal = Decimal("0.05")
    #: ``04`` §7: pause after this many consecutive trades at or below ``pause_loss_r``.
    pause_consecutive: int = 3
    pause_loss_r: Decimal = Decimal("-0.6")

    def __post_init__(self) -> None:
        if not self.underlyings or not set(self.underlyings) <= set(self.index_underlyings):
            raise ValueError(
                f"f1_underlyings {self.underlyings} must be a non-empty subset of "
                f"{self.index_underlyings} (04 §1)"
            )
        _between("f1_entry_sessions_before", self.entry_sessions_before, 10, 20)
        if not dt.time(9, 15) <= self.plan_time < self.entry_window_end <= dt.time(15, 0):
            raise ValueError("f1_entry_window must sit inside 09:15-15:00 (04 §1)")
        _between("f1_loss_close_mult", self.loss_close_mult, Decimal(1), Decimal(2))
        _between("f1_profit_take_pct", self.profit_take_pct, Decimal(30), Decimal(80))
        _between("f1_short_sigma", self.short_sigma, Decimal("0.75"), Decimal("1.5"))
        _between("f1_wing_sigma", self.wing_sigma, Decimal("0.25"), Decimal("1.0"))
        if self.min_short_oi_lots < 0:
            raise ValueError("f1_min_short_oi_lots cannot be negative")
        if not Decimal(0) < self.max_spread_pct <= Decimal(100):
            raise ValueError("f1_max_spread_pct must be a positive percent")
        _between("f1_max_open_per_underlying", self.max_open_per_underlying, 1, 1)
        if not Decimal(0) < self.max_cost_share_pct <= Decimal(100):
            raise ValueError("f1_max_cost_share must be a positive percent")
        if self.risk_per_trade_pct <= 0:
            raise ValueError("risk_per_trade_pct must be positive")
        if self.slippage_pct < 0 or self.slippage_min_inr < 0:
            raise ValueError("slippage cannot be negative")
        if self.pause_consecutive < 1 or self.pause_loss_r >= 0:
            raise ValueError("the F1 pause needs a positive count and a negative R")


@dataclass(frozen=True, slots=True)
class F2Config:
    """F2, stock-futures breakout long (``04`` §10, §7)."""

    universe_turnover_pct: Decimal = Decimal("60")
    turnover_sessions: int = 20
    breakout_sessions: int = 20
    trend_sessions: int = 50
    stop_atr: Decimal = Decimal("3.0")
    atr_sessions: int = 14
    trail: bool = True
    max_sessions: int = 40
    roll_before_expiry: int = 1
    max_open: int = 5
    max_per_industry: int = 2
    #: ``01`` §1b: buy the next month if the near one expires within this many sessions.
    near_month_min_sessions: int = 3
    #: ``04`` §7: pause new entries for the rest of the month at this closed-R total.
    month_pause_r: Decimal = Decimal("-6")
    risk_per_trade_pct: Decimal = Decimal("1.0")

    def __post_init__(self) -> None:
        _between("f2_universe_turnover_pct", self.universe_turnover_pct, Decimal(20), Decimal(100))
        _between("f2_breakout_sessions", self.breakout_sessions, 10, 60)
        _between("f2_trend_sessions", self.trend_sessions, 20, 200)
        _between("f2_stop_atr", self.stop_atr, Decimal("1.5"), Decimal("5.0"))
        _between("f2_max_sessions", self.max_sessions, 10, 60)
        _between("f2_roll_before_expiry", self.roll_before_expiry, 1, 3)
        _between("f2_max_open", self.max_open, 1, 10)
        if self.max_per_industry < 1 or self.atr_sessions < 1 or self.turnover_sessions < 1:
            raise ValueError("window lengths and the industry cap must be positive")
        if self.near_month_min_sessions < 0:
            raise ValueError("near_month_min_sessions cannot be negative")
        if self.month_pause_r >= 0:
            raise ValueError("f2 month pause must be a negative R")
        if self.risk_per_trade_pct <= 0:
            raise ValueError("risk_per_trade_pct must be positive")


@dataclass(frozen=True, slots=True)
class F3Config:
    """F3, the directional index credit spread (``04`` §11; Maulik, M.5, 28 Sep 2026).

    The auto-exit switch is **not** a field here: ``BASKFY_FNO_F3_AUTO_EXIT`` is the desk's env,
    read by ``fno_gates`` alone, so the pure core cannot know or change whether an exit is sent
    by a hand or by the monitor.
    """

    underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY")
    pivot_lookback: int = 60
    pivot_width: int = 3
    trend_sessions: int = 20
    weekly_sessions: int = 5
    confirm_bars: int = 10
    distance_pct: Decimal = Decimal("1.0")
    wing_pct: Decimal = Decimal("2.0")
    short_premium_min_inr: Decimal = Decimal("10")
    min_sessions_weekly: int = 2
    min_sessions_monthly: int = 5
    entry_share_pct: Decimal = Decimal("25")
    full_share_pct: Decimal = Decimal("50")
    add_working_pct: Decimal = Decimal("20")
    decay_target_pct: Decimal = Decimal("80")
    loss_cut_mult: Decimal = Decimal("2.0")
    level_buffer_pct: Decimal = Decimal("0.10")
    max_open_per_underlying: int = 1
    risk_per_trade_pct: Decimal = Decimal("1.0")
    #: F1's entry window (``04`` §1), shared: plan at 09:20, confirmable to 10:30.
    plan_time: dt.time = dt.time(9, 20)
    entry_window_end: dt.time = dt.time(10, 30)
    #: Research slippage per leg per crossing, the index-option figure F1 uses (``04`` §3).
    slippage_pct: Decimal = Decimal("0.5")
    slippage_min_inr: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        if not self.underlyings or not set(self.underlyings) <= {"NIFTY", "BANKNIFTY"}:
            raise ValueError(
                "f3_underlyings must be a non-empty subset of NIFTY, BANKNIFTY (04 §11)"
            )
        _between("f3_pivot_lookback", self.pivot_lookback, 20, 250)
        _between("f3_pivot_width", self.pivot_width, 1, 10)
        _between("f3_trend_sessions", self.trend_sessions, 5, 100)
        _between("f3_weekly_sessions", self.weekly_sessions, 3, 10)
        _between("f3_confirm_bars", self.confirm_bars, 3, 40)
        _between("f3_distance_pct", self.distance_pct, Decimal("0.25"), Decimal("3.0"))
        _between("f3_wing_pct", self.wing_pct, Decimal("0.5"), Decimal("5.0"))
        _between("f3_short_premium_min_inr", self.short_premium_min_inr, Decimal(1), Decimal(100))
        _between("f3_min_sessions_weekly", self.min_sessions_weekly, 1, 4)
        _between("f3_min_sessions_monthly", self.min_sessions_monthly, 1, 15)
        _between("f3_entry_share_pct", self.entry_share_pct, Decimal(5), Decimal(50))
        _between("f3_full_share_pct", self.full_share_pct, Decimal(10), Decimal(100))
        if self.full_share_pct < self.entry_share_pct:
            raise ValueError("f3_full_share_pct cannot sit below f3_entry_share_pct")
        _between("f3_add_working_pct", self.add_working_pct, Decimal(5), Decimal(80))
        _between("f3_decay_target_pct", self.decay_target_pct, Decimal(50), Decimal(95))
        _between("f3_loss_cut_mult", self.loss_cut_mult, Decimal("1.2"), Decimal("4.0"))
        _between("f3_level_buffer_pct", self.level_buffer_pct, Decimal(0), Decimal("1.0"))
        _between("f3_max_open_per_underlying", self.max_open_per_underlying, 1, 1)
        if self.risk_per_trade_pct <= 0:
            raise ValueError("risk_per_trade_pct must be positive")
        if not dt.time(9, 15) <= self.plan_time < self.entry_window_end <= dt.time(15, 0):
            raise ValueError("f3's entry window must sit inside 09:15-15:00 (04 §1)")
        if self.slippage_pct < 0 or self.slippage_min_inr < 0:
            raise ValueError("slippage cannot be negative")


@dataclass(frozen=True, slots=True)
class FutureCostRates:
    """``04`` §10's futures costs per order, as dated percent fields.

    Futures STT 0.05 % on the sale (Finance Act 2026, from 1 Apr 2026; ``docs/options``
    DECISIONS-OP OP0.1), exchange 0.00173 % per side, stamp 0.002 % on the buy, ₹20 per order,
    GST 18 % on brokerage and exchange charges, and 0.03 % slippage a side until FO3 measures it.
    """

    brokerage_per_order_inr: Decimal = Decimal("20")
    stt_sell_pct: Decimal = Decimal("0.05")
    exchange_txn_pct: Decimal = Decimal("0.00173")
    stamp_buy_pct: Decimal = Decimal("0.002")
    gst_pct: Decimal = Decimal("18")
    slippage_pct: Decimal = Decimal("0.03")


@dataclass(frozen=True, slots=True)
class StopVolConfig:
    """The desk's ``stop_from_vol`` band: ``kite-momentum-rebalancer/app/config.py``
    (``STOP_MIN, STOP_MAX, STOP_VOL_MULT = 0.08, 0.12, 2.2``) — non-negotiable 4's 8-12 %."""

    vol_mult: float = 2.2
    stop_min: float = 0.08
    stop_max: float = 0.12

    def __post_init__(self) -> None:
        if not 0 < self.stop_min <= self.stop_max < 1:
            raise ValueError("the stop band must satisfy 0 < min <= max < 1")


@dataclass(frozen=True, slots=True)
class SeriesConfig:
    """``04`` §4: the continuous series, the CA flag, ATR, RV, IV and basis."""

    #: A held-future move above this ratio, or below ``ca_down_ratio``, is a corporate action.
    ca_up_ratio: float = 1.4
    ca_down_ratio: float = 0.7
    ca_exclusion_sessions: int = 5
    atr_sessions: int = 14
    rv_sessions: int = 20
    annualisation_sessions: int = 252
    turnover_sessions: int = 20
    iv_min_sessions_left: int = 8
    iv_rate: float = 0.0
    iv_lower: float = 0.01
    iv_upper: float = 5.0
    iv_tolerance: float = 1e-6
    basis_from: dt.date = UDIFF_FIRST_SESSION

    def __post_init__(self) -> None:
        if not 0 < self.ca_down_ratio < 1 < self.ca_up_ratio:
            raise ValueError("the CA band must straddle 1")


@dataclass(frozen=True, slots=True)
class BookConfig:
    """``fo_book_config`` (``03`` §6, ``04`` §7): the book's own limits, under the ceilings."""

    monthly_pause_inr: Decimal = Decimal("75000")
    max_open_positions: int = 10
    max_per_underlying: int = 1

    def __post_init__(self) -> None:
        if self.monthly_pause_inr <= 0 or self.max_open_positions < 1:
            raise ValueError("the book's limits must be positive")
        if self.max_per_underlying < 1:
            raise ValueError("max per underlying must be at least one")


@dataclass(frozen=True, slots=True)
class FnoConfig:
    """The whole contract of ``04``."""

    common: CommonConfig = field(default_factory=CommonConfig)
    f1: F1Config = field(default_factory=F1Config)
    f2: F2Config = field(default_factory=F2Config)
    f3: F3Config = field(default_factory=F3Config)
    future_costs: FutureCostRates = field(default_factory=FutureCostRates)
    stop_vol: StopVolConfig = field(default_factory=StopVolConfig)
    series: SeriesConfig = field(default_factory=SeriesConfig)
    book: BookConfig = field(default_factory=BookConfig)

    def risk_per_trade_pct(self, sleeve: FoSleeve) -> Decimal:
        group = group_of(sleeve)
        if group is FoSleeveGroup.F1:
            return self.f1.risk_per_trade_pct
        if group is FoSleeveGroup.F3:
            return self.f3.risk_per_trade_pct
        return self.f2.risk_per_trade_pct


DEFAULT_FNO_CONFIG: Final = FnoConfig()
DEFAULT_FNO_CEILINGS: Final = FnoCeilings()


def ceiling_violations(config: FnoConfig, ceilings: FnoCeilings) -> tuple[str, ...]:
    """Every field that sits above its env ceiling (``02`` Track B). Empty means admissible."""
    found: list[str] = []
    for name, pct in (
        ("f1", config.f1.risk_per_trade_pct),
        ("f2", config.f2.risk_per_trade_pct),
        ("f3", config.f3.risk_per_trade_pct),
    ):
        if pct > ceilings.risk_pct_max:
            found.append(
                f"{name}.risk_per_trade_pct {pct} > BASKFY_FNO_RISK_PCT_MAX {ceilings.risk_pct_max}"
            )
    if config.book.monthly_pause_inr > ceilings.book_monthly_loss_inr_max:
        found.append(
            f"book.monthly_pause_inr {config.book.monthly_pause_inr} > "
            f"BASKFY_FNO_BOOK_MONTHLY_LOSS_INR_MAX {ceilings.book_monthly_loss_inr_max}"
        )
    if config.book.max_open_positions > ceilings.max_open_positions_max:
        found.append(
            f"book.max_open_positions {config.book.max_open_positions} > "
            f"BASKFY_FNO_MAX_OPEN_POSITIONS_MAX {ceilings.max_open_positions_max}"
        )
    if config.book.max_per_underlying > ceilings.max_per_underlying_max:
        found.append(
            f"book.max_per_underlying {config.book.max_per_underlying} > "
            f"BASKFY_FNO_MAX_PER_UNDERLYING_MAX {ceilings.max_per_underlying_max}"
        )
    if config.f2.max_open > ceilings.max_open_positions_max:
        found.append(
            f"f2.max_open {config.f2.max_open} > BASKFY_FNO_MAX_OPEN_POSITIONS_MAX "
            f"{ceilings.max_open_positions_max}"
        )
    return tuple(found)
