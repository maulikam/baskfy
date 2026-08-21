"""Itemised round-trip cost model for NIFTY options and futures on Zerodha.

WHY THIS IS A MODULE AND NOT A CONSTANT
Every conclusion about whether an option strategy has positive expectancy is a difference
between two numbers of similar size: gross edge and cost. A cost figure carried as a
single blended percentage cannot be audited, cannot be updated when one statutory rate
changes, and hides which component dominates — which for a 4-leg condor at small size is
the FLAT per-order brokerage, not any percentage.

Rates below are as of 16 August 2026. Each carries its source and effective date so a
stale rate is visible rather than silently wrong:

  Brokerage        OPTIONS: flat Rs 20 per executed order, with no percentage cap. The
                   "Rs 20 or 0.03%, whichever is lower" rule applies to FUTURES (and to
                   equity intraday), not to options. Applying the cap to options collapses
                   brokerage to under a rupee a leg and understates a small condor's cost
                   by an order of magnitude — and flat brokerage is precisely what makes a
                   4-leg structure uneconomic at low size, so the error would erase the
                   finding. Zerodha applies Rs 40 in specified cash-shortfall / debit
                   conditions, which is why it is a parameter and not a constant.
                   https://zerodha.com/charges/
  STT, option sell 0.15% of PREMIUM on the sell side only (was 0.10%), from 1 Apr 2026.
  STT, exercised   0.15% of INTRINSIC value on ITM exercise at expiry, from 1 Apr 2026.
  STT, futures     0.05% of NOTIONAL on the sell side only (was 0.02%), from 1 Apr 2026.
                   https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax
  NSE transaction  0.03553% of option premium turnover; 0.00173% of futures notional.
  SEBI turnover    Rs 10 per crore of turnover (0.0001%).
  Stamp duty       0.003% of BUY-side option premium; 0.002% buy-side futures notional.
  IPFT             Rs 0.50 per lakh of option premium (0.0005%), NSE investor protection
                   fund. Small, but included because "negligible" is a judgement the
                   caller should make from a number rather than inherit from the model.
  GST              18% on (brokerage + transaction + SEBI + IPFT).

SPREAD COST IS MEASURED, NOT ASSUMED
The crossing cost is computed from the ACTUAL quoted bid and ask at the moment of the
fill, never from an assumed number of ticks. A fixed two-tick assumption is wrong in both
directions: it flatters a liquid ATM leg and badly understates a 0.05-delta wing, where
one tick can be 9% of the premium. If a caller has no quote it must say so explicitly and
receive a cost of None rather than a plausible-looking fabrication.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

RATES_AS_OF = dt.date(2026, 8, 16)


@dataclass(frozen=True)
class CostRates:
    """Statutory and broker rates. Overridable so a historical period can be re-costed."""

    brokerage_per_order: float = 20.0          # options: flat, no cap
    brokerage_pct_cap: float = 0.0003          # FUTURES ONLY: 0.03%, whichever is lower
    stt_option_sell: float = 0.0015            # of premium, sell side
    stt_option_exercise: float = 0.0015        # of intrinsic, ITM exercise
    stt_future_sell: float = 0.0005            # of notional, sell side
    txn_option: float = 0.0003553              # of premium turnover
    txn_future: float = 0.0000173              # of notional turnover
    sebi_turnover: float = 0.000001            # Rs 10 per crore
    stamp_option_buy: float = 0.00003          # of premium, buy side
    stamp_future_buy: float = 0.00002          # of notional, buy side
    ipft_option: float = 0.000005              # Rs 0.50 per lakh of premium
    gst: float = 0.18
    as_of: dt.date = RATES_AS_OF


@dataclass(frozen=True)
class Fill:
    """One executed leg, priced at what was actually payable.

    `price` is the fill. `bid`/`ask` are the quote at that instant and are used only to
    measure the crossing cost; they never change the statutory charge, which is levied on
    the traded price.
    """

    side: str                      # BUY | SELL
    price: float
    quantity: int                  # contracts, i.e. lots * lot_size
    bid: float | None = None
    ask: float | None = None
    label: str = ""

    @property
    def turnover(self) -> float:
        return self.price * self.quantity

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread_cost(self) -> float | None:
        """Half-spread paid, signed so that crossing always costs.

        Measured against the mid at the instant of the fill. A fill better than mid
        returns a NEGATIVE cost — price improvement is real and should not be discarded,
        because a model that only ever charges is as wrong as one that never does.
        """
        m = self.mid
        if m is None:
            return None
        edge = (self.price - m) if self.side.upper() == "BUY" else (m - self.price)
        return edge * self.quantity


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: float
    stt: float
    transaction: float
    sebi: float
    stamp: float
    ipft: float
    gst: float
    statutory_total: float
    spread_cost: float | None
    spread_measured_legs: int
    total_legs: int
    total: float | None
    per_unit: float | None
    detail: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def spread_complete(self) -> bool:
        """True when every leg had a quote. A partial measurement is not a total."""
        return self.spread_measured_legs == self.total_legs

    def as_dict(self) -> dict:
        return {
            "brokerage": round(self.brokerage, 2), "stt": round(self.stt, 2),
            "transaction": round(self.transaction, 2), "sebi": round(self.sebi, 2),
            "stamp": round(self.stamp, 2), "ipft": round(self.ipft, 2),
            "gst": round(self.gst, 2),
            "statutory_total": round(self.statutory_total, 2),
            "spread_cost": None if self.spread_cost is None else round(self.spread_cost, 2),
            "spread_measured_legs": self.spread_measured_legs,
            "total_legs": self.total_legs,
            "spread_complete": self.spread_complete,
            "total": None if self.total is None else round(self.total, 2),
            "per_unit": None if self.per_unit is None else round(self.per_unit, 4),
            "detail": list(self.detail),
        }


def option_costs(fills: Sequence[Fill], *, rates: CostRates | None = None,
                 lot_size: int = 65) -> CostBreakdown:
    """Full cost of a set of option fills.

    Charges are computed per leg so the breakdown can be audited against a contract note.
    """
    r = rates or CostRates()
    brokerage = stt = txn = sebi = stamp = ipft = 0.0
    spread = 0.0
    measured = 0
    detail = []

    for f in fills:
        turnover = f.turnover
        b = r.brokerage_per_order          # flat for options; no percentage cap
        leg_stt = turnover * r.stt_option_sell if f.side.upper() == "SELL" else 0.0
        leg_txn = turnover * r.txn_option
        leg_sebi = turnover * r.sebi_turnover
        leg_stamp = turnover * r.stamp_option_buy if f.side.upper() == "BUY" else 0.0
        leg_ipft = turnover * r.ipft_option

        brokerage += b
        stt += leg_stt
        txn += leg_txn
        sebi += leg_sebi
        stamp += leg_stamp
        ipft += leg_ipft

        sc = f.spread_cost
        if sc is not None:
            spread += sc
            measured += 1

        detail.append({
            "label": f.label, "side": f.side.upper(), "price": f.price,
            "quantity": f.quantity, "turnover": round(turnover, 2),
            "brokerage": round(b, 2), "stt": round(leg_stt, 2),
            "spread_cost": None if sc is None else round(sc, 2),
            "quoted": None if f.mid is None else {"bid": f.bid, "ask": f.ask,
                                                  "mid": round(f.mid, 4)},
        })

    gst = r.gst * (brokerage + txn + sebi + ipft)
    statutory = brokerage + stt + txn + sebi + stamp + ipft + gst
    total = None if measured < len(fills) else statutory + spread
    units = lot_size * max(1, _lots_from(fills, lot_size))
    return CostBreakdown(
        brokerage=brokerage, stt=stt, transaction=txn, sebi=sebi, stamp=stamp,
        ipft=ipft, gst=gst, statutory_total=statutory,
        spread_cost=spread if measured else None,
        spread_measured_legs=measured, total_legs=len(fills),
        total=total,
        per_unit=None if total is None else total / units,
        detail=tuple(detail))


def _lots_from(fills: Sequence[Fill], lot_size: int) -> int:
    if not fills:
        return 1
    return max(1, round(max(f.quantity for f in fills) / max(lot_size, 1)))


def exercise_stt(intrinsic_points: float, quantity: int,
                 rates: CostRates | None = None) -> float:
    """STT on an ITM option left to expire.

    Levied on INTRINSIC value, not notional — the distinction that used to make letting an
    option expire ITM catastrophically expensive. Since 1 Apr 2026 the rate matches the
    ordinary sale rate, so expiring ITM and squaring off now cost the same.
    """
    r = rates or CostRates()
    return max(0.0, intrinsic_points) * quantity * r.stt_option_exercise


def future_costs(fills: Sequence[Fill], *, rates: CostRates | None = None,
                 lot_size: int = 65) -> CostBreakdown:
    """The same stack for NIFTY futures, so an option trade can be compared against
    expressing the identical view in the underlying."""
    r = rates or CostRates()
    brokerage = stt = txn = sebi = stamp = 0.0
    spread = 0.0
    measured = 0
    detail = []
    for f in fills:
        notional = f.turnover
        b = min(r.brokerage_per_order, notional * r.brokerage_pct_cap)
        leg_stt = notional * r.stt_future_sell if f.side.upper() == "SELL" else 0.0
        brokerage += b
        stt += leg_stt
        txn += notional * r.txn_future
        sebi += notional * r.sebi_turnover
        stamp += notional * r.stamp_future_buy if f.side.upper() == "BUY" else 0.0
        sc = f.spread_cost
        if sc is not None:
            spread += sc
            measured += 1
        detail.append({"label": f.label, "side": f.side.upper(), "price": f.price,
                       "notional": round(notional, 2), "stt": round(leg_stt, 2)})
    gst = r.gst * (brokerage + txn + sebi)
    statutory = brokerage + stt + txn + sebi + stamp + gst
    total = None if measured < len(fills) else statutory + spread
    return CostBreakdown(
        brokerage=brokerage, stt=stt, transaction=txn, sebi=sebi, stamp=stamp,
        ipft=0.0, gst=gst, statutory_total=statutory,
        spread_cost=spread if measured else None,
        spread_measured_legs=measured, total_legs=len(fills),
        total=total, per_unit=None if total is None else total / (lot_size),
        detail=tuple(detail))
