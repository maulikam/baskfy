"""What changed between two runs of one screen — PROMPTS.md Prompt 20 deliverable 3.

    "Screen alerts: a user subscribes a screen to a schedule (daily/weekly after publish) and
     receives an email with entries, exits, and rank changes since the last run — **computed by
     diffing screen_run rows**."

This module is that diff, and nothing else. It takes two ``screen_run.results`` payloads — docs/04
stores them as ``[{rank, instrument_id, factor_value}]`` — and answers three questions: who is new,
who is gone, and who moved. It lives in ``packages/core`` because it is arithmetic over two lists
and touches no database, no clock and no network; ``baskfy_api.alerts`` does the loading and
``baskfy_worker.tasks.alerts`` does the sending.

Two decisions worth stating
---------------------------
**A rank change is reported in "places moved", positive for improvement.** ``previous_rank 40 ->
current_rank 12`` is ``+28``, not ``-28``: rank 1 is the best row in a screen (docs/06 §step 6),
so a smaller number is a better position and an email that said "-28" about a stock that climbed
would be read backwards by every recipient.

**Order is total and deterministic.** Entries by current rank, exits by previous rank, movers by
the size of the move and then by current rank. A diff that ordered by ``set`` iteration would
produce a different email from the same two runs, which would make the snapshot test in
``services/api/tests/test_api_alerts.py`` meaningless and the emails themselves untrustworthy.

**Nothing here knows a symbol.** ``screen_run.results`` carries ``instrument_id`` and no ticker,
so the diff is over ids and the caller decorates it. Resolving symbols here would mean either a
query (forbidden in this package) or a second argument that every caller has to get right.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

__all__ = [
    "MIN_REPORTABLE_MOVE",
    "RankChange",
    "RankedRow",
    "ScreenDiff",
    "diff_runs",
    "rows_from_results",
]

#: The smallest rank move worth putting in an email by default. One place is noise on a
#: 4,000-row screen and a headline on a 20-row one, so it is a parameter — but a default of 1
#: means "report every move", which is the honest starting point rather than a silent filter.
MIN_REPORTABLE_MOVE: Final = 1


@dataclass(frozen=True, slots=True, order=True)
class RankedRow:
    """One row of a stored run: where it placed, and what it scored.

    ``factor_value`` is ``None`` when the run stored no sorting value for the row — docs/06 allows
    a NULL sorting factor to survive into the result set when the screen sorts on something the
    instrument has no history for.
    """

    rank: int
    instrument_id: int
    factor_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RankChange:
    """One name that was in both runs, and moved."""

    instrument_id: int
    previous_rank: int
    current_rank: int
    factor_value: Decimal | None = None

    @property
    def places_moved(self) -> int:
        """Positive when the name climbed. See the module docstring."""
        return self.previous_rank - self.current_rank

    @property
    def improved(self) -> bool:
        return self.places_moved > 0


@dataclass(frozen=True, slots=True)
class ScreenDiff:
    """Everything one alert email says, before it is worded."""

    previous_as_of: dt.date
    current_as_of: dt.date
    entries: tuple[RankedRow, ...]
    exits: tuple[RankedRow, ...]
    rank_changes: tuple[RankChange, ...]
    #: Names present in both runs at the same rank. Counted rather than listed: an email that
    #: enumerated 3,900 unchanged rows would bury the twelve that matter.
    held_count: int
    previous_count: int
    current_count: int

    @property
    def is_empty(self) -> bool:
        """True when nothing an alert exists to report happened.

        A screen whose membership and ordering are identical is not worth an email, and sending
        one anyway is the fastest way to teach a subscriber to filter the sender.
        """
        return not (self.entries or self.exits or self.rank_changes)

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def exit_count(self) -> int:
        return len(self.exits)

    @property
    def instrument_ids(self) -> tuple[int, ...]:
        """Every id the diff mentions, so a caller can resolve symbols in one query."""
        ids = {row.instrument_id for row in (*self.entries, *self.exits)}
        ids.update(change.instrument_id for change in self.rank_changes)
        return tuple(sorted(ids))


def _decimal(value: object) -> Decimal | None:
    """``screen_run.results`` stores the sorting value as a *string* (see ``docs/07a`` §6)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        # `bool` is an `int`; a boolean sorting factor is meaningless and would silently become 1.
        return None
    if isinstance(value, (int, str)):
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError):
            return None
    return None


def rows_from_results(results: Iterable[Mapping[str, object]]) -> tuple[RankedRow, ...]:
    """Parse a stored ``screen_run.results`` payload into rows, in rank order.

    Rows without an integer ``rank`` and ``instrument_id`` are dropped rather than guessed at: the
    column is JSONB, so a row written by an older build is a real possibility, and a diff that
    invented a rank for it would report a move nobody made.
    """
    parsed: list[RankedRow] = []
    for entry in results:
        rank = entry.get("rank")
        instrument_id = entry.get("instrument_id")
        if not isinstance(rank, int) or isinstance(rank, bool):
            continue
        if not isinstance(instrument_id, int) or isinstance(instrument_id, bool):
            continue
        parsed.append(
            RankedRow(
                rank=rank,
                instrument_id=instrument_id,
                factor_value=_decimal(entry.get("factor_value")),
            )
        )
    return tuple(sorted(parsed))


def _by_instrument(rows: Sequence[RankedRow]) -> dict[int, RankedRow]:
    """Best (lowest) rank wins if a run somehow lists an instrument twice.

    It should not happen — the screener ranks a distinct instrument set — but "should not" is not
    an invariant this module can enforce, and picking deterministically beats picking last.
    """
    best: dict[int, RankedRow] = {}
    for row in rows:
        held = best.get(row.instrument_id)
        if held is None or row.rank < held.rank:
            best[row.instrument_id] = row
    return best


def diff_runs(  # noqa: PLR0913 - two runs, two dates and two filters are all genuine inputs
    previous: Sequence[RankedRow],
    current: Sequence[RankedRow],
    *,
    previous_as_of: dt.date,
    current_as_of: dt.date,
    min_move: int = MIN_REPORTABLE_MOVE,
    top_n: int | None = None,
) -> ScreenDiff:
    """Diff two runs of the same screen.

    ``top_n`` restricts *both* sides to their first ``top_n`` ranks before diffing, which is what
    makes "entered the top 20" a meaningful phrase — without it, a name that moved from rank 900
    to rank 400 on a 4,000-row screen is an "entry" to nobody's portfolio. ``None`` diffs the whole
    result set, which is the right answer for a screen that is already narrow.

    ``min_move`` filters the rank-change list only. It never suppresses an entry or an exit:
    joining or leaving a screen is not a matter of degree.
    """
    if min_move < 1:
        raise ValueError("min_move must be at least 1; a move of zero is not a move")
    if top_n is not None and top_n < 1:
        raise ValueError("top_n must be at least 1")

    before = _clip(previous, top_n)
    after = _clip(current, top_n)
    before_by_id = _by_instrument(before)
    after_by_id = _by_instrument(after)

    entries = tuple(row for row in after if row.instrument_id not in before_by_id)
    exits = tuple(row for row in before if row.instrument_id not in after_by_id)

    changes: list[RankChange] = []
    held = 0
    for row in after:
        was = before_by_id.get(row.instrument_id)
        if was is None:
            continue
        if was.rank == row.rank:
            held += 1
            continue
        change = RankChange(
            instrument_id=row.instrument_id,
            previous_rank=was.rank,
            current_rank=row.rank,
            factor_value=row.factor_value,
        )
        if abs(change.places_moved) >= min_move:
            changes.append(change)
        else:
            held += 1

    changes.sort(key=lambda change: (-abs(change.places_moved), change.current_rank))
    return ScreenDiff(
        previous_as_of=previous_as_of,
        current_as_of=current_as_of,
        entries=entries,
        exits=exits,
        rank_changes=tuple(changes),
        held_count=held,
        previous_count=len(before),
        current_count=len(after),
    )


def _clip(rows: Sequence[RankedRow], top_n: int | None) -> tuple[RankedRow, ...]:
    ordered = tuple(sorted(rows))
    if top_n is None:
        return ordered
    return tuple(row for row in ordered if row.rank <= top_n)
