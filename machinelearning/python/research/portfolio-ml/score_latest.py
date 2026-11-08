"""Train on every completed 12-month label and rank the last session in the export.
This is the nightly job in miniature: out/latest_ranking.csv is what the page would show."""

from __future__ import annotations

import sys

import lightgbm as lgb
import numpy as np
import polars as pl

from explore_multibagger import PARAMS, ROUNDS, TRAIN_STRIDE, grp
from portfolioml.features import ALL_FEATURES

geometry = sys.argv[1] if len(sys.argv) > 1 else "mb_u100_h250"
lf = pl.scan_parquet(f"data/dataset_{geometry}.parquet")
univ = lf.filter(pl.col("universe")).select(
    "instrument_id", "symbol", "date", "label", "close_raw", "turnover20", *ALL_FEATURES).collect()
last = univ["date"].max()
sessions = univ["date"].unique().sort().to_list()
lab = univ.filter(pl.col("label").is_not_null())
train = lab.filter(pl.col("date").is_in(pl.Series(sessions[::TRAIN_STRIDE]).implode())).sort("date")
x = lambda df: df.select(ALL_FEATURES).to_numpy().astype(np.float32)  # noqa: E731
m = lgb.train(PARAMS, lgb.Dataset(x(train), train["label"].to_numpy(), group=grp(train)), ROUNDS)
m.save_model(f"out/model_{geometry}_latest.txt")
today = univ.filter(pl.col("date") == last)
today = today.with_columns(pl.Series("score", m.predict(x(today)))).sort("score", descending=True)
today = today.with_columns(
    pl.int_range(1, pl.len() + 1).alias("rank"),
    ((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).alias("investable"),
    (pl.col("turnover20") / 1e7).round(2).alias("turnover20_cr"),
)
cols = ["rank", "symbol", "score", "investable", "close_raw", "turnover20_cr", "r252", "dist_ath", "vol60", "n_hist"]
today.select(cols).write_csv(f"out/latest_ranking_{geometry}.csv")
print(f"trained on {train.shape[0]:,} rows through {lab['date'].max()}; ranked {today.shape[0]} names as of {last}")
pl.Config.set_tbl_rows(60); pl.Config.set_float_precision(3); pl.Config.set_tbl_width_chars(160)
print(today.filter(pl.col("investable")).select(cols).head(30))
