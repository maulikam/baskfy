"""Structured JSON logging with a request id (Prompt 7 deliverable 1).

docs/02 §Observability sends logs to Loki, which indexes labels and greps the rest — so the line
has to be machine-parseable or the request id is useless. Every record carries the id of the
request that produced it, taken from a context variable that the middleware sets, so a log
statement deep inside the screener does not have to be handed one to be traceable.

``local`` keeps plain text (``BASKFY_LOG_JSON=false``), because a developer reads it directly.

Redaction (Prompt 12 deliverable 2)
-----------------------------------
docs/11 §Security's PII inventory and Prompt 12's second acceptance criterion — "no password or
token value is ever written to logs" — are enforced here, at the formatter, rather than trusted to
every call site. :class:`RedactingFilter` runs on the root handler and replaces the value of any
field whose *name* looks secret, and any bearer token, `password=` pair or long opaque string it
finds in the message text.

It is a backstop, not the design. The design is that `baskfy_api.security` and the auth service
never pass a secret to a logger; this catches the day someone adds `extra={"body": payload}` to
debug something and forgets to take it out.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid
from contextvars import ContextVar
from typing import Final

#: Set by ``RequestContextMiddleware`` for the lifetime of one request.
request_id_var: ContextVar[str | None] = ContextVar("baskfy_request_id", default=None)

REQUEST_ID_HEADER: Final = "X-Request-Id"

#: ``logging.LogRecord``'s own attributes. Anything else on a record was put there by us with
#: ``extra=``, and belongs in the JSON line.
_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info", "taskName",
        "thread", "threadName",
    }
)  # fmt: skip


#: Field names whose value is never printed, whatever it is. Matched case-insensitively on a
#: substring, so `password`, `new_password`, `password_hash` and `otp_code` are all covered.
_SECRET_FIELD_MARKERS: Final[tuple[str, ...]] = (
    "password",
    "token",
    "secret",
    "authorization",
    "cookie",
    "otp",
    "code",
    "api_key",
    "apikey",
    "credential",
    "signature",
)

#: Field names that contain a marker but are not secrets. Without these, "no rows: token_count=0"
#: would be redacted and a reader would learn nothing.
_SECRET_FIELD_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {"status_code", "token_count", "error_code", "problem_code", "reason_code"}
)

REDACTED: Final = "[redacted]"

#: Patterns in free-text messages. Anything long enough to be a credential is replaced, which will
#: occasionally hide something innocent — the right trade for a log that must not carry secrets.
_MESSAGE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # `Bearer eyJ...` anywhere in the line.
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    # `password=hunter2`, `token: abc...`, `otp="123456"` — key, separator, value.
    re.compile(
        r"(?i)\b([a-z_]*(?:password|token|secret|otp|api[_-]?key)[a-z_]*)"
        r"(\s*[=:]\s*)[\"\']?[^\s\"\',;)}]+"
    ),
    # A bare JWT: three base64url segments separated by dots.
    re.compile(r"\beyJ[A-Za-z0-9._\-]{16,}"),
)


def _is_secret_field(name: str) -> bool:
    if name in _SECRET_FIELD_ALLOWLIST:
        return False
    lowered = name.lower()
    return any(marker in lowered for marker in _SECRET_FIELD_MARKERS)


def redact_text(message: str) -> str:
    """Replace anything in a free-text log message that looks like a credential."""
    redacted = _MESSAGE_PATTERNS[0].sub(rf"\1 {REDACTED}", message)
    redacted = _MESSAGE_PATTERNS[1].sub(rf"\1\2{REDACTED}", redacted)
    return _MESSAGE_PATTERNS[2].sub(REDACTED, redacted)


class RedactingFilter(logging.Filter):
    """Strips secrets from every record before a formatter ever sees it.

    A filter rather than a formatter wrapper, because filters run on the *record* — so the
    redacted values are gone whichever formatter is installed, and a second handler added later
    cannot accidentally print the unredacted original.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED or key.startswith("_"):
                continue
            if _is_secret_field(key) and value is not None:
                record.__dict__[key] = REDACTED

        # `record.msg` may be a format string with `record.args` — redact the rendered result and
        # drop the args, so the formatter has nothing left to interpolate a secret into.
        rendered = record.getMessage()
        cleaned = redact_text(rendered)
        if cleaned != rendered or record.args:
            record.msg = cleaned
            record.args = None
        return True


def new_request_id() -> str:
    return uuid.uuid4().hex


def current_request_id() -> str | None:
    return request_id_var.get()


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the request id folded in when there is one."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = current_request_id()
        if request_id is not None:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable, with the request id prefixed so a tail is still traceable."""

    def format(self, record: logging.LogRecord) -> str:
        request_id = current_request_id()
        prefix = f"[{request_id[:8]}] " if request_id else ""
        return f"{record.levelname:<8} {prefix}{record.name}: {record.getMessage()}"


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Install one handler on the root logger, replacing whatever was there.

    Replacing rather than adding: uvicorn installs its own handlers, and leaving them in place
    prints every line twice — once structured and once not, which is worse than either.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    # Prompt 12's second acceptance criterion, installed once rather than remembered at every
    # call site. See `RedactingFilter`.
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    for noisy in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(noisy)
        logger.handlers.clear()
        logger.propagate = True
