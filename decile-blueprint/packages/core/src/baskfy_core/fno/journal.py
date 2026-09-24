"""A closed FO structure's journal figures, pure (``docs/fno/03`` §6, ``06`` FO10).

One ``fo_journal`` row per structure, in ₹ and R, computed from the position's own **fills** —
never from the plan's intentions — so the journal records what happened:

* **gross** is the signed cash of every fill (a sale brings money in, a purchase pays it out):
  the entry, every roll and the exit together;
* **costs** are itemised per order group. F1's options legs pay ``docs/options/04`` §6.1's
  charges (``baskfy_core.options.costs.charges``, the rates OP0 verified); F2's futures pay
  ``04`` §10's per-order charges (``costs.future_charges``) **with the slippage line at zero**,
  because a fill's price already carries the slippage it paid — adding the research's 0.03 %
  again would count it twice. Each roll is journalled with **its own** costs (FO10.3);
* **net** = gross - costs; **R** is the planned maximum loss the position was opened with
  (``fo_position.max_loss_inr``: the condor's max loss, the future's entry-to-trigger risk;
  ``RESEARCH.md``: "R = P&L ÷ the planned maximum loss") and ``r_multiple = net / R``;
* **MAE / MFE** are the worst and best nightly ``fo_mark`` P&L, in R (0 when a mark never went
  the other way).

``PoolKey`` is ``(sleeve, simulated)`` — the two things ``03`` §6 forbids pooling.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from baskfy_core.fno.config import FoSleeve, FutureCostRates, PlanKind, Structure
from baskfy_core.fno.costs import future_charges
from baskfy_core.options.config import CostRates, Side
from baskfy_core.options.costs import CostFill, charges, paise

#: ``fo_journal.r_multiple`` is ``numeric(8, 2)``: rounded here, at write time (house rule 8).
_R_PLACES = Decimal("0.01")


class PooledRows(ValueError):
    """Rows of more than one ``(sleeve, simulated)`` handed to one figure."""


class CloseKind(StrEnum):
    """Every way an FO structure is closed and journalled (``04`` §1, §10; FO7)."""

    PROFIT_TAKE = "PROFIT_TAKE"
    LOSS_CLOSE = "LOSS_CLOSE"
    HARD_EXIT = "HARD_EXIT"
    LATE_EXIT = "LATE_EXIT"
    STOP = "STOP"
    TIME_EXIT = "TIME_EXIT"
    NAKED_FUTURE = "NAKED_FUTURE"
    ROLL_INCOMPLETE = "ROLL_INCOMPLETE"
    #: An entry that did not fill in full, whose filled legs were closed at once (FO7, FO10.4).
    ABANDONED_PARTIAL = "ABANDONED_PARTIAL"


@dataclass(frozen=True, slots=True)
class FoFillRecord:
    """One executed fill of a position, tagged with the plan it rode on."""

    plan_id: str
    kind: PlanKind
    side: Side
    price: Decimal
    quantity: int
    filled_at: dt.datetime

    @property
    def cash(self) -> Decimal:
        """Signed: a sale brings money in, a purchase pays it out."""
        amount = self.price * self.quantity
        return amount if self.side is Side.SELL else -amount


@dataclass(frozen=True, slots=True)
class CostLines:
    """One order group's costs, itemised and rounded to the paisa (house rule 8)."""

    orders: int
    brokerage: Decimal
    stt: Decimal
    exchange_txn: Decimal
    sebi: Decimal
    ipft: Decimal
    stamp: Decimal
    gst: Decimal

    @property
    def total(self) -> Decimal:
        return (
            self.brokerage
            + self.stt
            + self.exchange_txn
            + self.sebi
            + self.ipft
            + self.stamp
            + self.gst
        )

    def as_json(self) -> dict[str, str | int]:
        return {
            "orders": self.orders,
            "brokerage": str(self.brokerage),
            "stt": str(self.stt),
            "exchange_txn": str(self.exchange_txn),
            "sebi": str(self.sebi),
            "ipft": str(self.ipft),
            "stamp": str(self.stamp),
            "gst": str(self.gst),
            "total": str(self.total),
        }


@dataclass(frozen=True, slots=True)
class RollRecord:
    """One F2 roll: the held month sold, the next bought, and what the two orders cost."""

    plan_id: str
    on: dt.date
    sold_inr: Decimal
    bought_inr: Decimal
    costs: CostLines


@dataclass(frozen=True, slots=True)
class FoJournalFigures:
    entry_inr: Decimal
    exit_inr: Decimal
    gross_pnl_inr: Decimal
    entry_costs: CostLines
    exit_costs: CostLines
    rolls: tuple[RollRecord, ...]
    costs_inr: Decimal
    net_pnl_inr: Decimal
    r_inr: Decimal
    r_multiple: Decimal
    mae_r: Decimal | None
    mfe_r: Decimal | None

    def cost_detail(self) -> dict[str, object]:
        """The itemised costs as ``fo_journal.detail['costs']`` stores them."""
        return {
            "entry": self.entry_costs.as_json(),
            "exit": self.exit_costs.as_json(),
            "rolls": [
                {
                    "plan_id": r.plan_id,
                    "on": r.on.isoformat(),
                    "sold_inr": str(r.sold_inr),
                    "bought_inr": str(r.bought_inr),
                    "costs": r.costs.as_json(),
                }
                for r in self.rolls
            ],
            "total": str(self.costs_inr),
        }


_NO_COST = CostLines(0, *(Decimal("0.00") for _ in range(7)))


def r_multiple(net_pnl_inr: Decimal, r_inr: Decimal) -> Decimal:
    """``net ÷ R``. R must be positive: a zero R is a sizing bug, not a zero trade."""
    if r_inr <= 0:
        raise ValueError("R must be positive")
    return (net_pnl_inr / r_inr).quantize(_R_PLACES, rounding=ROUND_HALF_UP)


def option_costs(fills: Sequence[FoFillRecord], rates: CostRates) -> CostLines:
    """``docs/options/04`` §6.1 over a group of option orders (one fill is one order)."""
    if not fills:
        return _NO_COST
    c = charges([CostFill(f.side, f.price, f.quantity) for f in fills], rates)
    return CostLines(c.orders, c.brokerage, c.stt, c.exchange_txn, c.sebi, c.ipft, c.stamp, c.gst)


def future_costs(fills: Sequence[FoFillRecord], rates: FutureCostRates) -> CostLines:
    """``04`` §10's futures charges over a group of orders, slippage excluded (it is in the
    fill price already)."""
    if not fills:
        return _NO_COST
    buys = sum((f.price * f.quantity for f in fills if f.side is Side.BUY), Decimal(0))
    sells = sum((f.price * f.quantity for f in fills if f.side is Side.SELL), Decimal(0))
    c = future_charges(buys, sells, len(fills), dataclasses.replace(rates, slippage_pct=Decimal(0)))
    zero = Decimal("0.00")
    return CostLines(c.orders, c.brokerage, c.stt, c.exchange_txn, zero, zero, c.stamp, c.gst)


def _sum_lines(lines: Iterable[CostLines]) -> Decimal:
    return sum((line.total for line in lines), Decimal(0))


def journal_figures(  # noqa: PLR0913 - every input 03 §6 names, by keyword
    *,
    structure: Structure,
    fills: Sequence[FoFillRecord],
    r_inr: Decimal,
    marks_inr: Sequence[Decimal],
    option_rates: CostRates,
    future_rates: FutureCostRates,
) -> FoJournalFigures:
    """The figures one ``fo_journal`` row stores for a closed structure.

    ``fills`` are every fill of the position (entry, rolls, exit). ``marks_inr`` are its nightly
    ``fo_mark.pnl_inr`` values, for MAE/MFE."""
    if r_inr <= 0:
        raise ValueError("R must be positive")
    entry = [f for f in fills if f.kind is PlanKind.ENTRY]
    exits = [f for f in fills if f.kind is PlanKind.EXIT]
    roll_ids = sorted({f.plan_id for f in fills if f.kind is PlanKind.ROLL})
    if structure is Structure.IRON_CONDOR:
        entry_costs = option_costs(entry, option_rates)
        exit_costs = option_costs(exits, option_rates)
    else:
        entry_costs = future_costs(entry, future_rates)
        exit_costs = future_costs(exits, future_rates)
    rolls: list[RollRecord] = []
    for plan_id in roll_ids:
        group = [f for f in fills if f.plan_id == plan_id]
        rolls.append(
            RollRecord(
                plan_id=plan_id,
                on=min(f.filled_at for f in group).date(),
                sold_inr=paise(sum((f.cash for f in group if f.side is Side.SELL), Decimal(0))),
                bought_inr=paise(-sum((f.cash for f in group if f.side is Side.BUY), Decimal(0))),
                costs=future_costs(group, future_rates),
            )
        )
    gross = paise(sum((f.cash for f in fills), Decimal(0)))
    costs = paise(_sum_lines([entry_costs, exit_costs, *(r.costs for r in rolls)]))
    net = paise(gross - costs)
    worst = min(marks_inr, default=None)
    best = max(marks_inr, default=None)
    return FoJournalFigures(
        entry_inr=paise(abs(sum((f.cash for f in entry), Decimal(0)))),
        exit_inr=paise(abs(sum((f.cash for f in exits), Decimal(0)))),
        gross_pnl_inr=gross,
        entry_costs=entry_costs,
        exit_costs=exit_costs,
        rolls=tuple(rolls),
        costs_inr=costs,
        net_pnl_inr=net,
        r_inr=paise(r_inr),
        r_multiple=r_multiple(net, r_inr),
        mae_r=None if worst is None else r_multiple(min(worst, Decimal(0)), r_inr),
        mfe_r=None if best is None else r_multiple(max(best, Decimal(0)), r_inr),
    )


@dataclass(frozen=True, slots=True)
class PoolKey:
    sleeve: FoSleeve
    simulated: bool


def one_pool(keys: Iterable[PoolKey]) -> PoolKey | None:
    """The single key of a set of rows, ``None`` when empty; ``PooledRows`` when mixed."""
    found = set(keys)
    if len(found) > 1:
        raise PooledRows(f"one figure per (sleeve, simulated); got {len(found)} keys")
    return next(iter(found), None)


__all__ = [
    "CloseKind",
    "CostLines",
    "FoFillRecord",
    "FoJournalFigures",
    "PoolKey",
    "PooledRows",
    "RollRecord",
    "future_costs",
    "journal_figures",
    "one_pool",
    "option_costs",
    "r_multiple",
]
