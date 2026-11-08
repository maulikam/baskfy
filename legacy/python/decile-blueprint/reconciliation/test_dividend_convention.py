"""The arithmetic behind M27's verdict, asserted rather than reviewed.

The measurement decided a product question — momentum is a *price* return, not a total return —
so the four pieces of arithmetic it rests on are worth pinning. Each of these could be wrong in a
way that silently flips the answer:

* an adjustment applied on the wrong side of its ex-date reverses which series is which;
* an off-by-one in the window makes both conventions wrong and the comparison meaningless;
* a parser that drops the cash actions would leave nothing to vote on and report unanimity;
* a row where the two conventions agree must not be allowed to vote, or the tally is stuffed
  with abstentions that all look like agreement.

Pure — no database, no network. See `dividend_convention.py` for the method.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from reconciliation.dividend_convention import (
    Action,
    Vote,
    adjusted,
    load_actions,
    verdict,
    window_return,
)

DAYS = [dt.date(2026, 8, 3) + dt.timedelta(days=n) for n in range(10)]
FLAT = [(day, 100.0) for day in DAYS]


class TestAdjustment:
    def test_an_action_adjusts_only_the_bars_before_its_ex_date(self) -> None:
        """A split halves the history in front of it and leaves the ex-date itself alone.

        The exchange print on the ex-date is already the post-split price, so adjusting it too
        would halve it twice — which is the direction of error that turns a real 90% cliff into
        an invented one.
        """
        split = Action("X", DAYS[5], 2.0, "split/bonus")
        out = dict(adjusted(FLAT, [split], share_count_only=True))

        assert out[DAYS[4]] == 50.0, "the bar before the ex-date is adjusted"
        assert out[DAYS[5]] == 100.0, "the ex-date itself is not"
        assert out[DAYS[9]] == 100.0, "nor is anything after it"

    def test_share_count_only_ignores_the_cash_actions(self) -> None:
        split = Action("X", DAYS[5], 2.0, "split/bonus")
        dividend = Action("X", DAYS[5], 1.05, "cash/other")

        price = dict(adjusted(FLAT, [split, dividend], share_count_only=True))
        total = dict(adjusted(FLAT, [split, dividend], share_count_only=False))

        assert price[DAYS[0]] == 50.0
        assert total[DAYS[0]] == 50.0 / 1.05
        # The two series differ by exactly the dividends and nothing else. That is the whole
        # premise of the measurement.
        assert price[DAYS[0]] / total[DAYS[0]] == 1.05

    def test_actions_compound(self) -> None:
        out = dict(
            adjusted(
                FLAT,
                [
                    Action("X", DAYS[3], 2.0, "split/bonus"),
                    Action("X", DAYS[7], 5.0, "split/bonus"),
                ],
                share_count_only=True,
            )
        )
        assert out[DAYS[0]] == 10.0
        assert out[DAYS[5]] == 20.0
        assert out[DAYS[8]] == 100.0

    def test_no_actions_leaves_the_series_alone(self) -> None:
        assert adjusted(FLAT, [], share_count_only=True) == FLAT


class TestWindow:
    def test_the_base_is_n_bars_back_inclusive_of_both_endpoints(self) -> None:
        """M11's base fix: `P(t) / P(t-(N-1))`, not `P(t) / P(t-N)`.

        A 3-bar window over 100, 110, 121 is +21%, because it spans three bars and two steps.
        """
        series = [(DAYS[0], 100.0), (DAYS[1], 110.0), (DAYS[2], 121.0)]
        assert window_return(series, 3) == pytest.approx(21.0)

        # The rejected convention would reach one bar further back and find nothing, or -- on a
        # longer series -- a different base entirely. Two bars is +10%, not +21%.
        assert window_return(series, 2) == pytest.approx(10.0)

    def test_it_refuses_a_window_longer_than_the_history(self) -> None:
        assert window_return([(DAYS[0], 100.0)], 22) is None

    def test_a_zero_base_is_refused_rather_than_dividing(self) -> None:
        assert window_return([(DAYS[0], 0.0), (DAYS[1], 10.0)], 2) is None


class TestVoting:
    def test_a_row_where_both_conventions_agree_cannot_vote(self) -> None:
        """Below stored precision the row carries no information about the convention.

        Letting it vote would count an abstention as agreement — and since most rows have no
        dividend in the window, that alone would decide the result.
        """
        vote = Vote("X", 6, Decimal("10.00"), 10.0, 10.000001)
        assert vote.winner == "indistinguishable"

    def test_the_nearer_convention_wins_a_separated_row(self) -> None:
        assert Vote("X", 6, Decimal("10.00"), 10.0, 13.0).winner == "price"
        assert Vote("X", 6, Decimal("13.00"), 10.0, 13.0).winner == "total"

    def test_the_verdict_follows_the_deciding_rows_only(self) -> None:
        votes = [
            Vote("A", 6, Decimal("10.00"), 10.0, 13.0),  # price
            Vote("B", 6, Decimal("10.00"), 10.0, 13.0),  # price
            Vote("C", 6, Decimal("13.00"), 10.0, 13.0),  # total
            *[Vote("D", 6, Decimal("10.00"), 10.0, 10.0) for _ in range(50)],  # abstentions
        ]
        assert verdict(votes) == "price"

    def test_no_deciding_rows_is_reported_as_indeterminate(self) -> None:
        """The instruction's fallback branch: if the corpus cannot discriminate, say so."""
        assert verdict([Vote("A", 6, Decimal("10.00"), 10.0, 10.0)]) == "indeterminate"


class TestTheEvidenceFile:
    def test_it_parses_both_kinds_out_of_the_committed_table(self) -> None:
        actions = load_actions()
        kinds = {action.kind for action in actions}

        assert kinds == {"split/bonus", "cash/other"}, "one of the two sections did not parse"
        assert len(actions) == 85, "RECOVERED-ACTIONS.md no longer holds M24's 85 actions"
        assert sum(1 for a in actions if a.share_count) == 47
        assert sum(1 for a in actions if not a.share_count) == 38

    def test_the_recovered_factors_are_all_above_one(self) -> None:
        """`close_raw / kite_close` is what is still owed, so it can never be below 1.0.

        A factor under one would mean the exchange print was *lower* than the adjusted price,
        which is not a corporate action — it is a parsing error.
        """
        for action in load_actions():
            assert action.factor > 1.0, f"{action.symbol} {action.ex_date} = {action.factor}"

    def test_the_measurement_is_written_into_the_same_file(self) -> None:
        """The verdict lives beside the evidence it was drawn from, not in a separate report."""
        text = Path(load_actions.__globals__["EVIDENCE"]).read_text(encoding="utf-8")
        assert "## The dividend question, settled by measurement" in text
        assert "**VERDICT: PRICE RETURN.**" in text
