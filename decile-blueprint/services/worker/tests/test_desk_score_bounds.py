"""The desk score's bar load is bounded, and the bound changes no score (DECISIONS-MERGE 2C.5).

On 14 Sep 2026 ``backfill-ranking`` took the production box down before its first day finished:
``load_desk_bars`` read every bar since 2011 for every scanned instrument, and the nightly's
``compute_factors`` step ran the same load. These tests pin the fix at the level that failed — the
SQL the loader sends — rather than at a memory reading, which would depend on the machine:

* every bar statement carries a bound: a date lower bound no further back than the lookback's
  trading days before the as-of date, or a per-instrument ``LIMIT`` for the names that bound does
  not cover (a suspension, a hole) — and never a read of all history;
* the bounded load scores **exactly** as the whole history does, on a market with a suspended
  name, a late listing and a name with holes, so the bound cannot be tightened past what the book's
  windows read without a test failing.
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import polars as pl
import pytest
from helpers import TRADE_DATE, make_instrument, requires_db
from pandas.testing import assert_frame_equal
from sqlalchemy import event, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from baskfy_core.desk_score_service import score_day
from baskfy_core.models import FactorDaily, Instrument, OhlcvDaily, TradingDay
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.windows import HIGH_1Y_BARS, subtract_months
from baskfy_worker.engine import DEFAULT_LOOKBACK_DAYS, load_history
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.desk_score import (
    DESK_BAR_MARGIN,
    DESK_BAR_SCHEMA,
    desk_bar_lookback,
    desk_bar_window_start,
    load_carried,
    load_desk_bars,
    load_desk_trading_days,
    run_compute_desk_score,
)

#: Far more sessions than the lookback (~272), so an unbounded read is visibly larger.
SESSIONS: Final = 700


@dataclass(frozen=True)
class Shape:
    symbol: str
    #: Session indices (0 = oldest) this name has a bar on.
    sessions: tuple[int, ...]
    scanned: bool = True


def _all() -> tuple[int, ...]:
    return tuple(range(SESSIONS))


SHAPES: Final[tuple[Shape, ...]] = (
    Shape("BDREGA", _all()),
    Shape("BDREGB", _all()),
    Shape("BDREGC", _all()),
    Shape("BDREGD", _all()),
    # Suspended for most of the last year: its window reaches back across the hole.
    Shape("BDSUSP", tuple(i for i in _all() if i < 350 or i >= 600)),
    # Listed 100 sessions ago: short windows, and nothing older to top up with.
    Shape("BDNEWL", tuple(range(600, SESSIONS))),
    # Three missing days inside the lookback.
    Shape("BDHOLE", tuple(i for i in _all() if i not in (520, 610, 690))),
    # Bars but no factor row on the day: not scanned, never read.
    Shape("BDIDLE", _all(), scanned=False),
)


def _close(index: int, bar: int) -> Decimal:
    drift = 0.0002 + 0.0004 * index
    level = 100.0 * math.exp(drift * bar) * (1.0 + 0.03 * math.sin(bar / (6.0 + index) + index))
    return Decimal(str(round(level, 2)))


async def _sessions(session: AsyncSession) -> list[dt.date]:
    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date <= TRADE_DATE,
        )
        .order_by(TradingDay.date.desc())
        .limit(SESSIONS)
    )
    return sorted(row[0] for row in rows)


async def seed(session: AsyncSession) -> tuple[list[dt.date], dict[str, int]]:
    days = await _sessions(session)
    ids: dict[str, int] = {}
    for index, shape in enumerate(SHAPES):
        ids[shape.symbol] = await make_instrument(session, shape.symbol, token=900 + index)
        bars = []
        for bar in shape.sessions:
            close = _close(index, bar)
            volume = int(2_000_000 * (1.0 + 0.4 * math.sin(bar / 4.0 + index)))
            bars.append(
                {
                    "instrument_id": ids[shape.symbol],
                    "date": days[bar],
                    "open": close,
                    "high": (close * Decimal("1.012")).quantize(Decimal("0.01")),
                    "low": (close * Decimal("0.988")).quantize(Decimal("0.01")),
                    "close": close,
                    "volume": volume,
                    "close_raw": close,
                    "volume_raw": volume,
                    "adj_factor": Decimal(1),
                    "source": "nse",
                }
            )
        await session.execute(insert(OhlcvDaily), bars)
        if shape.scanned:
            session.add(
                FactorDaily(
                    instrument_id=ids[shape.symbol],
                    date=TRADE_DATE,
                    series="EQ",
                    marketcap_cr=Decimal(20_000),
                    beta_12m=Decimal("1.0"),
                    circuits_3m=0,
                    circuits_12m=0,
                    universe_mask=0,
                )
            )
    await session.flush()
    return days, ids


async def whole_history(session: AsyncSession, as_of: dt.date) -> pl.DataFrame:
    """The pre-fix read: every bar on or before ``as_of`` for the scanned NSE names."""
    scanned = select(FactorDaily.instrument_id).where(FactorDaily.date == as_of)
    rows = (
        await session.execute(
            select(
                Instrument.symbol,
                OhlcvDaily.instrument_id,
                OhlcvDaily.date,
                OhlcvDaily.open,
                OhlcvDaily.high,
                OhlcvDaily.low,
                OhlcvDaily.close,
                OhlcvDaily.volume,
                OhlcvDaily.close_raw,
                OhlcvDaily.volume_raw,
            )
            .join(Instrument, Instrument.id == OhlcvDaily.instrument_id)
            .where(
                Instrument.exchange_id == NSE_EXCHANGE_ID,
                OhlcvDaily.date <= as_of,
                OhlcvDaily.instrument_id.in_(scanned),
            )
            .order_by(Instrument.symbol, OhlcvDaily.date)
        )
    ).all()
    names = ("open", "high", "low", "close", "volume", "close_raw", "volume_raw")
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "instrument_id": [int(r[1]) for r in rows],
            "date": [r[2] for r in rows],
            **{name: [float(r[i]) for r in rows] for i, name in enumerate(names, start=3)},
        },
        schema=DESK_BAR_SCHEMA,
    )


@dataclass(frozen=True)
class Statement:
    sql: str
    parameters: tuple[object, ...]

    def bound(self, operator: str) -> object | None:
        """The parameter compared with ``ohlcv_daily.date`` by ``operator``, if there is one."""
        match = re.search(rf"ohlcv_daily\.date {re.escape(operator)} \$(\d+)", self.sql)
        return None if match is None else self.parameters[int(match.group(1)) - 1]


@contextmanager
def captured(engine: AsyncEngine) -> Iterator[list[Statement]]:
    """Every statement the driver is handed, with its positional parameters."""
    seen: list[Statement] = []

    def _record(*event_args: object) -> None:
        # (conn, cursor, statement, parameters, context, executemany)
        statement, parameters = event_args[2], event_args[3]
        values = tuple(parameters) if isinstance(parameters, (list, tuple)) else ()
        seen.append(Statement(str(statement), values))

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


def _bar_reads(statements: list[Statement]) -> list[Statement]:
    """Statements that read bar prices out of ``ohlcv_daily`` (not the calendar, not a count)."""
    return [s for s in statements if "ohlcv_daily.volume_raw" in s.sql and "FROM" in s.sql]


class TestLookbackArithmetic:
    def test_the_lookback_covers_the_twelve_month_window_plus_one_bar_and_the_252_bar_windows(
        self,
    ) -> None:
        weekdays = [
            dt.date(2024, 1, 1) + dt.timedelta(days=offset)
            for offset in range(1000)
            if (dt.date(2024, 1, 1) + dt.timedelta(days=offset)).weekday() < 5
        ]
        as_of = weekdays[-1]
        window_12m = bisect.bisect_right(weekdays, as_of) - bisect.bisect_right(
            weekdays, subtract_months(as_of, 12)
        )
        lookback = desk_bar_lookback(as_of, weekdays)
        assert lookback == max(window_12m + 1, HIGH_1Y_BARS) + DESK_BAR_MARGIN
        assert lookback < 400

        start = desk_bar_window_start(as_of, weekdays, lookback)
        assert len([d for d in weekdays if start <= d <= as_of]) == lookback
        # A calendar shorter than the lookback starts at its first day, and nothing is invented.
        assert desk_bar_window_start(as_of, weekdays[-10:], lookback) == weekdays[-10]
        assert desk_bar_window_start(as_of, [], lookback) == as_of


@pytest.mark.db
@requires_db
class TestBoundedDeskBars:
    async def test_every_bar_read_is_bounded_to_the_lookback_before_the_day(
        self, session: AsyncSession, engine: AsyncEngine
    ) -> None:
        days, _ids = await seed(session)
        lookback = desk_bar_lookback(TRADE_DATE, days)
        floor = days[-lookback]
        assert lookback * 2 < SESSIONS  # an unbounded read would be at least twice the size

        with captured(engine) as statements:
            written = await run_compute_desk_score(session, StepOutcome(), TRADE_DATE)
        assert written == sum(1 for shape in SHAPES if shape.scanned)

        reads = _bar_reads(statements)
        ranged = [s for s in reads if s.bound(">=") is not None]
        topped = [s for s in reads if "LIMIT" in s.sql and s.bound("<") is not None]
        assert len(ranged) == 1
        assert ranged[0].bound(">=") == floor
        assert ranged[0].bound("<=") == TRADE_DATE
        # The suspended name, the holed one and the new listing are topped up per instrument,
        # from strictly before the same floor, and each by a LIMIT rather than a date range.
        assert len(topped) == 1
        assert topped[0].bound("<") == floor
        # Nothing else read bars, so nothing read them unbounded.
        assert set(map(id, reads)) == set(map(id, ranged)) | set(map(id, topped))

    async def test_the_bounded_load_is_each_names_last_bars_and_scores_as_the_whole_history(
        self, session: AsyncSession
    ) -> None:
        days, ids = await seed(session)
        trading_days = await load_desk_trading_days(session, TRADE_DATE)
        assert trading_days == days
        lookback = desk_bar_lookback(TRADE_DATE, trading_days)
        floor = days[-lookback]

        bounded = await load_desk_bars(session, TRADE_DATE, trading_days)
        full = await whole_history(session, TRADE_DATE)
        assert full.height > 2 * bounded.height

        # Exactly each scanned name's last `lookback` bars — what the rolling windows read.
        expected = (
            full.sort("instrument_id", "date")
            .group_by("instrument_id", maintain_order=True)
            .tail(lookback)
            .select(list(DESK_BAR_SCHEMA))
            .sort("symbol", "date")
        )
        assert bounded.sort("symbol", "date").equals(expected)
        per_name = {
            symbol: frame
            for (symbol,), frame in bounded.partition_by("symbol", as_dict=True).items()
        }
        assert ids["BDIDLE"] not in set(bounded["instrument_id"].to_list())
        assert per_name["BDREGA"].height == lookback
        assert per_name["BDREGA"]["date"].min() == floor
        assert per_name["BDSUSP"].height == lookback
        suspended_first = per_name["BDSUSP"]["date"].min()
        assert isinstance(suspended_first, dt.date)
        assert suspended_first < floor  # reached back across the suspension
        assert per_name["BDHOLE"].height == lookback
        assert per_name["BDNEWL"].height == SESSIONS - 600
        assert bounded["date"].max() == TRADE_DATE

        carried = await load_carried(session, TRADE_DATE)
        assert_frame_equal(
            score_day(bounded, TRADE_DATE, trading_days, carried),
            score_day(full, TRADE_DATE, trading_days, carried),
        )

    async def test_the_factor_history_read_keeps_its_three_year_floor(
        self, session: AsyncSession, engine: AsyncEngine
    ) -> None:
        """``load_history`` was already bounded; streaming it must not have dropped the bound."""
        await seed(session)
        with captured(engine) as statements:
            history = await load_history(session, TRADE_DATE)
        reads = _bar_reads(statements)
        assert len(reads) == 1
        assert reads[0].bound(">=") == TRADE_DATE - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS)
        assert reads[0].bound("<=") == TRADE_DATE
        assert history.bars.height == sum(len(shape.sessions) for shape in SHAPES)
