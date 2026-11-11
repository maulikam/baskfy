"""Step 1: export → data/features.parquet (once) → data/dataset_<geometry>.parquet.

Memory-lean on purpose: per-name features and labels are computed in instrument chunks on the
price columns only, stored as float32, and joined at the end."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import polars as pl

from portfolioml import features as F
from portfolioml import panel
from portfolioml.labels import BarrierConfig, MultibaggerConfig, multibagger, triple_barrier

DATA = Path(os.environ.get("PM_DATA", Path(__file__).parent / "data"))
OUT = Path(__file__).parent / "out"
CHUNKS = 12
PRICE = ["instrument_id", "date", "open", "high", "low", "close", "volume", "close_raw", "turnover"]
KEEP = ["instrument_id", "date", "symbol", "eligible", "close", "close_raw", "open", "high", "low", "liquid", "n_hist"]


def _chunks(bars: pl.DataFrame) -> list[pl.DataFrame]:
    ids = bars["instrument_id"].unique().sort()
    step = (len(ids) + CHUNKS - 1) // CHUNKS
    return [bars.filter(pl.col("instrument_id").is_in(ids[i:i + step].implode())) for i in range(0, len(ids), step)]


def build_features(t0: float) -> pl.DataFrame:
    bars = panel.load_bars(DATA).select(PRICE)
    ins = panel.load_instruments(DATA)
    idx = panel.load_index_series(DATA)
    print(f"bars {bars.shape} sessions {bars['date'].n_unique()}  {time.time()-t0:.0f}s", flush=True)
    bars = bars.join(ins.select("instrument_id", "eligible", "symbol"), on="instrument_id", how="left")
    bars = bars.with_columns(pl.col("eligible").fill_null(False))
    tmp = DATA / "_chunks"; tmp.mkdir(exist_ok=True)
    for old in tmp.glob("*.parquet"):  # a stale chunk would join the glob below
        old.unlink()
    for i, ch in enumerate(_chunks(bars)):
        f = F.name_features(ch)
        f32 = [c for c in F.NAME_FEATURES if c in f.columns and c not in ("rs21", "rs63", "n_hist")] + ["turnover20"]
        part = f.select(*KEEP, *f32).with_columns([pl.col(c).cast(pl.Float32) for c in f32])
        part.write_parquet(tmp / f"{i:02d}.parquet")
        print(f"  chunk {i+1}/{CHUNKS} {part.shape}  {time.time()-t0:.0f}s", flush=True)
        del f, part
    del bars
    feat = pl.scan_parquet(str(tmp / "*.parquet")).sort("instrument_id", "date").collect()
    feat = feat.join(F.market_features(idx), on="date", how="left")
    feat = feat.join(F.breadth(feat), on="date", how="left")
    feat = F.cross_sectional(feat)
    cs = [c for c in F.CS_FEATURES + F.MARKET_FEATURES + ["rs21", "rs63"] if c in feat.columns]
    feat = feat.with_columns([pl.col(c).cast(pl.Float32) for c in cs])
    # suspicious overnight gaps = unadjusted corporate actions (VBT §1); quarantine the 60 sessions
    # *after* one — backward-looking only, so the universe never knows about a gap still to come
    sus = ((pl.col("r1").abs() > 0.6) | (pl.col("gap").abs() > 0.6)).cast(pl.Int8)
    feat = feat.with_columns(
        (sus.rolling_max(61, min_samples=1).over("instrument_id") > 0).alias("near_sus_gap")
    ).with_columns((pl.col("liquid") & pl.col("eligible") & ~pl.col("near_sus_gap")).alias("universe"))
    print(f"features {feat.shape}  {time.time()-t0:.0f}s", flush=True)
    return feat


def build_labels(bars: pl.DataFrame, cfg: BarrierConfig | MultibaggerConfig, t0: float, terminal: str | None = None) -> pl.DataFrame:
    parts = []
    last_complete = bars["date"].unique().sort()[-(cfg.horizon + 1)]
    for ch in _chunks(bars):
        if isinstance(cfg, MultibaggerConfig):
            lab = multibagger(ch, cfg, terminal, last_complete).select("instrument_id", "date", "entry_px", "label", "held", "fwd_ret_h", "fwd_max_ret", "fwd_min_ret", "fwd_min_close_ret")
            parts.append(lab.with_columns([pl.col(c).cast(pl.Float32) for c in ("fwd_ret_h", "fwd_max_ret", "fwd_min_ret", "fwd_min_close_ret")]))
            continue
        lab = triple_barrier(ch, cfg).select("instrument_id", "date", "entry_px", "label", "exit_k", "exit_ret", "fwd_ret_h")
        parts.append(lab.with_columns(pl.col("exit_k").cast(pl.Int16), *[pl.col(c).cast(pl.Float32) for c in ("exit_ret", "fwd_ret_h")]))
    out = pl.concat(parts)
    print(f"labels {out.shape}  {time.time()-t0:.0f}s", flush=True)
    return out


def main(argv: list[str]) -> None:
    t0 = time.time()
    mb = "--mb" in argv
    terminal = next((a.split("=")[1] for a in argv if a.startswith("--terminal=")), None)
    argv = [a for a in argv if a != "--mb" and not a.startswith("--terminal=")]
    if mb:  # python build_dataset.py --mb 1.0 250  → touches 2x within 250 sessions
        up, horizon = (float(argv[0]), int(argv[1])) if len(argv) >= 2 else (1.0, 250)
        cfg: BarrierConfig | MultibaggerConfig = MultibaggerConfig(up=up, horizon=horizon)
        tag = f"mb_u{int(round(up*100))}_h{horizon}" + (f"_t{terminal}" if terminal else "")
    else:
        up, down, horizon = (float(argv[0]), float(argv[1]), int(argv[2])) if len(argv) >= 3 else (0.05, 0.03, 10)
        cfg = BarrierConfig(up=up, down=down, horizon=horizon)
        tag = f"u{int(round(up*100))}_d{int(round(down*100))}_h{horizon}"
    fp = DATA / "features.parquet"
    if fp.exists():
        feat = pl.read_parquet(fp)
        print(f"features (cached) {feat.shape}", flush=True)
    else:
        feat = build_features(t0)
        feat.write_parquet(fp)
    bars = feat.select("instrument_id", "date", "open", "high", "low", "close")
    if terminal:
        status = panel.load_terminal(DATA)
        feat = feat.join(status, on="instrument_id", how="left").with_columns(pl.col("terminal").fill_null("alive"))
        bars = feat.select("instrument_id", "date", "open", "high", "low", "close", "terminal")
    labels = build_labels(bars, cfg, t0, terminal)
    del bars
    labels.drop("terminal", strict=False).write_parquet(DATA / "_labels.parquet")
    del labels
    feat.write_parquet(DATA / "_feat_t.parquet")
    del feat
    OUT.mkdir(exist_ok=True)
    (pl.scan_parquet(DATA / "_feat_t.parquet")
       .join(pl.scan_parquet(DATA / "_labels.parquet"), on=["instrument_id", "date"], how="left")
       .sink_parquet(DATA / f"dataset_{tag}.parquet"))
    ds = pl.scan_parquet(DATA / f"dataset_{tag}.parquet")
    print(f"dataset_{tag}  {time.time()-t0:.0f}s", flush=True)

    u = ds.filter(pl.col("universe")).select("date", "instrument_id", "label", "fwd_ret_h").collect()
    print("universe rows", u.shape[0], "labelled", u["label"].is_not_null().sum())
    print(u.group_by(pl.col("date").dt.year().alias("year")).agg(
        pl.len().alias("rows"), pl.col("instrument_id").n_unique().alias("names"),
        pl.col("label").mean().alias("base_rate"), pl.col("fwd_ret_h").mean().alias("mean_fwd_h"),
        pl.col("fwd_ret_h").median().alias("median_fwd_h"),
    ).sort("year"))
    print("near_sus_gap rows quarantined:", ds.filter(pl.col("liquid") & pl.col("eligible") & pl.col("near_sus_gap")).select(pl.len()).collect().item())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
