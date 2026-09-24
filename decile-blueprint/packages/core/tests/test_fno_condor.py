"""``docs/fno/04`` §2 — F1's strikes, credit, max loss, liquidity by name, sequences, and §1's
profit take and loss close."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from baskfy_core.fno.condor import (
    ENTRY_SEQUENCE,
    EXIT_SEQUENCE,
    CondorStrikes,
    LegQuote,
    LegRole,
    cost_to_close,
    credit,
    liquidity_refusals,
    loss_close_hit,
    loss_close_level,
    max_loss_per_unit,
    one_sd,
    price_structure,
    profit_take_hit,
    profit_take_level,
    select_strikes,
    sign_of,
)
from baskfy_core.fno.config import F1Config, PlanState

F1 = F1Config()
F = Decimal(25000)
IV = 0.12
DAYS = 21
STRIKES = [Decimal(k) for k in range(22000, 28001, 100)]


def _sd() -> Decimal:
    return F * Decimal(repr(IV * math.sqrt(DAYS / 365)))


def test_sd_is_f_sigma_root_t() -> None:
    assert one_sd(F, IV, DAYS) == _sd()


def test_strikes_follow_the_table() -> None:
    sd = _sd()  # ≈ 719.6
    choice = select_strikes(
        forward=F, iv=IV, days_to_expiry=DAYS, call_strikes=STRIKES, put_strikes=STRIKES, config=F1
    )
    assert choice.strikes is not None
    s = choice.strikes
    assert s.short_call == min(k for k in STRIKES if k >= F + sd)
    assert s.long_call == min(k for k in STRIKES if k >= F + Decimal("1.5") * sd)
    assert s.short_put == max(k for k in STRIKES if k <= F - sd)
    assert s.long_put == max(k for k in STRIKES if k <= F - Decimal("1.5") * sd)
    assert (s.long_call, s.short_call, s.short_put, s.long_put) == (
        Decimal(26100), Decimal(25800), Decimal(24200), Decimal(23900),
    )  # fmt: skip


def test_a_wing_equal_to_its_short_is_rejected_structure() -> None:
    coarse = [Decimal(k) for k in range(20000, 30001, 2500)]
    choice = select_strikes(
        forward=F, iv=IV, days_to_expiry=DAYS, call_strikes=coarse, put_strikes=coarse, config=F1
    )
    assert choice.strikes is None
    assert choice.state is PlanState.REJECTED_STRUCTURE
    assert any("wing equals the short" in r for r in choice.reasons)


def test_no_listed_strike_is_rejected_by_name() -> None:
    narrow = [Decimal(k) for k in range(24500, 25501, 100)]
    choice = select_strikes(
        forward=F, iv=IV, days_to_expiry=DAYS, call_strikes=narrow, put_strikes=narrow, config=F1
    )
    assert choice.state is PlanState.REJECTED_STRUCTURE
    assert "no listed strike for the long call" in choice.reasons


STRUCT = CondorStrikes(Decimal(26100), Decimal(25800), Decimal(24200), Decimal(23900))
MIDS = {
    LegRole.LONG_CALL: Decimal("20"),
    LegRole.SHORT_CALL: Decimal("45"),
    LegRole.SHORT_PUT: Decimal("60"),
    LegRole.LONG_PUT: Decimal("30"),
}


def test_credit_and_max_loss() -> None:
    assert credit(MIDS) == Decimal(55)
    assert max_loss_per_unit(STRUCT, Decimal(55)) == Decimal(245)
    priced = price_structure(STRUCT, MIDS)
    assert priced.state is None
    assert (priced.credit, priced.max_loss_per_unit) == (Decimal(55), Decimal(245))


def test_uneven_widths_take_the_wider() -> None:
    uneven = CondorStrikes(Decimal(26200), Decimal(25800), Decimal(24200), Decimal(23900))
    assert max_loss_per_unit(uneven, Decimal(55)) == Decimal(345)


def test_a_non_positive_credit_is_rejected() -> None:
    bad = dict(MIDS)
    bad[LegRole.LONG_PUT] = Decimal(90)
    assert price_structure(STRUCT, bad).state is PlanState.REJECTED_STRUCTURE


def test_sequences_are_longs_first_in_and_shorts_first_out() -> None:
    assert ENTRY_SEQUENCE == (
        LegRole.LONG_PUT, LegRole.LONG_CALL, LegRole.SHORT_PUT, LegRole.SHORT_CALL,
    )  # fmt: skip
    assert EXIT_SEQUENCE == (
        LegRole.SHORT_CALL, LegRole.SHORT_PUT, LegRole.LONG_CALL, LegRole.LONG_PUT,
    )  # fmt: skip
    assert [sign_of(r) for r in ENTRY_SEQUENCE] == [1, 1, -1, -1]


class TestLiquidity:
    good = LegQuote(Decimal("59"), Decimal("61"), 900)

    def _quotes(self, **override: LegQuote) -> dict[LegRole, LegQuote]:
        quotes = {role: self.good for role in LegRole}
        for name, quote in override.items():
            quotes[LegRole[name]] = quote
        return quotes

    def test_all_good_is_empty(self) -> None:
        assert liquidity_refusals(STRUCT, self._quotes(), F1) == ()

    def test_short_oi_below_500_lots_is_named(self) -> None:
        found = liquidity_refusals(
            STRUCT, self._quotes(SHORT_PUT=LegQuote(Decimal(59), Decimal(61), 499)), F1
        )
        assert found == ("short put 24200: OI 499 lots < 500",)

    def test_short_spread_above_five_percent_is_named(self) -> None:
        found = liquidity_refusals(
            STRUCT, self._quotes(SHORT_CALL=LegQuote(Decimal(57), Decimal(63), 900)), F1
        )
        assert len(found) == 1
        assert found[0].startswith("short call 25800: spread 10.00 %")

    def test_exactly_five_percent_passes(self) -> None:
        q = LegQuote(Decimal("58.5"), Decimal("61.5"), 900)
        assert liquidity_refusals(STRUCT, self._quotes(SHORT_CALL=q), F1) == ()

    def test_a_wing_needs_a_two_sided_quote_but_not_oi(self) -> None:
        assert (
            liquidity_refusals(
                STRUCT, self._quotes(LONG_CALL=LegQuote(Decimal(1), Decimal(2), 0)), F1
            )
            == ()
        )
        found = liquidity_refusals(
            STRUCT, self._quotes(LONG_CALL=LegQuote(None, Decimal(2), 0)), F1
        )
        assert found == ("long call 26100: no two-sided quote",)


class TestExitPredicates:
    def test_profit_take_at_half_the_credit(self) -> None:
        assert profit_take_level(Decimal(55), F1) == Decimal("27.5")
        assert profit_take_hit(Decimal("27.5"), Decimal(55), F1)
        assert not profit_take_hit(Decimal("27.6"), Decimal(55), F1)

    def test_loss_close_at_two_and_a_half_credits(self) -> None:
        assert loss_close_level(Decimal(55), STRUCT, F1) == Decimal("137.5")
        assert loss_close_hit(Decimal("137.5"), Decimal(55), STRUCT, F1)
        assert not loss_close_hit(Decimal("137.4"), Decimal(55), STRUCT, F1)

    def test_loss_close_never_widens_past_the_wings(self) -> None:
        # (1 + 1.5) x 150 = 375 > width 300: the level is the width, the max loss.
        assert loss_close_level(Decimal(150), STRUCT, F1) == Decimal(300)

    def test_cost_to_close_is_clamped_to_each_width(self) -> None:
        marks = {
            LegRole.SHORT_CALL: Decimal(900), LegRole.LONG_CALL: Decimal(100),
            LegRole.SHORT_PUT: Decimal(1), LegRole.LONG_PUT: Decimal(3),
        }  # fmt: skip
        assert cost_to_close(STRUCT, marks) == Decimal(300)
        # So the realised loss per unit is never beyond max loss.
        assert cost_to_close(STRUCT, marks) - Decimal(55) <= max_loss_per_unit(STRUCT, Decimal(55))


@pytest.mark.parametrize("pct", [Decimal(30), Decimal(80)])
def test_profit_take_follows_the_setting(pct: Decimal) -> None:
    cfg = F1Config(profit_take_pct=pct)
    assert profit_take_level(Decimal(100), cfg) == Decimal(100) - pct
