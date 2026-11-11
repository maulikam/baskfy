"""Single-split check, two ways, fundamentals vs price-only:
  CLEAN:     train on signals <= 2023-12-31 minus purge, test on 2024 -> last complete label.
  IN-SAMPLE: train on everything with a label (2019 -> last complete), test on 2024 -> same rows (overlap).
"""
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
frame = R.covered(frame)  # same judged rows as PM4
lab = frame.filter(pl.col("label").is_not_null()).sort("date")
sessions = lab["date"].unique().sort().to_list()
test = lab.filter(pl.col("date") >= pl.date(2024, 1, 1))
print("test rows", test.height, "signal dates", test["date"].min(), "->", test["date"].max())
x = lambda df, feats: df.select(feats).to_numpy().astype(np.float32)

def fit_eval(train, feats, name):
    train = train.filter(pl.col("date").is_in(pl.Series(sessions[::TRAIN_STRIDE]).implode())).sort("date")
    m = lgb.train(PARAMS, lgb.Dataset(x(train, feats), train["label"].to_numpy(), group=grp(train)), ROUNDS)
    t = test.with_columns(pl.Series("score", m.predict(x(test, feats))))
    inv = t.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).with_columns(
        pl.col("score").rank("ordinal", descending=True).over("date").alias("rk"))
    top = inv.filter(pl.col("rk") <= 20)
    return {"split": name, "train_rows": train.height, "auc": roc_auc_score(t["label"], t["score"]),
            "top20_hit": top["label"].mean(), "base_hit": inv["label"].mean(), "lift": top["label"].mean()/inv["label"].mean(),
            "top20_mean_12m": top["fwd_ret_h"].mean(), "univ_mean_12m": inv["fwd_ret_h"].mean(), "top20_halved": (top["fwd_ret_h"] <= -0.5).mean()}

before = [d for d in sessions if d.year <= 2023]
clean_train = lab.filter(pl.col("date") <= before[-(250 + 6)])
rows = []
for arm, feats in (("price_only", R.MODELS["controls_technicals"]), ("all_with_fundamentals", R.MODELS["all"])):
    rows.append({"arm": arm, **fit_eval(clean_train, feats, "CLEAN train<=2023 test 2024->")})
    rows.append({"arm": arm, **fit_eval(lab, feats, "IN-SAMPLE train all test 2024-> (overlap)")})
pl.Config.set_tbl_rows(20); pl.Config.set_tbl_width_chars(220); pl.Config.set_float_precision(3)
out = pl.DataFrame(rows).sort("split", "arm"); print(out); out.write_csv(R.OUT / "single_split_2024on.csv")
