"""PM0.12: classify one-day moves beyond 60 % against known corporate actions. Pure parsing and matching."""

from __future__ import annotations

import datetime as dt
import math
import re

MOVE, FIT = 0.60, math.log(1.5)  # the observed step must sit within 1.5× of what the action implies
SLACK_DAYS = 2
FALL_LOW, FALL_HIGH, FALL_FIT, FALL_MIN_FACTOR = 0.4, 0.8, math.log(1.15), 1.25
_SIDES = re.compile(r"(?:from\s*)?(?:rs\.?|re\.?|inr)\s*([\d.]+)[^\d]*?(?:per\s+share\s*)?to\s*(?:rs\.?|re\.?|inr)\s*([\d.]+)", re.I)
_BONUS = re.compile(r"bonus(?:\s+issue)?\s*[-:]?\s*(\d+)\s*:\s*(\d+)", re.I)


def action_terms(subject: str) -> list[tuple[str, float | None]]:
    """(class, factor) in a subject; ``factor`` is how many shares one becomes (0.1 for a 1-for-10 consolidation)."""
    text, out = subject.lower(), []
    bonus = _BONUS.search(text)
    if bonus and "debenture" not in text and "ncrp" not in text and "preference" not in text and int(bonus[2]) > 0:
        out.append(("bonus", (int(bonus[1]) + int(bonus[2])) / int(bonus[2])))
    sides = _SIDES.search(text)
    if sides and float(sides[2]) > 0:
        ratio = float(sides[1]) / float(sides[2])
        if "consolidat" in text and ratio < 1:
            out.append(("consolidation", ratio))
        elif ("split" in text or "sub-division" in text or "sub division" in text) and ratio > 1:
            out.append(("split", ratio))
    elif "consolidat" in text and "account" not in text and "financial" not in text:
        out.append(("consolidation", None))
    if not out and re.search(r"demerger|de-merger|scheme|sheme", text) and "bonus" not in text:
        out.append(("demerger", None))
    return out


def classify(step: float, candidates: list[tuple[str, float | None]]) -> tuple[str, float | None]:
    """(class, price multiplier for bars before the move), or unexplained. ``step`` is close over previous close."""
    if abs(step - 1) <= MOVE:
        return "not_a_move", None
    fits = [(abs(math.log(step * f)), c, f) for c, f in candidates if f]
    known = [(c, f) for c, f in candidates if f]
    if len(known) > 1:
        product = math.prod(f for _, f in known)
        fits.append((abs(math.log(step * product)), max(known, key=lambda kv: abs(math.log(kv[1])))[0], product))
    if fits:
        gap, name, factor = min(fits, key=lambda x: x[0])
        if gap <= FIT:
            return name, 1 / factor
    if step < 1 and any(c == "demerger" for c, _ in candidates):
        return "demerger", None
    return "unexplained", None


def classify_fall(step: float, candidates: list[tuple[str, float | None]]) -> tuple[str, float] | None:
    """A fall of 20–60 %: only a bonus or split that fits within 1.15× explains it, and only from a factor of 1.25."""
    if not FALL_LOW <= step <= FALL_HIGH:
        return None
    fits = [(abs(math.log(step * f)), c, f) for c, f in candidates if f and c in ("bonus", "split") and f >= FALL_MIN_FACTOR]
    if not fits:
        return None
    gap, name, factor = min(fits, key=lambda x: x[0])
    return (name, 1 / factor) if gap <= FALL_FIT else None


def in_window(ex_date: dt.date, previous_bar: dt.date, move: dt.date) -> bool:
    return previous_bar - dt.timedelta(days=SLACK_DAYS) < ex_date <= move + dt.timedelta(days=SLACK_DAYS)
