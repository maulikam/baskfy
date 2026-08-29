"""Deposit a Kite access token into Baskfy's store — the bridge around one redirect URL.

    python -m baskfy_worker.kite_session_cli deposit --token <access_token>
    python -m baskfy_worker.kite_session_cli status

THE PROBLEM THIS SOLVES
=======================
A Kite Connect app has exactly **one** registered redirect URL, and the RENIL app's points at
``https://desk.modelbasket.in/callback`` — the momentum desk, which places live orders and cannot
lose its login. Repointing it at Baskfy would break the desk; Zerodha will not issue a second app
for the same purpose. So Baskfy can never *start* a Kite login of its own.

It does not need to. ``KiteProvider`` performs no OAuth: it reads an access token out of
``AccessTokenStore`` and calls the API with it (`providers/kite.py`). The desk already obtains
exactly that token every morning, through the redirect it owns. What was missing was a way to put
it where Baskfy looks.

**So the bridge carries the access token, not the login.** One app, one redirect, one login — and
two consumers of the session it produces. Kite permits the same access token from several
processes; nothing here logs in, races the desk, or invalidates its session.

WHY NOT THE REQUEST TOKEN
-------------------------
The obvious alternative — have the operator copy the ``request_token`` out of the callback URL and
let Baskfy exchange it — cannot work. The desk's own ``/callback`` consumes it on arrival, and a
Kite request token is single-use. By the time a human could copy it, it is spent.

WHAT THIS UNLOCKS
-----------------
``fetch_daily_bars`` via Kite (the pipeline otherwise falls back to the bhavcopy, which only
reaches 2024), the deep historical backfill from 2011 (docs/08 D5), and broker holdings sync.

OPERATIONALLY
-------------
A Kite access token expires overnight with no refresh, so this runs once per trading day, after
the desk's login. It is deliberately a command rather than an endpoint: the token is a live
credential for an account that can trade, and an HTTP route that accepts one is a route that can
be tricked into accepting one. `docs/DECISIONS-MERGE.md` M57.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from baskfy_providers.settings import get_provider_settings
from baskfy_providers.tokens import AccessTokenStore


def _store() -> AccessTokenStore:
    settings = get_provider_settings()
    if not settings.kite_token_encryption_key:
        raise SystemExit(
            "BASKFY_KITE_TOKEN_ENCRYPTION_KEY is not set; refusing to write a live broker "
            "credential to disk unencrypted."
        )
    return AccessTokenStore(settings.kite_token_path, settings.kite_token_encryption_key)


def deposit(token: str) -> int:
    """Store *token*, then read it straight back to prove the round trip.

    Verified rather than assumed: an encryption key that does not match the one the provider
    reads with would store happily and fail at the next fetch, hours later, as an unexplained
    provider error. Better to fail here, in front of the person who ran the command.
    """
    store = _store()
    record = store.save(token.strip())
    reloaded = store.load()
    if reloaded.value != record.value:
        raise SystemExit("stored token did not read back identically; refusing to report success")

    # Never the token itself — this prints to a terminal and often into a scrollback.
    print(f"stored: {len(record.value)} chars, issued_at={record.issued_at.isoformat()}")
    print(f"path  : {store.path}")
    return 0


def status() -> int:
    """Say whether a usable session exists, without revealing it."""
    store = _store()
    if not store.exists():
        print(f"no token at {store.path}")
        return 1
    record = store.load()
    expired = record.is_expired()
    print(f"path      : {store.path}")
    print(f"issued_at : {record.issued_at.isoformat()}")
    print(f"expired   : {expired}")
    # A Kite token dies overnight; "present" and "usable" are different questions and conflating
    # them is how a pipeline discovers the problem at 6pm instead of at 9am.
    return 1 if expired else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m baskfy_worker.kite_session_cli", description=__doc__
    )
    sub = parser.add_subparsers(dest="command", required=True)
    dep = sub.add_parser("deposit", help="store an access token obtained by the desk's login")
    dep.add_argument(
        "--token",
        required=True,
        help="the access_token from the desk's Kite session (not the request_token)",
    )
    sub.add_parser("status", help="is there a usable Kite session, and how old is it")

    args = parser.parse_args(argv)
    if args.command == "deposit":
        return deposit(args.token)
    return status()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
