"""TW6's evening and morning plans — ``docs/twt/04`` §§10, 11, against a real database.

The claims, in the order the job makes them:

* **Exits come first**: a position with shares and no resting GTT is an ``ARM_GTT``; one whose
  evening-computed ``next_trigger`` beats its resting ``gtt_trigger`` is a ``RAISE_GTT_STOP`` —
  the ratchet, and the reason this sleeve exists in the first place.
* **A signal becomes a ``BUY_AT_OPEN`` line** sized against the sleeve's own money, with the cap
  that bound it named, and **every name it passed over recorded as a skip**, because a plan is
  not honest without them. A ``SCAN_ONLY`` row reaches the plan so its ``BELOW_LIQUIDITY_FLOOR``
  is recorded rather than lost between two modules (DECISIONS-TW TW6.1).
* **The morning rebuilds the same plan, re-sized and NOT re-detected** (``04`` §11.3). It reads
  the same session, the same signal rows and the same levels; what moves is the equity.
* **Nothing is placed.** Every line is ``PROPOSED``; there is no code path here that reaches a
  gateway, and there is no flag that changes it.
* **A corporate action under a hold raises ``TWT_ADJUSTMENT_RESET``** — in both branches, and in
  particular in the branch where the trail was refused and *nothing else happened*.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from helpers import add_bar, make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    AppUser,
    OhlcvDaily,
    TwBreadthDaily,
    TwConfig,
    TwOrder,
    TwPlan,
    TwPlanLine,
    TwPlanSkip,
    TwPosition,
    TwSession,
    TwSignalDaily,
)
from baskfy_core.models.twt import TW_PLAN_TTL_MINUTES
from baskfy_core.twt.config import Gate, SignalState
from baskfy_core.twt.plan import LineKind, SkipReason
from baskfy_worker.alerts import Alert, AlertName
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks import twt_evening
from baskfy_worker.tasks.twt_evening import (
    SOURCE_EVENING,
    SOURCE_MORNING,
    EveningReport,
    last_detected_session,
    run_twt_evening,
)

pytestmark = requires_db

AS_OF = dt.date(2026, 9, 10)
NOW = dt.datetime(2026, 9, 10, 21, 5, tzinfo=dt.UTC)
#: ``04`` §3.5's worked example: ten slots at ₹25 lakh is a ₹2.5 lakh line.
TWENTY_FIVE_LAKH = Decimal("2500000.00")
#: A name turning over ₹50 crore a day, where the 1 %-of-turnover cap does not bind.
FIFTY_CRORE = 500_000_000
#: The shipped floor is ₹5 crore (``04`` §3.5); ₹1 crore is under it.
ONE_CRORE = 10_000_000


async def _user(
    session: AsyncSession,
    tag: str,
    *,
    capital: Decimal = TWENTY_FIVE_LAKH,
    first_live_entries_left: int = 10,
) -> int:
    """A user with a seeded ``tw_config``. **Capital is a fixture's, never the run's.**"""
    user = AppUser(public_id=f"tw6-{tag}", email=f"tw6-{tag}@example.com")
    session.add(user)
    await session.flush()
    session.add(
        TwConfig(
            user_id=user.id,
            sleeve_capital_inr=capital,
            max_open_positions=10,
            max_position_pct=Decimal("12.50"),
            stop_pct=Decimal("20.00"),
            trail_pct=Decimal("20.00"),
            first_live_entries_left=first_live_entries_left,
            dry_run_sessions=0,
            updated_by="test",
        )
    )
    await session.flush()
    return int(user.id)


async def _breadth(
    session: AsyncSession,
    user_id: int,
    *,
    gate: Gate = Gate.OPEN,
    on: dt.date = AS_OF,
    thin: bool = False,
) -> None:
    session.add(
        TwBreadthDaily(
            user_id=user_id,
            date=on,
            universe_count=100,
            measured_count=100,
            above_count=0 if thin else 60,
            pct_above_dma=None if thin else Decimal("60.0000"),
            gate=Gate.SHUT.value if thin else gate.value,
            dma_bars=200,
            thin_session=thin,
        )
    )
    await session.flush()


async def _signal(  # noqa: PLR0913 - a signal row is its numbers
    session: AsyncSession,
    user_id: int,
    symbol: str,
    *,
    close: str = "100.00",
    rank: int = 1_000_000,
    turnover: int = FIFTY_CRORE,
    state: str = SignalState.SIGNAL.value,
    on: dt.date = AS_OF,
) -> int:
    instrument_id = await make_instrument(session, symbol)
    await add_bar(session, instrument_id, on, close)
    reference = Decimal(close)
    session.add(
        TwSignalDaily(
            user_id=user_id,
            date=on,
            instrument_id=instrument_id,
            state=state,
            failed_filters=[] if state == SignalState.SIGNAL.value else ["TURNOVER"],
            entry_reference_close=reference,
            stop_preview=(reference * Decimal("0.8")).quantize(Decimal("0.01")),
            sessions_out_before=5,
            rank_key=rank,
            turnover_avg_20=turnover,
        )
    )
    await session.flush()
    return instrument_id


async def _position(  # noqa: PLR0913 - a book row is its numbers
    session: AsyncSession,
    user_id: int,
    instrument_id: int,
    *,
    quantity: int = 100,
    entry: str = "100.00",
    gtt_id: str | None = "GTT-1",
    gtt_trigger: str | None = "80.00",
    next_trigger: str | None = None,
    next_trigger_for: dt.date | None = None,
    entry_adj_factor: str = "1",
) -> int:
    entry_avg = Decimal(entry)
    position = TwPosition(
        user_id=user_id,
        instrument_id=instrument_id,
        signal_date=AS_OF - dt.timedelta(days=60),
        entry_date=AS_OF - dt.timedelta(days=60),
        entry_avg=entry_avg,
        entry_adj_factor=Decimal(entry_adj_factor),
        quantity_entered=quantity,
        quantity_open=quantity,
        initial_stop=(entry_avg * Decimal("0.8")).quantize(Decimal("0.01")),
        stop_price=(entry_avg * Decimal("0.8")).quantize(Decimal("0.01")),
        high_since=entry_avg,
        high_since_date=AS_OF,
        gtt_id=gtt_id,
        gtt_trigger=None if gtt_trigger is None else Decimal(gtt_trigger),
        next_trigger=None if next_trigger is None else Decimal(next_trigger),
        next_trigger_for=next_trigger_for,
        state="OPEN",
        simulated=True,
    )
    session.add(position)
    await session.flush()
    return int(position.id)


async def _plan(
    session: AsyncSession,
    user_id: int,
    *,
    on: dt.date = AS_OF,
    source: str = SOURCE_EVENING,
    execution_enabled: bool = False,
) -> EveningReport | None:
    outcome = StepOutcome()
    return await run_twt_evening(
        session,
        outcome,
        on,
        user_id=user_id,
        source=source,
        now=NOW,
        execution_enabled=execution_enabled,
        notify=False,
    )


async def _lines(session: AsyncSession, kind: LineKind | None = None) -> list[TwPlanLine]:
    statement = sa.select(TwPlanLine)
    if kind is not None:
        statement = statement.where(TwPlanLine.kind == kind.value)
    return list((await session.execute(statement)).scalars())


async def _skips(session: AsyncSession) -> list[TwPlanSkip]:
    return list((await session.execute(sa.select(TwPlanSkip))).scalars())


@pytest.mark.db
class TestTheEveningPlansTheEntries:
    async def test_a_signal_becomes_a_buy_at_open_line_on_the_stored_plan(
        self, session: AsyncSession
    ) -> None:
        """``04`` §5.1 and §10.1 — the next session's open, at market, sized off the sleeve."""
        user_id = await _user(session, "entry")
        await _breadth(session, user_id)
        await _signal(session, user_id, "TWTCO", close="100.00")

        report = await _plan(session, user_id)

        assert report is not None
        assert report.entries == 1
        line = (await _lines(session, LineKind.BUY_AT_OPEN))[0]
        assert line.state == "PROPOSED"
        assert line.quantity == 2_500  # a tenth of ₹25 lakh at ₹100
        assert line.value_inr == Decimal("250000.00")
        assert line.stop_price == Decimal("80.00")  # 20 % under, on the tick
        # `04` §10.4: `plan_id:symbol` for an order, no kind suffix.
        assert line.client_id is not None and line.client_id.endswith(":TWTCO")

    async def test_the_plan_row_carries_its_gate_equity_and_thirty_minute_expiry(
        self, session: AsyncSession
    ) -> None:
        """``04`` §10.4 — the expiry is the schema's constant, not a number typed twice."""
        user_id = await _user(session, "row")
        await _breadth(session, user_id)
        await _signal(session, user_id, "TWTCO")

        await _plan(session, user_id)

        plan = (await session.execute(sa.select(TwPlan))).scalar_one()
        assert plan.source == SOURCE_EVENING
        assert plan.session_date == AS_OF
        assert plan.gate == Gate.OPEN.value
        assert plan.sleeve_equity_inr == TWENTY_FIVE_LAKH
        assert plan.total_new_exposure_inr == Decimal("250000.00")
        assert plan.expires_at - plan.built_at == dt.timedelta(minutes=TW_PLAN_TTL_MINUTES)
        assert len(plan.plan_hash) == 64

    async def test_at_most_three_entries_a_session_and_the_rest_are_skips(
        self, session: AsyncSession
    ) -> None:
        """``04`` §6.3 — and the plan says so by name rather than by being short."""
        user_id = await _user(session, "cap")
        await _breadth(session, user_id)
        for index in range(5):
            await _signal(session, user_id, f"NAME{index}", rank=1_000_000 - index)

        report = await _plan(session, user_id)

        assert report is not None
        assert report.entries == 3
        assert [skip.reason for skip in await _skips(session)] == [SkipReason.SESSION_CAP.value] * 2

    async def test_a_scan_only_row_reaches_the_plan_as_a_liquidity_skip(
        self, session: AsyncSession
    ) -> None:
        """DECISIONS-TW TW6.1 — the funnel is the argument for the floor, so it is recorded."""
        user_id = await _user(session, "scanonly")
        await _breadth(session, user_id)
        await _signal(
            session,
            user_id,
            "THINCO",
            turnover=ONE_CRORE,
            state=SignalState.SCAN_ONLY.value,
        )

        report = await _plan(session, user_id)

        assert report is not None
        assert report.entries == 0
        assert [skip.reason for skip in await _skips(session)] == [
            SkipReason.BELOW_LIQUIDITY_FLOOR.value
        ]

    async def test_a_shut_gate_lines_nothing_and_the_plan_says_why(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session, "shut")
        await _breadth(session, user_id, gate=Gate.SHUT)
        await _signal(session, user_id, "TWTCO")

        report = await _plan(session, user_id)

        assert report is not None
        assert report.entries == 0
        assert [skip.reason for skip in await _skips(session)] == [SkipReason.GATE_SHUT.value]

    async def test_a_sleeve_with_no_capital_plans_nothing(self, session: AsyncSession) -> None:
        """``04`` §9.3 — the rail that keeps this safe until Maulik funds it himself."""
        user_id = await _user(session, "broke", capital=Decimal("0.00"))
        await _breadth(session, user_id)
        await _signal(session, user_id, "TWTCO")

        report = await _plan(session, user_id)

        assert report is not None
        assert report.entries == 0
        assert [skip.reason for skip in await _skips(session)] == [
            SkipReason.NO_SLEEVE_CAPITAL.value
        ]

    async def test_a_name_the_sleeve_already_holds_is_never_averaged_down(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session, "held")
        await _breadth(session, user_id)
        instrument_id = await _signal(session, user_id, "TWTCO")
        await _position(session, user_id, instrument_id)

        report = await _plan(session, user_id)

        assert report is not None
        assert report.entries == 0
        assert [skip.reason for skip in await _skips(session)] == [SkipReason.ALREADY_HELD.value]

    async def test_the_session_cap_counts_orders_already_confirmed_this_session(
        self, session: AsyncSession
    ) -> None:
        """``04`` §6.3 — whatever plan they came from, so a rebuild cannot offer three more."""
        user_id = await _user(session, "already")
        await _breadth(session, user_id)
        first = await _signal(session, user_id, "AAA", rank=3)
        await _signal(session, user_id, "BBB", rank=2)
        await _signal(session, user_id, "CCC", rank=1)
        session.add(
            TwOrder(
                user_id=user_id,
                instrument_id=first,
                signal_date=AS_OF,
                side="BUY",
                quantity=10,
                stop_price=Decimal("80.00"),
                state="CONFIRMED",
            )
        )
        await session.flush()

        report = await _plan(session, user_id)

        assert report is not None
        assert report.entries == 2


@pytest.mark.db
class TestTheEveningPlansTheExitsFirst:
    async def test_a_position_without_a_resting_gtt_is_an_arm_line(
        self, session: AsyncSession
    ) -> None:
        """Non-negotiable 4, as a plan line: the one state the method forbids."""
        user_id = await _user(session, "naked")
        await _breadth(session, user_id)
        instrument_id = await make_instrument(session, "NAKEDCO")
        await add_bar(session, instrument_id, AS_OF, "100.00")
        await _position(session, user_id, instrument_id, gtt_id=None, gtt_trigger=None)

        report = await _plan(session, user_id)

        assert report is not None
        assert report.arms == 1
        assert report.naked == 1
        line = (await _lines(session, LineKind.ARM_GTT))[0]
        assert line.quantity == 100
        assert line.stop_price == Decimal("80.00")
        assert line.client_id is not None and line.client_id.endswith(":NAKEDCO:ARM_GTT")

    async def test_the_ratchet_is_a_plan_line_carrying_both_triggers(
        self, session: AsyncSession
    ) -> None:
        """``04`` §7.2 and §10.2 — the page shows the move, not a number."""
        user_id = await _user(session, "ratchet")
        await _breadth(session, user_id)
        instrument_id = await make_instrument(session, "RATCHETCO")
        await add_bar(session, instrument_id, AS_OF, "130.00")
        await _position(
            session,
            user_id,
            instrument_id,
            gtt_trigger="80.00",
            next_trigger="104.00",
            next_trigger_for=AS_OF,
        )

        report = await _plan(session, user_id)

        assert report is not None
        assert report.ratchets == 1
        line = (await _lines(session, LineKind.RAISE_GTT_STOP))[0]
        assert line.stop_price == Decimal("104.00")
        assert line.previous_trigger == Decimal("80.00")
        assert line.high_since == Decimal("100.00")
        assert line.client_id is not None and line.client_id.endswith(":RATCHETCO:RAISE_GTT_STOP")

    async def test_a_trigger_computed_for_another_session_is_not_a_line(
        self, session: AsyncSession
    ) -> None:
        """``04`` §11.3 — a plan never reads a trigger computed for another session."""
        user_id = await _user(session, "stale")
        await _breadth(session, user_id)
        instrument_id = await make_instrument(session, "STALECO")
        await add_bar(session, instrument_id, AS_OF, "130.00")
        await _position(
            session,
            user_id,
            instrument_id,
            next_trigger="104.00",
            next_trigger_for=AS_OF - dt.timedelta(days=1),
        )

        report = await _plan(session, user_id)

        assert report is not None
        assert report.ratchets == 0

    async def test_the_plan_never_emits_a_sell_at_open(self, session: AsyncSession) -> None:
        """``04`` §10.2 — TWT-1 has no end-of-day sell rule; the GTT is the exit."""
        user_id = await _user(session, "nosell")
        await _breadth(session, user_id)
        instrument_id = await make_instrument(session, "HOLDCO")
        await add_bar(session, instrument_id, AS_OF, "40.00")  # far under the stop
        await _position(session, user_id, instrument_id)
        await _signal(session, user_id, "TWTCO")

        await _plan(session, user_id)

        assert await _lines(session, LineKind.SELL_AT_OPEN) == []

    async def test_exits_are_stored_before_entries(self, session: AsyncSession) -> None:
        """``04`` §10.3, ``05`` §2 — a morning that runs out of attention has armed the stops."""
        user_id = await _user(session, "order")
        await _breadth(session, user_id)
        instrument_id = await make_instrument(session, "NAKEDCO")
        await add_bar(session, instrument_id, AS_OF, "100.00")
        await _position(session, user_id, instrument_id, gtt_id=None, gtt_trigger=None)
        await _signal(session, user_id, "TWTCO")

        await _plan(session, user_id)

        kinds = [line.kind for line in sorted(await _lines(session), key=lambda row: row.id)]
        assert kinds == [LineKind.ARM_GTT.value, LineKind.BUY_AT_OPEN.value]


@pytest.mark.db
class TestTheMorningRebuildsThePlan:
    async def test_the_morning_plan_is_a_second_row_not_a_rewrite_of_the_evening(
        self, session: AsyncSession
    ) -> None:
        """``04`` §11.3 — same session, a new ``plan_id``, and the evening's is still there."""
        user_id = await _user(session, "morning")
        await _breadth(session, user_id)
        await _signal(session, user_id, "TWTCO")

        evening = await _plan(session, user_id, source=SOURCE_EVENING)
        morning = await _plan(session, user_id, source=SOURCE_MORNING)

        assert evening is not None and morning is not None
        assert morning.session == evening.session == AS_OF
        assert morning.plan_id != evening.plan_id
        sources = sorted(row.source for row in (await session.execute(sa.select(TwPlan))).scalars())
        assert sources == [SOURCE_EVENING, SOURCE_MORNING]

    async def test_the_morning_resizes_and_does_not_redetect(self, session: AsyncSession) -> None:
        """The morning reads the **same** signal rows; what moves is the sleeve's equity.

        The position's mark is raised between the two builds — which is exactly what a session
        of marks does — and the entry line grows with the equity while the *signal* is untouched.
        A morning that re-detected would be trading a partial day.
        """
        user_id = await _user(session, "resize")
        await _breadth(session, user_id)
        await _signal(session, user_id, "TWTCO", close="100.00")
        held = await make_instrument(session, "HELDCO")
        await add_bar(session, held, AS_OF, "100.00")
        await _position(session, user_id, held, quantity=1_000, entry="100.00")

        evening = await _plan(session, user_id, source=SOURCE_EVENING)
        # The mark doubles: ₹1,00,000 of cost becomes ₹2,00,000 of value.
        await session.execute(
            sa.update(OhlcvDaily)
            .where(OhlcvDaily.instrument_id == held, OhlcvDaily.date == AS_OF)
            .values(close=Decimal("200.00"))
        )
        morning = await _plan(session, user_id, source=SOURCE_MORNING)

        assert evening is not None and morning is not None
        assert morning.equity_inr > evening.equity_inr
        evening_line = next(
            line
            for line in await _lines(session, LineKind.BUY_AT_OPEN)
            if line.client_id and line.client_id.startswith(evening.plan_id)
        )
        morning_line = next(
            line
            for line in await _lines(session, LineKind.BUY_AT_OPEN)
            if line.client_id and line.client_id.startswith(morning.plan_id)
        )
        assert morning_line.quantity > evening_line.quantity
        # And nothing re-detected: one signal row, the one the evening read.
        signals = list((await session.execute(sa.select(TwSignalDaily))).scalars())
        assert len(signals) == 1 and signals[0].date == AS_OF

    async def test_rebuilding_the_same_source_replaces_rather_than_duplicates(
        self, session: AsyncSession
    ) -> None:
        """House rule 7 — a re-run of the evening rewrites its own plan."""
        user_id = await _user(session, "rerun")
        await _breadth(session, user_id)
        await _signal(session, user_id, "TWTCO")

        first = await _plan(session, user_id)
        second = await _plan(session, user_id)

        assert first is not None and second is not None
        assert first.plan_id != second.plan_id
        plans = list((await session.execute(sa.select(TwPlan))).scalars())
        assert [str(row.plan_id) for row in plans] == [second.plan_id]

    async def test_the_morning_plans_from_the_last_tradeable_session(
        self, session: AsyncSession
    ) -> None:
        """``04`` §2.1 and §11.1 — a thin session is not the session the rules traded."""
        user_id = await _user(session, "last")
        await _breadth(session, user_id, on=AS_OF - dt.timedelta(days=1))
        await _breadth(session, user_id, on=AS_OF, thin=True)

        assert await last_detected_session(session, user_id, AS_OF) == AS_OF - dt.timedelta(days=1)


@pytest.mark.db
class TestWhatTheEveningRefusesAndRecords:
    async def test_a_session_the_detector_never_saw_plans_nothing_and_says_so(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session, "undetected")

        outcome = StepOutcome()
        report = await run_twt_evening(
            session, outcome, AS_OF, user_id=user_id, now=NOW, notify=False
        )

        assert report is None
        assert "no tw_breadth_daily row" in str(outcome.detail)
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(TwPlan))
        ).scalar_one() == 0

    async def test_the_session_row_records_the_gate_the_counts_and_the_plan_id(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session, "session")
        await _breadth(session, user_id)
        await _signal(session, user_id, "TWTCO")

        report = await _plan(session, user_id)

        assert report is not None
        row = (await session.execute(sa.select(TwSession))).scalar_one()
        assert row.gate == Gate.OPEN.value
        assert row.mode == "DRY_RUN"
        assert row.signals == 1
        assert row.plan_ids == {"evening": report.plan_id}

    async def test_the_evening_never_moves_the_first_live_countdown(
        self, session: AsyncSession
    ) -> None:
        """``04`` §6.4 — the countdown is spent by a **fill**, never by a plan (TW5.3)."""
        user_id = await _user(session, "countdown")
        await _breadth(session, user_id)
        await _signal(session, user_id, "TWTCO")

        await _plan(session, user_id, execution_enabled=True)

        row = (await session.execute(sa.select(TwConfig))).scalar_one()
        assert row.first_live_entries_left == 10

    async def test_a_corporate_action_under_a_hold_annotates_the_raise_line(
        self, session: AsyncSession
    ) -> None:
        """``04`` §7.3 step 3 — the row itself says the level was re-derived."""
        user_id = await _user(session, "split")
        await _breadth(session, user_id)
        instrument_id = await make_instrument(session, "SPLITCO")
        await add_bar(session, instrument_id, AS_OF, "60.00")
        await session.execute(
            sa.update(OhlcvDaily)
            .where(OhlcvDaily.instrument_id == instrument_id, OhlcvDaily.date == AS_OF)
            .values(adj_factor=Decimal("0.5"))
        )
        await _position(
            session,
            user_id,
            instrument_id,
            gtt_trigger="80.00",
            next_trigger="96.00",
            next_trigger_for=AS_OF,
            entry_adj_factor="1",
        )

        report = await _plan(session, user_id)

        assert report is not None
        assert report.adjustments == 1
        line = (await _lines(session, LineKind.RAISE_GTT_STOP))[0]
        assert line.note is not None
        assert "corporate action" in line.note
        assert "pre-adjustment price" in line.note

    async def test_a_corporate_action_that_emitted_no_line_still_raises_the_alert(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DECISIONS-TW TW0.7 — **the branch where nothing else happens** is the dangerous one.

        The trail was re-derived below the resting stop, so TW4 refused to emit a trigger and
        ``next_trigger`` is null. The GTT resting at the exchange is still quoting a pre-split
        price, and the alert is the only thing that tells anybody.
        """
        user_id = await _user(session, "refused")
        await _breadth(session, user_id)
        instrument_id = await make_instrument(session, "REFUSECO")
        await add_bar(session, instrument_id, AS_OF, "60.00")
        await session.execute(
            sa.update(OhlcvDaily)
            .where(OhlcvDaily.instrument_id == instrument_id, OhlcvDaily.date == AS_OF)
            .values(adj_factor=Decimal("0.5"))
        )
        await _position(
            session,
            user_id,
            instrument_id,
            gtt_trigger="80.00",
            next_trigger=None,
            next_trigger_for=None,
            entry_adj_factor="1",
        )
        raised = _capture_alerts(monkeypatch)

        outcome = StepOutcome()
        report = await run_twt_evening(
            session, outcome, AS_OF, user_id=user_id, now=NOW, notify=True
        )

        assert report is not None
        assert report.ratchets == 0
        assert report.adjustments == 1
        assert AlertName.TWT_ADJUSTMENT_RESET in raised

    async def test_an_ordinary_evening_raises_the_digest_and_no_adjustment_alert(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``06`` § TW6 — ``TWT_EVENING`` is the plan, delivered (DECISIONS-TW TW6.2)."""
        user_id = await _user(session, "digest")
        await _breadth(session, user_id)
        await _signal(session, user_id, "TWTCO")
        raised = _capture_alerts(monkeypatch)

        outcome = StepOutcome()
        await run_twt_evening(session, outcome, AS_OF, user_id=user_id, now=NOW, notify=True)

        assert raised == [AlertName.TWT_EVENING]


def _capture_alerts(monkeypatch: pytest.MonkeyPatch) -> list[AlertName]:
    """Record what the evening raised without letting a sink see it.

    ``dispatch`` is replaced on the module under test rather than in ``baskfy_worker.alerts``,
    so the substitution is exactly as narrow as the claim: *this job raised these names*.
    """
    seen: list[AlertName] = []

    async def _record(alert: Alert, *_args: object, **_kwargs: object) -> dict[str, object]:
        seen.append(alert.name)
        return {"alert": alert.name.value, "delivered_to": []}

    monkeypatch.setattr(twt_evening, "dispatch", _record)
    return seen
