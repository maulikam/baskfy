"""PM0.11: outcome census on price bars, 2011 on, dead names in, both terminal variants. No model, no network.

usage: PM_DATA=<export> python run_census.py        (writes out/census/)"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import polars as pl

from portfolioml import census as C

DATA = Path(os.environ.get("PM_DATA", "data"))
SOURCE = DATA / "dataset_mb_u100_h250_tb.parquet"
OUT = Path("out/census")
HORIZONS = {1: 250, 2: 500, 3: 750, 5: 1250}
GAINS = (1.5, 2.0, 3.0, 5.0, 10.0)
LOSSES = (-0.25, -0.5, -0.75)
VARIANTS = ("a", "b")
NOTE = ("PRICE RETURN ONLY: dividends are sparse in the data, so every figure excludes them. Entry is the next session's open after the first session of each "
        "quarter; thresholds are tested on adjusted closes. A dead name's path is variant a (last close carried) or b (zero 60 sessions after its last bar; merged names always a). "
        "A live name with no bars to the endpoint, and every name in a cohort whose window runs past the data, counts as unknown. Market cap and sector breakdowns wait for the crawl.")
NOT_DEATHS = ("merged",)
COLS = ["instrument_id", "date", "close", "entry_px", "universe", "eligible", "near_sus_gap", "n_hist", "close_raw", "turnover20", "r252", "breadth200", "smallcap250_vs_sma50", "terminal"]
pl.Config.set_tbl_rows(80); pl.Config.set_tbl_width_chars(200); pl.Config.set_float_precision(3); pl.Config.set_tbl_cols(30)


def load() -> tuple[pl.DataFrame, list, pl.DataFrame]:
    frame = pl.scan_parquet(SOURCE).select(COLS).collect()
    sessions = frame["date"].unique().sort().to_list()
    floorless = pl.col("eligible").fill_null(False) & ~pl.col("near_sus_gap").fill_null(False) & (pl.col("close_raw") >= 20) & (pl.col("n_hist") >= 260)
    return frame.with_columns(pl.col("universe").fill_null(False).alias("standard"), floorless.fill_null(False).alias("no_floor")), sessions, frame.group_by("instrument_id").agg(pl.col("terminal").first())


def quarter_starts(sessions: list) -> list[int]:
    out, seen = [], set()
    for i, d in enumerate(sessions):
        q = (d.year, (d.month - 1) // 3)
        if q not in seen:
            seen.add(q)
            out.append(i)
    return out


def bands(f: pl.DataFrame) -> pl.DataFrame:
    tv = pl.col("turnover20") / 1e7
    age, px, br = pl.col("n_hist"), pl.col("close_raw"), pl.col("breadth200")
    def cut(col: pl.Expr, edges: list, names: list[str]) -> pl.Expr:
        e = pl.when(col.is_null()).then(pl.lit("unknown"))
        for edge, name in zip(edges, names):
            e = e.when(col < edge).then(pl.lit(name))
        return e.otherwise(pl.lit(names[-1]))
    return f.with_columns(
        cut(tv, [2, 5, 25, 100], ["a <2cr", "b 2-5cr", "c 5-25cr", "d 25-100cr", "e 100cr+"]).alias("turnover_band"),
        cut(age, [500, 1250, 2500], ["a 1-2y", "b 2-5y", "c 5-10y", "d 10y+"]).alias("listing_age_band"),
        cut(px, [50, 100, 250, 500, 1000], ["a <50", "b 50-100", "c 100-250", "d 250-500", "e 500-1000", "f 1000+"]).alias("price_band"),
        cut(br, [0.3, 0.5, 0.7], ["a <30%", "b 30-50%", "c 50-70%", "d 70%+"]).alias("breadth200_band"),
        pl.when(pl.col("smallcap250_vs_sma50").is_null()).then(pl.lit("unknown")).when(pl.col("smallcap250_vs_sma50") > 0).then(pl.lit("above 50dma")).otherwise(pl.lit("below 50dma")).alias("smallcap_regime"),
        pl.when(pl.col("r252").is_null()).then(pl.lit("unknown")).otherwise(((pl.col("r252").rank("ordinal") - 1) * 10 // pl.len()).cast(pl.Int32).cast(pl.String).str.zfill(2)).alias("r252_decile"),
    )


BAND_COLS = ["turnover_band", "listing_age_band", "price_band", "breadth200_band", "smallcap_regime", "r252_decile"]


def build_rows(frame: pl.DataFrame, sessions: list) -> pl.DataFrame:
    ids = frame["instrument_id"].unique().sort().to_numpy()
    pos = {int(v): i for i, v in enumerate(ids)}
    sidx = {d: i for i, d in enumerate(sessions)}
    grid = np.full((len(sessions), len(ids)), np.nan)
    grid[[sidx[d] for d in frame["date"].to_list()], [pos[int(i)] for i in frame["instrument_id"].to_list()]] = frame["close"].to_numpy()
    last = np.where(~np.isnan(grid), np.arange(len(sessions))[:, None], -1).max(axis=0)
    terminal = dict(zip(*frame.group_by("instrument_id").agg(pl.col("terminal").first()).select("instrument_id", "terminal").to_dict(as_series=False).values(), strict=True))
    out = []
    for name in ("standard", "no_floor"):
        for t in quarter_starts(sessions):
            day = frame.filter((pl.col("date") == sessions[t]) & pl.col(name) & pl.col("entry_px").is_not_null())
            if day.is_empty():
                continue
            day = bands(day)
            cols = np.array([pos[int(i)] for i in day["instrument_id"].to_list()])
            status = [terminal[int(i)] for i in day["instrument_id"].to_list()]
            dead = np.array([s != "alive" for s in status])
            writes = np.array([s != "alive" and s not in NOT_DEATHS for s in status])
            for years, h in HORIZONS.items():
                inside = t + h < len(sessions)
                left = last[cols] - t
                complete = inside & ((left >= h) | dead)
                meta = day.select("instrument_id", *BAND_COLS).with_columns(pl.lit(name).alias("universe_pass"), pl.lit(years).alias("years"), pl.lit(sessions[t]).alias("cohort"), pl.Series("resolved", complete))
                if not inside:
                    for v in VARIANTS:
                        out.append(meta.with_columns(pl.lit(v).alias("variant")))
                    continue
                entry = day["entry_px"].to_numpy().astype(float)
                for v in VARIANTS:
                    p = C.paths(grid[t + 1:t + h + 1][:, cols], entry, left, writes, v)
                    extra: dict = {"terminal_mult": p[-1], "low": p.min(axis=0), "ptt": C.peak_to_trough(p), "suspect_step": C.suspicious_step(p)}
                    for g in GAINS:
                        touch = C.first_touch(p, g)
                        extra[f"t_{g}"] = touch
                        extra[f"pre_{g}"] = C.low_before(p, touch)
                    out.append(meta.with_columns(pl.lit(v).alias("variant"), *[pl.Series(k, a) for k, a in extra.items()]))
        print(name, flush=True)
    return pl.concat(out, how="diagonal_relaxed")


def cohort_tables(rows: pl.DataFrame) -> pl.DataFrame:
    keys = ["universe_pass", "variant", "years", "cohort"]
    counts = rows.group_by(keys).agg(pl.len().alias("eligible"), pl.col("resolved").sum().alias("resolved"))
    done = rows.filter(pl.col("resolved"))
    parts = []
    for g in GAINS:
        t, pre = pl.col(f"t_{g}"), pl.col(f"pre_{g}")
        won = t > 0
        wmask, lmask = pl.col("ptt").filter(won), pl.col("ptt").filter(~won)
        parts.append(done.group_by(keys).agg(
            pl.lit("gain").alias("kind"), pl.lit(g).alias("threshold"), won.mean().alias("touch_rate"), (pl.col("terminal_mult") >= g).mean().alias("terminal_rate"),
            won.sum().alias("winners"), (pl.col("terminal_mult").filter(won) >= g).mean().alias("winners_still_above"),
            t.filter(won).median().alias("median_sessions_to_touch"), t.filter(won).quantile(0.9).alias("p90_sessions_to_touch"),
            *[(pre.filter(won) <= 1 - x).mean().alias(f"winners_fell_{int(x*100)}") for x in C.FALLS],
            wmask.median().alias("ptt_winners_median"), wmask.quantile(0.1).alias("ptt_winners_p90"), lmask.median().alias("ptt_nonwinners_median"), lmask.quantile(0.1).alias("ptt_nonwinners_p90")))
    for loss in LOSSES:
        parts.append(done.group_by(keys).agg(pl.lit("loss").alias("kind"), pl.lit(loss).alias("threshold"), (pl.col("low") <= 1 + loss).mean().alias("touch_rate"), (pl.col("terminal_mult") <= 1 + loss).mean().alias("terminal_rate")))
    return counts.join(pl.concat(parts, how="diagonal_relaxed"), on=keys, how="left").with_columns((pl.col("eligible") - pl.col("resolved")).alias("unknown")).sort(*keys, "kind", "threshold")


def summaries(table: pl.DataFrame) -> pl.DataFrame:
    keys = ["universe_pass", "variant", "years", "kind", "threshold"]
    out = []
    for key, g in table.filter(pl.col("kind").is_not_null() & (pl.col("resolved") > 0)).sort("cohort").group_by(keys, maintain_order=True):
        rates, cohorts = g["touch_rate"].to_numpy(), g["cohort"].to_list()
        step = int(key[2]) * 4
        chain = rates[::step]
        out.append({**dict(zip(keys, key)), "cohorts": g.height, "resolved": g["resolved"].sum(),
                    "pooled_touch": float((g["touch_rate"] * g["resolved"]).sum() / g["resolved"].sum()), "pooled_terminal": float((g["terminal_rate"] * g["resolved"]).sum() / g["resolved"].sum()),
                    "equal_cohort_touch": float(rates.mean()), "equal_cohort_terminal": float(g["terminal_rate"].mean()), "median_cohort_touch": float(np.median(rates)),
                    "p25_cohort": float(np.quantile(rates, 0.25)), "p75_cohort": float(np.quantile(rates, 0.75)), "worst_cohort": float(rates.min()), "worst_date": str(cohorts[int(rates.argmin())]), "best_cohort": float(rates.max()), "best_date": str(cohorts[int(rates.argmax())]),
                    "non_overlapping_mean": float(chain.mean()), "non_overlapping_n": len(chain)})
    return pl.DataFrame(out).sort(*keys)


def concentration(rows: pl.DataFrame) -> pl.DataFrame:
    out = []
    done = rows.filter(pl.col("resolved"))
    for key, g in done.group_by(["universe_pass", "variant", "years"], maintain_order=True):
        for m in GAINS:
            wins = g.filter(pl.col(f"t_{m}") > 0)
            if wins.is_empty():
                continue
            names = wins.group_by("instrument_id").len().sort("len", descending=True)
            years = wins.group_by(pl.col("cohort").dt.year().alias("year")).len().sort("len", descending=True)
            out.append({"universe_pass": key[0], "variant": key[1], "years": key[2], "threshold": m, "winning_rows": wins.height, "distinct_companies": names.height, "rows_per_company": wins.height / names.height,
                        "top20_company_share": names.head(20)["len"].sum() / wins.height, "top_start_year": years["year"][0], "top_start_year_share": years["len"][0] / wins.height})
    return pl.DataFrame(out).sort("universe_pass", "variant", "years", "threshold")


def breakdowns(rows: pl.DataFrame) -> pl.DataFrame:
    done, out = rows.filter(pl.col("resolved")), []
    keys = ["universe_pass", "variant", "years"]
    for band in BAND_COLS:
        for m in GAINS:
            out.append(done.group_by(*keys, pl.col(band).alias("band")).agg(pl.lit(band).alias("breakdown"), pl.lit("gain").alias("kind"), pl.lit(m).alias("threshold"), pl.len().alias("n"), (pl.col(f"t_{m}") > 0).mean().alias("touch_rate"), (pl.col("terminal_mult") >= m).mean().alias("terminal_rate")))
        for loss in LOSSES:
            out.append(done.group_by(*keys, pl.col(band).alias("band")).agg(pl.lit(band).alias("breakdown"), pl.lit("loss").alias("kind"), pl.lit(loss).alias("threshold"), pl.len().alias("n"), (pl.col("low") <= 1 + loss).mean().alias("touch_rate"), (pl.col("terminal_mult") <= 1 + loss).mean().alias("terminal_rate")))
    return pl.concat(out).sort(*keys, "breakdown", "band", "kind", "threshold")


def halved_per_doubled(rows: pl.DataFrame) -> pl.DataFrame:
    done = rows.filter(pl.col("resolved"))
    return done.group_by("universe_pass", "variant", "years", pl.col("cohort").dt.year().alias("year")).agg(
        pl.len().alias("resolved"), (pl.col("terminal_mult") <= 0.5).sum().alias("halved_at_end"), (pl.col("terminal_mult") >= 2).sum().alias("doubled_at_end"), (pl.col("t_2.0") > 0).sum().alias("touched_2x")).with_columns(
        (pl.col("halved_at_end") / pl.col("doubled_at_end")).alias("halved_per_doubled"), (pl.col("halved_at_end") / pl.col("touched_2x")).alias("halved_per_touch_2x")).sort("universe_pass", "variant", "years", "year")


def step_check(rows: pl.DataFrame) -> pl.DataFrame:
    """How much of each outcome group holds a one-session move beyond 60 %: possibly an unadjusted action, not a real return."""
    done, out = rows.filter(pl.col("resolved")), []
    for key, g in done.group_by(["universe_pass", "variant", "years"], maintain_order=True):
        out.append({"universe_pass": key[0], "variant": key[1], "years": key[2], "group": "all", "rows": g.height, "with_suspect_step": g["suspect_step"].mean()})
        for m in GAINS:
            w = g.filter(pl.col(f"t_{m}") > 0)
            if w.height:
                out.append({"universe_pass": key[0], "variant": key[1], "years": key[2], "group": f"touched {m:g}x", "rows": w.height, "with_suspect_step": w["suspect_step"].mean()})
        for loss in LOSSES:
            w = g.filter(pl.col("terminal_mult") <= 1 + loss)
            if w.height:
                out.append({"universe_pass": key[0], "variant": key[1], "years": key[2], "group": f"ended {loss:+.0%}", "rows": w.height, "with_suspect_step": w["suspect_step"].mean()})
    return pl.DataFrame(out).sort("universe_pass", "variant", "years", "group")


def strategy_section(s: pl.DataFrame, c: pl.DataFrame, h: pl.DataFrame, steps: pl.DataFrame) -> str:
    base = s.filter((pl.col("universe_pass") == "standard") & (pl.col("variant") == "b") & (pl.col("kind") == "gain"))
    lines = ["## 8. Outcome census (PM0.11)", "", NOTE, "", "Standard universe, variant (b). `out/census/` holds every cohort, both variants and the no-floor pass.", "",
             "**Base rates, first touch on closes:**", "", "| Horizon | Threshold | Cohorts | Pooled | Equal-cohort | Median cohort | Worst to best cohort | Held at end, pooled |", "|---|---|---|---|---|---|---|---|"]
    for r in base.sort("years", "threshold").to_dicts():
        lines.append(f"| {r['years']}y | {r['threshold']:g}× | {r['cohorts']} | {r['pooled_touch']:.1%} | {r['equal_cohort_touch']:.1%} | {r['median_cohort_touch']:.1%} | {r['worst_cohort']:.1%} to {r['best_cohort']:.1%} | {r['pooled_terminal']:.1%} |")
    lines += ["", "**Halved per doubled, 12-month horizon, by cohort year:**", "", "| Year | Resolved | Halved at end | Doubled at end | Halved per doubled | Halved per 2× touch |", "|---|---|---|---|---|---|"]
    for r in h.filter((pl.col("universe_pass") == "standard") & (pl.col("variant") == "b") & (pl.col("years") == 1)).to_dicts():
        d = "n/a" if r["halved_per_doubled"] is None or not np.isfinite(r["halved_per_doubled"]) else f"{r['halved_per_doubled']:.2f}"
        lines.append(f"| {r['year']} | {r['resolved']} | {r['halved_at_end']} | {r['doubled_at_end']} | {d} | {r['halved_per_touch_2x']:.2f} |")
    lines += ["", "**Concentration of touch winners:**", "", "| Horizon | Threshold | Winning rows | Distinct companies | Top 20 companies' share | Top start year | Its share |", "|---|---|---|---|---|---|---|"]
    for r in c.filter((pl.col("universe_pass") == "standard") & (pl.col("variant") == "b")).to_dicts():
        lines.append(f"| {r['years']}y | {r['threshold']:g}× | {r['winning_rows']} | {r['distinct_companies']} | {r['top20_company_share']:.1%} | {r['top_start_year']} | {r['top_start_year_share']:.1%} |")
    lines += ["", "**Data caveat:** adjusted closes can still hold an unadjusted corporate action. Share of each group's windows with a one-session move beyond 60 %, 12-month horizon:", "", "| Group | Rows | With such a move |", "|---|---|---|"]
    for r in steps.filter((pl.col("universe_pass") == "standard") & (pl.col("variant") == "b") & (pl.col("years") == 1)).to_dicts():
        lines.append(f"| {r['group']} | {r['rows']} | {r['with_suspect_step']:.1%} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    frame, sessions, _ = load()
    print(f"bars {frame.height}, sessions {len(sessions)}, {sessions[0]} to {sessions[-1]}", flush=True)
    rows = build_rows(frame, sessions)
    del frame
    table = cohort_tables(rows)
    table.write_csv(OUT / "cohorts.csv")
    s = summaries(table)
    s.write_csv(OUT / "summary.csv")
    c = concentration(rows)
    c.write_csv(OUT / "concentration.csv")
    breakdowns(rows).write_csv(OUT / "breakdowns.csv")
    h = halved_per_doubled(rows)
    steps = step_check(rows)
    steps.write_csv(OUT / "suspect_steps.csv")
    print(steps.filter((pl.col("universe_pass") == "standard") & (pl.col("variant") == "b")))
    h.write_csv(OUT / "halved_per_doubled.csv")
    (OUT / "README.txt").write_text(NOTE + "\n")
    (OUT / "strategy_section.md").write_text(strategy_section(s, c, h, steps))
    print(NOTE)
    print(s.filter((pl.col("universe_pass") == "standard") & (pl.col("variant") == "b")).select("years", "kind", "threshold", "cohorts", "resolved", "pooled_touch", "equal_cohort_touch", "median_cohort_touch", "worst_cohort", "best_cohort", "pooled_terminal", "non_overlapping_mean", "non_overlapping_n"))
    print(c.filter((pl.col("universe_pass") == "standard") & (pl.col("variant") == "b")))


if __name__ == "__main__":
    main()
