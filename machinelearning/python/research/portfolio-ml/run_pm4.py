"""PM4: the re-run. Same walk-forward and book as STRATEGY §3/§4, price-only vs with PM1–PM3 columns,
judged on 2024, 2025 and pooled 2024+2025. Refuses to run until the backfill has covered the universe.

usage: python run_pm4.py mb_u100_h250        (writes out/pm4/)"""

from __future__ import annotations

import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

from explore_multibagger import PARAMS, ROUNDS, TRAIN_STRIDE, grp, summarize
from portfolioml import fundamentals as F
from portfolioml import panel, sector as M, shareholding as S, tranches
from portfolioml.features import ALL_FEATURES

HERE = Path(__file__).parent
DATA, OUT = HERE / "data", HERE / "out" / "pm4"
MIN_COVERAGE = 0.70
COVERAGE_FROM = 2019
HIT_MULTIPLE = 1.5
GATES = {"2024": (2024,), "2025": (2025,), "2024+2025": (2024, 2025)}
EXTRA = [*F.FEATURES, "days_since_filing", *S.FEATURES, *M.FEATURES]
KEEP = list(dict.fromkeys(["instrument_id", "date", "label", "held", "fwd_ret_h", "fwd_max_ret", "fwd_min_ret", "fwd_min_close_ret",
                           "entry_px", "close", "close_raw", "turnover20", "r21", "r63", *ALL_FEATURES]))


def fundamentals_coverage(keys: pl.DataFrame, filings: pl.DataFrame) -> tuple[float, pl.DataFrame]:
    """Share of universe rows from COVERAGE_FROM with a TTM figure, and the same by year."""
    joined = F.join_features(keys, F.snapshots(filings))
    recent = joined.filter(pl.col("date").dt.year() >= COVERAGE_FROM)
    share = 0.0 if recent.is_empty() else recent["pat_margin_ttm"].is_not_null().mean()
    return share, F.coverage_by_year(joined)


def assemble(univ: pl.DataFrame, filings: pl.DataFrame, holdings: pl.DataFrame, sectors: pl.DataFrame) -> pl.DataFrame:
    """The universe with every PM1–PM3 column joined on what was known that day."""
    out = F.join_features(univ, F.snapshots(filings))
    out = S.join_features(out, S.snapshots(holdings))
    out = M.join_mcap(out, filings).join(sectors, on="instrument_id", how="left")
    out = M.sector_features(M.mcap_features(out))
    return out.with_columns([pl.col(c).cast(pl.Float32) for c in EXTRA]).sort("date", "instrument_id")


def walk_forward(frame: pl.DataFrame, features: list[str], horizon: int) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """STRATEGY §3: every fifth signal date ≤ year-end minus the horizon; test each year's labelled rows."""
    sessions = frame["date"].unique().sort().to_list()
    lab = frame.filter(pl.col("label").is_not_null())
    x = lambda df: df.select(features).to_numpy().astype(np.float32)  # noqa: E731
    scores, aucs, imps = [], [], []
    for y in sorted({d.year for d in sessions if d.year >= 2020}):
        before = [d for d in sessions if d.year < y]
        if len(before) < horizon + 300:
            continue
        train_end = before[-(horizon + 6)]
        train = lab.filter((pl.col("date") <= train_end) & pl.col("date").is_in(before[::TRAIN_STRIDE])).sort("date")
        test = lab.filter(pl.col("date").dt.year() == y).sort("date")
        if test.is_empty() or train["label"].sum() < 50:
            continue
        model = lgb.train(PARAMS, lgb.Dataset(x(train), train["label"].to_numpy(), group=grp(train)), ROUNDS)
        test = test.with_columns(pl.Series("score", model.predict(x(test))))
        scores.append(test.select("instrument_id", "date", "score"))
        aucs.append({"year": y, "auc": summarize(test, "score")["auc"]})
        imps.append(pl.DataFrame({"feature": features, "gain": model.feature_importance("gain").astype(float), "year": y}))
        print(f"  {y} auc {aucs[-1]['auc']:.3f}", flush=True)
    return pl.concat(scores), pl.DataFrame(aucs), pl.concat(imps)


def gate_table(picks: dict[str, pl.DataFrame], base: pl.DataFrame) -> pl.DataFrame:
    """Each model's picks against the investable universe, for every gate window (06 PM4 AC1)."""
    rows = []
    for window, years in GATES.items():
        u = base.filter(pl.col("date").dt.year().is_in(years))
        for model, p in picks.items():
            t = p.filter(pl.col("date").dt.year().is_in(years))
            top_mean, univ_mean, top_hit, univ_hit = t["fwd_ret_h"].mean(), u["fwd_ret_h"].mean(), t["label"].mean(), u["label"].mean()
            ret_ok, hit_ok = bool(top_mean >= univ_mean), bool(top_hit >= HIT_MULTIPLE * univ_hit)
            rows.append({"window": window, "model": model, "picks": t.height, "top20_mean_12m": top_mean,
                         "universe_mean_12m": univ_mean, "top20_hit": top_hit, "universe_hit": univ_hit,
                         "hit_multiple": top_hit / univ_hit, "return_ok": ret_ok, "hit_ok": hit_ok, "pass": ret_ok and hit_ok})
    return pl.DataFrame(rows)


def verdict(gate: pl.DataFrame, model: str) -> bool:
    return gate.filter(pl.col("model") == model)["pass"].all()


def main(geometry: str) -> int:
    horizon = int(geometry.split("_h")[1])
    source = DATA / f"dataset_{geometry}.parquet"
    if not (DATA / "fundamental_pit.csv.gz").exists() or not source.exists():
        print("REFUSED: run the backfill, export_bars_local.sh and build_dataset.py first (NEEDS-MAULIK.md)")
        return 2
    filings = panel.load_fundamentals(DATA)
    keys = pl.scan_parquet(source).filter(pl.col("universe")).select("instrument_id", "date").collect()
    share, by_year = fundamentals_coverage(keys, filings)
    pl.Config.set_tbl_rows(40); pl.Config.set_tbl_width_chars(200); pl.Config.set_float_precision(3)
    print(by_year)
    if share < MIN_COVERAGE:
        print(f"REFUSED: fundamental_pit covers {share:.1%} of universe rows from {COVERAGE_FROM}; needs {MIN_COVERAGE:.0%}")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    by_year.write_csv(OUT / "coverage_fundamentals.csv")
    univ = pl.scan_parquet(source).filter(pl.col("universe")).select(KEEP).collect()
    frame = assemble(univ, filings, panel.load_shareholding(DATA), panel.load_sector(DATA))
    S.coverage_by_year(frame).write_csv(OUT / "coverage_shareholding.csv")
    print(S.coverage_by_year(frame))
    names_with_sector = M.sector_coverage(frame)
    check = M.mcap_check(frame, panel.load_fundamental_daily(DATA))
    print(f"sector coverage {names_with_sector:.1%} of names; mcap vs fundamental_daily {check}")
    print("sector_* features carry the static-sector leak (PM3.1)")

    print("price-only", flush=True)
    price, price_auc, _ = walk_forward(frame, ALL_FEATURES, horizon)
    print("with PM1–PM3", flush=True)
    full, full_auc, imps = walk_forward(frame, [*ALL_FEATURES, *EXTRA], horizon)

    investable = frame.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7))
    book = tranches.TrancheConfig(horizon=horizon, max_per_sector=None)
    capped = tranches.TrancheConfig(horizon=horizon, max_per_sector=3)
    flat = price.select("instrument_id", "date", pl.lit(0.0).alias("score"))
    sims = {"price_only": (price, book), "with_fundamentals": (full, book), "with_fundamentals_cap3": (full, capped)}
    picks, books = {}, []
    bench = panel.load_index_series(DATA).select("date", "nifty50", "midcap150", "smallcap250")
    for name, (scores, cfg) in sims.items():
        p, eq = tranches.simulate(investable, scores, "score", cfg)
        picks[name] = p
        books.append({"sim": name, **tranches.summary(eq, p, bench)})
        p.write_csv(OUT / f"picks_{name}.csv")
    base, _ = tranches.simulate(investable, flat, "score", tranches.TrancheConfig(top_n=10_000, horizon=horizon, max_per_sector=None))

    gate = gate_table({k: picks[k] for k in ("price_only", "with_fundamentals")}, base)
    gate.write_csv(OUT / "gate.csv")
    print(gate)
    aucs = price_auc.rename({"auc": "price_only"}).join(full_auc.rename({"auc": "with_fundamentals"}), on="year").with_columns(
        (pl.col("with_fundamentals") - pl.col("price_only")).alias("delta"))
    aucs.write_csv(OUT / "auc_by_year.csv")
    print(aucs)
    pl.DataFrame(books).write_csv(OUT / "tranche_summary.csv")
    print(pl.DataFrame(books).select("sim", "cagr", "max_dd", "sharpe", "picks", "pct_doubled_touch", "mean_12m", "median_12m"))
    top = imps.group_by("feature").agg(pl.col("gain").mean()).with_columns((pl.col("gain") / pl.col("gain").sum()).alias("share")).sort("gain", descending=True)
    top.write_csv(OUT / "importance.csv")
    print(top.head(12))
    in_top = top.head(12).filter(pl.col("feature").is_in(EXTRA)).height
    ac1, ac2 = verdict(gate, "with_fundamentals"), bool((aucs["delta"] >= -0.02).all())
    print(f"AC1 gate (each window): {'PASS' if ac1 else 'FAIL'}")
    print(f"AC2 no year's AUC down more than 0.02: {'PASS' if ac2 else 'FAIL'}")
    print(f"AC3 PM1–PM3 features in the top 12: {in_top}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "mb_u100_h250"))
