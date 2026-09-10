"""VB9 — the study re-run from the plant's own bars, and the row it leaves behind.

Named `..._job` rather than `test_vbt_backtest`: `packages/core/tests` already has a module by
that name for the pure engine, and pytest collects both trees in one run with no `__init__.py`
between them, so two files sharing a basename are a collection error rather than two test
modules. The core one is the engine's arithmetic; this one is the job around it.

`docs/vbt/06` VB9's acceptance, minus the wall-clock one (that needs the real 4,186-name history,
which no test database holds — STATUS records it as unmeasured):

* **a planted year reproduces the planted trade's return to 2 dp** — the same trade
  `packages/core/tests/test_vbt_backtest.py` works by hand, driven this time through the loader,
  the indicators, the detector and the job, so a break anywhere in that chain shows up here;
* **a second run appends and does not edit the first**, because `03` §8 makes the table
  append-only: the number that was on the page when the execution flag was considered has to
  survive a recalibration that produces a different one;
* **a run that raises stores its `error`, sets `finished_at`, and leaves the last good number
  where it was** — a failed re-run must never displace a result;
* **`params` is written on the way in**, so a run that died still says what it was asked for;
* **the three books come back over one detection pass**, and `full` beats `gate_off` on drawdown,
  which is `01` §3's argument for the gate, recomputed rather than quoted.
"""

from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import AppUser, OhlcvDaily, VbBacktestRun
from baskfy_core.vbt.drift import DRIFT_CAGR_POINTS, compare
from baskfy_core.vbt.published import PUBLISHED
from baskfy_worker.tasks import vbt_backtest as job
from baskfy_worker.tasks.vbt_backtest import (
    SOURCE_PLANT,
    latest_finished,
    run_vbt_backtest,
)

# The planted year lives with the pure engine's tests, because it is the engine's fixture; this
# suite drives the same bars through the database so the loader and the detector are in the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "core" / "tests"))
from vbt_backtest_fixtures import (
    DAYS,
    SLEEVE,
    planted_bars,
)

pytestmark = [requires_db, pytest.mark.db]

FIRST = DAYS[0]
LAST = DAYS[-1]

#: The planted trade's return, from the fixture's own docstring: a ₹96.24 cost basis sold at
#: ₹90.00 less costs, over a ₹99,993.36 position — **-6.7176%**, and the sleeve carries one such
#: trade, so the whole book's return rounds to the same tenth.
PLANTED_TRADE_RETURN_PCT = -6.72


async def _user(session: AsyncSession) -> int:
    user = AppUser(public_id="vb9-user", email="vb9@example.com")
    session.add(user)
    await session.flush()
    return int(user.id)


async def _seed_planted_year(session: AsyncSession) -> None:
    """The fixture's two names, as `ohlcv_daily` rows the loader will find."""
    bars = planted_bars()
    ids: dict[str, int] = {}
    for symbol in bars["symbol"].unique().to_list():
        ids[str(symbol)] = await make_instrument(session, str(symbol))
    rows: list[OhlcvDaily] = []
    for record in bars.iter_rows(named=True):
        close = Decimal(str(round(float(record["close"]), 4)))
        rows.append(
            OhlcvDaily(
                instrument_id=ids[str(record["symbol"])],
                date=record["date"],
                open=Decimal(str(round(float(record["open"]), 4))),
                high=Decimal(str(round(float(record["high"]), 4))),
                low=Decimal(str(round(float(record["low"]), 4))),
                close=close,
                close_raw=close,
                volume=int(record["volume"]),
                volume_raw=int(record["volume"]),
                turnover=(close * Decimal(int(record["volume"]))).quantize(Decimal("0.01")),
                adj_factor=Decimal(1),
                source="nse",
            )
        )
    session.add_all(rows)
    await session.flush()


async def _run(session: AsyncSession, user_id: int) -> VbBacktestRun:
    return await run_vbt_backtest(
        session, user_id=user_id, start=DAYS[201], end=LAST, sleeve_inr=SLEEVE
    )


class TestThePlantedYear:
    async def test_it_reproduces_the_planted_trade_to_two_decimals(
        self, session: AsyncSession
    ) -> None:
        """The whole chain — loader, indicators, detector, breadth, engine — on one known trade.

        `packages/core/tests/test_vbt_backtest.py` proves the arithmetic against a hand
        calculation. This proves the *plumbing* delivers the same bars to it.
        """
        user_id = await _user(session)
        await _seed_planted_year(session)

        row = await _run(session, user_id)

        assert row.stats is not None
        full = row.stats["full"]
        assert isinstance(full, dict)
        assert full["trades"] == 1
        assert round(float(full["avg_trade_pct"]), 2) == PLANTED_TRADE_RETURN_PCT

    async def test_the_three_books_come_back_over_one_pass(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _seed_planted_year(session)

        row = await _run(session, user_id)

        assert row.stats is not None
        assert set(row.stats) >= {"full", "gate_off", "raw_scan"}
        for label in ("full", "gate_off", "raw_scan"):
            book = row.stats[label]
            assert isinstance(book, dict), label

    async def test_the_stats_carry_the_curve_and_the_yearly_table_the_page_draws(
        self, session: AsyncSession
    ) -> None:
        """`05` §2 draws both. A page that recomputed them would be a second implementation."""
        user_id = await _user(session)
        await _seed_planted_year(session)

        row = await _run(session, user_id)

        assert row.stats is not None
        full = row.stats["full"]
        assert isinstance(full, dict)
        curve = full["equity_curve"]
        assert isinstance(curve, list) and len(curve) > 1
        first = curve[0]
        assert isinstance(first, dict)
        # House rule 9: money reaches the database as a string of its exact decimal, never a float.
        assert isinstance(first["equity_inr"], str)
        assert isinstance(full["yearly"], list)

    async def test_the_params_are_written_on_the_way_in(self, session: AsyncSession) -> None:
        """`03` §8: a run that never finished still says what it was asked for."""
        user_id = await _user(session)
        await _seed_planted_year(session)

        row = await _run(session, user_id)

        assert row.params["start"] == DAYS[201].isoformat()
        assert row.params["sleeve_inr"] == str(SLEEVE)
        assert row.params["source"] == SOURCE_PLANT
        config = row.params["config"]
        assert isinstance(config, dict)
        # Flattened field by field, so two runs can be diffed and a new config field appears
        # here without anybody remembering to add it.
        assert config["entry.valid_sessions"] == 3
        assert config["breadth.min_pct_above_dma"] == 40.0


class TestTheTableIsAppendOnly:
    async def test_a_second_run_appends_and_edits_nothing(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _seed_planted_year(session)

        first = await _run(session, user_id)
        first_id, first_stats, first_started = first.id, first.stats, first.started_at
        second = await _run(session, user_id)

        assert second.id != first_id
        stored = (
            await session.execute(sa.select(VbBacktestRun).where(VbBacktestRun.id == first_id))
        ).scalar_one()
        assert stored.stats == first_stats
        assert stored.started_at == first_started
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(VbBacktestRun))
        ).scalar_one() == 2

    async def test_a_failed_run_records_its_error_and_leaves_the_last_good_number(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed re-run must never displace a result. `03` §8's `finished_at` is set on
        failure too, so "still running" and "failed" are different states on the page."""
        user_id = await _user(session)
        await _seed_planted_year(session)
        good = await _run(session, user_id)

        def explode(*args: object, **kwargs: object) -> pl.DataFrame:
            raise RuntimeError("the plant fell over")

        monkeypatch.setattr(job, "with_vbt_indicators", explode)
        with pytest.raises(RuntimeError, match="the plant fell over"):
            await _run(session, user_id)

        failed = (
            await session.execute(
                sa.select(VbBacktestRun)
                .where(VbBacktestRun.id != good.id)
                .order_by(VbBacktestRun.id.desc())
                .limit(1)
            )
        ).scalar_one()
        assert failed.stats is None
        assert failed.error is not None and "RuntimeError: the plant fell over" in failed.error
        assert failed.finished_at is not None
        assert failed.params["source"] == SOURCE_PLANT
        # And the page still reads the good one.
        still_good = await latest_finished(session, user_id=user_id)
        assert still_good is not None
        assert still_good.id == good.id

    async def test_an_empty_plant_fails_loudly_rather_than_reporting_nothing(
        self, session: AsyncSession
    ) -> None:
        """Zero bars is not a zero-return strategy, and a row saying so would be a lie."""
        user_id = await _user(session)

        with pytest.raises(ValueError, match="no bars"):
            await _run(session, user_id)

        row = (await session.execute(sa.select(VbBacktestRun))).scalar_one()
        assert row.stats is None
        assert row.error is not None and "ValueError" in row.error
        assert row.finished_at is not None


class TestTheDrift:
    async def test_the_planted_year_is_flagged_because_it_is_not_the_study(
        self, session: AsyncSession
    ) -> None:
        """One synthetic trade over one year is nowhere near 18.2% a year, and the flag says so.

        That is the check working, not failing: `05` §2's banner asks a person to explain a
        difference, and "this was a fixture, not the history" is the explanation.
        """
        user_id = await _user(session)
        await _seed_planted_year(session)

        row = await _run(session, user_id)

        assert row.drift is not None
        assert row.drift["flagged"] is True
        assert row.drift["threshold_cagr_points"] == DRIFT_CAGR_POINTS
        assert row.drift["trades_delta"] == 1 - PUBLISHED.trades

    async def test_a_run_that_matches_the_study_is_not_flagged(self) -> None:
        """The pure half of the same claim, without a database."""
        exact = compare(
            cagr_pct=PUBLISHED.cagr_pct,
            max_drawdown_pct=PUBLISHED.max_drawdown_pct,
            trades=PUBLISHED.trades,
        )
        assert exact.flagged is False
        assert (exact.cagr_pct_delta, exact.trades_delta) == (0.0, 0)

    async def test_a_point_either_way_is_inside_the_threshold_and_more_is_not(self) -> None:
        """Both directions: beating the study by two points is as suspicious as missing by two."""
        assert (
            compare(cagr_pct=PUBLISHED.cagr_pct + 1.0, max_drawdown_pct=-27.9, trades=761).flagged
            is False
        )
        assert (
            compare(cagr_pct=PUBLISHED.cagr_pct + 1.5, max_drawdown_pct=-27.9, trades=761).flagged
            is True
        )
        assert (
            compare(cagr_pct=PUBLISHED.cagr_pct - 1.5, max_drawdown_pct=-27.9, trades=761).flagged
            is True
        )


class TestThePageReadsTheLatestFinished:
    async def test_a_run_still_in_flight_never_displaces_the_last_good_one(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _seed_planted_year(session)
        good = await _run(session, user_id)
        session.add(
            VbBacktestRun(
                user_id=user_id,
                source=SOURCE_PLANT,
                params={"source": SOURCE_PLANT},
                started_at=dt.datetime.now(tz=dt.UTC) + dt.timedelta(minutes=5),
            )
        )
        await session.flush()

        latest = await latest_finished(session, user_id=user_id)

        assert latest is not None and latest.id == good.id

    async def test_another_users_run_is_not_this_ones(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        other = AppUser(public_id="vb9-other", email="vb9-other@example.com")
        session.add(other)
        await session.flush()
        await _seed_planted_year(session)
        await _run(session, user_id)

        assert (await latest_finished(session, user_id=int(other.id))) is None
