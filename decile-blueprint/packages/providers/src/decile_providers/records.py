"""The records the provider ports exchange (Prompt 2 deliverable 1).

Pydantic models rather than plain dataclasses, because these cross a trust boundary: every one of
them is built from a broker response or a scraped exchange file, and a provider that quietly
hands back a malformed row is exactly the failure the data-quality gate (docs/09) exists to catch.
Validating at the seam turns that into a loud error at the point of ingestion instead.

All are frozen — a record is a reading of the world at a moment, never a mutable buffer.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

from decile_core.models.market import BAR_SOURCES, CORPORATE_ACTION_TYPES
from decile_core.models.reference import INSTRUMENT_TYPES

InstrumentType = Literal["EQ", "ETF", "INDEX"]
CorporateActionType = Literal["dividend", "split", "bonus", "rights", "demerger"]
BarSource = Literal["kite", "nse"]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InstrumentRecord(_Record):
    """One tradable instrument, as a provider sees it (docs/09 ``list_instruments``).

    Maps onto ``decile_core.models.Instrument``. ``kite_token`` is the provider key that
    ``daily_bars`` is addressed by, and is absent for instruments Kite does not carry.
    """

    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    instrument_type: InstrumentType
    series: str | None = None
    isin: str | None = None
    exchange: str = "NSE"
    kite_token: int | None = None
    lot_size: int | None = None
    face_value: Decimal | None = None
    listed_on: dt.date | None = None
    delisted_on: dt.date | None = None

    @field_validator("instrument_type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in INSTRUMENT_TYPES:
            raise ValueError(f"unknown instrument_type {v!r}; expected one of {INSTRUMENT_TYPES}")
        return v


class IndexSnapshot(_Record):
    """A published index level with its fundamentals (docs/09 ``index_snapshots``).

    ``pe``/``pb``/``div_yield`` are optional because docs/01 §7 observes derived indices
    (``Nifty50 PR 1x Inverse``, ``India VIX``) that publish no fundamentals at all — the reference
    product renders them as ``-``. A zero would be a lie; ``None`` is the truth.
    """

    index_slug: str = Field(min_length=1)
    date: dt.date
    level: Decimal | None = None
    change_abs: Decimal | None = None
    change_pct: Decimal | None = None
    pe: Decimal | None = None
    pb: Decimal | None = None
    div_yield: Decimal | None = None


class CorporateAction(_Record):
    """A split, bonus, dividend, rights issue or demerger (docs/09 ``corporate_actions``).

    Ratio convention follows docs/04: split 10:1 -> ``ratio_from=10, ratio_to=1``;
    bonus 4:1 -> ``ratio_from=4, ratio_to=1``. ``raw`` keeps the source payload so the
    adjustment step (docs/09 §"Adjustment algorithm") can always be re-derived and audited.
    """

    symbol: str = Field(min_length=1)
    action_type: CorporateActionType
    ex_date: dt.date
    ratio_from: Decimal | None = None
    ratio_to: Decimal | None = None
    amount: Decimal | None = None
    raw: dict[str, object] = Field(default_factory=dict)

    @field_validator("action_type")
    @classmethod
    def _known_action(cls, v: str) -> str:
        if v not in CORPORATE_ACTION_TYPES:
            raise ValueError(f"unknown action_type {v!r}; expected one of {CORPORATE_ACTION_TYPES}")
        return v

    @field_validator("ratio_from", "ratio_to")
    @classmethod
    def _positive_ratio(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError(f"corporate-action ratio legs must be positive; got {v}")
        return v


class ListingRecord(_Record):
    """One row of the NSE listings register (docs/01 §1 ``/listings``, 3,524 rows)."""

    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    series: str | None = None
    isin: str | None = None
    listed_on: dt.date | None = None
    face_value: Decimal | None = None
    paid_up_value: Decimal | None = None
    market_lot: int | None = None


# ---------------------------------------------------------------------------
# Frame schemas
#
# Prompt 2 §2 requires daily_bars() to return "a Polars DataFrame with a fixed schema". Declaring
# it once here means KiteProvider, FixtureProvider and any future vendor adapter are
# interchangeable at the call site, and a drifting adapter fails at its own boundary rather than
# three steps later inside the factor engine.
# ---------------------------------------------------------------------------

#: docs/09: "Kite returns unadjusted OHLC by default. Treat everything from Kite as raw."
#: These are therefore the *raw* exchange prints; the adjustment step (Prompt 3) derives `close`.
DAILY_BARS_SCHEMA: Final[dict[str, pl.DataType]] = {
    "symbol": pl.String(),
    "date": pl.Date(),
    "open": pl.Decimal(18, 4),
    "high": pl.Decimal(18, 4),
    "low": pl.Decimal(18, 4),
    "close": pl.Decimal(18, 4),
    "volume": pl.Int64(),
    "source": pl.String(),
}

#: docs/09 ``bhavcopy(on)`` — "incl. circuit bands, series".
BHAVCOPY_SCHEMA: Final[dict[str, pl.DataType]] = {
    "symbol": pl.String(),
    "series": pl.String(),
    "date": pl.Date(),
    "open": pl.Decimal(18, 4),
    "high": pl.Decimal(18, 4),
    "low": pl.Decimal(18, 4),
    "close": pl.Decimal(18, 4),
    "prev_close": pl.Decimal(18, 4),
    "volume": pl.Int64(),
    # docs/13 §2 finding 5: exchange turnover in rupees, not close x shares.
    "turnover": pl.Decimal(20, 2),
    "trades": pl.Int64(),
    "upper_circuit": pl.Decimal(18, 4),
    "lower_circuit": pl.Decimal(18, 4),
}


def empty_frame(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    """An empty frame that still satisfies ``schema``.

    Returning ``pl.DataFrame()`` for "no data" would silently change the column set, so every
    provider returns this instead — an instrument with no bars in a window is a normal outcome,
    not a schema change.
    """
    return pl.DataFrame(schema=schema)


def conform(frame: pl.DataFrame, schema: dict[str, pl.DataType]) -> pl.DataFrame:
    """Project ``frame`` onto ``schema``, raising if a column is missing.

    Casting rather than trusting the source keeps a provider that starts returning floats for
    prices from poisoning ``numeric`` columns downstream (docs/04: money in numeric, never float).
    """
    missing = [name for name in schema if name not in frame.columns]
    if missing:
        raise ValueError(f"frame is missing required columns {missing}; got {frame.columns}")
    return frame.select(
        [pl.col(name).cast(dtype, strict=True).alias(name) for name, dtype in schema.items()]
    )


__all__ = [
    "BAR_SOURCES",
    "BHAVCOPY_SCHEMA",
    "DAILY_BARS_SCHEMA",
    "BarSource",
    "CorporateAction",
    "CorporateActionType",
    "IndexSnapshot",
    "InstrumentRecord",
    "InstrumentType",
    "ListingRecord",
    "conform",
    "empty_frame",
]
