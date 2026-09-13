"""Ranking 2.C — the worker's half of docs/ranking/PLAN.md Phase 2, against a real database.

Four contracts, one class each, and each test name carries the gate's ``-k`` keyword:

* **G1** ``engine.py`` loads NIFTY 50 and NIFTY 500 from ``index_snapshot_daily`` and hands them
  to ``compute_factors`` as ``benchmark`` and ``market_benchmark`` (C1, DECISIONS-MERGE 2A.1).
* **G2** after the day's rows are upserted, ``mom_pctile`` (SQL ``PERCENT_RANK`` x 100 over the
  stored blend) and ``rank_persist_20`` (the last 20 dates' stored ``mom_pctile``) are filled,
  touching only those two columns, idempotently.
* **G3** ``desk_score_daily`` is what ``desk_score_service.score_day`` returns for the session,
  with ``carried`` taken from the as-of factor row exactly as 2B.2 tabulates it; idempotent; and a
  failure in it cannot poison the chain's transaction.
* **G4** ``backfill-ranking`` recomputes only the Phase-2 columns and desk scores, oldest day
  first, skips complete days unless forced, and leaves every pre-Phase-2 column byte-identical.

The market is closed-form (no randomness, no network): a handful of NSE names over 300 real
calendar sessions ending on ``TRADE_DATE``, plus NIFTY 50 and NIFTY 500 levels that move
differently, so a test that swapped the two indices would fail.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

import pandas as pd
import polars as pl
import pytest
from helpers import TRADE_DATE, make_instrument, requires_db
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.desk_score_service import COMPONENT_COLUMNS, DESK_SCORE_VERSION, score_day
from baskfy_core.models import (
    DeskScoreDaily,
    FactorDaily,
    FundamentalDaily,
    IndexMemberDaily,
    IndexSnapshotDaily,
    OhlcvDaily,
    TradingDay,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.universes import UNIVERSE_BY_SLUG
from baskfy_core.windows import resolve_window
from baskfy_worker import factors_cli, ranking_backfill
from baskfy_worker.engine import load_history
from baskfy_worker.orchestrator import run_compute_desk_score_step
from baskfy_worker.ranking_backfill import (
    COMPUTED,
    FAILED,
    SKIPPED_COMPLETE,
    day_is_complete,
    run_backfill_ranking,
)
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.tasks import desk_score as desk_score_task
from baskfy_worker.tasks.desk_score import load_carried, run_compute_desk_score
from baskfy_worker.tasks.factors import run_compute_factors
from baskfy_worker.tasks.ranking import (
    PER_INSTRUMENT_PHASE2_COLUMNS,
    PHASE2_COLUMNS,
    fill_rank_persist_20,
    run_rank_columns,
)

pytestmark = [pytest.mark.db, requires_db]

SESSIONS: Final = 300
NIFTY_50: Final = UNIVERSE_BY_SLUG["nifty-50"]
NIFTY_500: Final = UNIVERSE_BY_SLUG["nifty-500"]
NIFTY_FNO: Final = UNIVERSE_BY_SLUG["nifty-fno"]


@dataclass(frozen=True)
class Name:
    symbol: str
    drift: float
    amp: float
    period: float
    series: str = "EQ"
    fno: bool = False
    marketcap_cr: int | None = 20_000


NAMES: Final[tuple[Name, ...]] = (
    Name("RKALPHA", 0.0030, 0.020, 9.0, fno=True),
    Name("RKBRAVO", 0.0022, 0.035, 11.0),
    Name("RKCHARL", 0.0016, 0.015, 7.0, fno=True, marketcap_cr=None),
    Name("RKDELTA", 0.0010, 0.045, 13.0),
    Name("RKECHO", 0.0004, 0.025, 8.0, fno=True),
    Name("RKFOXT", -0.0008, 0.030, 10.0),
    Name("RKGOLF", 0.0026, 0.012, 6.0, series="BE"),
)


def _close(index: int, name: Name, bar: int) -> Decimal:
    level = (
        100.0 * math.exp(name.drift * bar) * (1.0 + name.amp * math.sin(bar / name.period + index))
    )
    return Decimal(str(round(level, 2)))


def _level(base: float, drift: float, amp: float, period: float, bar: int) -> Decimal:
    return Decimal(
        str(round(base * math.exp(drift * bar) * (1.0 + amp * math.sin(bar / period)), 2))
    )


async def _sessions(session: AsyncSession, count: int = SESSIONS) -> list[dt.date]:
    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date <= TRADE_DATE,
        )
        .order_by(TradingDay.date.desc())
        .limit(count)
    )
    return sorted(row[0] for row in rows)


@dataclass
class Market:
    days: list[dt.date]
    ids: dict[str, int]


async def seed_market(session: AsyncSession, *, with_indices: bool = True) -> Market:
    """Bars for every name over 300 sessions and, optionally, both benchmark index series."""
    days = await _sessions(session)
    ids: dict[str, int] = {}
    for index, name in enumerate(NAMES):
        ids[name.symbol] = await make_instrument(
            session, name.symbol, token=index + 1, series=name.series
        )
        bars = []
        for bar, day in enumerate(days):
            close = _close(index, name, bar)
            volume = int(1_500_000 * (1.0 + 0.3 * math.sin(bar / 5.0 + index)))
            bars.append(
                {
                    "instrument_id": ids[name.symbol],
                    "date": day,
                    "open": (close * Decimal("0.996")).quantize(Decimal("0.01")),
                    "high": (close * Decimal("1.011")).quantize(Decimal("0.01")),
                    "low": (close * Decimal("0.989")).quantize(Decimal("0.01")),
                    "close": close,
                    "volume": volume,
                    "close_raw": close,
                    "volume_raw": volume,
                    "adj_factor": Decimal(1),
                    "source": "nse",
                }
            )
        await session.execute(insert(OhlcvDaily), bars)
    if with_indices:
        levels = []
        for bar, day in enumerate(days):
            levels.append(
                {
                    "index_id": NIFTY_50.index_id,
                    "date": day,
                    "level": _level(20_000.0, 0.0004, 0.010, 13.0, bar),
                }
            )
            levels.append(
                {
                    "index_id": NIFTY_500.index_id,
                    "date": day,
                    "level": _level(18_000.0, 0.0009, 0.020, 7.0, bar),
                }
            )
        await session.execute(insert(IndexSnapshotDaily), levels)
    await session.flush()
    return Market(days, ids)


async def seed_day_inputs(session: AsyncSession, market: Market, day: dt.date) -> None:
    """Point-in-time membership and fundamentals for one computed day."""
    members = []
    fundamentals = []
    for name in NAMES:
        instrument_id = market.ids[name.symbol]
        members.append(
            {"index_id": NIFTY_500.index_id, "date": day, "instrument_id": instrument_id}
        )
        if name.fno:
            members.append(
                {"index_id": NIFTY_FNO.index_id, "date": day, "instrument_id": instrument_id}
            )
        if name.marketcap_cr is not None:
            fundamentals.append(
                {"instrument_id": instrument_id, "date": day, "marketcap_cr": name.marketcap_cr}
            )
    await session.execute(insert(IndexMemberDaily), members)
    await session.execute(insert(FundamentalDaily), fundamentals)
    await session.flush()


async def nightly(session: AsyncSession, market: Market, day: dt.date) -> None:
    await seed_day_inputs(session, market, day)
    await run_compute_factors(session, StepOutcome(), day)


async def factor_row(session: AsyncSession, instrument_id: int, day: dt.date) -> FactorDaily:
    row = (
        await session.execute(
            select(FactorDaily)
            .where(FactorDaily.instrument_id == instrument_id, FactorDaily.date == day)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


async def row_hashes(
    session: AsyncSession, table: str, *, excluding: Sequence[str] = ()
) -> dict[tuple[int, dt.date], str]:
    """``md5`` of each row's JSON, minus ``excluding`` — a byte-level fingerprint per row."""
    minus = "".join(f" - '{column}'" for column in excluding)
    rows = await session.execute(
        text(f"SELECT instrument_id, date, md5((to_jsonb(t){minus})::text) FROM {table} t")
    )
    return {(int(r[0]), r[1]): str(r[2]) for r in rows}


# ---------------------------------------------------------------------------------------------
# G1 — both benchmarks, from index_snapshot_daily
# ---------------------------------------------------------------------------------------------


class TestBenchmarks:
    async def test_market_benchmark_is_nifty_500_and_benchmark_is_nifty_50(
        self, session: AsyncSession
    ) -> None:
        """The loader hands each index to the argument C1 names, levels exactly as stored."""
        market = await seed_market(session)
        history = await load_history(session, TRADE_DATE)

        nifty_500 = dict(history.market_benchmark.select("date", "level").iter_rows())
        nifty_50 = dict(history.benchmark.select("date", "close").iter_rows())
        assert len(nifty_500) == len(nifty_50) == SESSIONS
        last = market.days[-1]
        assert nifty_500[last] == float(_level(18_000.0, 0.0009, 0.020, 7.0, SESSIONS - 1))
        assert nifty_50[last] == float(_level(20_000.0, 0.0004, 0.010, 13.0, SESSIONS - 1))

    async def test_ranking_factors_are_written_against_the_right_index_with_a_full_year(
        self, session: AsyncSession
    ) -> None:
        """excess_ret_12m subtracts NIFTY 500's 12M return; resid_ret_12m beta x NIFTY 50's."""
        market = await seed_market(session)
        await nightly(session, market, TRADE_DATE)
        row = await factor_row(session, market.ids["RKALPHA"], TRADE_DATE)

        assert row.ret_12m is not None
        assert row.beta_12m is not None
        assert row.excess_ret_12m is not None
        assert row.resid_ret_12m is not None
        assert row.rs_persist_126 is not None

        window = resolve_window(TRADE_DATE, 12, await _sessions(session, 800))
        start_bar = market.days.index(window.start)
        end_bar = SESSIONS - 1

        def index_return(base: float, drift: float, amp: float, period: float) -> float:
            start = float(_level(base, drift, amp, period, start_bar))
            end = float(_level(base, drift, amp, period, end_bar))
            return (end / start - 1.0) * 100.0

        nifty_500_return = index_return(18_000.0, 0.0009, 0.020, 7.0)
        nifty_50_return = index_return(20_000.0, 0.0004, 0.010, 13.0)
        assert float(row.excess_ret_12m) == pytest.approx(
            float(row.ret_12m) - nifty_500_return, abs=0.011
        )
        assert float(row.resid_ret_12m) == pytest.approx(
            float(row.ret_12m) - float(row.beta_12m) * nifty_50_return, abs=0.011
        )

    async def test_ranking_factors_are_null_without_the_index_series(
        self, session: AsyncSession
    ) -> None:
        market = await seed_market(session, with_indices=False)
        await nightly(session, market, TRADE_DATE)
        row = await factor_row(session, market.ids["RKALPHA"], TRADE_DATE)

        assert row.ret_12m is not None  # the instrument does have a full year
        assert row.excess_ret_12m is None
        assert row.resid_ret_12m is None
        assert row.rs_persist_126 is None


# ---------------------------------------------------------------------------------------------
# G2 — mom_pctile and rank_persist_20
# ---------------------------------------------------------------------------------------------


def _half_up(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _synthetic_rows(
    session: AsyncSession,
    ids: list[int],
    days: list[dt.date],
    sharpe: dict[tuple[int, dt.date], float | None],
    *,
    outside: int | None = None,
) -> None:
    """Factor rows carrying only what the two columns read: the Sharpe blend and the mask."""
    rows = []
    for day in days:
        for instrument_id in ids:
            value = sharpe[(instrument_id, day)]
            number = None if value is None else Decimal(str(value))
            rows.append(
                {
                    "instrument_id": instrument_id,
                    "date": day,
                    "sharpe_12m": number,
                    "sharpe_6m": number,
                    "sharpe_3m": number,
                    "sharpe_1m": number,
                    "universe_mask": 0 if instrument_id == outside else NIFTY_500.mask_value,
                }
            )
    await session.execute(insert(FactorDaily), rows)
    await session.flush()


class TestRankColumns:
    async def test_mom_pctile_is_sql_percent_rank_of_the_stored_blend(
        self, session: AsyncSession
    ) -> None:
        """(rank - 1) / (n - 1) x 100 over universe rows; NULL outside one or with no blend."""
        market = await seed_market(session)
        await nightly(session, market, TRADE_DATE)

        expected = {
            int(r[0]): r[1]
            for r in await session.execute(
                text(
                    "SELECT instrument_id, round((100 * percent_rank() OVER (ORDER BY "
                    "(sharpe_12m + sharpe_6m + sharpe_3m + sharpe_1m) / 4))::numeric, 2) "
                    "FROM factor_daily WHERE date = :d AND universe_mask <> 0 AND sharpe_12m IS "
                    "NOT NULL AND sharpe_6m IS NOT NULL AND sharpe_3m IS NOT NULL AND sharpe_1m "
                    "IS NOT NULL"
                ),
                {"d": TRADE_DATE},
            )
        }
        stored = {
            int(r[0]): r[1]
            for r in await session.execute(
                select(FactorDaily.instrument_id, FactorDaily.mom_pctile).where(
                    FactorDaily.date == TRADE_DATE
                )
            )
        }
        assert len(expected) == len(NAMES)
        assert stored == expected
        assert max(v for v in stored.values() if v is not None) == Decimal("100.00")
        assert min(v for v in stored.values() if v is not None) == Decimal("0.00")

    async def test_mom_pctile_excludes_rows_outside_every_universe(
        self, session: AsyncSession
    ) -> None:
        ids = [await make_instrument(session, f"PCT{i}") for i in range(4)]
        sharpe: dict[tuple[int, dt.date], float | None] = {
            (i, TRADE_DATE): float(n) for n, i in enumerate(ids)
        }
        sharpe[(ids[1], TRADE_DATE)] = None
        await _synthetic_rows(session, ids, [TRADE_DATE], sharpe, outside=ids[3])
        await run_rank_columns(session, TRADE_DATE)

        stored = {
            int(r[0]): r[1]
            for r in await session.execute(
                select(FactorDaily.instrument_id, FactorDaily.mom_pctile).where(
                    FactorDaily.date == TRADE_DATE
                )
            )
        }
        assert stored == {
            ids[0]: Decimal("0.00"),
            ids[1]: None,
            ids[2]: Decimal("100.00"),
            ids[3]: None,
        }

    async def test_rank_persist_20_counts_the_stored_mom_pctile_of_the_last_20_dates(
        self, session: AsyncSession
    ) -> None:
        """Walked oldest first, as the nightly and the backfill do; prior dates read from the DB."""
        days = await _sessions(session, 21)
        ids = [await make_instrument(session, f"PER{i}") for i in range(6)]
        # PER5 is top on even days only, PER4 top on odd days; everything else is ordered fixed.
        sharpe: dict[tuple[int, dt.date], float | None] = {}
        for n, day in enumerate(days):
            for rank, instrument_id in enumerate(ids):
                sharpe[(instrument_id, day)] = float(rank)
            sharpe[(ids[5], day)] = 10.0 if n % 2 == 0 else 1.5
            sharpe[(ids[4], day)] = 1.5 if n % 2 == 0 else 10.0
        await _synthetic_rows(session, ids, days, sharpe)

        for day in days:
            await run_rank_columns(session, day)

        persist = {
            (int(r[0]), r[1]): r[2]
            for r in await session.execute(
                select(FactorDaily.instrument_id, FactorDaily.date, FactorDaily.rank_persist_20)
            )
        }
        # Nineteen dates is not a window.
        assert all(persist[(i, days[18])] is None for i in ids)
        # Six names, so (rank - 1) / 5 x 100: top 100, second 80. PER3 (value 3) is second or
        # better every day; PER5 is top on the ten even days of days[0..19] and fourth (40) on
        # the odd ones, PER4 the mirror image; PER0 is last every day.
        last = days[19]
        assert persist[(ids[3], last)] == Decimal("100.00")
        assert persist[(ids[5], last)] == Decimal("50.00")
        assert persist[(ids[4], last)] == Decimal("50.00")
        assert persist[(ids[0], last)] == Decimal("0.00")
        # The window slides: days[20] drops days[0] and adds an even day.
        assert persist[(ids[5], days[20])] == Decimal("50.00")

        # It reads the stored history: make PER0 top on days[1]..days[20] in the DB only.
        await session.execute(
            update(FactorDaily)
            .where(FactorDaily.instrument_id == ids[0], FactorDaily.date.in_(days[1:]))
            .values(mom_pctile=Decimal("90.00"))
        )
        await fill_rank_persist_20(session, days[20])
        refreshed = await factor_row(session, ids[0], days[20])
        assert refreshed.rank_persist_20 == Decimal("100.00")

    async def test_rank_persist_20_is_null_when_a_date_in_the_window_lacks_mom_pctile(
        self, session: AsyncSession
    ) -> None:
        days = await _sessions(session, 20)
        ids = [await make_instrument(session, f"GAP{i}") for i in range(3)]
        sharpe: dict[tuple[int, dt.date], float | None] = {
            (i, d): float(n) for d in days for n, i in enumerate(ids)
        }
        sharpe[(ids[2], days[5])] = None
        await _synthetic_rows(session, ids, days, sharpe)
        for day in days:
            await run_rank_columns(session, day)
        assert (await factor_row(session, ids[2], days[-1])).rank_persist_20 is None
        # On days[5] only two names rank, so the middle one is top (100) that day and 50 otherwise.
        assert (await factor_row(session, ids[1], days[-1])).rank_persist_20 == Decimal("5.00")

    async def test_mom_pctile_and_rank_persist_touch_no_other_column_and_are_idempotent(
        self, session: AsyncSession
    ) -> None:
        market = await seed_market(session)
        for day in market.days[-2:]:
            await nightly(session, market, day)

        others_before = await row_hashes(
            session, "factor_daily", excluding=("mom_pctile", "rank_persist_20")
        )
        full_before = await row_hashes(session, "factor_daily")
        for day in market.days[-2:]:
            await run_rank_columns(session, day)
        assert (
            await row_hashes(session, "factor_daily", excluding=("mom_pctile", "rank_persist_20"))
            == others_before
        )
        assert await row_hashes(session, "factor_daily") == full_before

        # And the whole nightly step, twice, is the same rows too.
        await run_compute_factors(session, StepOutcome(), TRADE_DATE)
        assert await row_hashes(session, "factor_daily") == full_before


# ---------------------------------------------------------------------------------------------
# G3 — desk_score_daily through the book's service
# ---------------------------------------------------------------------------------------------


def _independent_bars(market: Market) -> pl.DataFrame:
    """The desk's `_BARS_SQL` shape, built from the generator rather than read back."""
    rows: list[dict[str, object]] = []
    for index, name in enumerate(NAMES):
        for bar, day in enumerate(market.days):
            close = _close(index, name, bar)
            volume = float(int(1_500_000 * (1.0 + 0.3 * math.sin(bar / 5.0 + index))))
            rows.append(
                {
                    "symbol": name.symbol,
                    "instrument_id": market.ids[name.symbol],
                    "date": day,
                    "open": float((close * Decimal("0.996")).quantize(Decimal("0.01"))),
                    "high": float((close * Decimal("1.011")).quantize(Decimal("0.01"))),
                    "low": float((close * Decimal("0.989")).quantize(Decimal("0.01"))),
                    "close": float(close),
                    "volume": volume,
                    "close_raw": float(close),
                    "volume_raw": volume,
                }
            )
    return pl.DataFrame(rows).sort("symbol", "date")


def _as_stored(frame: pd.DataFrame) -> dict[int, tuple[object, ...]]:
    def cell(value: object) -> object:
        if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
            return None
        return value

    out: dict[int, tuple[object, ...]] = {}
    for record in frame.to_dict(orient="records"):
        out[int(str(record["instrument_id"]))] = (
            None if cell(record["score"]) is None else Decimal(str(record["score"])),
            None if cell(record["score_rank"]) is None else int(str(record["score_rank"])),
            *(
                None if cell(record[c]) is None else Decimal(str(record[c]))
                for c in COMPONENT_COLUMNS.values()
            ),
            str(record["reject"]),
            str(record["score_version"]),
        )
    return out


async def _stored_desk(session: AsyncSession, day: dt.date) -> dict[int, tuple[object, ...]]:
    rows = await session.execute(
        select(
            DeskScoreDaily.instrument_id,
            DeskScoreDaily.score,
            DeskScoreDaily.score_rank,
            *(getattr(DeskScoreDaily, c) for c in COMPONENT_COLUMNS.values()),
            DeskScoreDaily.reject,
            DeskScoreDaily.score_version,
        ).where(DeskScoreDaily.date == day)
    )
    return {
        int(r[0]): (
            None if r[1] is None else Decimal(r[1]).normalize(),
            r[2],
            *(None if v is None else Decimal(v).normalize() for v in r[3:10]),
            r[10],
            r[11],
        )
        for r in rows
    }


def _normalised(rows: dict[int, tuple[object, ...]]) -> dict[int, tuple[object, ...]]:
    return {
        key: tuple(v.normalize() if isinstance(v, Decimal) else v for v in values)
        for key, values in rows.items()
    }


class TestDeskScore:
    async def test_desk_score_carried_columns_come_from_the_as_of_factor_row(
        self, session: AsyncSession
    ) -> None:
        """2B.2's table, column by column, with NULL passed through and the F&O bit as 0/1."""
        market = await seed_market(session)
        await nightly(session, market, TRADE_DATE)
        carried = {
            row["symbol"]: row for row in (await load_carried(session, TRADE_DATE)).to_dicts()
        }

        assert set(carried) == {name.symbol for name in NAMES}
        for name in NAMES:
            row = await factor_row(session, market.ids[name.symbol], TRADE_DATE)
            got = carried[name.symbol]
            assert got["series"] == row.series == name.series
            assert got["marketcap"] == (
                None if row.marketcap_cr is None else float(row.marketcap_cr)
            )
            assert got["beta"] == (None if row.beta_12m is None else float(row.beta_12m))
            assert got["circuits_three_months"] == row.circuits_3m
            assert got["circuits_one_year"] == row.circuits_12m
            assert got["is_nifty_fno"] == (1 if name.fno else 0)
        assert carried["RKCHARL"]["marketcap"] is None
        assert carried["RKALPHA"]["beta"] is not None

    async def test_desk_score_rows_are_exactly_what_the_books_service_returns(
        self, session: AsyncSession
    ) -> None:
        market = await seed_market(session)
        await nightly(session, market, TRADE_DATE)
        outcome = StepOutcome()
        written = await run_compute_desk_score(session, outcome, TRADE_DATE)

        carried = await load_carried(session, TRADE_DATE)
        expected = score_day(_independent_bars(market), TRADE_DATE, market.days, carried)
        stored = await _stored_desk(session, TRADE_DATE)

        assert written == len(NAMES) == len(stored)
        assert stored == _normalised(_as_stored(expected))
        assert {v[-1] for v in stored.values()} == {DESK_SCORE_VERSION}
        # The fixture is not all rejects: the book ranks some of it, and rejects the BE series.
        assert any(v[1] is not None for v in stored.values())
        assert "T2T_series" in str(stored[market.ids["RKGOLF"]][-2])
        assert outcome.detail["scored"] == sum(1 for v in stored.values() if v[0] is not None)

    async def test_desk_score_upsert_is_idempotent_and_drops_rows_the_scan_no_longer_makes(
        self, session: AsyncSession
    ) -> None:
        market = await seed_market(session)
        await nightly(session, market, TRADE_DATE)
        await run_compute_desk_score(session, StepOutcome(), TRADE_DATE)
        first = await row_hashes(session, "desk_score_daily")

        stray = await make_instrument(session, "RKSTRAY")
        session.add(
            DeskScoreDaily(instrument_id=stray, date=TRADE_DATE, reject="x", score_version="old")
        )
        await session.flush()
        await run_compute_desk_score(session, StepOutcome(), TRADE_DATE)
        assert await row_hashes(session, "desk_score_daily") == first

    async def test_a_desk_score_failure_is_reported_and_leaves_the_transaction_usable(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A database error inside the step would abort the chain's one transaction without the
        savepoint; with it, the factor rows survive and the next statement still runs."""
        market = await seed_market(session)
        await nightly(session, market, TRADE_DATE)
        factors_before = await row_hashes(session, "factor_daily")

        async def _poison(sess: AsyncSession, outcome: StepOutcome, on: dt.date) -> int:
            del outcome, on
            await sess.execute(
                insert(DeskScoreDaily).values(
                    instrument_id=market.ids["RKALPHA"],
                    date=TRADE_DATE,
                    reject="",
                    score_version=DESK_SCORE_VERSION,
                )
            )
            await sess.execute(text("SELECT 1 / 0"))
            return 0

        monkeypatch.setattr(desk_score_task, "run_compute_desk_score", _poison)
        desk = await run_compute_desk_score_step(session, TRADE_DATE)

        assert desk.status is StepStatus.FAILED
        assert "division by zero" in str(desk.detail["error"])
        assert await row_hashes(session, "factor_daily") == factors_before
        remaining = (
            await session.execute(select(func.count()).select_from(DeskScoreDaily))
        ).scalar_one()
        assert remaining == 0  # the partial write rolled back to the savepoint


# ---------------------------------------------------------------------------------------------
# G4 — backfill-ranking
# ---------------------------------------------------------------------------------------------


async def _phase2_values(session: AsyncSession) -> dict[tuple[int, dt.date], tuple[object, ...]]:
    columns = ", ".join(PHASE2_COLUMNS)
    rows = await session.execute(text(f"SELECT instrument_id, date, {columns} FROM factor_daily"))
    return {(int(r[0]), r[1]): tuple(r[2:]) for r in rows}


async def _desk_all(session: AsyncSession) -> dict[tuple[int, dt.date], str]:
    return await row_hashes(session, "desk_score_daily")


async def _strip_phase2(session: AsyncSession) -> None:
    """The state a pre-Phase-2 night left: legacy columns written, Phase 2 absent."""
    await session.execute(update(FactorDaily).values({column: None for column in PHASE2_COLUMNS}))
    await session.execute(delete(DeskScoreDaily))
    await session.flush()


class TestBackfillRanking:
    async def test_backfill_ranking_rebuilds_phase2_and_leaves_legacy_columns_byte_identical(
        self, session: AsyncSession
    ) -> None:
        market = await seed_market(session)
        days = market.days[-3:]
        for day in days:
            await nightly(session, market, day)
            await run_compute_desk_score(session, StepOutcome(), day)
        phase2_expected = await _phase2_values(session)
        desk_expected = await _desk_all(session)
        assert any(
            v[PHASE2_COLUMNS.index("excess_ret_12m")] is not None for v in phase2_expected.values()
        )
        assert any(
            v[PHASE2_COLUMNS.index("mom_pctile")] is not None for v in phase2_expected.values()
        )

        await _strip_phase2(session)
        # A legacy value the engine would NOT produce: if the backfill rewrote legacy columns,
        # this is the first thing it would overwrite.
        await session.execute(
            update(FactorDaily)
            .where(FactorDaily.instrument_id == market.ids["RKDELTA"], FactorDaily.date == days[0])
            .values(ret_1m=Decimal("12345.67"), close=Decimal("1.00"))
        )
        legacy_before = await row_hashes(session, "factor_daily", excluding=PHASE2_COLUMNS)
        assert not await day_is_complete(session, days[0])

        report = await run_backfill_ranking(session, days[0], days[-1])

        assert [d.status for d in report.days] == [COMPUTED, COMPUTED, COMPUTED]
        assert report.errors == []
        assert await row_hashes(session, "factor_daily", excluding=PHASE2_COLUMNS) == legacy_before
        tampered = await factor_row(session, market.ids["RKDELTA"], days[0])
        assert tampered.ret_1m == Decimal("12345.67")
        assert await _phase2_values(session) == phase2_expected
        assert await _desk_all(session) == desk_expected
        assert all([await day_is_complete(session, day) for day in days])

    async def test_backfill_ranking_skips_complete_days_unless_forced(
        self, session: AsyncSession
    ) -> None:
        market = await seed_market(session)
        days = market.days[-3:]
        for day in days:
            await nightly(session, market, day)
        await _strip_phase2(session)

        # A run killed after two days...
        first = await run_backfill_ranking(session, days[0], days[1])
        assert [d.status for d in first.days] == [COMPUTED, COMPUTED]
        after_first = await row_hashes(session, "factor_daily")

        # ...resumes at the third, and does not redo the two it finished.
        resumed = await run_backfill_ranking(session, days[0], days[-1])
        assert [d.status for d in resumed.days] == [SKIPPED_COMPLETE, SKIPPED_COMPLETE, COMPUTED]
        settled = await row_hashes(session, "factor_daily")
        settled_desk = await _desk_all(session)
        finished = {k: v for k, v in after_first.items() if k[1] != days[-1]}
        assert {k: v for k, v in settled.items() if k[1] != days[-1]} == finished
        assert {k: v for k, v in settled.items() if k[1] == days[-1]} != {
            k: v for k, v in after_first.items() if k[1] == days[-1]
        }

        forced = await run_backfill_ranking(session, days[0], days[-1], force=True)
        assert [d.status for d in forced.days] == [COMPUTED, COMPUTED, COMPUTED]
        assert await row_hashes(session, "factor_daily") == settled
        assert await _desk_all(session) == settled_desk

    async def test_backfill_ranking_walks_oldest_first_so_persistence_sees_prior_dates(
        self, session: AsyncSession
    ) -> None:
        """rank_persist_20 needs 20 dates of mom_pctile; only an in-order backfill of 20 empty
        dates can produce it on the 20th, and it must equal what twenty nightly runs wrote."""
        market = await seed_market(session)
        days = market.days[-20:]
        for day in days:
            await nightly(session, market, day)
        nightly_values = await _phase2_values(session)
        assert any(
            v[PHASE2_COLUMNS.index("rank_persist_20")] is not None
            for k, v in nightly_values.items()
            if k[1] == days[-1]
        )

        await _strip_phase2(session)
        report = await run_backfill_ranking(session, days[0], days[-1])
        assert [d.day for d in report.days] == days
        assert await _phase2_values(session) == nightly_values

    async def test_backfill_ranking_keeps_factor_columns_when_desk_score_fails_and_reports_it(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        market = await seed_market(session)
        day = market.days[-1]
        await nightly(session, market, day)
        phase2_expected = await _phase2_values(session)
        await _strip_phase2(session)

        async def _refuse(sess: AsyncSession, outcome: StepOutcome, on: dt.date) -> int:
            del outcome, on
            await sess.execute(text("SELECT 1 / 0"))
            return 0

        monkeypatch.setattr(ranking_backfill, "run_compute_desk_score", _refuse)
        report = await run_backfill_ranking(session, day, day)

        assert [d.status for d in report.days] == [FAILED]
        assert "desk_score" in report.errors[0]
        assert await _phase2_values(session) == phase2_expected
        assert not await day_is_complete(session, day)

    async def test_backfill_ranking_updates_no_column_outside_phase_2(self) -> None:
        """The writer's column list is C1 minus the two cross-sectional columns, nothing else."""
        assert set(PER_INSTRUMENT_PHASE2_COLUMNS) | {"mom_pctile", "rank_persist_20"} == set(
            PHASE2_COLUMNS
        )
        legacy = {"close", "ret_12m", "sharpe_12m", "beta_12m", "universe_mask", "marketcap_cr"}
        assert not legacy & set(PHASE2_COLUMNS)

    async def test_backfill_ranking_cli_refuses_resume_with_force(self) -> None:
        with pytest.raises(SystemExit):
            factors_cli.main(
                [
                    "backfill-ranking",
                    "--from",
                    "2026-08-01",
                    "--to",
                    "2026-08-18",
                    "--resume",
                    "--force",
                ]
            )
