"""High-breakout trigger, like for like: exposure-matched control, full deployment, 42/63/126-day highs. Corrected data, dead names, variant (b).

usage: PM_DATA=<export> python explore_trigger_match.py      (run from the folder holding that export's out/from2014; writes out/trigger_match/)"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import polars as pl

from explore_timing import HORIZON, MAX_LINES, SOURCE, SUB, TERMINAL, WATCH, WINDOW, Grid, compare, yearly
from portfolioml import timing as T, tranches
from run_longrun import CFG, load

OUT = Path("out/trigger_match")
HIGHS = (42, 63, 126)
TRANCHE, SLOT = 1 / 12, 1 / 12 / 20
TRAILING = 12
pl.Config.set_tbl_rows(200); pl.Config.set_tbl_width_chars(220); pl.Config.set_float_precision(3); pl.Config.set_tbl_cols(30)


def scaled(lines: list[T.Line], k: float) -> list[T.Line]:
    return [replace(x, weight=x.weight * k) for x in lines]


def avg_share(lines: list[T.Line], n: int, first: int) -> float:
    return float(T.pool_book_detail(lines, n, first)[2].mean())


def match_scale(lines: list[T.Line], target: float, n: int, first: int) -> tuple[float, bool]:
    """Scale in (0, 1] whose full-period average invested share equals ``target``; True if the unscaled book cannot reach it."""
    if avg_share(lines, n, first) <= target:
        return 1.0, True
    lo, hi = 0.0, 1.0
    for _ in range(25):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if avg_share(scaled(lines, mid), n, first) < target else (lo, mid)
    return (lo + hi) / 2, False


def split_lookahead(groups: list[tuple[int, list[T.Line]]]) -> list[T.Line]:
    """As specified: a tranche split equally among the names that fire. Uses the final count, unknown when the first name fires."""
    return [replace(x, weight=TRANCHE / len(m)) for _, m in groups for x in m]


def split_causal(groups: list[tuple[int, list[T.Line]]]) -> list[T.Line]:
    """Slot from the mean count of the last TRAILING tranches whose window had closed by this signal; unspent money stays in cash."""
    out, done = [], []
    for sig, m in groups:
        past = [c for s, c in done if s + WINDOW <= sig][-TRAILING:]
        slot = TRANCHE / max(1.0, float(np.mean(past)) if past else MAX_LINES)
        spent = 0.0
        for x in sorted(m, key=lambda a: a.start):
            w = min(slot, TRANCHE - spent)
            if w <= 0:
                break
            out.append(replace(x, weight=w))
            spent += w
        done.append((sig, len(m)))
    return out


def beats(arm: dict, control: dict) -> bool:
    return all(arm[k]["sharpe"] > control[k]["sharpe"] and arm[k]["max_dd"] > control[k]["max_dd"] for k in SUB)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ds, scores, marks, _ = load(TERMINAL, True)
    picks, _ = tranches.simulate(ds, scores, "score", CFG(top_n=20, skip_held=True, terminal=TERMINAL), marks=marks)
    bars = pl.scan_parquet(SOURCE).select("instrument_id", "date", "close", "terminal", "entry_px", "label", "fwd_ret_h").collect().sort("instrument_id", "date")
    grid = Grid(bars)
    months = sorted(picks["date"].unique().to_list())
    first, n = grid.sidx[months[0]] + 1, len(grid.sessions)
    days = grid.sessions[first:]
    g = pl.col("instrument_id")
    flags = bars.select("instrument_id", "date", "close").with_columns(pl.col("date").replace_strict(grid.sidx, return_dtype=pl.Int32).alias("s"), *[(pl.col("close") >= pl.col("close").rolling_max(h, min_samples=h).over(g)).fill_null(False).alias(f"high{h}") for h in HIGHS])
    ranked = ds.select("instrument_id", "date").join(scores.select("instrument_id", "date", "score"), on=["instrument_id", "date"]).with_columns(pl.col("score").rank("ordinal", descending=True).over("date").alias("rk"))
    watch = ranked.filter((pl.col("rk") <= WATCH) & pl.col("date").is_in(months)).with_columns(pl.col("date").replace_strict(grid.sidx, return_dtype=pl.Int32).alias("sig"))
    outcome = bars.select("instrument_id", "date", "entry_px", "fwd_ret_h").with_columns(pl.col("date").replace_strict(grid.sidx, return_dtype=pl.Int32).alias("e"))

    def trigger(h: int) -> list[tuple[int, list[T.Line]]]:
        ev = flags.filter(pl.col(f"high{h}")).select("instrument_id", pl.col("s").alias("e")).sort("e")
        w = watch.sort("sig").with_columns((pl.col("sig") + 1).alias("from")).join_asof(ev, left_on="from", right_on="e", by="instrument_id", strategy="forward").filter(pl.col("e").is_not_null() & (pl.col("e") <= pl.col("sig") + WINDOW) & (pl.col("sig") + WINDOW < n))
        got = w.join(outcome.drop("date"), on=["instrument_id", "e"]).filter(pl.col("fwd_ret_h").is_not_null())
        groups, last = [], {}
        for (sig,), m in got.sort("sig", "e", "rk").group_by("sig", maintain_order=True):
            lines = []
            for r in m.to_dicts():
                if len(lines) >= MAX_LINES or last.get(r["instrument_id"], -1) >= r["e"]:
                    continue
                line = grid.line(r["instrument_id"], r["e"], r["entry_px"], SLOT)
                if line:
                    lines.append(line); last[r["instrument_id"]] = line.start + HORIZON - 1
            if lines:
                groups.append((sig, lines))
        return groups

    book = [x for x in (grid.line(r["instrument_id"], grid.sidx[r["date"]], r["entry_px"], SLOT) for r in picks.to_dicts()) if x]
    arms: dict[str, list[T.Line]] = {"book_top20_skip_held": book}
    plan = []
    for h in HIGHS:
        groups = trigger(h)
        twentieth = [x for _, m in groups for x in m]
        for label, lines in ((f"high{h}_twentieth", twentieth), (f"high{h}_full_as_specified", split_lookahead(groups)), (f"high{h}_full_causal", split_causal(groups))):
            target = avg_share(lines, n, first)
            k, short = match_scale(book, target, n, first)
            arms[label] = lines
            arms[f"control_for_{label}"] = scaled(book, k)
            plan.append({"high": h, "arm": label, "target_share": target, "scale": k, "control_capped": short})

    rows, years, stats = [], [], {}
    frame = pl.DataFrame({"date": days})
    spans = {"full": (days[0].year, days[-1].year), **SUB}
    for name, lines in arms.items():
        eq, granted, invested = T.pool_book_detail(lines, n, first)
        r, s = compare(name, eq, days)
        shares = {sp: float(invested[frame["date"].dt.year().is_between(a, b).to_numpy()].mean()) for sp, (a, b) in spans.items()}
        rows += [{**x, "avg_invested": shares[x["span"]], "lines": len(lines), "wanted_money_granted": granted} for x in r]
        years += yearly(name, eq, days)
        stats[name] = s
    books, annual = pl.DataFrame(rows), pl.DataFrame(years)
    books.write_csv(OUT / "books.csv"); annual.write_csv(OUT / "yearly.csv")
    print(books); print(annual.pivot(on="arm", index="year", values="ret").sort("year"))

    v = []
    for p in plan:
        a, c = stats[p["arm"]], stats[f"control_for_{p['arm']}"]
        v.append({**p, **{f"sharpe_diff_{k}": a[k]["sharpe"] - c[k]["sharpe"] for k in SUB}, **{f"dd_diff_{k}_pts": (a[k]["max_dd"] - c[k]["max_dd"]) * 100 for k in SUB}, "beats_control": beats(a, c)})
    verdict = pl.DataFrame(v); verdict.write_csv(OUT / "verdict.csv"); print(verdict)
    passed = verdict.filter(pl.col("arm").str.ends_with("_twentieth") & pl.col("beats_control")).height
    print(f"RULE (trigger book as in PM0.15 vs its control): {passed} of {len(HIGHS)} windows pass; real={passed >= 2}")


if __name__ == "__main__":
    main()
