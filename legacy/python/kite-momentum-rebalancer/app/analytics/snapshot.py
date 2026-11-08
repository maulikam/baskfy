"""Daily EOD snapshot job — writes one idempotent row into snapshots().

Reads only. Places no orders, touches no order path: core/gateway.py stays the sole
route to the broker. Kite calls go through core/ratelimit.py and run in threads via
asyncio.to_thread, matching the gateway's convention so the event loop never blocks.

Holdings quantity is taken from kite_client.Kite.holdings(), which already sums
quantity + t1_quantity + collateral_quantity — pledged shares count as held.

Untouchable instruments (SGB/G-sec) are classified with core/guards.py itself rather than
a second symbol list, so there is exactly one definition of "untouchable" in the system.
They are recorded inside holdings_json but excluded from nav/invested by default (see
config.INCLUDE_UNTOUCHABLE_IN_NAV).

CLI:
    python -m app.analytics.snapshot                        # today, prices from holdings
    python -m app.analytics.snapshot --date 2026-08-14
    python -m app.analytics.snapshot --prices quote         # kc.quote OHLC close
    python -m app.analytics.snapshot --date 2026-08-11 --prices historical   # backfill
    python -m app.analytics.snapshot --force                # overwrite an existing row
    python -m app.analytics.snapshot --cashflow -1000000 --cashflow-type invest
    python -m app.analytics.snapshot --import-cashflows data/uploads/ledger.csv
    python -m app.analytics.snapshot --check-flows          # unrecorded-flow hints
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os

from .. import config as C
from ..core.guards import UntouchableInstrumentError, assert_tradeable
from ..core.ratelimit import KiteLimits
from ..kite_client import Kite
from . import db

log = logging.getLogger("snapshot")

INSTRUMENT_CACHE = "data/.instruments_{exchange}.json"


def is_untouchable(symbol: str, series: str | None = None) -> bool:
    """Single source of truth: ask guards.py, don't re-implement its rules."""
    try:
        assert_tradeable(symbol, series)
        return False
    except UntouchableInstrumentError:
        return True


class SnapshotJob:
    """Collects one EOD snapshot. Rate-limited, thread-offloaded, read-only."""

    def __init__(self, kite: Kite | None = None, limits: KiteLimits | None = None):
        self.kite = kite or Kite()
        self.limits = limits or KiteLimits()

    async def _call(self, fn, *args, **kwargs):
        await self.limits.api_slot()
        return await asyncio.to_thread(fn, *args, **kwargs)

    # --- price sources ---------------------------------------------------------------
    async def _instrument_tokens(self, symbols: list[str], exchange: str = "NSE") -> dict[str, int]:
        """tradingsymbol -> instrument_token, from a once-a-day cached instrument dump."""
        path = INSTRUMENT_CACHE.format(exchange=exchange)
        today = dt.date.today().isoformat()
        rows = None
        if os.path.exists(path):
            try:
                blob = json.load(open(path))
                if blob.get("fetched") == today:
                    rows = blob["rows"]
            except Exception:
                rows = None
        if rows is None:
            dump = await self._call(self.kite.instruments, exchange)
            rows = [{"tradingsymbol": r["tradingsymbol"],
                     "instrument_token": r["instrument_token"],
                     "segment": r.get("segment", ""),
                     "name": r.get("name", "")} for r in dump]
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                json.dump({"fetched": today, "rows": rows}, f)
        wanted = set(symbols)
        return {r["tradingsymbol"]: r["instrument_token"]
                for r in rows if r["tradingsymbol"] in wanted}

    async def _prices(self, holdings: list[dict], on_date: str, source: str) -> dict[str, float]:
        symbols = [h["symbol"] for h in holdings]
        if source == "holdings":
            # Run after the close and last_price IS the closing price — zero extra calls.
            return {h["symbol"]: float(h["last_price"]) for h in holdings}

        if source == "quote":
            keys = [f"{h['exchange']}:{h['symbol']}" for h in holdings]
            out: dict[str, float] = {}
            for i in range(0, len(keys), 200):          # Kite caps instruments per quote call
                chunk = await self._call(self.kite.quote_raw, keys[i:i + 200])
                for k, v in chunk.items():
                    close = (v.get("ohlc") or {}).get("close")
                    out[k.split(":", 1)[1]] = float(close if close else v["last_price"])
            return out

        if source == "historical":
            tokens = await self._instrument_tokens(symbols)
            day = dt.date.fromisoformat(on_date)
            out, missing = {}, []
            for sym in symbols:
                tok = tokens.get(sym)
                if not tok:
                    missing.append(sym)
                    continue
                candles = await self._call(self.kite.historical, tok, day, day, "day")
                if candles:
                    out[sym] = float(candles[-1]["close"])
                else:
                    missing.append(sym)
            # Zero candles across the whole book means the market never opened. Falling back
            # to live prices here would stamp today's marks onto a holiday and quietly
            # corrupt every downstream return.
            if not out:
                raise ValueError(
                    f"no daily candles for any holding on {on_date} — market holiday or "
                    f"weekend? Nothing was written.")
            if missing:
                log.warning("no %s close for %s — falling back to current price",
                            on_date, ", ".join(missing))
                for h in holdings:
                    out.setdefault(h["symbol"], float(h["last_price"]))
            return out

        raise ValueError(f"unknown price source: {source!r}")

    # --- collection ------------------------------------------------------------------
    async def collect(self, on_date: str | None = None, price_source: str = "holdings",
                      *, cash: float | None = None, allow_stale_cash: bool = False) -> dict:
        if not self.kite.is_authed():
            raise RuntimeError("Kite session expired — log in at / first (token is daily).")
        date = on_date or dt.date.today().isoformat()

        holdings = await self._call(self.kite.holdings)
        if cash is None:
            # kc.margins() reports the CURRENT balance — there is no historical margins
            # endpoint. Pairing today's cash with a past date's prices produces a NAV that
            # never existed, so backfills must state the cash explicitly.
            if date < dt.date.today().isoformat() and not allow_stale_cash:
                raise ValueError(
                    f"backfilling {date}: kc.margins() only knows today's cash. Pass "
                    f"cash=<balance on {date}> (CLI: --cash), or allow_stale_cash=True "
                    f"(CLI: --assume-current-cash) if you accept today's balance.")
            cash = float(await self._call(self.kite.available_cash))
        cash = float(cash)

        # Holdings quantities are also CURRENT. A backfill therefore reprices today's book
        # at a past date's closes; it is a reasonable seed for a young track record but is
        # NOT true history once the composition has changed. Recorded in holdings_json.
        backfilled = date < dt.date.today().isoformat()
        prices = await self._prices(holdings, date, price_source)

        positions, invested, excluded_value = [], 0.0, 0.0
        for h in holdings:
            px = float(prices.get(h["symbol"], h["last_price"]))
            value = h["quantity"] * px
            excluded = is_untouchable(h["symbol"])
            positions.append({
                "symbol": h["symbol"], "exchange": h["exchange"],
                "quantity": h["quantity"], "pledged_qty": h["pledged_qty"],
                "average_price": round(float(h["average_price"]), 4),
                "price": round(px, 4), "value": round(value, 2),
                "excluded": excluded,
            })
            if excluded and not C.INCLUDE_UNTOUCHABLE_IN_NAV:
                excluded_value += value
            else:
                invested += value

        holdings_json = {
            "as_of": date,
            "price_source": price_source,
            "include_untouchable_in_nav": C.INCLUDE_UNTOUCHABLE_IN_NAV,
            "excluded_value": round(excluded_value, 2),
            # True = today's book repriced at a past close, not observed history.
            "backfilled": backfilled,
            "positions": sorted(positions, key=lambda p: -p["value"]),
        }
        return {
            "date": date,
            "nav": round(invested + cash, 2),
            "invested": round(invested, 2),
            "cash": round(cash, 2),
            "holdings_json": json.dumps(holdings_json, separators=(",", ":")),
        }


async def run(on_date: str | None = None, *, price_source: str = "holdings",
              force: bool = False, db_path: str | None = None,
              kite: Kite | None = None, cash: float | None = None,
              allow_stale_cash: bool = False) -> dict:
    """Collect + persist one snapshot. Returns the snapshot plus the write outcome."""
    job = SnapshotJob(kite=kite)
    snap = await job.collect(on_date, price_source, cash=cash,
                             allow_stale_cash=allow_stale_cash)
    with db.connect(db_path) as conn:
        db.migrate(conn)
        outcome = db.save_snapshot(conn, snap, force=force)
        row = db.get_snapshot(conn, snap["date"])
        snap["index_value"] = row["index_value"]
        snap["outcome"] = outcome
    return snap


# --- CLI ------------------------------------------------------------------------------
def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="daily EOD portfolio snapshot")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--prices", default="holdings", choices=("holdings", "quote", "historical"))
    ap.add_argument("--force", action="store_true", help="overwrite an existing row")
    ap.add_argument("--cash", type=float, default=None,
                    help="cash balance on --date (required to backfill a past date)")
    ap.add_argument("--assume-current-cash", action="store_true",
                    help="backfill using today's cash balance (usually wrong — be sure)")
    ap.add_argument("--cashflow", type=float, default=None,
                    help="record an external flow (invest NEGATIVE, withdraw POSITIVE)")
    ap.add_argument("--cashflow-type", default="invest")
    ap.add_argument("--import-cashflows", default=None, metavar="CSV")
    ap.add_argument("--check-flows", action="store_true",
                    help="report days that look like unrecorded deposits/withdrawals")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    date = a.date or dt.date.today().isoformat()

    # cashflow bookkeeping first, so the index chain sees it on this run
    if a.cashflow is not None or a.import_cashflows:
        with db.connect(a.db) as conn:
            db.migrate(conn)
            with db.transaction(conn):
                if a.cashflow is not None:
                    added = db.record_cashflow(conn, date, a.cashflow, a.cashflow_type)
                    print(f"cashflow {a.cashflow:+,.2f} ({a.cashflow_type}) on {date}: "
                          f"{'recorded' if added else 'already present'}")
                if a.import_cashflows:
                    print("cashflow import:", db.import_cashflows_csv(conn, a.import_cashflows))
                db.rechain_index(conn)

    if a.check_flows:
        with db.connect(a.db) as conn:
            db.migrate(conn)
            hits = db.suggest_unrecorded_cashflows(conn)
        print(f"possible unrecorded cashflows: {len(hits)}")
        for h in hits:
            print(f"  {h['date']}  unexplained {h['unexplained']:+,.0f} "
                  f"(cash {h['cash_delta']:+,.0f}, invested {h['invested_delta']:+,.0f})")
        if not (a.cashflow is None and not a.import_cashflows):
            return
        if hits:
            print("  -> record real ones with --cashflow / --import-cashflows")
        return

    try:
        snap = asyncio.run(run(a.date, price_source=a.prices, force=a.force, db_path=a.db,
                               cash=a.cash, allow_stale_cash=a.assume_current_cash))
    except ValueError as exc:
        raise SystemExit(f"snapshot aborted: {exc}")
    print(f"DRY_RUN={C.DRY_RUN} (snapshots are read-only — no orders are ever placed)")
    print(f"snapshot {snap['date']}  [{snap['outcome']}]  prices={a.prices}")
    print(f"  nav        Rs {snap['nav']:>16,.2f}")
    print(f"  invested   Rs {snap['invested']:>16,.2f}")
    print(f"  cash       Rs {snap['cash']:>16,.2f}")
    print(f"  index      {snap['index_value']:>19.4f}")
    if snap["outcome"] == "unchanged":
        print("  (row already existed — values left untouched; pass --force to overwrite)")


if __name__ == "__main__":
    _main()
