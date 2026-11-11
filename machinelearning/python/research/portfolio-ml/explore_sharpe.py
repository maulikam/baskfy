"""Average Sharpe-ratio features: avg over 1-3-6-9-12 months, over 1-3, and over 3-6-9-12.
Sharpe(n) = mean daily return / std daily return * sqrt(252) over n sessions (21/63/126/189/252).
Raw values plus their within-day percentile ranks. Same walk-forward and rows as explore_technicals."""
import polars as pl, numpy as np, lightgbm as lgb
from sklearn.metrics import roc_auc_score
from explore_multibagger import PARAMS, ROUNDS, TRAIN_STRIDE, grp
from portfolioml.features import ALL_FEATURES
SRC = "data/dataset_mb_u100_h250_tb.parquet"; G = pl.col("instrument_id")
b = pl.scan_parquet(SRC).select("instrument_id", "date", "close").collect().sort("instrument_id", "date")
r = (pl.col("close") / pl.col("close").shift(1).over(G) - 1)
b = b.with_columns(r.alias("r"))
W = {1: 21, 3: 63, 6: 126, 9: 189, 12: 252}
b = b.with_columns([(pl.col("r").rolling_mean(n).over(G) / (pl.col("r").rolling_std(n).over(G) + 1e-12) * 252 ** 0.5).alias(f"sh{m}") for m, n in W.items()])
b = b.with_columns(
    pl.mean_horizontal("sh1", "sh3", "sh6", "sh9", "sh12").alias("sharpe_avg_all"),
    pl.mean_horizontal("sh1", "sh3").alias("sharpe_avg_1_3"),
    pl.mean_horizontal("sh3", "sh6", "sh9", "sh12").alias("sharpe_avg_3_12"))
RAW = ["sharpe_avg_all", "sharpe_avg_1_3", "sharpe_avg_3_12"]
b = b.select("instrument_id", "date", *[pl.col(k).cast(pl.Float32) for k in RAW])
u = pl.scan_parquet(SRC).filter(pl.col("universe")).select("instrument_id", "date", "label", "fwd_ret_h", "close_raw", "turnover20", *ALL_FEATURES).collect()
u = u.join(b, on=["instrument_id", "date"], how="left"); del b
u = u.with_columns([(pl.col(k).rank("average").over("date") / pl.col(k).count().over("date")).cast(pl.Float32).alias(f"{k}_cs") for k in RAW])
NEW = RAW + [f"{k}_cs" for k in RAW]
u = u.filter(pl.col("label").is_not_null()).sort("date")
sessions = sorted(pl.scan_parquet(SRC).select("date").unique().collect()["date"].to_list())
x = lambda df, f: df.select(f).to_numpy().astype(np.float32)
arms = {"price_only": ALL_FEATURES, "plus_sharpe": [*ALL_FEATURES, *NEW]}
rows, imps = [], []
for y in range(2014, 2026):
    before = [s for s in sessions if s.year < y]
    if len(before) < 600: continue
    tr_ = u.filter((pl.col("date") <= before[-256]) & pl.col("date").is_in(pl.Series(before[::TRAIN_STRIDE]).implode()))
    te = u.filter(pl.col("date").dt.year() == y)
    if te.is_empty() or tr_["label"].sum() < 50: continue
    for a, f in arms.items():
        m = lgb.train(PARAMS, lgb.Dataset(x(tr_, f), tr_["label"].to_numpy(), group=grp(tr_)), ROUNDS)
        t = te.select("date", "label", "fwd_ret_h", "close_raw", "turnover20").with_columns(pl.Series("s", m.predict(x(te, f))))
        inv = t.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).with_columns(pl.col("s").rank("ordinal", descending=True).over("date").alias("rk"))
        top = inv.filter(pl.col("rk") <= 20)
        rows.append({"year": y, "arm": a, "auc": roc_auc_score(t["label"], t["s"]), "top20_hit": top["label"].mean(), "lift": top["label"].mean() / inv["label"].mean(),
                     "top20_mean_12m": top["fwd_ret_h"].mean(), "halved": (top["fwd_ret_h"] <= -0.5).mean()})
        if a == "plus_sharpe": imps.append(pl.DataFrame({"feature": f, "gain": m.feature_importance("gain")}))
    # the three as stand-alone rankers, no model
    for k in RAW:
        t = te.filter(pl.col(k).is_not_null())
        inv = t.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).with_columns(pl.col(k).rank("ordinal", descending=True).over("date").alias("rk"))
        top = inv.filter(pl.col("rk") <= 20)
        rows.append({"year": y, "arm": f"raw_{k}", "auc": roc_auc_score(t["label"], t[k]), "top20_hit": top["label"].mean(), "lift": top["label"].mean() / inv["label"].mean(),
                     "top20_mean_12m": top["fwd_ret_h"].mean(), "halved": (top["fwd_ret_h"] <= -0.5).mean()})
    print(y, "done", flush=True)
r = pl.DataFrame(rows); r.write_csv("out/sharpe_vs_price.csv")
pl.Config.set_tbl_rows(40); pl.Config.set_float_precision(3); pl.Config.set_tbl_width_chars(200)
print(r.filter(pl.col("arm").is_in(["price_only", "plus_sharpe"])).pivot(on="arm", index="year", values=["auc", "lift", "halved"]))
print(r.group_by("arm").agg(pl.col("auc").mean(), pl.col("top20_hit").mean(), pl.col("lift").mean(), pl.col("top20_mean_12m").mean(), pl.col("halved").mean()).sort("arm"))
imp = pl.concat(imps).group_by("feature").agg(pl.col("gain").mean()).with_columns((pl.col("gain") / pl.col("gain").sum() * 100).alias("pct")).sort("pct", descending=True).with_row_index("rank", 1)
print(imp.filter(pl.col("feature").is_in(NEW)).select("rank", "feature", "pct"))
