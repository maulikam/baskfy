"""The desk-daily container's clock (`scripts.desk_daily_loop`).

Beat used to fire ``baskfy.desk.daily`` on baskfy-py, which does not contain this tree.
Maulik, 13 Sep 2026 (NEEDS-MAULIK §33): this process is the scheduler. These tests are
the schedule — 18:30 daily, 18:50 autorun, weekday catch-up, weekend skip — and the
wiring that makes the container actually run this module rather than a copy of it.
"""

from __future__ import annotations

import datetime as dt
import re
import zoneinfo
from pathlib import Path

import pytest

import scripts.desk_daily_loop as desk_daily_loop
from scripts.desk_daily_loop import (
    AUTORUN_AT,
    DAILY_AT,
    SETTLE_SECONDS,
    SLOTS,
    due_now,
    next_job,
    next_weekday_at,
    run_forever,
)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "decile-blueprint" / "infra" / "docker" / "Dockerfile.desk"
COMPOSE = REPO / "decile-blueprint" / "infra" / "docker" / "compose.prod.yml"

FRIDAY = dt.datetime(2026, 9, 11, 13, 9, tzinfo=IST)
FRIDAY_EVENING = dt.datetime(2026, 9, 11, 19, 0, tzinfo=IST)
SATURDAY = dt.datetime(2026, 9, 12, 13, 0, tzinfo=IST)


class TestTheSlotsMatchTheRetiredBeatAndTheTimer:
    def test_daily_is_eighteen_thirty_weekdays(self) -> None:
        assert DAILY_AT == dt.time(18, 30)
        assert SLOTS[0].name == "daily"
        assert SLOTS[0].at == DAILY_AT
        assert SLOTS[0].argv == ("-m", "scripts.daily", "--quiet", "--source", "schedule")

    def test_autorun_is_after_daily_not_before(self) -> None:
        assert AUTORUN_AT == dt.time(18, 50)
        assert SLOTS[1].name == "autorun"
        assert SLOTS[1].at > SLOTS[0].at
        assert SLOTS[1].argv == ("-m", "scripts.autorun",)


class TestNextWeekdayAt:
    def test_before_the_slot_it_waits_for_today(self) -> None:
        early = dt.datetime(2026, 9, 11, 18, 0, tzinfo=IST)
        assert next_weekday_at(early, DAILY_AT) == dt.datetime(2026, 9, 11, 18, 30, tzinfo=IST)

    def test_after_the_slot_it_waits_for_the_next_weekday(self) -> None:
        assert next_weekday_at(FRIDAY_EVENING, DAILY_AT) == dt.datetime(
            2026, 9, 14, 18, 30, tzinfo=IST
        )

    def test_friday_evening_skips_the_weekend(self) -> None:
        assert next_weekday_at(FRIDAY_EVENING, AUTORUN_AT).date() == dt.date(2026, 9, 14)

    def test_naive_now_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            next_weekday_at(dt.datetime(2026, 9, 11, 18, 30), DAILY_AT)


class TestCatchUp:
    def test_a_weekday_restart_after_eighteen_thirty_still_owes_the_day(self) -> None:
        """The timer's Persistent=true: down at 18:30, up at 19:00, still collect today."""
        assert due_now(FRIDAY_EVENING, DAILY_AT, already_ran=False) is True
        assert due_now(FRIDAY_EVENING, AUTORUN_AT, already_ran=False) is True

    def test_a_slot_that_already_ran_is_not_owed_again(self) -> None:
        assert due_now(FRIDAY_EVENING, DAILY_AT, already_ran=True) is False

    def test_a_weekend_does_not_catch_up_friday(self) -> None:
        """Kite has flushed /trades. Saturday cannot recover Friday's fills."""
        assert due_now(SATURDAY, DAILY_AT, already_ran=False) is False

    def test_before_the_slot_is_not_due(self) -> None:
        early = dt.datetime(2026, 9, 11, 18, 0, tzinfo=IST)
        assert due_now(early, DAILY_AT, already_ran=False) is False


class TestNextJob:
    def test_friday_afternoon_waits_for_eighteen_thirty(self) -> None:
        slot, when = next_job(FRIDAY)
        assert slot.name == "daily"
        assert when == dt.datetime(2026, 9, 11, 18, 30, tzinfo=IST)

    def test_friday_evening_catch_up_runs_daily_first(self) -> None:
        slot, when = next_job(FRIDAY_EVENING)
        assert slot.name == "daily"
        assert when == FRIDAY_EVENING

    def test_after_daily_it_runs_autorun_not_tomorrows_daily(self) -> None:
        slot, when = next_job(
            FRIDAY_EVENING, ran=frozenset({(FRIDAY_EVENING.date(), "daily")})
        )
        assert slot.name == "autorun"
        assert when == FRIDAY_EVENING

    def test_between_the_slots_it_waits_for_autorun(self) -> None:
        between = dt.datetime(2026, 9, 11, 18, 40, tzinfo=IST)
        slot, when = next_job(between, ran=frozenset({(between.date(), "daily")}))
        assert slot.name == "autorun"
        assert when == dt.datetime(2026, 9, 11, 18, 50, tzinfo=IST)

    def test_saturday_waits_for_monday(self) -> None:
        slot, when = next_job(SATURDAY)
        assert slot.name == "daily"
        assert when == dt.datetime(2026, 9, 14, 18, 30, tzinfo=IST)

    def test_after_both_friday_it_skips_the_weekend(self) -> None:
        ran = frozenset({
            (FRIDAY_EVENING.date(), "daily"),
            (FRIDAY_EVENING.date(), "autorun"),
        })
        slot, when = next_job(FRIDAY_EVENING, ran=ran)
        assert slot.name == "daily"
        assert when == dt.datetime(2026, 9, 14, 18, 30, tzinfo=IST)


class TestTheLoopRunsTheJobs:
    def test_friday_evening_runs_daily_then_autorun_then_waits_for_monday(self) -> None:
        clock = [FRIDAY_EVENING]
        calls: list[tuple[str, ...]] = []
        slept: list[float] = []

        def now() -> dt.datetime:
            return clock[0]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock[0] = clock[0] + dt.timedelta(seconds=seconds)

        def call(argv: list[str]) -> int:
            calls.append(tuple(argv[1:]))
            return 0

        run_forever(now_fn=now, sleep_fn=sleep, call_fn=call, limit=3)

        assert calls[0] == ("-m", "scripts.daily", "--quiet", "--source", "schedule")
        assert calls[1] == ("-m", "scripts.autorun",)
        assert calls[2] == calls[0]
        assert slept[0] == float(SETTLE_SECONDS)
        assert slept[1] == float(SETTLE_SECONDS)
        # Third sleep is Friday evening → Monday 18:30, not another 90s spin.
        assert slept[2] > 2 * 24 * 3600


class TestNothingHerePlacesAnOrder:
    def test_the_loop_cannot_reach_the_order_path(self) -> None:
        source = Path(desk_daily_loop.__file__).read_text()
        for forbidden in (
            "place_order",
            "OrderGateway",
            "baskfy_execution",
            "execute(",
            "/execute",
        ):
            assert forbidden not in source, f"the desk-daily loop must not reference {forbidden}"


class TestTheContainerRunsThisModule:
    def test_the_dockerfile_wrapper_execs_the_tested_module(self) -> None:
        """A heredoc copy of the schedule would go stale. The binary must launch this file."""
        text = DOCKERFILE.read_text()
        start = text.index("RUN cat > /usr/local/bin/desk-daily-loop")
        body = text[text.index("\n", start) + 1 :]
        heredoc = body[: body.index("\nEOF")]
        assert "exec python -m scripts.desk_daily_loop" in heredoc
        assert "chmod 0755" in text and "desk-daily-loop" in text.split("chmod 0755", 1)[1][:200]

    def test_compose_starts_desk_daily_from_the_desk_image(self) -> None:
        text = COMPOSE.read_text()
        match = re.search(r"^  desk-daily:(.*?)(?=^  [A-Za-z]|\Z)", text, flags=re.M | re.S)
        assert match is not None
        body = match.group(1)
        assert "<<: *desk" in body
        assert "command: [desk-daily-loop]" in body
        assert "healthcheck:" in body and "disable: true" in body
