"""Zerodha cost model for NSE equity DELIVERY (CNC).

Built after the live session of 18 Aug 2026: Rs 73,49,552 of turnover across 163 fills in
21 scrip-sides. Recomputed against those captured fills on 19 Aug, every component below
reproduces exactly.

    STT            7,349.55   85.6%  PROPORTIONAL, 0.1% on BOTH sides
    stamp duty       840.73    9.8%  proportional, buy side only
    exchange txn     218.28    2.5%  proportional
    DP charge        127.44    1.5%  FIXED, ~Rs 15.93 per scrip per day, SELL only
    SEBI + GST        47.95    0.6%  proportional
    brokerage          0.00      -   Zerodha charges nothing for delivery
    ------------------------------
    TOTAL          8,583.95         = 11.68 bps of turnover

THE Rs 7,695.87 FIGURE THAT NAMED THIS FILE IS A SUBSET, not the total, and this docstring
asserted otherwise until 19 Aug. It is STT + exchange + DP = Rs 7,695.27 — the charges
Zerodha bills directly — and it excludes stamp duty, which the state collects, and the
SEBI/GST line. Pairing that subtotal with the full component list made STT look like 95%
of the bill when it is 85.6%, and the session look like 10.5 bps when it is 11.68.

The model itself was never wrong: order_cost() has always charged all six components, so
the no-trade band and MAX_TRADE_COST_PCT were calibrated against the right arithmetic. Only
the anchor quoted here was.

NO MINIMUM TRADE SIZE AVOIDS STT. It scales with turnover, so the only way to pay less of
it is to trade less. The one genuinely fixed cost is the DP charge, and it is the reason a
small SELL is disproportionately expensive: Rs 16 on a Rs 2,000 sale is 0.9%, and on a
Rs 50,000 sale it is 0.03%.

Rates as of 18 August 2026. Each is a named constant so a change is one edit and shows up
in the diff rather than being buried in an expression.
"""
from __future__ import annotations

from dataclasses import dataclass

STT_DELIVERY = 0.001          # 0.1% of turnover, BUY and SELL
EXCHANGE_TXN_NSE = 0.0000297  # 0.00297% of turnover
SEBI_TURNOVER = 0.000001      # Rs 10 per crore
STAMP_DUTY_BUY = 0.00015      # 0.015% of buy turnover
GST = 0.18                    # on brokerage + exchange + SEBI
BROKERAGE_DELIVERY = 0.0      # Zerodha: delivery is free
DP_CHARGE_PER_SELL = 15.93    # per scrip per day, sell side only, flat


@dataclass(frozen=True)
class Cost:
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    dp: float
    brokerage: float = 0.0

    @property
    def total(self) -> float:
        return (self.stt + self.exchange + self.sebi + self.stamp + self.gst
                + self.dp + self.brokerage)

    def as_dict(self) -> dict:
        return {"stt": round(self.stt, 2), "exchange": round(self.exchange, 2),
                "sebi": round(self.sebi, 2), "stamp": round(self.stamp, 2),
                "gst": round(self.gst, 2), "dp": round(self.dp, 2),
                "brokerage": round(self.brokerage, 2), "total": round(self.total, 2)}


def order_cost(value: float, side: str) -> Cost:
    """Cost of one delivery order. `value` is quantity x price, always positive."""
    v = abs(float(value))
    is_sell = side.upper() == "SELL"
    exchange = v * EXCHANGE_TXN_NSE
    sebi = v * SEBI_TURNOVER
    brokerage = v * BROKERAGE_DELIVERY
    return Cost(stt=v * STT_DELIVERY,
                exchange=exchange, sebi=sebi,
                stamp=0.0 if is_sell else v * STAMP_DUTY_BUY,
                gst=GST * (brokerage + exchange + sebi),
                dp=DP_CHARGE_PER_SELL if is_sell else 0.0,
                brokerage=brokerage)


def cost_pct(value: float, side: str) -> float:
    """Cost as a percentage of the trade. The number the no-trade band judges.

    It is not flat: the DP charge means a small sell is proportionally far more expensive
    than a large one, which is exactly the asymmetry a single rupee threshold misses.
    """
    v = abs(float(value))
    return (order_cost(v, side).total / v * 100.0) if v else 100.0


def plan_cost(orders) -> dict:
    """Total cost of a set of planned orders, itemised.

    Shown before confirming, because "this rebalance will cost Rs X" is a question the
    operator should not have to answer from a contract note the next morning.
    """
    total = Cost(0, 0, 0, 0, 0, 0, 0)
    buys = sells = 0.0
    for o in orders:
        d = int(o.get("delta") or 0)
        if d == 0:
            continue
        v = abs(d) * float(o.get("ref_price") or 0.0)
        side = "BUY" if d > 0 else "SELL"
        c = order_cost(v, side)
        total = Cost(total.stt + c.stt, total.exchange + c.exchange, total.sebi + c.sebi,
                     total.stamp + c.stamp, total.gst + c.gst, total.dp + c.dp,
                     total.brokerage + c.brokerage)
        if d > 0:
            buys += v
        else:
            sells += v
    turnover = buys + sells
    return {**total.as_dict(), "turnover": round(turnover),
            "bps_of_turnover": round(total.total / turnover * 10000, 1) if turnover else 0.0}
