"""The overlap page's "Scan filings" button: read every listed name's filings on demand.

The catalyst feed (`baskfy_worker.tasks.swing_catalyst`) runs at 09:17 over a deliberately small
set — watched names, EP candidates, VBT and TWT signals — because every name costs two NSE
requests on a 1 req/s limiter the 09:16 swing monitor also needs. A swing FLAG or a TWT name that
is merely in the tight state is on `/build/overlap` but not in that set, so its Results and
Filing cells were dashes. Maulik, in session, 25 Sep 2026: "put laya scan button on overlap page".

The button publishes `baskfy.overlap.catalyst_scan`, which reads the names the page lists for
the scope in view through the SAME loop, limiter and upsert as the morning feed, then wakes the
Laya sidecar (`infra/laya/laya_loop.py`) so the new headlines are tagged within seconds rather
than at its next five-minute pass. Nothing it writes reaches a rank, a size or an order: it fills
`sw_catalyst`, which is display context.

This module is the contract the API and the worker share — the Redis keys and the status shape —
because the worker imports the API and never the other way round (`baskfy_api.queue`).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Final, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError

from baskfy_api.metrics import IST
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.queue import TaskQueue

SCAN_TASK: Final = "baskfy.overlap.catalyst_scan"
#: Set by the worker when a scan finishes; the Laya loop polls it while it sleeps.
LAYA_WAKE_KEY: Final = "laya:wake"
#: A scan that has not reported for this long is taken to have died, and a new one may start.
STALE_AFTER: Final = dt.timedelta(minutes=20)
#: How long a finished scan's status stays readable.
STATUS_TTL_SECONDS: Final = 7 * 24 * 3600
#: The morning window the NSE limiter belongs to the swing monitor (09:16) and the catalyst feed
#: (09:17). A scan asked for inside it is refused, not queued behind them.
BUSY_FROM: Final = dt.time(9, 10)
BUSY_UNTIL: Final = dt.time(9, 30)
#: `date.weekday()` of Saturday: the busy window is a weekday one.
SATURDAY: Final = 5

ScanState = Literal["idle", "queued", "running", "done", "failed"]
ScanScope = Literal["actionable", "all"]


def status_key(user_id: int) -> str:
    return f"overlap:catalyst-scan:{user_id}"


def lock_key(user_id: int) -> str:
    return f"overlap:catalyst-scan:{user_id}:lock"


def in_busy_window(now: dt.datetime) -> bool:
    local = now.astimezone(IST)
    return local.weekday() < SATURDAY and BUSY_FROM <= local.time() < BUSY_UNTIL


async def read_status(cache: Redis | None, user_id: int) -> dict[str, object]:
    """The last scan's status, or ``{"state": "idle"}`` when there has been none."""
    if cache is None:
        return {"state": "idle"}
    try:
        raw = await cache.get(status_key(user_id))
    except RedisError:
        return {"state": "idle"}
    if raw is None:
        return {"state": "idle"}
    parsed: object = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {"state": "idle"}


async def start_scan(
    cache: Redis | None,
    queue: TaskQueue | None,
    *,
    user_id: int,
    scope: ScanScope,
    now: dt.datetime,
) -> dict[str, object]:
    """Queue one scan for ``user_id``, or refuse with the reason. Returns the queued status."""
    if cache is None or queue is None:
        raise Problem(
            ProblemType.INTERNAL_ERROR,
            "No task broker or cache is configured, so filings cannot be scanned from here.",
        )
    if in_busy_window(now):
        raise Problem(
            ProblemType.SCAN_IN_FLIGHT,
            "09:10-09:30 IST belongs to the swing monitor and the morning catalyst feed; "
            "scan filings after 09:30.",
        )
    locked = await cache.set(
        lock_key(user_id), now.isoformat(), nx=True, ex=int(STALE_AFTER.total_seconds())
    )
    if not locked:
        raise Problem(
            ProblemType.SCAN_IN_FLIGHT,
            "A filings scan is already running; its progress is shown on the page.",
        )
    status: dict[str, object] = {
        "state": "queued",
        "scope": scope,
        "queued_at": now.isoformat(),
        "total": None,
        "done": 0,
    }
    await cache.set(status_key(user_id), json.dumps(status), ex=STATUS_TTL_SECONDS)
    status["task_id"] = str(queue.send_task(SCAN_TASK, [user_id, scope]))
    return status


__all__ = [
    "BUSY_FROM",
    "BUSY_UNTIL",
    "LAYA_WAKE_KEY",
    "SCAN_TASK",
    "STALE_AFTER",
    "STATUS_TTL_SECONDS",
    "ScanScope",
    "ScanState",
    "in_busy_window",
    "lock_key",
    "read_status",
    "start_scan",
    "status_key",
]
