"""PM0.15: entry timing on corrected data, dead names, variant (b). A: is the day informative. B: variable tranche size. C: per-stock triggers.

usage: PM_DATA=<export> python explore_timing.py      (run from the folder holding that export's out/from2014; writes out/timing/)"""

from __future__ import annotations

import os
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

from explore_multibagger import PARAMS, ROUNDS, TRAIN_STRIDE
from portfolioml import census as C, panel, timing as T, tranches
from portfolioml.features import ALL_FEATURES
from portfolioml.labels import DEATH_SESSIONS
from run_longrun import CFG, load

DATA = Path(os.environ.get("PM_DATA", "data"))
SOURCE = DATA / "dataset_mb_u100_h250_tb.parquet"
OUT = Path("out/timing")
HORIZON, FIRST_YEAR, TERMINAL = 250, 2014, "b"
BINARY = {k: v for k, v in PARAMS.items() if k not in ("objective", "metric", "eval_at", "lambdarank_truncation_level")} | {"objective": "binary", "metric": "binary_logloss", "num_threads": 4}
SUB = {"2014-2019": (2014, 2019), "2020-2025": (2020, 2025)}
WATCH, WINDOW, MAX_LINES = 50, 21, 20
pl.Config.set_tbl_rows(90); pl.Config.set_tbl_width_chars(200); pl.Config.set_float_precision(3); pl.Config.set_tbl_cols(30)


def deployability() -> pl.DataFrame:
    lf = pl.scan_parquet(SOURCE)
    sessions = lf.select("date").unique().collect()["date"].sort().to_list()
    frame = lf.filter(pl.col("universe")).select("instrument_id", "date", "label", *ALL_FEATURES).collect()
    lab = frame.filter(pl.col("label").is_not_null())
    x = lambda df: df.select(ALL_FEATURES).to_numpy().astype(np.float32)  # noqa: E731
    out = []
    for y in sorted({d.year for d in sessions if d.year >= FIRST_YEAR}):
        before = [d for d in sessions if d.year < y]
        if len(before) < HORIZON + 300:
            continue
        train_end = before[-(HORIZON + 6)]
        train = lab.filter((pl.col("date") <= train_end) & pl.col("date").is_in(list(set(before[::TRAIN_STRIDE]))))
        test = frame.filter(pl.col("date").dt.year() == y)
        if test.is_empty() or train["label"].sum() < 50:
            continue
        t = time.time()
        m = lgb.train(BINARY, lgb.Dataset(x(train), train["label"].to_numpy()), ROUNDS)
        out.append(test.select("date", "label", pl.Series("p", m.predict(x(test)))))
        print(y, f"{time.time()-t:.0f}s", flush=True)
    p = pl.concat(out)
    daily = p.group_by("date").agg(pl.col("p").mean().alias("deployability"), pl.col("label").mean().alias("cohort_hit"), pl.col("label").is_not_null().sum().alias("labelled")).sort("date")
    daily.write_csv(OUT / "deployability_daily.csv")
    return daily


def market_frame(daily: pl.DataFrame, idx: pl.DataFrame, ds: pl.DataFrame) -> pl.DataFrame:
    px = pl.col("smallcap250")
    sc = idx.select("date", "smallcap250").drop_nulls().sort("date").with_columns((px / px.rolling_max(252, min_samples=252) - 1).alias("sc_dd252"), (px > px.rolling_mean(100, min_samples=100)).alias("sc_above_100dma"))
    br = ds.group_by("date").agg(pl.col("breadth200").first())
    return daily.join(sc, on="date", how="left").join(br, on="date", how="left").sort("date")


def buckets(f: pl.DataFrame, col: str, n: int = 5) -> pl.DataFrame:
    return f.with_columns(((pl.col(col).rank("ordinal") - 1) * n // pl.col(col).count()).cast(pl.Int32).alias("bucket"))


def part_a(market: pl.DataFrame, picks: pl.DataFrame) -> None:
    months, rows = market.filter(pl.col("cohort_hit").is_not_null()).with_columns(pl.col("date").dt.strftime("%Y-%m").alias("month"), pl.col("date").dt.year().alias("year")), []
    measures = {"deployability": 1, "breadth200": 1, "sc_dd252": 1, "sc_above_100dma": 1}
    for name in measures:
        col = pl.col(name).cast(pl.Float64)
        m = months.group_by("month").agg(col.mean().alias("x"), pl.col("cohort_hit").mean().alias("y")).sort("month")
        y = months.group_by("year").agg(col.mean().alias("x"), pl.col("cohort_hit").mean().alias("y")).sort("year")
        rows.append({"measure": name, "rank_corr_by_month": T.spearman(m["x"].to_numpy(), m["y"].to_numpy()), "months": m.height, "rank_corr_by_year": T.spearman(y["x"].to_numpy(), y["y"].to_numpy()), "years": y.height})
    corr = pl.DataFrame(rows); corr.write_csv(OUT / "a_rank_correlation.csv"); print(corr)
    p = picks.join(market.select("date", "deployability", "breadth200", "sc_dd252", "sc_above_100dma"), on="date", how="left")
    out = []
    for name, col, n in (("deployability quintile", "deployability", 5), ("breadth200 quintile", "breadth200", 5), ("smallcap 252d drawdown quintile", "sc_dd252", 5)):
        dates = p.select("date", col).unique().drop_nulls()
        keyed = p.join(buckets(dates, col, n).select("date", "bucket"), on="date")
        for (b,), g in keyed.group_by("bucket", maintain_order=True):
            out.append({"benchmark": name, "bucket": str(b), "dates": g["date"].n_unique(), "picks": g.height, "range_low": float(g[col].min()), "range_high": float(g[col].max()), "top20_hit": g["label"].mean(), "median_12m": g["fwd_ret_h"].median(), "halved": (g["fwd_ret_h"] <= -0.5).mean()})
    for val, label in ((True, "above"), (False, "below")):
        g = p.filter(pl.col("sc_above_100dma") == val)
        out.append({"benchmark": "smallcap vs 100-DMA", "bucket": label, "dates": g["date"].n_unique(), "picks": g.height, "range_low": None, "range_high": None, "top20_hit": g["label"].mean(), "median_12m": g["fwd_ret_h"].median(), "halved": (g["fwd_ret_h"] <= -0.5).mean()})
    t = pl.DataFrame(out).sort("benchmark", "bucket"); t.write_csv(OUT / "a_buckets.csv"); print(t)


class Grid:
    def __init__(self, bars: pl.DataFrame) -> None:
        self.sessions = bars["date"].unique().sort().to_list()
        self.sidx = {d: i for i, d in enumerate(self.sessions)}
        self.ids = {int(v): i for i, v in enumerate(bars["instrument_id"].unique().sort().to_list())}
        self.closes = np.full((len(self.sessions), len(self.ids)), np.nan)
        self.closes[[self.sidx[d] for d in bars["date"].to_list()], [self.ids[int(i)] for i in bars["instrument_id"].to_list()]] = bars["close"].to_numpy()
        self.last = np.where(~np.isnan(self.closes), np.arange(len(self.sessions))[:, None], -1).max(axis=0)
        status = bars.group_by("instrument_id").agg(pl.col("terminal").first())
        self.writes = {int(i): s in ("delisted", "suspended") for i, s in status.iter_rows()}

    def line(self, iid: int, signal: int, entry: float, weight: float) -> T.Line | None:
        a, col = signal + 1, self.ids[iid]
        end = a + HORIZON
        if end > len(self.sessions) or not entry or np.isnan(entry):
            return None
        p = C.paths(self.closes[a:end, [col]], np.array([entry]), np.array([self.last[col] - signal]), np.array([self.writes[iid]]), "b")[:, 0]
        return T.Line(a, p, weight)


def compare(name: str, eq: np.ndarray, days: list) -> tuple[list[dict], dict]:
    rows, stats = [], {}
    frame = pl.DataFrame({"date": days, "v": eq})
    spans = {"full": (days[0].year, days[-1].year), **SUB}
    for span, (a, b) in spans.items():
        g = frame.filter(pl.col("date").dt.year().is_between(a, b))
        s = T.period_stats(g["v"].to_numpy(), g["date"].to_list())
        rows.append({"arm": name, "span": span, **s})
        stats[span] = s
    return rows, stats


def yearly(name: str, eq: np.ndarray, days: list) -> list[dict]:
    frame = pl.DataFrame({"date": days, "v": eq})
    return [{"arm": name, "year": y, "ret": g["v"][-1] / g["v"][0] - 1} for (y,), g in frame.group_by(pl.col("date").dt.year(), maintain_order=True)]


def run_books(arms: dict, grid: Grid, first: int, days: list) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    rows, years, stats = [], [], {}
    for name, lines in arms.items():
        eq, share = T.pool_book(lines, len(grid.sessions), first)
        r, s = compare(name, eq, days)
        rows += [{**x, "lines": len(lines), "wanted_money_granted": share} for x in r]
        years += yearly(name, eq, days)
        stats[name] = s
    return pl.DataFrame(rows), pl.DataFrame(years), stats


def verdict(stats: dict, base: str) -> pl.DataFrame:
    return pl.DataFrame([{"arm": a, "vs": base, **{f"cagr_gain_{k}_pts": (s[k]["cagr"] - stats[base][k]["cagr"]) * 100 for k in SUB}, **{f"dd_change_{k}_pts": (s[k]["max_dd"] - stats[base][k]["max_dd"]) * 100 for k in SUB}, "helps": all(T.helps(stats[base][k], s[k]) for k in SUB)} for a, s in stats.items() if a != base])


def events(flags: pl.DataFrame, flag: str, grid: Grid) -> pl.DataFrame:
    return flags.filter(pl.col(flag)).select("instrument_id", "s").sort("s")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    daily = pl.read_csv(OUT / "deployability_daily.csv", try_parse_dates=True) if (OUT / "deployability_daily.csv").exists() else deployability()
    ds, scores, marks, idx = load(TERMINAL, True)
    market = market_frame(daily, idx, ds)
    picks, _ = tranches.simulate(ds, scores, "score", CFG(top_n=20, skip_held=True, terminal=TERMINAL), marks=marks)
    part_a(market, picks)
    bars = pl.scan_parquet(SOURCE).select("instrument_id", "date", "low", "close", "terminal", "entry_px", "label", "fwd_ret_h").collect().sort("instrument_id", "date")
    grid = Grid(bars)
    months = sorted(picks["date"].unique().to_list())
    first = grid.sidx[months[0]] + 1
    days = grid.sessions[first:]
    weight_book = 1 / 12 / 20
    dep = daily.sort("date")
    dep_dates, dep_scores = dep["date"].to_list(), dep["deployability"].to_numpy()
    where = {d: i for i, d in enumerate(dep_dates)}
    # B
    size = {d: T.past_tercile_size(dep_scores, where[d]) for d in months}
    def book(sized: bool) -> list[T.Line]:
        out = []
        for r in picks.to_dicts():
            line = grid.line(r["instrument_id"], grid.sidx[r["date"]], r["entry_px"], weight_book * (size[r["date"]] if sized else 1.0))
            if line:
                out.append(line)
        return out
    b_rows, b_years, b_stats = run_books({"fixed_monthly": book(False), "variable_0.5_1_1.5": book(True)}, grid, first, days)
    b_rows.write_csv(OUT / "b_books.csv"); b_years.write_csv(OUT / "b_yearly.csv"); print(b_rows); print(b_years.pivot(on="arm", index="year", values="ret").sort("year"))
    print(pl.DataFrame({"size": list(size.values())}).group_by("size").len().sort("size"))
    bv = verdict(b_stats, "fixed_monthly"); bv.write_csv(OUT / "b_verdict.csv"); print(bv)
    # C
    g = pl.col("instrument_id")
    flags = bars.select("instrument_id", "date", "low", "close").with_columns(pl.col("date").replace_strict(grid.sidx, return_dtype=pl.Int32).alias("s"), pl.col("close").rolling_mean(20, min_samples=20).over(g).alias("sma20"), pl.col("close").rolling_max(63, min_samples=63).over(g).alias("hi63"))
    flags = flags.with_columns((pl.col("close") >= pl.col("hi63")).fill_null(False).alias("high63"), ((pl.col("close").shift(1).over(g) > pl.col("sma20").shift(1).over(g)) & (pl.col("low") <= pl.col("sma20"))).fill_null(False).alias("pullback20"))
    ranked = ds.select("instrument_id", "date").join(scores.select("instrument_id", "date", "score"), on=["instrument_id", "date"]).with_columns(pl.col("score").rank("ordinal", descending=True).over("date").alias("rk"))
    top10 = ranked.filter(pl.col("rk") <= 10).with_columns(pl.col("date").replace_strict(grid.sidx, return_dtype=pl.Int32).alias("s")).sort("instrument_id", "s").with_columns((pl.col("s") - pl.col("s").shift(1).over(g)).alias("gap")).filter(pl.col("gap").is_null() | (pl.col("gap") > 60)).select("instrument_id", "s")
    watch = ranked.filter((pl.col("rk") <= WATCH) & pl.col("date").is_in(months)).with_columns(pl.col("date").replace_strict(grid.sidx, return_dtype=pl.Int32).alias("sig"))
    outcome = bars.select("instrument_id", "date", "entry_px", "label", "fwd_ret_h").with_columns(pl.col("date").replace_strict(grid.sidx, return_dtype=pl.Int32).alias("e"))
    immediate = watch.join(outcome, left_on=["instrument_id", "sig"], right_on=["instrument_id", "e"]).filter(pl.col("fwd_ret_h").is_not_null())
    triggers = {"63d_high": events(flags, "high63", grid), "pullback_20dma": events(flags, "pullback20", grid), "first_top10_in_60": top10}
    rows = [{"arm": "buy_immediately_top50", "name_months": immediate.height, "never_bought": 0.0, "bought": immediate.height, **{k: v for k, v in zip(("hit", "median_12m", "halved"), (immediate["label"].mean(), immediate["fwd_ret_h"].median(), (immediate["fwd_ret_h"] <= -0.5).mean()))}}]
    p20 = picks.select("instrument_id", pl.col("date").alias("sig_date"), "label", "fwd_ret_h")
    rows.append({"arm": "book_top20_skip_held", "name_months": p20.height, "never_bought": 0.0, "bought": p20.height, "hit": p20["label"].mean(), "median_12m": p20["fwd_ret_h"].median(), "halved": (p20["fwd_ret_h"] <= -0.5).mean()})
    book_arms: dict = {"book_top20_skip_held": book(False)}
    def immediate_lines() -> list[T.Line]:
        out, last = [], {}
        for r in watch.sort("sig", "rk").join(outcome, left_on=["instrument_id", "sig"], right_on=["instrument_id", "e"]).filter(pl.col("fwd_ret_h").is_not_null()).to_dicts():
            if last.get(r["instrument_id"], -1) >= r["sig"]:
                continue
            line = grid.line(r["instrument_id"], r["sig"], r["entry_px"], 1 / 12 / WATCH)
            if line:
                out.append(line); last[r["instrument_id"]] = line.start + HORIZON - 1
        return out
    book_arms["buy_immediately_top50_skip_held"] = immediate_lines()
    for name, ev in triggers.items():
        w = watch.sort("sig").with_columns((pl.col("sig") + 1).alias("from")).join_asof(ev.rename({"s": "e"}).sort("e"), left_on="from", right_on="e", by="instrument_id", strategy="forward").with_columns((pl.col("e").is_not_null() & (pl.col("e") <= pl.col("sig") + WINDOW)).alias("fired"))
        inside = w.filter(pl.col("sig") + WINDOW < len(grid.sessions))
        got = inside.filter(pl.col("fired")).join(outcome.drop("date"), on=["instrument_id", "e"]).filter(pl.col("fwd_ret_h").is_not_null())
        rows.append({"arm": name, "name_months": inside.height, "never_bought": 1 - inside["fired"].mean(), "bought": got.height, "hit": got["label"].mean(), "median_12m": got["fwd_ret_h"].median(), "halved": (got["fwd_ret_h"] <= -0.5).mean()})
        lines, last = [], {}
        for (_,), m in got.sort("sig", "e", "rk").group_by("sig", maintain_order=True):
            taken = 0
            for r in m.to_dicts():
                if taken >= MAX_LINES or last.get(r["instrument_id"], -1) >= r["e"]:
                    continue
                line = grid.line(r["instrument_id"], r["e"], r["entry_px"], weight_book)
                if line:
                    lines.append(line); last[r["instrument_id"]] = line.start + HORIZON - 1; taken += 1
        book_arms[name] = lines
    c = pl.DataFrame(rows); c.write_csv(OUT / "c_outcomes.csv"); print(c)
    c_rows, c_years, c_stats = run_books(book_arms, grid, first, days)
    c_rows.write_csv(OUT / "c_books.csv"); c_years.write_csv(OUT / "c_yearly.csv"); print(c_rows); print(c_years.pivot(on="arm", index="year", values="ret").sort("year"))
    cv = verdict(c_stats, "book_top20_skip_held"); cv.write_csv(OUT / "c_verdict.csv"); print(cv)


if __name__ == "__main__":
    main()
