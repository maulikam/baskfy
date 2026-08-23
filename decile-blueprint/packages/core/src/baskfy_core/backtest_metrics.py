"""Backtest outputs — docs/10 §Outputs (Prompt 15 deliverable 3).

    **Metrics:** CAGR, total return, annualised volatility, Sharpe (rf from a configurable T-bill
    series), Sortino, max drawdown + its dates, Calmar, hit rate, average win/loss, annual
    turnover, total costs paid, exposure %, best/worst month, rolling 12-month return
    distribution, alpha/beta vs benchmark, tracking error, information ratio.

    **Artefacts:** daily equity curve, drawdown series, per-rebalance holdings with weights,
    trade log (date, symbol, side, qty, price, cost, reason …), monthly return heatmap.

Every one of those is here. Pure, like :mod:`baskfy_core.backtest` — a :class:`BacktestResult`
in, numbers out — so the same computation serves the API payload, the CSV export and the tests.

Money stays ``Decimal``; statistics are float
----------------------------------------------
Total costs, average win, average loss and the equity curve are rupees and are carried as
``Decimal`` (CLAUDE.md house rule 9). Volatility, Sharpe, beta, tracking error and the rest are
ratios estimated from a sample — a Sharpe ratio written to twenty-eight significant figures would
be false precision on a number whose *second* decimal place is noise. They are float, and
:meth:`Metrics.as_dict` rounds them, which is also what makes docs/10's determinism test
("same config + same ``data_version`` -> identical metrics hash") mean something: two runs that
agree to ten decimal places hash the same.

Annualisation
-------------
The periods-per-year factor is **observed, not assumed**: the number of daily returns divided by
the elapsed year fraction. NSE's calendar gives roughly 247 trading days a year (docs/13 §3), not
the 252 that gets copied from US texts, and a hard-coded 252 would overstate every annualised
figure by about 1%.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

import numpy as np

from baskfy_core.backtest import (
    BacktestConfig,
    BacktestResult,
    Trade,
    TradeSide,
    month_key,
    year_fraction,
)

__all__ = [
    "EQUITY_CURVE_POINTS",
    "METRIC_ROUNDING",
    "DrawdownPoint",
    "Metrics",
    "MonthlyReturn",
    "RollingDistribution",
    "RoundTrip",
    "compute_metrics",
    "downsample_curve",
    "drawdown_series",
    "metrics_hash",
    "monthly_returns",
    "round_trips",
]

#: Statistics are rounded to this many decimal places before they are published or hashed.
#: Ten places is far beyond anything meaningful and far inside float64's ~15 significant digits,
#: so it removes last-bit noise without removing information.
METRIC_ROUNDING: Final = 10

#: docs/10: "`backtest.metrics` and a downsampled equity curve live in Postgres for fast page
#: loads". Roughly two points per trading week over fifteen years — enough for a chart 900 pixels
#: wide, small enough that the row stays a few tens of kilobytes.
EQUITY_CURVE_POINTS: Final = 1500

_MONEY = Decimal("0.01")

#: A return series needs two observations before it is a series at all.
_MIN_OBSERVATIONS: Final = 2
_PERCENTILES: Final[tuple[int, ...]] = (5, 25, 50, 75, 95)


def _round(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, METRIC_ROUNDING)


@dataclass(frozen=True, slots=True)
class DrawdownPoint:
    """One day of docs/10's "drawdown series"."""

    date: dt.date
    equity: Decimal
    peak: Decimal
    drawdown: float


@dataclass(frozen=True, slots=True)
class MonthlyReturn:
    """One cell of docs/10's "monthly return heatmap"."""

    year: int
    month: int
    ret: float

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """One position, from the first share bought to the last share sold.

    docs/10 asks for "hit rate, average win/loss" and does not define the unit. A *round trip* is
    the only unit a user would recognise as "a trade that won or lost": counting every partial
    trim of a winner as its own winning trade would put the hit rate wherever the rebalance
    frequency happened to put it.
    """

    instrument_id: int
    symbol: str
    opened_on: dt.date
    closed_on: dt.date
    #: Net of every cost charged on the way in and on the way out.
    pnl: Decimal
    invested: Decimal
    #: True when the position was closed by a delisting rather than by a decision.
    delisted: bool = False

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass(frozen=True, slots=True)
class RollingDistribution:
    """docs/10: "rolling 12-month return distribution"."""

    count: int
    minimum: float | None
    p05: float | None
    p25: float | None
    median: float | None
    p75: float | None
    p95: float | None
    maximum: float | None
    #: The share of twelve-month windows that lost money — the number people actually want.
    negative_share: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "min": _round(self.minimum),
            "p05": _round(self.p05),
            "p25": _round(self.p25),
            "median": _round(self.median),
            "p75": _round(self.p75),
            "p95": _round(self.p95),
            "max": _round(self.maximum),
            "negative_share": _round(self.negative_share),
        }


@dataclass(frozen=True, slots=True)
class Metrics:
    """docs/10 §Outputs, one field per named metric, in the document's order."""

    cagr: float | None
    total_return: float
    annualised_volatility: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float
    max_drawdown_peak: dt.date | None
    max_drawdown_trough: dt.date | None
    max_drawdown_recovered: dt.date | None
    calmar: float | None
    hit_rate: float | None
    average_win: Decimal | None
    average_loss: Decimal | None
    annual_turnover: float | None
    total_costs: Decimal
    exposure: float | None
    best_month: MonthlyReturn | None
    worst_month: MonthlyReturn | None
    rolling_12m: RollingDistribution
    alpha: float | None
    beta: float | None
    tracking_error: float | None
    information_ratio: float | None
    # --- context, so a stored payload explains itself -----------------------
    start: dt.date
    end: dt.date
    trading_days: int
    periods_per_year: float | None
    initial_capital: Decimal
    final_equity: Decimal
    benchmark_total_return: float | None
    benchmark_cagr: float | None
    trades: int
    round_trips: int
    delistings: int
    dividends_credited: Decimal

    def as_dict(self) -> dict[str, object]:
        """A JSON object for ``backtest.metrics jsonb`` — and the input to the metrics hash.

        Rupee amounts are strings, not floats: ``json.dumps(Decimal)`` does not round-trip and
        ``float(Decimal("1234567.89"))`` is not ``1234567.89``. Everything else is a rounded
        float or ``None``.
        """
        return {
            "cagr": _round(self.cagr),
            "total_return": _round(self.total_return),
            "annualised_volatility": _round(self.annualised_volatility),
            "sharpe": _round(self.sharpe),
            "sortino": _round(self.sortino),
            "max_drawdown": _round(self.max_drawdown),
            "max_drawdown_peak": _iso(self.max_drawdown_peak),
            "max_drawdown_trough": _iso(self.max_drawdown_trough),
            "max_drawdown_recovered": _iso(self.max_drawdown_recovered),
            "calmar": _round(self.calmar),
            "hit_rate": _round(self.hit_rate),
            "average_win": _str(self.average_win),
            "average_loss": _str(self.average_loss),
            "annual_turnover": _round(self.annual_turnover),
            "total_costs": _str(self.total_costs),
            "exposure": _round(self.exposure),
            "best_month": _month(self.best_month),
            "worst_month": _month(self.worst_month),
            "rolling_12m": self.rolling_12m.as_dict(),
            "alpha": _round(self.alpha),
            "beta": _round(self.beta),
            "tracking_error": _round(self.tracking_error),
            "information_ratio": _round(self.information_ratio),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "trading_days": self.trading_days,
            "periods_per_year": _round(self.periods_per_year),
            "initial_capital": _str(self.initial_capital),
            "final_equity": _str(self.final_equity),
            "benchmark_total_return": _round(self.benchmark_total_return),
            "benchmark_cagr": _round(self.benchmark_cagr),
            "trades": self.trades,
            "round_trips": self.round_trips,
            "delistings": self.delistings,
            "dividends_credited": _str(self.dividends_credited),
        }


def _iso(value: dt.date | None) -> str | None:
    return None if value is None else value.isoformat()


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _month(value: MonthlyReturn | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"month": value.key, "return": _round(value.ret)}


# ---------------------------------------------------------------------------
# Artefacts
# ---------------------------------------------------------------------------


def drawdown_series(
    dates: Sequence[dt.date], equity: Sequence[Decimal]
) -> tuple[DrawdownPoint, ...]:
    """docs/10 §Artefacts: "drawdown series". Drawdown is a fraction, negative or zero."""
    points: list[DrawdownPoint] = []
    peak = Decimal(0)
    for day, value in zip(dates, equity, strict=True):
        peak = max(peak, value)
        fraction = 0.0 if peak <= 0 else float((value - peak) / peak)
        points.append(DrawdownPoint(day, value, peak, fraction))
    return tuple(points)


def monthly_returns(
    dates: Sequence[dt.date], equity: Sequence[Decimal]
) -> tuple[MonthlyReturn, ...]:
    """docs/10 §Artefacts: "monthly return heatmap".

    A month's return is measured from the last equity of the previous month to the last equity of
    this one. The first month is measured from the opening capital, so the heatmap covers the
    whole run rather than starting a month late.
    """
    if not dates:
        return ()
    closes: dict[str, tuple[dt.date, Decimal]] = {}
    for day, value in zip(dates, equity, strict=True):
        closes[month_key(day)] = (day, value)
    ordered = sorted(closes.items())
    rows: list[MonthlyReturn] = []
    previous = equity[0]
    for key, (_, value) in ordered:
        year, month = int(key[:4]), int(key[5:])
        rows.append(
            MonthlyReturn(year, month, 0.0 if previous <= 0 else float(value / previous - 1))
        )
        previous = value
    return tuple(rows)


def downsample_curve(
    dates: Sequence[dt.date],
    equity: Sequence[Decimal],
    benchmark: Sequence[Decimal | None] = (),
    *,
    limit: int = EQUITY_CURVE_POINTS,
) -> list[dict[str, object]]:
    """docs/10: "a downsampled equity curve live[s] in Postgres for fast page loads".

    Stride sampling, with the **last** point always kept: a curve whose final value is not the
    final equity would make the page disagree with the metrics table beside it.
    """
    total = len(dates)
    if total == 0:
        return []
    stride = max(1, math.ceil(total / limit))
    keep = list(range(0, total, stride))
    if keep[-1] != total - 1:
        keep.append(total - 1)
    levels = list(benchmark) if benchmark else [None] * total
    return [
        {
            "date": dates[index].isoformat(),
            "equity": str(equity[index]),
            "benchmark": None if levels[index] is None else str(levels[index]),
        }
        for index in keep
    ]


def round_trips(trades: Sequence[Trade]) -> tuple[RoundTrip, ...]:
    """Reconstruct closed positions from the fill log — see :class:`RoundTrip`."""
    open_state: dict[int, dict[str, object]] = {}
    finished: list[RoundTrip] = []
    for trade in trades:
        state = open_state.get(trade.instrument_id)
        if state is None:
            state = {
                "opened_on": trade.date,
                "quantity": 0,
                "pnl": Decimal(0),
                "invested": Decimal(0),
                "symbol": trade.symbol,
                "delisted": False,
            }
            open_state[trade.instrument_id] = state
        quantity = int(state["quantity"]) if isinstance(state["quantity"], int) else 0
        pnl = state["pnl"]
        invested = state["invested"]
        if not isinstance(pnl, Decimal) or not isinstance(invested, Decimal):  # pragma: no cover
            raise TypeError("round-trip state is corrupt")
        if trade.side is TradeSide.BUY:
            state["quantity"] = quantity + trade.quantity
            state["invested"] = invested + trade.notional + trade.cost
        else:
            state["quantity"] = quantity - trade.quantity
            state["pnl"] = pnl + (trade.realised_pnl or Decimal(0))
            if trade.reason.value == "delist":
                state["delisted"] = True
        if state["quantity"] == 0:
            opened = state["opened_on"]
            closed = trade.date
            realised = state["pnl"]
            spent = state["invested"]
            symbol = state["symbol"]
            if (
                isinstance(opened, dt.date)
                and isinstance(realised, Decimal)
                and isinstance(spent, Decimal)
                and isinstance(symbol, str)
            ):
                finished.append(
                    RoundTrip(
                        instrument_id=trade.instrument_id,
                        symbol=symbol,
                        opened_on=opened,
                        closed_on=closed,
                        pnl=realised,
                        invested=spent,
                        delisted=bool(state["delisted"]),
                    )
                )
            open_state.pop(trade.instrument_id, None)
    return tuple(finished)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _returns(values: Sequence[Decimal]) -> np.ndarray:
    if len(values) < _MIN_OBSERVATIONS:
        return np.array([], dtype=np.float64)
    series = np.array([float(value) for value in values], dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        stepped = np.where(series[:-1] > 0, series[1:] / series[:-1] - 1.0, 0.0)
    return np.asarray(stepped, dtype=np.float64)


def _benchmark_returns(levels: Sequence[Decimal | None]) -> tuple[np.ndarray, np.ndarray]:
    """Daily benchmark returns, and a mask of the days that were actually observed (M43).

    A gap in the level series is filled with a flat day. That is right for beta — dropping the day
    from one series but not the other would misalign every subsequent pair — and it was measured
    to be robust: beta moved only 1.18665..1.19045 across mutilations from 1% to 50% of levels
    removed, against a true 1.20.

    **It is wrong for tracking error**, which is why the mask exists. Tracking error is the spread
    of the active return, and an unobserved benchmark day produces no active return to measure.
    Imputing zero does not leave it unbiased, it invents agreement: at 1% of levels missing the
    error is +7.2%, at 5% +29.0%, at 20% +84.5%, at half the series absent +101.3%. So the second
    return value marks the days that carry a real observation, and everything that measures
    *dispersion* against the benchmark uses only those.

    The degenerate case this exists to stop: with **no** benchmark at all the old form returned an
    array of zeros, so ``active = portfolio - 0`` and the page published the portfolio's own
    volatility labelled "tracking error" and its own Sharpe labelled "information ratio". An empty
    mask now makes both `None`.
    """
    if len(levels) < _MIN_OBSERVATIONS:
        return np.array([], dtype=np.float64), np.array([], dtype=bool)
    out = np.zeros(len(levels) - 1, dtype=np.float64)
    observed = np.zeros(len(levels) - 1, dtype=bool)
    for index in range(1, len(levels)):
        before, after = levels[index - 1], levels[index]
        if before is None or after is None or before <= 0:
            continue
        out[index - 1] = float(after / before - 1)
        observed[index - 1] = True
    return out, observed


def compute_metrics(result: BacktestResult) -> Metrics:
    """Every metric docs/10 §Outputs names, from one pass over the run."""
    dates = result.dates
    equity = result.equity
    config = result.config
    start, end = dates[0], dates[-1]
    years = year_fraction(start, end)
    portfolio = _returns(equity)
    periods = float(len(portfolio)) / float(years) if years > 0 and len(portfolio) else None

    opening = float(config.initial_capital)
    closing = float(result.final_equity)
    total_return = closing / opening - 1.0 if opening > 0 else 0.0
    cagr = _cagr(opening, closing, years)

    volatility = _annualised_std(portfolio, periods)
    rf_daily = _daily_rate(config.risk_free_rate, periods)
    excess = portfolio - rf_daily if portfolio.size else portfolio
    sharpe = _ratio(excess, _annualised_std(excess, periods), periods)
    sortino = _ratio(excess, _downside_deviation(excess, periods), periods)

    drawdowns = drawdown_series(dates, equity)
    worst = min(drawdowns, key=lambda point: point.drawdown) if drawdowns else None
    max_drawdown = worst.drawdown if worst is not None else 0.0
    peak_date, recovered = _drawdown_dates(drawdowns, worst)
    calmar = None if cagr is None or max_drawdown >= 0 else cagr / abs(max_drawdown)

    months = monthly_returns(dates, equity)
    completed = round_trips(result.trades)
    wins = [trip.pnl for trip in completed if trip.is_win]
    losses = [trip.pnl for trip in completed if not trip.is_win]

    benchmark_returns, benchmark_observed = _benchmark_returns(result.benchmark)
    alpha, beta = _alpha_beta(excess, benchmark_returns - rf_daily, periods)
    # Dispersion against the benchmark is measured only where the benchmark was observed. See
    # `_benchmark_returns`: a gap imputed as a flat day inflates tracking error by up to 101%,
    # and a wholly absent benchmark would otherwise republish the portfolio's own numbers.
    active = (
        (portfolio - benchmark_returns)[benchmark_observed]
        if benchmark_returns.size == portfolio.size and bool(benchmark_observed.any())
        else None
    )
    tracking_error = _annualised_std(active, periods) if active is not None else None
    information_ratio = _ratio(active, tracking_error, periods) if active is not None else None

    return Metrics(
        cagr=cagr,
        total_return=total_return,
        annualised_volatility=volatility,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_drawdown,
        max_drawdown_peak=peak_date,
        max_drawdown_trough=worst.date if worst is not None and max_drawdown < 0 else None,
        max_drawdown_recovered=recovered,
        calmar=calmar,
        hit_rate=(len(wins) / len(completed)) if completed else None,
        average_win=_mean_money(wins),
        average_loss=_mean_money(losses),
        annual_turnover=_turnover(result, years),
        total_costs=result.total_costs,
        exposure=_exposure(result),
        best_month=max(months, key=lambda row: row.ret) if months else None,
        worst_month=min(months, key=lambda row: row.ret) if months else None,
        rolling_12m=rolling_distribution(dates, equity),
        alpha=alpha,
        beta=beta,
        tracking_error=tracking_error,
        information_ratio=information_ratio,
        start=start,
        end=end,
        trading_days=len(dates),
        periods_per_year=periods,
        initial_capital=config.initial_capital,
        final_equity=result.final_equity,
        benchmark_total_return=_benchmark_total(result.benchmark),
        benchmark_cagr=_benchmark_cagr(dates, result.benchmark, years),
        trades=len(result.trades),
        round_trips=len(completed),
        delistings=len(result.delistings),
        dividends_credited=result.dividends_credited,
    )


def _cagr(opening: float, closing: float, years: Decimal) -> float | None:
    """The compound annual rate, or ``None`` where there is not one.

    A closing value of exactly zero is **not** the undefined case, and returning ``None`` there
    used to blank the two headline numbers on the one run whose answer is least ambiguous: a
    portfolio that went to zero compounded at -100% a year. `total_return` already reported -1.0
    and `max_drawdown` -1.0 while `cagr` and, through it, `calmar` came back empty (M43).

    Genuinely undefined: a non-positive *opening* value, a zero-length window, or a closing value
    below zero, which this engine cannot produce — cash is floored at zero and shares are never
    sold short.
    """
    if opening <= 0 or closing < 0 or years <= 0:
        return None
    if closing == 0:
        return -1.0
    return float(float(closing / opening) ** (1.0 / float(years))) - 1.0


def _daily_rate(annual: Decimal, periods: float | None) -> float:
    """A flat annual rate spread over the observed number of periods, compounded."""
    if periods is None or periods <= 0 or annual <= 0:
        return 0.0
    return float((1.0 + float(annual)) ** (1.0 / periods)) - 1.0


def _annualised_std(series: np.ndarray | None, periods: float | None) -> float | None:
    if series is None or series.size < _MIN_OBSERVATIONS or periods is None or periods <= 0:
        return None
    return float(series.std(ddof=1)) * math.sqrt(periods)


def _downside_deviation(series: np.ndarray, periods: float | None) -> float | None:
    if series.size < _MIN_OBSERVATIONS or periods is None or periods <= 0:
        return None
    downside = np.minimum(series, 0.0)
    value = float(np.sqrt(np.mean(np.square(downside))))
    return value * math.sqrt(periods) if value > 0 else None


#: Below this, an annualised standard deviation is rounding noise rather than a measurement.
#:
#: The guard used to be `denominator == 0.0` exactly. A series that is constant to within storage
#: precision has a volatility of ~6e-08 rather than 0, which is not zero and is not a risk
#: measure either — it published `sharpe = -4292786.11`. Money is stored at 2 dp (house rule 9),
#: so a daily return below 1e-6 cannot be a real price move; annualised at ~250 periods that
#: floor is ~1.6e-05, and 1e-06 sits comfortably under it without ever refusing a real series.
_MIN_ANNUALISED_STD: Final = 1e-6


def _ratio(
    excess: np.ndarray | None, denominator: float | None, periods: float | None
) -> float | None:
    if excess is None or excess.size == 0 or periods is None:
        return None
    if denominator is None or abs(denominator) < _MIN_ANNUALISED_STD:
        return None
    return float(excess.mean()) * periods / denominator


def _drawdown_dates(
    points: Sequence[DrawdownPoint], worst: DrawdownPoint | None
) -> tuple[dt.date | None, dt.date | None]:
    if worst is None or worst.drawdown >= 0:
        return None, None
    peak_date: dt.date | None = None
    for point in points:
        if point.date > worst.date:
            break
        if point.equity >= worst.peak:
            peak_date = point.date
    recovered = next(
        (point.date for point in points if point.date > worst.date and point.equity >= worst.peak),
        None,
    )
    return peak_date, recovered


def _mean_money(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, Decimal(0)) / Decimal(len(values))).quantize(_MONEY, rounding=ROUND_HALF_UP)


def _turnover(result: BacktestResult, years: Decimal) -> float | None:
    """Annual turnover: one-way traded notional over average equity, per year.

    Buys and sells are added and halved, which is the convention every fund factsheet uses: a
    portfolio that sells everything and buys a whole new book has turned over once, not twice.
    """
    if years <= 0 or not result.equity:
        return None
    traded = sum((trade.notional for trade in result.trades), Decimal(0))
    average = sum(result.equity, Decimal(0)) / Decimal(len(result.equity))
    if average <= 0:
        return None
    return float(traded / 2 / average / years)


def _exposure(result: BacktestResult) -> float | None:
    """docs/10: "exposure %" — the average share of the book that was in the market."""
    pairs = [
        float(invested / equity)
        for invested, equity in zip(result.invested, result.equity, strict=True)
        if equity > 0
    ]
    return sum(pairs) / len(pairs) if pairs else None


def _alpha_beta(
    excess: np.ndarray, benchmark_excess: np.ndarray, periods: float | None
) -> tuple[float | None, float | None]:
    """Ordinary least squares of the portfolio's excess return on the benchmark's.

    Alpha is annualised by compounding the daily intercept, not by multiplying it: an intercept of
    2 bps a day is 5.2% a year, not 5.0%, and the difference is the whole of some strategies.
    """
    if excess.size < _MIN_OBSERVATIONS or excess.size != benchmark_excess.size or periods is None:
        return None, None
    variance = float(benchmark_excess.var(ddof=1))
    if variance <= 0:
        return None, None
    covariance = float(np.cov(excess, benchmark_excess, ddof=1)[0][1])
    beta = covariance / variance
    intercept = float(excess.mean()) - beta * float(benchmark_excess.mean())
    return float((1.0 + intercept) ** periods) - 1.0, beta


def _benchmark_total(levels: Sequence[Decimal | None]) -> float | None:
    known = [value for value in levels if value is not None and value > 0]
    if len(known) < _MIN_OBSERVATIONS:
        return None
    return float(known[-1] / known[0] - 1)


def _benchmark_cagr(
    dates: Sequence[dt.date], levels: Sequence[Decimal | None], years: Decimal
) -> float | None:
    """The benchmark's own compound rate, over the span the benchmark actually covers (M43).

    It used to divide by the *portfolio's* elapsed years while its numerator spanned only the
    first to last non-null level. Every missing leading day therefore stretched a shorter return
    over a longer denominator, and the error is **one-directional: it always understates the
    benchmark, which always flatters the strategy**. Measured on a 4.742-year run: with a quarter
    of the benchmark absent it reported 1.3380%/yr against 1.7879% true (-25.2% relative); with
    half absent, 1.3403% against 2.7018% (-1.3615 pp/yr); with three quarters absent, 0.7912%
    against 3.2176% (-75.4%).

    `years` is still accepted so a caller with full coverage gets exactly the old number, and is
    used only when the covered span cannot be measured.
    """
    covered = [
        (day, value)
        for day, value in zip(dates, levels, strict=False)
        if value is not None and value > 0
    ]
    if len(covered) < _MIN_OBSERVATIONS:
        return None
    span = year_fraction(covered[0][0], covered[-1][0])
    return _cagr(float(covered[0][1]), float(covered[-1][1]), span if span > 0 else years)


def rolling_distribution(
    dates: Sequence[dt.date], equity: Sequence[Decimal]
) -> RollingDistribution:
    """docs/10: "rolling 12-month return distribution".

    Windows are *calendar* years, not 252-day blocks: a user asking "what did a year in this
    strategy look like?" means a year, and a fixed row count drifts against the calendar as the
    number of trading days per year changes.
    """
    if len(dates) < _MIN_OBSERVATIONS:
        return RollingDistribution(0, None, None, None, None, None, None, None, None)
    ordered = list(dates)
    samples: list[float] = []
    for index, day in enumerate(ordered):
        try:
            target = day.replace(year=day.year + 1)
        except ValueError:  # 29 February
            target = day.replace(year=day.year + 1, day=28)
        # Binary search rather than a scan: the naive form is quadratic, and a fifteen-year run
        # has ~3,900 days, which is 7.6 million comparisons for one summary statistic.
        position = bisect_left(ordered, target, lo=index + 1)
        if position >= len(ordered):
            break
        opening = equity[index]
        if opening <= 0:
            continue
        samples.append(float(equity[position] / opening - 1))
    if not samples:
        return RollingDistribution(0, None, None, None, None, None, None, None, None)
    array = np.array(samples, dtype=np.float64)
    p05, p25, median, p75, p95 = (float(value) for value in np.percentile(array, _PERCENTILES))
    return RollingDistribution(
        count=len(samples),
        minimum=float(array.min()),
        p05=p05,
        p25=p25,
        median=median,
        p75=p75,
        p95=p95,
        maximum=float(array.max()),
        negative_share=float((array < 0).mean()),
    )


# ---------------------------------------------------------------------------
# Determinism (docs/10 §"Correctness harness" test 4)
# ---------------------------------------------------------------------------


def metrics_hash(metrics: Metrics, config: BacktestConfig, data_version: int | None = None) -> str:
    """docs/10: "same config + same ``data_version`` -> identical metrics hash".

    Over the metrics **and** the configuration that produced them **and** the data version, so
    that the hash answers "is this the same result?" rather than "do these two numbers match?".
    Canonical JSON with sorted keys, hashed with SHA-256 — the same recipe
    :func:`baskfy_core.screener.cache_key` uses, for the same reason.
    """
    payload: Mapping[str, object] = {
        "metrics": metrics.as_dict(),
        "config": json.loads(config.model_dump_json()),
        "data_version": data_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
