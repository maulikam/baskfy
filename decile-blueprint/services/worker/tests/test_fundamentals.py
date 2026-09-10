"""``fundamental_daily`` upsert from NSE quote-equity records (T9.1)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal

import pytest
from helpers import TRADE_DATE, add_bar, make_instrument, requires_db
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import FundamentalDaily
from baskfy_providers.records import EquityFundamental
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.fundamentals import (
    _marketcap_cr,
    _pe_on,
    fundamentals_scope,
    run_fetch_fundamentals,
    store_fundamentals,
)


class TestPeVintage:
    """House rule 5. NSE's quote has no history: it answers with today's price and today's P/E,
    so a fill for a past date must not carry the fetch-day price into that date's row."""

    def test_the_ratio_is_repriced_onto_the_target_dates_close(self) -> None:
        # eps = 200/20 = 10; on `on` the print was 150, so the P/E that day was 15.
        assert _pe_on(Decimal("20"), Decimal("150"), Decimal("200")) == Decimal("15.0000")

    def test_a_same_day_fetch_changes_nothing(self) -> None:
        """The nightly path fetches the day it publishes, where the two prices agree."""
        assert _pe_on(Decimal("20"), Decimal("150"), Decimal("150")) == Decimal("20.0000")

    def test_a_missing_close_leaves_the_quoted_ratio_alone(self) -> None:
        """No exchange print to reprice onto — the quoted ratio is the only number there is."""
        assert _pe_on(Decimal("20"), None, Decimal("200")) == Decimal("20")

    def test_an_absent_ratio_stays_absent(self) -> None:
        """A name without earnings has no P/E, and NULL must not become a number."""
        assert _pe_on(None, Decimal("150"), Decimal("200")) is None


class TestMarketcapMath:
    def test_close_raw_beats_the_quote_last_price(self) -> None:
        assert _marketcap_cr(4_148_506_959, Decimal("1850.25"), Decimal("9999")) == 767578

    def test_a_missing_price_is_none_not_zero(self) -> None:
        assert _marketcap_cr(1_000_000, None, None) is None


@pytest.mark.db
@requires_db
class TestStoreFundamentals:
    async def test_it_upserts_marketcap_from_shares_and_close_raw(
        self, session: AsyncSession
    ) -> None:
        instrument_id = await make_instrument(session, "INFY")
        await add_bar(session, instrument_id, TRADE_DATE, "1850.25")
        records = [
            EquityFundamental(
                symbol="INFY",
                date=TRADE_DATE,
                shares_outstanding=4_148_506_959,
                last_price=Decimal("9999"),
                marketcap_cr=1,
                pe=Decimal("24.50"),
            )
        ]

        result = await store_fundamentals(session, TRADE_DATE, records)
        await session.flush()

        row = (
            await session.execute(
                select(FundamentalDaily).where(
                    FundamentalDaily.instrument_id == instrument_id,
                    FundamentalDaily.date == TRADE_DATE,
                )
            )
        ).scalar_one()
        # 4_148_506_959 * 1850.25 / 1e7 = 767_578 (close_raw wins over the quote last price)
        assert result.rows_written == 1
        assert row.marketcap_cr == 767578
        # Both halves of the row are priced on `on`, not on the day the quote was fetched:
        # 24.50 * 1850.25 / 9999. See `_pe_on` and house rule 5.
        assert row.pe == Decimal("4.5336")
        assert row.shares_outstanding == 4_148_506_959

    async def test_an_unknown_symbol_is_skipped_not_invented(self, session: AsyncSession) -> None:
        records = [
            EquityFundamental(
                symbol="NOTALISTED",
                date=TRADE_DATE,
                shares_outstanding=1_000_000,
                last_price=Decimal("10"),
                marketcap_cr=1,
            )
        ]
        result = await store_fundamentals(session, TRADE_DATE, records)
        assert result.rows_written == 0
        assert result.unmatched == 1


class _Provider:
    def __init__(self, records: list[EquityFundamental]) -> None:
        self.records = records
        self.calls: list[tuple[dt.date, list[str], dict[str, str]]] = []

    def equity_fundamentals(
        self,
        on: dt.date,
        symbols: Sequence[str],
        *,
        series_by_symbol: Mapping[str, str] | None = None,
    ) -> list[EquityFundamental]:
        self.calls.append((on, list(symbols), dict(series_by_symbol or {})))
        return self.records


@pytest.mark.db
@requires_db
class TestFetchTask:
    async def test_it_records_row_counts(self, session: AsyncSession) -> None:
        instrument_id = await make_instrument(session, "TCS")
        await add_bar(session, instrument_id, TRADE_DATE, "4000")
        provider = _Provider(
            [
                EquityFundamental(
                    symbol="TCS",
                    date=TRADE_DATE,
                    shares_outstanding=3_618_087_644,
                    last_price=Decimal("4000"),
                    pe=Decimal("30"),
                )
            ]
        )
        outcome = StepOutcome()
        written = await run_fetch_fundamentals(
            session, provider, outcome, TRADE_DATE, [("TCS", "EQ")]
        )
        assert written == 1
        assert outcome.rows_out == 1
        assert provider.calls == [(TRADE_DATE, ["TCS"], {"TCS": "EQ"})]

    async def test_the_step_scopes_to_the_days_traded_names(self, session: AsyncSession) -> None:
        """At 1 req/s the difference between "every listing" and "traded that day" is hours."""
        traded = await make_instrument(session, "TRADED")
        await add_bar(session, traded, TRADE_DATE, "100")
        await make_instrument(session, "UNTRADED")
        await session.flush()

        assert await fundamentals_scope(session, TRADE_DATE) == [("TRADED", "EQ")]

    async def test_a_symbol_cannot_be_listed_under_two_series_at_all(
        self, session: AsyncSession
    ) -> None:
        """0039 made the row pair this used to build **unrepresentable**, which is the better fix.

        This test read: "(exchange_id, symbol, series) is the unique key, so this is a legal row
        pair. Left alone, both quotes map onto the one instrument_id the symbol lookup returns and
        the batch's ON CONFLICT DO UPDATE touches that row twice, which PostgreSQL refuses." All
        true — and it was a workaround for a schema that let one listing exist twice.

        The key is `(exchange_id, symbol)` now, because `series` is an attribute NSE changes
        rather than part of a listing's identity. So the duplicate cannot be created, the scope
        cannot contain it, and `fundamentals_scope`'s own de-duplication becomes a belt rather
        than the only thing standing between the batch and an error. 120 symbols were in this
        state on the live box before 0039 merged them.
        """
        await make_instrument(session, "TWICE", series="EQ")
        await session.flush()

        with pytest.raises(IntegrityError):
            await make_instrument(session, "TWICE", series="BE")
            await session.flush()
