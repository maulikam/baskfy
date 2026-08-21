"""The Razorpay gateway — docs/02 §Payments, docs/07 §"Account & billing" (Prompt 13 §2).

    "Razorpay integration: subscriptions for monthly/yearly, one-time order for forever;
     /checkout/session; a signature-verified, idempotent /webhooks/razorpay that is safe to
     replay" — PROMPTS.md Prompt 13 §2.

Two things live here and nothing else: **signature verification**, which is pure and therefore
directly testable, and a **thin HTTP client** for the three calls a checkout needs.

Why no ``razorpay`` SDK
-----------------------
docs/02 locks the stack and names Razorpay as the payment provider, not a library. The official
``razorpay`` package is a synchronous ``requests`` wrapper — it would block the event loop on
every checkout, ships no ``py.typed``, and wraps three endpoints we call. ``httpx`` is already a
dependency of this service (the Resend client uses it) and is async. Recorded in
``docs/DECISIONS.md``.

Signature schemes, which are different and are easy to confuse
--------------------------------------------------------------
* A **webhook** is signed with the *webhook secret* over the raw request body, and arrives in
  ``X-Razorpay-Signature``.
* A **checkout callback** is signed with the *API key secret* over ``order_id|payment_id`` (or
  ``payment_id|subscription_id`` for a subscription).

Both are HMAC-SHA256 compared with :func:`hmac.compare_digest`, because a timing-variable
comparison of a MAC is how a MAC gets forged.

Nothing in this module is reachable from the test suite's network path: ``network_guard.py``
blocks outbound sockets, and every test injects a transport.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from decimal import Decimal
from typing import Final

import httpx

from decile_api.settings import Settings

log = logging.getLogger(__name__)

#: Razorpay's header on a webhook delivery.
SIGNATURE_HEADER: Final = "X-Razorpay-Signature"
#: The event id Razorpay attaches to a delivery. The body carries no id of its own, so this is
#: what "idempotent by event id" (docs/07) is keyed on.
EVENT_ID_HEADER: Final = "X-Razorpay-Event-Id"

#: Razorpay speaks in paise, integers only.
PAISE_PER_RUPEE: Final = Decimal(100)


class RazorpayError(RuntimeError):
    """The gateway refused, or could not be reached."""


class PaymentsNotConfigured(RazorpayError):
    """No API keys. A laptop without credentials, not a failure of the gateway."""


def to_paise(amount_inr: Decimal) -> int:
    """₹500.00 -> 50000. Exact: the amount is a Decimal, never a float."""
    paise = (amount_inr * PAISE_PER_RUPEE).to_integral_value()
    if paise != amount_inr * PAISE_PER_RUPEE:
        raise ValueError(f"{amount_inr} is not a whole number of paise")
    return int(paise)


def from_paise(paise: int) -> Decimal:
    return (Decimal(paise) / PAISE_PER_RUPEE).quantize(Decimal("0.01"))


def expected_signature(payload: bytes, secret: str) -> str:
    """HMAC-SHA256, hex, lower case — Razorpay's own format."""
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_webhook_signature(payload: bytes, signature: str | None, secret: str) -> bool:
    """docs/11 §Security: "Razorpay webhooks: verify signature".

    An empty secret returns ``False`` rather than skipping the check. A deployment without one is
    refused at startup (:meth:`Settings.require_configured`); on a laptop, "no secret" must mean
    "no webhook is accepted", not "every webhook is accepted".
    """
    if not secret or not signature:
        return False
    return hmac.compare_digest(expected_signature(payload, secret), signature)


def verify_payment_signature(
    *, order_id: str, payment_id: str, signature: str, secret: str
) -> bool:
    """The browser callback's signature: HMAC over ``order_id|payment_id``."""
    if not secret or not signature:
        return False
    body = f"{order_id}|{payment_id}".encode()
    return hmac.compare_digest(expected_signature(body, secret), signature)


def verify_subscription_signature(
    *, payment_id: str, subscription_id: str, signature: str, secret: str
) -> bool:
    """The subscription callback's signature: HMAC over ``payment_id|subscription_id``."""
    if not secret or not signature:
        return False
    body = f"{payment_id}|{subscription_id}".encode()
    return hmac.compare_digest(expected_signature(body, secret), signature)


class RazorpayGateway:
    """The three calls a checkout makes. Async, and injectable end to end.

    ``transport`` exists so the contract tests drive a real ``httpx`` request/response cycle
    against a stub instead of the internet — the same shape the Resend client uses.
    """

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport

    @property
    def configured(self) -> bool:
        return self._settings.payments_configured()

    def _auth_header(self) -> str:
        raw = f"{self._settings.razorpay_key_id}:{self._settings.razorpay_key_secret}"
        return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")

    async def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        if not self.configured:
            raise PaymentsNotConfigured("Razorpay credentials are not configured.")
        url = f"{self._settings.razorpay_api_base.rstrip('/')}{path}"
        timeout = self._settings.razorpay_timeout_seconds
        client = (
            httpx.AsyncClient(timeout=timeout, transport=self._transport)
            if self._transport is not None
            else httpx.AsyncClient(timeout=timeout)
        )
        async with client:
            try:
                response = await client.post(
                    url, json=body, headers={"Authorization": self._auth_header()}
                )
            except httpx.HTTPError as exc:
                raise RazorpayError(f"could not reach Razorpay: {exc}") from exc
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # The body carries Razorpay's own description; the *request* carried our key, so
            # nothing from the request is logged here.
            raise RazorpayError(
                f"Razorpay refused {path} with {response.status_code}: {response.text[:300]}"
            )
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise RazorpayError(f"Razorpay returned a non-object for {path}")
        return parsed

    async def create_order(
        self, *, amount_inr: Decimal, receipt: str, notes: dict[str, str]
    ) -> dict[str, object]:
        """A one-time order — docs/02: "one-time for Forever"."""
        return await self._post(
            "/orders",
            {
                "amount": to_paise(amount_inr),
                "currency": "INR",
                "receipt": receipt,
                "notes": notes,
                # Razorpay's own idempotency: a repeated receipt returns the same order.
                "payment_capture": 1,
            },
        )

    async def create_subscription(
        self, *, plan_id: str, total_count: int, notes: dict[str, str], customer_id: str | None
    ) -> dict[str, object]:
        """A recurring subscription — docs/02: "subscriptions ... for monthly/yearly"."""
        body: dict[str, object] = {
            "plan_id": plan_id,
            "total_count": total_count,
            "customer_notify": 1,
            "notes": notes,
        }
        if customer_id:
            body["customer_id"] = customer_id
        return await self._post("/subscriptions", body)

    async def create_customer(self, *, name: str | None, email: str) -> dict[str, object]:
        """Razorpay's customer record, so a subscription is attached to a person.

        ``fail_existing: 0`` makes a repeat call return the existing customer rather than an
        error, which is what makes a retried checkout safe.
        """
        return await self._post(
            "/customers", {"name": name or email, "email": email, "fail_existing": 0}
        )
