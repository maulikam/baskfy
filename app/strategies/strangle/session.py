"""The management loop: exits first, then at most one adjustment, every poll.

The loop takes a SNAPSHOT PROVIDER rather than a broker. Live trading passes a provider
that polls Kite; tests pass a scripted list. That is what makes "no adjustment fires on an
unconfirmed touch" and "never more than three adjustments in a session" observable facts
about a replayed session rather than assertions about a function, and it is what will make
V3's synthetic Friday sequence reproducible.

ORDER IS NOT NEGOTIABLE: exits are evaluated before adjustments, every tick. A book that
should be closed must not spend money rolling first, and the hard stop must be seen before
anything that could keep a losing position alive.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from . import adjust as A
from . import attribution as AT
from . import fills_paper as F
from . import rules as R
from .book import Leg, cost_of


@dataclass
class LoopState:
    """Everything the pure rules need that spans more than one tick."""
    adjustments_today: int = 0
    last_adjustment_at: dt.datetime | None = None
    trail_active: bool = False
    best_pnl: float | None = None
    last_improve_at: dt.datetime | None = None
    minutes_since_pnl_improved: float = 0.0
    breaks: A.BreakTracker = field(default_factory=A.BreakTracker)
    minute: dt.datetime | None = None
    minute_close: float | None = None
    # V3 bookkeeping.
    widened: bool = False
    overrun_applied: bool = False
    reentries_used: int = 0
    lever_a: int = 0
    lever_b: int = 0
    # Section 4.6 asks for the risk-off fire count AND the reasons behind a zero, because
    # a lever that never fires needs its thresholds re-derived, not widened until it does.
    risk_off_refusals: dict = field(default_factory=dict)

    def as_rules_state(self) -> dict:
        return {"adjustments_today": self.adjustments_today,
                "last_adjustment_at": self.last_adjustment_at,
                "trail_active": self.trail_active,
                "minutes_since_pnl_improved": self.minutes_since_pnl_improved}


def marks_from(snapshot, book) -> dict[str, float]:
    """Liquidation marks: shorts at the ask, longs at the bid.

    A leg with no two-sided quote is left out, which makes book.pnl refuse rather than
    value the book with a leg silently at zero.
    """
    by_symbol = {r["symbol"]: r for r in snapshot.rows}
    out: dict[str, float] = {}
    for leg in book.open_legs:
        row = by_symbol.get(leg.symbol)
        if not row:
            continue
        price = row["ask"] if leg.is_short else row["bid"]
        if price > 0:
            out[leg.symbol] = float(price)
    return out


def vwaps_from(snapshot, book) -> dict[str, float]:
    """Session VWAP per leg, from the exchange's own average price.

    Kite's quote carries average_price, which is the day's volume-weighted average. Using
    it beats accumulating our own from 5-second samples: ours would only cover the period
    since the bot started, and a VWAP that begins mid-session says nothing about where the
    average short actually sits.
    """
    by_symbol = {r["symbol"]: r for r in snapshot.rows}
    out = {}
    for leg in book.open_legs:
        v = (by_symbol.get(leg.symbol) or {}).get("average_price")
        if v:
            out[leg.symbol] = float(v)
    return out


def _feed_minute(state: LoopState, snapshot, levels) -> str | None:
    """Roll 5-second samples into one-minute closes and confirm breaks off those.

    Break confirmation is defined on one-minute closes. Feeding every tick would confirm a
    'break' from two samples ten seconds apart, which is a touch wearing a costume.
    """
    stamp = snapshot.as_of.replace(second=0, microsecond=0)
    closed = None
    if state.minute is None:
        state.minute, state.minute_close = stamp, snapshot.spot
    elif stamp > state.minute:
        closed = state.minute_close
        state.minute, state.minute_close = stamp, snapshot.spot
    else:
        state.minute_close = snapshot.spot
    if closed is None:
        return None
    return state.breaks.observe(closed, resistance=levels.resistance,
                                support=levels.support, at=stamp)


def flatten(book, snapshot, cfg, *, lot_size: int, now: dt.datetime,
            executor=None) -> dict:
    """Close every open leg together.

    Rule R5 asks for one basket order. Kite Connect has no such order — see fills_live.py —
    so the closest achievable thing is used: every leg goes out concurrently rather than
    sequentially. `executor` is the paper fill engine by default and the live one under V4,
    which is why nothing else in this loop changes when the transport does.
    """
    by_symbol = {r["symbol"]: r for r in snapshot.rows}
    orders, legs = [], list(book.open_legs)
    for leg in legs:
        row = by_symbol.get(leg.symbol)
        if not row:
            raise F.DepthUnavailable(f"{leg.symbol}: not in the chain; cannot flatten")
        orders.append({"symbol": leg.symbol, "side": "BUY" if leg.is_short else "SELL",
                       "quantity": leg.quantity, "depth": row["depth"]})
    fills = (executor or F.simulate_basket)(orders, cfg)
    priced = [{"side": o["side"], "price": f.avg_price, "quantity": o["quantity"]}
              for o, f in zip(orders, fills)]
    cost = cost_of(priced, lot_size)
    for leg, f in zip(legs, fills):
        book.close_leg(leg.symbol, f.avg_price, when=now)
    book.accrued_costs += cost
    return {"legs": len(legs), "cost": round(cost, 2),
            "fills": [f.as_dict() for f in fills]}


def run_session(*, book, provider: Iterable, cfg: dict, levels, oi, journal,
                lot_size: int, step: int,
                original_legs: list | None = None,
                executor=None, kill_switch=None,
                on_event: Callable[[str, dict], None] | None = None) -> dict:
    """Drive one session to a flat book. Returns the session record.

    `provider` yields ChainSnapshots in time order; the loop stops when the book is flat or
    the provider is exhausted. A provider that runs dry with the book still open is
    reported as UNCLOSED rather than treated as an exit — pretending an unfinished session
    ended flat is how a paper record acquires trades that never closed.
    """
    state = LoopState()
    originals = list(original_legs if original_legs is not None else book.legs)
    ticks = 0
    exit_decision: R.Decision | None = None
    last_snapshot = None

    def emit(kind: str, payload: dict) -> None:
        journal.write(kind, **payload)
        if on_event:
            on_event(kind, payload)

    for snapshot in provider:
        ticks += 1
        last_snapshot = snapshot
        if book.is_flat:
            break

        marks = marks_from(snapshot, book)
        if len(marks) < len(book.open_legs):
            emit("mark_missing", {"at": snapshot.as_of,
                                  "have": len(marks), "need": len(book.open_legs)})
            continue

        # The kill switch outranks every strategy rule. A book past twice its stop, a dead
        # feed or a missed heartbeat all mean the position is not what the book thinks it
        # is, and none of those are improved by continuing to trade.
        if kill_switch is not None:
            kill_switch.beat(snapshot.as_of)
            tripped = kill_switch.check(now=snapshot.as_of,
                                        stale_seconds=snapshot.stale_seconds,
                                        book=book, marks=marks)
            if tripped:
                emit("kill_switch", {"at": snapshot.as_of, "reason": tripped})
                result = flatten(book, snapshot, cfg, lot_size=lot_size,
                                 now=snapshot.as_of, executor=executor)
                emit("emergency_flatten", {"at": snapshot.as_of, "reason": tripped,
                                           **result})
                exit_decision = R.Decision(R.Action.EXIT, code="KILL", reason=tripped,
                                           detail={"ends_day": True, "halt": True})
                break

        pnl = book.pnl(marks)
        if state.best_pnl is None or pnl > state.best_pnl:
            state.best_pnl, state.last_improve_at = pnl, snapshot.as_of
        book.high_water_mark = max(book.high_water_mark, pnl)
        if state.last_improve_at:
            state.minutes_since_pnl_improved = (
                (snapshot.as_of - state.last_improve_at).total_seconds() / 60.0)

        confirmed = _feed_minute(state, snapshot, levels)

        # R.Snapshot is FROZEN and carries a copy of the marks. Any rule consulted after
        # the book changes must be given a rebuilt one, or it values a position that no
        # longer exists — and book.pnl deliberately raises rather than treat a missing leg
        # as zero, so the session dies with a KeyError. Built through one closure so the
        # marks and the snapshot cannot drift apart again.
        def _observe():
            m = marks_from(snapshot, book)
            return m, R.Snapshot(now=snapshot.as_of, spot=snapshot.spot,
                                 asp=_asp(snapshot, step), marks=m,
                                 vwaps=vwaps_from(snapshot, book),
                                 resistance=levels.resistance, support=levels.support,
                                 feed_age_seconds=snapshot.stale_seconds)

        marks, snap = _observe()

        # --- exits first, always -------------------------------------------------------
        decision = R.evaluate_exits(book, snap, cfg, state=state.as_rules_state())
        if decision is not None:
            emit("exit_signal", {"at": snapshot.as_of, **decision.as_dict(),
                                 "pnl": round(pnl, 2)})
            result = flatten(book, snapshot, cfg, lot_size=lot_size, now=snapshot.as_of,
                             executor=executor)
            emit("flattened", {"at": snapshot.as_of, "code": decision.code, **result})
            exit_decision = decision

            # The one permitted re-entry. Only after an abort, only before the cutoff, only
            # while green — and on what is LEFT of the original target, so a day cannot
            # quietly double its objective by aborting and starting again.
            if decision.detail.get("allows_reentry"):
                banked = (book.pnl({}) - book.baseline) / max(book.units_at_entry, 1)
                plan = R.plan_reentry(book, snap, cfg, exit_code=decision.code,
                                      reentries_used=state.reentries_used,
                                      banked_points=banked, step=step)
                if plan.allowed:
                    prior = {l.strike for l in originals if l.is_short}
                    try:
                        opened = reenter(book, snapshot, cfg, levels=levels, oi=oi,
                                         plan=plan, prior_strikes=prior,
                                         lot_size=lot_size, step=step,
                                         executor=executor)
                    except (SelectionRefused, F.DepthUnavailable) as exc:
                        emit("reentry_refused", {"at": snapshot.as_of, "reason": str(exc)})
                        break
                    state.reentries_used += 1
                    state.trail_active = state.widened = state.overrun_applied = False
                    state.best_pnl = None
                    exit_decision = None
                    emit("reentered", {"at": snapshot.as_of, "banked_points": round(banked, 3),
                                       **opened})
                    continue
                emit("reentry_declined", {"at": snapshot.as_of, "reason": plan.reason})
            break

        # Trail activation is not an exit; it changes how the next tick is judged.
        if not state.trail_active and R.should_activate_trail(book, snap, cfg):
            state.trail_active = True
            emit("trail_armed", {"at": snapshot.as_of, "pnl": round(pnl, 2),
                                 "high_water_mark": round(book.high_water_mark, 2)})
            if not state.widened:
                state.widened = True
                for plan in A.plan_widen(book, chain=snapshot.as_rows(), marks=marks,
                                         cfg=cfg, step=step):
                    try:
                        A.apply_roll(book, plan, _roll_fills(plan, snapshot, cfg, executor),
                                     cfg, lot_size=lot_size, now=snapshot.as_of)
                        emit("widened", {"at": snapshot.as_of, **plan.as_dict()})
                    except (A.RollRefused, F.DepthUnavailable) as exc:
                        emit("widen_refused", {"at": snapshot.as_of, "reason": str(exc)})
                # The widen replaced legs. Everything below this point — target_overrun,
                # evaluate_adjustment, evaluate_risk_off — reads snap, so it is rebuilt
                # here rather than only `marks`. Reproduced by driving a real BANKNIFTY
                # chain through trail activation: KeyError on the freshly rolled leg.
                marks, snap = _observe()

        # Target overrun: extend how far a winner may run. The stop never moves.
        if not state.overrun_applied:
            raised = R.target_overrun(book, snap, cfg)
            if raised:
                state.overrun_applied = True
                book.target_points = raised / max(book.units_at_entry, 1)
                emit("target_raised", {"at": snapshot.as_of,
                                       "target_points": round(book.target_points, 2)})

        # --- then at most one adjustment ----------------------------------------------
        adj = R.evaluate_adjustment(book, snap, cfg, state=state.as_rules_state(),
                                    confirmed_break=confirmed)
        if adj.action is not R.Action.ROLL_WINNER:
            # Lever B — risk-off. Narrow window by design; every refusal is counted so a
            # zero fire count can be explained rather than merely observed.
            off = R.evaluate_risk_off(book, snap, cfg, state=state.as_rules_state())
            if off.action is not R.Action.ROLL_LOSER:
                key = off.reason.split(";")[0][:60]
                state.risk_off_refusals[key] = state.risk_off_refusals.get(key, 0) + 1
                continue
            loser = next(l for l in book.open_legs if l.symbol == off.detail["loser"])
            try:
                plan = A.plan_loser_roll(book, loser=loser, chain=snapshot.as_rows(),
                                         marks=marks, cfg=cfg)
                applied = A.apply_roll(book, plan,
                                       _roll_fills(plan, snapshot, cfg, executor), cfg,
                                       lot_size=lot_size, now=snapshot.as_of)
            except (A.RollRefused, F.DepthUnavailable) as exc:
                emit("risk_off_refused", {"at": snapshot.as_of, "reason": str(exc)})
                continue
            state.adjustments_today += 1
            state.lever_b += 1
            state.last_adjustment_at = snapshot.as_of
            emit("risk_off", {"at": snapshot.as_of, "n": state.adjustments_today,
                              **plan.as_dict(), **applied})
            continue

        winner = next(l for l in book.open_legs if l.symbol == adj.detail["winner"])
        loser = next(l for l in book.open_legs if l.symbol == adj.detail["loser"])
        try:
            plan = A.plan_winner_roll(
                book, winner=winner, loser=loser, chain=snapshot.as_rows(), marks=marks,
                spot=snapshot.spot, asp=_asp(snapshot, step) or 0.0, levels=levels, oi=oi,
                cfg=cfg, step=step,
                trending=state.breaks.is_trending(confirmed or "", snapshot.spot))
            fills = _roll_fills(plan, snapshot, cfg, executor)
            applied = A.apply_roll(book, plan, fills, cfg, lot_size=lot_size,
                                   now=snapshot.as_of)
        except (A.RollRefused, F.DepthUnavailable) as exc:
            # Doing nothing is always legal here: the book stop governs.
            emit("roll_refused", {"at": snapshot.as_of, "reason": str(exc)})
            continue

        state.adjustments_today += 1
        state.lever_a += 1
        state.last_adjustment_at = snapshot.as_of
        emit("rolled", {"at": snapshot.as_of, "n": state.adjustments_today,
                        **plan.as_dict(), **applied})

    record: dict[str, Any] = {
        "ticks": ticks,
        "adjustments": state.adjustments_today,
        "lever_a": state.lever_a,
        "lever_b": state.lever_b,
        "risk_off_refusals": dict(state.risk_off_refusals),
        "reentries_used": state.reentries_used,
        "widened": state.widened,
        "target_raised": state.overrun_applied,
        "exit_code": exit_decision.code if exit_decision else None,
        "exit_reason": exit_decision.reason if exit_decision else None,
        "ends_day": bool(exit_decision and exit_decision.detail.get("ends_day")),
        "flat": book.is_flat,
    }
    if not book.is_flat:
        record["status"] = "UNCLOSED"
        record["note"] = ("the provider ran out with the book still open; this session did "
                          "NOT end flat and must not be counted as a completed trade")
    else:
        record["status"] = "OK"
        final_marks = marks_from(last_snapshot, book) if last_snapshot else {}
        closing = {l.symbol: l.exit_price for l in book.closed_legs if l.exit_price}
        record["pnl"] = round(book.pnl({}), 2) if book.is_flat else None
        record["attribution"] = AT.attribution(book, originals, {**final_marks, **closing},
                                               lot_size,
                                               adjustments=state.adjustments_today)
    journal.write("session_closed", **record)
    return record


class SelectionRefused(RuntimeError):
    """No qualifying pair for the re-entry. The day ends rather than relaxing a boundary."""


def reenter(book, snapshot, cfg, *, levels, oi, plan, prior_strikes, lot_size: int,
            step: int, executor=None) -> dict:
    """Open the replacement position, at least two strikes further out than the last one.

    Everything is recomputed against the current chain: levels, the priced range and the
    premium balance. Re-using the morning's frame would put the new legs where the old ones
    already proved to be wrong.
    """
    from . import selection as SEL
    from .levels import priced_range

    if not book.is_flat:
        raise SelectionRefused("cannot re-enter while legs are still open")
    # Captured BEFORE the new legs exist: once they are added the book is no longer flat
    # and pnl({}) would raise on the missing marks.
    baseline = book.pnl({})

    asp = _asp(snapshot, step)
    if not asp:
        raise SelectionRefused("no ATM straddle price; cannot recompute the priced range")
    rows = snapshot.as_rows()
    widest_call = max((s for s in prior_strikes), default=0)
    tightest_put = min((s for s in prior_strikes), default=0)
    kept = [r for r in rows
            if (r["kind"] == "CE" and float(r["strike"]) >= widest_call + plan.min_strike_shift)
            or (r["kind"] == "PE" and float(r["strike"]) <= tightest_put - plan.min_strike_shift)]
    try:
        pair = SEL.select_pair(kept, pr=priced_range(snapshot.spot, asp, cfg),
                               levels=levels, oi=oi, cfg=cfg, step=step)
        pair = SEL.attach_wings(pair, rows, cfg)
    except SEL.NoQualifyingPair as exc:
        raise SelectionRefused(str(exc)) from exc

    legs = [(pair.call, "SELL"), (pair.put, "SELL")]
    if pair.call_wing and pair.put_wing:
        legs += [(pair.call_wing, "BUY"), (pair.put_wing, "BUY")]
    by_symbol = {r["symbol"]: r for r in rows}
    qty = book.units_at_entry
    fills = (executor or F.simulate_basket)(
        [{"symbol": c.symbol, "side": side, "quantity": qty,
          "depth": by_symbol[c.symbol]["depth"]} for c, side in legs], cfg)

    credit = 0.0
    for (c, side), f in zip(legs, fills):
        role = ("short_call" if (side == "SELL" and c.kind == "CE") else
                "short_put" if side == "SELL" else
                "wing_call" if c.kind == "CE" else "wing_put")
        book.add(Leg(symbol=c.symbol, kind=c.kind, strike=c.strike, side=side,
                     quantity=qty, entry_price=f.avg_price, role=role,
                     opened_at=snapshot.as_of))
        credit += f.avg_price * qty * (1 if side == "SELL" else -1)
    book.accrued_costs += cost_of([{"side": s, "price": f.avg_price, "quantity": qty}
                                   for (_c, s), f in zip(legs, fills)], lot_size)
    # The new position is judged from here: its stop must not be funded by the morning.
    book.baseline = baseline
    book.entry_credit = credit
    book.target_points = plan.target_points
    book.stop_points = plan.stop_points
    book.high_water_mark = 0.0
    return {"pair": pair.as_dict(), "target_points": plan.target_points,
            "stop_points": plan.stop_points, "baseline": round(book.baseline, 2)}


def _asp(snapshot, step: int) -> float | None:
    from .market import atm_straddle
    try:
        return atm_straddle(snapshot, step)[0]
    except Exception:                                    # noqa: BLE001
        return None


def _roll_fills(plan: A.RollPlan, snapshot, cfg: dict, executor=None) -> dict[str, Any]:
    """Simulate all four legs of the roll against live depth, as one basket."""
    by_symbol = {r["symbol"]: r for r in snapshot.rows}
    orders = [{"symbol": plan.close_short.symbol, "side": "BUY",
               "quantity": plan.close_short.quantity},
              {"symbol": plan.open_short.symbol, "side": "SELL",
               "quantity": plan.close_short.quantity}]
    if plan.close_wing and plan.open_wing:
        orders += [{"symbol": plan.close_wing.symbol, "side": "SELL",
                    "quantity": plan.close_wing.quantity},
                   {"symbol": plan.open_wing.symbol, "side": "BUY",
                    "quantity": plan.close_wing.quantity}]
    for o in orders:
        row = by_symbol.get(o["symbol"])
        if not row:
            raise F.DepthUnavailable(f"{o['symbol']}: not quoted; abandoning the roll")
        o["depth"] = row["depth"]
    fills = (executor or F.simulate_basket)(orders, cfg)
    return {o["symbol"]: f for o, f in zip(orders, fills)}
