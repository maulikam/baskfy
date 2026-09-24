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
    PRIORITY_OF,
    RULES,
    SOURCE_RULES,
    CatalystTag,
    EventType,
    ReviewPriority,
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
        assert imported <= {"__future__", "re", "dataclasses", "enum", "typing"}
