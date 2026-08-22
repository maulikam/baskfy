"""The backtest job — Prompt 15 deliverable 4.

    "Celery task on the `backtest` queue with progress events streamed to the client over SSE,
     a per-user concurrency cap of 1, and a global cap."

The task itself is the thin binding in ``baskfy_worker.tasks.celery_tasks``; everything it does
is here, in a coroutine that takes a session, so the acceptance tests can drive a whole backtest
in-process against a real database with no broker and no worker — the same arrangement the ten
nightly steps use, and for the same reason.

Progress
--------
Each frame is written twice: ``PUBLISH`` to the run's channel, for clients already listening,
and ``SET`` on a short-lived key, so a client that connects halfway through sees where the run
has got to instead of waiting for the next frame. ``baskfy_api.routers.backtests`` reads both.

The publish uses redis-py's **synchronous** client. The progress sink is a plain callable invoked
from inside :func:`baskfy_core.backtest.run_backtest`, which is synchronous by design (it is a
tight numeric loop, not an I/O pipeline), so there is no coroutine to await into. Fifteen
publishes over a fifteen-year run is not a latency budget worth restructuring the engine for.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from redis import Redis as SyncRedis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from baskfy_api.backtests import (
    ARTEFACTS,
    artefact_bytes,
    artefact_key,
    build_payload,
    events_channel,
    progress_frame,
    progress_key,
)
from baskfy_api.settings import Settings, get_settings
from baskfy_core.backtest import (
    BacktestConfig,
    BacktestError,
    BacktestProgress,
    DividendPolicy,
    ProgressSink,
)
from baskfy_core.models import Backtest, Screen
from baskfy_core.models.base import JsonObject
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_providers.archive import RawArchive
from baskfy_providers.errors import ArchiveError
from baskfy_providers.factory import build_archive
from baskfy_providers.settings import get_provider_settings
from baskfy_worker.backtest import execute_backtest, notes_for

log = logging.getLogger(__name__)

__all__ = [
    "PROGRESS_TTL_SECONDS",
    "BacktestNotRunnable",
    "RedisPublisher",
    "build_backtest_archive",
    "run_backtest_job",
]

#: How long the last progress frame outlives the run. Long enough for a browser that reconnects
#: after a laptop lid closes; short enough that finished runs do not accumulate in Redis.
PROGRESS_TTL_SECONDS: Final = 30 * 60

#: docs/04: ``queued|running|done|failed``.
STATUS_QUEUED: Final = "queued"
STATUS_RUNNING: Final = "running"
STATUS_DONE: Final = "done"
STATUS_FAILED: Final = "failed"


class BacktestNotRunnable(Exception):
    """The row is gone, or somebody else already took it."""


class Publisher(Protocol):
    """Where a progress frame goes. Narrow on purpose, so a test can pass a list."""

    def publish(self, public_id: str, payload: str) -> None: ...


class RedisPublisher:
    """Publishes to the run's channel and keeps the latest frame on a key."""

    def __init__(self, client: SyncRedis, ttl_seconds: int = PROGRESS_TTL_SECONDS) -> None:
        self._client = client
        self._ttl = ttl_seconds

    def publish(self, public_id: str, payload: str) -> None:
        try:
            self._client.set(progress_key(public_id), payload, ex=self._ttl)
            self._client.publish(events_channel(public_id), payload)
        except RedisError as exc:
            # A progress bar that stops moving is a worse experience than one that never
            # appeared, but neither is a reason to abandon a fifteen-year simulation.
            log.warning("backtest progress publish failed", extra={"error": str(exc)})

    def close(self) -> None:
        try:
            self._client.close()
        except RedisError:  # pragma: no cover - closing a broken client
            log.debug("closing the progress publisher failed")


def build_publisher(settings: Settings | None = None) -> RedisPublisher | None:
    resolved = settings or get_settings()
    try:
        return RedisPublisher(SyncRedis.from_url(resolved.redis_url))
    except (RedisError, ValueError) as exc:  # pragma: no cover - a malformed URL
        log.error("no progress publisher", extra={"error": str(exc)})
        return None


def build_backtest_archive(settings: Settings | None = None) -> RawArchive:
    """R2 when a bucket is configured, a directory otherwise — the same choice invoices make."""
    resolved = settings or get_settings()
    return build_archive(get_provider_settings(), local_root=Path(resolved.invoice_local_dir))


def _sink(public_id: str, publisher: Publisher | None) -> ProgressSink:
    def emit(event: BacktestProgress) -> None:
        if publisher is None:
            return
        publisher.publish(
            public_id,
            progress_frame(
                public_id,
                STATUS_RUNNING,
                stage=event.stage,
                completed=event.completed,
                total=event.total,
                as_of=event.as_of,
            ),
        )

    return emit


@dataclass(frozen=True, slots=True)
class JobOutcome:
    public_id: str
    status: str
    metrics_hash: str | None = None
    error: str | None = None
    trades: int = 0
    rebalances: int = 0

    def as_dict(self) -> JsonObject:
        return {
            "public_id": self.public_id,
            "status": self.status,
            "metrics_hash": self.metrics_hash,
            "error": self.error,
            "trades": self.trades,
            "rebalances": self.rebalances,
        }


def _migrated(stored: Mapping[str, object]) -> dict[str, object]:
    """Read a config written before M39 without changing what it computes (M42).

    Rows created before M39 carry ``dividends: "reinvest"``, which is now refused without a
    dividend schedule — so every backtest queued before that commit would fail on a policy the
    user never chose and cannot fix.

    Rewriting it to ``ignore`` is safe **because the numbers do not move**. Pre-M39 ``reinvest``
    marked the book to `ohlcv_daily.close` and credited nothing; that series is a price return
    (M28 applied the 47 share-count actions and not the 38 dividend-shaped ones), so the run it
    produced *was* a price return. Only the label was wrong, and `ignore` is the same arithmetic
    under the name it always deserved. The result is bit-identical.

    A read-time shim, not a data migration: the stored row keeps what it was written with, so
    nothing is rewritten under a user who may want to see what they originally asked for. It can
    be deleted once no `queued` row predates M39.
    """
    config = dict(stored)
    if config.get("dividends") == DividendPolicy.REINVEST.value:
        config["dividends"] = DividendPolicy.IGNORE.value
    return config


async def _claim(session: AsyncSession, public_id: str) -> Backtest:
    """Move the row from ``queued`` to ``running``, or refuse.

    ``FOR UPDATE`` so two workers handed the same message cannot both start it — Celery's
    ``acks_late`` makes a redelivery possible, and a fifteen-year simulation running twice is
    exactly the kind of waste a dedicated queue exists to avoid.
    """
    row = (
        await session.execute(
            select(Backtest).where(Backtest.public_id == public_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise BacktestNotRunnable(f"no backtest with public_id {public_id!r}")
    if row.status != STATUS_QUEUED:
        raise BacktestNotRunnable(
            f"backtest {public_id!r} is {row.status!r}, not {STATUS_QUEUED!r}"
        )
    row.status = STATUS_RUNNING
    row.error = None
    await session.flush()
    return row


async def _definition(
    session: AsyncSession, row: Backtest, config: BacktestConfig
) -> ScreenDefinition:
    """docs/10 §Config: "screen_public_id … or an inline definition"."""
    if row.screen_id is not None:
        screen = (
            await session.execute(select(Screen).where(Screen.id == row.screen_id))
        ).scalar_one_or_none()
        if screen is not None:
            return ScreenDefinition.model_validate(screen.definition)
    if config.screen_definition is not None:
        return ScreenDefinition.model_validate(config.screen_definition)
    raise BacktestError(
        "this backtest names neither a screen that still exists nor an inline definition"
    )


def _store(archive: RawArchive, public_id: str, payloads: dict[str, bytes]) -> list[str]:
    written: list[str] = []
    for artefact in ARTEFACTS:
        key = artefact_key(public_id, artefact)
        archive.put(key, payloads[artefact], content_type="text/csv")
        written.append(key)
    return written


async def run_backtest_job(
    session: AsyncSession,
    public_id: str,
    *,
    archive: RawArchive,
    publisher: Publisher | None = None,
    fragility: bool = True,
) -> JobOutcome:
    """Claim the row, simulate, store, and record. Failures land on the row, never in a log only.

    The whole thing is one transaction. If the artefacts cannot be written the row does not go to
    ``done``: a finished backtest whose trade log does not exist would be a result nobody can
    audit, which is the opposite of what docs/10 is for.
    """
    row = await _claim(session, public_id)
    sink = _sink(public_id, publisher)
    try:
        config = BacktestConfig.model_validate(_migrated(row.config))
        definition = await _definition(session, row, config)
        outcome = await execute_backtest(
            session, config, definition, fragility=fragility, progress=sink
        )
        payloads = artefact_bytes(outcome.result)
        _store(archive, public_id, payloads)
        stored = build_payload(
            public_id,
            config,
            outcome.result,
            outcome.fragility,
            extra_notes=notes_for(outcome.loaded, outcome.result),
        )
    except (BacktestError, ValueError, ArchiveError) as exc:
        row.status = STATUS_FAILED
        row.error = f"{type(exc).__name__}: {exc}"
        row.finished_at = dt.datetime.now(tz=dt.UTC)
        await session.flush()
        if publisher is not None:
            publisher.publish(
                public_id,
                progress_frame(
                    public_id,
                    STATUS_FAILED,
                    stage="failed",
                    completed=0,
                    total=1,
                    detail=row.error,
                ),
            )
        log.warning("backtest failed", extra={"public_id": public_id, "error": str(exc)})
        return JobOutcome(public_id, STATUS_FAILED, error=row.error)

    row.metrics = stored.metrics
    row.equity_curve = stored.equity_curve
    row.trades_key = stored.trades_key
    row.status = STATUS_DONE
    row.finished_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    if publisher is not None:
        publisher.publish(
            public_id,
            progress_frame(
                public_id,
                STATUS_DONE,
                stage="done",
                completed=1,
                total=1,
            ),
        )
    return JobOutcome(
        public_id,
        STATUS_DONE,
        metrics_hash=stored.metrics_hash,
        trades=len(outcome.result.trades),
        rebalances=len(outcome.result.rebalance_dates),
    )


def run_backtest_inline(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> Callable[[str, bool], Awaitable[None]]:
    """Adapt :func:`run_backtest_job` to the shape `BacktestRunner` wants (M42).

    The runner deliberately knows nothing about sessions, archives or publishers — it schedules an
    awaitable and bounds how many are in flight. This closure supplies those, and it lives in the
    worker package because that is where the job and its dependencies already are.

    One session per run, committed on the way out. A backtest is a single transaction by design
    (`run_backtest_job`: "If the artefacts cannot be written the row does not go to done"), and
    sharing the request's session would tie that transaction to an HTTP response already sent.
    """
    archive = build_backtest_archive(settings)
    publisher = build_publisher(settings)

    async def run(public_id: str, fragility: bool) -> None:
        async with session_factory() as session:
            try:
                await run_backtest_job(
                    session, public_id, archive=archive, publisher=publisher, fragility=fragility
                )
                await session.commit()
            except BacktestNotRunnable:
                # Somebody else claimed it, or it is not queued any more. Not an error.
                await session.rollback()
            except Exception:
                await session.rollback()
                # The job records failures on the row inside its own transaction, and that
                # transaction is what just rolled back. Do it in a fresh one, because a run that
                # ends as a silent `queued` is the exact state M42 exists to make impossible.
                await _mark_failed_out_of_band(session_factory, public_id)
                raise

    return run


async def _mark_failed_out_of_band(
    session_factory: async_sessionmaker[AsyncSession], public_id: str
) -> None:
    """Last resort: record the failure in its own transaction."""
    try:
        async with session_factory() as session:
            row = (
                await session.execute(select(Backtest).where(Backtest.public_id == public_id))
            ).scalar_one_or_none()
            if row is not None and row.status in {STATUS_QUEUED, STATUS_RUNNING}:
                row.status = STATUS_FAILED
                row.error = "the run stopped unexpectedly and nothing was recorded"
                row.finished_at = dt.datetime.now(tz=dt.UTC)
            await session.commit()
    except Exception:  # pragma: no cover - the database itself is gone
        log.exception("could not record backtest failure", extra={"public_id": public_id})
