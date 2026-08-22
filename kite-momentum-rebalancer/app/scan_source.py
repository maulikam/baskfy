"""Generate a weekly scan from the screener's database — M13 §2, the desk's half of the cord cut.

The desk has been fed a CSV exported by hand from momoindiascreener.in every week. This reads the
merged engine's bars instead and produces the same thirty columns through
:mod:`baskfy_core.momentum_scan`, so `/analyze` can build a plan without anyone visiting a website.

WHY THIS READS A DIFFERENT DATABASE THAN THE DESK WRITES
---------------------------------------------------------
The desk's own store is still SQLite and stays that way until M19 flips it. This reads the
*screener's* Postgres, which is a separate database the desk does not own and only ever reads.
That separation is deliberate: cutting the CSV cord and moving the desk's own storage are two
changes, and doing them in one step would leave no way to tell which one broke something.

WHAT IT DOES NOT DO
-------------------
It does not become the default. `/analyze` still takes an upload, and the generated path is opt-in,
because the delta table in `reconciliation/DESK-PARITY.md` is not empty: forty-one of the scan's
symbols carry unadjusted corporate actions, and fourteen of those are rejected outright by the
`far_from_high` filter — names the desk would otherwise have been able to trade. Until
`NEEDS-MAULIK.md` item 4 is resolved, a generated scan is a smaller universe than an uploaded one,
and the desk is told so rather than left to find out from a short plan.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import pandas as pd
import polars as pl

from baskfy_core import momentum_scan as ms

from . import config as C
from . import scoring as _scoring

log = logging.getLogger(__name__)

#: The screener's database. Read-only from here, always.
SCREENER_DSN = os.getenv(
    "SCREENER_DATABASE_URL", "postgresql://baskfy:baskfy@localhost:5433/baskfy"
)

_BARS_SQL = """
select i.symbol, b.instrument_id, b.date, b.open, b.high, b.low,
       b.close, b.volume, b.close_raw, b.volume_raw
  from ohlcv_daily b
  join instrument i on i.id = b.instrument_id
 where b.date <= %(as_of)s
   and (%(symbols)s::text[] is null or i.symbol = any(%(symbols)s))
 order by i.symbol, b.date
"""

_DAYS_SQL = "select distinct date from ohlcv_daily where date <= %(as_of)s order by date"


def _fetch(as_of: dt.date, symbols: list[str] | None) -> tuple[pl.DataFrame, list[dt.date]]:
    import psycopg  # noqa: PLC0415 — a Postgres driver is not needed to import this module

    with psycopg.connect(SCREENER_DSN) as conn, conn.cursor() as cur:
        cur.execute(_BARS_SQL, {"as_of": as_of, "symbols": symbols})
        rows = cur.fetchall()
        cur.execute(_DAYS_SQL, {"as_of": as_of})
        days = [r[0] for r in cur.fetchall()]

    def column(index: int) -> list[float]:
        return [float(r[index]) if r[index] is not None else 0.0 for r in rows]

    frame = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "instrument_id": [r[1] for r in rows],
            "date": [r[2] for r in rows],
            "open": column(3),
            "high": column(4),
            "low": column(5),
            "close": column(6),
            "volume": column(7),
            "close_raw": column(8),
            "volume_raw": column(9),
        }
    )
    return frame, days


def generate(
    as_of: dt.date, carried: pl.DataFrame, symbols: list[str] | None = None
) -> ms.MomentumScan:
    """Build one scan for `as_of`.

    ``carried`` supplies the four columns no bar series can produce — see
    :data:`baskfy_core.momentum_scan.CARRIED_COLUMNS`. It is required rather than defaulted for the
    reason M15 made ``tradeable`` required: a scan silently missing `beta` still scores, ranks and
    trades, just differently, and nothing says so.
    """
    bars, trading_days = _fetch(as_of, symbols)
    if bars.is_empty():
        raise ValueError(
            f"no bars on or before {as_of} in the screener's database. "
            f"Run `make backfill` there, or upload a scan CSV instead."
        )
    scan = ms.build(bars, as_of, trading_days, cfg=C, carried=carried)
    if scan.suspect_symbols:
        log.warning(
            "generated scan %s: %d of %d symbols carry an unadjusted corporate action "
            "(NEEDS-MAULIK item 4). Their highs and returns are wrong, and the far_from_high "
            "filter will reject some of them outright.",
            scan.screen_run_id,
            len(scan.suspect_symbols),
            scan.frame.height,
        )
    return scan


def as_desk_frame(scan: ms.MomentumScan) -> pd.DataFrame:
    """Cross into pandas exactly where the desk's scoring begins, and nowhere earlier."""
    frame = pd.DataFrame(scan.frame.to_dicts())
    missing = [c for c in _scoring.REQUIRED if c not in frame.columns]
    if missing:  # the same check `load_scan` makes on an upload
        raise ValueError(f"Generated scan missing columns: {missing}")
    return frame.drop_duplicates(subset="symbol").reset_index(drop=True)
