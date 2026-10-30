"""The ``session`` bundle variant: the ``base`` calls plus ``upstream/`` for wave 6's extra reads (R8.3).

Called by ``replay_build.py --variant session`` (or ``all``). Writes, under the bundle:

``upstream/archive/nse/fo-bhavcopy/<d>.zip``, ``upstream/archive/nse/fo-ban-list/<d>.csv``
    **Real raw files**: the last ``FO_SESSIONS`` F&O bhavcopies and ban lists on or before the day,
    read (never modified) from the AWS-exit archive tarball
    (``/Volumes/T7-Work/baskfy-aws-exit-20261029/bundle/vol-baskfy-staging_baskfy-archive.tgz``,
    whose ``nse/`` tree is exactly the box's raw archive) and extracted once into a dedicated
    directory, ``rust/target/replay/sources/aws-exit-20261029/``. The tarball holds F&O files up
    to 28 Oct; a day after that has no F&O file of its own (a task for it misses the archive, asks
    the upstream, gets 404 — the "not published" path).
``upstream/kite/instruments/NFO.csv``
    Kite's NFO dump: the options from **the bed's own record of Kite's master** (``op_contract``,
    real tokens and tradingsymbols, those not expired on the day), and the futures — which
    ``op_contract`` does not keep — **rebuilt** from the newest F&O bhavcopy (Kite's grammar,
    ``NIFTY26NOVFUT``; synthetic tokens).
``upstream/kite/quote/<HHMM>.json`` for ``QUOTE_MINUTES``
    ``/quote`` answers at each minute: index options from the **recorded chain**
    (``op_chain_snapshot``: last, bid/ask, Kite's own depth, volume, OI as the collector stored them
    at or before the minute; the day's running open/high/low; the previous session's last as
    ``ohlc.close``); NIFTY 50, NIFTY BANK and INDIA VIX from the **recorded index minutes**
    (``op_index_minute``); every equity with a bar on the day and the benchmark indices Kite quotes
    by name (``KiteProvider.INDEX_QUOTE_SYMBOL``) **synthesized** from the day's bar
    (``last_price`` walked open→close on the tick, volume pro rata, the day's range for the range
    so far, five levels of depth, the previous raw close as ``ohlc.close``), and the index futures
    from the F&O bhavcopy the same way. Stamped with the minute.
``upstream/kite/historical/<token>/minute.json``
    The **recorded index minute candles** (``op_index_minute``) of the day and the session before,
    as Kite's ``historical_data`` answers them.
``upstream/kite/holdings.json``
    The **bed's broker-linked holdings** (``portfolio_holding`` rows with a ``broker_account_id``
    and a quantity — an SGB among them, which non-negotiable 7 makes untouchable), in Kite's field
    set, last price the day's raw close; a synthetic eight-name book only if the bed had none.
``upstream/kite/margins.json``, ``trades.json``, ``profile.json``
    Synthetic: a cash balance, no trades, a profile.

Not recorded (404 at the upstream, a provider error on both sides): Kite's historical minute
candles (``/instruments/historical``) and anything else.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import shutil
import subprocess
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_TGZ = Path("/Volumes/T7-Work/baskfy-aws-exit-20261029/bundle/vol-baskfy-staging_baskfy-archive.tgz")
SOURCES = ROOT / "rust" / "target" / "replay" / "sources" / "aws-exit-20261029"
FO_SESSIONS = 5
QUOTE_MINUTES = ("0935", "1315", "1529")
QUOTED_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
TICK = Decimal("0.05")
OPEN, CLOSE = dt.time(9, 15), dt.time(15, 30)
WEEKLY_MONTH = "123456789OND"


def _members() -> list[str]:
    listing = subprocess.run(["tar", "-tzf", str(ARCHIVE_TGZ)], capture_output=True, text=True, check=True)
    return [m for m in listing.stdout.splitlines()
            if m.startswith(("./nse/fo-bhavcopy/", "./nse/fo-ban-list/")) and not m.endswith("/")]


def raw_fo_files(day: dt.date) -> dict[str, Path]:
    """The archive keys (``nse/fo-bhavcopy/<d>.zip``, ...) of the last F&O sessions up to ``day``,
    extracted once into ``SOURCES`` (a directory of their own; nothing in it is executed)."""
    marker = SOURCES / ".members"
    if not marker.exists():
        if not ARCHIVE_TGZ.is_file():
            raise SystemExit(f"the AWS-exit archive {ARCHIVE_TGZ} is not there")
        members = _members()
        SOURCES.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "-xzf", str(ARCHIVE_TGZ), "-C", str(SOURCES), *members], check=True)
        marker.write_text("\n".join(members))
    files = {str(p.relative_to(SOURCES)): p for p in SOURCES.rglob("*") if p.is_file() and p.name != ".members"}
    out: dict[str, Path] = {}
    for kind in ("fo-bhavcopy", "fo-ban-list"):
        dated = sorted(
            (dt.date.fromisoformat(Path(k).stem), k) for k in files if k.startswith(f"nse/{kind}/")
        )
        for d, key in [x for x in dated if x[0] <= day][-FO_SESSIONS:]:
            out[key] = files[key]
    return out


def _tick(value: Decimal) -> Decimal:
    return ((value / TICK).quantize(Decimal(1), rounding=ROUND_HALF_EVEN) * TICK).quantize(Decimal("0.01"))


def _num(value: Decimal | None) -> float | int:
    if value is None:
        return 0
    f = float(value)
    return int(f) if f == int(f) and abs(f) < 1e15 and value == value.to_integral_value() and "." not in str(value) else f


def _strike(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def fo_frame(path: Path, on: dt.date) -> Any:
    """The production parser over a raw F&O zip (the same frame the fno ingest reads)."""
    from baskfy_providers.nse import _fo_bhavcopy_to_frame, _maybe_unzip, _read_csv

    return _fo_bhavcopy_to_frame(_read_csv(_maybe_unzip(path.read_bytes()), context="replay"), on)


def nfo_contracts(frame: Any) -> list[dict[str, Any]]:
    """Kite's NFO dump rows for every contract in the F&O frame, with synthetic tokens."""
    rows = frame.to_dicts()
    monthly: dict[tuple[str, int, int], dt.date] = {}
    for r in rows:
        k = (r["symbol"], r["expiry"].year, r["expiry"].month)
        monthly[k] = max(monthly.get(k, r["expiry"]), r["expiry"])
    out = []
    for r in rows:
        is_future = r["option_type"] in (None, "", "XX") or str(r["instrument"]).endswith("F")
        e: dt.date = r["expiry"]
        yy = e.strftime("%y")
        is_monthly = monthly[(r["symbol"], e.year, e.month)] == e
        if is_future:
            symbol = f"{r['symbol']}{yy}{e.strftime('%b').upper()}FUT"
            kind, segment, strike = "FUT", "NFO-FUT", Decimal(0)
        else:
            strike = r["strike"] or Decimal(0)
            stem = (f"{r['symbol']}{yy}{e.strftime('%b').upper()}" if is_monthly
                    else f"{r['symbol']}{yy}{WEEKLY_MONTH[e.month - 1]}{e.strftime('%d')}")
            symbol = f"{stem}{_strike(strike)}{r['option_type']}"
            kind, segment = r["option_type"], "NFO-OPT"
        out.append({"tradingsymbol": symbol, "name": r["symbol"], "expiry": e.isoformat(), "strike": strike,
                    "lot_size": r["lot_size"] or 0, "instrument_type": kind, "segment": segment, "row": r})
    out.sort(key=lambda c: (c["name"], c["expiry"], c["instrument_type"], c["strike"], c["tradingsymbol"]))
    for i, c in enumerate(out):
        c["exchange_token"] = 40000 + i
        c["instrument_token"] = (40000 + i) * 256 + 3
    return out


def recorded_options(day: dt.date, bed_rows: Any) -> list[dict[str, Any]]:
    """The bed's record of Kite's NFO option master (``op_contract``), live on ``day``."""
    rows = bed_rows(
        "SELECT instrument_token, tradingsymbol, underlying, expiry, strike, option_type, lot_size, tick_size "
        f"FROM op_contract WHERE NOT expired AND expiry >= DATE '{day.isoformat()}' "
        "ORDER BY underlying, expiry, option_type, strike, tradingsymbol"
    )
    return [{"tradingsymbol": r["tradingsymbol"], "name": r["underlying"], "expiry": r["expiry"],
             "strike": r["strike"], "lot_size": r["lot_size"], "instrument_type": r["option_type"],
             "segment": "NFO-OPT", "tick_size": r["tick_size"],
             "instrument_token": int(r["instrument_token"]),
             "exchange_token": int(r["instrument_token"]) // 256} for r in rows]


def nfo_csv(contracts: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    buf.write("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,"
              "lot_size,instrument_type,segment,exchange\n")
    for c in contracts:
        tick = _strike(c.get("tick_size") or TICK)
        buf.write(f"{c['instrument_token']},{c['exchange_token']},{c['tradingsymbol']},\"{c['name']}\",0.0,"
                  f"{c['expiry']},{_strike(c['strike']) if c['instrument_type'] != 'FUT' else '0.0'},{tick},"
                  f"{c['lot_size']},{c['instrument_type']},{c['segment']},NFO\n")
    return buf.getvalue().encode()


def _fraction(minute: str) -> Decimal:
    t = dt.time(int(minute[:2]), int(minute[2:]))
    span = (CLOSE.hour * 60 + CLOSE.minute) - (OPEN.hour * 60 + OPEN.minute)
    at = (t.hour * 60 + t.minute) - (OPEN.hour * 60 + OPEN.minute)
    return Decimal(max(0, min(span, at))) / Decimal(span)


def quote(token: int, o: Decimal, h: Decimal, low: Decimal, c: Decimal, prev: Decimal, volume: int,
          oi: int, day: dt.date, minute: str) -> dict[str, Any]:
    frac = _fraction(minute)
    last = _tick(o + (c - o) * frac)
    stamp = f"{day.isoformat()} {minute[:2]}:{minute[2:]}:00"
    traded = f"{day.isoformat()} {minute[:2]}:{minute[2:]}:00" if frac > 0 else f"{day.isoformat()} 09:15:00"
    depth = {
        "buy": [{"price": _num(_tick(last - TICK * (i + 1))), "quantity": 100 * (i + 1), "orders": i + 1}
                for i in range(5)],
        "sell": [{"price": _num(_tick(last + TICK * (i + 1))), "quantity": 100 * (i + 1), "orders": i + 1}
                 for i in range(5)],
    }
    return {
        "instrument_token": token, "timestamp": stamp, "last_trade_time": traded,
        "last_price": _num(last), "last_quantity": 1, "buy_quantity": 1500, "sell_quantity": 1500,
        "volume": int(Decimal(volume) * frac), "average_price": _num(_tick((o + last) / 2)),
        "oi": oi, "oi_day_high": oi, "oi_day_low": oi, "net_change": 0,
        "lower_circuit_limit": _num(_tick(prev * Decimal("0.8"))),
        "upper_circuit_limit": _num(_tick(prev * Decimal("1.2"))),
        "ohlc": {"open": _num(o), "high": _num(h), "low": _num(low), "close": _num(prev)},
        "depth": depth,
    }


def _ist(day: dt.date, hhmm: str) -> str:
    return f"TIMESTAMPTZ '{day.isoformat()} {hhmm[:2]}:{hhmm[2:]}:00+05:30'"


def chain_quotes(day: dt.date, minute: str, bed_rows: Any) -> dict[str, Any]:
    """Index-option quotes at ``minute`` from the recorded chain (``op_chain_snapshot``)."""
    names = ", ".join(repr(u) for u in QUOTED_UNDERLYINGS)
    start, at, prev_from = _ist(day, "0000"), _ist(day, minute), _ist(day - dt.timedelta(days=7), "0000")
    rows = bed_rows(
        "WITH day AS (SELECT * FROM op_chain_snapshot WHERE ts >= " + start + " AND ts <= " + at + "), "
        "latest AS (SELECT DISTINCT ON (instrument_token) * FROM day ORDER BY instrument_token, ts DESC), "
        "running AS (SELECT instrument_token, (array_agg(last ORDER BY ts) FILTER (WHERE last IS NOT NULL))[1] AS o, "
        "max(last) AS h, min(last) AS l FROM day GROUP BY instrument_token), "
        "prev AS (SELECT DISTINCT ON (instrument_token) instrument_token, last FROM op_chain_snapshot "
        "WHERE ts >= " + prev_from + " AND ts < " + start + " AND last IS NOT NULL ORDER BY instrument_token, ts DESC) "
        "SELECT k.tradingsymbol, x.instrument_token, x.ts, x.last, x.bid_qty, x.ask_qty, x.depth_json, x.volume, "
        "x.oi, r.o, r.h, r.l, p.last AS prev FROM latest x JOIN op_contract k USING (instrument_token) "
        "JOIN running r USING (instrument_token) LEFT JOIN prev p USING (instrument_token) "
        f"WHERE k.underlying IN ({names}) AND x.last IS NOT NULL ORDER BY k.tradingsymbol"
    )
    out: dict[str, Any] = {}
    for r in rows:
        ts = dt.datetime.fromisoformat(str(r["ts"])).astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
        stamp = ts.strftime("%Y-%m-%d %H:%M:%S")
        depth = r["depth_json"] or {"buy": [], "sell": []}
        depth = {side: [{"price": _num(Decimal(str(level["price"]))), "quantity": int(level["quantity"]),
                         "orders": int(level["orders"])} for level in depth.get(side, [])]
                 for side in ("buy", "sell")}
        last = r["last"]
        prev = r["prev"] if r["prev"] is not None else last
        out[f"NFO:{r['tradingsymbol']}"] = {
            "instrument_token": int(r["instrument_token"]), "timestamp": f"{day.isoformat()} {minute[:2]}:{minute[2:]}:00",
            "last_trade_time": stamp, "last_price": _num(last), "last_quantity": 0,
            "buy_quantity": int(r["bid_qty"] or 0), "sell_quantity": int(r["ask_qty"] or 0),
            "volume": int(r["volume"] or 0), "average_price": _num(last), "oi": int(r["oi"] or 0),
            "oi_day_high": int(r["oi"] or 0), "oi_day_low": int(r["oi"] or 0), "net_change": 0,
            "lower_circuit_limit": 0.05, "upper_circuit_limit": _num(_tick(prev * 3)),
            "ohlc": {"open": _num(r["o"]), "high": _num(r["h"]), "low": _num(r["l"]), "close": _num(prev)},
            "depth": depth,
        }
    return out


def index_minutes(day: dt.date, bed_rows: Any) -> dict[int, list[dict[str, Any]]]:
    """The recorded index minute bars (``op_index_minute``) of the day and the session before."""
    rows = bed_rows(
        "SELECT i.kite_token, i.symbol, m.ts, m.open, m.high, m.low, m.close FROM op_index_minute m "
        "JOIN instrument i ON i.id = m.instrument_id "
        f"WHERE m.ts >= {_ist(day - dt.timedelta(days=4), '0000')} AND m.ts < {_ist(day + dt.timedelta(days=1), '0000')} "
        "AND i.kite_token IS NOT NULL ORDER BY i.kite_token, m.ts"
    )
    out: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(int(r["kite_token"]), []).append(r)
    return out


def write_upstream(bundle: Path, day: dt.date, bed_rows: Any) -> None:
    from baskfy_providers.kite import KiteProvider

    up = bundle / "upstream"
    if up.exists():
        shutil.rmtree(up)
    raw = raw_fo_files(day)
    for key, path in raw.items():
        target = up / "archive" / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    kite = up / "kite"
    (kite / "instruments").mkdir(parents=True)
    (kite / "quote").mkdir(parents=True)
    newest = sorted(k for k in raw if k.startswith("nse/fo-bhavcopy/"))
    futures: list[dict[str, Any]] = []
    if newest:
        fo_day = dt.date.fromisoformat(Path(newest[-1]).stem)
        futures = [c for c in nfo_contracts(fo_frame(raw[newest[-1]], fo_day)) if c["instrument_type"] == "FUT"]
    options = recorded_options(day, bed_rows)
    (kite / "instruments" / "NFO.csv").write_bytes(nfo_csv(options + futures))

    d = day.isoformat()
    equities = bed_rows(
        "SELECT i.symbol, i.kite_token, coalesce(o.open_raw, o.open) AS o, coalesce(o.high_raw, o.high) AS h, "
        "coalesce(o.low_raw, o.low) AS l, o.close_raw AS c, o.volume_raw AS v, o.turnover, i.isin, "
        "(SELECT p.close_raw FROM ohlcv_daily p WHERE p.instrument_id = o.instrument_id AND p.date < o.date "
        " ORDER BY p.date DESC LIMIT 1) AS prev "
        f"FROM ohlcv_daily o JOIN instrument i ON i.id = o.instrument_id WHERE o.date = DATE '{d}' "
        "AND i.kite_token IS NOT NULL ORDER BY i.symbol, i.id"
    )
    by_name = {v: k for k, v in KiteProvider.INDEX_QUOTE_SYMBOL.items()}
    indices = bed_rows(
        "SELECT d.slug, s.level, s.change_abs FROM index_snapshot_daily s JOIN index_def d ON d.id = s.index_id "
        f"WHERE s.date = DATE '{d}' AND d.slug IN ({', '.join(repr(s) for s in by_name.values())}) ORDER BY d.slug"
    )
    minutes = index_minutes(day, bed_rows)
    hist = kite / "historical"
    for token, bars in minutes.items():
        target = hist / str(token) / "minute.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        candles = []
        for b in bars:
            ts = dt.datetime.fromisoformat(str(b["ts"])).astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
            candles.append([ts.strftime("%Y-%m-%dT%H:%M:%S+0530"), _num(b["open"]), _num(b["high"]),
                            _num(b["low"]), _num(b["close"]), 0])
        target.write_text(json.dumps(candles, separators=(",", ":")))
    for minute in QUOTE_MINUTES:
        answer: dict[str, Any] = {}
        seen: set[str] = set()
        for e in equities:
            key = f"NSE:{e['symbol']}"
            if key in seen:
                continue
            seen.add(key)
            prev = e["prev"] if e["prev"] is not None else e["c"]
            answer[key] = quote(int(e["kite_token"]), e["o"], e["h"], e["l"], e["c"], prev, int(e["v"] or 0), 0,
                                day, minute)
        # The recorded index minutes replace the bar-walked quote for the indices the collector kept.
        cutoff = f"{d} {minute[:2]}:{minute[2:]}"
        for token, bars in minutes.items():
            today = [b for b in bars if str(b["ts"]) and dt.datetime.fromisoformat(str(b["ts"])).astimezone(
                dt.timezone(dt.timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M") <= cutoff
                and dt.datetime.fromisoformat(str(b["ts"])).astimezone(
                    dt.timezone(dt.timedelta(hours=5, minutes=30))).date() == day]
            before = [b for b in bars if dt.datetime.fromisoformat(str(b["ts"])).astimezone(
                dt.timezone(dt.timedelta(hours=5, minutes=30))).date() < day]
            if not today:
                continue
            last, prev = today[-1]["close"], (before[-1]["close"] if before else today[0]["open"])
            key = f"NSE:{today[-1]['symbol']}"
            answer[key] = {
                "instrument_token": token, "timestamp": f"{cutoff}:00", "last_trade_time": f"{cutoff}:00",
                "last_price": _num(last), "last_quantity": 0, "buy_quantity": 0, "sell_quantity": 0,
                "volume": 0, "average_price": 0, "oi": 0, "oi_day_high": 0, "oi_day_low": 0, "net_change": 0,
                "lower_circuit_limit": 0, "upper_circuit_limit": 0,
                "ohlc": {"open": _num(today[0]["open"]), "high": _num(max(b["high"] for b in today)),
                         "low": _num(min(b["low"] for b in today)), "close": _num(prev)},
                "depth": {"buy": [], "sell": []},
            }
        for i, x in enumerate(indices):
            if f"NSE:{KiteProvider.INDEX_QUOTE_SYMBOL[x['slug']]}" in answer:
                continue  # quoted from its own bars above
            level = x["level"]
            prev = level - (x["change_abs"] or Decimal(0))
            q = quote(256265 + i, prev, max(prev, level), min(prev, level), level, prev, 0, 0, day, minute)
            q["depth"] = {"buy": [], "sell": []}
            answer[f"NSE:{KiteProvider.INDEX_QUOTE_SYMBOL[x['slug']]}"] = q
        for c in futures:
            if c["name"] not in QUOTED_UNDERLYINGS:
                continue
            r = c["row"]
            base = r["close"] or r["settle"] or Decimal(0)
            o, h, low, cl = (r[k] or base for k in ("open", "high", "low", "close"))
            answer[f"NFO:{c['tradingsymbol']}"] = quote(
                c["instrument_token"], o, h, low, cl, r["settle"] or cl, int(r["volume"] or 0),
                int(r["open_interest"] or 0), day, minute)
        answer.update(chain_quotes(day, minute, bed_rows))
        (kite / "quote" / f"{minute}.json").write_text(json.dumps(answer, separators=(",", ":")))

    held = bed_rows(
        "SELECT i.symbol, i.kite_token, i.isin, h.quantity, h.avg_price, o.close_raw AS c, "
        "(SELECT p.close_raw FROM ohlcv_daily p WHERE p.instrument_id = h.instrument_id AND p.date < DATE "
        f"'{d}' ORDER BY p.date DESC LIMIT 1) AS prev FROM portfolio_holding h JOIN instrument i ON i.id = h.instrument_id "
        f"LEFT JOIN ohlcv_daily o ON o.instrument_id = h.instrument_id AND o.date = DATE '{d}' "
        "WHERE h.broker_account_id IS NOT NULL AND h.quantity > 0 ORDER BY i.symbol, h.portfolio_id"
    )
    holdings = []
    for e in held:
        qty = int(e["quantity"])
        avg = Decimal(str(e["avg_price"]))
        last = e["c"] if e["c"] is not None else avg
        prev = e["prev"] if e["prev"] is not None else last
        holdings.append({
            "tradingsymbol": e["symbol"], "exchange": "NSE", "instrument_token": int(e["kite_token"] or 0),
            "isin": e["isin"] or "", "product": "CNC", "price": 0, "quantity": qty, "used_quantity": 0,
            "t1_quantity": 0, "realised_quantity": qty, "authorised_quantity": 0,
            "authorised_date": f"{d} 00:00:00", "opening_quantity": qty, "collateral_quantity": 0,
            "collateral_type": "", "discrepancy": False, "average_price": _num(avg.quantize(Decimal("0.0001"))),
            "last_price": _num(last), "close_price": _num(prev),
            "pnl": _num(((last - avg) * qty).quantize(Decimal("0.01"))),
            "day_change": _num((last - prev).quantize(Decimal("0.01"))),
            "day_change_percentage": _num(((last - prev) / prev * 100).quantize(Decimal("0.0001")) if prev else Decimal(0)),
        })
    if not holdings:
        book = sorted((e for e in equities if e["turnover"] is not None),
                      key=lambda e: (-e["turnover"], e["symbol"]))[:8]
        for rank, e in enumerate(book):
            qty = 10 * (rank + 1)
            avg = _tick(e["c"] * Decimal("0.9"))
            prev = e["prev"] if e["prev"] is not None else e["c"]
            holdings.append({
                "tradingsymbol": e["symbol"], "exchange": "NSE", "instrument_token": int(e["kite_token"]),
                "isin": e["isin"] or "", "product": "CNC", "price": 0, "quantity": qty, "used_quantity": 0,
                "t1_quantity": 5 if rank == 0 else 0, "realised_quantity": qty, "authorised_quantity": 0,
                "authorised_date": f"{d} 00:00:00", "opening_quantity": qty,
                "collateral_quantity": 3 if rank == 0 else 0, "collateral_type": "", "discrepancy": False,
                "average_price": _num(avg), "last_price": _num(e["c"]), "close_price": _num(prev),
                "pnl": _num(((e["c"] - avg) * qty).quantize(Decimal("0.01"))),
                "day_change": _num((e["c"] - prev).quantize(Decimal("0.01"))),
                "day_change_percentage": 0,
            })
    (kite / "holdings.json").write_text(json.dumps(holdings, separators=(",", ":")))
    cash = {"cash": 250000.0, "opening_balance": 250000.0, "live_balance": 250000.0, "collateral": 0.0,
            "intraday_payin": 0.0, "adhoc_margin": 0.0}
    (kite / "margins.json").write_text(json.dumps({
        "equity": {"enabled": True, "net": 250000.0, "available": cash,
                   "utilised": {"debits": 0.0, "exposure": 0.0, "m2m_realised": 0.0, "m2m_unrealised": 0.0,
                                "option_premium": 0.0, "payout": 0.0, "span": 0.0, "holding_sales": 0.0,
                                "turnover": 0.0}},
        "commodity": {"enabled": False, "net": 0.0, "available": {k: 0.0 for k in cash}, "utilised": {}},
    }, separators=(",", ":")))
    (kite / "trades.json").write_text("[]")
    (kite / "profile.json").write_text(json.dumps({
        "user_id": "RP0001", "user_type": "individual", "email": "replay@example.test", "user_name": "Replay",
        "user_shortname": "Replay", "broker": "ZERODHA", "exchanges": ["NSE", "NFO"], "products": ["CNC"],
        "order_types": [], "avatar_url": None, "meta": {"demat_consent": "physical"},
    }, separators=(",", ":")))
