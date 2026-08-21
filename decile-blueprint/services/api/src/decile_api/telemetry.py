"""OpenTelemetry wiring (Prompt 7 deliverable 1; extended by Prompt 17 deliverable 1).

docs/02 §Observability: "OpenTelemetry -> Grafana/Tempo/Loki, Sentry for errors."

Off unless ``DECILE_OTEL_ENABLED`` is set. Tracing every request on a laptop costs latency and
produces spans nobody reads, and a default-on exporter pointed at nothing logs a connection error
per span. Turning it on with no ``DECILE_OTEL_EXPORTER_OTLP_ENDPOINT`` installs the tracer and no
exporter, which is what you want in tests: spans are created and dropped, so instrumentation bugs
still surface without a collector.

The span's ``request_id`` attribute is the same id the JSON logs carry, so a trace in Tempo and a
line in Loki can be joined without a correlation id per subsystem.

The chain Prompt 17 asks for
----------------------------
"OpenTelemetry traces across web -> API -> worker -> database, with ``trade_date``,
``rows_in``/``rows_out`` and screen definition hash as span attributes."

Each hop carries the W3C ``traceparent`` header, and each end of it is instrumented by the
library that owns the wire:

===========================  ==========================================================
web -> API                   ``@decile/api-client`` sets ``traceparent``;
                             ``FastAPIInstrumentor`` extracts it.
API -> worker                Celery message headers, written by ``CeleryInstrumentor`` in
                             ``decile_api.queue`` and read by the worker's own.
API/worker -> database       ``SQLAlchemyInstrumentor``.
===========================  ==========================================================

:data:`ATTRIBUTES` is the vocabulary. Every attribute this codebase sets is named there, prefixed
``decile.``, because an attribute key invented at a call site is one a Grafana query will not find.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from decile_api.settings import Settings

log = logging.getLogger(__name__)

#: What a span attribute may hold. OTel accepts more; this is what we set.
type AttributeValue = str | int | float | bool

#: The attribute vocabulary. PROMPTS.md Prompt 17 §1 names the first four by hand; the rest are
#: what the same spans need to be useful. Keys are stable — a dashboard depends on them.
ATTRIBUTES: Final[tuple[str, ...]] = (
    "decile.trade_date",
    "decile.rows_in",
    "decile.rows_out",
    "decile.screen_definition_hash",
    "decile.request_id",
    "decile.pipeline_step",
    "decile.pipeline_run_id",
    "decile.step_status",
    "decile.as_of",
    "decile.data_version",
    "decile.cache_hit",
    "decile.problem_type",
    "decile.provider",
    "decile.retry_attempt",
    "decile.instrument_id",
    "decile.backtest_id",
    "decile.alert",
)

TRACER_NAME: Final = "decile"


def get_tracer() -> trace.Tracer:
    """The one tracer this codebase opens spans on.

    Resolved per call rather than cached at import: ``trace.get_tracer`` returns a proxy that
    starts working the moment a provider is installed, so a module imported before
    :func:`configure_telemetry` still records.
    """
    return trace.get_tracer(TRACER_NAME)


def build_provider(settings: Settings, *, service_name: str) -> TracerProvider:
    """A tracer provider for one runtime, with an exporter only if one is configured."""
    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment": settings.environment,
            "service.version": settings.release or "unknown",
        }
    )
    provider = TracerProvider(resource=resource)
    if settings.otel_exporter_otlp_endpoint:
        # Imported here, not at module scope: the OTLP exporter is only needed when an endpoint
        # is configured, and it is the one OTel package a deployment without a collector should
        # not have to install.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
        )
    else:
        log.info("otel enabled with no exporter endpoint; spans are created and discarded")
    return provider


def configure_telemetry(app: FastAPI, settings: Settings) -> None:
    """Install a tracer provider and instrument FastAPI + SQLAlchemy."""
    if not settings.otel_enabled:
        return

    provider = build_provider(settings, service_name=settings.otel_service_name)
    trace.set_tracer_provider(provider)

    # Also deliberately lazy: importing the instrumentors patches library internals at import
    # time, and a process with telemetry switched off should not carry the patches.
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor  # noqa: PLC0415

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    engine = getattr(app.state, "engine", None)
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=provider)

    # The API is a Celery *producer* (`decile_api.queue`). Instrumenting it is what writes the
    # `traceparent` onto the message, and is therefore the whole of the API -> worker hop.
    # `opentelemetry-instrumentation-celery` ships no `py.typed`, like `celery` itself
    # (see the mypy override in the workspace pyproject) — hence the untyped-call exemption in
    # `[[tool.mypy.overrides]]` rather than a `type: ignore` here.
    from opentelemetry.instrumentation.celery import CeleryInstrumentor  # noqa: PLC0415

    CeleryInstrumentor().instrument(tracer_provider=provider)


def annotate_current_span(attributes: Mapping[str, AttributeValue]) -> None:
    """Attach request-scoped facts to whatever span is active, if any.

    Takes a mapping rather than keyword arguments because every attribute key is dotted
    (``decile.trade_date``) and therefore not a Python identifier — a ``**{...}`` splat at every
    call site is the same dictionary with more punctuation, and it defeats mypy's ability to check
    the value type.

    Silently does nothing when there is no recording span, which is the common case: telemetry is
    off by default, and a call site should not have to ask.
    """
    span = trace.get_current_span()
    if not span.is_recording():
        return
    for key, value in attributes.items():
        span.set_attribute(key, value)


def current_trace_id() -> str | None:
    """The active trace id as 32 hex characters, or ``None`` outside a trace.

    Logged alongside the request id so a Loki line points at a Tempo trace.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")


def traceparent_header() -> str | None:
    """The active span rendered as a W3C ``traceparent``, for a hop OTel does not instrument.

    Returns ``None`` when nothing is being traced, so a caller can simply omit the header.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    flags = "01" if context.trace_flags.sampled else "00"
    return f"00-{format(context.trace_id, '032x')}-{format(context.span_id, '016x')}-{flags}"
