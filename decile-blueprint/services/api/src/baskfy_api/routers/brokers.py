"""``/brokers`` — the connect catalog (M41 / P5.8), OAuth callback + holdings sync (Tree-3).

    GET  /brokers                         every broker on the grid, plus the D3 gate status
    GET  /brokers/{id}                    one broker
    POST /brokers/{id}/connect            start OAuth — only when the gate is open and wired
    GET  /brokers/callback                exchange request_token (state-validated)
    POST /brokers/{id}/sync-holdings      holdings shaped like HoldingRow (DRY_RUN safe)

The catalog is always served to a signed-in account. Live authorize redirects require
``BROKER_OAUTH_REVIEW.signed_off`` (D3 posture B). The web app still never places orders —
callback and sync-holdings store / read credentials only; they never touch the order path.
"""

from __future__ import annotations

import os
import secrets
from decimal import Decimal
from typing import Annotated, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, Field

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.broker_holdings import (
    HoldingsResult,
    HoldingsSource,
    holding_row_to_dict,
    holdings_for_broker,
)
from baskfy_api.broker_oauth import (
    consume_oauth_state,
    dry_run_enabled,
    exchange_request_token,
    register_oauth_state,
    store_access_token,
    token_store_for,
)
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.settings import get_settings
from baskfy_core.broker_connections import (
    BROKER_OAUTH_REVIEW,
    BrokerDef,
    broker_catalog,
    get_broker,
)

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
    broker_id: str
    connected: bool
    token_stored: bool
    simulated: bool = Field(
        description="True when the access token was minted by the DRY_RUN / missing-secret stub."
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


def _gate_out() -> BrokerGateOut:
    review = BROKER_OAUTH_REVIEW
    return BrokerGateOut(
        live_oauth_enabled=review.signed_off,
        requirement=review.requirement,
        signed_off=review.signed_off,
        decision_reference=review.decision_reference,
    )


def _broker_out(broker: BrokerDef) -> BrokerOut:
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
        connected=False,
        connection_status="not_connected",
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
    summary="OAuth callback — exchange request_token (state-validated)",
)
async def oauth_callback(
    principal: AuthenticatedDep,
    request_token: Annotated[str, Query(min_length=8, max_length=128)],
    state: Annotated[str, Query(min_length=8, max_length=128)],
) -> CallbackOut:
    """Finish Zerodha login: validate ``state``, exchange ``request_token``, encrypt at rest.

    Rejects a missing / reused / foreign ``state``. Under ``DRY_RUN`` or without
    ``BASKFY_KITE_API_SECRET``, stores a simulated token blob (never calls live Kite).
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

    api_key = os.environ.get("BASKFY_KITE_API_KEY", "").strip() or "dry-run-api-key"
    simulated = dry_run_enabled() or not os.environ.get("BASKFY_KITE_API_SECRET", "").strip()
    access_token = exchange_request_token(
        api_key=api_key,
        request_token=request_token,
        user_id=user_id,
    )
    store_access_token(access_token, store=token_store_for())
    return CallbackOut(
        broker_id=pending.broker_id,
        connected=True,
        token_stored=True,
        simulated=simulated,
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
        missing = " and ".join(
            name
            for name, value in (
                ("BASKFY_KITE_API_KEY", api_key),
                ("BASKFY_KITE_API_SECRET", api_secret),
            )
            if not value
        )
        return ConnectOut(
            broker_id=broker_id,
            oauth_available=False,
            reason=(
                f"Connecting {broker.name} needs a Kite **Connect** app, and {missing} "
                f"is not set on this deployment. A Kite **Publisher** key will not do: Publisher "
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
    settings = get_settings()
    redirect_uri = os.environ.get("BASKFY_BROKER_OAUTH_REDIRECT", "").strip() or (
        f"{settings.web_origin.rstrip('/')}{OAUTH_CALLBACK_PATH}"
    )
    query = urlencode({"api_key": api_key, "v": "3", "redirect_uri": redirect_uri, "state": state})
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
    principal.require_user()
    broker = get_broker(broker_id)
    if broker is None:
        raise Problem(ProblemType.NOT_FOUND, f"No broker with id {broker_id!r}.")

    result = holdings_for_broker(broker_id)
    holdings = [BrokerHoldingOut.model_validate(holding_row_to_dict(row)) for row in result.rows]
    dry = dry_run_enabled()
    return SyncHoldingsOut(
        broker_id=broker_id,
        holdings=holdings,
        dry_run=dry,
        source=result.source,
        degraded=result.degraded,
        note=_holdings_note(result, broker_name=broker.name, dry_run=dry),
    )
