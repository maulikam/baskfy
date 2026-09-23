"""The O3 plan builder, pure: a reasoned no-trade or one two-leg debit spread (``06`` OP8).

``build_plan(O3A | O3B, date, trigger)`` in ``06`` is, in order: the day's role → the setup (``04``
§5.1 range break, §5.2 gap hold) → the day's slot (§8.6) and one O3 a day (§5.5) → the chain →
``expiry_setups.build`` (strikes, debit ≤ ``max_debit_frac`` x width, sizing, liquidity, cost) →
margin → ``op_plan`` / ``op_leg``, or ``SKIPPED`` with the code. This module is everything in that
sentence that is arithmetic; the worker (``baskfy_worker.options.plan_o3``) reads the rows, asks
the broker's margin calculator and writes the result.

Two phases, O1's shape, because ``04`` §7.4 asks the margin calculator for O3 as for O1:

* :func:`decide_o3` — role, setup, slot and structure by **exactly** the functions the scan calls
  (``expiry_setups.gap_hold`` / ``range_break`` and ``expiry_setups.build`` over the decision
  minute's snapshot — ``scan.priced_view``, OP4.4). Its candidate therefore equals the scan's
  (tested). The decision minute is ``o3b_plan_time`` for O3-B and the minute after the trigger
  bar's close for O3-A — the minutes the scan prices from.
* :func:`finalize_o3` — the margin ceiling (``sizing.margin_check``, never a source of size), the
  ``plan_id``, ``expires_at = min(issued + 30 min, entry window end)`` and ``04`` §5.3's exits
  written out.

**Never naked.** The legs are listed in ``execution.entry_sequence`` order — the long, then the
short (``04`` §5.4) — and :func:`finalize_o3` asserts ``never_naked`` at every prefix. **Nothing
here places anything**: a plan is a proposal the desk's confirm (OP10) may act on within its 30
minutes; there is no auto-execute for any options sleeve (PACK.3).

**One O3 a day** (``04`` §5.5, OP8.2). O3-B decides first (its window closes at 10:00, before
O3-A's first possible trigger at 10:19). While O3-B's session is ``PLANNED``, ``CONFIRMED``,
``OPEN`` or ``CLOSED``, O3-A is refused ``REJECTED_SLOT_TAKEN`` with ``O3B_HOLDS``; a lapsed or
skipped O3-B frees it. **The expiry-day slot** (§8.6) is separate: whichever O1-or-O3 plan is
*confirmed* first holds it, and every other one is ``REJECTED_SLOT_TAKEN``.

Which days write a session (OP8.3): an expiry day with the setup enabled writes one per setup —
``PLANNED``, or ``SKIPPED`` with every reason. An event expiry is ``SKIPPED / EVENT_DAY`` (``04``
§1.3); any other day, or a disabled setup, is ``NO_SESSION`` and writes nothing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from baskfy_core.options import expiry_setups
from baskfy_core.options.bars import IST, ist, minute_after
from baskfy_core.options.calendar import RoleReason, expiry_for_o1_o3, lot_size_for, role
from baskfy_core.options.config import (
    ExpirySetupsConfig,
    Mode,
    OptionsCeilings,
    OptionsConfig,
    SizingMode,
    Sleeve,
)
from baskfy_core.options.costs import ChargeBreakdown, paise
from baskfy_core.options.plan import (
    STALE_CHAIN,
    MarginLeg,
    MarginQuote,
    PlanLeg,
    PlanState,
    Verdict,
    assert_never_naked,
    plan_costs,
    plan_id_for,
    plan_legs,
)
from baskfy_core.options.scan import DayContext, MarketDay, SnapshotBook, priced_view
from baskfy_core.options.session import SessionState, plan_expires_at
from baskfy_core.options.sizing import MarginCheck, MarginCode, margin_check
from baskfy_core.options.structures import Candidate, Direction, Structure

O3_SLEEVES: frozenset[Sleeve] = frozenset({Sleeve.O3A, Sleeve.O3B})

#: The setup is switched off in config (``o3a_enabled`` / ``o3b_enabled``): no session.
SETUP_DISABLED = "SETUP_DISABLED"
#: No previous NIFTY 50 close, so neither the gap nor the range can be judged.
NO_PREV_CLOSE = "NO_PREV_CLOSE"
#: Before the setup's first decision time (O3-B's plan time, O3-A's first trigger bar).
BEFORE_PLAN_TIME = "BEFORE_PLAN_TIME"
#: The morning window (O3-B's hold, O3-A's range) is not judged yet.
WINDOW_NOT_SETTLED = "WINDOW_NOT_SETTLED"
#: O3-A's trigger window closed with no break at the required efficiency: a skipped session.
NO_TRIGGER = "NO_TRIGGER"
#: O3-A's trigger window is open and nothing has broken yet.
NO_TRIGGER_YET = "NO_TRIGGER_YET"
#: The decision minute's chain is not stored yet.
NO_DECISION_SNAPSHOT = "NO_DECISION_SNAPSHOT"
#: First asked after the entry window with nothing decided: nothing is written.
ENTRY_WINDOW_CLOSED = "ENTRY_WINDOW_CLOSED"
#: ``04`` §5.5: O3-B planned (or traded) today, so O3-A may not.
O3B_HOLDS = "O3B_HOLDS"

#: O3-B's session states that hold the day's one O3 trade (OP8.2).
_O3B_HOLDING: frozenset[SessionState] = frozenset(
    {SessionState.PLANNED, SessionState.CONFIRMED, SessionState.OPEN, SessionState.CLOSED}
)


@dataclass(frozen=True, slots=True)
class O3Decision:
    """The first phase's answer: every reason, the setup's numbers, the priced candidate."""

    sleeve: Sleeve
    trade_date: dt.date
    now: dt.datetime
    state: PlanState
    reasons: tuple[str, ...]
    numbers: dict[str, object]
    verdict: Verdict | None = None
    expiry: dt.date | None = None
    direction: Direction | None = None
    #: O3-A's morning range, or O3-B's ``half_gap`` in both: what invalidation reads (§5.3).
    range_high: Decimal | None = None
    range_low: Decimal | None = None
    half_gap: Decimal | None = None
    candidate: Candidate | None = None
    legs: tuple[PlanLeg, ...] = ()
    as_of_minute: dt.datetime | None = None
    costs: ChargeBreakdown | None = None

    @property
    def writes_session(self) -> bool:
        return self.state in (PlanState.PLANNED, PlanState.SKIPPED)

    def margin_baskets(self) -> tuple[tuple[MarginLeg, ...], tuple[MarginLeg, ...]]:
        """The hedged basket (both legs) and no transient one: the only entry prefix short of the
        whole spread is the long alone, which costs its premium and nothing more (OP8.4)."""
        if self.state is not PlanState.PLANNED or not self.legs:
            return (), ()
        return tuple(MarginLeg(lg.tradingsymbol, lg.side, lg.quantity) for lg in self.legs), ()


@dataclass(frozen=True, slots=True)
class O3Exits:
    """``04`` §5.3 written out at plan time, on the close value ``V = long.bid - short.ask``.

    ``D`` is the planned debit; the fill replaces it when the desk opens the position, and the
    stop is then re-derived from the filled debit (§5.3: "``D`` = the entry debit from fills").
    """

    target_value: Decimal
    stop_value: Decimal
    #: O3-A: a 5-minute close back inside ``[range_low, range_high]`` on the broken side;
    #: O3-B: any bar through ``half_gap``.
    invalidation: str
    invalidation_level: Decimal
    hard_exit_at: dt.datetime


@dataclass(frozen=True, slots=True)
class O3Plan:
    """One ``op_plan`` row and its two ``op_leg`` rows, ready to write."""

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
    width_points: Decimal
    lot_size: int
    lots: int
    sizing_mode: SizingMode
    half_size: bool
    risk_per_lot_inr: Decimal
    risk_budget_inr: Decimal
    debit_inr: Decimal
    max_loss_inr: Decimal
    profit_target_inr: Decimal
    stop_inr: Decimal
    expected_cost_inr: Decimal
    cost_share: Decimal
    costs: ChargeBreakdown
    margin_required_inr: Decimal | None
    margin: MarginCheck | None
    exits: O3Exits
    warnings: tuple[str, ...]
    legs: tuple[PlanLeg, ...]
    as_of_minute: dt.datetime | None
    numbers: dict[str, object] = field(default_factory=dict)

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size


@dataclass(frozen=True, slots=True)
class O3Outcome:
    """The second phase's answer: the plan, or the decision that ended the day."""

    decision: O3Decision
    plan: O3Plan | None
    state: PlanState
    reasons: tuple[str, ...]


def _at(day: dt.date, clock: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, clock, tzinfo=IST)


def _q(value: Decimal | None, places: str = "0.01") -> str | None:
    return None if value is None else str(value.quantize(Decimal(places)))


def _t(value: dt.time | None) -> str | None:
    return None if value is None else value.strftime("%H:%M")


def entry_window_end(sleeve: Sleeve, config: ExpirySetupsConfig) -> dt.time:
    """O3-B: ``o3b_window_end`` (§5.2, 10:00). O3-A: ``o3a_entry_window_end`` (OP8.1, 13:30) —
    half an hour past the last trigger bar, so a 13:00 trigger still gets its full 30 minutes."""
    return config.o3b_window_end if sleeve is Sleeve.O3B else config.o3a_entry_window_end


def first_decision_time(sleeve: Sleeve, config: ExpirySetupsConfig) -> dt.time:
    """The earliest clock a setup can decide at: O3-B's plan time, O3-A's first trigger bar."""
    return config.o3b_plan_time if sleeve is Sleeve.O3B else config.o3a_window_start


def o3b_holds(context: DayContext) -> bool:
    """``04`` §5.5 on the user's rows: O3-B has a plan today that has not lapsed (OP8.2)."""
    state = context.of(Sleeve.O3B).session_state
    return state is not None and state in _O3B_HOLDING


def decide_o3(  # noqa: PLR0911, PLR0912, PLR0913 - one return per stage; every input 06 OP8 names
    sleeve: Sleeve,
    market: MarketDay,
    context: DayContext,
    snapshots: SnapshotBook,
    now: dt.datetime,
    *,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
) -> O3Decision:
    """Role → setup → slot → chain → spread → sizing → cost test, for one O3 setup at ``now``.

    ``snapshots`` is asked for at most one minute — the decision minute's chain — so
    :func:`wanted_minutes` can learn what to load by running this over an empty book.
    """
    if sleeve not in O3_SLEEVES:
        raise ValueError(f"{sleeve.value} is not an O3 setup")
    cfg = options.expiry_setups
    day = market.trade_date

    def decided(state: PlanState, reasons: Sequence[str], **rest: object) -> O3Decision:
        return _decision(sleeve, day, now, state, tuple(reasons), rest)

    day_role = role(
        day,
        sleeve,
        rows=market.contracts,
        event_days=context.event_days,
        trading_day=market.trading_day,
    )
    if not day_role.trades and day_role.reason is not RoleReason.EVENT_DAY:
        return decided(PlanState.NO_SESSION, [day_role.reason.value])
    enabled = cfg.o3a_enabled if sleeve is Sleeve.O3A else cfg.o3b_enabled
    if not enabled:
        return decided(PlanState.NO_SESSION, [SETUP_DISABLED])
    expiry = expiry_for_o1_o3(day)
    clock = ist(now).time()
    if clock < first_decision_time(sleeve, cfg):
        return decided(PlanState.NOT_READY, [BEFORE_PLAN_TIME], expiry=expiry)
    if not day_role.trades:  # an event expiry: skipped, never shifted (04 §1.3)
        return decided(
            PlanState.SKIPPED, [RoleReason.EVENT_DAY.value], expiry=expiry, verdict=Verdict.SKIP
        )
    if not market.daily_closes:
        return decided(PlanState.SKIPPED, [NO_PREV_CLOSE], expiry=expiry, verdict=Verdict.SKIP)
    prev_close = market.daily_closes[-1]
    if sleeve is Sleeve.O3B:
        setup = _gap_hold(market, prev_close, now, options)
    else:
        setup = _range_break(market, prev_close, now, options)
    common: dict[str, object] = {"expiry": expiry} | setup.fields
    if setup.state is not None:
        return decided(setup.state, setup.reasons, **common)
    if clock >= entry_window_end(sleeve, cfg):  # a plan issued now could not outlive its issue
        return decided(PlanState.WINDOW_CLOSED, [ENTRY_WINDOW_CLOSED], **common)
    snapshot = snapshots.get(setup.decision_minute)
    if snapshot is None:
        return decided(PlanState.NOT_READY, [NO_DECISION_SNAPSHOT], **common)
    holds = sleeve is Sleeve.O3A and o3b_holds(context)
    mine = context.of(sleeve)
    view = priced_view(snapshot, expiry, market, options)
    direction = setup.direction
    if direction is None:  # a decided setup always has one; kept for the type checker
        raise ValueError("a triggered setup without a direction")
    candidate = expiry_setups.build(
        view,
        direction,
        lot_size=lot_size_for(market.contracts, expiry, options.calendar.underlying),
        book=mine.book,
        config=cfg,
        options=options,
        ceilings=ceilings,
        expiry=expiry,
        slot_free=(context.slot_holder is None or context.slot_holder is sleeve) and not holds,
        paused=mine.paused,
    )
    common |= {
        "verdict": Verdict.TRADE,
        "candidate": candidate,
        "as_of_minute": snapshot.ts,
    }
    if candidate.rejection is not None:
        reasons = [candidate.rejection.value] + ([O3B_HOLDS] if holds else [])
        return decided(PlanState.SKIPPED, reasons, **common)
    age = (ist(now) - ist(snapshot.ts)).total_seconds()
    if age > options.chain.stale_scan_seconds:
        return decided(PlanState.SKIPPED, [STALE_CHAIN], **common)
    if view is None:  # a viable candidate always had a view; kept for the type checker
        raise ValueError("a viable candidate without a chain")
    legs = plan_legs(candidate, market.contracts, snapshot)
    costs = plan_costs(candidate, view.tick, options)
    return decided(PlanState.PLANNED, [], legs=legs, costs=costs, **common)


@dataclass(frozen=True, slots=True)
class _Setup:
    """A setup's verdict at ``now``: a terminal ``state`` with its reasons, or (``state is
    None``) a trigger with the minute to price it from."""

    state: PlanState | None
    reasons: tuple[str, ...]
    fields: dict[str, object]
    direction: Direction | None = None
    decision_minute: dt.datetime | None = None


def _gap_hold(
    market: MarketDay, prev_close: Decimal, now: dt.datetime, options: OptionsConfig
) -> _Setup:
    """``04`` §5.2, by the scan's function; decided at ``o3b_plan_time``."""
    cfg = options.expiry_setups
    gh = expiry_setups.gap_hold(
        market.bars,
        prev_close,
        now=now,
        event_day=False,
        config=cfg,
        session_open=options.calendar.market_open,
        grace_seconds=options.chain.stale_scan_seconds,
    )
    numbers: dict[str, object] = {
        "setup": "GAP_HOLD",
        "prev_close": _q(prev_close),
        "open_0915": _q(gh.open_0915),
        "gap_points": _q(gh.gap_points),
        "gap_pct": _q(gh.gap_pct, "0.0001"),
        "gap_min_pct": str(cfg.o3b_gap_min_pct),
        "gap_max_pct": str(cfg.o3b_gap_max_pct),
        "half_gap": _q(gh.half_gap),
        "direction": None if gh.direction is None else gh.direction.value,
        "held": gh.held_so_far,
        "bars": gh.bars,
        "plan_time": _t(cfg.o3b_plan_time),
        "window_end": _t(cfg.o3b_window_end),
    }
    fields: dict[str, object] = {
        "numbers": numbers,
        "direction": gh.direction,
        "half_gap": gh.half_gap,
    }
    if not gh.final:
        return _Setup(PlanState.NOT_READY, (WINDOW_NOT_SETTLED,), fields)
    if gh.reasons:
        reasons = tuple(r.value for r in gh.reasons)
        return _Setup(PlanState.SKIPPED, reasons, fields | {"verdict": Verdict.SKIP})
    if not gh.triggered or gh.direction is None:  # final and clean: triggered from plan time
        return _Setup(PlanState.NOT_READY, (BEFORE_PLAN_TIME,), fields)
    minute = _at(market.trade_date, cfg.o3b_plan_time)
    return _Setup(None, (), fields, gh.direction, minute)


def _range_break(
    market: MarketDay, prev_close: Decimal, now: dt.datetime, options: OptionsConfig
) -> _Setup:
    """``04`` §5.1, by the scan's function; decided on the minute after the trigger bar."""
    cfg = options.expiry_setups
    rb = expiry_setups.range_break(
        market.bars,
        prev_close,
        now=now,
        event_day=False,
        config=cfg,
        session_open=options.calendar.market_open,
        grace_seconds=options.chain.stale_scan_seconds,
    )
    numbers: dict[str, object] = {
        "setup": "RANGE_BREAK",
        "prev_close": _q(prev_close),
        "range_high": _q(rb.range_high),
        "range_low": _q(rb.range_low),
        "range_pct": _q(rb.range_pct, "0.0001"),
        "range_max_pct": str(cfg.o3a_range_max_pct),
        "bars": rb.bars,
        "level_up": _q(rb.level_up),
        "level_down": _q(rb.level_down),
        "er_min": str(cfg.o3a_er_min),
        "window_start": _t(cfg.o3a_window_start),
        "window_end": _t(cfg.o3a_window_end),
        "low_er_breaks": [
            {"close_time": _t(t), "close": _q(c), "er": _q(e, "0.0001")}
            for t, c, e in rb.low_er_breaks
        ],
    }
    fields: dict[str, object] = {
        "numbers": numbers,
        "range_high": rb.range_high,
        "range_low": rb.range_low,
    }
    if not rb.final:
        return _Setup(PlanState.NOT_READY, (WINDOW_NOT_SETTLED,), fields)
    if rb.reasons:
        reasons = tuple(r.value for r in rb.reasons)
        return _Setup(PlanState.SKIPPED, reasons, fields | {"verdict": Verdict.SKIP})
    if rb.trigger_time is None or rb.direction is None:
        if rb.window_closed:
            return _Setup(PlanState.SKIPPED, (NO_TRIGGER,), fields | {"verdict": Verdict.SKIP})
        return _Setup(PlanState.NOT_READY, (NO_TRIGGER_YET,), fields)
    numbers |= {
        "trigger_time": _t(rb.trigger_time),
        "trigger_close": _q(rb.trigger_close),
        "trigger_er": _q(rb.trigger_er, "0.0001"),
        "direction": rb.direction.value,
    }
    minute = _at(market.trade_date, minute_after(rb.trigger_time))
    return _Setup(None, (), fields | {"direction": rb.direction}, rb.direction, minute)


def _decision(  # noqa: PLR0913, PLR0917 - the decision's identity and the rest by name
    sleeve: Sleeve,
    day: dt.date,
    now: dt.datetime,
    state: PlanState,
    reasons: tuple[str, ...],
    rest: dict[str, object],
) -> O3Decision:
    def dec(key: str) -> Decimal | None:
        value = rest.get(key)
        return value if isinstance(value, Decimal) else None

    numbers = rest.get("numbers")
    expiry = rest.get("expiry")
    verdict = rest.get("verdict")
    direction = rest.get("direction")
    candidate = rest.get("candidate")
    legs = rest.get("legs")
    as_of = rest.get("as_of_minute")
    costs = rest.get("costs")
    return O3Decision(
        sleeve=sleeve,
        trade_date=day,
        now=now,
        state=state,
        reasons=reasons,
        numbers=dict(numbers) if isinstance(numbers, dict) else {},
        verdict=verdict if isinstance(verdict, Verdict) else None,
        expiry=expiry if isinstance(expiry, dt.date) else None,
        direction=direction if isinstance(direction, Direction) else None,
        range_high=dec("range_high"),
        range_low=dec("range_low"),
        half_gap=dec("half_gap"),
        candidate=candidate if isinstance(candidate, Candidate) else None,
        legs=legs if isinstance(legs, tuple) else (),
        as_of_minute=as_of if isinstance(as_of, dt.datetime) else None,
        costs=costs if isinstance(costs, ChargeBreakdown) else None,
    )


def wanted_minutes(  # noqa: PLR0913, PLR0917 - the setup, the market, the user, the clock and the rules
    sleeve: Sleeve,
    market: MarketDay,
    context: DayContext,
    now: dt.datetime,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
) -> frozenset[dt.datetime | None]:
    """The snapshot minutes :func:`decide_o3` will ask for at ``now`` (at most one)."""
    book = SnapshotBook()
    decide_o3(sleeve, market, context, book, now, options=options, ceilings=ceilings)
    return frozenset(book.asked)


def exits_for(decision: O3Decision, debit: Decimal, config: ExpirySetupsConfig) -> O3Exits:
    """``04`` §5.3's levels for a spread bought at ``debit`` (per unit)."""
    if decision.direction is None:
        raise ValueError("a planned spread has a direction")
    if decision.sleeve is Sleeve.O3B:
        rule = "O3B: any 1-minute bar through half_gap"
        level = decision.half_gap
    else:
        rule = "O3A: a completed 5-minute close back inside the morning range"
        level = decision.range_high if decision.direction is Direction.UP else decision.range_low
    if level is None:
        raise ValueError("a planned spread knows its invalidation level")
    return O3Exits(
        target_value=paise(config.target_frac_of_width * config.width_points),
        stop_value=paise(config.stop_frac_of_debit * debit),
        invalidation=rule,
        invalidation_level=paise(level),  # rounded at write time (house rule 8)
        hard_exit_at=_at(decision.trade_date, config.hard_exit_time),
    )


def finalize_o3(  # noqa: PLR0913 - the decision, the broker's answer, the book and the clock
    decision: O3Decision,
    margin: MarginQuote | None,
    *,
    user_id: int,
    mode: Mode,
    margin_pool_inr: Decimal,
    margin_in_use_inr: Decimal,
    options: OptionsConfig,
) -> O3Outcome:
    """The margin ceiling (``04`` §7.4, never a source of size), the ``plan_id``, the 30-minute
    expiry and the exits, or ``REJECTED_MARGIN``.

    With no answer from the calculator (``margin=None``): a paper plan is issued with the warning
    ``MARGIN_UNKNOWN``; a live plan is ``REJECTED_MARGIN`` (OP6.5, carried).
    """
    if decision.state is not PlanState.PLANNED:
        return O3Outcome(decision, None, decision.state, decision.reasons)
    candidate = decision.candidate
    if (
        candidate is None
        or decision.expiry is None
        or decision.costs is None
        or decision.direction is None
    ):
        raise ValueError("a PLANNED decision carries its candidate, expiry, direction and costs")
    assert_never_naked(decision.legs)
    warnings = list(candidate.warnings)
    check: MarginCheck | None = None
    if margin is None:
        if mode is Mode.LIVE:
            return _margin_rejected(decision, "the broker's margin calculator did not answer")
        warnings.append("MARGIN_UNKNOWN")
    else:
        check = margin_check(
            mode=mode,
            hedged_estimate_inr=margin.hedged_inr,
            transient_estimate_inr=margin.transient_inr,
            margin_pool_inr=margin_pool_inr,
            margin_in_use_inr=margin_in_use_inr,
        )
        if check.rejected:
            return _margin_rejected(decision, check.message)
        if check.code is MarginCode.MARGIN_POOL_UNSET:
            warnings.append(MarginCode.MARGIN_POOL_UNSET.value)
    cfg = options.expiry_setups
    issued = ist(decision.now).replace(tzinfo=IST)
    window = entry_window_end(decision.sleeve, cfg)
    expires = plan_expires_at(issued, window, options.execution.plan_ttl_minutes)
    lot_size = candidate.lot_size or 0
    quantity = candidate.lots * lot_size
    debit = candidate.points or Decimal(0)
    width = cfg.width_points
    plan = O3Plan(
        plan_id=plan_id_for(user_id, decision),
        sleeve=decision.sleeve,
        trade_date=decision.trade_date,
        expiry=decision.expiry,
        mode=mode,
        structure=candidate.structure,
        direction=decision.direction,
        issued_at=issued,
        entry_window_end=_at(decision.trade_date, window),
        expires_at=expires,
        debit_points=debit,
        width_points=width,
        lot_size=lot_size,
        lots=candidate.lots,
        sizing_mode=candidate.sizing_mode or SizingMode.PAPER_ONE_LOT,
        half_size=candidate.half_size,
        risk_per_lot_inr=paise(candidate.risk_per_lot_inr or Decimal(0)),
        risk_budget_inr=paise(candidate.r_inr or Decimal(0)),
        debit_inr=paise(debit * quantity),
        max_loss_inr=paise(candidate.max_loss_inr or Decimal(0)),
        profit_target_inr=paise((cfg.target_frac_of_width * width - debit) * quantity),
        stop_inr=paise((Decimal(1) - cfg.stop_frac_of_debit) * debit * quantity),
        expected_cost_inr=decision.costs.total,
        cost_share=(candidate.cost_share or Decimal(0)).quantize(Decimal("0.0001")),
        costs=decision.costs,
        margin_required_inr=None if margin is None else paise(margin.hedged_inr),
        margin=check,
        exits=exits_for(decision, debit, cfg),
        warnings=tuple(warnings),
        legs=decision.legs,
        as_of_minute=decision.as_of_minute,
        numbers=decision.numbers,
    )
    return O3Outcome(decision, plan, PlanState.PLANNED, ())


def _margin_rejected(decision: O3Decision, message: str) -> O3Outcome:
    code = "REJECTED_MARGIN"
    skipped = O3Decision(
        sleeve=decision.sleeve,
        trade_date=decision.trade_date,
        now=decision.now,
        state=PlanState.SKIPPED,
        reasons=(code,),
        numbers={**decision.numbers, "margin_message": message},
        verdict=decision.verdict,
        expiry=decision.expiry,
        direction=decision.direction,
        range_high=decision.range_high,
        range_low=decision.range_low,
        half_gap=decision.half_gap,
        candidate=decision.candidate,
        legs=decision.legs,
        as_of_minute=decision.as_of_minute,
        costs=decision.costs,
    )
    return O3Outcome(skipped, None, PlanState.SKIPPED, (code,))


def contract_label(plan: O3Plan) -> str:
    """``25000/25100 CE`` — long strike, short strike and right, for an alert or a page."""
    long_leg, short_leg = plan.legs
    return (
        f"{long_leg.strike.quantize(Decimal(1))}/{short_leg.strike.quantize(Decimal(1))} "
        f"{long_leg.option_type.value}"
    )


__all__ = [
    "BEFORE_PLAN_TIME",
    "ENTRY_WINDOW_CLOSED",
    "NO_DECISION_SNAPSHOT",
    "NO_PREV_CLOSE",
    "NO_TRIGGER",
    "NO_TRIGGER_YET",
    "O3B_HOLDS",
    "O3_SLEEVES",
    "SETUP_DISABLED",
    "WINDOW_NOT_SETTLED",
    "O3Decision",
    "O3Exits",
    "O3Outcome",
    "O3Plan",
    "contract_label",
    "decide_o3",
    "entry_window_end",
    "exits_for",
    "finalize_o3",
    "first_decision_time",
    "o3b_holds",
    "wanted_minutes",
]
