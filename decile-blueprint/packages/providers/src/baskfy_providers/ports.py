"""The provider ports (Prompt 2 deliverable 1), transcribed from docs/09 §"Provider ports".

The Protocols are deliberately **synchronous**, exactly as docs/09 writes them. Everything that
calls a provider is a Celery task (docs/02: "Celery + Celery Beat"), which is synchronous; the
async surface in this codebase belongs to the API, which never talks to a provider directly. A
provider port is a boundary to a blocking network device, and pretending otherwise would buy
nothing but colour.

``Capability`` exists so ``CompositeProvider`` can route by capability rather than by isinstance
checks — docs/09 describes it as routing "by capability", and docs/02 promises "adding a paid
vendor later is a new adapter, not a rewrite". A vendor that serves bars but not listings
advertises exactly that.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

import polars as pl

from baskfy_providers.records import (
    BrokerAccountRef,
    BrokerHoldingRecord,
    CorporateAction,
    EquityFundamental,
    IndexSnapshot,
    InstrumentRecord,
    ListingRecord,
)


class Capability(StrEnum):
    """One routable operation. Values match the port method names."""

    LIST_INSTRUMENTS = "list_instruments"
    DAILY_BARS = "daily_bars"
    INDEX_CONSTITUENTS = "index_constituents"
    INDEX_SNAPSHOTS = "index_snapshots"
    CORPORATE_ACTIONS = "corporate_actions"
    LISTINGS = "listings"
    BHAVCOPY = "bhavcopy"
    EQUITY_FUNDAMENTALS = "equity_fundamentals"
    #: PORTFOLIO_REDESIGN.md §4.6 layer 1 — what a broker says the user actually holds.
    BROKER_HOLDINGS = "broker_holdings"
    #: §4.4 — the broker's own cash balance, which becomes the Unallocated cash bucket.
    BROKER_CASH = "broker_cash"


#: docs/09's table: KiteProvider provides BarsProvider.
BARS_CAPABILITIES: frozenset[Capability] = frozenset(
    {Capability.LIST_INSTRUMENTS, Capability.DAILY_BARS}
)

#: docs/09's table: NSEProvider provides ReferenceProvider.
REFERENCE_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.INDEX_CONSTITUENTS,
        Capability.INDEX_SNAPSHOTS,
        Capability.CORPORATE_ACTIONS,
        Capability.LISTINGS,
        Capability.BHAVCOPY,
        Capability.EQUITY_FUNDAMENTALS,
    }
)


#: PORTFOLIO_REDESIGN.md §10 phase 1: HoldingsProvider is the broker-ledger port.
#:
#: Deliberately a *third* set rather than an extension of ``BARS_CAPABILITIES``. Kite happens to
#: serve both, but they are different products with different failure modes and — decisively —
#: different consequences when a fallback substitutes for them. A wrong candle is a wrong chart;
#: a wrong holdings list is somebody else's money on somebody's screen. Keeping the sets apart is
#: what lets the production stack register a fixture bars provider and *no* fixture holdings
#: provider, so a holdings call fails loudly instead of degrading into fiction.
HOLDINGS_CAPABILITIES: frozenset[Capability] = frozenset(
    {Capability.BROKER_HOLDINGS, Capability.BROKER_CASH}
)


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """What one adapter can currently serve, and why not if it cannot.

    ``available`` answers "would a call succeed right now, as far as we can tell without making
    one". `providers doctor` prints this; it must be derivable from configuration alone, because
    the doctor has to work with no credentials and no network (Prompt 2 acceptance criterion 4).
    """

    name: str
    available: bool
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    detail: str = ""

    @property
    def served_capabilities(self) -> frozenset[Capability]:
        """What it can serve *right now* — nothing at all when it is unavailable."""
        return self.capabilities if self.available else frozenset()


@runtime_checkable
class HealthReporting(Protocol):
    """What `providers doctor` needs from any adapter (Prompt 2 deliverable 6)."""

    @property
    def name(self) -> str:
        """Short identifier used in errors and in the doctor output."""
        ...

    def capabilities(self) -> frozenset[Capability]:
        """What this adapter can serve, in principle."""
        ...

    def check(self) -> ProviderHealth:
        """Probe configuration and report. Must never raise, and must never hit the network."""
        ...


@runtime_checkable
class BarsProvider(HealthReporting, Protocol):
    """docs/09 §"Provider ports"."""

    def list_instruments(self) -> list[InstrumentRecord]: ...

    def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame:
        """Raw daily candles for one instrument, conforming to ``DAILY_BARS_SCHEMA``.

        docs/09: what Kite returns is unadjusted, so this is the raw exchange print. The
        adjusted series is derived later by the pipeline, never by a provider.
        """
        ...


@runtime_checkable
class ReferenceProvider(HealthReporting, Protocol):
    """docs/09 §"Provider ports"."""

    def index_constituents(self, index_slug: str, on: dt.date) -> list[str]: ...

    def index_snapshots(self, on: dt.date) -> list[IndexSnapshot]: ...

    def corporate_actions(self, since: dt.date) -> list[CorporateAction]: ...

    def listings(self) -> list[ListingRecord]: ...

    def bhavcopy(self, on: dt.date) -> pl.DataFrame:
        """One day's bhavcopy, conforming to ``BHAVCOPY_SCHEMA`` (incl. series, circuit bands)."""
        ...

    def equity_fundamentals(
        self,
        on: dt.date,
        symbols: Sequence[str],
        *,
        series_by_symbol: Mapping[str, str] | None = None,
    ) -> list[EquityFundamental]:
        """Issued capital, P/E and derived market cap for cash equities on ``on``.

        docs/05 §14: marketcap and P/E come from NSE. A name the exchange does not quote
        that day is omitted — the join stores NULL, which the UI renders as an em dash.

        ``series_by_symbol`` is an optional hint: NSE quotes a symbol under a series, and a
        caller holding ``instrument.series`` can save the adapter a lookup round trip. An
        adapter is free to ignore it; a wrong hint must never produce a wrong row.
        """
        ...


@runtime_checkable
class HoldingsProvider(HealthReporting, Protocol):
    """The broker ledger — PORTFOLIO_REDESIGN.md §4.6 layer 1, read-only, forever.

    **This port cannot place an order and must never gain a method that can.** Law 2 says
    ``packages/execution`` is the only path to an order; a provider adapter is on the other side
    of that line, and the moment a "sell this" verb appears here the guards, the risk checks, the
    rate limits and the journal are all bypassed. Reads only: what is held, and how much cash is
    sitting there.

    Both methods take a :class:`BrokerAccountRef` rather than reading ambient credentials,
    because holdings are per-tenant and an adapter that infers the account from configuration
    cannot be told it has the wrong one.
    """

    def broker_holdings(self, account: BrokerAccountRef) -> list[BrokerHoldingRecord]:
        """Every equity position the broker reports for ``account``.

        An account holding nothing returns an empty list — that is an answer, not an error, and
        a sync must be able to tell "the user sold everything" from "the fetch failed". A fetch
        that fails raises a :class:`baskfy_providers.errors.ProviderError`; it never returns an
        empty list to paper over the failure, because an empty list here reads as a total exit
        and would have the sync ask the user about every position they own.
        """
        ...

    def broker_cash(self, account: BrokerAccountRef) -> Decimal | None:
        """The broker's reported cash balance for ``account``, or ``None`` if it publishes none.

        ``None`` and ``Decimal("0")`` are different statements and both are meaningful: zero is
        an empty account, ``None`` is a broker that does not tell us. §4.4's Unallocated cash
        bucket must not be written from a guess, so a ``None`` leaves the stored balance alone.

        Decimal, never float (house rule 9): this number is money.
        """
        ...
