"""Where the cash-band breadth number comes from — M14 §1.

The desk's cash bands key off "percentage of the universe above its 20-day moving average", and
that number was computed privately from whatever scan CSV was uploaded. The pipeline now computes
the same figure over a named index universe (`market_health_daily.pct_above_20dma`, added in
migration 0011), so the desk can read it instead of deriving its own.

THE RECONCILIATION THAT MADE THIS A WIRING CHANGE
--------------------------------------------------
`DESK-PARITY.md` §P1.9 recorded these as irreconcilable: different metrics over different
populations, with no `pct_above_20dma` in the pipeline at all. Two of those three are now closed —
the column exists, and on 2026-08-18 the desk's 271-row scan and the pipeline's
`nifty-total-market` membership are the **same 271 symbols**, symbol for symbol.

Both sides then produce **68.6347%**. Not close: equal. The desk's `audit` counts a NaN as below
its 20-DMA and the pipeline excludes NULLs from the denominator, and on this data no row is
missing either input, so the two denominators coincide.

WHY THE SOURCE STILL FOLLOWS THE SCAN
--------------------------------------
Breadth has to be measured over the population the plan is built from. A pipeline row read against
an uploaded scan of some *other* universe would be a number about a different market. So the
membership is checked rather than assumed: same set, use the pipeline; different set, use the
scan's own figure and say so on the plan. Neither branch guesses.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

#: The universe the desk's weekly scan corresponds to. Verified equal, symbol for symbol, on
#: 2026-08-18; the equality is asserted per plan rather than trusted, because index membership
#: changes and a scan CSV can be exported from anywhere.
DESK_UNIVERSE_SLUG = os.getenv("DESK_UNIVERSE_SLUG", "nifty-total-market")

SCREENER_DSN = os.getenv(
    "SCREENER_DATABASE_URL", "postgresql://baskfy:baskfy@localhost:5433/baskfy"
)

_SQL = """
select m.pct_above_20dma, m.constituent_count
  from market_health_daily m join index_def d on d.id = m.index_id
 where d.slug = %(slug)s and m.date = %(on)s
"""

_MEMBERS_SQL = """
select i.symbol
  from index_member_daily m
  join instrument i on i.id = m.instrument_id
  join index_def d on d.id = m.index_id
 where d.slug = %(slug)s and m.date = %(on)s
"""


def pipeline_breadth(on: dt.date, slug: str = DESK_UNIVERSE_SLUG) -> tuple[float | None, set[str]]:
    """The pipeline's 20-DMA breadth for one date, with the membership it was measured over."""
    import psycopg  # noqa: PLC0415 — a Postgres driver is not needed to import this module

    with psycopg.connect(SCREENER_DSN) as conn, conn.cursor() as cur:
        cur.execute(_SQL, {"slug": slug, "on": on})
        row = cur.fetchone()
        cur.execute(_MEMBERS_SQL, {"slug": slug, "on": on})
        members = {r[0] for r in cur.fetchall()}
    if row is None or row[0] is None:
        return None, members
    return float(row[0]), members


def breadth_for_plan(scan_breadth: float, symbols: set[str], on: dt.date) -> dict[str, Any]:
    """Decide which number the cash bands see, and record why.

    Returns the value plus enough provenance that a plan from six months ago can be read back and
    understood. `source` is "pipeline" or "scan"; `scan_value` is always present, so the two can be
    compared every week whichever one was used.
    """
    result: dict[str, Any] = {
        "value": scan_breadth,
        "source": "scan",
        "scan_value": scan_breadth,
        "pipeline_value": None,
        "universe": DESK_UNIVERSE_SLUG,
        "reason": "",
    }

    try:
        value, members = pipeline_breadth(on)
    except Exception as exc:
        result["reason"] = f"pipeline unreachable ({exc.__class__.__name__}); used the scan's own"
        log.warning("breadth: %s", result["reason"])
        return result

    result["pipeline_value"] = value
    if value is None:
        result["reason"] = f"no market_health_daily row for {on}; used the scan's own"
    elif members != symbols:
        # Not an error. The desk can be handed a scan of any universe, and a breadth number about
        # a different set of stocks is not a more authoritative number, it is a wrong one.
        only_scan = len(symbols - members)
        only_index = len(members - symbols)
        result["reason"] = (
            f"scan universe differs from {DESK_UNIVERSE_SLUG} on {on} "
            f"({only_scan} only in the scan, {only_index} only in the index); used the scan's own"
        )
    else:
        result["value"] = value
        result["source"] = "pipeline"
        result["reason"] = f"same {len(members)} symbols as {DESK_UNIVERSE_SLUG} on {on}"

    return result
