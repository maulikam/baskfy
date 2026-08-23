"""SC10 Track B — flags default off; paywall/signup/fee-collect 404; Free Access."""

from __future__ import annotations

import pytest

from baskfy_api.app import create_app
from baskfy_api.problems import Problem
from baskfy_api.routers import track_b as track_b_router
from baskfy_api.settings import Settings
from baskfy_api.track_b import free_access


def test_track_b_flags_default_false() -> None:
    settings = Settings()
    assert settings.subscriptions_enabled is False
    assert settings.fee_collection_enabled is False
    assert settings.public_signup_enabled is False
    assert Settings.model_fields["subscriptions_enabled"].default is False
    assert Settings.model_fields["fee_collection_enabled"].default is False
    assert Settings.model_fields["public_signup_enabled"].default is False


def test_free_access_while_subscriptions_off() -> None:
    off = Settings()
    assert off.subscriptions_enabled is False
    assert free_access(basket_access="FEE", settings=off) is True
    assert free_access(basket_access="FREE", settings=off) is True


def test_free_access_respects_label_when_subscriptions_on() -> None:
    on = Settings(subscriptions_enabled=True)
    assert free_access(basket_access="FEE", settings=on) is False
    assert free_access(basket_access="FREE", settings=on) is True


@pytest.mark.asyncio
async def test_paywall_and_signup_and_fee_collect_404_when_flags_off() -> None:
    off = Settings()
    with pytest.raises(Problem) as paywall:
        await track_b_router.paywall(off)
    assert paywall.value.status == 404

    with pytest.raises(Problem) as checkout:
        await track_b_router.paywall_checkout(off)
    assert checkout.value.status == 404

    with pytest.raises(Problem) as signup:
        await track_b_router.public_signup(off)
    assert signup.value.status == 404

    with pytest.raises(Problem) as fees:
        await track_b_router.fee_collect(off)
    assert fees.value.status == 404


@pytest.mark.asyncio
async def test_track_b_free_access_route_reports_free_while_off() -> None:
    off = Settings()
    body = await track_b_router.track_b_free_access(off, basket_access="FEE")
    assert body.basket_access == "FEE"
    assert body.free_access is True


@pytest.mark.asyncio
async def test_track_b_flags_route_mirrors_defaults() -> None:
    off = Settings()
    body = await track_b_router.track_b_flags(off)
    assert body.subscriptions_enabled is False
    assert body.fee_collection_enabled is False
    assert body.public_signup_enabled is False


def test_openapi_lists_dark_surfaces() -> None:
    paths = create_app().openapi()["paths"]
    assert "/api/v1/cb/paywall" in paths
    assert "/api/v1/cb/public-signup" in paths
    assert "/api/v1/cb/fees/collect" in paths
