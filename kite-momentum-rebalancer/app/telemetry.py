"""Observability over the order path — M20 §1.

Everything from a plan being built to an order reaching the broker has had `journalctl` and
nothing else. This adds three views of the same path, each independently optional:

* **spans** (OpenTelemetry) — the shape and duration of one rebalance, including which guard
  refused what;
* **metrics** (Prometheus) — counters and histograms that an alert rule can evaluate;
* **errors** (Sentry) — the exception, with the plan id attached, rather than a line in a log
  nobody is tailing at 09:20.

DEGRADING TO NOTHING IS A FEATURE, NOT A FALLBACK
---------------------------------------------------
The Mumbai box runs a live trading desk on a small instance. None of these three libraries is
installed there and none is required: every import is guarded, and when a library is absent the
corresponding helper becomes a no-op that costs an attribute lookup. **Observability must never be
the reason an order does not get placed**, so nothing here can raise into the order path — a
failure to record is not a failure to trade.

That is also why it is off unless configured. `OTEL_EXPORTER_OTLP_ENDPOINT`, `SENTRY_DSN` and
`METRICS_PORT` each switch on one view; absent, the desk behaves exactly as it did before this
file existed, which is what `tests/test_telemetry.py` asserts.

WHAT IS DELIBERATELY NOT RECORDED
-----------------------------------
No symbol quantities, no prices, no NAV, and no access token. A span attribute travels to a
third-party collector, and the desk's own journal already holds the full detail locally under the
operator's control. What leaves is shape: how many orders, how long, which guard, which outcome.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from typing import Any

log = logging.getLogger(__name__)

OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
METRICS_PORT = os.getenv("METRICS_PORT", "")
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "momentum-desk")

_tracer: Any = None
_metrics: dict[str, Any] = {}
_installed = False


def install() -> dict[str, bool]:
    """Turn on whichever views are both configured and available. Safe to call more than once."""
    global _installed, _tracer  # noqa: PLW0603 — one process, one provider; that is what these are
    if _installed:
        return {"traces": _tracer is not None, "metrics": bool(_metrics), "errors": False}
    _installed = True
    enabled = {"traces": False, "metrics": False, "errors": False}

    if OTLP_ENDPOINT:
        try:
            from opentelemetry import trace  # noqa: PLC0415
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
            from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
            from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

            provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(provider)
            _tracer = trace.get_tracer(SERVICE_NAME)
            enabled["traces"] = True
        except Exception as exc:
            log.warning("tracing not installed: %s", exc)

    if METRICS_PORT:
        try:
            from prometheus_client import Counter, Histogram, start_http_server  # noqa: PLC0415

            _metrics.update(
                {
                    "plans": Counter("desk_plans_built_total", "Plans built", ["source"]),
                    "orders": Counter(
                        "desk_orders_total", "Orders leaving the gateway", ["action", "outcome"]
                    ),
                    "refusals": Counter(
                        "desk_guard_refusals_total", "Orders a guard refused", ["guard"]
                    ),
                    "gtt": Counter("desk_gtt_total", "GTT operations", ["outcome"]),
                    "execute_seconds": Histogram(
                        "desk_execute_seconds", "Wall time of one execute batch"
                    ),
                    "batch_size": Histogram(
                        "desk_execute_orders",
                        "Orders in one execute batch",
                        buckets=(1, 2, 5, 10, 20, 50, 100),
                    ),
                }
            )
            start_http_server(int(METRICS_PORT))
            enabled["metrics"] = True
        except Exception as exc:
            log.warning("metrics not installed: %s", exc)

    if SENTRY_DSN:
        try:
            import sentry_sdk  # noqa: PLC0415

            sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.0, send_default_pii=False)
            enabled["errors"] = True
        except Exception as exc:
            log.warning("error capture not installed: %s", exc)

    return enabled


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:  # noqa: ANN401 — span attrs
    """One span, or nothing at all.

    The `nothing at all` branch is the one that runs on the box, so it must be cheap and it must
    never raise. A `with span(...)` around an order placement that threw because a collector was
    unreachable would be observability causing the outage it exists to explain.
    """
    if _tracer is None:
        yield None
        return
    try:
        with _tracer.start_as_current_span(name) as current:
            for key, value in attributes.items():
                current.set_attribute(key, value)
            yield current
    except Exception:
        # The span failed, not the work. Re-running the body is not an option, so this can only
        # be reached by a tracer error *before* the body ran; letting it through would abort a
        # rebalance for a telemetry fault.
        log.debug("span %s failed; continuing untraced", name, exc_info=True)
        yield None


def count(metric: str, /, **labels: str) -> None:
    """Increment a counter if metrics are on. Never raises."""
    handle = _metrics.get(metric)
    if handle is None:
        return
    try:
        (handle.labels(**labels) if labels else handle).inc()
    except Exception:
        log.debug("metric %s failed", metric, exc_info=True)


def observe(metric: str, value: float) -> None:
    """Record into a histogram if metrics are on. Never raises."""
    handle = _metrics.get(metric)
    if handle is None:
        return
    try:
        handle.observe(value)
    except Exception:
        log.debug("metric %s failed", metric, exc_info=True)


def capture(exc: BaseException, **context: Any) -> None:  # noqa: ANN401 — tag values
    """Send an exception to Sentry with context, if error capture is on. Never raises."""
    if not SENTRY_DSN:
        return
    try:
        import sentry_sdk  # noqa: PLC0415

        with sentry_sdk.push_scope() as scope:
            for key, value in context.items():
                scope.set_tag(key, str(value))
            sentry_sdk.capture_exception(exc)
    except Exception:
        log.debug("error capture failed", exc_info=True)
