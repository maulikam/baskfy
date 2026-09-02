"""Worker-side observability (PROMPTS.md Prompt 17 deliverables 1 and 2).

The API end of the trace is ``baskfy_api.telemetry``; this is the other end. A Celery worker is a
different process with a different lifecycle, so three things have to be arranged that the API
gets for free:

**Per-process initialisation.** Celery's prefork pool forks after the module is imported, so a
tracer provider (or a Sentry client, or a metrics HTTP server) built at import time is inherited
by every child and shares its batch queue and its socket across processes. Everything here is
installed on ``worker_process_init`` instead, which fires inside each child.

**A metrics endpoint per worker.** Prometheus scrapes processes, not services. Each worker child
would need its own port, which is unworkable — so ``prometheus_client``'s multiprocess mode is
used when ``PROMETHEUS_MULTIPROC_DIR`` is set, and a single exposition server runs on the parent.
Without that variable the server runs in whichever process starts first and the rest are silent,
which is honest for a solo-pool worker and clearly wrong for a prefork one; the runbook says so.

**Trace continuation.** ``CeleryInstrumentor`` reads the W3C ``traceparent`` the API's producer
wrote onto the message, so ``POST /backtests`` and the job that runs it are one trace.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Iterator
from typing import Final

from opentelemetry import trace

from baskfy_api.metrics import REGISTRY, observe_provider_call, observe_swing_task
from baskfy_api.sentry import configure_sentry
from baskfy_api.settings import Settings, get_settings
from baskfy_api.telemetry import annotate_current_span, build_provider
from baskfy_providers.retry import RetryHooks

log = logging.getLogger(__name__)

__all__ = [
    "WORKER_SERVICE_NAME",
    "install_worker_observability",
    "provider_retry_hooks",
    "start_metrics_server",
    "swing_span",
    "swing_timed",
]

#: What the worker calls itself in Sentry and in the OTel resource. Distinct from the API's
#: ``baskfy-api`` so "which runtime raised this" is answerable from the tag alone.
WORKER_SERVICE_NAME: Final = "baskfy-worker"

#: Where the worker's Prometheus exposition listens. A port rather than a path because a Celery
#: worker serves no HTTP of its own; 9101 sits just above the node exporter's 9100 by convention.
DEFAULT_METRICS_PORT: Final = 9101

_metrics_server_started = False


def start_metrics_server(port: int | None = None) -> bool:
    """Serve :data:`baskfy_api.metrics.REGISTRY` over HTTP. Returns whether it started.

    Idempotent: a second call in the same process is a no-op rather than an "address in use".
    A bind failure is logged and swallowed — a worker that refuses to start because its metrics
    port is taken is a worse outage than a worker nobody can scrape.
    """
    global _metrics_server_started  # noqa: PLW0603 - one listener per process
    if _metrics_server_started:
        return False
    from prometheus_client import start_http_server  # noqa: PLC0415

    configured = os.environ.get("BASKFY_WORKER_METRICS_PORT")
    chosen = port if port is not None else int(configured or DEFAULT_METRICS_PORT)
    try:
        start_http_server(chosen, registry=REGISTRY)
    except OSError as exc:
        log.warning("worker metrics server not started", extra={"port": chosen, "error": str(exc)})
        return False
    _metrics_server_started = True
    log.info("worker metrics server listening", extra={"port": chosen})
    return True


def install_worker_observability(settings: Settings | None = None) -> None:
    """Tracer provider, Celery + SQLAlchemy instrumentation, Sentry, metrics — in one call.

    Safe to call in a process where everything is switched off: it does nothing at all, which is
    what the test suite and a laptop get.
    """
    resolved = settings or get_settings()

    configure_sentry(resolved, service=WORKER_SERVICE_NAME)

    if resolved.otel_enabled:
        provider = build_provider(resolved, service_name=WORKER_SERVICE_NAME)
        trace.set_tracer_provider(provider)
        # Lazy for the same reason as in `baskfy_api.telemetry`: importing an instrumentor
        # patches library internals, and a worker with tracing off should not carry the patches.
        from opentelemetry.instrumentation.celery import CeleryInstrumentor  # noqa: PLC0415
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor  # noqa: PLC0415

        CeleryInstrumentor().instrument(tracer_provider=provider)
        # No engine argument: the worker builds a fresh engine per task
        # (`baskfy_worker.db.session_scope`), so the instrumentation has to be on the library
        # rather than on one instance of it.
        SQLAlchemyInstrumentor().instrument(tracer_provider=provider)

    if resolved.metrics_enabled:
        start_metrics_server()


def provider_retry_hooks() -> RetryHooks:
    """``RetryHooks`` that count provider failures (docs/09 §Observability: 'provider error rate').

    ``retry.py``'s ``on_retry`` docstring already names this as its purpose: "the hook Prompt 17
    hangs OpenTelemetry spans and the provider error-rate metric on." A retry is counted when the
    backoff is about to sleep, so ``baskfy_provider_calls_total{outcome="retry"}`` counts *failed
    attempts*, not failed operations — the operation's own outcome is recorded by the pipeline
    step that wrapped it.
    """

    def on_retry(attempt: int, delay: float, error: BaseException) -> None:
        provider = getattr(error, "provider", None) or "unknown"
        observe_provider_call(provider=str(provider), outcome="retry")
        annotate_current_span({"baskfy.provider": str(provider), "baskfy.retry_attempt": attempt})
        log.warning(
            "provider retry",
            extra={
                "provider": str(provider),
                "attempt": attempt,
                "delay_seconds": round(delay, 3),
                "error_type": type(error).__name__,
            },
        )

    return RetryHooks(on_retry=on_retry)


# --- SW11: spans and timings over the swing jobs, unable to raise into them ------------------
#
# `docs/swing/06` SW11: "Spans/metrics over detect, premarket, monitor, execute (M20 pattern,
# optional, unable to raise into the order path)." The OTel API's no-op tracer never raises, but
# a configured exporter, a sink replaced in a test, or a histogram with a broken label can — so
# both helpers catch everything at the call site, and ``tests/test_swing_telemetry.py`` hands
# them a tracer and a sink that raise and asserts the job still completes.


@contextlib.contextmanager
def swing_span(name: str, **attributes: str | int | float | bool) -> Iterator[None]:
    """One span around a swing job, or nothing at all — never an exception of its own.

    The span is opened and closed by hand rather than with ``with``, so that a tracer that
    fails to *open* leaves the body to run untraced, and a body that raises comes back out as
    its own exception with the span closed on it — never a second error from the closing.
    """
    scope: contextlib.AbstractContextManager[trace.Span] | None = None
    try:
        scope = trace.get_tracer(WORKER_SERVICE_NAME).start_as_current_span(name)
        span = scope.__enter__()
        for key, value in attributes.items():
            span.set_attribute(key, value)
    except Exception:
        log.debug("swing span %s unavailable", name, exc_info=True)
        scope = None
    try:
        yield None
    except BaseException as exc:
        if scope is not None:
            with contextlib.suppress(Exception):
                scope.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        if scope is not None:
            with contextlib.suppress(Exception):
                scope.__exit__(None, None, None)


@contextlib.contextmanager
def swing_timed(task: str) -> Iterator[None]:
    """Record the wall time of one swing job as ``baskfy_swing_task_duration_seconds``; the
    status is ``failed`` when the body raised, ``ok`` otherwise. Never raises itself."""
    started = time.perf_counter()
    status = "ok"
    try:
        yield None
    except BaseException:
        status = "failed"
        raise
    finally:
        try:
            observe_swing_task(
                task=task, status=status, duration_seconds=time.perf_counter() - started
            )
        except Exception:
            log.debug("swing task timing unavailable", exc_info=True)
