"""VB7 — the four in-process checks behind the volume-breakout sleeve's alerts.

The one that matters is `VBT_ORDER_PAST_EXPIRY`, and it is the only alert in this repository
that exists because of a *cliff*: `docs/vbt/04` §7.2's three-session window returns 18.2% a year
where two returns 11.4%, so a limit that outlives its window is not a worse version of the trade,
it is a different trade nobody chose. The evening's sweep writes the cancel line; this check is
what notices that nobody confirmed it.

Every one of the four is silent when all is well, quiet on a weekend, and incapable of fixing
what it finds — the fix is a confirm on the desk page, which is the sleeve's whole safety
argument (`docs/vbt/02` §2).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import cast

import pytest
import sqlalchemy as sa
from celery.schedules import crontab
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.email import Mailer, Message
from baskfy_api.settings import Settings
from baskfy_core.models import (
    AppUser,
    OhlcvDaily,
    VbBreadthDaily,
    VbConfig,
    VbOrder,
    VbPosition,
)
from baskfy_core.vbt.config import Gate
from baskfy_core.vbt.orders import LIVE_STATES, TERMINAL_STATES, OrderState
from baskfy_worker.alerts import AlertName, Severity
from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_DEFAULT, TASK_ROUTES
from baskfy_worker.ops import RUNBOOKS
from baskfy_worker.tasks.vbt_ops import (
    IST,
    RUNBOOK,
    check_detect_fresh,
    check_naked_positions,
    check_orders_past_expiry,
    check_positions_without_bars,
)

pytestmark = [requires_db, pytest.mark.db]

#: A Tuesday, and the session the sleeve's evening is about.
DAY = dt.date(2026, 9, 8)
SATURDAY = dt.date(2026, 9, 12)


def _at(hhmm: str, day: dt.date = DAY) -> dt.datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)


def _settings(**overrides: object) -> Settings:
    base = Settings(environment="test", log_json=False, log_level="CRITICAL")
    return base.model_copy(update=dict(overrides))


class Recording:
    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> None:
        self.sent.append(message)


async def _user(session: AsyncSession) -> int:
    user = AppUser(public_id="vb7-user", email="vb7@example.com")
    session.add(user)
    await session.flush()
    session.add(VbConfig(user_id=user.id, updated_by="test"))
    await session.flush()
    return int(user.id)


async def _order(  # noqa: PLR0913 - an order row is its fields
    session: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    state: str = OrderState.CONFIRMED.value,
    expires_after_session: dt.date | None,
    signal_date: dt.date = dt.date(2026, 9, 2),
) -> VbOrder:
    order = VbOrder(
        user_id=user_id,
        instrument_id=await make_instrument(session, symbol),
        signal_date=signal_date,
        limit_price=Decimal("100.00"),
        stop_price=Decimal("88.00"),
        quantity=100,
        state=state,
        working_from=signal_date,
        expires_after_session=expires_after_session,
    )
    session.add(order)
    await session.flush()
    return order


async def _position(  # noqa: PLR0913 - a position row is its fields
    session: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    gtt_id: str | None = "SIM-1",
    quantity_open: int = 100,
    state: str = "OPEN",
) -> int:
    instrument_id = await make_instrument(session, symbol)
    session.add(
        VbPosition(
            user_id=user_id,
            instrument_id=instrument_id,
            entry_date=dt.date(2026, 9, 1),
            entry_avg=Decimal("100.0000"),
            quantity_entered=100,
            quantity_open=quantity_open,
            initial_stop=Decimal("88.00"),
            stop_price=Decimal("88.00"),
            gtt_id=gtt_id,
            state=state,
        )
    )
    await session.flush()
    return instrument_id


async def _bar(session: AsyncSession, instrument_id: int, on: dt.date) -> None:
    session.add(
        OhlcvDaily(
            instrument_id=instrument_id,
            date=on,
            open=Decimal("100.0000"),
            high=Decimal("100.0000"),
            low=Decimal("100.0000"),
            close=Decimal("100.0000"),
            close_raw=Decimal("100.0000"),
            volume=1_000,
            volume_raw=1_000,
            turnover=Decimal("100000.00"),
            adj_factor=Decimal(1),
            source="nse",
        )
    )
    await session.flush()


async def _breadth(session: AsyncSession, user_id: int, on: dt.date) -> None:
    session.add(
        VbBreadthDaily(
            user_id=user_id,
            date=on,
            universe_count=100,
            measured_count=100,
            above_count=60,
            pct_above_dma=Decimal("60.0000"),
            gate=Gate.OPEN.value,
            dma_bars=200,
        )
    )
    await session.flush()


class TestTheExpiryAlarm:
    """`docs/vbt/06` VB7's last acceptance criterion: the alert fires in the test harness."""

    async def test_a_limit_past_its_third_session_raises_the_alert(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _order(
            session,
            user_id=user_id,
            symbol="STALE",
            expires_after_session=DAY - dt.timedelta(days=1),
        )
        await _order(session, user_id=user_id, symbol="LIVE", expires_after_session=DAY)
        mailer = Recording()

        result = await check_orders_past_expiry(
            session,
            now=_at("21:40"),
            user_id=user_id,
            settings=_settings(ops_alert_email="ops@example.com"),
            mailer=Mailer(mailer),
        )

        assert result["past_expiry"] == 1
        alert = cast(dict[str, object], result["alert"])
        assert alert["alert"] == AlertName.VBT_ORDER_PAST_EXPIRY.value
        assert alert["severity"] == Severity.CRITICAL.value
        assert alert["runbook"] == RUNBOOK
        assert "email" in cast(list[str], alert["delivered_to"])
        assert "1 volume-breakout limit order(s)" in mailer.sent[0].subject

    async def test_an_order_whose_window_ends_today_is_not_late(
        self, session: AsyncSession
    ) -> None:
        """The window is closed *after* the third session's close, so on the day itself the
        order is still the strategy's order and the sweep has not run yet."""
        user_id = await _user(session)
        await _order(session, user_id=user_id, symbol="TODAY", expires_after_session=DAY)

        result = await check_orders_past_expiry(session, now=_at("21:40"), user_id=user_id)

        assert result == {
            "date": DAY.isoformat(),
            "checked": True,
            "past_expiry": 0,
            "alert": None,
        }

    @pytest.mark.parametrize("state", sorted(state.value for state in LIVE_STATES))
    async def test_every_live_state_can_be_late(self, session: AsyncSession, state: str) -> None:
        """`04` §7.4: a `SENT` limit is at the broker and a `PARTIAL` one still has a resting
        remainder. Both are orders, so both can outlive their window."""
        user_id = await _user(session)
        await _order(
            session,
            user_id=user_id,
            symbol=f"S{state[:4]}",
            state=state,
            expires_after_session=DAY - dt.timedelta(days=1),
        )

        result = await check_orders_past_expiry(session, now=_at("21:40"), user_id=user_id)

        assert result["past_expiry"] == 1

    @pytest.mark.parametrize("state", sorted(state.value for state in TERMINAL_STATES))
    async def test_no_terminal_state_is_ever_late(self, session: AsyncSession, state: str) -> None:
        """A cancelled order is the sweep having *worked*. Alerting on it would page someone
        every evening after the machinery did exactly what it was built to do."""
        user_id = await _user(session)
        await _order(
            session,
            user_id=user_id,
            symbol=f"T{state[:4]}",
            state=state,
            expires_after_session=DAY - dt.timedelta(days=30),
        )

        result = await check_orders_past_expiry(session, now=_at("21:40"), user_id=user_id)

        assert result["past_expiry"] == 0

    async def test_an_order_with_no_window_is_not_reported_as_late(
        self, session: AsyncSession
    ) -> None:
        """A null `expires_after_session` is a row the evening has not adopted yet, not a late
        one — the runbook's step 4 is what fixes it, and a page would be wrong."""
        user_id = await _user(session)
        await _order(session, user_id=user_id, symbol="UNSET", expires_after_session=None)

        result = await check_orders_past_expiry(session, now=_at("21:40"), user_id=user_id)

        assert result["past_expiry"] == 0

    async def test_another_users_late_order_is_not_this_sleeve(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        other = AppUser(public_id="vb7-other", email="other@example.com")
        session.add(other)
        await session.flush()
        await _order(
            session,
            user_id=int(other.id),
            symbol="THEIRS",
            expires_after_session=DAY - dt.timedelta(days=5),
        )

        result = await check_orders_past_expiry(session, now=_at("21:40"), user_id=user_id)

        assert result["past_expiry"] == 0

    async def test_it_stays_quiet_on_a_saturday(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _order(
            session,
            user_id=user_id,
            symbol="STALE",
            expires_after_session=SATURDAY - dt.timedelta(days=7),
        )

        result = await check_orders_past_expiry(
            session, now=_at("21:40", SATURDAY), user_id=user_id
        )

        assert result["checked"] is False

    async def test_the_check_writes_nothing(self, session: AsyncSession) -> None:
        """It tells; it does not cancel. The cancel is a confirmed line on the desk page."""
        user_id = await _user(session)
        order = await _order(
            session,
            user_id=user_id,
            symbol="STALE",
            expires_after_session=DAY - dt.timedelta(days=1),
        )
        before = order.state

        await check_orders_past_expiry(session, now=_at("21:40"), user_id=user_id)

        await session.refresh(order)
        assert order.state == before


class TestTheBookChecks:
    async def test_a_position_with_shares_and_no_gtt_raises(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _position(session, user_id=user_id, symbol="NAKED", gtt_id=None)
        await _position(session, user_id=user_id, symbol="COVERED")
        await _position(session, user_id=user_id, symbol="DONE", gtt_id=None, quantity_open=0)
        mailer = Recording()

        result = await check_naked_positions(
            session,
            now=_at("21:40"),
            user_id=user_id,
            settings=_settings(ops_alert_email="ops@example.com"),
            mailer=Mailer(mailer),
        )

        assert result["naked"] == 1
        alert = cast(dict[str, object], result["alert"])
        assert alert["alert"] == AlertName.VBT_POSITION_NAKED.value
        assert alert["severity"] == Severity.CRITICAL.value
        assert "1 volume-breakout position(s)" in mailer.sent[0].subject

    async def test_a_covered_book_is_silent(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _position(session, user_id=user_id, symbol="COVERED")

        result = await check_naked_positions(session, now=_at("21:40"), user_id=user_id)

        assert result == {"date": DAY.isoformat(), "checked": True, "naked": 0, "alert": None}

    async def test_a_held_name_that_stopped_printing_raises(self, session: AsyncSession) -> None:
        """`04` §6.5 writes it off after five blank sessions; a person sees it first."""
        user_id = await _user(session)
        silent = await _position(session, user_id=user_id, symbol="GONE")
        trading = await _position(session, user_id=user_id, symbol="ALIVE")
        await _bar(session, silent, DAY - dt.timedelta(days=20))
        await _bar(session, trading, DAY - dt.timedelta(days=1))

        result = await check_positions_without_bars(session, now=_at("21:40"), user_id=user_id)

        assert result["silent"] == 1
        alert = cast(dict[str, object], result["alert"])
        assert alert["alert"] == AlertName.VBT_POSITION_NO_BAR.value
        assert alert["severity"] == Severity.WARNING.value

    async def test_a_held_name_with_no_bar_at_all_is_silent_too(
        self, session: AsyncSession
    ) -> None:
        """The worst case reads as no rows, not as a fresh bar."""
        user_id = await _user(session)
        await _position(session, user_id=user_id, symbol="NOBARS")

        result = await check_positions_without_bars(session, now=_at("21:40"), user_id=user_id)

        assert result["silent"] == 1

    async def test_a_book_that_is_printing_is_silent(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        trading = await _position(session, user_id=user_id, symbol="ALIVE")
        await _bar(session, trading, DAY - dt.timedelta(days=1))

        result = await check_positions_without_bars(session, now=_at("21:40"), user_id=user_id)

        assert result == {"date": DAY.isoformat(), "checked": True, "silent": 0, "alert": None}

    async def test_an_empty_book_asks_no_questions(self, session: AsyncSession) -> None:
        user_id = await _user(session)

        result = await check_positions_without_bars(session, now=_at("21:40"), user_id=user_id)

        assert result["silent"] == 0


class TestTheDetectorCheck:
    async def test_a_detector_that_has_written_nothing_raises(self, session: AsyncSession) -> None:
        user_id = await _user(session)

        result = await check_detect_fresh(session, now=_at("21:30"), user_id=user_id)

        alert = cast(dict[str, object], result["alert"])
        assert alert["alert"] == AlertName.VBT_DETECT_STALE.value
        assert alert["severity"] == Severity.WARNING.value
        assert "never" in cast(str, alert["summary"])

    async def test_a_breadth_row_for_the_session_is_fresh(self, session: AsyncSession) -> None:
        """A session with no signals still writes its breadth row — that is the whole reason
        this check can tell "nothing qualified" from "the detector did not run"."""
        user_id = await _user(session)
        await _breadth(session, user_id, DAY)

        result = await check_detect_fresh(session, now=_at("21:30"), user_id=user_id)

        assert result["alert"] is None
        assert result["latest"] == DAY.isoformat()

    async def test_a_long_weekend_is_not_staleness(self, session: AsyncSession) -> None:
        """Four days of tolerance, because a Thursday-to-Monday holiday run is normal and
        paging for the exchange being shut is how a check earns its way into the filter."""
        user_id = await _user(session)
        await _breadth(session, user_id, DAY - dt.timedelta(days=4))

        result = await check_detect_fresh(session, now=_at("21:30"), user_id=user_id)

        assert result["alert"] is None

    async def test_a_week_of_silence_is(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _breadth(session, user_id, DAY - dt.timedelta(days=8))

        result = await check_detect_fresh(session, now=_at("21:30"), user_id=user_id)

        assert result["alert"] is not None


class TestTheWiring:
    @pytest.mark.parametrize(
        ("entry", "task", "hour", "minute"),
        [
            ("vbt-check-detect-fresh", "baskfy.vbt.check_detect_fresh", 21, 30),
            ("vbt-check-orders-past-expiry", "baskfy.vbt.check_orders_past_expiry", 21, 40),
            ("vbt-check-naked-positions", "baskfy.vbt.check_naked_positions", 21, 40),
            (
                "vbt-check-positions-without-bars",
                "baskfy.vbt.check_positions_without_bars",
                21,
                40,
            ),
        ],
    )
    def test_each_check_has_a_beat_entry_on_the_default_queue(
        self, entry: str, task: str, hour: int, minute: int
    ) -> None:
        row = BEAT_SCHEDULE[entry]
        schedule = cast(crontab, row["schedule"])
        assert row["task"] == task
        assert (schedule.hour, schedule.minute) == ({hour}, {minute})
        assert schedule.day_of_week == {1, 2, 3, 4, 5}
        assert row["options"] == {"queue": QUEUE_DEFAULT}
        assert TASK_ROUTES["baskfy.vbt.check_*"] == {"queue": QUEUE_DEFAULT}

    def test_every_vbt_alert_names_the_same_runbook_and_it_exists(self) -> None:
        vbt = {name for name in AlertName if name.value.startswith("VBT_")}
        assert len(vbt) == 4
        assert {RUNBOOKS[name] for name in vbt} == {"docs/runbooks/08-vbt-evening.md"}

    def test_the_checks_run_after_the_evening_that_is_supposed_to_fix_things(self) -> None:
        """A check at 21:00 would page about work the 21:15 evening was about to do."""
        evening = cast(crontab, BEAT_SCHEDULE["vbt-evening"]["schedule"])
        for entry in ("vbt-check-orders-past-expiry", "vbt-check-naked-positions"):
            check = cast(crontab, BEAT_SCHEDULE[entry]["schedule"])
            assert min(check.hour) * 60 + min(check.minute) > (
                min(evening.hour) * 60 + min(evening.minute)
            )


class TestNoCheckTouchesTheBook:
    async def test_none_of_the_four_issues_a_write(self, session: AsyncSession) -> None:
        """The sleeve's safety argument in one assertion: the worker plans, a person executes.

        Every check runs against a book in its worst state and the row counts do not move.
        """
        user_id = await _user(session)
        await _order(
            session,
            user_id=user_id,
            symbol="STALE",
            expires_after_session=DAY - dt.timedelta(days=1),
        )
        await _position(session, user_id=user_id, symbol="NAKED", gtt_id=None)
        counts_before = (
            (await session.execute(sa.select(sa.func.count()).select_from(VbOrder))).scalar_one(),
            (
                await session.execute(sa.select(sa.func.count()).select_from(VbPosition))
            ).scalar_one(),
        )

        for check in (
            check_orders_past_expiry,
            check_naked_positions,
            check_detect_fresh,
            check_positions_without_bars,
        ):
            await check(session, now=_at("21:40"), user_id=user_id)

        counts_after = (
            (await session.execute(sa.select(sa.func.count()).select_from(VbOrder))).scalar_one(),
            (
                await session.execute(sa.select(sa.func.count()).select_from(VbPosition))
            ).scalar_one(),
        )
        assert counts_after == counts_before
