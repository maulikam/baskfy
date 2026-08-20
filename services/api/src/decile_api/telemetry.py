"""OpenTelemetry wiring (Prompt 7 deliverable 1).

docs/02 §Observability: "OpenTelemetry -> Grafana/Tempo/Loki, Sentry for errors."

Off unless ``DECILE_OTEL_ENABLED`` is set. Tracing every request on a laptop costs latency and
produces spans nobody reads, and a default-on exporter pointed at nothing logs a connection error
per span. Turning it on with no ``DECILE_OTEL_EXPORTER_OTLP_ENDPOINT`` installs the tracer and no
exporter, which is what you want in tests: spans are created and dropped, so instrumentation bugs
still surface without a collector.

The span's ``request_id`` attribute is the same id the JSON logs carry, so a trace in Tempo and a
line in Loki can be joined without a correlation id per subsystem.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from decile_api.settings import Settings

log = logging.getLogger(__name__)


def configure_telemetry(app: FastAPI, settings: Settings) -> None:
    """Install a tracer provider and instrument FastAPI + SQLAlchemy."""
    if not settings.otel_enabled:
        return

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "deployment.environment": settings.environment,
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
    trace.set_tracer_provider(provider)

    # Also deliberately lazy: importing the instrumentors patches library internals at import
    # time, and a process with telemetry switched off should not carry the patches.
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor  # noqa: PLC0415

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    engine = getattr(app.state, "engine", None)
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=provider)


def annotate_current_span(**attributes: str) -> None:
    """Attach request-scoped facts to whatever span is active, if any."""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    for key, value in attributes.items():
        span.set_attribute(key, value)
