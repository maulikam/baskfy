"""The options run's Kite reads, each on the box's shared limiter for its endpoint (OP3).

Kite's caps are per endpoint family — ``quote`` 1 req/s, ``historical`` 3 req/s, the rest 10 —
under a combined read ceiling the whole box shares (`baskfy_providers.factory`, M85). The desk's
swing monitor and page reads already queue on ``baskfy:ratelimit:kite:read`` and then on
``baskfy:ratelimit:kite:<family>`` (`kite-momentum-rebalancer/app/core/kite_limits.py`, SW21).
:func:`build_options_kite` builds one adapter per family on **those same keys**, so the chain
collector's minute ``quote()`` and the index-bar reads cannot, together with the desk, exceed any
cap: they wait for the same departure clocks.

Nothing here has an order verb (law 2). ``basket_order_margins`` is a margin *calculation*.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from baskfy_core.models.base import JsonObject
from baskfy_providers.factory import KiteFamily, KiteLane, build_kite_family_provider
from baskfy_providers.records import (
    BasketMarginRecord,
    MarginLegRecord,
    MinuteBarRecord,
    OptionContractRecord,
    OptionQuoteRecord,
)
from baskfy_providers.retry import RetryHooks
from baskfy_providers.settings import ProviderSettings, get_provider_settings


class QuoteReader(Protocol):
    """``quote()`` with depth — the ``quote`` family (1 req/s)."""

    def option_quotes(self, keys: Sequence[str]) -> list[OptionQuoteRecord]: ...


class BarReader(Protocol):
    """``historical_data(interval="minute")`` — the ``historical`` family (3 req/s)."""

    def minute_bars(
        self, token: int, start: dt.datetime | dt.date, end: dt.datetime | dt.date
    ) -> list[MinuteBarRecord]: ...


class GeneralReader(Protocol):
    """The ``general`` family reads the probe needs: the NFO master, margins, basket margins."""

    def option_contracts(self, underlying: str) -> list[OptionContractRecord]: ...

    def margins_shape(self) -> JsonObject: ...

    def basket_order_margins(
        self, legs: Sequence[MarginLegRecord], *, consider_positions: bool = False
    ) -> BasketMarginRecord: ...


@dataclass(frozen=True, slots=True)
class OptionsKite:
    """One adapter per endpoint family, each on the box's shared clocks for that family."""

    quotes: QuoteReader
    bars: BarReader
    general: GeneralReader


def build_options_kite(
    settings: ProviderSettings | None = None,
    retry_hooks: RetryHooks | None = None,
    *,
    bars_lane: KiteLane = KiteLane.INTERACTIVE,
) -> OptionsKite:
    """The three adapters. ``bars_lane=BULK`` for the Tier-1 backfill (it has nowhere to be).

    The quote adapter is always interactive: a minute's chain is only worth anything inside that
    minute, and the bulk lane's clock exists to make a backfill *yield*, not to delay a quote.
    """
    resolved = settings or get_provider_settings()
    return OptionsKite(
        quotes=build_kite_family_provider(resolved, KiteFamily.QUOTE, retry_hooks),
        bars=build_kite_family_provider(
            resolved, KiteFamily.HISTORICAL, retry_hooks, lane=bars_lane
        ),
        general=build_kite_family_provider(resolved, KiteFamily.GENERAL, retry_hooks),
    )
