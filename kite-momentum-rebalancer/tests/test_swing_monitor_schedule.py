"""When the swing-monitor container decides to start the monitor (`Dockerfile.desk`).

THE BUG THIS EXISTS TO CATCH, AND IT COST AN AFTERNOON
-------------------------------------------------------
`next_run` lived only inside a Dockerfile heredoc, so nothing executed it outside a deploy. It
added a day the moment 09:14 had passed, which was right while SW6 could say the monitor "stops
at 10:45": a container restarting at 13:00 had genuinely missed everything.

**SW26 widened the watch to 15:30 and did not touch this function.** So every restart after the
open silently forfeited the rest of the day — and on Friday 11 Sep 2026 a 13:09 restart printed

    swing-monitor: flag ON; next run Mon 2026-09-14 09:14 IST

and sat out an afternoon the whole of SW26 exists to watch. `deploy-swing.sh`'s session guard
describes a restart as losing "the signals from before it", which understates it: there were no
signals after it either, because nothing was running.

WHY THE TEST LIVES HERE AND READS THE DOCKERFILE
------------------------------------------------
The scheduler is four lines of Python embedded in a `RUN cat > ... <<'EOF'` block. Copying those
lines into a test would test the copy, and the copy is what goes stale — that is precisely how the
original bug survived SW26. So this extracts the real source from the real Dockerfile and executes
it. If someone edits the heredoc, these run against the edit.
"""

from __future__ import annotations

import datetime as dt
import re
import zoneinfo
from pathlib import Path
from typing import Any

import pytest

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

DOCKERFILE = (
    Path(__file__).resolve().parents[2]
    / "decile-blueprint"
    / "infra"
    / "docker"
    / "Dockerfile.desk"
)

#: Friday, inside the session. 11 Sep 2026 is the day the bug was seen.
FRIDAY = dt.datetime(2026, 9, 11, 13, 9, tzinfo=IST)
SATURDAY = dt.datetime(2026, 9, 12, 13, 0, tzinfo=IST)


def _loop_source() -> str:
    """The `swing-monitor-loop` heredoc, straight out of the Dockerfile."""
    text = DOCKERFILE.read_text()
    start = text.index("RUN cat > /usr/local/bin/swing-monitor-loop")
    body = text[text.index("\n", start) + 1 :]
    return body[: body.index("\nEOF")]


@pytest.fixture(scope="module")
def next_run() -> Any:
    """`next_run`, executed from the Dockerfile's own text.

    Only the definitions are executed — the `while True:` driver is cut off — so importing this
    cannot start a monitor or sleep.
    """
    source = _loop_source()
    source = source[: source.index("\nwhile True:")]
    # The container has `baskfy_core` on its path and this test runner may not; the fallback in
    # the source handles that, which is also why the fallback exists.
    namespace: dict[str, Any] = {}
    exec(compile(source, str(DOCKERFILE), "exec"), namespace)  # noqa: S102 - the file under test
    return namespace["next_run"]


class TestARestartMidSessionJoinsTheSession:
    """SW26's window is 09:15–15:30, and the schedule has to agree with it."""

    def test_a_restart_after_the_open_starts_at_once(self, next_run: Any) -> None:
        """The failure exactly as seen on the box: 13:09 Friday must not answer Monday."""
        assert next_run(FRIDAY) == FRIDAY

    def test_it_does_not_wait_for_monday(self, next_run: Any) -> None:
        assert next_run(FRIDAY).date() == FRIDAY.date()

    def test_a_disabled_monitor_still_waits_for_the_next_open(self, next_run: Any) -> None:
        """With the flag off the monitor exits 0 immediately, so joining would re-log every 90
        seconds to say nothing is happening. It waits for tomorrow, as it always did."""
        assert next_run(FRIDAY, enabled=False) == dt.datetime(2026, 9, 14, 9, 14, tzinfo=IST)


class TestTheOrdinaryScheduleIsUnchanged:
    def test_before_the_open_it_waits_for_the_open(self, next_run: Any) -> None:
        early = dt.datetime(2026, 9, 11, 6, 0, tzinfo=IST)
        assert next_run(early) == dt.datetime(2026, 9, 11, 9, 14, tzinfo=IST)

    def test_after_the_close_it_waits_for_the_next_weekday(self, next_run: Any) -> None:
        evening = dt.datetime(2026, 9, 10, 19, 0, tzinfo=IST)  # Thursday evening
        assert next_run(evening) == dt.datetime(2026, 9, 11, 9, 14, tzinfo=IST)

    def test_friday_evening_skips_the_weekend(self, next_run: Any) -> None:
        evening = dt.datetime(2026, 9, 11, 19, 0, tzinfo=IST)
        assert next_run(evening) == dt.datetime(2026, 9, 14, 9, 14, tzinfo=IST)

    def test_a_weekend_afternoon_is_not_a_session_to_join(self, next_run: Any) -> None:
        """The window check is `weekday() <= 4` as well as the clock: 13:00 on a Saturday is
        inside 09:15–15:30 and is not a trading session."""
        assert next_run(SATURDAY) == dt.datetime(2026, 9, 14, 9, 14, tzinfo=IST)

    def test_exactly_at_the_close_it_is_too_late_to_join(self, next_run: Any) -> None:
        at_close = dt.datetime(2026, 9, 11, 15, 30, tzinfo=IST)
        assert next_run(at_close) == dt.datetime(2026, 9, 14, 9, 14, tzinfo=IST)


class TestTheScheduleAgreesWithTheStrategy:
    def test_the_loop_reads_monitor_close_rather_than_hardcoding_it(self) -> None:
        """The bug was two numbers that had to agree and did not. Now there is one.

        The literal 15:30 is still in the source as an import fallback — a loop that cannot import
        must still start the monitor in the morning — but the live value comes from the config.
        """
        source = _loop_source()

        assert "DEFAULT_SWING_CONFIG.opening_range.monitor_close" in source
        assert "CLOSE = dt.time(15, 30)" in source, "the fallback is gone"

    def test_the_fallback_matches_todays_config(self) -> None:
        """If someone changes `monitor_close`, the fallback has to move with it or a box that
        cannot import the config would watch the wrong hours."""
        from baskfy_core.swing.config import DEFAULT_SWING_CONFIG

        assert DEFAULT_SWING_CONFIG.opening_range.monitor_close == (15, 30)
