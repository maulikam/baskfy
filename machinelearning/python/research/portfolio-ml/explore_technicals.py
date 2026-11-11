"""Do classic indicators add to the price-only ranker? CMO, CCI, Choppiness, Kaufman efficiency ratio.
Same walk-forward as STRATEGY §3 (2014-2025, stride 5, purge 256), identical rows, base vs base+indicators."""
import polars as pl, numpy as np, lightgbm as lgb, math
from sklearn.metrics import roc_auc_score
from explore_multibagger import PARAMS, ROUNDS, TRAIN_STRIDE, grp
from portfolioml.features import ALL_FEATURES
SRC = "data/dataset_mb_u100_h250_tb.parquet"; G = pl.col("instrument_id")
b = pl.scan_parquet(SRC).select("instrument_id", "date", "high", "low", "close").collect().sort("instrument_id", "date")
c, h, l = pl.col("close"), pl.col("high"), pl.col("low")
pc = c.shift(1).over(G); d = c - pc
tr = pl.max_horizontal(h - l, (h - pc).abs(), (l - pc).abs())
tp = (h + l + c) / 3
new = []
for n in (14, 20, 63):
    up, dn = d.clip(lower_bound=0).rolling_sum(n).over(G), (-d).clip(lower_bound=0).rolling_sum(n).over(G)
    new.append(((up - dn) / (up + dn + 1e-12) * 100).alias(f"cmo{n}"))
    new.append((100 * (tr.rolling_sum(n).over(G) / (h.rolling_max(n).over(G) - l.rolling_min(n).over(G) + 1e-12)).log10() / math.log10(n)).alias(f"chop{n}"))
    new.append(((c - c.shift(n).over(G)).abs() / (d.abs().rolling_sum(n).over(G) + 1e-12)).alias(f"er{n}"))
    # CCI with rolling std in place of mean absolute deviation (same shape, cheaper)
    new.append(((tp - tp.rolling_mean(n).over(G)) / (0.015 * tp.rolling_std(n).over(G) + 1e-12)).alias(f"cci{n}"))
b = b.with_columns(new)
NEW = [e.meta.output_name() for e in new]
b = b.select("instrument_id", "date", *[pl.col(k).cast(pl.Float32) for k in NEW])
u = pl.scan_parquet(SRC).filter(pl.col("universe") & pl.col("label").is_not_null()).select("instrument_id", "date", "label", "fwd_ret_h", "close_raw", "turnover20", *ALL_FEATURES).collect()
u = u.join(b, on=["instrument_id", "date"], how="left").sort("date"); del b
sessions = sorted(pl.scan_parquet(SRC).select("date").unique().collect()["date"].to_list())
x = lambda df, f: df.select(f).to_numpy().astype(np.float32)
arms = {"price_only": ALL_FEATURES, "plus_indicators": [*ALL_FEATURES, *NEW]}
rows, imps = [], []
for y in range(2014, 2026):
    before = [s for s in sessions if s.year < y]
    if len(before) < 600: continue
    end = before[-256]
    tr_ = u.filter((pl.col("date") <= end) & pl.col("date").is_in(pl.Series(before[::TRAIN_STRIDE]).implode()))
    te = u.filter(pl.col("date").dt.year() == y)
    if te.is_empty() or tr_["label"].sum() < 50: continue
    for a, f in arms.items():
        m = lgb.train(PARAMS, lgb.Dataset(x(tr_, f), tr_["label"].to_numpy(), group=grp(tr_)), ROUNDS)
        t = te.select("date", "label", "fwd_ret_h", "close_raw", "turnover20").with_columns(pl.Series("s", m.predict(x(te, f))))
        inv = t.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).with_columns(pl.col("s").rank("ordinal", descending=True).over("date").alias("rk"))
        top = inv.filter(pl.col("rk") <= 20)
        rows.append({"year": y, "arm": a, "auc": roc_auc_score(t["label"], t["s"]), "top20_hit": top["label"].mean(), "base": inv["label"].mean(),
                     "lift": top["label"].mean() / inv["label"].mean(), "top20_mean_12m": top["fwd_ret_h"].mean(), "halved": (top["fwd_ret_h"] <= -0.5).mean()})
        if a == "plus_indicators": imps.append(pl.DataFrame({"feature": f, "gain": m.feature_importance("gain")}))
    print(y, "done", flush=True)
r = pl.DataFrame(rows); r.write_csv("out/technicals_vs_price.csv")
pl.Config.set_tbl_rows(40); pl.Config.set_float_precision(3); pl.Config.set_tbl_width_chars(200)
w = r.pivot(on="arm", index="year", values=["auc", "lift", "top20_mean_12m", "halved"]); print(w)
print(r.group_by("arm").agg(pl.col("auc").mean(), pl.col("top20_hit").mean(), pl.col("lift").mean(), pl.col("top20_mean_12m").mean(), pl.col("halved").mean()))
imp = pl.concat(imps).group_by("feature").agg(pl.col("gain").mean()).with_columns((pl.col("gain") / pl.col("gain").sum() * 100).alias("pct")).sort("pct", descending=True).with_row_index("rank", 1)
print(imp.filter(pl.col("feature").is_in(NEW)).select("rank", "feature", "pct"))
