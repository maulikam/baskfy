"""The rest of the docs/11 performance budgets — Prompt 16 deliverable 1.

    "A benchmark suite ... covering: screen run cold/warm, dashboard, factsheet, CSV export, and
     the nightly factor computation."

The screen run is measured in ``test_api_benchmark.py`` (Prompt 7 built it) and the nightly factor
computation in ``services/worker/tests/test_factor_benchmark.py``. This module covers the three
that were unmeasured: the dashboard, the factsheet's TTFB, and the 4,000-row CSV export.

Not pytest-benchmark
--------------------
The prompt suggests "pytest-benchmark + k6 or Locust". docs/02 §"The decision in one table" locks
"pytest + hypothesis" for Python tests and CLAUDE.md house rule 1 requires a reason before any
dependency outside that table. Prompt 7 already built a hand-rolled p95 harness for the same kind
of budget (``test_api_benchmark.percentile``), pytest-benchmark's calibration machinery is aimed
at microbenchmarks rather than at a request that talks to PostgreSQL and Redis, and its statistics
are not the thing being asserted — a docs/11 budget is a threshold, not a distribution. The load
driver that k6 or Locust would have supplied is ``benchmarks/load_screens.py``, which is 150 lines
of asyncio and needs no runtime the repository does not already have.
``docs/DECISIONS.md`` §16.3.

Sizes
-----
The dashboard and the factsheet are measured on the seeded dataset, which is what they run on: the
dashboard fixture carries 117 indices (``docs/11a`` §1) against docs/11's "145", and the factsheet
reads one instrument's row whatever the table size. The CSV export cannot be: docs/11 budgets
**4,000 rows** and the reference export has 271, so this module builds a 4,200-instrument universe
and exports all of it. Every measurement records what it was measured against.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import AsyncIterator

import httpx
import pytest
from api_helpers import api_settings, bearer, make_user, running_app, url
from benchmarks.budgets import BUDGET_BY_KEY, BUDGETS, record
from screener_helpers import requires_db, seeded_database, synthetic_universe_sql
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from baskfy_core.screener import MAX_RESULT_ROWS
from baskfy_core.seed_data import EXAMPLE_SCREENS
from baskfy_core.universes import UNIVERSE_BY_SLUG

pytestmark = [pytest.mark.db, pytest.mark.redis, pytest.mark.benchmark, requires_db]

#: docs/11 §"Performance budgets".
DASHBOARD_BUDGET_MS = 500.0
FACTSHEET_TTFB_BUDGET_MS = 300.0
CSV_EXPORT_BUDGET_SECONDS = 2.0

#: Enough samples for a median to be stable without making `make bench` slow.
SAMPLES = 20

#: docs/11 budgets the export at 4,000 rows; ``MAX_RESULT_ROWS`` is the ceiling a screen returns.
#: A few hundred spare so the *filters* rather than the universe size decide the row count.
EXPORT_INSTRUMENTS = MAX_RESULT_ROWS + 200

#: One date is all an export reads.
EXPORT_DAYS = 2

SEEDED = "the seeded dataset (docs/13's 271-row export, 117 indices)"


def median_ms(samples: list[float]) -> float:
    return statistics.median(samples)


async def timed_get(client: httpx.AsyncClient, path: str) -> tuple[float, httpx.Response]:
    """One GET, and how long it took in milliseconds."""
    started = time.perf_counter()
    response = await client.get(url(path))
    return (time.perf_counter() - started) * 1000, response


class TestDashboard:
    """docs/11: "Dashboard (145 indices) | < 500 ms"."""

    async def test_the_dashboard_is_within_budget(self, api: httpx.AsyncClient) -> None:
        first = await api.get(url("/indices/dashboard"))
        assert first.status_code == 200, first.text[:300]
        rows = first.json()["data"]
        assert rows, "the dashboard must have rows for the measurement to mean anything"

        samples: list[float] = []
        for _ in range(SAMPLES):
            elapsed, response = await timed_get(api, "/indices/dashboard")
            assert response.status_code == 200
            samples.append(elapsed)
        median = median_ms(samples)
        assert median < DASHBOARD_BUDGET_MS, (
            f"dashboard median {median:.1f} ms exceeds the {DASHBOARD_BUDGET_MS:.0f} ms budget "
            f"(max {max(samples):.1f} ms over {len(rows)} indices)"
        )
        record(
            "dashboard",
            median,
            unit="ms",
            method=f"median of {SAMPLES} in-process ASGI requests over {len(rows)} indices",
            dataset=SEEDED,
        )


class TestFactsheetTtfb:
    """docs/11: "Instrument factsheet (RSC) | TTFB < 300 ms".

    The API response *is* the RSC's time to first byte, near enough: `docs/03` §"Request path"
    has the server component call the API and render from the answer, so the API call is the
    floor and everything Next adds sits on top of it. The LCP half of the budget is a browser
    measurement and belongs to `apps/web/e2e/lighthouse.spec.ts`.
    """

    async def test_the_factsheet_is_within_budget(self, api: httpx.AsyncClient) -> None:
        symbol = "CUPID"
        first = await api.get(url(f"/instruments/{symbol}"))
        assert first.status_code == 200, first.text[:300]

        samples: list[float] = []
        for _ in range(SAMPLES):
            elapsed, response = await timed_get(api, f"/instruments/{symbol}")
            assert response.status_code == 200
            samples.append(elapsed)
        median = median_ms(samples)
        assert median < FACTSHEET_TTFB_BUDGET_MS, (
            f"factsheet median {median:.1f} ms exceeds the "
            f"{FACTSHEET_TTFB_BUDGET_MS:.0f} ms budget (max {max(samples):.1f} ms)"
        )
        record(
            "factsheet_ttfb",
            median,
            unit="ms",
            method=f"median of {SAMPLES} in-process ASGI requests for GET /instruments/{symbol}",
            dataset=SEEDED,
        )


@pytest.fixture(scope="module")
def export_url() -> str:
    """A seeded database with more than 4,000 instruments in the total-market universe.

    docs/11 budgets the CSV export at 4,000 rows and docs/13's export has 271. Measuring 271 rows
    and multiplying by fifteen would be arithmetic, not a benchmark: the export streams, so its
    cost is dominated by per-row formatting that only shows up at volume, and by whether the
    result set fits in one round trip.
    """
    url_ = seeded_database()
    total_market = UNIVERSE_BY_SLUG["nifty-total-market"].index_id

    async def build() -> None:
        engine = create_async_engine(url_)
        try:
            async with engine.begin() as connection:
                for statement in synthetic_universe_sql(
                    total_market, EXPORT_INSTRUMENTS, EXPORT_DAYS
                ):
                    await connection.execute(text(statement))
                await connection.execute(text("ANALYZE factor_daily"))
                await connection.execute(text("ANALYZE index_member_daily"))
                await connection.execute(text("ANALYZE instrument"))
        finally:
            await engine.dispose()

    asyncio.run(build())
    return url_


@pytest.fixture
async def export_session(export_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(export_url)
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
async def export_api(
    export_url: str, export_session: AsyncSession, clean_redis_namespaces: None
) -> AsyncIterator[httpx.AsyncClient]:
    del clean_redis_namespaces
    async with running_app(api_settings(export_url), export_session) as client:
        yield client


class TestCsvExport:
    """docs/11: "CSV export (4,000 rows) | < 2 s"."""

    async def test_a_four_thousand_row_export_is_within_budget(
        self, export_api: httpx.AsyncClient, export_session: AsyncSession
    ) -> None:
        _, public_id = await make_user(export_session, "bench-export@example.com", subscribed=True)
        headers = bearer(public_id)
        screen = EXAMPLE_SCREENS[0].public_id

        started = time.perf_counter()
        response = await export_api.get(url(f"/screens/{screen}/csv"), headers=headers)
        elapsed = time.perf_counter() - started
        assert response.status_code == 200, response.text[:300]

        # The header line plus one line per row; the file ends with a newline (`docs/09a` §1).
        lines = response.text.count("\n") - 1
        assert lines >= 4000, (
            f"only {lines} rows exported; docs/11's budget is stated for 4,000 and the "
            "measurement is meaningless below it"
        )
        assert elapsed < CSV_EXPORT_BUDGET_SECONDS, (
            f"{lines}-row export took {elapsed:.2f} s, over the "
            f"{CSV_EXPORT_BUDGET_SECONDS:.0f} s budget"
        )
        record(
            "csv_export",
            elapsed,
            unit="s",
            method=f"one streamed GET /screens/{{id}}/csv returning {lines} rows",
            dataset=(
                f"the seeded dataset plus {EXPORT_INSTRUMENTS} synthetic instruments over "
                f"{EXPORT_DAYS} trading days"
            ),
        )

    async def test_the_export_is_the_reference_column_set(
        self, export_api: httpx.AsyncClient, export_session: AsyncSession
    ) -> None:
        """A fast export of the wrong columns is not a passing budget.

        docs/13 §5 step 6 makes the 93-column header the acceptance test; ``test_csv_export.py``
        asserts it against the committed fixture. This only checks that the benchmark measured
        that same shape rather than a truncated one.
        """
        _, public_id = await make_user(export_session, "bench-shape@example.com", subscribed=True)
        response = await export_api.get(
            url(f"/screens/{EXAMPLE_SCREENS[0].public_id}/csv"), headers=bearer(public_id)
        )
        assert response.status_code == 200
        header = response.text.split("\n", maxsplit=1)[0]
        assert header.count(",") + 1 == 93, header[:200]


class TestTheBudgetTableIsHonest:
    """docs/11's numbers and the harness's copy of them must not drift apart."""

    def test_the_recorded_budgets_match_the_constants_asserted_here(self) -> None:
        assert BUDGET_BY_KEY["dashboard"].limit == DASHBOARD_BUDGET_MS
        assert BUDGET_BY_KEY["factsheet_ttfb"].limit == FACTSHEET_TTFB_BUDGET_MS
        assert BUDGET_BY_KEY["csv_export"].limit == CSV_EXPORT_BUDGET_SECONDS

    def test_every_budget_names_a_harness(self) -> None:
        for budget in BUDGETS:
            assert budget.measured_by, f"{budget.key} does not say what measures it"
