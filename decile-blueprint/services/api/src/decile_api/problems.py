"""RFC 9457 ``application/problem+json`` — docs/07 §"Error catalogue" (Prompt 7 deliverable 1).

    "All responses are `application/json`; errors follow RFC 9457 `application/problem+json`."

Every row of docs/07's catalogue is a member of :class:`ProblemType`, and every error the service
can return goes through :class:`Problem`. Nothing raises a bare ``HTTPException`` with a string
body: a client that has to parse prose to find out what happened is a client that will guess.

``type`` is a relative URI reference, spelled exactly as docs/07's table spells it. RFC 9457 §3.1
allows that ("If the type URI is a relative reference, it is resolved against the document's base
URI"), and inventing an absolute namespace the document does not mention would make the wire
format disagree with the specification for no gain.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Final

CONTENT_TYPE: Final = "application/problem+json"


class ProblemType(StrEnum):
    """docs/07 §"Error catalogue", one member per row, in the document's order."""

    INVALID_SCREEN_DEFINITION = "invalid-screen-definition"
    UNAUTHENTICATED = "unauthenticated"
    PAYMENT_REQUIRED = "payment-required"
    NOT_FOUND = "not-found"
    STALE_DATA_VERSION = "stale-data-version"
    NO_TRADING_DAY = "no-trading-day"
    RATE_LIMITED = "rate-limited"
    PIPELINE_DEGRADED = "pipeline-degraded"
    #: Not in docs/07's table. Every service needs a terminal answer for "we broke", and
    #: returning HTML or a bare 500 body from a service that promises problem+json everywhere
    #: would be worse than naming it.
    INTERNAL_ERROR = "internal-error"


#: The status docs/07 pairs with each type. Kept beside the enum so a handler cannot pick a
#: status the document does not associate with the type it is reporting.
STATUS_FOR: Final[Mapping[ProblemType, int]] = {
    ProblemType.INVALID_SCREEN_DEFINITION: 400,
    ProblemType.UNAUTHENTICATED: 401,
    ProblemType.PAYMENT_REQUIRED: 402,
    ProblemType.NOT_FOUND: 404,
    ProblemType.STALE_DATA_VERSION: 409,
    ProblemType.NO_TRADING_DAY: 422,
    ProblemType.RATE_LIMITED: 429,
    ProblemType.PIPELINE_DEGRADED: 503,
    ProblemType.INTERNAL_ERROR: 500,
}

TITLE_FOR: Final[Mapping[ProblemType, str]] = {
    ProblemType.INVALID_SCREEN_DEFINITION: "Invalid screen definition",
    ProblemType.UNAUTHENTICATED: "Authentication required",
    ProblemType.PAYMENT_REQUIRED: "Your plan does not include this feature",
    ProblemType.NOT_FOUND: "Not found",
    ProblemType.STALE_DATA_VERSION: "Data version is no longer current",
    ProblemType.NO_TRADING_DAY: "No trading day available for that date",
    ProblemType.RATE_LIMITED: "Too many requests",
    ProblemType.PIPELINE_DEGRADED: "Data pipeline is degraded",
    ProblemType.INTERNAL_ERROR: "Internal server error",
}

#: docs/07 §Entitlements: 'A 402 `payment_required` problem response carries
#: `{"upgrade_url": "/pricing"}`.'
UPGRADE_URL: Final = "/pricing"


class Problem(Exception):
    """One RFC 9457 problem, raised anywhere and rendered by a single handler.

    ``extra`` carries the members docs/07 attaches to particular types — ``errors[]`` on an
    invalid definition, ``upgrade_url`` on a 402, ``retry_after`` on a 429. RFC 9457 §3.2 calls
    these extension members and requires exactly this: additional top-level fields.
    """

    def __init__(
        self,
        problem_type: ProblemType,
        detail: str,
        *,
        headers: Mapping[str, str] | None = None,
        **extra: object,
    ) -> None:
        super().__init__(detail)
        self.type = problem_type
        self.status = STATUS_FOR[problem_type]
        self.title = TITLE_FOR[problem_type]
        self.detail = detail
        self.headers = dict(headers or {})
        self.extra = extra

    def body(self, instance: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.type.value,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }
        if instance is not None:
            payload["instance"] = instance
        payload.update({key: value for key, value in self.extra.items() if value is not None})
        return payload


def unauthenticated(detail: str = "A valid bearer token is required.") -> Problem:
    return Problem(ProblemType.UNAUTHENTICATED, detail, headers={"WWW-Authenticate": "Bearer"})


def not_found(what: str, identifier: str) -> Problem:
    return Problem(ProblemType.NOT_FOUND, f"No {what} with id {identifier!r}.")


def payment_required(feature: str, *, detail: str | None = None) -> Problem:
    """docs/07: the 402 carries the upgrade URL, so the UI never has to hard-code it.

    ``detail`` lets a gate say something more useful than the generic sentence — the ₹0 tier's
    universe restriction names the universes it does allow (Prompt 13 §5).
    """
    return Problem(
        ProblemType.PAYMENT_REQUIRED,
        detail or f"{feature!r} is not included in your plan.",
        feature=feature,
        upgrade_url=UPGRADE_URL,
    )


def rate_limited(retry_after_seconds: int, limit_per_minute: int) -> Problem:
    """docs/07: "429 `rate-limited` — includes `Retry-After`"."""
    return Problem(
        ProblemType.RATE_LIMITED,
        f"Rate limit of {limit_per_minute} requests per minute exceeded.",
        headers={"Retry-After": str(retry_after_seconds)},
        retry_after=retry_after_seconds,
        limit_per_minute=limit_per_minute,
    )


def stale_data_version(sent: int, current: int) -> Problem:
    """docs/07: "409 — client sent a `data_version` that no longer exists"."""
    return Problem(
        ProblemType.STALE_DATA_VERSION,
        f"data_version {sent} is not the current one ({current}); re-fetch and retry.",
        sent_data_version=sent,
        current_data_version=current,
    )


def no_trading_day(requested: dt.date, earliest: dt.date, latest: dt.date) -> Problem:
    return Problem(
        ProblemType.NO_TRADING_DAY,
        f"{requested.isoformat()} is outside the servable range.",
        requested=requested.isoformat(),
        earliest_available=earliest.isoformat(),
        latest_available=latest.isoformat(),
    )


def invalid_screen_definition(errors: Sequence[Mapping[str, object]]) -> Problem:
    """docs/07: 400, "schema violation; `errors[]` lists field paths"."""
    return Problem(
        ProblemType.INVALID_SCREEN_DEFINITION,
        "The screen definition failed validation.",
        errors=list(errors),
    )


def pipeline_degraded(detail: str) -> Problem:
    """docs/07: "503 — last run failed its QA gate". docs/11: serve the last good version."""
    return Problem(ProblemType.PIPELINE_DEGRADED, detail)
