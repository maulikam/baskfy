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


def install(*, metrics_port: int | None = None) -> dict[str, bool]:
    """Turn on whichever views are both configured and available. Safe to call more than once.

    ``metrics_port`` overrides ``METRICS_PORT`` for a second process on the same box — the
    swing monitor (SW11) runs beside the desk server and cannot bind the same port twice.
    """
    global _installed, _tracer  # noqa: PLW0603 — one process, one provider; that is what these are
    if _installed:
        return {"traces": _tracer is not None, "metrics": bool(_metrics), "errors": False}
    _installed = True
    enabled = {"traces": False, "metrics": False, "errors": False}
    port = metrics_port if metrics_port is not None else (int(METRICS_PORT) if METRICS_PORT else 0)

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

    if port:
        try:
            from prometheus_client import (  # noqa: PLC0415
                Counter,
                Gauge,
                Histogram,
                start_http_server,
            )

            _metrics.update(
                {
                    **swing_metrics(Counter, Gauge, Histogram),
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
            start_http_server(port)
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


def swing_metrics(counter: Any, gauge: Any, histogram: Any) -> dict[str, Any]:  # noqa: ANN401
    """The swing book's metrics (SW11, docs/swing/06; STANDING-ANSWERS B8), by the names
    ``infra/prometheus/alerts.yml`` and the desk suite read. Built from the constructors handed
    in so the registry is only touched when metrics are on."""
    return {
        "swing_signals": counter(
            "desk_swing_signals_total", "Opening-range verdicts the monitor raised", ["state"]
        ),
        "swing_confirms": counter(
            "desk_swing_confirms_total", "Swing lines confirmed, by kind and outcome",
            ["kind", "outcome"],
        ),
        "swing_notifications": counter(
            "desk_swing_notifications_total", "Swing signal notices, by channel and outcome",
            ["channel", "outcome"],
        ),
        "swing_sweeps": counter(
            "desk_swing_sweeps_total", "The 10:45 cutoff and the 15:15 GTT sweep, by outcome",
            ["sweep", "outcome"],
        ),
        "swing_verdict_seconds": histogram(
            "desk_swing_verdict_seconds", "Tick to verdict, in-process (budget 5 ms)",
            buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.05, 0.1),
        ),
        "swing_confirm_seconds": histogram(
            "desk_swing_confirm_seconds",
            "One swing confirm: lock, re-size, order, GTT (budget 2 s)",
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 12.0),
        ),
        "swing_monitor_up": gauge(
            "desk_swing_monitor_up", "1 while the opening-range monitor loop is running"
        ),
        "swing_quote_polls": counter(
            "desk_swing_quote_polls_total", "Quote fallback calls the monitor made", ["outcome"]
        ),
    }


def gauge(metric: str, value: float, /, **labels: str) -> None:
    """Set a gauge if metrics are on. Never raises."""
    handle = _metrics.get(metric)
    if handle is None:
        return
    try:
        (handle.labels(**labels) if labels else handle).set(value)
    except Exception:
        log.debug("metric %s failed", metric, exc_info=True)


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
        scope = _tracer.start_as_current_span(name)
        current = scope.__enter__()
        for key, value in attributes.items():
            current.set_attribute(key, value)
    except Exception:
        # The span failed, not the work: a tracer error *before* the body ran. Letting it
        # through would abort a rebalance for a telemetry fault, so the body runs untraced.
        log.debug("span %s failed; continuing untraced", name, exc_info=True)
        yield None
        return
    # The body's own exception must come back out as itself — SW11: an HTTPException(400)
    # from the confirm path must not become a RuntimeError because a span sat around it. The
    # span is closed with the body's exception info and its own failure to close is dropped.
    try:
        yield current
    except BaseException as exc:
        try:
            scope.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            log.debug("span %s did not close cleanly", name, exc_info=True)
        raise
    else:
        try:
            scope.__exit__(None, None, None)
        except Exception:
            log.debug("span %s did not close cleanly", name, exc_info=True)


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
