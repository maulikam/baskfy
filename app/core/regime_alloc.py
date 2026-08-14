"""Sleeve-relative allocation solver + regime sell prioritisation. PURE — no I/O.

SLEEVE SEMANTICS (the thing most likely to be misread)
MIN_POSITION_WEIGHT and MAX_SINGLE_WEIGHT are percentages of the ACTIVE EQUITY SLEEVE,
not of total NAV. In R3 the sleeve is 40% of NAV, so a 10% sleeve position is 4% of NAV.
Both figures are returned: weights_sleeve for constraint checking, weights_nav for sizing.

ONE FINAL TARGET, THEN NET
The solver produces a single target book; orders are the delta against current holdings.
Regime trimming and replacement buying are therefore never separate passes, so a name that
would be trimmed by the regime and re-bought by scoring nets to one order (often zero)
instead of a round trip that pays STT twice.

INFEASIBILITY IS AN ERROR, NOT A NUDGE
If the requested position count, sleeve floors/caps and cluster cap cannot all hold, the
solver reports it. It never silently distorts weights to make the arithmetic close.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping, Sequence

from .regime import AllocationError, ForcedAction, NewBuyMode, RegimeTier

# Sell-priority buckets, worst-to-keep first.
PRIORITY_GUARD_EXIT = 0
PRIORITY_UNRANKED = 1
PRIORITY_LOSER = 2
PRIORITY_PROFIT_CLEAR = 3
PRIORITY_PROFIT_NEAR_LTCG = 4


@dataclass(frozen=True)
class Candidate:
    """A line of the full-risk target book produced by scoring, before regime limits."""
    symbol: str
    rank: int
    score: float
    price: float
    cluster: str = "other"
    liquidity: float = 0.0          # median daily traded value, for residual ordering


@dataclass(frozen=True)
class Position:
    """A currently held position."""
    symbol: str
    quantity: int
    price: float
    average_price: float = 0.0
    rank: int | None = None
    cluster: str = "other"
    guard_exit: bool = False        # an EXISTING hard-guard exit; always takes precedence
    liquidity: float = 0.0

    @property
    def value(self) -> float:
        return self.quantity * self.price

    @property
    def unrealised_pct(self) -> float:
        if self.average_price <= 0:
            return 0.0
        return (self.price / self.average_price - 1.0) * 100.0

    @property
    def is_loser(self) -> bool:
        return self.average_price > 0 and self.price < self.average_price


@dataclass(frozen=True)
class Allocation:
    weights_sleeve: Mapping[str, float]     # % of the equity sleeve, sums to ~100
    weights_nav: Mapping[str, float]        # % of total NAV, sums to ~sleeve_pct
    half_sized: frozenset[str]
    sleeve_pct: float
    cluster_weights: Mapping[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def n_positions(self) -> int:
        return len(self.weights_sleeve)


@dataclass(frozen=True)
class OrderLine:
    symbol: str
    action: str                      # BUY / ADD / TRIM / EXIT / HOLD
    qty_now: int
    delta: int
    qty_final: int
    ref_price: float
    weight_nav: float
    weight_sleeve: float
    value: float
    rank: int | None = None
    half_sized: bool = False
    tax_review: bool = False
    note: str = ""


@dataclass(frozen=True)
class RegimePlan:
    tier: RegimeTier
    sleeve_pct: float
    allocation: Allocation
    orders: tuple[OrderLine, ...]
    projected_equity_pct: float
    exposure_gap_after_plan_pct: float
    manual_action_required: bool
    executable: bool
    reason_codes: tuple[str, ...] = ()
    tax_flagged: tuple[str, ...] = ()
    infeasible_reason: str | None = None

    @property
    def sells(self) -> tuple[OrderLine, ...]:
        return tuple(o for o in self.orders if o.delta < 0)

    @property
    def buys(self) -> tuple[OrderLine, ...]:
        return tuple(o for o in self.orders if o.delta > 0)

    @property
    def round_trips(self) -> tuple[str, ...]:
        """Symbols appearing as BOTH a buy and a sell. Netting means this must be empty."""
        b = {o.symbol for o in self.buys}
        s = {o.symbol for o in self.sells}
        return tuple(sorted(b & s))


# =====================================================================================
# feasibility
# =====================================================================================
def feasible_position_range(min_sleeve_w: float, max_sleeve_w: float) -> tuple[int, int]:
    """Position counts for which floors and caps can both hold on a full sleeve."""
    if min_sleeve_w <= 0 or max_sleeve_w <= 0:
        raise AllocationError("sleeve weights must be positive")
    lo = math.ceil(100.0 / max_sleeve_w - 1e-9)
    hi = math.floor(100.0 / min_sleeve_w + 1e-9)
    return max(1, lo), max(1, hi)


def validate_allocation_request(n: int, *, min_sleeve_w: float, max_sleeve_w: float,
                                n_half: int = 0) -> None:
    """Raise AllocationError when the request cannot be satisfied without distortion."""
    if n <= 0:
        raise AllocationError("no eligible constituents to allocate to")
    if min_sleeve_w > max_sleeve_w:
        raise AllocationError(
            f"MIN_POSITION_WEIGHT ({min_sleeve_w}) exceeds MAX_SINGLE_WEIGHT "
            f"({max_sleeve_w}) — the sleeve constraints contradict each other")
    lo, hi = feasible_position_range(min_sleeve_w, max_sleeve_w)
    # Half-sized provisional entries are allowed below the floor, so they relax it.
    full = n - n_half
    if full > hi:
        raise AllocationError(
            f"{full} full-sized positions at a {min_sleeve_w}% sleeve floor need "
            f"{full * min_sleeve_w:.0f}% of the sleeve; only 100% exists. "
            f"Feasible full-size count is {lo}-{hi}.")
    if n < lo:
        raise AllocationError(
            f"{n} positions cannot absorb the sleeve without breaching the "
            f"{max_sleeve_w}% cap (need at least {lo}).")


# =====================================================================================
# solver
# =====================================================================================
def _waterfill(raw: Mapping[str, float], *, budget: float, floors: Mapping[str, float],
               caps: Mapping[str, float], max_iter: int = 64) -> dict[str, float]:
    """Proportional allocation honouring per-name floors and caps.

    Cap violators are fixed at their cap and their surplus redistributed; floor violators
    are then lifted to their floor and their shortfall taken from the rest. Repeats until
    stable, which is what keeps the result a genuine solution rather than a clipped
    approximation.
    """
    names = list(raw)
    if sum(floors.get(s, 0.0) for s in names) > budget + 1e-9:
        raise AllocationError(
            f"position floors total {sum(floors.get(s, 0.0) for s in names):.1f}% of a "
            f"{budget:.1f}% budget — infeasible")

    fixed: dict[str, float] = {}
    free = set(names)
    for _ in range(max_iter):
        remaining = budget - sum(fixed.values())
        pool = sum(max(raw[s], 0.0) for s in free)
        if pool <= 0:
            share = remaining / len(free) if free else 0.0
            w = {s: share for s in free}
        else:
            w = {s: max(raw[s], 0.0) / pool * remaining for s in free}

        over = [s for s in free if w[s] > caps.get(s, 100.0) + 1e-9]
        if over:
            for s in over:
                fixed[s] = caps.get(s, 100.0)
                free.discard(s)
            continue
        under = [s for s in free if w[s] < floors.get(s, 0.0) - 1e-9]
        if under:
            for s in under:
                fixed[s] = floors.get(s, 0.0)
                free.discard(s)
            continue
        return {**fixed, **w}
    return {**fixed, **{s: w.get(s, 0.0) for s in free}}


def solve_allocation(candidates: Sequence[Candidate], *, sleeve_pct: float,
                     min_sleeve_w: float, max_sleeve_w: float,
                     cluster_cap: float = 25.0,
                     half_sized: Iterable[str] = (),
                     half_factor: float = 0.5) -> Allocation:
    """Score-proportional sleeve allocation under floors, caps and the cluster cap.

    Cluster cap is applied to the SLEEVE, consistent with the position weights: it is a
    diversification limit on the equity book, so it should not mechanically loosen just
    because the regime shrank the sleeve.
    """
    if not candidates:
        raise AllocationError("no eligible constituents to allocate to")
    half = frozenset(half_sized)
    validate_allocation_request(len(candidates), min_sleeve_w=min_sleeve_w,
                                max_sleeve_w=max_sleeve_w, n_half=len(half))

    raw = {c.symbol: max(c.score, 1e-9) for c in candidates}
    floors = {c.symbol: (0.0 if c.symbol in half else min_sleeve_w) for c in candidates}
    caps = {c.symbol: (max_sleeve_w * half_factor if c.symbol in half else max_sleeve_w)
            for c in candidates}

    w = _waterfill(raw, budget=100.0, floors=floors, caps=caps)

    # Cluster cap: fix over-weight clusters at their share and re-solve the remainder.
    clusters = {c.symbol: (c.cluster or "other") for c in candidates}
    notes: list[str] = []
    for _ in range(8):
        agg: dict[str, float] = {}
        for s, v in w.items():
            agg[clusters[s]] = agg.get(clusters[s], 0.0) + v
        breach = [k for k, v in agg.items() if k != "other" and v > cluster_cap + 1e-9]
        if not breach:
            break
        for k in breach:
            members = [s for s in w if clusters[s] == k]
            floor_sum = sum(floors[s] for s in members)
            if floor_sum > cluster_cap + 1e-9:
                # Honest failure: the cluster cap and the position floor cannot both hold.
                # Lowering the floor here would be exactly the silent distortion the spec
                # forbids, so this is reported instead.
                raise AllocationError(
                    f"cluster {k!r} holds {len(members)} names needing {floor_sum:.1f}% "
                    f"of the sleeve at the {min(floors[s] for s in members):.1f}% floor, "
                    f"but the cluster cap is {cluster_cap:.1f}%. Reduce the position "
                    f"count, lower the floor, or raise the cluster cap.")
            scale = cluster_cap / agg[k]
            for s in members:
                # Never below the floor — the floor is a constraint, not a preference.
                caps[s] = max(floors[s], min(caps[s], w[s] * scale))
        notes.append(f"cluster cap applied: {', '.join(sorted(breach))}")
        w = _waterfill(raw, budget=100.0, floors=floors, caps=caps)

    # Half-sized entries are 50% of their solved sleeve weight; the freed weight is left
    # uninvested rather than pushed into other names — that is what "half size" means.
    for s in half:
        if s in w:
            w[s] *= half_factor

    breached = [s for s, v in w.items()
                if s not in half and v < floors[s] - 1e-6]
    if breached:
        raise AllocationError(
            f"constraints could not be satisfied without distorting weights: "
            f"{', '.join(sorted(breached))} fell below the {min_sleeve_w}% sleeve floor")

    total = sum(w.values())
    nav = {s: round(v * sleeve_pct / 100.0, 6) for s, v in w.items()}
    agg = {}
    for s, v in w.items():
        agg[clusters[s]] = round(agg.get(clusters[s], 0.0) + v, 6)
    return Allocation(weights_sleeve={s: round(v, 6) for s, v in w.items()},
                      weights_nav=nav, half_sized=half, sleeve_pct=sleeve_pct,
                      cluster_weights=agg,
                      notes=tuple(notes) + ((f"sleeve utilisation {total:.1f}%",)
                                            if total < 99.0 else ()))


# =====================================================================================
# sell prioritisation
# =====================================================================================
def sale_priority_key(p: Position, *, worst_rank: int, nearby_band: int,
                      tax_flagged: bool) -> tuple:
    """Sort key: sell FIRST what sorts smallest.

    1. existing guard-mandated exits (untouched by the regime, always first)
    2. lowest momentum rank
    3. within a nearby-rank band, unrealised losers before winners
    4. then profitable positions clear of the LTCG boundary
    5. profitable positions near the LTCG boundary go last
    """
    # Leading flag keeps guard exits ahead of EVERY band; band values are negative, so a
    # shared first element would let a low-ranked name sort in front of a guard exit.
    if p.guard_exit:
        return (0, 0, PRIORITY_GUARD_EXIT, 0)
    rank = p.rank if p.rank is not None else worst_rank + 1
    band = -(rank // nearby_band) if nearby_band > 0 else -rank
    if p.rank is None:
        bucket = PRIORITY_UNRANKED
    elif tax_flagged:
        bucket = PRIORITY_PROFIT_NEAR_LTCG
    elif p.is_loser:
        bucket = PRIORITY_LOSER
    else:
        bucket = PRIORITY_PROFIT_CLEAR
    return (1, band, bucket, -rank)


def order_sales(positions: Sequence[Position], *, nearby_band: int = 3,
                tax_flagged: Iterable[str] = ()) -> list[Position]:
    """Positions in the order the regime should sell them."""
    flagged = set(tax_flagged)
    worst = max((p.rank or 0) for p in positions) if positions else 0
    return sorted(positions, key=lambda p: sale_priority_key(
        p, worst_rank=worst, nearby_band=nearby_band, tax_flagged=p.symbol in flagged))


def select_survivors(candidates: Sequence[Candidate], positions: Sequence[Position], *,
                     max_positions: int, new_buys: NewBuyMode,
                     residual_max_names: int | None = None,
                     residual_ordering: Sequence[str] = ("rank", "liquidity"),
                     nearby_band: int = 3,
                     tax_flagged: Iterable[str] = ()) -> tuple[list[Candidate], list[str]]:
    """Choose the final constituent set. Returns (survivors, dropped_symbols).

    New names are admitted only when the tier permits buying. Provisional half-size
    entries count toward max_positions exactly like full-size ones.
    """
    held = {p.symbol for p in positions}
    by_symbol = {c.symbol: c for c in candidates}

    if residual_max_names is not None:
        # R4 residual policy: deterministic, explicit, and capped by name count.
        pool = [c for c in candidates if c.symbol in held] or list(candidates)
        for key in reversed(list(residual_ordering)):
            if key == "rank":
                pool.sort(key=lambda c: c.rank)
            elif key == "liquidity":
                pool.sort(key=lambda c: -c.liquidity)
        survivors = pool[:max(0, residual_max_names)]
    else:
        eligible = [c for c in candidates
                    if c.symbol in held or new_buys is not NewBuyMode.BLOCKED]
        eligible.sort(key=lambda c: c.rank)
        survivors = eligible[:max_positions]

    keep = {c.symbol for c in survivors}
    dropped = [p.symbol for p in order_sales(positions, nearby_band=nearby_band,
                                             tax_flagged=tax_flagged)
               if p.symbol not in keep]
    return survivors, dropped


# =====================================================================================
# netting
# =====================================================================================
def build_orders(allocation: Allocation, positions: Sequence[Position],
                 candidates: Sequence[Candidate], *, capital: float,
                 tax_flagged: Iterable[str] = (),
                 guard_exits: Iterable[str] = ()) -> list[OrderLine]:
    """One pass over the union of held and target names — netting is structural."""
    flagged = set(tax_flagged)
    guards = set(guard_exits)
    held = {p.symbol: p for p in positions}
    prices = {c.symbol: c.price for c in candidates}
    ranks = {c.symbol: c.rank for c in candidates}

    lines: list[OrderLine] = []
    for symbol in sorted(set(held) | set(allocation.weights_nav)):
        pos = held.get(symbol)
        px = prices.get(symbol) or (pos.price if pos else 0.0)
        if px <= 0:
            continue
        w_nav = allocation.weights_nav.get(symbol, 0.0)
        # A hard guard exit always wins: the regime may never re-establish a position the
        # existing guards said to leave.
        if symbol in guards:
            w_nav = 0.0
        target_qty = int(round(capital * w_nav / 100.0 / px))
        now = pos.quantity if pos else 0
        delta = target_qty - now
        if delta > 0:
            action = "BUY" if now == 0 else "ADD"
        elif delta < 0:
            action = "EXIT" if target_qty == 0 else "TRIM"
        else:
            action = "HOLD"
        lines.append(OrderLine(
            symbol=symbol, action=action, qty_now=now, delta=delta, qty_final=target_qty,
            ref_price=px, weight_nav=round(w_nav, 4),
            weight_sleeve=round(allocation.weights_sleeve.get(symbol, 0.0), 4),
            value=round(target_qty * px, 2), rank=ranks.get(symbol),
            half_sized=symbol in allocation.half_sized,
            tax_review=symbol in flagged and delta < 0,
            note="guard exit" if symbol in guards else ""))
    return lines


def projected_equity_pct(lines: Sequence[OrderLine], capital: float) -> float:
    if capital <= 0:
        return 0.0
    return round(sum(o.qty_final * o.ref_price for o in lines) / capital * 100.0, 6)
