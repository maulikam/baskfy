"""Account-level risk manager + kill switch. Engines consult this before every order;
gateway enforces it. Applies across ALL strategies (rebalance / intraday / options)."""
from __future__ import annotations
import time
from dataclasses import dataclass, field


@dataclass
class RiskConfig:
    max_daily_loss: float = 100_000.0        # ₹ realised+unrealised across engines
    max_orders_per_day: int = 500            # our own cap, well under Kite's 3000
    max_position_value: float = 1_500_000.0  # per symbol
    max_gross_exposure: float = 12_000_000.0
    intraday_square_off: str = "15:12"       # MIS positions force-closed after this


@dataclass
class RiskState:
    day_pnl: float = 0.0
    orders_today: int = 0
    killed: bool = False
    reasons: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self, cfg: RiskConfig | None = None):
        self.cfg, self.state = cfg or RiskConfig(), RiskState()
        self._day = time.strftime("%Y-%m-%d")

    def _roll(self):
        d = time.strftime("%Y-%m-%d")
        if d != self._day:
            self._day, self.state = d, RiskState()

    def kill(self, reason: str):
        self.state.killed = True
        self.state.reasons.append(reason)

    def on_pnl(self, pnl: float):
        self._roll()
        self.state.day_pnl = pnl
        if pnl <= -abs(self.cfg.max_daily_loss):
            self.kill(f"daily loss cap hit ({pnl:,.0f})")

    def pre_order(self, symbol: str, value: float, gross: float) -> tuple[bool, str]:
        self._roll()
        s, c = self.state, self.cfg
        if s.killed:
            return False, f"KILL SWITCH: {'; '.join(s.reasons)}"
        if s.orders_today >= c.max_orders_per_day:
            return False, "own daily order cap reached"
        if value > c.max_position_value:
            return False, f"{symbol} position value {value:,.0f} > cap"
        if gross > c.max_gross_exposure:
            return False, "gross exposure cap"
        s.orders_today += 1
        return True, "ok"
