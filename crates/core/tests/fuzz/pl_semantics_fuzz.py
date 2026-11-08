"""Differential cases for `crate::pl` and the twt NaN paths (DECISIONS-RS R3.1).

Writes one JSON file of cases computed by the **live** polars and `baskfy_core.twt`, with every
float encoded as its Python `repr` string (`"nan"`, `"-0.0"`, `"1e-05"`) or `null`, because the
golden encoder cannot carry a NaN. Read by `tests/pl_semantics.rs::differential_fuzz`:

    cd decile-blueprint
    PYTHONPATH=..:packages/core/src .venv/bin/python \
        ../rust/crates/baskfy-core/tests/fuzz/pl_semantics_fuzz.py /tmp/pl_fuzz.json
    BASKFY_PL_FUZZ=/tmp/pl_fuzz.json cargo test -p baskfy-core --test pl_semantics -- --ignored

Pure: no network, no database, no file but the one it writes. Seeded, so a rerun is identical.
"""

import json
import math
import random
import sys

import polars as pl
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG as C
from baskfy_core.twt.signals import liquidity_floor, month_low_back, tight_state

NAN = float("nan")
POOL = [NAN, 1.0, 2.0, 0.0, -0.0, None, -1.0, float("inf")]


def enc(x):
    if x is None:
        return None
    if isinstance(x, bool):
        return x
    return repr(float(x)) if not math.isnan(x) else "nan"


def col(values):
    return [enc(v) for v in values]


def main(out_path: str) -> None:
    rng = random.Random(20261029)
    cases = {"horizontal": [], "aggregate": [], "compare": [], "tight_state": [], "month_low": [],
             "liquidity": []}

    for _ in range(3000):
        k, h = rng.randint(1, 4), rng.randint(1, 4)
        cols = [[rng.choice(POOL) for _ in range(h)] for _ in range(k)]
        names = [f"c{i}" for i in range(k)]
        df = pl.DataFrame(dict(zip(names, cols)), schema={n: pl.Float64 for n in names})
        out = df.select(mx=pl.max_horizontal(*names), mn=pl.min_horizontal(*names))
        cases["horizontal"].append({"columns": [col(c) for c in cols],
                                    "max": col(out["mx"].to_list()),
                                    "min": col(out["mn"].to_list())})

    for _ in range(2000):
        n = rng.randint(1, 8)
        values = [rng.choice(POOL) for _ in range(n)]
        s = pl.DataFrame({"k": [1] * n, "v": values}, schema={"k": pl.Int64, "v": pl.Float64})
        g = s.group_by("k").agg(mn=pl.col("v").min(), mx=pl.col("v").max()).row(0)
        cases["aggregate"].append({"values": col(values), "min": enc(g[1]), "max": enc(g[2])})

    pairs = [(a, b) for a in POOL for b in POOL]
    df = pl.DataFrame({"a": [p[0] for p in pairs], "b": [p[1] for p in pairs]},
                      schema={"a": pl.Float64, "b": pl.Float64})
    cmp = df.select(gt=pl.col("a") > pl.col("b"), ge=pl.col("a") >= pl.col("b"),
                    lt=pl.col("a") < pl.col("b"), le=pl.col("a") <= pl.col("b"),
                    eq=pl.col("a") == pl.col("b"))
    for (a, b), row in zip(pairs, cmp.rows()):
        cases["compare"].append({"a": enc(a), "b": enc(b), "gt": row[0], "ge": row[1],
                                 "lt": row[2], "le": row[3], "eq": row[4]})

    price = [NAN, None, 100.0, 101.0, 102.0, 0.0, -0.0, 1.0, 200.0]
    for _ in range(400):
        h = rng.randint(1, 6)
        frame = {
            "weekly_close_0": [rng.choice(price) for _ in range(h)],
            "weekly_close_1": [rng.choice(price) for _ in range(h)],
            "weekly_close_2": [rng.choice(price) for _ in range(h)],
            "close": [rng.choice([NAN, None, 200.0, 120.0]) for _ in range(h)],
            "month_low_back": [rng.choice([NAN, None, 100.0]) for _ in range(h)],
            "close_raw": [rng.choice([NAN, None, 50.0, 10.0]) for _ in range(h)],
            "vol_sma": [rng.choice([NAN, None, 2e4, 10.0]) for _ in range(h)],
            "is_etf": [rng.choice([False, True, None]) for _ in range(h)],
        }
        schema = {k: pl.Float64 for k in frame if k != "is_etf"} | {"is_etf": pl.Boolean}
        out = tight_state(pl.DataFrame(frame, schema=schema), C)
        cases["tight_state"].append({
            "frame": {k: col(v) if k != "is_etf" else v for k, v in frame.items()},
            "tight_range_pct": col(out["tight_range_pct"].to_list()),
            "tight": out["tight"].to_list(),
            "above_month_low": out["above_month_low"].to_list(),
            "tight_state": out["tight_state"].to_list(),
        })

    for _ in range(300):
        h = rng.randint(1, 10)
        frame = {"instrument_id": [rng.randint(1, 2) for _ in range(h)],
                 "month_key": [rng.choice([10, 11, 13, 14]) for _ in range(h)],
                 "low": [rng.choice([NAN, None, 5.0, 4.0, 0.0, -0.0]) for _ in range(h)]}
        df = pl.DataFrame(frame | {"row": list(range(h))},
                          schema={"instrument_id": pl.Int64, "month_key": pl.Int64,
                                  "low": pl.Float64, "row": pl.Int64})
        # The join's row order is polars' own; the Rust port keeps the input order.
        out = month_low_back(df, C).sort("row")
        cases["month_low"].append({"frame": {"instrument_id": frame["instrument_id"],
                                             "month_key": frame["month_key"],
                                             "low": col(frame["low"])},
                                   "month_low_back": col(out["month_low_back"].to_list())})

    values = [NAN, None, 2e7, 1e8, 1e6, float("inf"), -0.0]
    df = pl.DataFrame({"turnover_avg_20": values}, schema={"turnover_avg_20": pl.Float64})
    cases["liquidity"].append({"turnover_avg_20": col(values),
                               "passes": df.select(liquidity_floor(C))["turnover_avg_20"].to_list()})

    with open(out_path, "w") as fh:
        json.dump(cases, fh)
    print(out_path, {k: len(v) for k, v in cases.items()})


if __name__ == "__main__":
    main(sys.argv[1])
