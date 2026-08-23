"""Track B dark surfaces — paywall / public signup / fee collect (SC10).

Routes are mounted so OpenAPI and the UI can name them, but each handler 404s while its
flag is false (docs/smallcase/02). Defaults stay off; do not flip them in settings.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from baskfy_api.settings import Settings, get_settings
from baskfy_api.track_b import (
    free_access,
    require_fee_collection_enabled,
    require_public_signup_enabled,
    require_subscriptions_enabled,
)

router = APIRouter(tags=["track-b"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


class FlagStateOut(BaseModel):
    subscriptions_enabled: bool
    fee_collection_enabled: bool
    public_signup_enabled: bool


class FreeAccessOut(BaseModel):
    basket_access: str
    free_access: bool


class EnabledStubOut(BaseModel):
    enabled: Literal[True] = True
    surface: str


@router.get("/cb/track-b/flags", response_model=FlagStateOut)
async def track_b_flags(settings: SettingsDep) -> FlagStateOut:
    """Read-only flag mirror for UI chips (Fee Based hidden while subscriptions off)."""
    return FlagStateOut(
        subscriptions_enabled=settings.subscriptions_enabled,
        fee_collection_enabled=settings.fee_collection_enabled,
        public_signup_enabled=settings.public_signup_enabled,
    )


@router.get("/cb/track-b/free-access", response_model=FreeAccessOut)
async def track_b_free_access(
    settings: SettingsDep,
    basket_access: str = "FEE",
) -> FreeAccessOut:
    """Free Access behaviour for a catalogue access label (Track B / SC10)."""
    return FreeAccessOut(
        basket_access=basket_access,
        free_access=free_access(basket_access=basket_access, settings=settings),
    )


@router.get("/cb/paywall", response_model=EnabledStubOut)
async def paywall(settings: SettingsDep) -> EnabledStubOut:
    require_subscriptions_enabled(settings)
    return EnabledStubOut(surface="paywall")


@router.post("/cb/paywall/checkout", response_model=EnabledStubOut)
async def paywall_checkout(settings: SettingsDep) -> EnabledStubOut:
    require_subscriptions_enabled(settings)
    return EnabledStubOut(surface="paywall-checkout")


@router.post("/cb/public-signup", response_model=EnabledStubOut)
async def public_signup(settings: SettingsDep) -> EnabledStubOut:
    require_public_signup_enabled(settings)
    return EnabledStubOut(surface="public-signup")


@router.post("/cb/fees/collect", response_model=EnabledStubOut)
async def fee_collect(settings: SettingsDep) -> EnabledStubOut:
    require_fee_collection_enabled(settings)
    return EnabledStubOut(surface="fee-collect")
