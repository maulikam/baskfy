"""M85 — a backfill and a login-time read share Kite's ceiling without starving each other.

WHAT MAULIK REPORTED, 4 Sep 2026
--------------------------------
"I wake at 1pm. I log in, I sync the broker, and I still have the older dataset — the swing
data is lagging, so the price has moved and I cannot buy the stock the scan is showing me."

M84 made a verified token start the day's data work, which is right and which introduced this:
the login publishes a missed-session catch-up (an hour or more of `historical_data`) *and* the
work the person is actually waiting for — the holdings read and today's live swing scan. Every
one of those is a Kite read, and since M85 every Kite read waits on one shared departure clock,
because Kite's published limit is three requests a second **combined**, not per endpoint.

One clock with no lanes means the catch-up reserves the next hour of departures and the
interactive reads either wait behind it or exceed their budget and fail. The product would then
be *more* stale after logging in than before, which is the opposite of the ask.

These tests assert the fix as arithmetic rather than as priority: the bulk lane is held below
the ceiling, so the shared clock is drained faster than bulk can fill it and never runs ahead of
now. `TestTheHeadroomIsReal` is the one that would have caught the bug — it runs both lanes at
once against the real Redis and measures what the interactive caller actually waits.
"""

from __future__ import annotations

import threading
import time
from typing import Final

import pytest

from baskfy_providers.factory import (
    BULK_MAX_WAIT_SECONDS,
    INTERACTIVE_MAX_WAIT_SECONDS,
    KITE_BULK_CLOCK_KEY,
    KITE_READ_CLOCK_KEY,
    KiteLane,
    build_kite_provider,
    build_kite_read_limiter,
    build_provider_stack,
)
from baskfy_providers.kite import KiteProvider
from baskfy_providers.ratelimit import (
    CallSpacingConfig,
    LayeredCallSpacer,
    RateLimiter,
    RedisCallSpacer,
    RedisLike,
)
from baskfy_providers.settings import ProviderSettings

#: A full-universe quote pull is the interactive workload that matters: `KiteProvider.quotes`
#: batches 500 names a call, so ~6,000 liquid names are about a dozen HTTP requests.
QUOTE_PULL_CALLS: Final = 12


class TestTheLanesAreDeclaredOnce:
    """Both lanes end on the ceiling. Only bulk carries a second clock."""

    def test_the_ceiling_key_is_the_one_the_desk_names_too(self) -> None:
        # `kite-momentum-rebalancer/app/core/kite_limits.py` builds `baskfy:ratelimit:kite:read`
        # from its `read` family. The two spellings have to be the same string or the box has
        # two ceilings and neither is 3 req/s.
        assert KITE_READ_CLOCK_KEY == "baskfy:ratelimit:kite:read"
        assert KITE_BULK_CLOCK_KEY == "baskfy:ratelimit:kite:bulk"

    def test_interactive_waits_on_the_ceiling_alone(self, settings: ProviderSettings) -> None:
        limiter = build_kite_read_limiter(settings, KiteLane.INTERACTIVE)
        assert isinstance(limiter, RedisCallSpacer)
        assert limiter.key == KITE_READ_CLOCK_KEY
        assert limiter.config.rate_per_second == settings.kite_rate_limit_per_second
        assert limiter.config.max_wait_seconds == INTERACTIVE_MAX_WAIT_SECONDS

    def test_bulk_takes_its_own_clock_first_and_the_ceiling_last(
        self, settings: ProviderSettings
    ) -> None:
        limiter = build_kite_read_limiter(settings, KiteLane.BULK)
        assert isinstance(limiter, LayeredCallSpacer)
        lane, ceiling = limiter.spacers
        assert lane.key == KITE_BULK_CLOCK_KEY
        assert lane.config.rate_per_second == settings.kite_bulk_rate_limit_per_second
        assert ceiling.key == KITE_READ_CLOCK_KEY
        assert ceiling.config.rate_per_second == settings.kite_rate_limit_per_second
        # The ceiling is what the whole box shares, so that is the key this limiter reports.
        assert limiter.key == KITE_READ_CLOCK_KEY

    def test_a_bulk_caller_is_the_patient_one(self, settings: ProviderSettings) -> None:
        """A backfill has nowhere to be; a person waiting on a login does."""
        assert BULK_MAX_WAIT_SECONDS > INTERACTIVE_MAX_WAIT_SECONDS
        limiter = build_kite_read_limiter(settings, KiteLane.BULK)
        assert isinstance(limiter, LayeredCallSpacer)
        for spacer in limiter.spacers:
            assert spacer.config.max_wait_seconds == BULK_MAX_WAIT_SECONDS

    def test_an_unclassified_call_site_is_interactive(self, settings: ProviderSettings) -> None:
        """The default has to be the lane that cannot starve anybody. Bulk is opt-in."""
        assert isinstance(build_kite_read_limiter(settings), RedisCallSpacer)
        assert build_kite_provider(settings).name == "kite"


class TestWhoIsBulk:
    def test_the_pipeline_stack_is_bulk(self, configured_settings: ProviderSettings) -> None:
        """`build_provider_stack` is what the nightly chain and every backfill run on."""
        stack = build_provider_stack(configured_settings)
        kite = next(p for p in stack.providers if isinstance(p, KiteProvider))
        assert isinstance(kite.rate_limiter, LayeredCallSpacer)
        assert kite.rate_limiter.spacers[0].key == KITE_BULK_CLOCK_KEY

    def test_a_directly_built_provider_is_interactive(
        self, configured_settings: ProviderSettings
    ) -> None:
        """The swing scan, the premarket quotes and the API's holdings read all take this path."""
        kite = build_kite_provider(configured_settings)
        assert isinstance(kite.rate_limiter, RedisCallSpacer)
        assert kite.rate_limiter.key == KITE_READ_CLOCK_KEY


class TestTheSettingRefusesToBeUseless:
    def test_a_bulk_lane_at_the_ceiling_is_refused(self) -> None:
        with pytest.raises(ValueError, match="strictly below"):
            ProviderSettings(
                _env_file=None,
                kite_rate_limit_per_second=3.0,
                kite_bulk_rate_limit_per_second=3.0,
            )

    def test_a_bulk_lane_above_the_ceiling_is_refused(self) -> None:
        with pytest.raises(ValueError, match="headroom"):
            ProviderSettings(
                _env_file=None,
                kite_rate_limit_per_second=3.0,
                kite_bulk_rate_limit_per_second=4.0,
            )

    def test_the_shipped_default_leaves_a_whole_request_a_second(self) -> None:
        settings = ProviderSettings(_env_file=None)
        headroom = settings.kite_rate_limit_per_second - settings.kite_bulk_rate_limit_per_second
        assert headroom >= 1.0, "less than one free request a second is not a lane"


class TestLayeredCallSpacer:
    """The composition itself, without a network."""

    def test_it_takes_every_clock_in_order_and_sums_the_waits(self) -> None:
        taken: list[str] = []

        class FakeSpacer:
            def __init__(self, name: str, wait: float) -> None:
                self.name, self.wait = name, wait

            @property
            def key(self) -> str:
                return self.name

            @property
            def config(self) -> CallSpacingConfig:
                return CallSpacingConfig(rate_per_second=1.0)

            def acquire(self, tokens: float = 1.0) -> float:
                taken.append(self.name)
                return self.wait

        layered = LayeredCallSpacer((FakeSpacer("lane", 0.5), FakeSpacer("ceiling", 0.25)))
        assert layered.acquire() == pytest.approx(0.75)
        assert taken == ["lane", "ceiling"], "the ceiling must be reserved at the lane's pace"
        assert layered.key == "ceiling"

    def test_an_empty_layering_is_a_limiter_that_limits_nothing(self) -> None:
        with pytest.raises(ValueError, match="at least one clock"):
            LayeredCallSpacer(())


@pytest.mark.redis
class TestTheHeadroomIsReal:
    """Two lanes, one Redis, one wall clock — the test that would have caught the 1pm bug."""

    @staticmethod
    def _spacer(client: RedisLike, key: str, rate: float) -> RedisCallSpacer:
        return RedisCallSpacer(
            client, key, CallSpacingConfig(rate_per_second=rate, max_wait_seconds=60.0)
        )

    def test_a_login_read_gets_a_slot_while_a_backfill_is_running(
        self, redis_client: RedisLike, request: pytest.FixtureRequest
    ) -> None:
        # Per-test keys, and the script's own PX expiry retires them: a reservation outlives
        # itself by about a second, so nothing here can be decided by a previous run.
        ceiling_key = f"baskfy:test:kite:read:{request.node.name}"
        lane_key = f"baskfy:test:kite:bulk:{request.node.name}"

        ceiling_rate, bulk_rate = 3.0, 2.0
        bulk = LayeredCallSpacer(
            (
                self._spacer(redis_client, lane_key, bulk_rate),
                self._spacer(redis_client, ceiling_key, ceiling_rate),
            )
        )
        interactive = self._spacer(redis_client, ceiling_key, ceiling_rate)

        stop = threading.Event()
        bulk_calls = 0

        def backfill() -> None:
            nonlocal bulk_calls
            while not stop.is_set():
                bulk.acquire()
                bulk_calls += 1

        thread = threading.Thread(target=backfill, daemon=True)
        thread.start()
        try:
            time.sleep(1.0)  # let the backfill get a head start, as it has at 1pm
            waits = [interactive.acquire() for _ in range(QUOTE_PULL_CALLS)]
        finally:
            stop.set()
            thread.join(timeout=5.0)

        # THE ASSERTION THAT MATTERS. Without a bulk lane the interactive caller queues behind
        # every departure the backfill has reserved. With one, its longest single wait is about
        # one ceiling interval, because the clock is being drained faster than bulk fills it.
        assert max(waits) < 3.0 / ceiling_rate, (
            f"a login-time read waited {max(waits):.2f}s behind the backfill; the lane is "
            "not leaving headroom"
        )
        assert bulk_calls > 0, "the backfill made no progress at all — this proves nothing"

    def test_the_ceiling_still_holds_across_both_lanes(
        self, redis_client: RedisLike, request: pytest.FixtureRequest
    ) -> None:
        """Headroom must not become a way to exceed 3 req/s — that is how an API key is lost."""
        ceiling_key = f"baskfy:test:kite:read:{request.node.name}"
        lane_key = f"baskfy:test:kite:bulk:{request.node.name}"

        ceiling_rate = 3.0
        bulk = LayeredCallSpacer(
            (
                self._spacer(redis_client, lane_key, 2.0),
                self._spacer(redis_client, ceiling_key, ceiling_rate),
            )
        )
        interactive = self._spacer(redis_client, ceiling_key, ceiling_rate)

        calls = 18
        started = time.monotonic()
        with_both: list[RateLimiter] = [bulk, interactive]
        for index in range(calls):
            with_both[index % 2].acquire()
        elapsed = time.monotonic() - started

        # `calls - 1` departures have to fit after the first, which is free.
        floor = (calls - 1) / ceiling_rate
        assert elapsed >= floor * 0.95, (
            f"{calls} reads finished in {elapsed:.2f}s, under the {floor:.2f}s that "
            f"{ceiling_rate:g} req/s allows"
        )
