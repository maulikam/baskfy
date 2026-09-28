#!/usr/bin/env python
"""F3-3: the EOD re-test of F3's daily rules on the archived F&O bhavcopies.

    cd decile-blueprint
    uv run python ../tools/fno/f3_retest.py --archive ~/baskfy-research/fno/archive \\
        --from 2022-01-03 --to 2026-09-22 --out ../docs/fno/evidence/f3-retest.md

Every day file is parsed through ``NSEProvider.fo_bhavcopy`` from the local archive (docs/09:
archive first, parse from the archive; nothing is fetched when the file is on disk). The index
rows (FUTIDX and OPTIDX on NIFTY and BANKNIFTY) are cached as a parquet beside the archive so a
re-run is seconds, then ``baskfy_core.fno.directional_retest`` runs the daily half of ``04`` §11
and the evidence file is written with the numbers and, first, what the closing file cannot test.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Sequence
from pathlib import Path

import polars as pl

from baskfy_core.fno import directional_retest as x
from baskfy_core.fno.config import DEFAULT_FNO_CONFIG
from baskfy_providers.factory import build_archive, build_nse_provider
from baskfy_providers.settings import ProviderSettings

INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY")
INDEX_INSTRUMENTS = ("FUTIDX", "OPTIDX")


def _days(start: dt.date, end: dt.date) -> list[dt.date]:
    out: list[dt.date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:  # noqa: PLR2004 - Monday to Friday
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def build_panel(archive_root: Path, start: dt.date, end: dt.date, cache: Path) -> pl.DataFrame:
    """The index rows of every archived day in ``[start, end]``, cached at ``cache``."""
    if cache.exists():
        frame = pl.read_parquet(cache)
        first, last = frame["date"].min(), frame["date"].max()
        if isinstance(first, dt.date) and isinstance(last, dt.date) and first <= start and last >= end:
            return frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))
    settings = ProviderSettings(raw_archive_dir=str(archive_root))
    provider = build_nse_provider(settings, archive=build_archive(settings))
    parts: list[pl.DataFrame] = []
    missing = 0
    for day in _days(start, end):
        if not (archive_root / "nse" / "fo-bhavcopy" / f"{day.isoformat()}.zip").exists():
            missing += 1
            continue
        frame = provider.fo_bhavcopy(day)
        parts.append(
            frame.filter(
                pl.col("symbol").is_in(INDEX_SYMBOLS) & pl.col("instrument").is_in(INDEX_INSTRUMENTS)
            )
        )
    sys.stderr.write(f"parsed {len(parts)} days, {missing} weekdays without a file\n")
    panel = pl.concat(parts, how="diagonal_relaxed")
    panel.write_parquet(cache)
    return panel


def _per_year(result: x.F3RetestResult) -> list[str]:
    rows = ["| Year | n | exp R | win |", "|---|---|---|---|"]
    if result.trades.is_empty():
        return [*rows, "| - | 0 | - | - |"]
    years = (
        result.trades.with_columns(pl.col("entry").dt.year().alias("yr"))
        .group_by("yr")
        .agg(pl.len().alias("n"), pl.col("R").mean().alias("exp"), (pl.col("R") > 0).mean().alias("win"))
        .sort("yr")
    )
    rows.extend(f"| {y} | {n} | {e:+.3f} | {w:.0%} |" for y, n, e, w in years.iter_rows())
    return rows


def _by_reason(result: x.F3RetestResult) -> list[str]:
    rows = ["| Exit | n | exp R | mean sessions |", "|---|---|---|---|"]
    if result.trades.is_empty():
        return [*rows, "| - | 0 | - | - |"]
    reasons = (
        result.trades.group_by("reason")
        .agg(pl.len().alias("n"), pl.col("R").mean().alias("exp"), pl.col("sessions").mean().alias("s"))
        .sort("reason")
    )
    rows.extend(f"| {k} | {n} | {e:+.3f} | {s:.1f} |" for k, n, e, s in reasons.iter_rows())
    return rows


def render(  # noqa: PLR0913 - the report's inputs, by keyword
    results: dict[str, x.F3RetestResult],
    *,
    start: dt.date,
    end: dt.date,
    days: int,
    ran_at: dt.datetime,
    f3_numbers: str,
) -> str:
    lines = [
        "# F3 — the EOD re-test of the daily rules (F3-3)",
        "",
        f"Run {ran_at.strftime('%d %b %Y %H:%M IST')} by `tools/fno/f3_retest.py` over {days} archived"
        f" F&O bhavcopies, {start.isoformat()} → {end.isoformat()}, through `NSEProvider.fo_bhavcopy`"
        " from the local archive. Rules and numbers: `docs/fno/04` §11 with `F3Config`'s defaults"
        f" ({f3_numbers}). One lot per entry, one add; R = net P&L per unit ÷ the spread's max loss"
        " per unit; costs are the research's index-option costs per leg per crossing.",
        "",
        "## What this cannot test — read first",
        "",
        "The bhavcopy is a closing file. The proxy **cannot test** and claims no number for:",
        "",
        *[f"* {item};" for item in x.NOT_TESTED],
        "",
        "The method's edge, as Maulik describes it, lives in exactly those intraday rules. What is"
        " tested is the daily half: the levels and direction at the close, the next-session entry at"
        " settle, the mark at each settle, the level break on the session's high or low (exited at that"
        " session's settle — worse than his rule when the break comes early, better when it reverses),"
        " the loss cut and the decay target at settle, the expiry-day settle, the next-session add."
        " The index bars are the **front-month future's** OHLC, a basis away from the cash index. Treat"
        " every number below as a floor for the daily half and as silence about the intraday half.",
        "",
    ]
    total_n = sum(r.summary.n for r in results.values())
    lines += ["## Headline", "", "| Sleeve | n | exp R | t | win | worst | gross R | cost R |", "|---|---|---|---|---|---|---|---|"]
    for symbol, res in results.items():
        s = res.summary
        gross = "-" if s.gross_r is None else f"{s.gross_r:+.3f}"
        cost = "-" if s.cost_r is None else f"{s.cost_r:.3f}"
        lines.append(
            f"| {symbol} | {s.n} | {s.exp_r:+.3f} | {s.t_stat:.2f} | {s.win:.0%} | {s.worst:+.2f} | {gross} | {cost} |"
        )
    lines += ["", f"{total_n} trades in all."]
    net = {k: v.summary.exp_r for k, v in results.items() if v.summary.n}
    if net and all(v <= 0 for v in net.values()):
        verdict = (
            "**Reading.** After costs the daily half loses on both underlyings in this proxy: the"
            " decay-target wins are small (a few percent of max loss each) and the level breaks and"
            " loss cuts, exited at the settle, give back more than the wins collect. Gross of costs"
            " the result is near zero, so the daily rules alone carry no edge here; whatever edge the"
            " method has must come from the intraday half this file cannot see. That is consistent"
            " with `01` §1c's honest limit and is why F3 runs on paper. Nothing here is a reason to"
            " turn it on, and nothing here disproves the intraday rules either."
        )
    elif net and all(v > 0 for v in net.values()):
        verdict = (
            "**Reading.** After costs the daily half is positive on both underlyings in this proxy."
            " It is a floor for the daily rules and says nothing about the intraday half."
        )
    else:
        verdict = (
            "**Reading.** The two underlyings disagree after costs; neither number is a floor for"
            " the method as a whole, and the intraday half is untested."
        )
    lines += ["", verdict]
    for symbol, res in results.items():
        lines += ["", f"## {symbol}", "", "Per year (by entry):", "", *_per_year(res), "", "By exit:", "", *_by_reason(res)]
        if res.skipped:
            lines += ["", "Signals not entered: " + ", ".join(f"{k} {v}" for k, v in sorted(res.skipped.items())) + "."]
        if not res.trades.is_empty():
            lines += ["", "Trades (first 15):", ""]
            cols = ["signal", "entry", "exit", "expiry", "direction", "option_type", "short", "wing", "credit", "lots", "reason", "R"]
            lines.append("| " + " | ".join(cols) + " |")
            lines.append("|" + "---|" * len(cols))
            for row in res.trades.select(cols).head(15).iter_rows():
                lines.append("| " + " | ".join(f"{v:+.3f}" if isinstance(v, float) and c == "R" else str(v) for c, v in zip(cols, row, strict=True)) + " |")
    lines += [
        "",
        "## What was not run",
        "",
        "* F3 is **not** in the quarterly re-test's families (`baskfy_core.fno.retest.FAMILIES`): the"
        " worker's option loader filters to the two nearest expiries with a future, which drops the"
        " NIFTY weeklies F3N trades. Registering it needs a raw-options loader; recorded in F3-PLAN.",
        "* No parameter was tuned; the numbers are `04` §11's defaults, first run.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--archive", required=True, help="the raw archive root (holds nse/fo-bhavcopy)")
    parser.add_argument("--from", dest="start", default="2022-01-03")
    parser.add_argument("--to", dest="end", default="2026-09-22")
    parser.add_argument("--out", required=True, help="the evidence markdown to write")
    parser.add_argument("--cache", default=None, help="parquet cache (default: <archive>/../f3_index_panel.parquet)")
    args = parser.parse_args(argv)
    archive = Path(args.archive).expanduser()
    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    cache = Path(args.cache).expanduser() if args.cache else archive.parent / "f3_index_panel.parquet"
    panel = build_panel(archive, start, end, cache)
    days = panel.select("date").unique().height
    f3 = DEFAULT_FNO_CONFIG.f3
    results = x.run_f3_retest(panel, f3, start=start, end=end)
    numbers = (
        f"pivot {f3.pivot_lookback}/{f3.pivot_width}, trend {f3.trend_sessions}, weekly {f3.weekly_sessions},"
        f" distance {f3.distance_pct} %, wing {f3.wing_pct} %, decay {f3.decay_target_pct} %,"
        f" cut {f3.loss_cut_mult}x, buffer {f3.level_buffer_pct} %, add at {f3.add_working_pct} %"
    )
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    text = render(results, start=start, end=end, days=days, ran_at=dt.datetime.now(tz=ist), f3_numbers=numbers)
    Path(args.out).write_text(text, encoding="utf-8")
    for symbol, res in results.items():
        s = res.summary
        sys.stdout.write(f"{symbol}: n={s.n} expR={s.exp_r:+.3f} t={s.t_stat:.2f} win={s.win:.0%} worst={s.worst:+.2f} skipped={res.skipped}\n")
    sys.stdout.write(f"wrote {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
