"""Simulated fills against live depth. The most important file in the project.

A short option is SOLD AT THE BID and BOUGHT BACK AT THE ASK. Filling both sides at LTP or
mid manufactures the full bid-ask spread as profit on every round trip. With four legs and
up to three adjustments a session that is roughly ten leg round trips a day, against a
planning-case edge of about 0.67% per MONTH. Half a point of fictitious edge per round trip
erases the entire strategy, and it does it silently: the equity curve looks better, not
broken.

So there is no LTP path in this module, not even as a fallback. If depth is missing the
trade is refused. A refused trade costs one session; a fabricated fill costs the whole
experiment, because you cannot tell afterwards which sessions were real.

The engine also walks the ladder rather than filling everything at the touch. Twenty lots
is 1,300 units and the best bid rarely holds that much, so the marginal units come from
worse levels. Assuming the touch is the same error as assuming mid, just smaller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


class DepthUnavailable(RuntimeError):
    """No usable depth for a leg. Never falls back to LTP — that is the whole point."""


@dataclass(frozen=True)
class Level:
    price: float
    quantity: int


@dataclass(frozen=True)
class SimFill:
    symbol: str
    side: str                 # BUY | SELL
    requested: int
    filled: int
    avg_price: float | None
    levels_consumed: int
    touch_price: float
    slippage_points: float    # avg vs the touch; always >= 0 for a marketable order
    depth_snapshot: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return self.filled == self.requested

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "side": self.side,
                "requested": self.requested, "filled": self.filled,
                "avg_price": None if self.avg_price is None else round(self.avg_price, 4),
                "touch_price": self.touch_price,
                "slippage_points": round(self.slippage_points, 4),
                "levels_consumed": self.levels_consumed,
                "complete": self.complete,
                # Every fill carries the book it was filled against, so any price in the
                # journal can be re-derived months later without trusting this code.
                "depth_snapshot": list(self.depth_snapshot)}


def _ladder(depth: Mapping[str, Any], side: str, max_levels: int) -> list[Level]:
    """The side of the book a marketable order consumes.

    A SELL hits resting BUY orders; a BUY lifts resting SELL orders. Getting this backwards
    is the same bug as filling at mid, wearing a disguise.
    """
    key = "buy" if side.upper() == "SELL" else "sell"
    rows = (depth or {}).get(key) or []
    out = []
    for row in rows[:max_levels]:
        price = float(row.get("price") or 0.0)
        qty = int(row.get("quantity") or 0)
        if price > 0 and qty > 0:
            out.append(Level(price, qty))
    return out


def simulate_fill(symbol: str, side: str, depth: Mapping[str, Any], qty: int,
                  cfg: dict) -> SimFill:
    """Walk the ladder and return the volume-weighted average price actually achievable."""
    f = cfg["fills"]
    if f["mode"] != "depth_walk":
        raise ValueError(f"fills.mode must be depth_walk, got {f['mode']!r}")

    ladder = _ladder(depth, side, int(f["walk_depth_levels"]))
    if not ladder:
        if f.get("refuse_if_depth_unavailable", True):
            raise DepthUnavailable(
                f"{symbol}: no {'bid' if side.upper() == 'SELL' else 'ask'} depth. "
                "Refusing the trade; there is no LTP fallback by design.")
        raise DepthUnavailable(symbol)

    touch = ladder[0].price
    # Queue-position penalty: a resting order is behind everything already at that price,
    # so assume the touch level is not available to us and start one tick worse.
    penalty_ticks = int(f.get("queue_position_penalty_ticks", 0))
    tick = float(f.get("tick_size", 0.05))
    adverse = -1 if side.upper() == "SELL" else 1     # sells fill lower, buys fill higher

    remaining, cost, consumed = qty, 0.0, 0
    for i, level in enumerate(ladder):
        price = level.price + (adverse * penalty_ticks * tick if i == 0 else 0.0)
        take = min(remaining, level.quantity)
        cost += take * price
        remaining -= take
        consumed += 1
        if remaining == 0:
            break

    filled = qty - remaining
    avg = cost / filled if filled else None
    slip = 0.0 if avg is None else abs(avg - touch)
    return SimFill(symbol=symbol, side=side.upper(), requested=qty, filled=filled,
                   avg_price=avg, levels_consumed=consumed, touch_price=touch,
                   slippage_points=slip,
                   depth_snapshot=tuple(dict(price=l.price, quantity=l.quantity)
                                        for l in ladder))


def simulate_basket(orders: Sequence[Mapping[str, Any]], cfg: dict) -> list[SimFill]:
    """Fill a whole basket, refusing the lot if any leg cannot be filled in full.

    Partial baskets are not a position this strategy knows how to reason about: a condor
    missing one wing is not a condor, and its margin and its tail are both wrong. Better to
    refuse the entry than to hold something the risk model does not describe.
    """
    fills = [simulate_fill(o["symbol"], o["side"], o["depth"], int(o["quantity"]), cfg)
             for o in orders]
    incomplete = [f for f in fills if not f.complete]
    if incomplete:
        raise DepthUnavailable(
            "basket not fillable in full at visible depth: "
            + ", ".join(f"{f.symbol} {f.filled}/{f.requested}" for f in incomplete))
    return fills


def mid_price_fill(symbol: str, side: str, depth: Mapping[str, Any], qty: int,
                   cfg: dict) -> SimFill:
    """DELIBERATELY WRONG reference engine — fills everything at mid.

    Exists only for the section 6 acceptance test: run a session through both engines and
    the gap between them is your spread leakage. If that gap is small, the depth walk is
    not doing its job and the result should not be believed.

    Never wire this into a live path. It has no other purpose.
    """
    buys = (depth or {}).get("buy") or []
    sells = (depth or {}).get("sell") or []
    if not buys or not sells:
        raise DepthUnavailable(symbol)
    mid = (float(buys[0]["price"]) + float(sells[0]["price"])) / 2.0
    return SimFill(symbol=symbol, side=side.upper(), requested=qty, filled=qty,
                   avg_price=mid, levels_consumed=1, touch_price=mid,
                   slippage_points=0.0, depth_snapshot=())
