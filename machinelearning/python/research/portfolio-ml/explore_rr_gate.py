"""PM10c: PM10b's eight swing setups with a market switch fixed before the run (Maulik, 11 Nov 2026).

Switch: a new trade may start only when, at the signal session's close, NIFTY Smallcap 250 closes above its 100-session
average; entry stays the next open, open trades run to their own exit. Two versions: plain, and one that flips only after
5 closes in a row on the new side (overlay.debounce, PM0.13). Rule, fixed in advance: a setup passes when the model's
top 5 a day, switched, nets at least +0.5 % a trade after 0.5 % costs, has a profit factor of at least 1.2, and is
positive in at least 8 of the 11 test years (a year with no trade is not positive). Every row is reported.

usage: python explore_rr_gate.py      → out/rr_gate/{summary,years}.csv. Research only."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import polars as pl
import xgboost as xgb

from explore_rr_swing import FIRST_TEST, SETUPS, labels, stats
from explore_xgb_swing import COST, STRIDE, XGB, trades
from portfolioml import overlay as O
from portfolioml import panel
from portfolioml.features import ALL_FEATURES

OUT = Path("out/rr_gate")
TOP = (5, 10)
MIN_NET, MIN_PF, MIN_YEARS = 0.005, 1.2, 8
pl.Config.set_tbl_rows(120); pl.Config.set_tbl_width_chars(240); pl.Config.set_float_precision(4); pl.Config.set_tbl_cols(20)


def switches() -> pl.DataFrame:
    idx = panel.load_index_series(Path("data")).select("date", "smallcap250").drop_nulls().sort("date")
    sma = idx["smallcap250"].rolling_mean(100, min_samples=100)
    raw = (idx["smallcap250"] > sma).fill_null(True).to_numpy()
    return idx.select("date").with_columns(pl.Series("plain", raw), pl.Series("slow", O.debounce(raw)))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    gate = switches()
    feat = pl.scan_parquet("data/features.parquet")
    bars = feat.select("instrument_id", "date", "open", "high", "low", "close").collect()
    univ = feat.filter(pl.col("universe")).select("instrument_id", "date", "close_raw", "turnover20", *ALL_FEATURES).collect().with_columns(
        ((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).alias("investable"))
    summary, years = [], []
    for name, cfg in SETUPS:
        t0 = time.time()
        df = univ.join(labels(bars, cfg), on=["instrument_id", "date"]).filter(pl.col("label").is_not_null())
        sessions = df["date"].unique().sort()
        scored_all = []
        for year in range(FIRST_TEST, sessions.max().year + 1):
            test = df.filter(pl.col("date").dt.year() == year)
            before = sessions.filter(sessions.dt.year() < year)
            if test.is_empty() or len(before) < 500:
                continue
            cut = before[-(cfg.horizon + 2)]
            train = df.filter(pl.col("date").is_in(before.filter(before <= cut)[::STRIDE].implode()))
            x, xt = (np.nan_to_num(f.select(ALL_FEATURES).to_numpy().astype(np.float32), nan=np.nan, posinf=np.nan, neginf=np.nan) for f in (train, test))
            p = xgb.XGBClassifier(**XGB).fit(x, train["label"].to_numpy()).predict_proba(xt)[:, 1]
            scored_all.append(test.select("date", "instrument_id", "label", "exit_ret", "exit_k", "investable").with_columns(pl.Series("p", p)))
        scored = pl.concat(scored_all).join(gate, on="date", how="left").with_columns(pl.col("plain").fill_null(True), pl.col("slow").fill_null(True))
        test_years = sorted(scored["date"].dt.year().unique().to_list())
        for sw in ("none", "plain", "slow"):
            s = scored if sw == "none" else scored.filter(pl.col(sw))
            open_share = s["date"].n_unique() / scored["date"].n_unique()
            base = s.filter(pl.col("investable"))
            summary.append({"setup": name, "switch": sw, "picks": "every investable entry", "open_share": open_share, **stats(base), "years_positive": None, "passes": None})
            for n in TOP:
                t = trades(s, cfg.horizon, n)
                by_year = {y: float(g["exit_ret"].mean()) - COST for (y,), g in t.group_by(pl.col("date").dt.year())}
                pos = sum(by_year.get(y, 0.0) > 0 for y in test_years)
                st = stats(t)
                ok = n == 5 and sw != "none" and st["expectancy"] >= MIN_NET and (st["profit_factor"] or 0) >= MIN_PF and pos >= MIN_YEARS
                summary.append({"setup": name, "switch": sw, "picks": f"model top {n}", "open_share": open_share, **st,
                                "years_positive": f"{pos}/{len(test_years)}", "passes": ok if n == 5 and sw != "none" else None})
                years += [{"setup": name, "switch": sw, "top": n, "year": y, "trades": t.filter(pl.col("date").dt.year() == y).height,
                           "net": by_year.get(y)} for y in test_years]
        top5 = [r for r in summary if r["setup"] == name and r["picks"] == "model top 5"]
        print(f"{name} ({time.time() - t0:.0f}s): " + "  ".join(f"{r['switch']} {r['expectancy']:+.4f} PF {r['profit_factor']:.2f} {r['years_positive']}" for r in top5), flush=True)
    s, y = pl.DataFrame(summary), pl.DataFrame(years)
    s.write_csv(OUT / "summary.csv"); y.write_csv(OUT / "years.csv")
    print(s.select("setup", "switch", "picks", "open_share", "trades", "win_rate", "payoff", "expectancy", "profit_factor", "worst", "years_positive", "passes"))
    print("PASSES:", s.filter(pl.col("passes") == True)["setup"].to_list() if s["passes"].drop_nulls().any() else "none")  # noqa: E712


if __name__ == "__main__":
    main()
