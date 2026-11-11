"""MB sleeve tasks (PM6): reads Postgres and the NSE archive, writes only mb_* tables. Never an order.

usage: python mb_task.py score --date YYYY-MM-DD      (the nightly, after publish; then the PM8 paper book)
       python mb_task.py paper-seed out/paper/seed.json   (once: the backtest history, labelled backtest)
       python mb_task.py train [--ack WHO]            (quarterly; publishes itself unless Spearman < 0.7)
       python mb_task.py publish MODEL_ID --ack WHO   (operator ack for a model the train step held back)
Exit codes: 0 done, 3 nothing to do (no model, no bars for the date), 4 model held for an ack, other = error.
Env: BASKFY_DATABASE_URL; BASKFY_MB_DIR (models, default /var/lib/baskfy/state/mb);
     BASKFY_ARCHIVE (default /var/lib/baskfy/archive); BASKFY_MB_SCHEMES (nse_schemes.csv)."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

from audit_moves import correct_bars
from explore_multibagger import PARAMS, ROUNDS, TRAIN_STRIDE, grp
from portfolioml import fundamentals as FU
from portfolioml import panel
from portfolioml import pipeline as P
from portfolioml.features import ALL_FEATURES
from portfolioml.labels import MultibaggerConfig, multibagger

HERE = Path(__file__).resolve().parent
LABEL = "touch_2x_250"
TERMINAL = "b"  # PM0.8: dead names held DEATH_SESSIONS then −100 %
MIN_SPEARMAN = 0.7
HI_WINDOW, HI_RECENT = 63, 21
INVESTABLE_CLOSE, INVESTABLE_TURNOVER = 50.0, 5e7
DISPLAY = ["close_raw", "turnover20", "r252", "dist_ath", "sessions_listed", "ttm_rev_yoy", "ttm_pat_yoy"]
OHLCV_SCHEMA = {"upper_circuit": pl.Float64, "lower_circuit": pl.Float64}

Q_OHLCV = ("SELECT instrument_id, date, open, high, low, close, volume, close_raw, volume_raw, adj_factor, upper_circuit, lower_circuit "
           "FROM ohlcv_daily WHERE date <= {d} ORDER BY instrument_id, date")
Q_INSTRUMENT = "SELECT id, symbol, name, isin, instrument_type, series, listed_on, delisted_on, is_active FROM instrument"
Q_MEMBERS = "SELECT index_id, date, instrument_id FROM index_member_daily WHERE index_id IN (SELECT id FROM index_def WHERE slug = 'etf')"
Q_DEFS = "SELECT id, slug, name, is_universe FROM index_def"
Q_FILINGS = ("SELECT instrument_id, period_end, filed_on, period_type, filed_at, available_on, is_revision, revenue, ebitda, "
             "depreciation, pat, total_debt, total_equity, cfo FROM fundamental_pit WHERE available_on <= {d}")
FILING_NUMERIC = ["revenue", "ebitda", "depreciation", "pat", "total_debt", "total_equity", "cfo"]
Q_TERMINAL = "SELECT instrument_id, status, reason, last_bar, source FROM instrument_terminal"
Q_INDEX = ("SELECT m.kite_tradingsymbol AS index_name, b.date, b.open, b.high, b.low, b.close FROM index_bar_daily b "
           "JOIN index_master m ON m.id = b.index_id WHERE m.kite_tradingsymbol IN ('NIFTY 50','NIFTY MIDCAP 150','NIFTY SMLCAP 250') "
           "AND b.date <= {d} UNION ALL SELECT 'NIFTY 500 MOMENTUM 50', s.date, s.level, s.level, s.level, s.level FROM index_snapshot_daily s "
           "JOIN index_def d ON d.id = s.index_id WHERE d.slug = 'nifty500-momentum-50' AND s.level IS NOT NULL AND s.date <= {d} ORDER BY 1,2")


@dataclass
class Raw:
    """The export's tables as read, before any research step."""
    ohlcv: pl.DataFrame
    instrument: pl.DataFrame
    members: pl.DataFrame
    defs: pl.DataFrame
    index: pl.DataFrame
    terminal: pl.DataFrame


def _csv(text: bytes | Path, **kw) -> pl.DataFrame:
    return pl.read_csv(text if isinstance(text, Path) else io.BytesIO(text), **kw)


def _ins(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(pl.col("listed_on").str.to_date(strict=False), pl.col("delisted_on").str.to_date(strict=False))


def raw_from_export(data: Path, through: dt.date | None = None) -> Raw:
    """The research export read the way the task reads Postgres (parity tests)."""
    o = _csv(data / "ohlcv_daily.csv.gz", schema_overrides=OHLCV_SCHEMA).with_columns(pl.col("date").str.to_date())
    isr = _csv(data / "index_series.csv").with_columns(pl.col("date").str.to_date())
    if through:
        o, isr = o.filter(pl.col("date") <= through), isr.filter(pl.col("date") <= through)
    return Raw(o, _ins(_csv(data / "instrument.csv.gz", schema_overrides={"isin": pl.String})), _csv(data / "index_member_daily.csv.gz"),
               _csv(data / "index_def.csv.gz"), isr, _csv(data / "instrument_terminal.csv.gz"))


def _copy(conn, sql: str, spill: Path | None = None) -> bytes | Path:
    with conn.cursor() as cur, cur.copy(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER)") as cp:
        if spill is None:
            return b"".join(bytes(b) for b in cp)
        with open(spill, "wb") as f:
            for b in cp:
                f.write(b)
        return spill


def raw_from_pg(conn, through: dt.date, tmp: Path) -> Raw:
    d = f"'{through.isoformat()}'::date"  # a date object's isoformat, never user text
    o = _csv(_copy(conn, Q_OHLCV.format(d=d), tmp / "ohlcv.csv"), schema_overrides=OHLCV_SCHEMA).with_columns(pl.col("date").str.to_date())
    isr = _csv(_copy(conn, Q_INDEX.format(d=d))).with_columns(pl.col("date").str.to_date())
    return Raw(o, _ins(_csv(_copy(conn, Q_INSTRUMENT), schema_overrides={"isin": pl.String})), _csv(_copy(conn, Q_MEMBERS)),
               _csv(_copy(conn, Q_DEFS)), isr, _csv(_copy(conn, Q_TERMINAL)))


def prepared(raw: Raw, ca_dir: Path, schemes: Path) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """bars (PM0.12-corrected, thin sessions dropped), instruments with `eligible`, wide index closes."""
    status = raw.terminal.select("instrument_id", "status")
    fixed, _ = correct_bars(raw.ohlcv, raw.instrument.select("id", "symbol", "isin"), status, str(ca_dir), str(schemes))
    return panel.bars_frame(fixed), panel.instruments_frame(raw.instrument, raw.members, raw.defs), panel.index_frame(raw.index)


def hi63(bars: pl.DataFrame) -> pl.DataFrame:
    """PM0.16: the last session within the last 21 on which the name closed at a 63-session high."""
    sessions = bars["date"].unique().sort()
    recent = sessions[-HI_RECENT:]
    flag = pl.col("close") >= pl.col("close").rolling_max(HI_WINDOW, min_samples=HI_WINDOW).over("instrument_id")
    tail = bars.filter(pl.col("date") >= sessions[-(HI_WINDOW + HI_RECENT)]).sort("instrument_id", "date")
    hits = tail.with_columns(flag.fill_null(False).alias("hi")).filter(pl.col("hi") & pl.col("date").is_in(recent.implode()))
    return hits.group_by("instrument_id").agg(pl.col("date").max().alias("hi63_trigger_date"))


def filings_from_pg(conn, through: dt.date) -> pl.DataFrame | None:
    """fundamental_pit as panel.load_fundamentals reads it; None where the table is absent."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.fundamental_pit')")
        if cur.fetchone()[0] is None:
            return None
    f = _csv(_copy(conn, Q_FILINGS.format(d=f"'{through.isoformat()}'::date")), schema_overrides=dict.fromkeys(FILING_NUMERIC, pl.Float64))
    return f.with_columns(pl.col("period_end").str.to_date(), pl.col("available_on").str.to_date(), pl.col("filed_at").str.to_datetime())


def display(day: pl.DataFrame, filings: pl.DataFrame | None) -> pl.DataFrame:
    """PM7's display-only columns; TTM growth from filings available on the day (never a model input)."""
    out = day.with_columns(pl.col("close_raw").round(2), pl.col("turnover20").cast(pl.Float64).round(2).alias("turnover20"),
                           pl.col("r252").cast(pl.Float64).round(4), pl.col("dist_ath").cast(pl.Float64).round(4),
                           pl.col("n_hist").cast(pl.Int32).alias("sessions_listed"))
    if filings is None or filings.is_empty():
        return out.with_columns(pl.lit(None, pl.Float64).alias("ttm_rev_yoy"), pl.lit(None, pl.Float64).alias("ttm_pat_yoy"))
    snaps = FU.snapshots(filings.filter(pl.col("instrument_id").is_in(day["instrument_id"].implode())))
    got = FU.join_features(day.select("instrument_id", "date"), snaps).select("instrument_id", "ttm_rev_yoy", "ttm_pat_yoy")
    return out.join(got, on="instrument_id", how="left").with_columns(pl.col("ttm_rev_yoy").round(4), pl.col("ttm_pat_yoy").round(4))


def ranked(day: pl.DataFrame, booster: lgb.Booster) -> pl.DataFrame:
    """Universe rows of one date → score (6 dp, rounded at write), rank, investable."""
    x = day.select(ALL_FEATURES).to_numpy().astype(np.float32)
    out = day.with_columns(pl.Series("score", booster.predict(x), dtype=pl.Float64).round(6)).sort(["score", "instrument_id"], descending=[True, False])
    return out.with_columns(
        pl.int_range(1, pl.len() + 1).alias("rank"),
        pl.lit(out.height).alias("universe_n"),
        ((pl.col("close_raw") >= INVESTABLE_CLOSE) & (pl.col("turnover20") >= INVESTABLE_TURNOVER)).alias("investable"),
    )


def paths() -> tuple[Path, Path, Path]:
    archive = Path(os.environ.get("BASKFY_ARCHIVE", "/var/lib/baskfy/archive"))
    schemes = Path(os.environ.get("BASKFY_MB_SCHEMES", HERE / "nse_schemes.csv"))
    if not schemes.exists():
        schemes = HERE.parents[3] / "crates/providers/src/reference/nse_schemes.csv"
    return Path(os.environ.get("BASKFY_MB_DIR", "/var/lib/baskfy/state/mb")), archive / "nse/corporate-actions-year", schemes


def connect():
    import psycopg
    url = os.environ.get("BASKFY_DATABASE_URL")
    if not url:
        sys.exit("BASKFY_DATABASE_URL is not set")
    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"))


def published(conn) -> tuple[int, str, list[str]] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id, path, features FROM mb_model WHERE published_at IS NOT NULL ORDER BY published_at DESC, id DESC LIMIT 1")
        row = cur.fetchone()
    return None if row is None else (row[0], row[1], row[2])


def booster(path: str, features: list[str]) -> lgb.Booster:
    if features != ALL_FEATURES:
        raise SystemExit("REFUSED: the model's feature list differs from portfolioml.features.ALL_FEATURES; retrain")
    return lgb.Booster(model_file=path)


def score_day(conn, bars: pl.DataFrame, ins: pl.DataFrame, idx: pl.DataFrame, date: dt.date, model: tuple[int, str, list[str]]) -> pl.DataFrame:
    """Score one session from bars that end on it, and write it (idempotent)."""
    cut = bars.filter(pl.col("date") <= date)
    feat = P.tail_features(cut, ins, idx.filter(pl.col("date") <= date))
    day = ranked(feat.filter((pl.col("date") == date) & pl.col("universe")), booster(model[1], model[2]))
    day = day.join(hi63(cut), on="instrument_id", how="left").with_columns(pl.col("hi63_trigger_date").is_not_null().alias("hi63_trigger"))
    day = display(day, filings_from_pg(conn, date))
    rows = day.select(pl.lit(date).alias("date"), "instrument_id", pl.lit(model[0]).alias("model_id"), "score", "rank", "universe_n",
                      "investable", "hi63_trigger", "hi63_trigger_date", *DISPLAY)
    write_scores(conn, date, rows)
    return rows


def score(date: dt.date) -> int:
    mb_dir, ca_dir, schemes = paths()
    with connect() as conn:
        model = published(conn)
        if model is None:
            print("no published mb_model; nothing scored")
            return 3
        with tempfile.TemporaryDirectory(dir=mb_dir if mb_dir.exists() else None) as tmp:
            raw = raw_from_pg(conn, date, Path(tmp))
        bars, ins, idx = prepared(raw, ca_dir, schemes)
        if bars.is_empty() or bars["date"].max() != date:
            print(f"no full session of bars on {date}; nothing scored")
            return 3
        rows = score_day(conn, bars, ins, idx, date, model)
        print(f"mb_score_daily {date}: {rows.height} names, model {model[0]}, investable {rows['investable'].sum()}, hi63 {rows['hi63_trigger'].sum()}")
        if has_table(conn, "mb_paper_tranche"):
            import mb_paper
            print(mb_paper.nightly(conn, bars, ins, idx, raw.terminal, date, model, score_day))
    return 0


def paper_seed(path: str) -> int:
    import mb_paper
    mb_dir, ca_dir, schemes = paths()
    with connect() as conn:
        n = mb_paper.seed(conn, path)
        with conn.cursor() as cur:
            cur.execute("SELECT max(date) FROM ohlcv_daily")
            through = cur.fetchone()[0]
        with tempfile.TemporaryDirectory(dir=mb_dir if mb_dir.exists() else None) as tmp:
            raw = raw_from_pg(conn, through, Path(tmp))
        bars, _, idx = prepared(raw, ca_dir, schemes)
        with conn.transaction():
            got = mb_paper.refresh(conn, mb_paper.grid_of(bars, raw.terminal), idx, mb_paper.BACKTEST)
    print(f"mb paper seed: {n} backtest tranches added; {got}")
    return 0


def has_table(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
        return cur.fetchone()[0] is not None


def write_scores(conn, date: dt.date, rows: pl.DataFrame) -> None:
    """Idempotent: the date's rows are replaced in one transaction."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM mb_score_daily WHERE date = %s", (date,))
        cols = ", ".join(rows.columns)
        with cur.copy(f"COPY mb_score_daily ({cols}) FROM STDIN") as cp:
            for r in rows.iter_rows():
                cp.write_row(r)


def training_frame(bars: pl.DataFrame, ins: pl.DataFrame, idx: pl.DataFrame, terminal: pl.DataFrame, tmp: Path) -> tuple[pl.DataFrame, pl.DataFrame]:
    """build_dataset.py's dataset (variant b), stride-5 universe rows with a completed label."""
    named = P.with_names(bars, ins)
    for i, ch in enumerate(P.chunks(named, 12)):
        P.name_part(ch).write_parquet(tmp / f"f{i:02d}.parquet")
    del named
    feat = P.finish(pl.scan_parquet(str(tmp / "f*.parquet")).sort("instrument_id", "date").collect(), idx)
    status = terminal.select("instrument_id", pl.col("status").alias("terminal"))
    feat = feat.join(status, on="instrument_id", how="left").with_columns(pl.col("terminal").fill_null("alive"))
    cfg = MultibaggerConfig(up=1.0, horizon=250)
    lbars = feat.select("instrument_id", "date", "open", "high", "low", "close", "terminal")
    last_complete = lbars["date"].unique().sort()[-(cfg.horizon + 1)]
    labels = pl.concat([multibagger(ch, cfg, TERMINAL, last_complete).select("instrument_id", "date", "label") for ch in P.chunks(lbars, 12)])
    univ = feat.filter(pl.col("universe")).join(labels, on=["instrument_id", "date"], how="left")
    sessions = univ["date"].unique().sort()
    return univ.filter(pl.col("label").is_not_null() & pl.col("date").is_in(sessions[::TRAIN_STRIDE].implode())).sort("date"), feat


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra, rb = pl.Series(a).rank().to_numpy(), pl.Series(b).rank().to_numpy()
    return float(np.corrcoef(ra, rb)[0, 1])


def train(ack: str | None) -> int:
    mb_dir, ca_dir, schemes = paths()
    mb_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT max(date) FROM ohlcv_daily")
            through = cur.fetchone()[0]
        with tempfile.TemporaryDirectory(dir=mb_dir) as tmp:
            raw = raw_from_pg(conn, through, Path(tmp))
            bars, ins, idx = prepared(raw, ca_dir, schemes)
            train_rows, feat = training_frame(bars, ins, idx, raw.terminal, Path(tmp))
        x = train_rows.select(ALL_FEATURES).to_numpy().astype(np.float32)
        m = lgb.train(PARAMS, lgb.Dataset(x, train_rows["label"].to_numpy(), group=grp(train_rows)), ROUNDS)
        trained_on = train_rows["date"].max()
        today = feat.filter((pl.col("date") == feat["date"].max()) & pl.col("universe"))
        new_scores = m.predict(today.select(ALL_FEATURES).to_numpy().astype(np.float32))
        prev = published(conn)
        rho = None if prev is None else spearman(new_scores, booster(prev[1], prev[2]).predict(today.select(ALL_FEATURES).to_numpy().astype(np.float32)))
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("INSERT INTO mb_model (trained_on, label, features, params, path, rows, positives, spearman_prev) "
                        "VALUES (%s, %s, %s, %s, '', %s, %s, %s) RETURNING id",
                        (trained_on, LABEL, json.dumps(ALL_FEATURES), json.dumps({**PARAMS, "rounds": ROUNDS, "stride": TRAIN_STRIDE, "terminal": TERMINAL}),
                         train_rows.height, int(train_rows["label"].sum()), None if rho is None else round(rho, 4)))
            model_id = cur.fetchone()[0]
            path = mb_dir / f"mb_model_{model_id}_{trained_on}.txt"
            m.save_model(str(path))
            cur.execute("UPDATE mb_model SET path = %s WHERE id = %s", (str(path), model_id))
        print(f"mb_model {model_id}: {train_rows.height:,} rows, {int(train_rows['label'].sum()):,} positives, labels through {trained_on}; "
              f"Spearman vs model {prev[0] if prev else '-'} on {today['date'][0]}: {'n/a' if rho is None else f'{rho:.3f}'}")
        if rho is not None and rho < MIN_SPEARMAN and not ack:
            print(f"HELD: Spearman {rho:.3f} < {MIN_SPEARMAN}; publish with: mb_task.py publish {model_id} --ack <who>")
            return 4
        return publish(conn, model_id, ack or ("first model" if rho is None else f"auto, Spearman {rho:.3f}"))


def publish(conn, model_id: int, who: str) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE mb_model SET published_at = now(), published_by = %s WHERE id = %s AND published_at IS NULL RETURNING id", (who, model_id))
        if cur.fetchone() is None:
            print(f"mb_model {model_id}: no such unpublished model")
            return 3
    print(f"mb_model {model_id} published ({who})")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    opt = lambda k: rest[rest.index(k) + 1] if k in rest else None  # noqa: E731
    if cmd == "score" and opt("--date"):
        return score(dt.date.fromisoformat(opt("--date")))
    if cmd == "paper-seed" and rest:
        return paper_seed(rest[0])
    if cmd == "train":
        return train(opt("--ack"))
    if cmd == "publish" and rest and opt("--ack"):
        with connect() as conn:
            return publish(conn, int(rest[0]), opt("--ack"))
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
