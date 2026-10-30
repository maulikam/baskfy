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
    def __init__(self, start: float) -> None:
        self.t = start

    def time(self) -> float:
        return self.t


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
    CLOCK.t = _REAL_DT.fromisoformat(args.clock).timestamp()
    setattr(_time_mod, "time", CLOCK.time)
    setattr(_dt_mod, "datetime", FakeDateTime)
    setattr(_dt_mod, "date", FakeDate)
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
    from app.main import app as desk  # noqa: PLC0415

    ist = _dt_mod.timezone(_dt_mod.timedelta(hours=5, minutes=30))

    async def harness_clock(request: Request) -> JSONResponse:
        body = json.loads((await request.body()) or b"{}")
        if "iso" in body:
            CLOCK.t = _REAL_DT.fromisoformat(body["iso"]).timestamp()
        if "advance" in body:
            CLOCK.t += max(0.0, float(body["advance"]))
        return JSONResponse({"now": _REAL_DT.fromtimestamp(CLOCK.t, ist).isoformat()})

    async def harness_kite_calls(request: Request) -> JSONResponse:  # noqa: ARG001
        state = desk_fake_kite.STATE
        return JSONResponse(list(state.calls) if state else [])

    # Starlette routes (the endpoint takes the request), behind the desk's own middleware.
    desk.add_route("/__harness/clock", harness_clock, methods=["POST"])
    desk.add_route("/__harness/kite-calls", harness_kite_calls, methods=["GET"])
    uvicorn.run(desk, host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
