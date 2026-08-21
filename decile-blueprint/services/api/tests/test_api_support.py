"""``POST /support`` — the contact form's destination (PROMPTS.md Prompt 18 §2).

Not in docs/07. `docs/DECISIONS.md` §18.5 records why the endpoint exists and why it lives on the
API rather than in a Next server action.

Every test drives a **recording transport**, not a mailer: the suite is network-blocked
(`network_guard.py`) and the point of the assertions is the *message that would be sent*, not that
`smtplib` works.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest
from api_helpers import PROBLEM_MEDIA_TYPE, api_settings, running_app, url
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api.email import EmailNotSent, Mailer, Message
from decile_api.schemas import SUPPORT_TOPICS

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

GOOD = {
    "name": "Asha Menon",
    "email": "asha@example.com",
    "topic": "A number looks wrong",
    "message": "CUPID's one-year return reads 726.63% and my broker shows something else.",
}


class Recorder:
    """A transport that keeps what it was handed. ``fail_on`` makes one recipient unreachable."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.sent: list[Message] = []
        self._fail_on = fail_on

    async def send(self, message: Message) -> None:
        if self._fail_on is not None and message.to == self._fail_on:
            raise EmailNotSent("transport refused")
        self.sent.append(message)


async def post_support(
    session: AsyncSession,
    seeded_url: str,
    payload: Mapping[str, object],
    *,
    recorder: Recorder | None = None,
    support_email: str = "inbox@example.com",
) -> tuple[httpx.Response, Recorder]:
    box = recorder or Recorder()
    settings = api_settings(seeded_url, support_email=support_email)
    async with running_app(settings, session, mailer=Mailer(box)) as client:
        response = await client.post(url("/support"), json=payload)
    return response, box


class TestASubmission:
    async def test_it_is_accepted_without_an_account(
        self, screener_session: AsyncSession, seeded_url: str
    ) -> None:
        """Someone who cannot sign in is exactly who most needs to reach support."""
        response, _ = await post_support(screener_session, seeded_url, dict(GOOD))
        assert response.status_code == 202
        assert response.json()["status"] == "accepted"

    async def test_it_delivers_to_the_configured_address_and_to_the_sender(
        self, screener_session: AsyncSession, seeded_url: str
    ) -> None:
        _, box = await post_support(screener_session, seeded_url, dict(GOOD))
        assert [message.to for message in box.sent] == ["inbox@example.com", "asha@example.com"]

    async def test_the_operator_copy_carries_the_body_and_replies_to_the_sender(
        self, screener_session: AsyncSession, seeded_url: str
    ) -> None:
        """Answering the mail has to reach the person, not the no-reply address."""
        _, box = await post_support(screener_session, seeded_url, dict(GOOD))
        operator = box.sent[0]
        assert operator.reply_to == "asha@example.com"
        assert "CUPID" in operator.text
        assert "asha@example.com" in operator.text

    async def test_the_subject_carries_only_the_closed_set_topic(
        self, screener_session: AsyncSession, seeded_url: str
    ) -> None:
        """The subject is a header. A free-text value there is a header-injection surface."""
        payload = dict(GOOD) | {"name": "Asha\r\nBcc: victim@example.com"}
        response, box = await post_support(screener_session, seeded_url, payload)
        assert response.status_code == 202
        assert "\r" not in box.sent[0].subject
        assert "Bcc:" not in box.sent[0].subject

    async def test_the_receipt_does_not_echo_the_message_back(
        self, screener_session: AsyncSession, seeded_url: str
    ) -> None:
        """A receipt containing the body turns the form into a way to mail arbitrary text to an
        arbitrary address in our name."""
        _, box = await post_support(screener_session, seeded_url, dict(GOOD))
        assert "CUPID" not in box.sent[1].text


class TestValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("email", "not-an-address"),
            ("topic", "Anything I like"),
            ("message", "too short"),
            ("name", ""),
        ],
    )
    async def test_a_bad_field_is_refused(
        self, screener_session: AsyncSession, seeded_url: str, field: str, value: str
    ) -> None:
        response, box = await post_support(
            screener_session, seeded_url, dict(GOOD) | {field: value}
        )
        # 400 `invalid-screen-definition`, not 422: `decile_api.app` renders *every*
        # `RequestValidationError` through docs/07's one validation problem type, whatever the
        # endpoint. The name reads oddly on a contact form and the shape is the documented one.
        assert response.status_code == 400
        assert response.json()["type"] == "invalid-screen-definition"
        assert box.sent == []

    async def test_every_offered_topic_is_accepted(
        self, screener_session: AsyncSession, seeded_url: str
    ) -> None:
        """The web form renders `SUPPORT_TOPICS`; a value it offers must not be a 422."""
        for topic in SUPPORT_TOPICS:
            response, _ = await post_support(
                screener_session, seeded_url, dict(GOOD) | {"topic": topic}
            )
            assert response.status_code == 202, topic


class TestDeliveryFailure:
    async def test_an_undeliverable_message_is_reported_not_swallowed(
        self, screener_session: AsyncSession, seeded_url: str
    ) -> None:
        """Unlike the auth endpoints, which must not become a membership oracle, this one has
        nothing to hide and everything to lose by claiming a send that did not happen."""
        response, box = await post_support(
            screener_session,
            seeded_url,
            dict(GOOD),
            recorder=Recorder(fail_on="inbox@example.com"),
        )
        assert response.status_code == 500
        assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
        assert box.sent == []

    async def test_a_failed_receipt_does_not_fail_the_request(
        self, screener_session: AsyncSession, seeded_url: str
    ) -> None:
        """The message reached support. Answering 500 now would invite a duplicate send."""
        response, box = await post_support(
            screener_session,
            seeded_url,
            dict(GOOD),
            recorder=Recorder(fail_on="asha@example.com"),
        )
        assert response.status_code == 202
        assert [message.to for message in box.sent] == ["inbox@example.com"]


class TestItIsNotAnOpenRelay:
    async def test_no_recipient_can_be_supplied(
        self, screener_session: AsyncSession, seeded_url: str
    ) -> None:
        """`extra="forbid"` on the input model is what makes this true, and it is worth an
        explicit test: the day someone adds a `to` field, this fails."""
        response, box = await post_support(
            screener_session, seeded_url, dict(GOOD) | {"to": "victim@example.com"}
        )
        assert response.status_code == 400
        assert box.sent == []
