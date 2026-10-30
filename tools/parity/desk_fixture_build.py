"""Build the replay-Kite fixture ``desk_diff.py`` serves both desks (DECISIONS-RS R8.3).

Synthetic and deterministic, from the regression corpus's scan CSVs (``data/uploads/*.csv`` in
the main tree — **read only**, never written) — never from ``portfolio.db``, never from a live
account. Writes ``tools/parity/desk_fixtures/<name>/kite.json`` and ``instruments/NSE.csv``:

* ``quotes``: every symbol in every scan, ``last_price`` = the scan's ``close`` × 1.01 (rounded
  to the 0.05 tick), the day's OHLC around it, a volume; instrument tokens 100001, 100002, … in
  sorted-symbol order.
* ``portfolio.holdings``: the first eight symbols of ``sample_scan.csv`` with fixed quantities —
  some carrying ``t1_quantity`` and ``collateral_quantity`` (non-negotiable 2 and 3) — plus the
  untouchable ``SGBDE31III`` (non-negotiable 7) and one name absent from every scan.
* ``portfolio.positions``: one CNC day buy (the same-day-buy rule in ``Kite.holdings``).
* ``user.margins``: ₹5,00,000 live balance; ``order.margins.basket``: a fixed total.
* ``gtt``, ``orders``, ``trades``: empty; ``user.profile``: a replay user; ``session.authed``.
* ``historical``: 260 business days of daily candles per held symbol, from a fixed walk.

    python3 tools/parity/desk_fixture_build.py --uploads <main>/kite-momentum-rebalancer/data/uploads \\
        --out tools/parity/desk_fixtures/default
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path
from typing import Any

UNTOUCHABLE = "SGBDE31III"
OFF_SCAN = "ZZREPLAY"


def tick(x: float) -> float:
    return round(round(x / 0.05) * 0.05, 2)


def scans(uploads: Path) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    for path in sorted(uploads.glob("*.csv")):
        with path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        if rows and "symbol" in rows[0] and "close" in rows[0]:
            out[path.name] = rows
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uploads", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    found = scans(Path(args.uploads))
    closes: dict[str, float] = {}
    for rows in found.values():
        for r in rows:
            try:
                closes.setdefault(r["symbol"], float(r["close"]))
            except (TypeError, ValueError):
                continue
    closes.setdefault(UNTOUCHABLE, 6200.0)
    closes.setdefault(OFF_SCAN, 250.0)
    symbols = sorted(closes)
    token = {s: 100001 + i for i, s in enumerate(symbols)}

    quotes: dict[str, Any] = {}
    for s in symbols:
        c = closes[s]
        last = tick(c * 1.01)
        quotes[f"NSE:{s}"] = {
            "instrument_token": token[s], "last_price": last, "volume": 100000 + token[s] % 977,
            "net_change": round(last - c, 2),
            "ohlc": {"open": tick(c * 1.002), "high": tick(c * 1.02), "low": tick(c * 0.99), "close": c},
        }

    sample = found.get("sample_scan.csv") or next(iter(found.values()))
    held = [r["symbol"] for r in sample[:8]]
    holdings = []
    for i, s in enumerate(held):
        holdings.append({
            "tradingsymbol": s, "exchange": "NSE", "instrument_token": token[s], "isin": f"INE{i:06d}R01",
            "product": "CNC", "quantity": 10 * (i + 1), "t1_quantity": 3 if i % 3 == 1 else 0,
            "collateral_quantity": 5 if i % 4 == 2 else 0, "collateral_type": "equity" if i % 4 == 2 else "",
            "average_price": tick(closes[s] * 0.9), "last_price": quotes[f"NSE:{s}"]["last_price"],
            "close_price": closes[s], "pnl": 0.0, "day_change": 0.0, "day_change_percentage": 0.0,
        })
    for s, q in ((UNTOUCHABLE, 4), (OFF_SCAN, 25)):
        holdings.append({
            "tradingsymbol": s, "exchange": "NSE", "instrument_token": token[s], "isin": "IN0020230085",
            "product": "CNC", "quantity": q, "t1_quantity": 0, "collateral_quantity": 0,
            "collateral_type": "", "average_price": tick(closes[s] * 0.95),
            "last_price": quotes[f"NSE:{s}"]["last_price"], "close_price": closes[s], "pnl": 0.0,
            "day_change": 0.0, "day_change_percentage": 0.0,
        })
    day_buy = sample[8]["symbol"] if len(sample) > 8 else held[0]
    positions = {"net": [{
        "tradingsymbol": day_buy, "exchange": "NSE", "instrument_token": token[day_buy], "product": "CNC",
        "quantity": 7, "overnight_quantity": 0, "average_price": quotes[f"NSE:{day_buy}"]["last_price"],
        "last_price": quotes[f"NSE:{day_buy}"]["last_price"], "pnl": 0.0,
    }], "day": []}

    historical: dict[str, Any] = {}
    start = dt.date(2025, 10, 30)
    for s in [*held, day_buy]:
        days, d, price = [], start, closes[s] * 0.8
        step = 0
        while len(days) < 260:
            if d.weekday() < 5:
                step += 1
                drift = ((token[s] * 7 + step * 13) % 21 - 10) / 1000.0
                o = tick(price)
                price = max(1.0, price * (1 + drift + 0.0008))
                c = tick(price)
                days.append([f"{d.isoformat()}T00:00:00+0530", o, tick(max(o, c) * 1.01), tick(min(o, c) * 0.99), c,
                             50000 + (step * 37) % 9000])
            d += dt.timedelta(days=1)
        historical[str(token[s])] = {"day": days}

    fixture = {
        "session": {"authed": True},
        "routes": {
            "user.profile": {"data": {"user_id": "REPLAY", "user_name": "Replay Desk", "email": "replay@example.invalid",
                                      "broker": "ZERODHA", "exchanges": ["NSE", "BSE"], "products": ["CNC"]}},
            "portfolio.holdings": {"data": holdings},
            "portfolio.positions": {"data": positions},
            "user.margins": {"data": {"equity": {"enabled": True, "net": 500000.0,
                                                 "available": {"live_balance": 500000.0, "cash": 500000.0,
                                                               "collateral": 0.0, "opening_balance": 500000.0},
                                                 "utilised": {"debits": 0.0}}}},
            "order.margins.basket": {"data": {"initial": {"total": 120000.0}, "final": {"total": 118500.0}, "orders": []}},
            "gtt": {"data": []},
            "orders": {"data": []},
            "trades": {"data": []},
        },
        "quotes": quotes,
        "historical": historical,
    }
    out = Path(args.out)
    (out / "instruments").mkdir(parents=True, exist_ok=True)
    (out / "kite.json").write_text(json.dumps(fixture, indent=1) + "\n")
    with (out / "instruments" / "NSE.csv").open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["instrument_token", "exchange_token", "tradingsymbol", "name", "last_price", "expiry",
                    "strike", "tick_size", "lot_size", "instrument_type", "segment", "exchange"])
        for s in symbols:
            w.writerow([token[s], token[s] // 256, s, s, 0, "", 0, 0.05, 1, "EQ", "NSE", "NSE"])
    print(f"{len(symbols)} symbols from {len(found)} scans, {len(holdings)} holdings -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
