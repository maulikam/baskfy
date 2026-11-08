"""Sentry wiring, shared by all three Python runtimes (PROMPTS.md Prompt 17 deliverable 1).

docs/02 §Observability: "OpenTelemetry -> Grafana/Tempo/Loki, **Sentry** for errors."

One module, three callers: :func:`baskfy_api.app.create_app`, the Celery worker's
``worker_process_init`` hook, and the restore-drill CLI. The alternative — an ``init`` per
runtime — is three places for the same four decisions to drift apart.

What is decided here, and why
-----------------------------
**Off unless a DSN is configured.** ``sentry_sdk.init()`` with no DSN is a no-op that still
installs every integration, so the honest default is not to call it at all. A laptop and the test
suite therefore carry no Sentry machinery, and `network_guard.py` never has to allow an egress it
would otherwise have to block.

**PII is never sent.** ``send_default_pii=False`` is the SDK default and is set explicitly
because docs/11 §Security keeps a PII inventory (email, name, payment metadata) and an exception
report is the easiest place for one of them to escape. :func:`_scrub` goes further and runs the
same redaction the logs use (``baskfy_api.logging.redact_text``) over every event's message and
its request headers, cookies and body — so a `password=` in a query string never leaves the box
even if a future integration decides to attach one.

**The trace id is shared with OpenTelemetry.** Sentry's own performance tracing is left off
(``traces_sample_rate=0``): docs/02 sends traces to Tempo, and two tracers sampling the same
request independently produce two disagreeing pictures of it. Instead the active OTel span's
trace id is attached as a tag, so an exception in Sentry links to the trace in Tempo.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from baskfy_api.logging import REDACTED, redact_text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sentry_sdk.types import Event, Hint

    from baskfy_api.settings import Settings

log = logging.getLogger(__name__)

__all__ = ["SENTRY_TRACE_TAG", "configure_sentry", "sentry_is_active"]

#: The tag that joins a Sentry issue to a Tempo trace. Named for the OTel field it carries.
SENTRY_TRACE_TAG: Final = "otel.trace_id"

#: Request members that are dropped or redacted wholesale before an event is sent. Sentry's ASGI
#: integration attaches ``request`` when ``send_default_pii`` is on; it is off, but a future
#: integration or an explicit ``set_context`` could still put one there.
_REQUEST_SECRET_KEYS: Final[tuple[str, ...]] = ("headers", "cookies", "data", "env")

#: Whether :func:`configure_sentry` actually initialised the SDK in this process. Module state
#: rather than a settings read, because the answer depends on what init did, not on what was asked.
_active = False


def sentry_is_active() -> bool:
    """Whether this process is reporting to Sentry. Read by ``/metrics`` and by the admin page."""
    return _active


def _scrub_value(value: object) -> object:
    """Redact anything string-shaped, recursively, using the log redactor."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return _scrub_mapping(value)
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    return value


def _scrub_mapping(mapping: dict[str, object]) -> dict[str, object]:
    """A dict with every secret-looking key replaced and every other value scrubbed in place."""
    return {
        key: (REDACTED if _looks_secret(str(key)) else _scrub_value(val))
        for key, val in mapping.items()
    }


def _looks_secret(name: str) -> bool:
    lowered = name.lower()
    return any(
        marker in lowered
        for marker in (
            "password",
            "token",
            "secret",
            "authorization",
            "cookie",
            "api-key",
            "api_key",
        )
    )


def _scrub(event: Event, _hint: Hint) -> Event:
    """``before_send``: the last thing that touches an event before it goes on the wire.

    Typed against ``sentry_sdk.types`` rather than ``dict[str, object]`` so that mypy checks the
    hook against the signature the SDK will actually call it with.
    """
    message = event.get("message")
    if isinstance(message, str):
        event["message"] = redact_text(message)

    request = event.get("request")
    if isinstance(request, dict):
        for key in _REQUEST_SECRET_KEYS:
            if key in request:
                request[key] = _scrub_value(request[key])
        query = request.get("query_string")
        if isinstance(query, str):
            request["query_string"] = redact_text(query)

    extra = event.get("extra")
    if isinstance(extra, dict):
        # `_scrub_mapping` rather than `_scrub_value` so the declared type is a mapping: Sentry's
        # `Event` TypedDict pins `extra` to `MutableMapping[str, object]`, and a hook that hands it
        # a bare `object` is a hook mypy cannot check.
        event["extra"] = _scrub_mapping(extra)
    return event


def configure_sentry(settings: Settings, *, service: str) -> bool:
    """Initialise Sentry for this process. Returns whether it was switched on.

    ``service`` names the runtime (``baskfy-api``, ``baskfy-worker``) and becomes the
    ``server_name`` tag, because the three runtimes share one project and "which process raised
    this" is the first question anyone asks.
    """
    global _active  # noqa: PLW0603 - one process-wide SDK, initialised once
    if not settings.sentry_dsn:
        return False

    # Imported lazily for the same reason the OTLP exporter is: a deployment with no DSN should
    # not pay the import, and the test suite should not carry the SDK's monkeypatching.
    import sentry_sdk  # noqa: PLC0415

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        release=settings.release or None,
        server_name=service,
        # docs/11 §Security's PII inventory. Explicit rather than defaulted; see the module
        # docstring.
        send_default_pii=False,
        # Traces go to Tempo (docs/02). Sentry reports exceptions and nothing else.
        traces_sample_rate=0.0,
        sample_rate=settings.sentry_sample_rate,
        before_send=_scrub,
    )
    _active = True
    log.info("sentry enabled", extra={"service": service, "environment": settings.environment})
    return True


def capture(exc: BaseException, **tags: str) -> None:
    """Report an exception if Sentry is on; do nothing at all if it is not.

    Used by the places that handle an exception rather than letting it propagate — the pipeline
    orchestrator, the alert dispatcher — where the SDK's automatic capture never sees it.
    """
    if not _active:
        return
    import sentry_sdk  # noqa: PLC0415

    with sentry_sdk.new_scope() as scope:
        for key, value in tags.items():
            scope.set_tag(key, value)
        trace_id = _current_trace_id()
        if trace_id is not None:
            scope.set_tag(SENTRY_TRACE_TAG, trace_id)
        sentry_sdk.capture_exception(exc)


def _current_trace_id() -> str | None:
    """The active OpenTelemetry trace id as 32 hex characters, or ``None`` when not tracing."""
    from opentelemetry import trace  # noqa: PLC0415

    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")
