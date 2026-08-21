"""Session state machine and the lockout that survives a restart.

    PREOPEN -> GATED --(veto)--> SKIPPED
        |
    WAITING_ENTRY --(window expires)--> NO_ENTRY
        |
    MANAGING <-> ADJUSTING
        |
    EXITING -> LOCKED_OUT           (terminal for the day)

    any state + feed stale / broker down -> EMERGENCY_EXIT -> LOCKED_OUT

LOCKED_OUT PERSISTS TO DISK. A process restart must not resume trading on a day that
already hit its stop — that is how one bad session becomes three. The lockout file records
which day and why, so a restart can tell "locked out today" from "never ran today".
"""
from __future__ import annotations

import datetime as dt
import json
import os
from enum import Enum
from typing import Any


class State(str, Enum):
    PREOPEN = "PREOPEN"
    GATED = "GATED"
    SKIPPED = "SKIPPED"
    WAITING_ENTRY = "WAITING_ENTRY"
    NO_ENTRY = "NO_ENTRY"
    MANAGING = "MANAGING"
    ADJUSTING = "ADJUSTING"
    EXITING = "EXITING"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    LOCKED_OUT = "LOCKED_OUT"


TERMINAL = {State.SKIPPED, State.NO_ENTRY, State.LOCKED_OUT}

_ALLOWED: dict[State, set[State]] = {
    State.PREOPEN: {State.GATED, State.EMERGENCY_EXIT},
    State.GATED: {State.SKIPPED, State.WAITING_ENTRY, State.EMERGENCY_EXIT},
    State.WAITING_ENTRY: {State.MANAGING, State.NO_ENTRY, State.EMERGENCY_EXIT},
    State.MANAGING: {State.ADJUSTING, State.EXITING, State.EMERGENCY_EXIT},
    State.ADJUSTING: {State.MANAGING, State.EXITING, State.EMERGENCY_EXIT},
    State.EXITING: {State.LOCKED_OUT, State.WAITING_ENTRY},   # re-entry is the one way back
    State.EMERGENCY_EXIT: {State.LOCKED_OUT},
    State.SKIPPED: set(), State.NO_ENTRY: set(), State.LOCKED_OUT: set(),
}


class IllegalTransition(RuntimeError):
    """The machine refuses rather than drifting into a state nothing else expects."""


class Session:
    def __init__(self, day: dt.date, lockout_path: str) -> None:
        self.day = day
        self.lockout_path = lockout_path
        self.state = State.PREOPEN
        self.history: list[tuple[State, str]] = []
        self.reentries_used = 0

    def to(self, nxt: State, reason: str = "") -> State:
        if nxt not in _ALLOWED[self.state]:
            raise IllegalTransition(f"{self.state.value} -> {nxt.value} ({reason})")
        self.history.append((nxt, reason))
        self.state = nxt
        if nxt in TERMINAL:
            self._persist(reason)
        return nxt

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    # --- lockout ----------------------------------------------------------------------
    def _persist(self, reason: str) -> None:
        rec = {"day": self.day.isoformat(), "state": self.state.value, "reason": reason,
               "at": dt.datetime.now().isoformat(timespec="seconds")}
        parent = os.path.dirname(self.lockout_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.lockout_path, "w") as fh:
            json.dump(rec, fh, indent=2)

    @staticmethod
    def locked_out(day: dt.date, lockout_path: str) -> dict[str, Any] | None:
        """The persisted terminal state for `day`, if any.

        A file for a DIFFERENT day is not a lockout: it is yesterday's, and returning it
        would silently refuse to trade forever.
        """
        if not os.path.exists(lockout_path):
            return None
        try:
            with open(lockout_path) as fh:
                rec = json.load(fh)
        except (ValueError, OSError):
            return None
        return rec if rec.get("day") == day.isoformat() else None
