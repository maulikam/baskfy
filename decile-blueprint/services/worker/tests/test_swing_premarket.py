"""SW6's worker half: the morning before the open, against a real database.

`docs/swing/06` SW6, in its own words: "refresh watch levels from the latest bar; at 09:09 pull
quotes for the liquid universe in ≤ 500-symbol batches, `live_gap` → new `sw_watch` rows (setup
EP, source `DETECTOR`, catalyst empty), then rebuild the morning plan (`source=MORNING`)". The
500-a-batch and the limiter are `KiteProvider.quotes`'s and are asserted in
`packages/providers/tests/test_kite.py::TestQuotes`; what is asserted here is that the job
hands the provider exactly the liquid universe, that with the flag off it hands it nothing,
and what it does with the answer.

The premises that matter, spelled out because the numbers below are chosen for them:

* `04` §1 admits a name at ADR ≥ 3.5 % and ₹5 cr average turnover, so the liquid fixture's bars
  run 3 % either side of the close at a million shares a day, and the thin one runs 1 %.
* `04` §7.3 wants a 10 % gap on 3x an average day's pace pro-rated to the minutes elapsed. At
  09:09 that is nine minutes of 375, so a 50,000-share pre-open print against a million-share
  day is 50,000 / (1,000,000 x 9 / 375) = 2.08x, and 100,000 is 4.17x.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    AppUser,
    OhlcvDaily,
    SwConfig,
    SwMarketDaily,
    SwPlan,
    SwPlanLine,
    SwSetupDaily,
    SwWatch,
    TradingDay,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG
from baskfy_providers.records import QuoteRecord
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.tasks.swing_premarket import (
    STAGE_GAPS,
    STAGE_LEVELS,
    LiquidName,
    evaluate_gaps,
    liquid_universe,
    minutes_since_preopen,
    refresh_levels,
    run_swing_premarket,
    watch_live_gaps,
)

pytestmark = requires_db

#: The session being prepared for, and the last close before it. Both are trading days in the
#: seeded calendar; the test derives the latter rather than assuming it.
SESSION = dt.date(2026, 8, 19)
NINE_OH_NINE = dt.datetime(2026, 8, 19, 9, 9)


async def _sessions_before(session: AsyncSession, before: dt.date, count: int) -> list[dt.date]:
    rows = await session.execute(
        sa.select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date < before,
        )
        .order_by(TradingDay.date.desc())
        .limit(count)
    )
    return sorted(row[0] for row in rows)


async def _user(session: AsyncSession, *, capital: str = "1000000") -> int:
    user = AppUser(public_id="sw6-user", email="sw6@example.com")
    session.add(user)
    await session.flush()
    session.add(SwConfig(user_id=user.id, sleeve_capital_inr=Decimal(capital), updated_by="test"))
    await session.flush()
    return int(user.id)


async def _market(session: AsyncSession, *, user_id: int, on: dt.date, gate: str = "GREEN") -> None:
    session.add(
        SwMarketDaily(
            user_id=user_id,
            date=on,
            constituent_count=40,
            pct_up_strong_1m=Decimal("8.0000"),
            gate=gate,
            exposure_level=3,
            max_open_positions=8,
            max_exposure_pct=Decimal("100.00"),
            new_entries_allowed=gate != "RED",
            parabolic_count=0,
            detail={},
        )
    )
    await session.flush()


async def _bars(  # noqa: PLR0913 - one keyword per column the liquidity rule reads
    session: AsyncSession,
    instrument_id: int,
    dates: Sequence[dt.date],
    *,
    close: float = 100.0,
    range_pct: float = 3.0,
    volume: int = 1_000_000,
    adj_factor: str = "1",
) -> None:
    factor = Decimal(adj_factor)
    for on in dates:
        session.add(
            OhlcvDaily(
                instrument_id=instrument_id,
                date=on,
                open=Decimal(str(close)),
                high=Decimal(str(round(close * (1 + range_pct / 100), 4))),
                low=Decimal(str(round(close * (1 - range_pct / 100), 4))),
                close=Decimal(str(close)),
                volume=volume,
                close_raw=Decimal(str(close)) / factor,
                volume_raw=volume,
                adj_factor=factor,
                source="nse",
            )
        )
    await session.flush()


async def _setup(  # noqa: PLR0913 - one keyword per stored column a test may set
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    on: dt.date,
    trigger: str = "110.00",
    stop_ref: str = "104.00",
    adj_factor: str = "1",
) -> None:
    session.add(
        SwSetupDaily(
            user_id=user_id,
            date=on,
            instrument_id=instrument_id,
            setup="FLAG",
            status="SETTING_UP",
            score=Decimal("72.00"),
            close=Decimal("108.00"),
            trigger=Decimal(trigger),
            stop_ref=Decimal(stop_ref),
            adj_factor=Decimal(adj_factor),
            adr_pct=Decimal("5.00"),
            turnover_avg=100_000_000,
            locked_upper_circuit=False,
            listed_within_2y=False,
        )
    )
    await session.flush()


async def _watch(  # noqa: PLR0913 - one keyword per stored column a test may set
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    on: dt.date,
    source: str = "DETECTOR",
    setup: str = "FLAG",
    trigger: str | None = "110.00",
    stop_ref: str | None = "104.00",
) -> int:
    row = SwWatch(
        user_id=user_id,
        instrument_id=instrument_id,
        setup=setup,
        source=source,
        added_on=on,
        expires_on=None,
        trigger=Decimal(trigger) if trigger else None,
        stop_ref=Decimal(stop_ref) if stop_ref else None,
        setup_daily_date=on if source == "DETECTOR" else None,
        state="WATCHING",
    )
    session.add(row)
    await session.flush()
    return int(row.id)


def _quote(symbol: str, last: str, *, volume: int, prev_close: str | None = "100") -> QuoteRecord:
    return QuoteRecord(
        symbol=symbol,
        last_price=Decimal(last),
        volume=volume,
        prev_close=Decimal(prev_close) if prev_close else None,
        upper_circuit=Decimal(last) * 2,
    )


class CountingQuotes:
    """A quote source that remembers what it was asked and answers what it was given."""

    def __init__(self, answers: list[QuoteRecord] | None = None) -> None:
        self.answers = answers or []
        self.requests: list[list[str]] = []

    def quotes(self, symbols: Sequence[str]) -> list[QuoteRecord]:
        self.requests.append(list(symbols))
        return [quote for quote in self.answers if quote.symbol in set(symbols)]


# --- the pure parts ------------------------------------------------------------------


class TestTheGapRule:
    """`04` §7.3 through the scan's own plumbing: which quote, which previous close."""

    NAME = LiquidName(
        instrument_id=1,
        symbol="GAPPER",
        avg_daily_volume=Decimal(1_000_000),
        prev_close=Decimal(100),
    )

    def test_a_ten_percent_gap_on_four_times_pace_is_a_candidate(self) -> None:
        found = evaluate_gaps(
            [self.NAME],
            [_quote("GAPPER", "112", volume=100_000)],
            at=NINE_OH_NINE.time(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert [(name.symbol, verdict.gap_pct) for name, _, verdict in found] == [
            ("GAPPER", Decimal("12.00"))
        ]

    def test_the_gap_alone_is_not_enough(self) -> None:
        """Twelve percent on two times pace: the volume is not confirming the gap."""
        found = evaluate_gaps(
            [self.NAME],
            [_quote("GAPPER", "112", volume=50_000)],
            at=NINE_OH_NINE.time(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert found == []

    def test_the_volume_alone_is_not_enough(self) -> None:
        found = evaluate_gaps(
            [self.NAME],
            [_quote("GAPPER", "105", volume=100_000)],
            at=NINE_OH_NINE.time(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert found == []

    def test_the_exchange_previous_close_beats_the_bar_table(self) -> None:
        """A 2:1 bonus overnight: our bar says 100, the exchange says 50. Last 56 is a 12 % gap
        over the exchange's number and a 44 % collapse over ours — the exchange is right."""
        found = evaluate_gaps(
            [self.NAME],
            [_quote("GAPPER", "56", volume=100_000, prev_close="50")],
            at=NINE_OH_NINE.time(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert [verdict.gap_pct for _, _, verdict in found] == [Decimal("12.00")]

    def test_without_an_exchange_previous_close_the_bar_table_is_the_fallback(self) -> None:
        found = evaluate_gaps(
            [self.NAME],
            [_quote("GAPPER", "112", volume=100_000, prev_close=None)],
            at=NINE_OH_NINE.time(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert len(found) == 1

    def test_a_name_with_no_quote_is_not_a_candidate(self) -> None:
        found = evaluate_gaps([self.NAME], [], at=NINE_OH_NINE.time(), config=DEFAULT_SWING_CONFIG)
        assert found == []

    def test_minutes_are_counted_from_the_preopen_and_never_zero(self) -> None:
        """SW6.1: the pre-open's volume has been accumulating since 09:00."""
        assert minutes_since_preopen(dt.time(9, 9)) == 9
        assert minutes_since_preopen(dt.time(9, 0)) == 1
        assert minutes_since_preopen(dt.time(8, 50)) == 1


# --- the levels ----------------------------------------------------------------------


@pytest.mark.db
class TestTheLevelsRefresh:
    """`03` §9: "recomputes from the latest bar rather than trusting last night's number"."""

    async def test_a_split_since_detection_rescales_the_level(self, session: AsyncSession) -> None:
        """Detected at factor 1.0 with a trigger of 110; a 1:2 split lands and the latest bar
        carries factor 0.5. The same adjusted level is now an exchange price of 220."""
        user_id = await _user(session)
        last = (await _sessions_before(session, SESSION, 1))[0]
        instrument = await make_instrument(session, "SPLITCO")
        await _bars(session, instrument, [last], adj_factor="0.5")
        await _setup(session, user_id=user_id, instrument_id=instrument, on=last, adj_factor="1")
        watch_id = await _watch(session, user_id=user_id, instrument_id=instrument, on=last)

        refreshed, unchanged = await refresh_levels(session, user_id=user_id, on=last)

        assert (refreshed, unchanged) == (1, 0)
        row = await session.get(SwWatch, watch_id)
        assert row is not None
        assert (row.trigger, row.stop_ref) == (Decimal("220.00"), Decimal("208.00"))

    async def test_an_unchanged_factor_leaves_the_level_alone(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        last = (await _sessions_before(session, SESSION, 1))[0]
        instrument = await make_instrument(session, "STEADYCO")
        await _bars(session, instrument, [last])
        await _setup(session, user_id=user_id, instrument_id=instrument, on=last)
        watch_id = await _watch(session, user_id=user_id, instrument_id=instrument, on=last)

        assert await refresh_levels(session, user_id=user_id, on=last) == (0, 1)
        row = await session.get(SwWatch, watch_id)
        assert row is not None
        assert row.trigger == Decimal("110.00")

    async def test_a_manual_row_keeps_what_the_person_typed(self, session: AsyncSession) -> None:
        """`03` §4: "a MANUAL row keeps what Maulik typed" — even through a split."""
        user_id = await _user(session)
        last = (await _sessions_before(session, SESSION, 1))[0]
        instrument = await make_instrument(session, "MANUALCO")
        await _bars(session, instrument, [last], adj_factor="0.5")
        await _setup(session, user_id=user_id, instrument_id=instrument, on=last, adj_factor="1")
        watch_id = await _watch(
            session, user_id=user_id, instrument_id=instrument, on=last, source="MANUAL"
        )

        assert await refresh_levels(session, user_id=user_id, on=last) == (0, 0)
        row = await session.get(SwWatch, watch_id)
        assert row is not None
        assert row.trigger == Decimal("110.00")

    async def test_the_levels_stage_refreshes_and_does_not_plan(
        self, session: AsyncSession
    ) -> None:
        """08:50 is the levels and nothing else — no quote, no plan."""
        user_id = await _user(session)
        last = (await _sessions_before(session, SESSION, 1))[0]
        await _market(session, user_id=user_id, on=last)
        quotes = CountingQuotes()

        report = await run_swing_premarket(
            session,
            StepOutcome(),
            SESSION,
            user_id=user_id,
            stage=STAGE_LEVELS,
            ep_premarket_enabled=True,
            quotes=quotes,
            now=dt.datetime(2026, 8, 19, 8, 50),
        )

        assert report.stage == STAGE_LEVELS
        assert report.plan_id is None
        assert quotes.requests == []
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(SwPlan))
        ).scalar_one() == 0


# --- the gaps ------------------------------------------------------------------------


@pytest.mark.db
class TestTheGapScan:
    async def _universe(self, session: AsyncSession) -> tuple[int, dt.date, int, int]:
        """A liquid name and a thin one, each with sixty sessions of bars to the last close."""
        user_id = await _user(session)
        days = await _sessions_before(session, SESSION, 60)
        liquid = await make_instrument(session, "LIQUIDCO")
        thin = await make_instrument(session, "THINCO")
        await _bars(session, liquid, days, range_pct=3.0, volume=1_000_000)
        await _bars(session, thin, days, range_pct=1.0, volume=1_000)
        await _market(session, user_id=user_id, on=days[-1])
        return user_id, days[-1], liquid, thin

    async def test_the_liquid_universe_is_the_detectors_own(self, session: AsyncSession) -> None:
        _, last, liquid, _ = await self._universe(session)
        names = await liquid_universe(session, as_of=last, config=DEFAULT_SWING_CONFIG)
        assert [(n.instrument_id, n.symbol) for n in names] == [(liquid, "LIQUIDCO")]
        assert names[0].avg_daily_volume == Decimal(1_000_000)
        assert names[0].prev_close == Decimal("100.00")

    async def test_with_the_flag_off_no_quote_is_pulled(self, session: AsyncSession) -> None:
        """`docs/swing/02` Track B: dark until the flag says otherwise — and the plan is still
        built, because the plan costs no Kite call."""
        user_id, _, _, _ = await self._universe(session)
        quotes = CountingQuotes([_quote("LIQUIDCO", "112", volume=100_000)])

        report = await run_swing_premarket(
            session,
            StepOutcome(),
            SESSION,
            user_id=user_id,
            stage=STAGE_GAPS,
            ep_premarket_enabled=False,
            quotes=quotes,
            now=NINE_OH_NINE,
        )

        assert quotes.requests == []
        assert report.gaps_added == 0
        assert report.plan_id is not None

    async def test_with_the_flag_on_exactly_the_liquid_universe_is_quoted(
        self, session: AsyncSession
    ) -> None:
        user_id, _, _, _ = await self._universe(session)
        quotes = CountingQuotes([_quote("LIQUIDCO", "112", volume=100_000)])

        report = await run_swing_premarket(
            session,
            StepOutcome(),
            SESSION,
            user_id=user_id,
            stage=STAGE_GAPS,
            ep_premarket_enabled=True,
            quotes=quotes,
            now=NINE_OH_NINE,
        )

        assert quotes.requests == [["LIQUIDCO"]], "the thin name costs nobody a quote"
        assert report.universe == 1
        assert report.quotes_pulled == 1
        assert report.gap_candidates == ["LIQUIDCO"]
        assert report.gaps_added == 1

    async def test_a_live_gap_becomes_an_ep_watch_row_with_no_stop_yet(
        self, session: AsyncSession
    ) -> None:
        """`06` SW6: "setup EP, source DETECTOR, catalyst empty". SW6.2: the trigger is the
        indicative price and the stop waits for the opening range."""
        user_id, last, liquid, _ = await self._universe(session)
        names = await liquid_universe(session, as_of=last, config=DEFAULT_SWING_CONFIG)

        found, added, already = await watch_live_gaps(
            session,
            user_id=user_id,
            on=SESSION,
            names=names,
            quotes=[_quote("LIQUIDCO", "112.50", volume=100_000)],
            at=NINE_OH_NINE.time(),
            config=DEFAULT_SWING_CONFIG,
        )

        assert (found, added, already) == (["LIQUIDCO"], 1, 0)
        row = (await session.execute(sa.select(SwWatch))).scalar_one()
        assert row.instrument_id == liquid
        assert (row.setup, row.source, row.catalyst, row.state) == (
            "EP",
            "DETECTOR",
            None,
            "WATCHING",
        )
        assert row.trigger == Decimal("112.50")
        assert row.stop_ref is None
        assert row.added_on == SESSION
        # `04` §3: an EP is watched for `valid_bars` [3] sessions.
        ahead = await session.execute(
            sa.select(TradingDay.date)
            .where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID,
                TradingDay.is_trading_day.is_(True),
                TradingDay.date > SESSION,
            )
            .order_by(TradingDay.date)
            .limit(3)
        )
        assert row.expires_on == [d for (d,) in ahead][-1]

    async def test_a_name_already_watched_is_not_watched_twice(self, session: AsyncSession) -> None:
        user_id, last, liquid, _ = await self._universe(session)
        await _watch(session, user_id=user_id, instrument_id=liquid, on=last)
        names = await liquid_universe(session, as_of=last, config=DEFAULT_SWING_CONFIG)

        found, added, already = await watch_live_gaps(
            session,
            user_id=user_id,
            on=SESSION,
            names=names,
            quotes=[_quote("LIQUIDCO", "112.50", volume=100_000)],
            at=NINE_OH_NINE.time(),
            config=DEFAULT_SWING_CONFIG,
        )

        assert (found, added, already) == (["LIQUIDCO"], 0, 1)
        count = (
            await session.execute(sa.select(sa.func.count()).select_from(SwWatch))
        ).scalar_one()
        assert count == 1

    async def test_the_scan_is_idempotent(self, session: AsyncSession) -> None:
        """House rule 7. A second 09:09 (a Beat retry) adds nothing."""
        user_id, _, _, _ = await self._universe(session)
        quotes = CountingQuotes([_quote("LIQUIDCO", "112", volume=100_000)])
        for _ in range(2):
            await run_swing_premarket(
                session,
                StepOutcome(),
                SESSION,
                user_id=user_id,
                ep_premarket_enabled=True,
                quotes=quotes,
                now=NINE_OH_NINE,
            )
        count = (
            await session.execute(sa.select(sa.func.count()).select_from(SwWatch))
        ).scalar_one()
        assert count == 1


# --- the morning plan ----------------------------------------------------------------


@pytest.mark.db
class TestTheMorningPlan:
    async def test_it_is_rebuilt_from_the_same_watchlist_as_source_morning(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        last = (await _sessions_before(session, SESSION, 1))[0]
        await _market(session, user_id=user_id, on=last)
        flag = await make_instrument(session, "GOODFLAG")
        await _bars(session, flag, [last])
        await _setup(session, user_id=user_id, instrument_id=flag, on=last)
        await _watch(session, user_id=user_id, instrument_id=flag, on=last)

        outcome = StepOutcome()
        report = await run_swing_premarket(
            session, outcome, SESSION, user_id=user_id, now=NINE_OH_NINE
        )

        assert report.entry_lines == 1
        plan = (await session.execute(sa.select(SwPlan))).scalar_one()
        assert plan.source == "MORNING"
        assert plan.as_of == SESSION
        assert plan.gate == "GREEN"
        assert (plan.expires_at - plan.built_at) == dt.timedelta(minutes=30)
        line = (await session.execute(sa.select(SwPlanLine))).scalar_one()
        assert (line.kind, line.trigger, line.stop) == (
            "BUY_ON_TRIGGER",
            Decimal("110.00"),
            Decimal("104.00"),
        )
        assert outcome.status is not StepStatus.SKIPPED

    async def test_a_live_ep_without_a_stop_is_not_a_line(self, session: AsyncSession) -> None:
        """SW6.2: the plan cannot size a name whose stop the opening range has not set yet. The
        monitor's SIGNAL plan carries it once the range breaks."""
        user_id = await _user(session)
        last = (await _sessions_before(session, SESSION, 1))[0]
        await _market(session, user_id=user_id, on=last)
        gapper = await make_instrument(session, "GAPPER")
        await _watch(
            session,
            user_id=user_id,
            instrument_id=gapper,
            on=SESSION,
            setup="EP",
            trigger="112.50",
            stop_ref=None,
        )

        report = await run_swing_premarket(
            session, StepOutcome(), SESSION, user_id=user_id, now=NINE_OH_NINE
        )

        assert report.plan_id is not None
        assert report.entry_lines == 0

    async def test_without_a_market_row_the_morning_is_skipped_and_says_why(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        outcome = StepOutcome()
        report = await run_swing_premarket(
            session, outcome, SESSION, user_id=user_id, now=NINE_OH_NINE
        )
        assert outcome.status is StepStatus.SKIPPED
        assert report.plan_id is None
        assert report.skipped_reason is not None
        assert "sw_market_daily" in report.skipped_reason
