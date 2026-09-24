"""``docs/fno/04`` §3 and §10 — lots from the risk budget, never from margin, never rounded up."""

from __future__ import annotations

from decimal import Decimal

from baskfy_core.fno.config import (
    DEFAULT_FNO_CEILINGS,
    F1_SEED_CAPITAL_INR,
    CommonConfig,
    PlanState,
)
from baskfy_core.fno.sizing import MarginVerdict, margin_check, risk_budget, size
from baskfy_core.options.config import Mode, SizingMode

COMMON = CommonConfig()

#: A ₹10 lakh sleeve: ₹10,000 at 1 %. The arithmetic below is written against it; the seeded
#: F1 capital is ₹25 lakh (M.2) and is pinned in test_fno_config.
TEN_LAKH = Decimal(1_000_000)


def test_f1_budget_is_ten_thousand_on_ten_lakh() -> None:
    assert risk_budget(TEN_LAKH, Decimal("1.0"), DEFAULT_FNO_CEILINGS) == Decimal(10000)


def test_the_seeded_f1_budget_is_the_per_trade_ceiling() -> None:
    # M.2: ₹25 lakh at 1 % = ₹25,000, exactly BASKFY_FNO_RISK_PER_TRADE_INR_MAX.
    assert risk_budget(F1_SEED_CAPITAL_INR, Decimal("1.0"), DEFAULT_FNO_CEILINGS) == Decimal(25000)


def test_the_per_trade_ceiling_caps_the_budget() -> None:
    assert risk_budget(Decimal(10_000_000), Decimal("1.0"), DEFAULT_FNO_CEILINGS) == Decimal(25000)


def test_the_pct_ceiling_caps_the_pct() -> None:
    assert risk_budget(Decimal(1_000_000), Decimal("2.0"), DEFAULT_FNO_CEILINGS) == Decimal(10000)


def test_lots_are_floored() -> None:
    # BANKNIFTY lot 30, max loss 150/unit → ₹4,500 a lot; ₹10,000 → 2.22 → 2 lots.
    s = size(
        mode=Mode.LIVE, capital_inr=TEN_LAKH, risk_pct=Decimal("1.0"),
        risk_per_unit=Decimal(150), lot_size=30, common=COMMON, ceilings=DEFAULT_FNO_CEILINGS,
    )  # fmt: skip
    assert (s.lots, s.state, s.max_loss_inr) == (2, None, Decimal(9000))


def test_lots_are_capped_at_fo_max_lots() -> None:
    s = size(
        mode=Mode.LIVE, capital_inr=TEN_LAKH, risk_pct=Decimal("1.0"),
        risk_per_unit=Decimal(10), lot_size=30, common=COMMON, ceilings=DEFAULT_FNO_CEILINGS,
    )  # fmt: skip
    assert s.lots == 2
    wider = CommonConfig(max_lots=10)
    s = size(
        mode=Mode.LIVE, capital_inr=TEN_LAKH, risk_pct=Decimal("1.0"),
        risk_per_unit=Decimal(10), lot_size=30, common=wider, ceilings=DEFAULT_FNO_CEILINGS,
    )  # fmt: skip
    assert s.lots == 10


def test_zero_lots_is_rejected_size_never_rounded_up() -> None:
    # One lot risks ₹10,500 > ₹10,000: 0.95 lots → REJECTED_SIZE, not 1.
    s = size(
        mode=Mode.LIVE, capital_inr=TEN_LAKH, risk_pct=Decimal("1.0"),
        risk_per_unit=Decimal(350), lot_size=30, common=COMMON, ceilings=DEFAULT_FNO_CEILINGS,
    )  # fmt: skip
    assert (s.lots, s.state) == (0, PlanState.REJECTED_SIZE)
    assert "0 lots" in s.message


def test_f2_most_names_size_to_zero_under_the_ceiling() -> None:
    # 3 x ATR = ₹120 a share, lot 500 → ₹60,000 a lot; even ₹25,000 cannot buy one.
    s = size(
        mode=Mode.LIVE, capital_inr=Decimal(100_000_000), risk_pct=Decimal("1.0"),
        risk_per_unit=Decimal(120), lot_size=500, common=COMMON, ceilings=DEFAULT_FNO_CEILINGS,
    )  # fmt: skip
    assert s.state is PlanState.REJECTED_SIZE


def test_capital_zero_is_one_paper_lot_and_live_refuses() -> None:
    paper = size(
        mode=Mode.PAPER, capital_inr=Decimal(0), risk_pct=Decimal("1.0"),
        risk_per_unit=Decimal(120), lot_size=500, common=COMMON, ceilings=DEFAULT_FNO_CEILINGS,
    )  # fmt: skip
    assert (paper.lots, paper.sizing_mode, paper.state) == (1, SizingMode.PAPER_ONE_LOT, None)
    assert paper.max_loss_inr == Decimal(60000)
    assert paper.lots_at_ceiling == 0  # what live would have been: recorded, not traded
    live = size(
        mode=Mode.LIVE, capital_inr=Decimal(0), risk_pct=Decimal("1.0"),
        risk_per_unit=Decimal(120), lot_size=500, common=COMMON, ceilings=DEFAULT_FNO_CEILINGS,
    )  # fmt: skip
    assert (live.lots, live.state) == (0, PlanState.NO_SLEEVE_CAPITAL)


def test_paper_with_capital_sizes_exactly_as_live() -> None:
    paper, live = (
        size(
            mode=mode, capital_inr=TEN_LAKH, risk_pct=Decimal("1.0"),
            risk_per_unit=Decimal(150), lot_size=30, common=COMMON, ceilings=DEFAULT_FNO_CEILINGS,
        )
        for mode in (Mode.PAPER, Mode.LIVE)
    )  # fmt: skip
    assert paper == live


def test_no_lot_size_is_rejected() -> None:
    s = size(
        mode=Mode.LIVE, capital_inr=TEN_LAKH, risk_pct=Decimal("1.0"),
        risk_per_unit=Decimal(150), lot_size=None, common=COMMON, ceilings=DEFAULT_FNO_CEILINGS,
    )  # fmt: skip
    assert s.state is PlanState.REJECTED_SIZE


def test_margin_only_rejects() -> None:
    assert margin_check(Decimal(90000), Decimal(100000)).verdict is MarginVerdict.OK
    assert margin_check(Decimal(100001), Decimal(100000)).verdict is MarginVerdict.REJECTED_MARGIN
