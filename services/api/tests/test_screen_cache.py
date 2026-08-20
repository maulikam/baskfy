"""The Redis result cache and its warm-up — docs/06 §Caching (Prompt 6 deliverable 5).

Against a real Redis, on loopback, which the suite's network block permits. A fake would prove
only that the fake agrees with itself; what is worth asserting here is that the key is byte-for-
byte the one docs/06 specifies, that a ``data_version`` bump makes every existing key unreachable,
and that publish leaves the namespace purged and re-warmed.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from screener_helpers import (
    AS_OF,
    DATA_VERSION,
    make_row,
    publish_run,
    requires_db,
    ttl_of,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api.screener import (
    SCREEN_CACHE_FALLBACK_TTL_SECONDS,
    WARM_CACHE_SCREEN_LIMIT,
    current_data_version,
    purge_screen_cache,
    run_screen,
    top_screen_definitions,
    warm_screen_cache,
)
from decile_core.models import PipelineRun, Screen, ScreenRun
from decile_core.screen_definition import ScreenDefinition
from decile_core.screener import cache_key
from decile_core.seed_data import EXAMPLE_SCREENS
from decile_core.universes import UNIVERSE_BY_SLUG
from decile_worker.steps import StepOutcome
from decile_worker.tasks.publish import SCREEN_CACHE_PREFIX, run_publish

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

SYNTHETIC = dt.date(2026, 8, 17)
NIFTY_500 = UNIVERSE_BY_SLUG["nifty-500"].index_id
INVESTING_001 = next(s for s in EXAMPLE_SCREENS if s.name == "Investing 001").definition


def defn(**overrides: object) -> ScreenDefinition:
    payload: dict[str, object] = {
        "index": "nifty-500",
        "sort_by": "ret_12m",
        "historical_date": SYNTHETIC.isoformat(),
    }
    payload.update(overrides)
    return ScreenDefinition.model_validate(payload)


async def seed_two_rows(session: AsyncSession) -> None:
    for i in range(2):
        await make_row(
            session,
            f"CACHE{i}",
            NIFTY_500,
            on=SYNTHETIC,
            values={
                "close": Decimal(100 + i),
                "close_raw": Decimal(100 + i),
                "ret_12m": Decimal(10 + i),
                "marketcap_cr": 1000 * (i + 1),
            },
        )


class TestTheKey:
    """docs/06 §Caching:

    ``screen:{sha256(definition_canonical_json)}:{as_of}:{data_version}``
    """

    async def test_a_miss_computes_and_stores_under_the_documented_key(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        await seed_two_rows(screener_session)
        definition = defn()
        outcome = await run_screen(screener_session, definition, cache=screen_cache)
        assert outcome.cache_hit is False

        key = cache_key(definition, SYNTHETIC, DATA_VERSION)
        assert key.startswith(SCREEN_CACHE_PREFIX)
        assert await screen_cache.get(key) == outcome.payload

    async def test_a_hit_returns_the_cached_bytes_verbatim(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        """Proven by planting a sentinel: if the value came from the database it would not match."""
        definition = defn()
        key = cache_key(definition, SYNTHETIC, DATA_VERSION)
        await screen_cache.set(key, '{"sentinel":true}')

        outcome = await run_screen(screener_session, definition, cache=screen_cache)
        assert outcome.cache_hit is True
        assert outcome.payload == '{"sentinel":true}'
        assert outcome.result is None

    async def test_the_cached_payload_is_byte_identical_to_a_fresh_run(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        await seed_two_rows(screener_session)
        definition = defn()
        cold = await run_screen(screener_session, definition, cache=screen_cache)
        warm = await run_screen(screener_session, definition, cache=screen_cache)
        assert warm.cache_hit is True
        assert warm.payload == cold.payload

    async def test_two_definitions_never_share_a_key(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        await seed_two_rows(screener_session)
        first = await run_screen(screener_session, defn(), cache=screen_cache)
        second = await run_screen(screener_session, defn(sort_by="close"), cache=screen_cache)
        assert second.cache_hit is False
        assert first.payload != second.payload

    async def test_a_cached_entry_carries_the_fallback_expiry(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        """Not the contract — the safety net for a publish that never comes."""
        await seed_two_rows(screener_session)
        await run_screen(screener_session, defn(), cache=screen_cache)
        ttl = await ttl_of(screen_cache, cache_key(defn(), SYNTHETIC, DATA_VERSION))
        assert 0 < ttl <= SCREEN_CACHE_FALLBACK_TTL_SECONDS


class TestInvalidation:
    """docs/06 §Caching: "TTL: until the next `data_version` bump"."""

    async def test_a_version_bump_makes_every_existing_key_unreachable(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        await seed_two_rows(screener_session)
        definition = defn()
        await run_screen(screener_session, definition, cache=screen_cache)

        await publish_run(screener_session, AS_OF, DATA_VERSION + 1)
        assert await current_data_version(screener_session) == DATA_VERSION + 1

        outcome = await run_screen(screener_session, definition, cache=screen_cache)
        assert outcome.cache_hit is False
        assert f'"data_version":{DATA_VERSION + 1}' in outcome.payload

    async def test_purging_empties_the_whole_namespace(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        await seed_two_rows(screener_session)
        await run_screen(screener_session, defn(), cache=screen_cache)
        await run_screen(screener_session, defn(sort_by="close"), cache=screen_cache)
        await screen_cache.set("unrelated:key", "keep me")

        assert await purge_screen_cache(screen_cache) == 2
        assert [key async for key in screen_cache.scan_iter(match="screen:*")] == []
        assert await screen_cache.get("unrelated:key") == "keep me"
        await screen_cache.delete("unrelated:key")

    async def test_purging_a_client_that_cannot_scan_is_not_fatal(self) -> None:
        assert await purge_screen_cache(object()) == 0


class TestWarming:
    """docs/06 §Caching: "Warm the top 200 most-run screen definitions after each publish"."""

    async def test_the_limit_is_the_documented_two_hundred(self) -> None:
        assert WARM_CACHE_SCREEN_LIMIT == 200

    async def test_screens_are_ranked_by_how_often_they_have_run(
        self, screener_session: AsyncSession
    ) -> None:
        screens = (
            (await screener_session.execute(select(Screen).order_by(Screen.id))).scalars().all()
        )
        assert len(screens) >= 3
        popular, occasional = screens[2], screens[1]
        for _ in range(5):
            screener_session.add(
                ScreenRun(
                    screen_id=popular.id,
                    as_of=AS_OF,
                    definition_hash=f"h{_}",
                    result_count=1,
                    results=[],
                )
            )
        screener_session.add(
            ScreenRun(
                screen_id=occasional.id,
                as_of=AS_OF,
                definition_hash="h0",
                result_count=1,
                results=[],
            )
        )
        await screener_session.flush()

        plan = await top_screen_definitions(screener_session)
        assert plan.targets[0].public_id == popular.public_id
        assert plan.targets[0].run_count == 5
        assert plan.targets[1].public_id == occasional.public_id
        assert plan.targets[1].run_count == 1

    async def test_a_never_run_deployment_still_warms_the_example_screens(
        self, screener_session: AsyncSession
    ) -> None:
        """``screen_run`` is empty until Prompt 7 wires it; warming nothing would be worse."""
        plan = await top_screen_definitions(screener_session)
        assert len(plan.targets) == len(EXAMPLE_SCREENS)
        assert all(target.run_count == 0 for target in plan.targets)

    async def test_warming_fills_the_cache_and_the_next_run_hits_it(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        outcome = await warm_screen_cache(screener_session, screen_cache)
        assert outcome.warmed == len(EXAMPLE_SCREENS)
        assert outcome.failures == ()

        hit = await run_screen(screener_session, INVESTING_001, columns=[], cache=screen_cache)
        assert hit.cache_hit is True
        assert '"result_count":271' in hit.payload

    async def test_the_limit_is_honoured(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        outcome = await warm_screen_cache(screener_session, screen_cache, limit=2)
        assert outcome.warmed == 2

    async def test_one_unrunnable_screen_does_not_stop_the_others(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        """A definition persisted before the registry gate existed must not break the warm-up."""
        screener_session.add(
            Screen(
                public_id="brokenxxxxxx",
                user_id=None,
                name="Broken",
                definition={"index": "nifty-500", "sort_by": "not_a_factor"},
                columns=[],
                is_example=False,
            )
        )
        await screener_session.flush()

        outcome = await warm_screen_cache(screener_session, screen_cache)
        assert outcome.warmed == len(EXAMPLE_SCREENS)
        assert len(outcome.failures) == 1
        assert "brokenxxxxxx" in outcome.failures[0]


class TestPublishIntegration:
    """docs/03 step 10: "bump data_version, purge Redis screen cache, warm top screens"."""

    async def test_publish_purges_then_warms_under_the_new_version(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        await seed_two_rows(screener_session)
        stale = await run_screen(screener_session, defn(), cache=screen_cache)
        assert stale.cache_hit is False

        run = PipelineRun(
            trade_date=AS_OF,
            status="running",
            started_at=dt.datetime(2026, 8, 18, 14, 0, tzinfo=dt.UTC),
        )
        screener_session.add(run)
        await screener_session.flush()

        outcome = StepOutcome()
        result = await run_publish(screener_session, outcome, run, screen_cache)

        assert result.data_version == DATA_VERSION + 1
        assert result.cache_keys_purged == 1
        assert result.screens_warmed == len(EXAMPLE_SCREENS)

        # The stale key is gone, and the example screens are cached under the new version.
        assert await screen_cache.get(cache_key(defn(), SYNTHETIC, DATA_VERSION)) is None
        warmed_key = cache_key(INVESTING_001, AS_OF, DATA_VERSION + 1)
        assert await screen_cache.get(warmed_key) is not None

    async def test_publish_without_a_cache_still_publishes(
        self, screener_session: AsyncSession
    ) -> None:
        run = PipelineRun(
            trade_date=AS_OF,
            status="running",
            started_at=dt.datetime(2026, 8, 18, 14, 0, tzinfo=dt.UTC),
        )
        screener_session.add(run)
        await screener_session.flush()
        outcome = StepOutcome()
        result = await run_publish(screener_session, outcome, run, None)
        assert result.data_version == DATA_VERSION + 1
        assert result.screens_warmed == 0
