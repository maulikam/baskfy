"""Curated-basket dividend derivation — docs/smallcase/04 §4, 03 ``cb_dividend``.

Fixtures assert the *spec*: cash CA x qty held on ex-date -> ``total`` rounded half-up
to paise; RECOVERED-ACTIONS ex-dates (TATASTEEL) drive entitlement windows; zero-qty and
out-of-window actions emit nothing. No I/O.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.curated_dividends import (
    DIVIDEND_SOURCE,
    CorporateActionCash,
    HoldingWindow,
    derive_dividends,
    qty_held_on,
    sum_dividends,
)
from baskfy_core.gst import money

# RECOVERED-ACTIONS.md cash/other rows for TATASTEEL (ex-dates only; amounts are cash
# fixtures — recovered actions carry factors, not ₹/share).
_TATA_EX_2024 = dt.date(2024, 6, 21)
_TATA_EX_2025 = dt.date(2025, 6, 6)
_TATA_ID = 101


def _d(value: str) -> Decimal:
    return Decimal(value)


class TestQtyHeldOn:
    def test_closed_window_includes_both_ends(self) -> None:
        windows = [HoldingWindow(dt.date(2024, 1, 1), dt.date(2024, 6, 21), _d("100"))]
        assert qty_held_on(windows, dt.date(2024, 1, 1)) == _d("100")
        assert qty_held_on(windows, _TATA_EX_2024) == _d("100")
        assert qty_held_on(windows, dt.date(2024, 6, 22)) == _d("0")

    def test_overlapping_windows_sum(self) -> None:
        windows = [
            (dt.date(2024, 1, 1), dt.date(2024, 12, 31), _d("40")),
            (dt.date(2024, 6, 1), dt.date(2024, 12, 31), _d("60")),
        ]
        assert qty_held_on(windows, _TATA_EX_2024) == _d("100")


class TestDeriveDividends:
    def test_total_is_amount_times_qty_rounded_half_up(self) -> None:
        # 1.125 * 3 = 3.375 -> 3.38 half-up.
        rows = derive_dividends(
            holdings_history={1: [(dt.date(2024, 1, 1), dt.date(2025, 1, 1), _d("3"))]},
            corporate_actions={1: [(dt.date(2024, 6, 1), _d("1.125"))]},
        )
        assert len(rows) == 1
        assert rows[0].qty_held == _d("3")
        assert rows[0].amount_per_share == _d("1.125")
        assert rows[0].total == money(_d("1.125") * _d("3"))
        assert rows[0].total == _d("3.38")
        assert rows[0].source == DIVIDEND_SOURCE
        assert rows[0].source == "CORPORATE_ACTIONS"

    def test_tatasteel_recovered_ex_dates_entitle_while_held(self) -> None:
        """SC4 AC: derivation tracks RECOVERED-ACTIONS ex-dates for a held name."""
        holdings = {
            _TATA_ID: [
                HoldingWindow(dt.date(2024, 1, 2), dt.date(2025, 12, 31), _d("250")),
            ]
        }
        actions = {
            _TATA_ID: [
                CorporateActionCash(_TATA_EX_2024, _d("3.60")),
                CorporateActionCash(_TATA_EX_2025, _d("3.60")),
            ]
        }
        rows = derive_dividends(holdings, actions)
        assert len(rows) == 2
        assert rows[0].ex_date == _TATA_EX_2024
        assert rows[0].total == _d("900.00")  # 3.60 * 250
        assert rows[1].ex_date == _TATA_EX_2025
        assert rows[1].total == _d("900.00")
        assert sum_dividends(rows) == _d("1800.00")

    def test_sold_before_ex_date_emits_nothing(self) -> None:
        holdings = {
            _TATA_ID: [
                (dt.date(2024, 1, 1), dt.date(2024, 6, 20), _d("250")),
            ]
        }
        actions = {_TATA_ID: [(_TATA_EX_2024, _d("3.60"))]}
        assert derive_dividends(holdings, actions) == ()

    def test_bought_on_ex_date_still_counts_closed_window(self) -> None:
        # Closed interval: from_date == ex_date means held that day (writer's contract).
        holdings = {_TATA_ID: [(_TATA_EX_2024, dt.date(2024, 12, 31), _d("10"))]}
        actions = {_TATA_ID: [(_TATA_EX_2024, _d("2.00"))]}
        rows = derive_dividends(holdings, actions)
        assert len(rows) == 1
        assert rows[0].total == _d("20.00")

    def test_instrument_without_holdings_is_skipped(self) -> None:
        rows = derive_dividends(
            holdings_history={},
            corporate_actions={1: [(dt.date(2024, 6, 1), _d("1.00"))]},
        )
        assert rows == ()

    def test_zero_amount_or_zero_qty_omitted(self) -> None:
        rows = derive_dividends(
            holdings_history={
                1: [(dt.date(2024, 1, 1), dt.date(2024, 12, 31), _d("0"))],
                2: [(dt.date(2024, 1, 1), dt.date(2024, 12, 31), _d("10"))],
            },
            corporate_actions={
                1: [(dt.date(2024, 6, 1), _d("5.00"))],
                2: [(dt.date(2024, 6, 1), _d("0"))],
            },
        )
        assert rows == ()

    def test_output_sorted_by_ex_date_then_instrument(self) -> None:
        rows = derive_dividends(
            holdings_history={
                2: [(dt.date(2024, 1, 1), dt.date(2025, 1, 1), _d("1"))],
                1: [(dt.date(2024, 1, 1), dt.date(2025, 1, 1), _d("1"))],
            },
            corporate_actions={
                2: [(dt.date(2024, 3, 1), _d("1.00"))],
                1: [
                    (dt.date(2024, 6, 1), _d("1.00")),
                    (dt.date(2024, 3, 1), _d("2.00")),
                ],
            },
        )
        assert [(r.ex_date, r.instrument_id) for r in rows] == [
            (dt.date(2024, 3, 1), 1),
            (dt.date(2024, 3, 1), 2),
            (dt.date(2024, 6, 1), 1),
        ]

    def test_rejects_negative_qty(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            HoldingWindow(dt.date(2024, 1, 1), dt.date(2024, 2, 1), _d("-1"))

    def test_rejects_inverted_window(self) -> None:
        with pytest.raises(ValueError, match="ends before"):
            HoldingWindow(dt.date(2024, 6, 1), dt.date(2024, 1, 1), _d("1"))
