"""READY-BUT-DISABLED intraday engine skeleton (config.INTRADAY_ENABLED gates orders).
Realistic latency with Kite: tick→decision <1ms in-process, order round-trip 100-300ms,
caps 10 orders/s / 400/min / ~3000/day, ticks ~1/sec/instrument. This is fast intraday algo, NOT exchange-colo HFT.

To go live later: implement signal() below, backtest on recorded ticks
(bus.last_tick journal), paper-trade with DRY_RUN=true, then enable in config.
Square-off discipline: RiskConfig.intraday_square_off is enforced by the engine loop.
"""
from __future__ import annotations
from .base import BaseStrategy


class IntradayMomentumBurst(BaseStrategy):
    name = "intraday_burst"
    products = ("MIS",)

    def __init__(self, gateway, tokens: list[int] | None = None):
        super().__init__(gateway)
        self.tokens = tokens or []
        self.position: dict[int, int] = {}

    def signal(self, tick: dict) -> int:
        """Return +qty / -qty / 0. TODO: your alpha here (e.g. VWAP deviation,
        opening-range breakout, order-flow imbalance from depth in MODE_FULL)."""
        return 0

    async def on_tick(self, tick: dict):
        qty = self.signal(tick)
        if qty:
            await self.gw.place(symbol=tick["tradingsymbol"], qty=abs(qty),
                                side="BUY" if qty > 0 else "SELL",
                                product="MIS", order_type="LIMIT",
                                price=tick["last_price"])

    async def generate_targets(self, context: dict) -> list[dict]:
        return []  # event-driven engine; batch hook unused
