"""Margin committed by other instruments' live sessions.

THE PROBLEM THIS SOLVES. Each instrument config sizes itself to max_utilisation_pct of the
account. That was unambiguous while NIFTY was the only underlying. With three, each one
reading the same Rs 50L and each entitled to 40% of it, the account is oversubscribed to
118% — and nothing in sizing.evaluate can see it, because basket_order_margins is called
with consider_positions=False and the other sessions are separate processes.

What would actually happen is worse than a clean refusal: the first two instruments enter
successfully, the third gets a margin query that finally reflects reality and refuses. So
the account is left carrying whichever two happened to start first, which is a position
nobody sized and nobody chose.

The ledger makes the 40% a PORTFOLIO cap. Each session records what it has committed; every
later sizing decision subtracts what is already live before deciding what it may take.

STALE ENTRIES ARE RECLAIMED, not trusted. An entry whose process is gone, or whose session
date is not today, is ignored — a crash at 09:31 must not lock capital out for the rest of
the day. This is the same reasoning as the session lock, for the same reason.
"""
from __future__ import annotations

import atexit
import contextlib
import datetime as dt
import fcntl
import json
import os
import pathlib
from typing import Any

PATH = "data/outputs/strangle_commitments.json"


@contextlib.contextmanager
def _exclusive(path: str):
    """Hold the ledger for a read-modify-write.

    commit() reads the file, adds one entry and writes it back. Two sessions starting in
    the same second each read the same state and the second write erases the first — so
    the account would be over-committed by exactly the amount the ledger exists to stop.
    autorun starts the instruments back to back, which is the case most likely to hit it.

    Reads elsewhere need no lock: _write replaces the file atomically, so a reader sees
    either the old contents or the new, never a torn file.
    """
    lock = pathlib.Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _read(path: str) -> dict[str, Any]:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    """Atomic replace, through a temp file unique to this process.

    A shared ".tmp" name is not merely racy, it RAISES: three sessions committing at once
    each wrote the same temp path and one replaced it out from under another, so the loser
    died with FileNotFoundError mid-commit. Reproduced with three processes before the lock
    was added. The lock now serialises commits, and the unique name means a reader or a
    filesystem without working flock still cannot produce that failure.
    """
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(p)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True                      # exists, owned by someone else
    except OSError:
        return False
    return True


def live(path: str = PATH, *, today: dt.date | None = None) -> dict[str, dict]:
    """Commitments from processes that are still running, for today's session only."""
    today = today or dt.date.today()
    out = {}
    for slug, rec in _read(path).items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("session_date")) != today.isoformat():
            continue
        if not _alive(rec.get("pid")):
            continue
        out[slug] = rec
    return out


def committed_elsewhere(slug: str, path: str = PATH, *,
                        today: dt.date | None = None) -> float:
    """Margin held by every live session that is NOT this instrument."""
    return float(sum(float(r.get("margin") or 0.0)
                     for s, r in live(path, today=today).items() if s != slug))


_REGISTERED: set[tuple[str, str]] = set()


def commit(slug: str, margin: float, path: str = PATH, *,
           today: dt.date | None = None, pid: int | None = None,
           release_on_exit: bool = True) -> dict:
    """Claim margin for this instrument, and arrange to give it back.

    The release is registered HERE, against the same path the claim was written to. It
    used to be registered by the caller as `atexit.register(release, slug)` with no path
    at all — correct only because the runner happens to use the default, and silently
    releasing the wrong file for any caller that does not. That is the same shape as the
    forgotten-argument defects this module exists to make impossible.

    Registered once per (slug, path): a session commits once, but a caller in a loop must
    not stack thousands of identical handlers.
    """
    today = today or dt.date.today()
    rec = {"pid": int(pid or os.getpid()), "margin": float(margin),
           "session_date": today.isoformat(),
           "at": dt.datetime.now().isoformat(timespec="seconds")}
    with _exclusive(path):
        data = _read(path)
        data[slug] = rec
        _write(path, data)
    key = (slug, str(path))
    if release_on_exit and key not in _REGISTERED:
        _REGISTERED.add(key)
        atexit.register(release, slug, path)
    return rec


def release(slug: str, path: str = PATH) -> None:
    with _exclusive(path):
        data = _read(path)
        if data.pop(slug, None) is not None:
            _write(path, data)
