"""PM4.5: the four feature sets, the per-model metrics and the plain 4-vs-1 statement."""

from datetime import date, timedelta

import polars as pl

import run_pm4 as R
from portfolioml.features import ALL_FEATURES


def test_controls_and_technicals_split_the_price_features_exactly():
    assert not set(R.CONTROLS) & set(R.TECHNICALS)
    assert set(R.CONTROLS) | set(R.TECHNICALS) == set(ALL_FEATURES)


def test_each_model_holds_only_what_its_name_says():
    assert R.MODELS["controls"] == R.CONTROLS
    assert not set(R.MODELS["controls_fundamentals"]) & set(R.TECHNICALS)
    assert not set(R.MODELS["controls_technicals"]) & set(R.EXTRA)
    assert set(R.MODELS["all"]) == set(ALL_FEATURES) | set(R.EXTRA)
    assert "is_bank" in R.EXTRA


def sample() -> tuple[pl.DataFrame, pl.DataFrame]:
    rows, scores = [], []
    for day in (date(2024, 1, 1) + timedelta(days=i) for i in range(2)):
        for i in range(40):
            rows.append({"instrument_id": i, "date": day, "label": int(i >= 30), "fwd_ret_h": 1.2 if i >= 30 else -0.6 if i < 4 else 0.1, "close_raw": 100.0, "turnover20": 1e8})
            scores.append({"instrument_id": i, "date": day, "score": float(i)})
    return pl.DataFrame(rows), pl.DataFrame(scores)


def test_metrics_read_the_top_of_the_score_and_deciles_rise():
    frame, s = sample()
    metrics, deciles = R.metric_tables(frame, {"m": s})
    pooled = metrics.filter(pl.col("period") == "pooled").to_dicts()[0]
    assert pooled["auc"] == 1.0 and pooled["top20_hit"] == 0.5 and pooled["topdec_hit"] == 1.0
    assert pooled["investable_hit"] == 0.25 and pooled["top20_lift"] == 2.0 and pooled["top20_halved"] == 0.0
    d = deciles.filter(pl.col("period") == "pooled").sort("decile")["observed_2x"].to_list()
    assert d[0] == 0.0 and d[-1] == 1.0


def statement(a_lift, c_lift, a_half, c_half):
    return R.all_vs_controls(pl.DataFrame({"model": ["all", "controls"], "period": ["pooled", "pooled"], "top20_lift": [a_lift, c_lift], "top20_halved": [a_half, c_half]}))


def test_all_must_beat_controls_on_lift_without_more_halving():
    assert statement(2.0, 1.5, 0.05, 0.05).startswith("all BEATS")
    assert statement(1.4, 1.5, 0.05, 0.08).startswith("all DOES NOT")
    assert statement(2.0, 1.5, 0.09, 0.05).startswith("all DOES NOT")
