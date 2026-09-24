"""A rules-based tag for an exchange headline — the baseline an enrichment model has to beat.

Pure: a string in, a small frozen record out. No network, no clock, no disk (law 1).

WHY RULES FIRST
---------------
Maulik evaluated Laya (a typed-decision classifier) for tagging the NSE announcements the swing
feed links on `/build/overlap`, and settled the order: a keyword baseline in production first,
corrections collected against it, and only then a fine-tuned model shadowed against the
baseline. The base checkpoint is near chance zero-shot and there is no labelled corpus of NSE
headlines yet, so a model column today would be a column of noise with a confidence attached.
This module is the baseline, and the ``source`` field on every tag says so.

WHAT A TAG IS, AND IS NOT
-------------------------
A tag restates the **headline's subject** in one of eight words and a review priority. It reads
the headline the exchange published and nothing else — the feed stores a headline, a stamp and a
link, never the filing (SW11B, A3) — so "Disclosure under Regulation 30" alone is ``other`` at
low priority, not a guess at what the PDF says. It is display context on a candidate row: it
never enters a rank, a filter, a size or an order, and `test_overlap_readonly.py` keeps the
read model that carries it free of every execution name.

The vocabulary is the one settled in session (25 Sep 2026): ``earnings | order | approval |
fundraising | governance | corporate_action | routine | other``. Priority follows the event
type — an order, a result or an approval is what an episodic-pivot reader opens first; a
trading-window closure is what they skip — and is fixed per type rather than scored, so two
readers see the same priority on the same headline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class EventType(StrEnum):
    EARNINGS = "earnings"
    ORDER = "order"
    APPROVAL = "approval"
    FUNDRAISING = "fundraising"
    GOVERNANCE = "governance"
    CORPORATE_ACTION = "corporate_action"
    ROUTINE = "routine"
    OTHER = "other"


class ReviewPriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: The one source this module produces. A model, when it comes, writes its own name here.
SOURCE_RULES: Final = "rules"

#: Priority is a property of the event type, not of the headline — fixed, so it is reproducible.
PRIORITY_OF: Final[dict[EventType, ReviewPriority]] = {
    EventType.ORDER: ReviewPriority.HIGH,
    EventType.EARNINGS: ReviewPriority.HIGH,
    EventType.APPROVAL: ReviewPriority.HIGH,
    EventType.FUNDRAISING: ReviewPriority.MEDIUM,
    EventType.CORPORATE_ACTION: ReviewPriority.MEDIUM,
    EventType.GOVERNANCE: ReviewPriority.MEDIUM,
    EventType.ROUTINE: ReviewPriority.LOW,
    EventType.OTHER: ReviewPriority.LOW,
}

#: Phrases per type, matched case-insensitively on word boundaries. **Order matters**: the first
#: block with a hit wins, so the material types come before the routine ones — "Updates — award
#: of order" is an order, "Outcome of board meeting — financial results" is a result, and
#: "Approval of financial results" is a result, not an approval. A type may appear twice.
#: Phrases are the exchange's own subject vocabulary, not a general English lexicon.
RULES: Final[tuple[tuple[EventType, tuple[str, ...]], ...]] = (
    # Adverse and regulatory first: "SEBI order" and "NCLT order" contain the word order and
    # are not one, and a reader wants them above everything else on the row.
    (
        EventType.GOVERNANCE,
        ("sebi order", "nclt", "court order", "insolvency", "penalty", "default"),
    ),
    (
        EventType.EARNINGS,
        (
            "financial results",
            "results",
            "quarterly",
            "earnings",
            "guidance",
            "q1",
            "q2",
            "q3",
            "q4",
            "half yearly",
            "audited",
            "unaudited",
        ),
    ),
    (
        EventType.ORDER,
        (
            "order",
            "orders",
            "contract",
            "contracts",
            "letter of award",
            "letter of intent",
            "loa",
            "work order",
            "purchase order",
            "tender",
            "bagging",
            "bagged",
            "project win",
            "supply agreement",
        ),
    ),
    (
        EventType.APPROVAL,
        (
            "usfda",
            "us fda",
            "fda",
            "anda",
            "eir",
            "approval",
            "approvals",
            "approved",
            "licence",
            "license",
            "dcgi",
            "cdsco",
            "who-gmp",
            "gmp",
            "patent",
            "clearance",
            "certification",
        ),
    ),
    (
        EventType.EARNINGS,
        (
            "financial results",
            "results",
            "quarterly",
            "earnings",
            "guidance",
            "q1",
            "q2",
            "q3",
            "q4",
            "half yearly",
            "audited",
            "unaudited",
        ),
    ),
    (
        EventType.FUNDRAISING,
        (
            "qip",
            "qualified institutions placement",
            "preferential",
            "rights issue",
            "fund raising",
            "fund raise",
            "raising of funds",
            "allotment",
            "warrants",
            "ncd",
            "ncds",
            "debenture",
            "debentures",
            "fccb",
            "capital raise",
            "issue of equity",
            "issue of shares",
        ),
    ),
    (
        EventType.CORPORATE_ACTION,
        (
            "dividend",
            "buyback",
            "buy-back",
            "bonus",
            "split",
            "sub-division",
            "subdivision",
            "record date",
            "merger",
            "amalgamation",
            "scheme of arrangement",
            "demerger",
            "acquisition",
            "acquire",
            "acquires",
            "acquired",
            "stake",
            "joint venture",
            "jv",
            "mou",
            "memorandum of understanding",
            "capacity expansion",
            "expansion",
            "capex",
            "commencement of commercial production",
        ),
    ),
    (
        EventType.GOVERNANCE,
        (
            "resignation",
            "resigns",
            "appointment",
            "appointed",
            "cessation",
            "change in director",
            "change in directorate",
            "change in management",
            "key managerial personnel",
            "kmp",
            "cfo",
            "ceo",
            "managing director",
            "auditor",
            "auditors",
            "credit rating",
            "rating",
            "outcome of board meeting",
            "board meeting",
        ),
    ),
    (
        EventType.ROUTINE,
        (
            "trading window",
            "closure of trading window",
            "regulation 74",
            "reg. 74",
            "reg 74",
            "compliance certificate",
            "certificate under",
            "newspaper publication",
            "newspaper advertisement",
            "loss of share certificate",
            "loss of share certificates",
            "issue of duplicate",
            "analyst",
            "analysts",
            "institutional investor meet",
            "investor meet",
            "investor presentation",
            "annual report",
            "agm",
            "egm",
            "annual general meeting",
            "extraordinary general meeting",
            "postal ballot",
            "shareholding pattern",
            "related party",
            "esop",
            "reconciliation of share capital",
            "corporate governance report",
            "voting results",
            "scrutinizer",
            "book closure",
            "intimation",
            "clarification",
            "reply to clarification",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class CatalystTag:
    event_type: EventType
    review_priority: ReviewPriority
    #: The phrases that decided it, in the order they matched — so a reader can see why.
    matched: tuple[str, ...]
    source: str = SOURCE_RULES


def _compile(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])")


_COMPILED: Final[tuple[tuple[EventType, tuple[tuple[str, re.Pattern[str]], ...]], ...]] = tuple(
    (event_type, tuple((phrase, _compile(phrase)) for phrase in phrases))
    for event_type, phrases in RULES
)


def tag_headline(headline: str | None) -> CatalystTag:
    """The tag for one exchange headline. Deterministic: the same string always tags the same.

    A blank or absent headline is ``other`` at low priority with nothing matched — there is
    nothing to read, and the tag says so rather than inventing a subject.
    """
    text = (headline or "").casefold()
    if not text.strip():
        return CatalystTag(EventType.OTHER, ReviewPriority.LOW, ())
    for event_type, phrases in _COMPILED:
        hits = tuple(phrase for phrase, pattern in phrases if pattern.search(text))
        if hits:
            return CatalystTag(event_type, PRIORITY_OF[event_type], hits)
    return CatalystTag(EventType.OTHER, ReviewPriority.LOW, ())
