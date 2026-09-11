"""TW7's safety property: **no session ends with an open TWT line and a null ``gtt_id``.**

``docs/twt/06`` § TW7's acceptance criterion asks for this over a *generated* book rather than a
spot check, and the reason is in ``FIRST-LIVE-MORNING`` §9.1: the failure has three causes, they
interleave, and the most likely one (a ratchet that cancelled and did not re-arm) fires on up to
ten lines a session for months. A hand-written scenario tests the ordering its author thought of.

So the book here is generated: fills arrive, ratchets strip stops off lines that had them, exits
close lines, and the sweep runs at the end of each session. The invariant asserted after every
sweep is non-negotiable 4 restated as a predicate::

    naked_lines(book) == ()      # after a sweep whose re-arm succeeded

and — the half that matters more, because the desk *can* refuse — when the re-arm does not
succeed, nothing is silently dropped: every line still naked is in ``still_naked`` **and** in a
``TWT_GTT_MISSING_AT_1515`` alert. A sweep is allowed to fail. It is not allowed to fail quietly.

Synchronous tests around ``asyncio.run``: Hypothesis drives the generation and the scenario owns
one event loop per example, which keeps the shrinker's reruns independent of the loop policy.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from baskfy_core.models.base import JsonObject
from baskfy_worker.alerts import Alert, AlertName

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
HARNESS: Final = REPO_ROOT.parent / "tools" / "twt"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from sweep import (  # noqa: E402 - after the sys.path insert above
    MemoryJournal,
    NakedLine,
    OpenLine,
    PositionId,
    RearmOutcome,
    is_naked,
    naked_lines,
    sweep,
)

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")
FIRST_SESSION: Final = dt.date(2026, 9, 14)
STOP: Final = Decimal("197.85")


class Event(StrEnum):
    """The three things ``FIRST-LIVE-MORNING`` §9.1 says happen to a stop, and one that does not.

    ``FILL`` is §9.1 case 1 — a buy filled and the arm did not, which is what leaves a brand-new
    line naked. ``RATCHET_CANCELLED`` is case 2, the likely one: the ``RAISE_GTT_STOP`` path is
    delete-and-replace and the replace failed, so a line that *had* a stop has none. ``EXIT``
    closes a line, which must stop the assertion applying to it. ``QUIET`` is a session in which
    nothing happened, which on this sleeve is most of them (eighteen entries a year).
    """

    FILL = "FILL"
    RATCHET_CANCELLED = "RATCHET_CANCELLED"
    EXIT = "EXIT"
    QUIET = "QUIET"


class Book:
    """A generated ``tw_position`` table, and the desk that arms into it.

    The re-arm mutates this book, exactly as TW6's will mutate the rows, so "the second sweep has
    nothing to arm" is a fact about the book and not about a mock.
    """

    def __init__(self, *, refuse_every: int) -> None:
        self.rows: dict[PositionId, OpenLine] = {}
        self.next_id = 1
        #: Refuse every n-th position id, so a generated run contains desk refusals as well as
        #: successes. ``0`` means the desk always arms.
        self.refuse_every = refuse_every
        self.armed: list[PositionId] = []

    def lines(self) -> tuple[OpenLine, ...]:
        return tuple(self.rows[key] for key in sorted(self.rows))

    def open_ids(self) -> list[PositionId]:
        return [key for key, row in self.rows.items() if row.state == "OPEN"]

    def fill(self) -> None:
        """A new line, with no stop yet — §9.1 case 1's shape."""
        position_id = self.next_id
        self.next_id += 1
        self.rows[position_id] = OpenLine(
            position_id=position_id,
            symbol=f"NAME{position_id}",
            state="OPEN",
            quantity_open=100,
            gtt_id=None,
            stop_price=STOP,
        )

    def ratchet_cancelled(self, seed: int) -> None:
        """§9.1 case 2: the cancel succeeded and the arm did not, so ``gtt_id`` is nulled."""
        candidates = [key for key in self.open_ids() if self.rows[key].gtt_id is not None]
        if not candidates:
            return
        chosen = candidates[seed % len(candidates)]
        self.rows[chosen] = replace(self.rows[chosen], gtt_id=None)

    def exit(self, seed: int) -> None:
        """A stop fired or a person sold: the line is closed and owns nothing."""
        candidates = self.open_ids()
        if not candidates:
            return
        chosen = candidates[seed % len(candidates)]
        self.rows[chosen] = replace(self.rows[chosen], state="CLOSED", quantity_open=0, gtt_id=None)

    def refuses(self, position_id: PositionId) -> bool:
        return self.refuse_every > 0 and position_id % self.refuse_every == 0

    async def rearm(self, position_id: PositionId) -> RearmOutcome:
        row = self.rows[position_id]
        # The sweep must never ask for a stop on a row the sleeve no longer owns shares in:
        # `04` §9.4's other side, and an order on a holding nobody holds. Asserted here, at the
        # seam, because that is where TW6's real path will see the same request.
        assert row.state == "OPEN", f"#{position_id} is {row.state}"
        assert row.quantity_open > 0, f"#{position_id} holds no shares"
        assert row.gtt_id is None, f"#{position_id} already has {row.gtt_id} resting"
        if self.refuses(position_id):
            return RearmOutcome(
                position_id, armed=False, reason="the trigger is at or above the last price"
            )
        gtt = f"gtt-{position_id}-{len(self.armed)}"
        self.armed.append(position_id)
        self.rows[position_id] = replace(self.rows[position_id], gtt_id=gtt)
        return RearmOutcome(position_id, armed=True, gtt_id=gtt)


class Recorder:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    async def __call__(self, alert: Alert) -> JsonObject:
        self.alerts.append(alert)
        return {"alert": alert.name.value}

    def positions_told_about(self, name: AlertName) -> set[PositionId]:
        told: set[PositionId] = set()
        for alert in self.alerts:
            if alert.name is not name:
                continue
            listed = alert.detail.get("still_naked", alert.detail.get("naked"))
            assert isinstance(listed, list)
            for entry in listed:
                assert isinstance(entry, dict)
                position_id = entry["position_id"]
                assert isinstance(position_id, int)
                told.add(position_id)
        return told


#: Sessions of events. Small on purpose: this sleeve holds ten lines at most (``04`` §6.1), and a
#: 4,000-position book would be testing numpy rather than the rule.
SESSIONS = st.lists(
    st.lists(st.sampled_from(list(Event)), min_size=0, max_size=6),
    min_size=1,
    max_size=8,
)
SEEDS = st.integers(min_value=0, max_value=97)

PROPERTY = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def apply(book: Book, events: list[Event], seed: int) -> None:
    for index, event in enumerate(events):
        if event is Event.FILL:
            book.fill()
        elif event is Event.RATCHET_CANCELLED:
            book.ratchet_cancelled(seed + index)
        elif event is Event.EXIT:
            book.exit(seed + index)


@dataclass(frozen=True, slots=True)
class Run:
    """One generated afternoon-by-afternoon: the book it left, the pages it sent, and — per
    session — what stayed naked and what was armed."""

    book: Book
    recorder: Recorder
    naked_after: tuple[tuple[PositionId, ...], ...]
    armed_per_session: tuple[tuple[PositionId, ...], ...]


async def _run_sessions(sessions: list[list[Event]], seed: int, *, refuse_every: int) -> Run:
    """Play the generated book, sweeping at 15:15 of every session."""
    book = Book(refuse_every=refuse_every)
    recorder = Recorder()
    journal = MemoryJournal()
    naked_after: list[tuple[PositionId, ...]] = []
    armed_per_session: list[tuple[PositionId, ...]] = []
    for offset, events in enumerate(sessions):
        apply(book, events, seed + offset)
        armed_before = len(book.armed)
        day = FIRST_SESSION + dt.timedelta(days=offset)
        result = await sweep(
            book.lines(),
            now=dt.datetime.combine(day, dt.time(15, 15), tzinfo=IST),
            rearm=book.rearm,
            journal=journal,
            dispatcher=recorder,
        )
        naked_after.append(tuple(entry.position_id for entry in result.still_naked))
        armed_per_session.append(tuple(book.armed[armed_before:]))
        _assert_result_agrees_with_the_book(book, result.still_naked)
    return Run(book, recorder, tuple(naked_after), tuple(armed_per_session))


def _assert_result_agrees_with_the_book(book: Book, still: tuple[NakedLine, ...]) -> None:
    """The result is only worth reading if it is the book's own answer, not a parallel count."""
    assert [entry.position_id for entry in still] == [
        entry.position_id for entry in naked_lines(book.lines())
    ]


class TestNoSessionEndsWithAnUnprotectedLine:
    """The property TW7 exists for, over a generated book rather than a scenario."""

    @given(sessions=SESSIONS, seed=SEEDS)
    @PROPERTY
    def test_no_naked_line_survives_a_session_the_sweep_ran(
        self, sessions: list[list[Event]], seed: int
    ) -> None:
        """With a desk that arms, every session ends with every open line protected.

        This is non-negotiable 4 as a predicate, and it is the live half of ``STOP_DAY0``: the
        backtest may take a position out at its stop on the fill day only because a GTT is
        actually resting there.
        """
        run = asyncio.run(_run_sessions(sessions, seed, refuse_every=0))
        assert run.naked_after == ((),) * len(sessions)
        assert naked_lines(run.book.lines()) == ()
        assert not [row for row in run.book.lines() if is_naked(row)]

    @given(sessions=SESSIONS, seed=SEEDS, refuse_every=st.integers(min_value=2, max_value=4))
    @PROPERTY
    def test_a_refusal_is_always_reported_and_no_naked_line_is_ever_silently_dropped(
        self, sessions: list[list[Event]], seed: int, refuse_every: int
    ) -> None:
        """The desk is allowed to refuse (§9.2 step 2). It is not allowed to refuse quietly.

        Every line the sweep could not arm is in ``still_naked`` **and** has been named in a
        ``TWT_GTT_MISSING_AT_1515`` page, so the person doing §9.2 knows which holding to open in
        the Kite app.
        """
        run = asyncio.run(_run_sessions(sessions, seed, refuse_every=refuse_every))
        left_naked = {entry.position_id for entry in naked_lines(run.book.lines())}
        ever_naked = {position for session in run.naked_after for position in session}
        told = run.recorder.positions_told_about(AlertName.TWT_GTT_MISSING_AT_1515)
        assert left_naked <= ever_naked
        assert ever_naked == told, "a line the sweep could not fix was never paged about"

    @given(sessions=SESSIONS, seed=SEEDS)
    @PROPERTY
    def test_every_naked_line_is_paged_about_at_the_moment_it_is_found(
        self, sessions: list[list[Event]], seed: int
    ) -> None:
        """``TWT_POSITION_NAKED`` is the "any naked line at any time" half of §8, so it fires for
        a line even on the sessions the sweep then successfully arms."""
        run = asyncio.run(_run_sessions(sessions, seed, refuse_every=0))
        armed = set(run.book.armed)
        told = run.recorder.positions_told_about(AlertName.TWT_POSITION_NAKED)
        assert armed <= told, "a line was armed without anyone being told it had been naked"

    @given(sessions=SESSIONS, seed=SEEDS)
    @PROPERTY
    def test_the_sweep_never_arms_a_line_twice_inside_one_session(
        self, sessions: list[list[Event]], seed: int
    ) -> None:
        """Two arms of one line in one sweep is a double-send — the failure
        ``client_id = plan_id:symbol`` exists to prevent (non-negotiable 6).

        A *later* session may arm the same line again, and that is correct: §9.1 case 2 nulls the
        stop of a line that had one. Within a session, never.
        """
        run = asyncio.run(_run_sessions(sessions, seed, refuse_every=0))
        for armed in run.armed_per_session:
            assert len(armed) == len(set(armed)), armed

    @given(sessions=SESSIONS, seed=SEEDS, refuse_every=st.integers(min_value=0, max_value=4))
    def test_no_naked_line_is_ever_asked_of_a_row_the_sleeve_no_longer_owns(
        self, sessions: list[list[Event]], seed: int, refuse_every: int
    ) -> None:
        """``Book.rearm`` asserts it at the seam: every re-arm request names an ``OPEN`` row with
        shares and no resting stop. This test is the generator that drives those assertions, and
        a sweep that asked for a stop on a closed line would fail inside the fake desk.
        """
        run = asyncio.run(_run_sessions(sessions, seed, refuse_every=refuse_every))
        closed_now = {row.position_id for row in run.book.lines() if row.state != "OPEN"}
        for row in run.book.lines():
            if row.position_id in closed_now:
                assert row.quantity_open == 0


class TestTheGeneratorActuallyGeneratesTheFailure:
    """A property that never sees a naked line passes for the wrong reason.

    These are not properties; they are the assertion that the generator above produces the state
    the properties are about. Without them, ``armed <= told`` and ``naked_after == ((),)*n`` are
    both true of a book in which nothing ever happened.
    """

    def test_a_run_of_fills_leaves_lines_naked_for_the_sweep_to_find(self) -> None:
        run = asyncio.run(
            _run_sessions([[Event.FILL, Event.FILL], [Event.FILL]], 0, refuse_every=0)
        )
        assert run.armed_per_session == ((1, 2), (3,))
        assert run.recorder.positions_told_about(AlertName.TWT_POSITION_NAKED) == {1, 2, 3}
        assert run.naked_after == ((), ())

    def test_a_ratchet_that_cancelled_strips_a_stop_the_sweep_puts_back(self) -> None:
        run = asyncio.run(
            _run_sessions(
                [[Event.FILL], [Event.RATCHET_CANCELLED], [Event.QUIET]], 0, refuse_every=0
            )
        )
        assert run.armed_per_session == ((1,), (1,), ())
        assert run.naked_after == ((), (), ())

    def test_a_desk_that_refuses_leaves_the_line_naked_and_pages_about_it(self) -> None:
        run = asyncio.run(_run_sessions([[Event.FILL, Event.FILL]], 0, refuse_every=2))
        assert run.naked_after == ((2,),)
        assert run.recorder.positions_told_about(AlertName.TWT_GTT_MISSING_AT_1515) == {2}

    def test_an_exit_takes_a_line_out_of_the_assertion(self) -> None:
        run = asyncio.run(
            _run_sessions([[Event.FILL], [Event.EXIT], [Event.QUIET]], 0, refuse_every=0)
        )
        assert run.book.rows[1].state == "CLOSED"
        assert run.naked_after == ((), (), ())
