"""Benchmark series: PRI from Kite (live) and TRI from niftyindices.com CSVs (authoritative).

WHY BOTH
Kite's index instruments carry PRICE-RETURN levels only — dividends are not reinvested.
Comparing a portfolio (which receives dividends) against a PRI index flatters the
portfolio by roughly the index dividend yield, ~1-1.5%/yr on Nifty 500. So:

    TRI (CSV)  -> authoritative for every reported metric
    PRI (Kite) -> live/intraday convenience, always labelled as such

The benchmark table stores both per (index_name, date): `close` = PRI, `tri` = TRI.
Upserts fill one column without clobbering the other, so the two ingestion paths can run
independently and in any order.

Tradingsymbols drift ("NIFTY 500", "NIFTY200 MOMENTM 30"), so index tokens are always
resolved against the LIVE instrument dump at runtime rather than hardcoded.

CLI:
    python -m app.analytics.benchmark --list                      # discover index names
    python -m app.analytics.benchmark --pri "NIFTY 500" --from 2026-01-01
    python -m app.analytics.benchmark --tri data/uploads/nifty500_tri.csv --name "NIFTY 500"
    python -m app.analytics.benchmark --show "NIFTY 500"
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import json
import logging
import os

import pandas as pd

from .. import config as C
from ..core.ratelimit import KiteLimits
from ..kite_client import Kite
from . import db

log = logging.getLogger("benchmark")

INSTRUMENT_CACHE = "data/.instruments_indices.json"
INDEX_SEGMENTS = ("INDICES", "NSE-INDICES")

PRI, TRI = "PRI", "TRI"

# Convenience aliases -> the tradingsymbol as it appears in the dump. Verified at runtime;
# a miss falls back to fuzzy matching rather than failing outright.
# All verified against the live dump on 2026-08-14. Note NIFTY200MOMENTM30 carries NO
# spaces, unlike every other Nifty index — do not "tidy" it to "NIFTY200 MOMENTM 30".
COMMON_INDICES = {
    "nifty500": "NIFTY 500",
    "nifty50": "NIFTY 50",
    "momentum30": "NIFTY200MOMENTM30",
    "momentum50": "NIFTY500MOMENTM50",
    "midcap150": "NIFTY MIDCAP 150",
    "smallcap250": "NIFTY SMLCAP 250",
}


# =====================================================================================
# instrument resolution
# =====================================================================================
async def _index_dump(kite: Kite, limits: KiteLimits, *, refresh: bool = False) -> list[dict]:
    """Index instruments from the daily dump, cached to disk (one call per day)."""
    today = dt.date.today().isoformat()
    if not refresh and os.path.exists(INSTRUMENT_CACHE):
        try:
            blob = json.load(open(INSTRUMENT_CACHE))
            if blob.get("fetched") == today:
                return blob["rows"]
        except Exception:
            pass

    await limits.api_slot()
    dump = await asyncio.to_thread(kite.kc.instruments)
    rows = [{"tradingsymbol": r["tradingsymbol"],
             "instrument_token": r["instrument_token"],
             "segment": r.get("segment", ""),
             "exchange": r.get("exchange", ""),
             "name": r.get("name", "")}
            for r in dump if r.get("segment") in INDEX_SEGMENTS]
    os.makedirs(os.path.dirname(INSTRUMENT_CACHE) or ".", exist_ok=True)
    with open(INSTRUMENT_CACHE, "w") as f:
        json.dump({"fetched": today, "rows": rows}, f)
    log.info("cached %d index instruments", len(rows))
    return rows


async def list_indices(kite: Kite | None = None, *, contains: str = "") -> list[dict]:
    """Every index instrument Kite exposes, optionally filtered. Use this to verify a
    tradingsymbol before wiring it into a config — the strings do change."""
    k = kite or Kite()
    rows = await _index_dump(k, KiteLimits())
    if contains:
        needle = contains.upper()
        rows = [r for r in rows if needle in r["tradingsymbol"].upper()]
    return sorted(rows, key=lambda r: r["tradingsymbol"])


async def resolve_index_token(index_name: str, kite: Kite | None = None) -> dict:
    """Resolve a tradingsymbol (or alias) to its instrument token against the live dump."""
    k = kite or Kite()
    rows = await _index_dump(k, KiteLimits())
    wanted = COMMON_INDICES.get(index_name.lower().replace(" ", ""), index_name).upper()

    exact = [r for r in rows if r["tradingsymbol"].upper() == wanted]
    if exact:
        return exact[0]

    # tolerate spacing/abbreviation drift before giving up
    squashed = wanted.replace(" ", "")
    loose = [r for r in rows if r["tradingsymbol"].upper().replace(" ", "") == squashed]
    if loose:
        log.warning("index %r matched loosely as %r", index_name, loose[0]["tradingsymbol"])
        return loose[0]

    partial = [r for r in rows if squashed in r["tradingsymbol"].upper().replace(" ", "")]
    if len(partial) == 1:
        log.warning("index %r matched partially as %r", index_name, partial[0]["tradingsymbol"])
        return partial[0]

    hint = ", ".join(sorted(r["tradingsymbol"] for r in partial)[:10]) or \
           ", ".join(sorted(r["tradingsymbol"] for r in rows)[:10])
    raise LookupError(f"index {index_name!r} not found in the live dump. Candidates: {hint}")


# =====================================================================================
# PRI — live from Kite
# =====================================================================================
async def kite_index_series(index_name: str, start: str, end: str | None = None,
                            kite: Kite | None = None) -> pd.Series:
    """Daily PRICE-RETURN closes for an index. NOT total-return — dividends excluded."""
    k = kite or Kite()
    if not k.is_authed():
        raise RuntimeError("Kite session expired — log in at / first (token is daily).")
    limits = KiteLimits()
    inst = await resolve_index_token(index_name, k)
    frm = dt.date.fromisoformat(start)
    to = dt.date.fromisoformat(end) if end else dt.date.today()

    out: dict[pd.Timestamp, float] = {}
    # historical_data caps the span per call for daily candles; walk it in chunks.
    step = dt.timedelta(days=1800)
    cursor = frm
    while cursor <= to:
        chunk_end = min(cursor + step, to)
        await limits.api_slot()
        candles = await asyncio.to_thread(
            k.kc.historical_data, inst["instrument_token"], cursor, chunk_end, "day")
        for c in candles:
            out[pd.Timestamp(c["date"]).tz_localize(None).normalize()] = float(c["close"])
        cursor = chunk_end + dt.timedelta(days=1)

    s = pd.Series(out, name=inst["tradingsymbol"]).sort_index()
    s.attrs["series_type"] = PRI
    s.attrs["index_name"] = inst["tradingsymbol"]
    return s


# =====================================================================================
# TRI — from niftyindices.com CSV
# =====================================================================================
_TRI_DATE_FORMATS = ("%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %B %Y")


def _parse_tri_date(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in _TRI_DATE_FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return pd.to_datetime(raw, dayfirst=True).date().isoformat()
    except Exception:
        return None


def tri_csv_loader(path: str) -> pd.DataFrame:
    """Parse a niftyindices.com "Total Returns Index Values" export.

    Expected columns: Date, Total Returns Index [, Net Total Return Index].
    The gross "Total Returns Index" is used as `tri`; the net series is kept alongside
    when present. Column names and date formats vary between exports, so both are
    matched leniently.
    """
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"{path}: empty or unreadable CSV")
        cols = {(c or "").strip().lower(): c for c in reader.fieldnames}

        def find(*needles: str) -> str | None:
            for key, original in cols.items():
                if all(n in key for n in needles):
                    return original
            return None

        date_col = find("date")
        gross_col = find("total", "return") if not find("net", "total") else None
        # prefer an explicit gross column when both exist
        for key, original in cols.items():
            if "total" in key and "return" in key and "net" not in key:
                gross_col = original
                break
        net_col = find("net", "total")
        if not date_col or not (gross_col or net_col):
            raise ValueError(
                f"{path}: expected Date + 'Total Returns Index' columns, got "
                f"{reader.fieldnames}")

        for line_no, row in enumerate(reader, start=2):
            # csv.DictReader parks surplus fields under the None key. That happens when a
            # number containing a thousands separator was exported UNQUOTED, in which case
            # "12,345.67" silently reads as 12.0 — a 1000x error in a benchmark level.
            # Refuse the file rather than ingest a corrupted series.
            extra = row.get(None)
            if extra and any(str(v).strip() for v in extra):
                raise ValueError(
                    f"{path}: line {line_no} has more fields than the header — a value "
                    f"with a thousands separator is probably unquoted. Re-export or quote "
                    f"it; parsing on would silently mis-read the levels.")
            date = _parse_tri_date(row.get(date_col, ""))
            if not date:
                continue

            def num(col: str | None) -> float | None:
                if not col:
                    return None
                raw = (row.get(col) or "").replace(",", "").strip()
                try:
                    return float(raw)
                except ValueError:
                    return None

            gross, net = num(gross_col), num(net_col)
            if gross is None and net is None:
                continue
            rows.append({"date": date, "tri": gross if gross is not None else net,
                         "net_tri": net})

    if not rows:
        raise ValueError(f"{path}: no parseable rows")
    df = pd.DataFrame(rows).drop_duplicates(subset="date").sort_values("date")
    return df.reset_index(drop=True)


# =====================================================================================
# persistence
# =====================================================================================
def upsert_benchmark(conn, index_name: str, rows, *, kind: str) -> dict:
    """Write a PRI or TRI series into benchmark(index_name, date, close, tri).

    Fills only its own column: COALESCE keeps whatever the other ingestion path wrote,
    so PRI and TRI can be loaded independently and in any order.
    """
    if kind not in (PRI, TRI):
        raise ValueError(f"kind must be {PRI} or {TRI}, got {kind!r}")
    col = "close" if kind == PRI else "tri"
    other = "tri" if kind == PRI else "close"

    if isinstance(rows, pd.Series):
        pairs = [(d.date().isoformat() if hasattr(d, "date") else str(d), float(v))
                 for d, v in rows.items() if pd.notna(v)]
    elif isinstance(rows, pd.DataFrame):
        value_col = "tri" if kind == TRI else "close"
        if value_col not in rows.columns:
            value_col = [c for c in rows.columns if c != "date"][0]
        pairs = [(str(r["date"]), float(r[value_col])) for _, r in rows.iterrows()
                 if pd.notna(r[value_col])]
    else:
        pairs = [(str(d), float(v)) for d, v in rows]

    sql = (f"INSERT INTO benchmark(index_name, date, {col}) VALUES(?,?,?) "
           f"ON CONFLICT(index_name, date) DO UPDATE SET "
           f"  {col}=excluded.{col}, {other}=COALESCE(benchmark.{other}, NULL)")
    with db.transaction(conn):
        conn.executemany(sql, [(index_name, d, v) for d, v in pairs])
    return {"index_name": index_name, "kind": kind, "rows": len(pairs),
            "first": pairs[0][0] if pairs else None,
            "last": pairs[-1][0] if pairs else None}


def benchmark_series(conn, index_name: str, *, prefer: str = TRI) -> pd.Series:
    """Read a stored benchmark back.

    `prefer=TRI` returns total-return levels and falls back to PRI only if no TRI rows
    exist. The returned Series carries .attrs['series_type'] so callers can label the
    chart or metric with what was actually used — never assume.
    """
    rows = conn.execute(
        "SELECT date, close, tri FROM benchmark WHERE index_name=? ORDER BY date",
        (index_name,)).fetchall()
    if not rows:
        return pd.Series(dtype=float)

    df = pd.DataFrame([dict(r) for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    order = [TRI, PRI] if prefer == TRI else [PRI, TRI]
    for kind in order:
        col = "tri" if kind == TRI else "close"
        s = df.set_index("date")[col].dropna()
        if not s.empty:
            s.name = index_name
            s.attrs["series_type"] = kind
            s.attrs["index_name"] = index_name
            if kind == PRI and prefer == TRI:
                log.warning("%s: no TRI rows — falling back to PRI (understates the "
                            "benchmark by ~the dividend yield)", index_name)
            return s
    return pd.Series(dtype=float)


def available_benchmarks(conn) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT index_name, COUNT(*) n, SUM(close IS NOT NULL) pri, SUM(tri IS NOT NULL) tri,"
        "       MIN(date) first, MAX(date) last "
        "FROM benchmark GROUP BY index_name ORDER BY index_name").fetchall()
    return pd.DataFrame([dict(r) for r in rows])


# =====================================================================================
# CLI
# =====================================================================================
def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="benchmark ingestion (PRI live / TRI CSV)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--list", nargs="?", const="", metavar="SUBSTR",
                    help="list index instruments from the live dump")
    ap.add_argument("--pri", metavar="INDEX", help='e.g. "NIFTY 500"')
    ap.add_argument("--from", dest="start", default="2020-01-01")
    ap.add_argument("--to", dest="end", default=None)
    ap.add_argument("--tri", metavar="CSV", help="niftyindices.com TRI export")
    ap.add_argument("--name", metavar="INDEX", help="index_name to store the TRI under")
    ap.add_argument("--show", metavar="INDEX", help="summarise a stored series")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if a.list is not None:
        rows = asyncio.run(list_indices(contains=a.list))
        print(f"{len(rows)} index instruments"
              + (f" matching {a.list!r}" if a.list else ""))
        for r in rows[:80]:
            print(f"  {r['tradingsymbol']:<32} token={r['instrument_token']:<12} {r['segment']}")
        return

    if a.tri:
        if not a.name:
            raise SystemExit("--tri requires --name (the index_name to store it under)")
        df = tri_csv_loader(a.tri)
        with db.connect(a.db) as conn:
            db.migrate(conn)
            res = upsert_benchmark(conn, a.name, df, kind=TRI)
        print(f"TRI  {res['index_name']}: {res['rows']} rows  {res['first']} -> {res['last']}")

    if a.pri:
        s = asyncio.run(kite_index_series(a.pri, a.start, a.end))
        with db.connect(a.db) as conn:
            db.migrate(conn)
            res = upsert_benchmark(conn, s.attrs["index_name"], s, kind=PRI)
        print(f"PRI  {res['index_name']}: {res['rows']} rows  {res['first']} -> {res['last']}"
              f"   (price-return only — TRI is authoritative)")

    if a.show:
        with db.connect(a.db) as conn:
            db.migrate(conn)
            s = benchmark_series(conn, a.show)
            if s.empty:
                print(f"no rows for {a.show!r}")
                return
            print(f"{a.show}: {len(s)} rows using {s.attrs['series_type']}  "
                  f"{s.index[0].date()} -> {s.index[-1].date()}  "
                  f"level {s.iloc[0]:.2f} -> {s.iloc[-1]:.2f}")

    if not any((a.pri, a.tri, a.show)):
        with db.connect(a.db) as conn:
            db.migrate(conn)
            print(available_benchmarks(conn).to_string(index=False) or "no benchmarks stored")


if __name__ == "__main__":
    _main()
