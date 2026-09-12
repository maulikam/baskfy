"""READ-ONLY probe: is the box's Kite access token present and unexpired?

Run with ``tools/deploy/box-python.sh worker ops/kite-token-state.py``.

Decrypts the stored blob and applies :meth:`AccessToken.is_expired` — the same accessor the Kite
adapter uses. It makes **no network call**, places no order and writes nothing: `AccessTokenStore`
opens the file for reading only, and nothing here touches `save()`. Prints no token value.

`gates/kite-sync.md` G6 is this script's output.
"""

from __future__ import annotations

import datetime as dt

from baskfy_providers.settings import get_provider_settings
from baskfy_providers.tokens import AccessTokenStore

settings = get_provider_settings()
print("token_path:", settings.kite_token_path)
print("api_key_set:", bool((settings.kite_api_key or "").strip()))

store = AccessTokenStore(settings.kite_token_path, settings.kite_token_encryption_key)
print("exists:", store.exists())
if store.exists():
    try:
        token = store.load()
    except Exception as exc:  # noqa: BLE001 - a probe reports every failure shape as text
        print("load_failed:", type(exc).__name__)
    else:
        # The value is never printed. Only when it was issued and whether that has lapsed.
        print("issued_at:", token.issued_at.isoformat())
        print("is_expired:", token.is_expired())
        print("now_utc:", dt.datetime.now(dt.UTC).isoformat())
