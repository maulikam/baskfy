"""SW3's acceptance: the detection job, against a real database.

Five claims, and the module plan states four of them almost word for word:

1. **Running the task twice for a date changes no rows.** House rule 7. A night that was killed
   and restarted must leave the same table as one that ran once.
2. **A date with no published bars writes nothing and says so.** Not an exception, and not a
   silent zero either: `SKIPPED` with a reason, because "no candidates" and "no data" look
   identical on a page and are opposite problems.
3. **A split inside a base yields a stored trigger equal to the raw price.** The detectors read
   the adjusted series so a 1:2 split does not fake a 50% flagpole; a person types the *exchange*
   price into a broker. Both have to be true at once, and this is where they meet.
4. **The step's failure leaves the run SUCCEEDED.** `docs/swing/06`: the step is "unable to fail
   the run".
5. And the one the module plan words as "a `sw_setup_daily` row for a real date exists on the dev
   stack", which the machine this ran on cannot satisfy — its `ohlcv_daily` holds ten sessions
   and a detector needs 125 (`docs/swing/STATUS.md` SW0, DECISIONS-SW SW0.2). The substitute is
   the same assertion against bars this file writes: a textbook flag, drawn to `04` §2's shape,
   is detected, scored, adjusted and stored, and every stored number is checked.

The synthetic series is deliberately the same shape as `packages/core/tests/swing_fixtures.py`
draws — a flat stretch, a pole, a tightening base — but written again here rather than imported,
because pytest gives test files no package and the two trees cannot import each other.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    AppUser,
    IndexDef,
    IndexMemberDaily,
    IndexSnapshotDaily,
    Instrument,
    OhlcvDaily,
    PipelineRun,
    PipelineRunStep,
    SwConfig,
    SwMarketDaily,
    SwSetupDaily,
    TradingDay,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.swing.config import Setup
from baskfy_core.swing.market import MarketGate
from baskfy_worker.deps import PipelineDependencies
from baskfy_worker.orchestrator import run_compute_swing_step
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.tasks import swing as swing_task
from baskfy_worker.tasks.swing import (
    LOOKBACK_SESSIONS,
    apply_score_adjustments,
    lookback_start,
    run_detect_swing,
    sector_breadth,
    to_exchange_prices,
)

pytestmark = requires_db

#: A Tuesday inside the seeded calendar, far enough into it that 140 sessions precede it.
AS_OF = dt.date(2026, 8, 18)

USER_EMAIL = "sw3@example.com"


async def _sole_user(session: AsyncSession) -> int:
    user = AppUser(public_id="sw3-user", email=USER_EMAIL)
    session.add(user)
    await session.flush()
    session.add(SwConfig(user_id=user.id, updated_by="test"))
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


def _flag_shape(bars: int) -> list[float]:
    """Flat, a 50% pole, then a tightening base with higher lows — `04` §2's picture.

    The proportions are the ones `swing_fixtures.flag_series` uses, because they are known to
    satisfy every rule in §2.1-§2.5 at once: a 30%+ pole, a base between 10 and 60 bars, under 30%
    deep, tight, on a rising 20-day average, with volume drying up.
    """
    pole_bars, base_bars = 20, 35
    flat = bars - pole_bars - base_bars
    top = 150.0
    closes = [100.0] * flat
    closes += [100.0 + (top - 100.0) * (i + 1) / pole_bars for i in range(pole_bars)]
    for i in range(base_bars):
        swing = (0.03 - 0.025 * i / (base_bars - 1)) * (-1.0 if i % 2 == 0 else 1.0)
        drift = 0.02 * i / (base_bars - 1)
        closes.append(top * (0.95 + drift + swing))
    return closes


async def _write_flag(
    session: AsyncSession,
    symbol: str,
    dates: list[dt.date],
    *,
    adj_factor: Decimal = Decimal(1),
    upper_circuit: Decimal | None = None,
) -> int:
    """A liquid textbook flag. Prices are the ADJUSTED series, as `ohlcv_daily` stores them.

    ``adj_factor`` is what a split leaves behind: the stored (adjusted) price is
    ``raw x adj_factor``, so a factor of 0.5 means the exchange printed twice these numbers.
    """
    instrument_id = await make_instrument(session, symbol)
    closes = _flag_shape(len(dates))
    pole_bars, base_bars = 20, 35
    flat = len(dates) - pole_bars - base_bars
    for index, (on, close) in enumerate(zip(dates, closes, strict=True)):
        # A wide enough daily range to clear the 3.5% ADR floor, and volume that dries up through
        # the base so §2.5's dry-up rule is satisfied.
        high = close * 1.025
        low = close * 0.975
        volume = 1_000_000 if index < flat else 3_000_000
        if index >= flat + pole_bars:
            volume = int(1_500_000 - 1_000_000 * (index - flat - pole_bars) / base_bars)
        session.add(
            OhlcvDaily(
                instrument_id=instrument_id,
                date=on,
                open=Decimal(str(round(close, 4))),
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


async def _detect(session: AsyncSession, user_id: int, on: dt.date = AS_OF) -> StepOutcome:
    outcome = StepOutcome()
    await run_detect_swing(session, outcome, on, user_id=user_id)
    return outcome


@pytest.mark.db
class TestTheDetectionJob:
    async def test_a_textbook_flag_is_detected_and_stored(self, session: AsyncSession) -> None:
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        await _write_flag(session, "FLAGCO", dates)

        outcome = await _detect(session, user_id)

        rows = (
            (await session.execute(sa.select(SwSetupDaily).where(SwSetupDaily.date == AS_OF)))
            .scalars()
            .all()
        )
        assert [row.setup for row in rows] == [Setup.FLAG.value]
        row = rows[0]
        assert row.user_id == user_id
        assert row.status == "SETTING_UP"
        assert 0 < row.score <= 100
        assert row.close is not None
        assert row.trigger is not None and row.trigger > row.close
        assert row.stop_ref is not None and row.stop_ref < row.close
        assert row.base_bars is not None and row.base_bars >= 10
        assert row.locked_upper_circuit is False
        assert outcome.rows_out == 1
        # `docs/swing/03` §2: the stored levels are rounded at write time, not at render time.
        assert row.trigger == row.trigger.quantize(Decimal("0.01"))

    async def test_the_funnel_says_how_many_names_reached_each_stage(
        self, session: AsyncSession
    ) -> None:
        """ "No flags today" is only useful with "out of N liquid names"."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        await _write_flag(session, "FLAGCO", dates)

        outcome = await _detect(session, user_id)

        funnel = outcome.detail["funnel"]
        assert isinstance(funnel, dict)
        assert funnel["with_a_bar_today"] == 1
        assert funnel["liquid"] == 1
        assert funnel["candidates"] == {"FLAG": 1, "EP": 0, "PARABOLIC_SHORT": 0}

    async def test_running_it_twice_changes_no_rows(self, session: AsyncSession) -> None:
        """House rule 7, over the whole job rather than over one insert."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        await _write_flag(session, "FLAGCO", dates)

        await _detect(session, user_id)
        first = await _snapshot(session)
        await _detect(session, user_id)
        second = await _snapshot(session)

        assert first == second
        count = (
            await session.execute(sa.select(sa.func.count()).select_from(SwSetupDaily))
        ).scalar_one()
        assert count == 1

    async def test_a_date_with_no_published_bars_writes_nothing_and_says_so(
        self, session: AsyncSession
    ) -> None:
        user_id = await _sole_user(session)
        outcome = await _detect(session, user_id, dt.date(2026, 8, 19))

        assert outcome.status is StepStatus.SKIPPED
        assert "no published bars" in str(outcome.detail["skipped_reason"])
        count = (
            await session.execute(sa.select(sa.func.count()).select_from(SwSetupDaily))
        ).scalar_one()
        assert count == 0

    async def test_a_date_no_instrument_traded_is_skipped_separately(
        self, session: AsyncSession
    ) -> None:
        """Bars exist in the window, but none is dated today — a holiday the calendar missed,
        or a date asked about before the night ran. A different reason from "no bars at all",
        and the operator needs to be able to tell them apart."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        await _write_flag(session, "FLAGCO", dates[:-1])

        outcome = await _detect(session, user_id)

        assert outcome.status is StepStatus.SKIPPED
        assert "no instrument has a bar" in str(outcome.detail["skipped_reason"])

    async def test_a_split_inside_the_base_stores_the_exchange_price(
        self, session: AsyncSession
    ) -> None:
        """The module plan's own criterion, and the reason `adj_factor` is on the row.

        The stored series is adjusted (``raw x 0.5`` after a 1:2 split), so the detector sees an
        unbroken base. The trigger a person sends to a broker is the exchange print, which is
        twice the adjusted level — and the factor is stored beside it so tomorrow's job can tell
        that this level was computed under an adjustment.
        """
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        await _write_flag(session, "SPLITCO", dates, adj_factor=Decimal("0.5"))

        await _detect(session, user_id)

        row = (
            await session.execute(sa.select(SwSetupDaily).where(SwSetupDaily.date == AS_OF))
        ).scalar_one()
        assert row.adj_factor == Decimal("0.5000000000")
        # The highest high of the last 20 adjusted bars, in exchange prices.
        highs = (
            await session.execute(
                sa.select(sa.func.max(OhlcvDaily.high))
                .where(OhlcvDaily.instrument_id == row.instrument_id)
                .where(OhlcvDaily.date > dates[-21])
            )
        ).scalar_one()
        # Quantized, because the row was rounded at write time (house rule 8) and the raw
        # arithmetic here has not been. Comparing the two at full precision would be asserting
        # that rounding did not happen.
        expected_trigger = (Decimal(highs) / Decimal("0.5")).quantize(Decimal("0.01"))
        assert row.trigger == expected_trigger
        adjusted_close = await _adjusted_close(session, row.instrument_id, AS_OF)
        assert row.close == (adjusted_close * 2).quantize(Decimal("0.01"))

    async def test_a_circuit_band_is_compared_in_the_adjusted_space(
        self, session: AsyncSession
    ) -> None:
        """The band is an exchange print and `high` has been adjusted in place.

        Without converting the band on the way in, the morning after a 1:2 split every stock in
        the market would read as locked at its upper circuit — the adjusted high is twice the
        band it is compared against. Here the band is set to the raw high of the last bar, so a
        correct comparison says "locked" and an unconverted one would say so too but for the
        wrong reason; the check that matters is the *unlocked* case below it.
        """
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        # A band far above every raw price: nothing is locked, whatever the factor.
        await _write_flag(
            session,
            "BANDCO",
            dates,
            adj_factor=Decimal("0.5"),
            upper_circuit=Decimal("100000"),
        )

        await _detect(session, user_id)

        row = (
            await session.execute(sa.select(SwSetupDaily).where(SwSetupDaily.date == AS_OF))
        ).scalar_one()
        assert row.locked_upper_circuit is False


@pytest.mark.db
class TestTheScoreAdjustments:
    async def test_a_young_listing_earns_five_points(self, session: AsyncSession) -> None:
        """`04` §2.6: "+5 for `listed_within_2y`", capped at 100."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        old_id = await _write_flag(session, "OLDCO", dates)
        young_id = await _write_flag(session, "YOUNGCO", dates)
        await session.execute(
            sa.update(Instrument)
            .where(Instrument.id == young_id)
            .values(listed_on=AS_OF - dt.timedelta(days=200))
        )

        await _detect(session, user_id)

        scores = {
            row.instrument_id: (row.score, row.listed_within_2y)
            for row in (
                await session.execute(sa.select(SwSetupDaily).where(SwSetupDaily.date == AS_OF))
            ).scalars()
        }
        assert scores[old_id][1] is False
        assert scores[young_id][1] is True
        assert scores[young_id][0] - scores[old_id][0] == Decimal("5.00")

    async def test_the_bonus_cannot_push_a_score_past_a_hundred(
        self, session: AsyncSession
    ) -> None:
        frame = pl.DataFrame(
            {"instrument_id": [1], "score": [99.0], "setup": ["FLAG"]},
            schema={"instrument_id": pl.Int64, "score": pl.Float64, "setup": pl.String},
        )
        out = apply_score_adjustments(
            frame, listed_within_2y={1: True}, sectors={1: "nifty-it"}, hot_sectors={"nifty-it"}
        )
        assert out["score"].item() == 100.0


@pytest.mark.db
class TestTheMarketRow:
    async def test_the_gate_and_the_tier_are_written(self, session: AsyncSession) -> None:
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        await _write_flag(session, "FLAGCO", dates)

        await _detect(session, user_id)

        row = (
            await session.execute(sa.select(SwMarketDaily).where(SwMarketDaily.date == AS_OF))
        ).scalar_one()
        assert row.user_id == user_id
        assert row.gate in {g.value for g in MarketGate}
        assert row.constituent_count == 1
        # No closed trades, so the ladder cannot climb: rung 0, two positions, 25% of the sleeve.
        assert (row.exposure_level, row.max_open_positions) == (0, 2)
        assert row.max_exposure_pct == Decimal("25.00")
        assert row.detail is not None
        assert row.detail["closed_r_multiples"] == []
        # A10 (SW10.5): the ladder reads real closes only, from day one.
        assert row.detail["closed_trades_read"] == "real"

    async def test_a_second_run_overwrites_the_market_row(self, session: AsyncSession) -> None:
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        await _write_flag(session, "FLAGCO", dates)

        await _detect(session, user_id)
        await _detect(session, user_id)

        count = (
            await session.execute(sa.select(sa.func.count()).select_from(SwMarketDaily))
        ).scalar_one()
        assert count == 1

    async def test_the_index_reading_falls_back_and_then_gives_up(
        self, session: AsyncSession
    ) -> None:
        """`04` §8.2: NIFTY 500, then NIFTY 50, then no reading at all.

        A missing benchmark must not read as a bear market — `market_gate` ignores a `None`
        index — so the honest outcome of "we hold no levels" is a row that says which index was
        used, and here that is nobody.
        """
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        await _write_flag(session, "FLAGCO", dates)

        await _detect(session, user_id)

        row = (
            await session.execute(sa.select(SwMarketDaily).where(SwMarketDaily.date == AS_OF))
        ).scalar_one()
        assert row.index_slug is None
        assert row.index_close is None

    async def test_an_index_with_enough_history_is_read(self, session: AsyncSession) -> None:
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        await _write_flag(session, "FLAGCO", dates)
        index_id = (
            await session.execute(sa.select(IndexDef.id).where(IndexDef.slug == "nifty-500"))
        ).scalar_one()
        for offset, on in enumerate(dates[-25:]):
            session.add(
                IndexSnapshotDaily(
                    index_id=index_id, date=on, level=Decimal(str(20000 + offset * 10))
                )
            )
        await session.flush()

        await _detect(session, user_id)

        row = (
            await session.execute(sa.select(SwMarketDaily).where(SwMarketDaily.date == AS_OF))
        ).scalar_one()
        assert row.index_slug == "nifty-500"
        assert row.index_close == Decimal("20240.00")
        # A rising series, so the close is above both averages.
        assert row.index_ma_fast is not None and row.index_close > row.index_ma_fast
        assert row.index_ma_slow is not None and row.index_ma_fast > row.index_ma_slow


@pytest.mark.db
class TestSectorsArePointInTime:
    async def test_a_candidate_carries_the_sector_it_was_in_that_day(
        self, session: AsyncSession
    ) -> None:
        """House rule 5. A name that joined NIFTY IT last week was not an IT stock in March,
        and the sector is read for the detection date rather than for today."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        instrument_id = await _write_flag(session, "FLAGCO", dates)
        sector_id = 900
        session.add(IndexDef(id=sector_id, slug="nifty-it", name="NIFTY IT", sort_order=900))
        await session.flush()
        session.add(
            IndexMemberDaily(
                index_id=sector_id, date=AS_OF, instrument_id=instrument_id, source="nse_file"
            )
        )
        await session.flush()

        await _detect(session, user_id)

        row = (
            await session.execute(sa.select(SwSetupDaily).where(SwSetupDaily.date == AS_OF))
        ).scalar_one()
        assert row.sector_slug == "nifty-it"

    async def test_membership_on_another_date_does_not_count(self, session: AsyncSession) -> None:
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        instrument_id = await _write_flag(session, "FLAGCO", dates)
        session.add(IndexDef(id=901, slug="nifty-it", name="NIFTY IT", sort_order=901))
        await session.flush()
        session.add(
            IndexMemberDaily(
                index_id=901,
                date=AS_OF - dt.timedelta(days=30),
                instrument_id=instrument_id,
                source="nse_file",
            )
        )
        await session.flush()

        await _detect(session, user_id)

        row = (
            await session.execute(sa.select(SwSetupDaily).where(SwSetupDaily.date == AS_OF))
        ).scalar_one()
        assert row.sector_slug is None


class TestTheHelpers:
    """The pure parts, without a database."""

    def test_a_level_is_divided_by_its_own_adjustment_factor(self) -> None:
        frame = pl.DataFrame(
            {
                "close": [50.0],
                "trigger": [55.0],
                "stop_ref": [45.0],
                "pivot_high": [55.0],
                "adj_factor": [0.5],
            }
        )
        out = to_exchange_prices(frame)
        assert out["trigger"].item() == 110.0
        assert out["close"].item() == 100.0

    def test_a_zero_adjustment_factor_is_treated_as_one(self) -> None:
        """A zero would be a division by zero. It should never happen — the column is NOT NULL
        with a default of 1 — and a nightly job is the wrong place to find out."""
        frame = pl.DataFrame(
            {
                "close": [50.0],
                "trigger": [55.0],
                "stop_ref": [45.0],
                "pivot_high": [55.0],
                "adj_factor": [0.0],
            }
        )
        assert to_exchange_prices(frame)["trigger"].item() == 55.0

    def test_a_sector_with_too_few_names_is_not_a_sector(self) -> None:
        """Two names, one of them above its 20-day average, is not a 50% sector."""
        frame = pl.DataFrame(
            {
                "instrument_id": [1, 2],
                "close": [100.0, 100.0],
                "ma_slow": [90.0, 110.0],
            }
        )
        assert sector_breadth(frame, {1: "nifty-it", 2: "nifty-it"}) == []

    def test_sectors_come_back_strongest_first(self) -> None:
        frame = pl.DataFrame(
            {
                "instrument_id": list(range(1, 11)),
                "close": [100.0] * 10,
                "ma_slow": [90.0] * 5 + [90.0, 110.0, 110.0, 110.0, 110.0],
            }
        )
        sectors = {i: ("nifty-it" if i <= 5 else "nifty-metal") for i in range(1, 11)}
        assert sector_breadth(frame, sectors) == [
            ("nifty-it", 100.0, 5),
            ("nifty-metal", 20.0, 5),
        ]

    def test_an_empty_universe_has_no_sectors(self) -> None:
        empty = pl.DataFrame(
            schema={"instrument_id": pl.Int64, "close": pl.Float64, "ma_slow": pl.Float64}
        )
        assert sector_breadth(empty, {1: "nifty-it"}) == []


@pytest.mark.db
class TestTheLookbackIsInTradingDays:
    async def test_it_counts_sessions_rather_than_calendar_days(
        self, session: AsyncSession
    ) -> None:
        """200 trading days is about 290 calendar days, and the error is not a constant — a
        quarter with Diwali and two long weekends is a different number from one without."""
        start = await lookback_start(session, AS_OF, LOOKBACK_SESSIONS)
        span = (AS_OF - start).days
        assert span > LOOKBACK_SESSIONS
        assert span < LOOKBACK_SESSIONS * 2

    async def test_the_window_is_exactly_the_requested_number_of_sessions(
        self, session: AsyncSession
    ) -> None:
        start = await lookback_start(session, AS_OF, 10)
        sessions = await _sessions_before(session, AS_OF, 10)
        assert start == sessions[0]


@pytest.mark.db
class TestTheStepCannotFailTheNight:
    """`docs/swing/06` SW3: the step is "unable to fail the run".

    The nightly chain's ninth step is a hard gate that stops everything; its eleventh and twelfth
    are caches that must not. A detector bug that took `data_version` down with it would turn a
    swing-page outage into a screener outage, which is the trade nobody would make.

    Tested against `run_compute_swing_step` rather than against a full pipeline run. A full run
    would answer this question only after eleven earlier steps had also succeeded, and a test
    that fails because the data-quality gate rejected a synthetic universe has told you nothing
    about the twelfth step.
    """

    async def _run_step(
        self, session: AsyncSession, deps: PipelineDependencies, *, on: dt.date = AS_OF
    ) -> PipelineRunStep:
        run = PipelineRun(trade_date=on, status="running", started_at=dt.datetime.now(tz=dt.UTC))
        session.add(run)
        await session.flush()
        await run_compute_swing_step(session, run.id, on, deps)
        return (
            await session.execute(
                sa.select(PipelineRunStep).where(
                    PipelineRunStep.run_id == run.id, PipelineRunStep.step == "compute_swing"
                )
            )
        ).scalar_one()

    async def test_a_broken_detector_does_not_raise(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _explode(*args: object, **kwargs: object) -> int:
            raise RuntimeError("the detector fell over")

        monkeypatch.setattr(swing_task, "run_detect_swing", _explode)
        user_id = await _sole_user(session)

        row = await self._run_step(
            session, PipelineDependencies(provider=object(), swing_user_id=user_id)
        )

        assert row.status == "skipped"
        assert "the detector fell over" in str(row.error)

    async def test_a_deployment_with_no_sole_tenant_skips_the_step(
        self, session: AsyncSession
    ) -> None:
        """The `sw_` schema is keyed by user; a nightly job may not invent one."""
        row = await self._run_step(session, PipelineDependencies(provider=object()))

        assert row.status == "skipped"
        assert "BASKFY_SOLE_USER_ID" in str(row.error)

    async def test_a_working_detector_records_the_funnel_on_the_step(
        self, session: AsyncSession
    ) -> None:
        """The happy path through the same seam, so the two cannot drift apart."""
        user_id = await _sole_user(session)
        dates = await _sessions_before(session, AS_OF, 140)
        await _write_flag(session, "FLAGCO", dates)

        row = await self._run_step(
            session, PipelineDependencies(provider=object(), swing_user_id=user_id)
        )

        assert row.status == "succeeded"
        assert row.rows_out == 1
        detail = row.error or {}
        funnel = detail.get("funnel")
        assert isinstance(funnel, dict)
        assert funnel["liquid"] == 1


async def _snapshot(session: AsyncSession) -> list[tuple[object, ...]]:
    rows = await session.execute(
        sa.select(
            SwSetupDaily.instrument_id,
            SwSetupDaily.setup,
            SwSetupDaily.status,
            SwSetupDaily.score,
            SwSetupDaily.trigger,
            SwSetupDaily.stop_ref,
            SwSetupDaily.sector_slug,
        ).order_by(SwSetupDaily.instrument_id, SwSetupDaily.setup)
    )
    return [tuple(row) for row in rows]


async def _adjusted_close(session: AsyncSession, instrument_id: int, on: dt.date) -> Decimal:
    return (
        await session.execute(
            sa.select(OhlcvDaily.close).where(
                OhlcvDaily.instrument_id == instrument_id, OhlcvDaily.date == on
            )
        )
    ).scalar_one()
