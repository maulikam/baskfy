"""Browser-facing defences for an interface that can place real orders.

THE THREAT CHANGED WHEN THE BOX DID. On a laptop this ran on loopback behind a firewall
nobody could route to. On a cloud host with a static IP the machine is findable, the
tunnel is open for hours at a time, and the browser holding that tunnel also visits the
rest of the internet. Two attacks follow from that, and neither needs a single packet to
reach port 8420 from outside:

  DNS REBINDING. An attacker's page resolves its own domain to 127.0.0.1 and then reads
  http://their-domain:8420/performance/data as same-origin. Every holding, every figure,
  the whole book. The defence is to check the Host header: this app answers to localhost
  and nothing else, so a request arriving as `evil.com` is refused before it routes.

  CROSS-SITE REQUEST FORGERY. A form on any page can POST to 127.0.0.1:8420 without a
  preflight. /execute and /stops/arm are already safe by accident — both need a plan_id
  that is a fresh uuid4 held in memory — but /settings takes named strategy knobs and
  /ops/run takes an operation name, and neither carries a secret. The defence is to
  require that unsafe methods declare an Origin this app recognises.

Optional on top: a password. Loopback is not a boundary between users of the same host,
and on a shared or compromised box every route here is reachable by anything that can
open a socket. Off by default so the laptop is unchanged; the deploy README turns it on.
"""
from __future__ import annotations

import base64
import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from .. import config as C

log = logging.getLogger("websec")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Paths that must answer before a password exists, or the login cannot be reached.
NO_AUTH = ("/status",)


def _hosts() -> set[str]:
    raw = getattr(C, "DESK_ALLOWED_HOSTS", "") or "127.0.0.1,localhost"
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _origins() -> set[str]:
    """Origins a form may legitimately come from: this app, on any allowed host."""
    out = set()
    for h in _hosts():
        for scheme in ("http", "https"):
            out.add(f"{scheme}://{h}")
            out.add(f"{scheme}://{h}:{getattr(C, 'DESK_PORT', 8420)}")
    return out


class DeskSecurity(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # --- 1. the Host must be one we answer to ---------------------------------
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host and host not in _hosts():
            log.warning("refused a request for host %r", host)
            return JSONResponse(
                {"detail": f"This desk answers to {sorted(_hosts())}, not {host!r}. "
                           "A request arriving under another name is how a browser is "
                           "tricked into treating this app as the attacker's own site."},
                status_code=421)

        # --- 2. anything that changes state must say where it came from -----------
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer") or ""
            allowed = _origins()
            ok = (origin in allowed) if origin else any(
                referer.startswith(o + "/") or referer == o for o in allowed)
            if not ok:
                log.warning("refused a %s to %s from origin=%r referer=%r",
                            request.method, request.url.path, origin, referer[:80])
                return JSONResponse(
                    {"detail": "Refused: this request did not come from the desk. A form "
                               "on another site can POST here without asking, so a "
                               "state-changing request has to declare an origin this app "
                               "recognises."},
                    status_code=403)

        # --- 3. optional password -------------------------------------------------
        pw = getattr(C, "DESK_PASSWORD", "") or ""
        if pw and not request.url.path.startswith(NO_AUTH):
            if not _basic_ok(request.headers.get("authorization"), pw):
                return Response(status_code=401, headers={
                    "WWW-Authenticate": 'Basic realm="Momentum Desk"'},
                    content="Authentication required.")

        return await call_next(request)


def _basic_ok(header: str | None, password: str) -> bool:
    """Constant-time comparison, so a wrong password cannot be found a byte at a time."""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8", "replace")
    except Exception:                                              # noqa: BLE001
        return False
    _user, _, given = decoded.partition(":")
    return hmac.compare_digest(given, password)


# =====================================================================================
# secrets on disk
# =====================================================================================
SECRET_FILES = (".env", "data/.kite_token.json")


def check_secret_permissions(fix: bool = False) -> list[dict]:
    """Report — and optionally correct — credential files any account can read.

    .env holds the API key and secret; the token file places orders until it expires
    tonight. A default umask leaves both at 0644, which is a non-issue on a personal
    laptop and a real one the moment the box has more than one login.
    """
    import os
    import pathlib as _pl

    out = []
    for name in SECRET_FILES:
        p = _pl.Path(name)
        if not p.exists():
            continue
        mode = p.stat().st_mode & 0o777
        if mode & 0o077:
            if fix:
                os.chmod(p, 0o600)
            out.append({"file": name, "mode": oct(mode),
                        "fixed": fix,
                        "detail": "readable by other accounts on this host"})
    return out
