"""AF lane D — tests that fail on the pre-fix code."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import CorporateAction, OhlcvDaily
from baskfy_providers.nse import (
    parse_corporate_action_purpose,
    parse_corporate_action_purposes,
)
from baskfy_worker.tasks.adjustments import reprocess_instrument
from baskfy_worker.tasks.bars import upsert_bars
import polars as pl

from helpers import add_bar, make_instrument


@pytest.mark.db
class TestNightlyKiteIsAdjusted:
    """AF 0.5 — ``source='kite'`` post-seam bars must enter ``adjust_bars``."""

    async def test_a_nightly_kite_bar_receives_the_split_factor(
        self, session: AsyncSession
    ) -> None:
        instrument = await make_instrument(session, "KITEADJ", token=91001)
        before = dt.date(2026, 8, 17)
        ex = dt.date(2026, 8, 18)
        session.add(
            OhlcvDaily(
                instrument_id=instrument,
                date=before,
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100"),
                volume=1000,
                close_raw=Decimal("100"),
                volume_raw=1000,
                open_raw=Decimal("100"),
                high_raw=Decimal("100"),
                low_raw=Decimal("100"),
                adj_factor=Decimal(1),
                source="kite",
            )
        )
        session.add(
            OhlcvDaily(
                instrument_id=instrument,
                date=ex,
                open=Decimal("50"),
                high=Decimal("50"),
                low=Decimal("50"),
                close=Decimal("50"),
                volume=1000,
                close_raw=Decimal("50"),
                volume_raw=1000,
                open_raw=Decimal("50"),
                high_raw=Decimal("50"),
                low_raw=Decimal("50"),
                adj_factor=Decimal(1),
                source="kite",
            )
        )
        session.add(
            CorporateAction(
                instrument_id=instrument,
                action_type="split",
                ex_date=ex,
                ratio_from=Decimal(2),
                ratio_to=Decimal(1),
                raw={"purpose": "FACE VALUE SPLIT FROM RS.2/- TO RE.1/-"},
            )
        )
        await session.flush()

        await reprocess_instrument(session, instrument)
        await session.flush()

        factor = (
            await session.execute(
                select(OhlcvDaily.adj_factor).where(
                    OhlcvDaily.instrument_id == instrument, OhlcvDaily.date == before
                )
            )
        ).scalar_one()
        assert factor == Decimal("0.5000000000")


@pytest.mark.db
class TestOnConflictSkipsAdjustedOhl:
    """AF 0.6 — raw refetch must not overwrite adjusted OHL."""

    async def test_adjusted_ohl_survives_a_raw_refetch(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "OHLKEEP", token=91002)
        on = dt.date(2026, 8, 18)
        session.add(
            OhlcvDaily(
                instrument_id=instrument,
                date=on,
                open=Decimal("40"),
                high=Decimal("45"),
                low=Decimal("38"),
                close=Decimal("42"),
                volume=1000,
                close_raw=Decimal("84"),
                volume_raw=1000,
                open_raw=Decimal("80"),
                high_raw=Decimal("90"),
                low_raw=Decimal("76"),
                adj_factor=Decimal("0.5"),
                source="nse",
            )
        )
        await session.flush()

        frame = pl.DataFrame(
            {
                "date": [on],
                "open": [Decimal("80")],
                "high": [Decimal("90")],
                "low": [Decimal("76")],
                "close": [Decimal("84")],
                "volume": [1000],
                "source": ["nse"],
            }
        )
        await upsert_bars(session, instrument, frame)
        await session.flush()

        row = (
            await session.execute(
                select(OhlcvDaily).where(
                    OhlcvDaily.instrument_id == instrument, OhlcvDaily.date == on
                )
            )
        ).scalar_one()
        assert row.open == Decimal("40.0000")
        assert row.high == Decimal("45.0000")
        assert row.low == Decimal("38.0000")
        assert row.open_raw == Decimal("80.0000")
        assert row.close_raw == Decimal("84.0000")


@pytest.mark.db
class TestM29SeamRescale:
    """AF 3.1 — a later split must rescale ``kite_adjusted`` deep history."""

    async def test_deep_segment_moves_with_the_seam_factor(
        self, session: AsyncSession
    ) -> None:
        instrument = await make_instrument(session, "SEAMCO", token=91003)
        deep_day = dt.date(2017, 1, 2)
        seam = dt.date(2024, 1, 2)
        ex = dt.date(2026, 8, 18)
        session.add(
            OhlcvDaily(
                instrument_id=instrument,
                date=deep_day,
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100"),
                volume=1,
                close_raw=Decimal("100"),
                volume_raw=1,
                adj_factor=Decimal(1),
                source="kite_adjusted",
            )
        )
        await add_bar(session, instrument, seam, "100")
        await add_bar(session, instrument, ex, "50")
        session.add(
            CorporateAction(
                instrument_id=instrument,
                action_type="split",
                ex_date=ex,
                ratio_from=Decimal(2),
                ratio_to=Decimal(1),
                raw={"purpose": "SPLIT 2:1"},
            )
        )
        await session.flush()

        await reprocess_instrument(session, instrument)
        await session.flush()

        deep = (
            await session.execute(
                select(OhlcvDaily).where(
                    OhlcvDaily.instrument_id == instrument, OhlcvDaily.date == deep_day
                )
            )
        ).scalar_one()
        assert deep.close == Decimal("50.0000")
        assert deep.adj_factor == Decimal("0.5000000000")
        assert deep.source == "kite_adjusted"


class TestMultiLegPurposes:
    """AF 3.2 / 3.3."""

    def test_split_and_bonus_yield_two_legs(self) -> None:
        legs = parse_corporate_action_purposes(
            "FACE VALUE SPLIT FROM RS.10/- TO RE.1/- AND BONUS 1:1"
        )
        assert [leg[0] for leg in legs] == ["split", "bonus"]
        assert legs[0][1:] == (Decimal("10"), Decimal("1"), None)
        assert legs[1][1:] == (Decimal("1"), Decimal("1"), None)

    def test_dividend_amounts_are_summed(self) -> None:
        parsed = parse_corporate_action_purpose("DIVIDEND - RS.2.50 + RS.1.00 PER SHARE")
        assert parsed == ("dividend", None, None, Decimal("3.50"))
