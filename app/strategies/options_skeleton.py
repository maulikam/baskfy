"""READY-BUT-DISABLED options engine skeleton (config.OPTIONS_ENABLED gates orders).
Foundation included now so strategies drop in later:
- instrument master → option chain resolution (weekly/monthly expiries)
- basket margin check via kite.basket_order_margins before entry
- strategy stubs: short strangle / iron condor / directional buys
NIFTY lot size and strike steps change; always resolve from the live instrument dump.
"""
from __future__ import annotations
import datetime as dt
from .base import BaseStrategy


class OptionChain:
    def __init__(self, kc, underlying: str = "NIFTY"):
        self.kc, self.underlying = kc, underlying
        self._instruments: list[dict] | None = None

    def load(self):
        self._instruments = [i for i in self.kc.instruments("NFO")
                             if i["name"] == self.underlying]

    def nearest_expiry(self) -> dt.date:
        return min(i["expiry"] for i in self._instruments)

    def strike(self, expiry, strike: float, kind: str) -> dict | None:
        for i in self._instruments:
            if i["expiry"] == expiry and i["strike"] == strike \
               and i["instrument_type"] == kind:
                return i
        return None


class ShortStrangle(BaseStrategy):
    name = "short_strangle"
    products = ("NRML",)

    async def generate_targets(self, context: dict) -> list[dict]:
        """TODO: pick delta-based strikes (needs greeks — add py_vollib), check
        basket margin, size by RiskConfig, attach stop-loss GTTs on both legs."""
        return []
