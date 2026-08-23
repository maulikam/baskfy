"""Running a backtest in the API process, because that is what the work is worth (M42).

## Why this exists

A backtest was a Celery job. `POST /backtests` published `baskfy.backtest.run` to Redis and a
worker picked it up. That design assumed a long computation, and the assumption was never checked
against the thing it was managing.

Measured, against nine years of real history, for the nine-rebalance quarterly run that was
actually sitting in the queue:

    calendar + schedule       38 ms
    screens (9 runs)          67 ms      7 ms each
    bars (46,634 rows)       131 ms
    benchmark, metadata        8 ms
    simulate                   7 ms
    -------------------------------
    total                    376 ms

The longest run this product can produce — nine years, monthly, with the fragility probe — is
about **two seconds**, nearly all of it waiting on Postgres. docs/11 budgets ten.

So the transport was three orders of magnitude heavier than the payload: a message broker, a
routing table, a second process and an operator to watch it, to manage four hundred milliseconds.

And its failure mode was not theoretical. A run sat at ``queued`` for hours because no
``BASKFY_REDIS_URL`` reached the API, so `build_task_queue` returned ``None``, so nothing was ever
published — and the only trace was a log line nobody was reading. The page said "queued" and meant
"never". Separately, the producer builds a bare Celery app with **no ``task_routes``**, so even
with a broker it would have published to the default queue rather than to ``backtest``. Three
independent faults, none of which the user could see.

## What this does instead

Runs it here, immediately, in a bounded pool of asyncio tasks. Postgres is the only thing it waits
on, and the event loop is free while it does.

## What it deliberately does not do

**It does not know what a backtest is.** The callable is injected. `baskfy_worker` imports
`baskfy_api`, so this module importing the worker would close a cycle; instead the composition
root — `app.lifespan` — supplies the job at startup, which is the one place allowed to know about
everything.

**It does not retry.** A backtest is deterministic given its data version: a run that failed
because the data was not there fails identically the second time, and one that failed on a bug
should surface the bug. Retrying would only hide which.

**It does not survive a restart.** A run in flight when the process stops is lost. That is the
bargain durability-by-broker was buying, and at two seconds a run it is not worth the machinery —
`drain()` finishes what is in flight on a clean shutdown, and a row left `running` by an unclean
one is visible as a row that never finished.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

log = logging.getLogger(__name__)

__all__ = ["DEFAULT_BACKLOG", "DEFAULT_CONCURRENCY", "BacktestRunner", "RunnerBusy"]

#: How many backtests may be in flight at once in one API process.
#:
#: Two rather than one: the work is nearly all awaited database time, so a second run costs almost
#: no CPU and halves the wait when somebody queues two. Not many more, because the simulation
#: itself is synchronous Python and a burst of those would block the loop every request shares.
DEFAULT_CONCURRENCY: Final = 2

#: The most runs that may be outstanding. A queue that grows without bound is a memory leak that
#: presents as a slow site; refusing at the door is something the caller can be told about.
DEFAULT_BACKLOG: Final = 32


class RunnerBusy(RuntimeError):
    """The backlog is full. The caller turns this into a refusal the user can read."""


class BacktestRunner:
    """A bounded pool of in-process runs.

    ``job`` is ``(public_id, fragility) -> None``, awaited. Whatever it raises is logged and
    dropped here: the job is responsible for writing the failure onto the row, because this class
    has no database session and no opinion about what a backtest is.
    """

    def __init__(
        self,
        job: Callable[[str, bool], Awaitable[None]],
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        backlog: int = DEFAULT_BACKLOG,
    ) -> None:
        self._job = job
        self._semaphore = asyncio.Semaphore(concurrency)
        self._backlog = backlog
        #: Strong references to running tasks. Without this the event loop may garbage collect a
        #: task nothing awaits and the run vanishes mid-flight — the documented asyncio footgun,
        #: and exactly the class of silent loss this module exists to stop having.
        self._running: set[asyncio.Task[None]] = set()

    @property
    def in_flight(self) -> int:
        return len(self._running)

    def submit(self, public_id: str, *, fragility: bool) -> None:
        """Schedule a run. Returns when it is scheduled, not when it finishes."""
        if len(self._running) >= self._backlog:
            raise RunnerBusy(
                f"{len(self._running)} backtests are already running in this process, which is "
                "the most it will hold. Try again once one has finished."
            )
        task = asyncio.create_task(self._job_wrapper(public_id, fragility))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _job_wrapper(self, public_id: str, fragility: bool) -> None:
        async with self._semaphore:
            try:
                await self._job(public_id, fragility)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The job writes its own failure onto the row; this is the backstop for the case
                # where it could not.
                log.exception("backtest run failed", extra={"public_id": public_id})

    async def drain(self, timeout: float = 30.0, cleanup_timeout: float = 5.0) -> None:
        """Let in-flight runs finish on shutdown, so a two-second job is not thrown away.

        Anything still going when ``timeout`` expires is cancelled **and then awaited**. The
        second half is the part that matters and the part this method did not do until M45.3.

        ``Task.cancel()` only *schedules* a ``CancelledError`` at the task's next suspension
        point; it does not deliver it, and it certainly does not run the task's cleanup. Measured:
        a run cancelled by this method was still in flight when ``drain`` returned, and its
        ``except`` block had not executed even after another pass of the event loop. So a clean
        shutdown — the case ``drain`` exists to handle well — left the row ``running`` forever,
        which is precisely the state a broker was supposed to be protecting us from.

        Awaiting the cancelled tasks gives each one its single delivery of ``CancelledError`` and
        lets it record a terminal state before the loop closes. ``cancel()`` is not called twice
        here, so a cleanup ``await`` inside the job runs normally rather than being cut short.

        A cleanup that hangs is bounded too: after ``cleanup_timeout`` the tasks are cancelled a
        second time, which does break the cleanup, and the survivors are logged by name. Shutdown
        that never finishes is worse than a row nobody could mark.
        """
        if not self._running:
            return
        done, pending = await asyncio.wait(set(self._running), timeout=timeout)
        for task in pending:
            task.cancel()
        stranded = 0
        if pending:
            _, stubborn = await asyncio.wait(pending, timeout=cleanup_timeout)
            stranded = len(stubborn)
            for task in stubborn:
                task.cancel()
                log.error("backtest task would not stop", extra={"task": task.get_name()})
        log.info(
            "backtest runner drained",
            extra={"finished": len(done), "cancelled": len(pending), "stranded": stranded},
        )
