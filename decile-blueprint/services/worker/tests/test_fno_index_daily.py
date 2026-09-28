"""``fo_index_daily`` from Kite's daily history (``gates/f3-2-data.md`` S2; ``docs/fno/03`` §9)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import FoIndexDaily, Instrument
from baskfy_providers.records import DAILY_BARS_SCHEMA
from baskfy_worker.fno import index_daily as X
from baskfy_worker.options import index_bars

pytestmark = [requires_db, pytest.mark.db]

NIFTY_TOKEN = 256265
BANK_TOKEN = 260105


class FakeDaily:
    """A ``DailyBarReader``: one candle per weekday in the window, prices keyed on the token."""

    def __init__(self, *, revise_close_by: int = 0) -> None:
        self.calls: list[tuple[int, dt.date, dt.date]] = []
        self.revise = revise_close_by

    def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame:
        self.calls.append((token, start, end))
        rows: list[dict[str, object]] = []
        day = start
        base = Decimal(token % 1000)
        while day <= end:
            if day.weekday() < 5:
                rows.append(
                    {
                        "symbol": str(token),
                        "date": day,
                        "open": base + 1,
                        "high": base + Decimal("2.125"),
                        "low": base - 1,
                        "close": base + Decimal("0.5") + self.revise,
                        "volume": 0,
                        "source": "kite",
                    }
                )
            day += dt.timedelta(days=1)
        return pl.DataFrame(rows, schema=DAILY_BARS_SCHEMA)


async def _indices(session: AsyncSession) -> None:
    for symbol, token in (("NIFTY 50", NIFTY_TOKEN), ("NIFTY BANK", BANK_TOKEN)):
        ident = await make_instrument(session, symbol, token=token, series=None)
        await session.execute(
            sa.update(Instrument).where(Instrument.id == ident).values(instrument_type="INDEX")
        )


async def _rows(session: AsyncSession, underlying: str) -> list[FoIndexDaily]:
    return list(
        (
            await session.execute(
                sa.select(FoIndexDaily)
                .where(FoIndexDaily.underlying == underlying)
                .order_by(FoIndexDaily.trade_date)
            )
        ).scalars()
    )


class TestDailyBackfill:
    async def test_daily_backfill_writes_both_indices_rounded_and_idempotent(
        self, session: AsyncSession
    ) -> None:
        await _indices(session)
        reader = FakeDaily()
        start, end = dt.date(2026, 9, 1), dt.date(2026, 9, 25)

        first = await X.backfill(session, reader, start, end)
        again = await X.backfill(session, reader, start, end)

        assert first.calls == 2 and first.skipped == []
        assert set(first.written) == {"NIFTY", "BANKNIFTY"}
        rows = await _rows(session, "NIFTY")
        assert len(rows) == 19
        assert rows[0].high == Decimal("267.13"), "rounded to the paisa at write"
        assert rows[-1].trade_date == end and first.latest["NIFTY"] == end.isoformat()
        assert len(await _rows(session, "NIFTY")) == 19 and again.written == first.written
        assert all(r.source == "KITE_HIST" for r in rows)

    async def test_daily_backfill_reports_a_missing_index_row_instead_of_guessing(
        self, session: AsyncSession
    ) -> None:
        ident = await make_instrument(session, "NIFTY 50", token=NIFTY_TOKEN, series=None)
        await session.execute(
            sa.update(Instrument).where(Instrument.id == ident).values(instrument_type="INDEX")
        )
        reader = FakeDaily()
        report = await X.backfill(session, reader, dt.date(2026, 9, 21), dt.date(2026, 9, 25))
        assert report.skipped == ["BANKNIFTY"] and report.calls == 1
        assert await _rows(session, "BANKNIFTY") == []


class TestDailyExtend:
    async def test_daily_extend_re_reads_a_week_and_overwrites_a_revised_candle(
        self, session: AsyncSession
    ) -> None:
        await _indices(session)
        await X.backfill(session, FakeDaily(), dt.date(2026, 9, 1), dt.date(2026, 9, 18))
        revised = FakeDaily(revise_close_by=10)

        report = await X.extend(session, revised, dt.date(2026, 9, 25))

        assert [c[1] for c in revised.calls] == [dt.date(2026, 9, 11)] * 2  # a week back
        rows = await _rows(session, "NIFTY")
        assert rows[-1].trade_date == dt.date(2026, 9, 25)
        closes = {r.trade_date: r.close for r in rows}
        assert closes[dt.date(2026, 9, 10)] == Decimal("265.50"), "before the overlap: untouched"
        assert closes[dt.date(2026, 9, 14)] == Decimal("275.50"), "inside the overlap: overwritten"
        assert report.latest["NIFTY"] == "2026-09-25"

    async def test_daily_extend_without_history_starts_at_the_default(
        self, session: AsyncSession
    ) -> None:
        await _indices(session)
        reader = FakeDaily()
        await X.extend(session, reader, dt.date(2026, 9, 25))
        assert all(c[1] == X.DEFAULT_BACKFILL_START for c in reader.calls)

    async def test_daily_bars_come_oldest_first_in_the_cores_shape(
        self, session: AsyncSession
    ) -> None:
        await _indices(session)
        await X.backfill(session, FakeDaily(), dt.date(2026, 9, 1), dt.date(2026, 9, 25))
        bars = await X.daily_bars(session, "NIFTY", end=dt.date(2026, 9, 23), limit=5)
        assert [b.date for b in bars] == [
            dt.date(2026, 9, 17),
            dt.date(2026, 9, 18),
            dt.date(2026, 9, 21),
            dt.date(2026, 9, 22),
            dt.date(2026, 9, 23),
        ]
        assert bars[-1].close == Decimal("265.50")


def test_the_minute_collector_reads_nifty_bank_too() -> None:
    """S2: ``NIFTY BANK`` joins the collector's symbols (the 75-minute confirm's source)."""
    assert index_bars.NIFTY_BANK in index_bars.INDEX_SYMBOLS
    assert set(X.INDEX_SYMBOL_FOR.values()) <= set(index_bars.INDEX_SYMBOLS) | {"INDIA VIX"}
    assert X.INDEX_SYMBOL_FOR == {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}
