"""Which trading sessions never landed — and the standing instruction to run them again (M84).

WHY THIS EXISTS
---------------
On **3 Sep 2026** the nightly chain started at 18:45 and was killed around 19:55 by a deploy that
restarted the worker mid-chain. `reap_abandoned_runs` did exactly its job fifteen minutes later:
run 25 was marked ``failed`` and an alert was raised. Then nothing happened at all. The nightly is
a once-a-day Beat entry, a failed run has never had a retry, and so the product served the 2 Sep
session to everybody until a person noticed the following night and asked for a re-run by hand.

That is the third time the same shape has cost sessions:

* **18-29 Aug** — the bar step could only speak to Kite, so every night wrote zero bars
  (`test_pipeline_self_sufficiency` is the record).
* **31 Aug** — three deploys in one evening redelivered the nightly, and the copies woke past
  midnight and ran against a session that had not happened.
* **3 Sep** — a deploy killed the run and nothing re-ran it.

Each fix so far has made one *failure* less likely. None of them made a *missed session* heal
itself, which is the property the operator actually wants: nobody should have to notice.

WHAT "LANDED" MEANS
-------------------
One thing, and it is the same thing the product means by it: a ``pipeline_run`` row for that trade
date carrying a ``data_version``. Only ``publish`` sets that column, only a run that passed the
quality gate reaches ``publish``, and ``data_version`` is what the site serves. A run that failed,
was abandoned, or is still going has none — so it is not landed, whatever its status column says.

WHAT THIS MODULE IS NOT
-----------------------
It is not a retry loop around a *failed gate*. `baskfy.pipeline.nightly` is deliberately not
auto-retried, and that reasoning still holds: a day the gate refused is a day whose data is wrong,
and running it again on a timer would either publish the same broken day later or bury the alert.
This module re-runs a session that produced **no verdict at all** — killed, abandoned, never
attempted. A day the gate has refused will simply be refused again, loudly, once; that is the
correct outcome and the alert is the point.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import PipelineRun, TradingDay

__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_MAX_SESSIONS",
    "SESSION_DATA_READY_IST",
    "landed_sessions",
    "unlanded_sessions",
]

#: NSE publishes the day's bhavcopy well after the 15:30 close, so before this hour there is
#: nothing to ingest for today and nothing to gate. Lives here rather than in the task module
#: because two callers now need it and a constant with two definitions is a constant with two
#: values; `baskfy_worker.tasks.celery_tasks` imports it from here.
SESSION_DATA_READY_IST: Final = dt.time(18, 0)

#: How far back a sweep looks. A week covers a long weekend plus the two days it takes anybody to
#: notice, and stops the sweep from proposing to re-run a quarter after a database restore.
DEFAULT_LOOKBACK_DAYS: Final = 7

#: How many sessions one invocation will run. The chain takes about two hours, so two is already
#: most of a night; the rest are reported and picked up by the next trigger rather than silently
#: queued behind a worker slot nobody can see.
DEFAULT_MAX_SESSIONS: Final = 2


async def landed_sessions(session: AsyncSession, start: dt.date, end: dt.date) -> set[dt.date]:
    """The trade dates in ``[start, end]`` that have a published run."""
    rows = await session.execute(
        select(PipelineRun.trade_date)
        .where(
            PipelineRun.trade_date >= start,
            PipelineRun.trade_date <= end,
            PipelineRun.data_version.is_not(None),
        )
        .distinct()
    )
    return set(rows.scalars().all())


async def unlanded_sessions(
    session: AsyncSession,
    *,
    through: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    now: dt.datetime | None = None,
) -> list[dt.date]:
    """Trading days in the window that were never published, oldest first.

    ``now`` is the IST moment the caller is asking at. Today is excluded until
    :data:`SESSION_DATA_READY_IST`, for the same reason the nightly task refuses to run before it:
    a session that has not published its bhavcopy is not a session we are missing, it is one that
    has not happened yet, and treating the two alike is how an alert becomes noise.

    The calendar is the source for "was there a session", never the weekday — 28 Aug 2026 was a
    Friday and shut (`ops.is_trading_day` carries the full reasoning). A date the calendar does not
    carry is not proposed: absent means the far future or a gap, and in both cases not running is
    the safe answer.
    """
    if lookback_days < 0:
        raise ValueError(f"lookback_days must not be negative, got {lookback_days}")
    start = through - dt.timedelta(days=lookback_days)

    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.date >= start,
            TradingDay.date <= through,
            TradingDay.is_trading_day.is_(True),
        )
        .order_by(TradingDay.date.asc())
    )
    sessions = list(rows.scalars().all())
    landed = await landed_sessions(session, start, through)

    moment = now
    return [day for day in sessions if day not in landed and _has_had_time_to_publish(day, moment)]


def _has_had_time_to_publish(day: dt.date, now: dt.datetime | None) -> bool:
    """False for today before the data can exist. Any past session has had its chance."""
    if now is None:
        return True
    if day < now.date():
        return True
    if day > now.date():
        return False
    return now.time() >= SESSION_DATA_READY_IST
