"""The options monitor's clock: one loop from start to the close (OP9).

`run_session` is `app.swing_monitor.run_until_close`'s shape without the swing chores: drain the
TickBus into the strategy, fall back to a throttled quote when the feed is quiet (the B10 pattern,
≤ 1 call per `min_interval` seconds), and — the one thing swing never needed — call
`strategy.check(now)` on **every** pass, ticks or not. A position whose feed has gone silent is
exactly the one `04` §8.5's `FEED_LOST` exists for, and nothing would ever evaluate it if the loop
only reacted to ticks.

Tokens are followed as they appear: after `OPEN`, the executor's new legs arrive through the
strategy's refresh, and `follow(tokens)` subscribes them on the live ticker (`kws.subscribe` +
full mode) and on the bus. `now` and `sleep` are seams, so a test can end the loop by handing it a
time past the close — the swing suite learnt that a loop with no such seam hangs the whole run.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

log = logging.getLogger("desk.options_clock")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
#: No tick from anything for this long → ask the quote endpoint (B10).
QUIET_SECONDS = 5.0


class Monitor(Protocol):
    """What the loop drives — `NiftyOptionsMonitor`, or a stand-in in tests."""

    tokens: Any
    exits: list[Any]

    async def on_start(self) -> None: ...

    async def on_stop(self) -> None: ...

    async def on_tick(self, tick: dict) -> None: ...

    def check(self, now: dt.datetime) -> Any: ...

    def session_over(self, at: dt.datetime) -> bool: ...

    def token_lost(self, now: dt.datetime) -> Any: ...


def is_token_error(exc: BaseException) -> bool:
    """Kite refused the access token (expired or revoked), not a network hiccup."""
    try:
        from kiteconnect.exceptions import TokenException  # noqa: PLC0415 - desk venv only
    except ImportError:  # pragma: no cover - kiteconnect is a desk dependency
        return type(exc).__name__ == "TokenException"
    return isinstance(exc, TokenException) or type(exc).__name__ == "TokenException"


class Bus(Protocol):
    def subscribe(self, token: int) -> asyncio.Queue: ...


class LegQuotes:
    """B10 for options: when the ticker is quiet, one `quote` call for every followed token.

    Kite's quote endpoint takes instrument tokens as keys, so the monitor needs no trading symbol
    to mark a leg. The cap lives on the object — `poll` returns ``None`` inside the interval — so a
    loop that asks every second still makes at most one call per `min_interval` seconds.
    """

    def __init__(
        self,
        kite: Any,
        *,
        min_interval: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.kite = kite
        self.min_interval = min_interval
        self.clock = clock
        self._last: float | None = None
        self.calls = 0
        self.failures = 0
        #: The last poll was refused for the token (OP14): the runner raises every exit at once.
        self.token_dead = False

    def poll(self, tokens: Iterable[int], now: dt.datetime) -> list[dict] | None:
        moment = self.clock()
        if self._last is not None and moment - self._last < self.min_interval:
            return None
        self._last = moment
        keys = [str(t) for t in tokens]
        if not keys:
            return []
        self.calls += 1
        try:
            payload = self.kite.quote_raw(keys)
        except Exception as exc:  # a failed poll is a stale mark, and the next poll tries again
            self.failures += 1
            if is_token_error(exc):
                self.token_dead = True
                log.error("options monitor: Kite token rejected; raising every exit (runbook 12)")
            else:
                log.exception("options quote fallback failed")
            return []
        self.token_dead = False
        return [_tick_of(key, quote, now) for key, quote in payload.items() if isinstance(quote, dict)]


def _tick_of(key: str, quote: dict, now: dt.datetime) -> dict:
    token = quote.get("instrument_token")
    if not isinstance(token, int):
        token = int(key) if key.isdigit() else None
    stamp = quote.get("timestamp") or quote.get("last_trade_time") or now
    return {
        "instrument_token": token,
        "last_price": quote.get("last_price"),
        "depth": quote.get("depth") or {},
        "exchange_timestamp": stamp,
    }


def ist_now() -> dt.datetime:
    return dt.datetime.now(tz=IST).replace(tzinfo=None)


async def run_session(  # noqa: PLR0913 - the strategy, the bus and every seam a test needs
    strategy: Monitor,
    bus: Bus,
    *,
    follow: Callable[[list[int]], None] | None = None,
    quotes: LegQuotes | None = None,
    act: Callable[[dt.datetime], Awaitable[Any]] | None = None,
    now: Callable[[], dt.datetime] = ist_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    poll_seconds: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Drive `strategy` until `session_over(now())`. Returns the number of exits it raised.

    `act(now)` runs after every evaluation pass — the runner hands it the executor's sweep of
    pending exit plans (OP10), so a raised exit is closed within a second of being raised."""
    queues: dict[int, asyncio.Queue] = {}

    def sync_tokens() -> None:
        new = [int(t) for t in strategy.tokens if int(t) not in queues]
        for token in new:
            queues[token] = bus.subscribe(token)
        if new and follow is not None:
            try:
                follow(new)
            except Exception:  # the bus still gets the quote fallback for these tokens
                log.exception("options monitor: could not subscribe %s on the ticker", new)

    await strategy.on_start()
    sync_tokens()
    last_tick = clock()
    try:
        while not strategy.session_over(now()):
            drained = 0
            for queue in list(queues.values()):
                while True:
                    try:
                        tick = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await strategy.on_tick(tick)
                    drained += 1
            if drained:
                last_tick = clock()
            elif quotes is not None and clock() - last_tick >= QUIET_SECONDS:
                for tick in quotes.poll(list(queues), now()) or []:
                    await strategy.on_tick(tick)
                if quotes.token_dead:
                    strategy.token_lost(now())
            strategy.check(now())
            if act is not None:
                try:
                    await act(now())
                except Exception:  # an executor error must not stop the marks and the exits
                    log.exception("options monitor: the exit sweep failed")
            sync_tokens()
            await sleep(poll_seconds)
    finally:
        await strategy.on_stop()
    return len(strategy.exits)
