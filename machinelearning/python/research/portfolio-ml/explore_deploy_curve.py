"""PM0.17: how much to put per fired name on the 63-day-high trigger, each size beside an exposure-matched control. Two levered arms, for information.

usage: PM_DATA=<export> python explore_deploy_curve.py      (run from the folder holding that export's out/from2014; writes out/deploy_curve/)"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

from explore_timing import HORIZON, MAX_LINES, SOURCE, TERMINAL, WATCH, WINDOW, Grid
from portfolioml import leverage as L, timing as T, tranches
from run_longrun import CFG, load

OUT = Path("out/deploy_curve")
HIGH, TRANCHE = 63, 1 / 12
SIZES = {"1/20": 1 / 20, "1/16": 1 / 16, "1/12": 1 / 12, "1/10": 1 / 10, "1/8": 1 / 8}
LEVERED = {"1/12 gross 130%": 1.3, "1/12 gross 150%": 1.5}
SPANS = {"full": None, "2014-2019": (2014, 2019), "2020-2025": (2020, 2025)}
DD_LIMIT = -0.35
pl.Config.set_tbl_rows(100); pl.Config.set_tbl_width_chars(190); pl.Config.set_float_precision(3); pl.Config.set_tbl_cols(30)


def mask(days: list, span: tuple | None) -> np.ndarray:
    return np.ones(len(days), dtype=bool) if span is None else np.array([span[0] <= d.year <= span[1] for d in days])


def scaled(lines: list[T.Line], k: float) -> list[T.Line]:
    return [T.Line(x.start, x.path, x.weight * k) for x in lines]


def worst_year(eq: np.ndarray, days: list, span: tuple | None) -> float:
    f = pl.DataFrame({"date": days, "v": eq}).filter(pl.lit(mask(days, span)))
    return float(min(g["v"][-1] / g["v"][0] - 1 for _, g in f.group_by(pl.col("date").dt.year())))


def figures(eq: np.ndarray, gross: np.ndarray, days: list, span: tuple | None) -> dict:
    m = mask(days, span)
    picked = [d for d, ok in zip(days, m) if ok]
    return {**T.period_stats(eq[m], picked), "avg_invested": float(gross[m].mean()), "peak_invested": float(gross[m].max()), "worst_year": worst_year(eq, days, span)}


def control(book: list[T.Line], target: float, n: int, first: int, days: list, span: tuple | None, cap: float) -> tuple[float, np.ndarray, np.ndarray]:
    """Scale the book of record so its average gross share over ``span`` meets ``target``, under the same cap."""
    low, high = 0.0, 3.0
    m = mask(days, span)
    for _ in range(9):
        k = (low + high) / 2
        _, gross, _ = L.pool_levered(scaled(book, k), n, first, cap)
        low, high = (k, high) if gross[m].mean() < target else (low, k)
    k = (low + high) / 2
    eq, gross, _ = L.pool_levered(scaled(book, k), n, first, cap)
    return k, eq, gross


def fired_lines(w: pl.DataFrame, grid: Grid) -> list[T.Line]:
    """PM0.15's selection at a twentieth of a tranche per name: at most twenty names a tranche, skip-held, first fired first."""
    lines, last = [], {}
    for (_,), m in w.sort("sig", "e", "rk").group_by("sig", maintain_order=True):
        taken = 0
        for r in m.to_dicts():
            if taken >= MAX_LINES or last.get(r["instrument_id"], -1) >= r["e"]:
                continue
            line = grid.line(r["instrument_id"], r["e"], r["entry_px"], TRANCHE / 20)
            if line:
                lines.append(line); last[r["instrument_id"]] = line.start + HORIZON - 1; taken += 1
    return lines


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ds, scores, marks, _ = load(TERMINAL, True)
    picks, _ = tranches.simulate(ds, scores, "score", CFG(top_n=20, skip_held=True, terminal=TERMINAL), marks=marks)
    bars = pl.scan_parquet(SOURCE).select("instrument_id", "date", "close", "terminal", "entry_px", "fwd_ret_h").collect().sort("instrument_id", "date")
    grid = Grid(bars)
    months = sorted(picks["date"].unique().to_list())
    first = grid.sidx[months[0]] + 1
    n, days = len(grid.sessions), grid.sessions[first:]
    book = [x for x in (grid.line(r["instrument_id"], grid.sidx[r["date"]], r["entry_px"], TRANCHE / 20) for r in picks.to_dicts()) if x]
    ranked = ds.select("instrument_id", "date").join(scores.select("instrument_id", "date", "score"), on=["instrument_id", "date"]).with_columns(pl.col("score").rank("ordinal", descending=True).over("date").alias("rk"))
    watch = ranked.filter((pl.col("rk") <= WATCH) & pl.col("date").is_in(months)).with_columns(pl.col("date").replace_strict(grid.sidx, return_dtype=pl.Int32).alias("sig"))
    outcome = bars.select("instrument_id", "date", "entry_px", "fwd_ret_h").with_columns(pl.col("date").replace_strict(grid.sidx, return_dtype=pl.Int32).alias("e"))
    ev = bars.select("instrument_id", "date", "close").with_columns(pl.col("close").rolling_max(HIGH, min_samples=HIGH).over("instrument_id").alias("hi")).filter(pl.col("close") >= pl.col("hi")).with_columns(pl.col("date").replace_strict(grid.sidx, return_dtype=pl.Int32).alias("e")).select("instrument_id", "e").sort("e")
    w = watch.sort("sig").with_columns((pl.col("sig") + 1).alias("from")).join_asof(ev, left_on="from", right_on="e", by="instrument_id", strategy="forward").filter(pl.col("e").is_not_null() & (pl.col("e") <= pl.col("sig") + WINDOW)).join(outcome.drop("date"), on=["instrument_id", "e"]).filter(pl.col("fwd_ret_h").is_not_null())
    base = fired_lines(w, grid)
    arms = {**{k: (scaled(base, s * 20), 1.0, False) for k, s in SIZES.items()}, **{k: (scaled(base, SIZES["1/12"] * 20), c, True) for k, c in LEVERED.items()}}
    rows, years = [], []
    for name, (lines, cap, levered) in arms.items():
        eq, gross, forced = L.pool_levered(lines, n, first, cap)
        f = pl.DataFrame({"date": days, "v": eq})
        years += [{"arm": name, "year": y, "ret": g["v"][-1] / g["v"][0] - 1} for (y,), g in f.group_by(pl.col("date").dt.year(), maintain_order=True)]
        for span_name, span in SPANS.items():
            t = figures(eq, gross, days, span)
            rows.append({"arm": name, "role": "trigger", "span": span_name, "levered": levered, "forced_liquidations": forced, "k": None, "lines": len(lines), **t})
            k, ceq, cgross = control(book, t["avg_invested"], n, first, days, span, cap)
            rows.append({"arm": name, "role": "control", "span": span_name, "levered": levered, "forced_liquidations": None, "k": k, "lines": len(book), **figures(ceq, cgross, days, span)})
            if span_name == "full":
                f2 = pl.DataFrame({"date": days, "v": ceq})
                years += [{"arm": f"control {name}", "year": y, "ret": g["v"][-1] / g["v"][0] - 1} for (y,), g in f2.group_by(pl.col("date").dt.year(), maintain_order=True)]
        print(name, flush=True)
    t = pl.DataFrame(rows); t.write_csv(OUT / "arms.csv"); pl.DataFrame(years).write_csv(OUT / "yearly.csv"); print(t)
    plain = t.filter((pl.col("role") == "trigger") & ~pl.col("levered") & (pl.col("span") != "full"))
    ok = plain.group_by("arm").agg((pl.col("max_dd") > DD_LIMIT).all().alias("above_limit"), pl.col("max_dd").min().alias("worst_dd"))
    ok.write_csv(OUT / "dd_limit.csv"); print(ok)


if __name__ == "__main__":
    main()
