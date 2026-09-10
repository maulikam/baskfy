"""The sleeve's own money (``docs/vbt/06`` VB5, DECISIONS-VB PACK.4).

**This book sizes against its own cash and never against the account.** A swing entry, a Friday
rebalance or a deposit must not silently resize a VBT line, and a name in the broker's account
that this sleeve did not buy is invisible to it — ``vb_position`` is the source of truth for what
it owns (``02`` Track C §5).

Pure, and the arithmetic is stated once here so the evening job, the desk page and the confirm
path cannot each derive a slightly different equity:

===================  ===========================================================================
realised             Σ ``pnl_inr`` over the sleeve's **closed** positions
cost of open         Σ ``entry_avg x quantity_open`` over the open ones
value of open        Σ ``mark x quantity_open`` — the mark is the latest published close, or the
                     entry when the name has not printed since
cash                 ``capital + realised - cost of open``
committed            Σ ``limit_price x quantity`` over the **working** orders — money a resting
                     bid has already spoken for
cash available       ``cash - committed``
equity               ``cash + value of open``
open exposure        ``value of open + committed``
===================  ===========================================================================

The distinction between *cash* and *cash available* is the one that matters and the one a naive
implementation loses: a limit resting in three names has not spent the money yet, and a plan that
sized a fourth line against the un-committed balance would over-commit the book on the morning
all four filled. ``04`` §9.1's ``SLOTS_FULL`` counts working orders for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class OpenPositionValue:
    """One open position, as the sleeve's arithmetic needs it."""

    instrument_id: int
    quantity_open: int
    entry_avg: Decimal
    #: The latest published close on or before the session, or the entry when there is none.
    mark: Decimal

    @property
    def cost(self) -> Decimal:
        return self.entry_avg * self.quantity_open

    @property
    def value(self) -> Decimal:
        return self.mark * self.quantity_open


@dataclass(frozen=True, slots=True)
class WorkingCommitment:
    """One resting limit, and the money it has spoken for but not yet spent."""

    instrument_id: int
    quantity: int
    limit_price: Decimal

    @property
    def value(self) -> Decimal:
        return self.limit_price * self.quantity


@dataclass(frozen=True, slots=True)
class SleeveValue:
    """Everything the plan needs to know about the sleeve's money, computed once."""

    capital_inr: Decimal
    realised_inr: Decimal
    cost_of_open_inr: Decimal
    value_of_open_inr: Decimal
    committed_inr: Decimal

    @property
    def cash_inr(self) -> Decimal:
        return self.capital_inr + self.realised_inr - self.cost_of_open_inr

    @property
    def cash_available_inr(self) -> Decimal:
        """Cash a **new** line may spend. Never negative: a book cannot un-commit money."""
        return max(self.cash_inr - self.committed_inr, _ZERO)

    @property
    def equity_inr(self) -> Decimal:
        """What the sleeve is worth. The slot is a tenth of this (``04`` §5.2)."""
        return self.cash_inr + self.value_of_open_inr

    @property
    def open_exposure_inr(self) -> Decimal:
        """What is already at work, or about to be. ``04`` §9.1's ``EXPOSURE_FULL`` reads this."""
        return self.value_of_open_inr + self.committed_inr

    @property
    def unrealised_inr(self) -> Decimal:
        return self.value_of_open_inr - self.cost_of_open_inr

    def quantize(self) -> SleeveValue:
        """Every rupee to the paise. Money is stored rounded (house rule 8)."""
        paise = Decimal("0.01")
        return SleeveValue(
            capital_inr=self.capital_inr.quantize(paise),
            realised_inr=self.realised_inr.quantize(paise),
            cost_of_open_inr=self.cost_of_open_inr.quantize(paise),
            value_of_open_inr=self.value_of_open_inr.quantize(paise),
            committed_inr=self.committed_inr.quantize(paise),
        )


def sleeve_value(
    *,
    capital_inr: Decimal,
    realised_inr: Decimal,
    open_positions: list[OpenPositionValue],
    working_orders: list[WorkingCommitment],
) -> SleeveValue:
    """The sleeve's money, from its own rows and nothing else.

    ``realised_inr`` is passed in rather than derived from a list of closed positions because the
    caller is already reading them for the journal and there is no reason to load them twice —
    and because a sleeve with three years of history should not carry three years of rows through
    this function to answer one question about today.
    """
    return SleeveValue(
        capital_inr=capital_inr,
        realised_inr=realised_inr,
        cost_of_open_inr=sum((position.cost for position in open_positions), _ZERO),
        value_of_open_inr=sum((position.value for position in open_positions), _ZERO),
        committed_inr=sum((order.value for order in working_orders), _ZERO),
    )
