"""SW5's acceptance: the evening job, against a real database.

The module plan's own criteria, in its own words:

* "a fixture book with one position on day 3 and green produces **exactly** a `SELL_AT_OPEN` of ⅓
  and a `RAISE_GTT_STOP` to entry";
* "the preview lists **every skip with its reason**";
* "the alert renders ... with the naked-GTT section present (empty) and the session counter".

Plus the ones those imply and a reader would want proven: that the watchlist fills itself and
prunes itself, that a position with no bar today is not managed against a stale one, and that the
session counter — the number `docs/swing/02` §3.2 gates the real-money flag on — counts sessions
rather than events.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.email.templates import SwingCandidate, SwingDigest, swing_eod
from baskfy_api.swing_watch import WATCHING, add_manual, auto_watch, list_watch, reconfirm
from baskfy_core.models import (
    AppUser,
    OhlcvDaily,
    SwConfig,
    SwConfigAudit,
    SwMarketDaily,
    SwPlan,
    SwPlanLine,
    SwPlanSkip,
    SwPosition,
    SwSession,
    SwSetupDaily,
    SwWatch,
    TradingDay,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.swing.plan import LineKind
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.tasks.swing_eod import (
    count_first_live_session,
    manage_open_positions,
    run_swing_eod,
)

pytestmark = requires_db

AS_OF = dt.date(2026, 8, 18)


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


async def _user(session: AsyncSession, *, capital: str = "1000000") -> int:
    user = AppUser(public_id="sw5-user", email="sw5@example.com")
    session.add(user)
    await session.flush()
    session.add(SwConfig(user_id=user.id, sleeve_capital_inr=Decimal(capital), updated_by="test"))
    await session.flush()
    return int(user.id)


async def _market(
    session: AsyncSession, *, user_id: int, gate: str = "GREEN", on: dt.date = AS_OF
) -> None:
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


async def _bars(
    session: AsyncSession, instrument_id: int, dates: list[dt.date], closes: list[float]
) -> None:
    for on, close in zip(dates, closes, strict=True):
        session.add(
            OhlcvDaily(
                instrument_id=instrument_id,
                date=on,
                open=Decimal(str(close)),
                high=Decimal(str(round(close * 1.01, 4))),
                low=Decimal(str(round(close * 0.99, 4))),
                close=Decimal(str(close)),
                volume=1_000_000,
                close_raw=Decimal(str(close)),
                volume_raw=1_000_000,
                adj_factor=Decimal(1),
                source="nse",
            )
        )
    await session.flush()


async def _position(  # noqa: PLR0913 - one keyword per column a test may set
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    entry_date: dt.date,
    entry: str = "100",
    initial_stop: str = "96",
    stop: str = "96",
    quantity: int = 300,
    gtt_id: str | None = "GTT-1",
    setup: str = "FLAG",
) -> int:
    row = SwPosition(
        user_id=user_id,
        instrument_id=instrument_id,
        setup=setup,
        entry_date=entry_date,
        entry_avg=Decimal(entry),
        quantity_entered=quantity,
        initial_stop=Decimal(initial_stop),
        stop=Decimal(stop),
        gtt_id=gtt_id,
        trail="MA20",
        partial_done=False,
        quantity_open=quantity,
        state="OPEN",
        simulated=True,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


@pytest.mark.db
class TestTheDayThreePartial:
    """The module plan's own fixture: one position, day 3, green."""

    async def test_it_produces_exactly_a_third_sold_and_a_stop_at_breakeven(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        dates = await _sessions_before(session, AS_OF, 40)
        instrument_id = await make_instrument(session, "FLAGCO")
        # Flat at 100 until the entry, then up to 110 — green on day 3, above both averages, and
        # never near the stop, so rules 1-3 of §6.4 stay quiet and rule 4 is the one that fires.
        closes = [100.0] * (len(dates) - 4) + [100.0, 104.0, 107.0, 110.0]
        await _bars(session, instrument_id, dates, closes)
        await _position(session, user_id=user_id, instrument_id=instrument_id, entry_date=dates[-4])

        lines, naked, managed = await manage_open_positions(session, user_id=user_id, on=AS_OF)

        assert managed == 1
        assert naked == []
        assert [(line.kind, line.quantity, line.stop, line.note) for line in lines] == [
            (LineKind.SELL_AT_OPEN, 100, None, "PARTIAL_INTO_STRENGTH"),
            (LineKind.RAISE_GTT_STOP, 0, Decimal("100.00"), "BREAKEVEN_AFTER_PARTIAL"),
        ]

    async def test_day_two_produces_no_partial(self, session: AsyncSession) -> None:
        """The partial window opens on day 3 (`04` §6.4.4). Day 2 does not sell.

        The close is only mildly green — 102 against an entry of 100 and one R of 4 — so the
        breakeven rule (§6.4.5, one full R) does not fire either and the position is left alone.
        A larger gain on day 2 *would* move the stop to breakeven, which is correct and is a
        different rule.
        """
        user_id = await _user(session)
        dates = await _sessions_before(session, AS_OF, 40)
        instrument_id = await make_instrument(session, "FLAGCO")
        closes = [100.0] * (len(dates) - 3) + [100.0, 101.0, 102.0]
        await _bars(session, instrument_id, dates, closes)
        await _position(session, user_id=user_id, instrument_id=instrument_id, entry_date=dates[-3])

        lines, _, managed = await manage_open_positions(session, user_id=user_id, on=AS_OF)

        assert managed == 1
        assert lines == []

    async def test_one_full_r_on_day_two_moves_the_stop_to_breakeven(
        self, session: AsyncSession
    ) -> None:
        """§6.4.5 is independent of the partial window: entry 100, initial stop 96, so a close
        at 104 is one R and the stop moves — on day 2 as on day 8."""
        user_id = await _user(session)
        dates = await _sessions_before(session, AS_OF, 40)
        instrument_id = await make_instrument(session, "FLAGCO")
        closes = [100.0] * (len(dates) - 3) + [100.0, 102.0, 104.0]
        await _bars(session, instrument_id, dates, closes)
        await _position(session, user_id=user_id, instrument_id=instrument_id, entry_date=dates[-3])

        lines, _, _ = await manage_open_positions(session, user_id=user_id, on=AS_OF)

        assert [(line.kind, line.stop, line.note) for line in lines] == [
            (LineKind.RAISE_GTT_STOP, Decimal("100.00"), "BREAKEVEN_AT_R")
        ]

    async def test_a_position_with_no_bar_today_is_not_managed(self, session: AsyncSession) -> None:
        """A suspended name has not given the rules a close. Managing it against a stale bar
        would sell it on yesterday's information."""
        user_id = await _user(session)
        dates = await _sessions_before(session, AS_OF, 40)
        instrument_id = await make_instrument(session, "HALTED")
        await _bars(session, instrument_id, dates[:-1], [100.0] * (len(dates) - 1))
        await _position(session, user_id=user_id, instrument_id=instrument_id, entry_date=dates[-5])

        lines, _, managed = await manage_open_positions(session, user_id=user_id, on=AS_OF)

        assert managed == 0
        assert lines == []

    async def test_a_position_without_a_resting_stop_is_reported_every_evening(
        self, session: AsyncSession
    ) -> None:
        """`03` §7: `gtt_id` null with an open quantity is the one state the method forbids."""
        user_id = await _user(session)
        dates = await _sessions_before(session, AS_OF, 40)
        instrument_id = await make_instrument(session, "NAKEDCO")
        await _bars(session, instrument_id, dates, [100.0] * len(dates))
        await _position(
            session,
            user_id=user_id,
            instrument_id=instrument_id,
            entry_date=dates[-5],
            gtt_id=None,
        )

        _, naked, _ = await manage_open_positions(session, user_id=user_id, on=AS_OF)

        assert naked == ["NAKEDCO"]


@pytest.mark.db
class TestTheWatchlistFillsAndPrunesItself:
    async def test_a_high_scoring_flag_and_every_ep_are_watched(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        good = await make_instrument(session, "GOODFLAG")
        weak = await make_instrument(session, "WEAKFLAG")
        ep = await make_instrument(session, "EPCO")
        await _setup(session, user_id=user_id, instrument_id=good, score="72.00")
        await _setup(session, user_id=user_id, instrument_id=weak, score="41.00")
        await _setup(
            session,
            user_id=user_id,
            instrument_id=ep,
            setup="EP",
            status="GAP_DAY",
            score="12.00",
        )

        result = await auto_watch(session, user_id=user_id, on=AS_OF)

        watched = {row.symbol for row in await list_watch(session, user_id=user_id)}
        assert watched == {"GOODFLAG", "EPCO"}
        assert result.added == 2

    async def test_a_breakout_today_is_not_watched(self, session: AsyncSession) -> None:
        """`04` §9.5 watches `SETTING_UP`. A name that already broke out today is not a level to
        wait for — it is a decision for this morning, and the morning has gone."""
        user_id = await _user(session)
        instrument_id = await make_instrument(session, "GONECO")
        await _setup(
            session,
            user_id=user_id,
            instrument_id=instrument_id,
            status="BREAKOUT_TODAY",
            score="88.00",
        )

        await auto_watch(session, user_id=user_id, on=AS_OF)

        assert await list_watch(session, user_id=user_id) == ()

    async def test_a_parabolic_name_is_never_watched(self, session: AsyncSession) -> None:
        """PACK.1: a watchlist is a list of things to buy, and this one can never be bought."""
        user_id = await _user(session)
        instrument_id = await make_instrument(session, "PARACO")
        await _setup(
            session,
            user_id=user_id,
            instrument_id=instrument_id,
            setup="PARABOLIC_SHORT",
            status="RUNNING",
            score="95.00",
        )

        await auto_watch(session, user_id=user_id, on=AS_OF)

        assert await list_watch(session, user_id=user_id) == ()

    async def test_running_it_twice_adds_nothing(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        instrument_id = await make_instrument(session, "GOODFLAG")
        await _setup(session, user_id=user_id, instrument_id=instrument_id, score="72.00")

        first = await auto_watch(session, user_id=user_id, on=AS_OF)
        second = await auto_watch(session, user_id=user_id, on=AS_OF)

        assert (first.added, second.added) == (1, 0)
        assert second.already_watching == 1
        assert len(await list_watch(session, user_id=user_id)) == 1

    async def test_a_row_past_its_expiry_leaves_the_list_without_being_deleted(
        self, session: AsyncSession
    ) -> None:
        """`04` §9.5: expiry is a state change. The record of what was watched is the record of
        what was passed over."""
        user_id = await _user(session)
        instrument_id = await make_instrument(session, "OLDFLAG")
        long_ago = AS_OF - dt.timedelta(days=60)
        await _setup(
            session, user_id=user_id, instrument_id=instrument_id, score="72.00", on=long_ago
        )
        assert (await auto_watch(session, user_id=user_id, on=long_ago)).added == 1
        await _setup(session, user_id=user_id, instrument_id=instrument_id, score="72.00")

        result = await auto_watch(session, user_id=user_id, on=AS_OF)

        # The old row retired, and today's detection put the name back with today's levels —
        # which is the shape a watchlist should have: a name is on it because it qualified
        # *today*, not because it qualified once.
        assert result.expired == 1
        every = await list_watch(session, user_id=user_id, state=None)
        assert sorted(row.state for row in every) == ["EXPIRED", "WATCHING"]
        expired = next(row for row in every if row.state == "EXPIRED")
        assert expired.added_on == long_ago
        current = next(row for row in every if row.state == WATCHING)
        assert current.added_on == AS_OF

    async def test_a_name_that_stopped_qualifying_leaves_the_list_for_good(
        self, session: AsyncSession
    ) -> None:
        """The other half: no detection today, so nothing replaces the expired row."""
        user_id = await _user(session)
        instrument_id = await make_instrument(session, "OLDFLAG")
        long_ago = AS_OF - dt.timedelta(days=60)
        await _setup(
            session, user_id=user_id, instrument_id=instrument_id, score="72.00", on=long_ago
        )
        await auto_watch(session, user_id=user_id, on=long_ago)

        result = await auto_watch(session, user_id=user_id, on=AS_OF)

        assert result.expired == 1
        assert await list_watch(session, user_id=user_id, state=WATCHING) == ()
        every = await list_watch(session, user_id=user_id, state=None)
        assert [row.state for row in every] == ["EXPIRED"]


async def _setup(  # noqa: PLR0913 - one keyword per stored column a test may set
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    setup: str = "FLAG",
    status: str = "SETTING_UP",
    score: str = "72.00",
    on: dt.date = AS_OF,
    trigger: str = "110.00",
    stop_ref: str = "104.00",
    locked: bool = False,
) -> None:
    session.add(
        SwSetupDaily(
            user_id=user_id,
            date=on,
            instrument_id=instrument_id,
            setup=setup,
            status=status,
            score=Decimal(score),
            close=Decimal("108.00"),
            trigger=Decimal(trigger),
            stop_ref=Decimal(stop_ref),
            adj_factor=Decimal(1),
            # 5.60 since SW9.5: the widest stop is one ADR (`04` §6), and a 104 stop under a 110
            # trigger is 5.45% — inside 5.60, outside the 5.00 the fixture used to carry; under
            # the 6% fast-trail line, so the line still trails the 20-day.
            adr_pct=Decimal("5.60"),
            turnover_avg=100_000_000,
            locked_upper_circuit=locked,
            listed_within_2y=False,
        )
    )
    await session.flush()


@pytest.mark.db
class TestTheEveningPlan:
    async def test_it_writes_a_plan_with_its_lines_and_its_skips(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _market(session, user_id=user_id)
        good = await make_instrument(session, "GOODFLAG")
        locked = await make_instrument(session, "LOCKEDCO")
        await _setup(session, user_id=user_id, instrument_id=good, score="72.00")
        await _setup(session, user_id=user_id, instrument_id=locked, score="80.00", locked=True)

        outcome = StepOutcome()
        report = await run_swing_eod(session, outcome, AS_OF, user_id=user_id)

        assert report.entry_lines == 1
        assert report.skips == 1
        plan = (await session.execute(sa.select(SwPlan))).scalar_one()
        lines = (
            (await session.execute(sa.select(SwPlanLine).where(SwPlanLine.plan_id == plan.id)))
            .scalars()
            .all()
        )
        skips = (
            (await session.execute(sa.select(SwPlanSkip).where(SwPlanSkip.plan_id == plan.id)))
            .scalars()
            .all()
        )
        assert [line.kind for line in lines] == ["BUY_ON_TRIGGER"]
        assert [(skip.symbol, skip.reason) for skip in skips] == [
            ("LOCKEDCO", "LOCKED_UPPER_CIRCUIT")
        ]
        assert plan.source == "EOD_PREVIEW"
        # `03` §6: 30 minutes, stored rather than derived.
        assert (plan.expires_at - plan.built_at) == dt.timedelta(minutes=30)

    async def test_the_client_id_carries_the_kind(self, session: AsyncSession) -> None:
        """`04` §9.4. Without the kind, a plan that sells part of a name *and* raises its stop
        would report the second line as a DUPLICATE and silently skip it."""
        user_id = await _user(session)
        await _market(session, user_id=user_id)
        instrument_id = await make_instrument(session, "GOODFLAG")
        await _setup(session, user_id=user_id, instrument_id=instrument_id, score="72.00")

        await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        line = (await session.execute(sa.select(SwPlanLine))).scalar_one()
        assert line.client_id.endswith(":GOODFLAG:BUY_ON_TRIGGER")

    async def test_a_red_gate_plans_nothing_and_says_why_for_every_name(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="RED")
        for symbol in ("AAA", "BBB", "CCC"):
            instrument_id = await make_instrument(session, symbol)
            await _setup(session, user_id=user_id, instrument_id=instrument_id, score="72.00")

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert report.entry_lines == 0
        assert report.skips == 3
        reasons = {skip.reason for skip in (await session.execute(sa.select(SwPlanSkip))).scalars()}
        assert reasons == {"GATE_RED"}

    async def test_a_sleeve_with_no_capital_plans_nothing_and_says_so(
        self, session: AsyncSession
    ) -> None:
        """`sw_config` seeds at zero and `sizing` refuses with `NO_EQUITY` until a person sets
        it (`02` §3.4). The plan is empty for a reason it can state."""
        user_id = await _user(session, capital="0")
        await _market(session, user_id=user_id)
        instrument_id = await make_instrument(session, "GOODFLAG")
        await _setup(session, user_id=user_id, instrument_id=instrument_id, score="72.00")

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert report.entry_lines == 0
        skip = (await session.execute(sa.select(SwPlanSkip))).scalar_one()
        assert (skip.reason, skip.detail) == ("SIZE_REFUSED", "NO_EQUITY")

    async def test_a_date_the_detectors_never_ran_is_skipped(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        outcome = StepOutcome()

        report = await run_swing_eod(session, outcome, AS_OF, user_id=user_id)

        assert outcome.status is StepStatus.SKIPPED
        assert report.plan_id is None
        assert "no sw_market_daily row" in str(outcome.detail["skipped_reason"])


@pytest.mark.db
class TestTheSessionCounter:
    async def test_one_row_per_session_whether_or_not_anything_happened(
        self, session: AsyncSession
    ) -> None:
        """`02` §3.2 counts sessions, not events. A gate satisfied by twenty interesting
        mornings rather than twenty ordinary ones would not be the gate it claims to be."""
        user_id = await _user(session)
        await _market(session, user_id=user_id)

        first = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)
        second = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (first.sessions_logged, second.sessions_logged) == (1, 1)
        row = (await session.execute(sa.select(SwSession))).scalar_one()
        assert row.mode == "DRY_RUN"
        assert isinstance(row.plan_ids, dict)
        plans = row.plan_ids["plans"]
        assert isinstance(plans, list)
        assert len(plans) == 2


@pytest.mark.db
class TestTheWatchFunnel:
    """STANDING-ANSWERS A14 (SW10.5): the top 20 flags by score + every EP are watched; the daily
    focus is the top 5 by score + every EP; MANUAL rows expire after 10 sessions unless
    re-confirmed."""

    async def test_only_the_top_20_flags_by_score_are_auto_watched_plus_every_ep(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        for index in range(25):
            flag = await make_instrument(session, f"FLAG{index:02d}")
            # Scores 99, 98, … 75 — all above the 60 floor; only the top twenty make the list.
            await _setup(session, user_id=user_id, instrument_id=flag, score=str(99 - index))
        ep = await make_instrument(session, "EPCO")
        await _setup(
            session, user_id=user_id, instrument_id=ep, setup="EP", status="GAP_DAY", score="12.00"
        )

        result = await auto_watch(session, user_id=user_id, on=AS_OF)

        rows = await list_watch(session, user_id=user_id)
        flags = sorted(row.symbol for row in rows if row.setup == "FLAG")
        assert flags == [f"FLAG{index:02d}" for index in range(20)], "the top 20 by score"
        assert {row.symbol for row in rows if row.setup == "EP"} == {"EPCO"}
        assert result.added == 21 and result.considered == 26
        by_symbol = {row.symbol: row for row in rows}
        assert by_symbol["FLAG00"].score == Decimal("99.00")
        assert by_symbol["FLAG00"].adr_pct == Decimal("5.60")

    async def test_the_daily_focus_is_the_top_5_flags_by_score_plus_every_ep(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        for index in range(8):
            flag = await make_instrument(session, f"FLAG{index}")
            await _setup(session, user_id=user_id, instrument_id=flag, score=str(90 - index))
        ep = await make_instrument(session, "EPCO")
        await _setup(
            session, user_id=user_id, instrument_id=ep, setup="EP", status="GAP_DAY", score="12.00"
        )

        result = await auto_watch(session, user_id=user_id, on=AS_OF)

        rows = await list_watch(session, user_id=user_id)
        focus = sorted(row.symbol for row in rows if row.focus)
        assert focus == ["EPCO", "FLAG0", "FLAG1", "FLAG2", "FLAG3", "FLAG4"]
        assert result.focus == 6
        below = sorted(row.symbol for row in rows if not row.focus)
        assert below == ["FLAG5", "FLAG6", "FLAG7"], "still watched, never pushed"

    async def test_the_focus_is_recomputed_not_accumulated(self, session: AsyncSession) -> None:
        """A better flag tonight pushes yesterday's fifth out of focus; the flag moves, the
        row stays."""
        user_id = await _user(session)
        yesterday = (await _sessions_before(session, AS_OF, 2))[0]
        for index in range(5):
            flag = await make_instrument(session, f"OLD{index}")
            await _setup(
                session, user_id=user_id, instrument_id=flag, score=str(80 - index), on=yesterday
            )
        await auto_watch(session, user_id=user_id, on=yesterday)
        assert sum(1 for r in await list_watch(session, user_id=user_id) if r.focus) == 5
        new = await make_instrument(session, "NEWBEST")
        await _setup(session, user_id=user_id, instrument_id=new, score="95.00")

        await auto_watch(session, user_id=user_id, on=AS_OF)

        rows = {row.symbol: row for row in await list_watch(session, user_id=user_id)}
        assert rows["NEWBEST"].focus is True
        assert rows["OLD4"].focus is False and rows["OLD4"].state == WATCHING
        assert sum(1 for r in rows.values() if r.focus) == 5

    async def test_a_manual_row_expires_after_ten_sessions_unless_reconfirmed(
        self, session: AsyncSession
    ) -> None:
        """A14: a typed pivot is stale after two weeks. One MANUAL row is left alone, one is
        re-confirmed on day nine; eleven sessions on, the first has expired and the second is
        still watched with its clock running from the re-confirmation."""
        user_id = await _user(session)
        days = await _sessions_before(session, AS_OF, 12)
        stale = await make_instrument(session, "STALECO")
        kept = await make_instrument(session, "KEPTCO")
        first = await add_manual(
            session,
            user_id=user_id,
            instrument_id=stale,
            setup="FLAG",
            on=days[0],
            trigger=Decimal("100"),
            stop_ref=Decimal("96"),
        )
        second = await add_manual(
            session,
            user_id=user_id,
            instrument_id=kept,
            setup="FLAG",
            on=days[0],
            trigger=Decimal("100"),
            stop_ref=Decimal("96"),
        )
        assert first.expires_on == days[10], "ten sessions from the day it was typed"
        await reconfirm(session, user_id=user_id, watch_id=second.id, on=days[8])
        assert second.reconfirmed_on == days[8]
        await _market(session, user_id=user_id)

        await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        rows = {row.symbol: row for row in await list_watch(session, user_id=user_id, state=None)}
        assert rows["STALECO"].state == "EXPIRED"
        assert rows["KEPTCO"].state == WATCHING
        assert rows["KEPTCO"].expires_on is not None and rows["KEPTCO"].expires_on > AS_OF

    async def test_a_manual_row_written_before_the_rule_is_given_its_clock_once(
        self, session: AsyncSession
    ) -> None:
        """A row from before 0031 has no `expires_on`; the first evening after the rule gives it
        one from `added_on`, and a fortnight-old one expires that same evening."""
        user_id = await _user(session)
        days = await _sessions_before(session, AS_OF, 12)
        old = await make_instrument(session, "OLDMANUAL")
        row = SwWatch(
            user_id=user_id,
            instrument_id=old,
            setup="FLAG",
            source="MANUAL",
            added_on=days[0],
            expires_on=None,
            trigger=Decimal("100"),
            stop_ref=Decimal("96"),
            state=WATCHING,
        )
        session.add(row)
        await session.flush()

        result = await auto_watch(session, user_id=user_id, on=AS_OF)

        assert result.expired == 1
        assert row.expires_on == days[10] and row.state == "EXPIRED"


@pytest.mark.db
class TestTheFirstLiveCountdown:
    """STANDING-ANSWERS A9 (SW10.5): half risk at plan time while the countdown runs, the
    countdown moved once per LIVE session by the evening, never by a request."""

    async def _watched_flag(self, session: AsyncSession, user_id: int) -> None:
        flag = await make_instrument(session, "GOODFLAG")
        await _setup(session, user_id=user_id, instrument_id=flag)
        await _market(session, user_id=user_id)

    async def test_a_live_session_closing_decrements_first_live_sessions_left_once(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await self._watched_flag(session, user_id)

        first = await run_swing_eod(
            session, StepOutcome(), AS_OF, user_id=user_id, execution_enabled=True
        )
        second = await run_swing_eod(
            session, StepOutcome(), AS_OF, user_id=user_id, execution_enabled=True
        )

        assert (first.first_live_sessions_left, second.first_live_sessions_left) == (4, 4), (
            "the evening re-run reads `first_live_counted` and leaves the count alone"
        )
        config = (await session.execute(sa.select(SwConfig))).scalar_one()
        assert config.first_live_sessions_left == 4
        row = (await session.execute(sa.select(SwSession))).scalar_one()
        assert row.first_live_counted is True
        audit = (
            (
                await session.execute(
                    sa.select(SwConfigAudit).where(SwConfigAudit.key == "first_live_sessions_left")
                )
            )
            .scalars()
            .all()
        )
        assert [(a.old_value, a.new_value, a.changed_by) for a in audit] == [
            ("5", "4", "swing-eod")
        ]

    async def test_a_dry_run_session_never_moves_the_countdown(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await self._watched_flag(session, user_id)

        report = await run_swing_eod(
            session, StepOutcome(), AS_OF, user_id=user_id, execution_enabled=False
        )

        assert report.first_live_sessions_left == 5
        row = (await session.execute(sa.select(SwSession))).scalar_one()
        assert row.first_live_counted is False

    async def test_the_plan_is_sized_at_half_risk_while_live_and_full_size_on_paper(
        self, session: AsyncSession
    ) -> None:
        """0.5% of ₹10 lakh over a ₹6 stop is 833 shares; at half risk 416. A paper plan with
        the same countdown is 833 — the paper record rehearses the rules at full size."""
        user_id = await _user(session)
        await self._watched_flag(session, user_id)

        paper = await run_swing_eod(
            session, StepOutcome(), AS_OF, user_id=user_id, execution_enabled=False
        )
        live = await run_swing_eod(
            session, StepOutcome(), AS_OF, user_id=user_id, execution_enabled=True
        )

        lines = (
            (await session.execute(sa.select(SwPlanLine).order_by(SwPlanLine.id))).scalars().all()
        )
        assert [line.quantity for line in lines] == [833, 416]
        assert (paper.risk_multiplier, paper.risk_pct_in_force) == ("1", "0.500")
        assert (live.risk_multiplier, live.risk_pct_in_force) == ("0.5", "0.250")
        assert live.first_live_header() == "first live sessions: 4 left · risk 0.250%"
        assert paper.first_live_header() == "first live sessions: 5 left · risk 0.500%"

    async def test_the_fifth_session_decrements_to_zero_and_the_sixth_plans_at_full_risk(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        days = await _sessions_before(session, AS_OF, 6)
        flag = await make_instrument(session, "GOODFLAG")
        for on in days:
            await _market(session, user_id=user_id, on=on)
        await _setup(session, user_id=user_id, instrument_id=flag, on=days[0])
        quantities: list[int] = []
        lefts: list[int] = []
        for on in days:
            report = await run_swing_eod(
                session, StepOutcome(), on, user_id=user_id, execution_enabled=True
            )
            lefts.append(report.first_live_sessions_left)
            lines = (
                (
                    await session.execute(
                        sa.select(SwPlanLine)
                        .join(SwPlan, SwPlan.id == SwPlanLine.plan_id)
                        .where(SwPlan.as_of == on)
                    )
                )
                .scalars()
                .all()
            )
            quantities.append(lines[0].quantity if lines else -1)

        assert lefts == [4, 3, 2, 1, 0, 0]
        # Each evening plans the NEXT session: evenings 1-4 (sessions 2-5 ahead) at half risk,
        # evening 5 has counted the fifth live session down to 0 and plans the sixth at full.
        assert quantities == [416, 416, 416, 416, 833, 833]
        assert report.first_live_header() == ""

    async def test_a_restart_mid_countdown_changes_nothing(self, session: AsyncSession) -> None:
        """The count lives in `sw_config` and the mark in `sw_session`; nothing in a process
        remembers it, so calling the counter again — as a restarted worker would — moves it
        no further."""
        user_id = await _user(session)
        await _market(session, user_id=user_id)
        await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id, execution_enabled=True)
        now = dt.datetime(2026, 8, 18, 21, 5, tzinfo=dt.UTC)

        again = await count_first_live_session(
            session, user_id=user_id, on=AS_OF, execution_enabled=True, now=now
        )

        assert again == 4
        config = (await session.execute(sa.select(SwConfig))).scalar_one()
        assert config.first_live_sessions_left == 4


class TestTheEmail:
    """`05` §4, rendered. No database: a template is a pure function of its digest."""

    def _digest(self, *, naked: tuple[str, ...] = ()) -> SwingDigest:
        return SwingDigest(
            as_of=AS_OF,
            gate="GREEN",
            exposure_level=0,
            max_open_positions=2,
            naked=naked,
            exits=(SwingCandidate("HELDCO", "FLAG", "—", "100.00", "PARTIAL_INTO_STRENGTH"),),
            entries=(SwingCandidate("GOODFLAG", "FLAG", "110.00", "104.00", "size by RISK"),),
            skips=(("LOCKEDCO", "LOCKED_UPPER_CIRCUIT no fill at band"),),
            flags=(SwingCandidate("GOODFLAG", "FLAG", "110.00", "104.00"),),
            eps=(),
            sessions_logged=14,
            sessions_required=20,
        )

    def test_the_naked_section_is_present_even_when_it_is_empty(self) -> None:
        """The criterion's own words. A section that appeared only when something was wrong
        would train the reader to skim past the top of the message."""
        message = swing_eod("a@example.com", self._digest(), swing_url="https://x/swing")
        assert "Every open position has a resting stop." in message.text
        assert "Every open position has a resting stop." in message.html

    def test_an_unprotected_position_leads_the_subject_line(self) -> None:
        message = swing_eod(
            "a@example.com", self._digest(naked=("NAKEDCO",)), swing_url="https://x/swing"
        )
        assert message.subject.startswith("1 unprotected")
        assert "NAKEDCO" in message.text

    def test_the_session_counter_is_on_it(self) -> None:
        message = swing_eod("a@example.com", self._digest(), swing_url="https://x/swing")
        assert "14 of 20 paper sessions logged." in message.text

    def test_every_skip_is_listed_with_its_reason(self) -> None:
        message = swing_eod("a@example.com", self._digest(), swing_url="https://x/swing")
        assert "LOCKEDCO" in message.text
        assert "LOCKED_UPPER_CIRCUIT" in message.text

    def test_an_evening_with_nothing_to_do_still_says_so(self) -> None:
        """An email of empty sections is more useful than an email that omits them: "nothing
        was refused" is a fact, and a missing section is an ambiguity."""
        digest = SwingDigest(
            as_of=AS_OF,
            gate="RED",
            exposure_level=0,
            max_open_positions=2,
            naked=(),
            exits=(),
            entries=(),
            skips=(),
            flags=(),
            eps=(),
            sessions_logged=1,
            sessions_required=20,
        )
        message = swing_eod("a@example.com", digest, swing_url="https://x/swing")
        assert "Tomorrow's exits: none." in message.text
        assert "Refused, and why: nothing was refused." in message.text
        assert message.subject.startswith("0 exits, 0 entries")
