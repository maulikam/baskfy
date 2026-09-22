"""Every threshold of the options run, named once (``docs/options/04-business-rules.md``).

``04`` is the numerical contract: each default below is the number in brackets there, and
``04`` §14 carries one row per field with the value it holds — ``test_options_docs_parity.py``
regenerates that table from this module and asserts it both ways. **Nothing downstream compares
to a literal:** a scan, a plan builder, the desk process and a page each read a field of one of
these frozen dataclasses, so a recalibration is an edit here, an edit in ``04`` and a
``DECISIONS-OP`` entry (condor PACK.7, carried as PACK.2).

Units, stated once
------------------
* Every ``*_pct`` is a **percent** (``0.75`` means 0.75 %), never a fraction.
* Every ``*_frac`` is a **fraction** (``0.25`` means a quarter).
* Every ``*_inr`` is rupees, a ``Decimal``. Points are index points.
* Every time is IST wall-clock; the pure core takes ``now`` as an argument (law 1).

What is deliberately absent
---------------------------
* **Lot sizes, strike steps and expiry dates.** They come from the NFO master (``04`` §1.1,
  §1.4, §2.1); a literal lot size appears only in test fixtures.
* **An auto-execute field**, for any sleeve (``02`` Track C §3, PACK.3). A field that exists is a
  field somebody turns on.
* **A day-before field for O1-W** (PACK.7 names ``trade_days_before_expiry`` as the *reversal*
  path; adding it now would add a knob nobody decided to have).
* **The env ceilings as config.** ``OptionsCeilings`` below carries ``02``'s defaults so the pure
  core can be called with them, but the values in force are system-only env read by OP2's
  settings; they are never fields of ``OptionsConfig`` and never form fields.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final

#: The day every rate of :class:`CostRates` was last checked against its primary source (OP0,
#: ``DECISIONS-OP`` OP0.1; pinned by OP6, OP6.3). ``costs.rates_review_due`` warns once
#: ``review_after_days`` have passed; re-verifying the rates moves this date and nothing else.
OPTIONS_COST_RATES_REVIEWED_ON: Final = dt.date(2026, 9, 22)


class Sleeve(StrEnum):
    """The five sleeve codes of ``03`` (the ``op_sleeve`` enum)."""

    O1M = "O1M"
    O1W = "O1W"
    O2 = "O2"
    O3A = "O3A"
    O3B = "O3B"


class SleeveGroup(StrEnum):
    """How config and flags group the sleeves: ``O3A``/``O3B`` are one sleeve, ``O3`` (``03``)."""

    O1M = "O1M"
    O1W = "O1W"
    O2 = "O2"
    O3 = "O3"


def group_of(sleeve: Sleeve) -> SleeveGroup:
    """The config/flag group a sleeve code belongs to."""
    if sleeve in (Sleeve.O3A, Sleeve.O3B):
        return SleeveGroup.O3
    return SleeveGroup(sleeve.value)


#: The sleeves that trade on an expiry day and share its one slot (``04`` §8.6). O2 is outside it.
SLOT_SLEEVES: frozenset[Sleeve] = frozenset({Sleeve.O1M, Sleeve.O1W, Sleeve.O3A, Sleeve.O3B})


class ExpiryKind(StrEnum):
    """``04`` §1.1 — monthly is the last master expiry of its calendar month."""

    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Mode(StrEnum):
    """From ``options_gates()`` at 09:15 (OP2); never changed later in the session (``03`` §8)."""

    PAPER = "PAPER"
    LIVE = "LIVE"


class SizingMode(StrEnum):
    """``04`` §7.3 / PACK.6. The journal never pools the two."""

    BUDGET = "BUDGET"
    PAPER_ONE_LOT = "PAPER_ONE_LOT"


class OiUnit(StrEnum):
    """What Kite's ``oi`` counts (``04`` §2.4). OP0's live read (e) is pending — OP1.3."""

    UNITS = "UNITS"
    LOTS = "LOTS"


class CondorVariant(StrEnum):
    MONTHLY = "MONTHLY"
    WEEKLY = "WEEKLY"


@dataclass(frozen=True, slots=True)
class CalendarConfig:
    """``04`` §1 and the session clock every sleeve shares."""

    #: Track C §5 / PACK.12: the code is underlying-agnostic, the config accepts NIFTY only.
    underlying: str = "NIFTY"
    allowed_underlyings: tuple[str, ...] = ("NIFTY",)
    market_open: dt.time = dt.time(9, 15)
    #: The settlement moment of an expiring contract, and the end of ``T`` (``04`` §2.3).
    market_close: dt.time = dt.time(15, 30)

    def __post_init__(self) -> None:
        if self.underlying not in self.allowed_underlyings:
            raise ValueError(
                f"underlying {self.underlying!r} is not allowed in v1 "
                f"(allowed: {', '.join(self.allowed_underlyings)}; 02 Track C §5)"
            )


@dataclass(frozen=True, slots=True)
class ChainConfig:
    """``04`` §2 — which contracts are read, the forward, IV/greeks, liquidity, staleness."""

    snapshot_strikes: int = 15
    rate: float = 0.065
    iv_lower: float = 0.01
    iv_upper: float = 5.0
    iv_tolerance: float = 1e-6
    min_premium: Decimal = Decimal("0.50")
    max_spread_pct: Decimal = Decimal("3.0")
    depth_levels: int = 3
    min_oi_lots: int = 200
    oi_unit: OiUnit = OiUnit.UNITS
    stale_quote_seconds: int = 15
    stale_index_seconds: int = 30
    stale_scan_seconds: int = 120
    greeks_model: str = "black76-parity-v1"


@dataclass(frozen=True, slots=True)
class CondorConfig:
    """O1 (``04`` §3; condor ``04`` §2, §4, §5.3, §6, §7, §9). Two instances, never shared."""

    variant: CondorVariant = CondorVariant.MONTHLY
    gap_max_pct: Decimal = Decimal("0.75")
    range_max_pct: Decimal = Decimal("0.80")
    er_max: Decimal = Decimal("0.30")
    observation_start: dt.time = dt.time(9, 15)
    observation_end: dt.time = dt.time(9, 59)
    opening_range_end: dt.time = dt.time(9, 44)
    min_bars: int = 45
    delta_min: Decimal = Decimal("0.20")
    delta_max: Decimal = Decimal("0.25")
    delta_target: Decimal = Decimal("0.22")
    strike_buffer_points: Decimal = Decimal("0")
    wing_width_points: Decimal = Decimal("150")
    credit_floor_frac: Decimal = Decimal("0.25")
    credit_target_frac: Decimal = Decimal("0.30")
    profit_take_frac: Decimal = Decimal("0.50")
    stop_frac: Decimal = Decimal("1.50")
    plan_time: dt.time = dt.time(10, 0)
    entry_window_end: dt.time = dt.time(10, 15)
    hard_exit_time: dt.time = dt.time(14, 30)
    cost_share_max: Decimal = Decimal("0.20")
    risk_per_trade_pct: Decimal = Decimal("1.0")
    max_lots: int = 3
    reserve_per_lot_inr: Decimal = Decimal("1000")
    tier3_min_sessions: int = 12


@dataclass(frozen=True, slots=True)
class DirectionalConfig:
    """O2 (``04`` §4; PACK.5). The trend reads NIFTY 50 on purpose, not the swing gate's index."""

    trend_ema_days: int = 20
    gap_max_pct: Decimal = Decimal("1.0")
    opening_range_start: dt.time = dt.time(9, 15)
    opening_range_end: dt.time = dt.time(9, 29)
    opening_range_bars: int = 15
    or_max_pct: Decimal = Decimal("0.90")
    vix_max: Decimal = Decimal("22")
    bar_minutes: int = 5
    entry_window_start: dt.time = dt.time(9, 30)
    entry_window_end: dt.time = dt.time(13, 30)
    buffer_pct: Decimal = Decimal("0.05")
    itm_steps: int = 1
    delta_min: Decimal = Decimal("0.50")
    delta_max: Decimal = Decimal("0.75")
    stop_frac: Decimal = Decimal("0.30")
    target_frac: Decimal = Decimal("0.60")
    time_stop_minutes: int = 45
    time_stop_min_gain: Decimal = Decimal("0.10")
    hard_exit_time: dt.time = dt.time(15, 0)
    reserve_per_lot_inr: Decimal = Decimal("300")
    premium_cap_pct: Decimal = Decimal("10")
    cost_share_max: Decimal = Decimal("0.15")
    risk_per_trade_pct: Decimal = Decimal("0.5")
    max_lots: int = 2
    tier3_min_sessions: int = 60


@dataclass(frozen=True, slots=True)
class ExpirySetupsConfig:
    """O3 (``04`` §5; PACK.10). ``O3A`` and ``O3B`` share one group, as config and flags do."""

    o3a_enabled: bool = True
    o3b_enabled: bool = True
    width_points: Decimal = Decimal("100")
    max_debit_frac: Decimal = Decimal("0.55")
    o3a_range_start: dt.time = dt.time(9, 15)
    o3a_range_end: dt.time = dt.time(10, 14)
    o3a_range_bars: int = 60
    o3a_range_max_pct: Decimal = Decimal("1.20")
    o3a_window_start: dt.time = dt.time(10, 19)
    o3a_window_end: dt.time = dt.time(13, 0)
    o3a_buffer_pct: Decimal = Decimal("0.05")
    o3a_er_min: Decimal = Decimal("0.40")
    o3b_gap_min_pct: Decimal = Decimal("0.50")
    o3b_gap_max_pct: Decimal = Decimal("1.50")
    o3b_hold_end: dt.time = dt.time(9, 44)
    o3b_plan_time: dt.time = dt.time(9, 45)
    o3b_window_end: dt.time = dt.time(10, 0)
    bar_minutes: int = 5
    target_frac_of_width: Decimal = Decimal("0.80")
    stop_frac_of_debit: Decimal = Decimal("0.50")
    hard_exit_time: dt.time = dt.time(14, 45)
    reserve_per_lot_inr: Decimal = Decimal("500")
    cost_share_max: Decimal = Decimal("0.20")
    risk_per_trade_pct: Decimal = Decimal("0.5")
    max_lots: int = 2
    tier3_min_sessions: int = 20


@dataclass(frozen=True, slots=True)
class CostRates:
    """``04`` §6 — every statutory and broker rate as a dated field.

    Verified by OP0 (``DECISIONS-OP`` OP0.1), **read 22 Sep 2026**:

    * STT on the sale of an option **0.15 % of premium**, and on an exercised option **0.15 % of
      intrinsic**, for transactions on/after 1 Apr 2026 — Memorandum Explaining the Provisions in
      the Finance Bill, 2026, Clause 143, https://www.indiabudget.gov.in/doc/memo.pdf ; corroborated
      by https://zerodha.com/charges/ .
    * NSE options transaction charge **0.03553 %** of premium and IPFT **₹0.01 per crore** (+GST),
      from 1 Mar 2026 — https://zerodha.com/charges/ ; NSE circular NSE/FA/73061 (27 Feb 2026),
      https://nsearchives.nseindia.com/content/circulars/FA73061.pdf .
    * Brokerage ₹20 flat per executed F&O order; SEBI ₹10 per crore; stamp 0.003 % buy side; GST
      18 % on brokerage + transaction + SEBI (+ IPFT) — https://zerodha.com/charges/ .
    * MIS auto square-off 15:26, ₹50 + GST per order — Zerodha support, "intraday auto square-off
      timings". Unreachable behind every hard exit ≤ 15:00.

    The frozen lab's ``options_costs.py`` (rates as of 16 Aug 2026) is the port's source for the
    shape; its IPFT (₹0.50 per lakh) is the stale half — OP0 found ₹0.01 per crore.
    ``reviewed_on`` + ``review_after_days`` is ``OPTIONS_COST_RATES_REVIEWED_ON``'s warning.
    """

    brokerage_per_order_inr: Decimal = Decimal("20")
    stt_sell_premium_pct: Decimal = Decimal("0.15")
    stt_exercise_intrinsic_pct: Decimal = Decimal("0.15")
    exchange_txn_pct: Decimal = Decimal("0.03553")
    sebi_per_crore_inr: Decimal = Decimal("10")
    ipft_per_crore_inr: Decimal = Decimal("0.01")
    stamp_buy_pct: Decimal = Decimal("0.003")
    gst_pct: Decimal = Decimal("18")
    auto_squareoff_inr: Decimal = Decimal("50")
    synthetic_half_spread_pct: Decimal = Decimal("1.5")
    synthetic_half_spread_min_inr: Decimal = Decimal("0.10")
    reviewed_on: dt.date = OPTIONS_COST_RATES_REVIEWED_ON
    review_after_days: int = 90


@dataclass(frozen=True, slots=True)
class SizingConfig:
    """``04`` §7.5 — the first-live multiplier. Per-sleeve risk % and max lots live in each
    sleeve's group because they differ by sleeve (``04`` §3.2, QUESTIONS Q2)."""

    first_live_trades: int = 5
    first_live_risk_multiplier: Decimal = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """``04`` §8 — the shared mechanics the core decides and the desk sends."""

    limit_improve_ticks: int = 1
    fill_wait_seconds: int = 20
    exit_final_marketable: bool = True
    latency_ticks: int = 1
    plan_ttl_minutes: int = 30
    #: ``04`` §8.5: O1/O3 lose the feed only after this time; O2 after a position is this old.
    feed_watch_from: dt.time = dt.time(14, 0)
    feed_grace_seconds: int = 60


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """``04`` §9 — per-sleeve limits in R, per-trade breach, the book's derived ₹ limits."""

    daily_loss_r: Decimal = Decimal("2")
    weekly_loss_r: Decimal = Decimal("4")
    monthly_loss_r: Decimal = Decimal("8")
    budget_breach_frac: Decimal = Decimal("1.0")
    book_daily_loss_pct: Decimal = Decimal("1.5")
    book_monthly_loss_pct: Decimal = Decimal("5")


@dataclass(frozen=True, slots=True)
class OptionsConfig:
    """The whole contract. ``condor_monthly`` and ``condor_weekly`` are distinct instances."""

    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    chain: ChainConfig = field(default_factory=ChainConfig)
    condor_monthly: CondorConfig = field(default_factory=CondorConfig)
    condor_weekly: CondorConfig = field(
        default_factory=lambda: CondorConfig(
            variant=CondorVariant.WEEKLY,
            risk_per_trade_pct=Decimal("0.5"),
            max_lots=2,
            tier3_min_sessions=20,
        )
    )
    directional: DirectionalConfig = field(default_factory=DirectionalConfig)
    expiry_setups: ExpirySetupsConfig = field(default_factory=ExpirySetupsConfig)
    costs: CostRates = field(default_factory=CostRates)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    def hard_exit_time(self, sleeve: Sleeve) -> dt.time:
        """Each sleeve's hard exit (``04`` §3.2, §4.5, §5.3)."""
        group = group_of(sleeve)
        if group is SleeveGroup.O1M:
            return self.condor_monthly.hard_exit_time
        if group is SleeveGroup.O1W:
            return self.condor_weekly.hard_exit_time
        if group is SleeveGroup.O2:
            return self.directional.hard_exit_time
        return self.expiry_setups.hard_exit_time

    def risk_per_trade_pct(self, sleeve: Sleeve) -> Decimal:
        group = group_of(sleeve)
        if group is SleeveGroup.O1M:
            return self.condor_monthly.risk_per_trade_pct
        if group is SleeveGroup.O1W:
            return self.condor_weekly.risk_per_trade_pct
        if group is SleeveGroup.O2:
            return self.directional.risk_per_trade_pct
        return self.expiry_setups.risk_per_trade_pct

    def max_lots(self, sleeve: Sleeve) -> int:
        group = group_of(sleeve)
        if group is SleeveGroup.O1M:
            return self.condor_monthly.max_lots
        if group is SleeveGroup.O1W:
            return self.condor_weekly.max_lots
        if group is SleeveGroup.O2:
            return self.directional.max_lots
        return self.expiry_setups.max_lots

    def reserve_per_lot_inr(self, sleeve: Sleeve) -> Decimal:
        group = group_of(sleeve)
        if group is SleeveGroup.O1M:
            return self.condor_monthly.reserve_per_lot_inr
        if group is SleeveGroup.O1W:
            return self.condor_weekly.reserve_per_lot_inr
        if group is SleeveGroup.O2:
            return self.directional.reserve_per_lot_inr
        return self.expiry_setups.reserve_per_lot_inr

    def cost_share_max(self, sleeve: Sleeve) -> Decimal:
        group = group_of(sleeve)
        if group is SleeveGroup.O1M:
            return self.condor_monthly.cost_share_max
        if group is SleeveGroup.O1W:
            return self.condor_weekly.cost_share_max
        if group is SleeveGroup.O2:
            return self.directional.cost_share_max
        return self.expiry_setups.cost_share_max

    def tier3_min_sessions(self, sleeve: Sleeve) -> int:
        group = group_of(sleeve)
        if group is SleeveGroup.O1M:
            return self.condor_monthly.tier3_min_sessions
        if group is SleeveGroup.O1W:
            return self.condor_weekly.tier3_min_sessions
        if group is SleeveGroup.O2:
            return self.directional.tier3_min_sessions
        return self.expiry_setups.tier3_min_sessions


@dataclass(frozen=True, slots=True)
class OptionsCeilings:
    """``02`` "Ceilings" — the system-only env bounds, as the pure core receives them.

    The values in force are read from env by OP2's settings (``BASKFY_OPTIONS_*_MAX``,
    ``BASKFY_OPTIONS_HARD_EXIT_LATEST``). These are the documented defaults so a test or a
    backtest can call the core without inventing numbers; a setting may sit below them, never
    above.
    """

    risk_per_trade_inr_max: Decimal = Decimal("25000")
    risk_pct_max: Decimal = Decimal("1.0")
    max_lots_max: int = 10
    book_daily_loss_inr_max: Decimal = Decimal("30000")
    book_monthly_loss_inr_max: Decimal = Decimal("75000")
    hard_exit_latest: dt.time = dt.time(15, 0)


DEFAULT_OPTIONS_CONFIG = OptionsConfig()
DEFAULT_CEILINGS = OptionsCeilings()


def check_hard_exits(config: OptionsConfig, ceilings: OptionsCeilings) -> tuple[str, ...]:
    """Every sleeve whose hard exit is later than ``BASKFY_OPTIONS_HARD_EXIT_LATEST``.

    Track C §1: no hard exit later than 15:00. An empty tuple means the config is admissible.
    """
    return tuple(
        sleeve.value
        for sleeve in Sleeve
        if config.hard_exit_time(sleeve) > ceilings.hard_exit_latest
    )
