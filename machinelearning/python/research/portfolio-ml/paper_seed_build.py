"""PM8 seed: the walk-forward (2014→) book of record and 63-day trigger lines as paper history, labelled backtest.

usage: python paper_seed_build.py      (reads data/dataset_mb_u100_h250_tb.parquet and out/from2014's walk-forward
       scores; writes out/paper/seed.json and prints the parity checks against tranches.simulate and the PM0.16 trigger)"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import polars as pl

from portfolioml import paper as PP
from portfolioml import timing as T
from portfolioml import tranches

DATA, OUT = Path("data"), Path("out/paper")
SOURCE = DATA / "dataset_mb_u100_h250_tb.parquet"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    lf = pl.scan_parquet(SOURCE)
    ds = lf.filter(pl.col("universe") & (pl.col("close_raw") >= 50) & (pl.col("turnover20") >= 5e7)).collect()
    scores = pl.concat([pl.read_parquet(p) for p in sorted(glob.glob("out/from2014/mb_scores_mb_u100_h250_tb_touch_*.parquet"))])
    bars = lf.select("instrument_id", "date", "open", "high", "close", "terminal", "entry_px").collect().sort("instrument_id", "date")
    sessions = bars["date"].unique().sort().to_list()
    sidx = {d: i for i, d in enumerate(sessions)}

    # the research book of record, for parity
    ref, _ = tranches.simulate(ds, scores, "score", tranches.TrancheConfig(top_n=20, skip_held=True, terminal="b"),
                               marks=lf.select("instrument_id", "date", "close", "terminal").collect())
    cand = ds.select("instrument_id", "date", "fwd_ret_h").join(scores.select("instrument_id", "date", "score"), on=["instrument_id", "date"])
    starts = [d for d in PP.month_starts(sessions) if d in set(scores["date"].to_list())]
    flags = bars.select("instrument_id", "date", "close").with_columns(
        (pl.col("close") >= pl.col("close").rolling_max(PP.HIGH, min_samples=PP.HIGH).over("instrument_id")).fill_null(False).alias("hi"))
    fired = {(int(i), sidx[d]) for i, d in flags.filter(pl.col("hi")).select("instrument_id", "date").iter_rows()}

    out, recent, watch = [], [], {}
    for d in starts:
        day = cand.filter(pl.col("date") == d).sort("score", descending=True)
        if day.is_empty():
            continue
        ranked = day.with_columns(pl.int_range(1, pl.len() + 1).alias("rk"))
        live = ranked.filter(pl.col("fwd_ret_h").is_not_null())  # what simulate could pick: a completed outcome
        book = PP.book_picks(live["instrument_id"].to_list(), recent)
        recent.append(book)
        rk = dict(zip(ranked["instrument_id"].to_list(), ranked["rk"].to_list()))
        w = [(int(i), int(r)) for i, r in ranked.filter(pl.col("rk") <= PP.WATCH).select("instrument_id", "rk").iter_rows()]
        watch[sidx[d]] = w
        out.append({"signal_date": d.isoformat(), "watch": w, "book": [[int(i), int(rk[i])] for i in book]})
    done = {(int(i), sidx[d]) for i, d in lf.filter(pl.col("fwd_ret_h").is_not_null()).select("instrument_id", "date").collect().iter_rows()}
    n = len(sessions)
    hits = PP.triggers(watch, lambda i, t: (i, t) in fired, n - 1, [],
                       admit=lambda i, e: (i, e) in done and e + 1 + PP.HORIZON <= n)
    by_sig = {}
    for f in hits:
        by_sig.setdefault(f.sig, []).append([f.iid, f.rank, sessions[f.e].isoformat()])
    for t in out:
        t["hi63"] = by_sig.get(sidx[pl.Series([t["signal_date"]]).str.to_date()[0]], [])
    (OUT / "seed.json").write_text(json.dumps({"from": "walk-forward 2014→, dataset_mb_u100_h250_tb", "tranches": out}))

    mine = {(d, i) for t in out for d, i in [(t["signal_date"], b[0]) for b in t["book"]]}
    theirs = {(d.isoformat(), int(i)) for i, d in ref.select("instrument_id", "date").iter_rows()}
    print(f"book tranches {len(out)}, lines {len(mine)}; equal to tranches.simulate's picks: {mine == theirs} (theirs {len(theirs)})")

    # marks of the 63-day twentieth line, against the research pool book (PM0.16: 16.6 % / −26.3 %, full period)
    ids = {int(v): k for k, v in enumerate(bars["instrument_id"].unique().sort().to_list())}
    closes = np.full((len(sessions), len(ids)), np.nan)
    closes[[sidx[d] for d in bars["date"].to_list()], [ids[int(i)] for i in bars["instrument_id"].to_list()]] = bars["close"].to_numpy()
    last = np.where(~np.isnan(closes), np.arange(len(sessions))[:, None], -1).max(axis=0)
    writes = {int(i): s in ("delisted", "suspended") for i, s in bars.group_by("instrument_id").agg(pl.col("terminal").first()).iter_rows()}
    entry = {(int(i), sidx[d]): px for i, d, px in bars.select("instrument_id", "date", "entry_px").iter_rows()}
    first = sidx[pl.Series([out[0]["signal_date"]]).str.to_date()[0]] + 1
    lines = []
    for f in hits:
        px = entry.get((f.iid, f.e))
        if px and f.e + 1 + PP.HORIZON <= len(sessions):
            p = PP.line_path(closes[:, ids[f.iid]], f.e + 1, px, last[ids[f.iid]], writes[f.iid])
            lines.append((f.e + 1, p, PP.SLOTS["hi63_20"]))
    eq, _ = PP.pool_marks(lines, len(sessions), first)
    ref_eq, _, _ = T.pool_book_detail([T.Line(s, p, w) for s, p, w in lines], len(sessions), first)
    days = sessions[first:]
    s = T.period_stats(eq, days)
    print(f"hi63 lines {len(hits)}; twentieth book {s['cagr']:.1%} / {s['max_dd']:.1%}; max |pool_marks − timing.pool_book_detail| = {np.abs(eq - ref_eq).max():.2e}")


if __name__ == "__main__":
    main()
