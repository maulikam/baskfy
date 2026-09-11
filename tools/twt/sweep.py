"""TW7 — the 15:15 sweep: **no TWT line sleeps unprotected** (``docs/twt/04`` §7.4).

    "In the live book the GTT armed in the same session **is** this rule, which is why
     non-negotiable 4 is not negotiable here: a fill without a same-session GTT is a position with
     no fill-day protection at all. TW7 is that module, and its sweep asserts that every open line
     has a resting GTT before 15:30."

The backtest's ``STOP_DAY0`` and this sweep are **one rule measured two ways**. The engine can
reach into a session's low and take the position out at the stop; the exchange cannot, unless a
GTT is actually resting there. So the backtest's guarantee is only true of the live book while
this sweep's assertion holds, and that assertion is the whole of this module::

    every tw_position with state OPEN and quantity_open > 0 has a non-null gtt_id, before 15:30

What it does, in order (``docs/twt/FIRST-LIVE-MORNING.md`` §8-9, which was written *before* this
code and is therefore the specification):

1. read the book;
2. ``TWT_POSITION_NAKED`` for any naked line — **at any time of day**, not only at 15:15;
3. re-arm each naked line through the desk's own GTT path;
4. ``TWT_GTT_MISSING_AT_1515`` for anything it could not fix, so a person does §9.2 by hand;
5. report ``naked: 0``, or say loudly that it is not.

The seam with TW6, stated once
------------------------------
**This module arms nothing itself.** Law 2 — ``packages/execution`` is the only path to an order
— and TW6 owns the desk's GTT paths. The re-arm arrives here as an injected callable::

    rearm: Callable[[PositionId], Awaitable[RearmOutcome]]

:func:`unavailable_rearm` is the placeholder the CLI wires until TW6's real one lands, and it
fails every line **loudly** rather than quietly reporting a clean book: a sweep that says
``naked: 0`` because it never tried is the exact failure this module exists to prevent. Swapping
in TW6's path is one line in :func:`build_rearm`.

Idempotent, and keyed on the day
--------------------------------
Running it twice re-arms nothing twice and raises nothing twice.

*Re-arming* is idempotent by construction rather than by bookkeeping: the sweep re-reads the book
every run, and a line that was armed is no longer naked, so there is nothing to arm. A line whose
re-arm **failed** is still naked and is tried again, which is right — nothing was placed, and a
retry cannot double-send.

*Alerting* is idempotent by a journal keyed on ``(day, alert, position)``:
:class:`FileJournal` for the CLI (one JSON file per day under the state directory) and
:class:`MemoryJournal` for a process that owns its own run. A line that goes naked *again* later
in the same day is a new fault and does alert again — the suppression is per position, never per
day, because "we already paged about something today" is how a second naked line goes unnoticed.

Nothing here writes to the book. It reads ``tw_position`` and calls the injected re-arm; every
row-level consequence of a re-arm is TW6's to write, inside TW6's transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol

from sqlalchemy import select

from baskfy_api.metrics import IST, NSE_CLOSE_IST
from baskfy_core.models import Instrument
from baskfy_core.models.base import JsonObject
from baskfy_core.models.twt import TwPosition
from baskfy_worker.alerts import Alert, AlertName, Severity, dispatch
from baskfy_worker.db import session_scope
from baskfy_worker.ops import RUNBOOKS

__all__ = [
    "CLOSE_IST",
    "RUNBOOK",
    "SWEEP_IST",
    "FileJournal",
    "MemoryJournal",
    "NakedLine",
    "OpenLine",
    "PositionId",
    "Rearm",
    "RearmOutcome",
    "SweepJournal",
    "SweepResult",
    "build_rearm",
    "is_naked",
    "main",
    "missing_at_1515_alert",
    "naked_alert",
    "naked_lines",
    "sweep",
    "unavailable_rearm",
]

#: A ``tw_position.id``. Named, because the seam with TW6 is spelled in terms of it.
PositionId = int

#: The chore's own time, and the deadline it is measured against. 15:15 is a quarter of an hour
#: of room before the exchange stops taking orders; ``NSE_CLOSE_IST`` is the desk's own constant
#: for the close, read from ``baskfy_api.metrics`` rather than restated here.
SWEEP_IST: Final = dt.time(15, 15)
CLOSE_IST: Final = NSE_CLOSE_IST

#: The runbook both alerts name, read from ``ops.RUNBOOKS`` so the two cannot disagree.
RUNBOOK: Final = RUNBOOKS[AlertName.TWT_GTT_MISSING_AT_1515]

#: ``tw_position.state`` for a line the sleeve still owns (``docs/twt/03`` §5).
OPEN: Final = "OPEN"


@dataclass(frozen=True, slots=True)
class OpenLine:
    """One row of the book, reduced to what the assertion is about.

    Deliberately not the ORM row: the rule reads three columns and a sweep that took a
    :class:`~baskfy_core.models.twt.TwPosition` would be untestable without a database, which is
    the same as untested.
    """

    position_id: PositionId
    symbol: str
    state: str
    quantity_open: int
    gtt_id: str | None
    #: The level a re-arm would rest at. Carried for the alert's body — a person reading
    #: ``FIRST-LIVE-MORNING`` §9.2 step 3 types this number into Kite by hand.
    stop_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class NakedLine:
    """A line with shares open and no resting stop, and why it is still that way."""

    position_id: PositionId
    symbol: str
    quantity_open: int
    stop_price: Decimal | None
    reason: str = ""

    def as_dict(self) -> JsonObject:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "quantity_open": self.quantity_open,
            "stop_price": None if self.stop_price is None else str(self.stop_price),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RearmOutcome:
    """What TW6's GTT path answered for one position.

    ``armed`` is the only field this module branches on. ``gtt_id`` is echoed so the result can
    be read without a second query, and ``reason`` is the refusal text ``FIRST-LIVE-MORNING``
    §9.2 step 2 tells a person to read — "the trigger is at or above the last price" and
    "an untouchable instrument" want completely different hands.
    """

    position_id: PositionId
    armed: bool
    gtt_id: str | None = None
    reason: str = ""


#: **The seam with TW6.** One position in, one outcome out; it arms, it does not decide.
Rearm = Callable[[PositionId], Awaitable[RearmOutcome]]

#: How an alert leaves this module. ``baskfy_worker.alerts.dispatch`` is the default and a fake
#: is what the tests pass, because an assertion about "the alert fired" should not depend on
#: whether an SMTP server answered.
Dispatch = Callable[[Alert], Awaitable[JsonObject]]


async def unavailable_rearm(position_id: PositionId) -> RearmOutcome:
    """The re-arm before TW6 lands: it refuses, loudly, and every line stays in ``still_naked``.

    A placeholder that returned ``armed=True`` would make this sweep report a protected book it
    had done nothing to protect, which is worse than no sweep at all.
    """
    return RearmOutcome(
        position_id=position_id,
        armed=False,
        reason=(
            "the desk's TWT re-arm path is not wired yet (TW6); arm the stop by hand — "
            "FIRST-LIVE-MORNING §9.2 step 3"
        ),
    )


# ---------------------------------------------------------------------------
# The assertion
# ---------------------------------------------------------------------------


def is_naked(line: OpenLine) -> bool:
    """``state == OPEN and quantity_open > 0 and gtt_id is None`` — the whole rule.

    ``quantity_open > 0`` and not ``>= 0``: a closed-out row that still says ``OPEN`` for a
    moment owns no shares and needs no stop. ``03`` §5's column comment is this predicate.
    """
    return line.state == OPEN and line.quantity_open > 0 and line.gtt_id is None


def naked_lines(lines: Iterable[OpenLine]) -> tuple[NakedLine, ...]:
    """Every line the assertion refuses, in position order so two runs read alike."""
    return tuple(
        NakedLine(
            position_id=line.position_id,
            symbol=line.symbol,
            quantity_open=line.quantity_open,
            stop_price=line.stop_price,
        )
        for line in sorted((line for line in lines if is_naked(line)), key=_by_id)
    )


def _by_id(line: OpenLine) -> PositionId:
    return line.position_id


# ---------------------------------------------------------------------------
# The two alerts
# ---------------------------------------------------------------------------


def naked_alert(naked: Sequence[NakedLine], *, at: dt.datetime) -> Alert | None:
    """``TWT_POSITION_NAKED`` — any naked line, **at any time**. ``None`` when the book is clean.

    Not keyed on 15:15: ``FIRST-LIVE-MORNING`` §8 says so in bold, and the reason is that the
    most likely cause (§9.1 case 2 — a ratchet that cancelled and did not re-arm) happens
    whenever the morning's ``RAISE_GTT_STOP`` is confirmed, which is nowhere near the close.
    """
    if not naked:
        return None
    ist = at.astimezone(IST)
    return Alert(
        name=AlertName.TWT_POSITION_NAKED,
        severity=Severity.CRITICAL,
        summary=(
            f"{len(naked)} TWT position(s) have shares open and no resting GTT at "
            f"{ist.isoformat(timespec='seconds')} — {_symbols(naked)}."
        ),
        labels={"trade_date": ist.date().isoformat()},
        detail={"naked": [line.as_dict() for line in naked], "checked_at": ist.isoformat()},
        runbook=RUNBOOK,
    )


def missing_at_1515_alert(
    still_naked: Sequence[NakedLine], *, at: dt.datetime, before_close: bool
) -> Alert | None:
    """``TWT_GTT_MISSING_AT_1515`` — what the sweep could **not** fix. ``None`` when it fixed all.

    The summary says whether there is still time, because the two situations want different
    speed from the person reading it: before 15:30 the answer is §9.2's three steps in the Kite
    app; after it, the book is already going into the night unprotected and step 4 — close the
    line — is on the table.
    """
    if not still_naked:
        return None
    ist = at.astimezone(IST)
    when = (
        f"before the {CLOSE_IST.isoformat(timespec='minutes')} close"
        if before_close
        else f"and it is already past the {CLOSE_IST.isoformat(timespec='minutes')} close"
    )
    return Alert(
        name=AlertName.TWT_GTT_MISSING_AT_1515,
        severity=Severity.CRITICAL,
        summary=(
            f"the 15:15 sweep could not arm a stop for {len(still_naked)} TWT position(s) "
            f"{when} on {ist.date().isoformat()} — {_symbols(still_naked)}."
        ),
        labels={"trade_date": ist.date().isoformat()},
        detail={
            "still_naked": [line.as_dict() for line in still_naked],
            "before_close": before_close,
            "checked_at": ist.isoformat(),
        },
        runbook=RUNBOOK,
    )


def _symbols(lines: Sequence[NakedLine]) -> str:
    return ", ".join(f"{line.symbol} (#{line.position_id})" for line in lines)


# ---------------------------------------------------------------------------
# The day key
# ---------------------------------------------------------------------------


class SweepJournal(Protocol):
    """What this day has already been told about, so a second run does not page twice.

    Keyed on ``(day, alert, position)`` and never on the day alone: a second line going naked at
    15:25 is a **new** fault, and a journal that suppressed it because something else had already
    alerted that day is how a naked position goes into the night unnoticed.
    """

    def already_alerted(self, day: dt.date, alert: AlertName) -> frozenset[PositionId]: ...

    def record_alerted(
        self, day: dt.date, alert: AlertName, positions: Iterable[PositionId]
    ) -> None: ...


@dataclass(slots=True)
class MemoryJournal:
    """One process's memory of its own day. What the tests use, and what a long-lived worker
    would use between two runs of the same Beat entry."""

    seen: dict[tuple[dt.date, AlertName], set[PositionId]] = field(default_factory=dict)

    def already_alerted(self, day: dt.date, alert: AlertName) -> frozenset[PositionId]:
        return frozenset(self.seen.get((day, alert), set()))

    def record_alerted(
        self, day: dt.date, alert: AlertName, positions: Iterable[PositionId]
    ) -> None:
        self.seen.setdefault((day, alert), set()).update(positions)


@dataclass(slots=True)
class FileJournal:
    """The CLI's day key: one JSON file per day, so two shells on one afternoon agree.

    A file and not a column, deliberately. The durable alternative is a migration on
    ``tw_session``, and TW6 is writing that table in a parallel session; a day key that is a file
    under an untracked ``data/`` directory buys the same idempotency without two agents editing
    one schema. ``docs/twt/DECISIONS-TW.md`` **TW7.2** records it, and names the column to add if
    the sweep ever needs to be idempotent across machines rather than across runs.
    """

    directory: Path

    def _path(self, day: dt.date) -> Path:
        return self.directory / f"{day.isoformat()}.json"

    def _load(self, day: dt.date) -> dict[str, list[int]]:
        path = self._path(day)
        if not path.is_file():
            return {}
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return {}
        out: dict[str, list[int]] = {}
        for key, value in loaded.items():
            if isinstance(key, str) and isinstance(value, list):
                out[key] = [int(entry) for entry in value if isinstance(entry, int)]
        return out

    def already_alerted(self, day: dt.date, alert: AlertName) -> frozenset[PositionId]:
        return frozenset(self._load(day).get(alert.value, []))

    def record_alerted(
        self, day: dt.date, alert: AlertName, positions: Iterable[PositionId]
    ) -> None:
        current = self._load(day)
        merged = sorted(set(current.get(alert.value, [])) | set(positions))
        current[alert.value] = merged
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path(day).write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one run did. ``naked`` is the number ``FIRST-LIVE-MORNING`` §8 tells a person to read,
    and it is the count **after** the re-arm, because that is the one that means anything."""

    day: dt.date
    at: dt.datetime
    open_lines: int
    naked_before: tuple[NakedLine, ...]
    rearmed: tuple[PositionId, ...]
    still_naked: tuple[NakedLine, ...]
    alerts: tuple[AlertName, ...]
    #: Alerts this run did **not** raise because this day had already been told about exactly
    #: these positions. The honest half of "raises nothing twice".
    suppressed: tuple[AlertName, ...]
    before_close: bool

    @property
    def naked(self) -> int:
        return len(self.still_naked)

    @property
    def clean(self) -> bool:
        return not self.still_naked

    def as_dict(self) -> JsonObject:
        return {
            "date": self.day.isoformat(),
            "at": self.at.astimezone(IST).isoformat(timespec="seconds"),
            "open_lines": self.open_lines,
            "naked_before": [line.as_dict() for line in self.naked_before],
            "rearmed": list(self.rearmed),
            "naked": self.naked,
            "still_naked": [line.as_dict() for line in self.still_naked],
            "alerts": [name.value for name in self.alerts],
            "suppressed": [name.value for name in self.suppressed],
            "before_close": self.before_close,
        }


async def sweep(
    lines: Sequence[OpenLine],
    *,
    now: dt.datetime,
    rearm: Rearm = unavailable_rearm,
    journal: SweepJournal | None = None,
    dispatcher: Dispatch = dispatch,
) -> SweepResult:
    """The 15:15 chore. Read the book, alert on what is naked, re-arm it, alert on what is left.

    ``now`` is passed in rather than read: the deadline this asserts is a wall-clock one, and a
    function that reads the clock cannot be tested against 15:29 and 15:31.
    """
    book = journal if journal is not None else MemoryJournal()
    ist = now.astimezone(IST)
    day = ist.date()
    before_close = ist.time() < CLOSE_IST

    naked = naked_lines(lines)
    raised: list[AlertName] = []
    suppressed: list[AlertName] = []

    await _raise(
        naked_alert(naked, at=now),
        positions=[line.position_id for line in naked],
        day=day,
        name=AlertName.TWT_POSITION_NAKED,
        journal=book,
        dispatcher=dispatcher,
        raised=raised,
        suppressed=suppressed,
    )

    rearmed: list[PositionId] = []
    still: list[NakedLine] = []
    for line in naked:
        outcome = await rearm(line.position_id)
        if outcome.armed and outcome.gtt_id is not None:
            rearmed.append(line.position_id)
            continue
        reason = outcome.reason or (
            "the re-arm reported success without a gtt_id, so nothing is resting at the exchange"
            if outcome.armed
            else "the re-arm was refused and gave no reason"
        )
        still.append(
            NakedLine(
                position_id=line.position_id,
                symbol=line.symbol,
                quantity_open=line.quantity_open,
                stop_price=line.stop_price,
                reason=reason,
            )
        )

    await _raise(
        missing_at_1515_alert(tuple(still), at=now, before_close=before_close),
        positions=[line.position_id for line in still],
        day=day,
        name=AlertName.TWT_GTT_MISSING_AT_1515,
        journal=book,
        dispatcher=dispatcher,
        raised=raised,
        suppressed=suppressed,
    )

    return SweepResult(
        day=day,
        at=now,
        open_lines=sum(1 for line in lines if line.state == OPEN and line.quantity_open > 0),
        naked_before=naked,
        rearmed=tuple(rearmed),
        still_naked=tuple(still),
        alerts=tuple(raised),
        suppressed=tuple(suppressed),
        before_close=before_close,
    )


async def _raise(  # noqa: PLR0913 - it is one statement per collaborator, and all seven are read
    alert: Alert | None,
    *,
    positions: Sequence[PositionId],
    day: dt.date,
    name: AlertName,
    journal: SweepJournal,
    dispatcher: Dispatch,
    raised: list[AlertName],
    suppressed: list[AlertName],
) -> None:
    """Dispatch ``alert`` unless every position in it has already been alerted today."""
    if alert is None:
        return
    told = journal.already_alerted(day, name)
    fresh = [position for position in positions if position not in told]
    if not fresh:
        suppressed.append(name)
        return
    await dispatcher(alert)
    journal.record_alerted(day, name, positions)
    raised.append(name)


# ---------------------------------------------------------------------------
# The command — `uv run python tools/twt/sweep.py --date <today>`
# ---------------------------------------------------------------------------


def build_rearm() -> Rearm:
    """The re-arm the CLI hands :func:`sweep`. **This function is the one-line swap.**

    Today it is :func:`unavailable_rearm`, which refuses every line and names the runbook step
    that arms it by hand. When TW6's desk path lands, this body becomes::

        from baskfy_api.twt_rearm import rearm_position
        return rearm_position

    and nothing else in this module changes — which is the point of the callable being injected
    rather than imported at the top. It is written as a function, not a constant, so that the
    swap is a diff a reviewer reads in one place.

    A dynamic ``try: import ... except ImportError`` was rejected: it would make "the desk's GTT
    path is wired" a fact nobody can see in a diff, and a typo in the module name would degrade
    silently to the placeholder on a live afternoon (DECISIONS-TW **TW7.3**).
    """
    return unavailable_rearm


async def _load_open_lines(user_id: int) -> tuple[OpenLine, ...]:
    """The book, straight from ``tw_position``. Read-only: this tool writes no row, ever."""
    async with session_scope() as session:
        rows = await session.execute(
            select(TwPosition, Instrument.symbol)
            .join(Instrument, Instrument.id == TwPosition.instrument_id)
            .where(TwPosition.user_id == user_id, TwPosition.state == OPEN)
            .order_by(TwPosition.id)
        )
        return tuple(
            OpenLine(
                position_id=position.id,
                symbol=symbol,
                state=position.state,
                quantity_open=position.quantity_open,
                gtt_id=position.gtt_id,
                stop_price=position.stop_price,
            )
            for position, symbol in rows.all()
        )


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="TW7's 15:15 sweep: assert every open TWT line has a resting GTT.",
    )
    parser.add_argument("--date", type=dt.date.fromisoformat, default=None, help="IST date")
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "data" / "twt" / "sweep",
        help="where the day's alert journal is kept (untracked)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the book without attempting a re-arm or dispatching an alert",
    )
    return parser.parse_args(argv)


async def _run(argv: Sequence[str]) -> int:
    args = _parse(argv)
    now = dt.datetime.now(tz=IST)
    if args.date is not None:
        now = dt.datetime.combine(args.date, now.timetz())
    lines = await _load_open_lines(args.user_id)

    async def quiet(_: Alert) -> JsonObject:
        return {"dispatched": False, "reason": "--dry-run"}

    result = await sweep(
        lines,
        now=now,
        rearm=unavailable_rearm if args.dry_run else build_rearm(),
        journal=FileJournal(args.state_dir),
        dispatcher=quiet if args.dry_run else dispatch,
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0 if result.clean else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 on ``naked: 0`` and 1 otherwise — so cron, and a person, both learn the same thing."""
    return asyncio.run(_run(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
