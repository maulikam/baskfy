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
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

import polars as pl

from decile_providers.records import (
    CorporateAction,
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
    }
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
