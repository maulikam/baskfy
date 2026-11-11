"""Train on everything up to (last complete label date - 6 months - purge), test on the last 6 months of
signals with complete 12-month paths. Also a 'partial' view: signals from the last 6 months of the data
(May-Nov 2026), scored on whatever path exists so far (touched 2x yet? return so far)."""
import polars as pl, numpy as np, lightgbm as lgb
from sklearn.metrics import roc_auc_score
import run_pm4 as R
from portfolioml import panel
from explore_multibagger import PARAMS, ROUNDS, TRAIN_STRIDE, grp
DATA = R.DATA; src = DATA / "dataset_mb_u100_h250_tb.parquet"
filings = panel.load_fundamentals(DATA)
univ = pl.scan_parquet(src).filter(pl.col("universe")).select(R.KEEP).collect()
frame = R.assemble(univ, filings, panel.load_shareholding(DATA), panel.load_sector(DATA)); del univ
frame = frame.with_columns([pl.col(c).cast(pl.Float32) for c, t in frame.schema.items() if t == pl.Float64])
frame = R.covered(frame)
lab = frame.filter(pl.col("label").is_not_null()).sort("date")
sessions = lab["date"].unique().sort().to_list()
last = sessions[-1]; test_start = sessions[-125]  # ~6 months of sessions
test = lab.filter(pl.col("date") >= test_start)
tr_end = sessions[sessions.index(test_start) - (250 + 6)]
train = lab.filter(pl.col("date") <= tr_end)
train = train.filter(pl.col("date").is_in(pl.Series(sessions[::TRAIN_STRIDE]).implode())).sort("date")
print(f"train <= {tr_end} ({train.height} rows)   test {test_start} -> {last} ({test.height} rows, {test['date'].n_unique()} sessions)")
x = lambda df, feats: df.select(feats).to_numpy().astype(np.float32)
rows = []
for arm, feats in (("price_only", R.MODELS["controls_technicals"]), ("all_with_fundamentals", R.MODELS["all"]), ("controls_only", R.MODELS["controls"])):
    m = lgb.train(PARAMS, lgb.Dataset(x(train, feats), train["label"].to_numpy(), group=grp(train)), ROUNDS)
    t = test.with_columns(pl.Series("score", m.predict(x(test, feats))))
    inv = t.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).with_columns(pl.col("score").rank("ordinal", descending=True).over("date").alias("rk"))
    top = inv.filter(pl.col("rk") <= 20)
    rows.append({"arm": arm, "auc": roc_auc_score(t["label"], t["score"]), "top20_hit": top["label"].mean(), "base_hit": inv["label"].mean(),
                 "lift": top["label"].mean()/inv["label"].mean(), "top20_mean_12m": top["fwd_ret_h"].mean(), "univ_mean_12m": inv["fwd_ret_h"].mean(),
                 "top20_median_12m": top["fwd_ret_h"].median(), "top20_halved": (top["fwd_ret_h"] <= -0.5).mean(), "distinct_names": top["instrument_id"].n_unique()})
pl.Config.set_tbl_rows(20); pl.Config.set_tbl_width_chars(220); pl.Config.set_float_precision(3)
out = pl.DataFrame(rows); print(out); out.write_csv(R.OUT / "last6m_split.csv")
