"""Shared setup for the billing contract tests (Prompt 13).

Two things every test in that suite needs and neither is worth repeating: an application whose
Razorpay gateway is a stub, and a correctly signed webhook body.

**Nothing here reaches Razorpay.** `network_guard.py` blocks outbound sockets for the whole suite;
the gateway is driven by an `httpx.MockTransport`, so the request/response cycle is real and the
destination is not.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

import api_helpers
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.razorpay import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    RazorpayGateway,
    expected_signature,
)
from baskfy_api.settings import Settings
from baskfy_core.models import AppUser, Subscription

WEBHOOK_SECRET: Final = "webhook-secret-for-tests"
KEY_ID: Final = "rzp_test_decile"
KEY_SECRET: Final = "rzp_test_secret"

#: The ids the stub gateway hands back. Fixed so a test can assert on them.
ORDER_ID: Final = "order_TESTORDER0001"
SUBSCRIPTION_ID: Final = "sub_TESTSUBSCR0001"
CUSTOMER_ID: Final = "cust_TESTCUSTOMER1"
PAYMENT_ID: Final = "pay_TESTPAYMENT001"


def _stub_response(request: httpx.Request) -> httpx.Response:
    """Razorpay's own response shapes for the three calls a checkout makes."""
    if request.url.path.endswith("/orders"):
        return httpx.Response(200, json={"id": ORDER_ID, "entity": "order", "status": "created"})
    if request.url.path.endswith("/subscriptions"):
        return httpx.Response(
            200, json={"id": SUBSCRIPTION_ID, "entity": "subscription", "status": "created"}
        )
    if request.url.path.endswith("/customers"):
        return httpx.Response(200, json={"id": CUSTOMER_ID, "entity": "customer"})
    return httpx.Response(404, json={"error": {"description": "no such route"}})


def stub_gateway(settings: Settings) -> RazorpayGateway:
    return RazorpayGateway(settings, transport=httpx.MockTransport(_stub_response))


def refusing_gateway(settings: Settings, status: int = 400) -> RazorpayGateway:
    """A gateway that answers an error, so the 5xx path is exercised rather than assumed."""

    def _refuse(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, json={"error": {"description": "declined"}})

    return RazorpayGateway(settings, transport=httpx.MockTransport(_refuse))


def billing_settings(database_url: str, invoice_dir: Path, **overrides: object) -> Settings:
    """`api_helpers.api_settings` plus credentials, a supplier and a private invoice directory."""
    return api_helpers.api_settings(
        database_url,
        razorpay_key_id=KEY_ID,
        razorpay_key_secret=KEY_SECRET,
        razorpay_webhook_secret=WEBHOOK_SECRET,
        razorpay_plan_id_monthly="plan_TESTMONTHLY01",
        razorpay_plan_id_yearly="plan_TESTYEARLY001",
        supplier_legal_name="Baskfy Analytics Private Limited",
        supplier_address_lines=("12 MG Road", "Bengaluru 560001"),
        supplier_gstin="29AAAAA0000A1Z5",
        supplier_state="Karnataka (29)",
        invoice_local_dir=str(invoice_dir),
        **overrides,
    )


@asynccontextmanager
async def billing_app(
    settings: Settings, session: AsyncSession, *, gateway: RazorpayGateway | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    async with api_helpers.running_app(
        settings, session, razorpay=gateway or stub_gateway(settings)
    ) as client:
        yield client


def signed(
    body: Mapping[str, object], *, event_id: str, secret: str = WEBHOOK_SECRET
) -> tuple[bytes, dict[str, str]]:
    """The exact bytes and headers Razorpay would deliver.

    The signature covers the *bytes*, so the body is serialised once here and posted verbatim —
    re-serialising it in the test would produce a different payload and a signature that is
    correct for something else.
    """
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return raw, {
        SIGNATURE_HEADER: expected_signature(raw, secret),
        EVENT_ID_HEADER: event_id,
        "content-type": "application/json",
    }


def payment_captured_event(
    *,
    user_public_id: str,
    plan_code: str,
    payment_id: str = PAYMENT_ID,
    amount_paise: int = 50_000,
    order_id: str = ORDER_ID,
) -> dict[str, object]:
    """Razorpay's `payment.captured`, in the shape their documentation gives it."""
    return {
        "entity": "event",
        "event": "payment.captured",
        "contains": ["payment"],
        "created_at": 1_787_000_000,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount_paise,
                    "currency": "INR",
                    "status": "captured",
                    "order_id": order_id,
                    "method": "upi",
                    "notes": {
                        "baskfy_user_public_id": user_public_id,
                        "baskfy_plan_code": plan_code,
                    },
                }
            }
        },
    }


def subscription_event(
    event: str,
    *,
    subscription_id: str = SUBSCRIPTION_ID,
    current_end: int | None = None,
) -> dict[str, object]:
    entity: dict[str, object] = {
        "id": subscription_id,
        "entity": "subscription",
        "status": event.split(".", maxsplit=1)[1],
    }
    if current_end is not None:
        entity["current_end"] = current_end
    return {
        "entity": "event",
        "event": event,
        "contains": ["subscription"],
        "payload": {"subscription": {"entity": entity}},
    }


async def grant(
    session: AsyncSession,
    user_id: int,
    plan_id: int,
    *,
    status: str = "active",
    period_end: dt.datetime | None = None,
) -> Subscription:
    """A subscription row in a chosen state, so the entitlement path can be driven directly."""
    subscription = Subscription(
        user_id=user_id,
        plan_id=plan_id,
        status=status,
        started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        current_period_end=period_end,
    )
    session.add(subscription)
    await session.flush()
    return subscription


async def user_named(session: AsyncSession, email: str) -> AppUser:
    """An `app_user` row, returned as the model rather than as ids."""
    await api_helpers.make_user(session, email)
    handle = email.split("@", maxsplit=1)[0].replace(".", "")[:12].ljust(12, "0")
    return (await session.execute(select(AppUser).where(AppUser.public_id == handle))).scalar_one()
