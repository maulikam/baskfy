"""Prompt 4's acceptance criteria, plus the deliverables behind them.

1. "A test asserts that resolving the NIFTY 500 universe for a past date does NOT use today's
    membership (build a fixture where membership changed and assert the difference)."
2. "Market-health percentages for a fixture universe are computed by hand in the test and
    matched exactly."
3. "Index snapshot rows exist for every trading day in the fixture range with no duplicates."
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import ClassVar

import pytest
from helpers import TRADE_DATE, add_bar, make_instrument, requires_db
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from baskfy_core.models import (
    FactorDaily,
    IndexDef,
    IndexMemberDaily,
    IndexSnapshotDaily,
    Instrument,
    MarketHealthDaily,
)
from baskfy_core.universes import (
    FIRST_NON_UNIVERSE_INDEX_ID,
    MARKET_HEALTH_SLUGS,
    UNIVERSE_BY_SLUG,
    UNIVERSES,
    slugify_index,
)
from baskfy_providers.records import IndexSnapshot, ListingRecord
from baskfy_worker.reference_backfill import backfill_reference_data, trading_days_in
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.listings import listed_count, listings_page, store_listings
from baskfy_worker.tasks.market_health import (
    run_compute_market_health,
)
from baskfy_worker.tasks.membership import (
    DERIVED_BY_RULE,
    PROVIDER_SOURCED,
    SOURCE_DERIVED,
    SOURCE_NSE_FILE,
    SOURCE_RECONSTRUCTED,
    membership_sources,
    refresh_membership,
    resolve_universe,
)
from baskfy_worker.tasks.snapshots import (
    dashboard_rows,
    index_count,
    run_refresh_index_snapshots,
    store_snapshots,
)
from baskfy_worker.window import DateWindow

pytestmark = [pytest.mark.db, requires_db]

PAST = dt.date(2019, 6, 14)
NIFTY_500 = UNIVERSE_BY_SLUG["nifty-500"]


class ChangingMembershipProvider:
    """NIFTY 500's constituents differ between two dates — the fixture criterion 1 asks for."""

    def __init__(self, by_date: dict[dt.date, list[str]]) -> None:
        self.by_date = by_date
        self.name = "changing"

    def index_constituents(self, index_slug: str, on: dt.date) -> list[str]:
        if index_slug != "nifty-500":
            return []
        return list(self.by_date.get(on, []))

    def index_snapshots(self, on: dt.date) -> list[IndexSnapshot]:
        del on
        return []

    def listings(self) -> list[ListingRecord]:
        return []


class TestCriterion1PointInTimeMembership:
    """ "Resolving the NIFTY 500 universe for a past date does NOT use today's membership." """

    async def _seed_two_dates(self, session: AsyncSession) -> dict[str, int]:
        ids = {
            symbol: await make_instrument(session, symbol, token=index + 1)
            for index, symbol in enumerate(["OLDCO", "STAYCO", "NEWCO"])
        }
        provider = ChangingMembershipProvider(
            {
                # In 2019 the index held OLDCO and STAYCO.
                PAST: ["OLDCO", "STAYCO"],
                # By 2026 OLDCO has been dropped and NEWCO added.
                TRADE_DATE: ["STAYCO", "NEWCO"],
            }
        )
        for day in (PAST, TRADE_DATE):
            await refresh_membership(session, provider, day)
        return ids

    async def test_a_past_date_returns_its_own_constituents(self, session: AsyncSession) -> None:
        ids = await self._seed_two_dates(session)
        past = set(await resolve_universe(session, NIFTY_500, PAST))
        assert past == {ids["OLDCO"], ids["STAYCO"]}

    async def test_todays_membership_is_different(self, session: AsyncSession) -> None:
        ids = await self._seed_two_dates(session)
        today = set(await resolve_universe(session, NIFTY_500, TRADE_DATE))
        assert today == {ids["STAYCO"], ids["NEWCO"]}

    async def test_the_two_dates_genuinely_differ(self, session: AsyncSession) -> None:
        """Guards the fixture: comparing two identical sets would prove nothing."""
        await self._seed_two_dates(session)
        past = set(await resolve_universe(session, NIFTY_500, PAST))
        today = set(await resolve_universe(session, NIFTY_500, TRADE_DATE))
        assert past != today

    async def test_a_dropped_constituent_stays_in_the_past(self, session: AsyncSession) -> None:
        """The survivorship-bias case. OLDCO left the index but was in it in 2019, and a screen
        run for 2019 must still see it (docs/01 §10, docs/06 §2)."""
        ids = await self._seed_two_dates(session)
        assert ids["OLDCO"] in await resolve_universe(session, NIFTY_500, PAST)
        assert ids["OLDCO"] not in await resolve_universe(session, NIFTY_500, TRADE_DATE)

    async def test_a_later_addition_is_absent_from_the_past(self, session: AsyncSession) -> None:
        """The look-ahead case, and the one docs/06 §2 calls "the single most common source of
        backtest look-ahead bias": NEWCO joined later and must not appear in a 2019 screen."""
        ids = await self._seed_two_dates(session)
        assert ids["NEWCO"] not in await resolve_universe(session, NIFTY_500, PAST)

    async def test_refreshing_today_does_not_rewrite_the_past(self, session: AsyncSession) -> None:
        """Re-running the nightly step must leave every historical date untouched."""
        ids = await self._seed_two_dates(session)
        provider = ChangingMembershipProvider({TRADE_DATE: ["STAYCO", "NEWCO"]})
        await refresh_membership(session, provider, TRADE_DATE)
        assert set(await resolve_universe(session, NIFTY_500, PAST)) == {
            ids["OLDCO"],
            ids["STAYCO"],
        }

    async def test_an_un_ingested_date_returns_nothing_rather_than_the_latest(
        self, session: AsyncSession
    ) -> None:
        """A visible empty result beats a silently wrong one built from today's membership."""
        await self._seed_two_dates(session)
        assert await resolve_universe(session, NIFTY_500, dt.date(2020, 1, 2)) == []


class TestMembershipSource:
    """docs/09 §Backfill: "record the reconstruction in `index_member_daily.source` so backtests
    can exclude uncertain periods"."""

    async def test_a_published_file_is_recorded_as_observed(self, session: AsyncSession) -> None:
        await make_instrument(session, "STAYCO", token=1)
        provider = ChangingMembershipProvider({TRADE_DATE: ["STAYCO"]})
        await refresh_membership(session, provider, TRADE_DATE)
        assert await membership_sources(session, NIFTY_500, TRADE_DATE) == {SOURCE_NSE_FILE: 1}

    async def test_a_date_with_no_file_is_reconstructed_and_marked(
        self, session: AsyncSession
    ) -> None:
        """docs/09: pre-2018 constituent files do not exist, so early membership is carried back
        from the earliest file — and must be flagged as such."""
        await make_instrument(session, "STAYCO", token=1)
        provider = ChangingMembershipProvider({TRADE_DATE: ["STAYCO"]})
        await refresh_membership(session, provider, TRADE_DATE)
        await refresh_membership(session, provider, PAST)
        assert await membership_sources(session, NIFTY_500, PAST) == {SOURCE_RECONSTRUCTED: 1}

    async def test_reconstruction_can_be_refused(self, session: AsyncSession) -> None:
        """A backtest that wants only observed history must be able to ask for it."""
        await make_instrument(session, "STAYCO", token=1)
        provider = ChangingMembershipProvider({TRADE_DATE: ["STAYCO"]})
        await refresh_membership(session, provider, TRADE_DATE)
        await refresh_membership(session, provider, PAST, allow_reconstruction=False)
        assert await resolve_universe(session, NIFTY_500, PAST) == []

    async def test_an_observed_row_is_never_downgraded_to_reconstructed(
        self, session: AsyncSession
    ) -> None:
        """A later backfill pass must not overwrite a genuine file with a projection of one."""
        await make_instrument(session, "STAYCO", token=1)
        observed = ChangingMembershipProvider({PAST: ["STAYCO"], TRADE_DATE: ["STAYCO"]})
        await refresh_membership(session, observed, PAST)
        await refresh_membership(session, observed, TRADE_DATE)

        silent = ChangingMembershipProvider({TRADE_DATE: ["STAYCO"]})
        await refresh_membership(session, silent, PAST)
        assert await membership_sources(session, NIFTY_500, PAST) == {SOURCE_NSE_FILE: 1}

    async def test_rule_derived_universes_are_marked_derived(self, session: AsyncSession) -> None:
        """docs/06 §"Step 2": allcap and etf come from a rule. Certain, but not from a file."""
        equity = await make_instrument(session, "EQCO", token=1)
        await add_bar(session, equity, TRADE_DATE, "100")
        await refresh_membership(session, ChangingMembershipProvider({}), TRADE_DATE)
        allcap = UNIVERSE_BY_SLUG["nifty-allcap"]
        assert await membership_sources(session, allcap, TRADE_DATE) == {SOURCE_DERIVED: 1}

    async def test_allcap_requires_a_bar_on_the_date(self, session: AsyncSession) -> None:
        """docs/06 §"Step 2": "all instruments with instrument_type='EQ' **and a bar on as_of**".

        Without the bar requirement a suspended or not-yet-listed name would join the universe.
        """
        listed = await make_instrument(session, "TRADED", token=1)
        await make_instrument(session, "SUSPENDED", token=2)
        await add_bar(session, listed, TRADE_DATE, "100")
        await refresh_membership(session, ChangingMembershipProvider({}), TRADE_DATE)
        allcap = UNIVERSE_BY_SLUG["nifty-allcap"]
        assert await resolve_universe(session, allcap, TRADE_DATE) == [listed]

    async def test_etf_and_derived_by_rule_are_disjoint(self) -> None:
        """One branch must answer for any universe (9 Sep 2026).

        `etf` used to be derived by the rule `instrument_type == "ETF"`, and nothing ever wrote
        that type, so it screened to nothing every day. It now comes from NSE's published ETF
        list via `PROVIDER_SOURCED`. Leaving it in BOTH sets would have let the provider branch
        silently win over a rule that still looked authoritative.
        """
        assert "etf" in PROVIDER_SOURCED
        assert "etf" not in DERIVED_BY_RULE
        assert not (set(PROVIDER_SOURCED) & DERIVED_BY_RULE), (
            "a universe resolved by two different branches"
        )

    async def test_etf_holds_only_etf_instruments(self, session: AsyncSession) -> None:
        equity = await make_instrument(session, "EQCO", token=1)
        await add_bar(session, equity, TRADE_DATE, "100")
        etf = Instrument(
            exchange_id=1,
            symbol="NIFTYBEES",
            name="NIFTY BEES",
            series="EQ",
            instrument_type="ETF",
            kite_token=2,
        )
        session.add(etf)
        await session.flush()
        await add_bar(session, etf.id, TRADE_DATE, "250")

        await refresh_membership(session, ChangingMembershipProvider({}), TRADE_DATE)
        # Kept as the record of what the rule DID, and it still works for any universe that
        # uses it — but `etf` no longer does, so nothing is written for it here.
        assert await resolve_universe(session, UNIVERSE_BY_SLUG["etf"], TRADE_DATE) == []


class TestCriterion2MarketHealthByHand:
    """ "Market-health percentages for a fixture universe are computed by hand in the test and
    matched exactly."

    Ten constituents with deliberately chosen factor values, so every percentage is a round
    number that can be read off the fixture rather than recomputed by the same code under test.
    """

    UNIVERSE = UNIVERSE_BY_SLUG["nifty-50"]

    #: (symbol, close, ma_200, ma_50, away_high_ath, ret_12m)
    #: away_high_ath is negative-or-zero: docs/13 §2 finding 2 defines it as (close/high - 1) x 100.
    FIXTURE: tuple[tuple[str, str, str, str, str, str], ...] = (
        # 7 of 10 above the 200-day MA
        ("A", "120", "100", "110", "-2", "15"),
        ("B", "120", "100", "110", "-4", "15"),
        ("C", "120", "100", "110", "-6", "15"),
        ("D", "120", "100", "110", "-8", "15"),
        ("E", "120", "100", "130", "-9", "15"),
        ("F", "120", "100", "130", "-11", "-5"),
        ("G", "120", "100", "130", "-15", "-5"),
        # 3 of 10 below it
        ("H", "90", "100", "80", "-20", "-5"),
        ("I", "90", "100", "80", "-25", "-5"),
        ("J", "90", "100", "95", "-30", "-5"),
    )
    # Hand-counted from the table above:
    EXPECTED_ABOVE_200 = Decimal("70.0000")  # A-G close > ma_200
    EXPECTED_ABOVE_50 = Decimal("60.0000")  # A,B,C,D (120>110), H,I (90>80)
    EXPECTED_WITHIN_10_ATH = Decimal("50.0000")  # |away| <= 10 -> A,B,C,D,E
    EXPECTED_RET_POSITIVE = Decimal("50.0000")  # A-E have ret_12m > 0

    async def _seed(self, session: AsyncSession) -> None:
        for index, (symbol, close, ma200, ma50, away, ret) in enumerate(self.FIXTURE):
            instrument = await make_instrument(session, symbol, token=index + 1)
            session.add(
                FactorDaily(
                    instrument_id=instrument,
                    date=TRADE_DATE,
                    close=Decimal(close),
                    close_raw=Decimal(close),
                    ma_200=Decimal(ma200),
                    ma_50=Decimal(ma50),
                    away_high_ath=Decimal(away),
                    ret_12m=Decimal(ret),
                    series="EQ",
                )
            )
            session.add(
                IndexMemberDaily(
                    index_id=self.UNIVERSE.index_id,
                    date=TRADE_DATE,
                    instrument_id=instrument,
                    source=SOURCE_NSE_FILE,
                )
            )
        await session.flush()

    async def _row(self, session: AsyncSession) -> MarketHealthDaily:
        await run_compute_market_health(session, StepOutcome(), TRADE_DATE)
        return (
            await session.execute(
                select(MarketHealthDaily).where(
                    MarketHealthDaily.index_id == self.UNIVERSE.index_id,
                    MarketHealthDaily.date == TRADE_DATE,
                )
            )
        ).scalar_one()

    async def test_pct_above_200dma(self, session: AsyncSession) -> None:
        await self._seed(session)
        assert (await self._row(session)).pct_above_200dma == self.EXPECTED_ABOVE_200

    async def test_pct_above_50dma(self, session: AsyncSession) -> None:
        await self._seed(session)
        assert (await self._row(session)).pct_above_50dma == self.EXPECTED_ABOVE_50

    async def test_pct_within_10pct_ath(self, session: AsyncSession) -> None:
        await self._seed(session)
        assert (await self._row(session)).pct_within_10pct_ath == self.EXPECTED_WITHIN_10_ATH

    async def test_pct_ret_1y_positive(self, session: AsyncSession) -> None:
        await self._seed(session)
        assert (await self._row(session)).pct_ret_1y_positive == self.EXPECTED_RET_POSITIVE

    async def test_constituent_count(self, session: AsyncSession) -> None:
        await self._seed(session)
        assert (await self._row(session)).constituent_count == len(self.FIXTURE)

    async def test_only_universe_members_are_counted(self, session: AsyncSession) -> None:
        """A stock outside the universe must not move its breadth, however it is behaving."""
        await self._seed(session)
        outsider = await make_instrument(session, "OUTSIDER", token=99)
        session.add(
            FactorDaily(
                instrument_id=outsider,
                date=TRADE_DATE,
                close=Decimal("500"),
                close_raw=Decimal("500"),
                ma_200=Decimal("100"),
                ma_50=Decimal("100"),
                away_high_ath=Decimal("0"),
                ret_12m=Decimal("100"),
                series="EQ",
            )
        )
        await session.flush()
        assert (await self._row(session)).pct_above_200dma == self.EXPECTED_ABOVE_200

    async def test_history_is_stored_per_date(self, session: AsyncSession) -> None:
        """Prompt 4 §3: "Store daily so history is queryable".

        The *reader* is `baskfy_api.market_data.market_health_history`, asserted in
        `services/api/tests/test_api_market_data.py`; what this step owes it is a row per date.
        """
        await self._seed(session)
        await run_compute_market_health(session, StepOutcome(), TRADE_DATE)
        dates = (
            (
                await session.execute(
                    select(MarketHealthDaily.date).where(
                        MarketHealthDaily.index_id == UNIVERSE_BY_SLUG["nifty-50"].index_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert list(dates) == [TRADE_DATE]

    async def test_all_twelve_universes_get_a_row(self, session: AsyncSession) -> None:
        """docs/01 §6's selector lists twelve."""
        await self._seed(session)
        await run_compute_market_health(session, StepOutcome(), TRADE_DATE)
        count = (
            await session.execute(
                select(func.count())
                .select_from(MarketHealthDaily)
                .where(MarketHealthDaily.date == TRADE_DATE)
            )
        ).scalar_one()
        assert int(count) == len(MARKET_HEALTH_SLUGS) == 12


class TestCriterion3IndexSnapshots:
    """Snapshot rows exist for every trading day in the fixture range, with no duplicates."""

    WINDOW = DateWindow(dt.date(2026, 8, 10), dt.date(2026, 8, 18))

    class SnapshotProvider:
        """The same small index set every day, including ones with no fundamentals."""

        def __init__(self) -> None:
            self.name = "snapshots"

        def index_constituents(self, index_slug: str, on: dt.date) -> list[str]:
            del index_slug, on
            return []

        def index_snapshots(self, on: dt.date) -> list[IndexSnapshot]:
            return [
                IndexSnapshot(
                    index_slug="NIFTY 50",
                    date=on,
                    level=Decimal("24500.35"),
                    change_abs=Decimal("100.35"),
                    change_pct=Decimal("0.41"),
                    pe=Decimal("22.4"),
                    pb=Decimal("3.9"),
                    div_yield=Decimal("1.2"),
                ),
                # docs/01 §7: derived indices publish no fundamentals and render as `-`.
                IndexSnapshot(
                    index_slug="India VIX",
                    date=on,
                    level=Decimal("12.5"),
                    change_abs=Decimal("0.5"),
                    change_pct=Decimal("4.0"),
                ),
                IndexSnapshot(
                    index_slug="Nifty50 PR 1x Inverse",
                    date=on,
                    level=Decimal("980.10"),
                    change_abs=Decimal("-4.1"),
                    change_pct=Decimal("-0.41"),
                ),
            ]

        def listings(self) -> list[ListingRecord]:
            return []

    async def test_a_row_exists_for_every_trading_day(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        del clean_db
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            expected_days = await trading_days_in(session, self.WINDOW)
        assert expected_days

        await backfill_reference_data(
            self.SnapshotProvider(), self.WINDOW, database_url=migrated_url
        )

        async with maker() as session:
            days = (
                (await session.execute(select(IndexSnapshotDaily.date).distinct())).scalars().all()
            )
        assert sorted(days) == expected_days

    async def test_no_weekend_rows(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        """Prompt 3 deliverable 7: never compute for a non-trading day."""
        del clean_db
        await backfill_reference_data(
            self.SnapshotProvider(), self.WINDOW, database_url=migrated_url
        )
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            days = (
                (await session.execute(select(IndexSnapshotDaily.date).distinct())).scalars().all()
            )
        assert [d for d in days if d.weekday() >= 5] == []

    async def test_no_duplicates(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        """Run it twice; the (index_id, date) key must keep the table single-valued."""
        del clean_db
        provider = self.SnapshotProvider()
        for _ in range(2):
            await backfill_reference_data(provider, self.WINDOW, database_url=migrated_url)

        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            duplicates = (
                await session.execute(
                    select(func.count()).select_from(
                        select(IndexSnapshotDaily.index_id, IndexSnapshotDaily.date)
                        .group_by(IndexSnapshotDaily.index_id, IndexSnapshotDaily.date)
                        .having(func.count() > 1)
                        .subquery()
                    )
                )
            ).scalar_one()
        assert int(duplicates) == 0

    async def test_indices_without_fundamentals_store_null(self, session: AsyncSession) -> None:
        """docs/01 §7: India VIX renders PE/PB/DivYield as `-`. Zero would read as a real P/E."""
        await store_snapshots(
            session, TRADE_DATE, self.SnapshotProvider().index_snapshots(TRADE_DATE)
        )
        rows = {row["slug"]: row for row in await dashboard_rows(session, TRADE_DATE)}
        vix = rows["india-vix"]
        assert vix["pe"] is None
        assert vix["pb"] is None
        assert vix["div_yield"] is None

    async def test_the_dashboard_is_sorted_by_percent_change_descending(
        self, session: AsyncSession
    ) -> None:
        """docs/01 §7: "Sorted by % change descending"."""
        await store_snapshots(
            session, TRADE_DATE, self.SnapshotProvider().index_snapshots(TRADE_DATE)
        )
        rows = await dashboard_rows(session, TRADE_DATE)
        changes = [row["change_pct"] for row in rows]
        assert all(isinstance(value, Decimal) for value in changes)
        decimals = [value for value in changes if isinstance(value, Decimal)]
        assert decimals == sorted(decimals, reverse=True)


class TestIndexRegistration:
    """docs/01 §7 puts ~145 indices on the dashboard; the bundle names two of them."""

    async def test_a_published_index_we_have_never_seen_is_registered(
        self, session: AsyncSession
    ) -> None:
        before = await index_count(session)
        await store_snapshots(
            session,
            TRADE_DATE,
            [IndexSnapshot(index_slug="NIFTY BANK", date=TRADE_DATE, level=Decimal("52000"))],
        )
        assert await index_count(session) == before + 1

    async def test_a_registered_index_is_not_selectable(self, session: AsyncSession) -> None:
        """docs/01 §2.1 fixes the screener's universe list at 14; the dashboard is separate."""
        await store_snapshots(
            session,
            TRADE_DATE,
            [IndexSnapshot(index_slug="NIFTY BANK", date=TRADE_DATE, level=Decimal("52000"))],
        )
        row = (
            await session.execute(select(IndexDef).where(IndexDef.slug == "nifty-bank"))
        ).scalar_one()
        assert row.is_universe is False

    async def test_a_registered_index_never_takes_a_universe_mask_bit(
        self, session: AsyncSession
    ) -> None:
        """factor_daily.universe_mask has 31 usable bits; a dashboard index stealing one would
        silently change which universe a stock appears to belong to."""
        await store_snapshots(
            session,
            TRADE_DATE,
            [IndexSnapshot(index_slug="NIFTY BANK", date=TRADE_DATE, level=Decimal("52000"))],
        )
        row = (
            await session.execute(select(IndexDef).where(IndexDef.slug == "nifty-bank"))
        ).scalar_one()
        assert row.id >= FIRST_NON_UNIVERSE_INDEX_ID

    async def test_the_fourteen_universes_keep_their_pinned_ids(
        self, session: AsyncSession
    ) -> None:
        await store_snapshots(
            session,
            TRADE_DATE,
            [IndexSnapshot(index_slug="NIFTY 50", date=TRADE_DATE, level=Decimal("24500"))],
        )
        rows = dict((await session.execute(select(IndexDef.slug, IndexDef.id))).tuples().all())
        for universe in UNIVERSES:
            assert rows[universe.slug] == universe.index_id

    async def test_registering_is_idempotent(self, session: AsyncSession) -> None:
        snapshots = [
            IndexSnapshot(index_slug="NIFTY BANK", date=TRADE_DATE, level=Decimal("52000"))
        ]
        await store_snapshots(session, TRADE_DATE, snapshots)
        after_first = await index_count(session)
        await store_snapshots(session, TRADE_DATE, snapshots)
        assert await index_count(session) == after_first

    async def test_ids_do_not_collide_across_batches(self, session: AsyncSession) -> None:
        await store_snapshots(
            session, TRADE_DATE, [IndexSnapshot(index_slug="NIFTY BANK", date=TRADE_DATE)]
        )
        await store_snapshots(
            session, TRADE_DATE, [IndexSnapshot(index_slug="NIFTY IT", date=TRADE_DATE)]
        )
        ids = (
            (
                await session.execute(
                    select(IndexDef.id).where(IndexDef.id >= FIRST_NON_UNIVERSE_INDEX_ID)
                )
            )
            .scalars()
            .all()
        )
        assert len(ids) == len(set(ids)) == 2

    @pytest.mark.parametrize(
        ("published", "expected"),
        [
            ("NIFTY 50", "nifty-50"),
            ("NIFTY MIDCAP 150", "nifty-midcap-150"),
            ("Nifty50 PR 1x Inverse", "nifty50-pr-1x-inverse"),
            ("India VIX", "india-vix"),
            ("  NIFTY  BANK  ", "nifty-bank"),
        ],
    )
    def test_index_names_become_stable_slugs(self, published: str, expected: str) -> None:
        assert slugify_index(published) == expected

    async def test_a_hundred_and_fifty_indices_fit(self, session: AsyncSession) -> None:
        """docs/01 §7 says ~145; the id allocator must not run out or collide at that scale."""
        snapshots = [
            IndexSnapshot(index_slug=f"NIFTY SECTOR {n}", date=TRADE_DATE, level=Decimal(n))
            for n in range(150)
        ]
        await store_snapshots(session, TRADE_DATE, snapshots)
        rows = await dashboard_rows(session, TRADE_DATE)
        assert len(rows) == 150


class TestListings:
    """Prompt 4 deliverable 4: "NSE listing dates and series for the full universe, upserted"."""

    RECORDS: ClassVar[list[ListingRecord]] = [
        ListingRecord(
            symbol="SBIN",
            name="STATE BANK OF INDIA",
            series="EQ",
            isin="INE062A01020",
            listed_on=dt.date(1997, 3, 1),
            face_value=Decimal("1"),
        ),
        ListingRecord(
            symbol="CUPID",
            name="CUPID LIMITED",
            series="EQ",
            isin="INE509G01020",
            listed_on=dt.date(2017, 10, 11),
            face_value=Decimal("1"),
        ),
    ]

    async def test_listings_are_upserted_onto_instrument(self, session: AsyncSession) -> None:
        result = await store_listings(session, self.RECORDS)
        assert result.rows_written == 2
        assert await listed_count(session) == 2

    async def test_the_register_fills_in_what_kite_does_not_publish(
        self, session: AsyncSession
    ) -> None:
        """docs/02: Kite has no series codes, ISINs or listing dates."""
        await store_listings(session, self.RECORDS)
        row = (
            await session.execute(select(Instrument).where(Instrument.symbol == "SBIN"))
        ).scalar_one()
        assert row.listed_on == dt.date(1997, 3, 1)
        assert row.isin == "INE062A01020"
        assert row.series == "EQ"

    async def test_re_reading_an_unchanged_register_is_a_no_op(self, session: AsyncSession) -> None:
        """docs/02 rule 3. An updated_at that moves when nothing changed is noise."""
        await store_listings(session, self.RECORDS)
        row = (
            await session.execute(select(Instrument).where(Instrument.symbol == "SBIN"))
        ).scalar_one()
        first = row.updated_at
        await store_listings(session, self.RECORDS)
        await session.refresh(row)
        assert row.updated_at == first

    async def test_a_new_symbol_is_reported(self, session: AsyncSession) -> None:
        """A first appearance in the register is a new NSE listing (docs/01 §1)."""
        await store_listings(session, self.RECORDS[:1])
        result = await store_listings(session, self.RECORDS)
        assert result.new_symbols == ["CUPID"]

    async def test_an_etf_classification_survives_the_register(self, session: AsyncSession) -> None:
        """The register cannot tell an ETF from an equity; overwriting the Kite-derived type
        would move the instrument out of the `etf` universe (docs/06 §"Step 2")."""
        etf = Instrument(
            exchange_id=1,
            symbol="NIFTYBEES",
            name="NIFTY BEES",
            series="EQ",
            instrument_type="ETF",
            kite_token=1,
        )
        session.add(etf)
        await session.flush()

        await store_listings(
            session,
            [ListingRecord(symbol="NIFTYBEES", name="NIFTY BEES", series="EQ")],
        )
        await session.refresh(etf)
        assert etf.instrument_type == "ETF"

    async def test_the_listings_page_is_newest_first(self, session: AsyncSession) -> None:
        """docs/01 §1: "/listings — All NSE listed securities by listing date"."""
        await store_listings(session, self.RECORDS)
        page = await listings_page(session)
        assert [row.symbol for row in page] == ["CUPID", "SBIN"]

    async def test_the_listings_page_filters_by_series(self, session: AsyncSession) -> None:
        """docs/07: GET /listings?from&to&series=&cursor="""
        await store_listings(
            session,
            [
                *self.RECORDS,
                ListingRecord(
                    symbol="TTL",
                    name="TRADE TO TRADE",
                    series="BE",
                    listed_on=dt.date(2020, 5, 4),
                ),
            ],
        )
        page = await listings_page(session, series=["BE"])
        assert [row.symbol for row in page] == ["TTL"]


class TestReferenceBackfill:
    """Prompt 4 deliverable 5: "A backfill mode for all of the above over the historical range"."""

    WINDOW = DateWindow(dt.date(2026, 8, 10), dt.date(2026, 8, 18))

    async def test_it_covers_only_trading_days(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        del clean_db
        report = await backfill_reference_data(
            TestCriterion3IndexSnapshots.SnapshotProvider(),
            self.WINDOW,
            database_url=migrated_url,
        )
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            expected = await trading_days_in(session, self.WINDOW)
        assert report.trading_days == len(expected) == 7

    async def test_it_writes_market_health_for_every_day(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        del clean_db
        report = await backfill_reference_data(
            TestCriterion3IndexSnapshots.SnapshotProvider(),
            self.WINDOW,
            database_url=migrated_url,
        )
        assert report.market_health_rows == report.trading_days * len(MARKET_HEALTH_SLUGS)

    async def test_running_it_twice_is_idempotent(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        del clean_db
        provider = TestCriterion3IndexSnapshots.SnapshotProvider()
        await backfill_reference_data(provider, self.WINDOW, database_url=migrated_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            first = int(
                (
                    await session.execute(select(func.count()).select_from(IndexSnapshotDaily))
                ).scalar_one()
            )
        await backfill_reference_data(provider, self.WINDOW, database_url=migrated_url)
        async with maker() as session:
            second = int(
                (
                    await session.execute(select(func.count()).select_from(IndexSnapshotDaily))
                ).scalar_one()
            )
        assert first == second

    async def test_one_bad_day_does_not_abandon_the_range(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        """A fifteen-year backfill that aborts on one malformed file has to restart from scratch."""
        del clean_db

        class OneBadDay(TestCriterion3IndexSnapshots.SnapshotProvider):
            def index_snapshots(self, on: dt.date) -> list[IndexSnapshot]:
                if on == dt.date(2026, 8, 12):
                    raise ValueError("malformed NSE file")
                return super().index_snapshots(on)

        report = await backfill_reference_data(OneBadDay(), self.WINDOW, database_url=migrated_url)
        assert list(report.failures) == ["2026-08-12"]
        assert report.snapshot_rows > 0
        assert not report.succeeded


class TestSnapshotStepOutcome:
    async def test_the_step_reports_newly_registered_indices(self, session: AsyncSession) -> None:
        outcome = StepOutcome()
        await run_refresh_index_snapshots(
            session, TestCriterion3IndexSnapshots.SnapshotProvider(), outcome, TRADE_DATE
        )
        registered = outcome.detail["newly_registered"]
        assert isinstance(registered, list)
        assert "india-vix" in registered
