"""Dump the generated scan's answer key for `baskfy-desk`'s `lanes::weekly::scan_source` (wave D2,
lane weekly_plan; DECISIONS-RS R8.WP1).

    cd <wt>/kite-momentum-rebalancer && \\
    PYTHONPATH=<wt>/kite-momentum-rebalancer:<wt>/decile-blueprint/packages/core/src:\\
    <wt>/decile-blueprint/packages/execution/src \\
    <main>/kite-momentum-rebalancer/.venv/bin/python ../tools/parity/desk_generated_goldens.py

Writes ``rust/crates/baskfy-desk/fixtures/weekly/generated.json``: the two synthetic bar fixtures
the Python suites use (``_scan_fixture`` for ``test_generated_scan.py``, ``_desk_score_fixture``
for ``test_desk_score_parity.py`` / ``test_desk_config_parity.py``) and variants of them, each run
through the desk's real path — ``scan_source.generate`` with its database read replaced by the
fixture's bars (nothing else), the carried columns read the way ``_carried_columns`` reads them
(a CSV through ``pd.read_csv`` and ``pl.DataFrame(records)``), ``as_desk_frame``, ``audit`` and
``score`` — plus ``app.config``'s scoring attributes. Synthetic data only; no database, no network.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = REPO / "rust" / "crates" / "baskfy-desk" / "fixtures" / "weekly"

for key in list(os.environ):
    if key not in ("PATH", "HOME", "PYTHONPATH", "LANG", "TZ") and not key.startswith("LC_"):
        os.environ.pop(key)
os.environ["DRY_RUN"] = "true"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "decile-blueprint" / "packages" / "core" / "tests"))

import pandas as pd  # noqa: E402
import polars as pl  # noqa: E402

import _desk_score_fixture as desk_fixture  # noqa: E402
import _scan_fixture as scan_fixture  # noqa: E402
from app import config as C  # noqa: E402
from app import scan_source, scoring  # noqa: E402
from baskfy_core import momentum_scan as ms  # noqa: E402
from baskfy_core.desk_config import SCORING_ATTRIBUTES  # noqa: E402
from desk_weekly_goldens import SCORED_COLS, dumps  # noqa: E402
from desk_weekly_goldens import column_dump as _column_dump  # noqa: E402


def column_dump(s: pd.Series) -> dict:
    """desk_weekly_goldens' encoding, except a `None` cell of an object column is JSON null (a
    generated scan's never-computed factor is an all-`None` object column; `str(None)` would
    read as the string "None")."""
    out = _column_dump(s)
    if out["dtype"] == "object":
        out["values"] = [None if v is None or (isinstance(v, float) and v != v) else str(v) for v in s]
    return out


def frame_dump(df: pd.DataFrame) -> dict:
    return {"columns": list(df.columns), "data": {c: column_dump(df[c]) for c in df.columns}}


def bars_rows(bars: pl.DataFrame) -> list[list[object]]:
    return [[r["symbol"], r["instrument_id"], r["date"].isoformat(), *(repr(float(r[c])) for c in
             ("open", "high", "low", "close", "volume", "close_raw", "volume_raw"))]
            for r in bars.iter_rows(named=True)]


def carried_from_csv(text: str) -> pl.DataFrame:
    """`_carried_columns`, from a CSV text instead of the newest retained scan."""
    frame = pd.read_csv(io.StringIO(text), encoding="utf-8-sig")
    return pl.DataFrame(frame[["symbol", "series", *ms.CARRIED_COLUMNS]].to_dict(orient="records"))


def run(name: str, bars: pl.DataFrame, days: list[dt.date], as_of: dt.date, carried_csv: str) -> dict:
    case: dict = {"name": name, "bars": bars_rows(bars), "days": [d.isoformat() for d in days],
                  "as_of": as_of.isoformat(), "carried_csv": carried_csv}
    original = scan_source._fetch
    scan_source._fetch = lambda _as_of, _symbols: (bars, days)  # the only substitution
    try:
        generated = scan_source.generate(as_of, carried_from_csv(carried_csv))
    except Exception as exc:  # noqa: BLE001
        case["error"] = str(exc)
        return case
    finally:
        scan_source._fetch = original
    case["screen_run_id"] = generated.screen_run_id
    case["provenance"] = dumps(generated.provenance())
    case["height"] = generated.frame.height
    case["to_csv"] = generated.to_csv().decode("utf-8")
    try:
        u = scan_source.as_desk_frame(generated)
    except Exception as exc:  # noqa: BLE001
        case["desk_frame_error"] = f"{type(exc).__name__}: {exc}"
        return case
    case["desk_frame"] = frame_dump(u)
    try:
        case["audit"] = dumps(scoring.audit(u))
    except Exception as exc:  # noqa: BLE001
        case["audit_error"] = f"{type(exc).__name__}: {exc}"
    try:
        sc = scoring.score(u)
    except Exception as exc:  # noqa: BLE001
        case["score_error"] = f"{type(exc).__name__}: {exc}"
        return case
    case["reject"] = sc["reject"].tolist()
    case["symbols"] = sc["symbol"].tolist()
    case["scored"] = {c: column_dump(sc[c]) for c in SCORED_COLS if c in sc.columns}
    case["loaded_columns"] = list(scoring.load_scan(io.BytesIO(generated.to_csv())).columns)
    return case


def with_step(bars: pl.DataFrame, symbol: str, from_bar: int, factor: float) -> pl.DataFrame:
    """A missing split: every close of `symbol` from bar `from_bar` on, multiplied by `factor`."""
    rows = bars.to_dicts()
    seen = 0
    for r in rows:
        if r["symbol"] == symbol:
            if seen >= from_bar:
                r["close"] = round(r["close"] * factor, 2)
            seen += 1
    return pl.DataFrame(rows)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cases = []
    days = scan_fixture.trading_days()
    bars = scan_fixture.bars()
    carried = scan_fixture.carried().write_csv()
    cases.append(run("scan_fixture", bars, days, days[-1], carried))
    cases.append(run("scan_fixture_short_history", bars.filter(pl.col("date") <= days[60]),
                     days[:61], days[60], carried))
    cases.append(run("scan_fixture_no_bars", bars.head(0), days, days[-1], carried))
    cases.append(run("scan_fixture_not_a_trading_day", bars, days, days[-1] + dt.timedelta(days=1),
                     carried))
    cases.append(run("scan_fixture_a_missing_split", with_step(bars, "BRAVO", 300, 0.5), days,
                     days[-1], carried))
    # Across a hole in the history a step is not a split: drop 10 calendar days of DELTA's bars.
    holed = with_step(bars, "DELTA", 280, 2.0).filter(
        ~((pl.col("symbol") == "DELTA") & (pl.col("date") > days[270]) & (pl.col("date") < days[280])))
    cases.append(run("scan_fixture_a_step_across_a_hole", holed, days, days[-1], carried))
    partial = "symbol,series,marketcap,beta,circuits_three_months,circuits_one_year,is_nifty_fno\n" \
              "ALPHA,EQ,5000.0,0.8,0,0,0\nCHARLIE,,8000.0,,0,2,0\nZULU,EQ,1.0,1.0,0,0,1\n"
    cases.append(run("scan_fixture_partial_carried", bars, days, days[-1], partial))
    cases.append(run("scan_fixture_no_overlap", bars, days, days[-1],
                     "symbol,series,marketcap,beta,circuits_three_months,circuits_one_year,is_nifty_fno\n"
                     "ZULU,EQ,1.0,1.0,0,0,1\n"))
    d_days = desk_fixture.trading_days()
    cases.append(run("desk_score_fixture", desk_fixture.bars(), d_days, d_days[-1],
                     desk_fixture.carried().write_csv()))
    config = {}
    for name in SCORING_ATTRIBUTES:
        value = getattr(C, name)
        if isinstance(value, dict):
            config[name] = {"type": "dict", "items": [[k, repr(v)] for k, v in value.items()]}
        elif isinstance(value, (set, frozenset)):
            config[name] = {"type": "set", "items": sorted(str(v) for v in value)}
        else:
            config[name] = {"type": type(value).__name__, "repr": repr(value)}
    (OUT / "generated.json").write_text(json.dumps({"cases": cases, "config": config,
                                                    "scoring_attributes": list(SCORING_ATTRIBUTES)}))
    print("wrote generated.json:", [(c["name"], c.get("error") or c.get("desk_frame_error") or "ok")
                                    for c in cases])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
