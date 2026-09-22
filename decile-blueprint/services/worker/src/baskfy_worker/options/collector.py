"""The once-a-minute chain collector: the forward dataset (``docs/options/03`` §5, ``07`` §3, OP3).

Every trading minute 09:15-15:30, **one** ``quote()`` call: the two nearest NIFTY expiries'
listed strikes within ``snapshot_strikes`` [15] steps of ATM, CE and PE (≤ 124 contracts), plus
``NSE:NIFTY 50`` for the spot — all in one batch, well under Kite's 500. Each contract becomes one
``op_chain_snapshot`` row with top of book, the five-level depth as Kite gave it, volume, OI (as
Kite reports it; the unit is a read-side conversion), the expiry's put-call-parity forward and
Black-76 IV and greeks computed **at write** by ``baskfy_core.options`` and rounded to the column
(house rule 8). A contract whose IV the solver refuses keeps its quote and has null greeks.

Which strikes are "within reach" needs a spot *before* the call. The newest stored NIFTY 50 close
(``op_index_minute``, written a minute earlier by the index-bar task) is the hint; ±15 strikes
around a one-minute-old ATM still bracket the true ATM by fourteen. Only when no bar has been
stored in the last four days does the collector spend a second ``quote()`` on the spot alone —
the rows then carry the spot from the main call regardless. DECISIONS-OP OP3.4.

Idempotent and append-only: a re-run of the same minute inserts nothing (``ON CONFLICT DO
NOTHING`` on ``(ts, instrument_token)``, house rule 7). Before writing, this month's and next
month's partitions are ensured (OP2.5 — there is no DEFAULT partition). Behind
``BASKFY_OPTIONS_COLLECT_ENABLED`` (default **false**) for the limiter's sake, not safety; the
collector reads prices and nothing else, and has no order path (law 2).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OpChainSnapshot
from baskfy_core.models.base import JsonObject
from baskfy_core.options.calendar import Contract
from baskfy_core.options.chain import (
    Level,
    OptionQuote,
    parity_forward,
    quote_greeks,
    snapshot_strikes,
    strike_step,
)
from baskfy_core.options.config import OptionsConfig
from baskfy_core.options.greeks import Greeks, year_fraction
from baskfy_providers.kite import QUOTE_BATCH_SIZE
from baskfy_providers.records import OptionQuoteRecord
from baskfy_worker.options.index_bars import IST, NIFTY_50, latest_close
from baskfy_worker.options.master import load_contracts
from baskfy_worker.options.partitions import ensure_chain_partition, month_bounds
from baskfy_worker.options.reads import QuoteReader

log = logging.getLogger("baskfy_worker.options.collector")

#: The spot, quoted in the same call as the chain.
SPOT_KEY: Final = f"NSE:{NIFTY_50}"
#: The exchange every contract key carries.
NFO: Final = "NFO"
#: ``03`` §5: the two nearest expiries.
EXPIRIES_PER_SNAPSHOT: Final = 2
#: How old a stored NIFTY 50 close may be and still pick the strikes (DECISIONS-OP OP3.4).
SPOT_HINT_MAX_AGE: Final = dt.timedelta(days=4)
SOURCE_QUOTE: Final = "QUOTE"

_CENT: Final = Decimal("0.01")
_MICRO: Final = Decimal("0.000001")
#: ``numeric(10,6)`` holds |x| < 10^4; a greek outside it is not stored (never truncated).
_GREEK_LIMIT: Final = Decimal(10_000)


class TooManySymbols(ValueError):
    """The snapshot would need more than one ``quote()`` call — a configuration error."""


@dataclass(frozen=True, slots=True)
class ChainPick:
    """Which contracts this minute reads, and each expiry's strike step."""

    expiries: tuple[dt.date, ...]
    contracts: tuple[Contract, ...]
    steps: dict[dt.date, Decimal]

    def keys(self) -> list[str]:
        """``NFO:<symbol>`` for each contract, then the spot — one batch, ≤ 500 (tested)."""
        keys = [f"{NFO}:{c.tradingsymbol}" for c in self.contracts]
        keys.append(SPOT_KEY)
        if len(keys) > QUOTE_BATCH_SIZE:
            raise TooManySymbols(
                f"{len(keys)} symbols exceed one quote() call ({QUOTE_BATCH_SIZE}); the collector "
                "makes exactly one call a minute (docs/options/03 §5)"
            )
        return keys


@dataclass(slots=True)
class CollectReport:
    """One minute's result; JSON-able for the task and the CLI."""

    ts: dt.datetime | None = None
    rows: int = 0
    inserted: int = 0
    calls: int = 0
    symbols: int = 0
    expiries: list[str] = field(default_factory=list)
    unquoted: int = 0
    no_greeks: int = 0
    spot: str | None = None
    spot_hint: str | None = None
    skipped: str | None = None
    partitions: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> JsonObject:
        return {
            "ts": self.ts.isoformat() if self.ts else None,
            "rows": self.rows,
            "inserted": self.inserted,
            "calls": self.calls,
            "symbols": self.symbols,
            "expiries": list(self.expiries),
            "unquoted": self.unquoted,
            "no_greeks": self.no_greeks,
            "spot": self.spot,
            "spot_hint": self.spot_hint,
            "skipped": self.skipped,
            "partitions": list(self.partitions),
            "seconds": round(self.seconds, 3),
        }


# --- pure: which contracts, and what a row holds ------------------------------------------------


def nearest_expiries(
    contracts: Iterable[Contract], today: dt.date, count: int = EXPIRIES_PER_SNAPSHOT
) -> tuple[dt.date, ...]:
    """The ``count`` nearest listed expiries on or after ``today`` (today's, on an expiry day)."""
    return tuple(sorted({c.expiry for c in contracts if c.expiry >= today})[:count])


def pick_contracts(
    contracts: Sequence[Contract], spot: Decimal, today: dt.date, config: OptionsConfig
) -> ChainPick:
    """``04`` §2.1: per expiry, the strike step read from the master and the strikes within
    ``snapshot_strikes`` steps of ATM, both types. An expiry whose step cannot be read is left
    out rather than guessed."""
    wanted = nearest_expiries(contracts, today)
    by_expiry: dict[dt.date, list[Contract]] = defaultdict(list)
    for c in contracts:
        if c.expiry in wanted:
            by_expiry[c.expiry].append(c)
    count = config.chain.snapshot_strikes
    picked: list[Contract] = []
    steps: dict[dt.date, Decimal] = {}
    for expiry in wanted:
        rows = by_expiry[expiry]
        step = strike_step((c.strike for c in rows), spot, count)
        if step is None:
            continue
        steps[expiry] = step
        strikes = set(snapshot_strikes((c.strike for c in rows), spot, step, count))
        picked.extend(
            sorted(
                (c for c in rows if c.strike in strikes),
                key=lambda c: (c.strike, c.option_type.value),
            )
        )
    return ChainPick(expiries=tuple(steps), contracts=tuple(picked), steps=steps)


def to_option_quote(record: OptionQuoteRecord, contract: Contract, ts: dt.datetime) -> OptionQuote:
    """A provider quote as the pure core's :class:`OptionQuote` (best bid/ask from depth)."""
    bids = tuple(Level(level.price, level.quantity) for level in record.bids)
    asks = tuple(Level(level.price, level.quantity) for level in record.asks)
    return OptionQuote(
        instrument_token=contract.instrument_token,
        expiry=contract.expiry,
        strike=contract.strike,
        option_type=contract.option_type,
        bid=bids[0].price if bids else None,
        ask=asks[0].price if asks else None,
        bids=bids,
        asks=asks,
        oi=record.oi or 0,
        ts=record.as_of or ts,
        last=record.last_price,
        volume=record.volume,
    )


def _cent(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _greek(value: float) -> Decimal | None:
    if not math.isfinite(value):
        return None
    rounded = Decimal(repr(value)).quantize(_MICRO, rounding=ROUND_HALF_UP)
    return rounded if abs(rounded) < _GREEK_LIMIT else None


def _depth_json(record: OptionQuoteRecord) -> JsonObject:
    """Five levels a side, as Kite gave them (zero padding dropped at the provider)."""
    return {
        "buy": [
            {"price": str(lv.price), "quantity": lv.quantity, "orders": lv.orders}
            for lv in record.bids
        ],
        "sell": [
            {"price": str(lv.price), "quantity": lv.quantity, "orders": lv.orders}
            for lv in record.asks
        ],
    }


def snapshot_rows(  # noqa: PLR0913 - the pick, the answers, the spot, both clocks, the config
    pick: ChainPick,
    quotes: dict[str, OptionQuoteRecord],
    spot: Decimal | None,
    *,
    minute: dt.datetime,
    now: dt.datetime,
    config: OptionsConfig,
) -> tuple[list[dict[str, object]], int]:
    """The rows for one minute, and how many carry no greeks.

    The forward is per expiry, from that expiry's quotes (``04`` §2.2); greeks per contract on it
    (``04`` §2.3), ``T`` from ``now`` to 15:30 on the expiry. With no spot there is no ATM, so no
    forward and no greeks — the quotes are still stored.
    """
    settle = config.calendar.market_close
    core: dict[int, OptionQuote] = {}
    for c in pick.contracts:
        record = quotes.get(f"{NFO}:{c.tradingsymbol}")
        if record is not None:
            core[c.instrument_token] = to_option_quote(record, c, now)
    forwards: dict[dt.date, Decimal | None] = {}
    for expiry, step in pick.steps.items():
        years = year_fraction(now, expiry, settle)
        same = [q for q in core.values() if q.expiry == expiry]
        forwards[expiry] = (
            parity_forward(same, spot, step, years, config.chain.rate)
            if spot is not None and years > 0 and same
            else None
        )
    rows: list[dict[str, object]] = []
    no_greeks = 0
    for c in pick.contracts:
        record = quotes.get(f"{NFO}:{c.tradingsymbol}")
        if record is None:
            continue
        quote = core[c.instrument_token]
        forward = forwards.get(c.expiry)
        greeks: Greeks | None = None
        if forward is not None:
            greeks = quote_greeks(
                quote, forward, now=now, tick=c.tick_size, config=config.chain, settle=settle
            )
        if greeks is None:
            no_greeks += 1
        rows.append(
            {
                "ts": minute,
                "instrument_token": c.instrument_token,
                "expiry": c.expiry,
                "strike": _cent(c.strike),
                "option_type": c.option_type.value,
                "spot": _cent(spot),
                "bid": _cent(quote.bid),
                "ask": _cent(quote.ask),
                "last": _cent(record.last_price),
                "bid_qty": record.bids[0].quantity if record.bids else None,
                "ask_qty": record.asks[0].quantity if record.asks else None,
                "depth_json": _depth_json(record),
                "volume": record.volume,
                "oi": record.oi,
                "forward": _cent(forward),
                "iv": _greek(greeks.iv) if greeks else None,
                "delta": _greek(greeks.delta) if greeks else None,
                "gamma": _greek(greeks.gamma) if greeks else None,
                "theta": _greek(greeks.theta) if greeks else None,
                "vega": _greek(greeks.vega) if greeks else None,
                "greeks_model": config.chain.greeks_model if forward is not None else None,
                "source": SOURCE_QUOTE,
            }
        )
    return rows, no_greeks


def minute_of(now: dt.datetime) -> dt.datetime:
    """The snapshot's minute: ``now`` in IST, floored."""
    return now.astimezone(IST).replace(second=0, microsecond=0)


def in_session(now: dt.datetime, config: OptionsConfig | None = None) -> bool:
    """09:15 ≤ now ≤ 15:30 IST (``07`` §3's window)."""
    cal = (config or OptionsConfig()).calendar
    clock = now.astimezone(IST).time()
    return cal.market_open <= clock <= cal.market_close


# --- I/O -----------------------------------------------------------------------------------------


async def ensure_partitions(session: AsyncSession, day: dt.date) -> list[str]:
    """This month's and next month's ``op_chain_snapshot`` partitions (OP2.5)."""
    _, next_month = month_bounds(day)
    return [
        await ensure_chain_partition(session, day),
        await ensure_chain_partition(session, next_month),
    ]


async def insert_rows(session: AsyncSession, rows: Sequence[dict[str, object]]) -> int:
    """Append-only: a minute already stored is left exactly as it was."""
    if not rows:
        return 0
    stmt = (
        insert(OpChainSnapshot)
        .values(list(rows))
        .on_conflict_do_nothing(index_elements=["ts", "instrument_token"])
    )
    result = await session.execute(stmt)
    count = getattr(result, "rowcount", None)
    return int(count) if isinstance(count, int) and count >= 0 else len(rows)


async def collect_minute(
    session: AsyncSession,
    reader: QuoteReader,
    now: dt.datetime,
    config: OptionsConfig | None = None,
) -> CollectReport:
    """One minute's snapshot. The caller has already checked the flag, the window and the day."""
    started = time.monotonic()
    cfg = config or OptionsConfig()
    report = CollectReport(ts=minute_of(now))
    today = now.astimezone(IST).date()
    report.partitions = await ensure_partitions(session, today)
    contracts = await load_contracts(session, cfg.calendar.underlying)
    if not contracts:
        report.skipped = "no NIFTY contracts in op_contract (the nightly master has not run)"
        return report
    hint = await latest_close(session, NIFTY_50, now - SPOT_HINT_MAX_AGE)
    hint_spot: Decimal | None = hint[1] if hint is not None else None
    if hint_spot is None:
        spot_only = reader.option_quotes([SPOT_KEY])
        report.calls += 1
        hint_spot = next((q.last_price for q in spot_only if q.key == SPOT_KEY), None)
    if hint_spot is None or hint_spot <= 0:
        report.skipped = "no NIFTY 50 level to centre the strikes on"
        return report
    report.spot_hint = str(hint_spot)
    pick = pick_contracts(contracts, hint_spot, today, cfg)
    if not pick.contracts:
        report.skipped = "no listed strikes near the spot on the two nearest expiries"
        return report
    keys = pick.keys()
    report.symbols = len(keys)
    report.expiries = [e.isoformat() for e in pick.expiries]
    answered = {q.key: q for q in reader.option_quotes(keys)}
    report.calls += 1
    spot_quote = answered.get(SPOT_KEY)
    spot = spot_quote.last_price if spot_quote is not None else None
    report.spot = str(spot) if spot is not None else None
    rows, report.no_greeks = snapshot_rows(
        pick, answered, spot, minute=report.ts or minute_of(now), now=now, config=cfg
    )
    report.rows = len(rows)
    report.unquoted = len(pick.contracts) - len(rows)
    report.inserted = await insert_rows(session, rows)
    report.seconds = time.monotonic() - started
    return report


TradingDayCheck = Callable[[dt.date], Awaitable[bool]]


async def collect_gate(
    now: dt.datetime,
    *,
    enabled: bool,
    kite_ok: Callable[[], bool],
    trading_day: TradingDayCheck,
    config: OptionsConfig | None = None,
) -> str | None:
    """Why this minute makes **no** Kite call, or ``None`` if it may.

    In order, cheapest first, and every refusal is before any network call: the flag; the
    09:15-15:30 window; the NSE calendar (a holiday or weekend makes no call — tested); a usable
    Kite token.
    """
    if not enabled:
        return "BASKFY_OPTIONS_COLLECT_ENABLED is false"
    if not in_session(now, config):
        return "outside 09:15-15:30 IST"
    if not await trading_day(now.astimezone(IST).date()):
        return "not an NSE trading day"
    if not kite_ok():
        return "no usable Kite session"
    return None
