"""PM0.16: like-for-like check of the high-breakout trigger: exposure-matched controls, full deployment, three windows.

usage: PM_DATA=<export> python explore_breakout_check.py      (run from the folder holding that export's out/from2014; writes out/breakout/)"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

from explore_timing import HORIZON, MAX_LINES, SOURCE, TERMINAL, WATCH, WINDOW, Grid
from portfolioml import timing as T, tranches
from run_longrun import CFG, load

OUT = Path("out/breakout")
WINDOWS = (42, 63, 126)
SPANS = {"full": None, "2014-2019": (2014, 2019), "2020-2025": (2020, 2025)}
TRANCHE = 1 / 12
KINDS = ("partial_1_20", "full_past_count", "full_ex_post")
NEEDED = 2
pl.Config.set_tbl_rows(120); pl.Config.set_tbl_width_chars(190); pl.Config.set_float_precision(3); pl.Config.set_tbl_cols(30)


def past_count(counts: list[tuple[int, int]], signal: int, window: int, default: float, lookback: int = 12) -> float:
    """Mean names fired over the last ``lookback`` tranches whose window closed before ``signal``."""
    done = [n for s, n in counts if s + window < signal][-lookback:]
    return float(np.mean(done)) if done else default


def beats(trigger: dict, control: dict) -> bool:
    return trigger["sharpe"] > control["sharpe"] and trigger["max_dd"] > control["max_dd"]


def mask(days: list, span: tuple | None) -> np.ndarray:
    return np.ones(len(days), dtype=bool) if span is None else np.array([span[0] <= d.year <= span[1] for d in days])


def scaled(lines: list[T.Line], k: float) -> list[T.Line]:
    return [T.Line(x.start, x.path, x.weight * k) for x in lines]


def evaluate(eq: np.ndarray, inv: np.ndarray, days: list, span: tuple | None) -> dict:
    m = mask(days, span)
    picked = [d for d, ok in zip(days, m) if ok]
    return {**T.period_stats(eq[m], picked), "avg_invested": float(inv[m].mean())}


def match(book: list[T.Line], target: float, n: int, first: int, days: list, span: tuple | None) -> tuple[float, np.ndarray, np.ndarray]:
    """The scale k whose control has the target average invested share over ``span``."""
    low, high = 0.0, 1.6
    for _ in range(9):
        k = (low + high) / 2
        _, _, inv = T.pool_book_detail(scaled(book, k), n, first)
        if inv[mask(days, span)].mean() < target:
            low = k
        else:
            high = k
    k = (low + high) / 2
    eq, _, inv = T.pool_book_detail(scaled(book, k), n, first)
    return k, eq, inv


def trigger_arms(w: pl.DataFrame, grid: Grid) -> dict[str, list[T.Line]]:
    """Three sizings of the same fired names, skip-held throughout."""
    arms: dict = {k: [] for k in KINDS}
    counts: list[tuple[int, int]] = []
    tranches_seen = []
    for (sig,), m in w.sort("sig", "e", "rk").group_by("sig", maintain_order=True):
        tranches_seen.append((sig, m.to_dicts()))
    last: dict = {k: {} for k in KINDS}
    for sig, rows in tranches_seen:
        def eligible(kind: str) -> list[dict]:
            seen, out = {}, []
            for r in rows:
                if last[kind].get(r["instrument_id"], -1) < r["e"] and r["instrument_id"] not in seen:
                    seen[r["instrument_id"]] = 1
                    out.append(r)
            return out
        fired = eligible("full_ex_post")
        estimate = past_count(counts, sig, WINDOW, float(MAX_LINES))
        taken = {"partial_1_20": 0, "full_past_count": 0.0}
        for kind in KINDS:
            names = eligible(kind) if kind != "full_ex_post" else fired
            for r in names:
                if kind == "partial_1_20":
                    if taken[kind] >= MAX_LINES:
                        break
                    weight = TRANCHE / MAX_LINES
                    taken[kind] += 1
                elif kind == "full_past_count":
                    weight = TRANCHE / max(estimate, 1.0)
                    if taken[kind] + weight > TRANCHE + 1e-12:
                        break
                    taken[kind] += weight
                else:
                    weight = TRANCHE / len(fired)
                line = grid.line(r["instrument_id"], r["e"], r["entry_px"], weight)
                if line:
                    arms[kind].append(line)
                    last[kind][r["instrument_id"]] = line.start + HORIZON - 1
        counts.append((sig, len(fired)))
    return arms


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
    rows, years, verdicts = [], [], []
    base_eq, _, base_inv = T.pool_book_detail(book, n, first)
    def add(arm: str, eq: np.ndarray, inv: np.ndarray, span_name: str, span: tuple | None, lines: int, extra: dict) -> dict:
        s = evaluate(eq, inv, days, span)
        rows.append({"arm": arm, "span": span_name, **s, "lines": lines, **extra})
        return s
    for name, span in SPANS.items():
        add("book_of_record", base_eq, base_inv, name, span, len(book), {"window": 0, "k": 1.0})
    def yr(arm: str, eq: np.ndarray) -> None:
        f = pl.DataFrame({"date": days, "v": eq})
        years.extend({"arm": arm, "year": y, "ret": g["v"][-1] / g["v"][0] - 1} for (y,), g in f.group_by(pl.col("date").dt.year(), maintain_order=True))
    yr("book_of_record", base_eq)
    for W in WINDOWS:
        ev = bars.select("instrument_id", "date", "close").with_columns(pl.col("close").rolling_max(W, min_samples=W).over("instrument_id").alias("hi")).filter(pl.col("close") >= pl.col("hi")).with_columns(pl.col("date").replace_strict(grid.sidx, return_dtype=pl.Int32).alias("e")).select("instrument_id", "e").sort("e")
        w = watch.sort("sig").with_columns((pl.col("sig") + 1).alias("from")).join_asof(ev, left_on="from", right_on="e", by="instrument_id", strategy="forward").filter(pl.col("e").is_not_null() & (pl.col("e") <= pl.col("sig") + WINDOW)).join(outcome.drop("date"), on=["instrument_id", "e"]).filter(pl.col("fwd_ret_h").is_not_null())
        arms = trigger_arms(w, grid)
        for kind, lines in arms.items():
            arm = f"{kind}_{W}d"
            eq, _, inv = T.pool_book_detail(lines, n, first)
            yr(arm, eq)
            spans = {}
            for name, span in SPANS.items():
                t = add(arm, eq, inv, name, span, len(lines), {"window": W, "k": None})
                k, ceq, cinv = match(book, t["avg_invested"], n, first, days, span)
                c = add(f"control_for_{arm}", ceq, cinv, name, span, len(book), {"window": W, "k": k})
                spans[name] = (t, c)
                if name == "full":
                    yr(f"control_for_{arm}", ceq)
            verdicts.append({"kind": kind, "window": W, **{f"{sp}_beats": beats(*spans[sp]) for sp in ("2014-2019", "2020-2025")}, "real_this_window": all(beats(*spans[sp]) for sp in ("2014-2019", "2020-2025"))})
            print(arm, flush=True)
    t, y, v = pl.DataFrame(rows), pl.DataFrame(years), pl.DataFrame(verdicts)
    t.write_csv(OUT / "arms.csv"); y.write_csv(OUT / "yearly.csv"); v.write_csv(OUT / "verdicts.csv")
    print(t); print(v)
    r = v.group_by("kind").agg(pl.col("real_this_window").sum().alias("windows_passing")).with_columns((pl.col("windows_passing") >= NEEDED).alias("real"))
    r.write_csv(OUT / "real.csv"); print(r)


if __name__ == "__main__":
    main()
