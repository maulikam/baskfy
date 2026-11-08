"""One broker, two faces — and they must not overlap (M16, P3.6).

Baskfy talks to Kite for two unrelated reasons, with two different sets of credentials, and
conflating them is the kind of mistake that is invisible until it is expensive:

**MarketData** — the SYSTEM's own Kite app. Instruments, historical candles, quotes. It feeds the
nightly pipeline, it is shared, its results are cached, and it must never be able to place an
order. `baskfy_providers.KiteProvider` is this face.

**Trading** — the ACCOUNT's credentials. Holdings, margins, orders, GTTs. It acts on exactly one
book, its token is per-user, it is never shared, and it must never be used to fetch the universe.
The desk's `app.kite_client.Kite` is this face.

THE RULE, STATED ONCE
---------------------
`CLAUDE.md`, from the merge analysis (docs/03 §3b): **a user's access token must never fetch
universe data, and the system token must never place an order.**

Tokens are opaque strings, so nothing can type-check which is which. What *can* be enforced is
that the two faces do not share a surface: if the trading client has no `daily_bars`, a caller
holding a trading token cannot make an ingestion call with it by accident, and if the market-data
client has no `place_order`, the system token cannot reach the order path at all.

That is what `packages/execution/tests/test_broker_faces.py` asserts. It is a weaker guarantee
than per-token authorisation and a much stronger one than a comment, and it becomes load-bearing
at P4 when there is more than one account.
"""
from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

#: Methods that belong to the SYSTEM's credentials. A trading client must not have these.
MARKET_DATA_ONLY: frozenset[str] = frozenset({"daily_bars", "list_instruments", "chunk_windows"})

#: Methods that act on ONE account's money. A market-data client must not have these.
TRADING_ONLY: frozenset[str] = frozenset(
    {"place_order", "place_cnc_order", "place_gtt_stop", "delete_gtt", "holdings", "margins",
     "available_cash", "trades"}
)


@runtime_checkable
class MarketData(Protocol):
    """Read-only, system-credentialled, shared. Never an order path."""

    def list_instruments(self) -> list[object]: ...

    def daily_bars(self, token: int, start: dt.date, end: dt.date) -> object: ...


@runtime_checkable
class Trading(Protocol):
    """One account's book. Never used to fetch the universe."""

    def holdings(self) -> list[dict[str, object]]: ...

    def available_cash(self) -> float: ...
