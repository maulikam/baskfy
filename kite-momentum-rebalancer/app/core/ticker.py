"""WebSocket tick bus. KiteTicker (threaded) pushes into an asyncio queue; strategies
subscribe. Hot path is dict-in-memory only — no DB, no disk. Latency budget: <1ms
in-process; network+broker dominate (~100ms+), which is the real Kite floor.

DESIGN CONSTRAINTS (verified against Kite docs/forum, 2026):
- Ticker delivers ~1 tick/sec per instrument (occasionally 2 for very liquid ones) —
  snapshots, NOT tick-by-tick. Strategies must not assume sub-second granularity.
- Max 3 websocket connections per API key, 3000 instruments each (9000 total).
- If full-mode parsing for 1000s of instruments saturates Python, split a Go
  ingester (gokiteconnect) publishing to this bus — do NOT rewrite strategies."""
from __future__ import annotations
import asyncio, logging
from collections import defaultdict

log = logging.getLogger("ticker")


class TickBus:
    def __init__(self):
        self.queues: dict[int, list[asyncio.Queue]] = defaultdict(list)
        self.last_tick: dict[int, dict] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self, token: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2048)
        self.queues[token].append(q)
        return q

    def publish_threadsafe(self, ticks: list[dict]):
        if not self.loop:
            return
        for t in ticks:
            tok = t["instrument_token"]
            self.last_tick[tok] = t
            for q in self.queues.get(tok, ()):
                self.loop.call_soon_threadsafe(q.put_nowait, t)


#: One websocket connection carries at most this many instruments (Kite: 3 connections per API
#: key, 3,000 each). SW20 refuses a longer list here rather than discovering the truncation as a
#: name that silently never ticks — a watched name with no ticks is a break nobody sees.
MAX_INSTRUMENTS_PER_CONNECTION = 3000


def start_ticker(api_key: str, access_token: str, tokens: list[int], bus: TickBus):
    """Call from an asyncio context. Returns the KiteTicker (runs in its own thread)."""
    from kiteconnect import KiteTicker
    if len(tokens) > MAX_INSTRUMENTS_PER_CONNECTION:
        raise ValueError(
            f"{len(tokens)} instruments asked for on one websocket; Kite carries "
            f"{MAX_INSTRUMENTS_PER_CONNECTION}. Split the list across connections, or watch "
            f"fewer names — a silently truncated subscription is a break nobody sees."
        )
    bus.loop = asyncio.get_running_loop()
    kws = KiteTicker(api_key, access_token)

    def on_ticks(ws, ticks):  # broker thread → bus
        bus.publish_threadsafe(ticks)

    def on_connect(ws, resp):
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)
        log.info("ticker subscribed to %d instruments", len(tokens))

    kws.on_ticks, kws.on_connect = on_ticks, on_connect
    kws.connect(threaded=True)
    return kws
