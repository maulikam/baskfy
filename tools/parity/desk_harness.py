"""Start the PYTHON desk for ``desk_diff.py``: replay Kite, injected clock, harness ids.

Harness-only. The desk's code is unchanged; this process patches four seams **before** the desk
is imported, then serves ``app.main:app`` with uvicorn:

* ``kiteconnect.KiteConnect`` → ``desk_fake_kite.ReplayKiteConnect`` (fixture ``--fixture``,
  call log ``--kite-log``). The desk constructs ``KiteConnect(api_key=...)`` and hands
  ``kite().kc`` to the gateway, so reads and order/GTT calls both land in the replay.
* ``time.time`` and ``datetime.datetime.now/utcnow/today`` / ``datetime.date.today`` read a
  settable clock starting at ``--clock`` (RFC 3339). ``time.monotonic`` is left alone (asyncio
  runs on it). The replacement classes construct real ``datetime``/``date`` objects and accept
  real ones in ``isinstance`` — the freezegun technique, so nothing downstream sees a subclass.
* ``uuid.uuid4`` → the harness sequence ``SHA-256("baskfy-desk-harness:<seed>:<n>")[:16]`` as a
  version-4 UUID — the same sequence as ``baskfy_desk::ids::HarnessIds``.
* ``dotenv.load_dotenv`` → no-op (no ``.env`` may leak in; the environment is the harness's).

and adds the two harness routes the Rust desk has under ``--harness``: ``POST /__harness/clock``
(``{"iso": ...}`` or ``{"advance": seconds}``) and ``GET /__harness/kite-calls``.

It refuses to start unless ``DRY_RUN`` is unset or true. Run it from the side's run directory
(``app`` symlinked to the desk's ``app``; ``data/`` fresh), with the worktree on ``PYTHONPATH``.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import datetime as _dt_mod
import hashlib
import json
import os
import sys
import time as _time_mod
import uuid as _uuid_mod
from typing import Any


class SettableClock:
    """``time.time`` for the desk, and the monotonic reading the Kite wrapper's LTP cache keeps.

    ``mono`` is ``baskfy_desk::clock::FixedClock``'s rule: it starts at 1000 s and moves forward
    by every forward step of the clock, never back, and stands still between steps. Without it
    a case clock jumping a day left Python's 5 s LTP cache warm (real ``time.monotonic`` had
    moved a second) while Rust's expired — one ``ltp`` read apart (wave D2 twt_desk finding).
    """

    def __init__(self, start: float) -> None:
        self.t = start
        self.mono = 1000.0

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def set(self, t: float) -> None:
        self.mono += max(0.0, t - self.t)
        self.t = t

    def advance(self, seconds: float) -> None:
        step = max(0.0, seconds)
        self.t += step
        self.mono += step


class _ClockedTime:
    """``app.kite_client``'s ``time`` module: the real one, except ``monotonic`` reads ``CLOCK``.

    Only that module sees it — asyncio, uvicorn and the shared Kite rate limiter keep the real
    ``time.monotonic`` (a limiter on a clock that stands still would wait forever)."""

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(_time_mod, name)

    @staticmethod
    def monotonic() -> float:
        return CLOCK.monotonic()


CLOCK = SettableClock(0.0)
_REAL_DT = _dt_mod.datetime
_REAL_D = _dt_mod.date


class _DateTimeMeta(type):
    def __instancecheck__(cls, obj: object) -> bool:
        return isinstance(obj, _REAL_DT)

    def __subclasscheck__(cls, sub: type) -> bool:
        return issubclass(sub, _REAL_DT)


class _DateMeta(type):
    def __instancecheck__(cls, obj: object) -> bool:
        return isinstance(obj, _REAL_D)

    def __subclasscheck__(cls, sub: type) -> bool:
        return issubclass(sub, _REAL_D)


class FakeDateTime(_REAL_DT, metaclass=_DateTimeMeta):
    def __new__(cls, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return _REAL_DT(*args, **kwargs)

    @classmethod
    def now(cls, tz: Any = None) -> Any:  # noqa: ANN401
        return _REAL_DT.fromtimestamp(CLOCK.time(), tz)

    @classmethod
    def utcnow(cls) -> Any:  # noqa: ANN401
        return _REAL_DT.fromtimestamp(CLOCK.time(), _dt_mod.timezone.utc).replace(tzinfo=None)

    @classmethod
    def today(cls) -> Any:  # noqa: ANN401
        return _REAL_DT.fromtimestamp(CLOCK.time())


class FakeDate(_REAL_D, metaclass=_DateMeta):
    def __new__(cls, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return _REAL_D(*args, **kwargs)

    @classmethod
    def today(cls) -> Any:  # noqa: ANN401
        return _REAL_D.fromtimestamp(CLOCK.time())


class HarnessIds:
    def __init__(self, seed: str) -> None:
        self.seed = seed
        self.n = 0

    def uuid4(self) -> _uuid_mod.UUID:
        digest = hashlib.sha256(f"baskfy-desk-harness:{self.seed}:{self.n}".encode()).digest()
        self.n += 1
        return _uuid_mod.UUID(bytes=digest[:16], version=4)


def _warm_polars_temporal() -> None:
    # Polars resolves the datetime classes on first conversion; it must see the real ones.
    with contextlib.suppress(ImportError):
        import polars as pl  # noqa: PLC0415

        day, moment = _REAL_D(2000, 1, 3), _REAL_DT(2000, 1, 3, 9, 15)
        frame = pl.DataFrame({"d": [day], "t": [moment]}).sort("d")
        frame.to_dicts()
        frame["d"].to_list()
        frame["t"].to_list()
        pl.DataFrame({"x": [1]}).with_columns(pl.lit(day).alias("d"), pl.lit(moment).alias("t")).rows()


def install_clock_seams(clock: str) -> None:
    _warm_polars_temporal()
    CLOCK.t = _REAL_DT.fromisoformat(clock).timestamp()
    setattr(_time_mod, "time", CLOCK.time)
    setattr(_dt_mod, "datetime", FakeDateTime)
    setattr(_dt_mod, "date", FakeDate)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--kite-log", default=None)
    parser.add_argument("--clock", default="2026-10-30T09:16:00+05:30")
    parser.add_argument("--seed", default="desk")
    args = parser.parse_args()

    if os.environ.get("DRY_RUN", "true").lower() == "false":
        print("desk_harness refuses to start with DRY_RUN=false", file=sys.stderr)
        return 2

    # Third-party libraries first, with the real classes, then the seams, then the desk.
    import dotenv  # noqa: PLC0415
    import fastapi  # noqa: PLC0415, F401
    import kiteconnect  # noqa: PLC0415
    import uvicorn  # noqa: PLC0415

    # Libraries whose C extensions subclass or capture datetime at import: they must see the
    # real classes (pandas' NaT subclasses datetime), so they load before the seams go in.
    for name in ("numpy", "pandas", "polars", "psycopg", "redis", "orjson", "dateutil.parser",
                 "starlette.templating", "jinja2", "pydantic", "pydantic.v1", "fastapi.encoders",
                 "fastapi.dependencies.utils", "email.utils", "zoneinfo"):
        with contextlib.suppress(ImportError):
            importlib.import_module(name)

    setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    install_clock_seams(args.clock)
    ids = HarnessIds(args.seed)
    setattr(_uuid_mod, "uuid4", ids.uuid4)

    import desk_fake_kite  # noqa: PLC0415 - tools/parity, on PYTHONPATH

    desk_fake_kite.install(args.fixture, args.kite_log)
    setattr(kiteconnect, "KiteConnect", desk_fake_kite.ReplayKiteConnect)

    from fastapi import Request  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    import app  # noqa: PLC0415
    import app.kite_client  # noqa: PLC0415

    setattr(app.kite_client, "KiteConnect", desk_fake_kite.ReplayKiteConnect)
    setattr(app.kite_client, "time", _ClockedTime())
    from app.main import app as desk  # noqa: PLC0415

    ist = _dt_mod.timezone(_dt_mod.timedelta(hours=5, minutes=30))

    async def harness_clock(request: Request) -> JSONResponse:
        body = json.loads((await request.body()) or b"{}")
        if "iso" in body:
            CLOCK.set(_REAL_DT.fromisoformat(body["iso"]).timestamp())
        if "advance" in body:
            CLOCK.advance(float(body["advance"]))
        return JSONResponse({"now": _REAL_DT.fromtimestamp(CLOCK.t, ist).isoformat()})

    async def harness_kite_calls(request: Request) -> JSONResponse:  # noqa: ARG001
        state = desk_fake_kite.STATE
        return JSONResponse(list(state.calls) if state else [])

    async def harness_swing_signal(request: Request) -> JSONResponse:
        """Lane swing_monitor (wave D2): raise one signal through `app.swing_monitor`'s real
        `PgSignalStore` on this side's database — the SIGNAL plan kind, graded by `desk_diff
        --plans` (`desk_cases/plans_swing_signal.json`). The Rust desk mounts the same route under
        `--harness` (`swing_monitor/harness.rs`). `live_level` stands in for Kite's index level;
        `drain` runs SW25's drain, which under the harness (DRY_RUN) confirms nothing."""
        import dataclasses  # noqa: PLC0415
        from decimal import Decimal  # noqa: PLC0415

        from app import config as C  # noqa: PLC0415
        from app import swing_monitor as M  # noqa: PLC0415
        from app.analytics import db as _db  # noqa: PLC0415
        from app.strategies.swing_breakout import Signal  # noqa: PLC0415
        from baskfy_core.swing.opening_range import TriggerState, TriggerVerdict  # noqa: PLC0415

        body = json.loads((await request.body()) or b"{}")

        def dec(value: Any) -> Any:  # noqa: ANN401
            return None if value is None else Decimal(str(value))

        with _db.connect() as conn:
            user_id = C.SOLE_USER_ID
            day = _dt_mod.datetime.now(ist).date()
            config = M.load_config(conn, user_id=user_id)
            names = {w.symbol: w for w in M.load_watchlist(conn, user_id=user_id)}
            watch = names.get(body.get("symbol"))
            if watch is None:
                return JSONResponse({"detail": "not watched"}, status_code=404)
            if "focus" in body:
                watch = dataclasses.replace(watch, focus=bool(body["focus"]))
            level = body.get("live_level")
            live = (lambda: float(level)) if level is not None else None
            store = M.PgSignalStore(conn, user_id=user_id, day=day, config=config,
                                    notifier=M.Notifier(), live_level=live)
            signal = Signal(
                watch=watch,
                at=_dt_mod.datetime.fromisoformat(body["at"]),
                verdict=TriggerVerdict(TriggerState(body["state"]), dec(body.get("entry")),
                                       dec(body.get("stop")), dec(body.get("range_high")),
                                       dec(body.get("range_low"))),
                last_price=dec(body["last_price"]),
                low_of_day=dec(body["low_of_day"]),
                window_minutes=int(body.get("window_minutes", 5)),
            )
            store.raise_signal(signal)
            queued = [[s.watch.symbol, p.plan_id, p.line_id] for s, p in store.pending_confirms]
            drained = M.drain_auto_execute(store) if body.get("drain") else None
            out = {
                "lines_written": store.lines_written,
                "queued": queued,
                "auto_executed": [list(x) for x in drained] if drained is not None else None,
                "notices": [M.notice_json(n) for n in store.notices],
                "sent": [[c, s, ok] for c, s, ok in store.notifier.sent],
                "gate": store.context.gate.value,
            }
        return JSONResponse(out)

    # Starlette routes (the endpoint takes the request), behind the desk's own middleware.
    desk.add_route("/__harness/clock", harness_clock, methods=["POST"])
    desk.add_route("/__harness/kite-calls", harness_kite_calls, methods=["GET"])
    desk.add_route("/__harness/swing-signal", harness_swing_signal, methods=["POST"])
    uvicorn.run(desk, host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
