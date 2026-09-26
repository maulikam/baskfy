"""The row in words, the attention question, and the honesty gate on the answer."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from baskfy_core.candidate_review import (
    REVIEW_CONFIDENCE_FLOOR,
    REVIEW_QUESTIONS,
    REVIEW_SCHEMA,
    STATE_FIELDS,
    ReviewLabel,
    ReviewOpinion,
    RowContext,
    RowFacts,
    describe_context,
    describe_row,
    describe_timeline,
    opinion_from_laya,
    review_key,
    review_state,
    rules_opinion,
    shown,
)
from baskfy_core.catalyst_tags import question_schema_hash

ORDER = "Bagging/Receiving of orders/contracts — order worth Rs 840 crore"

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
        state = review_state([EP], ORDER)
        assert set(state) == {"setup", "filing"}
        assert state["filing"].startswith("Bagging")
        assert review_state([EP], "  ")["filing"] == "No filing on record."
        # The short fields lead: laya truncates a long state from the right (OV11).
        assert list(state) == ["filing", "setup"]
        assert STATE_FIELDS == ("filing", "timeline", "context", "setup")

    def test_the_key_is_content_addressed(self) -> None:
        a = review_state([EP], "x")
        assert review_key(a) == review_key(dict(reversed(list(a.items()))))
        assert review_key(a) != review_key(review_state([VBT], "x"))
        assert review_key(a).startswith(f"candidate_review:v2:{REVIEW_SCHEMA}:")
        assert question_schema_hash(REVIEW_QUESTIONS) == REVIEW_SCHEMA


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
        unsure = opinion_from_laya({"choice": "worth_a_look", "answer_confidence": 0.38})
        assert unsure == ReviewOpinion(ReviewLabel.WORTH_A_LOOK, 0.38)
        assert not shown(unsure)
        sure = opinion_from_laya(
            {"choice": "look_first", "answer_confidence": REVIEW_CONFIDENCE_FLOOR}
        )
        assert shown(sure)
        assert shown(None) is False

    def test_the_number_is_the_chosen_word_s_probability_never_the_entropy_score(self) -> None:
        # OV11 (26 Sep 2026): both readers compared laya's `confidence` — on a `choice`
        # question a normalised-entropy score — to the 0.60 floor. The probability is what the
        # floor means; a payload carrying only the entropy score is not an opinion.
        assert opinion_from_laya({"choice": "skip", "confidence": 0.93}) is None
        from_probabilities = opinion_from_laya(
            {
                "choice": "skip",
                "confidence": 0.14,
                "probabilities": {"skip": 0.6, "look_first": 0.4},
            },
        )
        assert from_probabilities == ReviewOpinion(ReviewLabel.SKIP, 0.6)

    def test_a_label_outside_the_vocabulary_is_not_an_opinion(self) -> None:
        assert opinion_from_laya({"choice": "buy", "answer_confidence": 0.99}) is None
        assert opinion_from_laya(None) is None

    def test_a_person_s_label_is_always_shown(self) -> None:
        assert shown(ReviewOpinion(ReviewLabel.SKIP, 0.0, source="labelled"))


class TestEveryStoredNumberIsSaid:
    """Maulik, 25 Sep 2026: "all the parameters which swing has noticed, including the other
    parameters which we might have" — the sentence carries the whole row, and the day around it."""

    def test_the_swing_row_says_every_column_the_detector_wrote(self) -> None:
        full = RowFacts(
            "swing",
            "FLAG · BREAKOUT_TODAY",
            True,
            {
                "score": Decimal("71.20"),
                "gap_pct": Decimal("2.10"),
                "rvol": Decimal("3.4"),
                "base_depth_pct": Decimal("6.2"),
                "base_bars": 35,
                "tightness_adr": Decimal("1.8"),
                "dryup_ratio": Decimal("0.55"),
                "prior_move_pct": Decimal("62"),
                "adr_pct": Decimal("5.1"),
                "dist_ma_fast_pct": Decimal("3.2"),
                "dist_ma_slow_pct": Decimal("-1.5"),
                "up_streak": 3,
                "trigger": Decimal("149.60"),
                "stop_ref": Decimal("141.86"),
                "pivot_high": Decimal("149.60"),
                "close": Decimal("144.75"),
                "turnover_avg": 112_734_212,
                "locked_upper_circuit": False,
                "listed_within_2y": True,
            },
        )
        words = describe_row([full])
        assert words == (
            "Swing flag, breakout today above the pivot: setup score 71 of 100; gapped 2%; "
            "volume 3.4 times its average; base 6% deep; base 35 sessions long; base tightness "
            "1.8 ADRs; volume dried up to 0.55 of its base average; up 62% in the prior move; "
            "average daily range 5.1%; 3% above the fast moving average; 2% below the slow "
            "moving average; 3 up sessions in a row; trigger 149.60 with the stop 5.2% below it; "
            "3% below the pivot; average turnover Rs 11.3 crore a day; listed within the last "
            "two years."
        )
        assert "locked" not in words

    def test_the_volume_and_tight_rows_say_their_levels_and_filters(self) -> None:
        vbt = RowFacts(
            "volume_breakout",
            "scanned, did not pass the filters",
            False,
            {
                "rvol": Decimal("3.1"),
                "change_pct": Decimal("4.4"),
                "close_position": Decimal("0.7"),
                "ret_20_pct": Decimal("28"),
                "close": Decimal("210"),
                "sma_200": Decimal("180"),
                "ema_21": Decimal("200"),
                "high_20_prior": Decimal("205"),
                "limit_price": Decimal("210"),
                "stop_price": Decimal("184.8"),
                "turnover_avg_20": 250_000_000,
                "failed_filters": "RET20",
                "locked_upper_circuit": True,
            },
        )
        assert describe_row([vbt]) == (
            "Volume breakout scanned but rejected by the filters: volume 3.1 times its 50-day "
            "average; closed up 4%; in the upper half of the day's range; up 28% over the last "
            "20 sessions; 17% above its 200-day average; 5% above its 21-day average; 2% above "
            "the prior 20-day high; entry limit 210.00 with the stop 12.0% below it; average "
            "turnover Rs 25.0 crore a day; locked in the upper circuit; failed the ret20 filter(s)."
        )
        twt = RowFacts(
            "three_weeks_tight",
            "tight 4 sessions · signal",
            True,
            {
                "week_range_pct": Decimal("1.4237"),
                "week_close_0": Decimal("149.60"),
                "week_close_1": Decimal("148.90"),
                "week_close_2": Decimal("147.50"),
                "sessions_in_state": 4,
                "sessions_out_before": 7,
                "month_low_ratio": Decimal("1.4960"),
                "close": Decimal("149.60"),
                "sma_dma": Decimal("120"),
                "volume": 310_000,
                "vol_sma_50": 250_000,
                "turnover_avg_20": 500_000_000,
                "stop_preview": Decimal("119.68"),
            },
        )
        assert describe_row([twt]) == (
            "Three weeks tight, entry today: three weekly closes within 1.4% of each other; "
            "weekly closes 147.50, 148.90, 149.60 oldest first; 4 sessions in the state; 7 "
            "sessions out of the state before this entry; 50% above its three-month low; 25% "
            "above its 200-day average; volume 1.2 times its 50-day average; average turnover Rs "
            "50.0 crore a day; stop 20% below the close."
        )

    def test_the_day_s_context_is_a_third_field_and_changes_the_key(self) -> None:
        context = RowContext(
            gates={"Swing": "GREEN", "Three weeks tight": "OPEN"},
            breadth_pct={"Three weeks tight": Decimal("51.1588")},
            sector="nifty-it",
            screens=(("RSI Scan", 128, 2317), ("Trend Stack", None, None)),
        )
        assert describe_context(context) == (
            "Swing gate green; Three weeks tight gate open with 51% of the universe above its "
            "long average; sector nifty it; on screens RSI Scan #128 of 2317, Trend Stack."
        )
        bare = review_state([EP], "x")
        with_context = review_state([EP], "x", context)
        assert set(bare) == {"setup", "filing"}
        assert set(with_context) == {"setup", "filing", "context"}
        assert review_key(bare) != review_key(with_context)
        assert describe_context(None) == "" and describe_context(RowContext()) == ""


class TestTheRulesBaseline:
    """Always a word, always a reason, and the model or a person overrules it."""

    def test_an_actionable_row_with_a_material_filing_is_look_first(self) -> None:
        opinion = rules_opinion([EP], ORDER, "order", "high")
        assert (opinion.label, opinion.source) == (ReviewLabel.LOOK_FIRST, "rules")
        assert opinion.reason == "a strategy could act and the filing is material (order)"
        assert shown(opinion)

    def test_an_actionable_row_with_a_routine_or_missing_filing_is_worth_a_look(self) -> None:
        assert rules_opinion([VBT], "Copy of Newspaper Publication", "routine", "low").label is (
            ReviewLabel.WORTH_A_LOOK
        )
        none = rules_opinion([VBT], None, None, None)
        assert none.label is ReviewLabel.WORTH_A_LOOK
        assert none.reason == "a strategy could act; no filing on record"

    def test_the_skips_name_their_reason(self) -> None:
        # Merely in the tight state: no strategy could act.
        assert rules_opinion([TWT], ORDER, "order", "high").reason == (
            "no strategy could act on the row as it stands"
        )
        # A regulatory order is adverse even under a strong setup.
        demand = "Action(s) taken or orders passed — receipt of Demand Order under Section 156"
        adverse = rules_opinion([EP], demand, "governance", "medium")
        assert (adverse.label, adverse.reason) == (
            ReviewLabel.SKIP,
            "the filing is adverse (orders passed, action(s) taken)",
        )
        # A governance filing that is not adverse (a credit rating) is not a skip.
        rating = rules_opinion([EP], "Credit rating reaffirmed", "governance", "medium")
        assert rating.label is ReviewLabel.WORTH_A_LOOK
        # The acting strategy's gate is shut.
        shut = rules_opinion([EP], None, None, None, RowContext(gates={"Swing": "RED"}))
        assert (shut.label, shut.reason) == (ReviewLabel.SKIP, "Swing gate shut")
        # Another strategy's gate being shut does not touch this row.
        other = rules_opinion([EP], None, None, None, RowContext(gates={"Volume breakout": "SHUT"}))
        assert other.label is ReviewLabel.WORTH_A_LOOK
        # Locked in the upper circuit.
        locked = RowFacts("swing", "EP · GAP_DAY", True, {"locked_upper_circuit": True})
        assert rules_opinion([locked], ORDER, "order", "high").reason == (
            "locked in the upper circuit"
        )

    def test_the_adverse_flag_survives_a_confident_model_or_a_correction_re_tagging_it(
        self,
    ) -> None:
        # OV11 finding 3: the resolved tag used to be the only input. Laya, sure, said
        # corporate_action on "SEBI order"; a model tag has no phrases; the skip vanished.
        # The rules read the headline again here, whatever the Filing column shows.
        sebi = "SEBI order against the company and its promoters"
        as_laya_saw_it = rules_opinion([EP], sebi, "corporate_action", "medium")
        assert (as_laya_saw_it.label, as_laya_saw_it.reason) == (
            ReviewLabel.SKIP,
            "the filing is adverse (sebi order)",
        )
        as_corrected = rules_opinion([EP], sebi, "routine", "low")
        assert as_corrected.label is ReviewLabel.SKIP
        # And a non-adverse headline the model calls governance is not a skip.
        assert rules_opinion([EP], ORDER, "governance", "medium").label is ReviewLabel.WORTH_A_LOOK


class TestTheTimeline:
    """When each piece of the row was, relative to the session — never to today (OV11)."""

    SWING = dt.date(2026, 9, 25)

    def test_the_filing_the_results_and_the_screens_are_placed_against_the_session(self) -> None:
        context = RowContext(
            sessions={"Swing": self.SWING, "Three weeks tight": dt.date(2026, 9, 24)},
            filing_published=dt.date(2026, 9, 23),
            earnings_date=dt.date(2026, 10, 28),
            screen_runs={"RSI Scan": self.SWING, "Trend Stack": dt.date(2026, 9, 21)},
            screens_changed=("Trend Stack",),
        )
        assert describe_timeline(context, filing_on_record=True) == (
            "Swing facts are from the session of Fri 25 Sep 2026; Three weeks tight facts from "
            "Thu 24 Sep 2026, 1 day earlier; the filing was published 2 days before the session "
            "(Wed 23 Sep 2026); results are due 33 days ahead (Wed 28 Oct 2026); screen ranks: "
            "RSI Scan from the session day, Trend Stack 4 days older than the session "
            "(definition changed since)."
        )

    def test_same_day_after_the_session_and_past_results_have_their_own_words(self) -> None:
        same = RowContext(
            sessions={"Swing": self.SWING},
            filing_published=self.SWING,
            earnings_date=dt.date(2026, 9, 15),
        )
        assert describe_timeline(same, filing_on_record=True) == (
            "Swing facts are from the session of Fri 25 Sep 2026; the filing was published on "
            "the session day; results were 10 days before the session (Tue 15 Sep 2026)."
        )
        after = RowContext(sessions={"Swing": self.SWING}, filing_published=dt.date(2026, 9, 26))
        assert describe_timeline(after, filing_on_record=True).endswith(
            "the filing was published 1 day after the session (Sat 26 Sep 2026)."
        )

    def test_a_filing_without_a_date_says_so_and_no_filing_says_nothing_about_one(self) -> None:
        context = RowContext(sessions={"Swing": self.SWING})
        assert describe_timeline(context, filing_on_record=True) == (
            "Swing facts are from the session of Fri 25 Sep 2026; the filing's date is not on "
            "record."
        )
        assert describe_timeline(context, filing_on_record=False) == (
            "Swing facts are from the session of Fri 25 Sep 2026."
        )
        assert describe_timeline(None, filing_on_record=True) == ""
        assert describe_timeline(RowContext(), filing_on_record=True) == ""

    def test_the_timeline_is_a_state_field_in_second_place_and_changes_the_key(self) -> None:
        context = RowContext(sessions={"Swing": self.SWING}, filing_published=self.SWING)
        state = review_state([EP], ORDER, context)
        assert list(state) == ["filing", "timeline", "setup"]
        assert state["timeline"].endswith("the filing was published on the session day.")
        assert review_key(state) != review_key(review_state([EP], ORDER))
        # No clock anywhere: the same stored dates give the same words on any day.
        assert review_state([EP], ORDER, context) == state
