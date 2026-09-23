"""One open position's exit, evaluated per tick (``06`` OP9: "``exits.evaluate`` per tick").

Every sleeve already has its rule as a pure function — ``condor.exit_decision`` (condor §7),
``directional.exit_decision`` (``04`` §4.5), ``expiry_setups.exit_decision`` (§5.3) — and the
feed rule is ``execution.feed_lost`` (§8.5). This module is the one place that turns an open
position, its legs' latest quotes and the index's state into the inputs those functions take, and
returns the verdict. It decides; it never sends. The desk's ``options_monitor`` calls
:func:`evaluate` on every tick and hands a verdict to OP10's executor.

What a verdict reads (``04`` §8.5, §9.2):

* **The mark** is the conservative close — longs at the bid, shorts at the ask. A leg with no
  two-sided quote, or one older than ``chain.stale_quote_seconds``, makes the mark *stale*: the
  profit rules (``PROFIT``, ``TARGET``) are dropped, stops and invalidations still run on the last
  known mark, and with no mark at all only the clock rules can fire.
* **The loss** is the marked loss in ₹ against the entry from fills, for §9.2's budget breach.
* **Feed loss** is checked first and wins: ``HARD_EXIT`` with the reason ``FEED_LOST``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from baskfy_core.options import condor, directional, expiry_setups
from baskfy_core.options.bars import Bar, closed, five_minute_bars, ist, minute
from baskfy_core.options.config import OptionsConfig, Sleeve
from baskfy_core.options.execution import LegRole, feed_lost
from baskfy_core.options.structures import Direction, Structure

#: The reason on a ``HARD_EXIT`` the feed forced (``04`` §8.5).
FEED_LOST = "FEED_LOST"
#: The reason on a rule-driven exit: the rule's own code is the verdict.
RULE = "RULE"

_O1 = frozenset({Sleeve.O1M, Sleeve.O1W})


@dataclass(frozen=True, slots=True)
class OpenLeg:
    """One filled leg: its role, strike, units and the average fill price."""

    role: LegRole
    strike: Decimal
    quantity: int
    fill_price: Decimal
    instrument_token: int


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """What the exit rules need of an ``op_position`` and its plan.

    ``entry_points`` is the structure's premium per unit from fills: the condor's credit, the
    long's entry, the spread's debit. The invalidation fields are the plan's own (``or_high`` /
    ``or_low`` for O2, the morning range for O3-A, ``half_gap`` for O3-B).
    """

    sleeve: Sleeve
    structure: Structure
    legs: tuple[OpenLeg, ...]
    entry_points: Decimal
    opened_at: dt.datetime
    risk_budget_inr: Decimal
    direction: Direction | None = None
    range_high: Decimal | None = None
    range_low: Decimal | None = None
    half_gap: Decimal | None = None
    width_points: Decimal | None = None

    @property
    def quantity(self) -> int:
        return self.legs[0].quantity if self.legs else 0

    def leg(self, role: LegRole) -> OpenLeg | None:
        return next((lg for lg in self.legs if lg.role is role), None)


@dataclass(frozen=True, slots=True)
class LegMark:
    """A leg's latest quote and when it was taken."""

    bid: Decimal | None
    ask: Decimal | None
    at: dt.datetime


@dataclass(frozen=True, slots=True)
class IndexState:
    """What the desk knows of NIFTY 50 at ``now``: the last tick, the spot, and the bars so far."""

    spot: Decimal | None
    last_tick_at: dt.datetime | None
    bars: tuple[Bar, ...] = ()


@dataclass(frozen=True, slots=True)
class ExitVerdict:
    """The exit to send, why, and the mark it was decided on (``None`` when there was none)."""

    code: str
    reason: str
    value_points: Decimal | None
    marked_loss_inr: Decimal | None
    stale: bool


@dataclass(frozen=True, slots=True)
class Mark:
    """The conservative close of a whole position: its value per unit and whether it is stale."""

    value_points: Decimal | None
    stale: bool


def mark(
    position: OpenPosition,
    marks: Mapping[LegRole, LegMark],
    now: dt.datetime,
    options: OptionsConfig,
) -> Mark:
    """The position's close value per unit — longs sold at the bid, shorts bought at the ask.

    For a condor it is the cost to close (``D``, a positive number to pay); for a long or a debit
    spread it is what closing receives (``V``). ``None`` when some leg has never been quoted
    two-sided; ``stale`` when any leg's quote is older than ``stale_quote_seconds``.
    """
    total = Decimal(0)
    stale = False
    for lg in position.legs:
        quote = marks.get(lg.role)
        if quote is None or quote.bid is None or quote.ask is None:
            return Mark(None, True)
        if (ist(now) - ist(quote.at)).total_seconds() > options.chain.stale_quote_seconds:
            stale = True
        if position.structure is Structure.IRON_CONDOR:
            total += quote.ask if not lg.role.is_long else -quote.bid
        else:
            total += quote.bid if lg.role.is_long else -quote.ask
    return Mark(total, stale)


def marked_loss_inr(position: OpenPosition, value: Decimal) -> Decimal:
    """``04`` §9.2's marked loss in ₹ (positive = a loss) at close value ``value`` per unit."""
    per_unit = (
        value - position.entry_points
        if position.structure is Structure.IRON_CONDOR
        else position.entry_points - value
    )
    return per_unit * position.quantity


def latest_five_minute_close(
    bars: Iterable[Bar], now: dt.datetime, after: dt.datetime, session_open: dt.time, width: int
) -> Decimal | None:
    """The close of the last *completed* ``width``-minute bar that ended after ``after`` — what
    O2's and O3-A's invalidation reads (a close back inside the range, never an intrabar touch)."""
    seen = closed(bars, now)
    done = [
        b
        for b in five_minute_bars(seen, session_open, width)
        if b.complete and b.close_time > minute(after)
    ]
    return done[-1].close if done else None


def latest_bar_after(bars: Iterable[Bar], now: dt.datetime, after: dt.datetime) -> Bar | None:
    """The last closed one-minute bar that started at or after ``after``'s minute — O3-B's
    invalidation reads any such bar through ``half_gap``."""
    seen = [b for b in closed(bars, now) if minute(b.ts) >= minute(after)]
    return seen[-1] if seen else None


def evaluate(  # noqa: PLR0913 - every input §8.5 and §9.2 name, by keyword
    position: OpenPosition,
    marks: Mapping[LegRole, LegMark],
    index: IndexState,
    now: dt.datetime,
    *,
    options: OptionsConfig,
    manual: bool = False,
) -> ExitVerdict | None:
    """One tick's exit for ``position``, or ``None`` to hold."""
    if feed_lost(
        position.sleeve,
        now=ist(now),
        last_index_tick=None if index.last_tick_at is None else ist(index.last_tick_at),
        position_opened_at=ist(position.opened_at),
        stale_index_seconds=options.chain.stale_index_seconds,
        config=options.execution,
    ):
        return ExitVerdict("HARD_EXIT", FEED_LOST, None, None, True)
    current = mark(position, marks, now, options)
    if current.value_points is None:
        # No mark at all: only the clock can close it (the hard exit); a stop cannot be judged.
        if ist(now).time() >= options.hard_exit_time(position.sleeve):
            return ExitVerdict("HARD_EXIT", RULE, None, None, True)
        return None
    value = current.value_points
    loss = marked_loss_inr(position, value)
    code = _decide(position, value, loss, current.stale, index, now, options, manual)
    if code is None:
        return None
    return ExitVerdict(code, RULE, value, loss, current.stale)


def _decide(  # noqa: PLR0913, PLR0917 - the position, its mark, the index, the clock, the rules
    position: OpenPosition,
    value: Decimal,
    loss: Decimal,
    stale: bool,
    index: IndexState,
    now: dt.datetime,
    options: OptionsConfig,
    manual: bool,
) -> str | None:
    session_open = options.calendar.market_open
    if position.structure is Structure.IRON_CONDOR:
        if position.sleeve not in _O1:
            raise ValueError(f"{position.sleeve.value} does not trade a condor")
        short_call = position.leg(LegRole.SHORT_CALL)
        short_put = position.leg(LegRole.SHORT_PUT)
        if short_call is None or short_put is None or index.spot is None:
            raise ValueError("a condor exit needs both shorts and the index level")
        cfg = options.condor_monthly if position.sleeve is Sleeve.O1M else options.condor_weekly
        verdict = condor.exit_decision(
            entry_credit=position.entry_points,
            cost_now=value,
            spot=index.spot,
            short_call_strike=short_call.strike,
            short_put_strike=short_put.strike,
            now=now,
            stale=stale,
            marked_loss_inr=loss,
            risk_budget_inr=position.risk_budget_inr,
            config=cfg,
            options=options,
            manual=manual,
        )
        return None if verdict is None else verdict.value
    direction = position.direction
    if direction is None:
        raise ValueError("a directional position has a direction")
    if position.structure is Structure.LONG_OPTION:
        if position.range_high is None or position.range_low is None:
            raise ValueError("an O2 position carries its opening range")
        five = latest_five_minute_close(
            index.bars, now, position.opened_at, session_open, options.directional.bar_minutes
        )
        long_verdict = directional.exit_decision(
            entry=position.entry_points,
            bid=value,
            entered_at=position.opened_at,
            now=now,
            is_invalidated=directional.invalidated(
                five, direction, position.range_high, position.range_low
            ),
            stale=stale,
            marked_loss_inr=loss,
            risk_budget_inr=position.risk_budget_inr,
            config=options.directional,
            options=options,
            manual=manual,
        )
        return None if long_verdict is None else long_verdict.value
    cfg3 = options.expiry_setups
    if position.sleeve is Sleeve.O3B:
        if position.half_gap is None:
            raise ValueError("an O3-B position carries its half-gap")
        invalid = expiry_setups.gap_hold_invalidated(
            latest_bar_after(index.bars, now, position.opened_at), direction, position.half_gap
        )
    else:
        if position.range_high is None or position.range_low is None:
            raise ValueError("an O3-A position carries its morning range")
        five = latest_five_minute_close(
            index.bars, now, position.opened_at, session_open, cfg3.bar_minutes
        )
        invalid = expiry_setups.range_break_invalidated(
            five, direction, position.range_high, position.range_low
        )
    spread_verdict = expiry_setups.exit_decision(
        value=value,
        entry_debit=position.entry_points,
        width=position.width_points or cfg3.width_points,
        is_invalidated=invalid,
        now=now,
        stale=stale,
        marked_loss_inr=loss,
        risk_budget_inr=position.risk_budget_inr,
        config=cfg3,
        options=options,
        manual=manual,
    )
    return None if spread_verdict is None else spread_verdict.value


__all__ = [
    "FEED_LOST",
    "RULE",
    "ExitVerdict",
    "IndexState",
    "LegMark",
    "Mark",
    "OpenLeg",
    "OpenPosition",
    "evaluate",
    "latest_bar_after",
    "latest_five_minute_close",
    "mark",
    "marked_loss_inr",
]
