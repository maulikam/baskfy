"""Live chain snapshot: one instant of spot, quotes, depth and OI.

Read-only. Builds the flat chain rows that levels.py and selection.py consume, so those
two modules never touch a broker and stay testable with dictionaries.

Kite's quote() carries five-level depth, open interest and the last trade time in a single
call, which is everything the strategy needs. The websocket feed (V4) will produce the same
ChainSnapshot from ticks, so nothing downstream changes when the transport does.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .levels import atm_strike

MAX_QUOTE_BATCH = 200          # Kite accepts more; batching keeps one bad symbol contained


@dataclass(frozen=True)
class ChainSnapshot:
    as_of: dt.datetime
    spot: float
    expiry: dt.date
    rows: tuple[dict, ...]
    stale_seconds: float = 0.0

    def as_rows(self) -> list[dict]:
        return [dict(r) for r in self.rows]

    @property
    def complete(self) -> bool:
        """A chain with no two-sided quotes is not a chain; entry_early means the first
        tick where this is true, not a wall-clock time."""
        return any(r["bid"] > 0 and r["ask"] > 0 for r in self.rows)


def chain_instruments(instruments: Sequence[Mapping[str, Any]], *, name: str,
                      expiry: dt.date, spot: float, span_pct: float = 8.0
                      ) -> list[Mapping[str, Any]]:
    """Contracts within `span_pct` of spot for one expiry, both kinds."""
    lo, hi = spot * (1 - span_pct / 100), spot * (1 + span_pct / 100)
    return [i for i in instruments
            if i.get("name") == name and i.get("expiry") == expiry
            and i.get("segment", "").endswith("-OPT")
            and lo <= float(i["strike"]) <= hi]


def snapshot(kc, *, instruments: Sequence[Mapping[str, Any]], index_key: str,
             expiry: dt.date, name: str = "NIFTY", span_pct: float = 8.0,
             now: dt.datetime | None = None) -> ChainSnapshot:
    """One quote round trip for spot plus the whole near chain."""
    now = now or dt.datetime.now()
    spot_q = kc.quote([index_key]).get(index_key) or {}
    spot = float(spot_q.get("last_price") or 0.0)
    if spot <= 0:
        raise RuntimeError(f"{index_key} has no last price")

    contracts = chain_instruments(instruments, name=name, expiry=expiry, spot=spot,
                                  span_pct=span_pct)
    keys = [f"{c['exchange']}:{c['tradingsymbol']}" for c in contracts]
    quotes: dict[str, Any] = {}
    for i in range(0, len(keys), MAX_QUOTE_BATCH):
        quotes |= kc.quote(keys[i:i + MAX_QUOTE_BATCH]) or {}

    rows, newest = [], None
    for c in contracts:
        q = quotes.get(f"{c['exchange']}:{c['tradingsymbol']}")
        if not q:
            continue
        depth = q.get("depth") or {}
        buys, sells = depth.get("buy") or [], depth.get("sell") or []
        lt = q.get("last_trade_time")
        if isinstance(lt, dt.datetime):
            newest = lt if newest is None else max(newest, lt)
        rows.append({
            "symbol": c["tradingsymbol"], "token": int(c["instrument_token"]),
            "strike": float(c["strike"]), "kind": c["instrument_type"],
            "lot_size": int(c["lot_size"]),
            "bid": float((buys[0] if buys else {}).get("price") or 0.0),
            "ask": float((sells[0] if sells else {}).get("price") or 0.0),
            "last": float(q.get("last_price") or 0.0),
            "oi": float(q.get("oi") or 0.0),
            "volume": float(q.get("volume") or 0.0),
            # Kept whole: every simulated fill is walked against this and stored with it,
            # so any price in the journal can be re-derived without trusting the engine.
            "depth": {"buy": [{"price": float(l.get("price") or 0),
                               "quantity": int(l.get("quantity") or 0)} for l in buys[:5]],
                      "sell": [{"price": float(l.get("price") or 0),
                                "quantity": int(l.get("quantity") or 0)} for l in sells[:5]]},
        })

    # Kite returns last_trade_time NAIVE. Mixing it with an aware `now` raises, and
    # coercing the wrong way silently shifts staleness by the UTC offset — five and a half
    # hours here, which would mark every quote stale and veto every session.
    stale = 0.0
    if newest is not None:
        ref = now if newest.tzinfo is None else now.astimezone(newest.tzinfo)
        if ref.tzinfo is not None and newest.tzinfo is None:
            ref = ref.replace(tzinfo=None)
        stale = max(0.0, (ref - newest).total_seconds())
    return ChainSnapshot(as_of=now, spot=spot, expiry=expiry, rows=tuple(rows),
                         stale_seconds=stale)


def atm_straddle(snap: ChainSnapshot, step: int) -> tuple[float | None, float]:
    """(ASP, atm_strike) from mids. Mid is a valuation here, never a fill price."""
    k = atm_strike(snap.spot, step)
    mids: dict[str, float] = {}
    for r in snap.rows:
        if abs(r["strike"] - k) > 1e-6:
            continue
        if r["bid"] > 0 and r["ask"] > 0 and r["ask"] >= r["bid"]:
            mids[r["kind"]] = (r["bid"] + r["ask"]) / 2.0
    if "CE" in mids and "PE" in mids:
        return mids["CE"] + mids["PE"], k
    return None, k
