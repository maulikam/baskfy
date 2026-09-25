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

Number = Decimal | int | str | bool | None

SOURCE_LAYA: Final = "laya"
SOURCE_LABELLED: Final = "labelled"
SOURCE_RULES: Final = "rules"

#: The filing subjects that make a row one to skip whatever the pattern says — the same
#: phrases `catalyst_tags` files under governance as adverse or regulatory.
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
#: Gate words that mean the strategy may not enter today.
SHUT_GATES: Final[frozenset[str]] = frozenset({"RED", "SHUT", "CLOSED", "OFF"})

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
    numbers: dict[str, Number] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RowContext:
    """The day around the row — the strategies' own gates and breadth, the sector, the screens
    the name ranks on. Closed-session facts only: nothing that moves during the day, so the
    state (and its key) holds still until the next session."""

    #: Per strategy name, its gate word that session ("GREEN", "OPEN", "SHUT" …), when known.
    gates: dict[str, str] = field(default_factory=dict)
    #: Per strategy name, its breadth reading in percent, when known.
    breadth_pct: dict[str, Decimal] = field(default_factory=dict)
    sector: str | None = None
    #: ``(screen name, rank, of)`` for each screen the name is on.
    screens: tuple[tuple[str, int | None, int | None], ...] = ()


def describe_context(context: RowContext | None) -> str:
    if context is None:
        return ""
    parts: list[str] = []
    for name, gate in context.gates.items():
        breadth = context.breadth_pct.get(name)
        line = f"{name} gate {gate.lower()}"
        if breadth is not None:
            line += f" with {Decimal(breadth):.0f}% of the universe above its long average"
        parts.append(line)
    if context.sector:
        parts.append(f"sector {context.sector.replace('-', ' ')}")
    if context.screens:
        named = [
            f"{name} #{rank} of {of}" if rank is not None and of is not None else name
            for name, rank, of in context.screens
        ]
        parts.append("on screens " + ", ".join(named))
    return ("; ".join(parts) + ".") if parts else ""


@dataclass(frozen=True, slots=True)
class ReviewOpinion:
    label: ReviewLabel
    confidence: float
    source: str = SOURCE_LAYA
    #: The rules' reasons, in words, when the rules answered; a person's note when labelled.
    reason: str | None = None


def _num(n: dict[str, Number], key: str) -> Decimal | None:
    """The stored number as a Decimal, or None — a flag or a word under that key is not a number."""
    value = n.get(key)
    if value is None or isinstance(value, bool | str):
        return None
    return Decimal(value)


def _pct(value: Decimal | None, places: int = 0) -> str | None:
    return None if value is None else f"{value:.{places}f}%"


def _signed_pct(value: Decimal) -> str:
    """``"4% above"`` / ``"3% below"`` — a distance a person would say."""
    return f"{abs(value):.0f}% {'above' if value >= 0 else 'below'}"


def _times(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{value:.1f} times".replace(".0 times", " times")


def _crore(value: Decimal) -> str:
    return f"average turnover Rs {value / 10_000_000:.1f} crore a day"


def _distance(n: dict[str, Number], key: str, what: str) -> str | None:
    close, level = _num(n, "close"), _num(n, key)
    if close is None or level is None or level <= 0:
        return None
    return f"{_signed_pct((close / level - 1) * 100)} {what}"


def _stop_below(n: dict[str, Number], entry_key: str, stop_key: str, what: str) -> str | None:
    entry, stop = _num(n, entry_key), _num(n, stop_key)
    if entry is None or stop is None or entry <= 0:
        return None
    return f"{what} {entry:.2f} with the stop {((entry - stop) / entry * 100):.1f}% below it"


def _flags(n: dict[str, Number]) -> list[str]:
    parts: list[str] = []
    if n.get("locked_upper_circuit") is True:
        parts.append("locked in the upper circuit")
    if n.get("listed_within_2y") is True:
        parts.append("listed within the last two years")
    failed = n.get("failed_filters")
    if isinstance(failed, str) and failed:
        parts.append(f"failed the {failed.lower().replace('_', ' ')} filter(s)")
    return parts


def _said(pieces: list[str | None]) -> list[str]:
    return [piece for piece in pieces if piece]


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
    head = f"Swing {what}, {where}"
    score, gap, rvol = _num(n, "score"), _pct(_num(n, "gap_pct")), _times(_num(n, "rvol"))
    depth, bars, tight = (
        _pct(_num(n, "base_depth_pct")),
        _num(n, "base_bars"),
        _num(n, "tightness_adr"),
    )
    dry, prior, adr = (
        _num(n, "dryup_ratio"),
        _pct(_num(n, "prior_move_pct")),
        _pct(_num(n, "adr_pct"), 1),
    )
    fast, slow, streak = (
        _num(n, "dist_ma_fast_pct"),
        _num(n, "dist_ma_slow_pct"),
        _num(n, "up_streak"),
    )
    turnover = _num(n, "turnover_avg")
    parts = _said(
        [
            None if score is None else f"setup score {score:.0f} of 100",
            None if gap is None else f"gapped {gap}",
            None if rvol is None else f"volume {rvol} its average",
            None if depth is None else f"base {depth} deep",
            None if bars is None else f"base {int(bars)} sessions long",
            None if tight is None else f"base tightness {tight:.1f} ADRs",
            None if dry is None else f"volume dried up to {dry:.2f} of its base average",
            None if prior is None else f"up {prior} in the prior move",
            None if adr is None else f"average daily range {adr}",
            None if fast is None else f"{_signed_pct(fast)} the fast moving average",
            None if slow is None else f"{_signed_pct(slow)} the slow moving average",
            None if streak is None or streak <= 0 else f"{int(streak)} up sessions in a row",
            _stop_below(n, "trigger", "stop_ref", "trigger"),
            _distance(n, "pivot_high", "the pivot"),
            None if turnover is None else _crore(turnover),
            *_flags(n),
        ]
    )
    return f"{head}: {'; '.join(parts)}" if parts else head


def _vbt_words(facts: RowFacts) -> str:
    n = facts.numbers
    head = (
        "Volume breakout signal"
        if facts.actionable
        else "Volume breakout scanned but rejected by the filters"
    )
    rvol, chg, pos, ret = (
        _times(_num(n, "rvol")),
        _num(n, "change_pct"),
        _num(n, "close_position"),
        _num(n, "ret_20_pct"),
    )
    tenth = None if pos is None else min(int(pos * 10), 9)
    turnover = _num(n, "turnover_avg_20")
    parts = _said(
        [
            None if rvol is None else f"volume {rvol} its 50-day average",
            None if chg is None else f"closed {'up' if chg >= 0 else 'down'} {abs(chg):.0f}%",
            None
            if tenth is None
            else "in the top tenth of the day's range"
            if tenth >= _TOP_TENTH
            else "in the upper half of the day's range"
            if tenth >= _UPPER_HALF
            else "in the lower half of the day's range",
            None
            if ret is None
            else f"{'up' if ret >= 0 else 'down'} {abs(ret):.0f}% over the last 20 sessions",
            _distance(n, "sma_200", "its 200-day average"),
            _distance(n, "ema_21", "its 21-day average"),
            _distance(n, "high_20_prior", "the prior 20-day high"),
            _stop_below(n, "limit_price", "stop_price", "entry limit"),
            None if turnover is None else _crore(turnover),
            *_flags(n),
        ]
    )
    return f"{head}: {'; '.join(parts)}" if parts else head


def _twt_words(facts: RowFacts) -> str:
    n = facts.numbers
    head = (
        "Three weeks tight, entry today" if facts.actionable else "Three weeks tight, in the state"
    )
    rng = _pct(_num(n, "week_range_pct"), 1)
    closes = [_num(n, "week_close_2"), _num(n, "week_close_1"), _num(n, "week_close_0")]
    sessions, out, ratio = (
        _num(n, "sessions_in_state"),
        _num(n, "sessions_out_before"),
        _num(n, "month_low_ratio"),
    )
    vol, avg, turnover, stop, close = (
        _num(n, "volume"),
        _num(n, "vol_sma_50"),
        _num(n, "turnover_avg_20"),
        _num(n, "stop_preview"),
        _num(n, "close"),
    )
    parts = _said(
        [
            None if rng is None else f"three weekly closes within {rng} of each other",
            None
            if any(c is None for c in closes)
            else "weekly closes "
            + ", ".join(f"{c:.2f}" for c in closes if c is not None)
            + " oldest first",
            None
            if sessions is None
            else f"{int(sessions)} session{'' if int(sessions) == 1 else 's'} in the state",
            None if out is None else f"{int(out)} sessions out of the state before this entry",
            None if ratio is None else f"{(ratio - 1) * 100:.0f}% above its three-month low",
            _distance(n, "sma_dma", "its 200-day average"),
            None
            if vol is None or avg is None or avg <= 0
            else f"volume {_times(vol / avg)} its 50-day average",
            None if turnover is None else _crore(turnover),
            None
            if stop is None or close is None or close <= 0
            else f"stop {((close - stop) / close * 100):.0f}% below the close",
            *_flags(n),
        ]
    )
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
    facts: list[RowFacts] | tuple[RowFacts, ...],
    filing: str | None,
    context: RowContext | None = None,
) -> dict[str, str]:
    """What Laya is shown: every number the scans stored about the row, said in words; the
    day's context (gates, breadth, sector, screens); and the filing headline. No symbol and no
    live price, so the state holds still within a session and the same facts get the same
    opinion. (Maulik, 25 Sep 2026: "all the parameters which swing has noticed, including the
    other parameters which we might have" — this is that set.)"""
    state = {
        "setup": describe_row(facts),
        "filing": filing.strip() if filing and filing.strip() else "No filing on record.",
    }
    words = describe_context(context)
    if words:
        state["context"] = words
    return state


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
        opinion.source in {SOURCE_LABELLED, SOURCE_RULES}
        or opinion.confidence >= REVIEW_CONFIDENCE_FLOOR
    )


def rules_opinion(
    facts: list[RowFacts] | tuple[RowFacts, ...],
    filing_event: str | None,
    filing_matched: tuple[str, ...] | list[str],
    filing_priority: str | None,
    context: RowContext | None = None,
) -> ReviewOpinion:
    """The attention baseline, from facts already on the row — the same idea as the headline
    tag's rules: always a word, always explainable, and the model or a person overrules it.

    Maulik, 25 Sep 2026, on a page that read "not sure" on every row: the base checkpoint's
    answers were 3-12% on fourteen live rows, so the column said nothing. This says something
    a person can check:

    * **skip** — no strategy could act on the row (a rejected scan, a base with no entry), or
      the filing is adverse (a regulatory order, a penalty, a default, a resignation), or the
      acting strategy's gate is shut, or the name is locked in the upper circuit;
    * **look first** — a strategy could act and the filing is material (an order win, a
      result, an approval);
    * **worth a look** — a strategy could act and the filing is routine, unknown or absent.

    It is display context, not a score: nothing reads it downstream, and the reasons are on
    the wire so the page can say why.
    """
    reasons: list[str] = []
    actionable = [f for f in facts if f.actionable]
    adverse = filing_event == "governance" and any(
        phrase in ADVERSE_PHRASES for phrase in filing_matched
    )
    locked = any(f.numbers.get("locked_upper_circuit") is True for f in facts)
    gates = context.gates if context is not None else {}
    acting_names = {
        {
            "swing": "Swing",
            "volume_breakout": "Volume breakout",
            "three_weeks_tight": "Three weeks tight",
        }.get(f.strategy, f.strategy)
        for f in actionable
    }
    shut = [name for name in acting_names if gates.get(name, "").upper() in SHUT_GATES]
    if not actionable:
        reasons.append("no strategy could act on the row as it stands")
    if adverse:
        reasons.append("the filing is adverse")
    if locked:
        reasons.append("locked in the upper circuit")
    if shut:
        reasons.append(f"{', '.join(sorted(shut))} gate shut")
    if reasons:
        return ReviewOpinion(ReviewLabel.SKIP, 1.0, SOURCE_RULES, "; ".join(reasons))
    if filing_priority == "high":
        return ReviewOpinion(
            ReviewLabel.LOOK_FIRST,
            1.0,
            SOURCE_RULES,
            f"a strategy could act and the filing is material ({filing_event})",
        )
    what = (
        "no filing on record"
        if filing_event is None
        else f"the filing is {filing_event.replace('_', ' ')}"
    )
    return ReviewOpinion(
        ReviewLabel.WORTH_A_LOOK, 1.0, SOURCE_RULES, f"a strategy could act; {what}"
    )
