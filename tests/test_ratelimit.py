"""The per-second order cap, which failed in production on 18 Aug 2026.

A 21-order rebalance sent all 21 through the limiter: ten were accepted and eleven came
back "Maximum allowed order requests per second exceeded". The book was left fully sold
down with only three of fourteen buys placed. These tests measure the burst rather than
trusting the design.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.core.ratelimit import Bucket, KiteLimits, Spacer


def grant_times(n: int, take) -> list[float]:
    async def run():
        t0 = time.monotonic()
        out = []
        for _ in range(n):
            await take()
            out.append(time.monotonic() - t0)
        return out
    return asyncio.run(run())


def max_in_any_window(times: list[float], window: float = 1.0) -> int:
    """The most grants landing inside any sliding window."""
    return max(sum(1 for t in times if s <= t < s + window) for s in times)


# =====================================================================================
# the regression
# =====================================================================================
def test_twenty_one_orders_never_exceed_nine_in_any_second():
    """The exact shape of the failure: a 21-leg rebalance fired as fast as the code allows."""
    lim = KiteLimits()
    times = grant_times(21, lim.orders_sec.take)
    assert max_in_any_window(times) <= 9, f"burst of {max_in_any_window(times)}"


def test_a_full_token_bucket_would_have_allowed_the_burst():
    """Why the old design failed, kept as an executable explanation rather than a comment.
    A bucket of capacity 9 refilling 9/s starts FULL, so it grants nine instantly and nine
    more as the second elapses."""
    times = grant_times(18, Bucket(9, 1.0).take)
    assert max_in_any_window(times) > 9


def test_the_spacer_holds_its_interval():
    times = grant_times(10, Spacer(9).take)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 1.0 / 9 * 0.95, f"gaps {gaps}"


def test_concurrent_callers_cannot_both_take_the_same_slot():
    """Without the lock, two coroutines observe the same free slot and reproduce exactly
    the burst the class exists to prevent."""
    async def run():
        sp = Spacer(9)
        t0 = time.monotonic()
        await asyncio.gather(*(sp.take() for _ in range(9)))
        return time.monotonic() - t0
    elapsed = asyncio.run(run())
    assert elapsed >= 8 / 9 * 0.95, f"nine concurrent grants took only {elapsed:.3f}s"


def test_the_order_path_is_spaced_not_bucketed():
    lim = KiteLimits()
    assert isinstance(lim.orders_sec, Spacer)
    assert isinstance(lim.api, Spacer)
    # The long windows keep buckets: a burst inside 400/minute is harmless.
    assert isinstance(lim.orders_min, Bucket)
    assert isinstance(lim.orders_day, Bucket)


def test_the_full_order_slot_still_paces():
    lim = KiteLimits()
    times = grant_times(12, lim.order_slot)
    assert max_in_any_window(times) <= 9


def test_the_limiter_stays_under_the_sebi_threshold():
    """Under 10 orders/sec also keeps this below SEBI's retail-algo registration
    threshold, so the cap is regulatory as well as operational."""
    lim = KiteLimits()
    assert max_in_any_window(grant_times(15, lim.orders_sec.take)) < 10
