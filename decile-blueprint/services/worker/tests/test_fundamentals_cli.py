"""The one-off `equity_fundamentals` fill (NEEDS-MAULIK §15).

The properties worth asserting are the ones that decide whether a 2,500-symbol run against a
1 req/s upstream is trustworthy: it covers the date's real universe, it can be restarted without
refetching, running it twice changes nothing (house rule 7), and every symbol it touched is
accounted for rather than quietly dropped (house rule 3).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal

import pytest
from helpers import TRADE_DATE, add_bar, make_instrument, requires_db
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import FundamentalDaily, PipelineRun
from baskfy_providers.errors import (
    ArchiveError,
    ProviderError,
    ProviderUnavailable,
    UnexpectedPayload,
    UpstreamUnavailable,
)
from baskfy_providers.records import EquityFundamental
from baskfy_worker.fundamentals_cli import (
    FillOptions,
    FillReport,
    default_date,
    fill,
    fundamentals_scope,
)

QUIET = FillOptions(progress=False)


async def run(session: AsyncSession, provider: object, options: FillOptions = QUIET) -> FillReport:
    """Fill inside the fixture's transaction: flush is the checkpoint, so it can roll back."""
    return await fill(session, provider, TRADE_DATE, options, checkpoint=session.flush)


class StubProvider:
    """Answers per symbol, records how it was called, and can be told to fail for a name."""

    def __init__(
        self,
        quotes: Mapping[str, EquityFundamental] | None = None,
        *,
        failing: frozenset[str] = frozenset(),
        error: type[ProviderError] = UpstreamUnavailable,
    ) -> None:
        self._quotes = dict(quotes or {})
        self._failing = failing
        self._error = error
        self.calls: list[tuple[str, str | None]] = []

    def equity_fundamentals(
        self,
        on: dt.date,
        symbols: Sequence[str],
        *,
        series_by_symbol: Mapping[str, str] | None = None,
    ) -> list[EquityFundamental]:
        del on
        out: list[EquityFundamental] = []
        for symbol in symbols:
            hint = (series_by_symbol or {}).get(symbol)
            self.calls.append((symbol, hint))
            if symbol in self._failing:
                raise self._error(f"NSE refused {symbol}", provider="nse")
            quote = self._quotes.get(symbol)
            if quote is not None:
                out.append(quote)
        return out


def quote(symbol: str, *, shares: int = 1_000_000_000, pe: str = "20") -> EquityFundamental:
    return EquityFundamental(
        symbol=symbol,
        date=TRADE_DATE,
        shares_outstanding=shares,
        last_price=Decimal("100"),
        pe=Decimal(pe),
    )


async def _universe(session: AsyncSession) -> dict[str, int]:
    """Three names that traded on the date, one of them BE-series, plus one that did not."""
    ids: dict[str, int] = {}
    for symbol, series in (("AAA", "EQ"), ("BBB", "EQ"), ("CCC", "BE")):
        instrument_id = await make_instrument(session, symbol, series=series)
        await add_bar(session, instrument_id, TRADE_DATE, "100")
        ids[symbol] = instrument_id
    # Listed, but no bar on TRADE_DATE — it must never enter the scope.
    ids["DDD"] = await make_instrument(session, "DDD", series="EQ")
    await session.flush()
    return ids


@pytest.mark.db
@requires_db
class TestScope:
    async def test_scope_is_the_dates_own_universe_not_every_listing(
        self, session: AsyncSession
    ) -> None:
        """A symbol with no bar has no close_raw, so a quote for it could not be priced."""
        await _universe(session)
        scope = await fundamentals_scope(session, TRADE_DATE)
        assert [symbol for symbol, _ in scope] == ["AAA", "BBB", "CCC"]

    async def test_scope_carries_the_series_the_database_already_knows(
        self, session: AsyncSession
    ) -> None:
        """NSE answers a wrong series with 200 and an empty body, so the hint is load-bearing."""
        await _universe(session)
        assert dict(await fundamentals_scope(session, TRADE_DATE)) == {
            "AAA": "EQ",
            "BBB": "EQ",
            "CCC": "BE",
        }


def _run(on: dt.date, status: str, data_version: int | None) -> PipelineRun:
    """`started_at` is NOT NULL, so a run row cannot be built from the columns under test alone."""
    return PipelineRun(
        trade_date=on,
        status=status,
        started_at=dt.datetime(2026, 8, 21, 12, 0, tzinfo=dt.UTC),
        data_version=data_version,
    )


@pytest.mark.db
@requires_db
class TestDefaultDate:
    """Which date the command picks when none is given. The two candidates are not the same
    date, and picking the wrong one fills a table nobody reads."""

    async def test_it_prefers_the_published_date_over_the_newest_bars(
        self, session: AsyncSession
    ) -> None:
        """The API resolves every as-of through the published date. A fill aimed at the newer
        bar date leaves every surface on an em dash while `fundamental_daily` looks full."""
        instrument_id = await make_instrument(session, "AAA")
        await add_bar(session, instrument_id, TRADE_DATE, "100")
        newer = TRADE_DATE + dt.timedelta(days=3)
        await add_bar(session, instrument_id, newer, "101")
        session.add(_run(TRADE_DATE, "succeeded", 1))
        await session.flush()

        chosen, why = await default_date(session)

        assert chosen == TRADE_DATE
        assert chosen != newer
        assert "published" in why

    async def test_with_nothing_published_it_falls_back_to_the_newest_bars(
        self, session: AsyncSession
    ) -> None:
        """A plant that has never had a green night still has a sensible date to fill."""
        instrument_id = await make_instrument(session, "AAA")
        await add_bar(session, instrument_id, TRADE_DATE, "100")
        await session.flush()

        chosen, why = await default_date(session)

        assert chosen == TRADE_DATE
        assert "published" in why  # "nothing has been published yet"

    async def test_an_unpublished_run_does_not_count_as_published(
        self, session: AsyncSession
    ) -> None:
        """`data_version IS NULL` is a run that failed or never published; serving it would be
        serving numbers no gate passed."""
        instrument_id = await make_instrument(session, "AAA")
        await add_bar(session, instrument_id, TRADE_DATE, "100")
        newer = TRADE_DATE + dt.timedelta(days=3)
        await add_bar(session, instrument_id, newer, "101")
        session.add(_run(newer, "failed", None))
        await session.flush()

        chosen, _ = await default_date(session)

        assert chosen == newer  # fell back to bars, not to the failed run


@pytest.mark.db
@requires_db
class TestFill:
    async def test_it_stores_a_row_per_quoted_symbol(self, session: AsyncSession) -> None:
        await _universe(session)
        provider = StubProvider({"AAA": quote("AAA"), "BBB": quote("BBB"), "CCC": quote("CCC")})

        report = await run(session, provider)

        assert report.scoped == 3
        assert report.stored == 3
        stored = (
            await session.execute(
                select(func.count())
                .select_from(FundamentalDaily)
                .where(FundamentalDaily.date == TRADE_DATE)
            )
        ).scalar_one()
        assert stored == 3

    async def test_the_series_hint_reaches_the_provider(self, session: AsyncSession) -> None:
        await _universe(session)
        provider = StubProvider({"CCC": quote("CCC")})
        await run(session, provider)
        assert ("CCC", "BE") in provider.calls

    async def test_running_it_twice_produces_identical_rows(self, session: AsyncSession) -> None:
        """House rule 7 — idempotent ingestion, asserted rather than trusted."""
        await _universe(session)
        provider = StubProvider({"AAA": quote("AAA"), "BBB": quote("BBB"), "CCC": quote("CCC")})

        await run(session, provider)
        await session.flush()
        first = await _rows(session)

        await run(session, provider)
        await session.flush()

        assert await _rows(session) == first

    async def test_resume_skips_what_is_already_stored(self, session: AsyncSession) -> None:
        await _universe(session)
        first = StubProvider({"AAA": quote("AAA")})
        await run(session, first, FillOptions(limit=1, progress=False))
        await session.flush()

        second = StubProvider({"BBB": quote("BBB"), "CCC": quote("CCC")})
        report = await run(session, second, FillOptions(resume=True, progress=False))

        assert report.already_present == 1
        assert [symbol for symbol, _ in second.calls] == ["BBB", "CCC"]

    async def test_without_resume_every_symbol_is_attempted_again(
        self, session: AsyncSession
    ) -> None:
        """`--resume` must be a choice, so a deliberate refetch stays possible."""
        await _universe(session)
        await run(session, StubProvider({"AAA": quote("AAA")}))
        await session.flush()

        provider = StubProvider({"AAA": quote("AAA")})
        report = await run(session, provider)
        assert report.already_present == 0
        assert [symbol for symbol, _ in provider.calls] == ["AAA", "BBB", "CCC"]

    async def test_a_dry_run_touches_neither_nse_nor_the_database(
        self, session: AsyncSession
    ) -> None:
        await _universe(session)
        provider = StubProvider({"AAA": quote("AAA")})
        report = await run(session, provider, FillOptions(dry_run=True, progress=False))
        assert provider.calls == []
        assert report.stored == 0
        assert await _rows(session) == []

    async def test_a_dry_run_never_claims_to_be_accounted_for(self, session: AsyncSession) -> None:
        """Otherwise `--dry-run` prints the same reassuring line a real fill prints."""
        await _universe(session)
        report = await run(session, StubProvider(), FillOptions(dry_run=True, progress=False))
        assert report.accounted_for() is False
        assert "DRY RUN" in report.render()


@pytest.mark.db
@requires_db
class TestAccounting:
    async def test_every_scoped_symbol_lands_in_exactly_one_bucket(
        self, session: AsyncSession
    ) -> None:
        """House rule 3: a run covering two of three names must not read as success."""
        await _universe(session)
        provider = StubProvider({"AAA": quote("AAA")}, failing=frozenset({"BBB"}))

        report = await run(session, provider)

        assert report.scoped == 3
        assert report.stored == 1
        assert report.no_quote == ["CCC"]
        assert [symbol for symbol, _ in report.failed] == ["BBB"]
        assert report.accounted_for() is True

    async def test_a_failing_symbol_is_named_so_a_second_pass_can_target_it(
        self, session: AsyncSession
    ) -> None:
        await _universe(session)
        provider = StubProvider(failing=frozenset({"BBB"}))
        report = await run(session, provider)
        assert report.failed[0][0] == "BBB"
        assert "NSE refused BBB" in report.failed[0][1]
        assert "BBB" in report.render()

    async def test_a_quote_for_an_unknown_symbol_is_counted_not_dropped(
        self, session: AsyncSession
    ) -> None:
        """There is no instrument_id to hang it on, so it cannot be stored — but the arithmetic
        must still balance, or a silent drop would read as a clean run."""
        await _universe(session)
        stray = EquityFundamental(
            symbol="NOTINDB", date=TRADE_DATE, shares_outstanding=1, last_price=Decimal("1")
        )
        provider = StubProvider({"AAA": stray})

        report = await run(session, provider)

        assert report.unmatched == 1
        assert report.stored == 0
        assert report.accounted_for() is True
        assert "unmatched=1" in report.render()

    @pytest.mark.parametrize(
        "error",
        [UpstreamUnavailable, UnexpectedPayload, ArchiveError, ProviderUnavailable],
    )
    async def test_any_provider_error_is_recorded_not_fatal(
        self, session: AsyncSession, error: type[ProviderError]
    ) -> None:
        """A 2,500-symbol run must not be killed by one bad name.

        `ArchiveError` is in this list on purpose: an empty 200 body from NSE reaches the caller
        as an archive failure, not a parse failure, and it is the one that actually happened.
        """
        await _universe(session)
        provider = StubProvider(
            {"AAA": quote("AAA"), "CCC": quote("CCC")},
            failing=frozenset({"BBB"}),
            error=error,
        )

        report = await run(session, provider)

        assert report.stored == 2
        assert [symbol for symbol, _ in report.failed] == ["BBB"]
        assert error.__name__ in report.failed[0][1]
        assert report.accounted_for() is True

    async def test_one_failure_does_not_take_its_batch_down(self, session: AsyncSession) -> None:
        """Per-symbol calls exist precisely so a bad name costs one row, not twenty-five."""
        await _universe(session)
        provider = StubProvider(
            {"AAA": quote("AAA"), "CCC": quote("CCC")}, failing=frozenset({"BBB"})
        )
        report = await run(session, provider)
        assert report.stored == 2


async def _rows(session: AsyncSession) -> list[tuple[int, int | None, Decimal | None]]:
    result = await session.execute(
        select(
            FundamentalDaily.instrument_id,
            FundamentalDaily.marketcap_cr,
            FundamentalDaily.pe,
        )
        .where(FundamentalDaily.date == TRADE_DATE)
        .order_by(FundamentalDaily.instrument_id)
    )
    return [(int(r[0]), r[1], r[2]) for r in result.tuples()]
