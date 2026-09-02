"""docs/swing/04 §5 — risk per trade decides the size, and every cap says its name."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from baskfy_core.swing.config import SizingConfig
from baskfy_core.swing.sizing import (
    SizeCap,
    SizedPosition,
    SizeRefusal,
    implied_risk_pct,
    r_multiple,
    size_position,
)

CONFIG = SizingConfig()
TEN_LAKH = Decimal("1000000")
WIDE = Decimal("10")


def size(  # noqa: PLR0913 - a test helper mirroring the function under test
    *,
    equity: Decimal = TEN_LAKH,
    cash_available: Decimal = TEN_LAKH,
    entry: Decimal = Decimal("500"),
    stop: Decimal = Decimal("480"),
    avg_turnover_inr: Decimal | None = Decimal("500000000"),
    config: SizingConfig = CONFIG,
) -> SizedPosition:
    return size_position(
        equity=equity,
        cash_available=cash_available,
        entry=entry,
        stop=stop,
        avg_turnover_inr=avg_turnover_inr,
        config=config,
        max_stop_distance_pct=WIDE,
    )


def test_the_worked_example_from_the_method() -> None:
    """Stop 4% below, 0.5% risk → the position is 12.5% of the account."""
    sized = size(entry=Decimal("100"), stop=Decimal("96"))
    assert sized.quantity == 1250  # 1,000,000 x 0.5% / 4
    assert sized.position_pct == Decimal("12.50")
    assert sized.risk_inr == Decimal("5000.00")
    assert sized.cap is SizeCap.RISK
    assert implied_risk_pct(sized, TEN_LAKH) == Decimal("0.50")


def test_a_tight_stop_is_capped_by_the_position_limit_and_says_so() -> None:
    """A 1% stop would put 50% of the account in one name; the 20% cap binds, risk falls."""
    sized = size(entry=Decimal("100"), stop=Decimal("99"))
    assert sized.quantity == 2000  # 20% of 10 lakh at 100
    assert sized.cap is SizeCap.POSITION_PCT
    assert implied_risk_pct(sized, TEN_LAKH) < Decimal("0.5")


def test_cash_on_hand_binds_before_equity_does() -> None:
    sized = size(cash_available=Decimal("50000"))
    assert sized.cap is SizeCap.CASH
    assert sized.position_value <= Decimal("50000")


def test_liquidity_cap_is_one_percent_of_average_turnover() -> None:
    sized = size(avg_turnover_inr=Decimal("2000000"), entry=Decimal("100"), stop=Decimal("96"))
    assert sized.cap is SizeCap.TURNOVER
    assert sized.position_value <= Decimal("20000")


def test_stop_at_or_above_entry_is_refused() -> None:
    assert size(stop=Decimal("500")).refusal is SizeRefusal.STOP_NOT_BELOW_ENTRY
    assert size(stop=Decimal("510")).refusal is SizeRefusal.STOP_NOT_BELOW_ENTRY


def test_a_stop_wider_than_the_method_tolerates_is_refused_not_shrunk() -> None:
    sized = size(entry=Decimal("100"), stop=Decimal("85"))
    assert sized.refusal is SizeRefusal.STOP_TOO_WIDE
    assert sized.quantity == 0
    assert sized.stop_distance_pct == Decimal("15.00")


def test_below_minimum_trade_value_is_refused() -> None:
    tiny = replace(CONFIG, risk_per_trade_pct=0.01)
    sized = size(config=tiny, entry=Decimal("100"), stop=Decimal("96"))
    assert sized.refusal is SizeRefusal.BELOW_MIN_TRADE_VALUE


def test_no_equity_is_a_refusal() -> None:
    assert size(equity=Decimal("0")).refusal is SizeRefusal.NO_EQUITY


def test_r_multiple_is_exit_over_initial_risk() -> None:
    assert r_multiple(
        entry=Decimal("100"), stop=Decimal("96"), exit_price=Decimal("112")
    ) == Decimal("3.00")
    assert r_multiple(
        entry=Decimal("100"), stop=Decimal("96"), exit_price=Decimal("96")
    ) == Decimal("-1.00")
    with pytest.raises(ValueError, match="below entry"):
        r_multiple(entry=Decimal("100"), stop=Decimal("100"), exit_price=Decimal("101"))


@given(
    entry=st.decimals(min_value=Decimal("20"), max_value=Decimal("5000"), places=2),
    distance_pct=st.decimals(min_value=Decimal("0.5"), max_value=Decimal("9.5"), places=2),
    equity=st.decimals(min_value=Decimal("100000"), max_value=Decimal("50000000"), places=0),
)
def test_risk_never_exceeds_the_budget_and_position_never_exceeds_the_cap(
    entry: Decimal, distance_pct: Decimal, equity: Decimal
) -> None:
    stop = (entry * (1 - distance_pct / 100)).quantize(Decimal("0.01"))
    sized = size_position(
        equity=equity,
        cash_available=equity,
        entry=entry,
        stop=stop,
        avg_turnover_inr=None,
        config=CONFIG,
        max_stop_distance_pct=WIDE,
    )
    if sized.refusal is not None:
        assert sized.refusal is SizeRefusal.BELOW_MIN_TRADE_VALUE
        return
    assert sized.risk_inr <= equity * Decimal("0.005") + entry  # one share of slack from floor
    assert sized.position_pct <= Decimal(str(CONFIG.max_position_pct))
    assert sized.quantity > 0
