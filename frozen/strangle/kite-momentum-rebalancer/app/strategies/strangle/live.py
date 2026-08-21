"""V4 preconditions, broker failover and the kill switch.

NOTHING IN THIS MODULE PLACES AN ORDER. It decides whether live trading may begin and when
it must stop. The order path itself is fills_live.py, and that routes through
app/core/gateway.py like every other order in this system.

FIVE INDEPENDENT LOCKS stand between this code and a real order, and all five are closed
today. They are listed by preflight() with their current state, because a safety story you
cannot read off the running system is not a safety story:

    live.enabled              false      config/strangle.yaml
    typed confirmation        required   exact phrase, no default
    paper sessions            0 of 60    counted from the journal, not asserted
    config.OPTIONS_ENABLED    false      gateway refuses every NFO order
    config.INTRADAY_ENABLED   false      gateway refuses every MIS order

The last two are enforced inside the gateway, so even a caller that bypassed preflight
entirely would be refused at the layer that talks to Kite.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

CONFIRMATION_PHRASE = "TRADE LIVE MONEY"


# =====================================================================================
# preconditions
# =====================================================================================
@dataclass(frozen=True)
class Lock:
    name: str
    open: bool
    detail: str

    def as_dict(self) -> dict:
        return {"lock": self.name, "open": self.open, "detail": self.detail}


@dataclass(frozen=True)
class Preflight:
    locks: tuple[Lock, ...]

    @property
    def cleared(self) -> bool:
        return all(l.open for l in self.locks)

    @property
    def closed(self) -> list[str]:
        return [l.name for l in self.locks if not l.open]

    def as_dict(self) -> dict:
        return {"cleared": self.cleared, "closed_locks": self.closed,
                "locks": [l.as_dict() for l in self.locks]}


def completed_paper_sessions(journal) -> list[dict]:
    """Sessions that actually reached a flat book, read from the journal.

    Counted, never configured. A session that ended UNCLOSED is not a completed session,
    and one that never opened a position is not evidence of anything — including both would
    let sixty skipped days look like sixty days of experience.
    """
    return [r for r in journal.read()
            if r.get("event") == "session_closed" and r.get("status") == "OK"
            and r.get("flat") and r.get("pnl") is not None]


def observed_expectancy(sessions: Sequence[Mapping[str, Any]]) -> dict:
    """Mean net P&L per completed session, with its own standard error."""
    pnls = [float(s["pnl"]) for s in sessions]
    n = len(pnls)
    if n == 0:
        return {"n": 0, "mean": None, "standard_error": None, "t": None}
    mean = sum(pnls) / n
    if n < 2:
        return {"n": n, "mean": round(mean, 2), "standard_error": None, "t": None}
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    se = (var / n) ** 0.5
    return {"n": n, "mean": round(mean, 2),
            "standard_error": round(se, 2) if se else None,
            "t": round(mean / se, 2) if se else None,
            "wins": sum(1 for p in pnls if p > 0),
            "win_rate_pct": round(sum(1 for p in pnls if p > 0) / n * 100, 1)}


def preflight(cfg: dict, journal, *, confirmation: str = "",
              app_config=None, secondary_authenticated: bool = False) -> Preflight:
    """Every lock between here and a live order, with its actual state."""
    if app_config is None:
        from ... import config as app_config          # type: ignore[no-redef]

    live = cfg.get("live") or {}
    sessions = completed_paper_sessions(journal)
    need = int(live.get("min_paper_sessions_before_live", 60))
    exp = observed_expectancy(sessions)

    locks = [
        Lock("live.enabled", bool(live.get("enabled")),
             "config/strangle.yaml live.enabled"),
        Lock("typed_confirmation",
             (not live.get("require_typed_confirmation", True))
             or confirmation == CONFIRMATION_PHRASE,
             f"exact phrase {CONFIRMATION_PHRASE!r} required"),
        Lock("paper_sessions", len(sessions) >= need,
             f"{len(sessions)} of {need} completed paper sessions"),
        Lock("options_enabled", bool(getattr(app_config, "OPTIONS_ENABLED", False)),
             "config.OPTIONS_ENABLED — the gateway refuses every NFO/BFO order while off"),
        Lock("intraday_enabled", bool(getattr(app_config, "INTRADAY_ENABLED", False)),
             "config.INTRADAY_ENABLED — MIS is an intraday product on every segment"),
        Lock("secondary_broker",
             (not cfg["gates"].get("require_secondary_broker", True))
             or secondary_authenticated,
             "operational rule R1: a second session funded to buy back the position"),
    ]
    if live.get("require_expectancy_within_tolerance", True):
        # Deliberately weak, and said so: the config carries no expectancy number to test
        # against, so this can only check that the paper record is not negative. A real
        # tolerance test needs a target to compare with, and inventing one here would give
        # the appearance of a check without its substance.
        ok = exp["n"] > 0 and (exp["mean"] or 0) > 0
        locks.append(Lock("expectancy_positive", ok,
                          f"paper mean {exp['mean']} over {exp['n']} sessions "
                          "(no configured tolerance to test against)"))
    return Preflight(tuple(locks))


# =====================================================================================
# broker failover — operational rule R1
# =====================================================================================
class NoBrokerAvailable(RuntimeError):
    """Neither session can be reached. The position cannot be managed; flatten and halt."""


@dataclass
class BrokerPool:
    """Primary and secondary sessions, with failover on the primary going dark.

    The source lost Rs 15 lakh to an exchange outage and concluded the failure was his own
    for having no redundancy. The secondary is not a nicety: it must be funded well enough
    to BUY BACK the whole position, because the one thing you cannot afford during an
    outage is to be short and unable to act.
    """
    primary: Any
    secondary: Any | None = None
    active: str = "primary"
    failovers: list[tuple[dt.datetime, str]] = field(default_factory=list)

    def _alive(self, broker) -> bool:
        try:
            return bool(broker) and bool(broker.is_authed())
        except Exception:                                    # noqa: BLE001
            return False

    def current(self, *, now: dt.datetime | None = None):
        """The session to use right now, failing over if the primary is unreachable."""
        if self.active == "primary" and self._alive(self.primary):
            return self.primary
        if self._alive(self.secondary):
            if self.active != "secondary":
                self.active = "secondary"
                self.failovers.append((now or dt.datetime.now(),
                                       "primary unreachable"))
            return self.secondary
        if self._alive(self.primary):
            if self.active != "primary":
                self.active = "primary"
                self.failovers.append((now or dt.datetime.now(),
                                       "secondary unreachable, primary back"))
            return self.primary
        raise NoBrokerAvailable(
            "neither the primary nor the secondary session is authenticated; the position "
            "cannot be managed from here")

    @property
    def ready(self) -> bool:
        """Both sessions live. Required before a live session may START."""
        return self._alive(self.primary) and self._alive(self.secondary)


# =====================================================================================
# kill switch — rules R2 and R6
# =====================================================================================
@dataclass
class KillSwitch:
    """Heartbeat, staleness and the 2x-stop breach. Any trip means flatten and halt.

    R6 exists because a book past twice its stop is not a bad day, it is a broken system:
    either the marks are wrong, an order did not go through, or the position is not what the
    book thinks it is. None of those are improved by continuing to trade.
    """
    heartbeat_seconds: float
    max_misses: int
    max_staleness_seconds: float
    kill_multiple: float
    misses: int = 0
    last_beat: dt.datetime | None = None
    tripped: str = ""

    @classmethod
    def from_config(cls, cfg: dict) -> "KillSwitch":
        op = cfg["operational"]
        return cls(heartbeat_seconds=float(op["heartbeat_seconds"]),
                   max_misses=int(op["heartbeat_max_misses"]),
                   max_staleness_seconds=float(cfg["gates"]["max_data_staleness_seconds"]),
                   kill_multiple=float(op["kill_switch_multiple_of_stop"]))

    def beat(self, at: dt.datetime) -> None:
        self.last_beat = at
        self.misses = 0

    def check(self, *, now: dt.datetime, stale_seconds: float, book=None,
              marks: Mapping[str, float] | None = None) -> str:
        """Empty string when healthy, otherwise the reason to flatten immediately."""
        if self.tripped:
            return self.tripped

        if self.last_beat is not None:
            gap = (now - self.last_beat).total_seconds()
            if gap > self.heartbeat_seconds:
                self.misses = int(gap // self.heartbeat_seconds)
            if self.misses >= self.max_misses:
                self.tripped = (f"heartbeat missed {self.misses} times "
                                f"({gap:.0f}s without a beat)")
                return self.tripped

        if stale_seconds > self.max_staleness_seconds:
            self.tripped = f"feed stale {stale_seconds:.1f}s"
            return self.tripped

        if book is not None and marks:
            try:
                pnl = book.session_pnl(marks)
            except KeyError:
                return ""                     # a missing mark is handled by the loop
            if book.risk_budget and pnl <= -self.kill_multiple * book.risk_budget:
                self.tripped = (f"book {pnl:,.0f} is past {self.kill_multiple:g}x the stop "
                                f"({-self.kill_multiple * book.risk_budget:,.0f}); the "
                                "stop did not hold, so something is broken")
                return self.tripped
        return ""
