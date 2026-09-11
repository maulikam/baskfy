"""What a portfolio's recorded trades cost, from the exact model the desk already uses.

`docs/twt` and the portfolio attribution panel both listed "Fees, brokerage and taxes" as a figure
Baskfy could not produce. Four of the panel's other blocked effects need data that exists nowhere —
a sector map, benchmark constituents with weights, per-holding return series, a record of money
moving between cash and shares. **This one was different and its own entry said so:** *"Needs:
wiring the existing cost model through the portfolio rebalance path."*

The model exists, and it is not an approximation. `baskfy_core.costs` was recomputed on 19 Aug 2026
against 163 real fills — ₹73,49,552 of turnover — and every component reproduces exactly. Nothing
here restates a rate; each order goes through `order_cost`.

WHAT THIS IS, AND THE SENTENCE THAT MUST TRAVEL WITH IT
-------------------------------------------------------
**An estimate of what the recorded trades cost. Not a billed amount, and not a term that
reconciles.** `portfolio_nav_daily` is *not* net of these charges, so a return shown beside this
figure is still gross. Presenting it as "net of fees" would be the gross number under a different
name, which is the exact complaint the blocked entry made in the first place — it would be a
regression dressed as a feature.

Two further limits, stated rather than discovered:

* It can only cost the trades Baskfy has recorded. A portfolio whose holdings arrived by broker
  sync with no cash-flow rows behind them has no trades here, and gets **no figure with a reason**
  rather than ₹0 — absent is not zero.
* Brokerage is genuinely ₹0: Zerodha charges nothing for delivery. That is a fact about the
  broker, not a gap in the data, and the breakdown says so rather than omitting the line.

THE DP CHARGE IS THE ONE THAT IS EASY TO GET WRONG
---------------------------------------------------
Five of the six components are proportional to turnover, so summing them per order is exact. The
sixth is not: the depository charge is **flat, per scrip, per day, on the sell side only**. Selling
one holding in three tranches on one afternoon incurs it **once**, and charging it per order would
overstate that day threefold.

`costs.order_cost` charges it per call, correctly, because it models one order. Aggregating is
this module's job, so the aggregate subtracts the duplicates: the charge is counted once per
distinct `(instrument, day)` that had any sell. The model's own docstring is emphatic that this is
the charge which makes a small sell disproportionately expensive — ₹16 on a ₹2,000 sale is 0.9 % —
so getting it wrong is not a rounding error.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from baskfy_core.costs import DP_CHARGE_PER_SELL, order_cost

__all__ = ["PortfolioCosts", "TradeFlow", "estimate_portfolio_costs"]

#: The two cash-flow kinds that are trades. Everything else in `portfolio_cash_flow` — dividends,
#: deposits, assignments — moves money without incurring a trading charge.
BUY = "BUY"
SELL = "SELL"

NO_TRADES_REASON = (
    "No buys or sells are recorded for this portfolio in this window, so there are no charges to "
    "estimate. Holdings that arrived by broker sync carry no trade history until a statement is "
    "imported."
)

#: Travels with every figure this module produces. The panel renders it verbatim.
ESTIMATE_CAVEAT = (
    "An estimate of what these trades cost at current statutory rates, not a billed amount. "
    "Returns shown elsewhere are before these charges, not after them."
)


@dataclass(frozen=True, slots=True)
class TradeFlow:
    """One recorded trade. Mirrors the columns of ``portfolio_cash_flow`` that matter here."""

    on: dt.date
    kind: str
    #: Always positive. ``portfolio_cash_flow.amount`` is signed by direction; the caller abs()es
    #: it, because a cost is a function of turnover and turnover has no sign.
    value: Decimal
    instrument_id: int | None = None


@dataclass(frozen=True, slots=True)
class PortfolioCosts:
    """The six components, the total, and what they were computed over."""

    stt: Decimal
    exchange: Decimal
    sebi: Decimal
    stamp: Decimal
    gst: Decimal
    dp: Decimal
    brokerage: Decimal
    total: Decimal
    turnover: Decimal
    trades: int
    #: Distinct (instrument, day) sell combinations — what the DP charge is actually counted on.
    sell_scrip_days: int

    @property
    def bps_of_turnover(self) -> Decimal | None:
        """Charges as basis points of turnover, or ``None`` when nothing was traded."""
        if self.turnover == 0:
            return None
        return (self.total / self.turnover * Decimal(10_000)).quantize(Decimal("0.01"))


def _money(value: float) -> Decimal:
    """Round to paise once, at the boundary — house rule 8, and house rule 9's spirit.

    `costs.order_cost` is float because the statutory rates are float constants and the model was
    calibrated that way. Money leaves this module as `Decimal`, rounded exactly once, so nothing
    downstream inherits a float in a price path.
    """
    return Decimal(str(round(value, 2)))


def estimate_portfolio_costs(flows: Iterable[TradeFlow]) -> PortfolioCosts | None:
    """The charges on a portfolio's recorded trades, or ``None`` when it made none.

    ``None`` rather than a zeroed record: a portfolio that traded nothing and a portfolio whose
    trades were never recorded both produce no figure, and both deserve a sentence rather than a
    ₹0 that reads as "you were charged nothing".
    """
    trades: Sequence[TradeFlow] = [
        flow for flow in flows if flow.kind.upper() in {BUY, SELL} and flow.value != 0
    ]
    if not trades:
        return None

    stt = exchange = sebi = stamp = gst = brokerage = 0.0
    turnover = Decimal(0)
    sell_scrip_days: set[tuple[int | None, dt.date]] = set()

    for trade in trades:
        value = abs(trade.value)
        turnover += value
        side = trade.kind.upper()
        cost = order_cost(float(value), side)
        stt += cost.stt
        exchange += cost.exchange
        sebi += cost.sebi
        stamp += cost.stamp
        gst += cost.gst
        brokerage += cost.brokerage
        if side == SELL:
            # A scrip sold three times in one day is charged once. `order_cost` cannot know that —
            # it sees one order — so the aggregate counts the distinct pairs instead of summing
            # `cost.dp`, which is deliberately not accumulated above.
            sell_scrip_days.add((trade.instrument_id, trade.on))

    dp = _money(DP_CHARGE_PER_SELL * len(sell_scrip_days))
    parts = [
        _money(stt),
        _money(exchange),
        _money(sebi),
        _money(stamp),
        _money(gst),
        dp,
        _money(brokerage),
    ]
    return PortfolioCosts(
        stt=parts[0],
        exchange=parts[1],
        sebi=parts[2],
        stamp=parts[3],
        gst=parts[4],
        dp=parts[5],
        brokerage=parts[6],
        total=sum(parts, Decimal(0)),
        turnover=turnover,
        trades=len(trades),
        sell_scrip_days=len(sell_scrip_days),
    )
