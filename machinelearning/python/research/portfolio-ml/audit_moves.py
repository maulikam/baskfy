"""PM0.12: every one-day close move beyond 60 %, matched to archived NSE actions and the scheme list; writes a corrected export.

usage: python audit_moves.py <export dir> <corporate-actions-year dir> <nse_schemes.csv> <corrected export dir>
A destination equal to the export reads ohlcv_daily.csv.gz and writes the corrected ohlcv_daily.parquet beside it, the research export's own step.
Never writes to the database."""

from __future__ import annotations

import collections
import csv
import datetime as dt
import glob
import json
import os
import sys
from pathlib import Path

import polars as pl

from portfolioml import action_audit as A

OUT = Path(__file__).parent / "out" / "audit"
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
pl.Config.set_tbl_rows(50); pl.Config.set_tbl_width_chars(170)


def date_of(text: str) -> dt.date | None:
    try:
        d, m, y = text.strip()[:11].split("-")
        return dt.date(int(y), MONTHS[m.lower()], int(d))
    except (ValueError, KeyError, AttributeError):
        return None


def events(ca_dir: str, schemes: str) -> tuple[dict, dict]:
    by_symbol, by_issuer = collections.defaultdict(list), collections.defaultdict(list)
    for path in sorted(glob.glob(f"{ca_dir}/*")):
        for r in json.load(open(path, encoding="utf8")):
            ex = date_of(r.get("exDate") or "")
            terms = A.action_terms(r.get("subject") or "") if ex else []
            for term in terms:
                by_symbol[(r.get("symbol") or "").strip().upper()].append((ex, *term))
                by_issuer[(r.get("isin") or "")[:7]].append((ex, *term))
    for r in csv.DictReader(open(schemes, encoding="utf8")):
        ex = dt.date.fromisoformat(r["ex_date"])
        for term in A.action_terms(r["subject"]):
            by_issuer[r["issuer"]].append((ex, *term))
    return by_symbol, by_issuer


def stepped(bars: pl.DataFrame) -> pl.DataFrame:
    g = pl.col("instrument_id")
    return bars.sort("instrument_id", "date").with_columns(pl.col("close").shift(1).over(g).alias("prev_close"), pl.col("date").shift(1).over(g).alias("prev_date")).filter(
        pl.col("prev_close").is_not_null()).with_columns((pl.col("close") / pl.col("prev_close")).alias("step"))


def moves(bars: pl.DataFrame) -> pl.DataFrame:
    return stepped(bars).filter((pl.col("step") - 1).abs() > A.MOVE)


def read_bars(data: Path, in_place: bool) -> pl.DataFrame:
    if in_place:
        return pl.read_csv(data / "ohlcv_daily.csv.gz", schema_overrides={"upper_circuit": pl.Float64, "lower_circuit": pl.Float64}).with_columns(pl.col("date").str.to_date())
    return pl.read_parquet(data / "ohlcv_daily.parquet")


def nearby(r: dict, meta: dict, by_symbol: dict, by_issuer: dict) -> list:
    info = meta.get(r["instrument_id"], {})
    found = {*by_symbol.get((info.get("symbol") or "").upper(), []), *by_issuer.get((info.get("isin") or "")[:7], [])}
    return [(c, f) for ex, c, f in found if A.in_window(ex, r["prev_date"], r["date"])]


def classified(bars: pl.DataFrame, meta: dict, terminal: pl.DataFrame, by_symbol: dict, by_issuer: dict) -> pl.DataFrame:
    """Every move beyond 60 % and every matched 20–60 % fall, with its class and multiplier."""
    bars = bars.select("instrument_id", "date", "open", "close", "close_raw")  # all the scan reads
    m = moves(bars).join(terminal, on="instrument_id", how="left")
    rows = []
    for r in m.to_dicts():
        info = meta.get(r["instrument_id"], {})
        near = nearby(r, meta, by_symbol, by_issuer)
        name, multiplier = A.classify(r["step"], near)
        if name == "demerger" and multiplier is None:
            basis = r["open"] / r["prev_close"]
            multiplier = basis if 0 < basis < 1 else r["step"]
        rows.append({"instrument_id": r["instrument_id"], "symbol": info.get("symbol"), "date": r["date"], "prev_date": r["prev_date"], "step": r["step"], "close_raw": r["close_raw"],
                     "class": name, "multiplier": multiplier, "candidates": len(near), "dead": r["status"] is not None, "terminal": r["status"] or "alive", "band": "beyond_60"})
    falls = stepped(bars).filter(pl.col("step").is_between(A.FALL_LOW, A.FALL_HIGH)).join(terminal, on="instrument_id", how="left")
    for r in falls.to_dicts():
        got = A.classify_fall(r["step"], nearby(r, meta, by_symbol, by_issuer))
        if got:
            rows.append({"instrument_id": r["instrument_id"], "symbol": meta.get(r["instrument_id"], {}).get("symbol"), "date": r["date"], "prev_date": r["prev_date"], "step": r["step"], "close_raw": r["close_raw"],
                         "class": got[0], "multiplier": got[1], "candidates": 1, "dead": r["status"] is not None, "terminal": r["status"] or "alive", "band": "fall_20_60"})
    return pl.DataFrame(rows)


def corrected(bars: pl.DataFrame, t: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """Bars before each explained move scaled by its multiplier; the database is never written."""
    if t.is_empty():
        return bars, 0
    known = t.filter(pl.col("multiplier").is_not_null()).select("instrument_id", pl.col("date").alias("move"), "multiplier")
    hit = bars.join(known, on="instrument_id").filter(pl.col("date") < pl.col("move")).group_by("instrument_id", "date").agg(pl.col("multiplier").product().alias("mult"))
    fixed = bars.join(hit, on=["instrument_id", "date"], how="left").with_columns(pl.col("mult").fill_null(1.0)).with_columns(
        *[(pl.col(c) * pl.col("mult")).alias(c) for c in ("open", "high", "low", "close")], (pl.col("volume") / pl.col("mult")).round(0).cast(pl.Int64).alias("volume")).drop("mult")
    return fixed, hit.height


def correct_bars(bars: pl.DataFrame, ins: pl.DataFrame, terminal: pl.DataFrame, ca_dir: str, schemes: str) -> tuple[pl.DataFrame, int]:
    """The nightly's entry: ins has id, symbol, isin; terminal has instrument_id, status."""
    meta = {r["id"]: r for r in ins.to_dicts()}
    by_symbol, by_issuer = events(ca_dir, schemes)
    return corrected(bars, classified(bars, meta, terminal, by_symbol, by_issuer))


def main(export: str, ca_dir: str, schemes: str, dest: str) -> int:
    data = Path(export)
    OUT.mkdir(parents=True, exist_ok=True)
    in_place = Path(dest).resolve() == data.resolve()
    bars = read_bars(data, in_place)
    ins = pl.read_csv(data / "instrument.csv.gz", columns=["id", "symbol", "isin"], schema_overrides={"isin": pl.String})
    terminal = pl.read_csv(data / "instrument_terminal.csv.gz").select("instrument_id", "status")
    meta = {r["id"]: r for r in ins.to_dicts()}
    by_symbol, by_issuer = events(ca_dir, schemes)
    t = classified(bars, meta, terminal, by_symbol, by_issuer)
    t.write_csv(OUT / "moves.csv")
    print("moves", t.height, "names", t["instrument_id"].n_unique())
    summary = t.group_by("band", "class", "dead").len().pivot(on="dead", index=["band", "class"], values="len").fill_null(0).sort("band", "class")
    summary.write_csv(OUT / "classes.csv"); print(summary)
    print(t.group_by("class", (pl.col("step") < 1).alias("down")).len().sort("class", "down"))
    print("unexplained with an action nearby that did not fit:", t.filter((pl.col("class") == "unexplained") & (pl.col("candidates") > 0)).height)
    print("unexplained under ₹2:", t.filter((pl.col("class") == "unexplained") & (pl.col("close_raw") < 2)).height)
    print("unexplained after a gap of 30+ days:", t.filter((pl.col("class") == "unexplained") & ((pl.col("date") - pl.col("prev_date")).dt.total_days() >= 30)).height)
    fixed, adjusted = corrected(bars, t)
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    if not in_place:
        for f in data.iterdir():
            target = out / f.name
            if f.name != "ohlcv_daily.parquet" and not f.name.startswith(("dataset_", "features")) and not target.exists():
                os.symlink(f, target)
    fixed.write_parquet(out / "ohlcv_daily.parquet")
    print("rows", fixed.height, "adjusted bars", adjusted, "residual moves beyond 60 %", moves(fixed).height)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:5]))
