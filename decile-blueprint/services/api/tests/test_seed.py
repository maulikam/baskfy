"""Seeding is idempotent and lands the documented rows (Prompt 1 deliverables 4 and 5b)."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from baskfy_api.seed import (
    _run,
    seed_reference,
    seed_reference_fixture,
    seed_trading_days,
)
from baskfy_core.models import (
    FactorDaily,
    IndexDef,
    IndexMemberDaily,
    Instrument,
    Plan,
    Screen,
    TradingDay,
)
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.universes import UNIVERSE_BY_SLUG, UNIVERSES

ENV_VAR = "BASKFY_TEST_DATABASE_URL"

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        os.environ.get(ENV_VAR) is None,
        reason=f"{ENV_VAR} is not set; run `make up` and export it",
    ),
]

API_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def migrated(clean_database: object) -> None:
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env={
            **{k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"},
            "BASKFY_DATABASE_URL": os.environ[ENV_VAR],
        },
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.mark.asyncio
async def test_reference_seed_lands_the_documented_rows(
    engine: AsyncEngine, migrated: None
) -> None:
    async with async_sessionmaker(engine)() as session, session.begin():
        counts = await seed_reference(session)
    # SC1 added the two curated-basket managers to `seed_reference`, and SC2 added the SCAN
    # basket; the expected dict was never widened, so this assertion has been red since 8f0f9be
    # for every run that had a database to fail against.
    #
    # `cb_momentum_scan` is 0 on purpose and is asserted as 0 rather than dropped:
    # `seed_momentum_scan_basket` returns 0 when fewer than `top_n` of its fixture symbols exist
    # in `instrument`, and a reference-only seed has no instruments. If this ever becomes 1 here,
    # the seed order changed and the number is worth noticing.
    assert counts == {
        "exchange": 1,
        # M59 seeded a fifteenth universe; tracked from the registry so it cannot drift again.
        "index_def": len(UNIVERSES),
        "plan": 3,
        "screen": 6,
        "cb_manager": 2,
        "cb_momentum_scan": 0,
        # Two seed steps arrived after this dict was written and it had not caught up.
        # `cb_catalogue` is 0 for the same reason `cb_momentum_scan` is: a catalogue basket is cut
        # *from* a published date's data, and a reference-only seed publishes no pipeline run, so
        # there is nothing to cut. Asserted as 0 rather than dropped — if it ever becomes non-zero
        # here, the seed order changed and that is worth noticing.
        "cb_catalogue": 0,
        "cb_collection": 4,
    }

    async with async_sessionmaker(engine)() as session:
        universes = (await session.execute(select(func.count()).select_from(IndexDef))).scalar_one()
        plans = (await session.execute(select(func.count()).select_from(Plan))).scalar_one()
        screens = (await session.execute(select(func.count()).select_from(Screen))).scalar_one()
    assert (universes, plans, screens) == (len(UNIVERSES), 3, 6)


@pytest.mark.asyncio
async def test_reference_seed_is_idempotent(engine: AsyncEngine, migrated: None) -> None:
    """docs/02 rule 3: re-running any seed or ingest produces identical rows."""
    for _ in range(2):
        async with async_sessionmaker(engine)() as session, session.begin():
            await seed_reference(session)
    async with async_sessionmaker(engine)() as session:
        # Against the registry, not a literal. This said 14 and M59 seeded a fifteenth universe
        # (`nse-sme-emerge`), so it went red the moment the catalog grew — and stayed red unseen,
        # because this file is DB-gated and `make test` skips it. A count that tracks its source
        # cannot drift that way again.
        assert (
            await session.execute(select(func.count()).select_from(IndexDef))
        ).scalar_one() == len(UNIVERSES)
        assert (await session.execute(select(func.count()).select_from(Screen))).scalar_one() == 6


@pytest.mark.asyncio
async def test_seeded_example_screens_validate(engine: AsyncEngine, migrated: None) -> None:
    """What is stored must be re-readable by the same model that wrote it."""
    async with async_sessionmaker(engine)() as session, session.begin():
        await seed_reference(session)
    async with async_sessionmaker(engine)() as session:
        rows = (await session.execute(select(Screen))).scalars().all()
    assert len(rows) == 6
    for row in rows:
        assert row.is_example is True
        assert row.user_id is None
        ScreenDefinition.model_validate(row.definition)


@pytest.mark.asyncio
async def test_plan_prices_are_exact(engine: AsyncEngine, migrated: None) -> None:
    """docs/04: money in numeric, never float — so it must survive a database round trip."""
    async with async_sessionmaker(engine)() as session, session.begin():
        await seed_reference(session)
    async with async_sessionmaker(engine)() as session:
        prices = dict((await session.execute(select(Plan.code, Plan.price_inr))).tuples().all())
    assert prices == {
        "monthly": Decimal("500.00"),
        "yearly": Decimal("3999.00"),
        "forever": Decimal("14999.00"),
    }


@pytest.mark.asyncio
async def test_trading_days_cover_2011_to_current_year(engine: AsyncEngine, migrated: None) -> None:
    async with async_sessionmaker(engine)() as session, session.begin():
        await seed_reference(session)
        await seed_trading_days(session, today=dt.date(2026, 8, 20))
    async with async_sessionmaker(engine)() as session:
        bounds = (
            await session.execute(select(func.min(TradingDay.date), func.max(TradingDay.date)))
        ).one()
    assert bounds == (dt.date(2011, 1, 1), dt.date(2026, 12, 31))


@pytest.mark.asyncio
async def test_reseeding_does_not_demote_reconciled_days(
    engine: AsyncEngine, migrated: None
) -> None:
    """Real market data outranks the provisional seed list and must survive a re-seed."""
    async with async_sessionmaker(engine)() as session, session.begin():
        await seed_reference(session)
        await seed_trading_days(session, today=dt.date(2026, 8, 20))

    observed = dt.date(2026, 1, 26)  # a seeded holiday
    async with async_sessionmaker(engine)() as session, session.begin():
        row = (
            await session.execute(select(TradingDay).where(TradingDay.date == observed))
        ).scalar_one()
        row.source = "bhavcopy"
        row.is_trading_day = True
        row.holiday_name = None

    async with async_sessionmaker(engine)() as session, session.begin():
        await seed_trading_days(session, today=dt.date(2026, 8, 20))

    async with async_sessionmaker(engine)() as session:
        row = (
            await session.execute(select(TradingDay).where(TradingDay.date == observed))
        ).scalar_one()
    assert row.source == "bhavcopy"
    assert row.is_trading_day is True


@pytest.mark.asyncio
async def test_fixture_seed_loads_the_reference_export(engine: AsyncEngine, migrated: None) -> None:
    """docs/13's 271-row answer key must load with no network access."""
    async with async_sessionmaker(engine)() as session, session.begin():
        await seed_reference(session)
        loaded = await seed_reference_fixture(session)
    assert loaded == 271

    async with async_sessionmaker(engine)() as session:
        instruments = (
            await session.execute(select(func.count()).select_from(Instrument))
        ).scalar_one()
        factors = (
            await session.execute(select(func.count()).select_from(FactorDaily))
        ).scalar_one()
        members = (
            await session.execute(select(func.count()).select_from(IndexMemberDaily))
        ).scalar_one()
    assert (instruments, factors, members) == (271, 271, 1435)


@pytest.mark.asyncio
async def test_fixture_seed_preserves_full_precision(engine: AsyncEngine, migrated: None) -> None:
    """The whole point of the docs/13 §4 precision decision, checked through the database."""
    async with async_sessionmaker(engine)() as session, session.begin():
        await seed_reference(session)
        await seed_reference_fixture(session)

    async with async_sessionmaker(engine)() as session:
        row = (
            await session.execute(
                select(FactorDaily)
                .join(Instrument, Instrument.id == FactorDaily.instrument_id)
                .where(Instrument.symbol == "CUPID")
            )
        ).scalar_one()

    assert row.vol_12m == Decimal("0.5793179400")
    assert row.beta_12m == Decimal("0.8412554591")
    assert row.close == Decimal("284.03")
    assert row.marketcap_cr == 38192
    assert row.median_vol_12m == 1_687_913_366


@pytest.mark.asyncio
async def test_fixture_membership_matches_the_denormalised_mask(
    engine: AsyncEngine, migrated: None
) -> None:
    """docs/06: the nightly job asserts the two representations agree; so must the loader."""
    async with async_sessionmaker(engine)() as session, session.begin():
        await seed_reference(session)
        await seed_reference_fixture(session)

    universe = UNIVERSE_BY_SLUG["nifty-total-market"]
    async with async_sessionmaker(engine)() as session:
        from_membership = (
            await session.execute(
                select(func.count())
                .select_from(IndexMemberDaily)
                .where(IndexMemberDaily.index_id == universe.index_id)
            )
        ).scalar_one()
        from_mask = (
            await session.execute(
                select(func.count())
                .select_from(FactorDaily)
                .where(FactorDaily.universe_mask.bitwise_and(universe.mask_value) != 0)
            )
        ).scalar_one()
    assert from_membership == from_mask


@pytest.mark.asyncio
async def test_seed_all_leaves_the_catalogue_surfaces_with_something_to_render(
    engine: AsyncEngine, migrated: None
) -> None:
    """`make seed` must fill the tables the public pages read, not merely run without error.

    This is the test that would have caught 27 Aug 2026's two "the page shows nothing" reports.
    Both were empty tables under working code:

    * `/pricing` rendered its "Before you buy" preamble and no plan cards, because `plan` was
      empty — the deploy runbook had no seed step at all, so staging was migrated and never
      seeded (`docs/runbooks/07-deploy-phase-a.md` §4 now has one).
    * `/discover/collections` rendered an empty directory, and would have kept rendering empty
      shelves even after seeding: the `all` branch called `seed_momentum_scan_basket` with the
      default `FIXTURE_SCAN_SYMBOLS`, fifteen large caps that are not in the 271-row export, so
      it took its "fewer than top_n symbols exist" branch and returned 0. The `e2e` branch was
      fixed for exactly this in M46.6; `all` was not.

    Asserting counts rather than "it ran" is the whole point. A seeder that succeeds and populates
    nothing is indistinguishable from a working one until somebody opens the page.
    """
    # Driven through `_run`, the function the CLI calls, and not by calling the seeders directly.
    # That distinction is the test: `seed_momentum_scan_basket` was never broken — it returned 0
    # because of the arguments the `all` branch passed it. A test that called it with the right
    # ranked symbols would have passed throughout the bug.
    counts = await _run("all", str(engine.url.render_as_string(hide_password=False)))

    assert counts.get("cb_momentum_scan", 0) >= 1, (
        "`seed all` produced no basket, so every collection shelf renders empty — check that the "
        "fixture branch ranks from the reference export and not from FIXTURE_SCAN_SYMBOLS"
    )

    async with async_sessionmaker(engine)() as session:
        plans = (await session.execute(select(func.count()).select_from(Plan))).scalar_one()
        shelves = (await session.execute(text("select count(*) from cb_collection"))).scalar_one()

    # The three docs/01 §1 plans. `/pricing` renders one card each; zero is the reported bug.
    assert plans == 3, f"/pricing would render {plans} cards"
    assert shelves >= 1, "/discover/collections would render an empty directory"
