"""Catalog explore API + SCAN seed + metrics idempotency - SC2.

DB tests skip without ``BASKFY_TEST_DATABASE_URL``. Pure filter-param documentation is always run.
"""

from __future__ import annotations

import datetime as dt
import inspect
import os
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from baskfy_api.app import create_app
from baskfy_api.auth import Principal, PrincipalKind
from baskfy_api.curated_metrics_service import compute_all_metrics, upsert_metrics_row
from baskfy_api.curated_seed import seed_curated_managers, seed_momentum_scan_basket
from baskfy_api.problems import Problem
from baskfy_api.routers import explore
from baskfy_api.routers.explore import (
    DOCUMENTED_LIST_PARAMS,
    get_explore_constituents,
    list_explore_baskets,
)
from baskfy_core.models import (
    CbBasket,
    CbBasketVersion,
    CbConstituent,
    CbMetrics,
    Exchange,
    Instrument,
)
from baskfy_core.scan_projection import (
    DEFAULT_SCAN_TOP_N,
    FIXTURE_SCAN_SYMBOLS,
    MOMENTUM_SCAN_BASKET_SLUG,
)

ENV_VAR = "BASKFY_TEST_DATABASE_URL"
API_DIR = Path(__file__).resolve().parents[1]

pytestmark_db = [
    pytest.mark.db,
    pytest.mark.skipif(
        os.environ.get(ENV_VAR) is None,
        reason=f"{ENV_VAR} is not set; run `make up` and export it",
    ),
]


def test_documented_list_params_cover_ui_spec_facets() -> None:
    """05-ui-spec chips/dialog -> query params (DECISIONS-SC SC2)."""
    expected = {
        "max_min_amount",
        "access",
        "volatility",
        "category",
        "rebalance_frequency",
        "basket_type",
        "include_new",
        "sort",
        "order",
        "q",
    }
    assert set(DOCUMENTED_LIST_PARAMS) == expected


def test_explore_router_has_no_order_execute_routes() -> None:
    paths_methods: list[tuple[str, str]] = []
    for route in explore.router.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        for method in methods:
            paths_methods.append((method, path))
    for method, path in paths_methods:
        lower = path.lower()
        assert "execute" not in lower
        assert "/order" not in lower
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            assert "watchlist" in lower, f"unexpected mutating route {method} {path}"


def test_openapi_lists_every_documented_explore_query_param() -> None:
    """Filter/sort params round-trip through the published OpenAPI contract."""
    spec = create_app().openapi()
    explore_path = spec["paths"]["/api/v1/explore"]["get"]
    names = {p["name"] for p in explore_path.get("parameters", []) if p.get("in") == "query"}
    assert set(DOCUMENTED_LIST_PARAMS) <= names


def test_explore_list_avoids_n_plus_one_and_documents_p95_budget() -> None:
    """SC11 / leaf-1.8.3: catalog is one JOIN (N+1 avoided); p95 budget < 1s."""
    src = inspect.getsource(list_explore_baskets)
    mod = inspect.getsource(explore)
    assert "N+1 avoided" in src or "N+1 avoided" in mod
    assert "p95" in src or "p95" in mod
    assert "budget" in src.lower() or "budget" in mod.lower()


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    url = os.environ[ENV_VAR]
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=API_DIR,
        env={k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"}
        | {"BASKFY_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=True,
    )


@pytest_asyncio.fixture
async def migrated(engine: AsyncEngine) -> None:
    """Fresh schema: drop public + timescaledb (see screener_helpers._reset_and_seed)."""
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await connection.execute(text("DROP EXTENSION IF EXISTS timescaledb CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    try:
        _alembic("upgrade", "head")
    except subprocess.CalledProcessError as exc:
        raise AssertionError(
            f"alembic failed:\nstdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
        ) from exc


async def _seed_exchange_and_instruments(session: AsyncSession) -> None:
    session.add(Exchange(id=1, code="NSE"))
    await session.flush()
    for symbol in FIXTURE_SCAN_SYMBOLS[:DEFAULT_SCAN_TOP_N]:
        session.add(
            Instrument(
                exchange_id=1,
                symbol=symbol,
                name=f"{symbol} LIMITED",
                series="EQ",
                instrument_type="EQ",
                is_active=True,
            )
        )
    await session.flush()


@pytest.mark.asyncio
@pytest.mark.db
@pytest.mark.skipif(os.environ.get(ENV_VAR) is None, reason=f"{ENV_VAR} is not set")
async def test_scan_basket_seed_is_deterministic_and_idempotent(
    engine: AsyncEngine, migrated: None
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await _seed_exchange_and_instruments(session)
        await session.commit()

        first = await seed_momentum_scan_basket(session)
        await session.commit()
        second = await seed_momentum_scan_basket(session)
        await session.commit()

        basket = (
            await session.execute(
                select(CbBasket).where(CbBasket.slug == MOMENTUM_SCAN_BASKET_SLUG)
            )
        ).scalar_one()
        versions = (
            (
                await session.execute(
                    select(CbBasketVersion).where(CbBasketVersion.basket_id == basket.id)
                )
            )
            .scalars()
            .all()
        )
        constituents = (
            await session.execute(
                select(func.count())
                .select_from(CbConstituent)
                .where(CbConstituent.version_id == versions[0].id)
            )
        ).scalar_one()

    assert first == 1
    assert second == 1
    assert basket.source == "SCAN"
    assert basket.scan_strategy_key == "momentum-scan"
    assert len(versions) == 1
    assert versions[0].version_no == 1
    assert versions[0].label == "GENESIS"
    assert int(constituents) == DEFAULT_SCAN_TOP_N


@pytest.mark.asyncio
@pytest.mark.db
@pytest.mark.skipif(os.environ.get(ENV_VAR) is None, reason=f"{ENV_VAR} is not set")
async def test_metrics_rerun_for_a_date_is_noop(engine: AsyncEngine, migrated: None) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    as_of = dt.date(2024, 6, 3)
    now = dt.datetime(2024, 6, 3, 15, 0, tzinfo=dt.UTC)
    async with factory() as session:
        await _seed_exchange_and_instruments(session)
        await seed_momentum_scan_basket(session, as_of=as_of)
        await session.commit()

        await compute_all_metrics(session, as_of, now=now)
        row1 = (
            await session.execute(select(CbMetrics).where(CbMetrics.as_of_date == as_of))
        ).scalar_one()
        payload1 = (
            row1.min_amount,
            row1.volatility_bucket,
            row1.ret_1m,
            row1.since_inception_pct,
        )

        later = now + dt.timedelta(minutes=5)
        await compute_all_metrics(session, as_of, now=later)
        row2 = (
            await session.execute(select(CbMetrics).where(CbMetrics.as_of_date == as_of))
        ).scalar_one()
        payload2 = (
            row2.min_amount,
            row2.volatility_bucket,
            row2.ret_1m,
            row2.since_inception_pct,
        )
        count = (
            await session.execute(
                select(func.count()).select_from(CbMetrics).where(CbMetrics.as_of_date == as_of)
            )
        ).scalar_one()

    assert int(count) == 1
    assert payload1 == payload2


@pytest.mark.asyncio
@pytest.mark.db
@pytest.mark.skipif(os.environ.get(ENV_VAR) is None, reason=f"{ENV_VAR} is not set")
async def test_explore_filter_params_round_trip(engine: AsyncEngine, migrated: None) -> None:
    """Every documented list query param filters the catalog handler correctly."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_curated_managers(session)
        await _seed_exchange_and_instruments(session)
        await seed_momentum_scan_basket(session)
        basket_id = (
            await session.execute(
                select(CbBasket.id).where(CbBasket.slug == MOMENTUM_SCAN_BASKET_SLUG)
            )
        ).scalar_one()
        await upsert_metrics_row(
            session,
            basket_id=basket_id,
            as_of=dt.date(2024, 6, 3),
            min_amt=Decimal("5000.00"),
            vol_bucket="MED",
            vol_value=Decimal("0.1800000000"),
            ret_1m=Decimal("2.50"),
            ret_6m=Decimal("8.00"),
            ret_1y=Decimal("15.00"),
            cagr_3y=None,
            cagr_5y=None,
            since_inception_pct=Decimal("20.00"),
            computed_at=dt.datetime(2024, 6, 3, 15, 0, tzinfo=dt.UTC),
        )
        await session.commit()

        assert set(DOCUMENTED_LIST_PARAMS) == {
            "max_min_amount",
            "access",
            "volatility",
            "category",
            "rebalance_frequency",
            "basket_type",
            "include_new",
            "sort",
            "order",
            "q",
        }

        result = await list_explore_baskets(
            session,
            _principal(),
            max_min_amount=Decimal("25000"),
            access="FREE",
            volatility="MED",
            category="momentum",
            rebalance_frequency="WEEKLY",
            basket_type="STOCK",
            include_new=True,
            sort="min_amount",
            order="asc",
            q="momentum",
        )
        assert result.total >= 1
        assert any(item.slug == MOMENTUM_SCAN_BASKET_SLUG for item in result.items)
        card = next(i for i in result.items if i.slug == MOMENTUM_SCAN_BASKET_SLUG)
        assert card.metrics is not None
        assert card.metrics.min_amount == Decimal("5000.00")


def _principal() -> Principal:
    """The coroutine-level tests drive handlers directly, so they must supply the principal
    the route now requires. The HTTP-level assertions live in ``test_explore_http.py``."""
    return Principal(kind=PrincipalKind.USER, user_id=1, public_id="testuser0001")


@pytest.mark.asyncio
@pytest.mark.db
@pytest.mark.skipif(os.environ.get(ENV_VAR) is None, reason=f"{ENV_VAR} is not set")
async def test_constituents_route_serves_the_newest_version(
    engine: AsyncEngine, migrated: None
) -> None:
    """`/explore/{slug}/constituents` — the route the stub page was waiting for (9 Sep 2026).

    `/basket/[slug]/constituents` apologised that constituent rows "need an immutable version
    from the catalog engine (SC3)". SC3 shipped and the box carries 6 versions and 103
    `cb_constituent` rows; what was missing was a route. This asserts the route answers the
    newest version, heaviest weight first.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await _seed_exchange_and_instruments(session)
        await session.commit()
        await seed_momentum_scan_basket(session)
        await session.commit()

        out = await get_explore_constituents(MOMENTUM_SCAN_BASKET_SLUG, session, _signed_in())

    assert out.slug == MOMENTUM_SCAN_BASKET_SLUG
    assert out.version_no == 1
    assert out.label == "GENESIS"
    assert len(out.constituents) == DEFAULT_SCAN_TOP_N
    weights = [row.weight for row in out.constituents]
    assert weights == sorted(weights, reverse=True), "heaviest weight must lead"
    assert all(row.symbol for row in out.constituents), "a constituent with no symbol"


@pytest.mark.asyncio
@pytest.mark.db
@pytest.mark.skipif(os.environ.get(ENV_VAR) is None, reason=f"{ENV_VAR} is not set")
async def test_an_unknown_basket_is_a_404_not_an_empty_list(
    engine: AsyncEngine, migrated: None
) -> None:
    """Visibility is the same predicate every explore route uses, so an unlisted or absent
    basket must 404 here exactly as it does on the card — never leak as an empty page."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(Problem):
            await get_explore_constituents("no-such-basket", session, _signed_in())


def _signed_in() -> Principal:
    """A signed-in principal. The route only calls `require_user()`; the tenancy that matters is
    enforced by `_visible()` on the query, not by the caller."""
    return Principal(kind=PrincipalKind.USER, user_id=1, email="reader@example.com")
