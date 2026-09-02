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

from baskfy_core.models.market import BAR_SOURCES, CORPORATE_ACTION_TYPES
from baskfy_core.models.reference import INSTRUMENT_TYPES

InstrumentType = Literal["EQ", "ETF", "INDEX"]
CorporateActionType = Literal["dividend", "split", "bonus", "rights", "demerger"]
BarSource = Literal["kite", "nse"]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InstrumentRecord(_Record):
    """One tradable instrument, as a provider sees it (docs/09 ``list_instruments``).

    Maps onto ``baskfy_core.models.Instrument``. ``kite_token`` is the provider key that
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


class EquityFundamental(_Record):
    """One NSE cash-equity quote distilled onto ``fundamental_daily`` (docs/05 §14).

    ``marketcap_cr`` is issued shares * last price / 1e7, rounded to a rupee-crore integer
    (docs/13 §2 finding 7). ``pe`` is nullable: the exchange omits it for names without earnings,
    and the P/E filter must not treat a missing ratio as zero.
    """

    symbol: str = Field(min_length=1)
    date: dt.date
    shares_outstanding: int | None = None
    last_price: Decimal | None = None
    marketcap_cr: int | None = None
    pe: Decimal | None = None
    pb: Decimal | None = None
    div_yield: Decimal | None = None


class BrokerAccountRef(_Record):
    """Which tenant's broker account a read is for.

    Every holdings read names the account it is for, rather than letting an adapter infer it
    from ambient configuration. That mirrors the multi-tenant clause the two laws already put on
    the order path — "every order carries ``user_id`` + ``broker_account_id``, and the gateway
    refuses a mismatch" — and applies it to reads, which is where the same mistake is quieter:
    an order attributed to the wrong tenant is caught at the broker, while *holdings* attributed
    to the wrong tenant simply show one person another person's money.

    ``kite_user_id`` is the broker's own client id when it is known. It is carried so an adapter
    that can cheaply verify the session belongs to this account is able to; nothing here forces
    a round trip to find out.
    """

    broker_account_id: int = Field(gt=0)
    #: Catalog id from ``baskfy_core.broker_connections`` (``zerodha``, ``upstox``, ...).
    broker_id: str = Field(min_length=1)
    kite_user_id: str | None = None


class QuoteRecord(_Record):
    """One instrument's live quote, as ``KiteProvider.quotes`` reports it (docs/swing/06 SW6).

    Read by the premarket EP scan and nothing else so far. ``last_price`` is the print (during
    NSE's pre-open, the indicative equilibrium price), ``volume`` the session's volume so far,
    ``prev_close`` the exchange's own previous close — the number ``live_gap`` measures the gap
    from, and it comes with the quote rather than from our bar table so a corporate action
    between the two cannot manufacture a gap. The circuit bands are the exchange's for the day.
    Prices are Decimal at the boundary, never float (house rule 9).
    """

    symbol: str = Field(min_length=1)
    exchange: str = "NSE"
    instrument_token: int | None = None
    last_price: Decimal
    volume: int = 0
    prev_close: Decimal | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    upper_circuit: Decimal | None = None
    lower_circuit: Decimal | None = None
    #: The exchange's own timestamp for the print, when the quote carries one.
    as_of: dt.datetime | None = None


class CatalystRecord(_Record):
    """One NSE corporate announcement, reduced to what a watchlist may carry (SW11B, A3).

    A **headline, a timestamp and a link** — never the filing's text. STANDING-ANSWERS A3 and
    the Track C §7 amendment: the feed is single-tenant own-use, it links out, and it
    redistributes nothing. ``headline`` is NSE's own subject line (``desc``) and, where the
    exchange publishes one, its one-line summary of the filing, capped at
    ``baskfy_providers.nse.HEADLINE_MAX_CHARS``; the attachment body is never read into it.

    ``published_at`` is tz-aware IST when the exchange stamps the announcement, ``None`` when
    it does not (it happens — an undated row is still a link, it just cannot be "newest").
    ``url`` is the filing attachment, verbatim, query string and all: it is the uniqueness key
    in ``sw_catalyst``, and normalising it would be how two different filings collide.
    """

    symbol: str = Field(min_length=1)
    headline: str = Field(min_length=1)
    published_at: dt.datetime | None = None
    url: str = Field(min_length=1)
    source: str = "NSE_ANNOUNCEMENT"


class EarningsDateRecord(_Record):
    """One board-meeting entry from NSE's event calendar whose purpose is a financial result.

    ``event_date`` is the meeting date the exchange lists; ``purpose`` its own wording
    ("Financial Results", "Financial Results/Dividend"), kept so a page can show why the date
    is flagged. Nothing here is a forecast — it is the exchange's calendar, read once a morning.
    """

    symbol: str = Field(min_length=1)
    event_date: dt.date
    purpose: str = Field(min_length=1)
    #: The calendar page the date was read from — what a row links out to.
    url: str = Field(min_length=1)


class BrokerHoldingRecord(_Record):
    """One equity position as a broker reports it — the physical truth of §4.6's layer 1.

    **Quantity is three numbers, never one** (desk non-negotiable #2, carried verbatim from
    ``kite-momentum-rebalancer``): the settled quantity, the T1 quantity still in the settlement
    pipe, and the quantity pledged as collateral. A holding of 100 shares that has 40 pledged
    reports ``quantity=60, collateral_quantity=40``, and a sync that read ``quantity`` alone
    would see a sell of 40 shares that never happened and ask the user about it. So the sum is
    :attr:`total_quantity` and it is the only number the ledger is ever shown.

    ``average_price`` is optional in the type even though Kite always sends one, because §5.2
    turns on the difference between an unknown buy price and a zero: a broker that does not
    publish cost basis must produce ``None`` here and not a plausible-looking zero.
    """

    symbol: str = Field(min_length=1)
    exchange: str = "NSE"
    isin: str | None = None
    #: The settled, freely sellable quantity.
    quantity: Decimal = Decimal("0")
    #: Bought yesterday, not yet settled. Still the user's shares.
    t1_quantity: Decimal = Decimal("0")
    #: Pledged for margin. Sells directly on Zerodha (desk non-negotiable #3), so it counts.
    collateral_quantity: Decimal = Decimal("0")
    average_price: Decimal | None = None
    last_price: Decimal | None = None
    product: str = "CNC"

    @field_validator("quantity", "t1_quantity", "collateral_quantity")
    @classmethod
    def _non_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError(f"a holding quantity cannot be negative; got {v}")
        return v

    @field_validator("average_price", "last_price")
    @classmethod
    def _non_negative_price(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v < 0:
            raise ValueError(f"a price cannot be negative; got {v}")
        return v

    @property
    def total_quantity(self) -> Decimal:
        """The whole position. Non-negotiable #2's sum, in one place so nobody re-derives it."""
        return self.quantity + self.t1_quantity + self.collateral_quantity


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
    "BrokerAccountRef",
    "BrokerHoldingRecord",
    "CorporateAction",
    "CorporateActionType",
    "IndexSnapshot",
    "InstrumentRecord",
    "InstrumentType",
    "ListingRecord",
    "conform",
    "empty_frame",
]
