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
import json
import re
from collections.abc import Mapping
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
        (
            "sebi order",
            "nclt",
            "court order",
            "insolvency",
            "penalty",
            "default",
            # NSE's own subject for regulatory action: "Action(s) taken or orders passed" —
            # a demand order, a show-cause, a tax notice. Not an order win (25 Sep 2026, seen on
            # the box: "orders passed regarding receipt of Demand Order under Section 156").
            "orders passed",
            "action(s) taken",
            "actions taken",
            "demand order",
            "show cause",
            "show-cause",
        ),
    ),
    # Holdings disclosures under SAST / PIT are routine paperwork, whatever the subject line
    # says around them; they come before every material type because "acquisition" appears in
    # the regulation's own name (25 Sep 2026, the box).
    (
        EventType.ROUTINE,
        (
            "sebi takeover regulations",
            "sast",
            "regulation 29",
            "regulation 31",
            "regulation 7(2)",
            "prohibition of insider trading",
            "shareholders meeting",
            "annual general meeting",
            "scrutinizer",
            "scrutinizers",
            "srutinizer",
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
            "bagging/receiving of orders/contracts",
            "receipt of orders",
            "receipt of order",
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


#: The feed writes NSE's own subject line first, then the exchange's sentence about it:
#: ``"Bagging/Receiving of orders/contracts — Bharat Electronics Limited has informed the
#: Exchange regarding Receipt of orders worth Rs. 840 crores"``. The subject is the exchange's
#: category, and it is what a person scans; the sentence repeats the company's name and a
#: boilerplate clause that says nothing about the event.
SUBJECT_SEPARATOR: Final = " — "


def split_subject(headline: str) -> tuple[str, str]:
    """``(subject, rest)`` of an NSE headline; ``(headline, "")`` when it carries no subject."""
    subject, separator, rest = headline.partition(SUBJECT_SEPARATOR)
    if not separator:
        return headline.strip(), ""
    return subject.strip(), rest.strip()


def _first_hit(text: str) -> CatalystTag | None:
    for event_type, phrases in _COMPILED:
        hits = tuple(phrase for phrase, pattern in phrases if pattern.search(text))
        if hits:
            return CatalystTag(event_type, PRIORITY_OF[event_type], hits)
    return None


def tag_headline(headline: str | None) -> CatalystTag:
    """The tag for one exchange headline. Deterministic: the same string always tags the same.

    **The subject line is read first** (25 Sep 2026, the box): NSE's category — "Financial
    Results", "Copy of Newspaper Publication", "General Updates" — decides when it names an
    event, and only a subject that names nothing ("General Updates", "Updates") lets the rest of
    the headline decide. Reading the whole string at once let the boilerplate win: a "Shareholders
    meeting" notice that mentions a quarter tagged as a result.

    A blank or absent headline is ``other`` at low priority with nothing matched — there is
    nothing to read, and the tag says so rather than inventing a subject.
    """
    if headline is None or not headline.strip():
        return CatalystTag(EventType.OTHER, ReviewPriority.LOW, ())
    subject, rest = split_subject(headline)
    tagged = _first_hit(subject.casefold())
    if tagged is not None:
        return tagged
    if rest:
        tagged = _first_hit(rest.casefold())
        if tagged is not None:
            return tagged
    return CatalystTag(EventType.OTHER, ReviewPriority.LOW, ())


#: The governance phrases that make a filing adverse — a regulatory or court order, a penalty,
#: a default, a resignation. The first GOVERNANCE block above plus the resignation pair from the
#: second; `adverse_filing` is the one reader of this set, and `candidate_review` skips on it.
ADVERSE_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "sebi order",
        "nclt",
        "court order",
        "insolvency",
        "penalty",
        "default",
        "orders passed",
        "action(s) taken",
        "actions taken",
        "demand order",
        "show cause",
        "show-cause",
        "resignation",
        "resigns",
    }
)


def adverse_phrases(headline: str | None) -> tuple[str, ...]:
    """The adverse phrases the rules matched on this headline, in match order; empty when the
    rules do not read it as governance with one of `ADVERSE_PHRASES`.

    **Deterministic and independent of the tag the page shows** (OV11, 26 Sep 2026). The
    resolved tag may be Laya's or a person's, and a model tag has no phrases at all — so a
    confident `corporate_action` on "SEBI order" used to hand the attention baseline an empty
    ``matched`` and the skip vanished. The rules read the headline again here, and the flag
    survives whatever wins the Filing column.
    """
    tag = tag_headline(headline)
    if tag.event_type is not EventType.GOVERNANCE:
        return ()
    return tuple(phrase for phrase in tag.matched if phrase in ADVERSE_PHRASES)


def adverse_filing(headline: str | None) -> bool:
    """Whether the rules read the headline as an adverse filing. See `adverse_phrases`."""
    return bool(adverse_phrases(headline))


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


#: The cache-key generation. **v2 (OV11, 26 Sep 2026):** the payload's ``confidence`` became the
#: calibrated probability of the chosen answer (`chosen_probability`), so every v1 answer — whose
#: ``confidence`` was a normalised-entropy score on another scale — must never be read against
#: the floor again. Bump this whenever the payload's meaning changes; the schema hash below
#: covers the question changing.
CACHE_KEY_VERSION: Final = "v2"


def question_schema_hash(questions: Mapping[str, object]) -> str:
    """Eight hex characters over the question dict, canonically serialised. Part of every cache
    key, so a changed instruction, criterion or vocabulary is a new key and an answer to the old
    question is never served as an answer to the new one."""
    canonical = json.dumps(questions, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


TAG_SCHEMA: Final = question_schema_hash(LAYA_QUESTIONS)


def cache_key(headline: str) -> str:
    """Where a model tag for this exact headline lives in the cache — content-addressed, so the
    same headline on two names or two days is tagged once and served identically. The key names
    the payload generation and the question it answers: ``catalyst_tag:v2:<schema>:<sha256>``.
    Mirrored in `infra/laya/laya_loop.py`; a test there asserts the two agree."""
    digest = hashlib.sha256(headline.strip().casefold().encode()).hexdigest()
    return f"catalyst_tag:{CACHE_KEY_VERSION}:{TAG_SCHEMA}:{digest}"


def chosen_probability(answer: Mapping[str, object], choice: str) -> float | None:
    """The probability Laya put on the answer it chose, or ``None`` when the payload does not say.

    Read from ``answer_confidence`` — what laya ≥ 0.3.20 reports as the calibrated number, the
    one temperature scaling fits and the README's gating relies on — else from that choice's
    entry in ``probabilities``. **Never from ``confidence``:** on a `choice` question that field
    is ``1 - H(p)/log(k)``, how concentrated the whole distribution is, on a different scale
    (three-way p = 0.60/0.20/0.20 reads 0.14 there). Both readers used it against the 0.60
    floor from 25 Sep 2026 until OV11 (26 Sep 2026) found the mistake; the sidecar now stores it
    under ``entropy_confidence`` so it is on record without being compared to the floor.
    """
    value = answer.get("answer_confidence")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict):
        p = probabilities.get(choice)
        if isinstance(p, int | float) and not isinstance(p, bool):
            return float(p)
    return None


def tag_from_laya(answer: object) -> CatalystTag | None:
    """Laya's `event_type` answer as a tag, or ``None`` when the answer is not one.

    Reads the shape the sidecar caches — ``{"choice": "<type>", "answer_confidence": 0.98,
    "probabilities": {...}, ...}`` — and nothing that is not in the eight-type vocabulary becomes
    a tag. The number is the chosen answer's probability (`chosen_probability`); a payload that
    carries only the entropy ``confidence`` is not an answer. ``matched`` is empty: a model has
    no phrase to point at, and the wire says so rather than inventing one.
    """
    if not isinstance(answer, dict):
        return None
    choice = answer.get("choice")
    if not isinstance(choice, str):
        return None
    confidence = chosen_probability(answer, choice)
    if confidence is None:
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


#: A person's word, written over both readers on `/build/overlap`. The correction is the label
#: the fine-tune trains on, so it wins on the page — a corpus whose labels the page contradicts
#: is a corpus nobody trusts.
SOURCE_CORRECTED: Final = "corrected"


def resolve_tag(
    rules: CatalystTag, laya: CatalystTag | None, corrected: EventType | None = None
) -> CatalystTag:
    """The tag the page shows: a person's correction when there is one; else Laya when it is
    sure, the rules when it is not — and the other source's word carried as ``disagrees_with``
    whenever the two read the headline differently.

    A correction has no phrase to point at and no probability, so ``matched`` is empty and
    ``confidence`` is ``None``; ``disagrees_with`` names what it overruled — the model's word
    when the model read it differently, otherwise the rules' when they did, otherwise nothing.

    Pure and total: with no model answer and no correction the rules tag comes back as it was.
    """
    if corrected is not None:
        overruled: CatalystTag | None = None
        if laya is not None and laya.event_type is not corrected:
            overruled = laya
        elif rules.event_type is not corrected:
            overruled = rules
        return CatalystTag(
            corrected,
            PRIORITY_OF[corrected],
            (),
            source=SOURCE_CORRECTED,
            confidence=None,
            disagrees_with=(
                None if overruled is None else f"{overruled.source}:{overruled.event_type.value}"
            ),
        )
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
