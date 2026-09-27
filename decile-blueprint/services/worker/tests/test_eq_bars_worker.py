"""LV5 — one-minute equity bars for the liquid universe (``gates/live-5-eq-bars.md``).

The reader is a fake that records every window asked for; the database is the real test
Postgres (``requires_db``), because the upsert's idempotence and the hypertable are what is
asserted. No Kite is reached.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import cast

import pytest
from celery.schedules import crontab
from helpers import make_instrument, requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import EqMinuteBar
from baskfy_providers.records import MinuteBarRecord
from baskfy_worker import eq_bars
from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_INGEST, TASK_ROUTES
from baskfy_worker.eq_bars import EqName, backfill, closed_bars, day_windows, reconcile_session
from baskfy_worker.tasks.swing_premarket import LiquidName

IST = eq_bars.IST
DAY = dt.date(2026, 9, 25)


def minute(hhmm: str, day: dt.date = DAY) -> dt.datetime:
    hour, mm = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, mm), tzinfo=IST)


def bar(ts: dt.datetime, price: str, *, volume: int = 100) -> MinuteBarRecord:
    value = Decimal(price)
    return MinuteBarRecord(
        ts=ts, open=value, high=value + Decimal("0.005"), low=value - 1, close=value, volume=volume
    )


class FakeReader:
    """Answers the same session for any window and records what was asked."""

    def __init__(self, bars: list[MinuteBarRecord]) -> None:
        self.bars = bars
        self.calls: list[tuple[int, dt.datetime | dt.date, dt.datetime | dt.date]] = []

    def minute_bars(
        self, token: int, start: dt.datetime | dt.date, end: dt.datetime | dt.date
    ) -> list[MinuteBarRecord]:
        self.calls.append((token, start, end))
        lo = (
            start
            if isinstance(start, dt.datetime)
            else dt.datetime.combine(start, dt.time(0, 0), tzinfo=IST)
        )
        hi = (
            end
            if isinstance(end, dt.datetime)
            else dt.datetime.combine(end, dt.time(23, 59, 59), tzinfo=IST)
        )
        return [b for b in self.bars if lo <= b.ts <= hi]


class TestPure:
    def test_closed_minutes_only(self) -> None:
        bars = [
            bar(minute("09:15"), "100"),
            bar(minute("09:16"), "101"),
            bar(minute("09:17"), "102"),
        ]
        now = minute("09:17") + dt.timedelta(seconds=30)
        assert [b.ts for b in closed_bars(bars, now)] == [minute("09:15"), minute("09:16")]

    def test_day_windows_are_kites_sixty_day_chunks_inclusive(self) -> None:
        windows = day_windows(dt.date(2026, 1, 1), dt.date(2026, 3, 31))
        assert windows[0] == (dt.date(2026, 1, 1), dt.date(2026, 3, 1))
        assert windows[-1][1] == dt.date(2026, 3, 31) and len(windows) == 2
        with pytest.raises(ValueError, match="precedes"):
            day_windows(dt.date(2026, 3, 1), dt.date(2026, 1, 1))


class TestBeatAndUniverse:
    def test_the_beat_entry_runs_after_the_close_on_weekdays_on_the_ingest_queue(self) -> None:
        entry = BEAT_SCHEDULE["eq-bars-session"]
        assert entry["task"] == "baskfy.eq_bars.session"
        assert entry["schedule"] == crontab(hour=15, minute=45, day_of_week="mon-fri")
        assert entry["options"] == {"queue": QUEUE_INGEST}
        assert TASK_ROUTES["baskfy.eq_bars.*"] == {"queue": QUEUE_INGEST}

    @pytest.mark.asyncio
    async def test_the_universe_is_the_swing_books_liquid_universe_with_kite_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One predicate: `liquid_universe`, over the sole user's swing config; a name with no
        Kite token is left out."""
        seen: dict[str, object] = {}

        async def fake_config(session: object, user_id: int) -> str:
            seen["user_id"] = user_id
            return "cfg"

        async def fake_universe(
            session: object, *, as_of: dt.date, config: str
        ) -> list[LiquidName]:
            seen["as_of"], seen["config"] = as_of, config
            return [
                LiquidName(1, "AAA", Decimal(1), Decimal(1)),
                LiquidName(2, "BBB", Decimal(1), Decimal(1)),
            ]

        class Rows:
            def all(self) -> list[tuple[int, str, int | None]]:
                return [(1, "AAA", 111), (2, "BBB", None)]

        class Session:
            async def execute(self, statement: object) -> Rows:
                return Rows()

        monkeypatch.setattr(eq_bars, "load_swing_config", fake_config)
        monkeypatch.setattr(eq_bars, "liquid_universe", fake_universe)
        names = await eq_bars.liquid_names(cast(AsyncSession, Session()), as_of=DAY, user_id=7)
        assert names == [EqName(1, "AAA", 111)]
        assert seen == {"user_id": 7, "as_of": DAY, "config": "cfg"}


@requires_db
class TestTheStore:
    async def _stored(
        self, session: AsyncSession, instrument_id: int
    ) -> list[tuple[dt.datetime, Decimal, int]]:
        rows = (
            await session.execute(
                select(EqMinuteBar.ts, EqMinuteBar.close, EqMinuteBar.volume)
                .where(EqMinuteBar.instrument_id == instrument_id)
                .order_by(EqMinuteBar.ts)
            )
        ).all()
        return [(ts.astimezone(IST), Decimal(close), int(volume)) for ts, close, volume in rows]

    @pytest.mark.asyncio
    async def test_reconcile_is_idempotent_writes_closed_minutes_and_rounds_at_write(
        self, session: AsyncSession
    ) -> None:
        instrument = await make_instrument(session, "AAA", token=111)
        reader = FakeReader(
            [
                bar(minute("09:15"), "100.004"),
                bar(minute("09:16"), "100.005"),
                bar(minute("15:29"), "99"),
            ]
        )
        names = [EqName(instrument, "AAA", 111)]
        now = minute("15:30")
        first = await reconcile_session(session, reader, DAY, now=now, names=names)
        again = await reconcile_session(session, reader, DAY, now=now, names=names)
        assert first.written == 3 and again.written == 3 and first.calls == again.calls == 1
        stored = await self._stored(session, instrument)
        # identical rows on the re-run (house rule 7); the closed-minutes rule; two places (rule 8)
        assert [ts for ts, _c, _v in stored] == [minute("09:15"), minute("09:16"), minute("15:29")]
        assert [c for _t, c, _v in stored] == [
            Decimal("100.00"),
            Decimal("100.01"),
            Decimal("99.00"),
        ]
        assert reader.calls[0] == (111, minute("09:15"), minute("15:30"))

    @pytest.mark.asyncio
    async def test_a_forming_minute_is_not_written_and_arrives_on_the_next_pass(
        self, session: AsyncSession
    ) -> None:
        instrument = await make_instrument(session, "BBB", token=222)
        reader = FakeReader([bar(minute("09:15"), "10"), bar(minute("09:16"), "11")])
        names = [EqName(instrument, "BBB", 222)]
        await reconcile_session(
            session, reader, DAY, now=minute("09:16") + dt.timedelta(seconds=20), names=names
        )
        assert [ts for ts, _c, _v in await self._stored(session, instrument)] == [minute("09:15")]
        await reconcile_session(session, reader, DAY, now=minute("09:17"), names=names)
        assert len(await self._stored(session, instrument)) == 2

    @pytest.mark.asyncio
    async def test_backfill_resumes_from_the_newest_stored_bar_per_name(
        self, session: AsyncSession
    ) -> None:
        instrument = await make_instrument(session, "CCC", token=333)
        jan, feb, mar = dt.date(2026, 1, 5), dt.date(2026, 2, 5), dt.date(2026, 3, 5)
        reader = FakeReader(
            [
                bar(minute("10:00", jan), "1"),
                bar(minute("10:00", feb), "2"),
                bar(minute("10:00", mar), "3"),
            ]
        )
        names = [EqName(instrument, "CCC", 333)]
        now = minute("16:00", dt.date(2026, 3, 31))
        # A first, interrupted run stored January only.
        await eq_bars.upsert_bars(session, instrument, [bar(minute("10:00", jan), "1")])
        checkpoints = 0

        async def checkpoint() -> None:
            nonlocal checkpoints
            checkpoints += 1

        report = await backfill(
            session,
            reader,
            dt.date(2026, 1, 1),
            dt.date(2026, 3, 31),
            now=now,
            names=names,
            checkpoint=checkpoint,
        )
        assert report.resumed_from == {"CCC": jan.isoformat()}
        # The resume re-reads January's day (so a half-written day completes) and walks forward.
        assert reader.calls[0][1] == jan
        assert checkpoints == report.calls and report.calls >= 1
        assert [c for _t, c, _v in await self._stored(session, instrument)] == [
            Decimal("1.00"),
            Decimal("2.00"),
            Decimal("3.00"),
        ]

    @pytest.mark.asyncio
    async def test_a_name_whose_read_fails_is_counted_and_the_rest_are_written(
        self, session: AsyncSession
    ) -> None:
        good = await make_instrument(session, "DDD", token=444)
        bad = await make_instrument(session, "EEE", token=555)

        class Flaky(FakeReader):
            def minute_bars(
                self, token: int, start: dt.datetime | dt.date, end: dt.datetime | dt.date
            ) -> list[MinuteBarRecord]:
                if token == 555:
                    raise RuntimeError("Kite: token not found")
                return super().minute_bars(token, start, end)

        reader = Flaky([bar(minute("09:15"), "5")])
        report = await reconcile_session(
            session,
            reader,
            DAY,
            now=minute("15:30"),
            names=[EqName(good, "DDD", 444), EqName(bad, "EEE", 555)],
        )
        assert report.written == 1 and report.skipped == {
            "EEE": "RuntimeError: Kite: token not found"
        }
