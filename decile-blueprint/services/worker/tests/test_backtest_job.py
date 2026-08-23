"""The backtest against a real database — Prompt 15 deliverables 3, 4 and 5's server side.

``packages/core/tests/test_backtest.py`` proves the *engine*: docs/10's six correctness tests run
against a market whose right answer is known in closed form. This suite proves the half that
engine cannot — that the panel handed to it comes out of PostgreSQL point in time, that the screen
really is re-run as of each rebalance date, that a delisting recorded in ``instrument.delisted_on``
reaches the simulation, and that the job writes what docs/10 says it writes and where.

The market is small and synthetic, and built here rather than seeded, because the seeded
reference export is a single trading day of *results* (CLAUDE.md, ``test_reference_parity``).
Twelve names over six months is enough to exercise every join; making it larger would only make
the suite slower.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Final

import helpers
import polars as pl
import pytest
from helpers import requires_db
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_api.backtest_runner import BacktestRunner
from baskfy_api.backtests import artefact_key, build_payload, new_public_id
from baskfy_api.settings import Settings
from baskfy_core.backtest import (
    BacktestConfig,
    BacktestDataError,
    LookAheadError,
    PointInTimeReader,
    RebalanceFrequency,
    RebalanceSpec,
    SelectionSpec,
    TradeReason,
    rebalance_dates,
)
from baskfy_core.models import (
    AppUser,
    Backtest,
    FactorDaily,
    IndexMemberDaily,
    Instrument,
    OhlcvDaily,
    Screen,
)
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.universes import UNIVERSE_BY_SLUG
from baskfy_providers.archive import LocalRawArchive
from baskfy_worker.backtest import (
    _bar_frame,
    _require_a_loadable_size,
    execute_backtest,
    load_backtest_data,
    trading_calendar,
)
from baskfy_worker.tasks import backtests as job_module
from baskfy_worker.tasks.backtests import BacktestNotRunnable, _claim, run_backtest_job

pytestmark = [requires_db, pytest.mark.db]

START: Final = dt.date(2026, 3, 2)
END: Final = dt.date(2026, 8, 18)
UNIVERSE: Final = "nifty-500"
NAMES: Final = 12

#: The fifteen-year window docs/11 §"Performance budgets" prices, inside the seeded calendar.
LONG_START: Final = dt.date(2011, 1, 3)
LONG_END: Final = dt.date(2026, 1, 2)
#: Roughly the size of docs/13's reference export (271 rows), so the screen query does real work.
PERF_INSTRUMENTS: Final = 250

#: The name that stops trading part-way through, so survivorship has something to bite on.
DOOMED: Final = "SYNDEAD"
DOOMED_LAST_DAY: Final = dt.date(2026, 6, 15)


class RecordingPublisher:
    """Collects the frames the job would have published, so the SSE contract is assertable."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, str]] = []

    def publish(self, public_id: str, payload: str) -> None:
        self.frames.append((public_id, payload))


async def _seed_market(session: AsyncSession) -> dict[str, int]:
    """Twelve instruments, their bars, their factor rows and their index membership.

    Prices are deterministic and monotone per name — name ``i`` compounds at ``i`` basis points a
    day — so the screen's ranking is knowable without running it, and a test that asserts "the
    best-ranked name was bought" is asserting something.
    """
    calendar = await trading_calendar(session, START, END)
    assert calendar, "the seeded trading calendar is empty for this window"
    index_id = UNIVERSE_BY_SLUG[UNIVERSE].index_id
    mask = UNIVERSE_BY_SLUG[UNIVERSE].mask_value

    ids: dict[str, int] = {}
    for position in range(NAMES):
        symbol = DOOMED if position == 0 else f"SYN{position:03d}"
        instrument = Instrument(
            exchange_id=NSE_EXCHANGE_ID,
            symbol=symbol,
            name=f"{symbol} LIMITED",
            series="EQ",
            instrument_type="EQ",
            is_active=position != 0,
            delisted_on=DOOMED_LAST_DAY if position == 0 else None,
        )
        session.add(instrument)
        await session.flush()
        ids[symbol] = instrument.id

        base = Decimal(100 + position * 25)
        step = Decimal(1) + Decimal(position) / Decimal(10_000)
        price = base
        for day in calendar:
            if symbol == DOOMED and day >= DOOMED_LAST_DAY:
                # No bar at all after the delisting date. Never a zero, never a carried-forward
                # price — that is the forward-fill docs/10 §8 forbids.
                continue
            price = (price * step).quantize(Decimal("0.0001"))
            session.add(
                OhlcvDaily(
                    instrument_id=instrument.id,
                    date=day,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=100_000,
                    close_raw=price,
                    volume_raw=100_000,
                    adj_factor=Decimal(1),
                    source="nse",
                )
            )
            session.add(
                FactorDaily(
                    instrument_id=instrument.id,
                    date=day,
                    close=price,
                    close_raw=price,
                    ret_12m=Decimal(position * 10),
                    vol_12m=Decimal("0.2") + Decimal(position) / Decimal(100),
                    marketcap_cr=1_000 + position * 100,
                    series="EQ",
                    universe_mask=mask,
                )
            )
            session.add(
                IndexMemberDaily(
                    index_id=index_id,
                    date=day,
                    instrument_id=instrument.id,
                    source="nse_file",
                )
            )
    await session.flush()
    return ids


def _definition() -> ScreenDefinition:
    return ScreenDefinition.model_validate({"index": UNIVERSE, "sort_by": "ret_12m"})


def _config(**overrides: object) -> BacktestConfig:
    base: dict[str, object] = {
        "start": START,
        "end": END,
        "initial_capital": Decimal(1_000_000),
        "rebalance": RebalanceSpec(frequency=RebalanceFrequency.MONTHLY),
        "selection": SelectionSpec(top_n=5, hold_buffer=2),
        "benchmark": UNIVERSE,
    }
    base.update(overrides)
    return BacktestConfig.model_validate(base)


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


async def test_the_screen_is_run_as_of_each_rebalance_date(session: AsyncSession) -> None:
    """docs/10 §"Execution model" step 1: "Run the screen **as of `d`**"."""
    await _seed_market(session)
    loaded = await load_backtest_data(session, _config(), _definition(), with_offsets=False)

    assert loaded.schedule, "no rebalance dates were computed"
    for day in loaded.schedule:
        frame = loaded.data.screens[day]
        assert frame.height > 0
        # Every row carries the as-of, which is what the engine's guard checks.
        assert set(frame.get_column("date").to_list()) == {day}
        assert frame.get_column("rank").to_list() == sorted(frame.get_column("rank").to_list())


async def test_the_panel_is_restricted_to_names_the_screen_could_select(
    session: AsyncSession,
) -> None:
    """A name the book can never hold needs no price. See the module docstring in
    ``baskfy_worker.backtest``."""
    await _seed_market(session)
    loaded = await load_backtest_data(session, _config(), _definition(), with_offsets=False)
    assert 0 < loaded.instruments <= NAMES
    assert loaded.bars > 0


async def test_the_guard_rejects_a_frame_the_loader_could_not_have_produced(
    session: AsyncSession,
) -> None:
    """The loader stamps each frame with the as-of it ran for; the guard is what enforces it."""
    await _seed_market(session)
    loaded = await load_backtest_data(session, _config(), _definition(), with_offsets=False)
    reader = PointInTimeReader(loaded.data)
    reader.advance_to(loaded.schedule[0])
    with pytest.raises(LookAheadError):
        reader.screen_on(loaded.schedule[1])


async def test_an_empty_window_is_refused(session: AsyncSession) -> None:
    await _seed_market(session)
    with pytest.raises(BacktestDataError):
        await load_backtest_data(
            session,
            _config(start=dt.date(2019, 1, 1), end=dt.date(2019, 6, 30)),
            _definition(),
            with_offsets=False,
        )


async def test_a_rebalance_date_with_no_index_membership_is_refused(
    session: AsyncSession,
) -> None:
    """M45. The screen needs TWO things per as-of date, and M39's guard checked one.

    `baskfy_core.screener` joins `index_member_daily` on `(index_id, date)` with no snap-back, so
    a date with thousands of factor rows and no membership returns nothing — indistinguishable, to
    the engine, from a date with no factors at all.

    It is not hypothetical. `index_member_daily` begins 2021-08-02 and `factor_daily` reaches back
    to 2017-01-02. Measured on the live database, monthly over 2017-01-01..2026-08-18: 117
    rebalance dates, **117 passed the old guard, 15 had membership**. The guard was waving through
    102 dates guaranteed to screen empty, and the runs using them reported plausible numbers.
    """
    await _seed_market(session)
    config = _config()
    schedule = rebalance_dates(
        await trading_calendar(session, config.start, config.end),
        config.start,
        config.end,
        config.rebalance,
    )
    assert len(schedule) > 2

    # Factors intact; membership taken away from one rebalance date only.
    await session.execute(
        delete(IndexMemberDaily).where(IndexMemberDaily.date == schedule[1]),
    )

    with pytest.raises(BacktestDataError, match="index membership"):
        await load_backtest_data(session, config, _definition(), with_offsets=False)


async def test_a_rebalance_date_with_no_factor_rows_is_refused(session: AsyncSession) -> None:
    """M39. A gap in `factor_daily` must fail the load, not produce a plausible curve.

    ## Why this is the most valuable refusal in the loader

    An empty screen frame has two completely different causes and one appearance. Either every
    filter excluded every name — a real result, and the engine correctly goes to cash — or no
    factors were ever computed for that date, in which case the screen was never asked a question
    it could answer. The engine sees the same empty frame both ways.

    Against nine years of real Kite history this was not hypothetical: a monthly 2022-2026 run
    found factor rows on 15 of its 57 rebalance dates and reported +13.8% as a result, having sat
    the book in cash for the other 42. Nothing downstream could have detected it.
    """
    await _seed_market(session)
    config = _config()
    schedule = rebalance_dates(
        await trading_calendar(session, config.start, config.end),
        config.start,
        config.end,
        config.rebalance,
    )
    assert len(schedule) > 2

    # Take the factors away from one rebalance date only. One is enough: on that date the book
    # would be liquidated for a reason that has nothing to do with the strategy.
    await session.execute(
        delete(FactorDaily).where(FactorDaily.date == schedule[1]),
    )

    with pytest.raises(BacktestDataError, match="no factor rows"):
        await load_backtest_data(session, config, _definition(), with_offsets=False)


async def test_the_refusal_names_the_dates_so_it_can_be_acted_on(session: AsyncSession) -> None:
    """ "Something is missing" is not a bug report. The message has to say what and what to do."""
    await _seed_market(session)
    config = _config()
    await session.execute(delete(FactorDaily))

    with pytest.raises(BacktestDataError) as caught:
        await load_backtest_data(session, config, _definition(), with_offsets=False)

    message = str(caught.value)
    assert "have no factor rows" in message
    assert "compute_factors" in message
    assert "refresh_index_membership" in message, "the other half of what a screen needs"
    assert config.start.isoformat()[:4] in message, "no date was named"


# ---------------------------------------------------------------------------
# The simulation, end to end against the database
# ---------------------------------------------------------------------------


async def test_a_backtest_over_the_database_trades_and_compounds(session: AsyncSession) -> None:
    await _seed_market(session)
    config = _config()
    outcome = await execute_backtest(session, config, _definition(), fragility=False)
    result = outcome.result

    assert len(result.dates) > 100
    assert result.trades, "nothing was traded"
    assert result.holdings, "no per-rebalance holdings were recorded"
    assert result.total_costs > 0
    # Every name compounds upward in this fixture, so a momentum screen must make money.
    assert result.final_equity > config.initial_capital


async def test_the_best_ranked_name_is_bought_first(session: AsyncSession) -> None:
    """``ret_12m`` is highest for the last-numbered name, so it must be rank 1 and held."""
    ids = await _seed_market(session)
    outcome = await execute_backtest(session, _config(), _definition(), fragility=False)
    best = ids[f"SYN{NAMES - 1:03d}"]
    held = {holding.instrument_id for holding in outcome.result.holdings}
    assert best in held


async def test_a_delisted_holding_is_liquidated_from_the_reference_data(
    session: AsyncSession,
) -> None:
    """docs/10 §8, driven by ``instrument.delisted_on`` rather than by a fixture flag.

    ``SYNDEAD`` has the worst ``ret_12m``, so a top-5 momentum screen would never buy it. The run
    below widens the selection to the whole universe so the book *does* hold it when it dies —
    which is the only way to check that the liquidation happens at all.
    """
    ids = await _seed_market(session)
    config = _config(selection=SelectionSpec(top_n=NAMES, hold_buffer=0))
    outcome = await execute_backtest(session, config, _definition(), fragility=False)

    delisted = [trade for trade in outcome.result.trades if trade.reason is TradeReason.DELIST]
    assert delisted, "the delisted name was never sold"
    assert delisted[0].instrument_id == ids[DOOMED]
    assert delisted[0].date >= DOOMED_LAST_DAY
    assert outcome.result.delistings[0].priced_on < DOOMED_LAST_DAY
    # And it is gone afterwards: nothing was forward-filled.
    assert all(
        holding.instrument_id != ids[DOOMED]
        for holding in outcome.result.holdings
        if holding.executed_on > delisted[0].date
    )


async def test_the_fragility_probe_runs_five_configurations(session: AsyncSession) -> None:
    """docs/10 §"honesty features": +/-1 rebalance day and +/-25% costs."""
    await _seed_market(session)
    outcome = await execute_backtest(session, _config(), _definition(), fragility=True)
    assert outcome.fragility is not None
    assert len(outcome.fragility.variants) == 4
    assert outcome.loaded.offset_dates, "the offset probe needs its own screen results"


# ---------------------------------------------------------------------------
# The job (Prompt 15 §4)
# ---------------------------------------------------------------------------


async def _queued(session: AsyncSession, config: BacktestConfig) -> Backtest:
    user_id = await _user(session)
    screen = Screen(
        public_id=new_public_id()[:12],
        user_id=user_id,
        name="Synthetic momentum",
        definition=json.loads(_definition().model_dump_json()),
        columns=[],
    )
    session.add(screen)
    await session.flush()
    row = Backtest(
        public_id=new_public_id(),
        user_id=user_id,
        screen_id=screen.id,
        config=json.loads(config.model_dump_json()),
        status="queued",
    )
    session.add(row)
    await session.flush()
    return row


async def _user(session: AsyncSession) -> int:
    user = AppUser(public_id="backtestuser", email="backtest@example.com", name="Backtester")
    session.add(user)
    await session.flush()
    return user.id


async def test_the_job_writes_metrics_a_curve_and_three_artefacts(
    session: AsyncSession, tmp_path: Path
) -> None:
    """docs/10: "Large artefacts … go to R2; `backtest.metrics` and a downsampled equity curve
    live in Postgres for fast page loads"."""
    await _seed_market(session)
    row = await _queued(session, _config())
    archive = LocalRawArchive(tmp_path)
    publisher = RecordingPublisher()

    outcome = await run_backtest_job(
        session, row.public_id, archive=archive, publisher=publisher, fragility=False
    )

    assert outcome.status == "done"
    refreshed = (await session.execute(select(Backtest).where(Backtest.id == row.id))).scalar_one()
    assert refreshed.status == "done"
    assert refreshed.finished_at is not None
    assert refreshed.trades_key == artefact_key(row.public_id, "trades")
    assert isinstance(refreshed.metrics, dict)
    assert refreshed.metrics["metrics_hash"] == outcome.metrics_hash
    assert refreshed.metrics["cagr"] is not None
    assert refreshed.metrics["assumptions"]
    assert isinstance(refreshed.equity_curve, dict)
    assert refreshed.equity_curve["points"]

    for artefact in ("trades", "holdings", "equity"):
        assert archive.exists(artefact_key(row.public_id, artefact))
    header = archive.get(artefact_key(row.public_id, "trades")).decode().splitlines()[0]
    assert header.startswith("date,symbol,side,quantity,price,notional,cost,reason")

    assert publisher.frames, "no progress frames were published"
    assert '"status":"done"' in publisher.frames[-1][1]


async def test_a_job_that_is_not_queued_is_refused(session: AsyncSession, tmp_path: Path) -> None:
    """Celery's ``acks_late`` can redeliver a message. A fifteen-year run must not start twice."""
    await _seed_market(session)
    row = await _queued(session, _config())
    row.status = "running"
    await session.flush()
    with pytest.raises(BacktestNotRunnable):
        await run_backtest_job(
            session, row.public_id, archive=LocalRawArchive(tmp_path), fragility=False
        )


async def test_a_failing_run_lands_on_the_row(session: AsyncSession, tmp_path: Path) -> None:
    """A failure the user cannot read is a failure nobody will fix."""
    await _seed_market(session)
    row = await _queued(session, _config(start=dt.date(2019, 1, 2), end=dt.date(2019, 6, 28)))
    outcome = await run_backtest_job(
        session, row.public_id, archive=LocalRawArchive(tmp_path), fragility=False
    )
    assert outcome.status == "failed"
    refreshed = (await session.execute(select(Backtest).where(Backtest.id == row.id))).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.error
    assert refreshed.metrics is None


async def test_the_metrics_hash_is_stable_across_two_runs(session: AsyncSession) -> None:
    """docs/10 §"Correctness harness" 4, against the database rather than a fixture."""
    await _seed_market(session)
    config = _config()
    first = await execute_backtest(session, config, _definition(), fragility=False)
    second = await execute_backtest(session, config, _definition(), fragility=False)
    left = build_payload("a" * 24, config, first.result, None)
    right = build_payload("b" * 24, config, second.result, None)
    assert left.metrics_hash == right.metrics_hash


# ---------------------------------------------------------------------------
# docs/11 §"Performance budgets": "Backtest (15y, monthly, 20 names) | < 10 s"
# ---------------------------------------------------------------------------


async def _seed_long_market(
    session: AsyncSession, instruments: int
) -> tuple[tuple[dt.date, ...], tuple[dt.date, ...]]:
    """Fifteen years of bars for ``instruments`` names, and factor rows on the rebalance dates.

    Factor rows and index membership are written **only on the rebalance dates**, because those
    are the only dates the screener reads: it joins ``index_member_daily`` and ``factor_daily`` at
    the as-of and nowhere else (docs/06 §step 2). Writing fifteen years of factor rows for a
    timing test would measure the fixture rather than the query.
    """
    calendar = await trading_calendar(session, LONG_START, LONG_END)
    schedule = rebalance_dates(calendar, LONG_START, LONG_END, RebalanceSpec())
    index_id = UNIVERSE_BY_SLUG[UNIVERSE].index_id
    mask = UNIVERSE_BY_SLUG[UNIVERSE].mask_value
    rebalance_days = set(schedule)

    bars: list[dict[str, object]] = []
    factors: list[dict[str, object]] = []
    members: list[dict[str, object]] = []
    for position in range(instruments):
        instrument = Instrument(
            exchange_id=NSE_EXCHANGE_ID,
            symbol=f"PERF{position:04d}",
            name=f"PERF{position:04d} LIMITED",
            series="EQ",
            instrument_type="EQ",
            is_active=True,
        )
        session.add(instrument)
        await session.flush()
        step = Decimal(1) + Decimal((position % 37) + 1) / Decimal(100_000)
        price = Decimal(50 + position % 900)
        for day in calendar:
            price = (price * step).quantize(Decimal("0.0001"))
            bars.append(
                {
                    "instrument_id": instrument.id,
                    "date": day,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 100_000,
                    "close_raw": price,
                    "volume_raw": 100_000,
                    "adj_factor": Decimal(1),
                    "source": "nse",
                }
            )
            if day not in rebalance_days:
                continue
            factors.append(
                {
                    "instrument_id": instrument.id,
                    "date": day,
                    "close": price,
                    "close_raw": price,
                    "ret_12m": Decimal((position * 7) % 500),
                    "vol_12m": Decimal("0.2"),
                    "marketcap_cr": 1_000 + position,
                    "series": "EQ",
                    "universe_mask": mask,
                }
            )
            members.append(
                {
                    "index_id": index_id,
                    "date": day,
                    "instrument_id": instrument.id,
                    "source": "nse_file",
                }
            )

    for table, rows in ((OhlcvDaily, bars), (FactorDaily, factors), (IndexMemberDaily, members)):
        for start in range(0, len(rows), 20_000):
            await session.execute(insert(table), rows[start : start + 20_000])
    await session.flush()
    return calendar, schedule


@pytest.mark.benchmark
async def test_a_fifteen_year_monthly_run_over_twenty_names_is_under_ten_seconds(
    session: AsyncSession,
) -> None:
    """docs/11 §"Performance budgets" and PROMPTS.md Prompt 15's last acceptance criterion.

    **Not "the seeded dataset".** The prompt says "on the seeded dataset"; the seeded database is
    a single trading day of *results* (docs/13's export) with no price history at all, so no
    multi-year backtest can run against it. This is the closest honest measurement: a synthetic
    fifteen-year market **in PostgreSQL**, timed across the whole server-side path — 180 screen
    queries, the bar load, and the simulation. See ``docs/DECISIONS.md`` §15.20.
    """
    await _seed_long_market(session, instruments=PERF_INSTRUMENTS)
    config = BacktestConfig(
        start=LONG_START,
        end=LONG_END,
        initial_capital=Decimal(10_000_000),
        rebalance=RebalanceSpec(frequency=RebalanceFrequency.MONTHLY),
        selection=SelectionSpec(top_n=20, hold_buffer=10),
        benchmark=UNIVERSE,
    )

    began = time.perf_counter()
    outcome = await execute_backtest(session, config, _definition(), fragility=False)
    elapsed = time.perf_counter() - began

    assert len(outcome.result.dates) > 3_500
    assert len(outcome.result.rebalance_dates) >= 175
    assert outcome.result.trades
    # Printed as well as asserted: the headline number in docs/11's budget table is worth
    # knowing even when it passes, because "under ten seconds" and "0.4 seconds" are different
    # facts about whether this design has room in it.
    print(f"\n15y monthly / top 20 / {PERF_INSTRUMENTS} names, end to end: {elapsed:.2f}s")
    assert elapsed < 10.0, f"the 15-year run took {elapsed:.2f}s; docs/11 budgets 10s"


async def test_a_claimed_run_is_durably_running_with_a_start_time(
    session: AsyncSession,
) -> None:
    """M45.2. `running` must be visible outside the run's own transaction.

    Before this, the claim and the simulation shared one transaction, so `running` was never
    committed. Two consequences, both measured by an audit:

    * a successful run showed `queued` on `GET /backtests/{id}` for its entire duration, while the
      SSE frames said `running` — so `queued` already meant "currently running" in the happy path;
    * a process killed mid-simulation left a row identical in every column to one that had never
      been picked up (`status='queued' error=None finished_at=None metrics=None`), so no operator
      query and no reaper could separate "abandoned" from "waiting behind the concurrency cap".

    `created_at` cannot substitute: it records the POST, not the start.
    """
    await _seed_market(session)
    row = await _queued(session, _config())
    public_id = row.public_id
    await session.commit()

    engine = create_async_engine(helpers.database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as claiming:
        claimed = await _claim(claiming, public_id)
        assert claimed.status == "running"
        assert claimed.started_at is not None
        await claiming.commit()

    # A DIFFERENT transaction — which is the whole point.
    async with factory() as observer:
        status, started_at, created_at = (
            await observer.execute(
                select(Backtest.status, Backtest.started_at, Backtest.created_at).where(
                    Backtest.public_id == public_id
                )
            )
        ).one()

    assert status == "running", "an in-flight run must say so outside its own transaction"
    assert started_at is not None, "started-then-died would be identical to never-started"
    assert started_at >= created_at
    await engine.dispose()


async def test_a_run_cancelled_at_shutdown_lands_terminal(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M45.3. A clean shutdown must not strand a row at `running` forever.

    Two defects, one path, both measured before the fix:

    * `BacktestRunner.drain` called `task.cancel()` and returned. `cancel()` only *schedules* a
      `CancelledError` at the task's next suspension point — it does not deliver it and it does
      not run the task's cleanup. Measured: `in_flight` was still 1 when `drain` returned, and the
      job's handler had not executed even after another pass of the event loop.
    * `run_backtest_inline` guarded its out-of-band failure write with `except Exception`, and
      `asyncio.CancelledError` inherits from `BaseException`, not `Exception`. Measured: with the
      cancellation fully delivered and awaited, the handler that fires is the `BaseException` one.

    So the shutdown path — the case `drain` exists to handle *well* — was the one that lost runs.
    `drain` cancelling meant `running` forever, which is exactly the state in-process execution
    was supposed to be honest about.

    This drives the real `run_backtest_inline` through the real `BacktestRunner`; only the
    simulation is replaced, with something slow enough to be caught in flight. It fails if either
    half is reverted.
    """
    await _seed_market(session)
    row = await _queued(session, _config())
    public_id = row.public_id
    await session.commit()

    started = asyncio.Event()

    async def _never_finishes(*_args: object, **_kwargs: object) -> None:
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(job_module, "run_backtest_job", _never_finishes)
    monkeypatch.setattr(job_module, "build_publisher", lambda _s: None)

    engine = create_async_engine(helpers.database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(invoice_local_dir=str(tmp_path))
    runner = BacktestRunner(job_module.run_backtest_inline(factory, settings), concurrency=1)

    runner.submit(public_id, fragility=False)
    await asyncio.wait_for(started.wait(), timeout=5.0)

    async with factory() as observer:
        in_flight = (
            await observer.execute(select(Backtest.status).where(Backtest.public_id == public_id))
        ).scalar_one()
    assert in_flight == "running", "the claim must be committed before the work (M45.2)"

    await runner.drain(timeout=0.0)

    assert runner.in_flight == 0, "drain must await the cancellation, not merely request it"

    async with factory() as observer:
        status, error, finished_at = (
            await observer.execute(
                select(Backtest.status, Backtest.error, Backtest.finished_at).where(
                    Backtest.public_id == public_id
                )
            )
        ).one()
    await engine.dispose()

    assert status == "failed", f"a cancelled run must land terminal, not stay {status!r}"
    assert finished_at is not None
    assert error is not None
    assert "shut down" in error, f"the user should be told what happened, not {error!r}"


class TestTheLoaderDoesNotCostTwentyTimesWhatItBuilds:
    """M45.8. The price panel was materialised three times over before Polars saw any of it.

    `_bar_frame` called `.all()`, which holds every row as a SQLAlchemy `Row` carrying `Decimal`s,
    and then copied all of them into four Python lists, and only then built the frame — so the
    asyncpg buffer, the Rows, the lists and the answer were live together.

    Measured against the live database, 500 instruments over 2017-01-01..2026-08-19, 809,807 rows:
    **444 MB peak for a 21.6 MB frame, 20.5x**. Streamed in partitions: **52.8 MB, 2.4x**.

    That arithmetic was tolerable when backtests ran in a 4 GB worker. M42 moved them into the API
    process, where a peak like that evicts every other request as well as the run.
    """

    async def test_streaming_returns_exactly_what_materialising_did(
        self, session: AsyncSession
    ) -> None:
        """The point of the change is that it is invisible in the output.

        Built the old way here, in the same transaction, and compared frame to frame — ordering
        included, because the simulation reads the panel positionally.
        """
        await _seed_market(session)
        ids = (
            await session.execute(select(Instrument.id).order_by(Instrument.id))
        ).scalars().all()
        streamed = await _bar_frame(session, list(ids), START, END)

        statement = (
            select(OhlcvDaily.date, OhlcvDaily.instrument_id, OhlcvDaily.open, OhlcvDaily.close)
            .where(
                OhlcvDaily.instrument_id.in_(list(ids)),
                OhlcvDaily.date >= START,
                OhlcvDaily.date <= END,
            )
            .order_by(OhlcvDaily.instrument_id, OhlcvDaily.date)
        )
        rows = (await session.execute(statement)).all()
        materialised = pl.DataFrame(
            {
                "date": [row[0] for row in rows],
                "instrument_id": [int(row[1]) for row in rows],
                "open": [float(row[2]) for row in rows],
                "close": [float(row[3]) for row in rows],
            },
            schema={
                "date": pl.Date,
                "instrument_id": pl.Int64,
                "open": pl.Float64,
                "close": pl.Float64,
            },
        )

        assert streamed.height > 0, "an empty panel would make this prove nothing"
        assert streamed.schema == materialised.schema
        assert streamed.equals(materialised)

    async def test_a_run_too_large_to_hold_is_refused_rather_than_loaded(
        self, session: AsyncSession
    ) -> None:
        """Nothing bounded the size of a run.

        `MAX_REBALANCE_DATES` bounds how many screens a run issues and says nothing about the
        price history it then pulls. `top_n` reaches 500 and no maximum window exists, so the size
        of a run was whatever dates the user typed — in the process that also serves every other
        request.

        One indexed COUNT before anything is read, and the refusal names the two things the user
        can change. An eviction names nothing.
        """
        await _seed_market(session)
        ids = (
            await session.execute(select(Instrument.id).order_by(Instrument.id))
        ).scalars().all()

        # Refuses above the ceiling...
        with pytest.raises(BacktestDataError) as refused:
            await _require_a_loadable_size(session, list(ids), START, END, maximum=1)
        assert "Shorten the window" in str(refused.value)
        assert "daily bars" in str(refused.value)

        # ...and is silent at the real one, which no fixture is anywhere near.
        await _require_a_loadable_size(session, list(ids), START, END)
        await _require_a_loadable_size(session, [], START, END)
