"""Serve the desk under a path prefix (``/desk``) on the app's own address.

Maulik, 29 Oct 2026, after Baskfy moved onto his Mac: "i want both the app and desk in same, no
two different port and applications". The full merge of the desk's pages into the web app comes
with the backend rewrite; until then the desk keeps its own process and is mounted at ``/desk``
on the app's address by Caddy, which strips the prefix on the way in.

The desk's pages were written for the root of their own host: about seventy ``href="/…"``,
``action="/…"`` and ``fetch('/…')`` literals, plus ``Location: /…`` redirects. Many of those paths
(``/swing``, ``/twt``, ``/tradebook``, ``/login``) also exist in the web app, so an unprefixed
click would land in the wrong program. This middleware adds the prefix to them on the way out.

Off unless ``DESK_PATH_PREFIX`` is set: with it empty the desk's bytes are untouched, which is
how every existing deployment and every existing test runs it. It rewrites URLs only — it never
reads or changes a form value, an amount or an order field.
"""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: Responses whose text can carry the desk's own root-relative URLs.
_REWRITTEN_TYPES = (b"text/html", b"javascript")


def normalise(prefix: str) -> str:
    """``"desk"``, ``"/desk/"`` and ``"/desk"`` all mean ``"/desk"``; blank means off."""
    stripped = prefix.strip().strip("/")
    return f"/{stripped}" if stripped else ""


def rewrite_location(location: str, prefix: str) -> str:
    """Prefix a root-relative redirect; leave absolute and protocol-relative ones alone."""
    if not location.startswith("/") or location.startswith("//"):
        return location
    if location == prefix or location.startswith(prefix + "/"):
        return location
    return prefix + location


def rewrite_body(text: str, prefix: str) -> str:
    """Prefix the desk's root-relative URLs in markup and inline script."""
    escaped = re.escape(prefix.lstrip("/"))
    # `/` not followed by `/` (protocol-relative) nor by the prefix itself (already rewritten).
    root = rf"/(?!/|{escaped}(?:/|[\"'`?#]))"
    text = re.sub(rf'((?:href|action|src)=")' + root, rf"\1{prefix}/", text)
    text = re.sub(rf"((?:href|action|src)=')" + root, rf"\1{prefix}/", text)
    text = re.sub(r"(fetch\(\s*['\"`])" + root, rf"\1{prefix}/", text)
    return text


class PathPrefix:
    """Pure ASGI, so the rewrite sees the final response, after every other middleware."""

    def __init__(self, app: ASGIApp, prefix: str) -> None:
        self.app = app
        self.prefix = normalise(prefix)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.prefix:
            await self.app(scope, receive, send)
            return

        prefix = self.prefix
        held_start: Message | None = None
        chunks: list[bytes] = []

        async def wrapped(message: Message) -> None:
            nonlocal held_start
            if message["type"] == "http.response.start":
                headers = [
                    (k, rewrite_location(v.decode("latin-1"), prefix).encode("latin-1"))
                    if k.lower() == b"location" else (k, v)
                    for k, v in message.get("headers", [])
                ]
                message = {**message, "headers": headers}
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                encoded = any(k.lower() == b"content-encoding" for k, _ in headers)
                if not encoded and any(t in ctype for t in _REWRITTEN_TYPES):
                    held_start = message          # length changes; hold until the body is whole
                    return
                await send(message)
                return
            if message["type"] == "http.response.body" and held_start is not None:
                chunks.append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                body = rewrite_body(b"".join(chunks).decode("utf-8"), prefix).encode("utf-8")
                headers = [(k, v) for k, v in held_start["headers"] if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode("latin-1")))
                await send({**held_start, "headers": headers})
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
            await send(message)

        await self.app(scope, receive, wrapped)
