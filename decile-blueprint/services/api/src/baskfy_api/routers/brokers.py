"""``/brokers`` — the connect catalog (M41 / P5.8), OAuth callback + holdings sync (Tree-3).

    GET  /brokers                         every broker on the grid, plus the D3 gate status
    GET  /brokers/{id}                    one broker
    POST /brokers/{id}/connect            start OAuth — only when the gate is open and wired
    GET  /brokers/callback                exchange request_token (state-validated; 503 if it
                                          could only be simulated)
    POST /brokers/{id}/sync-holdings      holdings shaped like HoldingRow (DRY_RUN safe)

The catalog is always served to a signed-in account. Live authorize redirects require
``BROKER_OAUTH_REVIEW.signed_off`` (D3 posture B). The web app still never places orders —
callback and sync-holdings store / read credentials only; they never touch the order path.

Kite's registered redirect points at ``/api/v1/brokers/callback``, so that route is reachable
by a real login and is treated as live: it refuses rather than simulating, and a simulated
token can never be written where the real session lives (leaf 1.1.4).
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
from decimal import Decimal
from typing import Annotated, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, Field

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.broker_accounts import ensure_default_broker_account
from baskfy_api.broker_holdings import (
    HoldingsResult,
    HoldingsSource,
    holding_row_to_dict,
    holdings_for_broker,
)
from baskfy_api.broker_holdings_sync import (
    is_persistable,
    not_persisted,
    sync_holdings_into_portfolio,
)
from baskfy_api.broker_oauth import (
    consume_oauth_state,
    dry_run_enabled,
    exchange_request_token,
    is_simulated_token,
    register_oauth_state,
    simulated_token_storage_enabled,
    simulated_token_store_for,
    store_access_token,
    token_store_for,
)
from baskfy_api.db import SessionDep
from baskfy_api.metrics import IST
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.settings import get_settings
from baskfy_core.broker_connections import (
    BROKER_OAUTH_REVIEW,
    BrokerDef,
    broker_catalog,
    get_broker,
)
from baskfy_providers.errors import CredentialsMissing

#: Where Kite sends the browser back. Declared once, next to the route that serves it and the
#: value handed to Kite, because a redirect_uri that disagrees with the registered one fails at
#: the end of a login rather than the start — and the two used to be written out separately.
OAUTH_CALLBACK_PATH: Final = "/api/v1/brokers/callback"

router = APIRouter(prefix="/brokers", tags=["brokers"])

#: Brokers whose authorize URL shape is known in this codebase today.
#: HDFC / Kotak / ICICI / Groww stay unwired until their partner apps publish a stable
#: public authorize URL we can register (Tree-4 leaf 4.3).
_WIRED_AUTHORIZE: dict[str, str] = {
    "zerodha": "https://kite.zerodha.com/connect/login",
    "upstox": "https://api.upstox.com/v2/login/authorization/dialog",
    "angelone": "https://smartapi.angelbroking.com/publisher-login",
    "fyers": "https://api-t1.fyers.in/api/v3/generate-authcode",
    "dhan": "https://auth.dhan.co/login/consentApp-login",
}

#: Brokers whose OAuth can actually *finish*, not merely start.
#:
#: Knowing an authorize URL is not the same as having a login. ``BASKFY_KITE_API_KEY`` is a
#: Zerodha app credential and :func:`baskfy_api.broker_oauth.exchange_request_token` speaks
#: Kite's ``session/token`` checksum scheme, so Zerodha is the only broker for which a
#: ``request_token`` can be redeemed and a session stored. Redirecting anywhere else would put
#: Baskfy's Zerodha app key in another broker's URL and then fail at the callback — a login
#: that cannot complete, offered as though it could. Same class of over-claim as the holdings
#: note this leaf exists to fix; refuse honestly instead (Tree-5 leaf C1, handoff from C2).
_OAUTH_COMPLETABLE: frozenset[str] = frozenset({"zerodha"})


class BrokerCapabilitiesOut(BaseModel):
    oauth: str
    holdings_sync: str
    trading: str


class BrokerOut(BaseModel):
    id: str
    name: str
    short_name: str
    mark: str
    color: str
    blurb: str
    api_name: str
    docs_url: str
    capabilities: BrokerCapabilitiesOut
    sort_order: int
    connected: bool = False
    connection_status: str = "not_connected"
    adapter_wired: bool = False


class BrokerGateOut(BaseModel):
    live_oauth_enabled: bool
    requirement: str
    signed_off: bool
    decision_reference: str
    #: Whether this deployment actually holds Kite **Connect** credentials.
    #:
    #: Separate from ``live_oauth_enabled``, which is the D3 *policy* gate, because they answer
    #: different questions and used to be conflated. D3 says "connecting a broker is allowed";
    #: this says "we have the credential to do it". Baskfy runs on Kite **Publisher** — the free
    #: product that hands a basket to the user's own Kite — and Publisher issues no API secret, so
    #: this is `false` and the grid must not offer a login it cannot finish. `NEEDS-MAULIK.md` §28.
    connect_configured: bool = False


class BrokerListOut(BaseModel):
    gate: BrokerGateOut
    brokers: list[BrokerOut]
    adapters_wired: int


class ConnectOut(BaseModel):
    broker_id: str
    oauth_available: bool
    redirect_url: str | None = None
    state: str | None = None
    reason: str = Field(
        description="Why connect cannot start, when oauth_available is false. Empty when it can."
    )


class CallbackOut(BaseModel):
    """The result of one finished callback. Only ``connected: true`` is a broker session.

    Modelled on ``SyncHoldingsOut`` deliberately: a machine-readable provenance field, a
    boolean that says whether the thing is real, and prose that repeats the same statement
    for a human — because Kite redirects the *browser* here, so this body is read by a person
    as often as by a client.
    """

    broker_id: str
    connected: bool = Field(
        description=(
            "True only when a real Kite session was exchanged and stored. A simulated login "
            "is `false`: the flow ran, and there is no broker session behind it."
        )
    )
    token_stored: bool = Field(
        description=(
            "True when a token was written. Read it with `simulated` — a stored simulated "
            "token lives in a separate file that nothing reads, and is not a session."
        )
    )
    simulated: bool = Field(
        description="True when the access token was minted by the DRY_RUN / missing-secret stub."
    )
    holdings_synced: int = Field(
        default=0,
        description=(
            "Holdings written into the broker's holding group during this callback. The session is "
            "only just alive and Kite ends it at the start of the next trading day, so connect is "
            "the moment there is certainly something to read; 0 means the read was refused or "
            "failed, and `note` says which."
        ),
    )
    note: str = Field(
        description=(
            "The same statement as `connected` and `simulated`, in prose for a human. "
            "Never an order path."
        )
    )


# Named for the broker deliberately: `baskfy_api.schemas` already defines a `HoldingOut`, and
# two response models sharing a name make the OpenAPI generator emit qualified keys, which
# silently breaks the hand-written client the moment the document is regenerated.
class BrokerHoldingOut(BaseModel):
    """A holding as the broker reports it."""

    """Wire shape mirroring ``baskfy_execution.broker_ports.HoldingRow`` (+ documented total)."""

    symbol: str
    exchange: str
    quantity: Decimal
    t1_quantity: Decimal
    collateral_quantity: Decimal
    average_price: Decimal
    last_price: Decimal | None = None
    product: str = "CNC"
    total_quantity: Decimal = Field(
        description="quantity + t1_quantity + collateral_quantity (desk non-negotiable #2)."
    )


class SyncHoldingsOut(BaseModel):
    broker_id: str
    holdings: list[BrokerHoldingOut]
    dry_run: bool
    persisted: bool = Field(
        description=(
            "Whether these rows were written into the broker's holding group, which is what "
            "makes them appear on the Portfolio page. False for anything but a live read: a "
            "fixture behind a real rupee total is indistinguishable from your own positions."
        ),
    )
    written: int = Field(description="Holdings written to the portfolio.")
    portfolio_id: int | None = Field(description="The broker's holding group, once one exists.")
    sync_note: str = Field(
        description=(
            "What the persistence step did, in prose. Separate from `note`, which describes "
            "where the rows came from — two different questions."
        ),
    )
    unresolved: list[str] = Field(
        description=(
            "Symbols the broker reported that this build could not resolve to an instrument, "
            "named rather than dropped silently. Each costs one row, not the sync."
        ),
    )
    source: HoldingsSource = Field(
        description=(
            "Provenance of `holdings`, as a machine-readable value a client can switch on. "
            "`live` = the broker actually reported these rows; `fixture` = fabricated sample "
            "rows; `empty` = no rows at all (never carries any); `unwired` = this broker has "
            "no holdings adapter. Only `live` is the caller's real money."
        )
    )
    degraded: bool = Field(
        default=False,
        description=(
            "True when a live fetch was expected to work and did not, so `source` is a "
            "fallback rather than a deliberate stub. A degraded fixture is still a fixture; "
            "this is the second axis that tells the two apart."
        ),
    )
    note: str = Field(
        description=(
            "The same statement as `source`, in prose for a human. Always names `source`. "
            "Never an order path."
        )
    )


def _connect_configured() -> bool:
    """Can a **user** complete a broker login *here*? Three things, not two.

    The key and the secret are necessary — the login ends at ``session/token`` and its checksum
    needs both. They are not sufficient, and treating them as such is a trap this deployment walks
    straight into.

    **The redirect has to come back to us.** Baskfy uses the momentum desk's Kite app (one app,
    one registered redirect, pointed at ``desk.modelbasket.in/callback``) so that its *data* paths
    — the deep backfill, `fetch_daily_bars` — can run on the desk's daily session, carried across
    by `baskfy_worker.kite_session_cli`. Those credentials being present says nothing about
    whether a person clicking "Connect" lands back on Baskfy. They would land on the desk.

    So configuring Kite for the bridge must not switch on a user-facing button that cannot
    finish. That is the exact failure this field was added to prevent, and it would have been
    reintroduced by the thing that made the bridge work. `docs/DECISIONS-MERGE.md` M57.

    Read at call time rather than at import: the process is long-lived and an operator who adds a
    credential should not have to restart the API to make the grid tell the truth.
    """
    if not (
        os.environ.get("BASKFY_KITE_API_KEY", "").strip()
        and os.environ.get("BASKFY_KITE_API_SECRET", "").strip()
    ):
        return False

    settings = get_settings()
    redirect = (
        os.environ.get("BASKFY_BROKER_OAUTH_REDIRECT", "").strip()
        or f"{settings.web_origin.rstrip('/')}{OAUTH_CALLBACK_PATH}"
    )
    return redirect.startswith(settings.web_origin.rstrip("/"))


def _gate_out() -> BrokerGateOut:
    review = BROKER_OAUTH_REVIEW
    return BrokerGateOut(
        live_oauth_enabled=review.signed_off,
        requirement=review.requirement,
        signed_off=review.signed_off,
        decision_reference=review.decision_reference,
        connect_configured=_connect_configured(),
    )


def _connection_state(broker_id: str) -> tuple[bool, str]:
    """Is there a broker session behind this row, right now?

    `_broker_out` used to answer `False`/`"not_connected"` unconditionally, so a completed login
    was invisible: you connected, Kite came back, the token was written, and the page still said
    "Connect". Nothing in the catalog ever read the session back.

    What counts as connected is a token this deployment can actually use:

    * only for a broker whose OAuth is wired here — the other nine have no session to have;
    * the blob has to load, so an unreadable or absent file reads as disconnected rather than
      raising into a page that has nothing to do with tokens;
    * and a `sim_` token is NOT a connection. `exchange_request_token_stub` mints those in
      DRY_RUN, and `TestSimulatedTokenNeverPoisonsTheRealSession` exists because a simulated
      value that reads as real is the failure mode this whole module guards against. It reports
      `simulated` so the UI can say which it is instead of implying a broker is on the line.

    Deliberately no network call: this runs on every render of the brokers page, and "is the
    token still good with Kite" costs a round trip and can fail for reasons that have nothing to
    do with whether the user connected. Presence is what the page is asking about; the sync
    endpoint is where an expired token surfaces.
    """
    # `_OAUTH_COMPLETABLE`, not `_WIRED_AUTHORIZE`. Five brokers have an authorize URL, but the
    # blob this reads is Kite's — one store, keyed by BASKFY_KITE_*. Gating on the wider set made
    # a Zerodha token report Upstox as connected, which the test below caught.
    if broker_id not in _OAUTH_COMPLETABLE:
        return False, "not_connected"
    try:
        token = token_store_for().load()
    except CredentialsMissing:
        # `AccessTokenStore.load` funnels all three failures into this one type — no file, no
        # encryption key, key rotated so the blob will not decrypt. Every one of them means the
        # same thing to this page: there is no session. Caught by its own type rather than by a
        # bare `except`, which house rule 3 forbids, and narrow enough that a genuine bug in the
        # store still reaches the caller instead of being reported as "not connected".
        return False, "not_connected"
    if is_simulated_token(token.value):
        return False, "simulated"
    # Present is not alive (M79). Kite invalidates an access token at the start of the next
    # trading day, so the blob written yesterday reads back perfectly and is dead — and on the
    # morning of 2 Sep 2026 this reported "connected" while `holdings_for_broker` was already
    # answering `AccessTokenExpired` and serving a fixture. That is the same defect this function
    # was written to fix, one day later: the page claiming a session the server does not have.
    #
    # Still no network call. `is_expired` compares the issue date against the IST calendar day,
    # which is the boundary Kite actually uses, so the check stays local and costs nothing per
    # render. A live token that Kite has revoked early is beyond a local check; that surfaces at
    # the sync, which refuses anything that is not a `live` read.
    if token.is_expired():
        return False, "expired"
    return True, "connected"


def _broker_out(broker: BrokerDef) -> BrokerOut:
    connected, status = _connection_state(broker.id)
    return BrokerOut(
        id=broker.id,
        name=broker.name,
        short_name=broker.short_name,
        mark=broker.mark,
        color=broker.color,
        blurb=broker.blurb,
        api_name=broker.api_name,
        docs_url=broker.docs_url,
        capabilities=BrokerCapabilitiesOut(
            oauth=broker.capabilities.oauth,
            holdings_sync=broker.capabilities.holdings_sync,
            trading=broker.capabilities.trading,
        ),
        sort_order=broker.sort_order,
        connected=connected,
        connection_status=status,
        adapter_wired=broker.id in _WIRED_AUTHORIZE,
    )


def _bad_request(detail: str) -> Problem:
    return Problem(ProblemType.INVALID_SCREEN_DEFINITION, detail, errors=[{"message": detail}])


@router.get("", response_model=BrokerListOut, summary="Broker connect catalog")
async def list_brokers(principal: AuthenticatedDep) -> BrokerListOut:
    """The ten brokers on the grid. Requires a signed-in account; never starts OAuth."""
    principal.require_user()
    brokers = [_broker_out(b) for b in broker_catalog()]
    return BrokerListOut(
        gate=_gate_out(),
        brokers=brokers,
        adapters_wired=sum(1 for b in brokers if b.adapter_wired),
    )


@router.get(
    "/callback",
    response_model=CallbackOut,
    summary="OAuth callback — exchange request_token (state-validated; 503 if only simulable)",
)
async def oauth_callback(
    principal: AuthenticatedDep,
    session: SessionDep,
    request_token: Annotated[str, Query(min_length=8, max_length=128)],
    state: Annotated[str, Query(min_length=8, max_length=128)],
) -> CallbackOut:
    """Finish Zerodha login: validate ``state``, exchange ``request_token``, encrypt at rest.

    Rejects a missing / reused / foreign ``state``.

    **A login this deployment cannot actually complete is refused, not simulated** (leaf
    1.1.4). Under ``DRY_RUN``, or without ``BASKFY_KITE_API_KEY`` / ``BASKFY_KITE_API_SECRET``,
    the exchange can only mint a ``sim_`` stub — and this route is on the live path now that
    Kite's registered redirect points at it, so a real person finishing a real Kite login
    reaches this code. It used to write that stub over the shared token blob the desk bridge
    fills and answer ``connected: true``: one login destroyed the working session, replaced it
    with a value Kite rejects, and reported success. Now it answers 503 and stores nothing.

    ``BASKFY_BROKER_OAUTH_ALLOW_SIMULATED=true`` opts a demo box or an integration suite back
    into the simulated flow. Even then the stub goes to its own file, never the real session,
    and the response says ``connected: false`` — the flow ran; there is no broker behind it.
    """
    user_id = principal.require_user()
    if BROKER_OAUTH_REVIEW.blocks_live_oauth:
        raise _bad_request("Broker OAuth is not signed off; callback is closed.")

    pending = consume_oauth_state(state)
    if pending is None:
        raise _bad_request("Invalid or expired OAuth state.")
    if pending.user_id != user_id:
        raise _bad_request("OAuth state does not belong to this account.")
    if pending.broker_id not in _OAUTH_COMPLETABLE:
        # The exchange below is Kite's, and the token store is one shared blob: honouring a
        # state minted for another broker would file a Zerodha session under its name.
        raise _bad_request(
            f"Baskfy cannot complete an OAuth login for {pending.broker_id!r}; "
            "only Zerodha's token exchange is implemented."
        )

    exchange = exchange_request_token(
        api_key=os.environ.get("BASKFY_KITE_API_KEY", "").strip(),
        request_token=request_token,
        user_id=user_id,
    )
    if exchange.simulated:
        if not simulated_token_storage_enabled():
            # 503, matching `billing.py`'s "the payment gateway is not responding": nothing is
            # wrong with the request, and a 4xx would send the one person who cannot fix this
            # off to fix their own login. The reasons are named because the operator reading
            # them is the only one who can act, and the browser lands here directly.
            raise Problem(
                ProblemType.PIPELINE_DEGRADED,
                "This deployment cannot complete a broker login: "
                + "; ".join(exchange.reasons)
                + ". Nothing was stored and any existing broker session is untouched. This "
                "login's one-time state has been spent, so start the connect flow again once "
                "the deployment is configured; or set BASKFY_BROKER_OAUTH_ALLOW_SIMULATED=true "
                "to exercise the flow with a simulated session instead.",
                reasons=list(exchange.reasons),
            )
        # Opted in, and still not the real store: `store_access_token` would refuse it.
        store_access_token(exchange.access_token, store=simulated_token_store_for())
        return CallbackOut(
            broker_id=pending.broker_id,
            connected=False,
            token_stored=True,
            simulated=True,
            note=(
                "simulated: the OAuth flow ran end to end and stored a fake session in a "
                "separate file. You are not connected to "
                f"{pending.broker_id}, and the real broker session was not touched "
                "(" + "; ".join(exchange.reasons) + ")."
            ),
        )

    store_access_token(exchange.access_token, store=token_store_for())

    # PULL THE HOLDINGS NOW, NOT WHEN SOMEBODY REMEMBERS TO PRESS A BUTTON (M81).
    #
    # The session is only just alive and Kite ends it at the start of the next trading day, so this
    # is the moment there is certainly something to read. Leaving it to a manual Sync meant a fresh
    # login showed an empty Portfolio — "Holdings not synced yet" beside a broker that had just
    # connected — and the daily expiry makes that the normal state every morning, not an edge case.
    #
    # Best-effort by construction. A holdings read that fails must not fail the login: the token is
    # already stored and valid, the person is connected, and telling them otherwise would throw
    # away a working session over a data fetch. The outcome is reported in `note` instead, and the
    # Sync button remains for a re-read.
    synced = 0
    sync_note = ""
    try:
        result = holdings_for_broker(pending.broker_id)
        if is_persistable(result):
            broker_account_id = await ensure_default_broker_account(
                session, user_id, broker_id=pending.broker_id
            )
            sync = await sync_holdings_into_portfolio(
                session,
                result,
                user_id=user_id,
                broker_account_id=broker_account_id,
                broker_name=pending.broker_id,
                as_of=dt.datetime.now(tz=IST).date(),
            )
            await session.commit()
            synced, sync_note = sync.written, sync.reason
        else:
            sync_note = not_persisted(result).reason
    except Exception as exc:
        await session.rollback()
        sync_note = f"holdings were not read on connect ({type(exc).__name__}); use Sync holdings."

    return CallbackOut(
        broker_id=pending.broker_id,
        connected=True,
        token_stored=True,
        simulated=False,
        holdings_synced=synced,
        note=(
            f"live: a real {pending.broker_id} session was exchanged and stored encrypted. "
            f"{sync_note}"
        ),
    )


@router.get("/{broker_id}", response_model=BrokerOut, summary="One broker on the catalog")
async def get_one_broker(
    principal: AuthenticatedDep,
    broker_id: Annotated[str, Path(min_length=2, max_length=32)],
) -> BrokerOut:
    principal.require_user()
    broker = get_broker(broker_id)
    if broker is None:
        raise Problem(ProblemType.NOT_FOUND, f"No broker with id {broker_id!r}.")
    return _broker_out(broker)


@router.post(
    "/{broker_id}/connect",
    response_model=ConnectOut,
    summary="Start broker OAuth (gated on D3)",
)
async def connect_broker(
    principal: AuthenticatedDep,
    broker_id: Annotated[str, Path(min_length=2, max_length=32)],
) -> ConnectOut:
    """Begin the redirect flow, or explain why it cannot start.

    A closed gate is ``oauth_available: false`` with the requirement as ``reason`` — never a
    redirect, and never a stored token. That is Track C in ``docs/smallcase/02``.
    """
    user_id = principal.require_user()
    broker = get_broker(broker_id)
    if broker is None:
        raise Problem(ProblemType.NOT_FOUND, f"No broker with id {broker_id!r}.")

    if BROKER_OAUTH_REVIEW.blocks_live_oauth:
        return ConnectOut(
            broker_id=broker_id,
            oauth_available=False,
            reason=(
                "Live broker login opens after Baskfy's regulatory posture (D3) is recorded. "
                "The catalog and this page are ready; the redirect is not."
            ),
        )

    authorize_base = _WIRED_AUTHORIZE.get(broker_id)
    if authorize_base is None:
        return ConnectOut(
            broker_id=broker_id,
            oauth_available=False,
            reason=f"{broker.name} is on the catalog; its OAuth adapter is not wired yet.",
        )

    if broker_id not in _OAUTH_COMPLETABLE:
        # Refused *before* the app key is read, so a Zerodha credential can never be put in
        # another broker's authorize URL.
        return ConnectOut(
            broker_id=broker_id,
            oauth_available=False,
            reason=(
                f"{broker.name}'s authorize URL is known, but Baskfy holds no {broker.name} "
                "app credential and cannot redeem its request_token, so the login would "
                "start and could not finish. Zerodha is the only broker whose OAuth "
                "completes today."
            ),
        )

    # Both, and named separately, because the two failures are the same sentence to the code and
    # completely different sentences to whoever has to fix it.
    #
    # The old message was "The {broker} app key is not configured on this deployment." It reads as
    # "nobody pasted a key" — and on 27 Aug 2026 it was shown to someone who had pasted two, both
    # correct, neither of them the kind this endpoint needs. Kite sells two products: **Publisher**
    # (free; embeds a basket the user confirms in their own Kite) and **Connect** (paid; the REST
    # API, and the only one that can redeem a request_token at `session/token`). A Publisher key is
    # a perfectly good credential that can never satisfy this route, and the message said nothing
    # that would tell you so.
    api_key = os.environ.get("BASKFY_KITE_API_KEY", "").strip()
    api_secret = os.environ.get("BASKFY_KITE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        absent = [
            name
            for name, value in (
                ("BASKFY_KITE_API_KEY", api_key),
                ("BASKFY_KITE_API_SECRET", api_secret),
            )
            if not value
        ]
        missing = " and ".join(absent)
        # Agreement, because one missing variable and two are both ordinary states here and
        # "BASKFY_KITE_API_KEY and BASKFY_KITE_API_SECRET is not set" is the kind of sentence that
        # makes a reader wonder what else was not read carefully.
        verb = "is" if len(absent) == 1 else "are"
        return ConnectOut(
            broker_id=broker_id,
            oauth_available=False,
            reason=(
                f"Connecting {broker.name} needs a Kite **Connect** app, and {missing} "
                f"{verb} not set on this deployment. A Kite **Publisher** key will not do: "
                "Publisher "
                f"embeds a basket you confirm inside Kite and issues no API secret, so it cannot "
                f"complete the token exchange this login ends with. Baskfy's basket hand-off uses "
                f"Publisher and is unaffected."
            ),
        )

    state = secrets.token_urlsafe(24)
    register_oauth_state(state=state, user_id=user_id, broker_id=broker_id)
    # The fallback was `https://baskfy.com/brokers/callback`, which was wrong twice over and in
    # ways that only surface at the end of a login the user has already committed to:
    #
    #   * `baskfy.com` (the apex) has no DNS record — only `staging.baskfy.com` resolves — so the
    #     browser would be handed a redirect to a host that does not exist;
    #   * `/brokers/callback` is not a route. The callback this service serves is
    #     `/api/v1/brokers/callback`; `/brokers` is the *page*, and an unknown path under it just
    #     bounces through the sign-in gate.
    #
    # Derived from `web_origin` rather than hard-coded, so a deployment that moves host keeps a
    # coherent redirect without a second setting to remember. `BASKFY_BROKER_OAUTH_REDIRECT` still
    # overrides, because the value must match what is registered in the Kite console exactly and
    # only the operator knows what they registered.
    # No `redirect_uri` is sent. Kite Connect uses the redirect REGISTERED against the app and
    # ignores one supplied at login time, so passing it only looked like it was doing something.
    # Where the value still matters is `_connect_configured()`, which compares the registered
    # redirect against `web_origin` to decide whether a login started here can finish here —
    # that check is the reason `BASKFY_BROKER_OAUTH_REDIRECT` exists, and it is unchanged.
    # `state` travels in `redirect_params`, NOT as a top-level param. Kite ignores query keys it
    # does not know and echoes back only what `redirect_params` carries, so the previous
    # `{"state": state}` was dropped on the way out — every Baskfy-initiated login returned with
    # no state and the callback refused it as one it had not started. Leaf 1.1.1 predicted this
    # exactly ("`redirect_params` appears zero times in the repo") and it went unacted on until
    # Maulik hit it. `redirect_uri` is likewise informational: Kite uses the app's REGISTERED
    # redirect, not one supplied at login time.
    query = urlencode(
        {
            "api_key": api_key,
            "v": "3",
            "redirect_params": urlencode({"state": state}),
        }
    )
    return ConnectOut(
        broker_id=broker_id,
        oauth_available=True,
        redirect_url=f"{authorize_base}?{query}",
        state=state,
        reason="",
    )


def _holdings_note(result: HoldingsResult, *, broker_name: str, dry_run: bool) -> str:
    """Prose that says the same thing ``source`` says — and names it, so the two cannot drift.

    Every branch embeds ``result.source`` verbatim; a note that disagreed with the field it
    describes would recreate the defect this leaf exists to fix, one layer up.
    """
    source = result.source
    if source == "live":
        text = f"live holdings reported by {broker_name}"
    elif source == "unwired":
        text = f"unwired: {broker_name} has no holdings adapter in this build"
    elif source == "fixture" and result.degraded:
        text = (
            f"degraded: {broker_name} could not be reached, so these are fixture holdings "
            "— sample numbers, not your holdings"
        )
    elif source == "fixture":
        text = "fixture holdings (DRY_RUN or BASKFY_BROKER_HOLDINGS_FIXTURE)"
    elif result.degraded:
        text = (
            f"degraded: {broker_name} could not be reached and no fixture is configured, "
            "so the list is empty"
        )
    elif dry_run:
        text = "DRY_RUN: empty holdings (set BASKFY_BROKER_HOLDINGS_FIXTURE for a fixture list)"
    else:
        text = "empty: no holdings were returned for this broker"
    return f"{text} ({result.detail})" if result.detail else text


@router.post(
    "/{broker_id}/sync-holdings",
    response_model=SyncHoldingsOut,
    summary="Sync broker holdings (HoldingRow shape; DRY_RUN safe)",
)
async def sync_holdings(
    principal: AuthenticatedDep,
    session: SessionDep,
    broker_id: Annotated[str, Path(min_length=2, max_length=32)],
) -> SyncHoldingsOut:
    """Return holdings for the sole-tenant caller, labelled with where they came from.

    ``source`` is the load-bearing field: ``live`` means the broker actually reported these
    rows and they are the caller's real money; ``fixture`` means they are fabricated;
    ``empty`` and ``unwired`` carry nothing. ``degraded`` says whether a fixture was a
    deliberate stub or a fallback after a live fetch failed. Quantity fields follow desk
    non-negotiable #2 (qty + t1 + collateral). Under DRY_RUN or without a live session this
    is empty or a fixture — never crashes, never places an order.
    """
    user_id = principal.require_user()
    broker = get_broker(broker_id)
    if broker is None:
        raise Problem(ProblemType.NOT_FOUND, f"No broker with id {broker_id!r}.")

    result = holdings_for_broker(broker_id)
    holdings = [BrokerHoldingOut.model_validate(holding_row_to_dict(row)) for row in result.rows]
    dry = dry_run_enabled()

    # M75: this used to end here, returning the rows and writing nothing, so a connected broker
    # left the Portfolio page reading "Holdings not synced yet" permanently. The write goes to a
    # holding group the broker owns, so a person's own portfolios are never touched by a poll.
    # Asked before any database work. A fixture, an unwired broker or an empty read is refused,
    # and refusing must not cost a `broker_account` row written on the way to saying no.
    if is_persistable(result):
        broker_account_id = await ensure_default_broker_account(
            session, user_id, broker_id=broker_id
        )
        sync = await sync_holdings_into_portfolio(
            session,
            result,
            user_id=user_id,
            broker_account_id=broker_account_id,
            broker_name=broker.name,
            as_of=dt.datetime.now(tz=IST).date(),
        )
        await session.commit()
    else:
        sync = not_persisted(result)

    return SyncHoldingsOut(
        broker_id=broker_id,
        holdings=holdings,
        dry_run=dry,
        persisted=sync.persisted,
        written=sync.written,
        portfolio_id=sync.portfolio_id,
        unresolved=list(sync.unresolved),
        source=result.source,
        degraded=result.degraded,
        # `note` is left exactly as it was. It describes the PROVENANCE of the rows, and
        # `test_the_router_still_names_the_fixture_env_var_for_a_real_fixture` asserts it byte for
        # byte on purpose. Appending the sync outcome here broke that, and it was the wrong place
        # anyway: whether the rows were written down is a different question from where they came
        # from, and it has its own fields above.
        note=_holdings_note(result, broker_name=broker.name, dry_run=dry),
        sync_note=sync.reason,
    )
