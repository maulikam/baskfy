"""SC2 contract tests for the sole-operator curated catalog and watchlist."""

from __future__ import annotations

import ast
import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from unittest import mock

import api_helpers
import httpx
import pytest
import pytest_asyncio
import screener_helpers
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import curated_catalog as catalog_service
from baskfy_api.settings import Settings
from baskfy_core.models import (
    AppUser,
    BasketCollection,
    BasketConstituent,
    BasketMetrics,
    BasketVersion,
    BasketWatchlistItem,
    CuratedBasket,
    CuratedManager,
    Exchange,
    Instrument,
    TradingDay,
)

pytestmark = [pytest.mark.db, screener_helpers.requires_db]

API_SOURCE = Path(__file__).resolve().parents[1] / "src" / "baskfy_api"


@dataclass(frozen=True, slots=True)
class CatalogSeed:
    user: AppUser
    baskets: dict[str, CuratedBasket]
    engine: CuratedManager
    maulik: CuratedManager


@dataclass(frozen=True, slots=True)
class BasketSpec:
    slug: str
    name: str
    manager_id: int
    launched_at: dt.date
    categories: list[str]
    access: str = "FREE"
    rebalance_frequency: str = "MONTHLY"
    basket_type: str = "STOCK"


@dataclass(frozen=True, slots=True)
class MetricsSpec:
    as_of: dt.date
    minimum: str
    volatility: str
    since: str
    one_month: str
    six_month: str
    one_year: str
    three_year: str
    five_year: str


@pytest_asyncio.fixture
async def curated_api(
    seeded_url: str, screener_session: AsyncSession
) -> AsyncIterator[httpx.AsyncClient]:
    settings = api_helpers.api_settings(
        seeded_url,
        rate_limit_enabled=False,
        sole_user_id="baskfy-sole-user",
    )
    async with api_helpers.running_app(settings, screener_session) as client:
        yield client


def _basket(spec: BasketSpec) -> CuratedBasket:
    return CuratedBasket(
        slug=spec.slug,
        name=spec.name,
        manager_id=spec.manager_id,
        type=spec.basket_type,
        access=spec.access,
        visibility="PUBLISHED",
        categories=spec.categories,
        description_md=f"{spec.name} pitch",
        rationale_md=f"{spec.name} rationale",
        rebalance_frequency=spec.rebalance_frequency,
        benchmark_instrument_id=None,
        launched_at=spec.launched_at,
        next_review_at=dt.date(2026, 9, 30),
        source="MANUAL",
        scan_strategy_key=None,
        archived_at=None,
    )


def _metrics(basket_id: int, spec: MetricsSpec) -> BasketMetrics:
    return BasketMetrics(
        basket_id=basket_id,
        as_of_date=spec.as_of,
        min_amount=Decimal(spec.minimum),
        volatility_bucket=spec.volatility,
        volatility_value=Decimal("0.1234567890"),
        ret_1m=Decimal(spec.one_month),
        ret_6m=Decimal(spec.six_month),
        ret_1y=Decimal(spec.one_year),
        cagr_3y=Decimal(spec.three_year),
        cagr_5y=Decimal(spec.five_year),
        since_inception_pct=Decimal(spec.since),
    )


@pytest_asyncio.fixture
async def catalog_seed(screener_session: AsyncSession) -> CatalogSeed:
    user = (
        await screener_session.execute(
            select(AppUser).where(AppUser.public_id == "baskfy-sole-user")
        )
    ).scalar_one()
    managers = {
        manager.slug: manager
        for manager in (
            await screener_session.execute(
                select(CuratedManager).where(CuratedManager.slug.in_(["baskfy-engine", "maulik"]))
            )
        )
        .scalars()
        .all()
    }
    engine = managers["baskfy-engine"]
    maulik = managers["maulik"]
    baskets = {
        "alpha-old": _basket(
            BasketSpec(
                slug="alpha-old",
                name="Alpha Old",
                manager_id=engine.id,
                launched_at=dt.date(2020, 1, 1),
                categories=["thematic", "growth"],
                access="FEE",
            )
        ),
        "beta-new": _basket(
            BasketSpec(
                slug="beta-new",
                name="Beta New",
                manager_id=maulik.id,
                launched_at=dt.date(2025, 11, 20),
                categories=["quantitative"],
                rebalance_frequency="WEEKLY",
            )
        ),
        "gamma-mid": _basket(
            BasketSpec(
                slug="gamma-mid",
                name="Gamma Mid",
                manager_id=engine.id,
                launched_at=dt.date(2024, 1, 1),
                categories=["growth"],
                rebalance_frequency="QUARTERLY",
            )
        ),
    }
    screener_session.add_all(baskets.values())
    await screener_session.flush()

    instruments = (
        (
            await screener_session.execute(
                select(Instrument).where(Instrument.is_active.is_(True)).order_by(Instrument.id)
            )
        )
        .scalars()
        .all()
    )[:3]
    assert len(instruments) == 3

    alpha_v1 = BasketVersion(
        basket_id=baskets["alpha-old"].id,
        version_no=1,
        effective_date=dt.date(2020, 1, 1),
        label="GENESIS",
        added_count=1,
        removed_count=0,
        notes_md=None,
        source_scan_run_id=None,
    )
    alpha_v2 = BasketVersion(
        basket_id=baskets["alpha-old"].id,
        version_no=2,
        effective_date=dt.date(2026, 8, 1),
        label="CHANGED",
        added_count=1,
        removed_count=0,
        notes_md="Second version",
        source_scan_run_id=None,
    )
    alpha_v3_scheduled = BasketVersion(
        basket_id=baskets["alpha-old"].id,
        version_no=3,
        effective_date=dt.date(2026, 9, 1),
        label="CHANGED",
        added_count=1,
        removed_count=2,
        notes_md="Scheduled, not active at the latest metric date",
        source_scan_run_id=None,
    )
    beta_v1 = BasketVersion(
        basket_id=baskets["beta-new"].id,
        version_no=1,
        effective_date=dt.date(2025, 11, 20),
        label="GENESIS",
        added_count=1,
        removed_count=0,
        notes_md=None,
        source_scan_run_id=None,
    )
    gamma_v1 = BasketVersion(
        basket_id=baskets["gamma-mid"].id,
        version_no=1,
        effective_date=dt.date(2024, 1, 1),
        label="GENESIS",
        added_count=1,
        removed_count=0,
        notes_md=None,
        source_scan_run_id=None,
    )
    screener_session.add_all([alpha_v1, alpha_v2, alpha_v3_scheduled, beta_v1, gamma_v1])
    await screener_session.flush()
    screener_session.add_all(
        [
            BasketConstituent(
                version_id=alpha_v1.id,
                instrument_id=instruments[2].id,
                segment="Largecap",
                weight=Decimal("1.0000"),
            ),
            BasketConstituent(
                version_id=alpha_v2.id,
                instrument_id=instruments[0].id,
                segment="Largecap",
                weight=Decimal("0.6000"),
            ),
            BasketConstituent(
                version_id=alpha_v2.id,
                instrument_id=instruments[1].id,
                segment="Midcap",
                weight=Decimal("0.4000"),
            ),
            BasketConstituent(
                version_id=alpha_v3_scheduled.id,
                instrument_id=instruments[2].id,
                segment="Smallcap",
                weight=Decimal("1.0000"),
            ),
            BasketConstituent(
                version_id=beta_v1.id,
                instrument_id=instruments[1].id,
                segment="Largecap",
                weight=Decimal("1.0000"),
            ),
            BasketConstituent(
                version_id=gamma_v1.id,
                instrument_id=instruments[2].id,
                segment="Midcap",
                weight=Decimal("1.0000"),
            ),
        ]
    )
    screener_session.add_all(
        [
            _metrics(
                baskets["alpha-old"].id,
                MetricsSpec(
                    as_of=dt.date(2026, 7, 31),
                    minimum="4500.00",
                    volatility="MED",
                    since="50.0000000000",
                    one_month="1.0000000000",
                    six_month="9.0000000000",
                    one_year="19.0000000000",
                    three_year="29.0000000000",
                    five_year="49.0000000000",
                ),
            ),
            _metrics(
                baskets["alpha-old"].id,
                MetricsSpec(
                    as_of=dt.date(2026, 8, 20),
                    minimum="4000.00",
                    volatility="LOW",
                    since="55.0000000000",
                    one_month="2.0000000000",
                    six_month="10.0000000000",
                    one_year="20.0000000000",
                    three_year="30.0000000000",
                    five_year="50.0000000000",
                ),
            ),
            _metrics(
                baskets["beta-new"].id,
                MetricsSpec(
                    as_of=dt.date(2026, 8, 20),
                    minimum="24000.00",
                    volatility="HIGH",
                    since="7.0000000000",
                    one_month="5.0000000000",
                    six_month="5.0000000000",
                    one_year="5.0000000000",
                    three_year="5.0000000000",
                    five_year="5.0000000000",
                ),
            ),
            _metrics(
                baskets["gamma-mid"].id,
                MetricsSpec(
                    as_of=dt.date(2026, 8, 20),
                    minimum="10000.00",
                    volatility="MED",
                    since="25.0000000000",
                    one_month="3.0000000000",
                    six_month="11.0000000000",
                    one_year="20.0000000000",
                    three_year="40.0000000000",
                    five_year="60.0000000000",
                ),
            ),
        ]
    )
    await screener_session.flush()
    screener_session.add(
        BasketCollection(
            slug="editor-picks",
            title="Editor picks",
            subtitle="A deliberately ordered collection",
            basket_ids=[baskets["beta-new"].id, baskets["alpha-old"].id],
            position=1,
            curated_meta={"tone": "measured"},
        )
    )
    await screener_session.flush()
    return CatalogSeed(user=user, baskets=baskets, engine=engine, maulik=maulik)


def _headers(seed: CatalogSeed) -> dict[str, str]:
    return api_helpers.bearer(seed.user.public_id)


@pytest.mark.asyncio
async def test_catalog_filter_query_round_trip_and_dark_access(
    curated_api: httpx.AsyncClient, catalog_seed: CatalogSeed
) -> None:
    response = await curated_api.get(
        api_helpers.url("/catalog"),
        params=[
            ("category", " thematic "),
            ("category", "thematic"),
            ("category", "growth"),
            ("volatility", "LOW"),
            ("max_min_amount", "5000.00"),
            ("rebalance_frequency", "MONTHLY"),
            ("type", "STOCK"),
            ("include_new", "false"),
            ("access", "FREE"),
            ("sort", "returns"),
        ],
        headers=_headers(catalog_seed),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query"] == {
        "category": ["thematic", "growth"],
        "volatility": "LOW",
        "max_min_amount": 5000.0,
        "rebalance_frequency": "MONTHLY",
        "type": "STOCK",
        "include_new": False,
        "access": "FREE",
        "sort": "returns",
    }
    assert [row["slug"] for row in body["baskets"]] == ["alpha-old"]
    assert body["baskets"][0]["access"] == "FREE"  # stored FEE stays dark
    assert body["baskets"][0]["headline_return"] == {"label": "5Y CAGR", "value": 50.0}
    assert '"min_amount":4000.00' in response.text

    fee_only = await curated_api.get(
        api_helpers.url("/catalog"),
        params={"access": "FEE"},
        headers=_headers(catalog_seed),
    )
    assert fee_only.status_code == 200
    assert fee_only.json()["baskets"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("popular", ["beta-new", "gamma-mid", "alpha-old"]),
        ("newest", ["beta-new", "gamma-mid", "alpha-old"]),
        ("returns", ["alpha-old", "gamma-mid", "beta-new"]),
        ("min_amount", ["alpha-old", "gamma-mid", "beta-new"]),
    ],
)
async def test_catalog_sort_is_deterministic(
    sort: str,
    expected: list[str],
    curated_api: httpx.AsyncClient,
    catalog_seed: CatalogSeed,
) -> None:
    response = await curated_api.get(
        api_helpers.url("/catalog"), params={"sort": sort}, headers=_headers(catalog_seed)
    )
    assert response.status_code == 200, response.text
    assert [row["slug"] for row in response.json()["baskets"]] == expected


@pytest.mark.asyncio
async def test_catalog_filter_excludes_new_baskets_when_toggle_is_off(
    curated_api: httpx.AsyncClient, catalog_seed: CatalogSeed
) -> None:
    response = await curated_api.get(
        api_helpers.url("/catalog"),
        params={"include_new": "false"},
        headers=_headers(catalog_seed),
    )
    assert response.status_code == 200
    assert [row["slug"] for row in response.json()["baskets"]] == [
        "gamma-mid",
        "alpha-old",
    ]


@pytest.mark.asyncio
async def test_detail_uses_latest_version_metrics_and_constituents(
    curated_api: httpx.AsyncClient, catalog_seed: CatalogSeed
) -> None:
    response = await curated_api.get(
        api_helpers.url("/catalog/alpha-old"), headers=_headers(catalog_seed)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["latest_version"] == {
        "version_no": 2,
        "effective_date": "2026-08-01",
        "label": "CHANGED",
        "added_count": 1,
        "removed_count": 0,
    }
    assert body["metrics"]["as_of_date"] == "2026-08-20"
    assert body["metrics"]["min_amount"] == 4000.0
    assert len(body["constituents"]) == 2
    assert {row["weight"] for row in body["constituents"]} == {0.6, 0.4}

    young = await curated_api.get(
        api_helpers.url("/catalog/beta-new"), headers=_headers(catalog_seed)
    )
    assert young.status_code == 200, young.text
    assert young.json()["headline_return"] == {"label": "9M returns", "value": 7.0}


@pytest.mark.asyncio
async def test_returns_sort_and_label_use_calendar_month_boundaries(
    curated_api: httpx.AsyncClient,
    catalog_seed: CatalogSeed,
    screener_session: AsyncSession,
) -> None:
    boundary = _basket(
        BasketSpec(
            slug="calendar-boundary",
            name="Calendar Boundary",
            manager_id=catalog_seed.engine.id,
            launched_at=dt.date(2021, 8, 24),
            categories=["calendar-boundary"],
        )
    )
    screener_session.add(boundary)
    await screener_session.flush()
    screener_session.add(
        _metrics(
            boundary.id,
            MetricsSpec(
                as_of=dt.date(2026, 8, 23),
                minimum="5000.00",
                volatility="LOW",
                since="80.0000000000",
                one_month="1.0000000000",
                six_month="2.0000000000",
                one_year="3.0000000000",
                three_year="31.0000000000",
                five_year="99.0000000000",
            ),
        )
    )
    await screener_session.flush()
    response = await curated_api.get(
        api_helpers.url("/catalog"),
        params={"category": "calendar-boundary", "sort": "returns"},
        headers=_headers(catalog_seed),
    )
    assert response.status_code == 200, response.text
    assert response.json()["baskets"][0]["headline_return"] == {
        "label": "3Y CAGR",
        "value": 31.0,
    }


@pytest.mark.asyncio
async def test_include_new_filter_uses_latest_metric_calendar_boundary(
    curated_api: httpx.AsyncClient,
    catalog_seed: CatalogSeed,
    screener_session: AsyncSession,
) -> None:
    exact_year = _basket(
        BasketSpec(
            slug="exactly-one-year",
            name="Exactly One Year",
            manager_id=catalog_seed.engine.id,
            launched_at=dt.date(2025, 8, 20),
            categories=["age-boundary"],
        )
    )
    one_day_new = _basket(
        BasketSpec(
            slug="one-day-inside-year",
            name="One Day Inside Year",
            manager_id=catalog_seed.engine.id,
            launched_at=dt.date(2025, 8, 21),
            categories=["age-boundary"],
        )
    )
    screener_session.add_all([exact_year, one_day_new])
    await screener_session.flush()
    response = await curated_api.get(
        api_helpers.url("/catalog"),
        params=[("category", "age-boundary"), ("include_new", "false")],
        headers=_headers(catalog_seed),
    )
    assert response.status_code == 200, response.text
    assert [row["slug"] for row in response.json()["baskets"]] == ["exactly-one-year"]


@pytest.mark.asyncio
async def test_manager_and_collection_preserve_latest_cards_and_collection_order(
    curated_api: httpx.AsyncClient, catalog_seed: CatalogSeed
) -> None:
    manager = await curated_api.get(
        api_helpers.url("/managers/baskfy-engine"), headers=_headers(catalog_seed)
    )
    assert manager.status_code == 200, manager.text
    assert manager.json()["sebi_reg_no"] is None
    assert [row["slug"] for row in manager.json()["baskets"]] == [
        "gamma-mid",
        "alpha-old",
    ]

    collection = await curated_api.get(
        api_helpers.url("/collections/editor-picks"), headers=_headers(catalog_seed)
    )
    assert collection.status_code == 200, collection.text
    assert [row["slug"] for row in collection.json()["baskets"]] == [
        "beta-new",
        "alpha-old",
    ]
    assert collection.json()["curated_meta"] == {"tone": "measured"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/catalog/missing", "/managers/missing", "/collections/missing"],
)
async def test_detail_manager_collection_missing_states_are_honest(
    path: str, curated_api: httpx.AsyncClient, catalog_seed: CatalogSeed
) -> None:
    response = await curated_api.get(api_helpers.url(path), headers=_headers(catalog_seed))
    api_helpers.assert_problem(response, 404, "not-found")


@pytest.mark.asyncio
async def test_watchlist_crud_is_idempotent_preserves_baseline_and_computes_moved_pct(
    curated_api: httpx.AsyncClient,
    catalog_seed: CatalogSeed,
    screener_session: AsyncSession,
) -> None:
    path = api_helpers.url("/watchlist/alpha-old")
    first = await curated_api.put(path, headers=_headers(catalog_seed))
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["nav_at_watch"] == 155.0
    assert first_body["moved_pct"] == 0.0
    # 31 Jul -> 20 Aug has missing metric snapshots, so calling it a daily change would lie.
    assert first_body["daily_change_pct"] is None

    screener_session.add(
        _metrics(
            catalog_seed.baskets["alpha-old"].id,
            MetricsSpec(
                as_of=dt.date(2026, 8, 21),
                minimum="4000.00",
                volatility="LOW",
                since="70.0000000000",
                one_month="2.0000000000",
                six_month="10.0000000000",
                one_year="20.0000000000",
                three_year="30.0000000000",
                five_year="50.0000000000",
            ),
        )
    )
    await screener_session.flush()

    second = await curated_api.put(path, headers=_headers(catalog_seed))
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["watched_at"] == first_body["watched_at"]
    assert second_body["nav_at_watch"] == 155.0
    assert second_body["nav_today"] == 170.0
    assert second_body["moved_pct"] == pytest.approx(0.0967741935)
    assert second_body["daily_change_pct"] == pytest.approx(0.0967741935)

    listed = await curated_api.get(api_helpers.url("/watchlist"), headers=_headers(catalog_seed))
    assert listed.status_code == 200, listed.text
    assert listed.json()["count"] == 1
    assert listed.json()["items"][0]["basket"]["watched"] is True
    assert listed.json()["items"][0]["daily_change_pct"] == pytest.approx(0.0967741935)

    removed = await curated_api.delete(path, headers=_headers(catalog_seed))
    assert removed.status_code == 204
    assert removed.content == b""
    reset = await curated_api.put(path, headers=_headers(catalog_seed))
    assert reset.status_code == 200
    assert reset.json()["nav_at_watch"] == 170.0
    assert reset.json()["watched_at"] != first_body["watched_at"]


@pytest.mark.asyncio
async def test_watchlist_daily_change_is_null_without_a_prior_metric(
    curated_api: httpx.AsyncClient, catalog_seed: CatalogSeed
) -> None:
    response = await curated_api.put(
        api_helpers.url("/watchlist/beta-new"), headers=_headers(catalog_seed)
    )
    assert response.status_code == 200, response.text
    assert response.json()["daily_change_pct"] is None


@pytest.mark.asyncio
async def test_watchlist_daily_change_uses_only_the_nse_calendar(
    curated_api: httpx.AsyncClient,
    catalog_seed: CatalogSeed,
    screener_session: AsyncSession,
) -> None:
    basket = catalog_seed.baskets["beta-new"]
    for as_of, since in (
        (dt.date(2026, 8, 21), "8.0000000000"),
        (dt.date(2026, 8, 24), "10.0000000000"),
    ):
        screener_session.add(
            _metrics(
                basket.id,
                MetricsSpec(
                    as_of=as_of,
                    minimum="24000.00",
                    volatility="HIGH",
                    since=since,
                    one_month="5.0000000000",
                    six_month="5.0000000000",
                    one_year="5.0000000000",
                    three_year="5.0000000000",
                    five_year="5.0000000000",
                ),
            )
        )
    other_exchange = Exchange(id=2, code="TESTX")
    screener_session.add(other_exchange)
    await screener_session.flush()
    screener_session.add(
        TradingDay(
            exchange_id=other_exchange.id,
            date=dt.date(2026, 8, 23),
            is_trading_day=True,
            holiday_name=None,
            source="derived",
        )
    )
    await screener_session.flush()

    response = await curated_api.put(
        api_helpers.url("/watchlist/beta-new"), headers=_headers(catalog_seed)
    )
    assert response.status_code == 200, response.text
    assert response.json()["daily_change_pct"] == pytest.approx(0.0185185185)


@pytest.mark.asyncio
async def test_watchlist_missing_metric_fails_without_inventing_a_baseline(
    curated_api: httpx.AsyncClient,
    catalog_seed: CatalogSeed,
    screener_session: AsyncSession,
) -> None:
    empty = _basket(
        BasketSpec(
            slug="no-metrics",
            name="No Metrics",
            manager_id=catalog_seed.engine.id,
            launched_at=dt.date(2026, 8, 1),
            categories=["test"],
        )
    )
    screener_session.add(empty)
    await screener_session.flush()
    response = await curated_api.put(
        api_helpers.url("/watchlist/no-metrics"), headers=_headers(catalog_seed)
    )
    api_helpers.assert_problem(response, 503, "pipeline-degraded")
    assert (
        await screener_session.execute(
            select(BasketWatchlistItem).where(BasketWatchlistItem.basket_id == empty.id)
        )
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/catalog"),
        ("GET", "/catalog/alpha-old"),
        ("GET", "/managers/baskfy-engine"),
        ("GET", "/collections/editor-picks"),
        ("GET", "/watchlist"),
        ("PUT", "/watchlist/alpha-old"),
        ("DELETE", "/watchlist/alpha-old"),
    ],
)
async def test_auth_is_required_for_every_curated_surface(
    method: str,
    path: str,
    curated_api: httpx.AsyncClient,
    catalog_seed: CatalogSeed,
) -> None:
    del catalog_seed
    response = await curated_api.request(method, api_helpers.url(path))
    api_helpers.assert_problem(response, 401, "unauthenticated")


@pytest.mark.asyncio
async def test_non_sole_user_cannot_read_or_mutate_curated_surfaces(
    curated_api: httpx.AsyncClient,
    catalog_seed: CatalogSeed,
    screener_session: AsyncSession,
) -> None:
    _, public_id = await api_helpers.make_user(screener_session, "intruder@example.test")
    headers = api_helpers.bearer(public_id)
    for method, path in (
        ("GET", "/catalog"),
        ("GET", "/watchlist"),
        ("PUT", "/watchlist/alpha-old"),
        ("DELETE", "/watchlist/alpha-old"),
    ):
        response = await curated_api.request(method, api_helpers.url(path), headers=headers)
        api_helpers.assert_problem(response, 404, "not-found")
    rows = (
        await screener_session.execute(
            select(BasketWatchlistItem).where(
                BasketWatchlistItem.basket_id == catalog_seed.baskets["alpha-old"].id
            )
        )
    ).scalars()
    assert rows.all() == []


@pytest.mark.asyncio
async def test_tenant_user_id_query_is_rejected_and_body_cannot_select_owner(
    curated_api: httpx.AsyncClient,
    catalog_seed: CatalogSeed,
    screener_session: AsyncSession,
) -> None:
    invalid = await curated_api.get(
        api_helpers.url("/catalog"),
        params={"user_id": "999"},
        headers=_headers(catalog_seed),
    )
    api_helpers.assert_problem(invalid, 400, "invalid-screen-definition")

    watched = await curated_api.put(
        api_helpers.url("/watchlist/alpha-old"),
        json={"user_id": 999},
        headers=_headers(catalog_seed),
    )
    assert watched.status_code == 200
    row = (
        await screener_session.execute(
            select(BasketWatchlistItem).where(
                BasketWatchlistItem.basket_id == catalog_seed.baskets["alpha-old"].id
            )
        )
    ).scalar_one()
    assert row.user_id == catalog_seed.user.id


@pytest.mark.asyncio
async def test_large_catalog_query_count_is_bounded(
    curated_api: httpx.AsyncClient,
    catalog_seed: CatalogSeed,
    screener_session: AsyncSession,
) -> None:
    additions = [
        _basket(
            BasketSpec(
                slug=f"large-catalog-{index:03d}",
                name=f"Large Catalog {index:03d}",
                manager_id=catalog_seed.engine.id,
                launched_at=dt.date(2020, 1, 1) + dt.timedelta(days=index),
                categories=["scale"],
            )
        )
        for index in range(80)
    ]
    screener_session.add_all(additions)
    await screener_session.flush()
    with mock.patch.object(screener_session, "execute", wraps=screener_session.execute) as execute:
        response = await curated_api.get(
            api_helpers.url("/catalog"), headers=_headers(catalog_seed)
        )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 83
    # One auth lookup plus one set-based catalog statement, independent of basket count.
    assert execute.await_count == 2


@pytest.mark.asyncio
async def test_large_watchlist_query_count_is_bounded_with_daily_change(
    curated_api: httpx.AsyncClient,
    catalog_seed: CatalogSeed,
    screener_session: AsyncSession,
) -> None:
    additions = [
        _basket(
            BasketSpec(
                slug=f"large-watchlist-{index:03d}",
                name=f"Large Watchlist {index:03d}",
                manager_id=catalog_seed.engine.id,
                launched_at=dt.date(2020, 1, 1),
                categories=["scale"],
            )
        )
        for index in range(80)
    ]
    screener_session.add_all(additions)
    await screener_session.flush()
    metrics: list[BasketMetrics] = []
    watched: list[BasketWatchlistItem] = []
    for index, basket in enumerate(additions):
        for as_of, since in (
            (dt.date(2026, 8, 19), "9.0000000000"),
            (dt.date(2026, 8, 20), "10.0000000000"),
        ):
            metrics.append(
                _metrics(
                    basket.id,
                    MetricsSpec(
                        as_of=as_of,
                        minimum="5000.00",
                        volatility="LOW",
                        since=since,
                        one_month="1.0000000000",
                        six_month="2.0000000000",
                        one_year="3.0000000000",
                        three_year="4.0000000000",
                        five_year="5.0000000000",
                    ),
                )
            )
        watched.append(
            BasketWatchlistItem(
                user_id=catalog_seed.user.id,
                basket_id=basket.id,
                nav_at_watch=Decimal("109.0000") + Decimal(index),
            )
        )
    screener_session.add_all([*metrics, *watched])
    await screener_session.flush()
    with mock.patch.object(screener_session, "execute", wraps=screener_session.execute) as execute:
        response = await curated_api.get(
            api_helpers.url("/watchlist"), headers=_headers(catalog_seed)
        )
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 80
    assert len(response.json()["items"]) == 80
    # One auth lookup, one set-based watchlist read, one ranked prior-NAV read.
    assert execute.await_count == 3


@pytest.mark.asyncio
async def test_watchlist_limit_reports_total_and_truncation(
    curated_api: httpx.AsyncClient,
    catalog_seed: CatalogSeed,
    screener_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog_service, "CATALOG_RESPONSE_LIMIT", 2)
    screener_session.add_all(
        [
            BasketWatchlistItem(
                user_id=catalog_seed.user.id,
                basket_id=basket.id,
                nav_at_watch=Decimal("100.0000"),
            )
            for basket in catalog_seed.baskets.values()
        ]
    )
    await screener_session.flush()
    response = await curated_api.get(api_helpers.url("/watchlist"), headers=_headers(catalog_seed))
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 3
    assert response.json()["truncated"] is True
    assert len(response.json()["items"]) == 2


@pytest.mark.asyncio
async def test_watchlist_put_is_allowed_by_cors(
    curated_api: httpx.AsyncClient, catalog_seed: CatalogSeed
) -> None:
    del catalog_seed
    response = await curated_api.options(
        api_helpers.url("/watchlist/alpha-old"),
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert response.status_code == 200
    assert "PUT" in response.headers["access-control-allow-methods"]


def test_forbidden_execution_payment_and_network_imports_are_absent() -> None:
    forbidden_roots = {
        "baskfy_execution",
        "baskfy_api.billing",
        "baskfy_api.razorpay",
        "httpx",
        "requests",
        "urllib",
        "kiteconnect",
    }
    for relative in ("curated_catalog.py", "routers/curated_catalog.py"):
        source = (API_SOURCE / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not {
            name
            for name in imported
            if any(name == root or name.startswith(f"{root}.") for root in forbidden_roots)
        }


def test_catalog_routes_have_no_execution_surface() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        jwt_secret=api_helpers.TEST_JWT_SECRET,
        rate_limit_enabled=False,
    )
    document = __import__("baskfy_api.app", fromlist=["create_app"]).create_app(settings).openapi()
    paths = document["paths"]
    assert set(paths["/api/v1/catalog"]) == {"get"}
    assert set(paths["/api/v1/catalog/{slug}"]) == {"get"}
    assert set(paths["/api/v1/managers/{slug}"]) == {"get"}
    assert set(paths["/api/v1/collections/{slug}"]) == {"get"}
    assert set(paths["/api/v1/watchlist"]) == {"get"}
    assert set(paths["/api/v1/watchlist/{basket_slug}"]) == {"put", "delete"}
    assert not any("order" in path or "execute" in path for path in paths if "catalog" in path)
