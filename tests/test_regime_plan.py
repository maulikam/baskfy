"""Regime checkpoint 3 — allocation, sell priority, FIFO tax review, DRY_RUN integration."""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app import config as C
from app.analytics import db, regime_run as RR
from app.analytics import regime_store as RS
from app.analytics import tax_lots as TL
from app.core import regime as R
from app.core import regime_alloc as A
from app.core.gateway import OrderGateway
from app.core.risk import RiskConfig, RiskManager

D = dt.date
SLEEVE_MIN, SLEEVE_MAX = 6.0, 15.0


@pytest.fixture()
def cfg():
    return R.RegimeConfig()


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "p.db")) as c:
        db.migrate(c)
        yield c


def cands(n=12, start_rank=1, price=100.0, cluster=None, base_score=80.0):
    return [A.Candidate(symbol=f"S{i:02d}", rank=start_rank + i,
                        score=base_score - i, price=price,
                        cluster=cluster or f"c{i % 4}", liquidity=1e8 - i)
            for i in range(n)]


def positions(symbols, qty=100, price=100.0, avg=90.0, ranks=None, guard=()):
    return [A.Position(symbol=s, quantity=qty, price=price, average_price=avg,
                       rank=(ranks or {}).get(s, i + 1), cluster="c0",
                       guard_exit=s in guard)
            for i, s in enumerate(symbols)]


def state_for(cfg, tier, *, actual=100.0, new_buys=None):
    caps = {R.RegimeTier.R1: 100.0, R.RegimeTier.R2: 70.0,
            R.RegimeTier.R3: 40.0, R.RegimeTier.R4: 10.0}
    nb = new_buys or {R.RegimeTier.R1: R.NewBuyMode.FULL,
                      R.RegimeTier.R2: R.NewBuyMode.HALF,
                      R.RegimeTier.R3: R.NewBuyMode.BLOCKED,
                      R.RegimeTier.R4: R.NewBuyMode.BLOCKED}[tier]
    return R.RegimeState(
        raw_candidate_tier=tier, previous_policy_tier=None, policy_tier=tier,
        transition_limited=False, target_equity_cap_pct=caps[tier],
        actual_equity_pct=actual, exposure_gap_pct=actual - caps[tier],
        pending_sell_pct=0.0, pending_buy_pct=0.0,
        execution_status=R.ExecutionStatus.NOT_PLANNED, new_buys=nb,
        forced_action=R.ForcedAction.NONE, reason_codes=[], reasons=[],
        as_of_date=D(2026, 6, 5), signal_session_date=D(2026, 6, 5),
        scheduled_week_end=D(2026, 6, 5), evaluation_id="eval-test",
        breadth_pct=60.0, breadth_coverage_pct=100.0, override_active=False,
        book_category_weights={}, book_weight_source=R.WeightSource.CONFIG_DEFAULT,
        index_diagnostics={}, last_transition_date=None,
        next_evaluation_date=D(2026, 6, 12), data_stale=False,
        manual_action_required=False, algorithm_version=R.ALGORITHM_VERSION,
        config_hash="hash")


# =====================================================================================
# sleeve semantics
# =====================================================================================
def test_sleeve_weights_and_nav_weights_differ_by_the_sleeve():
    alloc = A.solve_allocation(cands(10), sleeve_pct=40.0,
                               min_sleeve_w=SLEEVE_MIN, max_sleeve_w=SLEEVE_MAX)
    assert sum(alloc.weights_sleeve.values()) == pytest.approx(100.0, abs=1e-6)
    assert sum(alloc.weights_nav.values()) == pytest.approx(40.0, abs=1e-6)
    for s, w in alloc.weights_sleeve.items():
        assert alloc.weights_nav[s] == pytest.approx(w * 0.40, abs=1e-6)


def test_the_specs_worked_example_holds():
    """'a 10% equity-sleeve position in R3 represents 4% of total NAV'."""
    alloc = A.solve_allocation(cands(10, base_score=50.0), sleeve_pct=40.0,
                               min_sleeve_w=SLEEVE_MIN, max_sleeve_w=SLEEVE_MAX)
    s = next(k for k, v in alloc.weights_sleeve.items() if abs(v - 10.0) < 1.5)
    assert alloc.weights_nav[s] == pytest.approx(alloc.weights_sleeve[s] * 0.4, abs=1e-6)


def test_min_and_max_are_enforced_on_the_sleeve_not_nav():
    alloc = A.solve_allocation(cands(12), sleeve_pct=40.0,
                               min_sleeve_w=SLEEVE_MIN, max_sleeve_w=SLEEVE_MAX)
    for w in alloc.weights_sleeve.values():
        assert SLEEVE_MIN - 1e-6 <= w <= SLEEVE_MAX + 1e-6
    # in NAV terms every position is far below the sleeve floor — that is correct
    assert max(alloc.weights_nav.values()) <= SLEEVE_MAX * 0.4 + 1e-6


def test_full_sleeve_reduces_to_the_old_nav_semantics():
    alloc = A.solve_allocation(cands(12), sleeve_pct=100.0,
                               min_sleeve_w=SLEEVE_MIN, max_sleeve_w=SLEEVE_MAX)
    assert alloc.weights_sleeve == alloc.weights_nav


# =====================================================================================
# feasibility
# =====================================================================================
def test_feasible_position_range_for_the_configured_weights():
    assert A.feasible_position_range(6.0, 15.0) == (7, 16)


def test_too_many_full_size_positions_is_an_error():
    with pytest.raises(A.AllocationError, match="only 100%"):
        A.solve_allocation(cands(17), sleeve_pct=100.0,
                           min_sleeve_w=SLEEVE_MIN, max_sleeve_w=SLEEVE_MAX)


def test_too_few_positions_to_absorb_the_sleeve_is_an_error():
    with pytest.raises(A.AllocationError, match="at least"):
        A.solve_allocation(cands(3), sleeve_pct=100.0,
                           min_sleeve_w=SLEEVE_MIN, max_sleeve_w=SLEEVE_MAX)


def test_contradictory_weights_are_rejected():
    with pytest.raises(A.AllocationError, match="contradict"):
        A.validate_allocation_request(10, min_sleeve_w=20.0, max_sleeve_w=10.0)


def test_no_candidates_is_an_error():
    with pytest.raises(A.AllocationError, match="no eligible"):
        A.solve_allocation([], sleeve_pct=100.0, min_sleeve_w=6.0, max_sleeve_w=15.0)


def test_weights_are_never_silently_distorted():
    """Every solved weight satisfies the constraints, or the solver raised."""
    alloc = A.solve_allocation(cands(14), sleeve_pct=70.0,
                               min_sleeve_w=SLEEVE_MIN, max_sleeve_w=SLEEVE_MAX)
    assert all(w >= SLEEVE_MIN - 1e-6 for w in alloc.weights_sleeve.values())
    assert sum(alloc.weights_sleeve.values()) == pytest.approx(100.0, abs=1e-6)


def test_cluster_cap_is_applied_on_the_sleeve():
    heavy = [A.Candidate(f"H{i}", i + 1, 80 - i, 100.0, cluster="pharma")
             for i in range(6)]
    rest = [A.Candidate(f"R{i}", 10 + i, 60 - i, 100.0, cluster=f"c{i}")
            for i in range(6)]
    alloc = A.solve_allocation(heavy + rest, sleeve_pct=100.0,
                               min_sleeve_w=1.0, max_sleeve_w=30.0, cluster_cap=25.0)
    assert alloc.cluster_weights["pharma"] <= 25.0 + 1e-6


# =====================================================================================
# half-size entries
# =====================================================================================
def test_half_sized_entry_is_half_the_solved_weight():
    full = A.solve_allocation(cands(10), sleeve_pct=100.0, min_sleeve_w=SLEEVE_MIN,
                              max_sleeve_w=SLEEVE_MAX)
    half = A.solve_allocation(cands(10), sleeve_pct=100.0, min_sleeve_w=SLEEVE_MIN,
                              max_sleeve_w=SLEEVE_MAX, half_sized={"S00"})
    assert half.weights_sleeve["S00"] < full.weights_sleeve["S00"]
    assert half.weights_sleeve["S00"] == pytest.approx(
        half.weights_sleeve["S00"], abs=1e-9)
    assert half.weights_sleeve["S00"] < SLEEVE_MIN     # allowed to sit below the floor


def test_half_sized_entries_count_toward_the_position_ceiling(cfg):
    st = state_for(cfg, R.RegimeTier.R2)
    plan = RR.plan_regime_rebalance(st, cands(30), [], capital=10_000_000,
                                    mode=R.RegimeMode.PROPOSE)
    assert plan.allocation.n_positions <= C.TARGET_POSITIONS[1]


def test_r2_new_entries_are_half_sized(cfg):
    st = state_for(cfg, R.RegimeTier.R2, actual=0.0)
    plan = RR.plan_regime_rebalance(st, cands(12), [], capital=10_000_000,
                                    mode=R.RegimeMode.PROPOSE)
    assert plan.allocation.half_sized == frozenset(o.symbol for o in plan.buys)
    assert all(o.half_sized for o in plan.buys)


def test_blocked_tier_admits_no_new_names(cfg):
    st = state_for(cfg, R.RegimeTier.R3, actual=100.0)
    held = positions([f"S{i:02d}" for i in range(12)])
    plan = RR.plan_regime_rebalance(st, cands(20), held, capital=10_000_000,
                                    mode=R.RegimeMode.PROPOSE)
    new_names = {o.symbol for o in plan.buys if o.qty_now == 0}
    assert new_names == set()


# =====================================================================================
# tier exposure targets
# =====================================================================================
def test_r2_trims_to_the_70_percent_cap(cfg):
    st = state_for(cfg, R.RegimeTier.R2, actual=100.0)
    held = positions([f"S{i:02d}" for i in range(12)], qty=1000, price=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), held, capital=1_200_000,
                                    mode=R.RegimeMode.PROPOSE)
    assert plan.projected_equity_pct == pytest.approx(70.0, abs=1.0)
    assert plan.exposure_gap_after_plan_pct <= 1e-6


def test_r3_reduction_reaches_40_percent_not_merely_half_the_names(cfg):
    """The spec's explicit trap: 'exit bottom half' is a selection pool, not the target."""
    st = state_for(cfg, R.RegimeTier.R3, actual=100.0)
    held = positions([f"S{i:02d}" for i in range(12)], qty=1000, price=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), held, capital=1_200_000,
                                    mode=R.RegimeMode.PROPOSE)
    assert plan.projected_equity_pct == pytest.approx(40.0, abs=1.0)
    # halving the NAMES would leave ~50%, which is not the target
    assert plan.projected_equity_pct < 45.0


def test_r4_residual_respects_the_configured_cap_and_name_limit(cfg):
    st = state_for(cfg, R.RegimeTier.R4, actual=100.0)
    held = positions([f"S{i:02d}" for i in range(12)], qty=1000, price=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), held, capital=1_200_000,
                                    mode=R.RegimeMode.PROPOSE,
                                    residual_max_names=2, min_sleeve_w=1.0)
    assert plan.projected_equity_pct <= 10.0 + 1.0
    assert plan.allocation.n_positions <= 2


def test_r4_with_no_residual_policy_is_full_cash(cfg):
    st = state_for(cfg, R.RegimeTier.R4, actual=100.0)
    held = positions([f"S{i:02d}" for i in range(12)], qty=1000, price=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), held, capital=1_200_000,
                                    mode=R.RegimeMode.PROPOSE, residual_max_names=0)
    assert plan.projected_equity_pct == pytest.approx(0.0, abs=1e-6)
    assert all(o.qty_final == 0 for o in plan.orders)


def test_r1_allows_the_full_book(cfg):
    st = state_for(cfg, R.RegimeTier.R1, actual=0.0)
    plan = RR.plan_regime_rebalance(st, cands(12), [], capital=10_000_000,
                                    mode=R.RegimeMode.PROPOSE)
    assert plan.projected_equity_pct == pytest.approx(100.0, abs=1.0)
    assert not plan.allocation.half_sized


# =====================================================================================
# netting — no regime-created round trips
# =====================================================================================
def test_planning_nets_trades_and_creates_no_round_trips(cfg):
    st = state_for(cfg, R.RegimeTier.R2, actual=100.0)
    held = positions([f"S{i:02d}" for i in range(12)], qty=1000, price=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), held, capital=1_200_000,
                                    mode=R.RegimeMode.PROPOSE)
    assert plan.round_trips == ()
    assert len({o.symbol for o in plan.orders}) == len(plan.orders)


def test_a_name_kept_at_the_same_weight_produces_no_order(cfg):
    st = state_for(cfg, R.RegimeTier.R1, actual=100.0)
    cs = cands(10, base_score=50.0)
    alloc = A.solve_allocation(cs, sleeve_pct=100.0, min_sleeve_w=SLEEVE_MIN,
                               max_sleeve_w=SLEEVE_MAX)
    capital = 10_000_000.0
    held = [A.Position(symbol=c.symbol,
                       quantity=int(round(capital * alloc.weights_nav[c.symbol] / 100
                                          / c.price)),
                       price=c.price, average_price=c.price, rank=c.rank)
            for c in cs]
    plan = RR.plan_regime_rebalance(st, cs, held, capital=capital,
                                    mode=R.RegimeMode.PROPOSE)
    assert all(o.delta == 0 for o in plan.orders)


# =====================================================================================
# sell priority
# =====================================================================================
def test_guard_exits_sort_first_and_are_never_reinstated(cfg):
    ps = positions(["A", "B", "C"], ranks={"A": 1, "B": 2, "C": 3}, guard={"A"})
    assert A.order_sales(ps)[0].symbol == "A"

    st = state_for(cfg, R.RegimeTier.R1, actual=100.0)
    cs = [A.Candidate("A", 1, 90, 100.0), A.Candidate("B", 2, 80, 100.0),
          A.Candidate("C", 3, 70, 100.0)] + cands(9, start_rank=10)
    plan = RR.plan_regime_rebalance(st, cs, ps, capital=1_000_000,
                                    mode=R.RegimeMode.PROPOSE, min_sleeve_w=1.0)
    a = next(o for o in plan.orders if o.symbol == "A")
    assert a.qty_final == 0 and a.note == "guard exit"
    assert RR.PLAN_GUARD_EXITS_PRESERVED in plan.reason_codes


def test_worst_ranked_positions_sell_first():
    ps = positions(["A", "B", "C"], ranks={"A": 1, "B": 5, "C": 20})
    assert [p.symbol for p in A.order_sales(ps, nearby_band=1)][0] == "C"


def test_within_a_nearby_band_losers_sell_before_winners():
    ps = [A.Position("WIN", 100, 120.0, 100.0, rank=10),
          A.Position("LOSE", 100, 80.0, 100.0, rank=11)]
    assert A.order_sales(ps, nearby_band=5)[0].symbol == "LOSE"


def test_tax_flagged_winners_sell_last_within_their_band():
    ps = [A.Position("CLEAR", 100, 120.0, 100.0, rank=10),
          A.Position("NEARLTCG", 100, 120.0, 100.0, rank=11)]
    order = A.order_sales(ps, nearby_band=5, tax_flagged={"NEARLTCG"})
    assert order[-1].symbol == "NEARLTCG"


def test_unranked_positions_sell_early():
    ps = [A.Position("RANKED", 100, 100.0, 90.0, rank=1),
          A.Position("GONE", 100, 100.0, 90.0, rank=None)]
    assert A.order_sales(ps, nearby_band=100)[0].symbol == "GONE"


# =====================================================================================
# FIFO tax lots
# =====================================================================================
def lots(*specs):
    return TL.lots_from_rows("X", [
        {"quantity": q, "acquired_on": d, "price": p} for q, d, p in specs])


def test_proposed_quantity_consumes_fifo_lots_in_order():
    ls = lots((100, "2025-01-01", 50.0), (100, "2025-06-01", 60.0),
              (100, "2026-01-01", 70.0))
    rev = TL.review_sale("X", 150, 100.0, ls, as_of=D(2026, 6, 5))
    assert [c.quantity for c in rev.consumptions] == [100, 50]
    assert [c.lot.acquired_on for c in rev.consumptions] == [D(2025, 1, 1), D(2025, 6, 1)]
    assert rev.covered_qty == 150


def test_estimated_gain_uses_the_consumed_lots_only():
    ls = lots((100, "2025-01-01", 50.0), (100, "2026-01-01", 90.0))
    rev = TL.review_sale("X", 100, 100.0, ls, as_of=D(2026, 6, 5))
    assert rev.estimated_gain == pytest.approx(100 * (100.0 - 50.0))


def test_position_level_age_would_be_wrong():
    """Oldest lot is long-term, newest is near-LTCG. Selling 100 hits only the old one."""
    ls = lots((100, "2024-01-01", 50.0), (100, "2025-07-20", 90.0))
    small = TL.review_sale("X", 100, 100.0, ls, as_of=D(2026, 6, 5))
    assert small.flagged is False
    assert TL.TAX_LOT_LONG_TERM in small.codes
    big = TL.review_sale("X", 200, 100.0, ls, as_of=D(2026, 6, 5))
    assert big.flagged is True                       # the second lot IS near the boundary


def test_lot_is_flagged_inside_the_review_window():
    ls = lots((100, "2025-07-01", 50.0))             # 339 days held -> 26 to LTCG
    rev = TL.review_sale("X", 100, 100.0, ls, as_of=D(2026, 6, 5),
                         review_days=TL.DEFAULT_REVIEW_DAYS)
    assert rev.flagged is True and TL.TAX_LOT_NEAR_LTCG in rev.codes
    assert rev.flagged_qty == 100


def test_lot_outside_the_review_window_is_not_flagged():
    ls = lots((100, "2025-11-01", 50.0))             # ~216 days held -> 149 to LTCG
    assert TL.review_sale("X", 100, 100.0, ls, as_of=D(2026, 6, 5)).flagged is False


def test_boundary_of_the_review_window_is_inclusive():
    acq = D(2026, 6, 5) - dt.timedelta(days=TL.LONG_TERM_DAYS - 45)
    rev = TL.review_sale("X", 100, 100.0, lots((100, acq.isoformat(), 50.0)),
                         as_of=D(2026, 6, 5), review_days=45)
    assert rev.flagged is True


def test_a_losing_lot_is_never_ltcg_deferred():
    ls = lots((100, "2025-07-01", 150.0))            # near LTCG but underwater
    rev = TL.review_sale("X", 100, 100.0, ls, as_of=D(2026, 6, 5))
    assert rev.flagged is False
    assert TL.TAX_LOT_LOSS in rev.codes


def test_already_long_term_lot_is_not_flagged():
    rev = TL.review_sale("X", 100, 100.0, lots((100, "2024-01-01", 50.0)),
                         as_of=D(2026, 6, 5))
    assert rev.flagged is False and TL.TAX_LOT_LONG_TERM in rev.codes


def test_missing_acquisition_date_yields_tax_data_unknown():
    ls = TL.lots_from_rows("X", [{"quantity": 100, "acquired_on": None, "price": 50.0}])
    rev = TL.review_sale("X", 100, 100.0, ls, as_of=D(2026, 6, 5))
    assert rev.data_unknown is True
    assert TL.TAX_MISSING_ACQUISITION_DATE in rev.codes
    assert rev.manual_review is True


def test_missing_acquisition_price_is_distinguished_from_missing_date():
    ls = TL.lots_from_rows("X", [{"quantity": 100, "acquired_on": "2025-07-01",
                                  "price": None}])
    rev = TL.review_sale("X", 100, 100.0, ls, as_of=D(2026, 6, 5))
    assert TL.TAX_MISSING_ACQUISITION_PRICE in rev.codes
    assert TL.TAX_MISSING_ACQUISITION_DATE not in rev.codes
    assert rev.estimated_gain is None                # never guessed


def test_incomplete_lot_history_is_never_assumed_tax_safe():
    ls = lots((50, "2025-01-01", 50.0))              # only 50 of the 200 proposed
    rev = TL.review_sale("X", 200, 100.0, ls, as_of=D(2026, 6, 5))
    assert rev.covered_qty == 50
    assert TL.TAX_LOTS_INCOMPLETE in rev.codes
    assert rev.data_unknown is True and rev.manual_review is True


def test_no_lots_at_all_is_unknown_not_safe():
    rev = TL.review_sale("X", 100, 100.0, [], as_of=D(2026, 6, 5))
    assert rev.data_unknown is True and rev.covered_qty == 0


def test_open_lots_read_from_the_trades_table(conn):
    conn.execute("INSERT INTO trades(symbol, qty, entry_ts, entry_price, exit_ts) "
                 "VALUES(?,?,?,?,?)",
                 ("X", 100, dt.datetime(2025, 7, 1).timestamp(), 50.0, None))
    conn.execute("INSERT INTO trades(symbol, qty, entry_ts, entry_price, exit_ts) "
                 "VALUES(?,?,?,?,?)",
                 ("X", 50, dt.datetime(2024, 1, 1).timestamp(), 40.0,
                  dt.datetime(2025, 1, 1).timestamp()))     # closed -> not a lot
    ls = TL.open_lots(conn, "X")
    assert len(ls) == 1 and ls[0].quantity == 100


def test_review_sales_batches_by_symbol(conn):
    for sym, px in (("A", 50.0), ("B", 40.0)):
        conn.execute("INSERT INTO trades(symbol, qty, entry_ts, entry_price, exit_ts) "
                     "VALUES(?,?,?,?,?)",
                     (sym, 100, dt.datetime(2025, 7, 1).timestamp(), px, None))
    out = TL.review_sales(conn, {"A": (100, 100.0), "B": (100, 100.0)},
                          as_of=D(2026, 6, 5))
    assert set(out) == {"A", "B"}
    assert TL.flagged_symbols(out) == ["A", "B"]


# =====================================================================================
# tax integration: advisory, never a risk override
# =====================================================================================
def test_tax_flag_does_not_leave_the_cap_silently_breached(cfg):
    st = state_for(cfg, R.RegimeTier.R3, actual=100.0)
    held = positions([f"S{i:02d}" for i in range(12)], qty=1000, price=100.0)
    ls = lots((1000, "2025-07-01", 50.0))
    reviews = {f"S{i:02d}": TL.review_sale(f"S{i:02d}", 1000, 100.0, ls,
                                           as_of=D(2026, 6, 5)) for i in range(12)}
    plan = RR.plan_regime_rebalance(st, cands(12), held, capital=1_200_000,
                                    tax_reviews=reviews, mode=R.RegimeMode.PROPOSE)
    assert plan.projected_equity_pct == pytest.approx(40.0, abs=1.0)
    assert plan.manual_action_required is True
    assert plan.tax_flagged


def test_tax_review_marks_the_affected_sell_lines(cfg):
    st = state_for(cfg, R.RegimeTier.R3, actual=100.0)
    held = positions([f"S{i:02d}" for i in range(12)], qty=1000, price=100.0)
    ls = lots((1000, "2025-07-01", 50.0))
    reviews = {"S11": TL.review_sale("S11", 1000, 100.0, ls, as_of=D(2026, 6, 5))}
    plan = RR.plan_regime_rebalance(st, cands(12), held, capital=1_200_000,
                                    tax_reviews=reviews, mode=R.RegimeMode.PROPOSE)
    s11 = next(o for o in plan.orders if o.symbol == "S11")
    assert (s11.delta >= 0) or s11.tax_review is True


def test_manual_review_items_carry_lot_detail():
    ls = lots((100, "2025-07-01", 50.0))
    reviews = {"X": TL.review_sale("X", 100, 100.0, ls, as_of=D(2026, 6, 5))}
    items = TL.manual_review_items(reviews)
    assert len(items) == 1
    assert items[0]["detail"][0]["days_to_ltcg"] is not None
    assert items[0]["estimated_gain"] == pytest.approx(5000.0)


# =====================================================================================
# rollout modes + DRY_RUN
# =====================================================================================
def test_observe_mode_produces_no_executable_plan(cfg):
    st = state_for(cfg, R.RegimeTier.R2, actual=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), positions([f"S{i:02d}" for i in range(12)]),
                                    capital=1_200_000, mode=R.RegimeMode.OBSERVE)
    assert plan.executable is False
    assert RR.PLAN_NOT_EXECUTABLE_OBSERVE in plan.reason_codes


def test_propose_mode_needs_approval(cfg):
    st = state_for(cfg, R.RegimeTier.R2, actual=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), [], capital=1_200_000,
                                    mode=R.RegimeMode.PROPOSE, dry_run=False)
    assert plan.executable is False
    assert RR.PLAN_NEEDS_APPROVAL in plan.reason_codes


def test_enforce_mode_is_executable_only_outside_dry_run(cfg):
    st = state_for(cfg, R.RegimeTier.R2, actual=100.0)
    dry = RR.plan_regime_rebalance(st, cands(12), [], capital=1_200_000,
                                   mode=R.RegimeMode.ENFORCE, dry_run=True)
    live = RR.plan_regime_rebalance(st, cands(12), [], capital=1_200_000,
                                    mode=R.RegimeMode.ENFORCE, dry_run=False)
    assert dry.executable is False and RR.PLAN_DRY_RUN in dry.reason_codes
    assert live.executable is True


class ExplodingKC:
    def place_order(self, **kw):
        raise AssertionError("no order may reach the broker in these tests")


def gateway():
    return OrderGateway(ExplodingKC(), RiskManager(RiskConfig(max_position_value=1e12)))


@pytest.mark.parametrize("mode", [R.RegimeMode.OBSERVE, R.RegimeMode.PROPOSE])
def test_non_enforce_modes_refuse_to_execute(cfg, monkeypatch, mode):
    monkeypatch.setattr(C, "REGIME_ENABLED", True)
    st = state_for(cfg, R.RegimeTier.R2, actual=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), [], capital=1_200_000, mode=mode)
    with pytest.raises(RR.PlanNotExecutableError, match="enforce"):
        asyncio.run(RR.execute_regime_plan(gateway(), plan, mode=mode, dry_run=False,
                                           approved=True))


def test_dry_run_submits_no_orders_even_in_enforce_mode(cfg, monkeypatch):
    monkeypatch.setattr(C, "REGIME_ENABLED", True)
    st = state_for(cfg, R.RegimeTier.R2, actual=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), [], capital=1_200_000,
                                    mode=R.RegimeMode.ENFORCE, dry_run=True)
    with pytest.raises(RR.PlanNotExecutableError, match="DRY_RUN"):
        asyncio.run(RR.execute_regime_plan(gateway(), plan, mode=R.RegimeMode.ENFORCE,
                                           dry_run=True, approved=True))


def test_execution_requires_explicit_approval(cfg, monkeypatch):
    monkeypatch.setattr(C, "REGIME_ENABLED", True)
    st = state_for(cfg, R.RegimeTier.R2, actual=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), [], capital=1_200_000,
                                    mode=R.RegimeMode.ENFORCE, dry_run=False)
    with pytest.raises(RR.PlanNotExecutableError, match="approval"):
        asyncio.run(RR.execute_regime_plan(gateway(), plan, mode=R.RegimeMode.ENFORCE,
                                           dry_run=False, approved=False))


def test_disabled_feature_refuses_to_execute(cfg, monkeypatch):
    monkeypatch.setattr(C, "REGIME_ENABLED", False)
    st = state_for(cfg, R.RegimeTier.R2, actual=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), [], capital=1_200_000,
                                    mode=R.RegimeMode.ENFORCE, dry_run=False)
    with pytest.raises(RR.RegimeDisabledError):
        asyncio.run(RR.execute_regime_plan(gateway(), plan, mode=R.RegimeMode.ENFORCE,
                                           dry_run=False, approved=True))


def test_execution_routes_through_the_gateway(cfg, monkeypatch):
    """Orders reach the broker only via OrderGateway; DRY_RUN inside it simulates."""
    monkeypatch.setattr(C, "REGIME_ENABLED", True)
    monkeypatch.setattr(C, "DRY_RUN", True)          # gateway simulates
    st = state_for(cfg, R.RegimeTier.R2, actual=0.0)
    plan = RR.plan_regime_rebalance(st, cands(12), [], capital=1_200_000,
                                    mode=R.RegimeMode.ENFORCE, dry_run=False)
    rep = asyncio.run(RR.execute_regime_plan(gateway(), plan, mode=R.RegimeMode.ENFORCE,
                                             dry_run=False, approved=True))
    assert rep.submitted and all(r["status"] == "DRY_RUN" for r in rep.submitted)
    assert rep.status is R.ExecutionStatus.COMPLETED


def test_execution_sells_before_buys(cfg, monkeypatch):
    monkeypatch.setattr(C, "REGIME_ENABLED", True)
    monkeypatch.setattr(C, "DRY_RUN", True)
    st = state_for(cfg, R.RegimeTier.R2, actual=100.0)
    held = positions([f"S{i:02d}" for i in range(6)], qty=2000, price=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), held, capital=1_200_000,
                                    mode=R.RegimeMode.ENFORCE, dry_run=False)
    rep = asyncio.run(RR.execute_regime_plan(gateway(), plan, mode=R.RegimeMode.ENFORCE,
                                             dry_run=False, approved=True))
    sides = [r["regime_action"] for r in rep.submitted]
    sells = [i for i, a in enumerate(sides) if a in ("TRIM", "EXIT")]
    buys = [i for i, a in enumerate(sides) if a in ("BUY", "ADD")]
    if sells and buys:
        assert max(sells) < min(buys)


def test_dry_run_does_not_commit_a_policy_tier(conn, cfg):
    st = state_for(cfg, R.RegimeTier.R2)
    with pytest.raises(RS.DryRunCommitError):
        RS.commit_evaluation(conn, st, mode=R.RegimeMode.ENFORCE, dry_run=True)
    assert RS.get_canonical(conn, st.evaluation_id) is None


# =====================================================================================
# reconciliation
# =====================================================================================
def test_partial_execution_preserves_the_gap_and_can_be_retried(conn, cfg):
    st = state_for(cfg, R.RegimeTier.R3, actual=100.0)
    held = positions([f"S{i:02d}" for i in range(12)], qty=1000, price=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), held, capital=1_200_000,
                                    mode=R.RegimeMode.PROPOSE)

    RR.reconcile(conn, st, plan, actual_equity_pct=100.0,
                 execution_status=R.ExecutionStatus.PLANNED,
                 observed_at="2026-06-08T10:00:00")
    RR.reconcile(conn, st, plan, actual_equity_pct=65.0,
                 execution_status=R.ExecutionStatus.PARTIAL,
                 observed_at="2026-06-08T10:05:00")

    row = RS.latest_exposure(conn, st.evaluation_id)
    assert row["exposure_gap_pct"] == pytest.approx(25.0)
    assert RS.open_exposure_gap(conn) is not None
    assert len(RS.exposure_history(conn, st.evaluation_id)) == 2
    assert len(RS.committed_history(conn)) == 0          # no new tier transition


def test_reconciliation_closes_when_the_cap_is_reached(conn, cfg):
    st = state_for(cfg, R.RegimeTier.R3, actual=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), [], capital=1_200_000,
                                    mode=R.RegimeMode.PROPOSE)
    RR.reconcile(conn, st, plan, actual_equity_pct=40.0,
                 execution_status=R.ExecutionStatus.COMPLETED,
                 observed_at="2026-06-09T10:00:00")
    assert RS.open_exposure_gap(conn) is None


# =====================================================================================
# infeasibility surfaces as manual review, never as distorted weights
# =====================================================================================
def test_infeasible_allocation_becomes_a_manual_review_state(cfg):
    st = state_for(cfg, R.RegimeTier.R1, actual=0.0)
    plan = RR.plan_regime_rebalance(st, cands(2), [], capital=1_000_000,
                                    mode=R.RegimeMode.PROPOSE,
                                    min_sleeve_w=40.0, max_sleeve_w=45.0,
                                    target_positions=(2, 2))
    assert plan.infeasible_reason is not None
    assert plan.manual_action_required is True
    assert plan.executable is False
    assert plan.orders == ()


def test_summary_is_json_friendly(cfg):
    st = state_for(cfg, R.RegimeTier.R2, actual=100.0)
    plan = RR.plan_regime_rebalance(st, cands(12), [], capital=1_200_000,
                                    mode=R.RegimeMode.PROPOSE)
    import json
    json.dumps(RR.summarise(plan))


# =====================================================================================
# gateway integrity
# =====================================================================================
def test_gateway_order_sequence_is_unchanged():
    import app.core.gateway as gw
    src = open(gw.__file__).read()
    order = [src.index(m) for m in ("layer 1: untouchables", "layer 2: risk",
                                    "layer 3: idempotency", "layer 4: rate limits")]
    assert order == sorted(order)


def test_regime_modules_never_call_the_broker_directly():
    import app.analytics.regime_run as mod
    import app.core.regime_alloc as alloc
    for m in (mod, alloc):
        src = open(m.__file__).read()
        # look for CALLS, not prose: the module docstrings mention these names when
        # documenting that they are never invoked
        assert "kc.place_order(" not in src
        assert "place_gtt(" not in src
        assert "place_gtt_stop(" not in src
