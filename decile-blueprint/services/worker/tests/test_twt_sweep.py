"""TW7 — ``tools/twt/sweep.py``: the 15:15 chore, and the two alerts a person reacts to.

``docs/twt/04`` §7.4 is one rule measured two ways. ``test_twt_fill_day.py`` asserts the
backtest's half — ``STOP_DAY0``, to the tick. This file asserts the live book's half: **no open
line sleeps without a resting GTT**, which is non-negotiable 4 and the only thing that makes the
backtest's half true of money.

``docs/twt/FIRST-LIVE-MORNING.md`` §§8-9 is the specification here, and it was written before this
code: it tells Maulik to expect ``naked: 0``, to re-run the sweep by hand without worrying
("idempotent and keyed on the day"), to react to ``TWT_POSITION_NAKED`` at **any** time of day and
to ``TWT_GTT_MISSING_AT_1515`` when the sweep could not fix a line. Each of those four sentences is
a test below.

**Nothing here arms a real GTT.** The re-arm is the seam with TW6 — an injected
``Callable[[PositionId], Awaitable[RearmOutcome]]`` — and :class:`Desk` below is a fake that
mutates its own book exactly as the real path will mutate ``tw_position``. That is what makes the
idempotence claim a claim about the algorithm rather than about a mock's call count.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from baskfy_core.models.base import JsonObject
from baskfy_worker.alerts import Alert, AlertName, Severity
from baskfy_worker.ops import RUNBOOKS

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
HARNESS: Final = REPO_ROOT.parent / "tools" / "twt"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from sweep import (  # noqa: E402 - after the sys.path insert above
    CLOSE_IST,
    RUNBOOK,
    SWEEP_IST,
    FileJournal,
    MemoryJournal,
    OpenLine,
    PositionId,
    RearmOutcome,
    build_rearm,
    is_naked,
    missing_at_1515_alert,
    naked_alert,
    naked_lines,
    sweep,
    unavailable_rearm,
)

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")
DAY: Final = dt.date(2026, 9, 11)
TOMORROW: Final = dt.date(2026, 9, 14)
STOP: Final = Decimal("197.85")


def at(hour: int, minute: int, day: dt.date = DAY) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)


def line(
    position_id: PositionId,
    *,
    gtt: str | None = None,
    quantity_open: int = 100,
    state: str = "OPEN",
) -> OpenLine:
    return OpenLine(
        position_id=position_id,
        symbol=f"NAME{position_id}",
        state=state,
        quantity_open=quantity_open,
        gtt_id=gtt,
        stop_price=STOP,
    )


class Recorder:
    """Where an alert goes instead of an inbox. ``dispatch`` never raises and neither does this."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    async def __call__(self, alert: Alert) -> JsonObject:
        self.alerts.append(alert)
        return {"alert": alert.name.value, "delivered_to": ["test"]}

    def names(self) -> list[AlertName]:
        return [alert.name for alert in self.alerts]


class Desk:
    """TW6's GTT path, faked — and it **mutates the book**, which is the point.

    A mock that only counted calls would let "re-arms nothing twice" be true of the mock and false
    of the desk. This one arms into ``self.book``, so the second sweep reads a book in which the
    line is no longer naked, exactly as the second sweep of a real afternoon would.
    """

    def __init__(self, lines: list[OpenLine], *, refuse: set[PositionId] | None = None) -> None:
        self.book = {entry.position_id: entry for entry in lines}
        self.refuse = refuse or set()
        self.calls: list[PositionId] = []

    def lines(self) -> tuple[OpenLine, ...]:
        return tuple(self.book[key] for key in sorted(self.book))

    async def rearm(self, position_id: PositionId) -> RearmOutcome:
        self.calls.append(position_id)
        if position_id in self.refuse:
            return RearmOutcome(
                position_id, armed=False, reason="the trigger is at or above the last price"
            )
        armed = f"gtt-{position_id}"
        self.book[position_id] = replace(self.book[position_id], gtt_id=armed)
        return RearmOutcome(position_id, armed=True, gtt_id=armed)


# ---------------------------------------------------------------------------


class TestTheAssertionItself:
    """``03`` §5: ``gtt_id`` null with ``quantity_open > 0`` on an ``OPEN`` row. Nothing else."""

    def test_an_open_line_with_shares_and_no_gtt_is_naked(self) -> None:
        assert is_naked(line(1)) is True

    def test_a_line_with_a_resting_gtt_is_not(self) -> None:
        assert is_naked(line(1, gtt="gtt-1")) is False

    def test_a_closed_line_is_not_naked_whatever_its_gtt_says(self) -> None:
        assert is_naked(line(1, state="CLOSED")) is False

    def test_an_open_line_with_no_shares_left_needs_no_stop(self) -> None:
        """``quantity_open > 0`` and not ``>= 0``: a row mid-close owns nothing to protect."""
        assert is_naked(line(1, quantity_open=0)) is False

    def test_naked_lines_are_returned_in_position_order_so_two_runs_read_alike(self) -> None:
        found = naked_lines([line(7), line(2, gtt="g"), line(4)])
        assert [entry.position_id for entry in found] == [4, 7]


class TestTheSweepArmsWhatIsNaked:
    async def test_it_arms_every_naked_line_and_reports_naked_zero(self) -> None:
        desk = Desk([line(1), line(2, gtt="gtt-2"), line(3)])
        result = await sweep(desk.lines(), now=at(15, 15), rearm=desk.rearm, dispatcher=Recorder())
        assert result.rearmed == (1, 3)
        assert result.naked == 0
        assert result.clean is True
        assert naked_lines(desk.lines()) == ()

    async def test_a_line_that_already_has_a_gtt_is_never_touched(self) -> None:
        """The sweep is not a re-arm-everything job. Cancelling and replacing a resting stop is
        the one thing this sleeve must never do on its own (``04`` §7.3)."""
        desk = Desk([line(1, gtt="gtt-1"), line(2, gtt="gtt-2")])
        result = await sweep(desk.lines(), now=at(15, 15), rearm=desk.rearm, dispatcher=Recorder())
        assert desk.calls == []
        assert result.naked_before == ()
        assert result.alerts == ()

    async def test_what_it_could_not_arm_is_reported_with_the_desks_own_refusal(self) -> None:
        desk = Desk([line(1), line(2)], refuse={2})
        result = await sweep(desk.lines(), now=at(15, 15), rearm=desk.rearm, dispatcher=Recorder())
        assert result.rearmed == (1,)
        assert [entry.position_id for entry in result.still_naked] == [2]
        assert "at or above the last price" in result.still_naked[0].reason

    async def test_a_rearm_that_claims_success_without_a_gtt_id_is_not_believed(self) -> None:
        """``armed=True`` and no id is a line with nothing resting at the exchange. Believing it
        is how a sweep reports ``naked: 0`` over an unprotected book."""

        async def liar(position_id: PositionId) -> RearmOutcome:
            return RearmOutcome(position_id, armed=True, gtt_id=None)

        result = await sweep([line(1)], now=at(15, 15), rearm=liar, dispatcher=Recorder())
        assert result.naked == 1
        assert "without a gtt_id" in result.still_naked[0].reason


class TestTheSweepIsIdempotentAndKeyedOnTheDay:
    """``FIRST-LIVE-MORNING`` §8: "It is idempotent and keyed on the day, so running it twice is
    safe." Two halves, and they are idempotent for different reasons."""

    async def test_a_second_run_is_idempotent_and_rearms_nothing_twice(self) -> None:
        """By construction, not by bookkeeping: the arm changed the book, so the second run has
        nothing naked to arm. This is the half that would still hold against a real database."""
        desk = Desk([line(1), line(2)])
        journal = MemoryJournal()
        recorder = Recorder()
        first = await sweep(
            desk.lines(), now=at(15, 15), rearm=desk.rearm, journal=journal, dispatcher=recorder
        )
        second = await sweep(
            desk.lines(), now=at(15, 20), rearm=desk.rearm, journal=journal, dispatcher=recorder
        )
        assert first.rearmed == (1, 2)
        assert second.rearmed == ()
        assert desk.calls == [1, 2], "each line was armed once, by the first run"

    async def test_a_second_run_is_idempotent_and_raises_nothing_twice(self) -> None:
        desk = Desk([line(1)], refuse={1})
        journal = MemoryJournal()
        recorder = Recorder()
        first = await sweep(
            desk.lines(), now=at(15, 15), rearm=desk.rearm, journal=journal, dispatcher=recorder
        )
        second = await sweep(
            desk.lines(), now=at(15, 20), rearm=desk.rearm, journal=journal, dispatcher=recorder
        )
        assert first.alerts == (AlertName.TWT_POSITION_NAKED, AlertName.TWT_GTT_MISSING_AT_1515)
        assert second.alerts == ()
        assert second.suppressed == (
            AlertName.TWT_POSITION_NAKED,
            AlertName.TWT_GTT_MISSING_AT_1515,
        )
        assert recorder.names() == [
            AlertName.TWT_POSITION_NAKED,
            AlertName.TWT_GTT_MISSING_AT_1515,
        ]
        assert second.naked == 1, "suppressing the page never suppresses the fact"

    async def test_a_line_that_goes_naked_again_later_the_same_day_still_pages(self) -> None:
        """The suppression is per position, never per day.

        §9.1 case 2 — a ratchet that cancelled and did not re-arm — is the *most likely* failure
        on this sleeve and it happens on up to ten lines a session. A journal keyed on the day
        alone would swallow the second one.
        """
        desk = Desk([line(1), line(2, gtt="gtt-2")], refuse={1, 2})
        journal = MemoryJournal()
        recorder = Recorder()
        await sweep(
            desk.lines(), now=at(15, 15), rearm=desk.rearm, journal=journal, dispatcher=recorder
        )
        desk.book[2] = replace(desk.book[2], gtt_id=None)  # the ratchet cancelled and failed
        second = await sweep(
            desk.lines(), now=at(15, 25), rearm=desk.rearm, journal=journal, dispatcher=recorder
        )
        assert second.alerts == (AlertName.TWT_POSITION_NAKED, AlertName.TWT_GTT_MISSING_AT_1515)
        assert [entry.position_id for entry in second.still_naked] == [1, 2]

    async def test_the_journal_is_keyed_on_the_day_so_tomorrow_pages_again(self) -> None:
        desk = Desk([line(1)], refuse={1})
        journal = MemoryJournal()
        recorder = Recorder()
        await sweep(
            desk.lines(), now=at(15, 15), rearm=desk.rearm, journal=journal, dispatcher=recorder
        )
        tomorrow = await sweep(
            desk.lines(),
            now=at(15, 15, TOMORROW),
            rearm=desk.rearm,
            journal=journal,
            dispatcher=recorder,
        )
        assert tomorrow.day == TOMORROW
        assert tomorrow.alerts == (
            AlertName.TWT_POSITION_NAKED,
            AlertName.TWT_GTT_MISSING_AT_1515,
        )

    async def test_the_file_journal_makes_a_second_shell_idempotent_too(
        self, tmp_path: Path
    ) -> None:
        """``uv run python tools/twt/sweep.py`` twice is two processes, so the day key has to
        outlive one of them."""
        desk = Desk([line(1)], refuse={1})
        recorder = Recorder()
        first = await sweep(
            desk.lines(),
            now=at(15, 15),
            rearm=desk.rearm,
            journal=FileJournal(tmp_path / "sweep"),
            dispatcher=recorder,
        )
        second = await sweep(
            desk.lines(),
            now=at(15, 16),
            rearm=desk.rearm,
            journal=FileJournal(tmp_path / "sweep"),
            dispatcher=recorder,
        )
        assert first.alerts != ()
        assert second.alerts == ()
        assert (tmp_path / "sweep" / f"{DAY.isoformat()}.json").is_file()

    def test_an_empty_state_directory_is_not_an_error(self, tmp_path: Path) -> None:
        journal = FileJournal(tmp_path / "never-written")
        assert journal.already_alerted(DAY, AlertName.TWT_POSITION_NAKED) == frozenset()


class TestTheTwoAlerts:
    """§8: "**`TWT_POSITION_NAKED` fires on any naked line at any time of day**, not only at
    15:15" — and `TWT_GTT_MISSING_AT_1515` for what the sweep could not fix."""

    def test_the_naked_alert_fires_at_any_hour_and_not_only_at_the_sweep(self) -> None:
        for hour, minute in ((9, 20), (11, 5), (15, 15), (21, 40)):
            alert = naked_alert(naked_lines([line(1)]), at=at(hour, minute))
            assert alert is not None
            assert alert.name is AlertName.TWT_POSITION_NAKED
            assert alert.severity is Severity.CRITICAL

    def test_the_naked_alert_is_silent_on_a_protected_book(self) -> None:
        assert naked_alert(naked_lines([line(1, gtt="gtt-1")]), at=at(15, 15)) is None

    def test_the_naked_alert_names_the_line_a_person_has_to_act_on(self) -> None:
        alert = naked_alert(naked_lines([line(4)]), at=at(15, 15))
        assert alert is not None
        assert "NAME4" in alert.summary and "#4" in alert.summary
        assert alert.labels["trade_date"] == DAY.isoformat()
        assert alert.detail["naked"] == [
            {
                "position_id": 4,
                "symbol": "NAME4",
                "quantity_open": 100,
                "stop_price": str(STOP),
                "reason": "",
            }
        ]

    def test_missing_at_1515_is_silent_when_the_sweep_fixed_everything(self) -> None:
        assert missing_at_1515_alert((), at=at(15, 15), before_close=True) is None

    def test_missing_at_1515_says_there_is_still_time_before_the_close(self) -> None:
        alert = missing_at_1515_alert(naked_lines([line(1)]), at=at(15, 15), before_close=True)
        assert alert is not None
        assert alert.name is AlertName.TWT_GTT_MISSING_AT_1515
        assert "before the 15:30 close" in alert.summary

    def test_missing_at_1515_says_so_when_the_close_has_already_passed(self) -> None:
        alert = missing_at_1515_alert(naked_lines([line(1)]), at=at(15, 31), before_close=False)
        assert alert is not None
        assert "already past the 15:30 close" in alert.summary
        assert alert.detail["before_close"] is False

    def test_both_names_a_runbook_that_exists(self) -> None:
        """The runbook is what makes the page actionable; §9 is the whole reason both exist."""
        assert RUNBOOKS[AlertName.TWT_POSITION_NAKED] == RUNBOOK
        assert RUNBOOKS[AlertName.TWT_GTT_MISSING_AT_1515] == RUNBOOK
        assert (REPO_ROOT / RUNBOOK).is_file(), RUNBOOK

    async def test_the_sweep_dispatches_both_through_the_injected_sink(self) -> None:
        desk = Desk([line(1)], refuse={1})
        recorder = Recorder()
        await sweep(desk.lines(), now=at(15, 15), rearm=desk.rearm, dispatcher=recorder)
        assert recorder.names() == [
            AlertName.TWT_POSITION_NAKED,
            AlertName.TWT_GTT_MISSING_AT_1515,
        ]
        assert {alert.runbook for alert in recorder.alerts} == {RUNBOOK}


class TestTheDeadlineIsBeforeTheClose:
    """ "every ``OPEN`` position with ``quantity_open > 0`` has a non-null ``gtt_id`` **before
    15:30**" — so the sweep has to know which side of 15:30 it is on."""

    def test_the_chore_is_at_1515_and_the_deadline_is_the_exchanges_close(self) -> None:
        assert dt.time(15, 15) == SWEEP_IST
        assert dt.time(15, 30) == CLOSE_IST
        assert SWEEP_IST < CLOSE_IST, "the chore leaves time to fix what it finds"

    @pytest.mark.parametrize(
        ("hour", "minute", "before"),
        [(15, 15, True), (15, 29, True), (15, 30, False), (15, 31, False), (18, 0, False)],
    )
    async def test_the_result_says_which_side_of_the_close_it_ran_on(
        self, hour: int, minute: int, before: bool
    ) -> None:
        result = await sweep([line(1, gtt="g")], now=at(hour, minute), dispatcher=Recorder())
        assert result.before_close is before

    async def test_the_clock_is_passed_in_rather_than_read(self) -> None:
        """A function that read ``datetime.now()`` could not be tested against 15:29 and 15:31,
        and the deadline it asserts is exactly that boundary."""
        result = await sweep([line(1, gtt="g")], now=at(15, 29), dispatcher=Recorder())
        assert result.at == at(15, 29)
        assert result.day == DAY


class TestTheSeamWithTw6:
    """The re-arm is injected. Until TW6's path lands the placeholder refuses **loudly**."""

    async def test_the_placeholder_refuses_and_leaves_the_line_in_still_naked(self) -> None:
        outcome = await unavailable_rearm(1)
        assert outcome.armed is False
        assert "TW6" in outcome.reason

    async def test_the_default_rearm_never_reports_a_book_it_did_not_protect(self) -> None:
        """A placeholder that answered ``armed=True`` would be worse than no sweep at all."""
        recorder = Recorder()
        result = await sweep([line(1)], now=at(15, 15), dispatcher=recorder)
        assert result.rearmed == ()
        assert result.naked == 1
        assert AlertName.TWT_GTT_MISSING_AT_1515 in recorder.names()

    def test_build_rearm_is_the_placeholder_and_the_swap_is_one_line(self) -> None:
        assert build_rearm() is unavailable_rearm

    async def test_the_sweep_writes_nothing_to_the_book_itself(self) -> None:
        """Law 2 and ``docs/twt/02`` Track C: this module reads and asks; TW6 writes."""
        before = (line(1), line(2, gtt="gtt-2"))
        await sweep(before, now=at(15, 15), dispatcher=Recorder())
        assert before == (line(1), line(2, gtt="gtt-2"))
