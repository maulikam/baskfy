"""OP15 — the `options-monitor` container: its clock, and compose runs it with every money flag false.

* the loop starts the monitor at 09:14 on a weekday, at once inside a session, not after the close,
  not on a weekend; a crash inside the session starts it again (restart resume); a clean exit
  ends the day (`TestTheClock`);
* the image carries the loop, compose runs it from the desk block, and the desk block names every
  options execution flag defaulting false, with `OPTIONS_ENABLED` and `INTRADAY_ENABLED` pinned
  false and no options auto-execute variable anywhere (`TestTheService`).
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import yaml

from scripts import options_monitor_loop as L

IST = L.IST
REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "decile-blueprint" / "infra" / "docker" / "compose.prod.yml"
DOCKERFILE = REPO / "decile-blueprint" / "infra" / "docker" / "Dockerfile.desk"
WED = dt.date(2026, 10, 28)


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=IST)


class TestTheClock:
    def test_before_the_open_it_waits_for_0914(self) -> None:
        assert L.next_run(at(WED, 8, 0)) == at(WED, 9, 14)

    def test_inside_the_session_it_starts_at_once(self) -> None:
        assert L.next_run(at(WED, 11, 30)) == at(WED, 11, 30)

    def test_after_the_close_or_once_run_it_waits_for_the_next_weekday(self) -> None:
        assert L.next_run(at(WED, 15, 45)) == at(WED + dt.timedelta(days=1), 9, 14)
        assert L.next_run(at(WED, 11, 30), ran_today=True) == at(WED + dt.timedelta(days=1), 9, 14)
        friday = dt.date(2026, 10, 30)
        assert L.next_run(at(friday, 16, 0)) == at(dt.date(2026, 11, 2), 9, 14)

    def test_a_crash_in_session_restarts_and_a_clean_exit_ends_the_day(self) -> None:
        moments = iter([at(WED, 10, 0)] * 3 + [at(WED, 10, 1)] * 3 + [at(WED, 15, 31)] * 6)
        codes = iter([1, 0])
        calls: list[list[str]] = []
        sleeps: list[float] = []

        def call(argv: list[str]) -> int:
            calls.append(argv)
            return next(codes)

        L.run_forever(now_fn=lambda: next(moments), sleep_fn=sleeps.append, call_fn=call, limit=2)
        assert len(calls) == 2 and calls[0][1:] == ["-m", "app.options_monitor"]
        assert L.RETRY_SECONDS in sleeps  # the crash was retried inside the session


class TestTheService:
    def test_the_image_carries_the_loop(self) -> None:
        text = DOCKERFILE.read_text()
        assert "options-monitor-loop" in text and "scripts.options_monitor_loop" in text

    def test_compose_runs_it_from_the_desk_block_with_money_flags_false(self) -> None:
        compose = yaml.safe_load(COMPOSE.read_text())
        service = compose["services"]["options-monitor"]
        assert service["command"] == ["options-monitor-loop"]
        env = service["environment"]
        for sleeve in ("O1M", "O1W", "O2", "O3"):
            name = f"BASKFY_OPTIONS_{sleeve}_EXECUTION_ENABLED"
            assert env[name] == f"${{{name}:-false}}", name
        assert env["BASKFY_OPTIONS_MONITOR_ENABLED"] == "${BASKFY_OPTIONS_MONITOR_ENABLED:-false}"
        assert env["OPTIONS_ENABLED"] == "false" and env["INTRADAY_ENABLED"] == "false"
        assert env["DRY_RUN"] == "${BASKFY_DESK_DRY_RUN:-true}"

    def test_no_options_auto_execute_variable_anywhere_in_compose(self) -> None:
        assert re.search(r"OPTIONS\w*AUTO", COMPOSE.read_text()) is None
