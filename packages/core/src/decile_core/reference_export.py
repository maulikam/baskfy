"""Reader for the reference product's CSV export (PROMPTS.md Prompt 1 deliverable 5b).

``tests/fixtures/reference-screen-export-2026-08-18.csv`` is a 271-row, 93-column export of the
"Investing 001" screen for trade date 2026-08-18 (docs/13). It is a labelled answer key, and this
module turns it into ``instrument``, ``factor_daily`` and ``index_member_daily`` rows so that
later prompts can test against real reference data with no network access.

A discrepancy worth knowing about
---------------------------------
docs/13 §1 describes the flag columns as three contiguous groups of 14
(``is_nifty_50 … is_etf``, then 14 ``*_top_beta``, then 14 ``*_top_volatility``). The actual file
groups **13** at a time and appends ``is_etf``, ``is_etf_top_beta``, ``is_etf_top_volatility`` at
the very end, as columns 90-92. The counts in docs/13 are right; the layout is not. Since docs/13
§5 requires our own export to reproduce "this file's exact column names, order, quoting and BOM",
:data:`EXPORT_COLUMNS` below is taken from the file itself, which is the binding artefact.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

import polars as pl

from decile_core.universes import UNIVERSES, Universe

#: The export is UTF-8 **with BOM** and quotes only the ``name`` field (docs/13 §1). Reproduce
#: both in Prompt 9's CSV writer.
EXPORT_ENCODING: Final = "utf-8-sig"

#: The 93 columns, in the file's real order (see the module docstring).
EXPORT_COLUMNS: Final[tuple[str, ...]] = (
    "name",
    "symbol",
    "series",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "marketcap",
    "volume_shares",
    "absolute_return_one_year",
    "absolute_return_nine_months",
    "absolute_return_six_months",
    "absolute_return_three_months",
    "absolute_return_one_month",
    "sharpe_return_one_year",
    "sharpe_return_nine_months",
    "sharpe_return_six_months",
    "sharpe_return_three_months",
    "sharpe_return_one_month",
    "rsi_one_year",
    "rsi_nine_months",
    "rsi_six_months",
    "rsi_three_months",
    "rsi_one_month",
    "volatility_one_year",
    "volatility_nine_months",
    "volatility_six_months",
    "volatility_three_months",
    "volatility_one_month",
    "beta",
    "circuits_one_year",
    "circuits_nine_months",
    "circuits_six_months",
    "circuits_three_months",
    "circuits_one_month",
    "positive_days_percent_one_year",
    "positive_days_percent_nine_months",
    "positive_days_percent_six_months",
    "positive_days_percent_three_months",
    "positive_days_percent_one_month",
    "high_one_year",
    "high_all_time",
    "away_from_high_one_year",
    "away_from_high_all_time",
    "ma_200",
    "ma_100",
    "ma_50",
    "ma_20",
    "median_volume_one_year",
    "is_nifty_50",
    "is_nifty_next_50",
    "is_nifty_100",
    "is_nifty_200",
    "is_nifty_500",
    "is_nifty_total_market",
    "is_nifty_large_mid_250",
    "is_nifty_midcap_150",
    "is_nifty_smallcap_250",
    "is_nifty_microcap_250",
    "is_nifty_mid_small_400",
    "is_nifty_allcap",
    "is_nifty_fno",
    "is_nifty_50_top_beta",
    "is_nifty_next_50_top_beta",
    "is_nifty_100_top_beta",
    "is_nifty_200_top_beta",
    "is_nifty_500_top_beta",
    "is_nifty_total_market_top_beta",
    "is_nifty_large_mid_250_top_beta",
    "is_nifty_midcap_150_top_beta",
    "is_nifty_smallcap_250_top_beta",
    "is_nifty_microcap_250_top_beta",
    "is_nifty_mid_small_400_top_beta",
    "is_nifty_allcap_top_beta",
    "is_nifty_fno_top_beta",
    "is_nifty_50_top_volatility",
    "is_nifty_next_50_top_volatility",
    "is_nifty_100_top_volatility",
    "is_nifty_200_top_volatility",
    "is_nifty_500_top_volatility",
    "is_nifty_total_market_top_volatility",
    "is_nifty_large_mid_250_top_volatility",
    "is_nifty_midcap_150_top_volatility",
    "is_nifty_smallcap_250_top_volatility",
    "is_nifty_microcap_250_top_volatility",
    "is_nifty_mid_small_400_top_volatility",
    "is_nifty_allcap_top_volatility",
    "is_nifty_fno_top_volatility",
    "is_etf",
    "is_etf_top_beta",
    "is_etf_top_volatility",
)

#: The window suffix the export uses, per months-back.
_WINDOW_SUFFIX: Final[dict[int, str]] = {
    12: "one_year",
    9: "nine_months",
    6: "six_months",
    3: "three_months",
    1: "one_month",
}

#: export column -> factor_daily column, for every column that maps 1:1.
FACTOR_COLUMN_MAP: Final[dict[str, str]] = {
    "close": "close",
    "marketcap": "marketcap_cr",
    # docs/13 §2 finding 5: `volume` is exchange turnover in ₹, not a share count.
    "volume": "vol_day_val",
    "median_volume_one_year": "median_vol_12m",
    "beta": "beta_12m",
    "high_one_year": "high_1y",
    "high_all_time": "high_ath",
    "away_from_high_one_year": "away_high_1y",
    "away_from_high_all_time": "away_high_ath",
    "ma_200": "ma_200",
    "ma_100": "ma_100",
    "ma_50": "ma_50",
    "ma_20": "ma_20",
    "series": "series",
    **{f"absolute_return_{s}": f"ret_{m}m" for m, s in _WINDOW_SUFFIX.items()},
    **{f"sharpe_return_{s}": f"sharpe_{m}m" for m, s in _WINDOW_SUFFIX.items()},
    **{f"rsi_{s}": f"rsi_{m}m" for m, s in _WINDOW_SUFFIX.items()},
    **{f"volatility_{s}": f"vol_{m}m" for m, s in _WINDOW_SUFFIX.items()},
    **{f"circuits_{s}": f"circuits_{m}m" for m, s in _WINDOW_SUFFIX.items()},
    **{f"positive_days_percent_{s}": f"pos_days_{m}m" for m, s in _WINDOW_SUFFIX.items()},
}

#: Columns present in the export that `factor_daily` deliberately does not hold.
#: `open`/`high`/`low`/`volume_shares` belong to `ohlcv_daily`; `name`/`symbol` to `instrument`.
UNMAPPED_EXPORT_COLUMNS: Final[frozenset[str]] = frozenset(
    {"name", "symbol", "date", "open", "high", "low", "volume_shares"}
)

_DECIMAL_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "close",
        "beta",
        "high_one_year",
        "high_all_time",
        "away_from_high_one_year",
        "away_from_high_all_time",
        "ma_200",
        "ma_100",
        "ma_50",
        "ma_20",
    }
    | {f"absolute_return_{s}" for s in _WINDOW_SUFFIX.values()}
    | {f"sharpe_return_{s}" for s in _WINDOW_SUFFIX.values()}
    | {f"rsi_{s}" for s in _WINDOW_SUFFIX.values()}
    | {f"volatility_{s}" for s in _WINDOW_SUFFIX.values()}
    | {f"positive_days_percent_{s}" for s in _WINDOW_SUFFIX.values()}
)
_INT_COLUMNS: Final[frozenset[str]] = frozenset(
    {"marketcap", "volume", "volume_shares", "median_volume_one_year"}
    | {f"circuits_{s}" for s in _WINDOW_SUFFIX.values()}
)


def default_fixture_path() -> Path:
    """``tests/fixtures/reference-screen-export-2026-08-18.csv``, found from this file."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "tests" / "fixtures" / "reference-screen-export-2026-08-18.csv"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "reference-screen-export-2026-08-18.csv not found under any parent tests/fixtures/"
    )


def read_export(path: Path | None = None) -> pl.DataFrame:
    """Load the export verbatim, with every column typed but no renaming."""
    source = path or default_fixture_path()
    frame = pl.read_csv(source, encoding=EXPORT_ENCODING, infer_schema_length=None)
    missing = [c for c in EXPORT_COLUMNS if c not in frame.columns]
    unexpected = [c for c in frame.columns if c not in EXPORT_COLUMNS]
    if missing or unexpected:
        raise ValueError(
            f"reference export schema drift: missing={missing} unexpected={unexpected}"
        )
    return frame.select(EXPORT_COLUMNS)


@dataclass(frozen=True, slots=True)
class InstrumentRow:
    symbol: str
    name: str
    series: str
    instrument_type: str


@dataclass(frozen=True, slots=True)
class MembershipRow:
    universe_slug: str
    symbol: str
    date: dt.date


@dataclass(frozen=True, slots=True)
class ReferenceRows:
    """Everything the export can populate, keyed for insertion by ``decile_core.seed``."""

    as_of: dt.date
    instruments: tuple[InstrumentRow, ...]
    #: One dict per instrument, keyed by ``factor_daily`` column name. ``instrument_id`` is
    #: resolved by the caller, which owns the database identity.
    factors: tuple[dict[str, object], ...]
    memberships: tuple[MembershipRow, ...]


def _flag(universe: Universe, suffix: str = "") -> str:
    base = "is_etf" if universe.slug == "etf" else f"is_{universe.slug.replace('-', '_')}"
    return f"{base}{suffix}"


def _as_int(value: object) -> int:
    """Narrow a Polars cell to ``int``. The export's numeric columns are ints or int-like text."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"cannot read {value!r} ({type(value).__name__}) as an integer")


def _mask(row: dict[str, object], suffix: str) -> int:
    mask = 0
    for universe in UNIVERSES:
        if _as_int(row[_flag(universe, suffix)]) == 1:
            mask |= universe.mask_value
    return mask


def to_rows(frame: pl.DataFrame | None = None) -> ReferenceRows:
    """Project the export into instrument / factor / membership rows."""
    data = frame if frame is not None else read_export()
    dates = data["date"].unique().to_list()
    if len(dates) != 1:
        raise ValueError(f"expected a single trade date in the export, got {sorted(dates)}")
    as_of = dt.date.fromisoformat(str(dates[0]))

    instruments: list[InstrumentRow] = []
    factors: list[dict[str, object]] = []
    memberships: list[MembershipRow] = []

    for record in data.iter_rows(named=True):
        symbol = str(record["symbol"])
        is_etf = _as_int(record["is_etf"]) == 1
        instruments.append(
            InstrumentRow(
                symbol=symbol,
                name=str(record["name"]),
                series=str(record["series"]),
                instrument_type="ETF" if is_etf else "EQ",
            )
        )

        factor_row: dict[str, object] = {"date": as_of, "symbol": symbol}
        for export_column, model_column in FACTOR_COLUMN_MAP.items():
            factor_row[model_column] = _coerce(export_column, record[export_column])
        factor_row["universe_mask"] = _mask(record, "")
        factor_row["top_beta_mask"] = _mask(record, "_top_beta")
        factor_row["top_volatility_mask"] = _mask(record, "_top_volatility")
        factors.append(factor_row)

        for universe in UNIVERSES:
            if _as_int(record[_flag(universe)]) == 1:
                memberships.append(MembershipRow(universe.slug, symbol, as_of))

    return ReferenceRows(
        as_of=as_of,
        instruments=tuple(instruments),
        factors=tuple(factors),
        memberships=tuple(memberships),
    )


def _coerce(column: str, value: object) -> object:
    if value is None or value == "":
        return None
    if column in _DECIMAL_COLUMNS:
        return Decimal(str(value))
    if column in _INT_COLUMNS:
        return _as_int(value)
    return value
