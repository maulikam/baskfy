"""Normalized holdings shape for every broker adapter (M41).

Law 2 still holds: ``packages/execution`` is the only path that may talk to a trading
credential. This module is the *read* half of that surface — a common holdings row — so a
future per-user connection can sync a book without inventing a second gateway.

Adapters never place an order from here. Order placement stays on the Trading protocol /
gateway. A holdings sync that could also fire ``place_order`` would collapse the two faces
M16 kept apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

__all__ = ["HoldingRow", "OAuthStart", "normalize_holding", "total_quantity"]


@dataclass(frozen=True, slots=True)
class OAuthStart:
    """Where to send the browser to begin a broker login, and the state that ties it back.

    Referenced by `adapters.py` since ef50c09 but never defined — an ImportError that aborted
    collection for the whole of `packages/execution/tests`, unnoticed because that directory was
    missing from `testpaths`. Restored here, beside the other wire shape this module owns.

    ``state`` is carried rather than re-derived: the callback validates the value the authorize
    URL actually went out with, and a state regenerated at check time would validate nothing.
    """

    authorize_url: str
    state: str


@dataclass(frozen=True, slots=True)
class HoldingRow:
    """One position, in Baskfy's shape.

    Quantities follow the desk's non-negotiable #2: total = quantity + t1 + collateral.
    """

    symbol: str
    exchange: str
    quantity: Decimal
    t1_quantity: Decimal
    collateral_quantity: Decimal
    average_price: Decimal
    last_price: Decimal | None = None
    product: str = "CNC"


def normalize_holding(
    *,
    symbol: str,
    exchange: str = "NSE",
    quantity: Decimal | int | str,
    t1_quantity: Decimal | int | str = 0,
    collateral_quantity: Decimal | int | str = 0,
    average_price: Decimal | int | str,
    last_price: Decimal | int | str | None = None,
    product: str = "CNC",
) -> HoldingRow:
    """Build a :class:`HoldingRow` from broker-native numbers (house rule 9: Decimal)."""
    return HoldingRow(
        symbol=symbol.strip().upper(),
        exchange=exchange.strip().upper(),
        quantity=Decimal(str(quantity)),
        t1_quantity=Decimal(str(t1_quantity)),
        collateral_quantity=Decimal(str(collateral_quantity)),
        average_price=Decimal(str(average_price)),
        last_price=None if last_price is None else Decimal(str(last_price)),
        product=product,
    )


def total_quantity(row: HoldingRow) -> Decimal:
    """Non-negotiable #2: quantity + t1 + collateral."""
    return row.quantity + row.t1_quantity + row.collateral_quantity
