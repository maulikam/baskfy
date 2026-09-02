"""SW9's runner: the backtest over bars read from the database, stored as a run.

`docs/swing/06` SW9: "a fixture year with a known planted flag reproduces the planted trade's R
to 2 dp" — here through the whole runner, not the engine alone: the planted year is written into
``ohlcv_daily`` on the NSE calendar, read back the way the detectors read bars, run, and stored
in ``sw_backtest_run``. Then the claims the runner makes on its own:

* **the bars are the detectors' bars** — the same frame ``load_swing_bars`` gives the nightly
  job, over ``start - 200 sessions .. end``, so the first session has its history;
* **survivorship is handled by ``instrument.delisted_on``** (`04` §11): a name delisted inside
  the run is in the frame, one delisted before it is not, and the nightly loader has neither;
* **a run is a fact** — a re-run inserts a second row and edits nothing; the stored ``stats``
  is ``result.to_json()`` byte for byte;
* **an empty range is an honest empty result**, not a crash;
* **a failure is recorded and re-raised** — ``error`` with the traceback, ``finished_at`` set,
  ``stats`` null, and the exception still reaches the caller;
* **the Celery binding** is ``baskfy.swing.backtest`` on the compute queue with no Beat entry,
  and its two-commit body leaves a failed run durable;
* **the index rule reads NIFTY 500 from ``index_snapshot_daily``** with the nightly job's NIFTY 50
  fallback, in-frame, and a run with no index says so in its caveats (SW9.6, A12);
* **a name delisted inside the run is sold at its last close and counted ``DELISTED``** (B2);
* **two runs with the same params and data are byte-identical** (B5).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from baskfy_core.models import (
    AppUser,
    IndexDef,
    IndexSnapshotDaily,
    Instrument,
    OhlcvDaily,
    SwConfig,
    TradingDay,
)
from baskfy_core.models.swing import SwBacktestRun
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.swing.backtest import (
    CAVEATS,
    INDEX_ABSENT_CAVEAT,
    BacktestCloseReason,
    BacktestParams,
    GateMode,
)
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, Setup
from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE
from baskfy_worker.db import run_checkpointed
from baskfy_worker.tasks.celery_tasks import swing_backtest_task
from baskfy_worker.tasks.swing import LOOKBACK_SESSIONS, load_swing_bars, lookback_start
from baskfy_worker.tasks.swing_backtest import (
    BARS_SCHEMA,
    DEFAULT_START,
    INDEX_FALLBACK_SLUG,
    INDEX_SCHEMA,
    INDEX_SLUG,
    SWING_BACKTEST_TASK,
    load_backtest_bars,
    load_backtest_calendar,
    load_backtest_index,
    load_delisted,
    params_for,
    params_json,
    resolve_index_slug,
    run_and_commit,
    run_swing_backtest,
)

# The planted year is the *specification* of the trade (R = 0.28, worked by hand in the
# fixture's docstring). A second copy of it here could drift from the one the engine is proved
# against, which would defeat the point of reproducing it through the database — so the
# fixture module is imported from the core suite's directory rather than retyped.
CORE_TESTS = Path(__file__).resolve().parents[3] / "packages" / "core" / "tests"
if str(CORE_TESTS) not in sys.path:
    sys.path.insert(0, str(CORE_TESTS))

from swing_backtest_fixtures import (  # noqa: E402 - path above
    DETECTION_BAR,
    ENTRY_BAR,
    PLANTED,
    WIN_TAIL,
    index_series,
    planted_frame,
)

pytestmark = [requires_db, pytest.mark.db]

#: The planted year starts here in the fixture; the runner test re-dates it onto NSE sessions
#: from the same day, so holidays fall where the calendar says they do.
FIXTURE_START = dt.date(2025, 10, 1)

#: A fixed clock, so `started_at` / `finished_at` are assertable.
T0 = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC)


def ticking(start: dt.datetime = T0) -> list[dt.datetime]:
    """A clock that advances a minute per reading; returns the readings it will give."""
    return [start + dt.timedelta(minutes=i) for i in range(10)]


class Clock:
    def __init__(self) -> None:
        self.readings = ticking()
        self.calls = 0

    def __call__(self) -> dt.datetime:
        reading = self.readings[self.calls]
        self.calls += 1
        return reading


async def _user(session: AsyncSession, *, email: str = "sw9@example.com") -> int:
    user = AppUser(public_id=email.split("@", maxsplit=1)[0], email=email)
    session.add(user)
    await session.flush()
    session.add(SwConfig(user_id=user.id, sleeve_capital_inr=Decimal(0), updated_by="test"))
    await session.flush()
    return int(user.id)


async def nse_sessions(session: AsyncSession, start: dt.date, count: int) -> list[dt.date]:
    rows = await session.execute(
        sa.select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date >= start,
        )
        .order_by(TradingDay.date)
        .limit(count)
    )
    return [row[0] for row in rows]


async def write_frame(
    session: AsyncSession,
    frame: pl.DataFrame,
    dates: dict[dt.date, dt.date],
    *,
    delisted: dict[str, dt.date] | None = None,
) -> dict[str, int]:
    """Write a fixture frame into ``ohlcv_daily``, each date mapped through ``dates``.

    Prices are stored at the four decimals `ohlcv_daily` keeps; `turnover` is left null so the
    indicator's ``close x volume`` fallback applies, as it does to the in-memory fixture.
    """
    ids: dict[str, int] = {}
    for symbol in frame["symbol"].unique().sort().to_list():
        ids[symbol] = await make_instrument(session, symbol, listed_on=dt.date(2011, 1, 1))
        if delisted and symbol in delisted:
            instrument = await session.get(Instrument, ids[symbol])
            assert instrument is not None
            instrument.delisted_on = delisted[symbol]
            instrument.is_active = False
    for row in frame.iter_rows(named=True):
        on = dates[row["date"]]
        session.add(
            OhlcvDaily(
                instrument_id=ids[row["symbol"]],
                date=on,
                open=Decimal(str(round(row["open"], 4))),
                high=Decimal(str(round(row["high"], 4))),
                low=Decimal(str(round(row["low"], 4))),
                close=Decimal(str(round(row["close"], 4))),
                volume=int(row["volume"]),
                close_raw=Decimal(str(round(row["close"], 4))),
                volume_raw=int(row["volume"]),
                turnover=None,
                adj_factor=Decimal(1),
                upper_circuit=None,
                source="nse",
            )
        )
    await session.flush()
    return ids


async def write_planted_year(
    session: AsyncSession, *, delisted: dict[str, dt.date] | None = None
) -> tuple[list[dt.date], dict[str, int]]:
    """The planted year on the NSE calendar; returns the sessions it covers and the ids."""
    frame, weekday_calendar = planted_frame()
    sessions = await nse_sessions(session, FIXTURE_START, len(weekday_calendar))
    assert len(sessions) == len(weekday_calendar)
    mapping = dict(zip(weekday_calendar, sessions, strict=True))
    ids = await write_frame(session, frame, mapping, delisted=delisted)
    return sessions, ids


async def _runs(session: AsyncSession, user_id: int) -> list[SwBacktestRun]:
    rows = await session.execute(
        sa.select(SwBacktestRun).where(SwBacktestRun.user_id == user_id).order_by(SwBacktestRun.id)
    )
    return list(rows.scalars())


class TestThePlantedYearThroughTheDatabase:
    async def test_the_planted_flag_reproduces_its_r_to_2dp(self, session: AsyncSession) -> None:
        """`06` SW9's acceptance, through the runner: written to `ohlcv_daily` on the NSE
        calendar, read back, run, stored — and the one trade is R = 0.28."""
        user_id = await _user(session)
        sessions, _ = await write_planted_year(session)
        params = await params_for(session, user_id=user_id, start=sessions[0], end=sessions[-1])

        row, result = await run_swing_backtest(session, user_id=user_id, params=params)

        assert [t.symbol for t in result.trades] == [PLANTED.symbol]
        trade = result.trades[0]
        assert trade.setup == Setup.FLAG.value
        assert trade.r_multiple == PLANTED.r_multiple
        assert trade.quantity == PLANTED.quantity
        assert trade.entry == PLANTED.entry_paid
        assert trade.initial_stop == PLANTED.stop
        assert trade.exit_avg == PLANTED.exit_avg
        assert row.stats is not None
        stored = row.stats["trades"]
        assert isinstance(stored, list)
        assert stored[0]["r_multiple"] == str(PLANTED.r_multiple)
        assert stored[0]["symbol"] == PLANTED.symbol

    async def test_the_stored_stats_are_to_json_exactly_and_the_run_is_a_row(
        self, session: AsyncSession
    ) -> None:
        clock = Clock()
        user_id = await _user(session)
        sessions, _ = await write_planted_year(session)
        params = await params_for(session, user_id=user_id, start=sessions[0], end=sessions[-1])

        row, result = await run_swing_backtest(session, user_id=user_id, params=params, clock=clock)

        assert row.stats == result.to_json()
        assert row.params == params_json(params) == result.to_json()["params"]
        assert row.error is None
        assert row.started_at == clock.readings[0]
        assert row.finished_at == clock.readings[1]
        assert row.stats["caveats"] == list(result.caveats) == [*CAVEATS, INDEX_ABSENT_CAVEAT]
        funnel = row.stats["funnel"]
        assert isinstance(funnel, dict)
        assert funnel["sessions"] == len(sessions)
        assert len(await _runs(session, user_id)) == 1

    async def test_the_run_is_sized_against_the_params_sleeve_not_sw_config(
        self, session: AsyncSession
    ) -> None:
        """`04` §11: "a constant ₹10 lakh sleeve". `sw_config.sleeve_capital_inr` is ₹0 here,
        as it is on every deployment until Maulik sets it, and the run still trades."""
        user_id = await _user(session)
        sessions, _ = await write_planted_year(session)
        params = await params_for(session, user_id=user_id, start=sessions[0], end=sessions[-1])
        assert params.sleeve_inr == Decimal(1_000_000)
        assert params.cost_pct_per_side == Decimal("0.13")

        _, result = await run_swing_backtest(session, user_id=user_id, params=params)

        assert result.trades[0].quantity == PLANTED.quantity

    async def test_the_users_liquidity_floors_are_the_runs_config(
        self, session: AsyncSession
    ) -> None:
        """The universe is the one the detectors run with tonight: `sw_config`'s three floors
        over the pack's defaults, nothing else from the settings row (`load_swing_config`)."""
        user_id = await _user(session)
        config = await session.get(SwConfig, user_id)
        assert config is not None
        config.adr_min_pct = Decimal("9.99")
        await session.flush()

        params = await params_for(
            session, user_id=user_id, start=dt.date(2026, 1, 1), end=dt.date(2026, 1, 31)
        )

        assert params.config.liquidity.adr_min_pct == 9.99
        assert params.config.flag == DEFAULT_SWING_CONFIG.flag
        assert params.config.sizing == DEFAULT_SWING_CONFIG.sizing

    async def test_a_given_sleeve_and_cost_override_the_defaults(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        params = await params_for(
            session,
            user_id=user_id,
            start=dt.date(2026, 1, 1),
            end=dt.date(2026, 1, 31),
            sleeve_inr=Decimal(500_000),
            cost_pct_per_side=Decimal("0.05"),
        )
        assert params.sleeve_inr == Decimal(500_000)
        assert params.cost_pct_per_side == Decimal("0.05")
        assert params_json(params)["sleeve_inr"] == "500000"


class TestTheBarsAreTheDetectorsBars:
    async def test_the_frame_is_load_swing_bars_over_the_lookback_window(
        self, session: AsyncSession
    ) -> None:
        """Bars from 200 sessions before `start` to `end`, exactly as the nightly job is given
        them — same columns, same rows, same adjusted space."""
        await _user(session)
        sessions, _ = await write_planted_year(session)
        start, end = sessions[60], sessions[-10]
        params = BacktestParams(start=start, end=end)

        mine = await load_backtest_bars(session, params)
        window_start = await lookback_start(session, start, LOOKBACK_SESSIONS)
        theirs = await load_swing_bars(session, window_start, end)

        assert mine.columns == theirs.columns == list(BARS_SCHEMA)
        assert mine.equals(theirs.cast(BARS_SCHEMA))
        assert mine["date"].min() == sessions[0] < start
        assert mine["date"].max() == end

    async def test_the_lookback_gives_the_first_session_its_history(
        self, session: AsyncSession
    ) -> None:
        """A run starting on the planted detection day still finds the flag: the 140 bars
        before it are in the frame even though the run's first session is after them."""
        user_id = await _user(session)
        sessions, _ = await write_planted_year(session)
        detection_day = sessions[139]
        params = await params_for(session, user_id=user_id, start=detection_day, end=sessions[-1])

        _, result = await run_swing_backtest(session, user_id=user_id, params=params)

        assert [t.symbol for t in result.trades] == [PLANTED.symbol]
        assert result.trades[0].r_multiple == PLANTED.r_multiple
        assert result.funnel["sessions"] == len(sessions) - 139

    async def test_the_calendar_is_the_nse_calendar_over_the_same_window(
        self, session: AsyncSession
    ) -> None:
        await _user(session)
        sessions, _ = await write_planted_year(session)
        params = BacktestParams(start=sessions[60], end=sessions[-1])

        calendar = await load_backtest_calendar(session, params)
        window_start = await lookback_start(session, sessions[60], LOOKBACK_SESSIONS)

        assert calendar[0] == window_start
        assert calendar[-1] == sessions[-1]
        assert calendar == sorted(set(calendar))
        assert all(day.weekday() < 5 for day in calendar)
        # A weekday holiday inside the window is a calendar row and not a session.
        holidays = await session.execute(
            sa.select(TradingDay.date).where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID,
                TradingDay.is_trading_day.is_(False),
                TradingDay.date >= window_start,
                TradingDay.date <= sessions[-1],
            )
        )
        weekday_holidays = [row[0] for row in holidays if row[0].weekday() < 5]
        assert weekday_holidays, "the seeded calendar has no weekday holiday in the window"
        assert not set(weekday_holidays) & set(calendar)

    async def test_survivorship_is_handled_by_delisted_on(self, session: AsyncSession) -> None:
        """`04` §11's third caveat. A name delisted inside the run is in the frame up to its
        last bar; a name delisted before the run is not; the nightly loader has neither."""
        await _user(session)
        frame, weekday_calendar = planted_frame()
        sessions = await nse_sessions(session, FIXTURE_START, len(weekday_calendar))
        mapping = dict(zip(weekday_calendar, sessions, strict=True))
        start = sessions[60]
        await write_frame(
            session,
            frame,
            mapping,
            delisted={"TAPECO": sessions[100], "FLATCO": sessions[10]},
        )
        params = BacktestParams(start=start, end=sessions[-1])

        mine = await load_backtest_bars(session, params)
        window_start = await lookback_start(session, start, LOOKBACK_SESSIONS)
        theirs = await load_swing_bars(session, window_start, sessions[-1])

        assert set(mine["symbol"].unique().to_list()) == {PLANTED.symbol, "TAPECO"}
        assert set(theirs["symbol"].unique().to_list()) == {PLANTED.symbol}

    async def test_a_name_delisted_on_the_first_session_is_still_in(
        self, session: AsyncSession
    ) -> None:
        """The boundary: `delisted_on >= start` keeps a name retired on the run's first day."""
        await _user(session)
        frame, weekday_calendar = planted_frame()
        sessions = await nse_sessions(session, FIXTURE_START, len(weekday_calendar))
        mapping = dict(zip(weekday_calendar, sessions, strict=True))
        start = sessions[60]
        await write_frame(session, frame, mapping, delisted={"TAPECO": start})

        mine = await load_backtest_bars(session, BacktestParams(start=start, end=sessions[-1]))

        assert "TAPECO" in mine["symbol"].unique().to_list()


class TestARunIsAFact:
    async def test_two_runs_are_two_rows_and_the_first_is_untouched(
        self, session: AsyncSession
    ) -> None:
        """House rule 7 read for a result table: re-running inserts, never edits."""
        user_id = await _user(session)
        sessions, _ = await write_planted_year(session)
        params = await params_for(session, user_id=user_id, start=sessions[0], end=sessions[-1])

        first, _ = await run_swing_backtest(session, user_id=user_id, params=params)
        first_stats = dict(first.stats or {})
        first_finished = first.finished_at
        second, _ = await run_swing_backtest(session, user_id=user_id, params=params)

        runs = await _runs(session, user_id)
        assert [run.id for run in runs] == [first.id, second.id]
        assert second.id > first.id
        assert runs[0].stats == first_stats
        assert runs[0].finished_at == first_finished
        assert runs[1].stats == first_stats  # deterministic: same inputs, same JSON

    async def test_a_range_with_no_bars_is_an_honest_empty_result(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        params = await params_for(
            session, user_id=user_id, start=dt.date(2024, 6, 3), end=dt.date(2024, 6, 7)
        )

        row, result = await run_swing_backtest(session, user_id=user_id, params=params)

        assert result.trades == ()
        assert result.stats.trades == 0
        assert result.funnel["sessions"] == 5
        assert result.funnel["candidates"] == 0
        assert row.error is None
        assert row.finished_at is not None
        assert row.stats is not None
        stats = row.stats["stats"]
        assert isinstance(stats, dict)
        assert stats["trades"] == 0
        assert row.stats["trades"] == []

    async def test_a_failure_is_recorded_on_the_row_and_re_raised(
        self, session: AsyncSession
    ) -> None:
        """The engine refuses `end < start`; the runner writes the refusal down — type, message
        and traceback — sets `finished_at`, leaves `stats` null, and lets the exception out."""
        clock = Clock()
        user_id = await _user(session)
        params = await params_for(
            session, user_id=user_id, start=dt.date(2024, 6, 7), end=dt.date(2024, 6, 3)
        )

        with pytest.raises(ValueError, match="before start"):
            await run_swing_backtest(session, user_id=user_id, params=params, clock=clock)

        runs = await _runs(session, user_id)
        assert len(runs) == 1
        row = runs[0]
        assert row.error is not None
        assert row.error.startswith("ValueError: end 2024-06-03 is before start 2024-06-07")
        assert "Traceback (most recent call last)" in row.error
        assert "run_backtest" in row.error
        assert row.stats is None
        assert row.started_at == clock.readings[0]
        assert row.finished_at == clock.readings[1]
        assert row.params == params_json(params)


async def write_index(
    session: AsyncSession,
    slug: str,
    frame: pl.DataFrame,
    dates: dict[dt.date, dt.date],
    *,
    keep: int | None = None,
) -> int:
    """``index_snapshot_daily`` rows for ``slug`` from an ``index_series`` frame, each date
    mapped through ``dates``; ``keep`` writes only the last that many closes."""
    index_id = (
        await session.execute(sa.select(IndexDef.id).where(IndexDef.slug == slug))
    ).scalar_one()
    rows = list(frame.iter_rows(named=True))
    if keep is not None:
        rows = rows[-keep:]
    for row in rows:
        session.add(
            IndexSnapshotDaily(
                index_id=index_id,
                date=dates[row["date"]],
                level=Decimal(str(round(row["close"], 2))),
            )
        )
    await session.flush()
    return len(rows)


class TestTheIndexRule:
    async def test_the_index_is_nifty_500_from_index_snapshot_daily_read_in_frame(
        self, session: AsyncSession
    ) -> None:
        """A12: NIFTY 500's closes, the averages computed from the closes on or before each
        session. A crash in the detection day's close turns the gate RED that evening — the
        planted flag is refused — and the stored run names the series it read."""
        user_id = await _user(session)
        frame, weekday_calendar = planted_frame()
        sessions = await nse_sessions(session, FIXTURE_START, len(weekday_calendar))
        mapping = dict(zip(weekday_calendar, sessions, strict=True))
        await write_frame(session, frame, mapping)
        await write_index(
            session, INDEX_SLUG, index_series(weekday_calendar, crash_on=DETECTION_BAR), mapping
        )
        params = await params_for(session, user_id=user_id, start=sessions[0], end=sessions[-1])
        assert params.index_slug == INDEX_SLUG == "nifty-500"

        row, result = await run_swing_backtest(session, user_id=user_id, params=params)

        assert result.trades == ()
        assert result.funnel["skipped_gate"] >= 1
        assert result.comparison is not None
        assert result.comparison.index_supplied is True
        assert result.comparison.primary is GateMode.FULL
        assert result.comparison.overall[GateMode.BREADTH_ONLY].entered == 1
        assert result.comparison.index_rule is not None
        assert result.comparison.index_rule.overall.entered == -1
        assert result.caveats == CAVEATS
        assert row.params["index_slug"] == "nifty-500"
        assert row.stats is not None
        assert row.stats["caveats"] == list(CAVEATS)
        stored_ladder = row.stats["ladder"]
        assert isinstance(stored_ladder, list)
        ladder = {day: gate for day, gate, _ in stored_ladder}
        assert ladder[sessions[DETECTION_BAR - 1].isoformat()] == "GREEN"
        assert ladder[sessions[DETECTION_BAR].isoformat()] == "RED"

    async def test_the_runner_falls_back_to_nifty_50_when_nifty_500_has_too_few_closes(
        self, session: AsyncSession
    ) -> None:
        """The nightly job's fallback, on the window: NIFTY 500 with fewer than `index_ma_slow`
        closes in it is not a series; NIFTY 50 with enough is."""
        user_id = await _user(session)
        frame, weekday_calendar = planted_frame()
        sessions = await nse_sessions(session, FIXTURE_START, len(weekday_calendar))
        mapping = dict(zip(weekday_calendar, sessions, strict=True))
        await write_frame(session, frame, mapping)
        slow = DEFAULT_SWING_CONFIG.market.index_ma_slow
        await write_index(
            session, INDEX_SLUG, index_series(weekday_calendar), mapping, keep=slow - 1
        )
        await write_index(session, INDEX_FALLBACK_SLUG, index_series(weekday_calendar), mapping)

        slug = await resolve_index_slug(
            session, start=sessions[0], end=sessions[-1], config=DEFAULT_SWING_CONFIG
        )
        assert slug == INDEX_FALLBACK_SLUG == "nifty-50"
        params = await params_for(session, user_id=user_id, start=sessions[0], end=sessions[-1])
        assert params.index_slug == "nifty-50"

        row, result = await run_swing_backtest(session, user_id=user_id, params=params)

        assert [t.symbol for t in result.trades] == [PLANTED.symbol], "a rising index is long"
        assert result.trades[0].r_multiple == PLANTED.r_multiple
        assert result.caveats == CAVEATS
        assert row.params["index_slug"] == "nifty-50"

    async def test_no_index_at_all_is_a_breadth_only_run_and_the_caveat_says_so(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        sessions, _ = await write_planted_year(session)

        params = await params_for(session, user_id=user_id, start=sessions[0], end=sessions[-1])
        assert params.index_slug is None
        assert await load_backtest_index(session, params) is None
        row, result = await run_swing_backtest(session, user_id=user_id, params=params)

        assert result.comparison is not None
        assert result.comparison.index_supplied is False
        assert result.comparison.primary is GateMode.BREADTH_ONLY
        assert result.comparison.index_rule is None
        assert INDEX_ABSENT_CAVEAT in result.caveats
        assert row.stats is not None
        stored_caveats = row.stats["caveats"]
        assert isinstance(stored_caveats, list)
        assert INDEX_ABSENT_CAVEAT in stored_caveats
        assert row.params["index_slug"] is None

    async def test_the_index_frame_covers_the_lookback_window_oldest_first(
        self, session: AsyncSession
    ) -> None:
        await _user(session)
        frame, weekday_calendar = planted_frame()
        sessions = await nse_sessions(session, FIXTURE_START, len(weekday_calendar))
        mapping = dict(zip(weekday_calendar, sessions, strict=True))
        await write_frame(session, frame, mapping)
        await write_index(session, INDEX_SLUG, index_series(weekday_calendar), mapping)
        start, end = sessions[60], sessions[-10]
        params = BacktestParams(start=start, end=end, index_slug=INDEX_SLUG)

        index = await load_backtest_index(session, params)

        assert index is not None
        assert index.schema == INDEX_SCHEMA
        window_start = await lookback_start(session, start, LOOKBACK_SESSIONS)
        assert window_start < sessions[0], "200 sessions reach back before the written year"
        assert index["date"].min() == sessions[0], "every written close inside the window is read"
        assert index["date"].max() == end
        assert index["date"].is_sorted()
        assert index.height == len([s for s in sessions if window_start <= s <= end])
        assert index["close"][0] == 1000.0


class TestDelistedNames:
    async def test_a_name_delisted_inside_the_run_is_sold_at_its_last_close_and_counted(
        self, session: AsyncSession
    ) -> None:
        """B2 through the runner: `instrument.delisted_on` reaches the engine, the planted name
        is sold at its last close on its last bar, and the funnel says `DELISTED`."""
        user_id = await _user(session)
        frame, weekday_calendar = planted_frame()
        sessions = await nse_sessions(session, FIXTURE_START, len(weekday_calendar))
        mapping = dict(zip(weekday_calendar, sessions, strict=True))
        last_bar = weekday_calendar[ENTRY_BAR + 1]
        gone = frame.filter(~((pl.col("symbol") == PLANTED.symbol) & (pl.col("date") > last_bar)))
        ids = await write_frame(
            session, gone, mapping, delisted={PLANTED.symbol: mapping[last_bar]}
        )
        params = await params_for(session, user_id=user_id, start=sessions[0], end=sessions[-1])

        bars = await load_backtest_bars(session, params)
        assert await load_delisted(session, bars) == {ids[PLANTED.symbol]: mapping[last_bar]}
        row, result = await run_swing_backtest(session, user_id=user_id, params=params)

        assert [t.symbol for t in result.trades] == [PLANTED.symbol]
        trade = result.trades[0]
        assert trade.close_reason == BacktestCloseReason.DELISTED.value
        assert trade.exit_date == mapping[last_bar]
        assert trade.exit_avg == (
            Decimal(str(WIN_TAIL[1].close)) * (1 - params.cost_pct_per_side / 100)
        ).quantize(Decimal("0.01"))
        assert result.funnel["closed_delisted"] == 1
        assert result.funnel["closed_no_bar"] == 0
        assert row.stats is not None
        stored_funnel = row.stats["funnel"]
        assert isinstance(stored_funnel, dict)
        assert stored_funnel["closed_delisted"] == 1


class TestDeterminism:
    async def test_two_runs_with_the_same_params_and_data_are_byte_identical(
        self, session: AsyncSession
    ) -> None:
        """B5, with the index and a delisting in play: the engine's JSON is the same bytes twice,
        and so are the two stored rows once JSONB has had its way with the key order."""
        user_id = await _user(session)
        frame, weekday_calendar = planted_frame()
        sessions = await nse_sessions(session, FIXTURE_START, len(weekday_calendar))
        mapping = dict(zip(weekday_calendar, sessions, strict=True))
        await write_frame(
            session, frame, mapping, delisted={"TAPECO": mapping[weekday_calendar[-3]]}
        )
        await write_index(
            session, INDEX_SLUG, index_series(weekday_calendar, crash_on=ENTRY_BAR + 6), mapping
        )
        params = await params_for(session, user_id=user_id, start=sessions[0], end=sessions[-1])
        assert params.index_slug == INDEX_SLUG

        first_row, first = await run_swing_backtest(session, user_id=user_id, params=params)
        second_row, second = await run_swing_backtest(session, user_id=user_id, params=params)

        assert json.dumps(first.to_json()).encode() == json.dumps(second.to_json()).encode()
        assert first_row.id != second_row.id
        assert json.dumps(first_row.stats, sort_keys=True) == json.dumps(
            second_row.stats, sort_keys=True
        )
        assert first_row.params == second_row.params == params_json(params)
        stats = first_row.stats
        assert stats is not None
        comparison = stats["comparison"]
        assert isinstance(comparison, dict)
        assert comparison["index_supplied"] is True
        assert sorted(comparison["modes"]) == ["breadth_only", "full", "gate_off"]
        drawdown = stats["drawdown"]
        assert isinstance(drawdown, dict)
        assert drawdown["max_pct"] == str(first.drawdown.max_pct)


class TestTheCeleryBinding:
    def test_it_is_named_and_routed_to_the_compute_queue_with_no_beat_entry(self) -> None:
        assert SWING_BACKTEST_TASK == "baskfy.swing.backtest"
        assert swing_backtest_task.name == SWING_BACKTEST_TASK
        assert swing_backtest_task.queue == QUEUE_COMPUTE
        assert swing_backtest_task.acks_late is True
        assert all(entry["task"] != SWING_BACKTEST_TASK for entry in BEAT_SCHEDULE.values())

    def test_the_default_start_is_2017(self) -> None:
        """`04` §11: "For each session 2017→"."""
        assert dt.date(2017, 1, 1) == DEFAULT_START

    def test_without_a_sole_user_the_task_skips_rather_than_inventing_a_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BASKFY_SOLE_USER_ID", raising=False)
        outcome = swing_backtest_task(start="2024-06-03", end="2024-06-07")
        assert outcome == {"start": "2024-06-03", "skipped": "no BASKFY_SOLE_USER_ID configured"}

    async def test_the_task_body_leaves_a_failed_run_durable(
        self, engine: AsyncEngine, migrated_url: str, clean_db: None
    ) -> None:
        """The body owns its commits: the error row survives the exception that follows it, so
        a failed nine-year run is a row an operator can read, not a rollback."""
        del clean_db
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as setup, setup.begin():
            user_id = await _user(setup, email="sw9-durable@example.com")

        async def body(session: AsyncSession) -> object:
            return await run_and_commit(
                session,
                user_id=user_id,
                start=dt.date(2024, 6, 7),
                end=dt.date(2024, 6, 3),
            )

        with pytest.raises(ValueError, match="before start"):
            await asyncio.to_thread(run_checkpointed, body, migrated_url)

        async with maker() as check:
            runs = await _runs(check, user_id)
        assert len(runs) == 1
        assert runs[0].error is not None
        assert runs[0].error.startswith("ValueError")
        assert runs[0].finished_at is not None
        assert runs[0].stats is None

    async def test_the_task_body_returns_the_summary_of_a_stored_run(
        self, engine: AsyncEngine, migrated_url: str, clean_db: None
    ) -> None:
        del clean_db
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as setup, setup.begin():
            user_id = await _user(setup, email="sw9-summary@example.com")

        async def body(session: AsyncSession) -> object:
            return await run_and_commit(
                session,
                user_id=user_id,
                start=dt.date(2024, 6, 3),
                end=dt.date(2024, 6, 7),
                sleeve_inr=Decimal(250_000),
            )

        outcome = await asyncio.to_thread(run_checkpointed, body, migrated_url)

        assert isinstance(outcome, dict)
        assert outcome["trades"] == 0
        assert outcome["sessions"] == 5
        assert outcome["start"] == "2024-06-03"
        assert outcome["finished_at"] is not None
        async with maker() as check:
            runs = await _runs(check, user_id)
        assert [run.id for run in runs] == [outcome["run_id"]]
        assert runs[0].params["sleeve_inr"] == "250000"
        assert runs[0].error is None
