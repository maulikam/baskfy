"""PM8: the paper book's rules equal the research's (book of record, trigger lines, pool marks, overlay)."""

from __future__ import annotations

import numpy as np

from portfolioml import paper as PP
from portfolioml import timing as T


def test_book_skips_names_held_in_the_last_twelve_tranches() -> None:
    recent = [[1, 2]] + [[100 + k] for k in range(11)]  # names 1 and 2 bought 12 tranches ago: still held
    assert PP.book_picks([1, 2, 3, 4], recent, n=2) == [3, 4]
    assert PP.book_picks([1, 2, 3, 4], recent + [[9]], n=2) == [1, 2]  # now 13 tranches ago: matured, eligible again


def test_triggers_take_the_first_high_in_the_window_in_session_then_rank_order() -> None:
    watch = {10: [(1, 1), (2, 2), (3, 3), (4, 4)]}
    fired = {(1, 15), (2, 12), (3, 12), (4, 40), (1, 13)}  # 4 fires after the 21-session window
    got = PP.triggers(watch, lambda i, t: (i, t) in fired, through=50, already=[])
    assert [(f.iid, f.e) for f in got] == [(2, 12), (3, 12), (1, 13)]


def test_triggers_cap_twenty_a_tranche_and_never_double_a_held_name() -> None:
    watch = {0: [(i, i) for i in range(1, 31)], 30: [(1, 1)]}
    every = PP.triggers(watch, lambda i, t: True, through=100, already=[])
    first = [f for f in every if f.sig == 0]
    assert len(first) == PP.MAX_LINES and [f.iid for f in first] == list(range(1, 21))
    assert not [f for f in every if f.sig == 30]  # name 1 is still held from tranche 0 (fired at 1, held 250 sessions)


def test_triggers_are_incremental_without_duplicates() -> None:
    watch = {0: [(1, 1), (2, 2)]}
    fired = {(1, 3), (2, 8)}
    day5 = PP.triggers(watch, lambda i, t: (i, t) in fired, through=5, already=[])
    day9 = PP.triggers(watch, lambda i, t: (i, t) in fired, through=9, already=day5)
    assert [f.iid for f in day5] == [1] and [f.iid for f in day9] == [2]


def test_pool_marks_equal_the_research_pool_book_on_complete_paths() -> None:
    rng = np.random.default_rng(7)
    lines = [(s, np.cumprod(1 + rng.normal(0, 0.02, PP.HORIZON)), PP.SLOTS["hi63_20"]) for s in range(5, 400, 7)]
    n = 700
    ours, inv = PP.pool_marks(lines, n, 5)
    theirs, _, inv_ref = T.pool_book_detail([T.Line(s, p, w) for s, p, w in lines], n, 5)
    assert np.allclose(ours, theirs) and np.allclose(inv, inv_ref)


def test_pool_marks_keep_an_open_line_at_the_end_of_the_data() -> None:
    p = np.array([1.0, 1.1, 1.2])  # three sessions held so far, horizon not reached
    eq, inv = PP.pool_marks([(1, p, 0.5)], 4, 1)
    assert inv[-1] > 0.5  # still invested, not sold on the last day


def test_overlay_goes_to_cash_five_sessions_after_the_index_breaks_its_100_dma() -> None:
    idx = np.concatenate([np.linspace(100, 200, 150), np.full(40, 120.0)])
    book = np.ones(len(idx))
    _, held = PP.overlay_marks(book, idx)
    breaks = 150  # the first close under the 100-DMA
    assert held[breaks + 5] == 1 and held[breaks + 7] == 0  # confirmed after 5 closes, traded the session after
