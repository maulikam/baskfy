"""The Console tradebook reader and the FIFO history it feeds (NEEDS-MAULIK §32, 14 Sep 2026).

Asserts the spec: a file is read completely or refused with the line, equity only, and a purchase
date is claimed only when the trades add up to what is held.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.tradebook import (
    TradebookError,
    TradeFill,
    TradeSide,
    history_for,
    parse_tradebook_csv,
    xirr_since_first_purchase,
)

HEADER = (
    "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,"
    "trade_id,order_id,order_execution_time\n"
)


def _csv(*rows: str) -> str:
    return HEADER + "\n".join(rows) + "\n"


def test_reads_a_console_export_with_decimals_and_ist_times() -> None:
    parsed = parse_tradebook_csv(
        _csv(
            "PWL,INE0,2025-02-03,NSE,EQ,EQ,buy,false,100.000000,412.35,T1,O1,2025-02-03T10:01:02",
            "PWL,INE0,2025-03-04,NSE,EQ,EQ,sell,false,40.000000,455.10,T2,O2,2025-03-04T14:00:00",
        )
    )
    assert parsed.skipped_non_equity == 0
    first, second = parsed.fills
    assert first.side is TradeSide.BUY
    assert first.quantity == Decimal("100")
    assert first.price == Decimal("412.35")
    assert first.trade_date == dt.date(2025, 2, 3)
    assert first.executed_at == dt.datetime(2025, 2, 3, 10, 1, 2)
    assert second.side is TradeSide.SELL
    assert second.trade_id == "T2"


def test_derivative_rows_are_counted_and_never_read_as_shares() -> None:
    parsed = parse_tradebook_csv(
        _csv(
            "NIFTY25FEBFUT,,2025-02-03,NFO,FO,,buy,false,75,23000,F1,O1,",
            "TCS,INE1,2025-02-03,NSE,EQ,EQ,buy,false,1,4000,T1,O2,",
        )
    )
    assert [fill.symbol for fill in parsed.fills] == ["TCS"]
    assert parsed.skipped_non_equity == 1


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        ("TCS,,2025-02-03,NSE,EQ,EQ,hold,false,1,4000,T1,O1,", "trade type"),
        ("TCS,,2025-02-03,NSE,EQ,EQ,buy,false,one,4000,T1,O1,", "quantity"),
        ("TCS,,2025-02-03,NSE,EQ,EQ,buy,false,1,,T1,O1,", "price"),
        ("TCS,,someday,NSE,EQ,EQ,buy,false,1,4000,T1,O1,", "trade date"),
        ("TCS,,2025-02-03,NSE,EQ,EQ,buy,false,1,4000,,O1,", "trade id"),
        ("TCS,,2025-02-03,NSE,EQ,EQ,buy,false,1,4,000,T1,O1,", "more fields"),
    ],
)
def test_an_unreadable_row_refuses_the_whole_file_with_its_line(row: str, fragment: str) -> None:
    with pytest.raises(TradebookError, match="line 2") as refused:
        parse_tradebook_csv(_csv(row))
    assert fragment in str(refused.value)


def test_a_file_without_a_trade_id_column_is_refused() -> None:
    with pytest.raises(TradebookError, match="trade_id"):
        parse_tradebook_csv("symbol,trade_date,trade_type,quantity,price\nTCS,2025-01-01,buy,1,1\n")


def _fill(side: TradeSide, qty: str, price: str, on: dt.date, tid: str) -> TradeFill:
    return TradeFill(
        symbol="PWL",
        exchange="NSE",
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        trade_date=on,
        trade_id=tid,
    )


def test_fifo_dates_the_holding_from_its_oldest_open_lot() -> None:
    fills = [
        _fill(TradeSide.BUY, "100", "400", dt.date(2024, 1, 10), "a"),
        _fill(TradeSide.BUY, "50", "500", dt.date(2024, 6, 10), "b"),
        _fill(TradeSide.SELL, "100", "600", dt.date(2025, 1, 10), "c"),
    ]
    history = history_for(fills, held_quantity=Decimal("50"))
    assert history.reconciles is True
    assert history.first_bought_on == dt.date(2024, 6, 10)
    assert history.open_cost == Decimal("25000")
    assert history.first_trade_on == dt.date(2024, 1, 10)


def test_a_holding_the_trades_do_not_add_up_to_gets_no_purchase_date() -> None:
    """A bonus issue or a transfer-in: 150 held, trades say 100. No date is claimed."""
    fills = [_fill(TradeSide.BUY, "100", "400", dt.date(2024, 1, 10), "a")]
    history = history_for(fills, held_quantity=Decimal("150"))
    assert history.reconciles is False
    assert history.first_bought_on is None
    assert history.open_cost is None


def test_a_sell_with_no_buy_in_the_file_is_history_that_starts_mid_position() -> None:
    fills = [_fill(TradeSide.SELL, "10", "400", dt.date(2024, 1, 10), "a")]
    history = history_for(fills, held_quantity=Decimal("0"))
    assert history.unmatched_sell_quantity == Decimal("10")
    assert history.reconciles is False


def test_same_day_buy_then_sell_is_not_a_short() -> None:
    day = dt.date(2024, 1, 10)
    fills = [
        _fill(TradeSide.SELL, "10", "410", day, "z"),
        _fill(TradeSide.BUY, "10", "400", day, "a"),
    ]
    history = history_for(fills, held_quantity=Decimal("0"))
    assert history.unmatched_sell_quantity == Decimal("0")
    assert history.reconciles is True


def test_xirr_from_trades_and_closing_value() -> None:
    """Bought 1,00,000 a year ago; worth 1,10,000 today: about 10% a year."""
    fills = [_fill(TradeSide.BUY, "100", "1000", dt.date(2025, 1, 1), "a")]
    rate = xirr_since_first_purchase(
        fills, closing_value=Decimal("110000"), as_of=dt.date(2026, 1, 1)
    )
    assert rate is not None
    assert abs(rate - Decimal("0.10")) < Decimal("0.001")
