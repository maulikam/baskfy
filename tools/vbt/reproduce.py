#!/usr/bin/env python
"""VB2: does ``baskfy_core.vbt`` reproduce the study? (``docs/vbt/06`` VB2)

    cd decile-blueprint
    uv run python ../tools/vbt/reproduce.py                # the full comparison
    uv run python ../tools/vbt/reproduce.py --json out.json

The study is ``research/volume-breakout/STRATEGY.md``; its answers are
``research/volume-breakout/out/final_trades.csv`` (761 trades) and ``out/final_metrics.json``
(18.2% CAGR, -27.9% maximum drawdown). This script runs the **production** core over the **same**
bars and prints, line by line, where the two agree and where they do not.

It exits 0 when every check passes and 1 when one does not, so it is usable as a gate; the same
comparison is a test (``packages/core/tests/test_vbt_goldens.py``) which skips loudly when the
export is absent.

Nothing here writes to the export, reaches a network, or touches a database.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, EntryConfig, VbtConfig  # noqa: E402
from baskfy_core.vbt.indicators import with_vbt_indicators  # noqa: E402
from baskfy_core.vbt.signals import with_signal_columns  # noqa: E402
from research_panel import RESULTS, load  # noqa: E402

#: The study's own frame: the first 200 sessions are warm-up, so the book starts here.
FIRST_SESSION = dt.date(2017, 10, 16)

#: How close each headline number has to be. The study stored some of them rounded to one
#: decimal (``win_rate_pct`` 37.8, ``avg_hold`` 18.3, ``exposure_pct`` 63.2), so half of that
#: last place is the honest tolerance — anything tighter would be testing its ``json.dump`` and
#: anything looser would stop testing the arithmetic.
TOLERANCE = {
    "cagr_pct": 0.01,
    "max_dd_pct": 0.01,
    "trades": 0.0,
    "win_rate_pct": 0.05,
    "profit_factor": 0.005,
    "avg_hold": 0.05,
    "exposure_pct": 0.05,
}


def published() -> dict[str, float]:
    """``out/final_metrics.json`` — the study's own answers, read rather than transcribed."""
    stored = json.loads((RESULTS / "final_metrics.json").read_text(encoding="utf-8"))
    return {name: float(stored[name]) for name in TOLERANCE}

#: The research's exit labels, mapped onto ``ExitReason``. ``stop_day0`` is a stop that fired on
#: the entry session itself; the production engine calls that a stop like any other.
REASON_OF = {
    "ema_close": "EMA_EXIT",
    "stop": "STOP_HIT",
    "stop_day0": "STOP_HIT",
    "stop_gap": "STOP_GAP",
    "no_bar": "NO_BAR",
    "end": "END_OF_RUN",
}


def build(config: VbtConfig) -> tuple[pl.DataFrame, pl.DataFrame]:
    """``(indicated-and-tagged frame, breadth series)`` over the research export."""
    loaded = load(config)
    indicated = with_vbt_indicators(loaded.bars, loaded.calendar, config)
    tagged = with_signal_columns(indicated, config)
    return tagged, breadth_series(tagged, config)


def run(
    tagged: pl.DataFrame,
    breadth: pl.DataFrame,
    *,
    config: VbtConfig = DEFAULT_VBT_CONFIG,
    signal_column: str = "state",
    gate: bool = True,
    label: str = "full",
) -> BacktestResult:
    panel = panel_from_frame(tagged, signal_column)
    open_at = (
        gate_vector(breadth, panel.sessions)
        if gate
        else np.ones(panel.sessions_count, dtype=bool)
    )
    params = BacktestParams(start=FIRST_SESSION, config=config, label=label)
    return run_backtest(panel, open_at, params)


def golden_trades() -> list[dict[str, str]]:
    with (RESULTS / "final_trades.csv").open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def compare_trades(result: BacktestResult) -> tuple[list[str], dict[str, object]]:
    """Match on identity, then on price. ``06`` VB2 AC 4."""
    theirs = golden_trades()
    ours = result.trades
    notes: list[str] = []
    summary: dict[str, object] = {
        "their_count": len(theirs),
        "our_count": len(ours),
        "identity_matches": 0,
        "price_within_a_paisa": 0,
        "worst_price_gap": 0.0,
        "first_divergence": None,
    }
    if len(theirs) != len(ours):
        notes.append(f"trade count: study {len(theirs)}, core {len(ours)}")
    for index, (their, our) in enumerate(zip(theirs, ours, strict=False)):
        identity = (
            their["symbol"] == our.symbol
            and their["entry_date"] == our.entry_date.isoformat()
            and their["exit_date"] == our.exit_date.isoformat()
            and int(their["qty"]) == our.quantity
            and REASON_OF[their["reason"]] == our.reason.value
        )
        if not identity:
            if summary["first_divergence"] is None:
                summary["first_divergence"] = {
                    "index": index,
                    "study": {k: their[k] for k in ("symbol", "entry_date", "exit_date", "qty",
                                                    "reason")},
                    "core": {
                        "symbol": our.symbol,
                        "entry_date": our.entry_date.isoformat(),
                        "exit_date": our.exit_date.isoformat(),
                        "qty": our.quantity,
                        "reason": our.reason.value,
                    },
                }
            continue
        summary["identity_matches"] += 1
        gap = max(
            abs(float(our.entry_price) - float(their["entry"])),
            abs(float(our.exit_price) - float(their["exit"])),
        )
        summary["worst_price_gap"] = max(float(summary["worst_price_gap"]), gap)
        if gap <= 0.01:
            summary["price_within_a_paisa"] += 1
    return notes, summary


def compare_metrics(stats: dict[str, object]) -> list[tuple[str, float, float, float]]:
    """``(name, study, core, difference)`` for each headline number. ``06`` VB2 AC 5."""
    ours = {
        "cagr_pct": float(stats["cagr_pct"]),
        "max_dd_pct": float(stats["max_drawdown_pct"]),
        "trades": float(stats["trades"]),
        "win_rate_pct": float(stats["win_rate_pct"]),
        "profit_factor": float(stats["profit_factor"] or 0.0),
        "avg_hold": float(stats["avg_hold_sessions"]),
        "exposure_pct": float(stats["exposure_pct"]),
    }
    theirs = published()
    return [(name, theirs[name], ours[name], ours[name] - theirs[name]) for name in TOLERANCE]


def sensitivity(tagged: pl.DataFrame, breadth: pl.DataFrame) -> list[tuple[str, float, float]]:
    """``06`` VB2 AC 6 — the neighbourhood, with the study's published number beside each."""
    published = {
        "entry window 2 sessions": 11.4,
        "entry window 5 sessions": 17.1,
        "stop 10%": 16.4,
        "stop 15%": 16.6,
        "8 slots": 15.5,
        "15 slots": 12.9,
        "gate 30%": 15.6,
        "gate 35%": 18.6,
        "gate 45%": 15.4,
        "gate 50%": 10.9,
        "no gate": 18.5,
        "raw Chartink scan": 0.8,
    }
    import dataclasses

    def with_entry(sessions: int) -> VbtConfig:
        return dataclasses.replace(
            DEFAULT_VBT_CONFIG, entry=EntryConfig(valid_sessions=sessions)
        )

    def with_field(group: str, **changes: object) -> VbtConfig:
        sub = dataclasses.replace(getattr(DEFAULT_VBT_CONFIG, group), **changes)
        return dataclasses.replace(DEFAULT_VBT_CONFIG, **{group: sub})

    def gate_at(pct: float) -> pl.DataFrame:
        """The gate at another threshold, in the form ``04`` §4.2 documents."""
        return breadth.with_columns(
            pl.when(pl.col("pct_above_dma") > pct)
            .then(pl.lit("OPEN"))
            .otherwise(pl.lit("SHUT"))
            .alias("gate")
        )

    cases: list[tuple[str, VbtConfig, pl.DataFrame, bool, str]] = [
        ("entry window 2 sessions", with_entry(2), breadth, True, "state"),
        ("entry window 5 sessions", with_entry(5), breadth, True, "state"),
        ("stop 10%", with_field("exits", stop_pct=10.0), breadth, True, "state"),
        ("stop 15%", with_field("exits", stop_pct=15.0), breadth, True, "state"),
        ("8 slots", with_field("sizing", max_slots=8), breadth, True, "state"),
        ("15 slots", with_field("sizing", max_slots=15), breadth, True, "state"),
        ("gate 30%", DEFAULT_VBT_CONFIG, gate_at(30.0), True, "state"),
        ("gate 35%", DEFAULT_VBT_CONFIG, gate_at(35.0), True, "state"),
        ("gate 45%", DEFAULT_VBT_CONFIG, gate_at(45.0), True, "state"),
        ("gate 50%", DEFAULT_VBT_CONFIG, gate_at(50.0), True, "state"),
        ("no gate", DEFAULT_VBT_CONFIG, breadth, False, "state"),
        ("raw Chartink scan", DEFAULT_VBT_CONFIG, breadth, True, "scan_hit"),
    ]
    out: list[tuple[str, float, float]] = []
    for label, config, gate_frame, gated, column in cases:
        result = run(
            tagged, gate_frame, config=config, signal_column=column, gate=gated, label=label
        )
        out.append((label, published[label], float(summarise(result)["cagr_pct"])))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="write the whole comparison here as JSON")
    parser.add_argument(
        "--skip-sensitivity", action="store_true", help="the twelve neighbourhood runs are slow"
    )
    args = parser.parse_args()

    started = time.time()
    print("loading the research export …", flush=True)
    tagged, breadth = build(DEFAULT_VBT_CONFIG)
    signals = int((tagged["state"] == "SIGNAL").sum())
    scan_hits = int(tagged["scan_hit"].sum())
    print(
        f"  {tagged['instrument_id'].n_unique()} instruments x "
        f"{tagged['date'].n_unique()} sessions   "
        f"scan hits {scan_hits}   signals {signals}   [{time.time() - started:.0f}s]",
        flush=True,
    )

    result = run(tagged, breadth)
    stats = summarise(result)
    notes, trade_summary = compare_trades(result)
    metrics = compare_metrics(stats)

    print("\n-- signals -----------------------------------------------------------")
    print(f"  raw Chartink scan   study 32,929   core {scan_hits:,}")
    print(f"  VBT-1 signals       study  6,293   core {signals:,}")

    print("\n-- trades ------------------------------------------------------------")
    print(f"  study {trade_summary['their_count']}   core {trade_summary['our_count']}")
    print(
        f"  identity matches {trade_summary['identity_matches']}   "
        f"prices within ₹0.01 {trade_summary['price_within_a_paisa']}   "
        f"worst price gap ₹{trade_summary['worst_price_gap']:.4f}"
    )
    if trade_summary["first_divergence"]:
        print(f"  first divergence: {json.dumps(trade_summary['first_divergence'])}")

    print("\n-- metrics -----------------------------------------------------------")
    metrics_ok = True
    for name, study, core, delta in metrics:
        within = abs(delta) <= TOLERANCE[name]
        metrics_ok = metrics_ok and within
        mark = " " if within else "  <-- outside tolerance"
        print(f"  {name:16s} study {study:9.2f}   core {core:9.2f}   Δ {delta:+.2f}{mark}")

    print("\n-- by year -----------------------------------------------------------")
    for row in yearly(result):
        print(
            f"  {row['year']}  {float(row['return_pct']):+7.1f}%  "
            f"{row['trades']:4d} trades  win {float(row['win_rate_pct']):.0f}%"
        )

    print("\n-- exits -------------------------------------------------------------")
    print(f"  {stats['by_reason']}")

    sensitivity_rows: list[tuple[str, float, float]] = []
    if not args.skip_sensitivity:
        print("\n-- the neighbourhood -------------------------------------------------")
        sensitivity_rows = sensitivity(tagged, breadth)
        for label, study, core in sensitivity_rows:
            print(f"  {label:24s} study {study:6.1f}%   core {core:6.1f}%   Δ {core - study:+.1f}")

    payload = {
        "elapsed_seconds": round(time.time() - started, 1),
        "scan_hits": scan_hits,
        "signals": signals,
        "trades": trade_summary,
        "metrics": [
            {"name": n, "study": s, "core": c, "delta": d} for n, s, c, d in metrics
        ],
        "stats": stats,
        "yearly": yearly(result),
        "sensitivity": [
            {"case": label, "study": s, "core": c} for label, s, c in sensitivity_rows
        ],
        "notes": notes,
    }
    if args.json:
        args.json.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        print(f"\nwrote {args.json}")

    # Every neighbourhood case reproduces to a tenth of a point except one, and the exception is
    # a fact about the data rather than a slack tolerance: at a 35% gate a single session of the
    # 2,396 flips, because one penny stock's flat price sits exactly on its own 200-day average
    # and two rolling-mean implementations disagree about it by one unit in the last place.
    # DECISIONS-VB VB2.2. At the 40% the strategy uses, the verdict is identical on every session.
    allowance = {"gate 35%": 0.25}
    sensitivity_ok = all(
        abs(core - study) <= allowance.get(label, 0.1)
        for label, study, core in sensitivity_rows
    )
    ok = (
        signals == 6_293
        and scan_hits == 32_929
        and trade_summary["our_count"] == trade_summary["their_count"]
        and trade_summary["identity_matches"] == trade_summary["their_count"]
        and trade_summary["price_within_a_paisa"] == trade_summary["their_count"]
        and metrics_ok
        and sensitivity_ok
    )
    print(f"\n{'PASS' if ok else 'DIFFERENCES — see above'}   [{time.time() - started:.0f}s]")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
