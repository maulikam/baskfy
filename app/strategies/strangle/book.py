"""Positions and book-level P&L.

THE BOOK IS THE UNIT OF RISK. NEVER A LEG.
A short strangle is one position expressed in four contracts. Judging any of them alone is
meaningless: the whole design is that when one leg loses the other is winning. In the
source's own account a single leg showed a Rs 4.5 lakh loss on a day the book was in
profit — a per-leg stop would have closed a winning session.

So book_pnl sums realised P&L from every leg ever closed, plus unrealised on everything
still open, net of costs. There is no per-leg stop anywhere in this codebase, and
test_book.py asserts that a leg at -300% does not on its own produce an exit.

MARKING IS AT LIQUIDATION, ALWAYS.
Shorts are marked at the ask (what it costs to buy them back), longs at the bid. That
embeds the full round-trip cost from the first tick, so the book opens at roughly -1.0 to
-1.5 points on four legs before the market has moved at all. That is real money and must
not be "corrected" — it is exactly what you would pay to flatten immediately.

One consequence worth stating because it has bitten this project already: because marks
are net, the expectancy model must use the RAW target and stop (10 and -5), not the raw
numbers minus costs. Charging costs in the marks AND again in the model double-counts.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from ..options_costs import CostRates, Fill, option_costs


@dataclass(frozen=True)
class Leg:
    symbol: str
    kind: str                 # CE | PE
    strike: float
    side: str                 # SELL (short) | BUY (long wing)
    quantity: int
    entry_price: float
    role: str = ""            # short_call | short_put | wing_call | wing_put
    opened_at: dt.datetime | None = None
    exit_price: float | None = None
    closed_at: dt.datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def is_short(self) -> bool:
        return self.side.upper() == "SELL"

    def realised(self) -> float:
        """Gross realised P&L in rupees. Costs are accounted separately, once."""
        if self.exit_price is None:
            return 0.0
        move = (self.entry_price - self.exit_price) if self.is_short \
            else (self.exit_price - self.entry_price)
        return move * self.quantity

    def unrealised(self, mark: float) -> float:
        """Marked at liquidation: a short costs `mark` (the ask) to close."""
        move = (self.entry_price - mark) if self.is_short else (mark - self.entry_price)
        return move * self.quantity


@dataclass
class Book:
    legs: list[Leg] = field(default_factory=list)
    accrued_costs: float = 0.0
    entry_credit: float = 0.0          # rupees received at entry, net of wing cost
    units_at_entry: int = 0
    stop_points: float = 0.0
    target_points: float = 0.0
    high_water_mark: float = 0.0
    # Book P&L at the moment the CURRENT position was opened. Zero for the first entry.
    # After a re-entry the day's realised profit is already banked, and judging the new
    # position's stop against the day total would let a good morning fund a much larger
    # afternoon loss than the stop was ever meant to permit.
    baseline: float = 0.0

    # --- construction -----------------------------------------------------------------
    def add(self, leg: Leg, cost: float = 0.0) -> None:
        self.legs.append(leg)
        self.accrued_costs += cost

    def close_leg(self, symbol: str, price: float, cost: float = 0.0,
                  when: dt.datetime | None = None) -> None:
        for i, leg in enumerate(self.legs):
            if leg.symbol == symbol and leg.is_open:
                self.legs[i] = Leg(**{**leg.__dict__, "exit_price": price,
                                      "closed_at": when})
                self.accrued_costs += cost
                return
        raise KeyError(f"no open leg {symbol!r}")

    # --- state ------------------------------------------------------------------------
    @property
    def open_legs(self) -> list[Leg]:
        return [l for l in self.legs if l.is_open]

    @property
    def closed_legs(self) -> list[Leg]:
        return [l for l in self.legs if not l.is_open]

    @property
    def is_flat(self) -> bool:
        return not self.open_legs

    @property
    def risk_budget(self) -> float:
        """stop_points * units_at_entry, in rupees.

        Fixed at entry. It does NOT move when legs are rolled, because the amount you were
        willing to lose is a decision made once, not a function of where the position
        drifted to. Called stop_cash in the operational rules; same quantity, one name.
        """
        return self.stop_points * self.units_at_entry

    @property
    def target_cash(self) -> float:
        return self.target_points * self.units_at_entry

    # --- the number everything depends on ---------------------------------------------
    def pnl(self, marks: Mapping[str, float]) -> float:
        """Realised + unrealised across EVERY leg, net of costs.

        `marks` maps symbol -> liquidation price (ask for shorts, bid for longs). A missing
        mark raises: a book valued with a leg silently at zero is not a valuation.
        """
        total = sum(l.realised() for l in self.closed_legs)
        for leg in self.open_legs:
            if leg.symbol not in marks:
                raise KeyError(
                    f"no mark for open leg {leg.symbol!r}; refusing to value the book "
                    "with a missing leg")
            total += leg.unrealised(marks[leg.symbol])
        return total - self.accrued_costs

    def session_pnl(self, marks: Mapping[str, float]) -> float:
        """P&L of the position currently open, measured from its own baseline.

        Identical to pnl() before any re-entry, which is why every existing rule can use
        it unchanged.
        """
        return self.pnl(marks) - self.baseline

    def pnl_points(self, marks: Mapping[str, float]) -> float:
        """Book P&L expressed in index points, which is how every threshold is written."""
        return self.pnl(marks) / self.units_at_entry if self.units_at_entry else 0.0

    def leg_pnl_pct(self, marks: Mapping[str, float]) -> dict[str, float]:
        """Per-leg P&L as a percentage of its own credit — for the JOURNAL ONLY.

        Deliberately not consumed by any rule. It exists so a reader can see the -300% leg
        that the book correctly ignored.
        """
        out = {}
        for leg in self.legs:
            mark = leg.exit_price if not leg.is_open else marks.get(leg.symbol)
            if mark is None or leg.entry_price <= 0:
                continue
            move = (leg.entry_price - mark) if leg.is_short else (mark - leg.entry_price)
            out[leg.symbol] = move / leg.entry_price * 100.0
        return out

    def net_close_cost(self, marks: Mapping[str, float]) -> float:
        """What it costs right now to flatten: buy back shorts, sell the wings.

        Used by the premium-exhausted exit. Measuring the shorts alone overstates the cost
        of closing a hedged book, because the wings are worth something on the way out.
        """
        cost = 0.0
        for leg in self.open_legs:
            mark = marks[leg.symbol]
            cost += mark * leg.quantity if leg.is_short else -mark * leg.quantity
        return cost


def cost_of(fills: Sequence[Mapping], lot_size: int,
            rates: CostRates | None = None) -> float:
    """Statutory + brokerage cost of a set of fills, in rupees.

    Delegates to options_costs, which carries each rate with its source and effective date
    and is covered by its own tests. Spread cost is NOT added here: it is already embedded
    in the fill prices the depth walk produced, and adding it again would double-charge the
    one cost this project is most careful about.
    """
    cf = [Fill(side=f["side"], price=float(f["price"]), quantity=int(f["quantity"]))
          for f in fills]
    return option_costs(cf, rates=rates, lot_size=lot_size).statutory_total
