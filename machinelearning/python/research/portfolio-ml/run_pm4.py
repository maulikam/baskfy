"""PM4: the re-run, a four-way comparison on the walk-forward and book of STRATEGY §3/§4 (PM4.5): controls,
controls + fundamentals, controls + technicals, all. The gate is judged on model 4, on 2024, 2025 and pooled
2024+2025, dead names included, terminal variant (b). Refuses to run until PM0.7 has run and the backfill has
covered the universe.

usage: python run_pm4.py mb_u100_h250 [--where-covered | --blank-rates]        (writes out/pm4/)
Both the common-coverage rows and the full universe always run; --where-covered makes the first the judged one (PM4.4).
--blank-rates reports PM1/PM2 blanks, dead vs live, by year, and stops; it needs no coverage."""

from __future__ import annotations

import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

from sklearn.metrics import average_precision_score, roc_auc_score

from explore_multibagger import PARAMS, ROUNDS, TRAIN_STRIDE, grp, summarize
from portfolioml import fundamentals as F
from portfolioml import panel, sector as M, shareholding as S, tranches
from portfolioml.features import ALL_FEATURES, MARKET_FEATURES

HERE = Path(__file__).parent
DATA, OUT = HERE / "data", HERE / "out" / "pm4"
MIN_COVERAGE = 0.70
COVERAGE_FROM = 2019
HIT_MULTIPLE = 1.5
TERMINAL = "b"  # 06 PM4 AC1
GATES = {"2024": (2024,), "2025": (2025,), "2024+2025": (2024, 2025)}
# No sector features: only dead names lack a sector, so a null would mark them (PM3.4).
EXTRA = [*F.FEATURES, "days_since_filing", *S.FEATURES, *M.MCAP_FEATURES]
KEEP = list(dict.fromkeys(["instrument_id", "date", "label", "held", "fwd_ret_h", "fwd_max_ret", "fwd_min_ret", "fwd_min_close_ret",
                           "entry_px", "close", "close_raw", "turnover20", "r21", "r63", "terminal", *ALL_FEATURES]))
CONTROLS = ["log_px", "log_turnover20", "log_turnover20_cs", "amihud20", "amihud20_cs", "n_hist", "vol20", "vol20_cs", "vol60", "atr14_pct", "atr14_pct_cs", *MARKET_FEATURES]
TECHNICALS = [f for f in ALL_FEATURES if f not in CONTROLS]
MODELS = {"controls": CONTROLS, "controls_fundamentals": [*CONTROLS, *EXTRA], "controls_technicals": [*CONTROLS, *TECHNICALS], "all": [*CONTROLS, *TECHNICALS, *EXTRA]}
BLANK_FEATURES = [*F.FEATURES, "days_since_filing", *S.FEATURES]
TTM = "pat_margin_ttm"
MAX_BLANK_GAP = 0.10


def dead_names_present(data: Path) -> bool:
    """PM0.7 has run: some instrument has a delisted_on."""
    path = data / "instrument.csv.gz"
    if not path.exists():
        return False
    return pl.read_csv(path, columns=["delisted_on"], schema_overrides={"delisted_on": pl.String})["delisted_on"].is_not_null().any()


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


def blank_rates(frame: pl.DataFrame, features: list[str]) -> pl.DataFrame:
    """Blank share of each feature by year, dead vs live; `scope` is every row or the ever-investable names."""
    ever = frame.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7))["instrument_id"].unique()
    tagged = frame.with_columns(pl.col("date").dt.year().alias("year"), (pl.col("terminal") != "alive").alias("dead"))
    rows = []
    for scope, part in (("all", tagged), ("ever_investable", tagged.filter(pl.col("instrument_id").is_in(ever.to_list())))):
        long = part.select("year", "dead", *features).unpivot(index=["year", "dead"], variable_name="feature")
        got = long.group_by("year", "feature", "dead").agg(pl.col("value").is_null().mean().alias("blank"), pl.len().alias("rows"))
        dead, live = got.filter(pl.col("dead")).drop("dead"), got.filter(~pl.col("dead")).drop("dead")
        rows.append(dead.join(live, on=["year", "feature"], suffix="_live").rename({"blank": "blank_dead", "rows": "rows_dead"})
                    .with_columns(pl.lit(scope).alias("scope"), (pl.col("blank_dead") - pl.col("blank_live")).alias("gap")))
    return pl.concat(rows).select("scope", "year", "feature", "blank_dead", "blank_live", "gap", "rows_dead", "rows_live").sort("scope", "year", "feature")


def blank_gap_breached(rates: pl.DataFrame) -> bool:
    """Any feature and year from COVERAGE_FROM whose dead and live blank rates differ by more than MAX_BLANK_GAP."""
    return bool(rates.filter((pl.col("year") >= COVERAGE_FROM) & (pl.col("gap").abs() > MAX_BLANK_GAP)).height)


def covered(frame: pl.DataFrame) -> pl.DataFrame:
    """Rows where the TTM figure exists, so missingness cannot carry information (PM4.4)."""
    return frame.filter(pl.col(TTM).is_not_null())


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


def _pick_stats(picks: pl.DataFrame, base_hit: float, prefix: str) -> dict:
    hit = picks["label"].mean()
    return {f"{prefix}_n": picks.height, f"{prefix}_hit": hit, f"{prefix}_lift": hit / base_hit if base_hit else None, f"{prefix}_halved": (picks["fwd_ret_h"] <= -0.5).mean(),
            f"{prefix}_lost_25": (picks["fwd_ret_h"] < -0.25).mean(), f"{prefix}_median_12m": picks["fwd_ret_h"].median()}


def _period_metrics(universe: pl.DataFrame) -> dict:
    """AUC and average precision on the universe; top-20 and top decile each date among investable names."""
    investable = universe.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).with_columns(
        pl.col("score").rank("ordinal", descending=True).over("date").alias("rk"), pl.len().over("date").alias("n"))
    base = investable["label"].mean()
    y, s = universe["label"].to_numpy(), universe["score"].to_numpy()
    return {"auc": float(roc_auc_score(y, s)), "average_precision": float(average_precision_score(y, s)), "investable_hit": base,
            **_pick_stats(investable.filter(pl.col("rk") <= 20), base, "top20"), **_pick_stats(investable.filter(pl.col("rk") <= pl.col("n") * 0.1), base, "topdec")}


def metric_tables(frame: pl.DataFrame, scores: dict[str, pl.DataFrame]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Per model, by year and pooled: the PM4.5 metrics, and the observed 2× rate by score decile (9 is the highest)."""
    rows, deciles = [], []
    for model, s in scores.items():
        scored = frame.join(s, on=["instrument_id", "date"]).filter(pl.col("label").is_not_null() & pl.col("fwd_ret_h").is_not_null()).with_columns(pl.col("date").dt.year().cast(pl.String).alias("period"))
        ranked = scored.with_columns((((pl.col("score").rank("ordinal").over("date") - 1) * 10 / pl.len().over("date")).floor().cast(pl.Int32)).alias("decile"))
        for period, part in [("pooled", scored), *[(k[0], g) for k, g in scored.group_by("period", maintain_order=True)]]:
            rows.append({"model": model, "period": period, **_period_metrics(part)})
        for scope, part in (("pooled", ranked), ("year", ranked)):
            keys = ["decile"] if scope == "pooled" else ["period", "decile"]
            d = part.group_by(keys).agg(pl.len().alias("n"), pl.col("label").mean().alias("observed_2x"))
            deciles.append(d.with_columns(pl.lit(model).alias("model"), pl.lit("pooled").alias("period") if scope == "pooled" else pl.col("period")).select("model", "period", "decile", "n", "observed_2x"))
    return pl.DataFrame(rows).sort("model", "period"), pl.concat(deciles).sort("model", "period", "decile")


def all_vs_controls(metrics: pl.DataFrame) -> str:
    """PM4.5: does "all" beat "controls only" on top-20 lift without a higher halved rate? Said plainly."""
    get = lambda m, c: metrics.filter((pl.col("model") == m) & (pl.col("period") == "pooled"))[c][0]  # noqa: E731
    a_lift, c_lift, a_half, c_half = get("all", "top20_lift"), get("controls", "top20_lift"), get("all", "top20_halved"), get("controls", "top20_halved")
    beat = a_lift > c_lift and a_half <= c_half
    return f"{'all BEATS controls only' if beat else 'all DOES NOT beat controls only'}: top-20 lift {a_lift:.2f} vs {c_lift:.2f}, halved {a_half:.1%} vs {c_half:.1%}"


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


def evaluate(frame: pl.DataFrame, horizon: int, marks: pl.DataFrame, bench: pl.DataFrame, suffix: str) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """The four models on `frame`: walk-forward, metrics, books, gate, AUCs, importance. Files carry `suffix`."""
    scores, aucs_by, imps_by = {}, {}, {}
    for name, features in MODELS.items():
        print(name, flush=True)
        scores[name], aucs_by[name], imps_by[name] = walk_forward(frame, features, horizon)
    metrics, deciles = metric_tables(frame, scores)
    metrics.write_csv(OUT / f"model_metrics{suffix}.csv")
    deciles.write_csv(OUT / f"score_deciles{suffix}.csv")
    print(metrics.filter(pl.col("period") == "pooled"))
    print(all_vs_controls(metrics))
    imps = imps_by["all"]

    investable = frame.filter((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7))
    book = tranches.TrancheConfig(horizon=horizon, max_per_sector=None, terminal=TERMINAL)
    capped = tranches.TrancheConfig(horizon=horizon, max_per_sector=3, terminal=TERMINAL)
    flat = scores["all"].select("instrument_id", "date", pl.lit(0.0).alias("score"))
    sims = {**{name: (s, book) for name, s in scores.items()}, "all_cap3": (scores["all"], capped)}
    picks, books = {}, []
    for name, (scores, cfg) in sims.items():
        p, eq = tranches.simulate(investable, scores, "score", cfg, marks=marks)
        picks[name] = p
        books.append({"sim": name, **tranches.summary(eq, p, bench)})
        p.write_csv(OUT / f"picks_{name}{suffix}.csv")
    base, _ = tranches.simulate(investable, flat, "score", tranches.TrancheConfig(top_n=10_000, horizon=horizon, max_per_sector=None, terminal=TERMINAL), marks=marks)

    gate = gate_table({k: picks[k] for k in MODELS}, base)
    gate.write_csv(OUT / f"gate{suffix}.csv")
    print(gate)
    aucs = aucs_by["controls_technicals"].rename({"auc": "price_only"}).join(aucs_by["all"].rename({"auc": "all"}), on="year").with_columns(
        (pl.col("all") - pl.col("price_only")).alias("delta"))
    aucs.write_csv(OUT / f"auc_by_year{suffix}.csv")
    print(aucs)
    pl.DataFrame(books).write_csv(OUT / f"tranche_summary{suffix}.csv")
    print(pl.DataFrame(books).select("sim", "cagr", "max_dd", "sharpe", "picks", "pct_doubled_touch", "mean_12m", "median_12m"))
    top = imps.group_by("feature").agg(pl.col("gain").mean()).with_columns((pl.col("gain") / pl.col("gain").sum()).alias("share")).sort("gain", descending=True)
    top.write_csv(OUT / f"importance{suffix}.csv")
    print(top.head(12))
    return gate, aucs, top


def report_blanks(source: Path, filings: pl.DataFrame) -> int:
    """Run when the backfill has reached the dead names; `ever_investable` is reliable first, `all` once it finishes."""
    univ = pl.scan_parquet(source).filter(pl.col("universe")).select(KEEP).collect()
    frame = S.join_features(F.join_features(univ, F.snapshots(filings)), S.snapshots(panel.load_shareholding(DATA)))
    rates = blank_rates(frame, BLANK_FEATURES)
    OUT.mkdir(parents=True, exist_ok=True)
    rates.write_csv(OUT / "blank_rates.csv")
    pl.Config.set_tbl_rows(60); pl.Config.set_tbl_width_chars(200); pl.Config.set_float_precision(3)
    print(rates.filter(pl.col("year") >= COVERAGE_FROM).sort(pl.col("gap").abs(), descending=True).head(40))
    print(f"gap above {MAX_BLANK_GAP:.0%}: {'YES, use --where-covered' if blank_gap_breached(rates) else 'no'}")
    return 0


def main(geometry: str, where_covered: bool = False, blanks_only: bool = False) -> int:
    horizon = int(geometry.split("_h")[1])
    source = DATA / f"dataset_{geometry}_t{TERMINAL}.parquet"
    if not dead_names_present(DATA):
        print("REFUSED: delisted_on is null for every instrument; PM0.7 has not run on this export")
        return 2
    if not (DATA / "fundamental_pit.csv.gz").exists() or not source.exists():
        print("REFUSED: run the backfill, export_bars_local.sh and build_dataset.py first (NEEDS-MAULIK.md)")
        return 2
    filings = panel.load_fundamentals(DATA)
    if blanks_only:
        return report_blanks(source, filings)
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

    marks = pl.scan_parquet(source).select("instrument_id", "date", "close", "terminal").collect()
    bench = panel.load_index_series(DATA).select("date", "nifty50", "midcap150", "smallcap250")
    print("common-coverage rows", flush=True)
    judged = evaluate(covered(frame), horizon, marks, bench, "_covered")
    print("full universe", flush=True)
    full = evaluate(frame, horizon, marks, bench, "_full")
    gate, aucs, top = judged if where_covered else full
    print(f"JUDGED: {'common-coverage rows' if where_covered else 'full universe'} (PM4.4)", flush=True)
    in_top = top.head(12).filter(pl.col("feature").is_in(EXTRA)).height
    ac1, ac2 = verdict(gate, "all"), bool((aucs["delta"] >= -0.02).all())
    print(f"AC1 gate (each window): {'PASS' if ac1 else 'FAIL'}")
    print(f"AC2 no year's AUC down more than 0.02: {'PASS' if ac2 else 'FAIL'}")
    print(f"AC3 PM1–PM3 features in the top 12: {in_top}")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(args[0] if args else "mb_u100_h250", "--where-covered" in sys.argv, "--blank-rates" in sys.argv))
