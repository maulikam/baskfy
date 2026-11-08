"""Desk-shaped plan previews for curated-basket invest / apply / exit (SC3).

Pure arithmetic over holdings, target weights, and prices. No database, no network, no
ambient clock — callers pass ``now`` so ``expires_at_hint`` is deterministic (desk
non-negotiable #1: plans expire in 30 minutes). The API layer stamps a synthetic
``desk_plan_id``; nothing here reaches ``OrderGateway``.

:func:`plan_is_expired` is that non-negotiable's clock, as a pure predicate beside the
constant it enforces. It answers the question; it does not hold plans and it does not read a
clock — the store that does both is ``baskfy_api.plan_store``, because law #1 keeps state, a
clock and a database out of ``packages/core``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from decimal import Decimal
from typing import Final, Literal, TypedDict

from baskfy_core.curated_metrics import shares_at_amount

__all__ = [
    "PLAN_TTL",
    "DeskPlan",
    "PlanKind",
    "PlanLeg",
    "build_apply_plan",
    "build_customize_plan",
    "build_exit_plan",
    "build_invest_plan",
    "plan_expires_at",
    "plan_is_expired",
]

PlanKind = Literal["BUY", "REBALANCE", "EXIT", "CUSTOMIZE"]


class PlanLeg(TypedDict):
    """One desk-shaped leg. Named keys, not a bare ``dict`` — the API layer reads them.

    ``ref_price`` is a reference price for sizing only; nothing here becomes an order.
    """

    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    ref_price: Decimal


class DeskPlan(TypedDict):
    """A plan preview. ``expires_at_hint`` is the caller's ``now`` + :data:`PLAN_TTL`."""

    kind: PlanKind
    legs: list[PlanLeg]
    requested_amount: Decimal
    expires_at_hint: dt.datetime


#: Desk non-negotiable #1 — plans expire thirty minutes after issue.
PLAN_TTL: Final = dt.timedelta(minutes=30)


def plan_expires_at(issued_at: dt.datetime) -> dt.datetime:
    """The instant a plan issued at *issued_at* stops being executable.

    One definition, read by both the preview (``expires_at_hint``) and the enforcement
    (:func:`plan_is_expired`), so the number a caller is shown and the number that refuses
    them cannot drift apart.
    """
    return issued_at + PLAN_TTL


def plan_is_expired(*, issued_at: dt.datetime, now: dt.datetime) -> bool:
    """Non-negotiable #1's clock, as a pure predicate: is this plan too old to execute?

    Pure on purpose. The rule lived at ``kite-momentum-rebalancer/app/main.py:519-524`` as
    ``time.time() - plan["created_at"] > 1800`` — inside a route handler, reachable only by
    posting a form, and deleted the day the desk retires. Here it is a function of two
    arguments with no database, no network and no ambient clock (law #1), which is what lets
    the boundary be asserted at the second rather than approximated with ``sleep``. The
    *store* that holds plans and reads a wall clock lives in ``services/``.

    THE BOUNDARY IS CLOSED AT THE START AND OPEN AT THE END: a plan is live over
    ``[issued_at, issued_at + PLAN_TTL)``. At exactly thirty minutes it is expired. The desk's
    ``> 1800`` kept it live for that one instant; this is the stricter of the two readings of
    "plans expire in 30 minutes", it is the safe direction (a refused plan is re-analysed, a
    stale one is executed against stale prices), and the difference is a single tick nothing
    can depend on.

    A plan whose ``issued_at`` is in the future is *not* expired — a clock that stepped
    backwards must not silently invalidate live plans. It is also not a licence to outlive the
    TTL: the plan expires ``PLAN_TTL`` after the stamp it carries, whenever that arrives.

    :raises ValueError: if one of the two instants is timezone-aware and the other naive.
        Subtracting them raises ``TypeError`` deep inside a comparison; refusing here says
        which argument was wrong, and an expiry check must never be the thing that guesses.
    """
    if (issued_at.tzinfo is None) != (now.tzinfo is None):
        raise ValueError(
            "issued_at and now must both be timezone-aware or both naive; got "
            f"issued_at={'aware' if issued_at.tzinfo else 'naive'}, "
            f"now={'aware' if now.tzinfo else 'naive'}"
        )
    return now >= plan_expires_at(issued_at)


def _expires_at_hint(now: dt.datetime) -> dt.datetime:
    return plan_expires_at(now)


def _leg(symbol: str, side: Literal["BUY", "SELL"], quantity: int, ref_price: Decimal) -> PlanLeg:
    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "ref_price": ref_price,
    }


def build_invest_plan(
    *,
    target_weights: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    amount: Decimal,
    now: dt.datetime,
) -> DeskPlan:
    """Lump-sum buy into a basket at *target_weights* for *amount* (kind BUY)."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    if not target_weights:
        raise ValueError("target_weights cannot be empty")

    symbols = sorted(target_weights)
    missing = [s for s in symbols if s not in prices]
    if missing:
        raise ValueError(f"missing prices for: {', '.join(missing)}")

    weight_list = [target_weights[s] for s in symbols]
    price_list = [prices[s] for s in symbols]
    qtys = shares_at_amount(amount, price_list, weight_list)
    legs = [
        _leg(symbol, "BUY", qty, prices[symbol])
        for symbol, qty in zip(symbols, qtys, strict=True)
        if qty > 0
    ]
    return {
        "kind": "BUY",
        "legs": legs,
        "requested_amount": amount,
        "expires_at_hint": _expires_at_hint(now),
    }


def build_apply_plan(
    *,
    holdings: Mapping[str, int],
    target_weights: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    amount: Decimal,
    now: dt.datetime,
) -> DeskPlan:
    """Diff current holdings vs *target_weights* sized to *amount* (kind REBALANCE).

    *amount* is the book the investor wants the basket sized to after the apply (typically
    current market value, optionally plus a top-up). Legs are BUY/SELL deltas only.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    if not target_weights:
        raise ValueError("target_weights cannot be empty")

    symbols = sorted(set(holdings) | set(target_weights))
    missing = [s for s in symbols if s not in prices]
    if missing:
        raise ValueError(f"missing prices for: {', '.join(missing)}")

    # Zero-weight / dropped names still need a price for exit sizing; shares_at_amount
    # rejects non-positive weights, so size only positive-weight constituents then exits.
    positive = [s for s in symbols if target_weights.get(s, Decimal("0")) > 0]
    if not positive:
        raise ValueError("target_weights must include at least one positive weight")

    pos_weights = [target_weights[s] for s in positive]
    pos_prices = [prices[s] for s in positive]
    target_qtys = dict(
        zip(positive, shares_at_amount(amount, pos_prices, pos_weights), strict=True)
    )

    legs: list[PlanLeg] = []
    for symbol in symbols:
        current = int(holdings.get(symbol, 0))
        target = int(target_qtys.get(symbol, 0))
        delta = target - current
        if delta > 0:
            legs.append(_leg(symbol, "BUY", delta, prices[symbol]))
        elif delta < 0:
            legs.append(_leg(symbol, "SELL", -delta, prices[symbol]))

    return {
        "kind": "REBALANCE",
        "legs": legs,
        "requested_amount": amount,
        "expires_at_hint": _expires_at_hint(now),
    }


def build_customize_plan(
    *,
    holdings: Mapping[str, int],
    target_weights: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    amount: Decimal,
    now: dt.datetime,
) -> DeskPlan:
    """Investor-driven constituent weight edit (kind CUSTOMIZE).

    Same leg arithmetic as :func:`build_apply_plan` (diff holdings vs target weights), but
    labelled ``CUSTOMIZE`` so fee / history paths treat it as a zero-platform-fee manage
    action rather than an ENGINE rebalance (docs/smallcase/04 §1, §7).
    """
    plan = build_apply_plan(
        holdings=holdings,
        target_weights=target_weights,
        prices=prices,
        amount=amount,
        now=now,
    )
    return {
        "kind": "CUSTOMIZE",
        "legs": plan["legs"],
        "requested_amount": plan["requested_amount"],
        "expires_at_hint": plan["expires_at_hint"],
    }


def build_exit_plan(
    *,
    holdings: Mapping[str, int],
    prices: Mapping[str, Decimal],
    now: dt.datetime,
    requested_amount: Decimal | None = None,
) -> DeskPlan:
    """Sell all (or a notional *requested_amount* of) holdings (kind EXIT)."""
    if not holdings:
        raise ValueError("holdings cannot be empty")

    symbols = sorted(s for s, qty in holdings.items() if qty > 0)
    if not symbols:
        raise ValueError("holdings must include at least one positive quantity")

    missing = [s for s in symbols if s not in prices]
    if missing:
        raise ValueError(f"missing prices for: {', '.join(missing)}")

    if requested_amount is None:
        requested_amount = sum(
            (Decimal(holdings[s]) * prices[s] for s in symbols),
            start=Decimal("0"),
        )

    legs = [_leg(symbol, "SELL", int(holdings[symbol]), prices[symbol]) for symbol in symbols]
    return {
        "kind": "EXIT",
        "legs": legs,
        "requested_amount": requested_amount,
        "expires_at_hint": _expires_at_hint(now),
    }
