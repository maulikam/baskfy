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
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, Field

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.broker_holdings import holding_row_to_dict, holdings_for_broker
from baskfy_api.broker_oauth import (
    consume_oauth_state,
    dry_run_enabled,
    exchange_request_token,
    register_oauth_state,
    store_access_token,
    token_store_for,
)
from baskfy_api.problems import Problem, ProblemType
from baskfy_core.broker_connections import (
    BROKER_OAUTH_REVIEW,
    BrokerDef,
    broker_catalog,
    get_broker,
)

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


class HoldingOut(BaseModel):
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
    holdings: list[HoldingOut]
    dry_run: bool
    note: str = Field(
        description="How the list was produced (live / fixture / empty). Never an order path."
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

    api_key = os.environ.get("BASKFY_KITE_API_KEY", "").strip()
    if not api_key:
        return ConnectOut(
            broker_id=broker_id,
            oauth_available=False,
            reason="Zerodha app key is not configured on this deployment.",
        )

    state = secrets.token_urlsafe(24)
    register_oauth_state(state=state, user_id=user_id, broker_id=broker_id)
    redirect_uri = os.environ.get(
        "BASKFY_BROKER_OAUTH_REDIRECT",
        "https://baskfy.com/brokers/callback",
    )
    query = urlencode({"api_key": api_key, "v": "3", "redirect_uri": redirect_uri, "state": state})
    return ConnectOut(
        broker_id=broker_id,
        oauth_available=True,
        redirect_url=f"{authorize_base}?{query}",
        state=state,
        reason="",
    )


@router.post(
    "/{broker_id}/sync-holdings",
    response_model=SyncHoldingsOut,
    summary="Sync broker holdings (HoldingRow shape; DRY_RUN safe)",
)
async def sync_holdings(
    principal: AuthenticatedDep,
    broker_id: Annotated[str, Path(min_length=2, max_length=32)],
) -> SyncHoldingsOut:
    """Return holdings for the sole-tenant caller.

    Quantity fields follow desk non-negotiable #2 (qty + t1 + collateral). Under DRY_RUN or
    without a live session this returns ``[]`` or an optional fixture — never crashes, never
    places an order.
    """
    principal.require_user()
    broker = get_broker(broker_id)
    if broker is None:
        raise Problem(ProblemType.NOT_FOUND, f"No broker with id {broker_id!r}.")

    rows = holdings_for_broker(broker_id)
    holdings = [HoldingOut.model_validate(holding_row_to_dict(row)) for row in rows]
    dry = dry_run_enabled()
    if holdings:
        note = "fixture holdings (DRY_RUN or BASKFY_BROKER_HOLDINGS_FIXTURE)"
    elif dry:
        note = "DRY_RUN: empty holdings (set BASKFY_BROKER_HOLDINGS_FIXTURE for a fixture list)"
    else:
        note = "no live holdings available for this broker yet"
    return SyncHoldingsOut(broker_id=broker_id, holdings=holdings, dry_run=dry, note=note)
