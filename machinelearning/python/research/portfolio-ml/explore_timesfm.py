"""PM9a: does Google's TimesFM-3 (zero-shot, non-commercial weights) add anything to the ranker?

Quarterly signal dates 2014->2025, every universe name with >= 256 bars. Context = last 512 adjusted
closes up to the signal date; 250-session forecast with 9 quantiles. Forecast-derived features are
appended to the price-only set and compared on identical rows with the same walk-forward.
Forecasts are checkpointed per date so the run is resumable.

usage: PYTHONPATH=<timesfm repo>/src python explore_timesfm.py [forecast|evaluate|all]"""
from __future__ import annotations

import sys, time
from pathlib import Path

import numpy as np
import polars as pl

HERE = Path(__file__).parent
DATA, OUT = HERE / "data", HERE / "out" / "timesfm"
SRC = DATA / "dataset_mb_u100_h250_tb.parquet"
CTX, H, MIN_CTX, BATCH = 512, 250, 256, 64
FC_COLS = ["tfm_ret50_250", "tfm_ret10_250", "tfm_ret90_250", "tfm_ret50_63", "tfm_ret90_63",
           "tfm_pathmax50", "tfm_pathmax90", "tfm_pathmin10", "tfm_width_250"]


def quarter_starts(dates: list) -> list:
    out, seen = [], set()
    for d in dates:
        k = (d.year, (d.month - 1) // 3)
        if k not in seen and d.year >= 2014:
            seen.add(k); out.append(d)
    return out


def forecast() -> None:
    import torch
    torch.set_num_threads(2)
    from timesfm3 import TimesFM3Evaluator, ModelConfig
    OUT.mkdir(parents=True, exist_ok=True)
    lf = pl.scan_parquet(SRC)
    closes = lf.select("instrument_id", "date", pl.col("close").cast(pl.Float32)).collect().sort("instrument_id", "date")
    univ = lf.filter(pl.col("universe")).select("instrument_id", "date").collect()
    sessions = sorted(closes["date"].unique().to_list())
    sidx = {d: i for i, d in enumerate(sessions)}
    qdates = [d for d in quarter_starts(sessions) if d in set(univ["date"].to_list())]
    # per-instrument arrays
    series = {k[0]: (g["date"].to_list(), g["close"].to_numpy()) for k, g in closes.group_by("instrument_id", maintain_order=True)}
    del closes
    model = TimesFM3Evaluator(ModelConfig(checkpoint_path="google/timesfm-3.0-pytorch", per_core_batch_size=BATCH, device="cpu"))
    t0 = time.time(); done = 0
    for qd in qdates:
        fp = OUT / f"fc_{qd}.parquet"
        if fp.exists():
            continue
        names = univ.filter(pl.col("date") == qd)["instrument_id"].to_list()
        ctxs, ids = [], []
        for n in names:
            ds, cs = series[n]
            # position of qd in this name's own dates (last bar <= qd)
            j = np.searchsorted(np.array([sidx[d] for d in ds]), sidx[qd], side="right")
            if j < MIN_CTX:
                continue
            c = cs[max(0, j - CTX):j]
            if not np.all(np.isfinite(c)) or c[-1] <= 0:
                continue
            ctxs.append(c.astype(np.float32)); ids.append(n)
        rows = []
        for b in range(0, len(ctxs), BATCH):
            outs = list(model.predict_batch(ctxs[b:b + BATCH], horizon=H, return_quantiles=True, use_symmetric_averaging=False))
            for n, c, o in zip(ids[b:b + BATCH], ctxs[b:b + BATCH], outs):
                last = float(c[-1]); q = o.quantiles  # (H, 9): 0.1..0.9, median at 4
                p50, p10, p90 = q[:, 4], q[:, 0], q[:, 8]
                rows.append({"instrument_id": n, "date": qd,
                             "tfm_ret50_250": p50[-1] / last - 1, "tfm_ret10_250": p10[-1] / last - 1, "tfm_ret90_250": p90[-1] / last - 1,
                             "tfm_ret50_63": p50[62] / last - 1, "tfm_ret90_63": p90[62] / last - 1,
                             "tfm_pathmax50": p50.max() / last - 1, "tfm_pathmax90": p90.max() / last - 1, "tfm_pathmin10": p10.min() / last - 1,
                             "tfm_width_250": (p90[-1] - p10[-1]) / max(abs(p50[-1]), 1e-6)})
        pl.DataFrame(rows).write_parquet(fp)
        done += len(rows)
        print(f"{qd}: {len(rows)} forecasts  total {done}  {time.time()-t0:.0f}s", flush=True)


def evaluate() -> None:
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    sys.path.insert(0, str(HERE))
    from explore_multibagger import PARAMS, ROUNDS, grp
    from portfolioml.features import ALL_FEATURES
    fc = pl.concat([pl.read_parquet(p) for p in sorted(OUT.glob("fc_*.parquet"))])
    keep = ["instrument_id", "date", "label", "fwd_ret_h", "close_raw", "turnover20", *ALL_FEATURES]
    ds = pl.scan_parquet(SRC).filter(pl.col("universe") & pl.col("date").is_in(fc["date"].unique().implode())).select(keep).collect()
    d = ds.join(fc, on=["instrument_id", "date"]).filter(pl.col("label").is_not_null()).sort("date")
    d = d.with_columns([pl.col(c).cast(pl.Float32) for c in FC_COLS])
    print("rows", d.height, "dates", d["date"].n_unique(), "positives", int(d["label"].sum()))
    arms = {"price_only": ALL_FEATURES, "price_plus_timesfm": [*ALL_FEATURES, *FC_COLS], "timesfm_only": FC_COLS}
    sessions = sorted(d["date"].unique().to_list())
    x = lambda df, f: df.select(f).to_numpy().astype(np.float32)  # noqa: E731
    scored = {a: [] for a in arms}
    for y in range(2020, 2026):
        test = d.filter(pl.col("date").dt.year() == y)
        train = d.filter(pl.col("date") < pl.date(y - 1, 1, 1) + pl.duration(days=0))  # quarterly dates: labels of year y-1 Q1 end in y-1 -> keep < y-1 to purge a full year
        if test.is_empty() or train["label"].sum() < 300:
            continue
        for a, f in arms.items():
            m = lgb.train(PARAMS, lgb.Dataset(x(train, f), train["label"].to_numpy(), group=grp(train)), ROUNDS)
            scored[a].append(test.select("instrument_id", "date", "label", "fwd_ret_h", "close_raw", "turnover20").with_columns(pl.Series("score", m.predict(x(test, f)))))
        print(f"fold {y}: train {train.height} test {test.height}", flush=True)
    # raw rankers, no learning
    raw = d.filter(pl.col("date").dt.year() >= 2020).select("instrument_id", "date", "label", "fwd_ret_h", "close_raw", "turnover20", "tfm_pathmax90", "tfm_ret50_250")
    scored["raw_timesfm_p90path"] = [raw.with_columns(pl.col("tfm_pathmax90").alias("score"))]
    scored["raw_timesfm_median"] = [raw.with_columns(pl.col("tfm_ret50_250").alias("score"))]
    rows = []
    for a, parts in scored.items():
        s = pl.concat([p.select("instrument_id", "date", "label", "fwd_ret_h", "close_raw", "turnover20", "score") for p in parts])
        inv = s.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).with_columns(
            pl.col("score").rank("ordinal", descending=True).over("date").alias("rk"), pl.col("date").dt.year().alias("y"))
        for period, part in [("pooled", inv), *[(str(k[0]), g) for k, g in inv.group_by("y", maintain_order=True)]]:
            full = s.filter(pl.col("date").dt.year() == int(period)) if period != "pooled" else s
            top = part.filter(pl.col("rk") <= 20)
            rows.append({"arm": a, "period": period, "auc": roc_auc_score(full["label"], full["score"]), "top20_hit": top["label"].mean(),
                         "base_hit": part["label"].mean(), "lift": top["label"].mean() / part["label"].mean(), "top20_mean_12m": top["fwd_ret_h"].mean(),
                         "univ_mean_12m": part["fwd_ret_h"].mean(), "top20_halved": (top["fwd_ret_h"] <= -0.5).mean(), "n_top": top.height})
    res = pl.DataFrame(rows).sort("period", "arm")
    pl.Config.set_tbl_rows(80); pl.Config.set_tbl_width_chars(230); pl.Config.set_float_precision(3)
    print(res); res.write_csv(OUT / "timesfm_vs_price.csv")
    # rule: beat price-only top-20 lift in 2024 and 2025 without a higher halved rate
    for a in ("price_plus_timesfm", "timesfm_only", "raw_timesfm_p90path"):
        ok = all(
            (res.filter((pl.col("arm") == a) & (pl.col("period") == str(y)))["lift"][0] > res.filter((pl.col("arm") == "price_only") & (pl.col("period") == str(y)))["lift"][0])
            and (res.filter((pl.col("arm") == a) & (pl.col("period") == str(y)))["top20_halved"][0] <= res.filter((pl.col("arm") == "price_only") & (pl.col("period") == str(y)))["top20_halved"][0])
            for y in (2024, 2025))
        print(f"RULE {a}: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("forecast", "all"):
        forecast()
    if mode in ("evaluate", "all"):
        evaluate()
