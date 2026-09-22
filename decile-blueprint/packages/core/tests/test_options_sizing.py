"""``docs/options/04`` §7 — lots from a risk budget, paper-one-lot, ceilings, margin a ceiling."""

from __future__ import annotations

from decimal import Decimal

import pytest
from options_fixtures import LOT

from baskfy_core.options.config import (
    DEFAULT_CEILINGS,
    DEFAULT_OPTIONS_CONFIG,
    Mode,
    OptionsCeilings,
    SizingMode,
    Sleeve,
)
from baskfy_core.options.sizing import (
    MarginCode,
    Sizing,
    SizingReject,
    gap_through_long,
    margin_check,
    premium_cap_ok,
    risk_per_lot_condor,
    risk_per_lot_debit_spread,
    risk_per_lot_long,
    size,
)

CFG = DEFAULT_OPTIONS_CONFIG
#: O1-M: width 150, credit 40 → (150 - 40) x 65 + 1,000 = 8,150 a lot.
CONDOR_LOT = risk_per_lot_condor(Decimal(150), Decimal(40), LOT, Decimal(1000))


def _size(  # noqa: PLR0913 - every sizing input, by keyword
    capital: int | str,
    *,
    mode: Mode = Mode.LIVE,
    sleeve: Sleeve = Sleeve.O1M,
    real_rows: int = 5,
    lot_size: int | None = LOT,
    risk_per_lot: Decimal = CONDOR_LOT,
    pct: Decimal | None = None,
    max_lots: int | None = None,
    ceilings: OptionsCeilings = DEFAULT_CEILINGS,
) -> Sizing:
    return size(
        mode=mode,
        sleeve_capital_inr=Decimal(capital),
        risk_per_trade_pct=pct if pct is not None else CFG.risk_per_trade_pct(sleeve),
        max_lots=max_lots if max_lots is not None else CFG.max_lots(sleeve),
        risk_per_lot_inr=risk_per_lot,
        lot_size=lot_size,
        real_journal_rows=real_rows,
        config=CFG.sizing,
        ceilings=ceilings,
    )


class TestRiskPerLot:
    def test_condor(self) -> None:
        assert Decimal(8150) == CONDOR_LOT

    def test_long_option_and_its_gap_through(self) -> None:
        """``04`` §4.6: 200.05 x 0.30 x 65 + 300; the gap-through worst case is the premium."""
        assert risk_per_lot_long(Decimal("200.05"), Decimal("0.30"), LOT, Decimal(300)) == Decimal(
            "4200.975"
        )
        assert gap_through_long(Decimal("200.05"), LOT) == Decimal("13003.25")

    def test_debit_spread(self) -> None:
        assert risk_per_lot_debit_spread(Decimal(45), LOT, Decimal(500)) == Decimal(3425)


class TestPaperOneLot:
    """``04`` §7.3 / PACK.6."""

    def test_capital_zero_in_paper_is_one_lot_and_r_is_one_lots_risk(self) -> None:
        got = _size(0, mode=Mode.PAPER)
        assert (got.lots, got.sizing_mode, got.r_inr, got.rejection) == (
            1, SizingMode.PAPER_ONE_LOT, CONDOR_LOT, None,
        )  # fmt: skip
        assert not got.half_size

    def test_capital_zero_live_is_refused(self) -> None:
        got = _size(0, mode=Mode.LIVE)
        assert got.rejection is SizingReject.REJECTED_NO_SLEEVE_CAPITAL
        assert got.lots == 0

    @pytest.mark.parametrize("lot_size", [None, 0])
    def test_no_lot_size_is_refused_first(self, lot_size: int | None) -> None:
        assert (
            _size(0, mode=Mode.PAPER, lot_size=lot_size).rejection
            is SizingReject.REJECTED_NO_LOT_SIZE
        )


class TestBudget:
    """``04`` §7.1-7.2."""

    def test_budget_too_small_names_the_arithmetic(self) -> None:
        got = _size(500_000)  # 1 % = 5,000 < 8,150
        assert got.rejection is SizingReject.REJECTED_BUDGET
        assert "5000.00" in got.message
        assert "8150.00" in got.message

    def test_floor_of_budget_over_risk_per_lot(self) -> None:
        got = _size(2_000_000)  # 20,000 / 8,150 = 2.45
        assert (got.lots, got.sizing_mode, got.r_inr) == (2, SizingMode.BUDGET, Decimal(20000))

    def test_the_inr_ceiling_caps_the_budget(self) -> None:
        got = _size(10_000_000)  # 1 % = 1,00,000 → capped at 25,000 → 3.07 → 3
        assert got.risk_budget_inr == DEFAULT_CEILINGS.risk_per_trade_inr_max
        assert got.lots == 3

    def test_the_sleeves_max_lots_caps(self) -> None:
        assert _size(10_000_000, sleeve=Sleeve.O1W, pct=Decimal("1.0")).lots == 2

    def test_the_system_max_lots_caps(self) -> None:
        cheap = Decimal(1000)
        assert (
            _size(10_000_000, risk_per_lot=cheap, max_lots=50).lots == DEFAULT_CEILINGS.max_lots_max
        )

    def test_a_pct_above_the_ceiling_is_clamped(self) -> None:
        got = _size(1_000_000, pct=Decimal("3.0"), risk_per_lot=Decimal(1000), max_lots=50)
        assert got.risk_budget_inr == Decimal(10000)

    def test_zero_risk_per_lot_is_a_bug(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            _size(1_000_000, risk_per_lot=Decimal(0))


class TestFirstLive:
    """``04`` §7.5: fewer than five real rows → budget x 0.5, tagged half_size."""

    def test_four_real_rows_halve_the_budget(self) -> None:
        got = _size(2_000_000, real_rows=4)
        assert got.half_size
        assert got.risk_budget_inr == Decimal(10000)
        assert got.lots == 1

    def test_five_real_rows_lift_it(self) -> None:
        got = _size(2_000_000, real_rows=5)
        assert not got.half_size
        assert got.risk_budget_inr == Decimal(20000)


class TestPremiumCap:
    """``04`` §4.6: lots x E x lot ≤ 10 % of capital, when capital > 0."""

    def test_boundary(self) -> None:
        cap = CFG.directional.premium_cap_pct
        assert premium_cap_ok(1, Decimal(150), LOT, Decimal(100_000), cap)  # 9,750
        assert premium_cap_ok(1, Decimal("153.84"), LOT, Decimal(100_000), cap)  # 9,999.60
        assert not premium_cap_ok(1, Decimal(200), LOT, Decimal(100_000), cap)  # 13,000

    def test_no_capital_nothing_to_cap(self) -> None:
        assert premium_cap_ok(1, Decimal(500), LOT, Decimal(0), CFG.directional.premium_cap_pct)


class TestMargin:
    """``04`` §7.4 — a ceiling, never a source."""

    def test_pool_unset_in_paper_warns(self) -> None:
        got = margin_check(
            mode=Mode.PAPER, hedged_estimate_inr=Decimal(90000), transient_estimate_inr=None,
            margin_pool_inr=Decimal(0), margin_in_use_inr=Decimal(0),
        )  # fmt: skip
        assert got.code is MarginCode.MARGIN_POOL_UNSET
        assert not got.rejected
        assert "90000.00" in got.message

    def test_pool_unset_live_rejects(self) -> None:
        got = margin_check(
            mode=Mode.LIVE, hedged_estimate_inr=Decimal(90000), transient_estimate_inr=None,
            margin_pool_inr=Decimal(0), margin_in_use_inr=Decimal(0),
        )  # fmt: skip
        assert got.rejected

    def test_fits_under_the_free_pool(self) -> None:
        got = margin_check(
            mode=Mode.LIVE, hedged_estimate_inr=Decimal(150000), transient_estimate_inr=None,
            margin_pool_inr=Decimal(200000), margin_in_use_inr=Decimal(50000),
        )  # fmt: skip
        assert got.code is MarginCode.OK
        assert got.available_inr == Decimal(150000)

    def test_hedged_above_the_free_pool(self) -> None:
        got = margin_check(
            mode=Mode.LIVE, hedged_estimate_inr=Decimal("150000.01"), transient_estimate_inr=None,
            margin_pool_inr=Decimal(200000), margin_in_use_inr=Decimal(50000),
        )  # fmt: skip
        assert got.rejected
        assert "hedged" in got.message

    def test_the_transient_figure_counts(self) -> None:
        got = margin_check(
            mode=Mode.PAPER, hedged_estimate_inr=Decimal(100000),
            transient_estimate_inr=Decimal(180000),
            margin_pool_inr=Decimal(200000), margin_in_use_inr=Decimal(50000),
        )  # fmt: skip
        assert got.rejected
        assert "transient" in got.message
