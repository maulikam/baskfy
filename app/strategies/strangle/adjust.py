"""Lever A — roll the WINNING leg toward spot until its premium matches the loser's.

There is no lever here that rolls a losing leg further out; that is Lever B and belongs to
V3. V2 is deliberately one lever, so the P&L attribution can say what rolling the winner
contributed without a second mechanism confounding it.

TWO THINGS THAT SILENTLY BREAK THE RISK MODEL IF GOT WRONG.

The wing moves WITH the short. If the short rolls 100 points toward spot and the wing stays
put, the spread widens from 500 to 600: max loss goes from 13% of capital to 15.6%, which
breaches the structural cap, and the margin the position was sized against is no longer the
margin it needs. So a roll is one atomic four-leg operation, and if the new wing cannot be
filled the whole roll is abandoned — never "fixed later", because in between you are short
a naked option.

Boundaries are recomputed against LIVE spot and LIVE ASP every time. Matching a threatened
leg's premium generally needs a strike about as close to spot as the loser is, which is
inside the boundary computed at 09:15. Checking a live premium against a stale frame makes
Lever A refuse to act in precisely the situation it exists for.
"""
from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .levels import PricedRange, priced_range
from .selection import Candidate

UP, DOWN = "UP", "DOWN"


class RollRefused(RuntimeError):
    """The roll cannot be completed as one atomic operation. Nothing is changed."""


# =====================================================================================
# break confirmation — never act on a touch
# =====================================================================================
@dataclass
class BreakTracker:
    """Two consecutive one-minute closes beyond a level.

    A touch that closes back inside is not a break. This is the mechanical form of "wait —
    either it breaks and your stop hits, or it rejects and your loss never happened", and
    it is the difference between adjusting on noise and adjusting on structure.
    """
    needed: int = 2
    closes: deque = field(default_factory=lambda: deque(maxlen=8))
    events: list[tuple[str, float, dt.datetime]] = field(default_factory=list)
    # Which break is currently in force. Cleared the moment price closes back inside, so a
    # later break in the SAME direction counts as a second event rather than being folded
    # into the first — "confirmed twice today" means twice, not once with a long tail.
    active: str | None = None

    def observe(self, close: float, *, resistance: float, support: float,
                at: dt.datetime | None = None) -> str | None:
        self.closes.append(close)
        if len(self.closes) < self.needed:
            return None
        recent = list(self.closes)[-self.needed:]
        direction = None
        if all(c > resistance for c in recent):
            direction = UP
        elif all(c < support for c in recent):
            direction = DOWN
        if direction:
            if self.active != direction:
                level = resistance if direction == UP else support
                self.events.append((direction, level, at or dt.datetime.now()))
                self.active = direction
            return direction
        self.active = None
        return None

    def is_trending(self, direction: str, spot: float) -> bool:
        """The same directional break confirmed at least twice today, with spot still
        beyond the more recent one. Anything less is a single break, not a trend."""
        same = [e for e in self.events if e[0] == direction]
        if len(same) < 2:
            return False
        _, level, _ = same[-1]
        return spot > level if direction == UP else spot < level


# =====================================================================================
# VWAP threat filter
# =====================================================================================
def threatened_legs(legs: Sequence, marks: Mapping[str, float],
                    vwaps: Mapping[str, float]) -> set[str]:
    """Symbols trading above their own session VWAP.

    An option below its session VWAP means the average short is in profit, so there is no
    short-covering pressure. Above VWAP is where a squeeze starts. This is the mechanical
    form of "as long as the average price is higher than the current market price I am
    happy to be short".
    """
    out = set()
    for leg in legs:
        v = vwaps.get(leg.symbol)
        m = marks.get(leg.symbol)
        if v is not None and m is not None and m > v:
            out.add(leg.symbol)
    return out


# =====================================================================================
# the trigger
# =====================================================================================
@dataclass(frozen=True)
class RebalanceCheck:
    allowed: bool
    reason: str
    ratio: float | None = None
    winner: Any = None
    loser: Any = None


def check_rebalance(book, *, marks: Mapping[str, float], now: dt.datetime,
                    state: Mapping[str, Any], cfg: dict) -> RebalanceCheck:
    """All four gates from section 4.6. Every one must hold.

    The throttle is not decoration. Without it the loop can fire on every tick and the
    strategy dies of transaction costs rather than of market risk.
    """
    m = cfg["management"]
    shorts = [l for l in book.open_legs if l.is_short]
    if len(shorts) != 2:
        return RebalanceCheck(False, f"expected two short legs, found {len(shorts)}")

    a, b = shorts
    if marks.get(a.symbol) is None or marks.get(b.symbol) is None:
        return RebalanceCheck(False, "a short leg has no mark")
    loser, winner = (a, b) if marks[a.symbol] >= marks[b.symbol] else (b, a)
    win_ask = marks[winner.symbol]
    ratio = (marks[loser.symbol] / win_ask) if win_ask > 0 else float("inf")

    if ratio < float(m["rebalance_ratio"]):
        return RebalanceCheck(False, f"imbalance {ratio:.2f} under "
                                     f"{float(m['rebalance_ratio']):.2f}", ratio,
                              winner, loser)

    done = int(state.get("adjustments_today", 0))
    if done >= int(m["max_adjustments_per_day"]):
        return RebalanceCheck(False, f"{done} adjustments already made today", ratio,
                              winner, loser)

    last = state.get("last_adjustment_at")
    if last is not None:
        mins = (now - last).total_seconds() / 60.0
        if mins < float(m["min_adjustment_interval_minutes"]):
            return RebalanceCheck(False, f"only {mins:.1f} min since the last adjustment",
                                  ratio, winner, loser)

    pnl = book.pnl(marks)
    floor = float(m["rebalance_allowed_above_pct_of_stop"]) * book.risk_budget
    if pnl <= floor:
        return RebalanceCheck(False, f"book {pnl:,.0f} at or below {floor:,.0f}", ratio,
                              winner, loser)

    return RebalanceCheck(True, f"imbalance {ratio:.2f}", ratio, winner, loser)


# =====================================================================================
# the roll
# =====================================================================================
@dataclass(frozen=True)
class RollPlan:
    close_short: Any                 # the winning short being bought back
    close_wing: Any | None
    open_short: Candidate
    open_wing: Candidate | None
    target_premium: float
    trending: bool
    wing_width: float

    def as_dict(self) -> dict:
        return {"close_short": self.close_short.symbol,
                "open_short": self.open_short.symbol or self.open_short.strike,
                "close_wing": self.close_wing.symbol if self.close_wing else None,
                "open_wing": (self.open_wing.symbol or self.open_wing.strike)
                             if self.open_wing else None,
                "target_premium": round(self.target_premium, 2),
                "trending": self.trending, "wing_width": self.wing_width}


def plan_winner_roll(book, *, winner, loser, chain: Sequence[Mapping[str, Any]],
                     marks: Mapping[str, float], spot: float, asp: float,
                     levels, oi, cfg: dict, step: int,
                     trending: bool = False) -> RollPlan:
    """Move the winning short toward spot to match the loser's premium.

    Raises RollRefused when no strike qualifies or the matching wing is unavailable. Doing
    nothing is always a legal outcome here: the book stop governs, and a roll that cannot
    be completed atomically is worse than no roll at all.
    """
    m = cfg["management"]["risk_on"]
    target = marks[loser.symbol] * (float(m["trending_day_multiplier"]) if trending
                                    else float(m["match_premium_multiplier"]))
    floor = target * float(m["min_fill_fraction"])

    # LIVE boundaries. A stale 09:15 frame makes this refuse exactly when it is needed.
    live_pr = priced_range(spot, asp, cfg)
    beyond = int(cfg["selection"]["min_strikes_beyond_priced_range"]) * step
    kind = winner.kind
    is_call = kind == "CE"
    limit = (live_pr.upper + beyond) if is_call else (live_pr.lower - beyond)

    def outside_priced_range(strike: float) -> bool:
        return strike >= limit if is_call else strike <= limit

    def beyond_swing(strike: float) -> bool:
        return strike > levels.resistance if is_call else strike < levels.support

    rows = [r for r in chain if r["kind"] == kind and float(r.get("bid") or 0) >= floor]
    candidates = [r for r in rows if outside_priced_range(float(r["strike"]))]
    # The entry's structural boundaries still apply, recomputed at live spot.
    if cfg["selection"].get("must_be_beyond_swing_level", True):
        candidates = [r for r in candidates if beyond_swing(float(r["strike"]))]
    if not candidates:
        raise RollRefused(
            f"no {kind} at or beyond {limit:.0f} with a bid over {floor:.2f}; "
            "letting the book stop govern")

    # Nearest to spot on the winner's side: that is what "roll toward spot" means.
    best = min(candidates, key=lambda r: abs(float(r["strike"]) - spot))
    new_short = Candidate(float(best["strike"]), kind, float(best["bid"]),
                          float(best["ask"]), float(best.get("oi") or 0.0),
                          str(best.get("symbol") or ""))
    if new_short.strike == winner.strike:
        raise RollRefused("the best strike is the one already held")

    # The wing moves with it, at the SAME width. Never let a roll widen the spread.
    close_wing = new_wing = None
    width = float(cfg["structure"]["wing_width_points"])
    if cfg["structure"]["type"].upper() == "HEDGED":
        role = "wing_call" if is_call else "wing_put"
        existing = [l for l in book.open_legs if l.role == role]
        if not existing:
            raise RollRefused(f"hedged book has no {role} to roll")
        close_wing = existing[0]
        want = new_short.strike + width if is_call else new_short.strike - width
        row = next((r for r in chain
                    if r["kind"] == kind and abs(float(r["strike"]) - want) < 1e-6), None)
        if row is None or float(row.get("ask") or 0) <= 0:
            raise RollRefused(
                f"no fillable {kind} wing at {want:.0f}; abandoning the roll rather than "
                "leaving a short unhedged or widening the spread")
        new_wing = Candidate(float(row["strike"]), kind, float(row.get("bid") or 0.0),
                             float(row["ask"]), float(row.get("oi") or 0.0),
                             str(row.get("symbol") or ""))
        assert_wing_invariant(new_short.strike, new_wing.strike, width, is_call)

    return RollPlan(close_short=winner, close_wing=close_wing, open_short=new_short,
                    open_wing=new_wing, target_premium=target, trending=trending,
                    wing_width=width)


def assert_wing_invariant(short_strike: float, wing_strike: float, width: float,
                          is_call: bool) -> None:
    """wing - short == width, every tick, for the life of the position.

    Checked as an invariant rather than trusted, because the failure is silent: the book
    still looks like a condor, the margin figure it was sized against is simply wrong.
    """
    gap = (wing_strike - short_strike) if is_call else (short_strike - wing_strike)
    if abs(gap - width) > 1e-6:
        raise RollRefused(
            f"wing width would become {gap:.0f} instead of {width:.0f}: max loss and "
            "margin would both change without the position looking any different")


def check_book_invariant(book, cfg: dict) -> None:
    """Every open short has a wing at exactly the configured width."""
    if cfg["structure"]["type"].upper() != "HEDGED":
        return
    width = float(cfg["structure"]["wing_width_points"])
    wings = {l.role: l for l in book.open_legs if l.role.startswith("wing")}
    for leg in book.open_legs:
        if not leg.is_short:
            continue
        role = "wing_call" if leg.kind == "CE" else "wing_put"
        wing = wings.get(role)
        if wing is None:
            raise RollRefused(f"{leg.symbol} is short with no {role}")
        assert_wing_invariant(leg.strike, wing.strike, width, leg.kind == "CE")


# =====================================================================================
# applying the roll
# =====================================================================================
def apply_roll(book, plan: RollPlan, fills: Mapping[str, Any], cfg: dict,
               *, lot_size: int, now: dt.datetime | None = None) -> dict:
    """Close the old pair and open the new one as ONE operation.

    `fills` maps symbol -> SimFill for all four legs. Every one must be present and
    complete before anything is mutated: a partially applied roll leaves the book in a
    shape the risk model does not describe, and the whole reason the wing moves with the
    short is that no intermediate state is acceptable.
    """
    from .book import Leg, cost_of

    now = now or dt.datetime.now()
    required = [plan.close_short.symbol, plan.open_short.symbol]
    if plan.close_wing and plan.open_wing:
        required += [plan.close_wing.symbol, plan.open_wing.symbol]
    missing = [s for s in required if s not in fills or not fills[s].complete]
    if missing:
        raise RollRefused(f"roll not fillable in full: {missing}; book unchanged")

    priced = [{"side": "BUY", "price": fills[plan.close_short.symbol].avg_price,
               "quantity": plan.close_short.quantity},
              {"side": "SELL", "price": fills[plan.open_short.symbol].avg_price,
               "quantity": plan.close_short.quantity}]
    if plan.close_wing and plan.open_wing:
        priced += [{"side": "SELL", "price": fills[plan.close_wing.symbol].avg_price,
                    "quantity": plan.close_wing.quantity},
                   {"side": "BUY", "price": fills[plan.open_wing.symbol].avg_price,
                    "quantity": plan.close_wing.quantity}]
    cost = cost_of(priced, lot_size)

    book.close_leg(plan.close_short.symbol,
                   fills[plan.close_short.symbol].avg_price, cost=cost, when=now)
    book.add(Leg(symbol=plan.open_short.symbol, kind=plan.open_short.kind,
                 strike=plan.open_short.strike, side="SELL",
                 quantity=plan.close_short.quantity,
                 entry_price=fills[plan.open_short.symbol].avg_price,
                 role=plan.close_short.role, opened_at=now))
    if plan.close_wing and plan.open_wing:
        book.close_leg(plan.close_wing.symbol,
                       fills[plan.close_wing.symbol].avg_price, when=now)
        book.add(Leg(symbol=plan.open_wing.symbol, kind=plan.open_wing.kind,
                     strike=plan.open_wing.strike, side="BUY",
                     quantity=plan.close_wing.quantity,
                     entry_price=fills[plan.open_wing.symbol].avg_price,
                     role=plan.close_wing.role, opened_at=now))

    # The invariant is re-checked AFTER mutation, not just planned for. A roll that leaves
    # the book mis-shaped must be discovered here, not at the next margin call.
    check_book_invariant(book, cfg)
    return {"closed": plan.close_short.symbol, "opened": plan.open_short.symbol,
            "wing_closed": plan.close_wing.symbol if plan.close_wing else None,
            "wing_opened": plan.open_wing.symbol if plan.open_wing else None,
            "cost": round(cost, 2), "at": now.isoformat(timespec="seconds")}
