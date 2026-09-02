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
from typing import cast

import pytest
import sqlalchemy as sa
from celery.schedules import crontab
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    AppUser,
    OhlcvDaily,
    SwConfig,
    SwMarketDaily,
    SwPlan,
    SwPlanLine,
    SwPlanSkip,
    SwPosition,
    SwSetupDaily,
    SwWatch,
    TradingDay,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG
from baskfy_providers.records import QuoteRecord
from baskfy_worker.celery_app import BEAT_SCHEDULE
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.tasks.swing_eod import run_swing_eod
from baskfy_worker.tasks.swing_premarket import (
    STAGE_GAPS,
    STAGE_LEVELS,
    LiquidName,
    catch_up_ladder,
    evaluate_gaps,
    gap_price,
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
            # 5.60 since SW9.5: the widest stop is one ADR (`04` §6), and a 104 stop under a 110
            # trigger is 5.45% — inside 5.60, outside the 5.00 the fixture used to carry; under
            # the 6% fast-trail line, so the line still trails the 20-day.
            adr_pct=Decimal("5.60"),
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

    def test_the_gap_is_measured_from_ohlc_open_when_the_quote_carries_it(self) -> None:
        """A4 (SW11): after the open, `ohlc.open` is the auction's equilibrium price — the gap.
        The last print may already have faded; it is not the gap. Here the open is 12% up and
        the last print 4% up: a candidate, at the open's 12."""
        quote = QuoteRecord(
            symbol="GAPPER",
            last_price=Decimal("104"),
            volume=200_000,
            prev_close=Decimal(100),
            open=Decimal("112"),
        )
        found = evaluate_gaps([self.NAME], [quote], at=dt.time(9, 16), config=DEFAULT_SWING_CONFIG)
        assert [(n.symbol, v.gap_pct) for n, _, v in found] == [("GAPPER", Decimal("12.00"))]
        assert gap_price(quote) == Decimal("112")

    def test_without_an_ohlc_open_the_last_print_is_the_gap(self) -> None:
        """A quote with no open (the pre-open, before 09:07) or a zero one reads the last
        price, as SW6 did."""
        assert gap_price(_quote("GAPPER", "112", volume=1)) == Decimal("112")
        zero = QuoteRecord(symbol="GAPPER", last_price=Decimal("111"), volume=1, open=Decimal(0))
        assert gap_price(zero) == Decimal("111")

    def test_the_gap_scan_beat_entry_is_nine_sixteen_ist(self) -> None:
        """MD5: the scan moved from 09:09 to 09:16, defensively, until the S2 probe answers
        whether the 09:09 reading was ever usable; the volume at 09:16 is sixteen minutes of
        375 against the pre-open match plus one minute of trading."""
        entry = BEAT_SCHEDULE["swing-premarket-gaps"]
        schedule = cast(crontab, entry["schedule"])
        assert (schedule.hour, schedule.minute) == ({9}, {16})
        assert entry["kwargs"] == {"stage": "GAPS"}
        assert minutes_since_preopen(dt.time(9, 16)) == 16

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
        # SW10.5 (A14, A7): ranked by the provisional EP score the pre-open knows — a 12.5%
        # gap is 35 x 12.5/20 = 21.875 and a 4.17x pace is 35 x 4.17/6 = 24.325, 46.20 of 70 —
        # and sized against the ADR the bars measured (3% either side of the close: 1.03/0.97
        # is 6.19%).
        assert row.score == Decimal("46.20")
        assert row.adr_pct == Decimal("6.19")
        assert row.focus is False, "focus is refreshed by the job after the scan, not here"
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

    async def test_a_live_ep_without_a_stop_is_a_pending_range_line_not_a_buy(
        self, session: AsyncSession
    ) -> None:
        """A7 (SW10.5; SW6.2 amended): the plan cannot size a name whose stop the opening range
        has not set, so it shows it as a `PENDING_RANGE` line — no quantity, no stop, a preview
        at a 1-ADR stop — and counts it as a reserved slot, not as an entry. The monitor's SIGNAL
        plan carries it once the range breaks."""
        user_id = await _user(session)
        last = (await _sessions_before(session, SESSION, 1))[0]
        await _market(session, user_id=user_id, on=last)
        gapper = await make_instrument(session, "GAPPER")
        watch_id = await _watch(
            session,
            user_id=user_id,
            instrument_id=gapper,
            on=SESSION,
            setup="EP",
            trigger="112.50",
            stop_ref=None,
        )
        row = (await session.execute(sa.select(SwWatch).where(SwWatch.id == watch_id))).scalar_one()
        row.adr_pct = Decimal("6.18")
        row.score = Decimal("46.20")
        await session.flush()

        report = await run_swing_premarket(
            session, StepOutcome(), SESSION, user_id=user_id, now=NINE_OH_NINE
        )

        assert report.plan_id is not None
        assert (report.entry_lines, report.pending_lines) == (0, 1)
        line = (await session.execute(sa.select(SwPlanLine))).scalar_one()
        assert line.kind == "PENDING_RANGE"
        assert (line.quantity, line.stop, line.trigger) == (0, None, Decimal("112.50"))
        assert line.risk_inr == 0 and line.position_value == 0
        assert line.state == "PROPOSED"
        assert line.note is not None and "slot reserved" in line.note
        assert "1 ADR (6.18%)" in line.note
        assert line.client_id.endswith(":GAPPER:PENDING_RANGE")

    async def test_a_pending_range_line_reserves_a_slot_ahead_of_lower_scored_flags(
        self, session: AsyncSession
    ) -> None:
        """A7: the session allows three new entries; a live gap scoring above three flags takes
        one slot, two flags get the other two, and the third flag is `SESSION_CAP`."""
        user_id = await _user(session)
        last = (await _sessions_before(session, SESSION, 1))[0]
        await _market(session, user_id=user_id, on=last)
        for index in range(3):
            flag = await make_instrument(session, f"FLAG{index}")
            await _bars(session, flag, [last])
            await _setup(session, user_id=user_id, instrument_id=flag, on=last)
            await _watch(session, user_id=user_id, instrument_id=flag, on=last)
        gapper = await make_instrument(session, "GAPPER")
        watch_id = await _watch(
            session,
            user_id=user_id,
            instrument_id=gapper,
            on=SESSION,
            setup="EP",
            trigger="112.50",
            stop_ref=None,
        )
        row = (await session.execute(sa.select(SwWatch).where(SwWatch.id == watch_id))).scalar_one()
        row.adr_pct, row.score = Decimal("6.18"), Decimal("80.00")
        await session.flush()

        report = await run_swing_premarket(
            session, StepOutcome(), SESSION, user_id=user_id, now=NINE_OH_NINE
        )

        assert (report.entry_lines, report.pending_lines, report.skips) == (2, 1, 1)
        kinds = (
            (await session.execute(sa.select(SwPlanLine.kind).order_by(SwPlanLine.id)))
            .scalars()
            .all()
        )
        assert list(kinds) == ["PENDING_RANGE", "BUY_ON_TRIGGER", "BUY_ON_TRIGGER"]
        skip = (await session.execute(sa.select(SwPlanSkip))).scalar_one()
        assert skip.reason == "SESSION_CAP"

    async def test_the_morning_plan_carries_the_first_live_risk_multiplier_when_live(
        self, session: AsyncSession
    ) -> None:
        """A9: with the countdown running and execution enabled the morning plan is sized at
        half risk (1,666 → 833 on a ₹6 stop); with execution disabled the same countdown plans
        full size — a paper plan rehearses the rules at the size the rules describe."""
        user_id = await _user(session)
        last = (await _sessions_before(session, SESSION, 1))[0]
        await _market(session, user_id=user_id, on=last)
        flag = await make_instrument(session, "GOODFLAG")
        await _bars(session, flag, [last])
        await _setup(session, user_id=user_id, instrument_id=flag, on=last)
        await _watch(session, user_id=user_id, instrument_id=flag, on=last)
        config = (await session.execute(sa.select(SwConfig))).scalar_one()
        assert config.first_live_sessions_left == 5

        paper = await run_swing_premarket(
            session,
            StepOutcome(),
            SESSION,
            user_id=user_id,
            now=NINE_OH_NINE,
            execution_enabled=False,
        )
        live = await run_swing_premarket(
            session,
            StepOutcome(),
            SESSION,
            user_id=user_id,
            now=NINE_OH_NINE,
            execution_enabled=True,
        )

        lines = (
            (await session.execute(sa.select(SwPlanLine).order_by(SwPlanLine.id))).scalars().all()
        )
        assert [line.quantity for line in lines] == [833, 416]
        assert (paper.risk_multiplier, paper.risk_pct_in_force) == ("1", "0.500")
        assert (live.risk_multiplier, live.risk_pct_in_force) == ("0.5", "0.250")
        assert live.first_live_sessions_left == 5

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


# --- A10: the 09:09 catch-up settlement --------------------------------------------------


@pytest.mark.db
class TestTheCatchUpSettlement:
    async def _evening_missed(self, session: AsyncSession) -> tuple[int, dt.date]:
        """A market row the detection job wrote for the last close, with no settlement record —
        the evening never ran — and five real closes the ladder should have read."""
        user_id = await _user(session)
        last = (await _sessions_before(session, SESSION, 1))[0]
        await _market(session, user_id=user_id, on=last)
        for index, r in enumerate(("2.00", "-1.00", "1.50", "-0.50", "2.50")):
            instrument_id = await make_instrument(session, f"DONE{index}")
            session.add(
                SwPosition(
                    user_id=user_id,
                    instrument_id=instrument_id,
                    setup="FLAG",
                    entry_date=last - dt.timedelta(days=10),
                    entry_avg=Decimal("100.0000"),
                    quantity_entered=100,
                    initial_stop=Decimal("96.00"),
                    stop=Decimal("96.00"),
                    trail="MA20",
                    quantity_open=0,
                    state="CLOSED",
                    closed_on=last - dt.timedelta(days=5 - index),
                    exit_avg=Decimal(100) + Decimal(r) * 4,
                    close_reason="CLOSE_BELOW_TRAIL_MA",
                    r_multiple=Decimal(r),
                    pnl_inr=Decimal(r) * 400,
                    simulated=False,
                )
            )
        await session.flush()
        return user_id, last

    async def test_the_morning_settles_the_previous_session_only_as_a_catch_up(
        self, session: AsyncSession
    ) -> None:
        """A10: no settlement record for the last close → the 09:09 job settles it (five good
        real closes in GREEN move the rung to 1), records it as the evening would, and the
        morning plan is built on the settled rung, not the detection job's preview of 3."""
        user_id, last = await self._evening_missed(session)

        report = await run_swing_premarket(
            session, StepOutcome(), SESSION, user_id=user_id, now=NINE_OH_NINE
        )

        assert report.ladder_caught_up is True
        market = (
            await session.execute(
                sa.select(SwMarketDaily).where(
                    SwMarketDaily.user_id == user_id, SwMarketDaily.date == last
                )
            )
        ).scalar_one()
        assert isinstance(market.detail, dict)
        assert market.detail["ladder"] == {"from": 0, "to": 1, "settled_by": "swing-eod"}
        assert market.detail["closed_trades_read"] == "real"
        assert market.exposure_level == 1
        config = (await session.execute(sa.select(SwConfig))).scalar_one()
        assert config.exposure_level == 1
        plan = (await session.execute(sa.select(SwPlan))).scalar_one()
        assert plan.exposure_level == 1

    async def test_a_session_the_evening_settled_is_never_a_second_settlement(
        self, session: AsyncSession
    ) -> None:
        """A10: one settlement per date. The evening ran (rung 0 → 1 on the record); the 09:09
        job reads the record and leaves it — a second settlement would climb 1 → 2 on the same
        five closes."""
        user_id, last = await self._evening_missed(session)
        evening = await run_swing_eod(session, StepOutcome(), last, user_id=user_id)
        assert (evening.rung_before, evening.exposure_level) == (0, 1)

        report = await run_swing_premarket(
            session, StepOutcome(), SESSION, user_id=user_id, now=NINE_OH_NINE
        )

        assert report.ladder_caught_up is False
        market = (
            await session.execute(
                sa.select(SwMarketDaily).where(
                    SwMarketDaily.user_id == user_id, SwMarketDaily.date == last
                )
            )
        ).scalar_one()
        assert isinstance(market.detail, dict)
        assert market.detail["ladder"] == {"from": 0, "to": 1, "settled_by": "swing-eod"}
        config = (await session.execute(sa.select(SwConfig))).scalar_one()
        assert config.exposure_level == 1

    async def test_the_catch_up_itself_runs_once(self, session: AsyncSession) -> None:
        """Two 09:09 runs (a Beat retry) settle once: the first leaves the record the second
        reads."""
        user_id, last = await self._evening_missed(session)
        config = DEFAULT_SWING_CONFIG
        now = dt.datetime(2026, 8, 19, 9, 9, tzinfo=dt.UTC)
        first = await catch_up_ladder(
            session,
            user_id=user_id,
            last_session=last,
            config=config,
            execution_enabled=False,
            now=now,
        )
        second = await catch_up_ladder(
            session,
            user_id=user_id,
            last_session=last,
            config=config,
            execution_enabled=False,
            now=now,
        )
        assert (first, second) == (True, False)
        row = (await session.execute(sa.select(SwConfig))).scalar_one()
        assert row.exposure_level == 1

    async def test_the_catch_up_reads_real_closes_only(self, session: AsyncSession) -> None:
        """A10: five paper wins on a missed evening move nothing at 09:09 either."""
        user_id = await _user(session)
        last = (await _sessions_before(session, SESSION, 1))[0]
        await _market(session, user_id=user_id, on=last)
        for index, r in enumerate(("2.00", "1.00", "1.50", "0.50", "2.50")):
            instrument_id = await make_instrument(session, f"PAPER{index}")
            session.add(
                SwPosition(
                    user_id=user_id,
                    instrument_id=instrument_id,
                    setup="FLAG",
                    entry_date=last - dt.timedelta(days=10),
                    entry_avg=Decimal("100.0000"),
                    quantity_entered=100,
                    initial_stop=Decimal("96.00"),
                    stop=Decimal("96.00"),
                    trail="MA20",
                    quantity_open=0,
                    state="CLOSED",
                    closed_on=last - dt.timedelta(days=5 - index),
                    exit_avg=Decimal(100) + Decimal(r) * 4,
                    close_reason="CLOSE_BELOW_TRAIL_MA",
                    r_multiple=Decimal(r),
                    pnl_inr=Decimal(r) * 400,
                    simulated=True,
                )
            )
        await session.flush()

        report = await run_swing_premarket(
            session, StepOutcome(), SESSION, user_id=user_id, now=NINE_OH_NINE
        )

        assert report.ladder_caught_up is True
        config = (await session.execute(sa.select(SwConfig))).scalar_one()
        assert config.exposure_level == 0
