"""The ten steps of docs/03 §"Nightly pipeline" (Prompt 3 deliverables 2, 3 and 7)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from helpers import PRIOR_DATE, TRADE_DATE, add_bar, make_instrument, requires_db
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from baskfy_core.models import (
    FactorDaily,
    IndexMemberDaily,
    Instrument,
    MarketHealthDaily,
    PipelineRunStep,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.universes import MARKET_HEALTH_SLUGS, UNIVERSE_BY_SLUG
from baskfy_worker.calendar import (
    NotATradingDay,
    classify,
    previous_trading_days,
    reconcile_calendar,
    require_trading_day,
)
from baskfy_worker.deps import PipelineDependencies
from baskfy_worker.orchestrator import run_nightly_pipeline
from baskfy_worker.steps import (
    HardFailure,
    PipelineStep,
    RunStatus,
    StepOutcome,
    StepStatus,
    open_run,
    record_step,
)
from baskfy_worker.tasks.factors import (
    assert_mask_matches_membership,
    run_compute_factors,
    universe_masks,
)
from baskfy_worker.tasks.instruments import active_instruments
from baskfy_worker.tasks.market_health import run_compute_market_health
from baskfy_worker.tasks.publish import current_data_version

pytestmark = [pytest.mark.db, requires_db]

SATURDAY = dt.date(2026, 8, 15)
REPUBLIC_DAY = dt.date(2026, 1, 26)


class TestTradingDayAwareness:
    """Prompt 3 deliverable 7: "never attempt to ingest or compute for a non-trading day"."""

    async def test_a_saturday_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(NotATradingDay):
            await require_trading_day(session, SATURDAY)

    async def test_a_seeded_holiday_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(NotATradingDay, match="Republic Day"):
            await require_trading_day(session, REPUBLIC_DAY)

    async def test_a_date_outside_the_calendar_is_refused(self, session: AsyncSession) -> None:
        """Silently treating an unknown date as open would ingest into a gap nobody planned."""
        with pytest.raises(NotATradingDay, match="outside the loaded calendar"):
            await require_trading_day(session, dt.date(1990, 1, 2))

    async def test_a_weekday_is_allowed(self, session: AsyncSession) -> None:
        assert (await require_trading_day(session, TRADE_DATE)).is_trading_day

    async def test_the_pipeline_aborts_cleanly_on_a_non_trading_day(
        self, session: AsyncSession
    ) -> None:
        """A weekend is not a failure — it must not page anyone."""
        outcome = await run_nightly_pipeline(
            session, SATURDAY, PipelineDependencies(provider=object())
        )
        assert outcome.status is RunStatus.ABORTED
        assert outcome.data_version is None

    async def test_previous_trading_days_skips_the_weekend(self, session: AsyncSession) -> None:
        """The gate's rolling windows are defined over trading days, not calendar days."""
        days = await previous_trading_days(session, dt.date(2026, 8, 17), 3)
        assert SATURDAY not in days
        assert days[0] == dt.date(2026, 8, 14)


class TestCalendarReconciliation:
    """docs/09 §Schedule: the calendar is "asserted against observed bar dates".

    docs/04a records the seeded calendar as provisional — it is missing India's lunar-calendar
    holidays. This is the mechanism that fixes it, and CLAUDE.md assigns it to this prompt.
    """

    async def test_a_date_with_bars_becomes_authoritative(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        before = await classify(session, TRADE_DATE)
        assert before.is_provisional

        await reconcile_calendar(session, TRADE_DATE, TRADE_DATE)
        after = await classify(session, TRADE_DATE)
        assert after.source == "bhavcopy"
        assert not after.is_provisional

    async def test_a_sparse_range_does_not_invent_holidays(self, session: AsyncSession) -> None:
        """A date with no bars because the backfill has not reached it is not a holiday."""
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        await reconcile_calendar(session, dt.date(2026, 8, 10), TRADE_DATE)
        untouched = await classify(session, dt.date(2026, 8, 12))
        assert untouched.is_trading_day

    async def test_a_dense_range_infers_the_missing_holidays(self, session: AsyncSession) -> None:
        """The only mechanism that can fill in the lunar-calendar holidays docs/04a is missing."""
        instruments = [await make_instrument(session, f"SYM{i}", token=i + 1) for i in range(25)]
        for instrument in instruments:
            await add_bar(session, instrument, PRIOR_DATE, "100")
            await add_bar(session, instrument, TRADE_DATE, "100")

        result = await reconcile_calendar(session, PRIOR_DATE, TRADE_DATE)
        assert result.confirmed == 2
        assert result.inferred_holidays == 0

        # 2026-08-19 is a weekday nobody traded on, inside a densely populated range.
        result = await reconcile_calendar(session, PRIOR_DATE, dt.date(2026, 8, 19))
        assert result.inferred_holidays == 1
        assert not (await classify(session, dt.date(2026, 8, 19))).is_trading_day

    async def test_a_day_nse_published_is_never_called_a_holiday(
        self, session: AsyncSession
    ) -> None:
        """M62, and the reason it exists: this is what cost 2026-08-28.

        The nightly's fetch failed, no bars were written, and inference turned a Friday NSE had
        traded into an exchange holiday. Every backfill iterates trading days, so the day was
        then unreachable — the mistake could not self-heal. A `published` check that answers yes
        must veto the inference outright.
        """
        instruments = [await make_instrument(session, f"SYM{i}", token=i + 1) for i in range(25)]
        for instrument in instruments:
            await add_bar(session, instrument, PRIOR_DATE, "100")
            await add_bar(session, instrument, TRADE_DATE, "100")

        missed = dt.date(2026, 8, 19)

        async def published(day: dt.date) -> bool:
            return day == missed

        result = await reconcile_calendar(session, PRIOR_DATE, missed, published=published)
        assert result.inferred_holidays == 0
        assert (await classify(session, missed)).is_trading_day
        # And it is reported rather than passed over in silence: the calendar is right now, but
        # the bars are still missing and only a backfill fixes that.
        assert result.missed_sessions == (missed,)

    async def test_a_day_nse_did_not_publish_is_still_inferred(self, session: AsyncSession) -> None:
        """The veto must not disable the mechanism — lunar holidays still need inferring."""
        instruments = [await make_instrument(session, f"SYM{i}", token=i + 1) for i in range(25)]
        for instrument in instruments:
            await add_bar(session, instrument, PRIOR_DATE, "100")
            await add_bar(session, instrument, TRADE_DATE, "100")

        async def published(day: dt.date) -> bool:
            del day
            return False

        result = await reconcile_calendar(
            session, PRIOR_DATE, dt.date(2026, 8, 19), published=published
        )
        assert result.inferred_holidays == 1
        assert result.missed_sessions == ()
        assert not (await classify(session, dt.date(2026, 8, 19))).is_trading_day


class TestStepRecording:
    """Prompt 3 deliverable 2: every task "writes a pipeline_run_step row with
    rows_in/rows_out/duration/error"."""

    async def test_a_successful_step_records_its_counts(self, session: AsyncSession) -> None:
        run = await open_run(session, TRADE_DATE)
        async with record_step(session, run.id, PipelineStep.PUBLISH) as outcome:
            outcome.rows_in = 7
            outcome.rows_out = 5
            outcome.note(engine="test")

        row = (
            await session.execute(select(PipelineRunStep).where(PipelineRunStep.run_id == run.id))
        ).scalar_one()
        assert row.status == StepStatus.SUCCEEDED
        assert (row.rows_in, row.rows_out) == (7, 5)
        assert row.duration_ms is not None
        assert row.error == {"engine": "test"}

    async def test_a_failing_step_records_the_error_and_re_raises(
        self, session: AsyncSession
    ) -> None:
        """No silently swallowed exceptions: the orchestrator must see the failure too."""
        run = await open_run(session, TRADE_DATE)
        with pytest.raises(HardFailure):
            async with record_step(session, run.id, PipelineStep.COMPUTE_FACTORS):
                raise HardFailure("boom")

        row = (
            await session.execute(select(PipelineRunStep).where(PipelineRunStep.run_id == run.id))
        ).scalar_one()
        assert row.status == StepStatus.FAILED
        assert row.error is not None
        assert row.error["type"] == "HardFailure"
        assert "traceback" in row.error

    async def test_re_running_a_step_replaces_its_row(self, session: AsyncSession) -> None:
        run = await open_run(session, TRADE_DATE)
        for count in (1, 2):
            async with record_step(session, run.id, PipelineStep.PUBLISH) as outcome:
                outcome.rows_out = count

        rows = (
            (await session.execute(select(PipelineRunStep).where(PipelineRunStep.run_id == run.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].rows_out == 2

    async def test_a_skipped_step_is_distinguishable_from_a_successful_one(
        self, session: AsyncSession
    ) -> None:
        run = await open_run(session, TRADE_DATE)
        async with record_step(session, run.id, PipelineStep.APPLY_ADJUSTMENTS) as outcome:
            outcome.status = StepStatus.SKIPPED

        row = (
            await session.execute(select(PipelineRunStep).where(PipelineRunStep.run_id == run.id))
        ).scalar_one()
        assert row.status == StepStatus.SKIPPED


class TestComputeFactors:
    """The step's contract: one ``factor_daily`` row per bar, whatever the engine computes."""

    async def test_one_factor_row_per_bar(self, session: AsyncSession) -> None:
        for index in range(4):
            instrument = await make_instrument(session, f"SYM{index}", token=index + 1)
            await add_bar(session, instrument, TRADE_DATE, "100")
        written = await run_compute_factors(session, StepOutcome(), TRADE_DATE)
        assert written == 4

    async def test_it_records_which_engine_ran(self, session: AsyncSession) -> None:
        """docs/05 §8 and §12 are INFERRED, so which engine produced a row is part of its audit."""
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        outcome = StepOutcome()
        await run_compute_factors(session, outcome, TRADE_DATE)
        assert outcome.detail["engine"] == "polars"
        assert outcome.detail["factors_computed"] is True

    async def test_it_records_the_recovered_window_lengths(self, session: AsyncSession) -> None:
        """docs/13 §3's numbers are derived from the calendar, so they belong in the audit trail."""
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        outcome = StepOutcome()
        await run_compute_factors(session, outcome, TRADE_DATE)
        lengths = outcome.detail["window_lengths"]
        assert isinstance(lengths, dict)
        assert set(lengths) == {"1", "3", "6", "9", "12"}

    async def test_the_columns_it_can_derive_are_written(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        await run_compute_factors(session, StepOutcome(), TRADE_DATE)
        row = (
            await session.execute(select(FactorDaily).where(FactorDaily.date == TRADE_DATE))
        ).scalar_one()
        assert row.close == Decimal("100.00")
        assert row.close_raw == Decimal("100.00")
        assert row.series == "EQ"

    async def test_a_single_bar_yields_null_factors_not_zeros(self, session: AsyncSession) -> None:
        """docs/05 §Notation: an instrument without a full window gets NULL for that window,
        "never computed on a short window, because that would break the shared-denominator
        property". Zero would read as a real measurement."""
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        await run_compute_factors(session, StepOutcome(), TRADE_DATE)
        row = (
            await session.execute(select(FactorDaily).where(FactorDaily.date == TRADE_DATE))
        ).scalar_one()
        assert row.ret_12m is None
        assert row.vol_12m is None
        assert row.beta_12m is None
        assert row.ma_200 is None

    async def test_the_universe_mask_is_materialised_from_membership(
        self, session: AsyncSession
    ) -> None:
        """docs/06 §"Universe flags" assigns the 42 flags to the nightly build, not to Prompt 5."""
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        nifty50 = UNIVERSE_BY_SLUG["nifty-50"]
        session.add(
            IndexMemberDaily(index_id=nifty50.index_id, date=TRADE_DATE, instrument_id=instrument)
        )
        await session.flush()

        await run_compute_factors(session, StepOutcome(), TRADE_DATE)
        row = (
            await session.execute(select(FactorDaily).where(FactorDaily.date == TRADE_DATE))
        ).scalar_one()
        assert row.universe_mask == nifty50.mask_value

    async def test_it_asserts_the_two_representations_agree(self, session: AsyncSession) -> None:
        """docs/06: "The nightly job asserts the two representations agree"."""
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        session.add(IndexMemberDaily(index_id=1, date=TRADE_DATE, instrument_id=instrument))
        await session.flush()
        await run_compute_factors(session, StepOutcome(), TRADE_DATE)
        assert await assert_mask_matches_membership(session, TRADE_DATE) == []

    async def test_a_disagreement_is_reported(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        await run_compute_factors(session, StepOutcome(), TRADE_DATE)
        session.add(IndexMemberDaily(index_id=1, date=TRADE_DATE, instrument_id=instrument))
        await session.flush()
        assert await assert_mask_matches_membership(session, TRADE_DATE) != []

    async def test_a_dashboard_only_index_never_occupies_a_mask_bit(
        self, session: AsyncSession
    ) -> None:
        """docs/01 §7's ~145 indices exceed an integer mask's 31 bits; only the 14 are masked."""
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        masks = await universe_masks(session, TRADE_DATE)
        assert masks == {}


class TestComputeMarketHealth:
    async def test_one_row_per_market_health_universe(self, session: AsyncSession) -> None:
        """docs/01 §6 lists twelve universes on /market-health."""
        written = await run_compute_market_health(session, StepOutcome(), TRADE_DATE)
        assert written == len(MARKET_HEALTH_SLUGS) == 12
        count = (
            await session.execute(select(func.count()).select_from(MarketHealthDaily))
        ).scalar_one()
        assert int(count) == 12

    async def test_breadth_is_null_not_zero_while_factors_are_absent(
        self, session: AsyncSession
    ) -> None:
        """ "0% above the 200-day average" is a claim about the market, not about our pipeline."""
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        session.add(IndexMemberDaily(index_id=1, date=TRADE_DATE, instrument_id=instrument))
        await session.flush()
        await run_compute_factors(session, StepOutcome(), TRADE_DATE)
        await run_compute_market_health(session, StepOutcome(), TRADE_DATE)

        row = (
            await session.execute(
                select(MarketHealthDaily).where(
                    MarketHealthDaily.index_id == 1, MarketHealthDaily.date == TRADE_DATE
                )
            )
        ).scalar_one()
        assert row.constituent_count == 1
        assert row.pct_above_200dma is None

    async def test_it_records_which_universes_lack_breadth(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        session.add(IndexMemberDaily(index_id=1, date=TRADE_DATE, instrument_id=instrument))
        await session.flush()
        await run_compute_factors(session, StepOutcome(), TRADE_DATE)
        outcome = StepOutcome()
        await run_compute_market_health(session, outcome, TRADE_DATE)
        assert outcome.detail["breadth_unavailable"] == ["nifty-50"]


class TestDataVersion:
    async def test_it_starts_at_zero(self, session: AsyncSession) -> None:
        assert await current_data_version(session) == 0

    async def test_it_is_the_maximum_over_published_runs(
        self, engine: AsyncEngine, clean_db: None
    ) -> None:
        del clean_db
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            run = await open_run(session, TRADE_DATE)
            run.data_version = 41
        async with maker() as session:
            assert await current_data_version(session) == 41


class TestInstrumentUpsert:
    async def test_delisted_instruments_are_excluded_from_the_active_set(
        self, session: AsyncSession
    ) -> None:
        """docs/01 §10: delisted rows stay, for point-in-time correctness — but are not fetched."""
        live = await make_instrument(session, "LIVE", token=1)
        gone = await make_instrument(session, "GONE", token=2)
        await session.execute(
            update(Instrument).where(Instrument.id == gone).values(delisted_on=dt.date(2020, 1, 1))
        )
        ids = {row[0] for row in await active_instruments(session)}
        assert ids == {live}


class TestTheNightOnlyAsksAboutInstrumentsWorthAsking:
    """M78. `active_instruments` returned everything still listed, and that cost three hours.

    Measured on staging 1 Sep 2026: 41,443 instruments with a `kite_token`, one Kite historical
    call each at 3 req/s. 10,130 of them have ever produced a bar; **31,603 have never produced
    one, ever** — Kite's NSE dump carries every contract on the exchange, not the cash names
    Baskfy screens. Three of every four calls asked about something that never returns a row.

    That is the cause of three separate defects reported as unrelated: the night outran Celery's
    Redis `visibility_timeout` and was redelivered, so two copies wrote the same tables; it held
    the chain's transaction open for hours, blocking every user write that touches `instrument`
    behind a foreign key; and the data was never published by the time anyone looked.
    """

    async def test_an_instrument_that_has_traded_is_always_fetched(
        self, session: AsyncSession
    ) -> None:
        """The direction that must never regress: history keeps you in, forever."""
        traded = await make_instrument(session, "TRADED", token=1, listed_on=dt.date(2011, 1, 3))
        await add_bar(session, traded, TRADE_DATE, "100")
        ids = {row[0] for row in await active_instruments(session)}
        assert traded in ids

    async def test_a_seriesless_instrument_with_no_history_is_skipped(
        self, session: AsyncSession
    ) -> None:
        """The 31,603. Kite's dump carries every contract on the exchange; none of them is a stock
        Baskfy screens, and asking about them cost three of every four calls."""
        junk = await make_instrument(session, "JUNK", token=2, series=None)
        ids = {row[0] for row in await active_instruments(session)}
        assert junk not in ids

    async def test_a_cash_name_with_no_bars_yet_is_fetched(self, session: AsyncSession) -> None:
        """Otherwise the filter is a trap: no bar means never asked, means never a bar.

        This is the bootstrap case and the second version of the filter got it wrong — keying the
        exception on a recent `listed_on` excluded every instrument with an old listing date and no
        bars, which on a fresh database is all of them.
        """
        fresh = await make_instrument(session, "FRESH", token=3, series="EQ")
        ids = {row[0] for row in await active_instruments(session)}
        assert fresh in ids

    async def test_a_seriesless_instrument_that_has_traded_is_kept(
        self, session: AsyncSession
    ) -> None:
        """The 6,459. ETFs carry no series and do return bars; history is what keeps them in."""
        etf = await make_instrument(session, "ETFY", token=4, series=None)
        await add_bar(session, etf, TRADE_DATE, "100")
        ids = {row[0] for row in await active_instruments(session)}
        assert etf in ids


class TestASeriesLessInstrumentCannotBeInsertedTwice:
    """M80. The constraint said UNIQUE (exchange_id, symbol, series) and did not mean it.

    Postgres treats NULLs as distinct in a unique constraint, and 38,542 instruments arrive from
    Kite's dump with no series — so for every one of them the constraint enforced nothing, the
    upsert's `ON CONFLICT` never fired, and each nightly run inserted another copy. Measured on
    staging 2 Sep 2026: 7,937 symbols had more than one live row and **every one had a NULL
    series**; `SGBDE31III-GB` had six. The published day was inflated to match — 8,537 bars for
    3,998 real securities — so a screener was ranking a universe where 1,387 names appeared up to
    five times, and the data-quality gate read the inflation as health.

    `NULLS NOT DISTINCT` (Postgres 15+, the box runs 16) made the constraint mean what it says.

    **0039 removed `series` from the key altogether**, which subsumes this whole class of bug: a
    column that is not in the key cannot be NULL *in* the key. The property below is unchanged and
    now holds for a simpler reason — `(exchange_id, symbol)` is two NOT NULL columns, so there is
    no NULL semantics to get right. M80's own diagnosis is kept because it is the evidence for why
    `series` was the wrong thing to key on: NSE moving a stock between EQ and BE produced exactly
    the same duplication for *non*-NULL series, 120 times, and nobody noticed until a holdings
    sync could not resolve GAYAPROJ.
    """

    async def test_the_second_insert_of_a_seriesless_symbol_is_refused(
        self, session: AsyncSession
    ) -> None:
        await make_instrument(session, "NULLSER", token=8001, series=None)
        with pytest.raises(IntegrityError):
            await make_instrument(session, "NULLSER", token=8001, series=None)
            await session.flush()

    async def test_the_upsert_updates_in_place_rather_than_failing(
        self, session: AsyncSession
    ) -> None:
        """The other half: the nightly must still run, not start erroring every night instead.

        `refresh_instruments` conflicts on `(exchange_id, symbol)` since 0039. A seriesless row
        matches that target like any other — there is no NULL in it — and is updated in place,
        which is what the upsert was written to do all along.
        """
        await make_instrument(session, "UPSER", token=8002, series=None)
        await session.flush()
        rows = (
            await session.execute(
                text(
                    "insert into instrument (exchange_id, symbol, name, instrument_type, series,"
                    " kite_token, is_active) values (:e,'UPSER','Renamed','EQ',null,8002,true)"
                    " on conflict (exchange_id, symbol) do update set name = excluded.name"
                    " returning id"
                ),
                {"e": NSE_EXCHANGE_ID},
            )
        ).all()
        assert len(rows) == 1
        total = (
            await session.execute(text("select count(*) from instrument where symbol = 'UPSER'"))
        ).scalar_one()
        assert total == 1, "the upsert inserted a second row instead of updating the first"
