"""The session supervisor: a Kite login is the event, whatever the hour (LV4).

Review gap 10: "Kite tokens expire at 06:00, so login is daily; the system reacts to a login only
for Swing. Everything else assumes the token was present at its cron minute and silently produces
nothing if it was not." The LV0 audit measured it: eight of fifteen mornings had no session at
09:05; on 22 and 25 Sep the login came at 10:40 and 11:58.

WHAT IT DOES
------------
One process (`session-supervisor` in compose, the desk's image), awake 08:30–15:50 IST on
weekdays, ticking every ten seconds:

* **Watches the token blob.** The desk's ``Kite`` already re-reads it when its mtime moves
  (``refresh_token_if_changed``); this process turns that moment into an event. On a new token
  that authenticates: a ``login`` heartbeat, the account's holdings **seeded into the shared risk
  ledger** (LV3: a manual Kite holding counts toward exposure from the first order of the day),
  and one reconcile pass at once (LV2: restart recovery — whatever the broker did while nobody
  was watching is booked before anything new is sent).
* **Runs the reconciler** every tick while the session is open and a token exists, and writes
  the ``reconciler`` heartbeat with the pass's one-line summary.
* **Says where it is** — ``lv_heartbeat`` rows ``supervisor`` (``waiting_for_login`` /
  ``session`` / ``idle``) and ``reconciler`` — which the web's ``/sleeves/state`` reads, so the
  Scan buttons can say "waiting for a login" instead of nothing.

WHAT IT NEVER DOES
------------------
Build a plan, place anything of its own, or start another process. The swing monitor reloads its
own watchlist (this module does not reach into it); ``twt-auto`` keeps its 09:15 clock (a plan
that expired at 09:35 is not revived by a login — review P1.3). Every write the reconciler causes
goes through the sleeves' handlers and the gateway. The token is read, never written.

**The one order path it touches, since LV8 (28 Sep 2026, DECISIONS-TW TW19, Maulik's):** once a
minute in session it asks ``twt_auto.drain_now`` to confirm TWT's LIVE plan — that module's own
``execute_line``, its plan expiry, gateway, guards, GTT stop and caps, under the same three flags
as the 09:15 loop (``DRY_RUN=false``, ``BASKFY_TWT_EXECUTION_ENABLED``,
``BASKFY_TWT_AUTO_EXECUTE``). With any of them off the drain is a no-op that beats ``idle``. This
module adds no flag and never builds the plan it drains.

Everything time- and broker-dependent is injected (`Deps`) so the supervisor is a table of
tests; `from_desk()` wires the real desk.
"""

from __future__ import annotations

import calendar
import datetime as dt
import logging
import time
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

log = logging.getLogger("session_supervisor")

IST: Final = zoneinfo.ZoneInfo("Asia/Kolkata")

#: The supervisor's day: from before the pre-open nudges to after the last GTT sweep.
WAKE_AT: Final = dt.time(8, 30)
SLEEP_AT: Final = dt.time(15, 50)
#: NSE's cash session — the reconciler runs inside it.
MARKET_OPEN: Final = dt.time(9, 15)
MARKET_CLOSE: Final = dt.time(15, 30)
#: One tick. The order book is one limiter slot; ten seconds is six calls a minute, well inside
#: Kite's general lane and fast enough that a fill is booked before a person could ask about it.
TICK_SECONDS: Final = 10.0
#: LV8: how often the supervisor asks TWT's auto-execute to drain a LIVE plan (twt_auto's number).
DRAIN_EVERY_SECONDS: Final = 60.0

STATE_WAITING: Final = "waiting_for_login"
STATE_SESSION: Final = "session"
STATE_IDLE: Final = "idle"
STATE_LOGIN: Final = "login"


@dataclass
class Deps:
    """Everything the supervisor touches, named so a test can hand it fakes."""

    authed: Callable[[], bool]
    token_mtime: Callable[[], float | None]
    holdings: Callable[[], list[dict]]
    seed_positions: Callable[[dict[str, float]], None]
    reconcile: Callable[[dt.datetime], Any]
    beat: Callable[[str, str, str, dt.datetime], None]
    #: LV8: TWT's auto-execute drain (``twt_auto.drain_now``), called once a minute while the
    #: session is open and a Kite session exists, so a LIVE plan is confirmed the minute it is
    #: built. ``None`` (tests, or a desk without the module) drains nothing. The drain itself
    #: refuses unless all three TWT flags are set; the supervisor adds no flag of its own.
    drain_twt: Callable[[dt.datetime], Any] | None = None


@dataclass
class Supervisor:
    deps: Deps
    reconcile_every: float = TICK_SECONDS
    _seen_mtime: float | None = field(default=None, init=False)
    _authed: bool = field(default=False, init=False)
    _last_reconcile: dt.datetime | None = field(default=None, init=False)
    _last_drain: dt.datetime | None = field(default=None, init=False)
    drain_every: float = DRAIN_EVERY_SECONDS
    logins: int = field(default=0, init=False)
    passes: int = field(default=0, init=False)
    drains: int = field(default=0, init=False)

    def tick(self, now: dt.datetime) -> str:
        """One look at the world. Returns the supervisor's state after it."""
        local = now.astimezone(IST)
        mtime = self._safe(self.deps.token_mtime, None)
        changed = mtime is not None and mtime != self._seen_mtime
        if changed or not self._authed:
            self._authed = bool(self._safe(self.deps.authed, False))
            if changed and self._authed:
                self._seen_mtime = mtime
                self._on_login(now)
            elif changed:
                self._seen_mtime = mtime
        if not self._authed:
            self._beat("supervisor", STATE_WAITING, "no Kite session; the token is absent or expired", now)
            return STATE_WAITING
        if MARKET_OPEN <= local.time() < MARKET_CLOSE:
            if self._due(now):
                self._reconcile(now)
            if self._drain_due(now):
                self._drain(now)
            self._beat("supervisor", STATE_SESSION, "session open; reconciling every tick", now)
            return STATE_SESSION
        self._beat("supervisor", STATE_IDLE, "session closed; token present", now)
        return STATE_IDLE

    # --- LV8: TWT's live-plan drain --------------------------------------------------------

    def _drain_due(self, now: dt.datetime) -> bool:
        if self.deps.drain_twt is None:
            return False
        if self._last_drain is None:
            return True
        return (now - self._last_drain).total_seconds() >= self.drain_every

    def _drain(self, now: dt.datetime) -> None:
        assert self.deps.drain_twt is not None
        self._last_drain = now
        try:
            report = self.deps.drain_twt(now)
        except Exception as exc:  # noqa: BLE001 - the drain must never stop the supervisor
            log.exception("TWT drain raised: %s", type(exc).__name__)
            self._beat("twt-auto", "error", f"drain raised {type(exc).__name__}", now)
            return
        self.drains += 1
        ran = bool(getattr(report, "ran", False))
        reason = str(getattr(report, "reason", "") or "")
        attempts = list(getattr(report, "attempts", []) or [])
        if ran:
            self._beat("twt-auto", "ran", f"{len(attempts)} line(s) sent through execute_line", now)
        else:
            self._beat("twt-auto", "idle", reason[:200] or "nothing to drain", now)

    # --- the login event -------------------------------------------------------------------

    def _on_login(self, now: dt.datetime) -> None:
        self.logins += 1
        log.info("a Kite session arrived at %s IST", now.astimezone(IST).strftime("%H:%M:%S"))
        self._beat("supervisor", STATE_LOGIN, "token arrived; seeding exposure and reconciling", now)
        try:
            rows = self.deps.holdings()
            values: dict[str, float] = {}
            for row in rows:
                symbol = str(row.get("symbol") or "")
                qty = float(row.get("quantity") or 0)
                price = float(row.get("last_price") or 0)
                if symbol and qty > 0 and price > 0:
                    values[symbol] = qty * price
            self.deps.seed_positions(values)
            log.info("seeded %d holding(s) into the risk ledger", len(values))
        except Exception as exc:  # noqa: BLE001 - a failed seed is logged; the session goes on
            log.error("could not seed holdings into the risk ledger: %s", exc)
        self._reconcile(now)

    # --- the reconciler ---------------------------------------------------------------------

    def _due(self, now: dt.datetime) -> bool:
        last = self._last_reconcile
        return last is None or (now - last).total_seconds() >= self.reconcile_every

    def _reconcile(self, now: dt.datetime) -> None:
        self._last_reconcile = now
        self.passes += 1
        try:
            run = self.deps.reconcile(now)
        except Exception as exc:  # noqa: BLE001 - one failed pass must not stop the next
            log.exception("reconcile pass failed")
            self._beat("reconciler", "error", f"{type(exc).__name__}: {exc}", now)
            return
        line = run.line() if hasattr(run, "line") else str(run)
        ok = bool(getattr(run, "ok", True))
        self._beat("reconciler", "ok" if ok else "errors", line, now)

    # --- plumbing -----------------------------------------------------------------------------

    def _beat(self, process: str, state: str, detail: str, now: dt.datetime) -> None:
        try:
            self.deps.beat(process, state, detail, now)
        except Exception as exc:  # noqa: BLE001 - the record, not the watching
            log.error("could not write the %s heartbeat: %s", process, exc)

    @staticmethod
    def _safe(fn: Callable[[], Any], default: Any) -> Any:  # noqa: ANN401 - a fallback value
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - a failed read is "unknown", not a crash
            log.warning("%s failed: %s", getattr(fn, "__name__", "read"), exc)
            return default


# --- the clock ----------------------------------------------------------------------------------


def next_wake(now: dt.datetime) -> dt.datetime:
    """``now`` inside a weekday's 08:30–15:50; else the next weekday's 08:30."""
    local = now.astimezone(IST)
    if local.weekday() <= calendar.FRIDAY and WAKE_AT <= local.time() < SLEEP_AT:
        return now
    day = local.date()
    if local.time() >= SLEEP_AT or local.weekday() > calendar.FRIDAY:
        day += dt.timedelta(days=1)
    while day.weekday() > calendar.FRIDAY:
        day += dt.timedelta(days=1)
    return dt.datetime.combine(day, WAKE_AT, tzinfo=IST)


def run_forever(
    supervisor: Supervisor,
    *,
    now_fn: Callable[[], dt.datetime] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    limit: int | None = None,
) -> int:
    """Tick every ``TICK_SECONDS`` inside the day; sleep to the next wake outside it."""
    now = now_fn or (lambda: dt.datetime.now(tz=IST))
    sleep = sleep_fn or time.sleep
    ticks = 0
    while limit is None or ticks < limit:
        current = now()
        wake = next_wake(current)
        if wake > current:
            wait = (wake - current).total_seconds()
            log.info("supervisor: asleep until %s IST (%.0fs)", wake.astimezone(IST), wait)
            sleep(wait)
            continue
        state = supervisor.tick(current)
        ticks += 1
        log.debug("supervisor: %s", state)
        sleep(TICK_SECONDS)
    return ticks


# --- the desk's wiring -----------------------------------------------------------------------


class PgHeartbeats:
    """``lv_heartbeat`` upserts through the desk's sqlite-shaped connection — one row a process."""

    def __init__(self, connect: Callable[[], Any], *, user_id: int, schema: str = "public") -> None:
        self._connect = connect
        self.user_id = int(user_id)
        self.schema = schema

    def t(self, table: str) -> str:
        return f"{self.schema}.{table}" if self.schema else table

    def beat(self, process: str, state: str, detail: str, at: dt.datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {self.t('lv_heartbeat')} (user_id, process, state, detail, at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (user_id, process) DO UPDATE SET "
                "state = EXCLUDED.state, detail = EXCLUDED.detail, at = EXCLUDED.at",
                (self.user_id, process, state, detail[:2000], at),
            )


def from_desk() -> Supervisor:
    """The real supervisor: the desk's Kite, its gateway's risk manager, the reconciler, the
    heartbeat table."""
    from . import config as C  # noqa: PLC0415
    from . import main as _main  # noqa: PLC0415 - the desk's one Kite and risk manager
    from . import reconcile  # noqa: PLC0415
    from .analytics import db as _db  # noqa: PLC0415
    from .kite_client import Kite  # noqa: PLC0415
    from .token_store import store_for  # noqa: PLC0415

    kite = _main.kite()
    store = store_for(C.TOKEN_FILE, getattr(C, "KITE_TOKEN_ENCRYPTION_KEY", ""))
    schema = "public" if _db.DB_BACKEND == "postgres" else ""
    beats = PgHeartbeats(_db.connect, user_id=C.SOLE_USER_ID, schema=schema)

    def seed(values: dict[str, float]) -> None:
        _main.gateway().risk.seed_positions(values)

    from . import twt_auto  # noqa: PLC0415 - LV8: the drain, flag-gated inside itself

    deps = Deps(
        authed=kite.is_authed,
        token_mtime=lambda: Kite._blob_mtime(store),  # noqa: SLF001 - the desk's own mtime read
        holdings=kite.holdings,
        seed_positions=seed,
        reconcile=reconcile.run_pass_from_desk,
        beat=beats.beat,
        drain_twt=twt_auto.drain_now,
    )
    return Supervisor(deps)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    run_forever(from_desk())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
