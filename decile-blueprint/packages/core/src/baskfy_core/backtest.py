"""The point-in-time backtest engine — docs/10-backtest-spec.md (Prompt 15 deliverables 1, 2, 7).

    "The reference product ships this in Dec 2026. We build it correctly from the start, because
     retrofitting point-in-time correctness is a rewrite."

This module is pure. Frames in, results out — no database, no clock, no network, because
docs/02 §"Repo layout" keeps ``packages/core`` I/O-free and says why: "That is what makes the
factor math unit-testable and the backtest engine reusable." ``baskfy_worker.backtest`` loads the
panel out of PostgreSQL and calls :func:`run_backtest`; nothing here knows PostgreSQL exists.

The execution model, step for step (docs/10 §"Execution model")
---------------------------------------------------------------
1. The screen is run **as of the rebalance date** ``d`` and handed to this engine as a small
   frame per rebalance date. Every read of it goes through :class:`PointInTimeReader`, which
   raises if a row carries a date later than the clock (deliverable 2).
2. Target = the top ``top_n``, widened by the **hold buffer**: a name already held is retained
   while its rank is ``<= top_n + hold_buffer``. The rule is not reimplemented here — it is
   :func:`baskfy_core.rank_buffer.plan_rebalance`, the same function the rebalance tracker calls,
   so the two surfaces cannot disagree about what "inside the buffer" means.
3. Target weights come from ``weighting``, are clipped by ``position_limits`` and renormalised.
   Clipping and renormalising fight each other, so :func:`apply_position_limits` iterates to a
   fixed point rather than clipping once and hoping.
4. Orders are executed at the **next trading day's open**, never at ``d``'s close. docs/10:
   "This one choice removes the most common source of inflated backtest returns."
5. Costs are charged on traded notional. Share counts are whole.
6. Between rebalances the book is marked to market daily on adjusted closes.
7. Corporate actions are already inside the adjusted series; the ``dividends`` policy decides
   whether cash dividends are additionally credited — see :class:`DividendPolicy`, which is where
   a subtlety in our own adjustment convention is written down.
8. A delisted holding is liquidated at its last available close and logged. It is never
   forward-filled, because that is precisely how survivorship bias gets in.

Two departures from CLAUDE.md, stated out loud
----------------------------------------------
**Prices are carried in float64 for lookup and converted to Decimal at the point of use.** House
rule 9 says money and prices are ``numeric``, never ``float``. A 2,800 x 2,000 matrix of
``Decimal`` objects is 5.6 million Python objects, which is not a thing docs/10's ten-second
budget can afford. ``ohlcv_daily`` stores prices at four decimal places (``PRICE_RAW``), and a
four-decimal value below 10^11 survives a float64 round trip exactly, so
``Decimal(f"{x:.4f}")`` recovers the stored number rather than approximating it. **Every rupee
that moves — cash, notional, cost, dividend, equity — is ``Decimal`` throughout.** Only the
lookup table is float. Recorded in ``docs/DECISIONS.md`` §15.

**Statistics are float.** Volatility, Sharpe, beta and the rest are ratios, not money; they live
in :mod:`baskfy_core.backtest_metrics` and are computed in NumPy. A Sharpe ratio expressed in
``Decimal`` would be false precision on a number whose third decimal place is noise.
"""

from __future__ import annotations

import calendar as _calendar
import datetime as dt
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Final

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

from baskfy_core.rank_buffer import HeldName, ScreenRank, plan_rebalance

__all__ = [
    "BASIS_POINTS",
    "MISSING_BAR_TOLERANCE_DAYS",
    "PRICE_EXPONENT",
    "BacktestConfig",
    "BacktestData",
    "BacktestDataError",
    "BacktestError",
    "BacktestProgress",
    "BacktestResult",
    "CashPolicy",
    "CostSpec",
    "DelistingEvent",
    "DividendPolicy",
    "FragilityReport",
    "FragilityRun",
    "HoldingSnapshot",
    "ImpactModel",
    "LookAheadError",
    "PointInTimeReader",
    "PositionLimits",
    "PricePanel",
    "ProgressSink",
    "RebalanceDay",
    "RebalanceFrequency",
    "RebalanceSpec",
    "RiskOverlay",
    "RiskRule",
    "SelectionSpec",
    "Trade",
    "TradeReason",
    "TradeSide",
    "Weighting",
    "apply_position_limits",
    "rebalance_dates",
    "run_backtest",
    "run_fragility",
]

#: ``ohlcv_daily`` prices are ``numeric(18,4)``. Four places is therefore the exact grid every
#: price in this engine sits on, and the grid a float64 lookup is snapped back onto.
PRICE_EXPONENT: Final = Decimal("0.0001")

#: Cash, notional, cost and equity are rupees.
MONEY_EXPONENT: Final = Decimal("0.01")

#: Weights are reported at the precision ``index_member_daily.weight`` uses (docs/04).
WEIGHT_EXPONENT: Final = Decimal("0.000001")

BASIS_POINTS: Final = Decimal(10_000)

#: How many consecutive trading days a held name may have no bar before the engine calls it dead.
#: The test is **backward-looking**: on day ``t`` the engine asks "has this name printed a bar in
#: the last five trading days?", which is answerable from data at ``t``. A trading halt lasts a
#: day or two; a series that has stopped for a week has stopped.
MISSING_BAR_TOLERANCE_DAYS: Final = 5

#: Calendar days in an average year, leap years included — the denominator for CAGR.
_DAYS_PER_YEAR: Final = Decimal("365.25")

#: docs/10 §Config's only overlay rule is ``index_above_200dma``, so the window is 200.
RISK_OVERLAY_WINDOW: Final = 200


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BacktestError(Exception):
    """Anything this engine refuses to do."""


class LookAheadError(BacktestError):
    """A read carried a date later than the clock (docs/10 §"Execution model" step 1).

        "No data after `d` may touch the decision. Enforced by a query-layer guard that raises if
         any read carries `date > as_of`."

    **Always on.** There is no flag that turns this off, because a backtest with the guard off is
    not a slower backtest, it is a different and wrong one.
    """

    def __init__(self, what: str, offending: dt.date, as_of: dt.date) -> None:
        super().__init__(
            f"{what} returned a row dated {offending.isoformat()}, which is after the "
            f"point-in-time cursor {as_of.isoformat()}. This is look-ahead."
        )
        self.what = what
        self.offending = offending
        self.as_of = as_of


class BacktestDataError(BacktestError):
    """The panel cannot answer something the configuration asks of it."""


class BacktestConfigError(BacktestError):
    """A configuration this engine cannot execute, caught before any simulation runs."""


# ---------------------------------------------------------------------------
# Configuration — docs/10 §Config, key for key
# ---------------------------------------------------------------------------


class RebalanceFrequency(StrEnum):
    """docs/10: ``weekly|fortnightly|monthly|quarterly``."""

    WEEKLY = "weekly"
    FORTNIGHTLY = "fortnightly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class RebalanceDay(StrEnum):
    """docs/10 names ``last_trading_day``. ``first_trading_day`` is the only other coherent
    reading of "day" for a calendar period, and costs nothing to support."""

    LAST_TRADING_DAY = "last_trading_day"
    FIRST_TRADING_DAY = "first_trading_day"


class Weighting(StrEnum):
    """docs/10: ``equal | inverse_volatility | rank | marketcap``."""

    EQUAL = "equal"
    INVERSE_VOLATILITY = "inverse_volatility"
    RANK = "rank"
    MARKETCAP = "marketcap"


class CashPolicy(StrEnum):
    """docs/10: ``hold_cash | benchmark``.

    ``benchmark`` means uninvested cash earns the benchmark's daily return — the honest reading of
    "the money you did not deploy tracked the index".
    """

    HOLD_CASH = "hold_cash"
    BENCHMARK = "benchmark"


class DividendPolicy(StrEnum):
    """docs/10 §7: ``dividends: "reinvest" | "cash" | "ignore"``.

    ## What the stored series actually is, and why this changed in M39

    This enum used to default to ``reinvest`` on the strength of docs/09's adjustment algorithm,
    which folds cash dividends into ``adj_factor`` alongside splits and bonuses
    (``D -> (P_cum - D) / P_cum``). If that ran, ``ohlcv_daily.close`` would be a *total-return*
    series and marking the book to it would be reinvestment, exactly.

    **It does not run.** M27 put the question to the reference corpus and measured the answer:
    of 45 deciding symbol-windows the price convention won 42, and it matched all 25 dividend-
    paying symbols exactly at stored precision on the three windows that reproduce. M28 then
    applied the 47 share-count actions and deliberately **not** the 38 dividend-shaped ones
    (`reconciliation/RECOVERED-ACTIONS.md`, "VERDICT: PRICE RETURN").

    So the adjusted close is a **price-return** series: splits and bonuses are inside it, cash
    dividends are not. Every policy below is defined against that fact.

    ``ignore``
        Mark to the stored series and credit nothing — a pure price return. **Exact, needs no
        extra data, and is therefore the default.** It is what the engine has always actually
        computed; until M39 it was mislabelled ``reinvest``.
    ``cash``
        Credit each dividend to cash on its ex-date. Needs a dividend schedule on the panel and
        is refused without one.
    ``reinvest``
        Would need a total-return series rebuilt from the dividend schedule. Same requirement,
        same refusal — and note it can no longer be served by doing nothing, which is what made
        the old default wrong rather than merely mislabelled: it understated every return by
        roughly the dividend yield while telling the reader dividends were included.
    """

    REINVEST = "reinvest"
    CASH = "cash"
    IGNORE = "ignore"


class ImpactModel(StrEnum):
    """docs/10 §Config: ``"impact_model": "fixed"``. Only the one the document names."""

    FIXED = "fixed"


class RiskRule(StrEnum):
    """docs/10 §Config: ``"rule": "index_above_200dma"``."""

    INDEX_ABOVE_200DMA = "index_above_200dma"


class TradeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class TradeReason(StrEnum):
    """docs/10 §Artefacts: "reason in ``enter|exit|rebalance|delist``"."""

    ENTER = "enter"
    EXIT = "exit"
    REBALANCE = "rebalance"
    DELIST = "delist"


class _Spec(BaseModel):
    """Every configuration block forbids unknown keys, exactly as ``ScreenDefinition`` does."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RebalanceSpec(_Spec):
    frequency: RebalanceFrequency = RebalanceFrequency.MONTHLY
    day: RebalanceDay = RebalanceDay.LAST_TRADING_DAY


class SelectionSpec(_Spec):
    """docs/10: ``{"top_n": 20, "hold_buffer": 10}``; "0 = strict top-N"."""

    top_n: int = Field(default=20, ge=1, le=500)
    hold_buffer: int = Field(default=10, ge=0, le=500)


class PositionLimits(_Spec):
    """docs/10: ``{"max_weight": 0.10, "min_weight": 0.01}``."""

    max_weight: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    min_weight: Decimal = Field(default=Decimal("0.01"), ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> PositionLimits:
        if self.min_weight > self.max_weight:
            raise ValueError(f"min_weight {self.min_weight} is above max_weight {self.max_weight}")
        return self


class CostSpec(_Spec):
    """docs/10: brokerage 3 bps, STT 10 bps, slippage 15 bps, ``impact_model: "fixed"``.

    All three are charged on **both** sides of a trade. In India STT is levied on the buy and the
    sell leg of a delivery trade, brokerage is per order, and slippage is a property of crossing
    the spread in either direction — so "costs on traded notional" is charged on traded notional,
    whichever way it was traded.
    """

    brokerage_bps: Decimal = Field(default=Decimal(3), ge=0, le=1000)
    stt_bps: Decimal = Field(default=Decimal(10), ge=0, le=1000)
    slippage_bps: Decimal = Field(default=Decimal(15), ge=0, le=1000)
    impact_model: ImpactModel = ImpactModel.FIXED

    @property
    def total_bps(self) -> Decimal:
        return self.brokerage_bps + self.stt_bps + self.slippage_bps

    def scaled(self, factor: Decimal) -> CostSpec:
        """The same cost stack, multiplied — docs/10 §"honesty features" asks for +/-25%."""
        return CostSpec(
            brokerage_bps=self.brokerage_bps * factor,
            stt_bps=self.stt_bps * factor,
            slippage_bps=self.slippage_bps * factor,
            impact_model=self.impact_model,
        )

    def charge(self, notional: Decimal) -> Decimal:
        """Cost on one traded notional, rounded to the paisa."""
        return _money(abs(notional) * self.total_bps / BASIS_POINTS)


class RiskOverlay(_Spec):
    """docs/10: ``{"enabled": false, "rule": "index_above_200dma"}``."""

    enabled: bool = False
    rule: RiskRule = RiskRule.INDEX_ABOVE_200DMA


class BacktestConfig(BaseModel):
    """docs/10 §Config, key for key, plus the two the document mentions only in prose.

    ``dividends`` appears in §"Execution model" step 7 rather than in the config block, and
    ``risk_free_rate`` is required by §Outputs ("Sharpe (rf from a configurable T-bill series)").
    ``docs/04`` has no T-bill table; the bundled OECD IR3TIB series is attached at execute time
    (T9.5) onto ``risk_free_curve``. The flat rate is the fallback when the series does not
    overlap the window. See ``docs/DECISIONS.md`` §15.5.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: docs/10: "screen_public_id … or an inline definition". The engine itself needs neither —
    #: it is handed the screen's *output* per rebalance date — so this is carried for the record
    #: and resolved by the caller.
    screen_public_id: str | None = None
    screen_definition: dict[str, object] | None = None

    start: dt.date
    end: dt.date
    initial_capital: Decimal = Field(default=Decimal(1_000_000), gt=0)

    rebalance: RebalanceSpec = RebalanceSpec()
    selection: SelectionSpec = SelectionSpec()
    weighting: Weighting = Weighting.EQUAL
    position_limits: PositionLimits = PositionLimits()
    costs: CostSpec = CostSpec()
    cash_policy: CashPolicy = CashPolicy.HOLD_CASH
    benchmark: str = "nifty-500"
    risk_overlay: RiskOverlay = RiskOverlay()
    dividends: DividendPolicy = DividendPolicy.IGNORE
    #: Annualised, as a decimal fraction. Zero remains the fallback when no overlapping
    #: T-bill observation exists (see ``risk_free_curve`` and ``baskfy_core.risk_free``).
    risk_free_rate: Decimal = Field(default=Decimal(0), ge=0, le=1)
    #: Monthly (date, annual rate as a fraction) observations, forward-filled onto each
    #: return day. Empty means "use ``risk_free_rate`` only". Worker backtests attach the
    #: bundled OECD IR3TIB series via :func:`baskfy_core.risk_free.attach_tbill_curve`.
    #:
    #: The JSON schema is overridden to a plain array-of-arrays rather than the ``prefixItems``
    #: tuple Pydantic would emit. Validation is unchanged — this is the *documented* shape only.
    #: openapi-typescript turns ``prefixItems`` into a TypeScript tuple, and the value openapi-fetch
    #: hands a caller is structurally widened to ``string[][]``, so the generated client could not
    #: assign its own response to its own ``BacktestOut``: the web build failed on
    #: ``Type 'string[][]' is not assignable to type '[string, string][]'``. A pair is honestly an
    #: array of two, and the length is enforced by the model, not by the document.
    #: ``docs/DECISIONS-MERGE.md`` M40.4.
    risk_free_curve: Annotated[
        tuple[tuple[dt.date, Decimal], ...],
        WithJsonSchema(
            {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
                "title": "Risk Free Curve",
            }
        ),
    ] = ()

    @model_validator(mode="after")
    def _dates_ordered(self) -> BacktestConfig:
        if self.end <= self.start:
            raise ValueError(f"end {self.end} must be after start {self.start}")
        return self

    def with_costs(self, costs: CostSpec) -> BacktestConfig:
        return self.model_copy(update={"costs": costs})


# ---------------------------------------------------------------------------
# The rebalance calendar
# ---------------------------------------------------------------------------


def _period_key(day: dt.date, frequency: RebalanceFrequency) -> tuple[int, int]:
    if frequency is RebalanceFrequency.MONTHLY:
        return (day.year, day.month)
    if frequency is RebalanceFrequency.QUARTERLY:
        return (day.year, (day.month - 1) // 3)
    iso = day.isocalendar()
    if frequency is RebalanceFrequency.WEEKLY:
        return (iso.year, iso.week)
    # Fortnightly: pair adjacent ISO weeks. Anchored on the ISO week number rather than on the
    # start date so the schedule is a property of the calendar, not of when someone pressed run.
    return (iso.year, iso.week // 2)


def rebalance_dates(
    calendar: Sequence[dt.date],
    start: dt.date,
    end: dt.date,
    spec: RebalanceSpec,
) -> tuple[dt.date, ...]:
    """The dates the screen is run on, inside ``[start, end]``.

    The first trading day on or after ``start`` is **always** a rebalance date. docs/10 gives a
    frequency and a day-of-period but no rule for the stub period at the front, and the
    alternative — waiting until the end of the first month — leaves the whole initial capital in
    cash for up to a month and quietly changes the answer for any short backtest.
    """
    window = [day for day in calendar if start <= day <= end]
    if not window:
        return ()

    chosen: dict[tuple[int, int], dt.date] = {}
    for day in window:
        key = _period_key(day, spec.frequency)
        if spec.day is RebalanceDay.LAST_TRADING_DAY:
            chosen[key] = day
        else:
            chosen.setdefault(key, day)

    dates = set(chosen.values())
    dates.add(window[0])
    return tuple(sorted(dates))


def shift_schedule(
    calendar: Sequence[dt.date], dates: Iterable[dt.date], offset: int
) -> tuple[dt.date, ...]:
    """Each rebalance date moved ``offset`` trading days — docs/10's ``+/-1`` fragility probe.

    A date that would fall off either end of the calendar is dropped rather than clamped: two
    rebalances landing on the same day would change the schedule's shape, not shift it.
    """
    index = {day: position for position, day in enumerate(calendar)}
    moved: list[dt.date] = []
    for day in dates:
        position = index.get(day)
        if position is None:
            continue
        target = position + offset
        if 0 <= target < len(calendar):
            moved.append(calendar[target])
    return tuple(sorted(set(moved)))


# ---------------------------------------------------------------------------
# Weighting
# ---------------------------------------------------------------------------


def apply_position_limits(
    weights: Mapping[int, Decimal], limits: PositionLimits
) -> dict[int, Decimal]:
    """Clip to ``[min_weight, max_weight]`` and renormalise to exactly 1.

    Clipping and renormalising pull against each other: scaling the survivors back up to sum to
    one can push a name straight back through the cap it was just clipped to. So this
    water-fills — fix the names that breach a bound, redistribute what is left over the rest in
    proportion, and repeat until nothing breaches. It terminates because every pass fixes at
    least one name and a fixed name is never freed.

    **Ceilings are resolved before floors.** Capping the largest name releases weight the smaller
    names then receive, so a name that looked to be under the floor on the first pass very often
    is not once the cap has been applied. Fixing it at the floor immediately — which an
    implementation that treats both bounds in one pass does — spends weight that has not been
    allocated yet, and the column comes out wrong.

    **Infeasibility is resolved, not raised.** ``n`` names cannot all sit above ``min_weight`` if
    ``n * min_weight > 1``, and cannot sum to one if ``n * max_weight < 1``. Rather than refuse a
    portfolio the user can see on the screen, the offending bound is relaxed to ``1/n`` — which is
    equal weight, the only allocation that satisfies a bound it cannot otherwise meet.
    """
    if not weights:
        return {}
    count = len(weights)
    even = Decimal(1) / Decimal(count)
    ceiling = max(limits.max_weight, even)
    floor = min(limits.min_weight, even)

    fixed: dict[int, Decimal] = {}
    free = dict(weights)
    for _ in range(count + 1):
        if not free:
            return _normalised(fixed, ceiling)
        remaining = Decimal(1) - sum(fixed.values(), Decimal(0))
        total = sum(free.values(), Decimal(0))
        if total <= 0:
            share = remaining / Decimal(len(free))
            scaled = {key: share for key in free}
        else:
            scaled = {key: value / total * remaining for key, value in free.items()}

        above = {key for key, value in scaled.items() if value > ceiling}
        if above:
            fixed.update({key: ceiling for key in above})
            free = {key: value for key, value in free.items() if key not in above}
            continue
        below = {key for key, value in scaled.items() if value < floor}
        if below:
            fixed.update({key: floor for key in below})
            free = {key: value for key, value in free.items() if key not in below}
            continue
        return _normalised({**fixed, **scaled}, ceiling)
    return _normalised({**fixed, **free}, ceiling)  # pragma: no cover - the loop converges


def _normalised(weights: Mapping[int, Decimal], ceiling: Decimal) -> dict[int, Decimal]:
    """Quantise to six places and place the remainder, so the column adds to exactly 1.

    The remainder goes to the largest name that still has room under ``ceiling``; handing it to
    the largest name unconditionally would let rounding push a capped position a few millionths
    through its own cap, which is a rule violation nobody would ever find.
    """
    if not weights:
        return {}
    rounded = {
        key: value.quantize(WEIGHT_EXPONENT, rounding=ROUND_DOWN) for key, value in weights.items()
    }
    remainder = Decimal(1) - sum(rounded.values(), Decimal(0))
    if remainder == 0:
        return rounded
    room = [key for key in rounded if rounded[key] + remainder <= ceiling]
    anchor = (
        max(room, key=lambda key: (rounded[key], -key))
        if room
        else max(rounded, key=lambda key: (rounded[key], -key))
    )
    rounded[anchor] += remainder
    return rounded


def raw_weights(scheme: Weighting, rows: Sequence[_Selected]) -> dict[int, Decimal]:
    """The pre-clip weights docs/10 §Config's ``weighting`` names."""
    if scheme is Weighting.EQUAL:
        return {row.instrument_id: Decimal(1) for row in rows}
    if scheme is Weighting.RANK:
        # Linearly decreasing in rank *within the selected set*: the best name gets n, the worst
        # gets 1. Using the screen's absolute rank would make a portfolio drawn from ranks 400-420
        # very nearly equal-weighted, which is not what "rank weighting" means to anyone.
        order = sorted(rows, key=lambda row: (row.rank, row.instrument_id))
        count = len(order)
        return {row.instrument_id: Decimal(count - position) for position, row in enumerate(order)}
    if scheme is Weighting.MARKETCAP:
        return {row.instrument_id: _positive(row.marketcap_cr, row, "marketcap_cr") for row in rows}
    volatilities = {row.instrument_id: _positive(row.volatility, row, "vol_12m") for row in rows}
    return {key: Decimal(1) / value for key, value in volatilities.items()}


def _positive(value: Decimal | None, row: _Selected, column: str) -> Decimal:
    if value is None or value <= 0:
        raise BacktestDataError(
            f"{column} is {value!r} for instrument {row.instrument_id} on rank {row.rank}; "
            "this weighting scheme cannot use it. Choose 'equal' or fix the screen's projection."
        )
    return value


# ---------------------------------------------------------------------------
# The panel, and the guard (deliverable 2)
# ---------------------------------------------------------------------------


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_EXPONENT, rounding=ROUND_HALF_UP)


def _price(value: float) -> Decimal:
    """Snap a float64 lookup back onto the four-decimal grid ``ohlcv_daily`` stores.

    Lossless for any value the database can hold: a ``numeric(18,4)`` below 10^11 is exactly
    representable in float64, so formatting to four places recovers the stored digits rather than
    inventing them.
    """
    return Decimal(f"{value:.4f}")


@dataclass(frozen=True, slots=True)
class _Selected:
    """One name the screen selected at a rebalance date, with what the weighting needs."""

    instrument_id: int
    symbol: str
    name: str
    rank: int
    marketcap_cr: Decimal | None = None
    volatility: Decimal | None = None


REQUIRED_PRICE_COLUMNS: Final[tuple[str, ...]] = ("date", "instrument_id", "open", "close")


class PricePanel:
    """Adjusted daily bars, pivoted into dense arrays for O(1) lookup.

    docs/10 §Performance: "run the portfolio simulation as an array walk over ~2,800 trading
    days". The array is built once, in Polars, and indexed by ``(day, instrument)`` thereafter.
    Missing bars are ``NaN`` — never zero, and never forward-filled.
    """

    def __init__(
        self,
        frame: pl.DataFrame,
        calendar: Sequence[dt.date],
        *,
        price_only: bool = False,
    ) -> None:
        missing = [name for name in REQUIRED_PRICE_COLUMNS if name not in frame.columns]
        if missing:
            raise BacktestDataError(f"the price panel is missing column(s) {missing}")
        #: Kept for callers that still pass it, and it no longer gates anything.
        #:
        #: The flag meant "these arrays are dividend-stripped rather than total-return". Since
        #: M28 established the stored close is a price-return series, that is true of **every**
        #: panel, so a flag distinguishing the two describes a distinction that no longer exists.
        #: The dividend policy is checked against `BacktestData.dividends` instead — whether a
        #: dividend schedule was supplied is the thing that actually varies. See DividendPolicy.
        self.is_price_only: Final[bool] = price_only
        self.calendar: Final[tuple[dt.date, ...]] = tuple(calendar)
        self._day_index: Final[dict[dt.date, int]] = {
            day: position for position, day in enumerate(self.calendar)
        }
        ids = frame.get_column("instrument_id").unique().sort().to_list()
        self.instrument_ids: Final[tuple[int, ...]] = tuple(int(value) for value in ids)
        self._column: Final[dict[int, int]] = {
            value: position for position, value in enumerate(self.instrument_ids)
        }

        open_column = "price_open" if price_only else "open"
        close_column = "price_close" if price_only else "close"
        rows = frame.select(
            pl.col("date"),
            pl.col("instrument_id").cast(pl.Int64),
            pl.col(open_column).cast(pl.Float64).alias("_open"),
            pl.col(close_column).cast(pl.Float64).alias("_close"),
        )
        shape = (len(self.calendar), len(self.instrument_ids))
        self._open = np.full(shape, np.nan, dtype=np.float64)
        self._close = np.full(shape, np.nan, dtype=np.float64)
        if rows.height:
            day_positions = np.array(
                [self._day_index.get(value, -1) for value in rows.get_column("date").to_list()],
                dtype=np.int64,
            )
            unknown = int((day_positions < 0).sum())
            if unknown:
                raise BacktestDataError(
                    f"{unknown} bar(s) fall on dates that are not in the trading calendar"
                )
            instrument_positions = np.array(
                [self._column[int(value)] for value in rows.get_column("instrument_id").to_list()],
                dtype=np.int64,
            )
            self._open[day_positions, instrument_positions] = rows.get_column("_open").to_numpy()
            self._close[day_positions, instrument_positions] = rows.get_column("_close").to_numpy()

        present = ~np.isnan(self._close)
        # The last day each series prints a bar. A property of the panel, not of any decision: it
        # schedules liquidations (see `liquidation_day`), and a liquidation always settles at a
        # price on or before the day it happens.
        self._last_row = np.where(present.any(axis=0), _last_true(present), -1)

    def index_of(self, day: dt.date) -> int:
        position = self._day_index.get(day)
        if position is None:
            raise BacktestDataError(f"{day.isoformat()} is not a trading day in this calendar")
        return position

    def close_on(self, instrument_id: int, day_position: int) -> Decimal | None:
        column = self._column.get(instrument_id)
        if column is None:
            return None
        value = self._close[day_position, column]
        return None if math.isnan(value) else _price(float(value))

    def open_on(self, instrument_id: int, day_position: int) -> Decimal | None:
        column = self._column.get(instrument_id)
        if column is None:
            return None
        value = self._open[day_position, column]
        return None if math.isnan(value) else _price(float(value))

    def last_close_at_or_before(
        self, instrument_id: int, day_position: int
    ) -> tuple[Decimal, int] | None:
        """The most recent printed close on or before ``day_position``, and which day it was."""
        column = self._column.get(instrument_id)
        if column is None:
            return None
        window = self._close[: day_position + 1, column]
        printed = np.flatnonzero(~np.isnan(window))
        if printed.size == 0:
            return None
        position = int(printed[-1])
        return _price(float(window[position])), position

    def liquidation_day(self, instrument_id: int, delisted_on: dt.date | None) -> int | None:
        """The day index a held position in ``instrument_id`` must be closed out, if any.

        Two triggers, whichever comes first:

        * ``instrument.delisted_on`` — a fact the reference data carries, effective from that day.
        * the series going quiet: no bar for :data:`MISSING_BAR_TOLERANCE_DAYS` consecutive
          trading days. Stated as a *backward*-looking question ("has it printed in the last five
          days?"), which is answerable on the day it is asked; the answer flips on exactly one
          day, so it is computed once here rather than re-asked 2,800 times.
        """
        column = self._column.get(instrument_id)
        candidates: list[int] = []
        if delisted_on is not None:
            position = next(
                (index for index, day in enumerate(self.calendar) if day >= delisted_on), None
            )
            if position is not None:
                candidates.append(position)
        if column is not None:
            last = int(self._last_row[column])
            if 0 <= last < len(self.calendar) - MISSING_BAR_TOLERANCE_DAYS:
                candidates.append(last + MISSING_BAR_TOLERANCE_DAYS + 1)
        return min(candidates) if candidates else None


def _last_true(mask: np.ndarray) -> np.ndarray:
    """Row index of the last ``True`` in each column."""
    reversed_first: np.ndarray = mask[::-1].argmax(axis=0)
    last: np.ndarray = mask.shape[0] - 1 - reversed_first
    return last


@dataclass(frozen=True, slots=True)
class BacktestData:
    """Everything the engine reads. Assembled by the caller; never fetched from here.

    ``screens`` maps a rebalance date to that date's screen output. Every frame must carry a
    ``date`` column — that column is what the look-ahead guard checks, and a frame without one is
    refused, because a guard that cannot see a date cannot guard anything.
    """

    calendar: tuple[dt.date, ...]
    prices: PricePanel
    screens: Mapping[dt.date, pl.DataFrame]
    #: ``date``, ``level`` — the benchmark docs/10 §Config names. Optional: the metrics that need
    #: it (alpha, beta, tracking error, information ratio) are reported as ``None`` without one.
    benchmark: pl.DataFrame | None = None
    #: ``instrument_id`` -> ``instrument.delisted_on``.
    delistings: Mapping[int, dt.date] = field(default_factory=dict)
    #: ``instrument_id`` -> symbol/name, for the trade log and the holdings artefact.
    symbols: Mapping[int, str] = field(default_factory=dict)
    names: Mapping[int, str] = field(default_factory=dict)
    #: ``date``, ``instrument_id``, ``amount`` — per-share cash dividends, in the units of the
    #: dividend-stripped price series. Only read under ``DividendPolicy.CASH``.
    dividends: pl.DataFrame | None = None
    #: The published ``pipeline_run.data_version`` behind the panel. Carried into the metrics hash
    #: so docs/10's determinism test ("same config + same `data_version`") means what it says.
    data_version: int | None = None

    def symbol_for(self, instrument_id: int) -> str:
        return self.symbols.get(instrument_id, f"#{instrument_id}")

    def name_for(self, instrument_id: int) -> str:
        return self.names.get(instrument_id, self.symbol_for(instrument_id))


class PointInTimeReader:
    """The guard (deliverable 2). Every read of dated data in this engine goes through it.

        "any read whose date exceeds the current as_of raises. This guard is always on in
         backtests, not a debug flag." — PROMPTS.md Prompt 15 §2

    It checks the **data**, not the request. Asking for the screen "as of 2015-01-30" and being
    handed a frame whose rows are stamped 2015-01-31 is exactly the bug docs/10 §"Correctness
    harness" test 1 injects — a factor column shifted forward one day — and a guard that only
    validated the requested key would wave it straight through.
    """

    def __init__(self, data: BacktestData) -> None:
        self._data = data
        self._as_of: dt.date | None = None

    @property
    def as_of(self) -> dt.date:
        if self._as_of is None:
            raise BacktestError("the point-in-time cursor has not been set")
        return self._as_of

    def advance_to(self, day: dt.date) -> None:
        """Move the clock forward. It never moves back — time does not, and neither may a walk."""
        if self._as_of is not None and day < self._as_of:
            raise BacktestError(
                f"the point-in-time cursor cannot move back from {self._as_of} to {day}"
            )
        self._as_of = day

    def _guard(self, frame: pl.DataFrame, what: str) -> pl.DataFrame:
        if "date" not in frame.columns:
            raise BacktestDataError(
                f"{what} has no 'date' column, so the look-ahead guard cannot check it"
            )
        if frame.height:
            latest = frame.get_column("date").max()
            if isinstance(latest, dt.datetime):
                latest = latest.date()
            if isinstance(latest, dt.date) and latest > self.as_of:
                raise LookAheadError(what, latest, self.as_of)
        return frame

    def screen_on(self, day: dt.date) -> pl.DataFrame:
        """The screen's output as of ``day``, guarded."""
        frame = self._data.screens.get(day)
        if frame is None:
            raise BacktestDataError(
                f"no screen result was precomputed for rebalance date {day.isoformat()}"
            )
        return self._guard(frame, f"screen result for {day.isoformat()}")

    def benchmark_to(self, day: dt.date) -> pl.DataFrame:
        """The benchmark's level series up to and including ``day``, guarded."""
        frame = self._data.benchmark
        if frame is None:
            raise BacktestDataError("this run needs a benchmark series and none was supplied")
        window = frame.filter(pl.col("date") <= day)
        return self._guard(window, "benchmark series")

    def dividends_on(self, day: dt.date) -> pl.DataFrame:
        frame = self._data.dividends
        if frame is None:
            raise BacktestDataError(
                "the 'cash' dividend policy needs a dividend schedule and none was supplied"
            )
        return self._guard(frame.filter(pl.col("date") == day), "dividend schedule")

    def price_close(self, instrument_id: int, day: dt.date) -> Decimal | None:
        self._require_not_future(day, "close")
        return self._data.prices.close_on(instrument_id, self._data.prices.index_of(day))

    def price_open(self, instrument_id: int, day: dt.date) -> Decimal | None:
        self._require_not_future(day, "open")
        return self._data.prices.open_on(instrument_id, self._data.prices.index_of(day))

    def last_close(self, instrument_id: int, day: dt.date) -> tuple[Decimal, dt.date] | None:
        self._require_not_future(day, "last close")
        found = self._data.prices.last_close_at_or_before(
            instrument_id, self._data.prices.index_of(day)
        )
        if found is None:
            return None
        price, position = found
        return price, self._data.prices.calendar[position]

    def _require_not_future(self, day: dt.date, what: str) -> None:
        if day > self.as_of:
            raise LookAheadError(f"{what} price", day, self.as_of)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Trade:
    """One fill. docs/10 §Artefacts: "date, symbol, side, qty, price, cost, reason"."""

    date: dt.date
    instrument_id: int
    symbol: str
    side: TradeSide
    quantity: int
    price: Decimal
    notional: Decimal
    cost: Decimal
    reason: TradeReason
    #: Realised profit on the shares this fill closed out, net of the cost of this fill. ``None``
    #: on a buy — nothing was closed.
    realised_pnl: Decimal | None = None

    def as_row(self) -> dict[str, object]:
        return {
            "date": self.date.isoformat(),
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": str(self.price),
            "notional": str(self.notional),
            "cost": str(self.cost),
            "reason": self.reason.value,
            "realised_pnl": None if self.realised_pnl is None else str(self.realised_pnl),
        }


@dataclass(frozen=True, slots=True)
class HoldingSnapshot:
    """What was held after one rebalance was executed — docs/10's "per-rebalance holdings"."""

    rebalance_date: dt.date
    executed_on: dt.date
    instrument_id: int
    symbol: str
    name: str
    rank: int | None
    target_weight: Decimal
    quantity: int
    price: Decimal
    value: Decimal
    actual_weight: Decimal

    def as_row(self) -> dict[str, object]:
        return {
            "rebalance_date": self.rebalance_date.isoformat(),
            "executed_on": self.executed_on.isoformat(),
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "name": self.name,
            "rank": self.rank,
            "target_weight": str(self.target_weight),
            "quantity": self.quantity,
            "price": str(self.price),
            "value": str(self.value),
            "actual_weight": str(self.actual_weight),
        }


@dataclass(frozen=True, slots=True)
class DelistingEvent:
    """A position closed because its instrument stopped trading. docs/10 §8: "log the event"."""

    date: dt.date
    instrument_id: int
    symbol: str
    quantity: int
    price: Decimal
    priced_on: dt.date
    proceeds: Decimal


@dataclass(frozen=True, slots=True)
class BacktestProgress:
    """One SSE frame's worth of state (Prompt 15 §4)."""

    stage: str
    completed: int
    total: int
    as_of: dt.date | None = None

    @property
    def percent(self) -> int:
        return 0 if self.total <= 0 else min(100, round(100 * self.completed / self.total))


ProgressSink = Callable[[BacktestProgress], None]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Everything docs/10 §Outputs asks for, before anything decides where to store it."""

    config: BacktestConfig
    dates: tuple[dt.date, ...]
    equity: tuple[Decimal, ...]
    cash: tuple[Decimal, ...]
    invested: tuple[Decimal, ...]
    benchmark: tuple[Decimal | None, ...]
    trades: tuple[Trade, ...]
    holdings: tuple[HoldingSnapshot, ...]
    delistings: tuple[DelistingEvent, ...]
    total_costs: Decimal
    dividends_credited: Decimal
    rebalance_dates: tuple[dt.date, ...]
    #: Rebalance dates whose screen returned no rows at all (M39).
    #:
    #: A screen selecting nothing is a legitimate outcome — every filter can exclude every name —
    #: and the engine handles it by going to cash. But it is *also* what a missing factor row
    #: looks like from in here, and those two produce the same equity curve while meaning
    #: completely different things. The engine cannot tell them apart; it can count them, so the
    #: caller that does know can decide whether the run means anything.
    blind_rebalances: tuple[dt.date, ...] = ()
    #: Fill days where a decision was taken and **nothing at all** could be traded (M45).
    #:
    #: Distinct from `blind_rebalances`, which is about the *screen* returning nothing. Here the
    #: screen was full and the market was not open enough to act on it — NSE's Muhurat and
    #: Budget-day sessions print 1-18% of a normal day's instruments. Before M45 this was a bare
    #: `continue`: a complete 30-name decision could execute none of itself and every honesty
    #: counter on the result would still read clean.
    unexecuted_fills: tuple[dt.date, ...] = ()
    notes: tuple[str, ...] = ()
    data_version: int | None = None

    @property
    def final_equity(self) -> Decimal:
        return self.equity[-1] if self.equity else self.config.initial_capital

    @property
    def blind_fraction(self) -> Decimal:
        """What share of rebalances decided with an empty screen. 0 when there were none."""
        if not self.rebalance_dates:
            return Decimal(0)
        return Decimal(len(self.blind_rebalances)) / Decimal(len(self.rebalance_dates))


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Position:
    quantity: int = 0
    #: Total rupees paid for the shares currently held, cost included. The basis for realised P&L.
    book_cost: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class _Decision:
    """What one rebalance date decided, waiting to be filled at the next open.

    Weights and ranks travel with the decision rather than being recomputed at the fill, because
    the fill happens on a different day and recomputing anything there would be reading data the
    decision was not allowed to see.
    """

    decided_on: dt.date
    targets: Mapping[int, Decimal]
    ranks: Mapping[int, int]
    reasons: Mapping[int, TradeReason]

    @property
    def instruments(self) -> tuple[int, ...]:
        """Every name the fill has to touch, best-ranked first.

        Order is load-bearing on the buy pass: when rounding leaves the book a few rupees short,
        the name that misses out should be the worst-ranked one, not whichever id sorted first.
        """
        return tuple(
            sorted(
                set(self.targets) | set(self.reasons),
                key=lambda key: (self.ranks.get(key, 10**9), key),
            )
        )


def run_backtest(  # noqa: PLR0912, PLR0915 - the execution model is a sequence; splitting it hides it
    config: BacktestConfig,
    data: BacktestData,
    *,
    progress: ProgressSink | None = None,
    schedule: Sequence[dt.date] | None = None,
) -> BacktestResult:
    """docs/10 §"Execution model", steps 1-8, in order.

    ``schedule`` overrides the computed rebalance calendar — that is how the fragility readout
    shifts every rebalance by one trading day without pretending the configuration changed.
    """
    if config.dividends is not DividendPolicy.IGNORE and data.dividends is None:
        raise BacktestConfigError(
            f"the {config.dividends.value!r} dividend policy needs a dividend schedule and none "
            "was supplied. The stored close is a PRICE-return series — M28 applied splits and "
            "bonuses and deliberately not the 38 dividend-shaped actions — so dividends cannot "
            "be assumed to be inside it. See DividendPolicy."
        )

    blind: list[dt.date] = []
    #: Fill days on which a decision existed and produced not one trade — see `_execute`.
    unexecuted: list[dt.date] = []
    reader = PointInTimeReader(data)
    panel = data.prices
    days = [day for day in data.calendar if config.start <= day <= config.end]
    if not days:
        raise BacktestDataError(
            f"the trading calendar holds no day between {config.start} and {config.end}"
        )

    dates = (
        tuple(schedule)
        if schedule is not None
        else rebalance_dates(data.calendar, config.start, config.end, config.rebalance)
    )
    if not dates:
        raise BacktestDataError("the configuration produces no rebalance dates")

    # Step 4: an order decided on `d` is filled on the next trading day. A decision taken on the
    # final day of the run is never filled — there is no next day inside the window — and is
    # dropped rather than executed at `d`'s close, which is the shortcut docs/10 exists to forbid.
    calendar_index = {day: position for position, day in enumerate(data.calendar)}
    fill_for: dict[dt.date, dt.date] = {}
    for decided in dates:
        position = calendar_index.get(decided)
        if position is None:
            raise BacktestDataError(
                f"rebalance date {decided.isoformat()} is not in the trading calendar"
            )
        if position + 1 < len(data.calendar) and data.calendar[position + 1] <= config.end:
            fill_for[decided] = data.calendar[position + 1]

    positions: dict[int, _Position] = {}
    cash = _money(config.initial_capital)
    trades: list[Trade] = []
    holdings: list[HoldingSnapshot] = []
    delistings: list[DelistingEvent] = []
    notes: list[str] = []
    total_costs = Decimal(0)
    dividends_credited = Decimal(0)

    equity_series: list[Decimal] = []
    cash_series: list[Decimal] = []
    invested_series: list[Decimal] = []
    benchmark_series: list[Decimal | None] = []

    pending: _Decision | None = None
    fills_due: dict[dt.date, dt.date] = {fill: decided for decided, fill in fill_for.items()}
    liquidation: dict[int, int] = {}
    benchmark_levels = _benchmark_levels(data)
    total_steps = len(days)

    for step, day in enumerate(days):
        reader.advance_to(day)
        position_index = panel.index_of(day)

        # --- Step 4: fill yesterday's decision at today's open ----------------
        if pending is not None and day in fills_due:
            cash, filled, unfillable = _execute(
                pending, day, position_index, panel, positions, cash, config, data
            )
            for trade in filled:
                trades.append(trade)
                total_costs += trade.cost
            if unfillable:
                # A thin session is not a strategy outcome and must not read like one. When a
                # rebalance can fill nothing at all, the day is recorded as unexecuted so the
                # result carries it rather than looking like a deliberate hold.
                shown = sorted(data.symbol_for(key) for key in unfillable)
                names = ", ".join(shown[:_SYMBOLS_IN_NOTE])
                more = (
                    f" and {len(shown) - _SYMBOLS_IN_NOTE} more"
                    if len(shown) > _SYMBOLS_IN_NOTE
                    else ""
                )
                notes.append(
                    f"{len(unfillable)} of the {len(pending.instruments)} names decided on "
                    f"{pending.decided_on.isoformat()} had no opening price on "
                    f"{day.isoformat()} and could not be filled ({names}{more}). "
                    + (
                        "NOTHING was filled that day; the book is unchanged."
                        if not filled
                        else "The rest were filled."
                    )
                )
                if not filled:
                    unexecuted.append(day)
            holdings.extend(_snapshot(pending, day, positions, panel, position_index, data, cash))
            pending = None

        # --- Step 8: delisting ------------------------------------------------
        for instrument_id in sorted(positions):
            if positions[instrument_id].quantity <= 0:  # pragma: no cover - `_fill` pops these
                continue
            if instrument_id not in liquidation:
                trigger = panel.liquidation_day(instrument_id, data.delistings.get(instrument_id))
                liquidation[instrument_id] = -1 if trigger is None else trigger
            trigger = liquidation[instrument_id]
            if trigger < 0 or position_index < trigger:
                continue
            event, cash, liquidated = _liquidate(
                reader, instrument_id, day, positions, cash, config, data
            )
            if event is None or liquidated is None:
                notes.append(
                    f"{data.symbol_for(instrument_id)} stopped trading before "
                    f"{day.isoformat()} and never printed a close; the position was written off."
                )
                positions.pop(instrument_id, None)
                continue
            delistings.append(event)
            trades.append(liquidated)
            total_costs += liquidated.cost

        # --- Step 7: cash dividends, when the policy credits them --------------
        if config.dividends is DividendPolicy.CASH:
            credited = _credit_dividends(reader, day, positions)
            cash = _money(cash + credited)
            dividends_credited += credited

        # --- Uninvested cash, under `cash_policy` ------------------------------
        if config.cash_policy is CashPolicy.BENCHMARK and step > 0:
            cash = _money(cash * _benchmark_growth(benchmark_levels, days[step - 1], day))

        # --- Steps 1-3: the decision, taken on today's close --------------------
        if day in fill_for:
            pending, decision_notes, screened = _decide(
                reader, day, positions, config, data, benchmark_levels
            )
            notes.extend(decision_notes)
            if screened == 0:
                blind.append(day)

        # --- Step 6: mark to market ---------------------------------------------
        invested = _market_value(positions, panel, position_index)
        equity_series.append(_money(cash + invested))
        cash_series.append(cash)
        invested_series.append(invested)
        benchmark_series.append(benchmark_levels.get(day))

        if progress is not None and (step % _PROGRESS_EVERY == 0 or step == total_steps - 1):
            progress(BacktestProgress("simulating", step + 1, total_steps, day))

    return BacktestResult(
        config=config,
        dates=tuple(days),
        equity=tuple(equity_series),
        cash=tuple(cash_series),
        invested=tuple(invested_series),
        benchmark=tuple(benchmark_series),
        trades=tuple(trades),
        holdings=tuple(holdings),
        delistings=tuple(delistings),
        total_costs=total_costs,
        dividends_credited=dividends_credited,
        rebalance_dates=tuple(dates),
        blind_rebalances=tuple(blind),
        unexecuted_fills=tuple(unexecuted),
        notes=tuple(dict.fromkeys(notes)),
        data_version=data.data_version,
    )


#: How often a progress event is emitted during the day walk. 250 trading days is about a year,
#: which is roughly fifteen frames over docs/10's headline run — often enough for a progress bar
#: to move, rare enough that publishing them is not the expensive part of the backtest.
_PROGRESS_EVERY: Final = 250

#: How many symbols an unfillable-day note names before summarising. Enough to recognise the
#: session, few enough that a thin day does not produce a note listing two thousand tickers.
_SYMBOLS_IN_NOTE: Final = 6


def _benchmark_levels(data: BacktestData) -> dict[dt.date, Decimal]:
    frame = data.benchmark
    if frame is None:
        return {}
    if "level" not in frame.columns:
        raise BacktestDataError("the benchmark frame needs a 'level' column")
    levels: dict[dt.date, Decimal] = {}
    for day, level in zip(
        frame.get_column("date").to_list(), frame.get_column("level").to_list(), strict=True
    ):
        if level is None:
            continue
        levels[day] = Decimal(str(level))
    return levels


def _benchmark_growth(
    levels: Mapping[dt.date, Decimal], previous: dt.date, day: dt.date
) -> Decimal:
    before = levels.get(previous)
    after = levels.get(day)
    if before is None or after is None or before <= 0:
        return Decimal(1)
    return after / before


def _market_value(
    positions: Mapping[int, _Position], panel: PricePanel, position_index: int
) -> Decimal:
    """docs/10 §6, and §8's "never forward-fill a dead instrument".

    A name that is merely quiet today — a halt, a missing print — is carried at its last close,
    because it is still a live position and marking it to zero would be a bigger lie. A name that
    has *stopped* is not here at all: the delisting step above sold it, which is the difference
    between forward-filling a halt and forward-filling a corpse.

    The lookup is bounded by ``position_index``, so it can only ever reach backwards.
    """
    total = Decimal(0)
    for instrument_id, position in positions.items():
        if position.quantity <= 0:  # pragma: no cover - `_fill` pops emptied positions
            continue
        close = panel.close_on(instrument_id, position_index)
        if close is None:
            found = panel.last_close_at_or_before(instrument_id, position_index)
            if found is None:
                continue
            close = found[0]
        total += close * position.quantity
    return _money(total)


def _decide(  # noqa: PLR0913, PLR0917 - a decision joins the screen, the book and the overlay
    reader: PointInTimeReader,
    day: dt.date,
    positions: Mapping[int, _Position],
    config: BacktestConfig,
    data: BacktestData,
    benchmark_levels: Mapping[dt.date, Decimal],
) -> tuple[_Decision, list[str], int]:
    """Steps 1-3: run the screen as of ``day``, apply the buffer, produce target weights.

    The third return value is how many rows the screen produced. Zero is reported rather than
    inferred from the decision, because a decision can also be empty when the risk overlay fired
    or when every selected name was unaffordable — and those are not the same thing as the screen
    having had nothing to say (see `BacktestResult.blind_rebalances`).
    """
    notes: list[str] = []
    rows = _selected_rows(reader.screen_on(day))

    targets: dict[int, Decimal] = {}
    ranks: dict[int, int] = {}
    if config.risk_overlay.enabled and not _risk_on(reader, day, benchmark_levels, notes):
        notes.append(
            f"the risk overlay was triggered on {day.isoformat()}: the benchmark closed below "
            "its 200-day moving average, so the book went to cash."
        )
    else:
        held = [
            HeldName(
                instrument_id=instrument_id,
                symbol=data.symbol_for(instrument_id),
                name=data.name_for(instrument_id),
                quantity=Decimal(position.quantity),
            )
            for instrument_id, position in sorted(positions.items())
            if position.quantity > 0
        ]
        # The rank-buffer rule is `baskfy_core.rank_buffer`, not a second copy of it: the tracker
        # and the backtest must never disagree about what "inside the buffer" means.
        plan = plan_rebalance(
            (
                ScreenRank(
                    instrument_id=row.instrument_id,
                    symbol=row.symbol,
                    name=row.name,
                    rank=row.rank,
                )
                for row in rows
            ),
            held,
            top_n=config.selection.top_n,
            hold_buffer=config.selection.hold_buffer,
        )
        keep = {row.instrument_id for row in (*plan.entries, *plan.holds, *plan.inside_wrh)}
        by_id = {row.instrument_id: row for row in rows}
        selected = [by_id[instrument_id] for instrument_id in sorted(keep)]
        if not selected:
            notes.append(
                f"the screen returned nothing on {day.isoformat()}; the book went to cash."
            )
        targets = apply_position_limits(
            raw_weights(config.weighting, selected), config.position_limits
        )
        ranks = {row.instrument_id: row.rank for row in selected}

    return (
        _Decision(
            decided_on=day,
            targets=targets,
            ranks=ranks,
            reasons=_reasons(targets, positions),
        ),
        notes,
        len(rows),
    )


def _risk_on(
    reader: PointInTimeReader,
    day: dt.date,
    levels: Mapping[dt.date, Decimal],
    notes: list[str],
) -> bool:
    """docs/10's only overlay rule: is the benchmark above its 200-day moving average?"""
    window = reader.benchmark_to(day)
    series = [value for value in window.get_column("level").to_list() if value is not None]
    if len(series) < RISK_OVERLAY_WINDOW:
        notes.append(
            f"the risk overlay had only {len(series)} benchmark observations on "
            f"{day.isoformat()}, fewer than the 200 its moving average needs; it stayed invested."
        )
        return True
    tail = [Decimal(str(value)) for value in series[-RISK_OVERLAY_WINDOW:]]
    average = sum(tail, Decimal(0)) / Decimal(RISK_OVERLAY_WINDOW)
    current = levels.get(day, tail[-1])
    return current >= average


def _selected_rows(frame: pl.DataFrame) -> list[_Selected]:
    required = {"instrument_id", "rank"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise BacktestDataError(f"a screen result is missing column(s) {missing}")
    has_symbol = "symbol" in frame.columns
    has_name = "name" in frame.columns
    has_cap = "marketcap_cr" in frame.columns
    has_vol = "vol_12m" in frame.columns
    rows: list[_Selected] = []
    for record in frame.iter_rows(named=True):
        instrument_id = int(record["instrument_id"])
        symbol = str(record["symbol"]) if has_symbol else f"#{instrument_id}"
        rows.append(
            _Selected(
                instrument_id=instrument_id,
                symbol=symbol,
                name=str(record["name"]) if has_name else symbol,
                rank=int(record["rank"]),
                marketcap_cr=_optional_decimal(record["marketcap_cr"]) if has_cap else None,
                volatility=_optional_decimal(record["vol_12m"]) if has_vol else None,
            )
        )
    return rows


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float | str):
        return Decimal(str(value))
    raise BacktestDataError(f"{value!r} is not a number the engine can weight by")


def _reasons(
    targets: Mapping[int, Decimal], positions: Mapping[int, _Position]
) -> dict[int, TradeReason]:
    """Why each name is being traded — docs/10 §Artefacts' ``enter|exit|rebalance``.

    ``delist`` is not decided here; it is not a decision at all, it is something that happened.
    """
    reasons: dict[int, TradeReason] = {}
    for instrument_id in sorted(set(targets) | set(positions)):
        held = positions.get(instrument_id)
        held_quantity = held.quantity if held is not None else 0
        if instrument_id not in targets:
            if held_quantity > 0:
                reasons[instrument_id] = TradeReason.EXIT
            continue
        reasons[instrument_id] = TradeReason.ENTER if held_quantity == 0 else TradeReason.REBALANCE
    return reasons


def _execute(  # noqa: PLR0913, PLR0917 - a fill needs the book, the prices and the costs
    decision: _Decision,
    day: dt.date,
    position_index: int,
    panel: PricePanel,
    positions: dict[int, _Position],
    cash: Decimal,
    config: BacktestConfig,
    data: BacktestData,
) -> tuple[Decimal, list[Trade], tuple[int, ...]]:
    """docs/10 steps 4-5: fill at today's open, whole shares, costs on traded notional.

    The third return value is every name the decision covered that had **no opening print that
    morning**, so nothing could be filled for it (M45).

    This used to be a bare `continue`. A name that does not trade cannot be traded, so skipping is
    the right *action* — but it was silent, and on a thin session it is not one name. NSE holds
    seven sessions in this dataset (Diwali Muhurat, Budget-day Saturdays, 2024's special
    Saturdays) that print 1-18% of a normal day's instruments. A rebalance whose fill day lands on
    one of them executes **none** of itself: measured, a full 30-name decision on 2026-01-30 with
    a 2026-02-01 fill day produced zero trades, and the run's own honesty counters reported it
    clean, because `blind_rebalances` counts an empty *screen* and this screen was full.

    Sells settle first. Buying before the sale that funds it would let the book spend money it
    does not have, which is the second most common way a backtest flatters itself.

    The book is valued at **today's opening prices** to size the orders, because that is the
    price the orders will actually be filled at. Sizing on yesterday's close and filling at
    today's open would leave every position mis-sized by exactly the overnight gap.
    """
    fills: list[Trade] = []
    equity = cash + _open_value(positions, panel, position_index)
    wanted: dict[int, int] = {}
    #: Names the decision covered that could not be filled, because they did not trade that
    #: morning. Counted rather than dropped in silence — see the note the caller attaches.
    unfillable: list[int] = []
    for instrument_id in decision.instruments:
        price = panel.open_on(instrument_id, position_index)
        if price is None or price <= 0:
            unfillable.append(instrument_id)
            continue
        wanted[instrument_id] = _target_quantity(decision.targets.get(instrument_id), equity, price)

    for pass_side in (TradeSide.SELL, TradeSide.BUY):
        for instrument_id in decision.instruments:
            target = wanted.get(instrument_id)
            if target is None:
                continue
            price = panel.open_on(instrument_id, position_index)
            if price is None or price <= 0:  # pragma: no cover - filtered building `wanted`
                continue
            held = positions.get(instrument_id)
            delta = target - (held.quantity if held is not None else 0)
            if delta == 0 or (delta < 0) != (pass_side is TradeSide.SELL):
                continue
            quantity = (
                -delta if pass_side is TradeSide.SELL else _affordable(delta, price, cash, config)
            )
            if quantity <= 0:
                continue
            cash, trade = _fill(
                instrument_id,
                day,
                quantity,
                price,
                pass_side,
                decision.reasons.get(instrument_id, TradeReason.REBALANCE),
                positions,
                cash,
                config,
                data,
            )
            fills.append(trade)
    return cash, fills, tuple(unfillable)


def _open_value(
    positions: Mapping[int, _Position], panel: PricePanel, position_index: int
) -> Decimal:
    """The book at today's open. A name with no opening print is carried at its last close."""
    total = Decimal(0)
    for instrument_id, position in positions.items():
        if position.quantity <= 0:
            continue
        price = panel.open_on(instrument_id, position_index)
        if price is None:
            found = panel.last_close_at_or_before(instrument_id, position_index)
            if found is None:
                continue
            price = found[0]
        total += price * position.quantity
    return _money(total)


def _target_quantity(weight: Decimal | None, equity: Decimal, price: Decimal) -> int:
    """docs/10 step 5: "Round to whole shares."

    Down, never up: a backtest may not buy shares on margin it does not have.
    """
    if weight is None or weight <= 0:
        return 0
    return int((equity * weight / price).to_integral_value(rounding=ROUND_DOWN))


def _affordable(delta: int, price: Decimal, cash: Decimal, config: BacktestConfig) -> int:
    """Trim a buy to what the cash on hand can actually pay for, cost included."""
    unit = price * (Decimal(1) + config.costs.total_bps / BASIS_POINTS)
    if unit <= 0:  # pragma: no cover - price is positive by the time this is reached
        return 0
    ceiling = int((cash / unit).to_integral_value(rounding=ROUND_DOWN))
    return max(0, min(delta, ceiling))


def _fill(  # noqa: PLR0913, PLR0917 - one parameter per thing a fill touches
    instrument_id: int,
    day: dt.date,
    quantity: int,
    price: Decimal,
    side: TradeSide,
    reason: TradeReason,
    positions: dict[int, _Position],
    cash: Decimal,
    config: BacktestConfig,
    data: BacktestData,
) -> tuple[Decimal, Trade]:
    notional = _money(price * quantity)
    cost = config.costs.charge(notional)
    position = positions.setdefault(instrument_id, _Position())
    realised: Decimal | None = None
    if side is TradeSide.BUY:
        cash = _money(cash - notional - cost)
        position.quantity += quantity
        position.book_cost += notional + cost
    else:
        basis = (
            _money(position.book_cost * Decimal(quantity) / Decimal(position.quantity))
            if position.quantity > 0
            else Decimal(0)
        )
        cash = _money(cash + notional - cost)
        position.quantity -= quantity
        position.book_cost -= basis
        realised = _money(notional - cost - basis)
        if position.quantity <= 0:
            positions.pop(instrument_id, None)
    return cash, Trade(
        date=day,
        instrument_id=instrument_id,
        symbol=data.symbol_for(instrument_id),
        side=side,
        quantity=quantity,
        price=price,
        notional=notional,
        cost=cost,
        reason=reason,
        realised_pnl=realised,
    )


def _liquidate(  # noqa: PLR0913, PLR0917 - a forced sale touches the book, the cash and the log
    reader: PointInTimeReader,
    instrument_id: int,
    day: dt.date,
    positions: dict[int, _Position],
    cash: Decimal,
    config: BacktestConfig,
    data: BacktestData,
) -> tuple[DelistingEvent | None, Decimal, Trade | None]:
    """docs/10 §8: "liquidate at the last available close, credit cash, log the event"."""
    found = reader.last_close(instrument_id, day)
    if found is None:
        return None, cash, None
    price, priced_on = found
    quantity = positions[instrument_id].quantity
    cash, trade = _fill(
        instrument_id,
        day,
        quantity,
        price,
        TradeSide.SELL,
        TradeReason.DELIST,
        positions,
        cash,
        config,
        data,
    )
    event = DelistingEvent(
        date=day,
        instrument_id=instrument_id,
        symbol=data.symbol_for(instrument_id),
        quantity=quantity,
        price=price,
        priced_on=priced_on,
        proceeds=trade.notional - trade.cost,
    )
    return event, cash, trade


def _credit_dividends(
    reader: PointInTimeReader, day: dt.date, positions: Mapping[int, _Position]
) -> Decimal:
    frame = reader.dividends_on(day)
    if not frame.height:
        return Decimal(0)
    total = Decimal(0)
    for record in frame.iter_rows(named=True):
        position = positions.get(int(record["instrument_id"]))
        if position is None or position.quantity <= 0:
            continue
        amount = _optional_decimal(record["amount"])
        if amount is None:
            continue
        total += amount * position.quantity
    return _money(total)


def _snapshot(  # noqa: PLR0913, PLR0917 - a snapshot is a join of the book and the decision
    decision: _Decision,
    executed_on: dt.date,
    positions: Mapping[int, _Position],
    panel: PricePanel,
    position_index: int,
    data: BacktestData,
    cash: Decimal,
) -> list[HoldingSnapshot]:
    """docs/10 §Artefacts: "per-rebalance holdings with weights", as actually filled.

    ``target_weight`` is what the decision asked for and ``actual_weight`` is what whole-share
    rounding produced. Showing only one of them would hide the difference, which for a small
    account on an expensive share is the whole story.
    """
    values: dict[int, Decimal] = {}
    for instrument_id, position in positions.items():
        if position.quantity <= 0:  # pragma: no cover - `_fill` pops emptied positions
            continue
        price = panel.open_on(instrument_id, position_index)
        if price is None:
            found = panel.last_close_at_or_before(instrument_id, position_index)
            if found is None:
                continue
            price = found[0]
        values[instrument_id] = _money(price * position.quantity)
    equity = sum(values.values(), Decimal(0)) + cash
    rows: list[HoldingSnapshot] = []
    for instrument_id in sorted(values, key=lambda key: (decision.ranks.get(key, 10**9), key)):
        price = values[instrument_id] / positions[instrument_id].quantity
        rows.append(
            HoldingSnapshot(
                rebalance_date=decision.decided_on,
                executed_on=executed_on,
                instrument_id=instrument_id,
                symbol=data.symbol_for(instrument_id),
                name=data.name_for(instrument_id),
                rank=decision.ranks.get(instrument_id),
                target_weight=decision.targets.get(instrument_id, Decimal(0)),
                quantity=positions[instrument_id].quantity,
                price=price.quantize(PRICE_EXPONENT),
                value=values[instrument_id],
                actual_weight=(
                    (values[instrument_id] / equity).quantize(WEIGHT_EXPONENT)
                    if equity > 0
                    else Decimal(0)
                ),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Fragility (deliverable 7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FragilityRun:
    """One perturbation and what it produced."""

    label: str
    description: str
    result: BacktestResult


@dataclass(frozen=True, slots=True)
class FragilityReport:
    """docs/10 §"honesty features": "the same config re-run with +/-1 rebalance-day offset and
    +/-25% costs, so users can see whether the result survives small perturbations. Most won't.
    That is the point."
    """

    base: FragilityRun
    variants: tuple[FragilityRun, ...]


#: docs/10 asks for "+/-25% costs".
FRAGILITY_COST_FACTORS: Final[tuple[tuple[str, Decimal], ...]] = (
    ("costs_minus_25pct", Decimal("0.75")),
    ("costs_plus_25pct", Decimal("1.25")),
)
FRAGILITY_OFFSETS: Final[tuple[tuple[str, int], ...]] = (
    ("rebalance_minus_1d", -1),
    ("rebalance_plus_1d", 1),
)


def run_fragility(
    config: BacktestConfig,
    data: BacktestData,
    *,
    base: BacktestResult | None = None,
    progress: ProgressSink | None = None,
) -> FragilityReport:
    """Five runs: the configuration as asked, then four small perturbations of it.

    The offset runs need a screen result for the shifted dates, which the caller must have
    precomputed — an offset date with no screen raises rather than silently reusing the
    neighbouring one, because reusing it would make the probe answer "perfectly stable" for the
    wrong reason.
    """
    baseline = base if base is not None else run_backtest(config, data, progress=progress)
    variants: list[FragilityRun] = []
    for label, factor in FRAGILITY_COST_FACTORS:
        percent = int((factor - 1) * 100)
        variants.append(
            FragilityRun(
                label=label,
                description=f"every cost component {percent:+d}%",
                result=run_backtest(config.with_costs(config.costs.scaled(factor)), data),
            )
        )
    for label, offset in FRAGILITY_OFFSETS:
        schedule = shift_schedule(data.calendar, baseline.rebalance_dates, offset)
        variants.append(
            FragilityRun(
                label=label,
                description=f"every rebalance {offset:+d} trading day",
                result=run_backtest(config, data, schedule=schedule),
            )
        )
    return FragilityReport(
        base=FragilityRun("base", "the configuration as submitted", baseline),
        variants=tuple(variants),
    )


def year_fraction(start: dt.date, end: dt.date) -> Decimal:
    """Calendar years between two dates, for CAGR. 365.25 absorbs leap years without a table."""
    return Decimal((end - start).days) / _DAYS_PER_YEAR


def month_key(day: dt.date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_end(day: dt.date) -> dt.date:
    return dt.date(day.year, day.month, _calendar.monthrange(day.year, day.month)[1])
