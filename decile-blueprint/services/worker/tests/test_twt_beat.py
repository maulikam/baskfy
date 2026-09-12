"""TW11 — the tight sleeve's clock: the two entries that were missing, and the one that must not be.

`baskfy.twt.evening` and `baskfy.twt.morning` were registered tasks with **no Beat entry** from TW6
until 12 Sep 2026, while `twt-detect` had had one since TW4 and VBT-1 had both. `REMAINING.md` §2
said "nothing schedules the evening, the morning or the sweep", and `docs/twt/FIRST-LIVE-MORNING.md`
told a person to open the desk and read a plan that nothing was building.

**The last test in this file is the important one.** The 15:15 sweep re-arms GTT stops, so it is
order flow, and a Beat entry for it would be the desk placing orders on a timer. The root
`CLAUDE.md`'s first non-negotiable allows exactly one named auto-execute exception and it is the
swing sleeve's; an agent may not add a second. `test_the_sweep_is_not_on_a_timer` is what makes
that a check rather than a comment.
"""

from __future__ import annotations

from typing import cast

from celery.schedules import crontab

from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE, build_celery


def _entry(name: str) -> dict[str, object]:
    assert name in BEAT_SCHEDULE, f"{name} is not in the desk's clock"
    return BEAT_SCHEDULE[name]


def test_twt_evening_runs_after_the_detector() -> None:
    """The plan is built from the session's signals and the session's gate, so an evening that
    ran before `twt-detect` at 21:00 would plan against yesterday's tape."""
    entry = _entry("twt-evening")
    assert entry["task"] == "baskfy.twt.evening"
    schedule = cast(crontab, entry["schedule"])
    assert schedule.hour == {21}
    assert schedule.minute == {20}
    assert schedule.day_of_week == {1, 2, 3, 4, 5}
    options = entry["options"]
    assert isinstance(options, dict), "the beat entry must carry an options mapping"
    assert options["queue"] == QUEUE_COMPUTE

    detect = cast(crontab, _entry("twt-detect")["schedule"])
    assert min(detect.hour) * 60 + min(detect.minute) < 21 * 60 + 20, (
        "the evening plan must run after the detector, not before it"
    )


def test_twt_morning_rebuilds_the_plan_before_the_open() -> None:
    """The desk's plans expire in thirty minutes, so last night's cannot be confirmed at 09:20."""
    entry = _entry("twt-morning")
    assert entry["task"] == "baskfy.twt.morning"
    schedule = cast(crontab, entry["schedule"])
    assert schedule.hour == {9}
    assert schedule.minute == {5}
    assert schedule.day_of_week == {1, 2, 3, 4, 5}
    # Before the 09:15 open, or the plan a person confirms is not the plan they were shown.
    assert min(schedule.hour) * 60 + min(schedule.minute) < 9 * 60 + 15


def test_every_twt_beat_entry_names_a_task_the_worker_registers() -> None:
    """A renamed task must not leave a dead entry firing into nothing."""
    app = build_celery()
    registered = set(app.tasks.keys())
    scheduled = {
        str(entry["task"])
        for name, entry in BEAT_SCHEDULE.items()
        if str(entry["task"]).startswith("baskfy.twt.")
    }
    # TW12 added the fourth: `baskfy.twt.scan_publish`, every minute, which publishes the
    # `tw_scan_run` rows the desk's "Scan now" button writes (the desk has no Celery client). It
    # is a `SELECT ... WHERE task_id IS NULL` and an `apply_async`; it confirms nothing, and
    # `test_the_sweep_is_not_on_a_timer` below is what holds that line — it refused this entry
    # under its first name, `twt-scan-sweep`, and the rename is DECISIONS-TW TW12.4.
    assert scheduled == {
        "baskfy.twt.detect",
        "baskfy.twt.evening",
        "baskfy.twt.morning",
        "baskfy.twt.scan_publish",
    }
    missing = scheduled - registered
    assert not missing, f"scheduled but not registered: {sorted(missing)}"


def test_the_sweep_is_not_on_a_timer() -> None:
    """**Non-negotiable #1, as a check.**

    The sweep re-arms GTT stops. Scheduling it would be the desk placing orders on a timer, which
    is a second auto-execute exception, which an agent may not add. It stays `POST /twt/sweep` and
    `tools/twt/sweep.py` — a person's command, which is what the runbook §9 already says it is.

    Asserted two ways because either alone is weak: no TWT entry may be *named* for a sweep, and
    no TWT entry may point at a task whose name contains one.
    """
    twt_entries = {
        name: str(entry["task"])
        for name, entry in BEAT_SCHEDULE.items()
        if name.startswith("twt-") or str(entry["task"]).startswith("baskfy.twt.")
    }
    assert not [n for n in twt_entries if "sweep" in n], (
        "a scheduled TWT sweep would be auto-execution"
    )
    assert not [t for t in twt_entries.values() if "sweep" in t], (
        "a TWT beat entry points at a sweep task"
    )
    # THIS TEST DID ITS JOB ON 12 SEP 2026 AND THE RECORD IS WORTH KEEPING.
    #
    # TW12's "Scan now" publisher was first written as `twt-scan-sweep` -> `baskfy.twt.scan_sweep`,
    # copying the swing book's `swing-scan-sweep` and VBT-1's `vbt-rescan-sweep`. It publishes
    # queued rows and could not reach a broker if it tried. This test failed it anyway, and was
    # right to: a person scanning Beat for "twt sweep" must not find a hit, because the one they
    # are looking for -- `sweep_naked`, at 15:15, through the gateway -- must never be there. The
    # task was renamed to `scan_publish` rather than the assertion narrowed. DECISIONS-TW TW12.4.
    assert "baskfy.twt.scan_publish" in twt_entries.values(), (
        "the scan publisher is gone; if it was renamed back to a sweep, read TW12.4 first"
    )
