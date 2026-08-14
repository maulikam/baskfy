"""Regime checkpoint 4 — read-only view model and backtest mechanics."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from app.analytics import db, regime_store as RS, regime_view as RV
from app.core import regime as R
from scripts import backtest_regime as BT

D = dt.date


@pytest.fixture()
def cfg():
    return R.RegimeConfig()


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "p.db")) as c:
        db.migrate(c)
        yield c


def make_state(cfg, tier=R.RegimeTier.R2, week=D(2026, 6, 5)):
    sig = {m: R.MaSignal(m, 100.0, 95.0, 5.26, R.SignalState.ABOVE, 3)
           for m in cfg.ma_lengths}
    idx = {n: R.IndexSignals(n, week, 100.0, sig) for n in cfg.structural_indices.values()}
    sent = R.IndexSignals(cfg.momentum_sentinel, week, 100.0, sig)
    brd = R.BreadthReading(week, 70.0, 500, 500, 100.0, "scan", "h", "run", "v1")
    return R.evaluate(as_of_date=week, scheduled_week_end=week, signal_session_date=week,
                      index_signals=idx, sentinel=sent, breadth=brd,
                      book=R.resolve_book_weights(cfg),
                      exposure=R.ExposureSnapshot(actual_equity_pct=85.0), cfg=cfg,
                      previous=R.PreviousRegime(tier, D(2026, 5, 29)))


# =====================================================================================
# view model
# =====================================================================================
def test_empty_database_reports_unavailable(conn):
    v = RV.build(conn)
    assert v["available"] is False and "message" in v


def test_view_builds_from_a_stored_preview(conn, cfg):
    st = make_state(cfg)
    RS.save_preview(conn, st, mode=R.RegimeMode.OBSERVE)
    v = RV.build(conn)
    assert v["available"] is True
    assert v["committed"] is False
    assert v["policy_tier"] == st.policy_tier.value
    assert v["algorithm_version"] == R.ALGORITHM_VERSION
    assert v["config_hash"] == cfg.config_hash()


def test_committed_row_is_preferred_over_previews(conn, cfg):
    st = make_state(cfg)
    RS.save_preview(conn, st, mode=R.RegimeMode.OBSERVE)
    RS.commit_evaluation(conn, st, mode=R.RegimeMode.ENFORCE, dry_run=False)
    v = RV.build(conn)
    assert v["committed"] is True and v["run_id"] == "canonical"


def test_cap_is_derived_from_the_tier_not_a_stale_exposure_row(conn, cfg):
    """A reconciliation row can outlive the decision it was written for."""
    # From R4 the re-risk cap allows only R3 (cap 40%), so the stale 100% row disagrees.
    st = make_state(cfg, tier=R.RegimeTier.R4)
    assert st.policy_tier is R.RegimeTier.R3
    RS.save_preview(conn, st, mode=R.RegimeMode.OBSERVE)
    RS.record_exposure(conn, evaluation_id=st.evaluation_id, actual_equity_pct=85.0,
                       target_equity_cap_pct=100.0,          # stale: written under R1
                       observed_at="2026-06-08T10:00:00")
    v = RV.build(conn)
    assert v["target_equity_cap_pct"] == cfg.cap_for(R.RegimeTier(v["policy_tier"]))
    assert v["exposure_cap_mismatch"] is True
    assert v["exposure_gap_pct"] == pytest.approx(85.0 - v["target_equity_cap_pct"])


def test_view_exposes_every_required_field(conn, cfg):
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    v = RV.build(conn)
    required = [
        "raw_candidate_tier", "policy_tier", "previous_policy_tier", "transition_limited",
        "target_equity_cap_pct", "actual_equity_pct", "exposure_gap_pct",
        "execution_status", "pending_buy_pct", "pending_sell_pct", "new_buys",
        "sentinel_veto", "reasons", "reason_codes", "breadth_pct",
        "breadth_coverage_pct", "book_weights", "structural_cards", "sentinel_card",
        "override_active", "data_as_of", "data_stale", "last_transition_date",
        "next_evaluation_date", "forced_action", "manual_review_count",
        "algorithm_version", "config_hash", "index_status", "health",
    ]
    missing = [k for k in required if k not in v]
    assert missing == [], f"view is missing required fields: {missing}"


def test_reason_pairs_map_codes_to_display_text(conn, cfg):
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    v = RV.build(conn)
    assert v["reason_pairs"]
    for pair in v["reason_pairs"]:
        assert pair["code"] and pair["text"] and pair["text"] != ""


def test_index_table_carries_all_three_moving_averages(conn, cfg):
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    v = RV.build(conn)
    for row in v["structural_cards"]:
        assert [m["length"] for m in row["mas"]] == [20, 50, 200]
        assert all("distance_pct" in m and "confirming_closes" in m
                   and "confirmation" in m for m in row["mas"])


def test_view_is_read_only(conn, cfg):
    """Building the view must not create, modify or delete a single row."""
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    tables = ["regime_evaluations", "regime_exposure", "breadth_readings",
              "regime_book_snapshots", "index_series", "snapshots"]
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    for _ in range(3):
        RV.build(conn)
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    assert before == after


def test_view_is_json_serialisable(conn, cfg):
    import json
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    json.dumps(RV.build(conn))


# =====================================================================================
# cost model — traded notional, never the whole book
# =====================================================================================
def test_cost_is_charged_on_traded_notional_only():
    costs = BT.CostModel()
    book, delta = 10_000_000.0, 0.30           # R1 -> R2
    notional = book * delta
    one_way = costs.one_way(notional, is_buy=False)
    assert one_way == pytest.approx(costs.one_way(3_000_000, is_buy=False))
    # charging the full corpus would be >3x
    assert one_way < costs.one_way(book, is_buy=False) / 3


def test_cost_components_are_reported_separately():
    b = BT.CostModel().breakdown(3_000_000, is_buy=True)
    assert set(b) == {"stt", "brokerage", "exchange", "sebi", "gst", "stamp", "dp",
                      "slippage"}
    assert b["stt"] == pytest.approx(3_000.0)      # 0.1% per side
    assert b["stamp"] > 0                          # buy side only


def test_stamp_duty_is_buy_side_only():
    c = BT.CostModel()
    assert c.breakdown(1_000_000, is_buy=False)["stamp"] == 0.0
    assert c.breakdown(1_000_000, is_buy=True)["stamp"] > 0.0


def test_round_trip_is_both_legs():
    c = BT.CostModel()
    assert c.round_trip(1_000_000) == pytest.approx(
        c.one_way(1_000_000, is_buy=True) + c.one_way(1_000_000, is_buy=False))


def test_slippage_scenarios_are_ordered():
    s = BT.SLIPPAGE_SCENARIOS
    assert s["low"] < s["base"] < s["stressed_smallcap"]


# =====================================================================================
# no lookahead
# =====================================================================================
def ohlc(n=10, start="2026-01-01"):
    idx = pd.date_range(start, periods=n, freq="B")
    return pd.DataFrame({"open": [100.0] * n, "high": [101.0] * n,
                         "low": [99.0] * n, "close": [100.0] * n}, index=idx)


def test_exposure_change_lands_on_the_next_session_not_the_signal_close():
    px = ohlc(6)
    sched = pd.DataFrame({"exposure": [0.0]}, index=[px.index[2]])
    sim = BT.simulate(sched, px, costs=BT.CostModel())
    exp = sim["exposure"]
    assert exp.iloc[2] == 100.0        # still invested ON the signal session
    assert exp.iloc[3] == 0.0          # change effective the NEXT session


def test_a_signal_on_the_final_session_cannot_be_executed():
    px = ohlc(5)
    sched = pd.DataFrame({"exposure": [0.0]}, index=[px.index[-1]])
    sim = BT.simulate(sched, px, costs=BT.CostModel())
    assert sim["transitions"] == 0     # no next session existed


def test_turnover_is_measured_in_nav_fractions_not_rupees():
    """A compounding curve must not make late trades dominate turnover."""
    idx = pd.date_range("2020-01-01", periods=600, freq="B")
    px = pd.DataFrame({"open": [100.0] * 600, "high": [101.0] * 600,
                       "low": [99.0] * 600,
                       "close": list(pd.Series(range(600)).mul(0.5).add(100.0))}, index=idx)
    sched = pd.DataFrame({"exposure": [0.0, 100.0]}, index=[idx[10], idx[400]])
    sim = BT.simulate(sched, px, costs=BT.CostModel())
    assert sim["traded_frac"] == pytest.approx(2.0)     # two full 100-point moves
    assert sim["cost_frac"] < 0.02                      # a couple of round-trip costs


# =====================================================================================
# whipsaw definition
# =====================================================================================
def sched_from(exposures, start="2026-01-02"):
    idx = pd.date_range(start, periods=len(exposures), freq="W-FRI")
    return pd.DataFrame({"exposure": exposures}, index=idx)


def flat_benchmark(n, start="2026-01-02"):
    idx = pd.date_range(start, periods=n, freq="W-FRI")
    return pd.Series([100.0] * n, index=idx)


def test_a_quick_reversal_without_a_benchmark_fall_is_a_whipsaw():
    sched = sched_from([100, 100, 40, 40, 100, 100])
    res = BT.count_whipsaws(sched, flat_benchmark(6), BT.WhipsawSpec())
    assert res["count"] == 1


def test_a_reduction_the_benchmark_justified_is_not_a_whipsaw():
    sched = sched_from([100, 100, 40, 40, 100, 100])
    bench = pd.Series([100.0, 100.0, 100.0, 80.0, 90.0, 100.0],
                      index=sched.index)          # -20% drawdown inside the window
    assert BT.count_whipsaws(sched, bench, BT.WhipsawSpec())["count"] == 0


def test_a_reduction_that_is_not_restored_is_not_a_whipsaw():
    sched = sched_from([100, 100, 40, 40, 40, 40])
    assert BT.count_whipsaws(sched, flat_benchmark(6), BT.WhipsawSpec())["count"] == 0


def test_a_small_reduction_is_not_a_whipsaw():
    sched = sched_from([100, 100, 90, 90, 100, 100])   # only 10 points
    assert BT.count_whipsaws(sched, flat_benchmark(6), BT.WhipsawSpec())["count"] == 0


def test_restoration_outside_the_window_is_not_a_whipsaw():
    sched = sched_from([100, 100, 40, 40, 40, 40, 40, 100])
    assert BT.count_whipsaws(sched, flat_benchmark(8),
                             BT.WhipsawSpec(window_evals=2))["count"] == 0


def test_whipsaw_thresholds_are_configurable():
    sched = sched_from([100, 100, 90, 90, 100, 100])
    spec = BT.WhipsawSpec(drop_points=5.0)
    assert BT.count_whipsaws(sched, flat_benchmark(6), spec)["count"] == 1


def test_exposure_reversals_counts_direction_changes():
    assert BT.exposure_reversals(sched_from([100, 40, 100, 40])) == 2
    assert BT.exposure_reversals(sched_from([100, 70, 40, 10])) == 0


# =====================================================================================
# variants + coverage gating
# =====================================================================================
def test_variant_d_is_reported_unavailable_not_approximated():
    d = next(v for v in BT.VARIANTS if v.key == "D")
    assert d.available is False
    assert "stock-level" in d.unavailable_reason


def test_binary_variants_come_in_raw_and_buffered_pairs():
    keys = {v.key for v in BT.VARIANTS}
    assert {"B_raw", "B_buf", "C_raw", "C_buf"} <= keys


def test_a_single_stale_breadth_reading_is_not_coverage(conn):
    from app.analytics import breadth as B
    B.save_reading(conn, R.BreadthReading(D(2026, 8, 14), 70.0, 500, 500, 100.0,
                                          "scan", "h", "r", "v"))
    assert BT.breadth_covers_window(conn, D(2005, 4, 1), D(2026, 8, 14)) is False


def test_dense_breadth_history_counts_as_coverage(conn):
    from app.analytics import breadth as B
    for i in range(60):
        day = D(2026, 1, 2) + dt.timedelta(days=7 * i)
        B.save_reading(conn, R.BreadthReading(day, 60.0, 500, 500, 100.0,
                                              "scan", "h", f"r{i}", "v"))
    assert BT.breadth_covers_window(conn, D(2026, 1, 2), D(2027, 2, 26)) is True


def test_breadth_absent_from_the_model_imposes_no_constraint_on_r1(cfg):
    """Not using breadth is a different model from breadth being broken."""
    no_breadth = R.replace(cfg, breadth_required=False)
    tier, _ = R.classify_candidate(health={20: 1.0, 50: 1.0, 200: 1.0}, stack_weight=0.0,
                                   breadth_pct=None, breadth_usable=False,
                                   sentinel=R.IndexSignals(
                                       "M50", D(2026, 6, 5), 100.0,
                                       {m: R.MaSignal(m, 100.0, 95.0, 5.0,
                                                      R.SignalState.ABOVE, 3)
                                        for m in cfg.ma_lengths}),
                                   cfg=no_breadth)
    assert tier is R.RegimeTier.R1


def test_required_but_unusable_breadth_still_blocks_r1(cfg):
    tier, _ = R.classify_candidate(health={20: 1.0, 50: 1.0, 200: 1.0}, stack_weight=0.0,
                                   breadth_pct=90.0, breadth_usable=False,
                                   sentinel=None, cfg=cfg)
    assert tier is not R.RegimeTier.R1


# =====================================================================================
# replay fidelity: the fast tape must equal the pure engine
# =====================================================================================
def test_signal_tape_matches_building_from_scratch(cfg):
    idx = pd.date_range("2024-01-01", periods=400, freq="B")
    closes = [100.0 + (i % 37) * 0.9 + i * 0.15 for i in range(400)]
    df = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes},
                      index=idx)
    tape = BT.SignalTape("X", df, cfg)
    for session in (idx[250], idx[320], idx[-1]):
        hist = df.loc[:session]
        scratch = R.build_index_signals(
            "X", [R.Candle(d.date(), r.open, r.high, r.low, r.close)
                  for d, r in hist.iterrows()], cfg, as_of=session.date())
        fast = tape.at(session)
        for m in cfg.ma_lengths:
            assert fast.state(m) is scratch.state(m)
            assert fast.signals[m].confirming_closes == scratch.signals[m].confirming_closes
            if scratch.signals[m].ma is not None:
                assert fast.signals[m].ma == pytest.approx(scratch.signals[m].ma)


def test_weekly_sessions_take_the_last_session_of_each_week():
    idx = list(pd.date_range("2026-06-01", periods=10, freq="B"))
    weeks = BT.weekly_signal_sessions(idx, 4)
    assert weeks[0] == pd.Timestamp("2026-06-05")     # Friday
    assert all(w in idx for w in weeks)


# =====================================================================================
# planner output reaches the page
# =====================================================================================
def test_snapshot_carries_proposed_orders_and_review_items(cfg):
    from app.analytics import regime_run as RR, tax_lots as TL
    from app.core import regime_alloc as A

    st = make_state(cfg)
    cands = [A.Candidate(f"S{i:02d}", i + 1, 85 - i, 100.0) for i in range(12)]
    held = [A.Position(f"S{i:02d}", 1000, 100.0, 90.0, rank=i + 1) for i in range(12)]
    acq = (D(2026, 6, 5) - dt.timedelta(days=329)).isoformat()
    rev = {"S11": TL.review_sale("S11", 1000, 100.0,
                                 TL.lots_from_rows("S11", [{"quantity": 1000,
                                                            "acquired_on": acq,
                                                            "price": 50.0}]),
                                 as_of=D(2026, 6, 5))}
    plan = RR.plan_regime_rebalance(st, cands, held, capital=1_200_000,
                                    tax_reviews=rev, mode=R.RegimeMode.OBSERVE)
    snap = RR.evaluation_snapshot(plan, rev)
    assert snap["proposed_orders"], "the page cannot show actions that were never stored"
    assert len(snap["manual_review_items"]) == 1
    assert snap["plan"]["tier"] == st.policy_tier.value


def test_view_renders_stored_proposed_orders(conn, cfg):
    from app.analytics import regime_run as RR
    from app.core import regime_alloc as A

    st = make_state(cfg)
    cands = [A.Candidate(f"S{i:02d}", i + 1, 85 - i, 100.0) for i in range(12)]
    held = [A.Position(f"S{i:02d}", 1000, 100.0, 90.0, rank=i + 1) for i in range(12)]
    plan = RR.plan_regime_rebalance(st, cands, held, capital=1_200_000,
                                    mode=R.RegimeMode.OBSERVE)
    RS.save_preview(conn, st, mode=R.RegimeMode.OBSERVE,
                    input_snapshot=RR.evaluation_snapshot(plan))
    v = RV.build(conn)
    assert len(v["proposed_orders"]) == len([o for o in plan.orders if o.delta != 0])
    assert len(v["proposed_sells"]) + len(v["proposed_buys"]) == len(v["proposed_orders"])
    assert v["plan_summary"]["sleeve_pct"] == plan.sleeve_pct


def test_pending_percentages_are_measured_against_rupee_capital(conn, cfg):
    """Pending legs are rupee values; dividing by the exposure PERCENTAGE reported 4235%."""
    from app.analytics import regime_run as RR
    from app.core import regime_alloc as A

    st = make_state(cfg)
    cands = [A.Candidate(f"S{i:02d}", i + 1, 85 - i, 100.0) for i in range(12)]
    held = [A.Position(f"S{i:02d}", 1000, 100.0, 90.0, rank=i + 1) for i in range(12)]
    capital = 1_200_000.0
    plan = RR.plan_regime_rebalance(st, cands, held, capital=capital,
                                    mode=R.RegimeMode.OBSERVE)
    RR.reconcile(conn, st, plan, actual_equity_pct=85.0, capital=capital,
                 observed_at="2026-06-08T10:00:00")
    row = RS.latest_exposure(conn, st.evaluation_id)
    expected = sum(abs(o.delta) * o.ref_price for o in plan.sells) / capital * 100.0
    assert row["pending_sell_pct"] == pytest.approx(expected, abs=1e-6)
    assert 0.0 <= row["pending_sell_pct"] <= 100.0


def test_missing_capital_does_not_produce_a_nonsense_percentage(conn, cfg):
    from app.analytics import regime_run as RR
    from app.core import regime_alloc as A

    st = make_state(cfg)
    cands = [A.Candidate(f"S{i:02d}", i + 1, 85 - i, 100.0) for i in range(12)]
    held = [A.Position(f"S{i:02d}", 1000, 100.0, 90.0, rank=i + 1) for i in range(12)]
    plan = RR.plan_regime_rebalance(st, cands, held, capital=1_200_000,
                                    mode=R.RegimeMode.OBSERVE)
    RR.reconcile(conn, st, plan, actual_equity_pct=85.0, observed_at="2026-06-08T11:00:00")
    row = RS.latest_exposure(conn, st.evaluation_id)
    assert row["pending_sell_pct"] == 0.0        # reported as unknown, never invented


# =====================================================================================
# navigation — every page must be reachable from the UI
# =====================================================================================
def test_every_html_page_is_reachable_from_every_other():
    """A page with no inbound link is unreachable unless you type the URL."""
    import re
    pages = {"app/templates/index.html": "/", "app/templates/regime.html": "/regime"}
    for path in pages:
        html = open(path).read()
        links = set(re.findall(r'href="([^"]+)"', html))
        for target in pages.values():
            assert target in links, f"{path} has no link to {target}"


def test_nav_marks_the_current_page():
    for path, current in (("app/templates/index.html", '<a href="/" aria-current="page">'),
                          ("app/templates/regime.html",
                           '<a href="/regime" aria-current="page">')):
        assert current in open(path).read(), f"{path} does not mark its own nav item"


# =====================================================================================
# the five questions the page must answer immediately
# =====================================================================================
def test_view_answers_all_five_questions(conn, cfg):
    from app.analytics import regime_run as RR
    from app.core import regime_alloc as A

    st = make_state(cfg, tier=R.RegimeTier.R4)          # -> R3, cap 40, actual 85
    cands = [A.Candidate(f"S{i:02d}", i + 1, 85 - i, 100.0,
                         cluster=["pharma", "ems", "cables", "auto"][i % 4])
             for i in range(12)]
    held = [A.Position(f"S{i:02d}", 1000, 100.0, 90.0, rank=i + 1) for i in range(12)]
    plan = RR.plan_regime_rebalance(st, cands, held, capital=1_200_000,
                                    mode=R.RegimeMode.OBSERVE)
    RS.save_preview(conn, st, mode=R.RegimeMode.OBSERVE,
                    input_snapshot=RR.evaluation_snapshot(plan))
    RR.reconcile(conn, st, plan, actual_equity_pct=85.0, capital=1_200_000,
                 observed_at="2026-06-08T10:00:00")
    v = RV.build(conn)

    # 1 what regime
    assert v["policy_tier"] and v["tier_name"] and v["raw_candidate_tier"]
    # 2 why
    assert v["narrative"], "no plain-language explanation"
    assert v["gates"] and all({"current", "threshold", "status"} <= set(g) for g in v["gates"])
    # 3 what exposure is permitted
    assert v["target_equity_cap_pct"] is not None and v["exposure_gap_pct"] is not None
    assert v["bar"]["cap_marker"] == v["target_equity_cap_pct"]
    # 4 what is pending
    assert v["proposed_orders"]
    # 5 is the data reliable
    assert v["data_health"] in ("Current", "Stale", "Incomplete")
    assert isinstance(v["banners"], list)


def test_tier_has_a_human_name_and_meaning(cfg, conn):
    for tier, name in (("R1", "Risk-On"), ("R2", "Caution"),
                       ("R3", "Risk-Off"), ("R4", "Crash")):
        assert RV.TIER_META[tier][0] == name
        assert RV.TIER_META[tier][2], f"{tier} has no plain-language meaning"


def test_every_gate_shows_value_threshold_and_status(conn, cfg):
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    v = RV.build(conn)
    allowed = {"Triggered", "Clear", "Met", "Not met", "Not evaluable", "Not required"}
    for g in v["gates"]:
        assert g["status"] in allowed, g
        assert g["threshold"], g
        assert g["code"] and g["label"]


def test_banner_fires_for_each_unsafe_condition(conn, cfg):
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    titles = [b["title"] for b in RV.build(conn)["banners"]]
    assert "DRY_RUN is on" in titles
    assert "Observe mode" in titles
    for b in RV.build(conn)["banners"]:
        assert b["detail"], "a banner must state its consequence, not just its condition"


def test_safe_state_is_absent_when_nothing_is_degraded(conn, cfg):
    """The sentence appears only when a protective behaviour is actually in force.
    Printing one unconditionally would train the reader to ignore it."""
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    assert RV.build(conn)["safe_state"] is None


def test_safe_state_explains_the_behaviour_when_breadth_is_unusable(conn, cfg):
    st = make_state(cfg)
    RS.save_preview(conn, R.replace(st, breadth_coverage_pct=10.0),
                    mode=R.RegimeMode.OBSERVE)
    v = RV.build(conn)
    assert v["safe_state"], "degraded inputs must be explained"
    assert "Risk-On" in v["safe_state"]
    assert v["data_health"] == "Incomplete"


def test_confirmation_counters_are_formatted_as_n_of_required(conn, cfg):
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    v = RV.build(conn)
    for card in v["index_cards"]:
        for m in card["mas"]:
            assert "/" in m["confirmation"], m


def test_config_panel_is_read_only_and_complete(conn, cfg):
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    c = RV.build(conn)["config"]
    for key in ("ma_lengths", "buffer_bps", "confirm_days", "breadth_thresholds",
                "health_thresholds", "exposure_map", "default_weights",
                "ltcg_review_days", "weekly_evaluation_weekday", "r4_residual",
                "whipsaw", "algorithm_version", "config_hash"):
        assert key in c, f"config panel missing {key}"
    html = open("app/templates/regime.html").read()
    assert "<form" not in html and "<input" not in html, "the status page must be read-only"


def test_backtest_page_is_separate_from_the_status_page():
    """Detailed backtesting stays off the live status page."""
    status = open("app/templates/regime.html").read()
    assert "/regime/backtest" in status          # linked
    assert "Strategy comparison" not in status   # but not embedded
    bt = open("app/templates/regime_backtest.html").read()
    assert "Strategy comparison" in bt and "not available" in bt.lower()


# =====================================================================================
# comprehension: the ladder, the switches, and the real book
# =====================================================================================
def test_tier_ladder_describes_all_four_tiers_and_marks_one_current(conn, cfg):
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    ladder = RV.build(conn)["tier_ladder"]
    assert [t["tier"] for t in ladder] == ["R1", "R2", "R3", "R4"]
    assert [t["cap"] for t in ladder] == [100.0, 70.0, 40.0, 10.0]
    assert sum(1 for t in ladder if t["current"]) == 1
    for t in ladder:
        assert t["when"] and t["does"] and t["buys"], f"{t['tier']} is not explained"


def test_mode_explainer_covers_all_three_switches(conn, cfg):
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    sw = RV.build(conn)["mode_explainer"]
    assert [x["label"] for x in sw] == ["Feature", "Mode", "Orders"]
    for x in sw:
        assert x["means"] and len(x["means"]) > 30, f"{x['label']} has no plain meaning"


def test_portfolio_block_reads_the_real_snapshot(conn, cfg):
    import json
    db.save_snapshot(conn, {
        "date": "2026-06-05", "nav": 1_000_000.0, "invested": 700_000.0,
        "cash": 300_000.0,
        "holdings_json": json.dumps({"positions": [
            {"symbol": "ALPHA", "quantity": 100, "average_price": 100.0, "price": 150.0,
             "value": 15000.0, "pledged_qty": 40, "excluded": False},
            {"symbol": "BETA", "quantity": 50, "average_price": 200.0, "price": 180.0,
             "value": 9000.0, "pledged_qty": 0, "excluded": False},
            {"symbol": "SGBX-GB", "quantity": 10, "average_price": 5000.0,
             "price": 9000.0, "value": 90000.0, "pledged_qty": 10, "excluded": True}]})})
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    p = RV.build(conn)["portfolio"]

    assert p["available"] and p["as_of"] == "2026-06-05"
    assert p["count"] == 2 and p["excluded_count"] == 1
    assert p["winners"] == 1 and p["losers"] == 1
    assert p["pledged_count"] == 1
    names = [x["symbol"] for x in p["tradeable"]]
    assert names == ["ALPHA", "BETA"], "positions must be real names, largest first"


def test_untouchable_position_has_no_nav_weight(conn, cfg):
    """It sits outside NAV by construction, so a NAV weight would be a category error."""
    import json
    db.save_snapshot(conn, {
        "date": "2026-06-05", "nav": 100_000.0, "invested": 40_000.0, "cash": 60_000.0,
        "holdings_json": json.dumps({"positions": [
            {"symbol": "REAL", "quantity": 10, "average_price": 100.0, "price": 120.0,
             "value": 1200.0, "pledged_qty": 0, "excluded": False},
            {"symbol": "SGBX-GB", "quantity": 10, "average_price": 100.0, "price": 900.0,
             "value": 9000.0, "pledged_qty": 0, "excluded": True}]})})
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    pos = {x["symbol"]: x for x in RV.build(conn)["portfolio"]["positions"]}
    assert pos["SGBX-GB"]["weight_pct"] is None
    assert pos["REAL"]["weight_pct"] is not None


def test_portfolio_pnl_excludes_untouchables(conn, cfg):
    import json
    db.save_snapshot(conn, {
        "date": "2026-06-05", "nav": 100_000.0, "invested": 40_000.0, "cash": 60_000.0,
        "holdings_json": json.dumps({"positions": [
            {"symbol": "REAL", "quantity": 10, "average_price": 100.0, "price": 110.0,
             "value": 1100.0, "pledged_qty": 0, "excluded": False},
            {"symbol": "SGBX-GB", "quantity": 10, "average_price": 100.0, "price": 900.0,
             "value": 9000.0, "pledged_qty": 0, "excluded": True}]})})
    RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE)
    p = RV.build(conn)["portfolio"]
    assert p["total_pnl"] == pytest.approx(100.0)      # the SGB's 8000 is not counted


def test_no_plan_reason_is_shown_instead_of_an_empty_table(conn, cfg):
    from app.analytics import regime_run as RR
    st = make_state(cfg)
    snap = RR.evaluation_snapshot(None, None)
    snap["no_plan_reason"] = "Exposure is inside the cap, so no reduction is required."
    RS.save_preview(conn, st, mode=R.RegimeMode.OBSERVE, input_snapshot=snap)
    v = RV.build(conn)
    assert v["proposed_orders"] == []
    assert "inside the cap" in v["no_plan_reason"]


def test_charts_are_server_rendered_with_no_external_dependency():
    """No CDN, no build step, no script: the page must work offline."""
    html = open("app/templates/regime.html").read()
    assert "<script" not in html
    assert "cdn." not in html and "https://" not in html.split("<style>")[1].split("</style>")[0]
    assert ".track" in html and ".fill" in html          # weight bars
    assert ".dv .pos" in html and ".dv .neg" in html      # diverging P&L bars
