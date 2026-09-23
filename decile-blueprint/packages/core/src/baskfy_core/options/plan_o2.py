"""The O2 plan builder, pure: a reasoned no-trade or one long-option plan (``06`` OP7).

``build_plan(O2, date, trigger)`` in ``06`` is, in order: the day filters → the trigger →
``expiry_for_o2`` → the strike → delta and liquidity → costs → sizing (including the premium cap)
→ ``op_plan`` / ``op_leg``, or ``SKIPPED`` with the code; ``expires_at = min(issued_at + 30 min,
13:30)``. This module is everything in that sentence that is arithmetic; the worker
(``baskfy_worker.options.plan_o2``) reads the rows and writes the result.

Two phases, the shape OP6 set:

* :func:`decide_o2` — role, day filters (``04`` §4.1), trigger (§4.2), contract, delta, sizing,
  premium cap and the cost test, by **exactly** the functions the scan calls
  (``scan.priced_view``, ``directional.day_filters``, ``directional.find_trigger``,
  ``directional.build`` over the trigger minute's snapshot — ``04`` §10, OP4.4). Its candidate
  therefore equals the scan's (tested).
* :func:`finalize_o2` — the ``plan_id``, the expiry, the exit levels of §4.5 and the legs. **No
  broker read**: ``04`` §7.4 asks the margin calculator for O1 and O3 only, because a long option
  costs its premium and nothing else; the premium cap of §4.6 is this sleeve's ceiling and it is
  applied in ``directional.build``, before any of this (OP7.3).

**One leg, always long.** ``entry_sequence`` puts the long first and refuses a short with no
protecting long, so ``never_naked`` holds at every prefix by construction; :func:`finalize_o2`
asserts it anyway, with the same helper the condor uses. **Nothing here places anything**: a plan
is a proposal the desk's confirm (OP10) may act on within its 30 minutes; there is no auto-execute
for any options sleeve (PACK.3).

Which days write a session (OP7.2): every non-event trading day is an O2 day. It writes one
``op_session`` — ``PLANNED``, or ``SKIPPED`` with every reason (a refused day filter, a day whose
entry window closed with no trigger, or the code that refused the contract). An event day is
``SKIPPED / EVENT_DAY`` (``04`` §1.3: skipped, never shifted); a holiday is ``NO_SESSION`` and
writes nothing. Before 09:30, while the opening range is unsettled, while no trigger has fired yet
or before the trigger minute's snapshot is stored, the answer is ``NOT_READY`` and nothing is
written — the caller asks again next minute.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from baskfy_core.options import directional
from baskfy_core.options.bars import IST, ist, minute_after
from baskfy_core.options.calendar import RoleReason, expiry_for_o2, lot_size_for, role
from baskfy_core.options.config import (
    DirectionalConfig,
    Mode,
    OptionsCeilings,
    OptionsConfig,
    SizingMode,
    Sleeve,
)
from baskfy_core.options.costs import ChargeBreakdown, paise
from baskfy_core.options.directional import Break, DayFilters, TriggerScan
from baskfy_core.options.plan import (
    STALE_CHAIN,
    PlanLeg,
    PlanState,
    Verdict,
    assert_never_naked,
    plan_costs,
    plan_id_for,
    plan_legs,
)
from baskfy_core.options.scan import DayContext, MarketDay, SnapshotBook, priced_view
from baskfy_core.options.session import plan_expires_at
from baskfy_core.options.sizing import gap_through_long
from baskfy_core.options.structures import Candidate, Direction, Structure

#: ``04`` §4.2's window closed and the tape never broke the opening range in the trend's
#: direction: the day is decided, and it is a skipped session (OP7.2).
NO_TRIGGER = "NO_TRIGGER"
#: The window is still open and nothing has broken yet — ask again next minute.
NO_TRIGGER_YET = "NO_TRIGGER_YET"
#: The master lists no expiry after today, so ``expiry_for_o2`` has no answer (OP7.4).
NO_EXPIRY = "NO_EXPIRY"
#: Before 09:30 there is no entry window yet.
BEFORE_ENTRY_WINDOW = "BEFORE_ENTRY_WINDOW"
#: The 09:15-09:29 opening range is not judged yet (OP4.3's rule, this sleeve's window).
RANGE_NOT_SETTLED = "RANGE_NOT_SETTLED"
#: The trigger minute's chain is not stored yet.
NO_DECISION_SNAPSHOT = "NO_DECISION_SNAPSHOT"
#: A trigger nobody can act on: ``expires_at`` would not be after ``issued_at`` (OP7.5).
ENTRY_WINDOW_CLOSED = "ENTRY_WINDOW_CLOSED"


@dataclass(frozen=True, slots=True)
class O2Decision:
    """The first phase's answer: every reason, the day's numbers, the priced candidate."""

    sleeve: Sleeve
    trade_date: dt.date
    now: dt.datetime
    state: PlanState
    reasons: tuple[str, ...]
    numbers: dict[str, object]
    verdict: Verdict | None = None
    expiry: dt.date | None = None
    direction: Direction | None = None
    trigger: Break | None = None
    #: The opening range the trigger broke and the invalidation rule reads (``04`` §4.5).
    or_high: Decimal | None = None
    or_low: Decimal | None = None
    candidate: Candidate | None = None
    legs: tuple[PlanLeg, ...] = ()
    as_of_minute: dt.datetime | None = None
    costs: ChargeBreakdown | None = None

    @property
    def writes_session(self) -> bool:
        return self.state in (PlanState.PLANNED, PlanState.SKIPPED)


@dataclass(frozen=True, slots=True)
class O2Exits:
    """``04`` §4.5 written out at plan time, on the mark the rules read (the option's **bid**).

    ``E`` is the planned entry (``ask + 1 tick``); the fill replaces it when the desk opens the
    position, and ``time_stop_minutes`` runs from that fill, not from the plan.
    """

    stop_price: Decimal
    target_price: Decimal
    invalidation_level: Decimal
    time_stop_minutes: int
    time_stop_min_price: Decimal
    hard_exit_at: dt.datetime


@dataclass(frozen=True, slots=True)
class O2Plan:
    """One ``op_plan`` row and its single ``op_leg`` row, ready to write."""

    plan_id: str
    sleeve: Sleeve
    trade_date: dt.date
    expiry: dt.date
    mode: Mode
    structure: Structure
    direction: Direction
    issued_at: dt.datetime
    entry_window_end: dt.datetime
    expires_at: dt.datetime
    debit_points: Decimal
    lot_size: int
    lots: int
    sizing_mode: SizingMode
    half_size: bool
    risk_per_lot_inr: Decimal
    risk_budget_inr: Decimal
    premium_inr: Decimal
    max_loss_inr: Decimal
    gap_through_inr: Decimal
    profit_target_inr: Decimal
    stop_inr: Decimal
    expected_cost_inr: Decimal
    cost_share: Decimal
    costs: ChargeBreakdown
    exits: O2Exits
    warnings: tuple[str, ...]
    legs: tuple[PlanLeg, ...]
    as_of_minute: dt.datetime | None
    numbers: dict[str, object] = field(default_factory=dict)

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size


@dataclass(frozen=True, slots=True)
class O2Outcome:
    """The second phase's answer: the plan, or the decision that ended the day."""

    decision: O2Decision
    plan: O2Plan | None
    state: PlanState
    reasons: tuple[str, ...]


def _at(day: dt.date, clock: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, clock, tzinfo=IST)


def _q(value: Decimal | None, places: str = "0.01") -> str | None:
    return None if value is None else str(value.quantize(Decimal(places)))


def _t(value: dt.time | None) -> str | None:
    return None if value is None else value.strftime("%H:%M")


def day_numbers(
    filters: DayFilters, config: DirectionalConfig, expiry: dt.date | None
) -> dict[str, object]:
    """``04`` §4.1's numbers beside their thresholds — the scan's vocabulary (``04`` §10)."""
    return {
        "trend": None if filters.trend is None else filters.trend.value,
        "ema": _q(filters.ema),
        "trend_ema_days": config.trend_ema_days,
        "index": "NIFTY 50",
        "prev_close": _q(filters.prev_close),
        "open_0915": _q(filters.open_0915),
        "gap_pct": _q(filters.gap_pct, "0.0001"),
        "gap_max_pct": str(config.gap_max_pct),
        "or_high": _q(filters.or_high),
        "or_low": _q(filters.or_low),
        "or_pct": _q(filters.or_pct, "0.0001"),
        "or_max_pct": str(config.or_max_pct),
        "vix": _q(filters.vix),
        "vix_max": str(config.vix_max),
        "bars": filters.bars,
        "expiry": expiry.isoformat() if expiry else None,
    }


def trigger_numbers(trig: TriggerScan, direction: Direction) -> dict[str, object]:
    """``04`` §4.2's levels, the trigger and every counter-trend break seen (never traded)."""
    level = trig.level_up if direction is Direction.UP else trig.level_down
    out: dict[str, object] = {
        "direction": direction.value,
        "trigger_level": _q(level),
        "level_up": _q(trig.level_up),
        "level_down": _q(trig.level_down),
        "last_close": _q(trig.last_close),
        "counter_trend_breaks": [
            {"close_time": _t(b.close_time), "close": _q(b.close), "direction": b.direction.value}
            for b in trig.counter_breaks
        ],
    }
    if trig.trigger is not None:
        out |= {
            "trigger_time": _t(trig.trigger.close_time),
            "trigger_close": _q(trig.trigger.close),
        }
    return out


def decision_minute_for(trigger: Break, day: dt.date) -> dt.datetime:
    """The minute a plan prices from: the one after the trigger bar's close minute (OP4.4)."""
    return _at(day, minute_after(trigger.close_time))


def decide_o2(  # noqa: PLR0911, PLR0912, PLR0913 - one return per stage; 06 OP7's inputs
    market: MarketDay,
    context: DayContext,
    snapshots: SnapshotBook,
    now: dt.datetime,
    *,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
) -> O2Decision:
    """Day filters → trigger → expiry → strike → delta → sizing → cost test, at ``now``.

    ``snapshots`` is asked for exactly one minute — the trigger minute's chain, the one the scan
    prices its candidate from — so :func:`wanted_minutes` can learn what to load by running this
    over an empty book.
    """
    cfg = options.directional
    day = market.trade_date

    def decided(state: PlanState, reasons: Sequence[str], **rest: object) -> O2Decision:
        return _decision(day, now, state, tuple(reasons), rest)

    day_role = role(
        day,
        Sleeve.O2,
        rows=market.contracts,
        event_days=context.event_days,
        trading_day=market.trading_day,
    )
    if not day_role.trades and day_role.reason is not RoleReason.EVENT_DAY:
        return decided(PlanState.NO_SESSION, [day_role.reason.value])
    expiry = expiry_for_o2(day, market.contracts, options.calendar.underlying)
    if ist(now).time() < cfg.entry_window_start:
        return decided(PlanState.NOT_READY, [BEFORE_ENTRY_WINDOW], expiry=expiry)
    if not day_role.trades:  # an event day: skipped, never shifted (04 §1.3)
        return decided(
            PlanState.SKIPPED, [RoleReason.EVENT_DAY.value], expiry=expiry, verdict=Verdict.SKIP
        )
    filters = directional.day_filters(
        market.bars,
        market.daily_closes,
        market.vix_prev_close,
        now=now,
        event_day=False,
        config=cfg,
        grace_seconds=options.chain.stale_scan_seconds,
    )
    numbers = day_numbers(filters, cfg, expiry)
    if not filters.final:
        return decided(PlanState.NOT_READY, [RANGE_NOT_SETTLED], expiry=expiry, numbers=numbers)
    if filters.reasons or filters.trend is None:
        return decided(
            PlanState.SKIPPED,
            [r.value for r in filters.reasons],
            expiry=expiry,
            numbers=numbers,
            verdict=Verdict.SKIP,
        )
    direction = filters.trend
    or_high, or_low = filters.or_high or Decimal(0), filters.or_low or Decimal(0)
    trig = directional.find_trigger(
        market.bars,
        or_high=or_high,
        or_low=or_low,
        direction=direction,
        now=now,
        config=cfg,
        session_open=options.calendar.market_open,
        grace_seconds=options.chain.stale_scan_seconds,
    )
    numbers |= trigger_numbers(trig, direction)
    common: dict[str, object] = {
        "expiry": expiry,
        "numbers": numbers,
        "direction": direction,
        "or_high": or_high,
        "or_low": or_low,
    }
    if trig.trigger is None:
        if trig.window_closed:
            return decided(PlanState.SKIPPED, [NO_TRIGGER], verdict=Verdict.SKIP, **common)
        return decided(PlanState.NOT_READY, [NO_TRIGGER_YET], **common)
    common["trigger"] = trig.trigger
    if ist(now).time() >= cfg.entry_window_end:  # a plan issued now could not outlive its issue
        return decided(PlanState.WINDOW_CLOSED, [ENTRY_WINDOW_CLOSED], **common)
    if expiry is None:
        return decided(PlanState.NOT_READY, [NO_EXPIRY], **common)
    minute = decision_minute_for(trig.trigger, day)
    snapshot = snapshots.get(minute)
    if snapshot is None:
        return decided(PlanState.NOT_READY, [NO_DECISION_SNAPSHOT], **common)
    view = priced_view(snapshot, expiry, market, options)
    mine = context.of(Sleeve.O2)
    candidate = directional.build(
        view,
        direction,
        lot_size=lot_size_for(market.contracts, expiry, options.calendar.underlying),
        book=mine.book,
        config=cfg,
        options=options,
        ceilings=ceilings,
        expiry=expiry,
        paused=mine.paused,
    )
    common |= {
        "verdict": Verdict.TRADE,
        "candidate": candidate,
        "as_of_minute": snapshot.ts,
    }
    if candidate.rejection is not None:
        return decided(PlanState.SKIPPED, [candidate.rejection.value], **common)
    age = (ist(now) - ist(snapshot.ts)).total_seconds()
    if age > options.chain.stale_scan_seconds:
        return decided(PlanState.SKIPPED, [STALE_CHAIN], **common)
    if view is None:  # a viable candidate always had a view; kept for the type checker
        raise ValueError("a viable candidate without a chain")
    legs = plan_legs(candidate, market.contracts, snapshot)
    costs = plan_costs(candidate, view.tick, options)
    return decided(PlanState.PLANNED, [], legs=legs, costs=costs, **common)


def _decision(
    day: dt.date,
    now: dt.datetime,
    state: PlanState,
    reasons: tuple[str, ...],
    rest: dict[str, object],
) -> O2Decision:
    numbers = rest.get("numbers")
    expiry = rest.get("expiry")
    verdict = rest.get("verdict")
    direction = rest.get("direction")
    trigger = rest.get("trigger")
    candidate = rest.get("candidate")
    legs = rest.get("legs")
    as_of = rest.get("as_of_minute")
    costs = rest.get("costs")
    or_high, or_low = rest.get("or_high"), rest.get("or_low")
    return O2Decision(
        sleeve=Sleeve.O2,
        trade_date=day,
        now=now,
        state=state,
        reasons=reasons,
        numbers=dict(numbers) if isinstance(numbers, dict) else {},
        verdict=verdict if isinstance(verdict, Verdict) else None,
        expiry=expiry if isinstance(expiry, dt.date) else None,
        direction=direction if isinstance(direction, Direction) else None,
        trigger=trigger if isinstance(trigger, Break) else None,
        or_high=or_high if isinstance(or_high, Decimal) else None,
        or_low=or_low if isinstance(or_low, Decimal) else None,
        candidate=candidate if isinstance(candidate, Candidate) else None,
        legs=legs if isinstance(legs, tuple) else (),
        as_of_minute=as_of if isinstance(as_of, dt.datetime) else None,
        costs=costs if isinstance(costs, ChargeBreakdown) else None,
    )


def wanted_minutes(
    market: MarketDay,
    context: DayContext,
    now: dt.datetime,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
) -> frozenset[dt.datetime | None]:
    """The snapshot minutes :func:`decide_o2` will ask for at ``now`` (at most the trigger's).

    The scan's idiom (``scan.wanted_minutes``): run the decision over an empty book and record
    what it asked for, so the database layer loads exactly those minutes and no others.
    """
    book = SnapshotBook()
    decide_o2(market, context, book, now, options=options, ceilings=ceilings)
    return frozenset(book.asked)


def exits_for(
    entry: Decimal,
    day: dt.date,
    invalidation_level: Decimal,
    config: DirectionalConfig,
) -> O2Exits:
    """``04`` §4.5's levels for a long bought at ``entry``.

    ``invalidation_level`` is the opening range's own edge — ``or_high`` for a call, ``or_low``
    for a put: a completed 5-minute index close back inside the range invalidates the break.
    """
    return O2Exits(
        stop_price=paise(entry * (Decimal(1) - config.stop_frac)),
        target_price=paise(entry * (Decimal(1) + config.target_frac)),
        invalidation_level=invalidation_level,
        time_stop_minutes=config.time_stop_minutes,
        time_stop_min_price=paise(entry * (Decimal(1) + config.time_stop_min_gain)),
        hard_exit_at=_at(day, config.hard_exit_time),
    )


def finalize_o2(
    decision: O2Decision,
    *,
    user_id: int,
    mode: Mode,
    options: OptionsConfig,
) -> O2Outcome:
    """The ``plan_id``, the 30-minute expiry and ``04`` §4.5's exits — no broker read (OP7.3).

    A decision that is not ``PLANNED`` passes straight through: this phase adds nothing a refusal
    needs.
    """
    if decision.state is not PlanState.PLANNED:
        return O2Outcome(decision, None, decision.state, decision.reasons)
    candidate = decision.candidate
    if (
        candidate is None
        or decision.expiry is None
        or decision.costs is None
        or decision.direction is None
    ):
        raise ValueError("a PLANNED decision carries its candidate, expiry, direction and costs")
    assert_never_naked(decision.legs)
    cfg = options.directional
    issued = ist(decision.now).replace(tzinfo=IST)
    window_end = _at(decision.trade_date, cfg.entry_window_end)
    expires = plan_expires_at(issued, cfg.entry_window_end, options.execution.plan_ttl_minutes)
    lot_size = candidate.lot_size or 0
    quantity = candidate.lots * lot_size
    entry = candidate.points or Decimal(0)
    invalidation = (
        decision.or_high if decision.direction is Direction.UP else decision.or_low
    ) or Decimal(0)
    plan = O2Plan(
        plan_id=plan_id_for(user_id, decision),
        sleeve=Sleeve.O2,
        trade_date=decision.trade_date,
        expiry=decision.expiry,
        mode=mode,
        structure=candidate.structure,
        direction=decision.direction,
        issued_at=issued,
        entry_window_end=window_end,
        expires_at=expires,
        debit_points=entry,
        lot_size=lot_size,
        lots=candidate.lots,
        sizing_mode=candidate.sizing_mode or SizingMode.PAPER_ONE_LOT,
        half_size=candidate.half_size,
        risk_per_lot_inr=paise(candidate.risk_per_lot_inr or Decimal(0)),
        risk_budget_inr=paise(candidate.r_inr or Decimal(0)),
        premium_inr=paise(entry * quantity),
        max_loss_inr=paise(candidate.max_loss_inr or Decimal(0)),
        gap_through_inr=paise(gap_through_long(entry, lot_size) * candidate.lots),
        profit_target_inr=paise(cfg.target_frac * entry * quantity),
        stop_inr=paise(cfg.stop_frac * entry * quantity),
        expected_cost_inr=decision.costs.total,
        cost_share=(candidate.cost_share or Decimal(0)).quantize(Decimal("0.0001")),
        costs=decision.costs,
        exits=exits_for(entry, decision.trade_date, invalidation, cfg),
        warnings=tuple(candidate.warnings),
        legs=decision.legs,
        as_of_minute=decision.as_of_minute,
        numbers=decision.numbers,
    )
    return O2Outcome(decision, plan, PlanState.PLANNED, ())


def leg_of(plan: O2Plan) -> PlanLeg:
    """The plan's one leg — O2 is a single long (``04`` §4.3)."""
    (leg,) = plan.legs
    return leg


def contract_label(leg: PlanLeg) -> str:
    """``25000 CE`` — the strike and right, for an alert or a page."""
    return f"{leg.strike.quantize(Decimal(1))} {leg.option_type.value}"


__all__ = [
    "BEFORE_ENTRY_WINDOW",
    "ENTRY_WINDOW_CLOSED",
    "NO_DECISION_SNAPSHOT",
    "NO_EXPIRY",
    "NO_TRIGGER",
    "NO_TRIGGER_YET",
    "RANGE_NOT_SETTLED",
    "O2Decision",
    "O2Exits",
    "O2Outcome",
    "O2Plan",
    "contract_label",
    "day_numbers",
    "decide_o2",
    "decision_minute_for",
    "exits_for",
    "finalize_o2",
    "leg_of",
    "trigger_numbers",
    "wanted_minutes",
]
