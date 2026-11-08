"""Recovering corporate actions from the gap between two price series (M28).

The spec is `baskfy_core.action_recovery`'s docstring: the ratio between an adjusted series and
the exchange print is flat between actions and steps at every ex-date, so every step is an action.

These assert the properties that decide whether a recovered action is *true*, because a wrong one
does not fail loudly — it silently rewrites a price history, and a wrong split is exactly the
defect M24 was recovering actions to fix.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from fractions import Fraction

import pytest

from baskfy_core.action_recovery import (
    CASH,
    SHARE_COUNT,
    RecoveredAction,
    ratio_series,
    recover_actions,
    small_ratio,
)

START = dt.date(2026, 1, 1)
DAYS = [START + dt.timedelta(days=n) for n in range(60)]


def series(values: list[float]) -> list[tuple[dt.date, float]]:
    return list(zip(DAYS[: len(values)], values, strict=True))


def with_split(
    count: int, at: int, factor: float, price: float = 100.0
) -> tuple[list[tuple[dt.date, float]], list[tuple[dt.date, float]]]:
    """A raw print that steps down by `factor` at `at`, and the adjusted series beside it.

    The adjusted series is flat: that is what an adjusted series *is* through a split.
    """
    raw = [price if i < at else price / factor for i in range(count)]
    adjusted = [price / factor for _ in range(count)]
    return series(raw), series(adjusted)


class TestFindingTheStep:
    def test_a_split_is_recovered_with_its_date_and_factor(self) -> None:
        raw, adjusted = with_split(40, at=20, factor=5.0)

        actions = recover_actions(raw, adjusted)

        assert len(actions) == 1
        assert actions[0].ex_date == DAYS[20]
        assert actions[0].factor == pytest.approx(5.0)
        assert actions[0].confirmed is True

    def test_a_flat_ratio_recovers_nothing(self) -> None:
        """The common case: 203 of the 268 compared symbols had no unapplied action at all."""
        flat = series([100.0] * 40)
        assert recover_actions(flat, flat) == []

    def test_several_actions_on_one_symbol_are_all_found(self) -> None:
        raw = series([1000.0] * 15 + [500.0] * 15 + [100.0] * 15)
        adjusted = series([100.0] * 45)

        actions = recover_actions(raw, adjusted)

        assert [a.ex_date for a in actions] == [DAYS[15], DAYS[30]]
        assert [round(a.factor, 4) for a in actions] == [2.0, 5.0]

    def test_a_step_below_tolerance_is_rounding_and_not_an_action(self) -> None:
        raw = series([100.0] * 20 + [100.5] * 20)
        assert recover_actions(raw, series([100.0] * 40)) == []


class TestRejectingGlitches:
    def test_a_one_day_spike_is_not_confirmed(self) -> None:
        """A bad bar makes two opposite steps a day apart; neither has a flat flank.

        This is the assertion that keeps a missing bar from becoming an invented split, which
        would be worse than the unadjusted history it was meant to repair.
        """
        raw = series([100.0] * 20 + [300.0] + [100.0] * 19)

        actions = recover_actions(raw, series([100.0] * 40))

        assert len(actions) == 2, "a spike produces two steps"
        assert not any(a.confirmed for a in actions), "and neither may be confirmed"
        # The distinction that matters: these had flanks to check and failed on them, which is
        # what separates a glitch from an action at the edge of the window.
        assert all(a.rejected for a in actions)
        assert not any(a.at_edge for a in actions)

    def test_a_step_at_the_edge_of_the_window_is_unconfirmed_not_discarded(self) -> None:
        """NESTLEIND's 10:1 split sits four days into the observed window. It is real."""
        raw, adjusted = with_split(40, at=2, factor=10.0)

        actions = recover_actions(raw, adjusted)

        assert len(actions) == 1
        assert actions[0].confirmed is False
        assert actions[0].at_edge is True, "unconfirmable for want of history"
        assert actions[0].rejected is False, "which is not the same as disbelieved"
        assert actions[0].factor == pytest.approx(10.0)

    def test_too_few_shared_days_recovers_nothing(self) -> None:
        raw, adjusted = with_split(12, at=6, factor=2.0)
        assert recover_actions(raw, adjusted) == []

    def test_only_shared_dates_are_compared(self) -> None:
        """Pairing across a gap would manufacture a step out of a missing bar."""
        raw = [(DAYS[0], 100.0), (DAYS[1], 100.0), (DAYS[5], 50.0)]
        adjusted = [(DAYS[0], 50.0), (DAYS[1], 50.0), (DAYS[9], 50.0)]

        assert [day for day, _ in ratio_series(raw, adjusted)] == [DAYS[0], DAYS[1]]

    def test_a_zero_price_is_skipped_rather_than_dividing(self) -> None:
        raw = [(DAYS[0], 100.0), (DAYS[1], 0.0)]
        adjusted = [(DAYS[0], 50.0), (DAYS[1], 50.0)]
        assert [day for day, _ in ratio_series(raw, adjusted)] == [DAYS[0]]


class TestClassification:
    @pytest.mark.parametrize(
        ("factor", "expected"),
        [(2.0, Fraction(2)), (5.0, Fraction(5)), (10.0, Fraction(10)), (1.5, Fraction(3, 2))],
    )
    def test_a_share_count_factor_resolves_to_a_small_fraction(
        self, factor: float, expected: Fraction
    ) -> None:
        assert small_ratio(factor) == expected

    @pytest.mark.parametrize("factor", [1.0201, 1.0251, 1.0346, 1.0436])
    def test_a_dividend_shaped_factor_resolves_to_nothing(self, factor: float) -> None:
        """Every one of these is a real corpus dividend: TATASTEEL, HEROMOTOCO, NMDC, CHENNPETRO.

        A dividend's factor is `P / (P - D)`, which is not a ratio of small whole numbers. If one
        of these ever resolved, a dividend would be written as a split and M27's price-return
        verdict would be quietly violated.
        """
        assert small_ratio(factor) is None

    def test_the_measured_ratios_from_the_real_corpus_all_classify(self) -> None:
        recovered = {2.0001: 2, 4.9994: 5, 9.9998: 10, 2.9997: 3, 1.3333: None, 1.2000: None}
        for factor, numerator in recovered.items():
            ratio = small_ratio(factor)
            assert ratio is not None, factor
            if numerator is not None:
                assert ratio.numerator == numerator

    def test_a_recovered_split_becomes_the_ratio_the_adjustment_maths_expects(self) -> None:
        """`split_factor(a, b)` is price `b/a`, so a factor of 5 must be stored as 5:1."""
        action = RecoveredAction(DAYS[0], 5.0, SHARE_COUNT, Fraction(5), True)
        assert action.as_split_ratio() == (Decimal(5), Decimal(1))

        four_thirds = RecoveredAction(DAYS[0], 4 / 3, SHARE_COUNT, Fraction(4, 3), True)
        assert four_thirds.as_split_ratio() == (Decimal(4), Decimal(3))

    def test_a_cash_action_refuses_to_produce_a_share_count_ratio(self) -> None:
        action = RecoveredAction(DAYS[0], 1.02, CASH, None, True)
        with pytest.raises(ValueError, match="cash-shaped"):
            action.as_split_ratio()

    @pytest.mark.parametrize(
        ("ratio", "clean"),
        [
            (Fraction(2), True),
            (Fraction(5), True),
            (Fraction(10), True),
            (Fraction(3, 2), True),
            (Fraction(6, 5), True),
            (Fraction(21, 20), True),
            (Fraction(22, 19), False),  # ITC, Jan 2025 — the ITC Hotels demerger
            (Fraction(21, 16), False),  # SIEMENS — Siemens Energy India
            (Fraction(23, 14), False),  # RAYMOND — Raymond Lifestyle
            (Fraction(170, 11), False),  # LAL — whatever this is, it is not a split
        ],
    )
    def test_an_irregular_ratio_is_marked_rather_than_called_a_split(
        self, ratio: Fraction, clean: bool
    ) -> None:
        """Nothing issues a 19:17. A ratio that is not a split's shape is nearly always a demerger.

        It is still applied — the price step is real — but the row says what it is. M28's first
        write labelled 22 demergers as splits, which is a row lying about where a number came from.
        """
        action = RecoveredAction(DAYS[0], float(ratio), SHARE_COUNT, ratio, True)

        assert action.clean_shape is clean
        assert action.shape == ("split_or_bonus" if clean else "irregular_probably_demerger")
        assert action.share_count is True, "irregular or not, it is still applied"

    def test_the_split_bonus_ambiguity_is_recorded_rather_than_guessed(self) -> None:
        """A 5:1 split and a 4:1 bonus are the same price factor. Price cannot separate them."""
        action = RecoveredAction(DAYS[0], 5.0, SHARE_COUNT, Fraction(5), True)
        assert action.ambiguous_with == "bonus 4:1"


class TestTheSignatureOfARealRecovery:
    def test_a_dividend_is_found_but_classified_as_cash(self) -> None:
        """Both kinds are recovered; only the classification decides what may be written.

        M27 measured that the reference corpus computes a price return, so the cash actions are
        recovered, reported and deliberately not applied.
        """
        raw = series([102.0] * 20 + [100.0] * 20)
        adjusted = series([100.0] * 40)

        actions = recover_actions(raw, adjusted)

        assert len(actions) == 1
        assert actions[0].kind == CASH
        assert actions[0].ratio is None
        assert actions[0].share_count is False
