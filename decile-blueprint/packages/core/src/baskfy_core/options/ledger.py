"""A closed session's journal figures, pure (``04`` §12, ``06`` OP11).

"Close → ``op_journal`` with the cost breakdown and MAE/MFE." Everything here is computed from the
session's own **fills** — never from the plan's intentions — so the journal records what happened:

* **gross** is the signed cash of every fill (a sale brings premium in, a purchase pays it out),
  entry and exit together; **entry** and **exit** are the two halves' cash, unsigned;
* **costs** are ``04`` §6.1's charges over every executed order (``costs.charges`` — the rates OP0
  verified), so a paper row carries the costs a live one would have paid;
* **net** = gross - costs; **R** is the risk the trade was taken at (``04`` §9.1: the budget in
  force, or ``risk_per_lot_inr`` in paper-one-lot) and ``r_multiple = net / R``;
* **MFE / MAE** are the best and worst marked P&L the monitor saw (``op_position.peak_value`` /
  ``trough_value``, points per unit, a gain positive), in R.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from baskfy_core.options.config import CostRates, Side
from baskfy_core.options.costs import ChargeBreakdown, CostFill, charges, paise
from baskfy_core.options.journal import r_multiple
from baskfy_core.options.structures import Structure

_R_PLACES = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class LegFill:
    """One executed fill: side, price per unit, units, and whether it closed the position."""

    side: Side
    price: Decimal
    quantity: int
    closing: bool


@dataclass(frozen=True, slots=True)
class JournalFigures:
    entry_inr: Decimal
    exit_inr: Decimal
    gross_pnl_inr: Decimal
    costs: ChargeBreakdown
    net_pnl_inr: Decimal
    r_inr: Decimal
    r_multiple: Decimal
    minutes_held: int
    mae_r: Decimal | None
    mfe_r: Decimal | None


def pnl_points(structure: Structure, entry_points: Decimal, value_points: Decimal) -> Decimal:
    """Marked P&L per unit, a gain positive: a condor's credit less the cost to close it; a long's
    or a spread's close value less what was paid."""
    if structure is Structure.IRON_CONDOR:
        return entry_points - value_points
    return value_points - entry_points


def _cash(fills: Sequence[LegFill]) -> Decimal:
    return sum(
        (f.price * f.quantity * (1 if f.side is Side.SELL else -1) for f in fills), Decimal(0)
    )


def journal_figures(  # noqa: PLR0913 - every input 04 §12 names, by keyword
    fills: Sequence[LegFill],
    *,
    r_inr: Decimal,
    quantity: int,
    opened_at: dt.datetime,
    closed_at: dt.datetime,
    peak_points: Decimal | None,
    trough_points: Decimal | None,
    rates: CostRates,
) -> JournalFigures:
    """The row ``op_journal`` stores for one closed session."""
    if r_inr <= 0:
        raise ValueError("R must be positive")
    opening = [f for f in fills if not f.closing]
    closing = [f for f in fills if f.closing]
    gross = paise(_cash(fills))
    cost = charges([CostFill(f.side, f.price, f.quantity) for f in fills], rates)
    net = paise(gross - cost.total)

    def in_r(points: Decimal | None) -> Decimal | None:
        return None if points is None else (points * quantity / r_inr).quantize(_R_PLACES)

    return JournalFigures(
        entry_inr=paise(abs(_cash(opening))),
        exit_inr=paise(abs(_cash(closing))),
        gross_pnl_inr=gross,
        costs=cost,
        net_pnl_inr=net,
        r_inr=paise(r_inr),
        r_multiple=r_multiple(net, r_inr).quantize(_R_PLACES),
        minutes_held=max(int((closed_at - opened_at).total_seconds() // 60), 0),
        mae_r=in_r(trough_points),
        mfe_r=in_r(peak_points),
    )


__all__ = ["JournalFigures", "LegFill", "journal_figures", "pnl_points"]
