"""The live strategy: weekly momentum rebalance (wraps scoring.py + rebalance.py)."""
from __future__ import annotations
from .base import BaseStrategy
from ..scoring import score, load_scan
from ..rebalance import build_plan


class MomentumWeekly(BaseStrategy):
    name = "momentum_weekly"
    products = ("CNC",)

    async def generate_targets(self, context: dict) -> list[dict]:
        scored = score(load_scan(context["scan_path"]))
        plan = build_plan(scored, context["holdings"], context["cash"])
        context["plan"] = plan
        return [dict(symbol=o["symbol"], side="SELL" if o["delta"] < 0 else "BUY",
                     qty=abs(o["delta"]), price=o["ref_price"], product="CNC",
                     stop=o.get("stop")) for o in plan["orders"] if o["delta"] != 0]
