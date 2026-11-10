"""PM0.14: the overlay test across many indices: each on itself, each as the book's exit signal, and votes fixed in advance.

usage: PM_DATA=<export> python explore_multi_index.py     (run from the folder holding that export's out/from2014; writes out/multi_index/)"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import polars as pl

from portfolioml import overlay as O, tranches
from run_longrun import CFG, load

DATA = Path(os.environ.get("PM_DATA", "data"))
OUT = Path("out/multi_index")
TERMINAL = "b"
GROUPS = {
    "large": {"NIFTY 50": "NIFTY 50", "NIFTY Next 50": "NIFTY NEXT 50", "NIFTY 100": "NIFTY 100"},
    "broad": {"NIFTY 500": "NIFTY 500", "NIFTY 500 Multicap 50:25:25": "NIFTY500 MULTICAP", "NIFTY500 Equal Weight": "NIFTY500 EQUAL WEIGHT", "NIFTY Total Market": "NIFTY TOTAL MKT", "NIFTY LargeMidcap 250": "NIFTY LARGEMID250", "NIFTY500 LargeMidSmall Equal-Cap": "NIFTY500 LMS EQL"},
    "mid_small": {"NIFTY Midcap 150": "NIFTY MIDCAP 150", "NIFTY MidSmallcap 400": "NIFTY MIDSML 400", "NIFTY Smallcap 250": "NIFTY SMLCAP 250", "NIFTY Smallcap 500": "NIFTY SMLCAP 500", "NIFTY Microcap 250": "NIFTY MICROCAP250"},
}
REPS = ("NIFTY 100", "NIFTY 500", "NIFTY MidSmallcap 400")
LATEST_START = "2013-12-31"
SWITCH_COST_ALONE, PERIODS_A, PERIODS_SIGNAL = 0.0025, (200, 100, 50), (100, 200)
MIN_DD_CUT, MAX_CAGR_COST = 0.25, 0.03
pl.Config.set_tbl_rows(150); pl.Config.set_tbl_width_chars(190); pl.Config.set_float_precision(3); pl.Config.set_tbl_cols(30)


def closes() -> tuple[pl.DataFrame, pl.DataFrame]:
    """Wide closes per available index, and the availability table."""
    raw = pl.read_csv(DATA / "index_close_all.csv.gz", try_parse_dates=True)
    rows, keep = [], {}
    for group, members in GROUPS.items():
        for label, name in members.items():
            g = raw.filter(pl.col("index_name") == name)
            start = None if g.is_empty() else str(g["date"].min())
            ok = start is not None and start <= LATEST_START
            rows.append({"group": group, "index": label, "source_name": name, "start": start, "end": None if g.is_empty() else str(g["date"].max()), "bars": g.height, "used": ok})
            if ok:
                keep[label] = name
    wide = raw.filter(pl.col("index_name").is_in(list(keep.values()))).pivot(on="index_name", index="date", values="close", aggregate_function="first").sort("date")
    return wide.rename({v: k for k, v in keep.items()}).with_columns(pl.exclude("date").forward_fill()), pl.DataFrame(rows)


def above(px: np.ndarray, k: int) -> np.ndarray:
    """Close over its k-day average; warm-up counts as above, as in PM0.13."""
    avg = pl.Series(px).rolling_mean(k, min_samples=k).to_numpy()
    return np.where(np.isnan(avg), True, px > avg).astype(bool)


def on_itself(wide: pl.DataFrame, avail: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    rows, days = [], wide["date"].to_list()
    group_of = {r["index"]: r["group"] for r in avail.to_dicts()}
    labels = [c for c in wide.columns if c != "date"]
    for label in labels:
        px = wide[label].fill_null(strategy="backward").to_numpy().astype(float)
        level = (px / px[0])[:, None]
        for name, held in [("buy_and_hold", np.ones(len(px), dtype=bool)), *[(f"close_gt_{k}dma", O.positions(above(px, k))) for k in PERIODS_A]]:
            v = O.run(level, [], held, days, cost=SWITCH_COST_ALONE)
            s = O.stats(v, days, held)
            rows.append({"group": group_of[label], "index": label, "rule": name, **{k: s[k] for k in ("cagr", "max_dd", "time_invested", "switches_per_year")}})
    r = np.diff(np.log(wide.select(labels).fill_null(strategy="backward").to_numpy().astype(float)), axis=0)
    corr = pl.DataFrame({"index": labels, **{labels[i]: np.corrcoef(r.T)[:, i] for i in range(len(labels))}})
    return pl.DataFrame(rows), corr


def signal_set(wide: pl.DataFrame, days: list) -> dict[str, tuple[str, str, np.ndarray]]:
    """name → (family, rule key, state on the book's sessions)."""
    out: dict = {}
    labels = [c for c in wide.columns if c != "date"]
    series = {n: {k: pl.DataFrame({"date": wide["date"], "a": above(wide[k].fill_null(strategy="backward").to_numpy().astype(float), n)}) for k in labels} for n in PERIODS_SIGNAL}
    mat = {}
    for n in PERIODS_SIGNAL:
        cols = [pl.DataFrame({"date": days}).join(series[n][k], on="date", how="left").with_columns(pl.col("a").forward_fill().fill_null(True))["a"].to_numpy().astype(bool) for k in labels]
        mat[n] = np.column_stack(cols)
        for i, k in enumerate(labels):
            out[f"B|{k}|{n}dma"] = ("B", f"{k} {n}dma", mat[n][:, i])
        reps = mat[n][:, [labels.index(r) for r in REPS]]
        out[f"C|majority_all|{n}dma"] = ("C", f"majority of all, {n}dma", O.majority(mat[n]))
        out[f"C|2_of_3|{n}dma"] = ("C", f"2 of 3 reps, {n}dma", O.at_least(reps, 2))
        out[f"C|3_of_3|{n}dma"] = ("C", f"3 of 3 reps, {n}dma", O.at_least(reps, 3))
    return out


def book_rows(book: str, detail: dict, sigs: dict) -> list[dict]:
    days, slices, periods = detail["days"], detail["slices"], detail["periods"]
    def row(fam: str, rule: str, version: str, held: np.ndarray) -> dict:
        s = O.stats(O.run(slices, periods, held, days), days, held)
        return {"book": book, "family": fam, "rule": rule, "version": version, **s, "cagr_after_tax": O.stats(O.run(slices, periods, held, days, tax=True), days, held)["cagr"]}
    rows = [row("none", "buy and hold the book", "-", np.ones(len(days), dtype=bool))]
    for fam, rule, state in sigs.values():
        rows.append(row(fam, rule, "plain", O.positions(state)))
        rows.append(row(fam, rule, "5_session", O.positions(O.debounce(state))))
    return rows


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    wide, avail = closes()
    avail.write_csv(OUT / "indices_available.csv"); print(avail)
    a, corr = on_itself(wide, avail)
    a.write_csv(OUT / "a_each_index.csv"); corr.write_csv(OUT / "a_correlation.csv")
    print(a.pivot(on="rule", index=["group", "index"], values="cagr")); print(a.pivot(on="rule", index=["group", "index"], values="max_dd"))
    ds, scores, marks, _ = load(TERMINAL, True)
    flat = scores.select("instrument_id", "date", pl.lit(0.0).alias("score"))
    books = {"top20_skip": (scores, CFG(top_n=20, skip_held=True, terminal=TERMINAL, cost_per_side=O.COST)), "investable_ew": (flat, CFG(top_n=10_000, terminal=TERMINAL, cost_per_side=O.COST))}
    rows = []
    for name, (s, cfg) in books.items():
        _, _, detail = tranches.simulate(ds, s, "score", cfg, marks=marks, detail=True)
        rows += book_rows(name, detail, signal_set(wide, detail["days"]))
        print(name, flush=True)
    t = pl.DataFrame(rows)
    base = t.filter(pl.col("family") == "none").select("book", pl.col("cagr").alias("c0"), pl.col("max_dd").alias("d0"))
    j = t.filter(pl.col("family") != "none").join(base, on="book").with_columns(((pl.col("max_dd") - pl.col("d0")) * 100).alias("dd_cut_pts"), ((pl.col("c0") - pl.col("cagr")) * 100).alias("cagr_cost_pts")).with_columns(
        ((pl.col("dd_cut_pts") >= MIN_DD_CUT * 100) & (pl.col("cagr_cost_pts") <= MAX_CAGR_COST * 100)).alias("pass"))
    t.write_csv(OUT / "b_c_rows.csv"); j.write_csv(OUT / "b_c_tested.csv")
    pairs = j.group_by("book", "family", "rule").agg(pl.col("pass").all().alias("pass_both"), pl.col("pass").any().alias("pass_either"), pl.col("pass").filter(pl.col("version") == "plain").first().alias("plain"), pl.col("pass").filter(pl.col("version") == "5_session").first().alias("five")).sort("book", "family", "rule")
    pairs.write_csv(OUT / "pairs.csv")
    print(t.sort("book", "family", "rule", "version"))
    print(pairs.filter(pl.col("pass_either")))
    print(f"TESTED {j.height} rows, passed {j['pass'].sum()}; {pairs.height} rule pairs, passed both {pairs['pass_both'].sum()}, either {pairs['pass_either'].sum()}")


if __name__ == "__main__":
    main()
