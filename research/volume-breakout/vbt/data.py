"""Load the exported Baskfy tables into a dense (instrument x session) price panel.

Everything the scan and the simulator need is numpy: one float64 matrix per field, rows are
instruments, columns are trading sessions (the exchange calendar, so a missing bar is NaN and
is never silently forward-filled into a fill price).
"""
from __future__ import annotations

import dataclasses as dc
import re
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

# The `etf` universe (index_def slug "etf", 350 members) is the authority; the regexes only catch
# an ETF that never made it into that list. Kept narrow on purpose: "GOLD" would flag GOLDIAM.
ETF_NAME_RE = re.compile(r"\bETF\b", re.I)
ETF_SYMBOL_RE = re.compile(r"(BEES|ETF|IETF)$", re.I)


@dc.dataclass(frozen=True)
class Panel:
    symbols: np.ndarray          # (N,) str
    instrument_ids: np.ndarray   # (N,) int
    dates: np.ndarray            # (D,) datetime64[D]
    open: np.ndarray             # (N, D) float64, NaN where no bar
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray            # adjusted
    close_raw: np.ndarray        # exchange print
    volume: np.ndarray           # adjusted volume
    upper_circuit: np.ndarray    # NaN where unknown
    lower_circuit: np.ndarray
    is_etf: np.ndarray           # (N,) bool

    @property
    def n(self) -> int:
        return len(self.symbols)

    @property
    def d(self) -> int:
        return len(self.dates)

    def col(self, date: np.datetime64) -> int:
        return int(np.searchsorted(self.dates, date))


def _read(path: Path, name: str) -> pl.DataFrame:
    f = path / f"{name}.csv.gz"
    if not f.exists():
        f = path / f"{name}.csv"
    return pl.read_csv(f, infer_schema_length=10000)


def load_panel(data_dir: str | Path, *, start: str | None = None, end: str | None = None) -> Panel:
    path = Path(data_dir)
    inst = _read(path, "instrument")
    bars = _read(path, "ohlcv_daily").with_columns(pl.col("date").str.to_date())
    if start:
        bars = bars.filter(pl.col("date") >= pl.lit(start).str.to_date())
    if end:
        bars = bars.filter(pl.col("date") <= pl.lit(end).str.to_date())

    # Universe: the cash segment as Chartink reads it. `series` is the instrument's *current*
    # series, so it cannot be applied point-in-time; BE/BZ (trade-for-trade) stay in because every
    # trade here is delivery anyway and dropping today's BE names would drop the history of the
    # names that were later demoted (a reverse survivorship bias). SM/ST/SZ (SME platform, lot
    # sizes) are out. A null series is a name with no current listing row — delisted, kept.
    eq = inst.filter((pl.col("instrument_type") == "EQ"))
    series = eq["series"].fill_null("EQ")
    eq = eq.with_columns(series.alias("series")).filter(pl.col("series").is_in(["EQ", "BE", "BZ"]))

    etf_ids: set[int] = set()
    try:
        idef = _read(path, "index_def")
        etf_index = idef.filter(pl.col("slug") == "etf")["id"].to_list()
        if etf_index:
            imd = _read(path, "index_member_daily")
            etf_ids = set(imd.filter(pl.col("index_id").is_in(etf_index))["instrument_id"].to_list())
    except FileNotFoundError:
        pass

    names = eq["name"].fill_null("").to_list()
    syms = eq["symbol"].to_list()
    ids = eq["id"].to_list()
    is_etf = np.array(
        [(i in etf_ids) or bool(ETF_NAME_RE.search(nm)) or bool(ETF_SYMBOL_RE.search(s)) for i, nm, s in zip(ids, names, syms)],
        dtype=bool,
    )

    bars = bars.filter(pl.col("instrument_id").is_in(ids))
    # keep only instruments that have at least one bar in the window (the instrument table also
    # carries thousands of long-delisted names with no bars in this history)
    have = set(bars["instrument_id"].unique().to_list())
    keep = [k for k, i in enumerate(ids) if i in have]
    ids = [ids[k] for k in keep]; syms = [syms[k] for k in keep]; is_etf = is_etf[keep]
    dates = np.sort(bars["date"].unique().to_numpy()).astype("datetime64[D]")
    id_to_row = {i: r for r, i in enumerate(ids)}
    rows = np.array([id_to_row[i] for i in bars["instrument_id"].to_list()], dtype=np.int64)
    cols = np.searchsorted(dates, bars["date"].to_numpy().astype("datetime64[D]"))

    def mat(colname: str) -> np.ndarray:
        m = np.full((len(ids), len(dates)), np.nan)
        v = bars[colname].cast(pl.Float64, strict=False).to_numpy()
        m[rows, cols] = v
        return m

    # Special sessions (a Saturday muhurat / test session where only ~200 names printed) are not
    # trading days for a daily strategy and would poison every rolling window that spans them.
    # Drop any column with fewer than 25% of the bars of the surrounding sessions.
    counts = np.bincount(cols, minlength=len(dates))
    med = pd.Series(counts).rolling(41, center=True, min_periods=5).median().to_numpy()
    keep_col = counts >= 0.25 * med
    if not keep_col.all():
        dropped = dates[~keep_col]
        print(f"dropping {len(dropped)} thin sessions: {[str(d) for d in dropped]}")
        keep_idx = np.nonzero(keep_col)[0]
        remap = np.full(len(dates), -1); remap[keep_idx] = np.arange(len(keep_idx))
        ok_rows = remap[cols] >= 0
        bars = bars.filter(pl.Series(ok_rows))
        rows = rows[ok_rows]; cols = remap[cols[ok_rows]]; dates = dates[keep_idx]

    return Panel(
        symbols=np.array(syms, dtype=object),
        instrument_ids=np.array(ids, dtype=np.int64),
        dates=dates,
        open=mat("open"), high=mat("high"), low=mat("low"), close=mat("close"),
        close_raw=mat("close_raw"), volume=mat("volume"),
        upper_circuit=mat("upper_circuit"), lower_circuit=mat("lower_circuit"),
        is_etf=is_etf,
    )


def load_index(path: str | Path, name: str) -> tuple[np.ndarray, np.ndarray]:
    """(dates, close) of one benchmark from desk.index_series."""
    df = pl.read_csv(Path(path)).filter(pl.col("index_name") == name).sort("date")
    return df["date"].str.to_date().to_numpy().astype("datetime64[D]"), df["close"].to_numpy().astype(float)
