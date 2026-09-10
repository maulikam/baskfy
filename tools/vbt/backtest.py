"""VB9's engine over the research export, without a database: ``python tools/vbt/backtest.py``.

The sleeve has two ways to re-run the study and they answer different questions.

``baskfy_worker.tasks.vbt_backtest``   the **plant's** bars, appended to ``vb_backtest_run``,
                                       drift flagged, shown on ``/vbt/backtest``. That is the one
                                       that matters, because it measures the data the book will
                                       actually trade on.
this file                              the **research export's** bars, printed and nothing else.
                                       It needs no database, no user and no migration, so it is
                                       what you reach for when the question is "did my change to
                                       the engine move a number", and the answer wanted is a
                                       diff rather than a row.

Both call the same functions, so neither can drift from the book (``04`` §11).

    python tools/vbt/backtest.py                       # the three books, 2017-10-16 onwards
    python tools/vbt/backtest.py --start 2021-01-01    # a slice
    python tools/vbt/backtest.py --json out/books.json # the numbers, for a diff

The export is 68 MB and gitignored; ``research/volume-breakout/export_bars_aws.sh`` regenerates
it. Without it this exits 2 and says so, rather than printing a zero that looks like a result.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "vbt"))
sys.path.insert(0, str(REPO / "decile-blueprint" / "packages" / "core" / "src"))

import numpy as np  # noqa: E402 - the sys.path inserts above have to come first
from research_panel import EXPORT, load  # noqa: E402

from baskfy_core.vbt.backtest import (  # noqa: E402
    BacktestParams,
    BacktestResult,
    gate_vector,
    panel_from_frame,
    run_backtest,
    summarise,
    yearly,
)
from baskfy_core.vbt.breadth import breadth_series  # noqa: E402
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG  # noqa: E402
from baskfy_core.vbt.drift import compare  # noqa: E402
from baskfy_core.vbt.indicators import with_vbt_indicators  # noqa: E402
from baskfy_core.vbt.published import PUBLISHED  # noqa: E402
from baskfy_core.vbt.signals import with_signal_columns  # noqa: E402

#: `01` §4's window. Everything before it is warm-up for the 200-session average.
DEFAULT_START = dt.date(2017, 10, 16)


def _headline(label: str, result: BacktestResult) -> dict[str, object]:
    stats = summarise(result)
    if stats is None:
        return {"book": label, "trades": 0}
    return {
        "book": label,
        "cagr_pct": round(stats.cagr_pct, 2),
        "max_drawdown_pct": round(stats.max_drawdown_pct, 2),
        "trades": stats.trades,
        "win_rate_pct": round(stats.win_rate_pct, 1),
        "profit_factor": None if stats.profit_factor is None else round(stats.profit_factor, 2),
        "exposure_pct": round(stats.exposure_pct, 1),
        "orders_offered": result.orders_offered,
        "fill_rate_pct": result.fill_rate_pct(),
        "yearly": [row.to_json() for row in yearly(result)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vbt-backtest", description=__doc__)
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=None)
    parser.add_argument("--sleeve", default="1000000")
    parser.add_argument("--json", default=None, help="also write the numbers here")
    args = parser.parse_args(argv)

    if not (EXPORT / "ohlcv_daily.csv.gz").exists():
        print(
            f"the research export is not in {EXPORT}. It is 68 MB and gitignored; regenerate it "
            f"with research/volume-breakout/export_bars_aws.sh",
            file=sys.stderr,
        )
        return 2

    started = time.perf_counter()
    params = BacktestParams(
        sleeve_inr=Decimal(args.sleeve),
        start=dt.date.fromisoformat(args.start),
        end=dt.date.fromisoformat(args.end) if args.end else None,
        config=DEFAULT_VBT_CONFIG,
    )
    loaded = load(DEFAULT_VBT_CONFIG)
    tagged = with_signal_columns(
        with_vbt_indicators(loaded.bars, loaded.calendar, DEFAULT_VBT_CONFIG), DEFAULT_VBT_CONFIG
    )
    breadth = breadth_series(tagged, DEFAULT_VBT_CONFIG)

    panel = panel_from_frame(tagged, "state")
    scan_panel = panel_from_frame(tagged, "scan_hit")
    books = {
        "full": run_backtest(
            panel, gate_vector(breadth, panel.sessions), dataclasses.replace(params, label="full")
        ),
        "gate_off": run_backtest(
            panel,
            np.ones(panel.sessions_count, dtype=bool),
            dataclasses.replace(params, label="gate_off"),
        ),
        "raw_scan": run_backtest(
            scan_panel,
            gate_vector(breadth, scan_panel.sessions),
            dataclasses.replace(params, label="raw_scan"),
        ),
    }
    took = time.perf_counter() - started

    rows = [_headline(label, result) for label, result in books.items()]
    primary = summarise(books["full"])
    drift = (
        compare(
            cagr_pct=primary.cagr_pct,
            max_drawdown_pct=primary.max_drawdown_pct,
            trades=primary.trades,
        )
        if primary
        else None
    )

    print(f"{'book':<12} {'CAGR':>8} {'max DD':>9} {'trades':>8} {'win':>7} {'fills':>7}")
    for row in rows:
        print(
            f"{row['book']:<12} {row.get('cagr_pct', 0):>7}% {row.get('max_drawdown_pct', 0):>8}% "
            f"{row.get('trades', 0):>8} {row.get('win_rate_pct', 0):>6}% "
            f"{row.get('fill_rate_pct') or 0:>6}%"
        )
    print(
        f"\nthe study (STRATEGY §4): {PUBLISHED.cagr_pct}% at {PUBLISHED.max_drawdown_pct}%, "
        f"{PUBLISHED.trades} trades"
    )
    if drift is not None:
        print(
            f"drift: {drift.cagr_pct_delta:+} CAGR points, {drift.trades_delta:+} trades — "
            + ("FLAGGED" if drift.flagged else "inside the threshold")
        )
    print(f"\n[{took:.0f}s]")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "books": rows,
                    "drift": drift.to_json() if drift else None,
                    "published": dataclasses.asdict(PUBLISHED),
                    "seconds": round(took, 1),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
