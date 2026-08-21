"""The rank-buffer rebalancing rule — docs/01 §8, docs/07 §"Portfolios & rebalance".

docs/01 §8 describes the reference product's tracker as a CSV diff that returns three copyable
lists, and names the rule behind it:

    "This is the classic *rank-buffer* rebalancing rule (buy top N, hold until rank > N + buffer),
     which materially reduces turnover."

docs/07 fixes the arithmetic of the middle list exactly:

    "``inside_wrh`` = held names whose current rank is ``> top_n`` but ``<= top_n + hold_buffer``."

and PROMPTS.md Prompt 14 §2 fixes the other two:

    entries:    in the screen's top_n, not currently held
    exits:      held and ranked worse than top_n + hold_buffer (or absent from the screen entirely)
    inside_wrh: held, rank > top_n but <= top_n + hold_buffer → hold

This module is pure: ranks in, lists out. No database, no HTTP, no clock (CLAUDE.md — anything
touching I/O belongs in ``services/``). ``baskfy_api.portfolios`` runs the screen, loads the
holdings and calls :func:`plan_rebalance`; the *rule* is only ever here, so the unit tests and the
endpoint cannot disagree about what "inside the buffer" means.

The fourth list
---------------
docs/01 §8 and docs/07 name three lists. A held name ranked **inside** ``top_n`` is in none of
them — it is neither entering, nor exiting, nor inside the buffer band, it is simply core. It is
returned as :attr:`RebalancePlan.holds` because the target weights include it and a user reading
three columns that omit half their portfolio would reasonably think we had lost it. Recorded in
``docs/DECISIONS.md`` §14.

Boundary, stated once
---------------------
``rank == top_n + hold_buffer`` **holds** (the comparison is ``<=``, from docs/07's own sentence);
``rank == top_n + hold_buffer + 1`` **exits**. ``hold_buffer = 0`` collapses the band to nothing,
which is the plain "rebalance to the top N" rule with no hysteresis.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Final

__all__ = [
    "WEIGHT_STEP",
    "ExitReason",
    "HeldName",
    "RebalancePlan",
    "RebalanceRow",
    "ScreenRank",
    "TargetWeight",
    "plan_rebalance",
]

#: ``portfolio``-adjacent weights are reported at ``numeric(10,6)``, the precision docs/04 already
#: uses for ``index_member_daily.weight``. Rounding happens here, once, so the API payload, the
#: CSV download and the clipboard copy carry identical digits (CLAUDE.md house rule 8).
WEIGHT_STEP: Final = Decimal("0.000001")

_ONE: Final = Decimal(1)


class ExitReason(StrEnum):
    """Why a held name is on the exit list.

    PROMPTS.md Prompt 14's first acceptance criterion requires "a delisted holding (exit with
    reason)", so every exit carries one rather than only the interesting case.
    """

    #: In the screen, but ranked worse than ``top_n + hold_buffer``.
    RANK_OUTSIDE_BUFFER = "rank_outside_buffer"
    #: Not in the screen's output at all — filtered out, or outside the screen's universe.
    NOT_IN_SCREEN = "not_in_screen"
    #: The instrument has a ``delisted_on`` date. Reported ahead of ``not_in_screen`` because it
    #: is the more specific and more actionable answer: the position cannot be held any longer.
    DELISTED = "delisted"


class Action(StrEnum):
    """What the plan says to do with a name that is in the target portfolio."""

    ENTER = "enter"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class ScreenRank:
    """One row of the screen's output, reduced to what the rule needs."""

    instrument_id: int
    symbol: str
    name: str
    rank: int


@dataclass(frozen=True, slots=True)
class HeldName:
    """One ``portfolio_holding`` row, joined to its instrument."""

    instrument_id: int
    symbol: str
    name: str
    quantity: Decimal | None = None
    #: ``instrument.delisted_on`` is set. The rule never needs the date, only the fact.
    delisted: bool = False


@dataclass(frozen=True, slots=True)
class RebalanceRow:
    """A name on one of the three lists.

    ``rank`` is ``None`` for a holding the screen did not return at all — there is no rank to
    report, and reporting ``0`` or the row count would be inventing one.
    """

    instrument_id: int
    symbol: str
    name: str
    rank: int | None = None
    reason: ExitReason | None = None


@dataclass(frozen=True, slots=True)
class TargetWeight:
    """What the portfolio should hold after acting on the plan.

    ``weight`` is a fraction of the portfolio, not a percentage — the same convention as
    ``volatility`` elsewhere in this codebase (docs/06a §10). The web app multiplies for display.
    """

    instrument_id: int
    symbol: str
    name: str
    rank: int
    weight: Decimal
    action: Action


@dataclass(frozen=True, slots=True)
class RebalancePlan:
    """The answer. docs/07's three lists, the fourth for completeness, and the target weights."""

    top_n: int
    hold_buffer: int
    exits: tuple[RebalanceRow, ...]
    inside_wrh: tuple[RebalanceRow, ...]
    entries: tuple[RebalanceRow, ...]
    holds: tuple[RebalanceRow, ...]
    target_weights: tuple[TargetWeight, ...]

    @property
    def buffer_limit(self) -> int:
        """``top_n + hold_buffer`` — the worst rank a held name may have and still be kept."""
        return self.top_n + self.hold_buffer


def plan_rebalance(
    ranked: Iterable[ScreenRank],
    held: Iterable[HeldName],
    *,
    top_n: int,
    hold_buffer: int,
) -> RebalancePlan:
    """Apply the rank-buffer rule.

    ``ranked`` is the screen's output in rank order; ``held`` is the portfolio. Both are matched
    on ``instrument_id``, never on symbol — NSE renames symbols (``symbol_alias``), and a rule
    that diffed strings would silently exit a position on the day of a rename.
    """
    if top_n < 1:
        raise ValueError(f"top_n must be at least 1, got {top_n}")
    if hold_buffer < 0:
        raise ValueError(f"hold_buffer must not be negative, got {hold_buffer}")

    by_id: Mapping[int, ScreenRank] = {row.instrument_id: row for row in ranked}
    holdings = list(held)
    held_ids = {holding.instrument_id for holding in holdings}
    limit = top_n + hold_buffer

    exits: list[RebalanceRow] = []
    inside: list[RebalanceRow] = []
    holds: list[RebalanceRow] = []

    for holding in holdings:
        row = by_id.get(holding.instrument_id)
        reason = _exit_reason(holding, row, limit)
        if reason is not None:
            exits.append(
                RebalanceRow(
                    instrument_id=holding.instrument_id,
                    symbol=holding.symbol,
                    name=holding.name,
                    rank=row.rank if row is not None else None,
                    reason=reason,
                )
            )
        elif row is not None and row.rank > top_n:
            inside.append(_row(row))
        elif row is not None:
            holds.append(_row(row))

    entries = [
        _row(row)
        for row in sorted(by_id.values(), key=lambda item: item.rank)
        if row.rank <= top_n and row.instrument_id not in held_ids
    ]

    targets = _target_weights(entries, holds, inside, by_id)
    return RebalancePlan(
        top_n=top_n,
        hold_buffer=hold_buffer,
        exits=tuple(sorted(exits, key=_exit_sort_key)),
        inside_wrh=tuple(sorted(inside, key=lambda item: item.rank or 0)),
        entries=tuple(entries),
        holds=tuple(sorted(holds, key=lambda item: item.rank or 0)),
        target_weights=targets,
    )


def _exit_reason(holding: HeldName, row: ScreenRank | None, limit: int) -> ExitReason | None:
    """``None`` means keep. Delisting outranks every other answer."""
    if holding.delisted:
        return ExitReason.DELISTED
    if row is None:
        return ExitReason.NOT_IN_SCREEN
    if row.rank > limit:
        return ExitReason.RANK_OUTSIDE_BUFFER
    return None


def _row(rank: ScreenRank) -> RebalanceRow:
    return RebalanceRow(
        instrument_id=rank.instrument_id, symbol=rank.symbol, name=rank.name, rank=rank.rank
    )


def _exit_sort_key(row: RebalanceRow) -> tuple[int, int, str]:
    """Ranked exits first, in rank order; then the rankless ones, alphabetically.

    ``rank is None`` sorts last rather than first: a name the screen still ranks is a decision
    about degree, and a name it does not rank at all is a different kind of event.
    """
    return (1, 0, row.symbol) if row.rank is None else (0, row.rank, row.symbol)


def _target_weights(
    entries: Sequence[RebalanceRow],
    holds: Sequence[RebalanceRow],
    inside: Sequence[RebalanceRow],
    by_id: Mapping[int, ScreenRank],
) -> tuple[TargetWeight, ...]:
    """Equal weight over the post-rebalance portfolio, summing to exactly 1.

    **Equal weight is a choice the bundle does not make.** docs/07 asks the rebalance response for
    ``target_weights`` and says nothing about how they are computed; docs/10 §"Execution model"
    offers four schemes (``equal | inverse_volatility | rank | marketcap``) but that is the
    *backtest's* configuration, and Prompt 14 has no weighting input in its wizard (§3: portfolio,
    screen, ``top_n``, ``hold_buffer``). Equal weight is docs/10's own default, needs no factor the
    tracker does not already have, and is the only scheme that cannot be wrong for a reason the
    user was never asked about. See ``docs/DECISIONS.md`` §14.

    The last name absorbs the rounding remainder, so the column adds to 1.000000 rather than to
    0.999999 — a portfolio sheet that does not add up invites a support ticket every time.
    """
    members = [*entries, *holds, *inside]
    if not members:
        return ()

    share = (_ONE / Decimal(len(members))).quantize(WEIGHT_STEP, rounding=ROUND_DOWN)
    ordered = sorted(members, key=lambda row: (by_id[row.instrument_id].rank, row.symbol))
    entering = {row.instrument_id for row in entries}

    weights: list[TargetWeight] = []
    allocated = Decimal(0)
    for position, row in enumerate(ordered):
        rank = by_id[row.instrument_id].rank
        last = position == len(ordered) - 1
        weight = (_ONE - allocated) if last else share
        allocated += weight
        weights.append(
            TargetWeight(
                instrument_id=row.instrument_id,
                symbol=row.symbol,
                name=row.name,
                rank=rank,
                weight=weight,
                action=Action.ENTER if row.instrument_id in entering else Action.HOLD,
            )
        )
    return tuple(weights)
