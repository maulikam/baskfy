"""``/brokers`` — the connect catalog (M41 / P5.8), live OAuth source-gated on D3.

    GET  /brokers                 every broker on the grid, plus the D3 gate status
    GET  /brokers/{id}            one broker
    POST /brokers/{id}/connect    start OAuth — only when the gate is open and the broker is wired

The catalog is always served to a signed-in account. Live redirects are not: until
``BROKER_OAUTH_REVIEW.signed_off`` is flipped in source, ``connect`` returns
``oauth_available: false`` and never builds an authorize URL. That matches
``docs/smallcase/02-scope-and-gating.md`` Track C (no third-party broker OAuth until D3).
"""

from __future__ import annotations

import os
import secrets
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Path
from pydantic import BaseModel, Field

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.problems import Problem, ProblemType
from baskfy_core.broker_connections import (
    BROKER_OAUTH_REVIEW,
    BrokerDef,
    broker_catalog,
    get_broker,
)

router = APIRouter(prefix="/brokers", tags=["brokers"])

#: Brokers whose authorize URL shape is known in this codebase today.
_WIRED_AUTHORIZE: dict[str, str] = {
    "zerodha": "https://kite.zerodha.com/connect/login",
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
    principal.require_user()
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
    redirect_uri = os.environ.get(
        "BASKFY_BROKER_OAUTH_REDIRECT",
        "https://baskfy.com/brokers/callback",
    )
    query = urlencode(
        {"api_key": api_key, "v": "3", "redirect_uri": redirect_uri, "state": state}
    )
    return ConnectOut(
        broker_id=broker_id,
        oauth_available=True,
        redirect_url=f"{authorize_base}?{query}",
        state=state,
        reason="",
    )
