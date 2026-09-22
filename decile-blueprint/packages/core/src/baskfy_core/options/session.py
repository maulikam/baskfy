"""The session state machine, per sleeve per date (``04`` §11; condor ``04`` §9).

::

    OBSERVING ──► SKIPPED                  (verdict SKIP, or the plan was REJECTED_*)
    OBSERVING ──► PLANNED                  (plan built)
    PLANNED   ──► LAPSED                   (no confirm by expires_at)
    PLANNED   ──► CONFIRMED                (POST /nifty-options/execute, confirm=true)
    CONFIRMED ──► OPEN                     (every entry leg filled)
    CONFIRMED ──► CLOSED / NEVER_OPENED    (ABANDONED_ENTRY)
    OPEN      ──► CLOSED / <exit code>     (an exit decision, every leg closed)

No other edge exists; ``transition`` raises on any other pair, and ``CLOSED`` carries a reason
that must match the edge it came by. One session per sleeve per trade date.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum

from baskfy_core.options.config import Sleeve


class SessionState(StrEnum):
    OBSERVING = "OBSERVING"
    SKIPPED = "SKIPPED"
    PLANNED = "PLANNED"
    LAPSED = "LAPSED"
    CONFIRMED = "CONFIRMED"
    OPEN = "OPEN"
    CLOSED = "CLOSED"


#: The reason a ``CONFIRMED`` session closes without opening (condor ``04`` §4.6).
NEVER_OPENED = "NEVER_OPENED"

EDGES: frozenset[tuple[SessionState, SessionState]] = frozenset(
    {
        (SessionState.OBSERVING, SessionState.SKIPPED),
        (SessionState.OBSERVING, SessionState.PLANNED),
        (SessionState.PLANNED, SessionState.LAPSED),
        (SessionState.PLANNED, SessionState.CONFIRMED),
        (SessionState.CONFIRMED, SessionState.OPEN),
        (SessionState.CONFIRMED, SessionState.CLOSED),
        (SessionState.OPEN, SessionState.CLOSED),
    }
)

TERMINAL: frozenset[SessionState] = frozenset(
    {SessionState.SKIPPED, SessionState.LAPSED, SessionState.CLOSED}
)


class IllegalTransition(ValueError):
    """An edge the machine does not have, or a ``CLOSED`` reason that does not fit its edge."""


class DuplicateSession(ValueError):
    """A second session for the same sleeve and trade date."""


@dataclass(frozen=True, slots=True)
class Session:
    sleeve: Sleeve
    trade_date: dt.date
    state: SessionState = SessionState.OBSERVING
    reason: str | None = None


def new_session(sleeve: Sleeve, trade_date: dt.date, existing: Iterable[Session]) -> Session:
    """Open today's session for ``sleeve``; refuses a second one (unique per sleeve per date)."""
    for other in existing:
        if other.sleeve is sleeve and other.trade_date == trade_date:
            raise DuplicateSession(f"{sleeve.value} already has a session on {trade_date}")
    return Session(sleeve=sleeve, trade_date=trade_date)


def transition(session: Session, to: SessionState, reason: str | None = None) -> Session:
    """Move along one of the machine's seven edges, or raise ``IllegalTransition``.

    * ``SKIPPED`` needs a reason (the skip code or ``REJECTED_*``).
    * ``CONFIRMED → CLOSED`` must say ``NEVER_OPENED``; ``OPEN → CLOSED`` must carry an exit code
      and must not say ``NEVER_OPENED``.
    """
    edge = (session.state, to)
    if edge not in EDGES:
        raise IllegalTransition(f"{session.state.value} → {to.value} is not an edge")
    if to is SessionState.SKIPPED and not reason:
        raise IllegalTransition("a skipped session carries its reason")
    if edge == (SessionState.CONFIRMED, SessionState.CLOSED) and reason != NEVER_OPENED:
        raise IllegalTransition("a confirmed session that never opened closes NEVER_OPENED")
    if edge == (SessionState.OPEN, SessionState.CLOSED) and (not reason or reason == NEVER_OPENED):
        raise IllegalTransition("an open session closes with its exit code")
    return replace(session, state=to, reason=reason)


def plan_expires_at(
    issued_at: dt.datetime, entry_window_end: dt.time, ttl_minutes: int
) -> dt.datetime:
    """``expires_at = min(issued_at + 30 min, entry_window_end)`` (``03`` §9, non-negotiable 1)."""
    window_end = dt.datetime.combine(issued_at.date(), entry_window_end, tzinfo=issued_at.tzinfo)
    return min(issued_at + dt.timedelta(minutes=ttl_minutes), window_end)


def plan_is_live(expires_at: dt.datetime, now: dt.datetime) -> bool:
    """A plan may be confirmed only strictly before it expires."""
    return now < expires_at
