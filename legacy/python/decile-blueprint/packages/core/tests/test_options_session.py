"""``docs/options/04`` §11 — the session machine: seven edges, nothing else, random edge walks."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from baskfy_core.options.config import Sleeve
from baskfy_core.options.session import (
    EDGES,
    NEVER_OPENED,
    TERMINAL,
    DuplicateSession,
    IllegalTransition,
    Session,
    SessionState,
    new_session,
    plan_expires_at,
    plan_is_live,
    transition,
)

S = SessionState
DAY = dt.date(2026, 10, 27)

#: The spec's edges, written out from ``04`` §11 / condor §9 rather than read from the module.
SPEC_EDGES = {
    (S.OBSERVING, S.SKIPPED),
    (S.OBSERVING, S.PLANNED),
    (S.PLANNED, S.LAPSED),
    (S.PLANNED, S.CONFIRMED),
    (S.CONFIRMED, S.OPEN),
    (S.CONFIRMED, S.CLOSED),
    (S.OPEN, S.CLOSED),
}


def _reason(frm: SessionState, to: SessionState) -> str | None:
    if to is S.SKIPPED:
        return "GAP_TOO_BIG"
    if to is S.CLOSED:
        return NEVER_OPENED if frm is S.CONFIRMED else "PROFIT"
    return None


def test_the_module_has_exactly_the_specs_edges() -> None:
    assert set(EDGES) == SPEC_EDGES


@pytest.mark.parametrize(("frm", "to"), list(itertools.product(SessionState, SessionState)))
def test_every_pair(frm: SessionState, to: SessionState) -> None:
    session = Session(Sleeve.O1M, DAY, frm)
    if (frm, to) in SPEC_EDGES:
        assert transition(session, to, _reason(frm, to)).state is to
    else:
        with pytest.raises(IllegalTransition):
            transition(session, to, _reason(frm, to))


def test_terminal_states_have_no_way_out() -> None:
    assert {S.SKIPPED, S.LAPSED, S.CLOSED} == set(TERMINAL)
    for frm, _ in SPEC_EDGES:
        assert frm not in TERMINAL


@settings(max_examples=500, deadline=None)
@given(st.lists(st.sampled_from(list(SessionState)), max_size=8))
def test_random_edge_walks(targets: list[SessionState]) -> None:
    """A walk either follows an edge or raises and leaves the session where it was."""
    session = new_session(Sleeve.O2, DAY, ())
    for to in targets:
        before = session
        try:
            session = transition(session, to, _reason(session.state, to))
        except IllegalTransition:
            assert (before.state, to) not in SPEC_EDGES
            session = before
            continue
        assert (before.state, session.state) in SPEC_EDGES


class TestReasons:
    def test_a_skip_carries_its_reason(self) -> None:
        with pytest.raises(IllegalTransition, match="reason"):
            transition(Session(Sleeve.O1M, DAY), S.SKIPPED)

    def test_confirmed_never_opened(self) -> None:
        with pytest.raises(IllegalTransition, match="NEVER_OPENED"):
            transition(Session(Sleeve.O1M, DAY, S.CONFIRMED), S.CLOSED, "PROFIT")
        closed = transition(Session(Sleeve.O1M, DAY, S.CONFIRMED), S.CLOSED, NEVER_OPENED)
        assert closed.reason == NEVER_OPENED

    def test_open_closes_with_an_exit_code(self) -> None:
        for bad in (None, NEVER_OPENED):
            with pytest.raises(IllegalTransition):
                transition(Session(Sleeve.O1M, DAY, S.OPEN), S.CLOSED, bad)
        assert (
            transition(Session(Sleeve.O1M, DAY, S.OPEN), S.CLOSED, "HARD_EXIT").reason
            == "HARD_EXIT"
        )


class TestOnePerSleevePerDate:
    def test_a_second_session_is_refused(self) -> None:
        first = new_session(Sleeve.O1M, DAY, ())
        with pytest.raises(DuplicateSession):
            new_session(Sleeve.O1M, DAY, (first,))

    def test_other_sleeves_and_dates_are_fine(self) -> None:
        first = new_session(Sleeve.O1M, DAY, ())
        assert new_session(Sleeve.O3A, DAY, (first,)).sleeve is Sleeve.O3A
        assert new_session(Sleeve.O1M, DAY + dt.timedelta(days=7), (first,)).state is S.OBSERVING


class TestPlanExpiry:
    """``expires_at = min(issued + 30 min, entry window end)``."""

    def test_the_window_end_binds_o1(self) -> None:
        issued = dt.datetime(2026, 10, 27, 10, 0)
        assert plan_expires_at(issued, dt.time(10, 15), 30) == dt.datetime(2026, 10, 27, 10, 15)

    def test_thirty_minutes_bind_o2(self) -> None:
        issued = dt.datetime(2026, 10, 21, 11, 20)
        assert plan_expires_at(issued, dt.time(13, 30), 30) == dt.datetime(2026, 10, 21, 11, 50)

    def test_live_strictly_before_expiry(self) -> None:
        expires = dt.datetime(2026, 10, 27, 10, 15)
        assert plan_is_live(expires, expires - dt.timedelta(seconds=1))
        assert not plan_is_live(expires, expires)
