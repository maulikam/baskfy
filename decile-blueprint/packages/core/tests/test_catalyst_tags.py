"""The rules baseline for exchange headlines: the vocabulary, the precedence, and the honesty.

These assert the contract settled on 25 Sep 2026 — eight event types, a priority fixed per type,
a bare Regulation 30 disclosure tagged ``other`` rather than guessed — not what the phrase list
happens to contain today. A phrase can be added without touching a test here; a change to the
precedence or the priority table cannot.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from baskfy_core import catalyst_tags
from baskfy_core.catalyst_tags import (
    ADVERSE_PHRASES,
    LAYA_CONFIDENCE_FLOOR,
    LAYA_QUESTIONS,
    PRIORITY_OF,
    RULES,
    SOURCE_CORRECTED,
    SOURCE_LAYA,
    SOURCE_RULES,
    TAG_SCHEMA,
    CatalystTag,
    EventType,
    ReviewPriority,
    adverse_filing,
    adverse_phrases,
    cache_key,
    chosen_probability,
    laya_state,
    question_schema_hash,
    resolve_tag,
    split_subject,
    tag_from_laya,
    tag_headline,
)


class TestTheVocabulary:
    def test_the_eight_types_and_three_priorities_are_the_settled_ones(self) -> None:
        assert {t.value for t in EventType} == {
            "earnings",
            "order",
            "approval",
            "fundraising",
            "governance",
            "corporate_action",
            "routine",
            "other",
        }
        assert {p.value for p in ReviewPriority} == {"high", "medium", "low"}

    def test_priority_is_a_property_of_the_type_and_every_type_has_one(self) -> None:
        assert set(PRIORITY_OF) == set(EventType)
        assert {t for t, p in PRIORITY_OF.items() if p is ReviewPriority.HIGH} == {
            EventType.ORDER,
            EventType.EARNINGS,
            EventType.APPROVAL,
        }
        assert {t for t, p in PRIORITY_OF.items() if p is ReviewPriority.LOW} == {
            EventType.ROUTINE,
            EventType.OTHER,
        }

    def test_every_rule_block_names_a_type_with_at_least_one_phrase(self) -> None:
        for event_type, phrases in RULES:
            assert isinstance(event_type, EventType)
            assert phrases, f"{event_type} has no phrases"
            assert all(phrase == phrase.casefold() for phrase in phrases), "phrases are casefolded"


class TestTheHeadlines:
    @pytest.mark.parametrize(
        ("headline", "event_type"),
        [
            ("Receipt of order worth Rs 840 crore from Ministry of Defence", EventType.ORDER),
            ("Press Release - FLAGCO wins a multi-year order", EventType.ORDER),
            ("Outcome of Board Meeting - Approval of Financial Results for Q2", EventType.EARNINGS),
            ("USFDA approval for ANDA", EventType.APPROVAL),
            ("Allotment of equity shares under QIP", EventType.FUNDRAISING),
            ("Board approves interim dividend and record date", EventType.CORPORATE_ACTION),
            ("Announcement under Regulation 30 (LODR)-Change in Directorate", EventType.GOVERNANCE),
            ("Closure of Trading Window", EventType.ROUTINE),
            ("Analysts/Institutional Investor Meet - Intimation", EventType.ROUTINE),
        ],
    )
    def test_the_exchange_s_own_subjects_land_on_their_type(
        self, headline: str, event_type: EventType
    ) -> None:
        tag = tag_headline(headline)
        assert tag.event_type is event_type
        assert tag.review_priority is PRIORITY_OF[event_type]
        assert tag.matched, "a typed headline names the phrase that decided it"
        assert tag.source == SOURCE_RULES

    def test_a_bare_regulation_30_disclosure_is_other_not_a_guess(self) -> None:
        """The feed stores the headline, never the filing. A subject the headline does not state
        is not one the tag may invent."""
        for headline in ("Disclosure under Regulation 30", "Updates", "Announcement"):
            assert tag_headline(headline) == CatalystTag(EventType.OTHER, ReviewPriority.LOW, ())

    def test_a_blank_headline_is_other_with_nothing_matched(self) -> None:
        assert tag_headline(None) == CatalystTag(EventType.OTHER, ReviewPriority.LOW, ())
        assert tag_headline("   ") == CatalystTag(EventType.OTHER, ReviewPriority.LOW, ())

    def test_the_word_order_inside_a_regulatory_order_is_not_an_order(self) -> None:
        assert tag_headline("SEBI order against the company").event_type is EventType.GOVERNANCE
        assert tag_headline("NCLT order admitting the petition").event_type is EventType.GOVERNANCE

    def test_a_result_approved_at_a_board_meeting_is_a_result(self) -> None:
        """Both "board meeting" (governance) and "approval" (approval) appear; the result wins,
        because that is what the reader opens the filing for."""
        assert (
            tag_headline(
                "Outcome of Board Meeting - Approval of Audited Financial Results"
            ).event_type
            is EventType.EARNINGS
        )

    def test_matching_is_on_word_boundaries_and_case_insensitive(self) -> None:
        assert tag_headline("BORDERLINE INDUSTRIES annual report").event_type is EventType.ROUTINE
        assert "order" not in tag_headline("Borderline Industries annual report").matched
        assert tag_headline("ORDER WIN").event_type is EventType.ORDER

    def test_it_is_deterministic(self) -> None:
        headline = "Receipt of order worth Rs 840 crore"
        assert tag_headline(headline) == tag_headline(headline)


class TestItStaysPure:
    def test_the_module_imports_no_io(self) -> None:
        tree = ast.parse(inspect.getsource(catalyst_tags))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        # `json` and `collections.abc` joined for the key's schema hash (OV11): pure stdlib,
        # no file, socket or clock behind either.
        assert imported <= {
            "__future__",
            "collections.abc",
            "dataclasses",
            "enum",
            "hashlib",
            "json",
            "re",
            "typing",
        }


class TestTheModelColumn:
    """Laya's answer resolved against the rules: sure wins, unsure defers, disagreement is kept."""

    def test_the_question_shows_the_model_the_headline_and_nothing_else(self) -> None:

        assert laya_state("Receipt of order") == {"headline": "Receipt of order"}
        [(name, question)] = LAYA_QUESTIONS.items()
        assert name == "event_type"
        assert question["type"] == "choice"
        criteria = question["criteria"]
        assert isinstance(criteria, dict)
        assert set(criteria) == {t.value for t in EventType}

    def test_a_confident_model_answer_is_the_tag_and_carries_its_number(self) -> None:

        # Measured 25 Sep 2026: "Receipt of order worth Rs 840 crore ..." zero-shot.
        laya = tag_from_laya({"type": "choice", "choice": "order", "answer_confidence": 0.9861})
        assert laya == CatalystTag(
            EventType.ORDER, ReviewPriority.HIGH, (), source=SOURCE_LAYA, confidence=0.9861
        )
        rules = tag_headline("Receipt of order worth Rs 840 crore from Ministry of Defence")
        chosen = resolve_tag(rules, laya)
        assert (chosen.source, chosen.event_type, chosen.confidence, chosen.disagrees_with) == (
            SOURCE_LAYA,
            EventType.ORDER,
            0.9861,
            None,
        )

    def test_an_unsure_model_defers_to_the_rules_and_the_disagreement_is_kept(self) -> None:

        # Measured: "Resignation of Chief Financial Officer" -> corporate_action at 0.3301.
        laya = tag_from_laya({"choice": "corporate_action", "answer_confidence": 0.3301})
        assert laya is not None and laya.confidence is not None
        assert laya.confidence < LAYA_CONFIDENCE_FLOOR
        rules = tag_headline("Resignation of Chief Financial Officer")
        chosen = resolve_tag(rules, laya)
        assert (chosen.source, chosen.event_type) == (SOURCE_RULES, EventType.GOVERNANCE)
        assert chosen.disagrees_with == "laya:corporate_action"

    def test_a_sure_but_different_model_answer_still_records_what_the_rules_said(self) -> None:

        # Measured: "SEBI order against the company" -> corporate_action at 0.6959 (wrong, sure).
        laya = tag_from_laya({"choice": "corporate_action", "answer_confidence": 0.6959})
        chosen = resolve_tag(tag_headline("SEBI order against the company"), laya)
        assert (chosen.source, chosen.event_type) == ("laya", EventType.CORPORATE_ACTION)
        assert chosen.disagrees_with == "rules:governance"

    def test_agreement_carries_no_disagreement_and_no_answer_returns_the_rules_untouched(
        self,
    ) -> None:

        rules = tag_headline("USFDA approval for ANDA")
        laya = tag_from_laya({"choice": "approval", "answer_confidence": 0.9943})
        assert resolve_tag(rules, laya).disagrees_with is None
        assert resolve_tag(rules, None) == rules

    def test_an_answer_outside_the_vocabulary_is_not_a_tag(self) -> None:

        assert tag_from_laya({"choice": "merger", "answer_confidence": 0.9}) is None
        assert tag_from_laya({"choice": "order"}) is None
        assert tag_from_laya("order") is None
        assert tag_from_laya(None) is None

    def test_the_cache_key_is_content_addressed_and_case_blind(self) -> None:

        assert cache_key("Receipt of Order") == cache_key("  receipt of order ")
        assert cache_key("Receipt of Order") != cache_key("Receipt of Orders")
        assert cache_key("x").startswith(f"catalyst_tag:v2:{TAG_SCHEMA}:")
        assert len(TAG_SCHEMA) == 8


class TestTheCorrection:
    """A person's word wins over both readers, and what it overruled is kept as the signal."""

    def test_a_correction_wins_over_a_sure_model_and_names_the_model_s_word(self) -> None:
        # Measured shape: the rules say order, Laya says corporate_action at 0.91 (sure, wrong),
        # and the person says order. The correction is the tag; the model's word is what it
        # overruled.
        rules = tag_headline("Press Release - BOTH wins a multi-year order")
        laya = tag_from_laya({"choice": "corporate_action", "answer_confidence": 0.91})
        chosen = resolve_tag(rules, laya, EventType.ORDER)
        assert chosen == CatalystTag(
            EventType.ORDER,
            ReviewPriority.HIGH,
            (),
            source=SOURCE_CORRECTED,
            confidence=None,
            disagrees_with="laya:corporate_action",
        )

    def test_a_correction_over_the_rules_alone_names_the_rules_word(self) -> None:
        rules = tag_headline("SEBI order against the company")
        assert rules.event_type is EventType.GOVERNANCE
        chosen = resolve_tag(rules, None, EventType.OTHER)
        assert (chosen.source, chosen.event_type, chosen.review_priority) == (
            SOURCE_CORRECTED,
            EventType.OTHER,
            ReviewPriority.LOW,
        )
        assert chosen.disagrees_with == "rules:governance"

    def test_the_model_s_word_is_named_before_the_rules_when_both_differed(self) -> None:
        rules = tag_headline("Resignation of Chief Financial Officer")
        laya = tag_from_laya({"choice": "corporate_action", "answer_confidence": 0.33})
        chosen = resolve_tag(rules, laya, EventType.ROUTINE)
        assert chosen.disagrees_with == "laya:corporate_action"

    def test_a_correction_that_agrees_with_the_model_still_names_the_rules_if_they_differed(
        self,
    ) -> None:
        rules = tag_headline("SEBI order against the company")  # governance
        laya = tag_from_laya({"choice": "corporate_action", "answer_confidence": 0.6959})
        chosen = resolve_tag(rules, laya, EventType.CORPORATE_ACTION)
        assert (chosen.source, chosen.disagrees_with) == (SOURCE_CORRECTED, "rules:governance")

    def test_a_correction_that_agrees_with_everyone_carries_no_disagreement(self) -> None:
        rules = tag_headline("USFDA approval for ANDA")
        laya = tag_from_laya({"choice": "approval", "answer_confidence": 0.9943})
        chosen = resolve_tag(rules, laya, EventType.APPROVAL)
        assert (chosen.source, chosen.confidence, chosen.matched, chosen.disagrees_with) == (
            SOURCE_CORRECTED,
            None,
            (),
            None,
        )

    def test_no_correction_leaves_the_resolution_as_it_was(self) -> None:
        rules = tag_headline("USFDA approval for ANDA")
        laya = tag_from_laya({"choice": "approval", "answer_confidence": 0.9943})
        assert resolve_tag(rules, laya, None) == resolve_tag(rules, laya)
        assert resolve_tag(rules, None, None) == rules

    def test_the_three_sources_are_distinct_words(self) -> None:
        assert len({SOURCE_RULES, SOURCE_LAYA, SOURCE_CORRECTED}) == 3
        assert SOURCE_CORRECTED == "corrected"


class TestTheExchangeSubjectLine:
    """NSE writes `Subject — Company has informed the Exchange …`; the subject decides."""

    @pytest.mark.parametrize(
        ("headline", "event_type"),
        [
            (
                "Bagging/Receiving of orders/contracts — Bharat Electronics Limited has informed "
                "the Exchange regarding Receipt of orders worth Rs. 840 crores",
                EventType.ORDER,
            ),
            (
                "Financial Results — Cubex Tubings Limited has informed the Exchange regarding "
                "Unaudited Financial Results for the quarter ended June 30, 2026",
                EventType.EARNINGS,
            ),
            (
                "Shareholders meeting — Kapston Services Limited has informed the Exchange "
                "regarding Proceedings of Annual General Meeting held on September 24, 2026",
                EventType.ROUTINE,
            ),
            (
                "Action(s) taken or orders passed — Anuh Pharma Limited has informed the Exchange "
                "about orders passed regarding receipt of Demand Order under Section 156",
                EventType.GOVERNANCE,
            ),
            (
                "Disclosure under SEBI Takeover Regulations — S.A.L. Steel Limited has submitted "
                "a copy of Disclosure under Regulation 29(1) of SEBI (SAST) Regulations",
                EventType.ROUTINE,
            ),
            (
                "General Updates — Disclosure under Regulation 29(2) of the SEBI SAST Regulations",
                EventType.ROUTINE,
            ),
            (
                "Copy of Newspaper Publication — Kiri Industries Limited has informed the "
                "Exchange about Copy of Newspaper Publication for Corrigendum",
                EventType.ROUTINE,
            ),
            (
                "General Updates — Bluspring Enterprises Limited has informed the Exchange "
                "regarding Authorisation to Key Managerial Personnel",
                EventType.GOVERNANCE,
            ),
            (
                "General Updates — Premier Polyfilm Limited has informed the Exchange about "
                "General Updates",
                EventType.OTHER,
            ),
        ],
    )
    def test_real_headlines_from_the_box_land_on_their_type(
        self, headline: str, event_type: EventType
    ) -> None:
        assert tag_headline(headline).event_type is event_type

    def test_the_subject_wins_over_the_sentence_after_it(self) -> None:
        """A meeting notice that mentions a quarter is a meeting notice: the rest of the headline
        is read only when the subject names nothing."""
        headline = (
            "Shareholders meeting — X Limited has informed the Exchange regarding results of "
            "voting at the meeting for the quarter"
        )
        assert tag_headline(headline).event_type is EventType.ROUTINE
        assert split_subject(headline)[0] == "Shareholders meeting"
        assert split_subject("Receipt of order worth Rs 840 crore") == (
            "Receipt of order worth Rs 840 crore",
            "",
        )

    def test_a_regulatory_order_is_never_an_order_win(self) -> None:
        assert tag_headline("Action(s) taken or orders passed — demand order").event_type is (
            EventType.GOVERNANCE
        )


class TestTheNumberOnTheAnswer:
    """The probability of the chosen word — never laya's entropy score (OV11, 26 Sep 2026)."""

    def test_answer_confidence_is_read_first_then_the_chosen_probability(self) -> None:
        both = {"choice": "order", "answer_confidence": 0.98, "probabilities": {"order": 0.97}}
        assert chosen_probability(both, "order") == 0.98
        only_probabilities = {"choice": "order", "probabilities": {"order": 0.61, "routine": 0.3}}
        assert chosen_probability(only_probabilities, "order") == 0.61

    def test_a_payload_carrying_only_the_entropy_score_is_not_an_answer(self) -> None:
        # laya 0.3.20: on a `choice` question `confidence` is 1 - H(p)/log(k), how concentrated
        # the whole distribution is — three-way 0.60/0.20/0.20 reads about 0.14 there. It was
        # compared to the 0.60 floor from 25 to 26 Sep 2026; the readers now refuse it.
        entropy_only = {"choice": "order", "confidence": 0.93}
        assert chosen_probability(entropy_only, "order") is None
        assert tag_from_laya(entropy_only) is None
        assert chosen_probability({"choice": "order", "answer_confidence": True}, "order") is None

    def test_the_key_names_the_generation_and_the_question(self) -> None:
        assert question_schema_hash(LAYA_QUESTIONS) == TAG_SCHEMA
        changed = {"event_type": {**LAYA_QUESTIONS["event_type"], "instructions": "Say what."}}
        assert question_schema_hash(changed) != TAG_SCHEMA
        assert cache_key("x").split(":")[:3] == ["catalyst_tag", "v2", TAG_SCHEMA]


class TestTheAdverseReader:
    """Deterministic, from the headline, whatever tag the page shows (OV11 finding 3)."""

    def test_a_regulatory_or_court_order_a_penalty_a_default_or_a_resignation_is_adverse(
        self,
    ) -> None:
        assert adverse_phrases("SEBI order against the company") == ("sebi order",)
        assert adverse_phrases(
            "Action(s) taken or orders passed — receipt of Demand Order under Section 156"
        ) == ("orders passed", "action(s) taken")
        assert adverse_filing("Resignation of Chief Financial Officer")
        assert adverse_filing("Default in payment of interest on NCDs")
        assert set(adverse_phrases("NCLT admits insolvency petition")) == {"nclt", "insolvency"}

    def test_governance_that_is_not_adverse_and_every_other_type_is_not(self) -> None:
        assert adverse_phrases("Credit rating reaffirmed at AA-") == ()
        assert not adverse_filing("Bagging/Receiving of orders/contracts — Rs 840 crore order")
        assert not adverse_filing("Closure of trading window")
        assert not adverse_filing(None) and not adverse_filing("  ")

    def test_the_adverse_phrases_are_all_rules_the_headline_reader_knows(self) -> None:
        governance = {
            phrase
            for event_type, phrases in RULES
            if event_type is EventType.GOVERNANCE
            for phrase in phrases
        }
        assert governance >= ADVERSE_PHRASES
