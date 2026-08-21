"""Contract tests for plans, checkout, the Razorpay webhook and invoices — Prompt 13.

The three acceptance criteria this suite is responsible for:

* "Webhook replay test: the same event delivered five times produces exactly one payment row."
* "A test asserts a downgraded/expired user loses gated endpoints immediately (402 with
   upgrade_url) while retaining read access to their saved screens."
* "No price or entitlement is hard-coded in the web app; all read from the API" — the API half of
   it, that `GET /plans` actually publishes both.

The fourth ("invoice numbers are gapless and unique under concurrent payment creation") needs
genuinely concurrent transactions and lives in `test_invoice_numbering.py`.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
from pathlib import Path

import billing_helpers as helpers
import httpx
import pytest
import pytest_asyncio
from api_helpers import assert_problem, bearer, url
from screener_helpers import requires_db
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.logging import JsonFormatter, RedactingFilter
from baskfy_core.models import AppUser, Payment, Plan, Screen, Subscription, WebhookEvent
from baskfy_core.seed_data import EXAMPLE_SCREENS, PRE_PURCHASE_DISCLAIMERS

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

EXAMPLE_ID = EXAMPLE_SCREENS[0].public_id
MINIMAL = {"index": "nifty-500", "sort_by": "ret_12m"}


@pytest.fixture
def invoice_dir(tmp_path: Path) -> Path:
    """A private directory per test, so nothing writes into the repository's `.archive`."""
    return tmp_path / "invoices"


@pytest_asyncio.fixture
async def billing_api(
    seeded_url: str,
    screener_session: AsyncSession,
    clean_redis_namespaces: None,
    invoice_dir: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    del clean_redis_namespaces
    settings = helpers.billing_settings(seeded_url, invoice_dir)
    async with helpers.billing_app(settings, screener_session) as client:
        yield client


async def _account(session: AsyncSession, email: str) -> tuple[AppUser, dict[str, str]]:
    user = await helpers.user_named(session, email)
    return user, bearer(user.public_id)


async def _plan(session: AsyncSession, code: str) -> Plan:
    return (await session.execute(select(Plan).where(Plan.code == code))).scalar_one()


async def _payment_count(session: AsyncSession, payment_id: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Payment)
                .where(Payment.razorpay_payment_id == payment_id)
            )
        ).scalar_one()
    )


class TestThePlanCatalogue:
    """docs/07: `GET /plans`. Prompt 13 §1 and acceptance criterion 4."""

    async def test_it_is_public(self, billing_api: httpx.AsyncClient) -> None:
        """`/pricing` is a marketing page; a signed-out visitor must be able to read it."""
        assert (await billing_api.get(url("/plans"))).status_code == 200

    async def test_it_publishes_the_three_documented_plans(
        self, billing_api: httpx.AsyncClient
    ) -> None:
        """docs/01 §1: "Monthly ₹500 · Yearly ₹3,999 · Forever ₹14,999"."""
        body = (await billing_api.get(url("/plans"))).json()
        prices = {row["code"]: row["price_inr"] for row in body["data"]}
        assert prices == {"monthly": "500.00", "yearly": "3999.00", "forever": "14999.00"}

    async def test_prices_are_exact_strings_not_floats(
        self, billing_api: httpx.AsyncClient
    ) -> None:
        """CLAUDE.md house rule 9: money is `numeric`, never `float` — all the way to the wire."""
        body = (await billing_api.get(url("/plans"))).json()
        assert all(isinstance(row["price_inr"], str) for row in body["data"])

    async def test_it_carries_the_december_2026_prices(
        self, billing_api: httpx.AsyncClient
    ) -> None:
        """docs/01 §1: "rising to ₹899 / ₹5,999 / ₹19,999 in Dec 2026"."""
        body = (await billing_api.get(url("/plans"))).json()
        future = {row["code"]: row["price_from_dec_2026"] for row in body["data"]}
        assert future == {"monthly": "899.00", "yearly": "5999.00", "forever": "19999.00"}

    async def test_only_forever_carries_a_point_of_sale_disclosure(
        self, billing_api: httpx.AsyncClient
    ) -> None:
        """docs/11 §Compliance: the Forever plan "must state, at the point of sale, that it means
        the lifetime of the service"."""
        body = (await billing_api.get(url("/plans"))).json()
        disclosures = {row["code"]: row["disclosure"] for row in body["data"]}
        assert disclosures["monthly"] is None
        assert disclosures["yearly"] is None
        assert "lifetime of the website" in disclosures["forever"]

    async def test_it_carries_the_pre_purchase_disclaimers(
        self, billing_api: httpx.AsyncClient
    ) -> None:
        """docs/11 §Compliance: a disclaimer at checkout, and no advice language."""
        body = (await billing_api.get(url("/plans"))).json()
        assert body["disclaimers"] == list(PRE_PURCHASE_DISCLAIMERS)
        assert any("not a SEBI-registered" in line for line in body["disclaimers"])

    async def test_it_carries_the_entitlements_each_plan_grants(
        self, billing_api: httpx.AsyncClient
    ) -> None:
        """Acceptance criterion 4: the web app reads entitlements, it does not decide them."""
        body = (await billing_api.get(url("/plans"))).json()
        monthly = next(row for row in body["data"] if row["code"] == "monthly")
        assert monthly["entitlements"]["export_csv"] is True
        assert monthly["entitlements"]["api_access"] is False
        assert monthly["entitlements"]["max_screens"] == 50

    async def test_it_names_the_features_the_reference_product_gates(
        self, billing_api: httpx.AsyncClient
    ) -> None:
        """PROMPTS.md Prompt 13 §1: "screener, export, custom columns, historical ranks,
        backtests, community access, AMAs"."""
        body = (await billing_api.get(url("/plans"))).json()
        monthly = next(row for row in body["data"] if row["code"] == "monthly")
        labels = " ".join(entry["label"] for entry in monthly["features"]).lower()
        for expected in ("screener", "export", "column", "historical", "backtest", "slack", "ama"):
            assert expected in labels, expected

    async def test_the_free_tier_is_hidden_behind_its_flag(
        self, billing_api: httpx.AsyncClient
    ) -> None:
        """Prompt 13 §5: the ₹0 tier is optional and "behind a feature flag"."""
        body = (await billing_api.get(url("/plans"))).json()
        assert body["free_tier_enabled"] is False
        assert "free" not in {row["code"] for row in body["data"]}


class TestCheckout:
    """docs/07: `POST /checkout/session { plan_code } -> Razorpay order/subscription payload`."""

    async def test_it_needs_an_account(self, billing_api: httpx.AsyncClient) -> None:
        assert_problem(
            await billing_api.post(url("/checkout/session"), json={"plan_code": "monthly"}),
            401,
            "unauthenticated",
        )

    async def test_a_recurring_plan_creates_a_subscription(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/02: "subscriptions ... for monthly/yearly"."""
        _, headers = await _account(screener_session, "checkout1@example.com")
        body = (
            await billing_api.post(
                url("/checkout/session"), json={"plan_code": "monthly"}, headers=headers
            )
        ).json()
        assert body["kind"] == "subscription"
        assert body["subscription_id"] == helpers.SUBSCRIPTION_ID
        assert body["amount_inr"] == "500.00"
        assert body["key_id"] == helpers.KEY_ID

    async def test_forever_creates_a_one_time_order(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/02: "one-time for Forever"."""
        _, headers = await _account(screener_session, "checkout2@example.com")
        body = (
            await billing_api.post(
                url("/checkout/session"), json={"plan_code": "forever"}, headers=headers
            )
        ).json()
        assert body["kind"] == "order"
        assert body["order_id"] == helpers.ORDER_ID
        assert "lifetime of the website" in body["disclosure"]

    async def test_the_secret_never_leaves_the_service(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await _account(screener_session, "checkout3@example.com")
        response = await billing_api.post(
            url("/checkout/session"), json={"plan_code": "monthly"}, headers=headers
        )
        assert helpers.KEY_SECRET not in response.text

    async def test_the_pending_grant_entitles_nothing(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A checkout that was started and never paid must not unlock anything."""
        _, headers = await _account(screener_session, "checkout4@example.com")
        await billing_api.post(
            url("/checkout/session"), json={"plan_code": "monthly"}, headers=headers
        )
        me = (await billing_api.get(url("/me"), headers=headers)).json()
        assert me["entitlements"]["export_csv"] is False
        assert_problem(
            await billing_api.get(url(f"/screens/{EXAMPLE_ID}/csv"), headers=headers),
            402,
            "payment-required",
        )

    async def test_a_repeated_checkout_reuses_the_pending_row(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """An abandoned checkout must not leave a subscription row behind on every attempt."""
        user, headers = await _account(screener_session, "checkout5@example.com")
        for _ in range(3):
            await billing_api.post(
                url("/checkout/session"), json={"plan_code": "monthly"}, headers=headers
            )
        count = (
            await screener_session.execute(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.user_id == user.id)
            )
        ).scalar_one()
        assert count == 1

    async def test_an_unknown_plan_is_a_404(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await _account(screener_session, "checkout6@example.com")
        assert_problem(
            await billing_api.post(
                url("/checkout/session"), json={"plan_code": "platinum"}, headers=headers
            ),
            404,
            "not-found",
        )

    async def test_a_refusing_gateway_is_reported_and_not_swallowed(
        self,
        seeded_url: str,
        screener_session: AsyncSession,
        clean_redis_namespaces: None,
        invoice_dir: Path,
    ) -> None:
        """A declined call must not look like a successful checkout to the browser."""
        del clean_redis_namespaces
        settings = helpers.billing_settings(seeded_url, invoice_dir)
        _, headers = await _account(screener_session, "checkout7@example.com")
        async with helpers.billing_app(
            settings, screener_session, gateway=helpers.refusing_gateway(settings)
        ) as client:
            assert_problem(
                await client.post(
                    url("/checkout/session"), json={"plan_code": "monthly"}, headers=headers
                ),
                503,
                "pipeline-degraded",
            )


class TestTheWebhookSignature:
    """docs/11 §Security: "Razorpay webhooks: verify signature"."""

    async def test_an_unsigned_delivery_is_refused(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user, _ = await _account(screener_session, "hook1@example.com")
        raw, headers = helpers.signed(
            helpers.payment_captured_event(user_public_id=user.public_id, plan_code="monthly"),
            event_id="evt_unsigned",
        )
        del headers["X-Razorpay-Signature"]
        response = await billing_api.post(url("/webhooks/razorpay"), content=raw, headers=headers)
        assert response.status_code == 400

    async def test_a_forged_signature_is_refused(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user, _ = await _account(screener_session, "hook2@example.com")
        raw, headers = helpers.signed(
            helpers.payment_captured_event(user_public_id=user.public_id, plan_code="monthly"),
            event_id="evt_forged",
            secret="not-the-webhook-secret",
        )
        assert (
            await billing_api.post(url("/webhooks/razorpay"), content=raw, headers=headers)
        ).status_code == 400

    async def test_a_tampered_body_is_refused(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The signature covers the bytes, so changing the amount must invalidate it."""
        user, _ = await _account(screener_session, "hook3@example.com")
        raw, headers = helpers.signed(
            helpers.payment_captured_event(user_public_id=user.public_id, plan_code="monthly"),
            event_id="evt_tampered",
        )
        tampered = raw.replace(b"50000", b"00001")
        assert (
            await billing_api.post(url("/webhooks/razorpay"), content=tampered, headers=headers)
        ).status_code == 400

    async def test_a_refused_delivery_records_nothing(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user, _ = await _account(screener_session, "hook4@example.com")
        raw, headers = helpers.signed(
            helpers.payment_captured_event(user_public_id=user.public_id, plan_code="monthly"),
            event_id="evt_norecord",
            secret="wrong",
        )
        await billing_api.post(url("/webhooks/razorpay"), content=raw, headers=headers)
        found = (
            await screener_session.execute(
                select(WebhookEvent).where(WebhookEvent.event_id == "evt_norecord")
            )
        ).scalar_one_or_none()
        assert found is None


class TestTheWebhookIsIdempotent:
    """Prompt 13 acceptance criterion 1, and docs/07's "idempotent by event id"."""

    async def test_five_deliveries_produce_exactly_one_payment(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user, _ = await _account(screener_session, "replay@example.com")
        raw, headers = helpers.signed(
            helpers.payment_captured_event(
                user_public_id=user.public_id, plan_code="monthly", payment_id="pay_REPLAY0001"
            ),
            event_id="evt_replay_0001",
        )

        for _ in range(5):
            response = await billing_api.post(
                url("/webhooks/razorpay"), content=raw, headers=headers
            )
            # Razorpay retries anything that is not a 2xx, so a replay must still be accepted.
            assert response.status_code == 200

        assert await _payment_count(screener_session, "pay_REPLAY0001") == 1

    async def test_the_same_charge_under_two_event_types_records_once(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """`payment.captured` and `order.paid` both fire for one order."""
        user, _ = await _account(screener_session, "twoevents@example.com")
        captured = helpers.payment_captured_event(
            user_public_id=user.public_id, plan_code="monthly", payment_id="pay_TWOEVENTS1"
        )
        raw_a, headers_a = helpers.signed(captured, event_id="evt_two_a")
        order_paid: dict[str, object] = {**captured, "event": "order.paid"}
        raw_b, headers_b = helpers.signed(order_paid, event_id="evt_two_b")

        await billing_api.post(url("/webhooks/razorpay"), content=raw_a, headers=headers_a)
        await billing_api.post(url("/webhooks/razorpay"), content=raw_b, headers=headers_b)

        assert await _payment_count(screener_session, "pay_TWOEVENTS1") == 1

    async def test_a_delivery_is_remembered(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/11: "dedupe by event id" needs the id to survive a cache flush."""
        user, _ = await _account(screener_session, "remembered@example.com")
        raw, headers = helpers.signed(
            helpers.payment_captured_event(
                user_public_id=user.public_id, plan_code="monthly", payment_id="pay_REMEMBER01"
            ),
            event_id="evt_remembered",
        )
        await billing_api.post(url("/webhooks/razorpay"), content=raw, headers=headers)
        event = (
            await screener_session.execute(
                select(WebhookEvent).where(WebhookEvent.event_id == "evt_remembered")
            )
        ).scalar_one()
        assert event.status == "processed"
        assert event.processed_at is not None


class TestTheWebhookGrantsAndInvoices:
    async def test_a_captured_payment_activates_the_plan(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user, headers = await _account(screener_session, "granted@example.com")
        raw, hook_headers = helpers.signed(
            helpers.payment_captured_event(
                user_public_id=user.public_id, plan_code="monthly", payment_id="pay_GRANT00001"
            ),
            event_id="evt_grant_0001",
        )
        await billing_api.post(url("/webhooks/razorpay"), content=raw, headers=hook_headers)

        me = (await billing_api.get(url("/me"), headers=headers)).json()
        assert me["plan_code"] == "monthly"
        assert me["subscription_status"] == "active"
        assert me["entitlements"]["export_csv"] is True

    async def test_the_payment_row_carries_the_gst_split(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/11 §Compliance, and docs/DECISIONS.md §13.1 — the parts sum to what was charged."""
        user, _ = await _account(screener_session, "gst@example.com")
        raw, hook_headers = helpers.signed(
            helpers.payment_captured_event(
                user_public_id=user.public_id, plan_code="monthly", payment_id="pay_GST000001"
            ),
            event_id="evt_gst_0001",
        )
        await billing_api.post(url("/webhooks/razorpay"), content=raw, headers=hook_headers)

        payment = (
            await screener_session.execute(
                select(Payment).where(Payment.razorpay_payment_id == "pay_GST000001")
            )
        ).scalar_one()
        assert payment.amount_inr == Decimal("500.00")
        assert payment.taxable_inr == Decimal("423.73")
        assert payment.gst_rate == Decimal("18.00")
        assert payment.sac_code == "998439"
        assert payment.place_of_supply == "Karnataka (29)"
        assert (
            payment.taxable_inr + (payment.cgst_inr or 0) + (payment.sgst_inr or 0)
            == payment.amount_inr
        )

    async def test_an_amount_razorpay_reports_beats_the_plan_price(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The invoice must state what was actually charged, not what we hoped to charge."""
        user, _ = await _account(screener_session, "amount@example.com")
        raw, hook_headers = helpers.signed(
            helpers.payment_captured_event(
                user_public_id=user.public_id,
                plan_code="monthly",
                payment_id="pay_AMOUNT0001",
                amount_paise=45_000,
            ),
            event_id="evt_amount_0001",
        )
        await billing_api.post(url("/webhooks/razorpay"), content=raw, headers=hook_headers)
        payment = (
            await screener_session.execute(
                select(Payment).where(Payment.razorpay_payment_id == "pay_AMOUNT0001")
            )
        ).scalar_one()
        assert payment.amount_inr == Decimal("450.00")

    async def test_an_uncaptured_payment_is_ignored(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user, _ = await _account(screener_session, "authorized@example.com")
        event = helpers.payment_captured_event(
            user_public_id=user.public_id, plan_code="monthly", payment_id="pay_AUTHONLY01"
        )
        container = event["payload"]
        assert isinstance(container, dict)
        container["payment"]["entity"]["status"] = "authorized"
        raw, headers = helpers.signed(event, event_id="evt_authorized")
        await billing_api.post(url("/webhooks/razorpay"), content=raw, headers=headers)
        assert await _payment_count(screener_session, "pay_AUTHONLY01") == 0

    async def test_an_unattributable_payment_records_nothing(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A charge whose notes name nobody must not be attached to an arbitrary account."""
        raw, headers = helpers.signed(
            helpers.payment_captured_event(
                user_public_id="nobody000000", plan_code="monthly", payment_id="pay_ORPHAN0001"
            ),
            event_id="evt_orphan",
        )
        assert (
            await billing_api.post(url("/webhooks/razorpay"), content=raw, headers=headers)
        ).status_code == 200
        assert await _payment_count(screener_session, "pay_ORPHAN0001") == 0


class TestSubscriptionLifecycle:
    """Prompt 13 §2: "subscription lifecycle handling (created, charged, halted, cancelled,
    expired)". docs/DECISIONS.md §13.10 has the mapping onto docs/04's four statuses."""

    async def _subscribe(
        self, api: httpx.AsyncClient, session: AsyncSession, email: str
    ) -> tuple[AppUser, dict[str, str]]:
        user, headers = await _account(session, email)
        await api.post(url("/checkout/session"), json={"plan_code": "monthly"}, headers=headers)
        return user, headers

    async def _deliver(
        self, api: httpx.AsyncClient, event: Mapping[str, object], event_id: str
    ) -> None:
        raw, headers = helpers.signed(event, event_id=event_id)
        response = await api.post(url("/webhooks/razorpay"), content=raw, headers=headers)
        assert response.status_code == 200

    async def _status(self, session: AsyncSession, user: AppUser) -> str:
        return (
            await session.execute(
                select(Subscription.status).where(Subscription.user_id == user.id)
            )
        ).scalar_one()

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ("subscription.activated", "active"),
            ("subscription.charged", "active"),
            ("subscription.halted", "past_due"),
            ("subscription.pending", "past_due"),
            ("subscription.cancelled", "cancelled"),
            ("subscription.completed", "expired"),
        ],
    )
    async def test_each_event_lands_the_documented_status(
        self,
        billing_api: httpx.AsyncClient,
        screener_session: AsyncSession,
        event: str,
        expected: str,
    ) -> None:
        user, _ = await self._subscribe(
            billing_api, screener_session, f"life-{event.split('.')[1]}@example.com"
        )
        await self._deliver(billing_api, helpers.subscription_event(event), f"evt_{event}")
        assert await self._status(screener_session, user) == expected

    async def test_activation_records_the_period_end(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user, _ = await self._subscribe(billing_api, screener_session, "period@example.com")
        end = int(dt.datetime(2027, 1, 1, tzinfo=dt.UTC).timestamp())
        await self._deliver(
            billing_api,
            helpers.subscription_event("subscription.activated", current_end=end),
            "evt_period",
        )
        stored = (
            await screener_session.execute(
                select(Subscription.current_period_end).where(Subscription.user_id == user.id)
            )
        ).scalar_one()
        assert stored == dt.datetime(2027, 1, 1, tzinfo=dt.UTC)

    async def test_a_cancellation_removes_the_gated_features(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user, headers = await self._subscribe(billing_api, screener_session, "gone@example.com")
        await self._deliver(
            billing_api, helpers.subscription_event("subscription.activated"), "evt_gone_on"
        )
        assert (await billing_api.get(url("/me"), headers=headers)).json()["entitlements"][
            "export_csv"
        ] is True

        await self._deliver(
            billing_api, helpers.subscription_event("subscription.cancelled"), "evt_gone_off"
        )
        del user
        assert (await billing_api.get(url("/me"), headers=headers)).json()["entitlements"][
            "export_csv"
        ] is False

    async def test_an_event_for_an_unknown_subscription_is_ignored(
        self, billing_api: httpx.AsyncClient
    ) -> None:
        raw, headers = helpers.signed(
            helpers.subscription_event(
                "subscription.cancelled", subscription_id="sub_NOTOURS00001"
            ),
            event_id="evt_unknown_sub",
        )
        assert (
            await billing_api.post(url("/webhooks/razorpay"), content=raw, headers=headers)
        ).status_code == 200


class TestADowngradedUser:
    """Prompt 13 acceptance criterion 2, word for word:

    "a downgraded/expired user loses gated endpoints immediately (402 with upgrade_url) while
     retaining read access to their saved screens."
    """

    async def _lapsed(
        self, api: httpx.AsyncClient, session: AsyncSession, email: str, status: str
    ) -> tuple[dict[str, str], str]:
        user, headers = await _account(session, email)
        plan = await _plan(session, "monthly")
        await helpers.grant(
            session,
            user.id,
            plan.id,
            status=status,
            period_end=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        created = await api.post(
            url("/screens"), json={"name": "Mine", "definition": MINIMAL}, headers=headers
        )
        assert created.status_code == 201
        return headers, str(created.json()["public_id"])

    @pytest.mark.parametrize("status", ["cancelled", "expired", "past_due"])
    async def test_the_csv_export_is_refused_with_an_upgrade_url(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession, status: str
    ) -> None:
        headers, public_id = await self._lapsed(
            billing_api, screener_session, f"lapsed-{status}@example.com", status
        )
        body = assert_problem(
            await billing_api.get(url(f"/screens/{public_id}/csv"), headers=headers),
            402,
            "payment-required",
        )
        assert body["upgrade_url"] == "/pricing"
        assert body["feature"] == "export_csv"

    async def test_an_active_row_whose_period_has_ended_does_not_entitle(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The row is only as fresh as the last webhook — see docs/DECISIONS.md §13.12."""
        headers, public_id = await self._lapsed(
            billing_api, screener_session, "stale-active@example.com", "active"
        )
        assert_problem(
            await billing_api.get(url(f"/screens/{public_id}/csv"), headers=headers),
            402,
            "payment-required",
        )

    async def test_they_keep_read_access_to_their_saved_screens(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers, public_id = await self._lapsed(
            billing_api, screener_session, "keeps-reading@example.com", "expired"
        )
        listed = await billing_api.get(url("/screens"), headers=headers)
        assert listed.status_code == 200
        assert "Mine" in {row["name"] for row in listed.json()["data"]}

        fetched = await billing_api.get(url(f"/screens/{public_id}"), headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "Mine"

    async def test_they_can_still_run_the_screen_they_saved(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/01 §1 gates export, not the screener."""
        headers, public_id = await self._lapsed(
            billing_api, screener_session, "still-runs@example.com", "cancelled"
        )
        response = await billing_api.post(
            url(f"/screens/{public_id}/run"), json={}, headers=headers
        )
        assert response.status_code == 200

    async def test_the_screen_is_not_deleted_by_a_lapse(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _headers, public_id = await self._lapsed(
            billing_api, screener_session, "not-deleted@example.com", "expired"
        )
        rows = (
            (await screener_session.execute(select(Screen).where(Screen.public_id == public_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1


class TestInvoices:
    """docs/07: `GET /invoices` and `GET /invoices/{id}/pdf` (Prompt 13 §4)."""

    async def _paid(
        self, api: httpx.AsyncClient, session: AsyncSession, email: str, payment_id: str
    ) -> dict[str, str]:
        user, headers = await _account(session, email)
        raw, hook_headers = helpers.signed(
            helpers.payment_captured_event(
                user_public_id=user.public_id, plan_code="monthly", payment_id=payment_id
            ),
            event_id=f"evt_{payment_id}",
        )
        response = await api.post(url("/webhooks/razorpay"), content=raw, headers=hook_headers)
        assert response.status_code == 200
        return headers

    async def test_a_payment_produces_an_invoice(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await self._paid(
            billing_api, screener_session, "inv1@example.com", "pay_INVOICE0001"
        )
        body = (await billing_api.get(url("/invoices"), headers=headers)).json()
        assert len(body["data"]) == 1
        row = body["data"][0]
        assert row["invoice_number"].startswith("DCL/")
        assert row["amount_inr"] == "500.00"
        assert row["plan_code"] == "monthly"
        assert row["sac_code"] == "998439"

    async def test_the_invoice_number_is_scoped_to_the_financial_year(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Rule 46(b): unique within a financial year, which starts on 1 April."""
        headers = await self._paid(
            billing_api, screener_session, "inv2@example.com", "pay_INVOICE0002"
        )
        number = (await billing_api.get(url("/invoices"), headers=headers)).json()["data"][0][
            "invoice_number"
        ]
        prefix, series, running = number.split("/")
        assert prefix == "DCL"
        assert len(series) == len("2026-27")
        assert running.isdigit()

    async def test_one_user_never_sees_another_users_invoices(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        await self._paid(billing_api, screener_session, "inv3@example.com", "pay_INVOICE0003")
        _, other = await _account(screener_session, "inv4@example.com")
        body = (await billing_api.get(url("/invoices"), headers=other)).json()
        assert body["data"] == []

    async def test_the_pdf_downloads(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await self._paid(
            billing_api, screener_session, "inv5@example.com", "pay_INVOICE0005"
        )
        row = (await billing_api.get(url("/invoices"), headers=headers)).json()["data"][0]
        response = await billing_api.get(row["pdf_url"], headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF-")
        assert b"TAX INVOICE" in response.content
        assert b"29AAAAA0000A1Z5" in response.content

    async def test_another_users_pdf_is_a_404(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Not-yours reads as absent, so the series cannot be enumerated."""
        headers = await self._paid(
            billing_api, screener_session, "inv6@example.com", "pay_INVOICE0006"
        )
        row = (await billing_api.get(url("/invoices"), headers=headers)).json()["data"][0]
        _, other = await _account(screener_session, "inv7@example.com")
        assert_problem(await billing_api.get(row["pdf_url"], headers=other), 404, "not-found")

    async def test_the_list_needs_an_account(self, billing_api: httpx.AsyncClient) -> None:
        assert_problem(await billing_api.get(url("/invoices")), 401, "unauthenticated")


class TestTheFreeTierFlag:
    """Prompt 13 §5: "A ₹0 free tier with a limited universe is optional — implement it behind a
    feature flag"."""

    @pytest_asyncio.fixture
    async def free_tier_api(
        self,
        seeded_url: str,
        screener_session: AsyncSession,
        clean_redis_namespaces: None,
        invoice_dir: Path,
    ) -> AsyncIterator[httpx.AsyncClient]:
        del clean_redis_namespaces
        settings = helpers.billing_settings(seeded_url, invoice_dir, free_tier_enabled=True)
        async with helpers.billing_app(settings, screener_session) as client:
            yield client

    async def test_the_flag_is_published(self, free_tier_api: httpx.AsyncClient) -> None:
        body = (await free_tier_api.get(url("/plans"))).json()
        assert body["free_tier_enabled"] is True

    async def test_an_unpaid_account_is_limited_to_one_universe(
        self, free_tier_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await _account(screener_session, "freetier1@example.com")
        allowed = await free_tier_api.post(
            url("/screens/preview"),
            json={"definition": {"index": "nifty-50", "sort_by": "ret_12m"}},
            headers=headers,
        )
        assert allowed.status_code == 200

        body = assert_problem(
            await free_tier_api.post(
                url("/screens/preview"),
                json={"definition": {"index": "nifty-500", "sort_by": "ret_12m"}},
                headers=headers,
            ),
            402,
            "payment-required",
        )
        assert body["feature"] == "universes"
        assert body["upgrade_url"] == "/pricing"

    async def test_a_paid_account_is_not_limited(
        self, free_tier_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user, headers = await _account(screener_session, "freetier2@example.com")
        plan = await _plan(screener_session, "monthly")
        await helpers.grant(screener_session, user.id, plan.id)
        response = await free_tier_api.post(
            url("/screens/preview"),
            json={"definition": {"index": "nifty-500", "sort_by": "ret_12m"}},
            headers=headers,
        )
        assert response.status_code == 200


class TestMaxScreens:
    """Prompt 13 §6: gating applied to "a max_screens limit"."""

    async def test_an_unpaid_account_is_capped(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await _account(screener_session, "capped@example.com")
        me = (await billing_api.get(url("/me"), headers=headers)).json()
        limit = me["entitlements"]["max_screens"]
        assert limit < 50

        for index in range(limit):
            created = await billing_api.post(
                url("/screens"), json={"name": f"S{index}", "definition": MINIMAL}, headers=headers
            )
            assert created.status_code == 201, created.text

        body = assert_problem(
            await billing_api.post(
                url("/screens"),
                json={"name": "One too many", "definition": MINIMAL},
                headers=headers,
            ),
            402,
            "payment-required",
        )
        assert body["feature"] == "max_screens"
        assert body["upgrade_url"] == "/pricing"

    async def test_a_paid_account_gets_the_documented_fifty(
        self, billing_api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/07 §Entitlements' own example: `"max_screens": 50`."""
        user, headers = await _account(screener_session, "fifty@example.com")
        plan = await _plan(screener_session, "monthly")
        await helpers.grant(screener_session, user.id, plan.id)
        me = (await billing_api.get(url("/me"), headers=headers)).json()
        assert me["entitlements"]["max_screens"] == 50


@pytest.mark.filterwarnings("ignore:Setting per-request cookies:DeprecationWarning")
class TestNoPaymentSecretIsLogged:
    """docs/11 §Security: "Secrets in the platform's secret store, never in the repo" — and never
    in a log line either. The webhook is the one endpoint that receives a MAC on every call."""

    async def test_neither_the_key_secret_nor_the_signature_is_written(
        self,
        billing_api: httpx.AsyncClient,
        screener_session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.handler.addFilter(RedactingFilter())
        user, headers = await _account(screener_session, "nosecret@example.com")
        raw, hook_headers = helpers.signed(
            helpers.payment_captured_event(
                user_public_id=user.public_id, plan_code="monthly", payment_id="pay_NOSECRET01"
            ),
            event_id="evt_nosecret",
        )

        with caplog.at_level(logging.DEBUG):
            await billing_api.post(
                url("/checkout/session"), json={"plan_code": "monthly"}, headers=headers
            )
            await billing_api.post(url("/webhooks/razorpay"), content=raw, headers=hook_headers)
            # A refused delivery too: that is where a careless `log.warning(signature)` would be.
            bad_raw, bad_headers = helpers.signed(
                helpers.subscription_event("subscription.cancelled"),
                event_id="evt_nosecret_bad",
                secret="wrong-secret",
            )
            await billing_api.post(url("/webhooks/razorpay"), content=bad_raw, headers=bad_headers)

        rendered = "\n".join(JsonFormatter().format(record) for record in caplog.records)
        for secret, what in (
            (helpers.KEY_SECRET, "the Razorpay key secret"),
            (helpers.WEBHOOK_SECRET, "the webhook secret"),
            (hook_headers["X-Razorpay-Signature"], "the webhook signature"),
        ):
            assert secret not in rendered, f"{what} was written to the log"
