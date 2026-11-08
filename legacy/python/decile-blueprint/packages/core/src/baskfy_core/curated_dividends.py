"""Dividend derivation for curated investments — docs/smallcase/04 §4, 03 ``cb_dividend``.

Pure Decimal math: holdings-history windows and cash corporate actions in, ``cb_dividend``
row payloads out. No database, no network, no clock.

A cash dividend accrues when the investment held a positive quantity of the instrument on
the action's ex-date. ``total = amount_per_share * qty_held``, rounded half-up to paise at
write time (house rule 8). Source is always ``CORPORATE_ACTIONS``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from baskfy_core.gst import money

__all__ = [
    "DIVIDEND_SOURCE",
    "CorporateActionCash",
    "DividendRow",
    "HoldingWindow",
    "derive_dividends",
    "qty_held_on",
    "sum_dividends",
]

#: docs/smallcase/03 — the only ``cb_dividend.source`` value this run emits.
DIVIDEND_SOURCE: Final = "CORPORATE_ACTIONS"

DividendSource = Literal["CORPORATE_ACTIONS"]

InstrumentId = int

#: Closed window: held on ``d`` when ``from_date <= d <= to_date``.
#: A sell on ex-date still entitles the seller (NSE ex-date convention).
HoldingTuple = tuple[dt.date, dt.date, Decimal]
ActionTuple = tuple[dt.date, Decimal]


@dataclass(frozen=True, slots=True)
class HoldingWindow:
    """One contiguous lot for an instrument. Both ends inclusive."""

    from_date: dt.date
    to_date: dt.date
    qty: Decimal

    def __post_init__(self) -> None:
        if self.to_date < self.from_date:
            raise ValueError(
                f"holding window ends before it starts: {self.from_date} → {self.to_date}"
            )
        if self.qty < 0:
            raise ValueError(f"holding qty cannot be negative: {self.qty}")


@dataclass(frozen=True, slots=True)
class CorporateActionCash:
    """One cash dividend (or dividend-shaped) corporate action per share."""

    ex_date: dt.date
    amount_per_share: Decimal

    def __post_init__(self) -> None:
        if self.amount_per_share < 0:
            raise ValueError(f"amount_per_share cannot be negative: {self.amount_per_share}")


@dataclass(frozen=True, slots=True)
class DividendRow:
    """One ``cb_dividend`` row's computed columns (id / investment_id filled by the writer)."""

    instrument_id: int
    ex_date: dt.date
    amount_per_share: Decimal
    qty_held: Decimal
    total: Decimal
    source: DividendSource = DIVIDEND_SOURCE


def _as_window(raw: HoldingWindow | HoldingTuple) -> HoldingWindow:
    if isinstance(raw, HoldingWindow):
        return raw
    from_date, to_date, qty = raw
    return HoldingWindow(from_date=from_date, to_date=to_date, qty=Decimal(qty))


def _as_action(raw: CorporateActionCash | ActionTuple) -> CorporateActionCash:
    if isinstance(raw, CorporateActionCash):
        return raw
    ex_date, amount = raw
    return CorporateActionCash(ex_date=ex_date, amount_per_share=Decimal(amount))


def qty_held_on(windows: Sequence[HoldingWindow | HoldingTuple], on: dt.date) -> Decimal:
    """Sum quantity across windows that cover ``on`` (closed ``[from, to]``)."""
    total = Decimal(0)
    for raw in windows:
        window = _as_window(raw)
        if window.from_date <= on <= window.to_date:
            total += window.qty
    return total


def derive_dividends(
    holdings_history: Mapping[InstrumentId, Sequence[HoldingWindow | HoldingTuple]],
    corporate_actions: Mapping[InstrumentId, Sequence[CorporateActionCash | ActionTuple]],
) -> tuple[DividendRow, ...]:
    """Emit ``cb_dividend`` payloads for every cash CA falling inside a holdings window.

    Instruments present only in one map contribute nothing. Zero ``qty_held`` or zero
    ``amount_per_share`` rows are omitted (nothing to journal). Output is sorted by
    ``(ex_date, instrument_id)`` so re-runs are idempotent for writers.
    """
    rows: list[DividendRow] = []
    for instrument_id, actions in corporate_actions.items():
        windows = holdings_history.get(instrument_id, ())
        if not windows:
            continue
        for raw_action in actions:
            action = _as_action(raw_action)
            if action.amount_per_share == 0:
                continue
            qty = qty_held_on(windows, action.ex_date)
            if qty == 0:
                continue
            total = money(action.amount_per_share * qty)
            if total == 0:
                continue
            rows.append(
                DividendRow(
                    instrument_id=instrument_id,
                    ex_date=action.ex_date,
                    amount_per_share=action.amount_per_share,
                    qty_held=qty,
                    total=total,
                    source=DIVIDEND_SOURCE,
                )
            )
    rows.sort(key=lambda row: (row.ex_date, row.instrument_id))
    return tuple(rows)


def sum_dividends(rows: Sequence[DividendRow]) -> Decimal:
    """Investor-math dividends total — docs/smallcase/04 §4."""
    return money(sum((row.total for row in rows), Decimal(0)))
