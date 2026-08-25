"""The lifecycle of a third-party manager, as a pure state machine.

Before this module a manager was a seed row: ``cb_manager`` held a slug, a name and a kind, and
the only two rows that ever existed were created by ``curated_seed``. There was no way for a
person to *become* one. This is the missing half — the states an application moves through and
the transitions that are legal between them.

Pure by construction (law 1): states in, decision out. Nothing here reads a database, a clock or
a network, so the rule a test asserts and the rule the API enforces are the same function.

## The decision this encodes, and why

**A suspended manager cannot be restored straight to approved.** The tempting transition is
``SUSPENDED -> APPROVED``, because an operator who suspended someone by mistake wants one click
back. It is refused anyway: suspension exists for the case where something is wrong with a
person's standing, and the only honest way out is the review that establishes it is wrong no
longer. So the path back is ``SUSPENDED -> SUBMITTED -> APPROVED``, which forces a decision to be
made a second time rather than reversed by muscle memory. A one-click undo would make suspension
mean "hidden for now", and that is not what it is for.

**Rejection is not terminal.** ``REJECTED -> SUBMITTED`` is legal: an application refused for a
missing disclosure should be fixable. Nothing is deleted on rejection, so the reviewer's reason
survives the next submission.

**Only APPROVED may publish.** :func:`may_publish` is the single predicate; no caller re-derives
it from a status string comparison, because that is how a fifth state eventually gets forgotten.

Nothing in this module decides *whether* someone should be approved. That is a human judgement
made against D3, and D3 is unreviewed.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "MANAGER_STATES",
    "PUBLISHABLE_STATES",
    "TRANSITIONS",
    "ManagerStateError",
    "may_publish",
    "next_state",
    "reachable_from",
]

#: Every state a manager identity may hold. Ordered as the happy path reads.
MANAGER_STATES: Final[tuple[str, ...]] = (
    "DRAFT",
    "SUBMITTED",
    "APPROVED",
    "REJECTED",
    "SUSPENDED",
)

#: The only states from which a basket may be published.
PUBLISHABLE_STATES: Final[frozenset[str]] = frozenset({"APPROVED"})

#: Legal transitions, as ``state -> the states it may become``.
#:
#: ``SUSPENDED`` deliberately does NOT reach ``APPROVED``; see the module docstring.
TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "DRAFT": frozenset({"SUBMITTED"}),
    "SUBMITTED": frozenset({"APPROVED", "REJECTED"}),
    "APPROVED": frozenset({"SUSPENDED"}),
    "REJECTED": frozenset({"SUBMITTED"}),
    "SUSPENDED": frozenset({"SUBMITTED"}),
}


class ManagerStateError(ValueError):
    """An illegal transition was attempted.

    A ``ValueError`` so an API layer's existing validation handling renders it as a 400 rather
    than a 500: asking for an impossible transition is a bad request, not a server fault.
    """

    def __init__(self, current: str, requested: str) -> None:
        self.current = current
        self.requested = requested
        legal = ", ".join(sorted(TRANSITIONS.get(current, frozenset()))) or "nothing"
        super().__init__(
            f"a manager in {current} cannot become {requested}; "
            f"from {current} the legal next states are {legal}"
        )


def reachable_from(state: str) -> frozenset[str]:
    """The states ``state`` may legally become. Unknown states reach nothing."""
    return TRANSITIONS.get(state, frozenset())


def next_state(current: str, requested: str) -> str:
    """Return ``requested`` if the transition is legal, else raise.

    Both arguments are validated against :data:`MANAGER_STATES` first, so a typo fails loudly
    here rather than writing a state nothing else understands into the database.
    """
    for name, value in (("current", current), ("requested", requested)):
        if value not in MANAGER_STATES:
            # Deliberately NOT a ManagerStateError: that type says "this transition is illegal",
            # which would be a misleading answer to "that is not a state at all".
            raise ValueError(
                f"{name} state {value!r} is not a manager state; "
                f"expected one of {', '.join(MANAGER_STATES)}"
            )
    if requested not in TRANSITIONS.get(current, frozenset()):
        raise ManagerStateError(current, requested)
    return requested


def may_publish(state: str) -> bool:
    """Whether a manager in ``state`` may publish a basket.

    The single predicate. Callers must not compare status strings themselves — routing every
    check through here is what stops a future sixth state from silently gaining publish rights.
    """
    return state in PUBLISHABLE_STATES
