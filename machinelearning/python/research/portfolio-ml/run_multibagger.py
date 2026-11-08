"""Step 3 (multibagger): out/mb_scores_*  → tranche portfolios vs baselines and benchmarks.

usage: python run_multibagger.py mb_u100_h250 touch"""

from __future__ import annotations

import datetime as dt
import glob
import json
import sys
from pathlib import Path

import polars as pl

from portfolioml import panel, tranches

HERE = Path(__file__).parent
DATA, OUT = HERE / "data", HERE / "out"


def main(geometry: str, kind: str) -> None:
    out = OUT / f"{geometry}_{kind}"
    out.mkdir(parents=True, exist_ok=True)
    horizon = int(geometry.split("_h")[1])
    ds = pl.read_parquet(DATA / f"dataset_{geometry}.parquet").filter(pl.col("universe"))
    scores = pl.concat([pl.read_parquet(p) for p in sorted(glob.glob(str(OUT / f"mb_scores_{geometry}_{kind}_*.parquet")))])
    allscores = scores.join(ds.select("instrument_id", "date", pl.col("r252_cs").alias("bl_r252"), pl.col("mom_12_1_cs").alias("bl_mom12_1"),
                                      pl.lit(0.0).alias("bl_universe")), on=["instrument_id", "date"])
    bench = panel.load_index_series(DATA).select("date", "nifty50", "midcap150", "smallcap250")
    investable = ds.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7))
    sims = {
        "ml_top20": (ds, "score", tranches.TrancheConfig(horizon=horizon)),
        "ml_top20_stop35": (ds, "score", tranches.TrancheConfig(horizon=horizon, stop=-0.35)),
        "ml_top20_gate_sc50": (ds, "score", tranches.TrancheConfig(horizon=horizon, gate_col="smallcap250_vs_sma50", gate_min=0.0)),
        "ml_top20_investable": (investable, "score", tranches.TrancheConfig(horizon=horizon)),
        "ml_top20_investable_stop35": (investable, "score", tranches.TrancheConfig(horizon=horizon, stop=-0.35)),
        "ml_top20_investable_gate_sc50_stop35": (investable, "score", tranches.TrancheConfig(horizon=horizon, gate_col="smallcap250_vs_sma50", gate_min=0.0, stop=-0.35)),
        "ml_top50": (ds, "score", tranches.TrancheConfig(top_n=50, horizon=horizon)),
        "bl_r252_top20": (ds, "bl_r252", tranches.TrancheConfig(horizon=horizon)),
        "bl_r252_top20_investable": (investable, "bl_r252", tranches.TrancheConfig(horizon=horizon)),
        "bl_mom12_1_top20": (ds, "bl_mom12_1", tranches.TrancheConfig(horizon=horizon)),
        "universe_ew": (ds, "bl_universe", tranches.TrancheConfig(top_n=10_000, horizon=horizon)),
        "investable_ew": (investable, "bl_universe", tranches.TrancheConfig(top_n=10_000, horizon=horizon)),
    }
    rows = []
    for name, (frame, col, cfg) in sims.items():
        picks, eq = tranches.simulate(frame, allscores, col, cfg)
        if eq.is_empty():
            print(name, "no picks"); continue
        s = {"sim": name, **tranches.summary(eq, picks, bench)}
        loaded = tranches.summary(eq, picks, bench, start=dt.date(2021, 1, 1))
        s.update({f"{k}_from2021": v for k, v in loaded.items() if k in ("cagr", "max_dd", "sharpe", "nifty50_cagr", "midcap150_cagr", "smallcap250_cagr", "smallcap250_max_dd")})
        rows.append(s)
        tranches.yearly(eq, bench).write_csv(out / f"yearly_{name}.csv")
        picks.write_csv(out / f"picks_{name}.csv")
        eq.write_csv(out / f"equity_{name}.csv")
        print(name, json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in s.items()}), flush=True)
    summ = pl.DataFrame(rows)
    summ.write_csv(out / "tranche_summary.csv")
    # per-signal-year outcome of the picks, ml vs baselines
    yr = []
    for name in ("ml_top20", "ml_top20_investable", "bl_r252_top20", "universe_ew", "investable_ew"):
        p = pl.read_csv(out / f"picks_{name}.csv", try_parse_dates=True)
        yr.append(p.group_by(pl.col("date").dt.year().alias("signal_year")).agg(
            pl.lit(name).alias("sim"), pl.len().alias("picks"), pl.col("label").mean().alias("pct_touched_2x"),
            (pl.col("fwd_ret_h") >= 1).mean().alias("pct_2x_at_12m"), (pl.col("fwd_ret_h") > 0).mean().alias("pct_pos"),
            pl.col("fwd_ret_h").mean().alias("mean_12m"), pl.col("fwd_ret_h").median().alias("median_12m"),
            pl.col("fwd_min_ret").mean().alias("mean_worst_point")))
    pl.concat(yr).sort("sim", "signal_year").write_csv(out / "picks_by_year.csv")
    pl.Config.set_tbl_rows(80); pl.Config.set_tbl_width_chars(220); pl.Config.set_float_precision(3)
    print(pl.read_csv(out / "picks_by_year.csv"))
    print(summ.select("sim", "cagr", "max_dd", "sharpe", "cagr_from2021", "max_dd_from2021", "sharpe_from2021", "picks", "pct_doubled_touch", "pct_2x_at_12m", "mean_12m", "median_12m"))
    print(summ.select("nifty50_cagr_from2021", "midcap150_cagr_from2021", "smallcap250_cagr_from2021", "smallcap250_max_dd_from2021").head(1))
    print(pl.read_csv(out / "yearly_ml_top20.csv")); print(pl.read_csv(out / "yearly_bl_mom12_1_top20.csv")); print(pl.read_csv(out / "yearly_universe_ew.csv"))
    print(summ.select("sim", "nifty50_cagr", "midcap150_cagr", "smallcap250_cagr", "smallcap250_max_dd").head(1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "touch")
