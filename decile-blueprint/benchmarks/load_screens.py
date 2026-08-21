"""Concurrent screen runs — Prompt 16's second acceptance criterion.

    "A load test at 50 concurrent screen runs sustains p95 < 400 ms with no error rate."

Two entry points onto one driver:

* :func:`drive` takes an ``httpx.AsyncClient`` and hammers it. ``services/api/tests/test_load.py``
  hands it an ASGI transport, so the criterion is asserted in CI with no server to start.
* ``python -m benchmarks.load_screens --base-url http://localhost:8000`` hands it a real HTTP
  client, so the same number can be taken against a running uvicorn — which is the only way to
  include the socket, the worker count and the reverse proxy.

**They are not the same measurement and the report says which one produced the number.** The
in-process run excludes the network and the ASGI server; it is a floor. What it does include, and
what makes it worth asserting, is everything Prompt 16 is responsible for: the connection pool
under contention, the Redis round trip, the rate limiter, and whether 50 coroutines on one event
loop actually interleave or serialise behind a blocking call.

No Locust, no k6
----------------
The prompt suggests either. docs/02 locks the stack and CLAUDE.md house rule 1 wants a reason
before anything outside it; k6 is a Go binary nobody in this repository has, and Locust brings a
gevent-based runtime and a web UI to do what ``asyncio.gather`` does in thirty lines against a
client the suite already depends on. ``docs/DECISIONS.md`` §16.3.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

import httpx

from decile_core.seed_data import EXAMPLE_SCREENS

#: The one status docs/07 uses for a successful analytics answer; anything else is a refusal.
HTTP_OK: Final = 200

#: Prompt 16 acceptance criterion 2.
CONCURRENCY: Final = 50
P95_BUDGET_MS: Final = 400.0

#: Requests each concurrent worker issues. The criterion says "sustains", so one round of fifty
#: is not enough — that measures a cold start. Ten rounds is 500 requests, which is long enough
#: for the pool to reach steady state and short enough to run inside a test.
REQUESTS_PER_WORKER: Final = 10


@dataclass(slots=True)
class LoadResult:
    """What one load run observed."""

    latencies_ms: list[float] = field(default_factory=list)
    #: Status code -> count, for every response that came back at all.
    statuses: dict[int, int] = field(default_factory=dict)
    #: Exceptions raised by the transport (a connection refused, a timeout).
    transport_errors: list[str] = field(default_factory=list)
    wall_seconds: float = 0.0

    @property
    def total(self) -> int:
        return sum(self.statuses.values()) + len(self.transport_errors)

    @property
    def errors(self) -> int:
        """docs/07 answers a *problem document* for everything it refuses, so anything but 200
        is an error for this purpose — including a 429, which would mean the rate limiter, not
        the service, decided the load test's outcome."""
        return sum(count for status, count in self.statuses.items() if status != HTTP_OK) + len(
            self.transport_errors
        )

    @property
    def error_rate(self) -> float:
        return self.errors / self.total if self.total else 1.0

    def percentile(self, fraction: float) -> float:
        """Nearest-rank, so the number reported is one of the samples."""
        if not self.latencies_ms:
            return float("inf")
        ordered = sorted(self.latencies_ms)
        index = max(0, min(len(ordered) - 1, round(fraction * len(ordered)) - 1))
        return ordered[index]

    @property
    def throughput_per_second(self) -> float:
        return self.total / self.wall_seconds if self.wall_seconds else 0.0

    def summary(self) -> str:
        return (
            f"{self.total} requests, {self.errors} errors ({self.error_rate:.2%}), "
            f"p50 {self.percentile(0.5):.0f} ms, p95 {self.percentile(0.95):.0f} ms, "
            f"p99 {self.percentile(0.99):.0f} ms, "
            f"{self.throughput_per_second:.0f} req/s over {self.wall_seconds:.1f} s"
        )


async def drive(  # noqa: PLR0913 - target, payload and load shape are three separate concerns
    client: httpx.AsyncClient,
    path: str,
    *,
    body: object = None,
    headers: dict[str, str] | None = None,
    concurrency: int = CONCURRENCY,
    requests_per_worker: int = REQUESTS_PER_WORKER,
) -> LoadResult:
    """Fire ``concurrency`` workers, each issuing ``requests_per_worker`` POSTs, and time each.

    Every worker starts at once and runs to completion; the offered load is therefore "as fast as
    fifty clients can go", which is the shape "50 concurrent screen runs" describes. A rate-paced
    generator would measure a different thing (latency at a fixed arrival rate) and would need an
    arrival rate the criterion does not give.
    """
    result = LoadResult()
    lock = asyncio.Lock()

    async def worker() -> None:
        for _ in range(requests_per_worker):
            started = time.perf_counter()
            try:
                response = await client.post(path, json=body, headers=headers)
            except httpx.HTTPError as exc:
                async with lock:
                    result.transport_errors.append(f"{type(exc).__name__}: {exc}")
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000
            async with lock:
                result.latencies_ms.append(elapsed_ms)
                result.statuses[response.status_code] = (
                    result.statuses.get(response.status_code, 0) + 1
                )

    began = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    result.wall_seconds = time.perf_counter() - began
    return result


async def _main(argv: Sequence[str]) -> int:  # pragma: no cover - a developer CLI
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--screen", default=None, help="screen public_id; defaults to the first")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    parser.add_argument("--requests", type=int, default=REQUESTS_PER_WORKER)
    parser.add_argument("--token", default=None, help="a bearer token, for a gated screen")
    args = parser.parse_args(argv)

    screen = args.screen or EXAMPLE_SCREENS[0].public_id
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else None

    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
        result = await drive(
            client,
            f"/api/v1/screens/{screen}/run",
            body={},
            headers=headers,
            concurrency=args.concurrency,
            requests_per_worker=args.requests,
        )

    print(result.summary())
    if result.latencies_ms:
        print(f"  mean {statistics.mean(result.latencies_ms):.0f} ms")
    for status, count in sorted(result.statuses.items()):
        print(f"  {status}: {count}")
    for error in result.transport_errors[:5]:
        print(f"  transport: {error}")

    p95 = result.percentile(0.95)
    ok = p95 < P95_BUDGET_MS and result.errors == 0
    print(
        f"\n{'PASS' if ok else 'FAIL'}: p95 {p95:.0f} ms against a {P95_BUDGET_MS:.0f} ms budget, "
        f"{result.errors} errors"
    )
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - a developer CLI
    import sys

    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
