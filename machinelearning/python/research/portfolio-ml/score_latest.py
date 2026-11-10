"""Train on every completed 12-month label and rank the last session in the export.
This is the nightly job in miniature: out/latest_ranking.csv is what the page would show."""

from __future__ import annotations

import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

from explore_multibagger import PARAMS, ROUNDS, TRAIN_STRIDE, grp
from portfolioml import fundamentals as F, panel, screener as SC
from portfolioml.features import ALL_FEATURES

geometry = sys.argv[1] if len(sys.argv) > 1 else "mb_u100_h250"
lf = pl.scan_parquet(f"data/dataset_{geometry}.parquet")
univ = lf.filter(pl.col("universe")).select(
    "instrument_id", "symbol", "date", "label", "close_raw", "turnover20", *ALL_FEATURES).collect()
last = univ["date"].max()
sessions = univ["date"].unique().sort().to_list()
lab = univ.filter(pl.col("label").is_not_null())
train = lab.filter(pl.col("date").is_in(pl.Series(sessions[::TRAIN_STRIDE]).implode())).sort("date")
x = lambda df: df.select(ALL_FEATURES).to_numpy().astype(np.float32)  # noqa: E731
m = lgb.train(PARAMS, lgb.Dataset(x(train), train["label"].to_numpy(), group=grp(train)), ROUNDS)
m.save_model(f"out/model_{geometry}_latest.txt")
today = univ.filter(pl.col("date") == last)
today = today.with_columns(pl.Series("score", m.predict(x(today)))).sort("score", descending=True)
today = today.with_columns(
    pl.int_range(1, pl.len() + 1).alias("rank"),
    ((pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).alias("investable"),
    (pl.col("turnover20") / 1e7).round(2).alias("turnover20_cr"),
)
cols = ["rank", "symbol", "score", "investable", "close_raw", "turnover20_cr", "r252", "dist_ath", "vol60", "n_hist"]
today.select(cols).write_csv(f"out/latest_ranking_{geometry}.csv")
print(f"trained on {train.shape[0]:,} rows through {lab['date'].max()}; ranked {today.shape[0]} names as of {last}")
pl.Config.set_tbl_rows(60); pl.Config.set_float_precision(3); pl.Config.set_tbl_width_chars(160)
print(today.filter(pl.col("investable")).select(cols).head(30))


def ttm_line(rows: list[dict], root: Path) -> dict | None:
    """Our latest TTM beside Screener's over the same four quarters, on the basis our features would use."""
    known = {(F._index(r["period_end"]), r["period_type"]): r for r in sorted(rows, key=lambda r: r["filed_at"])}
    q = F._chosen(known)
    i = max(q)
    basis, ends = q[i]["period_type"], [q[k]["period_end"].isoformat() for k in range(i - 3, i + 1) if k in q]
    if len(ends) < 4:
        return None
    theirs = SC.read_quarters(root, basis, rows[0]["symbol"])
    rev, pat = F._ttm(q, i, "revenue"), F._ttm(q, i, "pat")
    ours = {"revenue": None if rev is None else rev / SC.CRORE, "net_profit": None if pat is None else pat / SC.CRORE}
    line: dict = {"symbol": rows[0]["symbol"], "basis": basis[:4], "to": ends[-1]}
    for metric, value in ours.items():
        other = SC.ttm(theirs, ends, metric)
        line |= {f"{metric}_ours": value, f"{metric}_screener": other, f"{metric}_flag": "OVER 2%" if SC.differs(value, other) else ""}
    return line


def screener_check(top: pl.DataFrame, data: Path, root: Path) -> None:
    """PM1.13: operator aid, printed and never fed to the model. Reads the local export, makes no call."""
    if not root.exists() or not (data / "fundamental_pit.csv.gz").exists():
        print("screener_check skipped: no local export or no fundamental_pit")
        return
    filings = panel.load_fundamentals(data).join(top.select("instrument_id", "symbol"), on="instrument_id")
    lines = [line for (_,), g in filings.group_by(["instrument_id"]) if (line := ttm_line(g.to_dicts(), root))]
    if not lines:
        print("screener_check: none of the top names has four quarters on file")
        return
    frame = pl.DataFrame(lines).join(top.select("symbol", "rank"), on="symbol").sort("rank")
    flagged = frame.filter((pl.col("revenue_flag") != "") | (pl.col("net_profit_flag") != "")).height
    print(f"screener_check, top {top.height}: {frame.height} compared, {flagged} over 2 % (crore, operator aid only)")
    print(frame.select("rank", pl.exclude("rank")))


screener_check(today.filter(pl.col("investable")).head(50), Path("data"), Path(__file__).resolve().parents[4] / "data" / "screener")
