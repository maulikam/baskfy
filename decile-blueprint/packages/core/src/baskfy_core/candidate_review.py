"""A candidate row, in words, for Laya's opinion on how much it deserves a look.

Pure (law 1): numbers in, a sentence and a typed question out. Nothing here reads a table or a
model; the sidecar asks the question and the API serves the answer.

MAULIK'S DECISION, AND WHAT WAS MEASURED (25 Sep 2026)
------------------------------------------------------
"Let's keep text and technical both": the technicals each scan shortlisted a stock on go to
Laya beside the filing, so the model has what the scan had. Two things were settled first.

**Words, not numbers.** Laya is a text encoder; `"rvol": 4.2` is tokens to it, with no sense
that 4.2 is high. So `describe_row` turns each strategy's stored facts into the sentence a
person would say — "volume 6 times its average, gapped up 9%, base 3% deep" — and that is the
state. The numbers are the scans' own, restated; none is computed here.

**The question is attention, never a trade.** `look_first | worth_a_look | skip` answers "how
much does this row deserve a look before the others on the list". It is not "buy", it is not
an input to any rank, size or order path, and the page says so. A buy label would be a
recommendation (D3, human-track) sitting one click from the gateway.

**Measured on the base checkpoint, zero-shot, on eight rows built the way this module builds
them:** every answer was near a third each way (confidence 0.00 to 0.16), and the row with a 9%
gap on six times its volume under a ₹840 crore order was a coin flip. So the opinion is shown
only above `REVIEW_CONFIDENCE_FLOOR` — today that is rarely — and the labels a person puts on
rows (`candidate_review_label`) are what the fine-tune trains on. The plumbing is the point:
when a checkpoint that has learned these rows exists, it drops in under the same key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final


class ReviewLabel(StrEnum):
    LOOK_FIRST = "look_first"
    WORTH_A_LOOK = "worth_a_look"
    SKIP = "skip"


#: Tenths of the day's range: the top tenth is a strong close, the upper half a fair one.
_TOP_TENTH: Final = 9
_UPPER_HALF: Final = 5

SOURCE_LAYA: Final = "laya"
SOURCE_LABELLED: Final = "labelled"

#: Below this the opinion is "not sure" on the page. The same bar the headline tag uses.
REVIEW_CONFIDENCE_FLOOR: Final = 0.60

REVIEW_QUESTIONS: Final[dict[str, dict[str, object]]] = {
    "review_priority": {
        "type": "choice",
        "instructions": (
            "`setup` describes a technical pattern a screener found on a stock today, in words. "
            "`filing` is the newest material exchange filing for it. How much does this row "
            "deserve a trader's attention before the others on the same list?"
        ),
        "criteria": {
            ReviewLabel.LOOK_FIRST.value: (
                "the pattern is strong and the filing is the kind of news that produces it "
                "(an order win, a result, an approval, an expansion)"
            ),
            ReviewLabel.WORTH_A_LOOK.value: (
                "the pattern is real but the filing is routine or unknown, or the filing is "
                "good but the pattern is weak"
            ),
            ReviewLabel.SKIP.value: (
                "the pattern is weak or stale, or the filing is adverse (a regulatory order, "
                "a default, a resignation under a cloud)"
            ),
        },
    }
}


@dataclass(frozen=True, slots=True)
class RowFacts:
    """One strategy's stored numbers about the row, as the scan wrote them. Any may be absent."""

    strategy: str
    detail: str
    actionable: bool
    numbers: dict[str, Decimal | int | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReviewOpinion:
    label: ReviewLabel
    confidence: float
    source: str = SOURCE_LAYA


def _pct(value: Decimal | int | None, places: int = 0) -> str | None:
    if value is None:
        return None
    return f"{Decimal(value):.{places}f}%"


def _times(value: Decimal | int | None) -> str | None:
    if value is None:
        return None
    return f"{Decimal(value):.1f} times".replace(".0 times", " times")


def _swing_words(facts: RowFacts) -> str:
    n = facts.numbers
    setup, _, status = facts.detail.partition(" · ")
    what = {
        "EP": "episodic pivot",
        "FLAG": "flag",
        "PARABOLIC_SHORT": "parabolic run (detect only)",
    }.get(setup, setup.lower())
    where = {
        "GAP_DAY": "gap day",
        "BREAKOUT_TODAY": "breakout today above the pivot",
        "SETTING_UP": "still setting up below the pivot",
        "TRIGGERED": "triggered",
    }.get(status, status.lower().replace("_", " "))
    parts = [f"Swing {what}, {where}"]
    if (gap := _pct(n.get("gap_pct"))) is not None:
        parts.append(f"gapped {gap}")
    if (rvol := _times(n.get("rvol"))) is not None:
        parts.append(f"volume {rvol} its average")
    if (depth := _pct(n.get("base_depth_pct"))) is not None:
        parts.append(f"base {depth} deep")
    if (prior := _pct(n.get("prior_move_pct"))) is not None:
        parts.append(f"up {prior} in the prior move")
    if (adr := _pct(n.get("adr_pct"), 1)) is not None:
        parts.append(f"average daily range {adr}")
    if (bars := n.get("base_bars")) is not None:
        parts.append(f"base {int(bars)} sessions long")
    return ": ".join([parts[0], "; ".join(parts[1:])]) if len(parts) > 1 else parts[0]


def _vbt_words(facts: RowFacts) -> str:
    n = facts.numbers
    head = (
        "Volume breakout signal"
        if facts.actionable
        else "Volume breakout scanned but rejected by the filters"
    )
    parts: list[str] = []
    if (rvol := _times(n.get("rvol"))) is not None:
        parts.append(f"volume {rvol} its 50-day average")
    if (chg := n.get("change_pct")) is not None:
        parts.append(f"closed {'up' if Decimal(chg) >= 0 else 'down'} {abs(Decimal(chg)):.0f}%")
    if (pos := n.get("close_position")) is not None:
        tenth = min(int(Decimal(pos) * 10), 9)
        parts.append(
            "in the top tenth of the day's range"
            if tenth >= _TOP_TENTH
            else "in the upper half of the day's range"
            if tenth >= _UPPER_HALF
            else "in the lower half of the day's range"
        )
    if (ret := n.get("ret_20_pct")) is not None:
        direction = "up" if Decimal(ret) >= 0 else "down"
        parts.append(f"{direction} {abs(Decimal(ret)):.0f}% over the last 20 sessions")
    return f"{head}: {'; '.join(parts)}" if parts else head


def _twt_words(facts: RowFacts) -> str:
    n = facts.numbers
    head = (
        "Three weeks tight, entry today" if facts.actionable else "Three weeks tight, in the state"
    )
    parts: list[str] = []
    if (rng := _pct(n.get("week_range_pct"), 1)) is not None:
        parts.append(f"three weekly closes within {rng} of each other")
    if (sessions := n.get("sessions_in_state")) is not None:
        parts.append(f"{int(sessions)} session{'' if int(sessions) == 1 else 's'} in the state")
    if (ratio := n.get("month_low_ratio")) is not None:
        parts.append(f"{(Decimal(ratio) - 1) * 100:.0f}% above its three-month low")
    return f"{head}: {'; '.join(parts)}" if parts else head


_WORDS: Final = {
    "swing": _swing_words,
    "volume_breakout": _vbt_words,
    "three_weeks_tight": _twt_words,
}


def describe_row(facts: list[RowFacts] | tuple[RowFacts, ...]) -> str:
    """The row's technicals as a person would say them, one sentence per strategy."""
    sentences = [_WORDS.get(f.strategy, lambda x: f"{x.strategy}: {x.detail}")(f) for f in facts]
    return " ".join(s.rstrip(".") + "." for s in sentences)


def review_state(
    facts: list[RowFacts] | tuple[RowFacts, ...], filing: str | None
) -> dict[str, str]:
    """What Laya is shown: the technicals in words and the filing headline. Nothing else —
    no symbol, no price, no rank — so two names with the same facts get the same opinion."""
    return {
        "setup": describe_row(facts),
        "filing": filing.strip() if filing and filing.strip() else "No filing on record.",
    }


def review_key(state: dict[str, str]) -> str:
    """Content-addressed: the same state is asked once and answered identically."""
    canonical = json.dumps(state, sort_keys=True, ensure_ascii=False)
    return "candidate_review:v1:" + hashlib.sha256(canonical.encode()).hexdigest()


def opinion_from_laya(answer: object) -> ReviewOpinion | None:
    if not isinstance(answer, dict):
        return None
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    if not isinstance(choice, str) or not isinstance(confidence, int | float):
        return None
    try:
        label = ReviewLabel(choice)
    except ValueError:
        return None
    return ReviewOpinion(label, round(float(confidence), 4))


def shown(opinion: ReviewOpinion | None) -> bool:
    """Whether the page shows the opinion as a word rather than "not sure"."""
    return opinion is not None and (
        opinion.source == SOURCE_LABELLED or opinion.confidence >= REVIEW_CONFIDENCE_FLOOR
    )
