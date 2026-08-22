"""The screener's tracker and the desk's planner use ONE band rule (M17, P3.8).

`docs/03` §3a: the screener's `plan_rebalance` is a symbol diff, the desk's `build_plan` is a money
plan, and "the desk's is the real one and the screener's is a degenerate case of it". M17 makes
that literal rather than rhetorical: both now call :func:`baskfy_core.rank_buffer.inside_hold_band`.

They are not the same rule end to end, and pretending otherwise would be the mistake. The desk's is
this band **plus** a replacement-edge hurdle: a name that falls out of the band is kept anyway
unless a challenger beats it by `REPLACEMENT_EDGE` points. So:

* **inside the band** the two must agree exactly — that is what the shared predicate guarantees;
* **outside it** the desk keeps names the tracker exits, and that difference is deliberate.

This pins both halves, so a future edit to either cannot quietly move the boundary.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from baskfy_core import basket
from baskfy_core.rank_buffer import HeldName, ScreenRank, inside_hold_band, plan_rebalance

TOP_N = 12
BUFFER = 5


@pytest.mark.parametrize(
    ("rank", "expected"),
    [
        (1, True),  # core
        (TOP_N, True),  # the last name in the target set
        (TOP_N + 1, True),  # first name in the band
        (TOP_N + BUFFER, True),  # docs/07: `<= top_n + hold_buffer` HOLDS
        (TOP_N + BUFFER + 1, False),  # one worse exits
        (999, False),
    ],
)
def test_the_band_boundary_is_where_docs_07_puts_it(rank: int, expected: bool) -> None:
    assert inside_hold_band(rank, TOP_N, BUFFER) is expected


def test_a_zero_buffer_collapses_the_band() -> None:
    """`hold_buffer = 0` is the plain "rebalance to the top N" rule, with no hysteresis."""
    assert inside_hold_band(TOP_N, TOP_N, 0) is True
    assert inside_hold_band(TOP_N + 1, TOP_N, 0) is False


def test_the_tracker_keeps_exactly_the_names_the_predicate_keeps() -> None:
    """The screener's own consumer must agree with the shared predicate on every rank."""
    ranks = [
        ScreenRank(instrument_id=i, symbol=f"S{i:03d}", name=f"Name {i}", rank=i)
        for i in range(1, 41)
    ]
    held = [HeldName(instrument_id=i, symbol=f"S{i:03d}", name=f"Name {i}") for i in range(1, 41)]
    plan = plan_rebalance(ranks, held, top_n=TOP_N, hold_buffer=BUFFER)

    # Everything the rule did NOT send to the exit list is kept: held at rank <= N, or sitting
    # in the band between N and N + buffer.
    exited = {row.symbol for row in plan.exits}
    kept = {row.symbol for row in plan.holds} | {row.symbol for row in plan.inside_wrh}
    assert not (exited & kept), "a name is in both the exit list and a keep list"
    for entry in ranks:
        should_keep = inside_hold_band(entry.rank, TOP_N, BUFFER)
        assert (entry.symbol in kept) is should_keep, (
            f"{entry.symbol} at rank {entry.rank}: the tracker and the shared predicate disagree"
        )


def test_the_desks_extra_hurdle_is_the_only_difference() -> None:
    """Documented, not asserted by hand-waving: the desk's rule is this band PLUS a hurdle.

    `baskfy_core.basket` calls `inside_hold_band` for the band, then applies REPLACEMENT_EDGE to
    names that fall outside it. This checks the source says so, so that a refactor which drops the
    hurdle -- turning the desk's planner into the tracker -- fails here rather than in a live
    rebalance.
    """
    source = inspect.getsourcefile(basket)
    assert source is not None
    src = pathlib.Path(source).read_text()
    assert "inside_hold_band(r, n, cfg.RETENTION_BUFFER)" in src, (
        "the desk's planner no longer uses the shared band predicate"
    )
    assert "cfg.REPLACEMENT_EDGE" in src, (
        "the replacement-edge hurdle is gone; the desk would now exit every name that leaves "
        "the band, which is the tracker's rule and not the desk's"
    )
