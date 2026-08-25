"""A configurable provider stub, shared by the composite and CLI tests.

Lives in its own module rather than inside a test file because pytest's rootdir-based import mode
gives test modules no package, so one test file cannot import another.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from baskfy_providers.ports import Capability, ProviderHealth
from baskfy_providers.records import (
    DAILY_BARS_SCHEMA,
    CorporateAction,
    EquityFundamental,
    IndexSnapshot,
    InstrumentRecord,
    ListingRecord,
    empty_frame,
)


class StubProvider:
    """A provider that advertises a capability set and answers however the test wants."""

    def __init__(  # noqa: PLR0913 - a test double is configured, not called
        self,
        name: str,
        capabilities: frozenset[Capability],
        *,
        raises: Exception | None = None,
        available: bool = True,
        symbols: tuple[str, ...] = ("SBIN",),
        check_raises: Exception | None = None,
    ) -> None:
        self._name = name
        self._capabilities = capabilities
        self._raises = raises
        self._available = available
        self._symbols = symbols
        self._check_raises = check_raises
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    def check(self) -> ProviderHealth:
        if self._check_raises is not None:
            raise self._check_raises
        return ProviderHealth(
            name=self._name,
            available=self._available,
            capabilities=self._capabilities,
            detail="stub",
        )

    def _answer(self) -> None:
        self.calls += 1
        if self._raises is not None:
            raise self._raises

    def list_instruments(self) -> list[InstrumentRecord]:
        self._answer()
        return [
            InstrumentRecord(symbol=s, name=s, instrument_type="EQ", series="EQ")
            for s in self._symbols
        ]

    def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame:
        del token, start, end
        self._answer()
        return empty_frame(DAILY_BARS_SCHEMA)

    def index_constituents(self, index_slug: str, on: dt.date) -> list[str]:
        del index_slug, on
        self._answer()
        return list(self._symbols)

    def index_snapshots(self, on: dt.date) -> list[IndexSnapshot]:
        self._answer()
        return [IndexSnapshot(index_slug="nifty-50", date=on)]

    def corporate_actions(self, since: dt.date) -> list[CorporateAction]:
        self._answer()
        return [CorporateAction(symbol="CUPID", action_type="bonus", ex_date=since)]

    def listings(self) -> list[ListingRecord]:
        self._answer()
        return [ListingRecord(symbol=s, name=s) for s in self._symbols]

    def bhavcopy(self, on: dt.date) -> pl.DataFrame:
        del on
        self._answer()
        return empty_frame(DAILY_BARS_SCHEMA)

    def equity_fundamentals(
        self, on: dt.date, symbols: tuple[str, ...] | list[str]
    ) -> list[EquityFundamental]:
        del on, symbols
        self._answer()
        return []
