"""Portfolio-aware selection must not mutate SCORE — and, for C5, must apply its rules as specified.

The first three tests pin Phase 1.4 (``select_from_ranks``). Everything after them asserts
docs/ranking/PLAN.md contract C5 for ``select_portfolio``, rule by rule in the contract's order,
with the semantics the module docstring settles (and DECISIONS-MERGE "Ranking 2.E" records).
"""

from __future__ import annotations

import json
import pickle
import random
import struct
from collections.abc import Callable, Sequence
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from baskfy_core.rank_buffer import HeldName, ScreenRank, plan_rebalance
from baskfy_core.ranking_selection import (
    MIN_CORRELATION_OBSERVATIONS,
    Candidate,
    Holding,
    QualityRow,
    SelectionAction,
    SelectionConstraints,
    SelectionFlag,
    SelectionInput,
    SelectionReason,
    SelectionResult,
    SelectionRow,
    SelectionWarning,
    assert_score_unchanged,
    quality_rows_from_scored,
    select_from_ranks,
    select_portfolio,
)

# ---------------------------------------------------------------------------------------------
# Phase 1.4
# ---------------------------------------------------------------------------------------------


def test_select_from_ranks_uses_buffer_and_preserves_scores() -> None:
    ranked = (
        QualityRow(1, "AAA", "A", rank=1, score=90.0),
        QualityRow(2, "BBB", "B", rank=2, score=80.0),
        QualityRow(3, "CCC", "C", rank=3, score=70.0),
        QualityRow(4, "DDD", "D", rank=4, score=60.0),
        QualityRow(5, "EEE", "E", rank=5, score=50.0),
    )
    holdings = (
        HeldName(1, "AAA", "A", quantity=Decimal(10)),
        HeldName(5, "EEE", "E", quantity=Decimal(10)),  # rank 5 → exit when top_n=2, buffer=1
    )
    score_before = {r.symbol: r.score for r in ranked}

    plan = select_from_ranks(
        SelectionInput(ranked=ranked, holdings=holdings, top_n=2, hold_buffer=1)
    )

    assert_score_unchanged(score_before, {r.symbol: r.score for r in ranked})
    assert {r.symbol for r in plan.exits} == {"EEE"}
    assert {r.symbol for r in plan.entries} == {"BBB"}
    assert {r.symbol for r in plan.holds} == {"AAA"}


def test_assert_score_unchanged_on_dataframe() -> None:
    frame = pd.DataFrame(
        {
            "instrument_id": [1, 2],
            "symbol": ["AAA", "BBB"],
            "SCORE": [90.0, 80.0],
            "screen_rank": [1, 2],
            "name": ["A", "B"],
        }
    )
    snapshot = frame.copy()
    rows = quality_rows_from_scored(frame)
    plan = select_from_ranks(
        SelectionInput(
            ranked=rows,
            holdings=(HeldName(1, "AAA", "A", quantity=Decimal(1)),),
            top_n=1,
            hold_buffer=0,
        )
    )
    assert plan.holds[0].symbol == "AAA"
    # Selection never touched the frame.
    assert_score_unchanged(snapshot, frame)


def test_assert_score_unchanged_detects_mutation() -> None:
    before = pd.DataFrame({"SCORE": [1.0, 2.0]})
    after = pd.DataFrame({"SCORE": [1.0, 99.0]})
    with pytest.raises(AssertionError, match="SCORE"):
        assert_score_unchanged(before, after)


def test_assert_score_unchanged_refuses_a_frame_against_a_map() -> None:
    with pytest.raises(TypeError):
        assert_score_unchanged(pd.DataFrame({"SCORE": [1.0]}), {"AAA": 1.0})


# ---------------------------------------------------------------------------------------------
# C5 helpers
# ---------------------------------------------------------------------------------------------


def cand(
    instrument_id: int,
    rank: int,
    *,
    sector: str | None = None,
    adv: str | None = None,
    close: str | None = None,
) -> Candidate:
    return Candidate(
        instrument_id=instrument_id,
        symbol=f"S{instrument_id}",
        quality_rank=rank,
        sector=sector,
        adv_value_inr=Decimal(adv) if adv is not None else None,
        close_raw=Decimal(close) if close is not None else None,
    )


def hold(instrument_id: int, *, qty: int = 10, sector: str | None = None) -> Holding:
    return Holding(
        instrument_id=instrument_id,
        symbol=f"S{instrument_id}",
        quantity=Decimal(qty),
        sector=sector,
    )


def row(result: SelectionResult, instrument_id: int) -> SelectionRow:
    matches = [r for r in result.rows if r.instrument_id == instrument_id]
    assert len(matches) == 1, f"expected exactly one row for {instrument_id}, got {matches}"
    return matches[0]


def ids(rows: Sequence[SelectionRow]) -> list[int]:
    return [r.instrument_id for r in rows]


def wide(  # noqa: PLR0913 - one keyword per C5 constraint is the point of the helper
    *,
    max_names: int = 100,
    entry_rank: int = 15,
    retention_rank: int = 30,
    max_per_sector: int | None = None,
    capital_inr: Decimal | None = None,
    max_adv_participation_pct: Decimal | None = Decimal("1.0"),
    turnover_budget_names: int | None = None,
    max_correlation: float | None = None,
    correlation_window: int = 126,
) -> SelectionConstraints:
    """C5 constraints with a name limit wide enough to refuse nothing unless a test says so."""
    return SelectionConstraints(
        max_names=max_names,
        entry_rank=entry_rank,
        retention_rank=retention_rank,
        max_per_sector=max_per_sector,
        capital_inr=capital_inr,
        max_adv_participation_pct=max_adv_participation_pct,
        turnover_budget_names=turnover_budget_names,
        max_correlation=max_correlation,
        correlation_window=correlation_window,
    )


# ---------------------------------------------------------------------------------------------
# Rule 1 — holdings: hold inside retention, exit with a reason
# ---------------------------------------------------------------------------------------------


def test_holding_at_retention_rank_is_kept_and_one_worse_exits() -> None:
    result = select_portfolio(
        [cand(1, 30), cand(2, 31)], [hold(1), hold(2)], wide(entry_rank=15, retention_rank=30)
    )
    assert row(result, 1).action is SelectionAction.HOLD
    assert row(result, 1).reasons == (SelectionReason.RANK_WITHIN_RETENTION,)
    assert row(result, 2).action is SelectionAction.EXIT
    assert row(result, 2).reasons == (SelectionReason.RANK_OUTSIDE_RETENTION,)
    assert "31" in row(result, 2).explanation and "30" in row(result, 2).explanation


def test_holding_missing_from_results_exits_with_not_in_results_and_no_rank() -> None:
    result = select_portfolio([cand(1, 1)], [hold(9, qty=7)], wide())
    exit_row = row(result, 9)
    assert exit_row.action is SelectionAction.EXIT
    assert exit_row.reasons == (SelectionReason.NOT_IN_RESULTS,)
    assert exit_row.quality_rank is None
    assert exit_row.current_quantity == Decimal(7)
    assert result.exit_count == 1


def test_retention_is_the_rank_buffer_rule() -> None:
    """One hold-band rule: C5 retention == rank_buffer with top_n=entry, buffer=retention-entry."""
    rng = random.Random(20260913)
    for _ in range(200):
        entry = rng.randint(1, 20)
        retention = entry + rng.randint(0, 20)
        cands = [cand(i, rng.randint(1, 60)) for i in range(1, 41)]
        held_ids = rng.sample(range(1, 61), 12)  # some not in the results at all
        result = select_portfolio(
            cands,
            [hold(i) for i in held_ids],
            wide(entry_rank=entry, retention_rank=retention),
        )
        plan = plan_rebalance(
            [ScreenRank(c.instrument_id, c.symbol, c.symbol, c.quality_rank) for c in cands],
            [HeldName(i, f"S{i}", f"S{i}") for i in held_ids],
            top_n=entry,
            hold_buffer=retention - entry,
        )
        assert set(ids(result.exits)) == {r.instrument_id for r in plan.exits}


# ---------------------------------------------------------------------------------------------
# Rule 3 — entries within entry_rank, in quality order
# ---------------------------------------------------------------------------------------------


def test_only_candidates_within_entry_rank_are_considered() -> None:
    result = select_portfolio([cand(i, i) for i in range(1, 21)], [], wide(entry_rank=15))
    assert ids(result.entries) == list(range(1, 16))
    assert result.skips == ()
    assert all(r.reasons == (SelectionReason.RANK_WITHIN_ENTRY,) for r in result.entries)
    assert {r.instrument_id for r in result.rows}.isdisjoint(range(16, 21))


def test_a_held_candidate_is_a_hold_not_an_entry() -> None:
    result = select_portfolio([cand(1, 1), cand(2, 2)], [hold(1)], wide())
    assert row(result, 1).action is SelectionAction.HOLD
    assert ids(result.entries) == [2]


# ---------------------------------------------------------------------------------------------
# SECTOR_CAP
# ---------------------------------------------------------------------------------------------


def test_sector_cap_counts_kept_holdings_and_accepted_entrants() -> None:
    result = select_portfolio(
        [
            cand(10, 5, sector="bank"),  # held
            cand(1, 1, sector="bank"),
            cand(2, 2, sector="bank"),
            cand(3, 3, sector="it"),
            cand(4, 4, sector="it"),
        ],
        [hold(10, sector="bank")],
        wide(max_per_sector=2),
    )
    assert row(result, 1).action is SelectionAction.ENTER
    skipped = row(result, 2)
    assert skipped.action is SelectionAction.SKIP
    assert skipped.reasons == (SelectionReason.SECTOR_CAP,)
    assert "bank" in skipped.explanation and "2" in skipped.explanation
    assert ids(result.entries) == [1, 3, 4]
    assert dict(result.sector_counts) == {"bank": 2, "it": 2}


def test_unclassified_sector_never_counts_against_a_cap_and_is_flagged() -> None:
    result = select_portfolio(
        [cand(1, 1, sector=None), cand(2, 2, sector="unclassified"), cand(3, 3, sector="")],
        [],
        wide(max_per_sector=1),
    )
    assert ids(result.entries) == [1, 2, 3]
    assert all(SelectionFlag.SECTOR_UNCLASSIFIED in r.flags for r in result.entries)
    assert dict(result.sector_counts) == {"unclassified": 3}


def test_sector_unclassified_flag_only_when_a_cap_is_active() -> None:
    result = select_portfolio([cand(1, 1)], [], wide())
    assert result.entries[0].flags == ()


def test_holding_sector_falls_back_to_its_candidate_and_is_never_exited_for_a_cap() -> None:
    result = select_portfolio(
        [cand(10, 20, sector="bank"), cand(11, 21, sector="bank"), cand(1, 1, sector="bank")],
        [hold(10, sector=None), hold(11, sector="bank")],
        wide(max_per_sector=1),
    )
    assert row(result, 10).action is SelectionAction.HOLD
    assert row(result, 10).sector == "bank"
    assert row(result, 11).action is SelectionAction.HOLD
    assert row(result, 1).reasons == (SelectionReason.SECTOR_CAP,)
    assert [n.code for n in result.notes] == [
        SelectionWarning.SECTOR_OVER_CAP_FROM_HOLDINGS,
        SelectionWarning.CAPACITY_NOT_CHECKED,
    ]


# ---------------------------------------------------------------------------------------------
# CAPACITY and the proposed value / qty / participation arithmetic
# ---------------------------------------------------------------------------------------------


def test_proposed_value_qty_and_participation_arithmetic() -> None:
    constraints = wide(max_names=15, capital_inr=Decimal("1500000"))
    result = select_portfolio([cand(1, 1, adv="20000000", close="333")], [], constraints)
    entry = row(result, 1)
    assert entry.proposed_value_inr == Decimal("100000.00")  # capital / max_names
    assert entry.proposed_qty == 300  # floor(100000 / 333) = floor(300.3)
    assert entry.adv_participation_pct == Decimal("0.5000")  # 100000 / 2e7 x 100
    assert "₹100,000.00" in entry.explanation and "300 shares" in entry.explanation


def test_slot_value_rounds_down_to_the_paisa() -> None:
    result = select_portfolio(
        [cand(1, 1, adv="1e12", close="1")],
        [],
        wide(max_names=3, capital_inr=Decimal("1000000")),
    )
    assert row(result, 1).proposed_value_inr == Decimal("333333.33")


def test_capacity_refuses_above_the_cap_and_allows_exactly_at_it() -> None:
    constraints = wide(max_names=15, capital_inr=Decimal("1500000"))  # slot ₹100,000
    result = select_portfolio(
        [
            cand(1, 1, adv="10000000", close="100"),  # exactly 1.0000%
            cand(2, 2, adv="9999000", close="100"),  # 1.00010001% → 1.0001% > 1.0
        ],
        [],
        constraints,
    )
    assert row(result, 1).action is SelectionAction.ENTER
    assert row(result, 1).adv_participation_pct == Decimal("1.0000")
    refused = row(result, 2)
    assert refused.action is SelectionAction.SKIP
    assert refused.reasons == (SelectionReason.CAPACITY,)
    assert refused.adv_participation_pct == Decimal("1.0001")
    assert "1.0001%" in refused.explanation and "1%" in refused.explanation


def test_capacity_decides_on_the_rounded_participation_it_reports() -> None:
    # 100000 / 9999700 x 100 = 1.0000300009…%, reported as 1.0000 — so it must not be refused.
    result = select_portfolio(
        [cand(1, 1, adv="9999700", close="100")],
        [],
        wide(max_names=15, capital_inr=Decimal("1500000")),
    )
    assert row(result, 1).adv_participation_pct == Decimal("1.0000")
    assert row(result, 1).action is SelectionAction.ENTER


def test_missing_adv_is_flagged_not_refused_and_zero_adv_is_refused() -> None:
    result = select_portfolio(
        [cand(1, 1, adv=None, close="100"), cand(2, 2, adv="0", close="100")],
        [],
        wide(capital_inr=Decimal("1000000")),
    )
    unknown = row(result, 1)
    assert unknown.action is SelectionAction.ENTER
    assert SelectionFlag.CAPACITY_UNKNOWN in unknown.flags
    assert unknown.adv_participation_pct is None
    assert row(result, 2).reasons == (SelectionReason.CAPACITY,)


def test_no_capital_means_no_sizes_no_capacity_refusal_and_a_note() -> None:
    result = select_portfolio([cand(1, 1, adv="0", close="100")], [], wide())
    entry = row(result, 1)
    assert entry.action is SelectionAction.ENTER
    assert (entry.proposed_value_inr, entry.proposed_qty, entry.adv_participation_pct) == (
        None,
        None,
        None,
    )
    assert [n.code for n in result.notes] == [SelectionWarning.CAPACITY_NOT_CHECKED]


def test_no_participation_cap_sizes_but_never_refuses() -> None:
    result = select_portfolio(
        [cand(1, 1, adv="1000", close="100")],
        [],
        wide(capital_inr=Decimal("1000000"), max_adv_participation_pct=None),
    )
    assert row(result, 1).action is SelectionAction.ENTER
    assert row(result, 1).adv_participation_pct is not None
    assert [n.code for n in result.notes] == [SelectionWarning.CAPACITY_NOT_CHECKED]


def test_price_flags() -> None:
    result = select_portfolio(
        [cand(1, 1, adv="1e12", close=None), cand(2, 2, adv="1e12", close="150000")],
        [],
        wide(max_names=15, capital_inr=Decimal("1500000")),
    )
    assert SelectionFlag.PRICE_UNKNOWN in row(result, 1).flags
    assert row(result, 1).proposed_qty is None
    assert row(result, 2).proposed_qty == 0
    assert SelectionFlag.PRICE_ABOVE_SLOT_VALUE in row(result, 2).flags


def test_a_hold_above_capacity_is_kept_and_flagged() -> None:
    result = select_portfolio(
        [cand(1, 1, adv="1000", close="100")],
        [hold(1)],
        wide(capital_inr=Decimal("1000000")),
    )
    assert row(result, 1).action is SelectionAction.HOLD
    assert SelectionFlag.HOLD_ABOVE_CAPACITY in row(result, 1).flags


# ---------------------------------------------------------------------------------------------
# CORRELATION
# ---------------------------------------------------------------------------------------------


def _series(seed: int, n: int = 200) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 0.01, n)


def _frame(columns: dict[int, np.ndarray]) -> pd.DataFrame:
    n = len(next(iter(columns.values())))
    return pd.DataFrame(columns, index=pd.bdate_range("2026-01-01", periods=n))


def test_correlation_refuses_an_entrant_too_close_to_a_kept_holding() -> None:
    s1, s2 = _series(1), _series(2)
    returns = _frame({100: s1, 1: 0.95 * s1 + 0.05 * s2, 2: s2})
    result = select_portfolio(
        [cand(100, 3), cand(1, 1), cand(2, 2)], [hold(100)], wide(max_correlation=0.7), returns
    )
    refused = row(result, 1)
    assert refused.action is SelectionAction.SKIP
    assert refused.reasons == (SelectionReason.CORRELATION,)
    assert refused.correlation_peer == "S100"
    assert refused.max_correlation is not None and refused.max_correlation > 0.7
    assert "S100" in refused.explanation and "126 sessions" in refused.explanation
    assert row(result, 2).action is SelectionAction.ENTER


def test_correlation_counts_accepted_entrants_and_ignores_skipped_ones() -> None:
    s1, s2 = _series(1), _series(2)
    # A correlates ~0.8 with H (refused); B correlates ~0.3 with H but ~0.8 with A.
    a = 0.8 * s1 + 0.6 * s2
    b = 0.3 * s1 + 0.95 * s2
    returns = _frame({100: s1, 1: a, 2: b, 3: a + 0.01 * _series(3)})
    result = select_portfolio(
        [cand(100, 20), cand(1, 1), cand(2, 2), cand(3, 3)],
        [hold(100)],
        wide(max_correlation=0.7),
        returns,
    )
    assert row(result, 1).reasons == (SelectionReason.CORRELATION,)
    assert row(result, 2).action is SelectionAction.ENTER  # A was skipped, so not a peer
    third = row(result, 3)  # ≈ A: ~0.8 with the holding and ~0.8 with the accepted B
    assert third.action is SelectionAction.SKIP
    assert third.reasons == (SelectionReason.CORRELATION,)


def test_correlation_peer_can_be_an_accepted_entrant() -> None:
    s1 = _series(1)
    returns = _frame({1: s1, 2: s1 + 0.001 * _series(2)})
    result = select_portfolio([cand(1, 1), cand(2, 2)], [], wide(max_correlation=0.9), returns)
    assert row(result, 1).action is SelectionAction.ENTER
    assert row(result, 1).max_correlation is None  # no peers yet
    assert row(result, 2).correlation_peer == "S1"
    assert row(result, 2).reasons == (SelectionReason.CORRELATION,)


def test_exiting_holdings_are_not_correlation_peers() -> None:
    s1 = _series(1)
    returns = _frame({100: s1, 1: s1})
    result = select_portfolio(
        [cand(100, 99), cand(1, 1)], [hold(100)], wide(max_correlation=0.5), returns
    )
    assert row(result, 100).action is SelectionAction.EXIT
    assert row(result, 1).action is SelectionAction.ENTER


def test_negative_correlation_is_not_refused() -> None:
    s1 = _series(1)
    returns = _frame({100: s1, 1: -s1})
    result = select_portfolio(
        [cand(100, 2), cand(1, 1)], [hold(100)], wide(max_correlation=0.5), returns
    )
    assert row(result, 1).action is SelectionAction.ENTER
    assert row(result, 1).max_correlation == -1.0


def test_correlation_uses_only_the_last_window_rows_by_date() -> None:
    s1, s2 = _series(1, 600), _series(2, 600)
    follower = s1.copy()
    follower[-126:] = s2[-126:]  # identical history, then independent for the last 126 sessions
    returns = _frame({100: s1, 1: follower})
    shuffled = returns.sample(frac=1.0, random_state=7)  # order of rows must not matter
    for frame in (returns, shuffled):
        result = select_portfolio(
            [cand(100, 2), cand(1, 1)],
            [hold(100)],
            wide(max_correlation=0.5, correlation_window=126),
            frame,
        )
        assert row(result, 1).action is SelectionAction.ENTER
        assert abs(row(result, 1).max_correlation or 0.0) < 0.5
    longer = select_portfolio(
        [cand(100, 2), cand(1, 1)],
        [hold(100)],
        wide(max_correlation=0.5, correlation_window=600),
        returns,
    )
    assert longer.entries == ()


def test_correlation_needs_sixty_pairwise_complete_observations() -> None:
    s1 = _series(1, 126)
    enough = s1.copy()
    enough[: 126 - MIN_CORRELATION_OBSERVATIONS] = np.nan  # exactly 60 overlapping
    short = s1.copy()
    short[: 126 - MIN_CORRELATION_OBSERVATIONS + 1] = np.nan  # 59 overlapping
    holder = s1.copy()
    returns = _frame({100: holder, 1: enough, 2: short})
    result = select_portfolio(
        [cand(100, 3), cand(1, 1), cand(2, 2)], [hold(100)], wide(max_correlation=0.5), returns
    )
    assert row(result, 1).reasons == (SelectionReason.CORRELATION,)
    flagged = row(result, 2)
    assert flagged.action is SelectionAction.ENTER
    assert SelectionFlag.CORRELATION_HISTORY_SHORT in flagged.flags
    assert "60" in flagged.explanation


def test_pairwise_complete_drops_rows_where_either_side_is_missing() -> None:
    s1 = _series(1, 126)
    holder = s1.copy()
    holder[::2] = np.nan  # 63 left
    entrant = s1.copy()
    entrant[1::4] = np.nan  # removes a further ~32 of the holder's rows → < 60 overlap
    returns = _frame({100: holder, 1: entrant})
    result = select_portfolio(
        [cand(100, 3), cand(1, 1)], [hold(100)], wide(max_correlation=0.5), returns
    )
    assert SelectionFlag.CORRELATION_HISTORY_SHORT in row(result, 1).flags
    assert row(result, 1).action is SelectionAction.ENTER


def test_missing_returns_column_and_constant_series_are_flagged_not_refused() -> None:
    s1 = _series(1)
    returns = _frame({100: s1, 2: np.zeros(len(s1))})
    result = select_portfolio(
        [cand(100, 3), cand(1, 1), cand(2, 2)], [hold(100)], wide(max_correlation=0.1), returns
    )
    assert SelectionFlag.CORRELATION_HISTORY_SHORT in row(result, 1).flags
    assert row(result, 1).action is SelectionAction.ENTER
    assert SelectionFlag.CORRELATION_UNDEFINED in row(result, 2).flags
    assert row(result, 2).action is SelectionAction.ENTER


def test_max_correlation_without_returns_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match="returns"):
        select_portfolio([cand(1, 1)], [], wide(max_correlation=0.5))


# ---------------------------------------------------------------------------------------------
# TURNOVER_BUDGET
# ---------------------------------------------------------------------------------------------


def test_turnover_budget_exits_worst_ranked_first_and_retains_the_rest() -> None:
    result = select_portfolio(
        [cand(40, 40), cand(35, 35), cand(50, 50), cand(1, 1)],
        [hold(40), hold(35), hold(50), hold(999)],  # 999 is not in the results
        wide(retention_rank=30, turnover_budget_names=2),
    )
    assert set(ids(result.exits)) == {999, 50}
    for kept in (35, 40):
        assert row(result, kept).action is SelectionAction.HOLD
        assert row(result, kept).reasons == (
            SelectionReason.RETAINED_BY_TURNOVER_BUDGET,
            SelectionReason.RANK_OUTSIDE_RETENTION,
        )
        assert "turnover budget of 2" in row(result, kept).explanation
    assert row(result, 1).reasons == (SelectionReason.TURNOVER_BUDGET,)
    assert result.turnover_used == 2


def test_turnover_budget_tie_at_the_same_rank_exits_the_higher_id_first() -> None:
    result = select_portfolio(
        [cand(7, 40), cand(3, 40)],
        [hold(7), hold(3)],
        wide(retention_rank=30, turnover_budget_names=1),
    )
    assert ids(result.exits) == [7]
    assert row(result, 3).reasons[0] is SelectionReason.RETAINED_BY_TURNOVER_BUDGET


def test_retained_unranked_holding_says_why_it_would_have_exited() -> None:
    result = select_portfolio([], [hold(1), hold(2)], wide(turnover_budget_names=1))
    retained = result.holds[0]
    assert retained.reasons == (
        SelectionReason.RETAINED_BY_TURNOVER_BUDGET,
        SelectionReason.NOT_IN_RESULTS,
    )
    assert "not in this screen's results" in retained.explanation


def test_entries_fill_the_budget_left_after_exits_in_rank_order() -> None:
    result = select_portfolio(
        [cand(i, i) for i in range(1, 6)] + [cand(100, 99)],
        [hold(100), hold(200)],
        wide(turnover_budget_names=5),
    )
    assert result.exit_count == 2
    assert ids(result.entries) == [1, 2, 3]
    assert [row(result, i).reasons for i in (4, 5)] == [(SelectionReason.TURNOVER_BUDGET,)] * 2
    assert result.turnover_used == 5


def test_zero_turnover_budget_changes_nothing() -> None:
    result = select_portfolio(
        [cand(1, 1), cand(100, 99)], [hold(100), hold(200)], wide(turnover_budget_names=0)
    )
    assert result.exits == ()
    assert len(result.holds) == 2
    assert row(result, 1).reasons == (SelectionReason.TURNOVER_BUDGET,)


# ---------------------------------------------------------------------------------------------
# FULL, several refusals, notes
# ---------------------------------------------------------------------------------------------


def test_full_when_kept_plus_accepted_reach_max_names() -> None:
    result = select_portfolio(
        [cand(10, 4), cand(11, 5), cand(1, 1), cand(2, 2), cand(3, 3)],
        [hold(10), hold(11)],
        wide(max_names=3),
    )
    assert ids(result.entries) == [1]
    assert [row(result, i).reasons for i in (2, 3)] == [(SelectionReason.FULL,)] * 2
    assert "full at 3" in row(result, 2).explanation
    assert result.unfilled_slots == 0


def test_retained_holdings_count_towards_full_and_the_sector_cap() -> None:
    result = select_portfolio(
        [cand(50, 50, sector="bank"), cand(1, 1, sector="bank")],
        [hold(50, sector="bank"), hold(60)],
        wide(max_names=1, max_per_sector=1, turnover_budget_names=5, retention_rank=30),
    )
    # 60 is not in the results and exits; 50 (rank 50) exits too — budget 5 covers both.
    assert set(ids(result.exits)) == {50, 60}
    assert ids(result.entries) == [1]

    tight = select_portfolio(
        [cand(50, 50, sector="bank"), cand(1, 1, sector="bank")],
        [hold(50, sector="bank"), hold(60)],
        wide(max_names=1, max_per_sector=1, turnover_budget_names=1, retention_rank=30),
    )
    assert ids(tight.exits) == [60]
    assert row(tight, 1).reasons == (
        SelectionReason.SECTOR_CAP,
        SelectionReason.TURNOVER_BUDGET,
        SelectionReason.FULL,
    )


def test_every_refusal_is_reported_in_contract_order() -> None:
    s1 = _series(1)
    returns = _frame({100: s1, 1: s1})
    result = select_portfolio(
        [cand(100, 2, sector="it"), cand(1, 1, sector="it", adv="10", close="10")],
        [hold(100, sector="it")],
        SelectionConstraints(
            max_names=1,
            entry_rank=15,
            retention_rank=30,
            max_per_sector=1,
            capital_inr=Decimal("100000"),
            turnover_budget_names=0,
            max_correlation=0.5,
        ),
        returns,
    )
    refused = row(result, 1)
    assert refused.reasons == (
        SelectionReason.SECTOR_CAP,
        SelectionReason.CAPACITY,
        SelectionReason.CORRELATION,
        SelectionReason.TURNOVER_BUDGET,
        SelectionReason.FULL,
    )
    assert refused.explanation.startswith("Skip: ranked 1, inside the entry limit of 15, but ")
    assert refused.explanation.count("; and ") == 4


def test_kept_holdings_beyond_max_names_are_not_trimmed_and_noted() -> None:
    result = select_portfolio(
        [cand(i, i) for i in range(1, 6)], [hold(i) for i in range(2, 6)], wide(max_names=2)
    )
    assert len(result.holds) == 4
    assert row(result, 1).reasons == (SelectionReason.FULL,)
    assert SelectionWarning.HOLDINGS_EXCEED_MAX_NAMES in [n.code for n in result.notes]
    assert result.unfilled_slots == 0


def test_skips_do_not_dig_past_entry_rank_and_leave_slots_unfilled() -> None:
    result = select_portfolio(
        [cand(1, 1, sector="it"), cand(2, 2, sector="it"), cand(3, 16, sector="bank")],
        [],
        wide(max_names=3, entry_rank=2, retention_rank=30, max_per_sector=1),
    )
    assert ids(result.entries) == [1]
    assert result.unfilled_slots == 2
    assert result.skip_count == 1


# ---------------------------------------------------------------------------------------------
# Determinism, ordering, JSON, validation
# ---------------------------------------------------------------------------------------------


def test_rank_ties_break_by_instrument_id_whatever_the_input_order() -> None:
    cands = [cand(7, 1), cand(3, 1), cand(5, 2)]
    first = select_portfolio(cands, [], wide(max_names=1))
    assert ids(first.entries) == [3]
    rng = random.Random(1)
    for _ in range(10):
        shuffled = cands[:]
        rng.shuffle(shuffled)
        assert select_portfolio(shuffled, [], wide(max_names=1)).to_dict() == first.to_dict()


def test_rows_are_in_rank_order_with_unranked_exits_last() -> None:
    result = select_portfolio(
        [cand(1, 3), cand(2, 1), cand(3, 40)],
        [hold(1), hold(3), hold(8), hold(4)],
        wide(),
    )
    assert ids(result.rows) == [2, 1, 3, 4, 8]


def test_to_dict_is_json_with_exact_decimals_and_no_order_route() -> None:
    scored = [
        Candidate(1, "S1", 1, Decimal("87.5"), None, Decimal("20000000"), Decimal("333")),
        Candidate(2, "S2", 2, 1.25),
    ]
    result = select_portfolio(
        scored,
        [hold(1, qty=3)],
        wide(capital_inr=Decimal("1500000"), max_names=15),
    )
    payload = result.to_dict()
    text = json.dumps(payload)
    assert payload["informational_only"] is True
    rows = payload["rows"]
    assert isinstance(rows, list)
    first = rows[0]
    assert first["proposed_value_inr"] == "100000.00"
    assert first["current_quantity"] == "3"
    assert first["score"] == "87.5"
    assert rows[1]["score"] == 1.25
    assert first["action"] == "hold" and first["reasons"] == ["RANK_WITHIN_RETENTION"]
    for forbidden in ("plan_id", "order", "execute"):
        assert f'"{forbidden}' not in text


@pytest.mark.parametrize(
    "build",
    [
        lambda: SelectionConstraints(max_names=0),
        lambda: SelectionConstraints(entry_rank=0),
        lambda: SelectionConstraints(entry_rank=20, retention_rank=19),
        lambda: SelectionConstraints(max_per_sector=0),
        lambda: SelectionConstraints(capital_inr=Decimal(0)),
        lambda: SelectionConstraints(max_adv_participation_pct=Decimal(0)),
        lambda: SelectionConstraints(turnover_budget_names=-1),
        lambda: SelectionConstraints(max_correlation=1.5),
        lambda: SelectionConstraints(max_correlation=float("nan")),
        lambda: SelectionConstraints(correlation_window=MIN_CORRELATION_OBSERVATIONS - 1),
    ],
)
def test_constraints_refuse_meaningless_values(build: Callable[[], SelectionConstraints]) -> None:
    with pytest.raises(ValueError):
        build()


def test_constraint_defaults_are_the_contracts() -> None:
    c = SelectionConstraints()
    assert (c.max_names, c.entry_rank, c.retention_rank) == (15, 15, 30)
    assert (c.max_per_sector, c.capital_inr, c.turnover_budget_names, c.max_correlation) == (
        None,
        None,
        None,
        None,
    )
    assert c.max_adv_participation_pct == Decimal("1.0")
    assert c.correlation_window == 126


def test_inputs_refuse_nonsense() -> None:
    with pytest.raises(ValueError):
        cand(1, 0)
    with pytest.raises(ValueError):
        Candidate(1, "S1", 1, float("nan"))
    with pytest.raises(ValueError):
        cand(1, 1, close="0")
    with pytest.raises(ValueError):
        cand(1, 1, adv="-1")
    with pytest.raises(ValueError):
        hold(1, qty=0)
    with pytest.raises(ValueError, match="duplicate"):
        select_portfolio([cand(1, 1), cand(1, 2)], [], wide())
    with pytest.raises(ValueError, match="duplicate"):
        select_portfolio([], [hold(1), hold(1)], wide())


# ---------------------------------------------------------------------------------------------
# Properties over random books
# ---------------------------------------------------------------------------------------------

_SECTORS = st.sampled_from([None, "", "unclassified", "bank", "it", "pharma", "auto"])
_SCORES = st.one_of(
    st.none(),
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.decimals(min_value=-1000, max_value=1000, places=2, allow_nan=False, allow_infinity=False),
)


@st.composite
def _books(
    draw: st.DrawFn,
) -> tuple[list[Candidate], list[Holding], SelectionConstraints, pd.DataFrame | None]:
    instrument_ids = draw(st.lists(st.integers(1, 500), max_size=40, unique=True))
    candidates = [
        Candidate(
            instrument_id=iid,
            symbol=f"S{iid}",
            quality_rank=draw(st.integers(1, 60)),
            score=draw(_SCORES),
            sector=draw(_SECTORS),
            adv_value_inr=draw(
                st.one_of(
                    st.none(),
                    st.decimals(min_value=0, max_value=10**10, places=2, allow_nan=False),
                )
            ),
            close_raw=draw(
                st.one_of(
                    st.none(),
                    st.decimals(
                        min_value=Decimal("0.05"), max_value=200000, places=2, allow_nan=False
                    ),
                )
            ),
        )
        for iid in instrument_ids
    ]
    held_from_results = (
        draw(st.lists(st.sampled_from(instrument_ids), unique=True, max_size=20))
        if instrument_ids
        else []
    )
    held_elsewhere = draw(st.lists(st.integers(600, 700), unique=True, max_size=5))
    holdings = [
        Holding(iid, f"S{iid}", Decimal(draw(st.integers(1, 1000))), draw(_SECTORS))
        for iid in [*held_from_results, *held_elsewhere]
    ]
    entry = draw(st.integers(1, 30))
    max_correlation = draw(st.one_of(st.none(), st.floats(min_value=-1.0, max_value=1.0)))
    constraints = SelectionConstraints(
        max_names=draw(st.integers(1, 20)),
        entry_rank=entry,
        retention_rank=entry + draw(st.integers(0, 30)),
        max_per_sector=draw(st.one_of(st.none(), st.integers(1, 5))),
        capital_inr=draw(
            st.one_of(st.none(), st.decimals(min_value=100000, max_value=10**8, places=2))
        ),
        max_adv_participation_pct=draw(
            st.one_of(st.none(), st.decimals(min_value=Decimal("0.1"), max_value=5, places=2))
        ),
        turnover_budget_names=draw(st.one_of(st.none(), st.integers(0, 10))),
        max_correlation=max_correlation,
        correlation_window=draw(st.integers(MIN_CORRELATION_OBSERVATIONS, 150)),
    )
    returns: pd.DataFrame | None = None
    if max_correlation is not None or draw(st.booleans()):
        rng = np.random.default_rng(draw(st.integers(0, 2**32 - 1)))
        columns = sorted({*instrument_ids, *held_elsewhere})
        n_rows = draw(st.integers(0, 180))
        common = rng.normal(0, 0.01, (n_rows, 1))
        data = common * rng.uniform(-1, 1.5, (1, len(columns))) + rng.normal(
            0, 0.01, (n_rows, len(columns))
        )
        data[rng.random(data.shape) < 0.1] = np.nan
        returns = pd.DataFrame(
            data, index=pd.bdate_range("2026-01-01", periods=n_rows), columns=columns
        )
    return candidates, holdings, constraints, returns


def _score_bytes(score: float | Decimal | None) -> bytes:
    if score is None:
        return b"none"
    if isinstance(score, float):
        return b"f" + struct.pack(">d", score)
    return b"d" + str(score).encode()


@settings(max_examples=250, deadline=None)
@given(book=_books())
def test_scores_and_ranks_are_unchanged_by_selection_immutable(
    book: tuple[list[Candidate], list[Holding], SelectionConstraints, pd.DataFrame | None],
) -> None:
    candidates, holdings, constraints, returns = book
    before = pickle.dumps(candidates)
    ranks_before = [(c.instrument_id, c.quality_rank, _score_bytes(c.score)) for c in candidates]
    returns_before = returns.copy(deep=True) if returns is not None else None

    result = select_portfolio(candidates, holdings, constraints, returns)

    assert pickle.dumps(candidates) == before
    assert [
        (c.instrument_id, c.quality_rank, _score_bytes(c.score)) for c in candidates
    ] == ranks_before
    if returns is not None and returns_before is not None:
        pd.testing.assert_frame_equal(returns, returns_before, check_exact=True)
    by_id = {c.instrument_id: c for c in candidates}
    for out in result.rows:
        source = by_id.get(out.instrument_id)
        if source is None:
            assert out.quality_rank is None and out.score is None
        else:
            assert out.quality_rank == source.quality_rank
            assert _score_bytes(out.score) == _score_bytes(source.score)


@settings(max_examples=250, deadline=None)
@given(book=_books())
def test_caps_hold_for_random_books(
    book: tuple[list[Candidate], list[Holding], SelectionConstraints, pd.DataFrame | None],
) -> None:
    candidates, holdings, c, returns = book
    result = select_portfolio(candidates, holdings, c, returns)
    held_ids = {h.instrument_id for h in holdings}

    # Every holding is exactly one hold/exit row; every eligible non-held candidate one enter/skip.
    assert sorted(ids([*result.holds, *result.exits])) == sorted(held_ids)
    eligible = {
        x.instrument_id
        for x in candidates
        if x.instrument_id not in held_ids and x.quality_rank <= c.entry_rank
    }
    assert sorted(ids([*result.entries, *result.skips])) == sorted(eligible)

    assert result.entry_count <= max(0, c.max_names - result.kept_count)
    if c.turnover_budget_names is not None:
        assert result.turnover_used <= c.turnover_budget_names
    for skipped in result.skips:
        order = [
            SelectionReason.SECTOR_CAP,
            SelectionReason.CAPACITY,
            SelectionReason.CORRELATION,
            SelectionReason.TURNOVER_BUDGET,
            SelectionReason.FULL,
        ]
        assert skipped.reasons and list(skipped.reasons) == [
            r for r in order if r in skipped.reasons
        ]
    if c.max_per_sector is not None:
        counts = dict(result.sector_counts)
        for entered in result.entries:
            if entered.sector not in (None, "", "unclassified"):
                assert counts[entered.sector] <= c.max_per_sector
    for entered in result.entries:
        if (
            c.capital_inr is not None
            and c.max_adv_participation_pct is not None
            and entered.adv_participation_pct is not None
        ):
            assert entered.adv_participation_pct <= c.max_adv_participation_pct
        if c.max_correlation is not None and entered.max_correlation is not None:
            assert entered.max_correlation <= c.max_correlation
    json.dumps(result.to_dict())
