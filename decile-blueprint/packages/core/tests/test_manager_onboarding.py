"""The manager lifecycle, asserted as a specification.

These tests state what the state machine must do, not what it happens to do. The one that
matters most is ``test_a_suspended_manager_cannot_be_restored_without_a_fresh_review``: if a
future edit adds ``SUSPENDED -> APPROVED`` for operator convenience, that test is the thing
that says no.
"""

from __future__ import annotations

import pytest

from baskfy_core.manager_onboarding import (
    MANAGER_STATES,
    PUBLISHABLE_STATES,
    TRANSITIONS,
    ManagerStateError,
    may_publish,
    next_state,
    reachable_from,
)


class TestTheLegalTransitions:
    @pytest.mark.parametrize(
        ("current", "requested"),
        [
            ("DRAFT", "SUBMITTED"),
            ("SUBMITTED", "APPROVED"),
            ("SUBMITTED", "REJECTED"),
            ("APPROVED", "SUSPENDED"),
            ("REJECTED", "SUBMITTED"),
            ("SUSPENDED", "SUBMITTED"),
        ],
    )
    def test_the_happy_paths_are_permitted(self, current: str, requested: str) -> None:
        assert next_state(current, requested) == requested

    @pytest.mark.parametrize(
        ("current", "requested"),
        [
            ("DRAFT", "APPROVED"),
            ("DRAFT", "REJECTED"),
            ("DRAFT", "SUSPENDED"),
            ("SUBMITTED", "SUSPENDED"),
            ("APPROVED", "APPROVED"),
            ("APPROVED", "REJECTED"),
            ("REJECTED", "APPROVED"),
            ("SUSPENDED", "REJECTED"),
        ],
    )
    def test_every_other_transition_is_refused(self, current: str, requested: str) -> None:
        with pytest.raises(ManagerStateError):
            next_state(current, requested)

    def test_no_state_may_transition_to_itself(self) -> None:
        """A no-op write is a bug, not a transition: it hides a missing guard upstream."""
        for state in MANAGER_STATES:
            assert state not in reachable_from(state), f"{state} reaches itself"

    def test_every_transition_target_is_a_real_state(self) -> None:
        for state, targets in TRANSITIONS.items():
            assert state in MANAGER_STATES
            for target in targets:
                assert target in MANAGER_STATES, f"{state} -> {target} is not a state"

    def test_every_state_has_a_transition_entry(self) -> None:
        """A state missing from the table silently reaches nothing, which is a trap."""
        assert set(TRANSITIONS) == set(MANAGER_STATES)


class TestSuspension:
    def test_a_suspended_manager_cannot_be_restored_without_a_fresh_review(self) -> None:
        """The decision this module exists to encode. Do not relax it for convenience.

        Suspension means something is wrong with a person's standing. The only honest way out is
        the review that establishes it is wrong no longer, so the path back runs through
        SUBMITTED. A one-click undo would turn suspension into "hidden for now".
        """
        with pytest.raises(ManagerStateError):
            next_state("SUSPENDED", "APPROVED")
        assert next_state("SUSPENDED", "SUBMITTED") == "SUBMITTED"
        assert next_state("SUBMITTED", "APPROVED") == "APPROVED"

    def test_a_suspended_manager_may_not_publish(self) -> None:
        assert may_publish("SUSPENDED") is False

    def test_the_refusal_names_what_is_actually_legal(self) -> None:
        """An error a person can act on beats one they have to look up."""
        with pytest.raises(ManagerStateError) as caught:
            next_state("SUSPENDED", "APPROVED")
        assert "SUBMITTED" in str(caught.value)


class TestPublishRights:
    def test_only_approved_may_publish(self) -> None:
        assert {s for s in MANAGER_STATES if may_publish(s)} == {"APPROVED"}

    def test_publishable_states_and_the_predicate_agree(self) -> None:
        """Two sources of truth drift; this asserts they are the same one."""
        assert {s for s in MANAGER_STATES if may_publish(s)} == set(PUBLISHABLE_STATES)

    def test_an_unknown_state_may_not_publish(self) -> None:
        """Fail closed: a state nobody has heard of does not get publish rights."""
        assert may_publish("SOMETHING_NEW") is False


class TestInputValidation:
    @pytest.mark.parametrize("bad", ["", "approved", "BANANA", "APPROVED "])
    def test_an_unrecognised_state_is_refused_loudly(self, bad: str) -> None:
        with pytest.raises(ValueError):
            next_state("DRAFT", bad)

    def test_an_unrecognised_current_state_is_not_reported_as_an_illegal_transition(self) -> None:
        """'That is not a state' and 'that transition is illegal' are different answers."""
        with pytest.raises(ValueError) as caught:
            next_state("BANANA", "SUBMITTED")
        assert not isinstance(caught.value, ManagerStateError)
        assert "current state" in str(caught.value)

    def test_reachable_from_an_unknown_state_is_empty_rather_than_raising(self) -> None:
        """A read-only query answers; only a write refuses."""
        assert reachable_from("BANANA") == frozenset()
