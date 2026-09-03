"""Broker OAuth state + request_token exchange (Tree-3 leaf 3.2).

Sole-tenant today: ``connect`` mints a one-time ``state`` bound to the signed-in
``user_id``; ``callback`` consumes it, exchanges the ``request_token``, and stores
the access token via :class:`baskfy_providers.tokens.AccessTokenStore` (Fernet).

Live Kite ``session/token`` is never called when ``DRY_RUN`` is truthy (the agent
default), when ``BASKFY_KITE_API_SECRET`` is unset, or when ``BASKFY_KITE_API_KEY``
is unset. Those are the *simulated* paths and they mint a deterministic ``sim_``
token so unit tests stay network-free.

**A simulated token is not allowed anywhere near the real session** (leaf 1.1.4).
``token_store_path()`` falls back to ``BASKFY_KITE_TOKEN_PATH`` — the same encrypted
blob the M58 desk bridge fills with the live Kite session that the nightly pipeline
reads. Writing a ``sim_`` stub there destroys a working session and leaves something
Kite will reject in its place, and until this leaf the callback did exactly that on
every request. So the two token kinds now have two files and the split is enforced
at the lowest layer:

* :func:`token_store_path` — the real session. Only a token from a completed live
  exchange may be written here.
* :func:`simulated_token_store_path` — a sibling ``*.simulated<suffix>`` file that no
  reader of the live session ever opens. Only ``sim_`` tokens may be written here.

:func:`store_access_token` refuses either crossing by raising
:class:`SimulatedTokenRefused`, so caller discipline is not what keeps the live
session safe. Storing a simulated token at all is additionally opt-in
(``BASKFY_BROKER_OAUTH_ALLOW_SIMULATED``): a deployment that cannot really complete a
login should say so rather than answer as though it had.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

from baskfy_providers.tokens import AccessToken, AccessTokenStore

__all__ = [
    "KITE_AUTHORIZE_URL",
    "SIMULATED_TOKEN_PREFIX",
    "STATE_TTL_SECONDS",
    "KiteLoginUrl",
    "OauthPending",
    "SimulatedTokenRefused",
    "TokenExchange",
    "clear_oauth_states",
    "consume_oauth_state",
    "dry_run_enabled",
    "exchange_request_token",
    "exchange_request_token_stub",
    "is_simulated_token",
    "kite_login_url",
    "register_oauth_state",
    "simulated_exchange_reasons",
    "simulated_token_storage_enabled",
    "simulated_token_store_for",
    "simulated_token_store_path",
    "store_access_token",
    "token_encryption_key",
    "token_store_for",
    "token_store_path",
]

#: Every token minted by :func:`exchange_request_token_stub` carries this prefix, so a
#: simulated value is recognisable from the string alone — including one read back out of a
#: blob written by an older build.
SIMULATED_TOKEN_PREFIX: Final = "sim_"

#: Default TTL for pending OAuth ``state`` values (seconds). Public because anything that
#: hands a login link to a human has to be able to tell them how long it is good for — the
#: 08:45 nudge (`baskfy_worker.tasks.kite_login_nudge`) says so in the message.
STATE_TTL_SECONDS: Final = 30 * 60
_STATE_TTL_SECONDS = STATE_TTL_SECONDS

#: Zerodha's authorize endpoint — the one entry of ``routers.brokers._WIRED_AUTHORIZE`` that
#: can actually finish a login here, named once so the router and the worker cannot drift.
KITE_AUTHORIZE_URL: Final = "https://kite.zerodha.com/connect/login"

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
    return AccessTokenStore(
        path or token_store_path(), key if key is not None else token_encryption_key()
    )


def simulated_token_store_path() -> Path:
    """Where a ``sim_`` token is allowed to live — never :func:`token_store_path`.

    A sibling of the real blob rather than a path of its own, so the two always land on the
    same volume and inherit the same directory permissions: an operator who moved the session
    file cannot accidentally leave the simulated one behind in the image.

    Nothing reads this file. That is the point — the pipeline (``index_backfill``, ``ops``),
    the holdings sync (``broker_holdings``), the Kite provider and the M58 desk bridge all
    open :func:`token_store_path`, so a simulated login stays exercisable end to end (state,
    exchange, Fernet encrypt, read back) without any of them ever seeing it.
    """
    real = token_store_path()
    return real.with_name(f"{real.stem}.simulated{real.suffix}")


def simulated_token_store_for(
    *, path: Path | None = None, key: str | None = None
) -> AccessTokenStore:
    return AccessTokenStore(
        path or simulated_token_store_path(), key if key is not None else token_encryption_key()
    )


def simulated_token_storage_enabled() -> bool:
    """Opt-in: may this deployment persist a simulated session at all?

    Off unless explicitly on, and the inverse of :func:`dry_run_enabled` in that respect —
    dry-run defaults *safe* by defaulting true, this defaults safe by defaulting false. A
    DRY_RUN demo box or an integration suite that wants the whole callback exercisable sets
    ``BASKFY_BROKER_OAUTH_ALLOW_SIMULATED=true``; everything else gets a loud refusal instead
    of a stored stub and a cheerful 200.
    """
    raw = os.environ.get("BASKFY_BROKER_OAUTH_ALLOW_SIMULATED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def is_simulated_token(access_token: str) -> bool:
    """True for anything :func:`exchange_request_token_stub` could have produced."""
    return access_token.startswith(SIMULATED_TOKEN_PREFIX)


def _same_file(left: Path, right: Path) -> bool:
    """Path equality that survives ``~``, ``..`` and the ``/tmp`` → ``/private/tmp`` symlink."""
    return left.expanduser().resolve() == right.expanduser().resolve()


class SimulatedTokenRefused(RuntimeError):
    """A write that would have mixed the simulated and the real session.

    Raised by :func:`store_access_token`, which is the single write path for both files. It is
    a programming error rather than a user-facing condition — the callback decides whether a
    simulated login is permitted *before* it stores anything — so it escapes as a 500 rather
    than being translated into a problem response. The one thing it must never do is pass.
    """


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


@dataclass(frozen=True, slots=True)
class KiteLoginUrl:
    """One authorize URL and the one-time ``state`` that was minted with it."""

    url: str
    state: str
    broker_id: str
    #: How long :func:`consume_oauth_state` will still accept ``state``, in whole minutes. On
    #: the URL rather than left for the caller to recompute: two readings of the same TTL are
    #: two chances to promise a human something the callback will not honour.
    valid_for_minutes: int


def kite_login_url(
    *,
    api_key: str,
    user_id: int,
    broker_id: str = "zerodha",
    authorize_base: str = KITE_AUTHORIZE_URL,
) -> KiteLoginUrl:
    """Mint a one-time ``state`` and build the Kite login URL that carries it.

    **The single builder.** ``POST /brokers/{id}/connect`` calls it, and so does the 08:45
    login nudge (SW18) — a second builder somewhere else is a second chance to drop the state
    the way `redirect_params` was dropped before M-whichever, and the failure only shows up at
    the end of a login the person has already committed to.

    Two things about the query string that are not obvious and must not be "tidied":

    * ``state`` travels inside ``redirect_params``, **not** as a top-level parameter. Kite
      drops query keys it does not know and echoes back only what ``redirect_params`` carries.
    * no ``redirect_uri`` is sent. Kite Connect uses the redirect registered against the app
      and ignores one supplied at login time.

    ``user_id`` is what :func:`consume_oauth_state` checks the finishing session against, so a
    link minted for one account cannot be completed by another. It also means an offline
    minter (the nudge) has to know whose morning it is; ``None`` is not a sensible default and
    there is deliberately no overload that omits it.
    """
    state = secrets.token_urlsafe(24)
    register_oauth_state(state=state, user_id=user_id, broker_id=broker_id)
    query = urlencode(
        {
            "api_key": api_key,
            "v": "3",
            "redirect_params": urlencode({"state": state}),
        }
    )
    return KiteLoginUrl(
        url=f"{authorize_base}?{query}",
        state=state,
        broker_id=broker_id,
        valid_for_minutes=STATE_TTL_SECONDS // 60,
    )


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
    # The constant, not the literal: `is_simulated_token` is what keeps a stub out of the real
    # session store, and it recognises tokens by exactly this prefix. Spelling it twice would
    # let a rename here silently turn every future stub into something the guard waves through.
    return f"{SIMULATED_TOKEN_PREFIX}{digest}"


@dataclass(frozen=True, slots=True)
class TokenExchange:
    """The outcome of one exchange: the token, and whether it is real.

    One value carrying both, because the caller used to recompute "was this simulated?" from
    the environment a second time and report *that* to the user. Two readings of the same
    environment are two chances to disagree, and the field that says whether a session is real
    is the last field in this service that should be able to drift from what happened.
    """

    access_token: str
    simulated: bool
    #: Human-readable preconditions that were missing, in the order checked. Empty iff
    #: ``simulated`` is false. Named so an error message can quote them verbatim.
    reasons: tuple[str, ...]


def simulated_exchange_reasons(*, api_key: str, api_secret: str | None = None) -> tuple[str, ...]:
    """Why a live ``session/token`` call cannot be made here. Empty tuple = it can.

    All three are collected rather than short-circuiting on the first, because an operator
    reading the refusal is the only person who can act on it and a box missing two things
    should hear about two. ``api_key`` is in the list for a reason of its own: the callback
    used to substitute the literal ``"dry-run-api-key"`` for a missing key, which is harmless
    in the stub and, on a box with a secret and ``DRY_RUN=false``, would have posted a
    placeholder credential to Kite as though it were real.

    Names only — never a value. These strings reach an HTTP response body.
    """
    secret = (
        api_secret if api_secret is not None else os.environ.get("BASKFY_KITE_API_SECRET", "")
    ).strip()
    reasons: list[str] = []
    if dry_run_enabled():
        reasons.append("DRY_RUN is on, so no live broker call may be made")
    if not api_key.strip():
        reasons.append("BASKFY_KITE_API_KEY is not set on this deployment")
    if not secret:
        reasons.append("BASKFY_KITE_API_SECRET is not set on this deployment")
    return tuple(reasons)


def exchange_request_token(
    *,
    api_key: str,
    request_token: str,
    user_id: int,
    api_secret: str | None = None,
) -> TokenExchange:
    """Exchange a Kite ``request_token``, saying plainly whether the result is real.

    Uses :func:`exchange_request_token_stub` whenever :func:`simulated_exchange_reasons` finds
    anything missing. A live POST to ``api.kite.trade`` runs only when all three preconditions
    hold — never from the default agent / unit-test environment.
    """
    secret = (
        api_secret if api_secret is not None else os.environ.get("BASKFY_KITE_API_SECRET", "")
    ).strip()
    reasons = simulated_exchange_reasons(api_key=api_key, api_secret=secret)
    if reasons:
        return TokenExchange(
            access_token=exchange_request_token_stub(
                api_key=api_key, request_token=request_token, user_id=user_id
            ),
            simulated=True,
            reasons=reasons,
        )

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
    access_token = str(data["access_token"])
    if not access_token:
        raise ValueError("kite session/token returned an empty access_token")
    if is_simulated_token(access_token):
        # Kite cannot mint one, so this is either a fixture pointed at the live path or a
        # proxy standing in for Kite. Either way it must not reach the real store, and the
        # store's own guard would refuse it one line later; failing here names the cause.
        raise ValueError(
            f"kite session/token returned a token prefixed {SIMULATED_TOKEN_PREFIX!r}, "
            "which this service reserves for simulated sessions"
        )
    return TokenExchange(access_token=access_token, simulated=False, reasons=())


def store_access_token(access_token: str, *, store: AccessTokenStore | None = None) -> AccessToken:
    """Persist ``access_token`` encrypted at rest, in the file its kind belongs in.

    The guard is here, at the only write path, rather than at the callback that happens to be
    today's only caller: the failure this leaf fixes was a caller storing unconditionally, and
    a rule that lives in the caller is a rule the next caller does not inherit. Both crossings
    are refused, and the real-token-into-the-simulated-file direction matters too — a live
    session filed under the wrong name is a session the pipeline cannot find.
    """
    simulated = is_simulated_token(access_token)
    target = store or (simulated_token_store_for() if simulated else token_store_for())
    into_simulated_store = _same_file(target.path, simulated_token_store_path())
    if simulated and not into_simulated_store:
        raise SimulatedTokenRefused(
            "refusing to write a simulated access token to the real session store; "
            f"simulated tokens belong in {simulated_token_store_path()}"
        )
    if not simulated and into_simulated_store:
        raise SimulatedTokenRefused(
            "refusing to write a real access token to the simulated session store; "
            f"the live session belongs in {token_store_path()}"
        )
    return target.save(access_token)
