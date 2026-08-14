"""Strategy plugin interface. Every engine — weekly momentum, intraday, options —
implements this and talks to the market ONLY via the OrderGateway it is given."""
from __future__ import annotations
from abc import ABC, abstractmethod
from ..core.gateway import OrderGateway


class BaseStrategy(ABC):
    name: str = "base"
    products: tuple[str, ...] = ("CNC",)      # which products this strategy may use

    def __init__(self, gateway: OrderGateway):
        self.gw = gateway

    # --- lifecycle ---
    async def on_start(self): ...
    async def on_stop(self): ...

    # --- event hooks (intraday engines) ---
    async def on_tick(self, tick: dict): ...
    async def on_order_update(self, update: dict): ...

    # --- batch hook (rebalance engines) ---
    @abstractmethod
    async def generate_targets(self, context: dict) -> list[dict]:
        """Return [{symbol, qty_final, side, price, product, stop}] for the engine."""
