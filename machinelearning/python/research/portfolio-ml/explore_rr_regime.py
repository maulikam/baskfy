"""PM10d: which market switch keeps the swing setups out of the bad years? (Maulik, 11 Nov 2026: 200-DMA, crossovers.)

Seven switches, all on information at the signal session's close (entry the next open), fixed before the run:
  sc_100       NIFTY Smallcap 250 above its 100-session average (PM10c, the reference)
  sc_200       ... above its 200-session average
  sc_50_200    its 50-session average above its 200-session average
  sc_20_50     its 20-session average above its 50-session average
  sc_200_both  sc_200 and sc_50_200
  sc_200_up    its 200-session average higher than 20 sessions earlier
  breadth_50   at least half the liquid universe above its own 200-session average (features.breadth200)
Setups: PM10b's trail −7 % / 12 % h60 and fixed −7/+25 h40, the model's top 5 a day. Pass rule as PM10c: net ≥ +0.5 % a
trade, profit factor ≥ 1.2, ≥ 8 of 11 years positive. Seven switches on 11 years: a pass earns a paper trial, not
adoption. Also written: each switch's share of sessions on, by year.

usage: python explore_rr_regime.py      → out/rr_regime/{summary,years,on_share}.csv. Research only."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
import xgboost as xgb

from explore_rr_gate import MIN_NET, MIN_PF, MIN_YEARS
from explore_rr_swing import FIRST_TEST, SETUPS, labels, stats
from explore_xgb_swing import COST, STRIDE, XGB, trades
from portfolioml import panel
from portfolioml.features import ALL_FEATURES

OUT = Path("out/rr_regime")
KEEP = ("trail -7 / 12% h60", "fixed -7/+25 h40")
TOP_N = 5
pl.Config.set_tbl_rows(120); pl.Config.set_tbl_width_chars(240); pl.Config.set_float_precision(4); pl.Config.set_tbl_cols(20)


def switches(breadth: pl.DataFrame) -> pl.DataFrame:
    sc = pl.col("smallcap250")
    ma = lambda n: sc.rolling_mean(n, min_samples=n)  # noqa: E731
    idx = panel.load_index_series(Path("data")).select("date", "smallcap250").drop_nulls().sort("date")
    g = idx.with_columns(ma(20).alias("m20"), ma(50).alias("m50"), ma(100).alias("m100"), ma(200).alias("m200")).with_columns(
        (sc > pl.col("m100")).alias("sc_100"),
        (sc > pl.col("m200")).alias("sc_200"),
        (pl.col("m50") > pl.col("m200")).alias("sc_50_200"),
        (pl.col("m20") > pl.col("m50")).alias("sc_20_50"),
        (pl.col("m200") > pl.col("m200").shift(20)).alias("sc_200_up"),
    ).with_columns((pl.col("sc_200") & pl.col("sc_50_200")).alias("sc_200_both"))
    g = g.join(breadth, on="date", how="left").with_columns((pl.col("breadth200") >= 0.5).alias("breadth_50"))
    names = ["sc_100", "sc_200", "sc_50_200", "sc_20_50", "sc_200_both", "sc_200_up", "breadth_50"]
    return g.select("date", *[pl.col(n).fill_null(True) for n in names]), names


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    feat = pl.scan_parquet("data/features.parquet")
    breadth = feat.select("date", "breadth200").unique("date").collect()
    gate, names = switches(breadth)
    bars = feat.select("instrument_id", "date", "open", "high", "low", "close").collect()
    univ = feat.filter(pl.col("universe")).select("instrument_id", "date", "close_raw", "turnover20", *ALL_FEATURES).collect().with_columns(
        ((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).alias("investable"))
    summary, years = [], []
    for name, cfg in [s for s in SETUPS if s[0] in KEEP]:
        df = univ.join(labels(bars, cfg), on=["instrument_id", "date"]).filter(pl.col("label").is_not_null())
        sessions = df["date"].unique().sort()
        parts = []
        for year in range(FIRST_TEST, sessions.max().year + 1):
            test = df.filter(pl.col("date").dt.year() == year)
            before = sessions.filter(sessions.dt.year() < year)
            if test.is_empty() or len(before) < 500:
                continue
            cut = before[-(cfg.horizon + 2)]
            train = df.filter(pl.col("date").is_in(before.filter(before <= cut)[::STRIDE].implode()))
            x, xt = (np.nan_to_num(f.select(ALL_FEATURES).to_numpy().astype(np.float32), nan=np.nan, posinf=np.nan, neginf=np.nan) for f in (train, test))
            p = xgb.XGBClassifier(**XGB).fit(x, train["label"].to_numpy()).predict_proba(xt)[:, 1]
            parts.append(test.select("date", "instrument_id", "label", "exit_ret", "exit_k", "investable").with_columns(pl.Series("p", p)))
        scored = pl.concat(parts).join(gate, on="date", how="left")
        test_years = sorted(scored["date"].dt.year().unique().to_list())
        for sw in ["none", *names]:
            s = scored if sw == "none" else scored.filter(pl.col(sw))
            t = trades(s, cfg.horizon, TOP_N)
            by_year = {y: float(g["exit_ret"].mean()) - COST for (y,), g in t.group_by(pl.col("date").dt.year())}
            pos = sum(by_year.get(y, 0.0) > 0 for y in test_years)
            st = stats(t)
            summary.append({"setup": name, "switch": sw, "on_share": s["date"].n_unique() / scored["date"].n_unique(), **st,
                            "years_positive": f"{pos}/{len(test_years)}",
                            "passes": sw != "none" and st["expectancy"] >= MIN_NET and (st["profit_factor"] or 0) >= MIN_PF and pos >= MIN_YEARS})
            years += [{"setup": name, "switch": sw, "year": y, "trades": t.filter(pl.col("date").dt.year() == y).height, "net": by_year.get(y)} for y in test_years]
        print(f"{name}: done", flush=True)
    on = gate.filter(pl.col("date").dt.year() >= FIRST_TEST).group_by(pl.col("date").dt.year().alias("year")).agg(
        [pl.col(n).mean() for n in names]).sort("year")
    s, y = pl.DataFrame(summary), pl.DataFrame(years)
    s.write_csv(OUT / "summary.csv"); y.write_csv(OUT / "years.csv"); on.write_csv(OUT / "on_share.csv")
    print(s.select("setup", "switch", "on_share", "trades", "win_rate", "expectancy", "profit_factor", "worst", "years_positive", "passes"))
    print(on)
    for setup in KEEP:
        print(setup); print(y.filter(pl.col("setup") == setup).pivot(on="switch", index="year", values="net"))


if __name__ == "__main__":
    main()
