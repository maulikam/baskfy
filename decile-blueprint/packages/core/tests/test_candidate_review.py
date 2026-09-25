"""The row in words, the attention question, and the honesty gate on the answer."""

from __future__ import annotations

from decimal import Decimal

from baskfy_core.candidate_review import (
    REVIEW_CONFIDENCE_FLOOR,
    REVIEW_QUESTIONS,
    ReviewLabel,
    ReviewOpinion,
    RowFacts,
    describe_row,
    opinion_from_laya,
    review_key,
    review_state,
    shown,
)

EP = RowFacts(
    "swing",
    "EP · GAP_DAY",
    True,
    {"gap_pct": Decimal("9.10"), "rvol": Decimal("6.2"), "base_depth_pct": Decimal("3.4")},
)
VBT = RowFacts(
    "volume_breakout",
    "signal",
    True,
    {"rvol": Decimal("4.2"), "change_pct": Decimal("3.8"), "close_position": Decimal("0.95")},
)
TWT = RowFacts(
    "three_weeks_tight",
    "tight 4 sessions",
    False,
    {
        "week_range_pct": Decimal("1.4237"),
        "sessions_in_state": 4,
        "month_low_ratio": Decimal("1.4960"),
    },
)


class TestTheRowInWords:
    def test_each_strategy_s_numbers_become_the_sentence_a_person_would_say(self) -> None:
        assert describe_row([EP]) == (
            "Swing episodic pivot, gap day: gapped 9%; volume 6.2 times its average; base 3% deep."
        )
        assert describe_row([VBT]) == (
            "Volume breakout signal: volume 4.2 times its 50-day average; closed up 4%; "
            "in the top tenth of the day's range."
        )
        assert describe_row([TWT]) == (
            "Three weeks tight, in the state: three weekly closes within 1.4% of each other; "
            "4 sessions in the state; 50% above its three-month low."
        )

    def test_two_strategies_are_two_sentences_and_missing_numbers_are_left_out(self) -> None:
        bare = RowFacts("swing", "FLAG · SETTING_UP", True)
        assert describe_row([bare, TWT]).startswith(
            "Swing flag, still setting up below the pivot. Three weeks tight"
        )

    def test_the_state_carries_the_words_and_the_filing_and_nothing_else(self) -> None:
        state = review_state(
            [EP], "Bagging/Receiving of orders/contracts — order worth Rs 840 crore"
        )
        assert set(state) == {"setup", "filing"}
        assert state["filing"].startswith("Bagging")
        assert review_state([EP], "  ")["filing"] == "No filing on record."

    def test_the_key_is_content_addressed(self) -> None:
        a = review_state([EP], "x")
        assert review_key(a) == review_key(dict(reversed(list(a.items()))))
        assert review_key(a) != review_key(review_state([VBT], "x"))
        assert review_key(a).startswith("candidate_review:v1:")


class TestTheQuestionAndTheAnswer:
    def test_the_question_asks_for_attention_never_a_trade(self) -> None:
        [(name, question)] = REVIEW_QUESTIONS.items()
        assert name == "review_priority"
        criteria = question["criteria"]
        assert isinstance(criteria, dict)
        assert set(criteria) == {label.value for label in ReviewLabel}
        text = str(question).lower()
        assert "buy" not in text and "sell" not in text

    def test_an_answer_below_the_floor_is_kept_but_not_shown(self) -> None:
        # Measured 25 Sep 2026: the base checkpoint answered near a third each way.
        unsure = opinion_from_laya({"choice": "worth_a_look", "confidence": 0.13})
        assert unsure == ReviewOpinion(ReviewLabel.WORTH_A_LOOK, 0.13)
        assert not shown(unsure)
        sure = opinion_from_laya({"choice": "look_first", "confidence": REVIEW_CONFIDENCE_FLOOR})
        assert shown(sure)
        assert shown(None) is False

    def test_a_label_outside_the_vocabulary_is_not_an_opinion(self) -> None:
        assert opinion_from_laya({"choice": "buy", "confidence": 0.99}) is None
        assert opinion_from_laya(None) is None

    def test_a_person_s_label_is_always_shown(self) -> None:
        assert shown(ReviewOpinion(ReviewLabel.SKIP, 0.0, source="labelled"))
