"""``make reconcile`` — the reconciliation report (Prompt 19 §4).

    "A reconciliation report CLI comparing our computed values against a snapshot of the reference
     product's public output for a set of symbols — starting with the 271-row reference export in
     fixtures/ — printing every disagreement over a tolerance."

Two modes, and the second one has never run.

**Snapshot mode (the default).** Reconciles the committed 271-row export against itself through
our own formula code: every relationship docs/05 asserts between the published columns, with
tolerances derived from docs/13 §4's storage precision rather than picked. See
:mod:`baskfy_core.reconcile`. This is what runs in CI and what
``packages/core/tests/test_reconciliation_report.py`` pins.

**Recomputation mode (``--bars``).** Point it at a Parquet file of real adjusted daily bars and it
recomputes every mapped column with :func:`baskfy_core.factors.compute_factors` and diffs against
the export cell by cell — docs/13 §5 step 2, the decisive test. Nothing in this repository can
supply that file: ``fixtures/`` is a single day of *results*, the Prompt 2 provider bars are seeded
random walks, and the suite is network-blocked. The mode is implemented and tested against a
synthetic panel; it has never been run against real history. Say so when you report its output.

The CLI lives in ``services/worker`` and not in ``packages/core`` for the reason CLAUDE.md gives:
core is I/O-free. The arithmetic is in core; reading files and printing is here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final

import polars as pl

from baskfy_core.factors import compute_factors
from baskfy_core.precision import COLUMN_PRECISION, quantise
from baskfy_core.reconcile import (
    CheckResult,
    Disagreement,
    reconcile,
    total_disagreements,
)
from baskfy_core.reference_export import (
    FACTOR_COLUMN_MAP,
    read_export,
)
from baskfy_providers.reference_export_io import reference_export_path

#: docs/05 §8 — listed rather than silently skipped. The recomputation mode reports these
#: separately: they are known not to reconcile and the reason is a definition, not a bug.
KNOWN_UNRECONCILED: Final[frozenset[str]] = frozenset({"ret_12m_minus_1m", "ret_12m_minus_2m"})

#: Export columns that are not factors and so are not recomputed here.
NOT_RECOMPUTED: Final[frozenset[str]] = frozenset({"series", "marketcap_cr", "vol_day_val"})

AS_OF: Final = dt.date(2026, 8, 18)

#: How many missing symbols to name before summarising. A list of 271 helps nobody.
MISSING_SYMBOLS_SHOWN: Final = 10


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(results: list[CheckResult], *, limit: int) -> str:
    lines: list[str] = [
        "Reconciliation report — our formulas against the reference product's published output",
        "=" * 92,
        "",
        f"{'check':<30} {'docs':<26} {'cells':>7} {'bad':>5}  status",
        "-" * 92,
    ]
    for result in results:
        status = "clean" if result.clean else "DISAGREEMENTS"
        if result.unresolved and result.clean:
            status = "clean (see UNRESOLVED)"
        lines.append(
            f"{result.name:<30} {result.reference:<26} {result.cells:>7} "
            f"{len(result.disagreements):>5}  {status}"
        )

    bad = total_disagreements(results)
    lines += ["", f"{bad} disagreement(s) over tolerance.", ""]

    for result in results:
        if not result.disagreements:
            continue
        lines += [f"--- {result.name} ({result.reference}) ---", f"    {result.description}", ""]
        for disagreement in result.disagreements[:limit]:
            lines.append(f"    {disagreement}")
        if len(result.disagreements) > limit:
            lines.append(f"    ... and {len(result.disagreements) - limit} more")
        lines.append("")

    unresolved = [r for r in results if r.unresolved]
    if unresolved:
        lines += ["UNRESOLVED — investigated, documented, not settled by this data", "=" * 92, ""]
        for result in unresolved:
            lines += [f"--- {result.name} ({result.reference}) ---"]
            lines += [f"    {line}" for line in result.unresolved]
            lines.append("")

    noted = [r for r in results if r.notes]
    if noted:
        lines += ["NOTES", "=" * 92, ""]
        for result in noted:
            for note in result.notes:
                lines.append(f"    {result.name}: {note}")
        lines.append("")
    return "\n".join(lines)


def to_payload(results: list[CheckResult]) -> dict[str, object]:
    return {
        "as_of": AS_OF.isoformat(),
        "disagreements": total_disagreements(results),
        "checks": [
            {
                "name": result.name,
                "reference": result.reference,
                "description": result.description,
                "cells": result.cells,
                "clean": result.clean,
                "disagreements": [
                    {
                        "symbol": d.symbol,
                        "column": d.column,
                        "expected": str(d.expected),
                        "published": str(d.actual),
                        "delta": str(d.delta),
                        "tolerance": str(d.tolerance),
                    }
                    for d in result.disagreements
                ],
                "unresolved": list(result.unresolved),
                "notes": list(result.notes),
            }
            for result in results
        ],
    }


# ---------------------------------------------------------------------------
# Recomputation mode (docs/13 §5 step 2)
# ---------------------------------------------------------------------------


def recompute_against_bars(
    export: pl.DataFrame, bars: pl.DataFrame, benchmark: pl.DataFrame | None = None
) -> CheckResult:
    """Recompute every mapped column from real bars and diff it against the export.

    ``bars`` must carry ``symbol`` (or ``instrument_id``) plus the columns
    :func:`baskfy_core.factors.compute_factors` requires. The comparison is done **after** storage
    rounding on both sides, because that is the number the reference product published.
    """
    symbols = export["symbol"].to_list()
    if "symbol" not in bars.columns:
        raise ValueError("the bars file must carry a `symbol` column to be joined to the export")

    ids = {symbol: index + 1 for index, symbol in enumerate(sorted(set(bars["symbol"].to_list())))}
    prepared = bars.with_columns(
        pl.col("symbol").replace_strict(ids, return_dtype=pl.Int64).alias("instrument_id")
    )
    calendar = sorted(prepared["date"].unique().to_list())
    computed = compute_factors(prepared, AS_OF, calendar, benchmark=benchmark).frame
    by_id = {int(row["instrument_id"]): row for row in computed.iter_rows(named=True)}

    disagreements: list[Disagreement] = []
    missing: list[str] = []
    cells = 0
    for row in export.iter_rows(named=True):
        symbol = row["symbol"]
        computed_row = by_id.get(ids.get(symbol, -1))
        if computed_row is None:
            missing.append(symbol)
            continue
        for export_column, factor_column in FACTOR_COLUMN_MAP.items():
            if factor_column in NOT_RECOMPUTED or factor_column not in computed_row:
                continue
            published, ours = row[export_column], computed_row[factor_column]
            if published is None or ours is None:
                continue
            cells += 1
            places = COLUMN_PRECISION.get(factor_column, 0)
            expected = quantise(Decimal(str(ours)), places)
            if expected is None:
                continue
            actual = Decimal(str(published))
            tolerance = Decimal(1).scaleb(-places) / 2 if places else Decimal(0)
            if abs(expected - actual) > tolerance:
                disagreements.append(
                    Disagreement(
                        "recomputation", symbol, export_column, expected, actual, tolerance
                    )
                )

    unresolved = [
        "docs/05 §8's two skip-month columns are not in the export and cannot be recomputed "
        f"against it: {sorted(KNOWN_UNRECONCILED)}."
    ]
    if missing:
        unresolved.append(
            f"{len(missing)} exported symbols had no bars in the supplied file: "
            f"{', '.join(missing[:MISSING_SYMBOLS_SHOWN])}"
            f"{' ...' if len(missing) > MISSING_SYMBOLS_SHOWN else ''}"
        )
    return CheckResult(
        "recomputation",
        "docs/13 §5 step 2",
        "every mapped factor column recomputed from adjusted bars and compared cell by cell",
        cells,
        disagreements,
        unresolved=unresolved,
        notes=[f"{len(symbols) - len(missing)} of {len(symbols)} exported symbols were recomputed"],
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m baskfy_worker.reconcile_cli", description=__doc__
    )
    parser.add_argument(
        "--export",
        type=Path,
        default=None,
        help="the reference snapshot (default: the committed 271-row fixture)",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="comma-separated symbols to restrict the report to",
    )
    parser.add_argument(
        "--bars",
        type=Path,
        default=None,
        help="Parquet of real adjusted daily bars; enables docs/13 §5 step 2 recomputation",
    )
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=None,
        help="Parquet of the NIFTY 50 level series (date, close) for beta",
    )
    parser.add_argument("--limit", type=int, default=25, help="disagreements printed per check")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--write",
        type=Path,
        default=None,
        help="also write the rendered report here (reconciliation/REPORT.md is the committed one)",
    )
    args = parser.parse_args(argv)

    # `--export` is optional and its help text promises "default: the committed 271-row fixture",
    # so the default has to be resolved here. AF 3.10 made `read_export`'s `path` required when it
    # moved `default_fixture_path` out of `packages/core` (Law 1: core does not walk the
    # filesystem), and this caller was left passing `args.export` straight through. With no
    # `--export` that is `None`, which reached polars as `pl.read_csv(None)` and failed with
    # `TypeError: Object does not have a .read() method` — a message that names neither the flag
    # nor the file, for the CLI's own default invocation, which is the one CI runs.
    #
    # `baskfy_providers` is where the path lives now and the worker already depends on it; a
    # service may read the disk, only core may not.
    export_path = args.export or reference_export_path()
    export = read_export(export_path)
    if args.symbols:
        wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        export = export.filter(pl.col("symbol").is_in(wanted))
        if export.height == 0:
            print(f"none of {wanted} are in the export", file=sys.stderr)
            return 2

    results = reconcile(export)
    if args.bars is not None:
        benchmark = pl.read_parquet(args.benchmark) if args.benchmark else None
        results.append(recompute_against_bars(export, pl.read_parquet(args.bars), benchmark))

    text = render(results, limit=args.limit)
    if args.write is not None:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(text + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(to_payload(results), indent=2, sort_keys=True))
    else:
        print(text)
        if args.bars is None:
            # The snapshot is named, and the phrasing is the one the test calls "the single most
            # important honesty line in the report". AF 3.10 had to reword it because the sentence
            # built the filename from `default_fixture_path()`, which left `packages/core` with
            # the rest of the I/O — so the name and the words both went, and the test that pins
            # the line was not updated with them. The path is reachable from a service again, so
            # the sentence says which snapshot as well as what was not done.
            print(
                "NOTE: run with --bars to enable docs/13 §5 step 2 (full recomputation). "
                f"The committed snapshot is {export_path.name} and carries no price history, "
                "so no factor was recomputed from bars in this run "
                "(it is an answer key, not a bar panel).",
            )

    return 1 if total_disagreements(results) else 0


if __name__ == "__main__":
    sys.exit(main())
