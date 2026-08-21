"""HTTP caching for the analytics reads — Prompt 16 deliverable 3.

    "Redis caching per docs/06, plus HTTP caching: ETags derived from `data_version`,
     stale-while-revalidate on RSC fetches, and cache warming after publish."

The Redis half and the warm-up already exist (``decile_api.screener``,
``decile_worker.tasks.publish``). This module is the HTTP half: a validator on every published
analytics response, and the freshness directives the Next.js data cache reads.

Why the ETag is derived from ``data_version``
--------------------------------------------
docs/06 §"Determinism guarantee": "Given the same `data_version`, the same definition and the same
`as_of`, results are byte-identical." That is exactly the promise an ETag makes, so the version is
the strongest part of the validator we can offer. It is not sufficient on its own — one
``data_version`` covers every instrument, every universe and every query string — so the tag is
``W/"v{data_version}-{sha256(body)}"``: the version says *which snapshot*, the digest says *which
answer within it*. A publish changes the first half for every route at once, which is what makes
"revalidate on `data_version`" true at the HTTP layer as well as in the Next data cache
(``docs/11a`` §6).

Weak, not strong. A strong ETag asserts byte equality of the *representation*, including the
Content-Encoding a proxy may have applied. We only control the body we hand to the transport.

Why ``stale-while-revalidate``
------------------------------
docs/08 §Routes marks the analytics pages "RSC, revalidate on `data_version`", and the fallback
path (``apps/web/src/lib/instrument/fetch.ts``) re-fetches on a timer. Without a freshness hint
every one of those re-fetches blocks a render on a full round trip. ``max-age=0,
stale-while-revalidate=N`` tells the Next data cache and any CDN in front of it to serve the copy
it has and refresh behind the request — and the ETag then makes that refresh a 304 with no body on
the overwhelmingly common night when nothing changed.

``private``, always. These responses are entitlement-filtered (``decile_api.entitlements``) and a
shared cache must never hand one user's answer to another.

What is *not* cached here
-------------------------
Only the routes in :data:`CACHEABLE_PREFIXES` — the published, read-only analytics surface. The
account, billing, portfolio and backtest routes are per-user mutable state; the CSV export is a
``StreamingResponse`` whose whole point is that it is never buffered (``decile_api.csv_export``),
and buffering it to hash it would defeat the streaming budget in docs/11. ``POST /screens/{id}/run``
is a POST: it has the Redis cache, and a validator on a non-idempotent method is not something a
browser will use.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import AsyncIterable, Awaitable, Callable
from typing import Final, Protocol, runtime_checkable

from fastapi import FastAPI, Request, Response
from starlette.datastructures import MutableHeaders

from decile_api.settings import API_PREFIX, Settings

#: Route prefixes (below ``/api/v1``) that serve published, read-only analytics.
CACHEABLE_PREFIXES: Final[tuple[str, ...]] = (
    "/meta/",
    "/instruments",
    "/indices/",
    "/market-health",
    "/listings",
)

#: Paths under those prefixes that must never carry a validator. ``/screens/{id}/export`` is not
#: under a cacheable prefix at all; this is for exceptions *inside* one.
UNCACHEABLE_SUFFIXES: Final[tuple[str, ...]] = ("/csv", "/export", "/download", "/events")

#: Only a 200 carries a representation worth validating. A 304 has none, and a problem document
#: (docs/07 §"Error catalogue") is not something a client should hold on to.
HTTP_OK: Final = 200
HTTP_NOT_MODIFIED: Final = 304

DATA_VERSION_HEADER: Final = "X-Decile-Data-Version"
AS_OF_HEADER: Final = "X-Decile-As-Of"

#: How many bytes of the digest go into the tag. 128 bits: a collision would have to be
#: engineered, and there is nothing to gain by engineering one — a false 304 shows a user their
#: own previous answer.
_DIGEST_CHARS: Final = 32

#: Responses larger than this are streamed through untouched rather than buffered to be hashed.
#: The analytics payloads are far below it (the dashboard, the largest, is ~145 rows); the guard
#: exists so that a future route added under a cacheable prefix cannot quietly become a memory
#: problem.
MAX_BUFFER_BYTES: Final = 4 * 1024 * 1024


def snapshot_headers(as_of: dt.date | None, data_version: int | None) -> dict[str, str]:
    """The two headers that name the snapshot a response was computed from.

    ``POST /screens/{id}/run`` has set them since Prompt 7 and ``decile_api.app`` already lists
    both in the CORS ``expose_headers``; every other published read now sets them too, because
    :func:`compute_etag` derives the validator from ``data_version`` and a response that does not
    say which version it came from can only be validated by its digest.
    """
    headers: dict[str, str] = {}
    if as_of is not None:
        headers[AS_OF_HEADER] = as_of.isoformat()
    if data_version is not None:
        headers[DATA_VERSION_HEADER] = str(data_version)
    return headers


def is_cacheable_path(path: str) -> bool:
    """Whether ``path`` is one of the published analytics reads."""
    if not path.startswith(API_PREFIX):
        return False
    tail = path[len(API_PREFIX) :]
    if any(tail.endswith(suffix) for suffix in UNCACHEABLE_SUFFIXES):
        return False
    return any(tail.startswith(prefix) for prefix in CACHEABLE_PREFIXES)


def compute_etag(body: bytes, data_version: str | None) -> str:
    """``W/"v{data_version}-{sha256(body)[:32]}"``.

    ``data_version`` may be absent — ``/meta/factors`` is a static catalogue with no snapshot
    behind it — in which case the digest stands alone and the tag still validates correctly.
    """
    digest = hashlib.sha256(body).hexdigest()[:_DIGEST_CHARS]
    prefix = f"v{data_version}-" if data_version else ""
    return f'W/"{prefix}{digest}"'


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 §13.1.2 ``If-None-Match``, weak comparison.

    ``*`` matches any existing representation. Otherwise the header is a comma-separated list and
    a match on any member is a match — and the comparison ignores the ``W/`` prefix, because weak
    comparison is what a ``GET`` revalidation uses.
    """
    if not if_none_match:
        return False
    candidate = _strip_weak(etag)
    for raw in if_none_match.split(","):
        token = raw.strip()
        if token == "*":
            return True
        if _strip_weak(token) == candidate:
            return True
    return False


def _strip_weak(token: str) -> str:
    return token[2:] if token.startswith("W/") else token


def cache_control(max_stale_seconds: int) -> str:
    """The freshness directives for a published analytics read.

    ``max-age=0`` — never reuse without asking. ``must-revalidate`` — and do not paper over an
    unreachable origin with a stale answer, because a screener that silently serves last week is
    worse than one that errors. ``stale-while-revalidate`` — but *do* answer instantly from the
    stored copy while that ask is in flight, which is the whole point of the directive.
    """
    return f"private, max-age=0, must-revalidate, stale-while-revalidate={max_stale_seconds}"


@runtime_checkable
class _Streamed(Protocol):
    """The one attribute Starlette's ``call_next`` response exposes for its body.

    ``BaseHTTPMiddleware`` hands the next middleware a private ``_StreamingResponse`` whose body
    has not been read yet. Naming the attribute structurally, rather than importing the private
    class or reaching for a cast, is what keeps this module free of an escape hatch (CLAUDE.md
    house rule 3).
    """

    @property
    def body_iterator(self) -> AsyncIterable[bytes]: ...


async def _drain(response: Response) -> bytes:
    """Collect a Starlette response's body, whether it streams or is already buffered."""
    if isinstance(response, _Streamed):
        chunks = [bytes(chunk) async for chunk in response.body_iterator]
        return b"".join(chunks)
    return bytes(response.body)


def register_http_cache(app: FastAPI, settings: Settings) -> None:
    """Attach the validator/freshness middleware.

    Registered from :func:`decile_api.app.register_middleware` so its position relative to the
    request-id middleware is explicit and testable rather than incidental.
    """

    @app.middleware("http")
    async def _http_cache(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.method not in ("GET", "HEAD"):
            return response
        if response.status_code != HTTP_OK or not is_cacheable_path(request.url.path):
            return response

        body = await _drain(response)
        if len(body) > MAX_BUFFER_BYTES:  # pragma: no cover - guard for a future route
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        etag = compute_etag(body, response.headers.get(DATA_VERSION_HEADER))
        headers = MutableHeaders(raw=list(response.raw_headers))
        headers["ETag"] = etag
        headers["Cache-Control"] = cache_control(settings.http_stale_while_revalidate_seconds)
        headers["Vary"] = _merged_vary(headers.get("Vary"))

        if etag_matches(request.headers.get("if-none-match"), etag):
            # RFC 9110 §15.4.5: a 304 carries the headers that would have been sent on a 200 and
            # no body. Content-Length must go with the body it described.
            del headers["Content-Length"]
            return Response(status_code=HTTP_NOT_MODIFIED, headers=dict(headers))

        headers["Content-Length"] = str(len(body))
        return Response(
            content=body,
            status_code=HTTP_OK,
            headers=dict(headers),
            media_type=response.media_type,
        )


def _merged_vary(existing: str | None) -> str:
    """``Authorization`` must be in ``Vary``.

    These responses are entitlement-filtered, so the same URL answers differently for an anonymous
    caller and a subscriber. Without this a shared cache is a data leak.
    """
    parts = [part.strip() for part in (existing or "").split(",") if part.strip()]
    if not any(part.lower() == "authorization" for part in parts):
        parts.append("Authorization")
    return ", ".join(parts)
