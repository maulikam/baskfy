"""Position sizing: usable margin, queried utilisation, and the catastrophic-loss cap.

MARGIN IS QUERIED, NEVER ESTIMATED.
The brief shipped an estimate of Rs 32,500 per hedged lot. The live basket API returned
Rs 92,497 — 2.85x higher, and stable at Rs 83k-96k across three expiries and three strike
distances. The reason is visible in the breakdown: SPAN nets down 76% against the wings,
but EXPOSURE margin receives no hedge benefit at all and is about Rs 63k/lot on its own.

That reverses the brief's conclusion. This strategy is MARGIN-bound, not risk-bound: at
20 lots the catastrophic cap is satisfied with room to spare while utilisation is the
constraint that actually binds. Any sizing that trusts a per-lot constant will silently
over-leverage by a factor of three.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class SizingRefused(RuntimeError):
    """A cap was breached. Entry is refused; caps are not negotiated down at runtime."""


@dataclass(frozen=True)
class Capital:
    total: float
    cash: float
    collateral: float

    @property
    def usable(self) -> float:
        """Gross availability, capped by Zerodha's 50%-cash rule.

        Both terms are live: shifting the split to 20L cash / 30L pledged makes 2*cash the
        binding term instead of gross availability, and a constant would not notice.
        """
        return min(self.cash + self.collateral, 2.0 * self.cash)


def capital_from_config(cfg: dict) -> Capital:
    c = cfg["capital"]
    collateral = float(c["pledged_market_value"]) * (1.0 - float(c["pledge_haircut_pct"]))
    return Capital(total=float(c["total"]), cash=float(c["cash"]), collateral=collateral)


@dataclass(frozen=True)
class SizingDecision:
    lots: int
    units: int
    margin_required: float
    margin_per_lot: float
    utilisation_pct: float
    catastrophic_loss: float
    catastrophic_pct: float
    max_lots_by_margin: int
    max_lots_by_tail: int
    binding_constraint: str

    def as_dict(self) -> dict:
        return {"lots": self.lots, "units": self.units,
                "margin_required": round(self.margin_required),
                "margin_per_lot": round(self.margin_per_lot),
                "utilisation_pct": round(self.utilisation_pct * 100, 1),
                "catastrophic_loss": round(self.catastrophic_loss),
                "catastrophic_pct": round(self.catastrophic_pct * 100, 1),
                "max_lots_by_margin": self.max_lots_by_margin,
                "max_lots_by_tail": self.max_lots_by_tail,
                "binding_constraint": self.binding_constraint}


def session_lots(cfg: dict, size_mult: float, *, loss_streak: int = 0,
                 win_streak: int = 0) -> int:
    """Configured lots scaled by the session multiplier and any streak ladder.

    Never scales above the configured number: max_lots_multiplier is [STRUCTURAL] at 1.00
    so a winning streak can restore size that a losing streak removed, and nothing more.
    """
    s = cfg["sizing"]
    lots = float(s["lots"])
    if s.get("scale_by_session_multiplier", True):
        lots *= float(size_mult)
    if loss_streak > 0:
        lots *= float(s["loss_streak_multiplier"]) ** loss_streak
    if win_streak >= 3:
        lots *= float(s["win_streak_multiplier"])
    ceiling = float(s["lots"]) * float(s.get("max_lots_multiplier", 1.0))
    return max(int(s["min_lots"]), min(int(lots), int(ceiling)))


def max_lots_by_tail(cfg: dict, wing_width: float | None = None) -> int:
    """wing_width * units <= max_catastrophic_loss_pct * capital.total.

    Denominator is total capital, not usable margin — stated explicitly because the two
    differ by 5% here and picking the wrong one changes the cap by a lot at the margin.
    """
    s = cfg["sizing"]
    if cfg["structure"]["type"].upper() != "HEDGED":
        return 10 ** 6                      # naked has no wing; the tail is unbounded
    width = float(wing_width or cfg["structure"]["wing_width_points"])
    lot = int(cfg["instrument"]["lot_size"])
    budget = float(s["max_catastrophic_loss_pct"]) * float(cfg["capital"]["total"])
    return int(budget / (width * lot))


def evaluate(cfg: dict, *, lots: int, margin_required: float,
             wing_width: float | None = None) -> SizingDecision:
    """Check a proposed size against both caps. Raises rather than trimming silently."""
    cap = capital_from_config(cfg)
    lot = int(cfg["instrument"]["lot_size"])
    units = lots * lot
    width = float(wing_width or cfg["structure"]["wing_width_points"])
    hedged = cfg["structure"]["type"].upper() == "HEDGED"

    max_util = float(cfg["margin"]["max_utilisation_pct"])
    util = margin_required / cap.usable if cap.usable else 1.0
    per_lot = margin_required / lots if lots else 0.0
    by_margin = int(cap.usable * max_util / per_lot) if per_lot else 0
    by_tail = max_lots_by_tail(cfg, width)

    tail = width * units if hedged else float("inf")
    tail_pct = tail / float(cfg["capital"]["total"]) if hedged else float("inf")
    binding = "margin" if by_margin <= by_tail else "tail"

    decision = SizingDecision(
        lots=lots, units=units, margin_required=margin_required, margin_per_lot=per_lot,
        utilisation_pct=util, catastrophic_loss=tail, catastrophic_pct=tail_pct,
        max_lots_by_margin=by_margin, max_lots_by_tail=by_tail,
        binding_constraint=binding)

    if util > max_util:
        raise SizingRefused(
            f"margin {margin_required:,.0f} is {util:.1%} of usable {cap.usable:,.0f}, "
            f"over the {max_util:.0%} cap. Max {by_margin} lots at the queried "
            f"Rs {per_lot:,.0f}/lot — not the estimate.")
    if hedged and tail_pct > float(cfg["sizing"]["max_catastrophic_loss_pct"]):
        raise SizingRefused(
            f"a gap through the wings loses {tail:,.0f} = {tail_pct:.1%} of capital, over "
            f"the {float(cfg['sizing']['max_catastrophic_loss_pct']):.0%} cap. "
            f"Max {by_tail} lots at {width:.0f}-point wings.")
    return decision


def query_margin(kc, basket: Sequence[Mapping[str, Any]]) -> float:
    """Post-hedge-benefit margin for a basket, from the broker.

    Reads `final`, not `initial`. `initial` is the sum of the legs' standalone margins and
    ignores the hedge entirely — reading it makes a condor look identical to a naked
    strangle, which is how a 2x capital efficiency gets mistaken for none at all.
    """
    resp = kc.basket_order_margins(list(basket), consider_positions=False)
    final = (resp or {}).get("final") or {}
    total = final.get("total")
    if total is None:
        raise SizingRefused(
            "basket_order_margins returned no final.total; refusing to fall back to the "
            "per-lot estimate, which was wrong by 2.85x the last time it was checked")
    return float(total)
