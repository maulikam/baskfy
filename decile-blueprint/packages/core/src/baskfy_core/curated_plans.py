"""Desk-shaped plan previews for curated-basket invest / apply / exit (SC3).

Pure arithmetic over holdings, target weights, and prices. No database, no network, no
ambient clock — callers pass ``now`` so ``expires_at_hint`` is deterministic (desk
non-negotiable #1: plans expire in 30 minutes). The API layer stamps a synthetic
``desk_plan_id``; nothing here reaches ``OrderGateway``.
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


def _expires_at_hint(now: dt.datetime) -> dt.datetime:
    return now + PLAN_TTL


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
