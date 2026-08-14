"""Index OHLC adapter + repository for the regime overlay (checkpoint 2).

Fetches daily candles through kc.historical_data, caches them in index_series, and hands
app/core/regime.py the pure Candle objects it expects. Nothing here decides anything about
the market — resolution, caching and session alignment only.

SAFETY PROPERTIES
- Instrument resolution FAILS on zero or multiple matches. It never silently takes the
  first row: two indices whose symbols differ by a space would otherwise be swappable.
- A session that has not closed is stored with is_final=0 and is never returned to the
  signal path. Consuming a live candle would make the signal change intraday.
- UPSERT is transactional and idempotent; re-running a day's update changes nothing.
- Daily updates fetch only the dates missing from the cache.
- The clock is injected, so tests and historical replay are deterministic.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import time as _time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from ..core.ratelimit import KiteLimits
from ..core.regime import Candle, RegimeConfig
from . import db

log = logging.getLogger("index_cache")

# Every instrument. benchmark.py keeps a separate, index-only cache: sharing one
# filename let whichever module ran last redefine what the other read.
INSTRUMENT_CACHE = "data/.instruments_all.json"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MARKET_CLOSE = dt.time(15, 30)
# Kite caps a daily-candle request; walk long histories in chunks well inside the limit.
CHUNK_DAYS = 1800


class InstrumentNotFoundError(LookupError):
    pass


class AmbiguousInstrumentError(LookupError):
    pass


# =====================================================================================
# clock (injected so replay and tests are deterministic)
# =====================================================================================
@dataclass(frozen=True)
class Clock:
    """Wall clock in IST. Tests inject a frozen instance."""
    fixed: dt.datetime | None = None

    def now(self) -> dt.datetime:
        return self.fixed or dt.datetime.now(IST)

    def today(self) -> dt.date:
        return self.now().date()

    def session_is_closed(self, session: dt.date) -> bool:
        """True once `session` can no longer change — i.e. its candle is final."""
        now = self.now()
        if session < now.date():
            return True
        if session > now.date():
            return False
        return now.timetz().replace(tzinfo=None) >= MARKET_CLOSE


# =====================================================================================
# instrument resolution
# =====================================================================================
def resolve_index_instrument(instruments: Sequence[dict], index_name: str,
                             cfg: RegimeConfig) -> dict:
    """Resolve one index tradingsymbol to exactly one instrument. Pure over the dump.

    Matches on exchange + segment + (exact symbol OR a configured alias). Raises rather
    than guessing — picking arbitrarily among candidates is how you end up trading the
    wrong index for months without noticing.
    """
    accepted = {index_name.strip().upper()}
    accepted.update(a.strip().upper() for a in cfg.index_aliases.get(index_name, ()))

    hits = [
        r for r in instruments
        if str(r.get("segment", "")).upper() == cfg.segment.upper()
        and str(r.get("exchange", cfg.exchange)).upper() == cfg.exchange.upper()
        and str(r.get("tradingsymbol", "")).strip().upper() in accepted
    ]
    if not hits:
        near = sorted({r.get("tradingsymbol", "") for r in instruments
                       if index_name.split()[0].upper()
                       in str(r.get("tradingsymbol", "")).upper()})[:10]
        raise InstrumentNotFoundError(
            f"index {index_name!r} not found on {cfg.exchange}/{cfg.segment}. "
            f"Accepted symbols/aliases: {sorted(accepted)}. Nearby: {near or 'none'}")
    if len(hits) > 1:
        raise AmbiguousInstrumentError(
            f"index {index_name!r} matched {len(hits)} instruments: "
            f"{[(h['tradingsymbol'], h['instrument_token']) for h in hits]}. "
            f"Narrow the configured symbol/aliases — refusing to choose.")
    return hits[0]


async def load_instruments(kite, limits: KiteLimits | None = None, *,
                           clock: Clock | None = None, refresh: bool = False,
                           cache_path: str = INSTRUMENT_CACHE) -> list[dict]:
    """Instrument dump, cached to disk once per day."""
    ck = clock or Clock()
    today = ck.today().isoformat()
    if not refresh and os.path.exists(cache_path):
        try:
            blob = json.load(open(cache_path))
            if blob.get("fetched") == today:
                return blob["rows"]
        except Exception:
            pass
    lim = limits or KiteLimits()
    await lim.api_slot()
    dump = await asyncio.to_thread(kite.kc.instruments)
    rows = [{"tradingsymbol": r["tradingsymbol"], "instrument_token": r["instrument_token"],
             "segment": r.get("segment", ""), "exchange": r.get("exchange", ""),
             "name": r.get("name", "")} for r in dump]
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump({"fetched": today, "rows": rows}, f)
    return rows


# =====================================================================================
# repository
# =====================================================================================
def normalise_session_date(value) -> dt.date:
    """Kite returns tz-aware datetimes; the trading date is its IST calendar date."""
    if isinstance(value, dt.datetime):
        return (value.astimezone(IST).date() if value.tzinfo else value.date())
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


class IndexRepository:
    """All index_series reads/writes. Holds a connection; never opens one itself."""

    def __init__(self, conn):
        self.conn = conn

    # --- writes ---------------------------------------------------------------------
    def upsert(self, index_name: str, candles: Iterable[Candle], *,
               instrument_token: int | None = None) -> dict:
        rows = []
        now = dt.datetime.now(IST).isoformat(timespec="seconds")
        for c in candles:
            rows.append((index_name, c.date.isoformat(), c.open, c.high, c.low,
                         float(c.close), instrument_token, 1 if c.is_final else 0, now))
        if not rows:
            return {"index_name": index_name, "written": 0, "first": None, "last": None}
        with db.transaction(self.conn):
            self.conn.executemany(
                """INSERT INTO index_series(index_name, date, open, high, low, close,
                                            instrument_token, is_final, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(index_name, date) DO UPDATE SET
                       open=excluded.open, high=excluded.high, low=excluded.low,
                       close=excluded.close,
                       instrument_token=COALESCE(excluded.instrument_token,
                                                 index_series.instrument_token),
                       is_final=excluded.is_final, updated_at=excluded.updated_at""",
                rows)
        return {"index_name": index_name, "written": len(rows),
                "first": rows[0][1], "last": rows[-1][1]}

    # --- reads -----------------------------------------------------------------------
    def load(self, index_name: str, *, start: dt.date | None = None,
             end: dt.date | None = None, final_only: bool = True) -> list[Candle]:
        sql = "SELECT date, open, high, low, close, is_final FROM index_series WHERE index_name=?"
        args: list = [index_name]
        if final_only:
            sql += " AND is_final=1"
        if start:
            sql += " AND date >= ?"
            args.append(start.isoformat())
        if end:
            sql += " AND date <= ?"
            args.append(end.isoformat())
        sql += " ORDER BY date"
        return [Candle(date=dt.date.fromisoformat(r["date"]), open=r["open"], high=r["high"],
                       low=r["low"], close=r["close"], is_final=bool(r["is_final"]))
                for r in self.conn.execute(sql, args)]

    def last_date(self, index_name: str, *, final_only: bool = True) -> dt.date | None:
        sql = "SELECT MAX(date) d FROM index_series WHERE index_name=?"
        if final_only:
            sql += " AND is_final=1"
        row = self.conn.execute(sql, (index_name,)).fetchone()
        return dt.date.fromisoformat(row["d"]) if row and row["d"] else None

    def session_dates(self, index_name: str) -> set[dt.date]:
        return {dt.date.fromisoformat(r["date"]) for r in self.conn.execute(
            "SELECT date FROM index_series WHERE index_name=? AND is_final=1", (index_name,))}

    def coverage(self, index_name: str) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) n, SUM(is_final) f, MIN(date) first, MAX(date) last "
            "FROM index_series WHERE index_name=?", (index_name,)).fetchone()
        return {"index_name": index_name, "rows": row["n"] or 0,
                "final_rows": row["f"] or 0, "first": row["first"], "last": row["last"]}

    def has_warmup(self, index_name: str, cfg: RegimeConfig) -> tuple[bool, int, int]:
        """max(MA lengths) + confirmation days of FINAL sessions must exist."""
        need = max(cfg.ma_lengths) + cfg.confirm_days
        have = self.conn.execute(
            "SELECT COUNT(*) n FROM index_series WHERE index_name=? AND is_final=1",
            (index_name,)).fetchone()["n"]
        return have >= need, have, need

    # --- session alignment -------------------------------------------------------------
    def aligned_sessions(self, index_names: Sequence[str]) -> list[dt.date]:
        """Sessions where EVERY required index has a final candle.

        Intersection, never union — a weekly evaluation may not run on a date where one
        index is missing, and a missing index is never forward-filled.
        """
        if not index_names:
            return []
        sets = [self.session_dates(n) for n in index_names]
        common = set.intersection(*sets) if sets else set()
        return sorted(common)

    def signal_session_for_week(self, index_names: Sequence[str], week_end: dt.date,
                                *, max_lookback_days: int = 7) -> dt.date | None:
        """Latest aligned session at or before week_end.

        This is what handles a holiday: if the configured Friday has no session, the
        signal is taken from the final completed session of that week instead.
        """
        floor = week_end - dt.timedelta(days=max_lookback_days)
        candidates = [d for d in self.aligned_sessions(index_names) if floor <= d <= week_end]
        return candidates[-1] if candidates else None


# =====================================================================================
# fetching
# =====================================================================================
async def _with_retry(fn: Callable, *args, attempts: int = 3, base_delay: float = 1.0,
                      **kwargs):
    """Retry transient broker errors with exponential backoff."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:            # kiteconnect raises a wide variety
            last = exc
            if attempt == attempts - 1:
                break
            delay = base_delay * (2 ** attempt)
            log.warning("historical_data failed (%s); retrying in %.1fs", exc, delay)
            await asyncio.sleep(delay)
    raise RuntimeError(f"historical_data failed after {attempts} attempts: {last}") from last


async def fetch_candles(kite, token: int, start: dt.date, end: dt.date, *,
                        limits: KiteLimits, clock: Clock,
                        attempts: int = 3) -> list[Candle]:
    """Chunked daily candles. Sessions that have not closed are marked is_final=0."""
    out: list[Candle] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + dt.timedelta(days=CHUNK_DAYS), end)
        await limits.api_slot()
        rows = await _with_retry(kite.kc.historical_data, token, cursor, chunk_end, "day",
                                 attempts=attempts)
        for r in rows:
            d = normalise_session_date(r["date"])
            out.append(Candle(date=d, open=r.get("open"), high=r.get("high"),
                              low=r.get("low"), close=float(r["close"]),
                              is_final=clock.session_is_closed(d)))
        cursor = chunk_end + dt.timedelta(days=1)
    return out


async def update_index_history(kite, conn, index_name: str, cfg: RegimeConfig, *,
                               start: dt.date | None = None, end: dt.date | None = None,
                               clock: Clock | None = None,
                               limits: KiteLimits | None = None,
                               instruments: Sequence[dict] | None = None,
                               full_refresh: bool = False) -> dict:
    """Bring one index up to date, fetching ONLY the dates the cache is missing."""
    ck = clock or Clock()
    lim = limits or KiteLimits()
    repo = IndexRepository(conn)

    dump = instruments if instruments is not None else await load_instruments(
        kite, lim, clock=ck)
    inst = resolve_index_instrument(dump, index_name, cfg)
    token = inst["instrument_token"]
    stored_name = index_name          # store under the CONFIGURED name, not the broker's

    to = end or ck.today()
    if full_refresh or start is not None:
        frm = start or dt.date(2005, 1, 1)
    else:
        last = repo.last_date(stored_name)
        # Re-fetch the last cached session too: it may have been provisional.
        frm = last if last else dt.date(2005, 1, 1)

    if frm > to:
        return {"index_name": stored_name, "written": 0, "skipped": "already current",
                "instrument_token": token}

    candles = await fetch_candles(kite, token, frm, to, limits=lim, clock=ck)
    res = repo.upsert(stored_name, candles, instrument_token=token)
    res["instrument_token"] = token
    res["resolved_symbol"] = inst["tradingsymbol"]
    res["fetched_from"] = frm.isoformat()
    return res


async def update_all(kite, conn, cfg: RegimeConfig, *, start: dt.date | None = None,
                     clock: Clock | None = None, limits: KiteLimits | None = None,
                     full_refresh: bool = False) -> list[dict]:
    """Update every structural index plus the momentum sentinel."""
    ck = clock or Clock()
    lim = limits or KiteLimits()
    dump = await load_instruments(kite, lim, clock=ck)
    names = list(cfg.structural_indices.values()) + [cfg.momentum_sentinel]
    out = []
    for name in names:
        out.append(await update_index_history(kite, conn, name, cfg, start=start,
                                              clock=ck, limits=lim, instruments=dump,
                                              full_refresh=full_refresh))
    return out


def required_index_names(cfg: RegimeConfig) -> list[str]:
    return list(cfg.structural_indices.values()) + [cfg.momentum_sentinel]


def build_all_signals(conn, cfg: RegimeConfig, *, as_of: dt.date,
                      end: dt.date | None = None) -> dict:
    """Cache -> IndexSignals for every required index, via the PURE engine functions."""
    from ..core.regime import build_index_signals

    repo = IndexRepository(conn)
    out = {}
    for name in required_index_names(cfg):
        candles = repo.load(name, end=end or as_of, final_only=True)
        out[name] = build_index_signals(name, candles, cfg, as_of=as_of)
    return out
