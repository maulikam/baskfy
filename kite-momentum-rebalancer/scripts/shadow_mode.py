"""Shadow mode — M14 §2. Build one Friday's plan both ways and diff at order level.

    make shadow DATE=2026-08-18          (from the decile workspace)
    .venv/bin/python -m scripts.shadow_mode --date 2026-08-18

The desk has been traded off an uploaded CSV. M13 built the path that generates the same scan from
the merged engine, and left it off by default. This is what earns it the right to be turned on:
run both, compare the **orders** — symbol, side, quantity — and append the result to a log.

WHY ORDERS AND NOT SCORES
-------------------------
A score that moves by a tenth changes nothing anybody can lose money on. An order that appears,
disappears, or changes size is the whole difference. `reconciliation/DESK-PARITY.md` already
compares scores and ranks; this compares the only output that reaches a broker.

WHAT IT DOES NOT TOUCH
----------------------
No Kite session, no live prices, no holdings, and therefore no orders that could ever be sent. It
builds both plans against the same fixed book so the only variable is the scan. That also means it
runs on a Sunday, which is when anyone will actually look at it.

THE PROTOCOL THIS FEEDS
-----------------------
`docs/SHADOW-MODE.md`: four consecutive Fridays with an empty order-level diff, then the default
flips. Four is calendar time and cannot be hurried; this is the tooling for it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "shadow-mode.jsonl"

#: A fixed, synthetic book. The comparison is between two scans, so the book must not vary — and
#: a real book would make the run depend on a Kite session and on the hour it was executed.
BOOK: list[dict[str, Any]] = []
CASH = 1_000_000.0

#: How many order deltas to print before deferring to the log.
SHOWN = 20


def _orders(plan: dict) -> dict[str, tuple[str, int]]:
    return {
        str(o["symbol"]): (str(o.get("action", "")), int(o["delta"]))
        for o in plan.get("orders", [])
        if o.get("delta")
    }


def _diff(left: dict[str, tuple[str, int]], right: dict[str, tuple[str, int]]) -> list[dict]:
    rows: list[dict] = []
    for symbol in sorted(set(left) | set(right)):
        a, b = left.get(symbol), right.get(symbol)
        if a != b:
            rows.append({"symbol": symbol, "uploaded": a, "generated": b})
    return rows


def run(as_of: dt.date, upload: Path) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    import pandas as pd  # noqa: PLC0415
    import polars as pl  # noqa: PLC0415
    from app import scan_source  # noqa: PLC0415
    from app.rebalance import build_plan  # noqa: PLC0415
    from app.scoring import audit, load_scan, score  # noqa: PLC0415

    from baskfy_core.momentum_scan import CARRIED_COLUMNS  # noqa: PLC0415

    uploaded = load_scan(str(upload))
    carried = pl.DataFrame(
        pd.DataFrame(uploaded)[["symbol", "series", *CARRIED_COLUMNS]].to_dict(orient="records")
    )
    generated_scan = scan_source.generate(as_of, carried)
    generated = scan_source.as_desk_frame(generated_scan)

    plans = {}
    for name, frame in (("uploaded", uploaded), ("generated", generated)):
        # Breadth is deliberately taken from each scan's own rows here rather than the pipeline.
        # The question shadow mode asks is whether the two scans produce the same orders; feeding
        # both the same external breadth number would hide a disagreement that is really there.
        plans[name] = build_plan(
            score(frame), list(BOOK), CASH, live_prices=_prices(frame), breadth_override=None
        )

    deltas = _diff(_orders(plans["uploaded"]), _orders(plans["generated"]))
    return {
        "as_of": as_of.isoformat(),
        "upload": upload.name,
        "screen_run_id": generated_scan.screen_run_id,
        "suspect_symbols": len(generated_scan.suspect_symbols),
        "orders_uploaded": len(_orders(plans["uploaded"])),
        "orders_generated": len(_orders(plans["generated"])),
        "breadth_uploaded": audit(uploaded)["breadth_above_20dma"],
        "breadth_generated": audit(generated)["breadth_above_20dma"],
        "order_deltas": deltas,
        "green": not deltas,
    }


def _prices(frame: pd.DataFrame) -> dict[str, float]:
    """Every candidate priced from its own scan's close.

    Live LTP would make the two runs differ for a reason that has nothing to do with the scans,
    and would tie the harness to a Kite session and to market hours.
    """
    return {str(r.symbol): float(r.close) for r in frame.itertuples() if r.close and r.close > 0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="the Friday, YYYY-MM-DD")
    ap.add_argument("--upload", default="", help="defaults to the newest scan in data/uploads/")
    ap.add_argument("--log", default=str(LOG))
    args = ap.parse_args()

    as_of = dt.date.fromisoformat(args.date)
    if args.upload:
        upload = Path(args.upload)
    else:
        scans = sorted(
            (ROOT / "data" / "uploads").glob("scan_*.csv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not scans:
            raise SystemExit("no scan CSV in data/uploads/ to compare against")
        upload = scans[0]

    result = run(as_of, upload)

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result) + "\n")

    verdict = "GREEN" if result["green"] else f"{len(result['order_deltas'])} ORDER DELTAS"
    print(f"shadow mode {result['as_of']}  {upload.name} vs {result['screen_run_id']}: {verdict}")
    print(
        f"  orders: {result['orders_uploaded']} uploaded / {result['orders_generated']} generated"
        f"   breadth: {result['breadth_uploaded']} / {result['breadth_generated']}"
        f"   suspect symbols: {result['suspect_symbols']}"
    )
    for row in result["order_deltas"][:SHOWN]:
        print(f"  {row['symbol']:<14} uploaded={row['uploaded']}  generated={row['generated']}")
    if len(result["order_deltas"]) > SHOWN:
        rest = len(result["order_deltas"]) - SHOWN
        print(f"  ... and {rest} more (all of them are in {log_path})")
    print(f"appended to {log_path}")
    return 0 if result["green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
