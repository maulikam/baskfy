"""SW30 — the swing gate re-read on the live index during the session (Maulik, 29 Sep 2026).

*"The gate we are checking is currently implemented as end of the day. The gate should be
checked on live data. Let's say tomorrow, if the 10-day moving average is crossing the 20-day,
the gate should automatically open."* His two answers the same evening: **both ways** (a cross
back down closes it again) and **the index live, breadth at the last close**.

What this does, and nothing more: between 09:15 and 15:30 on a weekday, the gate that
`load_context` hands the monitor and the confirm is `market_gate` over

* the last close's breadth (`sw_market_daily`, the row the session's plan was built on), and
* the benchmark's 10- and 20-day averages over the last 19 published closes plus today's
  Kite quote — the index is `SWING_INDEX_SLUG` (SW29: NIFTY Smallcap 250).

The rung comes from :func:`baskfy_core.swing.market.intraday_tier`: the ladder never moves on a
quote. Outside the session, without a Kite session, without a quote symbol for the index or
without enough published closes, the gate is the last close's, exactly as before SW30.
Nothing here places, confirms or cancels anything; it changes which entries the rules allow.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from baskfy_core.swing.config import MarketConfig
from baskfy_core.swing.market import (
    BreadthSnapshot,
    ExposureTier,
    IndexReading,
    MarketGate,
    index_reading,
    intraday_tier,
    market_gate,
)

from . import config as C

log = logging.getLogger("swing.live_gate")

SESSION_OPEN = dt.time(9, 15)
SESSION_CLOSE = dt.time(15, 30)

#: Our index slug -> Kite's tradingsymbol on NSE. The worker's `KiteProvider.INDEX_QUOTE_SYMBOL`
#: is the same table; the desk does not import the providers package, so it is written out.
INDEX_QUOTE_SYMBOL: dict[str, str] = {
    "nifty-smallcap-250": "NIFTY SMLCAP 250",
    "nifty-mid-small-400": "NIFTY MIDSML 400",
    "nifty-500": "NIFTY 500",
}

#: Today's level of the gate's benchmark, or ``None`` when it cannot be read.
LiveLevel = Callable[[], float | None]


@dataclass(frozen=True, slots=True)
class LiveGate:
    """The gate as the live index reads it, with what it was read from."""

    gate: MarketGate
    tier: ExposureTier
    reading: IndexReading
    index_slug: str


def in_session(moment: dt.datetime) -> bool:
    """09:15–15:30 on a weekday. A holiday has no session and no monitor; the quote would be
    the last close, which is already the last published level, so it is never appended."""
    return moment.weekday() < 5 and SESSION_OPEN <= moment.time() <= SESSION_CLOSE


def kite_level(kite: Any, slug: str | None = None) -> LiveLevel:  # noqa: ANN401 - Kite client
    """A :data:`LiveLevel` over the desk's Kite client (`Kite.ltp`, cached and rate-limited)."""
    index = slug or C.SWING_INDEX_SLUG

    def read() -> float | None:
        symbol = INDEX_QUOTE_SYMBOL.get(index)
        if symbol is None:
            log.warning("no Kite symbol for the gate index %s; the last close's gate stands", index)
            return None
        try:
            value = kite.ltp([symbol]).get(symbol)
        except Exception as exc:  # noqa: BLE001 - no session, no live gate; the close's stands
            log.warning("no live level for %s: %s; the gate is the last close's", symbol, exc)
            return None
        return float(value) if value else None

    return read


def desk_level() -> LiveLevel:
    """A :data:`LiveLevel` over the desk server's own Kite client, resolved at read time so a
    login after the store was opened is seen, and no session is ``None`` rather than an error."""

    def read() -> float | None:
        try:
            from . import main as _main  # noqa: PLC0415 - main owns the client; import at call

            kite = _main.kite()
        except Exception as exc:  # noqa: BLE001 - no client, no live gate; the close's stands
            log.warning("no Kite client for the live gate: %s; the gate is the last close's", exc)
            return None
        return kite_level(kite)()

    return read


def published_levels(
    conn: Any, *, slug: str, before: dt.date, count: int, schema: str  # noqa: ANN401 - DB-API
) -> list[float]:
    """The last ``count`` published levels of ``slug`` strictly before ``before``, oldest first."""
    prefix = f"{schema}." if schema else ""
    rows = conn.execute(
        f"SELECT s.level FROM {prefix}index_snapshot_daily s "
        f"JOIN {prefix}index_def d ON d.id = s.index_id "
        "WHERE d.slug = ? AND s.date < ? AND s.level IS NOT NULL ORDER BY s.date DESC LIMIT ?",
        (slug, before.isoformat(), count),
    ).fetchall()
    return [float(r["level"]) for r in reversed(rows)]


def live_gate(  # noqa: PLR0913 - one keyword per input the reading depends on
    *,
    breadth: BreadthSnapshot,
    close_tier: ExposureTier,
    closes: list[float],
    level: float,
    slug: str,
    config: MarketConfig,
) -> LiveGate | None:
    """The gate from the last close's breadth and the index with today's level appended.

    ``closes`` are the published levels before today, oldest first. ``None`` when they are too
    few for the slow average — the caller keeps the last close's gate.
    """
    reading = index_reading([*closes[-(config.index_ma_slow - 1) :], level], config)
    if reading is None:
        return None
    gate = market_gate(breadth, reading, config)
    return LiveGate(
        gate=gate, tier=intraday_tier(close_tier, gate, config), reading=reading, index_slug=slug
    )
