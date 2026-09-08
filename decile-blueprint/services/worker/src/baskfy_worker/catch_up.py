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
attempted. A day the gate has refused is refused again, loudly, once; that is the correct outcome
and the alert is the point.

**That last sentence was a claim, not a mechanism, until 5 Sep 2026** — and the claim was false.
Nothing here distinguished "no verdict" from "the verdict was no", because both leave
``data_version`` null, so the sweep re-ran a refused day every time it fired: four full chains for
2026-09-04 over eighteen hours, all refused for the same uningested corporate action.
`gate_refused_sessions` is the mechanism that makes the paragraph true.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import PipelineRun, PipelineRunStep, TradingDay

__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_MAX_SESSIONS",
    "QUALITY_GATE_STEP",
    "SESSION_DATA_READY_IST",
    "STEP_FAILED",
    "gate_refused_sessions",
    "landed_sessions",
    "unlanded_sessions",
]

#: The step whose failure means "the chain reached a verdict and the verdict was no".
QUALITY_GATE_STEP: Final = "data_quality_gate"
STEP_FAILED: Final = "failed"

#: NSE publishes the day's bhavcopy well after the 15:30 close, so before this hour there is
#: nothing to ingest for today and nothing to gate. Lives here rather than in the task module
#: because two callers now need it and a constant with two definitions is a constant with two
#: values; `baskfy_worker.tasks.celery_tasks` imports it from here.
SESSION_DATA_READY_IST: Final = dt.time(18, 0)

#: WHY THIS IS NOT EARLIER, THOUGH KITE ANSWERS SOONER (8 Sep 2026 — M88 reverted).
#:
#: Maulik asked "shouldn't we have the latest data since we have the token?", and at 15:35 on
#: 7 Sep Kite did return that day's bars for RELIANCE, TCS and INFY. M88 concluded the cutoff
#: could move to 15:45. **That was generalised from three of the most liquid symbols in India
#: and it was wrong.** Kite's daily bars cover roughly 3,000 of this universe's ~4,855
#: instruments; the rest arrive only in NSE's bhavcopy, which is published well after the close.
#: The measured difference: 7 Sep ingested 3,056 bars at 15:37 and the quality gate refused it
#: (3,056 against a 10-day median of 3,524), while 4 Sep — the same chain with the bhavcopy
#: top-up — ingested 4,454 and published.
#:
#: So an early run does not produce an early day; it produces a **partial** day that the gate
#: correctly refuses, three times over. The token makes the liquid names available sooner, and
#: the live intraday scan (M85/SW24) is what already uses them. The published end-of-day series
#: waits for the bhavcopy because that is when the day is actually complete.

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


async def gate_refused_sessions(
    session: AsyncSession, start: dt.date, end: dt.date
) -> set[dt.date]:
    """Trade dates whose chain ran to a verdict and the verdict was **no**.

    THE BUG THIS EXISTS TO FIX (5 Sep 2026). This module's own docstring promised that it is
    "not a retry loop around a *failed gate*" and that "a day the gate has refused will simply be
    refused again, loudly, once". It was not once. `landed_sessions` asks only whether
    ``data_version`` is set, and a run the quality gate refused has none — exactly like a run that
    was killed or never attempted. So the sweep could not tell the two apart and re-ran the chain
    for a refused day every time it fired.

    On 4-5 Sep 2026 that ran the full chain **four times** for 2026-09-04 (runs 30-33, at 21:32,
    02:03, 06:45 and 17:54) because one instrument, TCC, had an uningested 1:5 split and tripped
    ``no_unexplained_jumps`` every single time. Nothing about re-running could have changed that:
    the gate was right, the data really was wrong, and the fix was a corporate action nobody was
    going to insert by running the same chain again. Hours of chain work, four alerts for one
    problem, and the box busy for no reason.

    A run has *reached a verdict* when it recorded a `data_quality_gate` step. If that verdict was
    a refusal, the day is not "missing" — it is **known bad**, and it needs a person, not a timer.
    """
    rows = await session.execute(
        select(PipelineRun.trade_date)
        .join(PipelineRunStep, PipelineRunStep.run_id == PipelineRun.id)
        .where(
            PipelineRun.trade_date >= start,
            PipelineRun.trade_date <= end,
            PipelineRun.data_version.is_(None),
            PipelineRunStep.step == QUALITY_GATE_STEP,
            PipelineRunStep.status == STEP_FAILED,
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
    # A day the gate has already refused is not a missing session — it is a known-bad one, and
    # re-running the chain cannot change the verdict. Excluded so the sweep stops looping on it.
    refused = await gate_refused_sessions(session, start, through)

    moment = now
    return [
        day
        for day in sessions
        if day not in landed and day not in refused and _has_had_time_to_publish(day, moment)
    ]


def _has_had_time_to_publish(day: dt.date, now: dt.datetime | None) -> bool:
    """False for today before the data can exist. Any past session has had its chance."""
    if now is None:
        return True
    if day < now.date():
        return True
    if day > now.date():
        return False
    return now.time() >= SESSION_DATA_READY_IST
