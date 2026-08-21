"""Which address family the broker sees us on.

WHY THIS EXISTS. Kite authorises order placement against an allowlist of IP addresses.
This machine's connection offers both families and the resolver prefers IPv6, so every
order on 19 Aug 2026 was refused with

    IP (2409:4090:1012:be04:...) is not allowed to place orders for this app

while the allowlist held the IPv4 address. Adding the v6 address is not a fix: it is a
residential Jio prefix that rotates, and the v4 address had already changed from
103.238.14.245 the day before. Whatever is allowlisted, the request has to LEAVE on it.

WHAT THIS DOES. kiteconnect builds a requests.Session, which resolves through
urllib3.util.connection.allowed_gai_family. Overriding that to AF_INET makes every
connection in the process — including the ticker — use IPv4, so the address Kite sees is
the one you can actually allowlist.

WHAT IT DOES NOT DO. It does not make the address STATIC. SEBI's retail-algo framework
requires a static IP for order APIs, and a residential connection does not provide one.
This makes the allowlist entry take effect; it does not make the entry stop expiring.
"""
from __future__ import annotations

import logging
import socket

log = logging.getLogger("net")


def _ipv4_only() -> int:
    return socket.AF_INET


def force_ipv4() -> bool:
    """Pin outbound HTTP to IPv4. Returns True if this call changed anything.

    Idempotent: applying it twice is a no-op, so importing from several entry points is
    safe. Failure to patch is logged rather than raised — a broker call refused for the
    wrong address is a clear error, and an app that will not start is worse.
    """
    try:
        import urllib3.util.connection as conn
    except Exception as exc:                                       # noqa: BLE001
        log.warning("could not pin outbound traffic to IPv4: %s", exc)
        return False
    if getattr(conn.allowed_gai_family, "__name__", "") == "_ipv4_only":
        return False
    conn.allowed_gai_family = _ipv4_only
    log.info("outbound HTTP pinned to IPv4 so the broker sees an allowlistable address")
    return True


def outbound_ip(timeout: float = 6.0) -> str | None:
    """The address the world actually sees. Read-only, and never on the order path."""
    import urllib.request
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=timeout) as r:
            return r.read().decode().strip()
    except Exception:                                              # noqa: BLE001
        return None
