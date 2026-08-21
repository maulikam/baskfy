"""A synthetic market for the backtest correctness harness (docs/10 §"Correctness harness").

Why synthetic, and why that is the right call here
--------------------------------------------------
Every one of docs/10's six tests is an *identity*: buy-and-hold must reproduce the benchmark,
zero-cost zero-turnover must reproduce the equal-weighted universe, a delisting must show up as a
loss. An identity can only be checked against a market whose right answer is known in closed
form, and the seeded database holds a single trading day of *results* (docs/13's export), not a
price history — see CLAUDE.md's note on ``test_reference_parity``. So the market is built here,
with a fixed seed, and the benchmark index is constructed to be **exactly** the value-weighted
portfolio of its own constituents. That is what makes test 2 an assertion rather than a vibe.

Everything is deterministic: a seeded ``numpy`` generator, prices rounded to the four decimal
places ``ohlcv_daily`` stores, and a weekday calendar. Two runs of this module produce the same
market byte for byte, which is what docs/10's determinism test needs to be about the *engine*
rather than about the fixture.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import numpy as np
import polars as pl

from decile_core.backtest import BacktestData, PricePanel

#: Trading days used to rank on trailing momentum. Roughly a year of NSE weekdays.
MOMENTUM_WINDOW: Final = 250

#: Every price in the fixture sits on ``ohlcv_daily``'s four-decimal grid.
PRICE_DP: Final = 4


def weekday_calendar(start: dt.date, end: dt.date) -> tuple[dt.date, ...]:
    """Mon-Fri between two dates. No exchange holidays — a holiday is a gap, and a gap here would
    test the fixture's calendar rather than the engine."""
    days: list[dt.date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return tuple(days)


@dataclass(frozen=True, slots=True)
class SyntheticMarket:
    """A price panel, a value-weighted benchmark, and everything the engine needs to read it."""

    calendar: tuple[dt.date, ...]
    instrument_ids: tuple[int, ...]
    closes: np.ndarray
    opens: np.ndarray
    shares: np.ndarray
    symbols: dict[int, str]
    names: dict[int, str]
    delistings: dict[int, dt.date]
    last_row: dict[int, int]

    # -- the pieces the engine takes ---------------------------------------

    def price_frame(self) -> pl.DataFrame:
        rows_date: list[dt.date] = []
        rows_id: list[int] = []
        rows_open: list[float] = []
        rows_close: list[float] = []
        for column, instrument_id in enumerate(self.instrument_ids):
            last = self.last_row[instrument_id]
            for row in range(last + 1):
                rows_date.append(self.calendar[row])
                rows_id.append(instrument_id)
                rows_open.append(float(self.opens[row, column]))
                rows_close.append(float(self.closes[row, column]))
        return pl.DataFrame(
            {
                "date": rows_date,
                "instrument_id": rows_id,
                "open": rows_open,
                "close": rows_close,
            }
        )

    def marketcap_at(self, row: int) -> np.ndarray:
        """Value-weighted marketcap in rupees on ``row``. ``NaN`` for a dead name."""
        caps: np.ndarray = self.closes[row] * self.shares
        return caps

    def benchmark_frame(self) -> pl.DataFrame:
        """A value-weighted index of the *live* constituents, in the same units as a marketcap.

        This is the whole point of the fixture: a buy-and-hold portfolio weighted by marketcap
        holds exactly this basket, so docs/10 §"Correctness harness" test 2 has an exact answer to
        compare against rather than an approximate one.
        """
        levels: list[float] = []
        for row in range(len(self.calendar)):
            caps = self.marketcap_at(row)
            levels.append(float(np.nansum(caps)))
        return pl.DataFrame({"date": list(self.calendar), "level": levels})

    def screen_frame(self, day: dt.date) -> pl.DataFrame:
        """The screen's output as of ``day`` — ranked on trailing momentum, point in time.

        Nothing after ``day`` touches it, and the frame is stamped with ``day``, which is the
        column the engine's look-ahead guard checks.
        """
        row = self.calendar.index(day)
        window = max(0, row - MOMENTUM_WINDOW)
        live = [
            (column, instrument_id)
            for column, instrument_id in enumerate(self.instrument_ids)
            if self.last_row[instrument_id] >= row and not np.isnan(self.closes[row, column])
        ]
        scored = []
        for column, instrument_id in live:
            base = self.closes[window, column]
            if np.isnan(base) or base <= 0:
                continue
            scored.append((float(self.closes[row, column] / base - 1.0), instrument_id, column))
        # Tie-break on instrument id, exactly as `decile_core.screener` does, so a tie cannot
        # give two runs different ranks.
        scored.sort(key=lambda item: (-item[0], item[1]))
        return pl.DataFrame(
            {
                "date": [day] * len(scored),
                "instrument_id": [instrument_id for _, instrument_id, _ in scored],
                "symbol": [self.symbols[instrument_id] for _, instrument_id, _ in scored],
                "name": [self.names[instrument_id] for _, instrument_id, _ in scored],
                "rank": list(range(1, len(scored) + 1)),
                "marketcap_cr": [
                    float(self.closes[row, column] * self.shares[column] / 1e7)
                    for _, _, column in scored
                ],
                "vol_12m": [
                    _trailing_volatility(self.closes[: row + 1, column]) for _, _, column in scored
                ],
            }
        )

    def data(self, rebalance_days: Sequence[dt.date], *, data_version: int = 1) -> BacktestData:
        panel = PricePanel(self.price_frame(), self.calendar)
        return BacktestData(
            calendar=self.calendar,
            prices=panel,
            screens={day: self.screen_frame(day) for day in rebalance_days},
            benchmark=self.benchmark_frame(),
            delistings=dict(self.delistings),
            symbols=dict(self.symbols),
            names=dict(self.names),
            data_version=data_version,
        )

    def open_prices(self, day: dt.date) -> dict[int, Decimal]:
        row = self.calendar.index(day)
        prices: dict[int, Decimal] = {}
        for column, instrument_id in enumerate(self.instrument_ids):
            value = self.opens[row, column]
            if not np.isnan(value):
                prices[instrument_id] = Decimal(f"{float(value):.4f}")
        return prices

    def close_prices(self, day: dt.date) -> dict[int, Decimal]:
        row = self.calendar.index(day)
        prices: dict[int, Decimal] = {}
        for column, instrument_id in enumerate(self.instrument_ids):
            value = self.closes[row, column]
            if not np.isnan(value):
                prices[instrument_id] = Decimal(f"{float(value):.4f}")
        return prices


def _trailing_volatility(series: np.ndarray) -> float:
    """Annualised standard deviation of daily log-ish returns, for inverse-volatility weighting."""
    tail = series[-MOMENTUM_WINDOW:]
    tail = tail[~np.isnan(tail)]
    if tail.size < 3:
        return 0.2
    steps = tail[1:] / tail[:-1] - 1.0
    value = float(steps.std(ddof=1)) * float(np.sqrt(250.0))
    return value if value > 0 else 0.2


def build_market(  # noqa: PLR0913 - a market is described by exactly these knobs
    *,
    start: dt.date = dt.date(2011, 1, 3),
    end: dt.date = dt.date(2013, 12, 31),
    instruments: int = 40,
    seed: int = 20260821,
    drift: float = 0.00035,
    volatility: float = 0.016,
    delist: tuple[int, dt.date] | None = None,
    delist_slide: float = -0.004,
) -> SyntheticMarket:
    """A seeded random-walk market.

    ``delist`` names an instrument that slides and then stops printing bars on a given date, which
    is what docs/10 §"Correctness harness" test 5 needs: a name that must show up as a loss and
    must not be silently dropped.
    """
    calendar = weekday_calendar(start, end)
    rng = np.random.default_rng(seed)
    days = len(calendar)
    instrument_ids = tuple(range(1, instruments + 1))

    # Each name gets its own drift so the ranking has something to rank.
    drifts = drift + rng.normal(0.0, drift, size=instruments)
    base = rng.uniform(80.0, 900.0, size=instruments)

    steps = rng.normal(0.0, volatility, size=(days, instruments)) + drifts
    overnight = rng.normal(0.0, volatility / 3.0, size=(days, instruments))

    closes = np.empty((days, instruments), dtype=np.float64)
    opens = np.empty((days, instruments), dtype=np.float64)
    previous = base
    for row in range(days):
        opened = np.round(previous * (1.0 + overnight[row]), PRICE_DP)
        closed = np.round(opened * (1.0 + steps[row]), PRICE_DP)
        closed = np.maximum(closed, 0.05)
        opens[row] = opened
        closes[row] = closed
        previous = closed

    last_row = {instrument_id: days - 1 for instrument_id in instrument_ids}
    delistings: dict[int, dt.date] = {}
    if delist is not None:
        target, stop_on = delist
        column = instrument_ids.index(target)
        stop_row = next(index for index, day in enumerate(calendar) if day >= stop_on)
        # Slide the name into the ground before it stops trading, so the loss is unmistakable.
        slide = np.cumprod(np.full(stop_row, 1.0 + delist_slide))
        closes[:stop_row, column] = np.round(closes[:stop_row, column] * slide, PRICE_DP)
        opens[:stop_row, column] = np.round(opens[:stop_row, column] * slide, PRICE_DP)
        closes[stop_row:, column] = np.nan
        opens[stop_row:, column] = np.nan
        last_row[target] = stop_row - 1
        delistings[target] = calendar[stop_row]

    shares = np.round(rng.uniform(1e6, 4e7, size=instruments), 0)
    return SyntheticMarket(
        calendar=calendar,
        instrument_ids=instrument_ids,
        closes=closes,
        opens=opens,
        shares=shares,
        symbols={instrument_id: f"SYN{instrument_id:03d}" for instrument_id in instrument_ids},
        names={
            instrument_id: f"Synthetic {instrument_id:03d} Ltd" for instrument_id in instrument_ids
        },
        delistings=delistings,
        last_row=last_row,
    )
