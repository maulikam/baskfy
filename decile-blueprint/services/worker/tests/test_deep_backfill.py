"""Deep history from Kite: chunking, splicing, and what must never be written (M29).

The module fetches nine years of adjusted candles and joins them onto a verified two-year segment
adjusted a *different* way. Three things decide whether the result is usable:

* the request windows must respect Kite's hard 2,000-day cap, or every call fails;
* the join must be continuous, or every backtest crossing it reads a step that never happened;
* a zero-price placeholder candle must never land, because a zero close is not a cheap price —
  it is the absence of one, and the bar after it is an infinite gain.

Pure where it can be; the database tests use the real `ohlcv_daily`.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from itertools import pairwise

import pytest
from helpers import add_bar, make_instrument, requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OhlcvDaily
from baskfy_worker import deep_backfill as deep
from baskfy_worker.tasks.adjustments import reprocess_instrument


class TestChunking:
    def test_no_window_exceeds_kites_hard_cap(self) -> None:
        """2,001 days is rejected outright: `interval exceeds max limit: 2000 days`."""
        windows = deep.chunk_windows(dt.date(2017, 1, 1), dt.date(2026, 8, 21))

        assert windows, "nine years must produce at least one window"
        for start, end in windows:
            assert (end - start).days < deep.MAX_SPAN_DAYS

    def test_the_windows_are_contiguous_and_gapless(self) -> None:
        windows = deep.chunk_windows(dt.date(2017, 1, 1), dt.date(2026, 8, 21))

        assert windows[0][0] == dt.date(2017, 1, 1)
        assert windows[-1][1] == dt.date(2026, 8, 21)
        for (_, end), (next_start, _) in pairwise(windows):
            assert next_start == end + dt.timedelta(days=1), "a gap would lose a day silently"

    def test_a_request_before_kites_epoch_is_clamped_not_sent(self) -> None:
        """Kite serves nothing before 2000-01-03. Asking anyway wastes a call and returns empty."""
        windows = deep.chunk_windows(dt.date(1990, 1, 1), dt.date(2001, 1, 1))
        assert windows[0][0] == deep.KITE_EPOCH

    def test_nine_years_is_two_requests(self) -> None:
        assert len(deep.chunk_windows(dt.date(2017, 1, 1), dt.date(2026, 8, 21))) == 2


class TestSplicing:
    def test_the_factor_anchors_the_deep_segment_to_the_verified_one(self) -> None:
        """Kite is dividend-adjusted, ours is not; the level has to be matched at the join."""
        seam = dt.date(2024, 1, 1)
        factor, joined = deep.splice_factor({seam: 100.0}, {seam: 105.0})

        assert factor == pytest.approx(1.05)
        assert joined == seam

    def test_it_anchors_on_the_FIRST_shared_date(self) -> None:
        """Any later date has more drift between the two conventions, not less."""
        early, late = dt.date(2024, 1, 1), dt.date(2025, 1, 1)
        factor, joined = deep.splice_factor(
            {early: 100.0, late: 200.0}, {early: 105.0, late: 240.0}
        )

        assert joined == early
        assert factor == pytest.approx(1.05)

    def test_no_shared_date_yields_no_factor(self) -> None:
        """Then there is nothing to anchor to, and the caller refuses rather than guessing."""
        factor, joined = deep.splice_factor(
            {dt.date(2024, 1, 1): 100.0}, {dt.date(2025, 1, 1): 105.0}
        )
        assert (factor, joined) == (None, None)

    def test_a_zero_on_either_side_is_not_an_anchor(self) -> None:
        seam = dt.date(2024, 1, 1)
        assert deep.splice_factor({seam: 0.0}, {seam: 105.0}) == (None, None)
        assert deep.splice_factor({seam: 100.0}, {seam: 0.0}) == (None, None)


@requires_db
@pytest.mark.db
class TestAgainstTheDatabase:
    async def test_written_bars_are_marked_and_carry_no_adjustment_factor(
        self, session: AsyncSession
    ) -> None:
        """`source='kite'` is what stops `reprocess_instrument` touching them."""
        instrument_id = await make_instrument(session, "DEEPCO", token=999002)
        bars = [
            (
                dt.date(2017, 1, 2),
                {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 100.0},
            ),
        ]

        await deep._write(session, instrument_id, bars)
        await session.flush()

        row = (
            await session.execute(
                select(OhlcvDaily).where(OhlcvDaily.instrument_id == instrument_id)
            )
        ).scalar_one()
        assert row.source == "kite"
        assert row.adj_factor == Decimal(1), "the adjustment is inside the price, not on top of it"
        assert row.close == row.close_raw, "there is no exchange print for these years"

    async def test_it_never_overwrites_a_bhavcopy_bar(self, session: AsyncSession) -> None:
        """A bhavcopy row has a real exchange print and a verified adjustment. It wins."""
        instrument_id = await make_instrument(session, "DEEPCO2", token=999003)
        on = dt.date(2024, 1, 2)
        await add_bar(session, instrument_id, on, "500")
        await session.flush()

        await deep._write(
            session,
            instrument_id,
            [(on, {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})],
        )
        await session.flush()

        row = (
            await session.execute(
                select(OhlcvDaily).where(
                    OhlcvDaily.instrument_id == instrument_id, OhlcvDaily.date == on
                )
            )
        ).scalar_one()
        assert row.close_raw == Decimal("500.0000")
        assert row.source != "kite"

    async def test_reprocess_leaves_the_deep_segment_alone(self, session: AsyncSession) -> None:
        """The guard that matters.

        A Kite bar is already adjusted and has no exchange print behind it. If
        `reprocess_instrument` read it, the stored corporate actions would be applied a *second*
        time on top of the vendor's own adjustment — the CUPID double-count of M28.2, but silent
        and across nine years.
        """
        instrument_id = await make_instrument(session, "DEEPCO3", token=999004)
        await deep._write(
            session,
            instrument_id,
            [
                (
                    dt.date(2017, 1, 2),
                    {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1.0},
                )
            ],
        )
        await add_bar(session, instrument_id, dt.date(2024, 1, 2), "500")
        await session.flush()

        await reprocess_instrument(session, instrument_id)
        await session.flush()

        deep_row = (
            await session.execute(
                select(OhlcvDaily).where(
                    OhlcvDaily.instrument_id == instrument_id,
                    OhlcvDaily.date == dt.date(2017, 1, 2),
                )
            )
        ).scalar_one()
        assert deep_row.close == Decimal("10.0000"), "the deep bar was rewritten"
        assert deep_row.source == "kite", "and it kept its provenance"
