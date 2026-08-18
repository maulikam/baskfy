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

import datetime as dt
import json
import os
import pathlib
from typing import Any

PATH = "data/outputs/strangle_commitments.json"


def _read(path: str) -> dict[str, Any]:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(p)


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


def commit(slug: str, margin: float, path: str = PATH, *,
           today: dt.date | None = None, pid: int | None = None) -> dict:
    today = today or dt.date.today()
    data = _read(path)
    data[slug] = {"pid": int(pid or os.getpid()), "margin": float(margin),
                  "session_date": today.isoformat(),
                  "at": dt.datetime.now().isoformat(timespec="seconds")}
    _write(path, data)
    return data[slug]


def release(slug: str, path: str = PATH) -> None:
    data = _read(path)
    if data.pop(slug, None) is not None:
        _write(path, data)
