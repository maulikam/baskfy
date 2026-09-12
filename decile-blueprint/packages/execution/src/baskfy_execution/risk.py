"""Account-level risk manager + kill switch. Engines consult this before every order;
gateway enforces it. Applies across ALL strategies (rebalance / intraday / options)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")


@dataclass
class RiskConfig:
    max_daily_loss: float = 100_000.0  # ₹ realised+unrealised across engines
    max_orders_per_day: int = 500  # our own cap, well under Kite's 3000
    max_position_value: float = 1_500_000.0  # per symbol (accumulated, not per-order)
    max_gross_exposure: float = 12_000_000.0
    intraday_square_off: str = "15:12"  # MIS positions force-closed after this


@dataclass
class RiskState:
    day_pnl: float = 0.0
    orders_today: int = 0
    killed: bool = False
    reasons: list[str] = field(default_factory=list)
    #: Running per-symbol notional after accepted pre_order checks. Buys add; sells subtract
    #: toward zero. The position cap reads this, not the single order's value alone.
    position_value: dict[str, float] = field(default_factory=dict)


def _ist_day() -> str:
    """The trading day's calendar date in IST — not the host's local TZ."""
    return datetime.now(tz=_IST).strftime("%Y-%m-%d")


class RiskManager:
    def __init__(
        self,
        cfg: RiskConfig | None = None,
        *,
        state_path: str | None = None,
    ):
        self.cfg = cfg or RiskConfig()
        self._state_path = state_path
        self.state = RiskState()
        self._day = _ist_day()
        if state_path:
            self._load()

    def _load(self) -> None:
        path = self._state_path
        if not path or not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError(f"risk state at {path} is not an object")
        day = raw.get("day")
        if day != _ist_day():
            # Stale day: keep the file but start a fresh IST day rather than replaying
            # yesterday's kill switch or order count.
            self._day = _ist_day()
            self.state = RiskState()
            self._persist()
            return
        self._day = str(day)
        positions = raw.get("position_value") or {}
        if not isinstance(positions, dict):
            raise ValueError(f"risk state position_value at {path} is not an object")
        self.state = RiskState(
            day_pnl=float(raw.get("day_pnl", 0.0)),
            orders_today=int(raw.get("orders_today", 0)),
            killed=bool(raw.get("killed", False)),
            reasons=[str(r) for r in (raw.get("reasons") or [])],
            position_value={str(k): float(v) for k, v in positions.items()},
        )

    def _persist(self) -> None:
        path = self._state_path
        if not path:
            return
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = {
            "day": self._day,
            "day_pnl": self.state.day_pnl,
            "orders_today": self.state.orders_today,
            "killed": self.state.killed,
            "reasons": list(self.state.reasons),
            "position_value": dict(self.state.position_value),
        }
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)

    def _roll(self) -> None:
        d = _ist_day()
        if d != self._day:
            self._day, self.state = d, RiskState()
            self._persist()

    def kill(self, reason: str) -> None:
        self.state.killed = True
        self.state.reasons.append(reason)
        self._persist()

    def on_pnl(self, pnl: float) -> None:
        self._roll()
        self.state.day_pnl = pnl
        if pnl <= -abs(self.cfg.max_daily_loss):
            self.kill(f"daily loss cap hit ({pnl:,.0f})")
        else:
            self._persist()

    def pre_order(
        self,
        symbol: str,
        value: float,
        gross: float,
        *,
        side: str = "BUY",
    ) -> tuple[bool, str]:
        self._roll()
        s, c = self.state, self.cfg
        if s.killed:
            return False, f"KILL SWITCH: {'; '.join(s.reasons)}"
        if s.orders_today >= c.max_orders_per_day:
            return False, "own daily order cap reached"
        current = float(s.position_value.get(symbol, 0.0))
        side_u = (side or "BUY").upper()
        if side_u == "BUY":
            projected = current + abs(value)
        else:
            # A sell reduces exposure; the cap still refuses a sell larger than any
            # conceivable book only when the order itself exceeds the per-name ceiling.
            projected = abs(value)
        if projected > c.max_position_value:
            return False, f"{symbol} position value {projected:,.0f} > cap"
        if gross > c.max_gross_exposure:
            return False, "gross exposure cap"
        s.orders_today += 1
        if side_u == "BUY":
            s.position_value[symbol] = current + abs(value)
        else:
            s.position_value[symbol] = max(0.0, current - abs(value))
        self._persist()
        return True, "ok"
