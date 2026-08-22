"""Encrypted at-rest storage for the daily Kite access token (M16, P3.7).

The token places real orders for the rest of the trading day. It used to be written as plain JSON
at mode 0600 — which is the right mode, and still leaves the secret readable in any backup, any
`rsync`, any screen-share of the directory listing, and to anything running as the same user.

This wraps `baskfy_providers.tokens.AccessTokenStore`, the screener's Fernet-encrypted store, so
there is one implementation of "how a Kite token is kept" across the merged system rather than two.

ON THE KEY, HONESTLY
--------------------
`KITE_TOKEN_ENCRYPTION_KEY` in the environment is the real protection: the key lives somewhere the
ciphertext does not. When it is absent this module **generates one and writes it beside the token
at 0600**, and says so in the log.

That fallback is a deliberate, limited trade. A key stored next to the ciphertext defends against
the secret being *copied* — backups, syncs, a snapshot pulled off the box — and it does **not**
defend against an attacker who can already read files as this user, because they can read the key
too. The alternative was to refuse to store a token without configuration, which would have broken
login on a desk that has to be able to rebalance on a Friday.

**Set `KITE_TOKEN_ENCRYPTION_KEY` in `.env` and the fallback stops being used.** Until then this is
encryption against disclosure-by-copy, not against a local attacker, and it should not be described
as more than that.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from baskfy_providers.tokens import AccessTokenStore

log = logging.getLogger("token_store")

#: Beside the token, and only used when the environment supplies nothing better.
_FALLBACK_KEY_SUFFIX = ".key"


def _bootstrap_key(token_path: Path) -> str:
    """Generate and persist a Fernet key at 0600, once, next to the token."""
    from cryptography.fernet import Fernet

    key_path = token_path.with_suffix(token_path.suffix + _FALLBACK_KEY_SUFFIX)
    if key_path.is_file():
        return key_path.read_text().strip()

    key = Fernet.generate_key().decode()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    # 0600 at creation, never chmod'd afterwards, for the same reason the token was.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(key)
    log.warning(
        "KITE_TOKEN_ENCRYPTION_KEY is not set, so a key was generated at %s. The token is now "
        "encrypted at rest against being copied, but the key sits beside it — anything that can "
        "read one can read the other. Set KITE_TOKEN_ENCRYPTION_KEY in .env for real separation.",
        key_path,
    )
    return key


def store_for(token_path: str | Path, configured_key: str = "") -> AccessTokenStore:
    """The encrypted store for this desk's token."""
    path = Path(token_path)
    key = configured_key or os.getenv("KITE_TOKEN_ENCRYPTION_KEY", "") or _bootstrap_key(path)
    return AccessTokenStore(path, key)
