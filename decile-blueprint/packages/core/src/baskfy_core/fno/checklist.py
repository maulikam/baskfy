"""The paper checklist, ``04`` §9, as a tally — pure (``docs/fno/06`` FO10).

``04`` §9: **F1**: 6 consecutive monthly cycles per underlying (12 structures in all), with ≥ 4
actually opened. **F2**: 60 consecutive trading sessions with ≥ 15 positions closed, including
≥ 3 rolls. **Zero rule violations**: no uncovered short at any step, no position into expiry day,
no ``LATE_EXIT``, no journal gap — and ``NAKED_FUTURE`` (``04`` §10) — and a cycle the desk missed
for want of a Kite login counts as a violation, not a skip.

The period counts **paper only** (``simulated``) and restarts after a violation: a sleeve's
cycles and sessions are counted from the day after its latest violation (FO10.6). "Consecutive"
is therefore literal — a clean run, not a total with a hole in it. The detectors below derive the
violations the tables can prove on their own; the ones the desk records as they happen
(``LATE_EXIT``, ``NAKED_FUTURE`` in ``fo_plan.detail.violations``, FO7.12) are passed in.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from baskfy_core.fno.config import FoSleeve, FoSleeveGroup, group_of
from baskfy_core.options.config import Side

#: ``04`` §9, verbatim.
F1_CYCLES: Final = 6
F1_MIN_OPENED: Final = 4
F2_SESSIONS: Final = 60
F2_MIN_CLOSED: Final = 15
F2_MIN_ROLLS: Final = 3


class ViolationKind(StrEnum):
    UNCOVERED_SHORT = "UNCOVERED_SHORT"
    INTO_EXPIRY = "INTO_EXPIRY"
    LATE_EXIT = "LATE_EXIT"
    JOURNAL_GAP = "JOURNAL_GAP"
    NAKED_FUTURE = "NAKED_FUTURE"
    MISSED_CYCLE = "MISSED_CYCLE"


@dataclass(frozen=True, slots=True)
class Violation:
    kind: ViolationKind
    sleeve: FoSleeve
    on: dt.date
    ref: str
    message: str


class CycleOutcome(StrEnum):
    #: A structure was opened in the cycle.
    OPENED = "OPENED"
    #: The rules skipped it (event, pause, rejection, a lapsed plan): a cycle, not a violation.
    SKIPPED = "SKIPPED"
    #: A candidate with no plan raised: the desk was not there (``04`` §9: a violation).
    MISSED = "MISSED"


@dataclass(frozen=True, slots=True)
class F1Cycle:
    sleeve: FoSleeve
    entry_session: dt.date
    outcome: CycleOutcome
    note: str = ""


@dataclass(frozen=True, slots=True)
class LegFill:
    """One fill of an option position, in time order, for the covered check."""

    role: str
    side: Side
    quantity: int
    ref: str
    on: dt.date


# --- detectors ------------------------------------------------------------------------------------


def uncovered_steps(sleeve: FoSleeve, fills: Sequence[LegFill]) -> list[Violation]:
    """Replay a condor's fills in order: after every fill each short side must be covered by its
    long (``SHORT_CALL`` ≤ ``LONG_CALL``, ``SHORT_PUT`` ≤ ``LONG_PUT``; ``02`` §2.1). One violation
    per step that leaves a short uncovered."""
    held: Counter[str] = Counter()
    found: list[Violation] = []
    for fill in fills:
        sign = 1 if fill.side is Side.BUY else -1
        if fill.role.startswith("SHORT"):
            held[fill.role] -= sign * fill.quantity  # a sale opens the short
        else:
            held[fill.role] += sign * fill.quantity
        for short, long_ in (("SHORT_CALL", "LONG_CALL"), ("SHORT_PUT", "LONG_PUT")):
            if held[short] > max(held[long_], 0):
                found.append(
                    Violation(
                        ViolationKind.UNCOVERED_SHORT,
                        sleeve,
                        fill.on,
                        fill.ref,
                        f"{held[short]} {short} units against {max(held[long_], 0)} {long_} after "
                        f"a {fill.side.value} of {fill.role}",
                    )
                )
    return found


def into_expiry(
    *,
    sleeve: FoSleeve,
    ref: str,
    expiry: dt.date | None,
    closed_on: dt.date | None,
    as_of: dt.date,
) -> Violation | None:
    """A position still open on its contract's expiry day (``PACK.4``: never)."""
    if expiry is None:
        return None
    last_held = closed_on if closed_on is not None else as_of
    if last_held >= expiry:
        return Violation(
            ViolationKind.INTO_EXPIRY,
            sleeve,
            expiry,
            ref,
            f"held on {last_held.isoformat()}, its contract's expiry is {expiry.isoformat()}",
        )
    return None


def journal_gap(
    *, sleeve: FoSleeve, ref: str, closed_on: dt.date, journalled: bool
) -> Violation | None:
    """A closed position without its ``fo_journal`` row."""
    if journalled:
        return None
    return Violation(
        ViolationKind.JOURNAL_GAP, sleeve, closed_on, ref, "closed with no journal row"
    )


def mark_gaps(
    *, sleeve: FoSleeve, ref: str, held_sessions: Iterable[dt.date], marked: Iterable[dt.date]
) -> list[Violation]:
    """Sessions an open position was held (and the bhavcopy landed) without its ``fo_mark``: the
    nightly record is part of the journal (``03`` §5), so a missing night is a gap."""
    have = set(marked)
    return [
        Violation(ViolationKind.JOURNAL_GAP, sleeve, day, ref, f"no mark for {day.isoformat()}")
        for day in sorted(held_sessions)
        if day not in have
    ]


def missed_cycles(cycles: Iterable[F1Cycle]) -> list[Violation]:
    return [
        Violation(
            ViolationKind.MISSED_CYCLE,
            c.sleeve,
            c.entry_session,
            f"{c.sleeve.value}@{c.entry_session.isoformat()}",
            c.note or "a candidate cycle with no plan raised: the desk was not there",
        )
        for c in cycles
        if c.outcome is CycleOutcome.MISSED
    ]


# --- the tally ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class F1Tally:
    sleeve: FoSleeve
    since: dt.date | None
    cycles: int
    opened: int


@dataclass(frozen=True, slots=True)
class F2Tally:
    since: dt.date | None
    sessions: int
    closed: int
    rolls: int


@dataclass(frozen=True, slots=True)
class Checklist:
    as_of: dt.date
    f1: tuple[F1Tally, ...]
    f1_opened: int
    f1_met: bool
    f2: F2Tally
    f2_met: bool
    violations: tuple[Violation, ...]

    def violations_by_kind(self, group: FoSleeveGroup | None = None) -> dict[str, int]:
        counts = Counter(
            v.kind.value for v in self.violations if group is None or group_of(v.sleeve) is group
        )
        return dict(sorted(counts.items()))


@dataclass(frozen=True, slots=True)
class F2Close:
    """A closed paper F2 position: when, and how many rolls it carried."""

    closed_on: dt.date
    rolls: int


def _since(violations: Sequence[Violation], group: FoSleeveGroup) -> dt.date | None:
    days = [v.on for v in violations if group_of(v.sleeve) is group]
    return max(days) if days else None


def paper_checklist(  # noqa: PLR0913 - every input 04 §9 names, by keyword
    *,
    as_of: dt.date,
    f1_cycles: Iterable[F1Cycle],
    f2_start: dt.date | None,
    f2_closes: Iterable[F2Close],
    sessions: Sequence[dt.date],
    violations: Iterable[Violation],
) -> Checklist:
    """``04`` §9's tally at ``as_of``. ``sessions`` is the exchange calendar; ``f2_start`` the
    first paper F2 session (its first plan's day), ``None`` before there is one."""
    all_v = tuple(sorted(violations, key=lambda v: (v.on, v.kind.value, v.ref)))
    all_v = tuple(v for v in all_v if v.on <= as_of)
    f1_since = _since(all_v, FoSleeveGroup.F1)
    f2_since = _since(all_v, FoSleeveGroup.F2)
    cycles = [
        c
        for c in f1_cycles
        if c.entry_session <= as_of and (f1_since is None or c.entry_session > f1_since)
    ]
    tallies = tuple(
        F1Tally(
            sleeve,
            f1_since,
            sum(1 for c in cycles if c.sleeve is sleeve),
            sum(1 for c in cycles if c.sleeve is sleeve and c.outcome is CycleOutcome.OPENED),
        )
        for sleeve in (FoSleeve.F1N, FoSleeve.F1B)
    )
    f1_opened = sum(t.opened for t in tallies)
    f1_met = all(t.cycles >= F1_CYCLES for t in tallies) and f1_opened >= F1_MIN_OPENED
    start = f2_start
    if start is not None and f2_since is not None and f2_since >= start:
        start = next((d for d in sessions if d > f2_since), None)
    counted = [] if start is None else [d for d in sessions if start <= d <= as_of]
    closes = [c for c in f2_closes if start is not None and start <= c.closed_on <= as_of]
    f2 = F2Tally(start, len(counted), len(closes), sum(c.rolls for c in closes))
    f2_met = f2.sessions >= F2_SESSIONS and f2.closed >= F2_MIN_CLOSED and f2.rolls >= F2_MIN_ROLLS
    return Checklist(as_of, tallies, f1_opened, f1_met, f2, f2_met, all_v)


__all__ = [
    "F1_CYCLES",
    "F1_MIN_OPENED",
    "F2_MIN_CLOSED",
    "F2_MIN_ROLLS",
    "F2_SESSIONS",
    "Checklist",
    "CycleOutcome",
    "F1Cycle",
    "F1Tally",
    "F2Close",
    "F2Tally",
    "LegFill",
    "Violation",
    "ViolationKind",
    "into_expiry",
    "journal_gap",
    "mark_gaps",
    "missed_cycles",
    "paper_checklist",
    "uncovered_steps",
]
