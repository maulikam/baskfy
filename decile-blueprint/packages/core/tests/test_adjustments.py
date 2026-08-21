"""The adjustment algorithm asserts docs/09 §"Adjustment algorithm", not our output.

The table it specifies:

    Split  a:b       price b/a        volume x a/b
    Bonus  a:b       price b/(a+b)    volume x (a+b)/b
    Rights           (P_cum - value of the right) / P_cum      volume unchanged
    Dividend D       (P_cum - D) / P_cum                       volume unchanged

with ``a = ratio_from`` and ``b = ratio_to`` per docs/04's orientation (split 10:1 -> from=10).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest

from baskfy_core.adjustments import (
    AdjustmentOutcome,
    CorporateActionInput,
    adjust_bars,
    bonus_factor,
    dividend_factor,
    resolve_action,
    rights_factor,
    split_factor,
)

EX_DATE = dt.date(2026, 3, 9)


def bars(closes: dict[dt.date, str], volume: int = 1000) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "date": day,
                "open_raw": Decimal(value),
                "high_raw": Decimal(value),
                "low_raw": Decimal(value),
                "close_raw": Decimal(value),
                "volume_raw": volume,
            }
            for day, value in sorted(closes.items())
        ]
    )


class TestFactorFormulas:
    def test_split_ten_to_one(self) -> None:
        """docs/09: split a:b -> price b/a, volume a/b. A 10:1 split decimates the price."""
        price, volume = split_factor(Decimal(10), Decimal(1))
        assert price == Decimal("0.1")
        assert volume == Decimal(10)

    def test_bonus_four_to_one(self) -> None:
        """docs/09: bonus a:b -> price b/(a+b). Four new for one held leaves five shares."""
        price, volume = bonus_factor(Decimal(4), Decimal(1))
        assert price == Decimal("0.2")
        assert volume == Decimal(5)

    def test_bonus_one_to_one(self) -> None:
        price, volume = bonus_factor(Decimal(1), Decimal(1))
        assert price == Decimal("0.5")
        assert volume == Decimal(2)

    def test_dividend(self) -> None:
        """docs/09: (P_cum - D) / P_cum."""
        assert dividend_factor(Decimal(100), Decimal(10)) == Decimal("0.9")

    def test_a_dividend_at_or_above_the_price_is_refused(self) -> None:
        """A factor of zero would erase every price before the ex-date."""
        with pytest.raises(ValueError, match="not less than"):
            dividend_factor(Decimal(10), Decimal(10))

    def test_rights_below_market_reduces_prior_prices(self) -> None:
        """1 new at 50 for every 1 held at 100 -> TERP 75, so the factor is 0.75."""
        assert rights_factor(Decimal(100), Decimal(1), Decimal(1), Decimal(50)) == Decimal("0.75")

    def test_rights_at_or_above_market_is_worthless(self) -> None:
        """A right priced above the market has no value; a negative one would *raise* history."""
        assert rights_factor(Decimal(100), Decimal(1), Decimal(1), Decimal(120)) == Decimal(1)

    @pytest.mark.parametrize("leg", [Decimal(0), Decimal(-1)])
    def test_non_positive_ratio_legs_are_refused(self, leg: Decimal) -> None:
        with pytest.raises(ValueError, match="positive"):
            split_factor(leg, Decimal(1))


class TestRefusalToGuess:
    """An action we cannot quantify must leave the series alone and say so.

    A wrong factor silently rewrites every price before the ex-date, and nothing in the resulting
    series reveals it — which is exactly why these return INSUFFICIENT_DATA rather than a default.
    """

    def test_a_split_without_a_ratio(self) -> None:
        result = resolve_action(CorporateActionInput("split", EX_DATE), Decimal(100))
        assert result.outcome is AdjustmentOutcome.INSUFFICIENT_DATA
        assert result.price_factor == Decimal(1)

    def test_a_dividend_without_an_amount(self) -> None:
        result = resolve_action(CorporateActionInput("dividend", EX_DATE), Decimal(100))
        assert result.outcome is AdjustmentOutcome.INSUFFICIENT_DATA

    def test_a_dividend_with_no_cum_close(self) -> None:
        result = resolve_action(CorporateActionInput("dividend", EX_DATE, amount=Decimal(5)), None)
        assert result.outcome is AdjustmentOutcome.INSUFFICIENT_DATA

    def test_a_rights_issue_without_a_subscription_price(self) -> None:
        """NSE's free-text purpose usually omits it, and TERP is undefined without it."""
        result = resolve_action(
            CorporateActionInput("rights", EX_DATE, Decimal(1), Decimal(1)), Decimal(100)
        )
        assert result.outcome is AdjustmentOutcome.INSUFFICIENT_DATA
        assert "subscription price" in result.detail

    def test_a_demerger_is_not_price_affecting_here(self) -> None:
        """docs/09's table gives no demerger formula; the entitlement ratio is per-case."""
        result = resolve_action(CorporateActionInput("demerger", EX_DATE), Decimal(100))
        assert result.outcome is AdjustmentOutcome.NOT_PRICE_AFFECTING

    def test_an_unknown_action_type_is_inert(self) -> None:
        result = resolve_action(CorporateActionInput("buyback", EX_DATE), Decimal(100))
        assert result.outcome is AdjustmentOutcome.NOT_PRICE_AFFECTING
        assert result.price_factor == Decimal(1)

    def test_an_impossible_ratio_is_invalid_not_a_crash(self) -> None:
        result = resolve_action(
            CorporateActionInput("split", EX_DATE, Decimal(0), Decimal(1)), Decimal(100)
        )
        assert result.outcome is AdjustmentOutcome.INVALID


class TestSeriesContinuity:
    """The property that matters: the adjusted series has no artificial step at the ex-date."""

    def test_a_two_for_one_split_leaves_the_close_continuous(self) -> None:
        """The acceptance criterion, in its simplest form. 100 -> 50 raw becomes 50 -> 50."""
        frame = bars(
            {
                dt.date(2026, 3, 6): "100",
                dt.date(2026, 3, 9): "50",
                dt.date(2026, 3, 10): "51",
            }
        )
        result = adjust_bars(
            frame, [CorporateActionInput("split", EX_DATE, Decimal(2), Decimal(1))]
        )
        closes = result.bars["close"].to_list()
        assert closes[0] == Decimal("50")
        assert closes[1] == Decimal("50")

    def test_the_split_shows_as_a_raw_discontinuity(self) -> None:
        """Confirms the fixture really does contain the step the adjustment removes."""
        frame = bars({dt.date(2026, 3, 6): "100", dt.date(2026, 3, 9): "50"})
        result = adjust_bars(
            frame, [CorporateActionInput("split", EX_DATE, Decimal(2), Decimal(1))]
        )
        raw = result.bars["close_raw"].to_list()
        assert raw[0] / raw[1] == Decimal(2)

    def test_bars_on_and_after_the_ex_date_are_untouched(self) -> None:
        """docs/09: "all bars with `date < e`". The ex-date bar is already ex."""
        frame = bars({dt.date(2026, 3, 9): "50", dt.date(2026, 3, 10): "51"})
        result = adjust_bars(
            frame, [CorporateActionInput("split", EX_DATE, Decimal(2), Decimal(1))]
        )
        assert result.bars["adj_factor"].to_list() == [Decimal(1), Decimal(1)]

    def test_the_most_recent_bar_always_equals_its_exchange_print(self) -> None:
        """What makes the adjusted series comparable with a quote screen today."""
        frame = bars({dt.date(2026, 3, 6): "100", dt.date(2026, 3, 10): "51"})
        result = adjust_bars(
            frame, [CorporateActionInput("split", EX_DATE, Decimal(2), Decimal(1))]
        )
        last = result.bars.tail(1).to_dicts()[0]
        assert last["close"] == last["close_raw"]

    def test_volume_moves_the_other_way(self) -> None:
        """docs/09: a split multiplies the share count as it divides the price."""
        frame = bars({dt.date(2026, 3, 6): "100", dt.date(2026, 3, 9): "50"}, volume=1000)
        result = adjust_bars(
            frame, [CorporateActionInput("split", EX_DATE, Decimal(2), Decimal(1))]
        )
        assert result.bars["volume"].to_list()[0] == Decimal(2000)

    def test_ohl_are_adjusted_with_the_close(self) -> None:
        frame = bars({dt.date(2026, 3, 6): "100", dt.date(2026, 3, 9): "50"})
        result = adjust_bars(
            frame, [CorporateActionInput("split", EX_DATE, Decimal(2), Decimal(1))]
        )
        first = result.bars.head(1).to_dicts()[0]
        assert first["open"] == first["high"] == first["low"] == first["close"] == Decimal(50)


class TestCumulativeProduct:
    """docs/09: "Cumulative product of all factors after date `d` = `adj_factor[d]`"."""

    def test_two_actions_compound(self) -> None:
        """CUPID's documented pair: a 10:1 split and a 1:1 bonus on the same day (docs/01 §9)."""
        frame = bars(
            {
                dt.date(2024, 4, 12): "1000",
                dt.date(2024, 4, 15): "50",
                dt.date(2024, 4, 16): "52",
            }
        )
        result = adjust_bars(
            frame,
            [
                CorporateActionInput("split", dt.date(2024, 4, 15), Decimal(10), Decimal(1)),
                CorporateActionInput("bonus", dt.date(2024, 4, 15), Decimal(1), Decimal(1)),
            ],
        )
        # 1/10 for the split, 1/2 for the bonus -> 1/20.
        assert result.bars["adj_factor"].to_list()[0] == Decimal("0.05")
        assert result.bars["close"].to_list()[0] == Decimal("50")

    def test_a_later_action_affects_bars_before_an_earlier_one(self) -> None:
        """The defining property of a cumulative product: the oldest bar carries every factor."""
        frame = bars(
            {
                dt.date(2024, 1, 2): "400",
                dt.date(2025, 1, 2): "200",
                dt.date(2026, 1, 2): "100",
            }
        )
        result = adjust_bars(
            frame,
            [
                CorporateActionInput("split", dt.date(2025, 1, 2), Decimal(2), Decimal(1)),
                CorporateActionInput("split", dt.date(2026, 1, 2), Decimal(2), Decimal(1)),
            ],
        )
        factors = result.bars["adj_factor"].to_list()
        assert factors == [Decimal("0.25"), Decimal("0.5"), Decimal(1)]

    def test_an_unquantified_action_is_surfaced_not_dropped(self) -> None:
        frame = bars({dt.date(2026, 3, 6): "100", dt.date(2026, 3, 9): "95"})
        result = adjust_bars(
            frame, [CorporateActionInput("rights", EX_DATE, Decimal(1), Decimal(1))]
        )
        assert len(result.unquantified_actions) == 1
        assert result.bars["adj_factor"].to_list() == [Decimal(1), Decimal(1)]


class TestIdempotence:
    def test_adjusting_twice_from_raw_gives_the_same_answer(self) -> None:
        """The rebuild rule works because the inputs are raw prints, never previous outputs."""
        frame = bars({dt.date(2026, 3, 6): "100", dt.date(2026, 3, 9): "50"})
        actions = [CorporateActionInput("split", EX_DATE, Decimal(2), Decimal(1))]
        first = adjust_bars(frame, actions)
        second = adjust_bars(frame, actions)
        assert first.bars.equals(second.bars)

    def test_no_actions_leaves_everything_at_unity(self) -> None:
        frame = bars({dt.date(2026, 3, 6): "100", dt.date(2026, 3, 9): "101"})
        result = adjust_bars(frame, [])
        assert result.bars["adj_factor"].to_list() == [Decimal(1), Decimal(1)]
        assert result.bars["close"].to_list() == [Decimal(100), Decimal(101)]


class TestInputValidation:
    def test_missing_columns_are_refused(self) -> None:
        with pytest.raises(ValueError, match="missing required columns"):
            adjust_bars(pl.DataFrame({"date": [dt.date(2026, 1, 1)]}), [])

    def test_an_empty_history_is_not_an_error(self) -> None:
        """A newly listed instrument has no bars yet; that is normal, not a failure."""
        empty = bars({dt.date(2026, 3, 6): "100"}).clear()
        result = adjust_bars(
            empty, [CorporateActionInput("split", EX_DATE, Decimal(2), Decimal(1))]
        )
        assert result.bars.height == 0
