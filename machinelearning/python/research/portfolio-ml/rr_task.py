"""PM11 swing picks (Maulik, 11 Nov 2026: "keep this and build the model"): PM10f's ew_10_20_200 row made nightly.

XGBoost on every third universe session; label: entered at the next open, out on a −7 % stop that trails 12 % under
the highest high, or after 60 sessions, the trade made at least +14 %. Each night: score the universe; the switch is
on when NIFTY500 Equal Weight closes above its 200-session average and its 10-session average is above its 20; while
on, the top 5 investable names not already held become paper picks, followed to their exit. Writes only rr_* tables.
Never an order. Called from mb_task.py (score, train, rr-train, rr-seed)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import polars as pl

from portfolioml import pipeline as P
from portfolioml.features import ALL_FEATURES, UniverseConfig
from portfolioml.labels import TrailConfig, trailing

HERE = Path(__file__).resolve().parent
UCFG = UniverseConfig()
CFG = TrailConfig(stop=0.07, trail=0.12, horizon=60)
LABEL = "trail_7_12_h60_gain_14"
TOP_N, STRIDE = 5, 3
EW_SLUG = "nifty500-equal-weight"
INVESTABLE_CLOSE, INVESTABLE_TURNOVER = 50.0, 5e7
# explore_xgb_swing.XGB, the research's fixed parameters
XGB = dict(n_estimators=400, max_depth=6, learning_rate=0.05, subsample=0.8, colsample_bytree=0.6, min_child_weight=50,
           reg_lambda=10.0, tree_method="hist", n_jobs=8, random_state=7, eval_metric="auc")


def has_tables(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.rr_pick') IS NOT NULL")
        return bool(cur.fetchone()[0])


def label_chunk(ch: pl.DataFrame) -> pl.DataFrame:
    return trailing(ch, CFG).select("instrument_id", "date", (pl.col("exit_ret") >= 2 * CFG.stop).cast(pl.Int8).alias("label"))


def clean(x: np.ndarray) -> np.ndarray:
    return np.nan_to_num(x.astype(np.float32), nan=np.nan, posinf=np.nan, neginf=np.nan)


# ---- the switch ------------------------------------------------------------------------------------------------------

def gate_frame(conn, through: dt.date) -> pl.DataFrame:
    with conn.cursor() as cur:
        cur.execute("SELECT s.date, s.level::float8 FROM index_snapshot_daily s JOIN index_def d ON d.id = s.index_id "
                    "WHERE d.slug = %s AND s.level IS NOT NULL AND s.date <= %s ORDER BY 1", (EW_SLUG, through))
        rows = cur.fetchall()
    ew = pl.DataFrame(rows, schema={"date": pl.Date, "ew_close": pl.Float64}, orient="row")
    ma = lambda n: pl.col("ew_close").rolling_mean(n, min_samples=n)  # noqa: E731
    g = ew.with_columns(ma(10).alias("ma10"), ma(20).alias("ma20"), ma(200).alias("ma200"))
    g = g.with_columns((pl.col("ew_close") > pl.col("ma200")).fill_null(False).alias("above_200"),
                       (pl.col("ma10") > pl.col("ma20")).fill_null(False).alias("up_10_20"))
    return g.with_columns((pl.col("above_200") & pl.col("up_10_20")).alias("is_on"))


def write_gate(conn, g: pl.DataFrame) -> None:
    rows = g.with_columns([pl.col(c).round(4) for c in ("ew_close", "ma10", "ma20", "ma200")])
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM rr_gate_daily")
        with cur.copy("COPY rr_gate_daily (date, ew_close, ma10, ma20, ma200, above_200, up_10_20, is_on) FROM STDIN") as cp:
            for r in rows.select("date", "ew_close", "ma10", "ma20", "ma200", "above_200", "up_10_20", "is_on").iter_rows():
                cp.write_row(r)


# ---- scoring and picks -----------------------------------------------------------------------------------------------

def published(conn) -> tuple[int, str, list[str]] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id, path, features FROM rr_model WHERE published_at IS NOT NULL ORDER BY published_at DESC, id DESC LIMIT 1")
        row = cur.fetchone()
    return None if row is None else (row[0], row[1], row[2])


def classifier(path: str, features: list[str]):
    import xgboost as xgb
    if features != ALL_FEATURES:
        raise SystemExit("REFUSED: the rr model's feature list differs from portfolioml.features.ALL_FEATURES; retrain")
    return xgb.Booster(model_file=path)


def follow(b: pl.DataFrame) -> dict:
    """One pick's paper trade from the bars after its signal session (labels.trailing's rule, step by step)."""
    if b.is_empty():
        return {"status": "waiting"}
    o, h, lo, c = (b[k].to_numpy() for k in ("open", "high", "low", "close"))
    d = b["date"].to_list()
    entry = float(o[0])
    floor, level, peak = entry * (1 - CFG.stop), entry * (1 - CFG.stop), None
    base = {"entry_date": d[0], "entry_px": entry}
    for i in range(min(len(o), CFG.horizon)):
        if i > 0:
            peak = float(h[i - 1]) if peak is None else max(peak, float(h[i - 1]))
            level = max(floor, peak * (1 - CFG.trail))
        if lo[i] <= level:
            px = min(level, float(o[i]))
            return {**base, "status": "closed", "exit_date": d[i], "exit_px": px, "exit_reason": "stop" if level <= floor else "trail",
                    "sessions_held": i + 1, "last_date": d[i], "last_px": px, "peak_px": max(peak or entry, float(h[i])), "stop_px": level}
        if i + 1 == CFG.horizon:
            return {**base, "status": "closed", "exit_date": d[i], "exit_px": float(c[i]), "exit_reason": "time", "sessions_held": i + 1,
                    "last_date": d[i], "last_px": float(c[i]), "peak_px": max(peak or entry, float(h[i])), "stop_px": level}
    peak = float(h[: len(o)].max())
    return {**base, "status": "open", "sessions_held": len(o), "last_date": d[-1], "last_px": float(c[-1]), "peak_px": peak,
            "stop_px": max(floor, peak * (1 - CFG.trail))}


def refresh(conn, bars: pl.DataFrame, through: dt.date) -> int:
    """Re-follow every pick not closed before, from the corrected bars; prices shown in today's raw terms."""
    with conn.cursor() as cur:
        cur.execute("SELECT signal_date, instrument_id FROM rr_pick WHERE status <> 'closed'")
        todo = cur.fetchall()
    if not todo:
        return 0
    ids = list({i for _, i in todo})
    sub = bars.filter(pl.col("instrument_id").is_in(ids) & (pl.col("date") <= through)).sort("instrument_id", "date")
    factor = {i: f for i, f in sub.group_by("instrument_id").agg((pl.col("close_raw").last() / pl.col("close").last()).alias("f")).iter_rows()}
    px_cols = ("entry_px", "stop_px", "peak_px", "last_px", "exit_px")
    with conn.transaction(), conn.cursor() as cur:
        for sd, iid in todo:
            r = follow(sub.filter((pl.col("instrument_id") == iid) & (pl.col("date") > sd)))
            f = factor.get(iid) or 1.0
            for k in px_cols:
                if r.get(k) is not None:
                    r[k] = round(r[k] * f, 2)
            ret = None if r.get("entry_px") in (None, 0) else round(r["last_px"] / r["entry_px"] - 1, 4)
            cur.execute("UPDATE rr_pick SET status=%s, entry_date=%s, entry_px=%s, stop_px=%s, peak_px=%s, last_date=%s, last_px=%s, "
                        "ret=%s, sessions_held=%s, exit_date=%s, exit_px=%s, exit_reason=%s WHERE signal_date=%s AND instrument_id=%s",
                        (r["status"], r.get("entry_date"), r.get("entry_px"), r.get("stop_px"), r.get("peak_px"), r.get("last_date"),
                         r.get("last_px"), ret, r.get("sessions_held"), r.get("exit_date"), r.get("exit_px"), r.get("exit_reason"), sd, iid))
    return len(todo)


SCOPES = ("quarantine", "young", "sme", "cheap", "thin")  # first reason that applies, in this order


def scoped(day: pl.DataFrame, ins: pl.DataFrame) -> pl.DataFrame:
    """PM11.1: every equity of the session (ETFs and non-equity left out), with why it is outside the training universe."""
    kind = ins.select("instrument_id", "instrument_type", "series")
    d = day.join(kind, on="instrument_id", how="left")
    main = pl.col("series").is_in(["EQ", "BE", "BZ"])
    etf = (pl.col("instrument_type") == "EQ") & main & ~pl.col("eligible")
    d = d.filter((pl.col("instrument_type") == "EQ") & ~etf)
    u = UCFG
    return d.with_columns(
        pl.when(pl.col("universe")).then(pl.lit("universe"))
        .when(pl.col("near_sus_gap")).then(pl.lit("quarantine"))
        .when(pl.col("n_hist") < u.min_history).then(pl.lit("young"))
        .when(~main).then(pl.lit("sme"))
        .when(pl.col("close_raw") < u.min_close_raw).then(pl.lit("cheap"))
        .otherwise(pl.lit("thin")).alias("scope"))


def score_day(conn, bars: pl.DataFrame, ins: pl.DataFrame, idx: pl.DataFrame, date: dt.date) -> str:
    """Score one session, write the switch, follow open picks, then add tonight's picks (idempotent for the date)."""
    model = published(conn)
    if model is None:
        return "rr: no published model"
    gate = gate_frame(conn, date)
    write_gate(conn, gate)
    cut = bars.filter(pl.col("date") <= date)
    feat = P.tail_features(cut, ins, idx.filter(pl.col("date") <= date))
    day = scoped(feat.filter(pl.col("date") == date), ins)
    p = classifier(model[1], model[2]).inplace_predict(clean(day.select(ALL_FEATURES).to_numpy()))
    day = day.with_columns(pl.Series("score", p, dtype=pl.Float64).round(6)).sort(["score", "instrument_id"], descending=[True, False])
    day = day.with_columns(pl.int_range(1, pl.len() + 1).alias("rank"), pl.lit(day.height).alias("universe_n"),
                           ((pl.col("close_raw") >= INVESTABLE_CLOSE) & (pl.col("turnover20") >= INVESTABLE_TURNOVER)).alias("investable"),
                           pl.col("close_raw").round(2))
    rows = day.select(pl.lit(date).alias("date"), "instrument_id", pl.lit(model[0]).alias("model_id"), "score", "rank", "universe_n",
                      "investable", "close_raw", "scope")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM rr_score_daily WHERE date = %s", (date,))
        with cur.copy(f"COPY rr_score_daily ({', '.join(rows.columns)}) FROM STDIN") as cp:
            for r in rows.iter_rows():
                cp.write_row(r)
    refresh(conn, bars, date)
    g = gate.filter(pl.col("date") == date)
    on = (not g.is_empty()) and bool(g["is_on"][0])
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM rr_pick WHERE signal_date = %s", (date,))
        picks = []
        if on:
            cur.execute("SELECT instrument_id FROM rr_pick WHERE signal_date < %s AND (status <> 'closed' OR exit_date >= %s)", (date, date))
            held = {r[0] for r in cur.fetchall()}
            for iid, score in rows.filter(pl.col("investable") & (pl.col("scope") == "universe")).select("instrument_id", "score").iter_rows():
                if len(picks) == TOP_N:
                    break
                if iid not in held:
                    picks.append((date, iid, model[0], len(picks) + 1, score, "waiting"))
            cur.executemany("INSERT INTO rr_pick (signal_date, instrument_id, model_id, pick_rank, score, status) VALUES (%s,%s,%s,%s,%s,%s)", picks)
    n_in = rows.filter(pl.col("scope") == "universe").height
    state = "no switch row" if g.is_empty() else ("ON" if on else "off")
    return f"rr_score_daily {date}: {rows.height} names ({n_in} in the universe), model {model[0]}, switch {state}, picks {len(picks)}"


# ---- training and the backtest record --------------------------------------------------------------------------------

def seed_backtest(conn, path: Path = HERE / "rr_backtest.json") -> int:
    rows = json.loads(path.read_text())["rows"]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM rr_backtest")
        cur.executemany("INSERT INTO rr_backtest (period, trades, net, win_rate, profit_factor, worst, on_share) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        [(r["period"], r["trades"], r["net"], r["win_rate"], r["profit_factor"], r["worst"], r["on_share"]) for r in rows])
    return len(rows)


def train(conn, raw, ca_dir: Path, schemes: Path, tmp: Path, model_dir: Path, ack: str | None) -> int:
    import xgboost as xgb

    import mb_task as M
    rows, _ = M.training_frame(raw, ca_dir, schemes, tmp, label_chunk=label_chunk, stride_n=STRIDE)
    x, y = clean(rows.select(ALL_FEATURES).to_numpy()), rows["label"].to_numpy()
    m = xgb.XGBClassifier(**XGB).fit(x, y)
    trained_on = rows["date"].max()
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("INSERT INTO rr_model (trained_on, label, features, params, path, rows, positives) VALUES (%s,%s,%s,%s,'',%s,%s) RETURNING id",
                    (trained_on, LABEL, json.dumps(ALL_FEATURES), json.dumps({**XGB, "stride": STRIDE, "stop": CFG.stop, "trail": CFG.trail,
                     "horizon": CFG.horizon, "top_n": TOP_N, "switch": "ew_10_20_200"}), rows.height, int(y.sum())))
        model_id = cur.fetchone()[0]
        path = model_dir / f"rr_model_{model_id}_{trained_on}.json"
        m.get_booster().save_model(str(path))  # the sklearn wrapper's save fails under scikit-learn 1.9
        cur.execute("UPDATE rr_model SET path = %s, published_at = now(), published_by = %s WHERE id = %s",
                    (str(path), ack or "auto", model_id))
    seed_backtest(conn)
    print(f"rr_model {model_id} published: {rows.height:,} rows, {int(y.sum()):,} positives, labels through {trained_on}")
    return 0
