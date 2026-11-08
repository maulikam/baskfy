"""The desk monitor's decisions, pure (``docs/fno/04`` §1-§3, §10; ``06`` FO7).

The desk process (``kite-momentum-rebalancer/app/fno_monitor.py``) reads the scan, the live book
and the bhavcopy, and hands the numbers here; every verdict it acts on is one of these functions.

* :func:`reprice_condor` — the 09:20 F1 plan on live quotes: the scan's strikes, priced on live
  mids, the liquidity refusals by name, sizing (``04`` §3, never rounded up) and the round trip as
  a share of the credit. The first failure in the order structure → liquidity → size → cost is the
  state, and every reason found is kept. The broker's basket margin is the caller's last check
  (``sizing.margin_check``): it can only reject, never size.
* :func:`reprice_future` — the 09:20 F2 plan on the live ask: stop ``entry - 3 x ATR14`` (ATR at
  the signal close), the GTT at the tighter of that and ``stop_from_vol``, lots from the budget.
* :func:`f1_close_cost` / :func:`f1_pnl_inr` / :func:`f2_pnl_inr` — the marks.
* :func:`f2_trail_step` — the evening trail: highest close, the stop moved **never lower**, the
  GTT trigger that follows it.
* :func:`f2_action` — the F2 action due on a tick: a position without its resting stop is exited
  (``NAKED_FUTURE``), a roll date passed while the desk was down is ``LATE_EXIT``, then
  ``exits.f2_exit`` (stop, time exit, roll).

Law 1: no database, no network, no disk, no clock.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from baskfy_core.fno.condor import (
    CondorStrikes,
    LegQuote,
    LegRole,
    cost_to_close,
    liquidity_refusals,
    price_structure,
)
from baskfy_core.fno.config import CommonConfig, FnoCeilings, FnoConfig, PlanState
from baskfy_core.fno.costs import CondorCost, FutureCharges, condor_round_trip, future_round_trip
from baskfy_core.fno.exits import (
    ExitDecision,
    ExitReason,
    f2_exit,
    f2_initial_stop,
    f2_trail,
    gtt_trigger,
)
from baskfy_core.fno.sizing import FoSizing, size
from baskfy_core.options.config import CostRates, Mode

_CENT = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


# ================================================================================================
# F1 — the 09:20 re-price
# ================================================================================================


@dataclass(frozen=True, slots=True)
class CondorReprice:
    """The live plan's numbers. ``state`` is ``None`` for a clean plan, or the first rejection."""

    state: PlanState | None
    reasons: tuple[str, ...]
    mids: Mapping[LegRole, Decimal]
    credit: Decimal | None
    max_loss_per_unit: Decimal | None
    sizing: FoSizing | None
    cost: CondorCost | None


def reprice_condor(  # noqa: PLR0913 - every input 04 §2-§3 names, by keyword
    *,
    strikes: CondorStrikes,
    quotes: Mapping[LegRole, LegQuote],
    lot_size: int,
    capital_inr: Decimal,
    mode: Mode,
    config: FnoConfig,
    rates: CostRates,
    ceilings: FnoCeilings,
) -> CondorReprice:
    """``04`` §2-§3 on live quotes for the scan's strikes."""
    liquidity = liquidity_refusals(strikes, quotes, config.f1)
    mids: dict[LegRole, Decimal] = {}
    for role in LegRole:
        quote = quotes.get(role)
        mid = None if quote is None else quote.mid
        if mid is not None:
            mids[role] = mid
    if len(mids) < len(LegRole):
        return CondorReprice(PlanState.REJECTED_LIQUIDITY, liquidity, mids, None, None, None, None)
    priced = price_structure(strikes, mids)
    states: list[PlanState] = []
    reasons: list[str] = [*priced.reasons, *liquidity]
    if priced.state is not None:
        states.append(priced.state)
    if liquidity:
        states.append(PlanState.REJECTED_LIQUIDITY)
    sizing: FoSizing | None = None
    cost: CondorCost | None = None
    if priced.state is None:
        sizing = size(
            mode=mode,
            capital_inr=capital_inr,
            risk_pct=config.f1.risk_per_trade_pct,
            risk_per_unit=priced.max_loss_per_unit,
            lot_size=lot_size,
            common=config.common,
            ceilings=ceilings,
        )
        if sizing.state is not None:
            states.append(sizing.state)
            reasons.append(sizing.message)
        cost = condor_round_trip(
            entry=mids,
            exit_=None,
            quantity=max(sizing.lots, 1) * lot_size,
            rates=rates,
            config=config.f1,
        )
        if cost.state is not None:
            states.append(cost.state)
            reasons.append(
                f"round trip ₹{cost.round_trip_inr} is {cost.cost_share_pct} % of the credit "
                f"₹{cost.credit_inr} > {config.f1.max_cost_share_pct} %"
            )
    return CondorReprice(
        states[0] if states else None,
        tuple(reasons),
        mids,
        priced.credit,
        priced.max_loss_per_unit,
        sizing,
        cost,
    )


def f1_close_cost(strikes: CondorStrikes, marks: Mapping[LegRole, Decimal]) -> Decimal | None:
    """The cost to close per unit on ``marks`` (live mids or the bhavcopy settles), or ``None``
    when a leg has no mark — a missing leg is not a zero."""
    if any(role not in marks for role in LegRole):
        return None
    return cost_to_close(strikes, marks)


def f1_pnl_inr(entry_credit: Decimal, close_cost: Decimal, quantity: int) -> Decimal:
    """``(credit - cost to close) x quantity``: a condor earns what it does not buy back."""
    return _money((entry_credit - close_cost) * quantity)


# ================================================================================================
# F2 — the 09:20 re-price, the marks and the evening trail
# ================================================================================================


@dataclass(frozen=True, slots=True)
class FutureReprice:
    state: PlanState | None
    reasons: tuple[str, ...]
    entry: Decimal | None
    stop: Decimal | None
    trigger: Decimal | None
    risk_per_unit: Decimal | None
    sizing: FoSizing | None
    cost: FutureCharges | None


def reprice_future(  # noqa: PLR0913 - every input 04 §10 names, by keyword
    *,
    bid: Decimal | None,
    ask: Decimal | None,
    atr: Decimal,
    ann_vol: float,
    lot_size: int,
    capital_inr: Decimal,
    mode: Mode,
    config: FnoConfig,
    ceilings: FnoCeilings,
) -> FutureReprice:
    """``04`` §10 at the live ask (what a buy pays). The stop is ``entry - stop_atr x ATR14`` at
    the signal close's ATR; the GTT triggers at the tighter of it and ``stop_from_vol``."""
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return FutureReprice(
            PlanState.REJECTED_LIQUIDITY,
            ("the future has no two-sided quote",),
            None, None, None, None, None, None,
        )  # fmt: skip
    if atr <= 0:
        return FutureReprice(
            PlanState.REJECTED_STRUCTURE,
            (f"ATR14 {atr} is not positive; no stop can be placed",),
            ask, None, None, None, None, None,
        )  # fmt: skip
    entry = ask
    stop = _money(f2_initial_stop(entry, atr, config.f2))
    trigger = _money(gtt_trigger(stop, entry, ann_vol, config.stop_vol))
    risk = entry - stop
    if stop <= 0 or risk <= 0:
        return FutureReprice(
            PlanState.REJECTED_STRUCTURE,
            (f"stop {stop} is not a price below the entry {entry}",),
            entry, stop, trigger, None, None, None,
        )  # fmt: skip
    sizing = size(
        mode=mode,
        capital_inr=capital_inr,
        risk_pct=config.f2.risk_per_trade_pct,
        risk_per_unit=risk,
        lot_size=lot_size,
        common=config.common,
        ceilings=ceilings,
    )
    cost = future_round_trip(entry, entry, max(sizing.lots, 1) * lot_size, config.future_costs)
    reasons = (sizing.message,)
    return FutureReprice(sizing.state, reasons, entry, stop, trigger, risk, sizing, cost)


def f2_pnl_inr(entry: Decimal, mark: Decimal, quantity: int) -> Decimal:
    return _money((mark - entry) * quantity)


@dataclass(frozen=True, slots=True)
class TrailStep:
    """The evening's stop, its GTT trigger and the highest close; ``moved`` when the trigger
    rose (the GTT is then modified)."""

    stop: Decimal
    trigger: Decimal
    highest_close: Decimal
    moved: bool


def f2_trail_step(  # noqa: PLR0913 - every input 04 §10 names, by keyword
    *,
    stop: Decimal,
    trigger: Decimal,
    highest_close: Decimal,
    close: Decimal,
    entry: Decimal,
    atr_at_entry: Decimal,
    ann_vol: float,
    config: FnoConfig,
) -> TrailStep:
    """Each close: ``stop = max(stop, highest close - mult x ATR at entry)``, the trigger the
    tighter of that and ``stop_from_vol(entry)``. **Neither is ever lowered**: the new values
    are taken as maxima with the old, so a lower candidate leaves them where they were."""
    highest = max(highest_close, close)
    new_stop = _money(f2_trail(stop, highest, atr_at_entry, config.f2))
    candidate = _money(gtt_trigger(new_stop, entry, ann_vol, config.stop_vol))
    new_trigger = max(trigger, candidate)
    return TrailStep(max(stop, new_stop), new_trigger, highest, new_trigger > trigger)


def f2_action(  # noqa: PLR0913 - every input 04 §10 names, by keyword
    *,
    now: dt.datetime,
    price: Decimal | None,
    trigger: Decimal,
    time_exit_date: dt.date | None,
    roll_date: dt.date | None,
    naked: bool,
    common: CommonConfig,
) -> ExitDecision | None:
    """The F2 action due at ``now``: no resting stop first (``NAKED_FUTURE``, exit at the next
    check), a roll date passed while the desk was down (``LATE_EXIT`` — never rolled late, never
    held into expiry day), then the stop, the time exit and the roll (``exits.f2_exit``). With no
    price the stop cannot be tested, but the calendar exits still can."""
    if naked:
        return ExitDecision(
            ExitReason.NAKED_FUTURE, "the future has no resting GTT stop; exited at this check"
        )
    if roll_date is not None and now.date() > roll_date:
        return ExitDecision(
            ExitReason.LATE_EXIT,
            f"roll date {roll_date.isoformat()} passed while the desk was down",
        )
    if price is None:
        return f2_exit(
            now=now,
            price=trigger + 1,
            stop=trigger,
            time_exit_date=time_exit_date,
            roll_date=roll_date,
            common=common,
        )
    return f2_exit(
        now=now,
        price=price,
        stop=trigger,
        time_exit_date=time_exit_date,
        roll_date=roll_date,
        common=common,
    )


__all__ = [
    "CondorReprice",
    "FutureReprice",
    "TrailStep",
    "f1_close_cost",
    "f1_pnl_inr",
    "f2_action",
    "f2_pnl_inr",
    "f2_trail_step",
    "reprice_condor",
    "reprice_future",
]
