"""The S2 Kite timing probe (SW11, STANDING-ANSWERS A4, MD5): one morning, four answers, then
never again."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from celery.schedules import crontab

from baskfy_providers.records import QuoteRecord
from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE
from baskfy_worker.settings import WorkerSettings
from baskfy_worker.tasks import celery_tasks
from baskfy_worker.tasks.swing_timing_probe import (
    DONE_MARKER,
    IST,
    REPORT_NAME,
    SCHEDULE,
    ProbeReport,
    probe_once,
    render_report,
    run_timing_probe,
)

DAY = dt.date(2026, 9, 3)  # a Thursday


class Clock:
    """A driven clock: `now()` reads it, `sleep()` advances it."""

    def __init__(self, start: dt.time) -> None:
        self.now_at = dt.datetime.combine(DAY, start, tzinfo=IST)

    def now(self) -> dt.datetime:
        return self.now_at

    def sleep(self, seconds: float) -> None:
        self.now_at += dt.timedelta(seconds=seconds)


class ScriptedKite:
    """What a Kite morning looks like under one hypothesis: the pre-open LTP moves once at
    09:07:30, `ohlc.open` and `volume` appear at 09:08, and the 09:20 candle is served from
    09:20:35 (not at 09:20:05)."""

    def __init__(
        self, *, open_at: dt.time = dt.time(9, 8), candle_from: dt.time = dt.time(9, 20, 35)
    ) -> None:
        self.open_at = open_at
        self.candle_from = candle_from
        self.clock: Clock | None = None
        self.quote_calls: list[dt.datetime] = []
        self.candle_calls: list[tuple[dt.datetime, dt.datetime]] = []

    def _now(self) -> dt.datetime:
        assert self.clock is not None
        return self.clock.now()

    def quotes(self, symbols: Sequence[str]) -> list[QuoteRecord]:
        now = self._now()
        self.quote_calls.append(now)
        moved = now.time() >= dt.time(9, 7, 30)
        opened = now.time() >= self.open_at
        return [
            QuoteRecord(
                symbol=symbol,
                last_price=Decimal("101.50") if moved else Decimal("100.00"),
                volume=12_000 if opened else 0,
                prev_close=Decimal("100.00"),
                open=Decimal("101.50") if opened else None,
                as_of=now.replace(tzinfo=None),
            )
            for symbol in symbols
        ]

    def minute_candles(
        self, token: int, start: dt.datetime, end: dt.datetime
    ) -> list[dict[str, object]]:
        now = self._now()
        self.candle_calls.append((start, end))
        last = dt.time(9, 20) if now.time() >= self.candle_from else dt.time(9, 19)
        candles: list[dict[str, object]] = []
        minute = dt.datetime.combine(DAY, dt.time(9, 15))
        while minute.time() <= last:
            candles.append({"date": minute, "open": 100, "high": 101, "low": 99, "close": 100.5})
            minute += dt.timedelta(minutes=1)
        return candles


def _run(source: ScriptedKite, *, start: dt.time = dt.time(9, 4)) -> tuple[ProbeReport, Clock]:
    clock = Clock(start)
    source.clock = clock
    report = run_timing_probe(
        source, symbols=["RELIANCE", "TCS"], token=738561, day=DAY, now=clock.now, sleep=clock.sleep
    )
    return report, clock


class TestTheProbeWalksItsSchedule:
    def test_timing_probe_samples_every_scheduled_moment_in_order(self) -> None:
        source = ScriptedKite()
        report, clock = _run(source)
        quote_times = [t.time() for t in source.quote_calls]
        assert quote_times == [when for when, kind in SCHEDULE if kind == "quote"]
        assert len(source.candle_calls) == 3
        assert clock.now().time() == dt.time(9, 21, 10)
        assert report.skipped == [] and report.errors == []
        assert len(report.quotes) == 2 * len(quote_times)

    def test_timing_probe_answers_the_four_questions_from_the_samples(self) -> None:
        report, _ = _run(ScriptedKite())
        assert report.preopen_volume_seen() is True  # volume > 0 from 09:08
        assert report.preopen_ltp_moved() is True  # 100.00 → 101.50 at 09:07:30
        assert report.open_before_session() is True  # ohlc.open present at 09:08
        assert report.candle_first_seen_at() == dt.datetime.combine(
            DAY, dt.time(9, 20, 35), tzinfo=IST
        )
        assert report.good() is True

    def test_timing_probe_reads_the_other_hypothesis_honestly(self) -> None:
        """Nothing before 09:15 — no volume, no open, no LTP move — and the candle only at
        09:21:05: every answer flips, and the run is still a good run (it answered)."""

        class NothingBeforeTheOpen(ScriptedKite):
            def quotes(self, symbols: Sequence[str]) -> list[QuoteRecord]:
                if self._now().time() < dt.time(9, 15):
                    return [
                        QuoteRecord(
                            symbol=s, last_price=Decimal("100"), volume=0, prev_close=Decimal("100")
                        )
                        for s in symbols
                    ]
                return [
                    QuoteRecord(
                        symbol=s,
                        last_price=Decimal("103"),
                        volume=500,
                        prev_close=Decimal("100"),
                        open=Decimal("102"),
                    )
                    for s in symbols
                ]

        source = NothingBeforeTheOpen(open_at=dt.time(9, 15), candle_from=dt.time(9, 21, 5))
        report, _ = _run(source)
        assert report.preopen_volume_seen() is False
        assert report.preopen_ltp_moved() is False
        assert report.open_before_session() is False
        assert report.candle_first_seen_at() == dt.datetime.combine(
            DAY, dt.time(9, 21, 5), tzinfo=IST
        )
        assert report.good() is True

    def test_timing_probe_started_late_skips_what_it_missed_and_is_not_a_good_run(self) -> None:
        report, _ = _run(ScriptedKite(), start=dt.time(9, 18))
        assert len(report.skipped) == 9 and report.skipped[0].startswith("quote at 09:04:30")
        assert [t.time() for t in [q.at for q in report.quotes]][:1] == [dt.time(9, 21, 10)]
        assert report.preopen_volume_seen() is None
        assert report.good() is False

    def test_timing_probe_records_a_source_error_and_carries_on(self) -> None:
        class FirstAskFails(ScriptedKite):
            failures = 0

            def minute_candles(
                self, token: int, start: dt.datetime, end: dt.datetime
            ) -> list[dict[str, object]]:
                if self.failures == 0:
                    self.failures += 1
                    raise RuntimeError("token expired")
                return super().minute_candles(token, start, end)

        source = FirstAskFails()
        report, _ = _run(source)
        assert report.candles[0].error == "token expired" and report.candles[1].error is None
        assert report.good() is False  # a candle ask errored: try again tomorrow


class TestTheReportAndTheMarker:
    def test_timing_probe_report_names_the_answers_and_the_schedule_consequence(self) -> None:
        report, clock = _run(ScriptedKite())
        text = render_report(report, generated_at=clock.now())
        assert text.startswith("# S2 — Kite timing, as measured")
        assert "1. `volume` at ≤ 09:09 carries the pre-open matched quantity: **yes**" in text
        assert "3. `ohlc.open` is populated before 09:15: **yes**" in text
        assert "first appeared in `historical_data` at: 09:20:35" in text
        assert "the scan could return to 09:09" in text
        assert "| 09:09:00 | RELIANCE | 101.50 | 101.50 | 12000 |" in text

    def test_timing_probe_writes_the_report_and_a_marker_after_one_good_run(
        self, tmp_path: Path
    ) -> None:
        source = ScriptedKite()
        clock = Clock(dt.time(9, 4))
        source.clock = clock
        result = probe_once(
            source,
            out_dir=tmp_path / "status",
            symbols=["RELIANCE"],
            token=1,
            now=clock.now,
            sleep=clock.sleep,
        )
        assert result["good"] is True
        assert (tmp_path / "status" / REPORT_NAME).read_text(encoding="utf-8").startswith("# S2")
        assert (tmp_path / "status" / DONE_MARKER).read_text(
            encoding="utf-8"
        ).strip() == DAY.isoformat()
        # The second morning: nothing is sampled.
        again = probe_once(
            ScriptedKite(),
            out_dir=tmp_path / "status",
            symbols=["RELIANCE"],
            token=1,
            now=clock.now,
            sleep=clock.sleep,
        )
        assert "already had its good run" in str(again["skipped"])

    def test_timing_probe_does_not_disable_itself_after_a_bad_run(self, tmp_path: Path) -> None:
        source = ScriptedKite()
        clock = Clock(dt.time(9, 18))  # started late
        source.clock = clock
        result = probe_once(
            source,
            out_dir=tmp_path,
            symbols=["RELIANCE"],
            token=1,
            now=clock.now,
            sleep=clock.sleep,
        )
        assert result["good"] is False
        assert (tmp_path / REPORT_NAME).exists() and not (tmp_path / DONE_MARKER).exists()


class TestTheBinding:
    def test_timing_probe_beat_entry_is_0904_ist_on_weekdays(self) -> None:
        entry = BEAT_SCHEDULE["swing-timing-probe"]
        assert entry["task"] == "baskfy.swing.timing_probe"
        schedule = cast(crontab, entry["schedule"])
        assert (schedule.hour, schedule.minute) == ({9}, {4})
        assert schedule.day_of_week == {1, 2, 3, 4, 5}
        assert entry["options"] == {"queue": QUEUE_COMPUTE}

    def test_timing_probe_is_gated_by_the_flag_and_off_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BASKFY_SWING_TIMING_PROBE", raising=False)
        assert WorkerSettings(_env_file=None).swing_timing_probe is False
        monkeypatch.setattr(
            celery_tasks, "get_worker_settings", lambda: WorkerSettings(_env_file=None)
        )
        assert celery_tasks.swing_timing_probe_task() == {
            "skipped": "BASKFY_SWING_TIMING_PROBE is false"
        }

    def test_timing_probe_task_skips_when_the_marker_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / DONE_MARKER).write_text("2026-09-03\n", encoding="utf-8")
        monkeypatch.setattr(
            celery_tasks,
            "get_worker_settings",
            lambda: WorkerSettings(
                _env_file=None, swing_timing_probe=True, swing_timing_probe_dir=str(tmp_path)
            ),
        )
        assert "already had its good run" in str(celery_tasks.swing_timing_probe_task()["skipped"])
