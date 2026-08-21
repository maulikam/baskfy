"""The shared token bucket (Prompt 2 deliverable 2, acceptance criterion 2).

    "Rate limiter test proves >3 req/s is impossible across two concurrent workers."

docs/09 §"Kite specifics": "Rate limit ~ 3 req/s -> a token-bucket limiter shared across workers
(Redis)." Shared is the property under test. A per-process limiter would let two workers make
6 req/s between them and get the API key throttled, and no single-threaded test would notice.

These run against the real Redis `make up` provides, on loopback, which the suite's network block
permits. A hand-written fake would only prove the fake agrees with itself — the atomicity that
makes this correct lives in Redis's execution of the Lua script, not in our Python.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from baskfy_providers.errors import RateLimited
from baskfy_providers.ratelimit import RedisLike, RedisTokenBucket, TokenBucketConfig

pytestmark = pytest.mark.redis

#: docs/09's figure for Kite.
KITE_RATE: float = 3.0


@pytest.fixture
def bucket_key(request: pytest.FixtureRequest) -> str:
    """A key per test, so a leftover bucket cannot make a later test pass or fail spuriously."""
    return f"baskfy:test:ratelimit:{request.node.name}"


def make_bucket(
    client: RedisLike, key: str, *, rate: float = KITE_RATE, max_wait: float = 30.0
) -> RedisTokenBucket:
    return RedisTokenBucket(
        client,
        key,
        TokenBucketConfig(rate_per_second=rate, capacity=rate, max_wait_seconds=max_wait),
    )


class TestSingleWorker:
    def test_a_cold_bucket_starts_full(self, redis_client: RedisLike, bucket_key: str) -> None:
        """No requests have been made, so the full burst is legitimately available."""
        bucket = make_bucket(redis_client, bucket_key)
        for _ in range(int(KITE_RATE)):
            assert bucket.try_acquire() == 0.0

    def test_exhausting_the_bucket_reports_the_wait(
        self, redis_client: RedisLike, bucket_key: str
    ) -> None:
        bucket = make_bucket(redis_client, bucket_key)
        for _ in range(int(KITE_RATE)):
            bucket.try_acquire()
        wait = bucket.try_acquire()
        assert 0 < wait <= 1.0 / KITE_RATE + 0.01

    def test_tokens_refill_at_the_configured_rate(
        self, redis_client: RedisLike, bucket_key: str
    ) -> None:
        """A continuous bucket, not a fixed window: one token accrues every 1/rate seconds."""
        bucket = make_bucket(redis_client, bucket_key)
        start = time.monotonic()
        for _ in range(int(KITE_RATE)):
            bucket.try_acquire(now=start)
        assert bucket.try_acquire(now=start) > 0
        assert bucket.try_acquire(now=start + 1.0 / KITE_RATE + 0.001) == 0.0

    def test_it_gives_up_rather_than_waiting_forever(
        self, redis_client: RedisLike, bucket_key: str
    ) -> None:
        """A pipeline that blocks indefinitely on a limiter never reports why it is late."""
        bucket = make_bucket(redis_client, bucket_key, rate=1.0, max_wait=0.05)
        bucket.acquire()
        with pytest.raises(RateLimited, match="budget"):
            bucket.acquire()


class TestConcurrentWorkers:
    """Acceptance criterion 2, stated directly."""

    def test_two_workers_cannot_exceed_the_rate_between_them(
        self, redis_client: RedisLike, bucket_key: str
    ) -> None:
        """Two workers, 12 requests, 3 req/s. Sharing means it cannot finish faster than ~3s.

        The bucket starts full, so the first 3 are free; the remaining 9 are paced at 3/s, giving
        a floor of 3 seconds. A per-process limiter would let each worker take its own 3-token
        burst and pace independently, finishing in roughly half that — which is the failure this
        catches.
        """
        total_requests = 12
        workers = 2
        bucket_a = make_bucket(redis_client, bucket_key)
        bucket_b = make_bucket(redis_client, bucket_key)

        start = time.monotonic()

        def worker(bucket: RedisTokenBucket) -> int:
            for _ in range(total_requests // workers):
                bucket.acquire()
            return total_requests // workers

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker, bucket_a), pool.submit(worker, bucket_b)]
            results = [f.result() for f in futures]

        elapsed = time.monotonic() - start
        assert sum(results) == total_requests

        # 3 free from the initial burst, the other 9 paced at KITE_RATE per second.
        floor = (total_requests - KITE_RATE) / KITE_RATE
        assert elapsed >= floor * 0.9, (
            f"{total_requests} requests across {workers} workers finished in {elapsed:.2f}s; "
            f"a shared {KITE_RATE:g} req/s limiter cannot go below ~{floor:.2f}s. "
            "The limiter is not actually shared."
        )

    def test_sustained_throughput_never_exceeds_the_configured_rate(
        self, redis_client: RedisLike, bucket_key: str
    ) -> None:
        """Measures the sustained req/s across both workers — the number docs/09 constrains.

        Note what a token bucket does and does not promise. Over any window of length T it admits
        at most ``capacity + rate x T`` requests, so a full bucket really can emit a short burst
        above the nominal rate; that is the definition of the algorithm docs/09 asks for, not a
        defect. What it guarantees, and what "~3 req/s" means, is that the *sustained* rate after
        that initial burst cannot exceed ``rate``. Both bounds are asserted.
        """
        total_requests = 14
        stamps: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            bucket = make_bucket(redis_client, bucket_key)
            for _ in range(total_requests // 2):
                bucket.acquire()
                with lock:
                    stamps.append(time.monotonic())

        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in [pool.submit(worker), pool.submit(worker)]:
                future.result()

        stamps.sort()

        # 1. The token-bucket bound: capacity + rate x T in any window of length T.
        burst_ceiling = KITE_RATE + KITE_RATE * 1.0
        max_in_window = max(sum(1 for t in stamps if start <= t < start + 1.0) for start in stamps)
        assert max_in_window <= burst_ceiling, (
            f"observed {max_in_window} requests in a one-second window; a bucket of capacity "
            f"{KITE_RATE:g} refilling at {KITE_RATE:g}/s admits at most {burst_ceiling:g}"
        )

        # 2. The sustained bound: discount the initial burst and the rate must hold.
        span = stamps[-1] - stamps[0]
        sustained = (len(stamps) - KITE_RATE) / span
        assert sustained <= KITE_RATE * 1.15, (
            f"sustained {sustained:.2f} req/s across two workers against a {KITE_RATE:g} req/s "
            "shared limit"
        )

    def test_the_burst_is_bounded_by_capacity(
        self, redis_client: RedisLike, bucket_key: str
    ) -> None:
        """A full bucket may burst, but only up to capacity — never unboundedly."""
        bucket = make_bucket(redis_client, bucket_key)
        granted = 0
        while bucket.try_acquire() == 0.0:
            granted += 1
            if granted > KITE_RATE * 10:  # pragma: no cover - only on a broken limiter
                pytest.fail("the bucket granted an unbounded burst")
        assert granted == int(KITE_RATE)

    def test_a_separate_key_is_a_separate_budget(
        self, redis_client: RedisLike, bucket_key: str
    ) -> None:
        """Kite and NSE have independent limits; one must not starve the other."""
        kite = make_bucket(redis_client, f"{bucket_key}:kite")
        nse = make_bucket(redis_client, f"{bucket_key}:nse")
        for _ in range(int(KITE_RATE)):
            kite.try_acquire()
        assert kite.try_acquire() > 0
        assert nse.try_acquire() == 0.0


class TestConfiguration:
    def test_a_non_positive_rate_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="rate_per_second"):
            TokenBucketConfig(rate_per_second=0, capacity=3)

    def test_a_non_positive_capacity_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="capacity"):
            TokenBucketConfig(rate_per_second=3, capacity=0)
