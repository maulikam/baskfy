"""Breadth adapter — reads scoring.audit() WITHOUT touching any scoring factor or weight.

scoring.audit() supplies exactly one of the nine fields the regime engine needs
(`breadth_above_20dma`). The other eight are derived here, from the same DataFrame audit
was handed, so the number the engine consumes is byte-identical to the number scoring
reported while still carrying the provenance a decision must be reproducible from.

DENOMINATOR — read this before comparing two readings
scoring.audit computes `(u.close > u.ma_20).mean() * 100` over EVERY row. A NaN close or
NaN ma_20 makes that comparison False, so a suspended or data-missing symbol is counted as
BELOW its 20-DMA, not excluded. That is recorded as missing_policy="counted_as_below" on
every stored row. `coverage_pct` reports how much of the universe actually had usable data,
which is what lets the engine reject a reading that is technically present but hollow.

Changing the eligible universe changes universe_hash, so readings computed under different
definitions are never silently compared.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Mapping

import pandas as pd

from ..core.regime import BreadthReading
from . import db

CALCULATION_VERSION = "breadth/1.0.0"
MISSING_POLICY = "counted_as_below"      # matches scoring.audit's denominator exactly


def universe_hash(symbols) -> str:
    """Stable over content, not order. A different eligible set => a different hash."""
    blob = "|".join(sorted(str(s) for s in symbols))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def audit_run_id(universe_id: str, as_of: dt.date, uhash: str) -> str:
    blob = f"{universe_id}|{as_of.isoformat()}|{uhash}|{CALCULATION_VERSION}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def reading_from_audit(audit: Mapping[str, Any], scan: pd.DataFrame, *,
                       universe_id: str = "scan", as_of: dt.date | None = None
                       ) -> BreadthReading:
    """Build a fully-provenanced BreadthReading from scoring.audit() + its scan frame.

    `audit` is consumed as-is — its breadth value is authoritative and untouched.
    """
    if "breadth_above_20dma" not in audit:
        raise ValueError("audit dict has no 'breadth_above_20dma' — wrong source?")

    if as_of is None:
        dates = sorted(str(d) for d in audit.get("scan_date", []) or [])
        as_of = dt.date.fromisoformat(dates[-1][:10]) if dates else dt.date.today()

    eligible = int(len(scan))
    if {"close", "ma_20"} <= set(scan.columns):
        observed = int((scan["close"].notna() & scan["ma_20"].notna()).sum())
    else:
        observed = eligible
    coverage = (observed / eligible * 100.0) if eligible else 0.0

    uhash = universe_hash(scan["symbol"]) if "symbol" in scan.columns else universe_hash([])
    return BreadthReading(
        as_of_date=as_of,
        pct_above_20dma=float(audit["breadth_above_20dma"]),
        eligible_count=eligible,
        observed_count=observed,
        coverage_pct=round(coverage, 4),
        universe_id=universe_id,
        universe_hash=uhash,
        audit_run_id=audit_run_id(universe_id, as_of, uhash),
        calculation_version=CALCULATION_VERSION,
    )


# =====================================================================================
# persistence
# =====================================================================================
def save_reading(conn, reading: BreadthReading, *,
                 missing_policy: str = MISSING_POLICY) -> str:
    """Idempotent upsert keyed (as_of_date, universe_id). Returns 'inserted'|'updated'."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    existing = conn.execute(
        "SELECT 1 FROM breadth_readings WHERE as_of_date=? AND universe_id=?",
        (reading.as_of_date.isoformat(), reading.universe_id)).fetchone()
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO breadth_readings(as_of_date, universe_id, pct_above_20dma,
                   eligible_count, observed_count, coverage_pct, universe_hash,
                   audit_run_id, calculation_version, missing_policy, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(as_of_date, universe_id) DO UPDATE SET
                   pct_above_20dma=excluded.pct_above_20dma,
                   eligible_count=excluded.eligible_count,
                   observed_count=excluded.observed_count,
                   coverage_pct=excluded.coverage_pct,
                   universe_hash=excluded.universe_hash,
                   audit_run_id=excluded.audit_run_id,
                   calculation_version=excluded.calculation_version,
                   missing_policy=excluded.missing_policy""",
            (reading.as_of_date.isoformat(), reading.universe_id, reading.pct_above_20dma,
             reading.eligible_count, reading.observed_count, reading.coverage_pct,
             reading.universe_hash, reading.audit_run_id, reading.calculation_version,
             missing_policy, now))
    return "updated" if existing else "inserted"


def _row_to_reading(r) -> BreadthReading:
    return BreadthReading(
        as_of_date=dt.date.fromisoformat(r["as_of_date"]),
        pct_above_20dma=r["pct_above_20dma"], eligible_count=r["eligible_count"],
        observed_count=r["observed_count"], coverage_pct=r["coverage_pct"],
        universe_id=r["universe_id"], universe_hash=r["universe_hash"],
        audit_run_id=r["audit_run_id"], calculation_version=r["calculation_version"])


def latest_reading(conn, *, universe_id: str = "scan",
                   on_or_before: dt.date | None = None) -> BreadthReading | None:
    sql = "SELECT * FROM breadth_readings WHERE universe_id=?"
    args: list = [universe_id]
    if on_or_before:
        sql += " AND as_of_date <= ?"
        args.append(on_or_before.isoformat())
    sql += " ORDER BY as_of_date DESC LIMIT 1"
    row = conn.execute(sql, args).fetchone()
    return _row_to_reading(row) if row else None


def history(conn, *, universe_id: str = "scan") -> list[BreadthReading]:
    return [_row_to_reading(r) for r in conn.execute(
        "SELECT * FROM breadth_readings WHERE universe_id=? ORDER BY as_of_date",
        (universe_id,))]


def comparable(a: BreadthReading, b: BreadthReading) -> bool:
    """False when two readings came from different universe definitions or versions.

    The backtest must not chart these together without disclosure.
    """
    return (a.universe_hash == b.universe_hash
            and a.calculation_version == b.calculation_version
            and a.universe_id == b.universe_id)


def distinct_universes(conn, *, universe_id: str = "scan") -> list[dict]:
    """Every universe definition the stored history spans — a disclosure aid."""
    return [dict(r) for r in conn.execute(
        "SELECT universe_hash, calculation_version, COUNT(*) n, "
        "       MIN(as_of_date) first, MAX(as_of_date) last "
        "FROM breadth_readings WHERE universe_id=? "
        "GROUP BY universe_hash, calculation_version ORDER BY first", (universe_id,))]
