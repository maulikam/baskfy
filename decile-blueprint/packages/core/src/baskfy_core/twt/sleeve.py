"""The sleeve's own money (``docs/twt/04`` §9).

**This book sizes against its own cash and never against the account.** It is a ``MY_STRATEGY``
capital portfolio in the M34 / ``PORTFOLIO_REDESIGN`` sense, exactly as the swing and VBT sleeves
are, and it never reads the account's total holdings: a swing entry, a Friday rebalance or a
deposit must not silently resize a TWT line.

**And it never sells what it did not buy.** ``tw_position`` is the source of truth for what the
sleeve owns; a holding in the account that the sleeve never bought is invisible to the arithmetic
below and can never appear in a TWT plan line (``02`` Track C §5; TW10 proves it).

Pure, and the arithmetic is stated once here so the evening job, the desk page and the confirm path
cannot each derive a slightly different equity:

===================  =========================================================================
realised             Σ ``pnl_inr`` over the sleeve's **closed** positions
cost of open         Σ ``entry_avg x quantity_open`` over the open ones
value of open        Σ ``mark x quantity_open`` — the mark is the latest published close on or
                     before the session, or the last known close when the name did not print,
                     and the plan's detail **says which**
cash                 ``capital + realised - cost of open``
equity               ``cash + value of open``
cash available       ``equity - value of open`` — which is ``cash``
===================  =========================================================================

The last identity is worth stating rather than hiding: ``04`` §9.2 defines cash available as equity
less the value of the open positions, and **there are no working orders in this sleeve to reserve
against** (``03`` §6 — TWT-1 buys at the next open, at market, so nothing rests). VBT-1's sleeve has
to subtract its committed limits and this one does not, which is the difference between a book that
bids and waits and a book that takes the open.

**A sleeve at ₹0 plans nothing.** ``tw_config.sleeve_capital_inr`` is seeded at zero and every
signal is skipped ``NO_SLEEVE_CAPITAL`` until Maulik sets it — the run never does (root
``CLAUDE.md`` safety rails).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

_ZERO = Decimal(0)
_PAISE = Decimal("0.01")


class MarkSource(StrEnum):
    """Where a position's mark came from (``04`` §9.1), because the page has to say so."""

    #: The instrument's published close on the session itself.
    SESSION_CLOSE = "SESSION_CLOSE"
    #: The name did not print on the session; the last known close is used instead.
    LAST_KNOWN_CLOSE = "LAST_KNOWN_CLOSE"
    #: Nothing has printed since the fill. The entry is the only honest mark.
    ENTRY = "ENTRY"


@dataclass(frozen=True, slots=True)
class OpenPositionValue:
    """One open position, as the sleeve's arithmetic needs it."""

    instrument_id: int
    quantity_open: int
    entry_avg: Decimal
    mark: Decimal
    mark_source: MarkSource = MarkSource.SESSION_CLOSE

    @property
    def cost(self) -> Decimal:
        return self.entry_avg * self.quantity_open

    @property
    def value(self) -> Decimal:
        return self.mark * self.quantity_open

    @property
    def is_stale_mark(self) -> bool:
        """True when the plan's detail must say the mark is not this session's close."""
        return self.mark_source is not MarkSource.SESSION_CLOSE


@dataclass(frozen=True, slots=True)
class SleeveValue:
    """Everything the plan needs to know about the sleeve's money, computed once."""

    capital_inr: Decimal
    realised_inr: Decimal
    cost_of_open_inr: Decimal
    value_of_open_inr: Decimal

    @property
    def cash_inr(self) -> Decimal:
        return self.capital_inr + self.realised_inr - self.cost_of_open_inr

    @property
    def equity_inr(self) -> Decimal:
        """``04`` §9.1. The slot is a tenth of this (``04`` §6.1)."""
        return self.cash_inr + self.value_of_open_inr

    @property
    def cash_available_inr(self) -> Decimal:
        """``04`` §9.2: equity less the value of the open positions. Never negative — a book
        cannot un-commit money it has already spent."""
        return max(self.equity_inr - self.value_of_open_inr, _ZERO)

    @property
    def open_exposure_inr(self) -> Decimal:
        """What is already at work. ``04`` §10.1's ``EXPOSURE_FULL`` reads this."""
        return self.value_of_open_inr

    @property
    def unrealised_inr(self) -> Decimal:
        return self.value_of_open_inr - self.cost_of_open_inr

    @property
    def has_capital(self) -> bool:
        """``04`` §9.3: a sleeve at ₹0 plans nothing."""
        return self.equity_inr > _ZERO

    def quantize(self) -> SleeveValue:
        """Every rupee to the paise. Money is stored rounded (house rule 8)."""
        return SleeveValue(
            capital_inr=self.capital_inr.quantize(_PAISE),
            realised_inr=self.realised_inr.quantize(_PAISE),
            cost_of_open_inr=self.cost_of_open_inr.quantize(_PAISE),
            value_of_open_inr=self.value_of_open_inr.quantize(_PAISE),
        )


def sleeve_value(
    *,
    capital_inr: Decimal,
    realised_inr: Decimal,
    open_positions: list[OpenPositionValue],
) -> SleeveValue:
    """The sleeve's money, from its own rows and nothing else (``04`` §9.1).

    ``realised_inr`` is passed in rather than derived from a list of closed positions because the
    caller is already reading them for the journal and there is no reason to load them twice — and
    because a sleeve whose average hold is a hundred sessions will accumulate years of closed rows
    that have nothing to say about today.
    """
    return SleeveValue(
        capital_inr=capital_inr,
        realised_inr=realised_inr,
        cost_of_open_inr=sum((position.cost for position in open_positions), _ZERO),
        value_of_open_inr=sum((position.value for position in open_positions), _ZERO),
    )
