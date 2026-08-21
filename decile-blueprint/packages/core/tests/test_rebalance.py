"""The rank-buffer rule — PROMPTS.md Prompt 14's first acceptance criterion.

    "Unit tests for the buffer rule covering: held name at rank exactly top_n+hold_buffer (hold),
     at +1 beyond (exit), a delisted holding (exit with reason), and a symbol not in our universe."

Each of those four is a test below, named for it. The rest of the module asserts the spec around
them: docs/07's definition of ``inside_wrh``, Prompt 14 §2's definitions of ``entries`` and
``exits``, and that the target weights add to exactly one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_core.rebalance import (
    Action,
    ExitReason,
    HeldName,
    ScreenRank,
    plan_rebalance,
)

TOP_N = 20
BUFFER = 10


def ranked(count: int = 40) -> list[ScreenRank]:
    """A screen output of ``count`` names, ranked 1..count. ``S1`` is rank 1."""
    return [
        ScreenRank(instrument_id=index, symbol=f"S{index}", name=f"S{index} LIMITED", rank=index)
        for index in range(1, count + 1)
    ]


def held(*instrument_ids: int, delisted: frozenset[int] = frozenset()) -> list[HeldName]:
    return [
        HeldName(
            instrument_id=instrument_id,
            symbol=f"S{instrument_id}",
            name=f"S{instrument_id} LIMITED",
            quantity=Decimal("100"),
            delisted=instrument_id in delisted,
        )
        for instrument_id in instrument_ids
    ]


def symbols(rows: object) -> list[str]:
    assert isinstance(rows, tuple)
    return [row.symbol for row in rows]


class TestTheFourNamedCases:
    """The four cases the acceptance criterion enumerates, one test each."""

    def test_a_held_name_at_exactly_top_n_plus_buffer_holds(self) -> None:
        """docs/07: ``inside_wrh`` is ``> top_n`` but ``<= top_n + hold_buffer`` — inclusive."""
        plan = plan_rebalance(ranked(), held(30), top_n=TOP_N, hold_buffer=BUFFER)
        assert plan.buffer_limit == 30
        assert symbols(plan.inside_wrh) == ["S30"]
        assert symbols(plan.exits) == []

    def test_a_held_name_one_rank_beyond_the_buffer_exits(self) -> None:
        plan = plan_rebalance(ranked(), held(31), top_n=TOP_N, hold_buffer=BUFFER)
        assert symbols(plan.exits) == ["S31"]
        assert plan.exits[0].reason is ExitReason.RANK_OUTSIDE_BUFFER
        assert plan.exits[0].rank == 31
        assert symbols(plan.inside_wrh) == []

    def test_a_delisted_holding_exits_with_a_reason(self) -> None:
        """Even one still ranked inside the top N: the position cannot be held any longer."""
        plan = plan_rebalance(
            ranked(), held(3, delisted=frozenset({3})), top_n=TOP_N, hold_buffer=BUFFER
        )
        assert symbols(plan.exits) == ["S3"]
        assert plan.exits[0].reason is ExitReason.DELISTED
        assert symbols(plan.holds) == []

    def test_a_symbol_not_in_our_universe_exits_as_not_in_screen(self) -> None:
        """A holding the screen never returned — filtered out, or outside its universe.

        There is no rank to report, and ``rank`` says so rather than inventing one.
        """
        stranger = HeldName(instrument_id=9_999, symbol="ZZZZ", name="ZZZZ LIMITED")
        plan = plan_rebalance(ranked(), [stranger], top_n=TOP_N, hold_buffer=BUFFER)
        assert symbols(plan.exits) == ["ZZZZ"]
        assert plan.exits[0].reason is ExitReason.NOT_IN_SCREEN
        assert plan.exits[0].rank is None


class TestTheThreeLists:
    def test_entries_are_the_top_n_names_not_currently_held(self) -> None:
        plan = plan_rebalance(ranked(), held(1, 2), top_n=5, hold_buffer=BUFFER)
        assert symbols(plan.entries) == ["S3", "S4", "S5"]

    def test_entries_are_in_rank_order(self) -> None:
        plan = plan_rebalance(list(reversed(ranked())), [], top_n=3, hold_buffer=0)
        assert [row.rank for row in plan.entries] == [1, 2, 3]

    def test_a_held_name_inside_the_top_n_is_neither_an_entry_nor_an_exit(self) -> None:
        plan = plan_rebalance(ranked(), held(2), top_n=TOP_N, hold_buffer=BUFFER)
        assert "S2" not in symbols(plan.entries)
        assert "S2" not in symbols(plan.exits)
        assert "S2" not in symbols(plan.inside_wrh)
        assert symbols(plan.holds) == ["S2"]

    def test_every_holding_lands_in_exactly_one_list(self) -> None:
        plan = plan_rebalance(
            ranked(), held(2, 25, 31, 40, delisted=frozenset({40})), top_n=TOP_N, hold_buffer=BUFFER
        )
        placed = symbols(plan.exits) + symbols(plan.inside_wrh) + symbols(plan.holds)
        assert sorted(placed) == sorted(["S2", "S25", "S31", "S40"])
        assert len(placed) == len(set(placed))

    def test_a_zero_buffer_is_the_plain_top_n_rule(self) -> None:
        plan = plan_rebalance(ranked(), held(21), top_n=TOP_N, hold_buffer=0)
        assert symbols(plan.inside_wrh) == []
        assert symbols(plan.exits) == ["S21"]

    def test_exits_are_ranked_first_then_the_rankless_ones(self) -> None:
        stranger = HeldName(instrument_id=9_999, symbol="AAAA", name="AAAA LIMITED")
        plan = plan_rebalance(ranked(), [*held(35, 31), stranger], top_n=TOP_N, hold_buffer=BUFFER)
        assert symbols(plan.exits) == ["S31", "S35", "AAAA"]


class TestTargetWeights:
    def test_they_cover_entries_holds_and_inside_wrh(self) -> None:
        plan = plan_rebalance(ranked(), held(2, 25, 31), top_n=3, hold_buffer=BUFFER)
        # top 3 = S1..S3; held S2 (hold), S25 (outside 3+10=13 → exit), S31 (exit)
        assert symbols(plan.exits) == ["S25", "S31"]
        assert [weight.symbol for weight in plan.target_weights] == ["S1", "S2", "S3"]
        assert [weight.action for weight in plan.target_weights] == [
            Action.ENTER,
            Action.HOLD,
            Action.ENTER,
        ]

    def test_they_sum_to_exactly_one(self) -> None:
        plan = plan_rebalance(ranked(), held(2, 5), top_n=7, hold_buffer=3)
        assert sum(weight.weight for weight in plan.target_weights) == Decimal(1)

    def test_they_sum_to_exactly_one_when_the_share_does_not_divide(self) -> None:
        """Three names is 0.333333 each; the last absorbs the remainder rather than losing it."""
        plan = plan_rebalance(ranked(), [], top_n=3, hold_buffer=0)
        weights = [weight.weight for weight in plan.target_weights]
        assert weights[:2] == [Decimal("0.333333"), Decimal("0.333333")]
        assert sum(weights) == Decimal(1)

    def test_they_are_in_rank_order(self) -> None:
        plan = plan_rebalance(ranked(), held(12), top_n=5, hold_buffer=10)
        assert [weight.rank for weight in plan.target_weights] == [1, 2, 3, 4, 5, 12]

    def test_an_empty_target_portfolio_has_no_weights(self) -> None:
        plan = plan_rebalance([], held(1), top_n=TOP_N, hold_buffer=BUFFER)
        assert plan.target_weights == ()
        assert symbols(plan.exits) == ["S1"]


class TestInputs:
    @pytest.mark.parametrize("top_n", [0, -1])
    def test_top_n_must_be_at_least_one(self, top_n: int) -> None:
        with pytest.raises(ValueError, match="top_n"):
            plan_rebalance(ranked(), [], top_n=top_n, hold_buffer=0)

    def test_the_buffer_must_not_be_negative(self) -> None:
        with pytest.raises(ValueError, match="hold_buffer"):
            plan_rebalance(ranked(), [], top_n=5, hold_buffer=-1)

    def test_a_screen_shorter_than_top_n_is_not_padded(self) -> None:
        plan = plan_rebalance(ranked(3), [], top_n=TOP_N, hold_buffer=BUFFER)
        assert symbols(plan.entries) == ["S1", "S2", "S3"]

    def test_holdings_are_matched_on_instrument_id_not_symbol(self) -> None:
        """A renamed instrument keeps its position; a symbol diff would exit it on rename day."""
        renamed = HeldName(instrument_id=2, symbol="OLDNAME", name="S2 LIMITED")
        plan = plan_rebalance(ranked(), [renamed], top_n=TOP_N, hold_buffer=BUFFER)
        assert symbols(plan.exits) == []
        assert symbols(plan.holds) == ["S2"]
