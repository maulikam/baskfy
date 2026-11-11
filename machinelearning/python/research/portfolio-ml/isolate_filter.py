"""PM4 follow-up: is the covered-mode gain the candidate pool (filers only) or the training set?
Train price-only (controls+technicals) on the FULL universe as in run_pm4, then restrict picks to
rows with a TTM figure. Gate stats for both pools from the same scores."""
import polars as pl, numpy as np
from pathlib import Path
import run_pm4 as R
from portfolioml import panel, fundamentals as F, shareholding as S, sector as M

DATA = R.DATA; src = DATA / "dataset_mb_u100_h250_tb.parquet"
filings = panel.load_fundamentals(DATA)
univ = pl.scan_parquet(src).filter(pl.col("universe")).select(R.KEEP).collect()
frame = R.assemble(univ, filings, panel.load_shareholding(DATA), panel.load_sector(DATA)); del univ
frame = frame.with_columns([pl.col(c).cast(pl.Float32) for c, t in frame.schema.items() if t == pl.Float64])
scores, aucs, _ = R.walk_forward(frame, R.MODELS["controls_technicals"], 250, arm="price_only_full")
sc = frame.select("instrument_id","date","label","fwd_ret_h","close_raw","turnover20", R.TTM).join(scores, on=["instrument_id","date"])
inv = sc.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7) & pl.col("label").is_not_null() & pl.col("fwd_ret_h").is_not_null())
rows = []
for pool_name, pool in (("full", inv), ("filers_only", inv.filter(pl.col(R.TTM).is_not_null()))):
    p = pool.with_columns(pl.col("score").rank("ordinal", descending=True).over("date").alias("rk"), pl.col("date").dt.year().alias("y"))
    for win, years in R.GATES.items():
        u = p.filter(pl.col("y").is_in(years)); t = u.filter(pl.col("rk") <= 20)
        rows.append({"pool": pool_name, "window": win, "top20_hit": t["label"].mean(), "univ_hit": u["label"].mean(),
                     "hit_x": t["label"].mean()/u["label"].mean(), "top20_mean": t["fwd_ret_h"].mean(), "univ_mean": u["fwd_ret_h"].mean(),
                     "top20_median": t["fwd_ret_h"].median(), "top20_halved": (t["fwd_ret_h"] <= -0.5).mean(), "n_top": t.height})
    for y in (2020,2021,2022,2023):
        u = p.filter(pl.col("y")==y); t = u.filter(pl.col("rk") <= 20)
        rows.append({"pool": pool_name, "window": str(y), "top20_hit": t["label"].mean(), "univ_hit": u["label"].mean(), "hit_x": t["label"].mean()/u["label"].mean(),
                     "top20_mean": t["fwd_ret_h"].mean(), "univ_mean": u["fwd_ret_h"].mean(), "top20_median": t["fwd_ret_h"].median(), "top20_halved": (t["fwd_ret_h"] <= -0.5).mean(), "n_top": t.height})
out = pl.DataFrame(rows).sort("window","pool")
pl.Config.set_tbl_rows(40); pl.Config.set_tbl_width_chars(220); pl.Config.set_float_precision(3)
print(out); out.write_csv(R.OUT / "isolate_filers_filter.csv")
# share of investable rows that are filers, by year
print(inv.with_columns(pl.col("date").dt.year().alias("y")).group_by("y").agg(pl.col(R.TTM).is_not_null().mean().alias("filer_share"), pl.len()).sort("y"))
