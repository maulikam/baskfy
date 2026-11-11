"""Swing setups built for payoff, not hit rate (Maulik, 11 Nov 2026): small losses, large winners.

usage: python explore_rr_swing.py
Fixed barriers at 3.6:1 to 5:1 and trailing exits (initial stop, then a trail under the highest high, held at most H
sessions). Same walk-forward as explore_xgb_swing.py (XGBoost, fixed parameters, every third session, purged by
H + 1). Classifier target: the trade made at least twice its initial risk (fixed: hit the target). Trades: each
session's top 5 / 10 investable names, never one still held, 0.5 % round trip. Baseline: every investable entry.
Writes out/rr_swing/{years,summary}.csv. Research only."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import polars as pl
import xgboost as xgb
from sklearn.metrics import roc_auc_score

from explore_xgb_swing import COST, STRIDE, XGB, trades
from portfolioml import pipeline as P
from portfolioml.features import ALL_FEATURES
from portfolioml.labels import BarrierConfig, TrailConfig, trailing, triple_barrier

OUT = Path("out/rr_swing")
FIRST_TEST, TOP = 2016, (5, 10)
SETUPS = [
    ("fixed -3/+15 h20", BarrierConfig(up=0.15, down=0.03, horizon=20)),
    ("fixed -4/+20 h30", BarrierConfig(up=0.20, down=0.04, horizon=30)),
    ("fixed -5/+20 h30", BarrierConfig(up=0.20, down=0.05, horizon=30)),
    ("fixed -5/+25 h40", BarrierConfig(up=0.25, down=0.05, horizon=40)),
    ("fixed -7/+25 h40", BarrierConfig(up=0.25, down=0.07, horizon=40)),
    ("trail -4 / 8% h30", TrailConfig(stop=0.04, trail=0.08, horizon=30)),
    ("trail -5 / 10% h40", TrailConfig(stop=0.05, trail=0.10, horizon=40)),
    ("trail -7 / 12% h60", TrailConfig(stop=0.07, trail=0.12, horizon=60)),
]
pl.Config.set_tbl_rows(100); pl.Config.set_tbl_width_chars(230); pl.Config.set_float_precision(4); pl.Config.set_tbl_cols(20)


def labels(bars: pl.DataFrame, cfg) -> pl.DataFrame:
    parts = []
    for ch in P.chunks(bars, 12):
        if isinstance(cfg, BarrierConfig):
            lab = triple_barrier(ch, cfg).select("instrument_id", "date", "label", "exit_k", "exit_ret")
        else:
            lab = trailing(ch, cfg).select("instrument_id", "date", "exit_k", "exit_ret").with_columns(
                pl.when(pl.col("exit_ret").is_not_null()).then((pl.col("exit_ret") >= 2 * cfg.stop).cast(pl.Int8)).alias("label"))
        parts.append(lab.with_columns(pl.col("exit_ret").cast(pl.Float32)))
    return pl.concat(parts)


def stats(t: pl.DataFrame) -> dict:
    r = t["exit_ret"] - COST
    wins, losses = r.filter(r > 0), r.filter(r <= 0)
    return {"trades": t.height, "win_rate": float((r > 0).mean()), "avg_win": float(wins.mean() or 0), "avg_loss": float(losses.mean() or 0),
            "payoff": float((wins.mean() or 0) / -(losses.mean() or -1e-9)), "expectancy": float(r.mean()),
            "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else None, "worst": float(r.min()), "avg_hold": float(t["exit_k"].mean())}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    feat = pl.scan_parquet("data/features.parquet")
    bars = feat.select("instrument_id", "date", "open", "high", "low", "close").collect()
    univ = feat.filter(pl.col("universe")).select("instrument_id", "date", "close_raw", "turnover20", *ALL_FEATURES).collect().with_columns(
        ((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).alias("investable"))
    years, summary = [], []
    for name, cfg in SETUPS:
        t0 = time.time()
        df = univ.join(labels(bars, cfg), on=["instrument_id", "date"]).filter(pl.col("label").is_not_null())
        sessions = df["date"].unique().sort()
        pooled = {n: [] for n in TOP}
        base = []
        for year in range(FIRST_TEST, sessions.max().year + 1):
            test = df.filter(pl.col("date").dt.year() == year)
            before = sessions.filter(sessions.dt.year() < year)
            if test.is_empty() or len(before) < 500:
                continue
            cut = before[-(cfg.horizon + 2)]
            train = df.filter(pl.col("date").is_in(before.filter(before <= cut)[::STRIDE].implode()))
            x, xt = (np.nan_to_num(f.select(ALL_FEATURES).to_numpy().astype(np.float32), nan=np.nan, posinf=np.nan, neginf=np.nan) for f in (train, test))
            p = xgb.XGBClassifier(**XGB).fit(x, train["label"].to_numpy()).predict_proba(xt)[:, 1]
            scored = test.select("date", "instrument_id", "label", "exit_ret", "exit_k", "investable").with_columns(pl.Series("p", p))
            inv = test.filter(pl.col("investable"))
            base.append(inv.select("exit_ret", "exit_k"))
            row = {"setup": name, "year": year, "auc": roc_auc_score(test["label"], p), "base_rate": float(test["label"].mean()),
                   "all_entries_net": float(inv["exit_ret"].mean()) - COST}
            for n in TOP:
                t = trades(scored, cfg.horizon, n)
                pooled[n].append(t)
                row[f"top{n}_net"] = float(t["exit_ret"].mean()) - COST
                row[f"top{n}_win"] = float((t["exit_ret"] - COST > 0).mean())
            years.append(row)
        b = pl.concat(base)
        summary.append({"setup": name, "picks": "every investable entry (no model)", **stats(b), "years_positive": None})
        for n in TOP:
            yrs = [r[f"top{n}_net"] for r in years if r["setup"] == name]
            summary.append({"setup": name, "picks": f"model top {n}", **stats(pl.concat(pooled[n])), "years_positive": f"{sum(y > 0 for y in yrs)}/{len(yrs)}"})
        print(f"{name}: done in {time.time() - t0:.0f}s; top-10 expectancy {summary[-1]['expectancy']:+.4f}, PF {summary[-1]['profit_factor']}", flush=True)
    y, s = pl.DataFrame(years), pl.DataFrame(summary)
    y.write_csv(OUT / "years.csv"); s.write_csv(OUT / "summary.csv")
    print(s); print(y.select("setup", "year", "auc", "all_entries_net", "top5_net", "top10_net"))


if __name__ == "__main__":
    main()
