"""Latency budget for ``POST /screens/{id}/run`` — Prompt 7's last acceptance criterion.

    "p95 of /screens/{id}/run against the seeded dataset is under 150 ms warm (assert in a
     benchmark test, not a unit test)."

docs/11 §"Performance budgets" is the source: "Screen run, warm cache | p95 < 150 ms" and
"Screen run, cold | p95 < 800 ms". Both are measured here.

What "warm" means, precisely
----------------------------
docs/03 §"Request path" step 3: a warm request is one that hits the Redis result cache and returns
without touching ``factor_daily``. So the first request populates the cache and is discarded; the
measured ones are cache hits, which is the shape the budget describes.

The measurement runs in-process over an ASGI transport, so it excludes the network and the
uvicorn worker and includes everything this prompt is responsible for: routing, token
verification, the rate-limit round trip, the entitlement query, the cache round trip and
serialisation. It is a floor, not a production p95 — and a floor that misses is unambiguous.
"""

from __future__ import annotations

import statistics
import time

import httpx
import pytest
from api_helpers import url
from benchmarks.budgets import record
from screener_helpers import requires_db

from decile_core.seed_data import EXAMPLE_SCREENS

pytestmark = [pytest.mark.db, pytest.mark.redis, pytest.mark.benchmark, requires_db]

EXAMPLE_ID = EXAMPLE_SCREENS[0].public_id

#: docs/11 §"Performance budgets".
WARM_BUDGET_MS = 150.0
COLD_BUDGET_MS = 800.0

#: Enough samples for a p95 to mean something without making the suite slow.
SAMPLES = 40


def percentile(values: list[float], fraction: float) -> float:
    """The nearest-rank p95 — no interpolation, so the number is one of the samples."""
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * len(ordered)) - 1))
    return ordered[index]


async def _time_run(api: httpx.AsyncClient) -> tuple[float, str]:
    started = time.perf_counter()
    response = await api.post(url(f"/screens/{EXAMPLE_ID}/run"), json={})
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert response.status_code == 200, response.text[:300]
    assert response.json()["result_count"] == 271
    return elapsed_ms, response.headers.get("X-Decile-Cache", "?")


class TestScreenRunLatency:
    async def test_warm_p95_is_within_budget(self, api: httpx.AsyncClient) -> None:
        _, first = await _time_run(api)
        assert first == "miss", "the first request should have populated the cache"

        samples: list[float] = []
        for _ in range(SAMPLES):
            elapsed, cache = await _time_run(api)
            assert cache == "hit", "a warm sample must be served from the result cache"
            samples.append(elapsed)

        p95 = percentile(samples, 0.95)
        assert p95 < WARM_BUDGET_MS, (
            f"warm p95 {p95:.1f} ms exceeds the {WARM_BUDGET_MS:.0f} ms budget "
            f"(median {statistics.median(samples):.1f} ms, max {max(samples):.1f} ms)"
        )
        # Prompt 16 acceptance criterion 1: the number, not just the verdict.
        record(
            "screen_run_warm",
            p95,
            unit="ms",
            method=f"p95 of {SAMPLES} in-process ASGI requests, all cache hits",
            dataset="the seeded dataset (docs/13's 271-row export)",
        )

    async def test_cold_p95_is_within_budget(self, api: httpx.AsyncClient) -> None:
        """Every sample recomputes: the cache is bypassed by asking for a fresh definition.

        Varying ``ignore_above_beta`` changes the definition hash — and therefore the cache key —
        without changing which rows come back, so each request pays the full query cost.
        """
        samples: list[float] = []
        for index in range(SAMPLES):
            definition = {
                **EXAMPLE_SCREENS[0].definition.model_dump(mode="json", by_alias=True),
                "ignore_above_beta": 90 - index,
            }
            started = time.perf_counter()
            response = await api.post(
                url(f"/screens/{EXAMPLE_ID}/run"), json={"override_definition": definition}
            )
            samples.append((time.perf_counter() - started) * 1000)
            assert response.status_code == 200, response.text[:300]
            assert response.headers["X-Decile-Cache"] == "miss"

        p95 = percentile(samples, 0.95)
        assert p95 < COLD_BUDGET_MS, (
            f"cold p95 {p95:.1f} ms exceeds the {COLD_BUDGET_MS:.0f} ms budget "
            f"(median {statistics.median(samples):.1f} ms)"
        )
        record(
            "screen_run_cold",
            p95,
            unit="ms",
            method=f"p95 of {SAMPLES} in-process ASGI requests, every one a cache miss",
            dataset="the seeded dataset (docs/13's 271-row export)",
        )
