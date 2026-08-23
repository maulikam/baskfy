"""Broker OAuth state + request_token exchange (Tree-3 leaf 3.2).

Sole-tenant today: ``connect`` mints a one-time ``state`` bound to the signed-in
``user_id``; ``callback`` consumes it, exchanges the ``request_token``, and stores
the access token via :class:`baskfy_providers.tokens.AccessTokenStore` (Fernet).

Live Kite ``session/token`` is never called when ``DRY_RUN`` is truthy (the agent
default) or when ``BASKFY_KITE_API_SECRET`` is unset — those paths persist a
deterministic simulated token so unit tests stay network-free.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from baskfy_providers.tokens import AccessToken, AccessTokenStore

__all__ = [
    "OauthPending",
    "clear_oauth_states",
    "consume_oauth_state",
    "dry_run_enabled",
    "exchange_request_token",
    "exchange_request_token_stub",
    "register_oauth_state",
    "token_encryption_key",
    "token_store_for",
    "token_store_path",
]

#: Default TTL for pending OAuth ``state`` values (seconds).
_STATE_TTL_SECONDS = 30 * 60

_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class OauthPending:
    """One in-flight authorize redirect, bound to a sole-tenant user."""

    state: str
    user_id: int
    broker_id: str
    created_at: float


_pending: dict[str, OauthPending] = {}


def dry_run_enabled() -> bool:
    """Agents and tests default to dry-run; only an explicit false opts into live exchange."""
    raw = os.environ.get("DRY_RUN", "true").strip().lower()
    return raw in ("", "1", "true", "yes", "on")


def token_store_path() -> Path:
    """Encrypted blob path — broker-specific env first, then the shared Kite path."""
    raw = (
        os.environ.get("BASKFY_BROKER_TOKEN_PATH", "").strip()
        or os.environ.get("BASKFY_KITE_TOKEN_PATH", "").strip()
        or ".secrets/broker-token.enc"
    )
    return Path(raw)


def token_encryption_key() -> str:
    return os.environ.get("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", "").strip()


def token_store_for(*, path: Path | None = None, key: str | None = None) -> AccessTokenStore:
    return AccessTokenStore(path or token_store_path(), key if key is not None else token_encryption_key())


def _state_file() -> Path | None:
    raw = os.environ.get("BASKFY_BROKER_OAUTH_STATE_PATH", "").strip()
    return Path(raw) if raw else None


def _persist_states_locked() -> None:
    path = _state_file()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        state: {
            "state": pending.state,
            "user_id": pending.user_id,
            "broker_id": pending.broker_id,
            "created_at": pending.created_at,
        }
        for state, pending in _pending.items()
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _load_states_locked() -> None:
    path = _state_file()
    if path is None or not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    now = time.time()
    for state, body in raw.items():
        if not isinstance(body, dict):
            continue
        try:
            pending = OauthPending(
                state=str(body["state"]),
                user_id=int(body["user_id"]),
                broker_id=str(body["broker_id"]),
                created_at=float(body["created_at"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if now - pending.created_at <= _STATE_TTL_SECONDS:
            _pending[str(state)] = pending


def _purge_expired_locked(now: float) -> None:
    expired = [s for s, p in _pending.items() if now - p.created_at > _STATE_TTL_SECONDS]
    for state in expired:
        del _pending[state]


def register_oauth_state(*, state: str, user_id: int, broker_id: str) -> OauthPending:
    """Record a one-time ``state`` issued by ``POST .../connect``."""
    if not state:
        raise ValueError("oauth state must be non-empty")
    pending = OauthPending(
        state=state,
        user_id=user_id,
        broker_id=broker_id,
        created_at=time.time(),
    )
    with _lock:
        _load_states_locked()
        _purge_expired_locked(pending.created_at)
        _pending[state] = pending
        _persist_states_locked()
    return pending


def consume_oauth_state(state: str) -> OauthPending | None:
    """Validate and remove ``state``. Returns ``None`` when missing, reused, or expired."""
    if not state:
        return None
    now = time.time()
    with _lock:
        _load_states_locked()
        _purge_expired_locked(now)
        pending = _pending.pop(state, None)
        _persist_states_locked()
    if pending is None:
        return None
    if now - pending.created_at > _STATE_TTL_SECONDS:
        return None
    return pending


def clear_oauth_states() -> None:
    """Test helper — wipe in-memory (and file-backed) pending states."""
    with _lock:
        _pending.clear()
        path = _state_file()
        if path is not None and path.is_file():
            path.unlink()


def exchange_request_token_stub(
    *,
    api_key: str,
    request_token: str,
    user_id: int,
) -> str:
    """Pure simulated access token for DRY_RUN / missing-secret paths and unit tests.

    Deterministic for a given ``(api_key, request_token, user_id)`` so re-running the
    callback with the same inputs yields the same encrypted blob contents.
    """
    material = f"{api_key}|{request_token}|{user_id}|baskfy-oauth-stub".encode()
    digest = hashlib.sha256(material).hexdigest()[:40]
    return f"sim_{digest}"


def exchange_request_token(
    *,
    api_key: str,
    request_token: str,
    user_id: int,
    api_secret: str | None = None,
) -> str:
    """Exchange a Kite ``request_token`` for an access token string.

    Uses :func:`exchange_request_token_stub` whenever dry-run is on or the API secret is
    missing. A live POST to ``api.kite.trade`` runs only when both are configured for a
    real session — never from the default agent / unit-test environment.
    """
    secret = (api_secret if api_secret is not None else os.environ.get("BASKFY_KITE_API_SECRET", "")).strip()
    if dry_run_enabled() or not secret:
        return exchange_request_token_stub(api_key=api_key, request_token=request_token, user_id=user_id)

    # Live path — kept for operator sole-tenant login; network-blocked suites never reach here.
    import httpx  # noqa: PLC0415

    checksum = hashlib.sha256(f"{api_key}{request_token}{secret}".encode()).hexdigest()
    response = httpx.post(
        "https://api.kite.trade/session/token",
        data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
        headers={"X-Kite-Version": "3"},
        timeout=30.0,
    )
    response.raise_for_status()
    body = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or "access_token" not in data:
        raise ValueError("kite session/token response missing access_token")
    return str(data["access_token"])


def store_access_token(access_token: str, *, store: AccessTokenStore | None = None) -> AccessToken:
    """Persist ``access_token`` encrypted at rest."""
    target = store or token_store_for()
    return target.save(access_token)
