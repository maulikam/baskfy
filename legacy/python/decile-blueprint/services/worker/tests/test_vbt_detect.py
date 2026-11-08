"""VB4's acceptance: the nightly detection job, against a real database.

Six claims, and the module plan states five of them almost word for word:

1. **Running the task twice for a date changes no rows.** House rule 7. A night that was killed
   and restarted must leave the same table as one that ran once.
2. **A date with no published bars writes nothing and says so.** Not an exception, and not a
   silent zero either: `SKIPPED` with a reason, because "no signals" and "no data" look identical
   on a page and are opposite problems.
3. **A split yields a stored limit equal to the exchange price.** Every rule reads the adjusted
   series so a 1:2 split does not fake a trend; a person sends the *exchange* price to a broker.
   Both have to be true at once, and this is where they meet.
4. **The step's failure leaves the run SUCCEEDED.** `docs/vbt/06`: the step is "unable to fail the
   run", and DECISIONS-VB VB0.5 says why that trade is the right way round.
5. **The gate is read at the signal's own close.** Shifting the breadth series by one session
   moves the gate by exactly one, which is the look-ahead test `06` VB4 asks for.
6. And the one about neighbours: `COMPUTE_SWING` still precedes `COMPUTE_VBT` in the chain, and
   nothing here writes an `sw_` row.

The synthetic series is drawn to `04` §3's shape — a long rise into a shelf, then one breakout bar
on four times its usual volume — and written here rather than imported, because pytest gives test
files no package and the core's fixtures live in another tree.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    AppUser,
    IndexDef,
    IndexMemberDaily,
    OhlcvDaily,
    PipelineRun,
    PipelineRunStep,
    SwSetupDaily,
    TradingDay,
    VbBreadthDaily,
    VbConfig,
    VbSignalDaily,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, Gate, SignalState
from baskfy_worker.deps import PipelineDependencies
from baskfy_worker.orchestrator import run_compute_vbt_step
from baskfy_worker.steps import POST_PUBLISH_STEPS, PipelineStep, StepOutcome, StepStatus
from baskfy_worker.tasks import vbt as vbt_task
from baskfy_worker.tasks.vbt import (
    LOOKBACK_SESSIONS,
    load_universe,
    load_vbt_config,
    lookback_start,
    published_signal_count,
    run_detect_vbt,
)

pytestmark = requires_db

#: A Tuesday inside the seeded calendar, with well over 260 sessions behind it.
AS_OF = dt.date(2026, 8, 18)

#: Sessions of history each fixture name gets: enough for a 200-day average under `04` §2.2's
#: tolerance, with room to spare.
HISTORY = 250


async def _sole_user(session: AsyncSession) -> int:
    user = AppUser(public_id="vb4-user", email="vb4@example.com")
    session.add(user)
    await session.flush()
    session.add(VbConfig(user_id=user.id, updated_by="test"))
    await session.flush()
    return int(user.id)


async def _sessions_before(session: AsyncSession, as_of: dt.date, count: int) -> list[dt.date]:
    rows = await session.execute(
        sa.select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date <= as_of,
        )
        .order_by(TradingDay.date.desc())
        .limit(count)
    )
    return sorted(row[0] for row in rows)


def _breakout_shape(bars: int, *, shelf: float = 90.0, breakout: float = 96.0) -> list[float]:
    """A long rise into a twenty-session shelf, then one breakout bar. `04` §3's picture.

    The proportions satisfy all eleven rules at once: above a rising 200-day average (A), clear of
    the prior twenty sessions' high (B), up 6.7% over twenty sessions rather than 25% (C), closing
    on its high (D), a 6.7% day rather than 15% (E), and liquid (F).
    """
    shelf_bars = 20
    rise = bars - shelf_bars - 1
    closes = [20.0 + (shelf - 20.0) * (index + 1) / rise for index in range(rise)]
    closes += [shelf] * shelf_bars
    closes.append(breakout)
    return closes


async def _write_breakout(  # noqa: PLR0913 - a fixture is its knobs
    session: AsyncSession,
    symbol: str,
    dates: list[dt.date],
    *,
    adj_factor: Decimal = Decimal(1),
    breakout: float = 96.0,
    base_volume: int = 250_000,
    signal_volume: int = 1_000_000,
    upper_circuit: Decimal | None = None,
) -> int:
    """A liquid textbook signal. Prices are the ADJUSTED series, as ``ohlcv_daily`` stores them.

    ``adj_factor`` is what a split leaves behind: the stored (adjusted) price is
    ``raw x adj_factor``, so a factor of 0.5 means the exchange printed twice these numbers.
    """
    instrument_id = await make_instrument(session, symbol)
    closes = _breakout_shape(len(dates), breakout=breakout)
    for index, (on, close) in enumerate(zip(dates, closes, strict=True)):
        last = index == len(dates) - 1
        # The signal bar closes on its high and opened at yesterday's close — `04` §3.2's filter D
        # wants a close in the top 40% of the range, and this is the top of it.
        high = close
        low = closes[index - 1] if last else close * 0.99
        volume = signal_volume if last else base_volume
        session.add(
            OhlcvDaily(
                instrument_id=instrument_id,
                date=on,
                open=Decimal(str(round(low, 4))),
                high=Decimal(str(round(high, 4))),
                low=Decimal(str(round(low, 4))),
                close=Decimal(str(round(close, 4))),
                volume=volume,
                close_raw=Decimal(str(round(close / float(adj_factor), 4))),
                volume_raw=volume,
                turnover=Decimal(str(round(close * volume, 2))),
                adj_factor=adj_factor,
                upper_circuit=upper_circuit,
                source="nse",
            )
        )
    await session.flush()
    return instrument_id


async def _write_flat(session: AsyncSession, symbol: str, dates: list[dt.date]) -> int:
    """A name that goes nowhere: in the universe, never a signal, and below its own average."""
    instrument_id = await make_instrument(session, symbol)
    for on in dates:
        session.add(
            OhlcvDaily(
                instrument_id=instrument_id,
                date=on,
                open=Decimal("100.0000"),
                high=Decimal("100.0000"),
                low=Decimal("100.0000"),
                close=Decimal("100.0000"),
                volume=100_000,
                close_raw=Decimal("100.0000"),
                volume_raw=100_000,
                turnover=Decimal("10000000.00"),
                adj_factor=Decimal(1),
                source="nse",
            )
        )
    await session.flush()
    return instrument_id


async def _detect(session: AsyncSession, user_id: int, on: dt.date = AS_OF) -> StepOutcome:
    outcome = StepOutcome()
    await run_detect_vbt(session, outcome, on, user_id=user_id)
    return outcome


@pytest.mark.db
class TestTheDetectionJob:
    async def test_a_textbook_breakout_is_detected_and_stored(self, session: AsyncSession) -> None:
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, HISTORY)
        await _write_breakout(session, "VBTCO", dates)
        await _write_flat(session, "FLATCO", dates)

        outcome = await _detect(session, user_id)

        rows = (
            (await session.execute(sa.select(VbSignalDaily).where(VbSignalDaily.date == AS_OF)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.user_id == user_id
        assert row.state == SignalState.SIGNAL.value
        assert row.failed_filters == []
        assert row.limit_price == Decimal("96.00")
        # `04` §6.1: 12% below the limit, floored to the tick.
        assert row.stop_price == Decimal("84.45")
        assert row.sma_200 is not None and row.sma_200 < row.limit_price
        assert row.high_20_prior == Decimal("90.00")
        assert row.rank_key > 0
        assert row.locked_upper_circuit is False
        assert outcome.rows_out == 1
        # `03` §2: rounded at write time, not at render time.
        assert row.limit_price == row.limit_price.quantize(Decimal("0.01"))

    async def test_the_breadth_row_records_the_gate_and_the_funnel(
        self, session: AsyncSession
    ) -> None:
        """ "No signals today" is only useful with "out of N names with a 200-day average"."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, HISTORY)
        await _write_breakout(session, "VBTCO", dates)
        await _write_flat(session, "FLATCO", dates)

        await _detect(session, user_id)

        row = (
            await session.execute(sa.select(VbBreadthDaily).where(VbBreadthDaily.date == AS_OF))
        ).scalar_one()
        assert row.measured_count == 2
        # The riser is above its average; the flat name sits exactly on its own and is not above.
        assert row.above_count == 1
        assert row.pct_above_dma == Decimal("50.0000")
        assert row.gate == Gate.OPEN.value
        assert row.thin_session is False
        assert row.detail is not None
        assert row.detail["signals"] == 1
        assert row.detail["with_an_average"] == 2
        assert row.dma_bars == DEFAULT_VBT_CONFIG.breadth.dma_bars

    async def test_a_shut_gate_still_writes_the_row_and_the_signals(
        self, session: AsyncSession
    ) -> None:
        """`04` §4.3 — the gate refuses new *entries*. Detection is what the gate is measured on,
        so a shut gate that stopped the detector would be measuring itself."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, HISTORY)
        await _write_breakout(session, "VBTCO", dates)
        for index in range(4):
            await _write_flat(session, f"DULL{index}", dates)

        await _detect(session, user_id)

        row = (
            await session.execute(sa.select(VbBreadthDaily).where(VbBreadthDaily.date == AS_OF))
        ).scalar_one()
        assert row.pct_above_dma == Decimal("20.0000")
        assert row.gate == Gate.SHUT.value
        signals = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(VbSignalDaily)
                .where(VbSignalDaily.date == AS_OF)
            )
        ).scalar_one()
        assert signals == 1

    async def test_a_rejected_scan_hit_is_stored_with_the_letters_that_failed(
        self, session: AsyncSession
    ) -> None:
        """DECISIONS-VB PACK.6 — the ablation table is the argument for the six filters, and a
        system that stores only what it accepted cannot show a person what it passed over."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, HISTORY)
        # A 22% day: it clears the volume scan and fails filter E, and only filter E.
        await _write_breakout(session, "SPIKECO", dates, breakout=110.0)

        await _detect(session, user_id)

        row = (
            await session.execute(sa.select(VbSignalDaily).where(VbSignalDaily.date == AS_OF))
        ).scalar_one()
        assert row.state == SignalState.SCAN_ONLY.value
        assert row.failed_filters == ["E"]

    async def test_running_it_twice_changes_no_rows(self, session: AsyncSession) -> None:
        """House rule 7, over the whole job rather than over one insert."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, HISTORY)
        await _write_breakout(session, "VBTCO", dates)

        await _detect(session, user_id)
        first = (
            (await session.execute(sa.select(VbSignalDaily).where(VbSignalDaily.date == AS_OF)))
            .scalars()
            .all()
        )
        snapshot = [(row.instrument_id, row.state, row.limit_price, row.rank_key) for row in first]

        await _detect(session, user_id)
        second = (
            (await session.execute(sa.select(VbSignalDaily).where(VbSignalDaily.date == AS_OF)))
            .scalars()
            .all()
        )
        assert [
            (row.instrument_id, row.state, row.limit_price, row.rank_key) for row in second
        ] == snapshot
        breadth = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(VbBreadthDaily)
                .where(VbBreadthDaily.date == AS_OF)
            )
        ).scalar_one()
        assert breadth == 1

    async def test_a_date_with_no_bars_writes_nothing_and_says_so(
        self, session: AsyncSession
    ) -> None:
        user_id = await _sole_user(session)
        outcome = await _detect(session, user_id, dt.date(2026, 8, 19))
        assert outcome.status is StepStatus.SKIPPED
        assert "no bars" in str(outcome.detail.get("skipped_reason", ""))
        rows = (
            await session.execute(sa.select(sa.func.count()).select_from(VbSignalDaily))
        ).scalar_one()
        assert rows == 0

    async def test_a_split_stores_the_exchange_price_as_the_limit(
        self, session: AsyncSession
    ) -> None:
        """The rules read the adjusted series; the broker is sent the exchange price. Here the
        stored series is twice what the exchange printed, and the limit is the exchange's."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, HISTORY)
        await _write_breakout(session, "SPLITCO", dates, adj_factor=Decimal(2))

        await _detect(session, user_id)

        row = (
            await session.execute(sa.select(VbSignalDaily).where(VbSignalDaily.date == AS_OF))
        ).scalar_one()
        assert row.close == Decimal("96.00")
        assert row.close_raw == Decimal("48.0000")
        assert row.limit_price == Decimal("48.00")
        assert row.stop_price == Decimal("42.20")
        assert row.adj_factor == Decimal(2)
        # The context levels come back into today's money too, or a page would compare ₹48 with
        # a 200-day average of ₹90 and report a name below a trend it is above.
        assert row.sma_200 is not None and row.sma_200 < row.limit_price
        assert row.high_20_prior == Decimal("45.00")

    async def test_a_locked_bar_is_stored_and_flagged_rather_than_dropped(
        self, session: AsyncSession
    ) -> None:
        """`04` §3.3 — the plan is what skips it, so the page can say why a name it can see is a
        name it will not buy."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, HISTORY)
        await _write_breakout(session, "LOCKCO", dates, upper_circuit=Decimal("96.0000"))

        await _detect(session, user_id)

        row = (
            await session.execute(sa.select(VbSignalDaily).where(VbSignalDaily.date == AS_OF))
        ).scalar_one()
        assert row.locked_upper_circuit is True
        assert row.state == SignalState.SIGNAL.value


@pytest.mark.db
class TestTheUniverse:
    async def test_an_etf_is_not_in_the_universe(self, session: AsyncSession) -> None:
        """`04` §1 — the `etf` universe is the authority; the patterns catch what it missed."""
        dates = await _sessions_before(session, AS_OF, 5)
        fund = await make_instrument(session, "NIFTYBEES")
        ordinary = await make_instrument(session, "ACMECO")
        # `make seed` already defines the `etf` universe, so this uses it rather than a second
        # one: two rows with the same slug is a unique-constraint violation, and a test that
        # created its own would be asserting against a universe the job does not read.
        slug = DEFAULT_VBT_CONFIG.data.etf_universe_slug
        index_id = (
            await session.execute(sa.select(IndexDef.id).where(IndexDef.slug == slug))
        ).scalar_one_or_none()
        if index_id is None:
            index = IndexDef(id=9_101, slug=slug, name="ETFs")
            session.add(index)
            await session.flush()
            index_id = index.id
        session.add(
            IndexMemberDaily(index_id=index_id, instrument_id=fund, date=dates[-1], weight=None)
        )
        await session.flush()

        universe = await load_universe(session)
        assert ordinary in universe
        assert fund not in universe

    async def test_the_symbol_pattern_catches_a_fund_the_list_missed(
        self, session: AsyncSession
    ) -> None:
        stray = await make_instrument(session, "SOMETHINGETF")
        assert stray not in await load_universe(session)

    async def test_the_pattern_is_narrow_enough_to_keep_a_company(
        self, session: AsyncSession
    ) -> None:
        """A wider rule would take GOLDIAM with GOLDBEES."""
        company = await make_instrument(session, "GOLDIAM")
        assert company in await load_universe(session)


@pytest.mark.db
class TestTheStepCannotFailTheNight:
    async def test_a_detector_that_raises_leaves_the_run_succeeded(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`docs/vbt/06` VB4, and DECISIONS-VB VB0.5.

        A signal nobody wrote is a page saying "no candidates today"; a nightly run that failed
        is a screener serving yesterday to everybody. The trade is not close.
        """
        user_id = await _sole_user(session)
        run = PipelineRun(trade_date=AS_OF, status="running", started_at=dt.datetime.now(dt.UTC))
        session.add(run)
        await session.flush()

        async def _explode(*_args: object, **_kwargs: object) -> int:
            raise RuntimeError("the detector fell over")

        monkeypatch.setattr(vbt_task, "run_detect_vbt", _explode)
        await run_compute_vbt_step(
            session, int(run.id), AS_OF, PipelineDependencies(provider=None, vbt_user_id=user_id)
        )

        step = (
            await session.execute(
                sa.select(PipelineRunStep).where(
                    PipelineRunStep.run_id == run.id,
                    PipelineRunStep.step == PipelineStep.COMPUTE_VBT.value,
                )
            )
        ).scalar_one()
        assert step.status == StepStatus.SKIPPED.value
        assert "RuntimeError" in str(step.error)

    async def test_no_sole_tenant_skips_rather_than_inventing_one(
        self, session: AsyncSession
    ) -> None:
        run = PipelineRun(trade_date=AS_OF, status="running", started_at=dt.datetime.now(dt.UTC))
        session.add(run)
        await session.flush()
        await run_compute_vbt_step(
            session, int(run.id), AS_OF, PipelineDependencies(provider=None, vbt_user_id=None)
        )
        step = (
            await session.execute(
                sa.select(PipelineRunStep).where(
                    PipelineRunStep.run_id == run.id,
                    PipelineRunStep.step == PipelineStep.COMPUTE_VBT.value,
                )
            )
        ).scalar_one()
        assert step.status == StepStatus.SKIPPED.value
        assert "BASKFY_SOLE_USER_ID" in str(step.error)

    async def test_the_flag_off_skips_the_step(self, session: AsyncSession) -> None:
        user_id = await _sole_user(session)
        run = PipelineRun(trade_date=AS_OF, status="running", started_at=dt.datetime.now(dt.UTC))
        session.add(run)
        await session.flush()
        await run_compute_vbt_step(
            session,
            int(run.id),
            AS_OF,
            PipelineDependencies(provider=None, vbt_user_id=user_id, vbt_nightly_enabled=False),
        )
        step = (
            await session.execute(
                sa.select(PipelineRunStep).where(
                    PipelineRunStep.run_id == run.id,
                    PipelineRunStep.step == PipelineStep.COMPUTE_VBT.value,
                )
            )
        ).scalar_one()
        assert step.status == StepStatus.SKIPPED.value
        assert "NIGHTLY" in str(step.error)


@pytest.mark.db
class TestTheClock:
    async def test_the_lookback_counts_sessions_not_days(self, session: AsyncSession) -> None:
        """260 trading days is about 375 calendar days, and the error is not constant."""
        start = await lookback_start(session, AS_OF, LOOKBACK_SESSIONS)
        assert (AS_OF - start).days > LOOKBACK_SESSIONS
        sessions = await _sessions_before(session, AS_OF, LOOKBACK_SESSIONS)
        assert start == sessions[0]

    async def test_the_detector_only_ever_reads_the_session_it_was_asked_for(
        self, session: AsyncSession
    ) -> None:
        """`04` §10 — signals and breadth are the last **completed** session, always. A bar dated
        after the as-of date must not reach the row, whatever else is in the table."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, HISTORY)
        instrument_id = await _write_breakout(session, "VBTCO", dates)
        later = (await _sessions_before(session, dt.date(2026, 8, 25), 5))[-1]
        session.add(
            OhlcvDaily(
                instrument_id=instrument_id,
                date=later,
                open=Decimal("500.0000"),
                high=Decimal("500.0000"),
                low=Decimal("500.0000"),
                close=Decimal("500.0000"),
                volume=9_000_000,
                close_raw=Decimal("500.0000"),
                volume_raw=9_000_000,
                turnover=Decimal("4500000000.00"),
                adj_factor=Decimal(1),
                source="nse",
            )
        )
        await session.flush()

        await _detect(session, user_id)

        rows = (await session.execute(sa.select(VbSignalDaily))).scalars().all()
        assert {row.date for row in rows} == {AS_OF}
        assert rows[0].close == Decimal("96.00")

    async def test_the_retry_asks_before_it_works(self, session: AsyncSession) -> None:
        """The 21:10 Beat entry re-derives nothing the chain already wrote."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, HISTORY)
        await _write_breakout(session, "VBTCO", dates)
        assert await published_signal_count(session, user_id, AS_OF) == 0
        await _detect(session, user_id)
        assert await published_signal_count(session, user_id, AS_OF) == 1


@pytest.mark.db
class TestTheSettings:
    async def test_the_users_stop_reaches_the_stored_level(self, session: AsyncSession) -> None:
        """`03` §1: the stop is the one exit number that is a setting."""
        user_id = await _sole_user(session)
        row = (
            await session.execute(sa.select(VbConfig).where(VbConfig.user_id == user_id))
        ).scalar_one()
        row.stop_pct = Decimal("10.00")
        await session.flush()
        config = await load_vbt_config(session, user_id)
        assert config.exits.stop_pct == 10.0

        dates = await _sessions_before(session, AS_OF, HISTORY)
        await _write_breakout(session, "VBTCO", dates)
        await _detect(session, user_id)
        stored = (
            await session.execute(sa.select(VbSignalDaily).where(VbSignalDaily.date == AS_OF))
        ).scalar_one()
        assert stored.stop_price == Decimal("86.40")

    async def test_a_user_with_no_row_gets_the_defaults(self, session: AsyncSession) -> None:
        """A missing settings row is a seeding problem, not a reason to skip a night."""
        assert await load_vbt_config(session, 9_999_999) is DEFAULT_VBT_CONFIG

    async def test_the_setting_can_lower_the_book_but_never_widen_it(
        self, session: AsyncSession
    ) -> None:
        """`04` §9.1 takes the smaller of the setting and the strategy's own ten slots."""
        user_id = await _sole_user(session)
        row = (
            await session.execute(sa.select(VbConfig).where(VbConfig.user_id == user_id))
        ).scalar_one()
        row.max_open_positions = 15
        await session.flush()
        assert (await load_vbt_config(session, user_id)).sizing.max_slots == 10
        row.max_open_positions = 4
        await session.flush()
        assert (await load_vbt_config(session, user_id)).sizing.max_slots == 4


@pytest.mark.db
class TestTheNeighboursAreUntouched:
    async def test_the_swing_step_still_precedes_this_one_in_the_chain(self) -> None:
        chain = list(PipelineStep)
        assert chain.index(PipelineStep.COMPUTE_SWING) < chain.index(PipelineStep.COMPUTE_VBT)
        # It was `chain[-1] is COMPUTE_VBT` until TW4 added `compute_twt` after it. The property
        # this test is for was never the position — `steps.py` says so where `POST_PUBLISH_STEPS`
        # is defined: "'last' stopped being the property the moment there were two of them". What
        # must hold is that everything from here on is a step the run's success does not depend
        # on, which is what the set membership below asserts and the index above cannot.
        assert set(chain[chain.index(PipelineStep.COMPUTE_VBT) :]) <= POST_PUBLISH_STEPS

    async def test_the_detector_writes_no_swing_row(self, session: AsyncSession) -> None:
        """`02` Track C §5 — this sleeve never writes an `sw_` row."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, HISTORY)
        await _write_breakout(session, "VBTCO", dates)
        await _detect(session, user_id)
        swing_rows = (
            await session.execute(sa.select(sa.func.count()).select_from(SwSetupDaily))
        ).scalar_one()
        assert swing_rows == 0
