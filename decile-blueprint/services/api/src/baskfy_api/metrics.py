"""Prometheus metrics (PROMPTS.md Prompt 17 deliverable 2).

    "Prometheus metrics + Grafana dashboards for: pipeline step durations, provider error rates,
     gate pass/fail, publish latency (EOD close -> data live), API latency by route, cache hit
     rate, queue depth."

docs/09 §Observability asks for the same list in fewer words: "Metrics: task duration p50/p95,
provider error rate, gate pass rate, publish latency (EOD close -> data live)."

One registry, two kinds of metric
---------------------------------
**Process counters** are incremented where the thing happens — an HTTP request finishing, a screen
cache hit, a provider retry — and are only meaningful for the process that recorded them. Both the
API and the worker expose their own.

**Database-derived gauges** are refreshed by :func:`refresh_pipeline_metrics` immediately before an
exposition is rendered. Nine of the ten pipeline steps run in a Celery worker that may not be the
process being scraped, and a worker that finished the run an hour ago may since have been
restarted — so the durable answer lives in ``pipeline_run_step``, and the scrape reads it. This is
the difference between "the pipeline step duration metric disappeared" and "the pipeline has not
run", which are very different pages at 3am.

Cardinality
-----------
``route`` is the FastAPI *route template* (``/api/v1/screens/{public_id}/run``), never the
resolved path. A metric labelled with the resolved path grows one time series per screen id and
takes Prometheus down with it. :func:`route_label` enforces that, and falls back to a single
``__unmatched__`` bucket rather than to the raw path.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Final

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import CollectorRegistry as _Registry
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from baskfy_core.models import PipelineRun, PipelineRunStep

log = logging.getLogger(__name__)

__all__ = [
    "CONTENT_TYPE",
    "REGISTRY",
    "UNMATCHED_ROUTE",
    "observe_cache",
    "observe_provider_call",
    "observe_request",
    "observe_step",
    "observe_swing_task",
    "refresh_pipeline_metrics",
    "refresh_swing_metrics",
    "render",
    "route_label",
]

#: Prometheus' text exposition content type, including the version parameter Prometheus expects.
CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

#: The label used for a request that matched no route — a 404, or a mount we did not register.
#: One bucket rather than one series per probed URL.
UNMATCHED_ROUTE: Final = "__unmatched__"

#: NSE's closing bell, in IST. docs/09 §Observability defines publish latency as "EOD close ->
#: data live", and docs/03 §"Nightly pipeline" times the chain from "NSE EOD files settle
#: ~18:00-19:00 IST" — so the *close*, not the file drop, is the clock's zero.
NSE_CLOSE_IST: Final = dt.time(15, 30)
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")

#: docs/11 §Reliability: "data published by 20:15 IST on >=95% of trading days."
PUBLISH_SLO_IST: Final = dt.time(20, 15)

#: Latency buckets, in seconds. docs/11's budgets are 150 ms warm / 800 ms cold for a screen run
#: and 500 ms for the dashboard, so the buckets are placed to make *those* boundaries readable in
#: a histogram_quantile rather than to be evenly spaced.
_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.4, 0.5, 0.8, 1.2, 2.0, 5.0, 10.0,
)  # fmt: skip

#: Step durations run from milliseconds to tens of minutes (docs/11: the whole chain < 45 min).
_STEP_BUCKETS: Final[tuple[float, ...]] = (
    0.1, 0.5, 1.0, 5.0, 15.0, 30.0, 60.0, 180.0, 300.0, 600.0, 1800.0, 2700.0,
)  # fmt: skip

#: Our own registry rather than ``prometheus_client.REGISTRY``. The default registry is global
#: process state that every library may write to; a dedicated one means the exposition contains
#: exactly what this module defines, and a test can assert on it without the interpreter's
#: garbage-collection metrics in the way.
REGISTRY: Final[CollectorRegistry] = _Registry(auto_describe=True)


# --- Process counters ------------------------------------------------------

REQUEST_DURATION: Final = Histogram(
    "baskfy_http_request_duration_seconds",
    "API latency by route (docs/11 §'Performance budgets').",
    labelnames=("method", "route", "status"),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

REQUESTS: Final = Counter(
    "baskfy_http_requests_total",
    "API requests by route and status. The error-rate alert divides 5xx by this.",
    labelnames=("method", "route", "status"),
    registry=REGISTRY,
)

SCREEN_CACHE: Final = Counter(
    "baskfy_screen_cache_events_total",
    "Screen result cache outcomes (docs/06 §Caching). Hit rate = hit / (hit + miss).",
    labelnames=("result",),
    registry=REGISTRY,
)

PROVIDER_CALLS: Final = Counter(
    "baskfy_provider_calls_total",
    "Provider outcomes by adapter (docs/09 §Observability: 'provider error rate').",
    labelnames=("provider", "outcome"),
    registry=REGISTRY,
)

STEP_DURATION: Final = Histogram(
    "baskfy_pipeline_step_duration_seconds",
    "Pipeline step wall time, recorded in the worker that ran it.",
    labelnames=("step", "status"),
    buckets=_STEP_BUCKETS,
    registry=REGISTRY,
)

ALERTS: Final = Counter(
    "baskfy_alerts_total",
    "Alerts raised, by name and severity (Prompt 17 deliverable 3).",
    labelnames=("alert", "severity"),
    registry=REGISTRY,
)


# --- Database-derived gauges ----------------------------------------------

LAST_STEP_DURATION: Final = Gauge(
    "baskfy_pipeline_last_step_duration_seconds",
    "Duration of each step of the most recent pipeline run, from pipeline_run_step.",
    labelnames=("step",),
    registry=REGISTRY,
)

LAST_STEP_ROWS: Final = Gauge(
    "baskfy_pipeline_last_step_rows_out",
    "rows_out of each step of the most recent pipeline run.",
    labelnames=("step",),
    registry=REGISTRY,
)

RUN_STATUS: Final = Gauge(
    "baskfy_pipeline_run_status",
    "1 for the status the most recent pipeline run is in, 0 for every other status.",
    labelnames=("status",),
    registry=REGISTRY,
)

GATE_PASSED: Final = Gauge(
    "baskfy_pipeline_gate_passed",
    "1 if the most recent run's data-quality gate passed, 0 if it failed; absent if it did not "
    "run at all.",
    registry=REGISTRY,
)

PUBLISH_LATENCY: Final = Gauge(
    "baskfy_publish_latency_seconds",
    "EOD close (15:30 IST) to data live, for the most recently published run.",
    registry=REGISTRY,
)

DATA_VERSION: Final = Gauge(
    "baskfy_data_version",
    "The currently published data_version (docs/03 step 10).",
    registry=REGISTRY,
)

LAST_PUBLISH_AGE: Final = Gauge(
    "baskfy_last_publish_age_seconds",
    "Seconds since the most recent successful publish.",
    registry=REGISTRY,
)

RUNNING_RUN_AGE: Final = Gauge(
    "baskfy_pipeline_running_age_seconds",
    "Age of the oldest pipeline run still in 'running'. 0 when none is. A worker killed "
    "mid-pipeline leaves exactly this behind, which is what the stale-run alert fires on.",
    registry=REGISTRY,
)

QUEUE_DEPTH: Final = Gauge(
    "baskfy_queue_depth",
    "Messages waiting on each Celery queue, read from the Redis broker.",
    labelnames=("queue",),
    registry=REGISTRY,
)

# --- The swing book (SW11, STANDING-ANSWERS B8) ----------------------------
#
# Database-derived, like the pipeline gauges above, and for the same reason: the desk and the
# worker are other processes, and the fact the rule wants ("is a position naked right now") is
# a row, not a counter in whichever process last touched it. Refreshed by
# :func:`refresh_swing_metrics` once per scrape from ``baskfy_api.swing_health``.

SWING_NAKED_POSITIONS: Final = Gauge(
    "baskfy_swing_naked_positions",
    "Swing positions with shares open and no resting GTT (SWING_POSITION_NAKED, "
    "SWING_GTT_MISSING_AT_1515).",
    registry=REGISTRY,
)

SWING_MONITOR_ENABLED: Final = Gauge(
    "baskfy_swing_monitor_enabled",
    "1 when BASKFY_SWING_MONITOR_ENABLED is true in this process's settings; the alert reads "
    "it beside monitor_ran_today. MD20: one env file feeds every service on the box.",
    registry=REGISTRY,
)

SWING_MONITOR_RAN_TODAY: Final = Gauge(
    "baskfy_swing_monitor_ran_today",
    "1 when an sw_session row for today (IST) has monitor_ran, written when the monitor handles "
    "its first tick (SWING_MONITOR_DID_NOT_START).",
    registry=REGISTRY,
)

SWING_OPEN_BUY_ORDERS_TODAY: Final = Gauge(
    "baskfy_swing_open_buy_orders_today",
    "BUY lines still SENT on today's plans — live orders the 10:45 cutoff should have closed "
    "(SWING_ORDER_OPEN_AFTER_CUTOFF).",
    registry=REGISTRY,
)

SWING_DETECT_RAN_FOR_PUBLISHED_DATE: Final = Gauge(
    "baskfy_swing_detect_ran_for_published_date",
    "1 when sw_market_daily carries the most recently published trade date (or nothing is "
    "published yet), 0 when the detect step has not run for it (SWING_DETECT_STALE).",
    registry=REGISTRY,
)

SWING_TASK_DURATION: Final = Histogram(
    "baskfy_swing_task_duration_seconds",
    "Wall time of the swing jobs (detect, premarket, eod, backtest), in the worker that ran "
    "them. The detect budget is 180 s for 2,500 names (STANDING-ANSWERS B9).",
    labelnames=("task", "status"),
    buckets=_STEP_BUCKETS,
    registry=REGISTRY,
)

#: Every status ``pipeline_run.status`` may hold, so :data:`RUN_STATUS` publishes a 0 for the
#: ones the last run is *not* in. A gauge that only ever appears for the current status makes
#: ``baskfy_pipeline_run_status{status="failed"} == 1`` an alert that never resolves.
_RUN_STATUSES: Final[tuple[str, ...]] = ("running", "succeeded", "failed", "aborted")


def route_label(request: Request) -> str:
    """The route *template* for a request, or :data:`UNMATCHED_ROUTE`.

    Starlette puts the matched ``APIRoute`` on the request scope, which is the only place the
    template is available after routing. See the module docstring on cardinality.
    """
    route = request.scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not isinstance(template, str) or not template:
        return UNMATCHED_ROUTE

    # The route object Starlette leaves on the scope is the *inner* one, so its template omits
    # whatever prefix it was mounted under: `/meta/status`, not `/api/v1/meta/status`. Left as-is
    # the label would collide the day a second API version mounts the same router — so the prefix
    # is recovered by segment count. Both strings start with "/", hence the -1.
    raw = request.scope.get("path")
    if isinstance(raw, str):
        raw_parts = raw.split("/")
        template_parts = template.split("/")
        carried = len(template_parts) - 1
        if 0 < carried <= len(raw_parts):
            prefix = "/".join(raw_parts[: len(raw_parts) - carried])
            return f"{prefix}{template}"
    return template


def observe_request(*, method: str, route: str, status: int, duration_seconds: float) -> None:
    """Record one finished HTTP request."""
    labels = (method, route, str(status))
    REQUEST_DURATION.labels(*labels).observe(duration_seconds)
    REQUESTS.labels(*labels).inc()


def observe_cache(*, hit: bool) -> None:
    """Record one screen-cache lookup. Called from the one place that knows (``run_screen``)."""
    SCREEN_CACHE.labels("hit" if hit else "miss").inc()


def observe_provider_call(*, provider: str, outcome: str) -> None:
    """Record a provider call outcome: ``ok``, ``retry``, ``error`` or ``circuit_open``."""
    PROVIDER_CALLS.labels(provider, outcome).inc()


def observe_step(*, step: str, status: str, duration_seconds: float) -> None:
    """Record a pipeline step in the process that ran it."""
    STEP_DURATION.labels(step, status).observe(duration_seconds)


def publish_latency_seconds(trade_date: dt.date, finished_at: dt.datetime) -> float:
    """Seconds from the trade date's 15:30 IST close to ``finished_at``.

    Negative is impossible in practice and is not clamped: a negative reading means the run's
    ``finished_at`` predates the close it claims to be for, which is a bug worth seeing rather
    than a zero worth hiding.
    """
    close = dt.datetime.combine(trade_date, NSE_CLOSE_IST, tzinfo=IST)
    moment = finished_at if finished_at.tzinfo is not None else finished_at.replace(tzinfo=dt.UTC)
    return (moment - close).total_seconds()


async def refresh_pipeline_metrics(
    session: AsyncSession, *, now: dt.datetime | None = None
) -> None:
    """Re-read the durable pipeline facts into gauges. Called once per scrape.

    Deliberately a handful of small indexed queries rather than one join: a ``/metrics`` scrape
    happens every fifteen seconds forever, and it must not be the most expensive query the
    database serves.
    """
    moment = now or dt.datetime.now(tz=dt.UTC)

    latest = (
        await session.execute(
            select(PipelineRun)
            .order_by(PipelineRun.trade_date.desc(), PipelineRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    for status in _RUN_STATUSES:
        RUN_STATUS.labels(status).set(1 if latest is not None and latest.status == status else 0)

    LAST_STEP_DURATION.clear()
    LAST_STEP_ROWS.clear()
    if latest is not None:
        steps = (
            await session.execute(
                select(PipelineRunStep).where(PipelineRunStep.run_id == latest.id)
            )
        ).scalars()
        gate_status: str | None = None
        for row in steps:
            if row.duration_ms is not None:
                LAST_STEP_DURATION.labels(row.step).set(row.duration_ms / 1000.0)
            if row.rows_out is not None:
                LAST_STEP_ROWS.labels(row.step).set(row.rows_out)
            if row.step == "data_quality_gate":
                gate_status = row.status
        if gate_status is not None:
            GATE_PASSED.set(1 if gate_status in {"succeeded", "skipped"} else 0)

    published = (
        await session.execute(
            select(PipelineRun)
            .where(PipelineRun.data_version.is_not(None))
            .order_by(PipelineRun.data_version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if published is not None and published.data_version is not None:
        DATA_VERSION.set(published.data_version)
        if published.finished_at is not None:
            PUBLISH_LATENCY.set(
                publish_latency_seconds(published.trade_date, published.finished_at)
            )
            finished = published.finished_at
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=dt.UTC)
            LAST_PUBLISH_AGE.set((moment - finished).total_seconds())

    oldest_running = (
        await session.execute(
            select(func.min(PipelineRun.started_at)).where(PipelineRun.status == "running")
        )
    ).scalar_one_or_none()
    if oldest_running is None:
        RUNNING_RUN_AGE.set(0)
    else:
        started = oldest_running if oldest_running.tzinfo else oldest_running.replace(tzinfo=dt.UTC)
        RUNNING_RUN_AGE.set(max(0.0, (moment - started).total_seconds()))


def observe_swing_task(*, task: str, status: str, duration_seconds: float) -> None:
    """Record one swing job's wall time in the process that ran it. Never raises."""
    try:
        SWING_TASK_DURATION.labels(task, status).observe(duration_seconds)
    except Exception:  # a metric is never a reason a job fails (SW11, B8)
        log.debug("swing task metric failed", exc_info=True)


async def refresh_swing_metrics(
    session: AsyncSession, *, monitor_enabled: bool, now: dt.datetime | None = None
) -> None:
    """Re-read the swing book's five facts into gauges. Called once per scrape (SW11).

    ``monitor_enabled`` is this process's reading of ``BASKFY_SWING_MONITOR_ENABLED``: the
    desk's flag is what starts the monitor, and MD20 puts every service on one env file, so
    the API's copy is the same value — the runbook says to keep them together.
    """
    from baskfy_api.swing_health import read_swing_health  # noqa: PLC0415 - avoids a cycle

    health = await read_swing_health(session, now=now)
    SWING_NAKED_POSITIONS.set(health.naked_positions)
    SWING_MONITOR_ENABLED.set(1 if monitor_enabled else 0)
    SWING_MONITOR_RAN_TODAY.set(1 if health.monitor_ran_today else 0)
    SWING_OPEN_BUY_ORDERS_TODAY.set(health.open_buy_orders_today)
    SWING_DETECT_RAN_FOR_PUBLISHED_DATE.set(1 if health.detect_ran_for_published_date else 0)


async def refresh_queue_depth(broker: object, queues: tuple[str, ...]) -> None:
    """Read each Celery queue's length off the Redis broker.

    Celery's Redis transport stores a queue as a plain list keyed by the queue name, so ``LLEN``
    is the depth. An unreachable broker leaves the gauge at its previous value rather than
    reporting zero — "the broker is down" and "the queue is empty" must not look identical.
    """
    llen = getattr(broker, "llen", None)
    if not callable(llen):
        return
    for queue in queues:
        try:
            depth = await llen(queue)
        except Exception as exc:  # a scrape must never fail on a broker hiccup
            log.warning("queue depth unavailable", extra={"queue": queue, "error": str(exc)})
            continue
        if isinstance(depth, int):
            QUEUE_DEPTH.labels(queue).set(depth)


def render() -> bytes:
    """The Prometheus text exposition for :data:`REGISTRY`."""
    return generate_latest(REGISTRY)
