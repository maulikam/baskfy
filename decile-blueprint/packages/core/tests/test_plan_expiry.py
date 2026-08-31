"""Non-negotiable #1's clock, asserted at the second.

    "plans expire in 30 minutes"

The spec is thirty minutes, so these tests name thirty minutes. They do not sleep, they do not
read a wall clock, and they do not assert what the code happens to do: every case here is a
sentence from ``CLAUDE.md`` or from the desk's ``app/main.py:519-524`` turned into two
timestamps and an expected verdict.

The boundary is the point of the file. A test that only checked "an hour old is expired" would
pass against a TTL of ten minutes, of twenty-nine, or of an hour — it would lock in the *idea*
of expiry and measure none of it. 29:59 live and 30:01 dead is the pair that pins the number.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
from decimal import Decimal
from types import ModuleType

import pytest

from baskfy_core import curated_plans
from baskfy_core.curated_plans import (
    PLAN_TTL,
    build_invest_plan,
    plan_expires_at,
    plan_is_expired,
)


def _code_only(module: ModuleType) -> str:
    """The module's source with every comment and docstring removed.

    A prose scan over raw source is a trap: this file's own explanation of why there is no
    ``datetime.now()`` in it contains the string ``datetime.now(``. Unparsing the AST with the
    docstrings taken out leaves only what actually executes, so the assertion measures the code
    and not the paragraph above it.
    """
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body[0] = ast.Pass()
    return ast.unparse(ast.fix_missing_locations(tree))


#: An arbitrary but fixed instant. Timezone-aware, because production stamps IST.
ISSUED = dt.datetime(2026, 8, 31, 10, 0, 0, tzinfo=dt.UTC)


def _at(*, minutes: int = 0, seconds: int = 0) -> dt.datetime:
    return ISSUED + dt.timedelta(minutes=minutes, seconds=seconds)


class TestTheConstantIsThirtyMinutes:
    def test_plan_ttl_is_exactly_thirty_minutes(self) -> None:
        """The number in CLAUDE.md, not "some timedelta"."""
        thirty_minutes = dt.timedelta(minutes=30)
        assert thirty_minutes == PLAN_TTL
        assert PLAN_TTL.total_seconds() == 1800

    def test_the_deadline_is_the_issue_time_plus_the_ttl(self) -> None:
        assert plan_expires_at(ISSUED) == ISSUED + dt.timedelta(minutes=30)


class TestTheBoundary:
    """29:59 live, 30:01 dead — and 30:00 exactly, which the spec has to answer somehow."""

    def test_a_brand_new_plan_is_live(self) -> None:
        assert plan_is_expired(issued_at=ISSUED, now=ISSUED) is False

    def test_at_twenty_nine_minutes_fifty_nine_seconds_it_is_still_live(self) -> None:
        assert plan_is_expired(issued_at=ISSUED, now=_at(minutes=29, seconds=59)) is False

    def test_one_microsecond_before_the_deadline_it_is_still_live(self) -> None:
        """The tightest statement of "live for thirty minutes"."""
        almost = plan_expires_at(ISSUED) - dt.timedelta(microseconds=1)
        assert plan_is_expired(issued_at=ISSUED, now=almost) is False

    def test_at_exactly_thirty_minutes_it_has_expired(self) -> None:
        """The window is ``[issued, issued + PLAN_TTL)``.

        The desk's ``> 1800`` kept a plan alive for this one instant. Closing the interval at
        the top is the stricter reading of "expires in 30 minutes" and the safe direction: the
        cost of refusing is re-running a preview, the cost of accepting is an order priced off
        a quote that is by definition out of its stated window.
        """
        assert plan_is_expired(issued_at=ISSUED, now=_at(minutes=30)) is True

    def test_at_thirty_minutes_one_second_it_has_expired(self) -> None:
        assert plan_is_expired(issued_at=ISSUED, now=_at(minutes=30, seconds=1)) is True

    def test_hours_later_it_is_still_expired(self) -> None:
        assert plan_is_expired(issued_at=ISSUED, now=_at(minutes=600)) is True


class TestTheAwkwardCases:
    def test_a_clock_that_stepped_backwards_does_not_invalidate_a_live_plan(self) -> None:
        """``now`` before ``issued_at`` means the machine's clock moved, not that the plan died.

        Refusing here would turn an NTP correction into a wave of 410s on plans that are inside
        their window by their own stamp.
        """
        assert plan_is_expired(issued_at=ISSUED, now=_at(minutes=-5)) is False

    def test_a_future_stamped_plan_still_expires_a_ttl_after_its_stamp(self) -> None:
        """Tolerating a backwards clock must not become an unlimited lifetime."""
        assert plan_is_expired(issued_at=ISSUED, now=ISSUED + PLAN_TTL) is True

    def test_naive_and_aware_instants_are_refused_not_guessed(self) -> None:
        naive = ISSUED.replace(tzinfo=None)
        with pytest.raises(ValueError, match="timezone-aware"):
            plan_is_expired(issued_at=ISSUED, now=naive)
        with pytest.raises(ValueError, match="timezone-aware"):
            plan_is_expired(issued_at=naive, now=ISSUED)

    def test_two_naive_instants_are_fine(self) -> None:
        """Tests and fixtures use naive datetimes; only *mixing* them is ambiguous."""
        naive = ISSUED.replace(tzinfo=None)
        assert plan_is_expired(issued_at=naive, now=naive + dt.timedelta(minutes=31)) is True

    def test_the_predicate_reads_the_same_clock_in_any_zone(self) -> None:
        """A plan stamped in IST and checked in UTC is the same instant, so the same verdict."""
        ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
        issued_ist = ISSUED.astimezone(ist)
        assert plan_is_expired(issued_at=issued_ist, now=_at(minutes=29)) is False
        assert plan_is_expired(issued_at=issued_ist, now=_at(minutes=31)) is True


class TestThePredicateAndThePreviewAgree:
    """The hint the investor is shown must be the deadline that refuses them."""

    def test_expires_at_hint_is_exactly_when_the_predicate_flips(self) -> None:
        plan = build_invest_plan(
            target_weights={"RELIANCE": Decimal("1")},
            prices={"RELIANCE": Decimal("100")},
            amount=Decimal("1000"),
            now=ISSUED,
        )
        hint = plan["expires_at_hint"]
        assert hint == plan_expires_at(ISSUED)
        assert plan_is_expired(issued_at=ISSUED, now=hint - dt.timedelta(seconds=1)) is False
        assert plan_is_expired(issued_at=ISSUED, now=hint) is True


class TestThePredicateIsPure:
    """Law #1: core touches nothing. An expiry check that read the wall clock could not be
    tested at the second, and would have made the store's clock untestable too."""

    def test_the_module_names_no_ambient_clock(self) -> None:
        source = _code_only(curated_plans)
        for forbidden in ("datetime.now(", "dt.datetime.now(", "time.time(", "utcnow("):
            assert forbidden not in source, f"curated_plans.py reads an ambient clock: {forbidden}"

    def test_the_same_arguments_always_give_the_same_answer(self) -> None:
        first = plan_is_expired(issued_at=ISSUED, now=_at(minutes=29, seconds=59))
        second = plan_is_expired(issued_at=ISSUED, now=_at(minutes=29, seconds=59))
        assert first is False
        assert second is False
