"""Strike selection: hard boundaries first, then balance by PREMIUM.

THE BALANCING RULE IS THE POINT OF THE STRATEGY.
Equidistant strikes look neutral and are not. Put premium is structurally fatter because
of fear skew, so a call and a put the same distance from spot carry different credit, and
the position is implicitly long delta. Balancing by premium instead puts the two legs at
different distances — typically the put further out — which is what makes the book
directionally neutral in the only currency that matters, money at risk.

The source's own live example is the regression case: 17,300 CE and 16,700 PE, both at
Rs 27, with spot around 17,050. That is 250 points up and 350 points down. Any selector
that matches distance gets this wrong; see test_selection.py.

Boundaries are absolute. If nothing qualifies, the day is skipped. There is no relaxation
path, because every relaxation is a decision to sell a strike the rules said was too close.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .levels import OIStructure, PricedRange


@dataclass(frozen=True)
class Candidate:
    strike: float
    kind: str            # CE | PE
    bid: float
    ask: float
    oi: float
    symbol: str = ""

    @property
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2.0
        return (self.ask - self.bid) / mid if mid > 0 else 1.0


@dataclass(frozen=True)
class StranglePair:
    call: Candidate
    put: Candidate
    imbalance: float
    call_wing: Candidate | None = None
    put_wing: Candidate | None = None

    @property
    def credit_points(self) -> float:
        """Net credit per unit: shorts sold at bid, wings bought at ask."""
        c = self.call.bid + self.put.bid
        if self.call_wing and self.put_wing:
            c -= (self.call_wing.ask + self.put_wing.ask)
        return c

    def as_dict(self) -> dict:
        d = {"call": self.call.symbol or self.call.strike,
             "put": self.put.symbol or self.put.strike,
             "call_strike": self.call.strike, "put_strike": self.put.strike,
             "call_bid": self.call.bid, "put_bid": self.put.bid,
             "imbalance_pct": round(self.imbalance * 100, 2),
             "credit_points": round(self.credit_points, 2)}
        if self.call_wing and self.put_wing:
            d |= {"call_wing": self.call_wing.strike, "put_wing": self.put_wing.strike}
        return d


class NoQualifyingPair(RuntimeError):
    """No pair cleared the boundaries. The day is skipped; boundaries are never relaxed."""


def to_candidates(chain: Sequence[Mapping[str, Any]]) -> list[Candidate]:
    out = []
    for row in chain:
        out.append(Candidate(strike=float(row["strike"]), kind=str(row["kind"]),
                             bid=float(row.get("bid") or 0.0),
                             ask=float(row.get("ask") or 0.0),
                             oi=float(row.get("oi") or 0.0),
                             symbol=str(row.get("symbol") or "")))
    return out


def qualifies(c: Candidate, *, pr: PricedRange, levels, oi: OIStructure, cfg: dict,
              step: int) -> tuple[bool, str]:
    """Every boundary from section 4.5, with the reason it failed.

    Returned rather than raised so a skipped day can say WHICH gate emptied the book —
    "no pair qualified" is not a diagnosis.
    """
    sel = cfg["selection"]
    gates = cfg["gates"]
    beyond = int(sel["min_strikes_beyond_priced_range"]) * step

    if c.bid < float(sel["min_leg_premium"]):
        return False, f"bid {c.bid} < min_leg_premium {sel['min_leg_premium']}"
    if c.oi < float(gates["min_leg_oi"]):
        return False, f"oi {c.oi:,.0f} < {gates['min_leg_oi']:,.0f}"
    if c.bid <= 0 or c.ask <= 0 or c.ask < c.bid:
        return False, "no two-sided quote"
    if c.spread_pct > float(gates["max_bid_ask_spread_pct"]):
        return False, f"spread {c.spread_pct:.1%} > {gates['max_bid_ask_spread_pct']:.1%}"

    if c.kind == "CE":
        if c.strike < pr.upper + beyond:
            return False, f"strike {c.strike} inside priced range upper {pr.upper:.0f}"
        if sel.get("must_be_beyond_swing_level", True) and c.strike <= levels.resistance:
            return False, f"strike {c.strike} not beyond resistance {levels.resistance:.0f}"
        if sel.get("must_be_beyond_oi_wall", True) and oi.call_wall is not None \
           and c.strike <= oi.call_wall:
            return False, f"strike {c.strike} not beyond call wall {oi.call_wall:.0f}"
    else:
        if c.strike > pr.lower - beyond:
            return False, f"strike {c.strike} inside priced range lower {pr.lower:.0f}"
        if sel.get("must_be_beyond_swing_level", True) and c.strike >= levels.support:
            return False, f"strike {c.strike} not beyond support {levels.support:.0f}"
        if sel.get("must_be_beyond_oi_wall", True) and oi.put_wall is not None \
           and c.strike >= oi.put_wall:
            return False, f"strike {c.strike} not beyond put wall {oi.put_wall:.0f}"
    return True, ""


def imbalance(call: Candidate, put: Candidate) -> float:
    """|call_bid - put_bid| / max(bid). Zero is a perfectly balanced pair."""
    hi = max(call.bid, put.bid)
    return abs(call.bid - put.bid) / hi if hi > 0 else 1.0


def select_pair(chain: Sequence[Mapping[str, Any]], *, pr: PricedRange, levels,
                oi: OIStructure, cfg: dict, step: int) -> StranglePair:
    """The qualifying pair with the smallest premium imbalance.

    Distance never enters the comparison. Two legs at Rs 27 each are balanced whether they
    sit 250 and 350 points away or 300 and 300.
    """
    cands = to_candidates(chain)
    calls, puts, rejects = [], [], []
    for c in cands:
        ok, why = qualifies(c, pr=pr, levels=levels, oi=oi, cfg=cfg, step=step)
        if ok:
            (calls if c.kind == "CE" else puts).append(c)
        else:
            rejects.append((c.kind, c.strike, why))

    if not calls or not puts:
        raise NoQualifyingPair(
            f"no qualifying {'call' if not calls else 'put'} leg; "
            f"{len(rejects)} candidates rejected, e.g. {rejects[:3]}")

    tol = float(cfg["selection"]["premium_balance_tolerance_pct"])
    best: StranglePair | None = None
    for c in calls:
        for p in puts:
            imb = imbalance(c, p)
            if imb > tol:
                continue
            if best is None or imb < best.imbalance:
                best = StranglePair(call=c, put=p, imbalance=imb)
    if best is None:
        closest = min((imbalance(c, p) for c in calls for p in puts), default=1.0)
        raise NoQualifyingPair(
            f"no pair within {tol:.0%} premium balance (closest {closest:.1%}); "
            "skipping the day rather than selling an unbalanced pair")
    return best


# =====================================================================================
# wings
# =====================================================================================
def attach_wings(pair: StranglePair, chain: Sequence[Mapping[str, Any]], cfg: dict
                 ) -> StranglePair:
    """Buy the protective wings at a fixed width from each short.

    Width is invariant for the life of the position: a wing that fails to move with its
    short turns a 500-wide spread into a 600-wide one, which raises max loss, breaches the
    catastrophic cap and silently invalidates the margin figure the size was computed from.
    Enforced here at entry and again on every roll in V2.
    """
    if cfg["structure"]["type"].upper() != "HEDGED":
        return pair

    by_key = {(float(r["strike"]), str(r["kind"])): r for r in chain}
    widths = [int(cfg["structure"]["wing_width_points"]),
              int(cfg["structure"]["wing_widen_to"])]
    max_cost = float(cfg["structure"]["max_wing_cost_pct"])
    credit = pair.call.bid + pair.put.bid

    for width in widths:
        cw = by_key.get((pair.call.strike + width, "CE"))
        pw = by_key.get((pair.put.strike - width, "PE"))
        if not cw or not pw:
            continue
        cwc = Candidate(float(cw["strike"]), "CE", float(cw.get("bid") or 0.0),
                        float(cw.get("ask") or 0.0), float(cw.get("oi") or 0.0),
                        str(cw.get("symbol") or ""))
        pwc = Candidate(float(pw["strike"]), "PE", float(pw.get("bid") or 0.0),
                        float(pw.get("ask") or 0.0), float(pw.get("oi") or 0.0),
                        str(pw.get("symbol") or ""))
        if cwc.ask <= 0 or pwc.ask <= 0:
            continue
        cost = cwc.ask + pwc.ask
        if credit > 0 and cost / credit <= max_cost:
            return StranglePair(call=pair.call, put=pair.put, imbalance=pair.imbalance,
                                call_wing=cwc, put_wing=pwc)

    raise NoQualifyingPair(
        f"wings cost more than {max_cost:.0%} of the Rs {credit:.2f} credit at every "
        f"configured width {widths}; skipping the day")
