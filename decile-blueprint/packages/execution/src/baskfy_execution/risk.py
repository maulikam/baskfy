"""Account-level risk manager + kill switch. Engines consult this before every order;
gateway enforces it. Applies across ALL strategies (rebalance / intraday / options).

ONE ACCOUNT, MANY PROCESSES (LV3, 28 Sep 2026)
----------------------------------------------
The desk, the swing monitor, ``twt-auto`` and the session supervisor each build their own
``RiskManager``. With the state in a JSON file per process, or in memory, each of them had its own
day-loss cap, its own order counter and its own idea of what the account holds — the review's
P0.2: "simultaneous Swing and TWT entries can spend the same cash". A :class:`RiskStateStore` is
the fix: one durable row per IST day that every decision **reloads under a lock** before it is
made and writes back after. The desk wires a Postgres row (``risk_ledger``, ``SELECT … FOR
UPDATE``); the JSON file path this class always had keeps working for a single process; and with
neither the state is in memory, as before.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
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


class RiskStateStore(Protocol):
    """Durable, shared risk state — the same JSON payload the file path writes, held by a row.

    ``lock()`` is a context manager that holds the row exclusively for the caller; ``load`` and
    ``save`` run inside it. Every decision the manager makes is: lock → load → decide → save →
    unlock, so two processes cannot both read the same headroom and both spend it.
    """

    def lock(self) -> contextlib.AbstractContextManager[None]: ...

    def load(self) -> dict[str, object] | None: ...

    def save(self, payload: dict[str, object]) -> None: ...


def _state_from(raw: dict[str, object]) -> tuple[str, RiskState]:
    """The payload's day and state, tolerant of a payload written by an older writer."""
    day = str(raw.get("day") or _ist_day())
    raw_pv = raw.get("position_value")
    position_value = (
        {str(k): float(v) for k, v in raw_pv.items()} if isinstance(raw_pv, dict) else {}
    )
    raw_reasons = raw.get("reasons")
    reasons = [str(r) for r in raw_reasons] if isinstance(raw_reasons, list) else []
    raw_pnl = raw.get("day_pnl")
    raw_orders = raw.get("orders_today")
    state = RiskState(
        day_pnl=float(raw_pnl) if isinstance(raw_pnl, (int, float)) else 0.0,
        orders_today=int(raw_orders) if isinstance(raw_orders, (int, float)) else 0,
        killed=bool(raw.get("killed", False)),
        reasons=reasons,
        position_value=position_value,
    )
    return day, state


class RiskManager:
    def __init__(
        self,
        cfg: RiskConfig | None = None,
        *,
        state_path: str | None = None,
        store: RiskStateStore | None = None,
    ):
        self.cfg = cfg or RiskConfig()
        self._state_path = state_path
        self._store = store
        self.state = RiskState()
        self._day = _ist_day()
        if store is not None:
            with store.lock():
                self._reload()
        elif state_path:
            self._load()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the shared row (or nothing, without a store) and reload before deciding."""
        if self._store is None:
            yield
            return
        with self._store.lock():
            self._reload()
            yield

    def _reload(self) -> None:
        """Take the shared row's word for the state. Called inside :meth:`_locked` only."""
        assert self._store is not None
        raw = self._store.load()
        if raw is None:
            return
        self._day, self.state = _state_from(raw)

    def refresh(self) -> None:
        """Re-read the shared state without deciding anything — for readers such as the kill
        switch check, which must see a ``kill()`` from another process."""
        if self._store is None:
            return
        with self._store.lock():
            self._reload()
        self._roll()

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

    def _payload(self) -> dict[str, object]:
        return {
            "day": self._day,
            "day_pnl": self.state.day_pnl,
            "orders_today": self.state.orders_today,
            "killed": self.state.killed,
            "reasons": list(self.state.reasons),
            "position_value": dict(self.state.position_value),
        }

    def _persist(self) -> None:
        if self._store is not None:
            self._store.save(self._payload())
            return
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
        with self._locked():
            self._kill(reason)

    def _kill(self, reason: str) -> None:
        self.state.killed = True
        self.state.reasons.append(reason)
        self._persist()

    def on_pnl(self, pnl: float) -> None:
        with self._locked():
            self._roll()
            self.state.day_pnl = pnl
            if pnl <= -abs(self.cfg.max_daily_loss):
                self._kill(f"daily loss cap hit ({pnl:,.0f})")
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
        with self._locked():
            return self._pre_order(symbol, value, gross, side=side)

    def _pre_order(
        self,
        symbol: str,
        value: float,
        gross: float,
        *,
        side: str,
    ) -> tuple[bool, str]:
        self._roll()
        s, c = self.state, self.cfg
        if s.killed:
            return False, f"KILL SWITCH: {'; '.join(s.reasons)}"
        if s.orders_today >= c.max_orders_per_day:
            return False, "own daily order cap reached"
        current = float(s.position_value.get(symbol, 0.0))
        side_u = (side or "BUY").upper()
        if side_u == "BUY":  # noqa: SIM108 — sell branch carries the exposure rationale
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

    def release(self, symbol: str, value: float) -> None:
        """Give back a reservation ``pre_order`` took for shares that never filled (LV2/LV3).

        A rejected or cancelled order spent nothing, and an order that filled in part spent only
        its filled part; the reconciler hands the remainder back here so a dead entry does not
        keep a symbol's cap or the gross cap busy for the rest of the day. Never below zero, and
        never a refund of something not reserved: the map is clamped, as the sell branch is.
        """
        with self._locked():
            self._roll()
            current = float(self.state.position_value.get(symbol, 0.0))
            self.state.position_value[symbol] = max(0.0, current - abs(value))
            self._persist()

    def seed_positions(self, values: dict[str, float]) -> None:
        """Count what the account already holds toward today's exposure (LV3).

        ``values`` is ``{symbol: notional}`` from the broker's holdings — a manual Kite buy, the
        weekly book, yesterday's fills. The per-symbol map is raised to at least that notional
        (never lowered: a reservation taken this session stands), so a sleeve cannot add to a name
        the account already carries at the cap, and the gross cap sees the whole book.
        """
        with self._locked():
            self._roll()
            for symbol, notional in values.items():
                if notional <= 0:
                    continue
                current = float(self.state.position_value.get(symbol, 0.0))
                self.state.position_value[symbol] = max(current, float(notional))
            self._persist()
