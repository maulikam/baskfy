"""LV4 — one deterministic state and reason per sleeve, from stored facts and the clock.

`gates/live-4-login-event.md` L4. `derive_state` is pure, so every branch is a table here; the
route is asserted read-only by `test_api_artifacts`' verb map.
"""

from __future__ import annotations

import datetime as dt

import pytest

from baskfy_api.sleeve_state import (
    HEARTBEAT_FRESH_SECONDS,
    SCAN_MEANS,
    SLEEVES,
    SleeveFacts,
    derive_state,
)
from baskfy_core.market_hours_cb import IST

MONDAY = dt.date(2026, 9, 28)


def at(hhmm: str) -> dt.datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(MONDAY, dt.time(hour, minute), tzinfo=IST)


def facts(  # noqa: PLR0913 - one keyword per fact, so a test names only what it changes
    sleeve: str = "twt",
    *,
    session_day: bool = True,
    market_open: bool = True,
    broker_session: bool = True,
    scan_status: str | None = None,
    scan_found: int | None = None,
    scan_finished_at: dt.datetime | None = None,
    scan_provisional: bool = False,
    plan_source: str | None = None,
    plan_session_date: dt.date | None = None,
    plan_expires_at: dt.datetime | None = None,
    heartbeat_at: dt.datetime | None = None,
    heartbeat_state: str | None = None,
    open_issues: int = 0,
    issue_kinds: tuple[str, ...] = (),
    watching: int = 0,
) -> SleeveFacts:
    return SleeveFacts(
        sleeve=sleeve,
        session_day=session_day,
        market_open=market_open,
        broker_session=broker_session,
        scan_status=scan_status,
        scan_found=scan_found,
        scan_finished_at=scan_finished_at,
        scan_provisional=scan_provisional,
        plan_source=plan_source,
        plan_session_date=plan_session_date,
        plan_expires_at=plan_expires_at,
        heartbeat_at=heartbeat_at,
        heartbeat_state=heartbeat_state,
        open_issues=open_issues,
        issue_kinds=issue_kinds,
        watching=watching,
    )


class TestPrecedence:
    def test_no_session_before_the_open_is_waiting_for_login(self) -> None:
        out = derive_state(facts(market_open=False, broker_session=False), at("08:50"))
        assert out.state == "waiting_for_login"
        assert "no Kite session" in out.reason and "log in" in out.next

    def test_no_session_after_the_close_is_idle_not_waiting(self) -> None:
        out = derive_state(facts(market_open=False, broker_session=False), at("16:00"))
        assert out.state == "idle" and "closed" in out.reason

    def test_a_holiday_is_closed_whatever_else_is_true(self) -> None:
        out = derive_state(
            facts(session_day=False, market_open=False, broker_session=False), at("10:30")
        )
        assert out.state == "closed"

    def test_an_open_protection_issue_blocks_first(self) -> None:
        out = derive_state(
            facts(open_issues=2, issue_kinds=("GTT_MISSING", "NAKED"), plan_source="MORNING"),
            at("10:30"),
        )
        assert out.state == "blocked"
        assert "GTT_MISSING, NAKED" in out.reason and "lifecycle" in out.next

    def test_a_queued_or_running_scan_is_scanning(self) -> None:
        assert derive_state(facts(scan_status="QUEUED"), at("10:30")).state == "scanning"
        assert derive_state(facts(scan_status="RUNNING"), at("10:30")).state == "scanning"


class TestTheTenThirtyLogin:
    """The review's acceptance: a 10:30 login produces an explicit outcome per sleeve."""

    def test_twt_with_the_morning_plan_expired_is_missed_window(self) -> None:
        out = derive_state(
            facts(
                "twt",
                plan_source="MORNING",
                plan_session_date=MONDAY,
                plan_expires_at=at("09:35"),
            ),
            at("10:30"),
        )
        assert out.state == "missed_window"
        # LV8: the open is no longer the only entry — Scan reads today live and buys at market.
        assert "expired at 09:35" in out.reason and "at market" in out.reason
        assert "Scan now" in out.next and "tomorrow" in out.next

    def test_twt_with_no_morning_plan_is_missed_window_too(self) -> None:
        out = derive_state(facts("twt"), at("10:30"))
        assert out.state == "missed_window" and "no MORNING plan" in out.reason

    def test_twt_before_the_window_closes_with_a_live_plan_is_plan_ready(self) -> None:
        out = derive_state(
            facts(
                "twt", plan_source="MORNING", plan_session_date=MONDAY, plan_expires_at=at("09:35")
            ),
            at("09:20"),
        )
        assert out.state == "plan_ready" and "twt-auto" in out.next

    def test_vbt_with_an_expired_plan_is_missed_window(self) -> None:
        out = derive_state(
            facts(
                "vbt", plan_source="MORNING", plan_session_date=MONDAY, plan_expires_at=at("09:30")
            ),
            at("10:30"),
        )
        assert out.state == "missed_window" and "signal close" in out.reason

    def test_swing_with_a_fresh_monitor_heartbeat_is_monitoring(self) -> None:
        out = derive_state(
            facts("swing", heartbeat_at=at("10:29"), heartbeat_state="watching", watching=5),
            at("10:30"),
        )
        assert out.state == "monitoring" and "5 name(s)" in out.reason

    def test_swing_with_a_stale_heartbeat_is_not_monitoring(self) -> None:
        stale = at("10:30") - dt.timedelta(seconds=HEARTBEAT_FRESH_SECONDS + 1)
        out = derive_state(facts("swing", heartbeat_at=stale, watching=5), at("10:30"))
        assert out.state != "monitoring"


class TestSignalsAndIdle:
    def test_a_done_scan_with_candidates_is_signal_ready(self) -> None:
        out = derive_state(
            facts(
                "swing",
                scan_status="DONE",
                scan_found=3,
                scan_provisional=True,
                scan_finished_at=at("10:20"),
            ),
            at("10:30"),
        )
        assert out.state == "signal_ready"
        assert (
            "3 candidate(s)" in out.reason and "provisional" in out.reason and "10:20" in out.reason
        )

    def test_a_done_scan_with_nothing_is_idle(self) -> None:
        out = derive_state(facts("vbt", scan_status="DONE", scan_found=0), at("10:30"))
        assert out.state == "idle" and "found nothing" in out.reason

    def test_a_failed_scan_says_so(self) -> None:
        out = derive_state(facts("swing", scan_status="FAILED"), at("10:30"))
        assert out.state == "idle" and "failed" in out.reason


class TestTheVocabularyIsHonest:
    @pytest.mark.parametrize("sleeve", SLEEVES)
    def test_every_answer_says_what_scan_means_for_its_sleeve(self, sleeve: str) -> None:
        out = derive_state(facts(sleeve), at("10:30"))
        assert out.scan_means == SCAN_MEANS[sleeve]
        assert out.sleeve == sleeve and out.updated_at == at("10:30")

    def test_every_sleeve_scans_today_so_far_in_session_and_the_published_session_outside(
        self,
    ) -> None:
        """LV4 said TWT's and VBT's scans were never live. Maulik's LV8 decision (28 Sep 2026,
        DECISIONS-LV LV8.0; TW19, VB16) makes them so during the session — the same words as the
        swing book's, so the chip teaches one vocabulary — and both still say what happens
        outside it."""
        for sleeve in ("twt", "vbt"):
            assert "today so far" in SCAN_MEANS[sleeve]
            assert "last published session" in SCAN_MEANS[sleeve]
            assert "at market" in SCAN_MEANS[sleeve]
        assert "today so far" in SCAN_MEANS["swing"]
