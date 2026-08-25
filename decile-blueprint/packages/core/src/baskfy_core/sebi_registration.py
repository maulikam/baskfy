"""Capture a manager's SEBI registration as structured, checkable data.

``cb_manager.sebi_reg_no`` was a bare nullable string. A string cannot say which registration it
is, whether it has lapsed, or whether anyone ever checked it — so the field could hold anything
and the product had no way to tell a real number from a typo from an invention.

This module is the format half of the fix: a constrained registration type, a syntactic check per
type, and a validity window. Pure (law 1) — no registry is contacted, no clock is read.

## What this does NOT claim, which matters more than what it does

A number that passes :func:`check_format` is **well-formed, and nothing else**. It is not
verified, not current, and it emphatically does not mean the holder may manage anyone's money.

Three separate facts get confused constantly, so they are three separate things here:

1. **Well-formed** — :func:`check_format`, this module. Cheap, offline, syntactic.
2. **Verified** — somebody compared the number against the SEBI register. That is an operator
   action with a date, stored as ``sebi_reg_verified_at``; it is NULL until then and this module
   never sets it.
3. **Compliant** — whether Baskfy may host this person's baskets at all. That is D3, D3 is
   posture B, and posture B is ⚠ UNREVIEWED pending counsel. Nothing in this codebase decides it.

Because the validity window is stored rather than computed, "is this registration current?" takes
the caller's own as-of date: a pure function cannot read a clock, and a registration's currency is
a point-in-time question that a background job and a request handler must answer identically.

The formats below are syntactic patterns for the registration prefixes SEBI issues, and AMFI's
ARN for distributors. They are recorded here because a typo caught at capture is worth catching;
they are not a compliance check, and a number that fails a pattern should be surrendered to a
human rather than silently rejected as fake — see :func:`check_format`'s ``UNKNOWN`` type.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "REGISTRATION_PATTERNS",
    "REGISTRATION_TYPES",
    "FormatVerdict",
    "check_format",
    "is_current",
    "normalise",
]

#: The registration kinds a manager may declare. ``NONE`` is a real answer — an operator-curated
#: basket that is not investment advice may legitimately have no registration — and ``UNKNOWN``
#: exists so a number in a shape we do not recognise can still be stored and reviewed by a person
#: instead of being thrown away at the door.
REGISTRATION_TYPES: Final[tuple[str, ...]] = (
    "NONE",
    "RESEARCH_ANALYST",
    "INVESTMENT_ADVISER",
    "PORTFOLIO_MANAGER",
    "AIF",
    "MF_DISTRIBUTOR",
    "UNKNOWN",
)

#: Syntactic patterns only. Anchored, and case-folded by :func:`normalise` before matching.
REGISTRATION_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "RESEARCH_ANALYST": re.compile(r"^INH\d{9}$"),
    "INVESTMENT_ADVISER": re.compile(r"^INA\d{9}$"),
    "PORTFOLIO_MANAGER": re.compile(r"^INP\d{9}$"),
    "AIF": re.compile(r"^IN/AIF[123]/\d{2}-\d{2}/\d{4}$"),
    "MF_DISTRIBUTOR": re.compile(r"^ARN-\d{1,6}$"),
}


@dataclass(frozen=True, slots=True)
class FormatVerdict:
    """The outcome of a syntactic check.

    ``well_formed`` is the only thing this type asserts. It is named that way rather than
    ``valid`` on purpose: ``valid`` invites a reader to believe the registration is good.
    """

    registration_type: str
    normalised: str
    well_formed: bool
    reason: str

    def __post_init__(self) -> None:
        if self.registration_type not in REGISTRATION_TYPES:
            raise ValueError(
                f"{self.registration_type!r} is not a registration type; "
                f"expected one of {', '.join(REGISTRATION_TYPES)}"
            )
        if self.well_formed and self.registration_type in {"NONE", "UNKNOWN"}:
            raise ValueError(
                "NONE and UNKNOWN are never well-formed registrations: "
                "NONE has no number to check and UNKNOWN is the answer when no pattern matched"
            )


def normalise(number: str) -> str:
    """Upper-case and strip a registration number so storage and comparison agree.

    Interior whitespace is removed as well: registration numbers are copied off certificates and
    arrive with stray spaces, and ``INH 000001234`` is the same registration as ``INH000001234``.
    """
    if not isinstance(number, str):  # pragma: no cover - defended by the type checker
        raise TypeError(f"a registration number must be a str, not {type(number).__name__}")
    return re.sub(r"\s+", "", number).upper()


def check_format(registration_type: str, number: str | None) -> FormatVerdict:
    """Check ``number`` against the pattern for ``registration_type``.

    ``NONE`` with no number is well-formed in the sense that there is nothing wrong with it, but
    it is reported as ``well_formed=False`` because there is no registration to be well-formed —
    the ``reason`` says so. Callers asking "may I store this?" should read ``reason``; callers
    asking "did a pattern match?" should read ``well_formed``.

    A number that matches no pattern comes back as ``UNKNOWN`` rather than an exception. Refusing
    it outright would mean a manager holding a registration shape we have not seen cannot even
    apply, and the honest outcome there is a human looking at it.
    """
    if registration_type not in REGISTRATION_TYPES:
        raise ValueError(
            f"{registration_type!r} is not a registration type; "
            f"expected one of {', '.join(REGISTRATION_TYPES)}"
        )
    if registration_type == "NONE":
        if number not in (None, ""):
            raise ValueError(
                "a registration type of NONE cannot carry a number; "
                "declare the type that number belongs to, or UNKNOWN"
            )
        return FormatVerdict("NONE", "", False, "no registration declared")
    if number is None or not number.strip():
        raise ValueError(f"registration type {registration_type} requires a number")

    cleaned = normalise(number)
    pattern = REGISTRATION_PATTERNS.get(registration_type)
    if pattern is None:  # UNKNOWN was declared explicitly
        return FormatVerdict("UNKNOWN", cleaned, False, "no pattern for this type; needs review")
    if pattern.match(cleaned):
        return FormatVerdict(registration_type, cleaned, True, "matches the expected pattern")
    matched = [name for name, pat in REGISTRATION_PATTERNS.items() if pat.match(cleaned)]
    if matched:
        return FormatVerdict(
            "UNKNOWN",
            cleaned,
            False,
            f"declared {registration_type} but the number matches {matched[0]}; needs review",
        )
    return FormatVerdict("UNKNOWN", cleaned, False, "matches no known pattern; needs review")


def is_current(
    *,
    valid_from: dt.date | None,
    valid_to: dt.date | None,
    as_of: dt.date,
) -> bool:
    """Whether a registration's window contains ``as_of``.

    ``as_of`` is required and has no default: a default would mean reading the clock, which law 1
    forbids and which would make a background job and a request handler disagree about the same
    registration on the same day.

    An open end (``valid_to=None``) is treated as still current — SEBI's perpetual registrations
    have no expiry — but an absent ``valid_from`` is NOT treated as beginning at the dawn of time:
    with no start date there is no window, so the answer is ``False`` and somebody must supply one.
    """
    if valid_from is None:
        return False
    if as_of < valid_from:
        return False
    return valid_to is None or as_of <= valid_to
