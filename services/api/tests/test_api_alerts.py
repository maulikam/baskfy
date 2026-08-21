"""Screen alerts end to end — subscription, dispatch, idempotency and unsubscribe (Prompt 20 §3).

The email's *wording* is pinned byte for byte in ``test_alert_email.py``, which needs no database.
This module is about the machinery around it: which alerts are due, which two ``screen_run`` rows
get diffed, what happens when the screen's definition changed, and the property CLAUDE.md house
rule 7 demands — running the night twice sends one email.

Every test drives a **recording transport**. The suite is network-blocked, and what matters is the
message that would have been sent.
"""

from __future__ import annotations

import datetime as dt

import api_helpers
import httpx
import pytest
import screener_helpers
from alert_helpers import (
    CURRENT,
    PREVIOUS,
    Recorder,
    instrument_ids,
    make_run,
    make_screen,
    settings_for,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api import alerts as service
from decile_api.email import Mailer
from decile_core.models import ScreenAlert, ScreenAlertDelivery

pytestmark = [screener_helpers.requires_db, pytest.mark.db, pytest.mark.redis]

ALERTS = api_helpers.url("/alerts")


class TestSubscription:
    async def test_a_user_can_subscribe_a_screen_and_see_it_back(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user_id, public_id = await api_helpers.make_user(
            screener_session, "sub@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, user_id, "alertscrn001", "Momentum 12-1")
        response = await api.post(
            ALERTS,
            json={"screen_public_id": screen.public_id, "frequency": "daily"},
            headers=api_helpers.bearer(public_id),
        )
        assert response.status_code == 201, response.text
        assert response.json()["screen_name"] == "Momentum 12-1"

        listed = await api.get(ALERTS, headers=api_helpers.bearer(public_id))
        assert [alert["public_id"] for alert in listed.json()["alerts"]] == [
            response.json()["public_id"]
        ]

    async def test_a_second_subscription_to_the_same_screen_is_refused(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user_id, public_id = await api_helpers.make_user(
            screener_session, "sub2@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, user_id, "alertscrn002", "Momentum")
        body = {"screen_public_id": screen.public_id}
        first = await api.post(ALERTS, json=body, headers=api_helpers.bearer(public_id))
        assert first.status_code == 201
        second = await api.post(ALERTS, json=body, headers=api_helpers.bearer(public_id))
        api_helpers.assert_problem(second, 400, "invalid-screen-definition")

    async def test_a_screen_the_caller_cannot_see_is_a_404(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        owner_id, _ = await api_helpers.make_user(
            screener_session, "owner-a@example.com", subscribed=True
        )
        _, stranger = await api_helpers.make_user(
            screener_session, "stranger-a@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, owner_id, "alertscrn003", "Private")
        response = await api.post(
            ALERTS,
            json={"screen_public_id": screen.public_id},
            headers=api_helpers.bearer(stranger),
        )
        api_helpers.assert_problem(response, 404, "not-found")


class TestUnsubscribe:
    async def test_the_link_from_an_email_turns_the_alert_off_without_a_session(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        settings = settings_for("postgresql+asyncpg://unused/unused")
        user_id, _ = await api_helpers.make_user(
            screener_session, "unsub@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, user_id, "alertscrn004", "Momentum")
        alert = await service.create_alert(
            screener_session, user_id=user_id, screen=screen, settings=settings
        )
        token = service.unsubscribe_token(settings, alert.public_id)

        response = await api.post(f"{ALERTS}/unsubscribe", json={"token": token})
        assert response.status_code == 200, response.text
        assert response.json()["unsubscribed"] is True
        await screener_session.refresh(alert)
        assert alert.is_active is False

    async def test_an_unknown_token_answers_the_same_thing(self, api: httpx.AsyncClient) -> None:
        """Never an oracle: a distinguishable answer would be a way to test tokens."""
        response = await api.post(f"{ALERTS}/unsubscribe", json={"token": "f" * 32})
        assert response.status_code == 200
        assert response.json() == {"unsubscribed": True, "screen_name": None}

    async def test_the_token_in_the_link_matches_the_stored_digest(
        self, screener_session: AsyncSession
    ) -> None:
        settings = settings_for("postgresql+asyncpg://unused/unused")
        user_id, _ = await api_helpers.make_user(
            screener_session, "unsub2@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, user_id, "alertscrn005", "Momentum")
        alert = await service.create_alert(
            screener_session, user_id=user_id, screen=screen, settings=settings
        )
        link = service.unsubscribe_url(settings, alert.public_id)
        token = link.split("token=", 1)[1]
        assert await service.unsubscribe(screener_session, token) is not None


class TestSchedule:
    def test_a_daily_alert_is_always_due(self) -> None:
        alert = ScreenAlert(frequency="daily", is_active=True, min_move=1)
        assert service.is_due(alert, dt.date(2026, 8, 19))

    def test_a_weekly_alert_is_due_on_its_weekday_only(self) -> None:
        alert = ScreenAlert(frequency="weekly", weekday=4, is_active=True, min_move=1)
        assert service.is_due(alert, dt.date(2026, 8, 21))  # a Friday
        assert not service.is_due(alert, dt.date(2026, 8, 20))

    def test_a_weekly_alert_with_no_weekday_defaults_to_friday(self) -> None:
        alert = ScreenAlert(frequency="weekly", weekday=None, is_active=True, min_move=1)
        assert service.is_due(alert, dt.date(2026, 8, 21))

    def test_an_inactive_alert_is_never_due(self) -> None:
        alert = ScreenAlert(frequency="daily", is_active=False, min_move=1)
        assert not service.is_due(alert, dt.date(2026, 8, 19))


class TestDispatch:
    async def test_it_sends_one_email_naming_the_entries_and_exits(
        self, screener_session: AsyncSession
    ) -> None:
        settings = settings_for("postgresql+asyncpg://unused/unused")
        user_id, _ = await api_helpers.make_user(
            screener_session, "dispatch@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, user_id, "alertscrn010", "Momentum 12-1")
        ids = await instrument_ids(screener_session, 4)
        await make_run(screener_session, screen, PREVIOUS, [(1, ids[0]), (2, ids[1]), (3, ids[2])])
        await make_run(screener_session, screen, CURRENT, [(1, ids[0]), (2, ids[3]), (3, ids[1])])
        await service.create_alert(
            screener_session, user_id=user_id, screen=screen, settings=settings
        )

        box = Recorder()
        report = await service.dispatch(
            screener_session, as_of=CURRENT, mailer=Mailer(box), settings=settings
        )
        assert report.alerts_sent == 1
        assert len(box.sent) == 1
        body = box.sent[0].text
        assert "Entries (1)" in body
        assert "Exits (1)" in body

    async def test_running_the_night_twice_sends_one_email(
        self, screener_session: AsyncSession
    ) -> None:
        """CLAUDE.md house rule 7: "Re-running any day's job produces identical rows"."""
        settings = settings_for("postgresql+asyncpg://unused/unused")
        user_id, _ = await api_helpers.make_user(
            screener_session, "twice@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, user_id, "alertscrn011", "Momentum")
        ids = await instrument_ids(screener_session, 3)
        await make_run(screener_session, screen, PREVIOUS, [(1, ids[0]), (2, ids[1])])
        await make_run(screener_session, screen, CURRENT, [(1, ids[0]), (2, ids[2])])
        await service.create_alert(
            screener_session, user_id=user_id, screen=screen, settings=settings
        )

        box = Recorder()
        first = await service.dispatch(
            screener_session, as_of=CURRENT, mailer=Mailer(box), settings=settings
        )
        second = await service.dispatch(
            screener_session, as_of=CURRENT, mailer=Mailer(box), settings=settings
        )
        assert first.emails_sent == 1
        assert second.emails_sent == 0
        assert len(box.sent) == 1

    async def test_an_unchanged_screen_sends_nothing_and_records_why(
        self, screener_session: AsyncSession
    ) -> None:
        settings = settings_for("postgresql+asyncpg://unused/unused")
        user_id, _ = await api_helpers.make_user(
            screener_session, "quiet@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, user_id, "alertscrn012", "Momentum")
        ids = await instrument_ids(screener_session, 2)
        rows = [(1, ids[0]), (2, ids[1])]
        await make_run(screener_session, screen, PREVIOUS, rows)
        await make_run(screener_session, screen, CURRENT, rows)
        alert = await service.create_alert(
            screener_session, user_id=user_id, screen=screen, settings=settings
        )

        box = Recorder()
        report = await service.dispatch(
            screener_session, as_of=CURRENT, mailer=Mailer(box), settings=settings
        )
        assert box.sent == []
        assert report.skips["no_change"] == 1
        delivery = (
            await screener_session.execute(
                select(ScreenAlertDelivery).where(ScreenAlertDelivery.alert_id == alert.id)
            )
        ).scalar_one()
        assert delivery.status == "skipped"
        assert delivery.detail == {"reason": "no_change"}

    async def test_a_screen_whose_definition_changed_is_skipped_not_reported(
        self, screener_session: AsyncSession
    ) -> None:
        """Diffing two definitions would blame the market for the user's edit."""
        settings = settings_for("postgresql+asyncpg://unused/unused")
        user_id, _ = await api_helpers.make_user(
            screener_session, "edited@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, user_id, "alertscrn013", "Momentum")
        ids = await instrument_ids(screener_session, 3)
        await make_run(
            screener_session,
            screen,
            PREVIOUS,
            [(1, ids[0])],
            definition_hash="a" * 64,
        )
        await make_run(screener_session, screen, CURRENT, [(1, ids[1]), (2, ids[2])])
        await service.create_alert(
            screener_session, user_id=user_id, screen=screen, settings=settings
        )

        box = Recorder()
        report = await service.dispatch(
            screener_session, as_of=CURRENT, mailer=Mailer(box), settings=settings
        )
        assert box.sent == []
        assert report.skips["definition_changed"] == 1

    async def test_a_failed_send_is_recorded_as_failed_not_as_sent(
        self, screener_session: AsyncSession
    ) -> None:
        settings = settings_for("postgresql+asyncpg://unused/unused")
        user_id, _ = await api_helpers.make_user(
            screener_session, "bounced@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, user_id, "alertscrn014", "Momentum")
        ids = await instrument_ids(screener_session, 3)
        await make_run(screener_session, screen, PREVIOUS, [(1, ids[0]), (2, ids[1])])
        await make_run(screener_session, screen, CURRENT, [(1, ids[0]), (2, ids[2])])
        alert = await service.create_alert(
            screener_session, user_id=user_id, screen=screen, settings=settings
        )

        report = await service.dispatch(
            screener_session,
            as_of=CURRENT,
            mailer=Mailer(Recorder(fail=True)),
            settings=settings,
        )
        assert report.alerts_failed == 1
        delivery = (
            await screener_session.execute(
                select(ScreenAlertDelivery).where(ScreenAlertDelivery.alert_id == alert.id)
            )
        ).scalar_one()
        assert delivery.status == "failed"

    async def test_two_digest_alerts_produce_one_email(
        self, screener_session: AsyncSession
    ) -> None:
        """Prompt 20 §3's digest preference, which is the whole point of the flag."""
        settings = settings_for("postgresql+asyncpg://unused/unused")
        user_id, _ = await api_helpers.make_user(
            screener_session, "digest@example.com", subscribed=True
        )
        ids = await instrument_ids(screener_session, 4)
        for index in (1, 2):
            screen = await make_screen(
                screener_session, user_id, f"alertdigest{index:02d}", f"Screen {index}"
            )
            await make_run(screener_session, screen, PREVIOUS, [(1, ids[0]), (2, ids[1])])
            await make_run(screener_session, screen, CURRENT, [(1, ids[0]), (2, ids[1 + index])])
            await service.create_alert(
                screener_session,
                user_id=user_id,
                screen=screen,
                settings=settings,
                digest_preference=True,
            )

        box = Recorder()
        report = await service.dispatch(
            screener_session, as_of=CURRENT, mailer=Mailer(box), settings=settings
        )
        assert report.alerts_sent == 2
        assert report.emails_sent == 1
        assert len(box.sent) == 1
        assert "2 screens changed" in box.sent[0].subject

    async def test_two_separate_alerts_produce_two_emails(
        self, screener_session: AsyncSession
    ) -> None:
        settings = settings_for("postgresql+asyncpg://unused/unused")
        user_id, _ = await api_helpers.make_user(
            screener_session, "separate@example.com", subscribed=True
        )
        ids = await instrument_ids(screener_session, 4)
        for index in (1, 2):
            screen = await make_screen(
                screener_session, user_id, f"alertsingle{index:02d}", f"Screen {index}"
            )
            await make_run(screener_session, screen, PREVIOUS, [(1, ids[0]), (2, ids[1])])
            await make_run(screener_session, screen, CURRENT, [(1, ids[0]), (2, ids[1 + index])])
            await service.create_alert(
                screener_session, user_id=user_id, screen=screen, settings=settings
            )

        box = Recorder()
        report = await service.dispatch(
            screener_session, as_of=CURRENT, mailer=Mailer(box), settings=settings
        )
        assert report.emails_sent == 2
        assert len(box.sent) == 2

    async def test_an_inactive_alert_is_left_alone(self, screener_session: AsyncSession) -> None:
        settings = settings_for("postgresql+asyncpg://unused/unused")
        user_id, _ = await api_helpers.make_user(
            screener_session, "off@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, user_id, "alertscrn015", "Momentum")
        ids = await instrument_ids(screener_session, 3)
        await make_run(screener_session, screen, PREVIOUS, [(1, ids[0]), (2, ids[1])])
        await make_run(screener_session, screen, CURRENT, [(1, ids[0]), (2, ids[2])])
        alert = await service.create_alert(
            screener_session, user_id=user_id, screen=screen, settings=settings
        )
        await service.update_alert(screener_session, alert, is_active=False)

        box = Recorder()
        report = await service.dispatch(
            screener_session, as_of=CURRENT, mailer=Mailer(box), settings=settings
        )
        assert box.sent == []
        assert report.alerts_considered == 0
