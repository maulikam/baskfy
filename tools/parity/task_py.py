"""The Python side of ``task_diff.py``: one Celery task body, run once, with the replay and an injected clock.

    task_py.py run NAME [--args JSON] [--now YYYY-MM-DDTHH:MM[:SS]] --replay-bundle DIR
                        [--replay-upstream URL]

Harness code only — production Python is unchanged. At process start it reuses ``nightly_py``'s
injection (the loopback-only **network guard**, the replay provider behind the production
``CompositeProvider``, ``kite_session_cli.refresh_quietly`` as a no-op), then:

* points ``kiteconnect.KiteConnect`` at ``--replay-upstream`` (the harness's loopback server of
  recorded Kite REST answers, ``replay_upstream.py``) by its class default root — the NSE adapter
  reaches the same server through ``BASKFY_NSE_ARCHIVE_URL``/``BASKFY_NSE_BASE_URL``, set by the
  harness;
* installs the **injected clock** (``--now``, an IST wall-clock moment) in the **production modules
  only** (``baskfy_*``, the desk's ``app``/``scripts``): their ``datetime`` module reference (the
  code reads the clock as ``dt.datetime.now(...)`` through ``import datetime as dt``) is re-pointed
  to a copy whose ``datetime`` and ``date`` classes answer ``now``/``utcnow``/``today`` from that
  moment advanced by real elapsed (monotonic) time in whole seconds — the same clock ``baskfy-worker task --now``
  gives the Rust side. Construction, ``isinstance`` and every other classmethod behave as the real
  types (instances are always real ``datetime``/``date`` objects); a production module imported
  later is re-pointed once it has loaded. Libraries keep the real module (a process-wide patch
  breaks polars' dtype inference). Not injected: Postgres's ``now()`` and ``time.time()`` (the
  harness masks what they stamp; DECISIONS-RS R8.2).

``run`` calls the registered task as a Celery worker would — ``app.tasks[NAME](*args, **kwargs)``,
once, no retry — and prints its return value as JSON (``json.dumps(..., default=str)``, key order
kept). A raised error prints ``{"raised": <class>, "error": <str>}``; exit 75 when the task's
``autoretry_for`` covers it, 1 otherwise, 2 for an unknown task or bad ``--args``.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.abc
import json
import sys
import time
import types
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nightly_py  # noqa: E402  (arms the network guard and imports the worker)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")
_REAL_DATETIME = datetime.datetime
_REAL_DATE = datetime.date


class _Clock:
    start_utc: _REAL_DATETIME | None = None
    started = 0.0

    @classmethod
    def instant(cls) -> _REAL_DATETIME:
        """The injected moment advanced by real elapsed time in **whole seconds**, aware UTC.

        Whole seconds, as the Rust side's `TaskClock`: a task that reads the clock in its first
        second sees exactly the case's moment on both sides (a microsecond of drift would change
        an option's time to expiry, and so its greeks), and a task that waits still progresses.
        """
        assert cls.start_utc is not None
        return cls.start_utc + datetime.timedelta(seconds=int(time.monotonic() - cls.started))


class _Like(type):
    """``isinstance``/``issubclass`` against the fake answer for the real type."""

    _real: type

    def __instancecheck__(cls, obj: object) -> bool:
        return isinstance(obj, cls._real)

    def __subclasscheck__(cls, sub: type) -> bool:
        return issubclass(sub, cls._real)


class FakeDatetime(_REAL_DATETIME, metaclass=_Like):
    _real = _REAL_DATETIME

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        return _REAL_DATETIME(*args, **kwargs)

    @classmethod
    def now(cls, tz: datetime.tzinfo | None = None) -> Any:  # noqa: D102
        moment = _Clock.instant()
        if tz is None:
            return moment.astimezone().replace(tzinfo=None)
        return moment.astimezone(tz)

    @classmethod
    def utcnow(cls) -> Any:  # noqa: D102
        return _Clock.instant().replace(tzinfo=None)

    @classmethod
    def today(cls) -> Any:  # noqa: D102
        return cls.now()


class FakeDate(_REAL_DATE, metaclass=_Like):
    _real = _REAL_DATE

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        return _REAL_DATE(*args, **kwargs)

    @classmethod
    def today(cls) -> Any:  # noqa: D102
        return _Clock.instant().astimezone().date()


#: Modules that see the injected clock: production code only (the worker and its packages, and
#: the desk's ``app``/``scripts`` some tasks import). Libraries keep the real ``datetime`` — polars,
#: SQLAlchemy and pydantic resolve types by identity, and a process-wide patch breaks them (it
#: made polars refuse a date column on the first run of this harness).
PATCHED_PREFIXES = ("baskfy_", "app.", "scripts.")
PATCHED_NAMES = ("app", "scripts")


def _ours(name: str) -> bool:
    return name.startswith(PATCHED_PREFIXES) or name in PATCHED_NAMES


def _proxy_module() -> types.ModuleType:
    """A ``datetime`` module whose ``datetime`` and ``date`` read the injected clock."""
    proxy = types.ModuleType("datetime")
    proxy.__dict__.update({k: v for k, v in vars(datetime).items() if not k.startswith("__")})
    proxy.datetime = FakeDatetime  # type: ignore[attr-defined]
    proxy.date = FakeDate  # type: ignore[attr-defined]
    return proxy


_PROXY: types.ModuleType | None = None


def _repoint(module: types.ModuleType) -> int:
    count = 0
    for attr, value in list(vars(module).items()):
        if value is datetime:
            setattr(module, attr, _PROXY)
        elif value is _REAL_DATETIME:
            setattr(module, attr, FakeDatetime)
        elif value is _REAL_DATE:
            setattr(module, attr, FakeDate)
        else:
            continue
        count += 1
    return count


class _RepointOnImport(importlib.abc.MetaPathFinder):
    """Re-points a production module imported *after* the clock was installed (a lazy import
    inside a task body), once its code has run — so its classes were built on the real types."""

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        if not _ours(fullname):
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
                continue
            loader = spec.loader
            original = loader.exec_module

            def exec_module(module: types.ModuleType, _original: Any = original) -> None:
                _original(module)
                _repoint(module)

            loader.exec_module = exec_module  # type: ignore[method-assign]
            return spec
        return None


def install_clock(now_ist: _REAL_DATETIME) -> int:
    """Inject the clock into the production modules; returns how many references were re-pointed.

    SQLAlchemy's mappers are configured first, so every ``Mapped[dt.date]`` annotation resolves to
    the real type before any module's ``dt`` is re-pointed.
    """
    global _PROXY
    from sqlalchemy.orm import configure_mappers  # noqa: PLC0415

    configure_mappers()
    _Clock.start_utc = now_ist.replace(tzinfo=IST).astimezone(datetime.UTC)
    _Clock.started = time.monotonic()
    _PROXY = _proxy_module()
    touched = 0
    for name, module in list(sys.modules.items()):
        if module is not None and _ours(name):
            touched += _repoint(module)
    sys.meta_path.insert(0, _RepointOnImport())
    return touched


def point_kite_at(upstream: str) -> None:
    import kiteconnect  # noqa: PLC0415

    setattr(kiteconnect.KiteConnect, "_default_root_uri", upstream.rstrip("/"))


def emit(payload: Any) -> None:
    print(json.dumps(payload, default=str), flush=True)


def parse_now(text: str) -> _REAL_DATETIME:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return _REAL_DATETIME.strptime(text, fmt)
        except ValueError:
            continue
    raise SystemExit(f"--now {text!r} is not YYYY-MM-DDTHH:MM[:SS]")


def cmd_run(name: str, raw_args: str | None, now: str | None, upstream: str | None) -> int:
    from baskfy_worker.celery_app import app  # noqa: PLC0415

    import baskfy_worker.tasks.celery_tasks  # noqa: F401, PLC0415
    import baskfy_worker.tasks.desk  # noqa: F401, PLC0415

    if upstream:
        point_kite_at(upstream)
    if now:
        install_clock(parse_now(now))
    task = app.tasks.get(name)
    if task is None or name.startswith("celery."):
        emit({"raised": "NotRegistered", "error": repr(name)})
        return 2
    positional: list[Any] = []
    keywords: dict[str, Any] = {}
    if raw_args:
        parsed = json.loads(raw_args)
        if isinstance(parsed, list):
            positional = parsed
        elif isinstance(parsed, dict):
            keywords = parsed
        else:
            print("--args must be a JSON array (args) or object (kwargs)", file=sys.stderr)
            return 2
    try:
        result = task(*positional, **keywords)
    except Exception as exc:
        emit({"raised": type(exc).__name__, "error": str(exc)})
        retry_for = tuple(getattr(task, "autoretry_for", ()) or ())
        return 75 if retry_for and isinstance(exc, retry_for) else 1
    emit(result)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("--args")
    r.add_argument("--now")
    r.add_argument("--replay-bundle", required=True, type=Path)
    r.add_argument("--replay-upstream")
    args = parser.parse_args(argv)
    nightly_py.inject(args.replay_bundle)
    return cmd_run(args.name, args.args, args.now, args.replay_upstream)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
