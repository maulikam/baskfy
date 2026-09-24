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

import hashlib
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
    #: A model's probability for its choice; ``None`` for the rules, which have no such number.
    confidence: float | None = None
    #: ``"<source>:<event_type>"`` of the other reader when it read the headline differently.
    disagrees_with: str | None = None


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


# --- Laya: the model column, resolved against the rules ------------------------------------
#
# Maulik, 25 Sep 2026: "let use laya use as soon as possible and complete it". Measured before
# wiring (this workspace, CPU, `convaiinnovations/laya` 0.3.20 zero-shot on fourteen NSE-style
# headlines): the unambiguous ones — an order win, USFDA, results, a directorate change — come
# back right at 0.90 to 0.99; the ambiguous ones do not ("SEBI order" → corporate_action 0.86,
# "Resignation of CFO" → corporate_action 0.38 over governance 0.37, "commencement of commercial
# production" → other 0.35). So the model is used **where it is sure and the rules where it is
# not**: `resolve_tag` takes Laya's answer when its confidence clears `LAYA_CONFIDENCE_FLOOR`,
# otherwise the rules tag, and either way records the other source's word when they disagree —
# that disagreement list is the correction seed for the fine-tune. The `noul` "is this
# material" question was tried and dropped: it answered 0.001 on the ₹840 crore order.

#: Laya's own name for its source on the wire; the rules keep `SOURCE_RULES`.
SOURCE_LAYA: Final = "laya"

#: Below this Laya's answer is not shown as the tag; the rules are. 0.60 sits above every wrong
#: answer and below every right one in the fourteen-headline check — a number to revisit with
#: the corrected corpus, not a constant of nature.
LAYA_CONFIDENCE_FLOOR: Final = 0.60

#: The one question, in Laya's typed-question schema. A `choice` over the eight types with the
#: exchange's own subject vocabulary as criteria; the state is `{"headline": ...}` and nothing
#: else — no price, no setup, no filing — so the model can only ever read what the rules read.
LAYA_QUESTIONS: Final[dict[str, dict[str, object]]] = {
    "event_type": {
        "type": "choice",
        "instructions": (
            "What kind of corporate event does the exchange filing `headline` announce?"
        ),
        "criteria": {
            EventType.EARNINGS.value: "quarterly or annual financial results, or guidance",
            EventType.ORDER.value: "winning an order, contract, tender or letter of award",
            EventType.APPROVAL.value: "a regulatory approval, licence, certification or patent",
            EventType.FUNDRAISING.value: (
                "raising capital: QIP, preferential issue, rights issue, warrants, debentures"
            ),
            EventType.GOVERNANCE.value: (
                "a director, auditor or KMP change, a credit rating, or a regulatory or court order"
            ),
            EventType.CORPORATE_ACTION.value: (
                "dividend, buyback, bonus, split, merger, acquisition, stake, JV, "
                "capacity expansion"
            ),
            EventType.ROUTINE.value: (
                "a routine compliance notice: trading window, certificates, meetings, publications"
            ),
            EventType.OTHER.value: "the headline does not say what the event is",
        },
    }
}


def laya_state(headline: str) -> dict[str, str]:
    """The state Laya is shown: the headline, and only the headline."""
    return {"headline": headline}


def cache_key(headline: str) -> str:
    """Where a model tag for this exact headline lives in the cache — content-addressed, so the
    same headline on two names or two days is tagged once and served identically."""
    return "catalyst_tag:v1:" + hashlib.sha256(headline.strip().casefold().encode()).hexdigest()


def tag_from_laya(answer: object) -> CatalystTag | None:
    """Laya's `event_type` answer as a tag, or ``None`` when the answer is not one.

    Reads the shape `Agent.predict` returns — ``{"choice": "<type>", "confidence": 0.98, ...}``
    — and nothing that is not in the eight-type vocabulary becomes a tag. ``matched`` is empty:
    a model has no phrase to point at, and the wire says so rather than inventing one.
    """
    if not isinstance(answer, dict):
        return None
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    if not isinstance(choice, str) or not isinstance(confidence, int | float):
        return None
    try:
        event_type = EventType(choice)
    except ValueError:
        return None
    return CatalystTag(
        event_type,
        PRIORITY_OF[event_type],
        (),
        source=SOURCE_LAYA,
        confidence=round(float(confidence), 4),
    )


def resolve_tag(rules: CatalystTag, laya: CatalystTag | None) -> CatalystTag:
    """The tag the page shows: Laya when it is sure, the rules when it is not — and the other
    source's word carried as ``disagrees_with`` whenever the two read the headline differently.

    Pure and total: with no model answer the rules tag comes back as it was.
    """
    if laya is None:
        return rules
    if laya.confidence is not None and laya.confidence >= LAYA_CONFIDENCE_FLOOR:
        chosen, other = laya, rules
    else:
        chosen, other = rules, laya
    if other.event_type is chosen.event_type:
        return chosen
    return CatalystTag(
        chosen.event_type,
        chosen.review_priority,
        chosen.matched,
        source=chosen.source,
        confidence=chosen.confidence,
        disagrees_with=f"{other.source}:{other.event_type.value}",
    )
