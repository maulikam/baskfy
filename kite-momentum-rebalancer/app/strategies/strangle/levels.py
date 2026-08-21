"""Daily reference levels: swing structure, OI walls, ATM straddle price, priced range.

Two things here are load-bearing and easy to get subtly wrong.

THE PRICED RANGE IS THE FULL STRADDLE, NOT HALF OF IT.
    UPPER = spot + (atm_call_mid + atm_put_mid)
Halving it puts every strike far too close to spot and reintroduces exactly the delta
exposure the strategy exists to avoid. Worked check: spot 17,000 with a 250 straddle gives
16,750-17,250, not 16,875-17,125.

A SWING POINT NEEDS BARS TO ITS RIGHT.
The in-progress candle can never be a confirmed swing: confirmation requires two bars
after it, and those have not happened. Using it is look-ahead bias, it never shows up as
an error, and it silently inflates every backtest.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Bar:
    ts: Any
    open: float
    high: float
    low: float
    close: float
    is_final: bool = True


@dataclass(frozen=True)
class Levels:
    resistance: float
    support: float
    resistance_source: str
    support_source: str
    swings_high: tuple[float, ...] = ()
    swings_low: tuple[float, ...] = ()


@dataclass(frozen=True)
class PricedRange:
    spot: float
    asp: float
    upper: float
    lower: float

    def as_dict(self) -> dict:
        return {"spot": self.spot, "asp": round(self.asp, 2),
                "upper": round(self.upper, 2), "lower": round(self.lower, 2)}


# =====================================================================================
# swing structure
# =====================================================================================
def confirmed_swings(bars: Sequence[Bar], left: int = 2, right: int = 2,
                     use_unconfirmed: bool = False) -> tuple[list[int], list[int]]:
    """Indices of confirmed swing highs and lows.

    A bar at index i is a swing high when it is above the `left` bars before it and at
    least as high as the `right` bars after it. The final `right` bars can therefore never
    qualify, and neither can an unfinished bar.
    """
    usable = [b for b in bars if b.is_final or use_unconfirmed]
    highs: list[int] = []
    lows: list[int] = []
    n = len(usable)
    for i in range(left, n - right):
        h = usable[i].high
        if all(h > usable[i - k].high for k in range(1, left + 1)) and \
           all(h >= usable[i + k].high for k in range(1, right + 1)):
            highs.append(i)
        lo = usable[i].low
        if all(lo < usable[i - k].low for k in range(1, left + 1)) and \
           all(lo <= usable[i + k].low for k in range(1, right + 1)):
            lows.append(i)
    return highs, lows


def _cluster(points: Sequence[tuple[float, float]], tolerance: float
             ) -> list[tuple[float, float, int]]:
    """Group nearby levels. Returns (level, score, touches), best score first."""
    out: list[tuple[float, float, int]] = []
    for price, weight in sorted(points, key=lambda p: p[0]):
        for idx, (lvl, score, touches) in enumerate(out):
            if abs(price - lvl) <= tolerance:
                merged = (lvl * touches + price) / (touches + 1)
                out[idx] = (merged, score + weight, touches + 1)
                break
        else:
            out.append((price, weight, 1))
    return sorted(((lvl, score * touches, touches) for lvl, score, touches in out),
                  key=lambda t: -t[1])


def compute_levels(bars: Sequence[Bar], spot: float, cfg: dict) -> Levels:
    """Top-scoring confirmed swing above and below spot, with a 10-day fallback."""
    lv = cfg["levels"]
    left, right = int(lv["swing_left_bars"]), int(lv["swing_right_bars"])
    lam = float(lv["swing_recency_lambda"])
    tol = float(lv["cluster_tolerance_points"])
    usable = [b for b in bars if b.is_final or lv.get("use_unconfirmed_candle", False)]
    hi_idx, lo_idx = confirmed_swings(bars, left, right,
                                      lv.get("use_unconfirmed_candle", False))
    n = len(usable)

    def weighted(idxs, attr):
        return [(getattr(usable[i], attr), math.exp(-lam * (n - 1 - i))) for i in idxs]

    highs = _cluster(weighted(hi_idx, "high"), tol)
    lows = _cluster(weighted(lo_idx, "low"), tol)

    above = [h for h in highs if h[0] > spot]
    below = [lo for lo in lows if lo[0] < spot]
    if above:
        resistance, r_src = above[0][0], "swing"
    else:
        resistance, r_src = (max((b.high for b in usable), default=spot), "fallback_high")
    if below:
        support, s_src = below[0][0], "swing"
    else:
        support, s_src = (min((b.low for b in usable), default=spot), "fallback_low")

    return Levels(resistance=resistance, support=support,
                  resistance_source=r_src, support_source=s_src,
                  swings_high=tuple(h[0] for h in highs),
                  swings_low=tuple(lo[0] for lo in lows))


# =====================================================================================
# open interest structure
# =====================================================================================
@dataclass(frozen=True)
class OIStructure:
    pomc: float | None
    call_wall: float | None
    put_wall: float | None


def oi_structure(chain: Sequence[Mapping[str, Any]], spot: float) -> OIStructure:
    """Point of maximum combined OI, and the nearest heavy call/put strikes.

    A filter, never truth: rising OI can be fresh longs rather than writing, and this
    cannot tell the difference. It only ever narrows the candidate set.
    """
    by_strike: dict[float, dict[str, float]] = {}
    for row in chain:
        k = float(row["strike"])
        slot = by_strike.setdefault(k, {"CE": 0.0, "PE": 0.0})
        slot[row["kind"]] = slot.get(row["kind"], 0.0) + float(row.get("oi") or 0.0)
    if not by_strike:
        return OIStructure(None, None, None)
    pomc = max(by_strike, key=lambda k: by_strike[k]["CE"] + by_strike[k]["PE"])
    calls_above = {k: v["CE"] for k, v in by_strike.items() if k > spot and v["CE"] > 0}
    puts_below = {k: v["PE"] for k, v in by_strike.items() if k < spot and v["PE"] > 0}
    return OIStructure(
        pomc=pomc,
        call_wall=max(calls_above, key=calls_above.get) if calls_above else None,
        put_wall=max(puts_below, key=puts_below.get) if puts_below else None)


# =====================================================================================
# the priced range
# =====================================================================================
def atm_strike(spot: float, step: int) -> float:
    return round(spot / step) * step


def straddle_price(chain: Sequence[Mapping[str, Any]], spot: float, step: int
                   ) -> float | None:
    """ATM call mid + ATM put mid. None if either side lacks a two-sided quote.

    Mid is correct HERE — this is a valuation of what the market is pricing, not a fill.
    Fills never use mid; see fills_paper.py.
    """
    k = atm_strike(spot, step)
    mids: dict[str, float] = {}
    for row in chain:
        if abs(float(row["strike"]) - k) > 1e-6:
            continue
        bid, ask = float(row.get("bid") or 0.0), float(row.get("ask") or 0.0)
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mids[row["kind"]] = (bid + ask) / 2.0
    if "CE" not in mids or "PE" not in mids:
        return None
    return mids["CE"] + mids["PE"]


def priced_range(spot: float, asp: float, cfg: dict) -> PricedRange:
    """spot +/- the FULL straddle.

    multiplier is [STRUCTURAL] and ships at 1.0. It exists as a named constant so that
    halving the range would have to be a deliberate, visible edit rather than an
    off-by-one-half nobody notices.
    """
    mult = float(cfg["priced_range"]["multiplier"])
    width = asp * mult
    return PricedRange(spot=spot, asp=asp, upper=spot + width, lower=spot - width)
