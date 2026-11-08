"""Lots from a risk budget; margin a ceiling, never a source (``04`` §3, §10; Track C §6).

``lots = floor(min(capital x risk %, BASKFY_FNO_RISK_PER_TRADE_INR_MAX) ÷ (risk per unit x
lot_size))``, capped at ``fo_max_lots`` (and its ceiling 10). F1's risk per unit is the condor's
max loss per unit (credit subtracted); F2's is ``entry - stop``. **Zero lots is
``REJECTED_SIZE``, never rounded up to one.** A sleeve at ₹0 runs one lot on paper — recording
what the live size would have been at the per-trade ceiling — and live refuses
``NO_SLEEVE_CAPITAL``. The broker's basket margin is checked **after** sizing and can only
reject (``REJECTED_MARGIN``); it never changes the lots.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from baskfy_core.fno.config import CommonConfig, FnoCeilings, PlanState
from baskfy_core.options.config import Mode, SizingMode

_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class FoSizing:
    """The sizing's answer. ``lots == 0`` iff ``state`` is set."""

    lots: int
    sizing_mode: SizingMode
    risk_budget_inr: Decimal
    risk_per_lot_inr: Decimal
    #: The plan's max loss in ₹ (lots x risk per lot); the journal's R for a paper-one-lot plan.
    max_loss_inr: Decimal
    #: Paper at ₹0 only: the lots a live plan would get with the per-trade ceiling as budget.
    lots_at_ceiling: int | None
    state: PlanState | None
    message: str


def _floor_lots(budget: Decimal, per_lot: Decimal) -> int:
    return int((budget / per_lot).to_integral_value(rounding=ROUND_FLOOR))


def risk_budget(capital_inr: Decimal, risk_pct: Decimal, ceilings: FnoCeilings) -> Decimal:
    """``min(capital x min(pct, RISK_PCT_MAX) ÷ 100, RISK_PER_TRADE_INR_MAX)``."""
    pct = min(risk_pct, ceilings.risk_pct_max)
    return min(capital_inr * pct / _HUNDRED, ceilings.risk_per_trade_inr_max)


def size(  # noqa: PLR0913 - every input 04 §3 names, by keyword
    *,
    mode: Mode,
    capital_inr: Decimal,
    risk_pct: Decimal,
    risk_per_unit: Decimal,
    lot_size: int | None,
    common: CommonConfig,
    ceilings: FnoCeilings,
) -> FoSizing:
    """``04`` §3 and §10, for either sleeve (``risk_per_unit`` is what differs)."""
    if lot_size is None or lot_size <= 0:
        return FoSizing(
            0,
            SizingMode.BUDGET,
            Decimal(0),
            Decimal(0),
            Decimal(0),
            None,
            PlanState.REJECTED_SIZE,
            "the master has no lot size; nothing is sized from a guess",
        )
    if risk_per_unit <= 0:
        raise ValueError("risk per unit must be positive: a structure with no risk is a bug")
    per_lot = risk_per_unit * lot_size
    cap = min(common.max_lots, common.max_lots_ceiling)
    if capital_inr <= 0:
        if mode is Mode.LIVE:
            return FoSizing(
                0,
                SizingMode.BUDGET,
                Decimal(0),
                per_lot,
                Decimal(0),
                None,
                PlanState.NO_SLEEVE_CAPITAL,
                "sleeve capital is ₹0; a live plan needs capital Maulik has set",
            )
        at_ceiling = min(_floor_lots(ceilings.risk_per_trade_inr_max, per_lot), cap)
        return FoSizing(
            1,
            SizingMode.PAPER_ONE_LOT,
            per_lot,
            per_lot,
            per_lot,
            at_ceiling,
            None,
            f"paper, capital ₹0: one lot; live at the ₹{ceilings.risk_per_trade_inr_max} "
            f"ceiling would be {at_ceiling} lots",
        )
    budget = risk_budget(capital_inr, risk_pct, ceilings)
    raw = _floor_lots(budget, per_lot)
    lots = min(raw, cap)
    if lots <= 0:
        return FoSizing(
            0,
            SizingMode.BUDGET,
            budget,
            per_lot,
            Decimal(0),
            None,
            PlanState.REJECTED_SIZE,
            f"risk budget ₹{budget:.2f} / risk per lot ₹{per_lot:.2f} = {raw} lots",
        )
    return FoSizing(
        lots,
        SizingMode.BUDGET,
        budget,
        per_lot,
        per_lot * lots,
        None,
        None,
        f"₹{budget:.2f} / ₹{per_lot:.2f} per lot → {lots} lots",
    )


class MarginVerdict(StrEnum):
    OK = "OK"
    REJECTED_MARGIN = "REJECTED_MARGIN"


@dataclass(frozen=True, slots=True)
class MarginCheck:
    verdict: MarginVerdict
    message: str


def margin_check(required_inr: Decimal, free_inr: Decimal) -> MarginCheck:
    """``basket_order_margins`` for the plan's legs ≤ free margin, or ``REJECTED_MARGIN``.
    It never returns a lot count: margin never sizes (Track C §6)."""
    if required_inr <= free_inr:
        return MarginCheck(MarginVerdict.OK, f"₹{required_inr:.2f} within free ₹{free_inr:.2f}")
    return MarginCheck(
        MarginVerdict.REJECTED_MARGIN, f"margin ₹{required_inr:.2f} > free ₹{free_inr:.2f}"
    )
