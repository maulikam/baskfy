"""C8 ablation: rank monthly with core's ranking engine, simulate with core's validator, one CSV row per model.

    cd decile-blueprint && uv run python ../research/ranking-validation/run_validation.py [--limit N]

Stage 1 builds (or reuses) the factor cache — see ``rvl/factors.py``; nothing is recomputed per
model. Stage 2 runs the models one at a time: ``ranking_engine.rank_frame`` per signal date, then
``ranking_validation.simulate_monthly_rebalance`` over one shared price panel, then
``summary_row``. ``--limit N`` runs everything on the N most-often-liquid instruments into
``cache/subset-N`` and ``out/ablation-subsetN.csv`` (the memory/timing rehearsal).

The models (docs/ranking/PLAN.md C8; the base is this runner's choice, stated here):

* ``base`` — composite over ``factors_ranking.MOM_PCTILE_SOURCE`` (``avg_sharpe_12_6_3_1``), the
  screener's momentum blend and the source of ``mom_pctile``. Entry 20 / retention 40, top 20.
* ``base+<key>`` — base plus one term, for every registry factor that is rankable and not
  ``legacy`` (the C1 set: stored ranking factors, ``regime_priority``, ``nse_momentum_score``),
  each with its registry preference. ``atr_ext_20``'s target range is C7's ``[0, 3]``.
  Composite family shares are the engine's defaults (equal per family present).
* ``sharpe_12_6`` vs ``sortino_for_sharpe`` — the 12M+6M Sharpe pair against the 12M+6M Sortino
  pair (Sortino exists only for 6M/12M, so the base's 4-window blend is not the fair control).
* ``base+rsi_penalty`` — base with the desk F-penalty's RSI band subtracted from the composite
  (rsi_1m > 82: -4, > 78: -2 points; ``baskfy_core.score``), re-ranked. ``base`` is the "off" row.
* ``base|regime_in=...`` — base with C3's ``regime_in`` eligibility filter on the instrument regime.
* ``grid_e<E>_r<R>`` — base ranking under each entry/retention cell (retention >= entry).
"""

from __future__ import annotations

import argparse
import datetime as dt
import gc
import math
import resource
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from baskfy_core.factor_registry import FACTORS, ValidationStatus  # noqa: E402
from baskfy_core.factors_ranking import MOM_PCTILE_SOURCE  # noqa: E402
from baskfy_core.ranking_engine import (  # noqa: E402
    COMPOSITE_SCORE,
    IN_NIFTY_200,
    IS_FNO,
    PASSES_FILTERS,
    RankingSpec,
    TermSpec,
    rank_frame,
)
from baskfy_core.ranking_validation import (  # noqa: E402
    ValidationConfig,
    build_price_panel,
    simulate_monthly_rebalance,
    summary_row,
)

from rvl import data, factors  # noqa: E402

#: PLAN C7 ``trend_structure``: "atr_ext_20 target [0,3]".
TARGET_RANGES: dict[str, tuple[float | None, float | None]] = {"atr_ext_20": (0.0, 3.0)}
#: baskfy_core.score F-penalty, RSI band: ``rsi_one_month > 82 -> -4, > 78 -> -2``.
RSI_PENALTY_BANDS: tuple[tuple[float, float], ...] = ((82.0, 4.0), (78.0, 2.0))
REGIME_FILTERS: tuple[tuple[str, ...], ...] = (("BULL",), ("BULL", "NEUTRAL"))
GRID_ENTRY = (10, 15, 20, 25)
GRID_RETENTION = (20, 30, 40, 60)
BASE_CONFIG = ValidationConfig()  # C8: top 20, entry 20 / retention 40, 25 bps a side

DATA_NOTES = {
    "rs_persist_126": "NIFTY 500 levels absent from the export: factor is NULL, row equals base",
    "nse_momentum_score": "Nifty 200 / F&O membership exported only from 2021-08-02",
}


def c1_rankable_keys() -> list[str]:
    return [
        key
        for key, factor in FACTORS.items()
        if factor.rankable and factor.validation_status is not ValidationStatus.LEGACY
    ]


def term(key: str) -> TermSpec:
    factor = FACTORS[key]
    low, high = TARGET_RANGES.get(key, (None, None))
    return TermSpec(
        key=key,
        label=factor.label,
        weight_family=factor.weight_family.value,
        preference=factor.preference.value,
        target_min=low,
        target_max=high,
        null_policy=factor.null_policy.value,
    )


def composite(*keys: str) -> RankingSpec:
    return RankingSpec(
        terms=tuple(term(k) for k in keys), mode="composite", scope="filtered_results"
    )


Filter = Callable[[pd.DataFrame], pd.Series]
PostRank = Callable[[pd.DataFrame], pd.DataFrame]


def rsi_penalised(ranked: pd.DataFrame) -> pd.DataFrame:
    rsi = ranked["rsi_1m"].astype("float64")
    penalty = pd.Series(0.0, index=ranked.index)
    for threshold, points in reversed(RSI_PENALTY_BANDS):
        penalty = penalty.mask(rsi > threshold, points)
    out = ranked.assign(_adjusted=ranked[COMPOSITE_SCORE].astype("float64") - penalty)
    out = out.assign(_first=-out[MOM_PCTILE_SOURCE].astype("float64").fillna(-np.inf))
    out = out.sort_values(["_adjusted", "_first", "instrument_id"], ascending=[False, True, True])
    out["rank"] = range(1, len(out) + 1)
    return out.drop(columns=["_adjusted", "_first"])


def signals_for(
    cache: pd.DataFrame,
    spec: RankingSpec,
    keep: Filter | None = None,
    post: PostRank | None = None,
) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for day, frame in cache.groupby("date", sort=True):
        frame = frame.assign(
            **{PASSES_FILTERS: True if keep is None else keep(frame).to_numpy()}
        )
        ranked = rank_frame(frame, spec).ranked
        if post is not None:
            ranked = post(ranked)
        parts.append(
            pd.DataFrame(
                {
                    "date": day,
                    "instrument_id": ranked["instrument_id"].astype("int64").to_numpy(),
                    "quality_rank": ranked["rank"].astype("int64").to_numpy(),
                    "symbol": ranked["symbol"].astype(str).to_numpy(),
                }
            )
        )
    return pd.concat(parts, ignore_index=True)


def rss_gb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9  # bytes on macOS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    t0 = time.time()

    factors.build_cache(args.limit)
    print(f"stage 1 done [{time.time() - t0:.0f}s, max RSS {rss_gb():.2f} GB]", flush=True)

    keys = c1_rankable_keys()
    columns = sorted(
        {
            "date",
            "instrument_id",
            "symbol",
            "rsi_1m",
            "regime",
            IN_NIFTY_200,
            IS_FNO,
            MOM_PCTILE_SOURCE,
            "sharpe_12m",
            "sharpe_6m",
            *(k for k in keys if k != "nse_momentum_score"),
            "nse_mr6",
            "nse_mr12",
        }
    )
    cache = factors.load_cache(args.limit, columns)
    cache["date"] = pd.to_datetime(cache["date"]).dt.date
    dates = sorted(cache["date"].unique())
    ids = sorted(int(i) for i in cache["instrument_id"].unique())
    print(f"cache: {len(cache)} rows, {len(dates)} dates, {len(ids)} instruments", flush=True)

    bars = data.load_bars(["instrument_id", "date", "open", "close"], ids=ids, start=dates[0])
    panel = build_price_panel(data.to_pandas(bars))
    del bars
    gc.collect()
    print(
        f"panel: {len(panel.sessions)} sessions x {len(panel.instrument_ids)} instruments "
        f"[{time.time() - t0:.0f}s, max RSS {rss_gb():.2f} GB]",
        flush=True,
    )

    rows: list[dict[str, object]] = []
    base_signals: pd.DataFrame | None = None

    def record(
        model: str,
        signals: pd.DataFrame,
        config: ValidationConfig = BASE_CONFIG,
        **extra: object,
    ) -> None:
        t = time.time()
        result = simulate_monthly_rebalance(panel, signals, config)
        row: dict[str, object] = dict(summary_row(model, result))
        row.update(extra)
        rows.append(row)
        oos = result.out_of_sample
        print(
            f"  {model:34s} full CAGR {result.full.cagr_net}  DD {result.full.max_drawdown}  "
            f"OOS CAGR {oos.cagr_net if oos else None}  turnover {result.full.annual_turnover}  "
            f"[{time.time() - t:.0f}s, max RSS {rss_gb():.2f} GB]",
            flush=True,
        )
        del result

    def coverage(key: str) -> float:
        if key not in cache.columns:
            return math.nan
        return round(float(cache[key].notna().mean()), 4)

    base_spec = composite(MOM_PCTILE_SOURCE)
    base_signals = signals_for(cache, base_spec)
    record("base", base_signals, study="base", factor=MOM_PCTILE_SOURCE)

    for key in keys:
        signals = signals_for(cache, composite(MOM_PCTILE_SOURCE, key))
        record(
            f"base+{key}",
            signals,
            study="factor",
            factor=key,
            preference=FACTORS[key].preference.value,
            weight_family=FACTORS[key].weight_family.value,
            factor_coverage=coverage(key) if key != "nse_momentum_score" else None,
            note=DATA_NOTES.get(key),
        )
        del signals
        gc.collect()

    record("sharpe_12_6", signals_for(cache, composite("sharpe_12m", "sharpe_6m")), study="sortino")
    record(
        "sortino_for_sharpe",
        signals_for(cache, composite("sortino_12m", "sortino_6m")),
        study="sortino",
    )
    record(
        "base+rsi_penalty",
        signals_for(cache, base_spec, post=rsi_penalised),
        study="rsi_penalty",
        factor="rsi_1m",
    )
    for allowed in REGIME_FILTERS:
        record(
            f"base|regime_in={'+'.join(allowed)}",
            signals_for(cache, base_spec, keep=lambda f, a=allowed: f["regime"].isin(a)),
            study="regime_filter",
            factor="regime",
        )
    for entry in GRID_ENTRY:
        for retention in GRID_RETENTION:
            if retention < entry:
                continue
            config = ValidationConfig(entry_rank=entry, retention_rank=retention)
            record(f"grid_e{entry}_r{retention}", base_signals, config, study="grid")

    name = "ablation.csv" if args.limit is None else f"ablation-subset{args.limit}.csv"
    data.OUT.mkdir(exist_ok=True)
    table = pd.DataFrame(rows)
    front = ["model", "study", "factor", "preference", "weight_family", "factor_coverage", "note"]
    table = table[[c for c in front if c in table] + [c for c in table if c not in front]]
    table.to_csv(data.OUT / name, index=False)
    print(
        f"wrote {data.OUT / name}: {len(table)} models, generated {dt.date.today()} "
        f"[{time.time() - t0:.0f}s, max RSS {rss_gb():.2f} GB]",
        flush=True,
    )


if __name__ == "__main__":
    main()
