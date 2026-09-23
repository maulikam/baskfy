"""The O1 plan builder, pure: a reasoned no-trade or one four-leg plan (``06`` OP6).

``build_plan(O1M | O1W, date)`` in ``06`` is, in order: the day's role → the gate → the chain →
``condor.build`` → costs → sizing → margin → ``op_plan`` / ``op_leg``, or ``SKIPPED`` with the code;
``expires_at = min(issued_at + 30 min, 10:15)`` (``03`` §9, non-negotiable 1). This module is
everything in that sentence that is arithmetic; the worker (``baskfy_worker.options.plan``) reads
the rows, asks the broker's margin calculator and writes the result.

Two phases, because the margin is a broker read that needs the legs first (``04`` §7.4):

* :func:`decide_o1` — role, gate, chain, structure, sizing and the cost test, by **exactly** the
  functions the scan calls (``scan.priced_view``, ``condor.observe``, ``condor.build`` over the
  decision minute's snapshot — ``04`` §10, OP4.4). Its candidate therefore equals the scan's
  (tested). It also names the two baskets the margin calculator is asked about.
* :func:`finalize_o1` — the margin ceiling (``sizing.margin_check``), the ``plan_id``, the
  expiry and the legs in send order, or ``REJECTED_MARGIN``.

**Never naked.** The legs are listed in ``execution.entry_sequence`` order — both wings, then the
shorts — which refuses a short without its wing; :func:`finalize_o1` asserts ``never_naked`` at
every prefix of the list, so a plan that could leave the book short more than long cannot be
built. **Nothing here places anything**: a plan is a proposal the desk's confirm (OP10) may act on
within its 30 minutes; there is no auto-execute for any options sleeve (PACK.3).

Which days write a session (OP6.2): an O1 day (the master's monthly for O1-M, a non-monthly weekly
for O1-W) writes one — ``PLANNED`` or ``SKIPPED`` with every reason; an O1 day that is an event day
is ``SKIPPED / EVENT_DAY`` (``04`` §1.3: skipped, never shifted); any other day is ``NO_SESSION``
and writes nothing — O1-W on the monthly Tuesday has no session.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from baskfy_core.options import condor
from baskfy_core.options.bars import IST, ist
from baskfy_core.options.calendar import (
    Contract,
    RoleReason,
    expiry_for_o1_o3,
    lot_size_for,
    role,
)
from baskfy_core.options.chain import Level
from baskfy_core.options.config import (
    CondorConfig,
    Mode,
    OptionsCeilings,
    OptionsConfig,
    OptionType,
    Side,
    SizingMode,
    Sleeve,
)
from baskfy_core.options.costs import ChargeBreakdown, CostFill, charges, paise
from baskfy_core.options.execution import LegRole, entry_sequence, never_naked
from baskfy_core.options.scan import DayContext, MarketDay, Snapshot, priced_view
from baskfy_core.options.session import plan_expires_at
from baskfy_core.options.sizing import MarginCheck, MarginCode, margin_check
from baskfy_core.options.structures import (
    Candidate,
    CandidateLeg,
    Structure,
    exit_price,
)

O1_SLEEVES: frozenset[Sleeve] = frozenset({Sleeve.O1M, Sleeve.O1W})


class PlanState(StrEnum):
    """What one build attempt concluded."""

    #: A four-leg plan, issued.
    PLANNED = "PLANNED"
    #: An O1 day with no plan: every reason is on the decision.
    SKIPPED = "SKIPPED"
    #: Not an O1 day for this sleeve (or not a trading day): nothing is written.
    NO_SESSION = "NO_SESSION"
    #: Too early to decide — before ``plan_time``, the window not yet settled, or the decision
    #: minute's snapshot not yet stored. The caller asks again next minute.
    NOT_READY = "NOT_READY"
    #: First asked after ``entry_window_end`` with nothing decided: no plan can be confirmed any
    #: more, and a day the builder never saw is not a skipped day (OP6.2). Nothing is written.
    WINDOW_CLOSED = "WINDOW_CLOSED"


class Verdict(StrEnum):
    """``op_session.verdict``: the gate's answer, whatever the plan then did."""

    TRADE = "TRADE"
    SKIP = "SKIP"


#: ``04`` §2.5 applied to a plan (OP6.4): the decision minute's quotes are more than
#: ``stale_scan_seconds`` old when the plan would be issued — no plan from stale quotes.
STALE_CHAIN = "STALE_CHAIN"
#: ``04`` §7.4 when the broker's calculator did not answer: a warning in PAPER (OP6.5).
MARGIN_UNKNOWN = "MARGIN_UNKNOWN"


@dataclass(frozen=True, slots=True)
class PlanLeg:
    """One ``op_leg`` row: the contract, the side, the units and the attempt-1 limit (``04``
    §8.2), with the quote and greeks it was priced from."""

    seq: int
    role: LegRole
    tradingsymbol: str
    instrument_token: int
    strike: Decimal
    option_type: OptionType
    side: Side
    quantity: int
    limit_price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    iv: float | None
    delta: float | None
    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()


@dataclass(frozen=True, slots=True)
class MarginLeg:
    """One leg of a basket the broker's margin *calculator* is asked about — never an order."""

    tradingsymbol: str
    side: Side
    quantity: int


@dataclass(frozen=True, slots=True)
class MarginQuote:
    """The calculator's answers: the whole basket (hedged) and the worst state the entry passes
    through (OP6.5: the three-leg prefix — both wings and the first short)."""

    hedged_inr: Decimal
    transient_inr: Decimal | None


@dataclass(frozen=True, slots=True)
class O1Decision:
    """The first phase's answer: every reason, the gate's numbers, the priced candidate."""

    sleeve: Sleeve
    trade_date: dt.date
    now: dt.datetime
    state: PlanState
    reasons: tuple[str, ...]
    numbers: dict[str, object]
    verdict: Verdict | None = None
    expiry: dt.date | None = None
    candidate: Candidate | None = None
    legs: tuple[PlanLeg, ...] = ()
    as_of_minute: dt.datetime | None = None
    costs: ChargeBreakdown | None = None

    @property
    def writes_session(self) -> bool:
        return self.state in (PlanState.PLANNED, PlanState.SKIPPED)

    def margin_baskets(self) -> tuple[tuple[MarginLeg, ...], tuple[MarginLeg, ...]]:
        """The hedged basket (all four legs) and the transient one (the longest entry prefix
        that holds a short: both wings and the first short). Asked with ``consider_positions``
        false, so the answer is this basket's alone (Track C §8)."""
        if self.state is not PlanState.PLANNED or not self.legs:
            return (), ()
        basket = tuple(MarginLeg(lg.tradingsymbol, lg.side, lg.quantity) for lg in self.legs)
        return basket, basket[:-1]


@dataclass(frozen=True, slots=True)
class O1Plan:
    """One ``op_plan`` row and its ``op_leg`` rows, ready to write."""

    plan_id: str
    sleeve: Sleeve
    trade_date: dt.date
    expiry: dt.date
    mode: Mode
    structure: Structure
    issued_at: dt.datetime
    entry_window_end: dt.datetime
    expires_at: dt.datetime
    credit_points: Decimal
    width_points: Decimal
    lot_size: int
    lots: int
    sizing_mode: SizingMode
    half_size: bool
    risk_per_lot_inr: Decimal
    risk_budget_inr: Decimal
    credit_inr: Decimal
    max_loss_inr: Decimal
    profit_target_inr: Decimal
    stop_inr: Decimal
    expected_cost_inr: Decimal
    cost_share: Decimal
    costs: ChargeBreakdown
    margin_required_inr: Decimal | None
    margin_transient_inr: Decimal | None
    margin: MarginCheck | None
    warnings: tuple[str, ...]
    legs: tuple[PlanLeg, ...]
    as_of_minute: dt.datetime | None
    numbers: dict[str, object] = field(default_factory=dict)

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size


@dataclass(frozen=True, slots=True)
class O1Outcome:
    """The second phase's answer: the plan, or the decision that ended the day."""

    decision: O1Decision
    plan: O1Plan | None
    state: PlanState
    reasons: tuple[str, ...]


def condor_config_for(sleeve: Sleeve, options: OptionsConfig) -> CondorConfig:
    """``condor_monthly`` for O1-M, ``condor_weekly`` for O1-W — two objects (``04`` §3.2)."""
    if sleeve not in O1_SLEEVES:
        raise ValueError(f"{sleeve.value} is not an O1 sleeve")
    return options.condor_monthly if sleeve is Sleeve.O1M else options.condor_weekly


def _at(day: dt.date, clock: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, clock, tzinfo=IST)


def _q(value: Decimal | None, places: str = "0.01") -> str | None:
    return None if value is None else str(value.quantize(Decimal(places)))


def _gate_numbers(verdict: condor.DayVerdict, config: CondorConfig) -> dict[str, object]:
    n = verdict.numbers
    return {
        "variant": config.variant.value,
        "prev_close": _q(n.prev_close),
        "bars": n.bars,
        "open_0915": _q(n.open_0915),
        "gap_pct": _q(n.gap_pct, "0.0001"),
        "range_pct": _q(n.range_pct, "0.0001"),
        "or_high": _q(n.or_high),
        "or_low": _q(n.or_low),
        "last_close": _q(n.last_close),
        "contained": n.contained,
        "er": _q(n.er, "0.0001"),
    }


def plan_legs(candidate: Candidate, contracts: Sequence[Contract], snapshot: Snapshot | None) -> (
    tuple[PlanLeg, ...]
):  # fmt: skip
    """The candidate's legs as ``op_leg`` rows, in ``execution.entry_sequence`` order, each with
    the master's trading symbol and the snapshot's depth (``03`` §9)."""
    by_role = {lg.role: lg for lg in candidate.legs}
    order = entry_sequence(by_role)  # refuses a short without its wing (Track C §2)
    symbols = {c.instrument_token: c.tradingsymbol for c in contracts}
    depth = {q.instrument_token: q for q in snapshot.quotes} if snapshot is not None else {}
    quantity = candidate.lots * (candidate.lot_size or 0)
    out: list[PlanLeg] = []
    for seq, leg_role in enumerate(order, start=1):
        lg: CandidateLeg = by_role[leg_role]
        symbol = symbols.get(lg.instrument_token)
        if symbol is None:
            raise ValueError(f"token {lg.instrument_token} is not in the master")
        quote = depth.get(lg.instrument_token)
        out.append(
            PlanLeg(
                seq=seq,
                role=lg.role,
                tradingsymbol=symbol,
                instrument_token=lg.instrument_token,
                strike=lg.strike,
                option_type=lg.option_type,
                side=lg.entry_side,
                quantity=quantity,
                limit_price=lg.limit_price,
                bid=lg.bid,
                ask=lg.ask,
                iv=lg.iv,
                delta=lg.delta,
                bids=quote.bids if quote is not None else (),
                asks=quote.asks if quote is not None else (),
            )
        )
    return tuple(out)


def plan_costs(candidate: Candidate, tick: Decimal, options: OptionsConfig) -> ChargeBreakdown:
    """``04`` §6.1 itemised over the plan's eight orders — every leg in at its attempt-1 limit
    and out at the same quotes' attempt-1 close (OP4.5). One day's orders are one contract note,
    so each component is rounded once over all eight; its total **is** the candidate's
    ``round_trip_inr`` (tested)."""
    quantity = candidate.lots * (candidate.lot_size or 0)
    fills = [CostFill(lg.entry_side, lg.limit_price, quantity) for lg in candidate.legs]
    fills += [
        CostFill(lg.role.exit_side, exit_price(lg, tick, options.execution), quantity)
        for lg in candidate.legs
    ]
    return charges(fills, options.costs)


def decide_o1(  # noqa: PLR0911, PLR0913 - one return per stage; every input 06 OP6 names
    sleeve: Sleeve,
    market: MarketDay,
    context: DayContext,
    snapshot: Snapshot | None,
    now: dt.datetime,
    *,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
) -> O1Decision:
    """Role → gate → chain → structure → sizing → cost test, for one O1 sleeve at ``now``.

    ``snapshot`` is the decision minute's chain — the first stored minute at or after
    ``plan_time`` (OP4.4), the one the scan prices its candidate from.
    """
    cfg = condor_config_for(sleeve, options)
    day = market.trade_date

    def decided(state: PlanState, reasons: Sequence[str], **rest: object) -> O1Decision:
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
    expiry = expiry_for_o1_o3(day)
    clock = ist(now).time()
    if clock < cfg.plan_time:
        return decided(PlanState.NOT_READY, ["BEFORE_PLAN_TIME"], expiry=expiry)
    if clock >= cfg.entry_window_end:
        return decided(PlanState.WINDOW_CLOSED, ["ENTRY_WINDOW_CLOSED"], expiry=expiry)
    if not day_role.trades:  # an O1 day that is an event day: skipped, never shifted (04 §1.3)
        return decided(
            PlanState.SKIPPED, [RoleReason.EVENT_DAY.value], expiry=expiry, verdict=Verdict.SKIP
        )
    if not market.daily_closes:
        return decided(PlanState.SKIPPED, ["NO_PREV_CLOSE"], expiry=expiry, verdict=Verdict.SKIP)
    verdict = condor.observe(
        market.bars,
        market.daily_closes[-1],
        now=now,
        event_day=False,
        config=cfg,
        grace_seconds=options.chain.stale_scan_seconds,
    )
    numbers = _gate_numbers(verdict, cfg)
    if not verdict.final:
        return decided(PlanState.NOT_READY, ["WINDOW_NOT_SETTLED"], expiry=expiry, numbers=numbers)
    if verdict.reasons:
        return decided(
            PlanState.SKIPPED,
            [r.value for r in verdict.reasons],
            expiry=expiry,
            numbers=numbers,
            verdict=Verdict.SKIP,
        )
    if snapshot is None:
        return decided(
            PlanState.NOT_READY, ["NO_DECISION_SNAPSHOT"], expiry=expiry, numbers=numbers
        )
    mine = context.of(sleeve)
    view = priced_view(snapshot, expiry, market, options)
    candidate = condor.build(
        view,
        or_high=verdict.numbers.or_high or Decimal(0),
        or_low=verdict.numbers.or_low or Decimal(0),
        lot_size=lot_size_for(market.contracts, expiry, options.calendar.underlying),
        book=mine.book,
        config=cfg,
        options=options,
        ceilings=ceilings,
        slot_free=context.slot_holder is None or context.slot_holder is sleeve,
        paused=mine.paused,
        expiry=expiry,
    )
    common: dict[str, object] = {
        "expiry": expiry,
        "numbers": numbers,
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


def _decision(  # noqa: PLR0913, PLR0917 - the decision's identity and the rest by name
    sleeve: Sleeve,
    day: dt.date,
    now: dt.datetime,
    state: PlanState,
    reasons: tuple[str, ...],
    rest: dict[str, object],
) -> O1Decision:
    expiry = rest.get("expiry")
    numbers = rest.get("numbers")
    verdict = rest.get("verdict")
    candidate = rest.get("candidate")
    legs = rest.get("legs")
    as_of = rest.get("as_of_minute")
    costs = rest.get("costs")
    return O1Decision(
        sleeve=sleeve,
        trade_date=day,
        now=now,
        state=state,
        reasons=reasons,
        numbers=dict(numbers) if isinstance(numbers, dict) else {},
        verdict=verdict if isinstance(verdict, Verdict) else None,
        expiry=expiry if isinstance(expiry, dt.date) else None,
        candidate=candidate if isinstance(candidate, Candidate) else None,
        legs=legs if isinstance(legs, tuple) else (),
        as_of_minute=as_of if isinstance(as_of, dt.datetime) else None,
        costs=costs if isinstance(costs, ChargeBreakdown) else None,
    )


class Decided(Protocol):
    """What :func:`plan_id_for` and :func:`assert_never_naked` need of a decision — the sleeve,
    the date and the legs. O1's and O2's decisions both satisfy it (OP7.1)."""

    @property
    def sleeve(self) -> Sleeve: ...

    @property
    def trade_date(self) -> dt.date: ...

    @property
    def legs(self) -> tuple[PlanLeg, ...]: ...


def assert_never_naked(legs: Sequence[PlanLeg]) -> None:
    """Refuse a leg order that would leave the book short more than long at any prefix.

    The sequence is ``execution.entry_sequence``'s, so this can only fire if a caller built the
    legs itself; it is asserted at plan time all the same (Track C §2), for every sleeve.
    """
    for prefix in range(1, len(legs) + 1):
        held: dict[LegRole, int] = {}
        for lg in legs[:prefix]:
            held[lg.role] = held.get(lg.role, 0) + lg.quantity
        if not never_naked(held):
            raise ValueError("the legs would leave the book short more than long")


def plan_id_for(user_id: int, decision: Decided) -> str:
    """Deterministic: the same user, sleeve, date and legs mint the same id — a rebuilt plan
    can never be a second plan (idempotent per date), and the id is a valid ``client_id`` half
    (no ``:``, no whitespace — the execution package's client-id rule)."""
    canonical = "|".join(
        [str(user_id), decision.sleeve.value, decision.trade_date.isoformat()]
        + [
            f"{lg.seq},{lg.instrument_token},{lg.side.value},{lg.quantity},{lg.limit_price}"
            for lg in decision.legs
        ]
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{decision.sleeve.value}-{decision.trade_date:%Y%m%d}-{digest}"


def finalize_o1(  # noqa: PLR0913 - the decision, the broker's answer, the book and the clock
    decision: O1Decision,
    margin: MarginQuote | None,
    *,
    user_id: int,
    mode: Mode,
    margin_pool_inr: Decimal,
    margin_in_use_inr: Decimal,
    options: OptionsConfig,
) -> O1Outcome:
    """The margin ceiling (``04`` §7.4, never a source of size — Track C §10), the ``plan_id``
    and the 30-minute expiry, or ``REJECTED_MARGIN``.

    With no answer from the calculator (``margin=None``): a paper plan is issued with the
    warning ``MARGIN_UNKNOWN``; a live plan is ``REJECTED_MARGIN`` (OP6.5).
    """
    if decision.state is not PlanState.PLANNED:
        return O1Outcome(decision, None, decision.state, decision.reasons)
    candidate = decision.candidate
    if candidate is None or decision.expiry is None or decision.costs is None:
        raise ValueError("a PLANNED decision carries its candidate, expiry and costs")
    assert_never_naked(decision.legs)
    warnings = list(candidate.warnings)
    check: MarginCheck | None = None
    if margin is None:
        if mode is Mode.LIVE:
            return _margin_rejected(decision, "the broker's margin calculator did not answer")
        warnings.append(MARGIN_UNKNOWN)
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
    cfg = condor_config_for(decision.sleeve, options)
    issued = ist(decision.now).replace(tzinfo=IST)
    window_end = _at(decision.trade_date, cfg.entry_window_end)
    expires = plan_expires_at(issued, cfg.entry_window_end, options.execution.plan_ttl_minutes)
    lot_size = candidate.lot_size or 0
    quantity = candidate.lots * lot_size
    credit = candidate.points or Decimal(0)
    credit_inr = credit * quantity
    width = cfg.wing_width_points
    plan = O1Plan(
        plan_id=plan_id_for(user_id, decision),
        sleeve=decision.sleeve,
        trade_date=decision.trade_date,
        expiry=decision.expiry,
        mode=mode,
        structure=candidate.structure,
        issued_at=issued,
        entry_window_end=window_end,
        expires_at=expires,
        credit_points=credit,
        width_points=width,
        lot_size=lot_size,
        lots=candidate.lots,
        sizing_mode=candidate.sizing_mode or SizingMode.PAPER_ONE_LOT,
        half_size=candidate.half_size,
        risk_per_lot_inr=paise(candidate.risk_per_lot_inr or Decimal(0)),
        risk_budget_inr=paise(candidate.r_inr or Decimal(0)),
        credit_inr=paise(credit_inr),
        max_loss_inr=paise(candidate.max_loss_inr or Decimal(0)),
        profit_target_inr=paise((Decimal(1) - cfg.profit_take_frac) * credit_inr),
        stop_inr=paise((cfg.stop_frac - Decimal(1)) * credit_inr),
        expected_cost_inr=decision.costs.total,
        cost_share=(candidate.cost_share or Decimal(0)).quantize(Decimal("0.0001")),
        costs=decision.costs,
        margin_required_inr=None if margin is None else paise(margin.hedged_inr),
        margin_transient_inr=None
        if margin is None or margin.transient_inr is None
        else paise(margin.transient_inr),
        margin=check,
        warnings=tuple(warnings),
        legs=decision.legs,
        as_of_minute=decision.as_of_minute,
        numbers=decision.numbers,
    )
    return O1Outcome(decision, plan, PlanState.PLANNED, ())


def _margin_rejected(decision: O1Decision, message: str) -> O1Outcome:
    code = "REJECTED_MARGIN"
    numbers = {**decision.numbers, "margin_message": message}
    skipped = _decision(
        decision.sleeve,
        decision.trade_date,
        decision.now,
        PlanState.SKIPPED,
        (code,),
        {
            "expiry": decision.expiry,
            "numbers": numbers,
            "verdict": decision.verdict,
            "candidate": decision.candidate,
            "legs": decision.legs,
            "as_of_minute": decision.as_of_minute,
            "costs": decision.costs,
        },
    )
    return O1Outcome(skipped, None, PlanState.SKIPPED, (code,))
