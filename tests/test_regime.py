"""Regime overlay — checkpoint 1: deterministic tests for the PURE domain engine.

No broker credentials, no network, no clock reads, no database. Every input is an
explicit fixture, which is what lets live evaluation and historical replay be asserted
identical.

Tests covering persistence, DRY_RUN order suppression, rollout modes, allocation solving,
FIFO tax lots and gateway sequence belong to later checkpoints — those subsystems do not
exist yet. They are listed in the checkpoint report rather than stubbed here.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.core import regime as R

D = dt.date


# =====================================================================================
# fixtures / builders
# =====================================================================================
@pytest.fixture()
def cfg() -> R.RegimeConfig:
    return R.RegimeConfig()


def candles(closes, start=D(2026, 1, 1), final=True):
    """Daily candles with a synthetic OHLC around each close."""
    return [R.Candle(date=start + dt.timedelta(days=i), open=c, high=c * 1.01,
                     low=c * 0.99, close=float(c), is_final=final)
            for i, c in enumerate(closes)]


def signals_for(name, closes, cfg, *, as_of=None, final=True):
    return R.build_index_signals(name, candles(closes, final=final), cfg, as_of=as_of)


def forced_signals(name, *, states, close=100.0, mas=None, as_of=D(2026, 6, 1),
                   stale=False, has_data=True):
    """Construct IndexSignals directly, to isolate classifier logic from MA maths."""
    sig = {}
    for length, state in states.items():
        ma = (mas or {}).get(length, close)
        sig[length] = R.MaSignal(ma_length=length, close=close, ma=ma,
                                 distance_pct=(close / ma - 1) * 100 if ma else None,
                                 state=state, confirming_closes=3)
    return R.IndexSignals(index_name=name, as_of_date=as_of, close=close, signals=sig,
                          is_stale=stale, has_data=has_data)


ABOVE, BELOW, UNKNOWN = R.SignalState.ABOVE, R.SignalState.BELOW, R.SignalState.UNKNOWN


def book(cfg, small=0.5, mid=0.3, large=0.2, source=R.WeightSource.CONFIG_DEFAULT):
    return R.BookWeights({"smallcap": small, "midcap": mid, "largecap": large},
                         source, 100.0)


def breadth(pct=60.0, *, as_of=D(2026, 6, 1), coverage=100.0):
    return R.BreadthReading(as_of_date=as_of, pct_above_20dma=pct, eligible_count=500,
                            observed_count=500, coverage_pct=coverage,
                            universe_id="nifty500", universe_hash="hash1",
                            audit_run_id="run1", calculation_version="v1")


def all_indices(cfg, *, state_map):
    """state_map: category -> {ma_length: SignalState}"""
    return {cfg.structural_indices[cat]: forced_signals(cfg.structural_indices[cat],
                                                        states=st)
            for cat, st in state_map.items()}


def healthy(cfg):
    st = {20: ABOVE, 50: ABOVE, 200: ABOVE}
    return all_indices(cfg, state_map={c: st for c in cfg.structural_indices})


_UNSET = object()


def evaluate(cfg, *, indices=None, sentinel=None, brd=_UNSET, bk=None, exposure=None,
             previous=None, as_of=D(2026, 6, 5), week_end=D(2026, 6, 5),
             session=D(2026, 6, 5)):
    return R.evaluate(
        as_of_date=as_of, scheduled_week_end=week_end, signal_session_date=session,
        index_signals=indices if indices is not None else healthy(cfg),
        sentinel=sentinel if sentinel is not None else forced_signals(
            cfg.momentum_sentinel, states={20: ABOVE, 50: ABOVE, 200: ABOVE}),
        breadth=breadth() if brd is _UNSET else brd,
        book=bk or book(cfg),
        exposure=exposure or R.ExposureSnapshot(actual_equity_pct=50.0),
        cfg=cfg, previous=previous)


# =====================================================================================
# hysteresis / signal state
# =====================================================================================
def test_two_confirming_closes_do_not_change_state(cfg):
    """Below for a long time, then only two closes above -> state must still be BELOW."""
    closes = [100.0] * 60 + [80.0] * 30 + [120.0, 121.0]
    mas = R.moving_average(closes, 20)
    states = R.confirmed_state_series(closes, mas, buffer=cfg.buffer, confirm_days=3)
    assert states[-1][0] is BELOW


def test_third_confirming_close_changes_state(cfg):
    closes = [100.0] * 60 + [80.0] * 30 + [120.0, 121.0, 122.0]
    mas = R.moving_average(closes, 20)
    states = R.confirmed_state_series(closes, mas, buffer=cfg.buffer, confirm_days=3)
    assert states[-1][0] is ABOVE


def test_buffer_band_preserves_previous_state(cfg):
    """A close 0.5% above the MA is inside the 1.5% buffer -> no state change."""
    closes = [100.0] * 60 + [80.0] * 30
    mas = R.moving_average(closes, 20)
    ma_now = mas[-1]
    inside = ma_now * 1.005                      # above the MA but inside the buffer
    closes2 = closes + [inside, inside, inside]
    mas2 = R.moving_average(closes2, 20)
    states = R.confirmed_state_series(closes2, mas2, buffer=cfg.buffer, confirm_days=3)
    assert states[-1][0] is BELOW               # retained, not flipped


def test_each_confirmation_day_uses_that_days_own_ma(cfg):
    """A rising MA must be able to invalidate an earlier 'above' close.

    If the implementation compared all three closes to the LATEST MA, this series would
    confirm ABOVE. Comparing each close to its own day's MA correctly refuses.
    """
    # MA climbs steeply; the first of the three closes is above its own MA, the later
    # ones are not, so no 3-day run exists on either side.
    closes = [10.0] * 19 + [10.0, 40.0, 40.0, 12.0, 12.0]
    mas = R.moving_average(closes, 20)
    per_day = [c > (m * (1 + cfg.buffer)) if m else None for c, m in zip(closes, mas)]
    assert per_day[-3:] == [True, False, False]
    states = R.confirmed_state_series(closes, mas, buffer=cfg.buffer, confirm_days=3)
    assert states[-1][0] is UNKNOWN              # never confirmed either side


def test_upward_and_downward_hysteresis_are_symmetric(cfg):
    up = [100.0] * 60 + [130.0, 131.0, 132.0]
    dn = [100.0] * 60 + [70.0, 69.0, 68.0]
    for series, expected in ((up, ABOVE), (dn, BELOW)):
        mas = R.moving_average(series, 20)
        st = R.confirmed_state_series(series, mas, buffer=cfg.buffer, confirm_days=3)
        assert st[-1][0] is expected


def test_state_starts_unknown_and_stays_until_first_confirmation(cfg):
    closes = [100.0] * 25
    mas = R.moving_average(closes, 20)
    states = R.confirmed_state_series(closes, mas, buffer=cfg.buffer, confirm_days=3)
    assert all(s is UNKNOWN for s, _ in states)


def test_confirming_close_counter(cfg):
    closes = [100.0] * 60 + [130.0] * 5
    mas = R.moving_average(closes, 20)
    states = R.confirmed_state_series(closes, mas, buffer=cfg.buffer, confirm_days=3)
    assert states[-1][1] == 5


def test_confirm_days_is_configurable(cfg):
    closes = [100.0] * 60 + [80.0] * 30 + [120.0, 121.0]
    mas = R.moving_average(closes, 20)
    two = R.confirmed_state_series(closes, mas, buffer=cfg.buffer, confirm_days=2)
    assert two[-1][0] is ABOVE                   # 2 closes suffice when configured


def test_buffer_is_stored_in_basis_points(cfg):
    assert cfg.buffer_bps == 150
    assert cfg.buffer == pytest.approx(0.015)


# =====================================================================================
# warm-up / unfinished candles
# =====================================================================================
def test_insufficient_warmup_leaves_long_ma_unknown(cfg):
    sig = signals_for("NIFTY 50", [100.0 + i for i in range(120)], cfg)
    assert sig.state(20) is ABOVE
    assert sig.state(50) is ABOVE
    assert sig.state(200) is UNKNOWN             # 120 < 200 observations
    assert sig.signals[200].ma is None


def test_unfinished_candle_is_never_consumed(cfg):
    final = candles([100.0] * 30)
    provisional = R.Candle(date=D(2026, 3, 1), open=1.0, high=1.0, low=1.0,
                           close=9_999.0, is_final=False)
    sig = R.build_index_signals("NIFTY 50", final + [provisional], cfg)
    assert sig.close == 100.0
    assert sig.as_of_date == final[-1].date


def test_no_final_candles_reports_no_data(cfg):
    sig = R.build_index_signals("NIFTY 50", candles([100.0] * 5, final=False), cfg)
    assert sig.has_data is False and sig.any_unknown is True


def test_staleness_is_measured_against_as_of(cfg):
    cs = candles([100.0] * 30, start=D(2026, 1, 1))
    fresh = R.build_index_signals("NIFTY 50", cs, cfg, as_of=cs[-1].date)
    stale = R.build_index_signals("NIFTY 50", cs, cfg,
                                  as_of=cs[-1].date + dt.timedelta(days=30))
    assert fresh.is_stale is False and stale.is_stale is True


# =====================================================================================
# book-composition weights
# =====================================================================================
def test_full_risk_target_is_preferred_source(cfg):
    target = [R.TargetPosition("A", "smallcap", 600_000.0),
              R.TargetPosition("B", "midcap", 300_000.0),
              R.TargetPosition("C", "largecap", 100_000.0)]
    bw = R.resolve_book_weights(cfg, full_risk_target=target)
    assert bw.source is R.WeightSource.FULL_RISK_TARGET
    assert bw.weights["smallcap"] == pytest.approx(0.6)
    assert sum(bw.weights.values()) == pytest.approx(1.0)


def test_weights_use_market_value_not_position_count(cfg):
    target = [R.TargetPosition("A", "smallcap", 900_000.0),
              R.TargetPosition("B", "largecap", 50_000.0),
              R.TargetPosition("C", "largecap", 50_000.0)]
    bw = R.resolve_book_weights(cfg, full_risk_target=target)
    assert bw.weights["smallcap"] == pytest.approx(0.9)   # 1 name, 90% of value


def test_falls_back_to_last_r1_snapshot(cfg):
    snap = R.BookWeights({"smallcap": 0.4, "midcap": 0.4, "largecap": 0.2},
                         R.WeightSource.FULL_RISK_TARGET, 100.0)
    bw = R.resolve_book_weights(cfg, full_risk_target=None, last_r1_snapshot=snap)
    assert bw.source is R.WeightSource.LAST_R1_SNAPSHOT
    assert bw.weights["midcap"] == pytest.approx(0.4)


def test_falls_back_to_config_defaults(cfg):
    bw = R.resolve_book_weights(cfg)
    assert bw.source is R.WeightSource.CONFIG_DEFAULT
    assert bw.weights == {"smallcap": 0.5, "midcap": 0.3, "largecap": 0.2}
    assert R.Reason.BOOK_WEIGHTS_CONFIG_DEFAULT in bw.reason_codes


def test_empty_holdings_fall_back_to_defaults(cfg):
    bw = R.resolve_book_weights(cfg, full_risk_target=[])
    assert bw.source is R.WeightSource.CONFIG_DEFAULT


def test_low_classification_coverage_falls_back_and_flags(cfg):
    target = [R.TargetPosition("A", "smallcap", 100_000.0),
              R.TargetPosition("B", None, 900_000.0)]        # 10% classified
    bw = R.resolve_book_weights(cfg, full_risk_target=target)
    assert bw.source is R.WeightSource.CONFIG_DEFAULT
    assert R.Reason.BOOK_CLASSIFICATION_COVERAGE_LOW in bw.reason_codes


def test_unknown_category_is_never_silently_reassigned(cfg):
    target = [R.TargetPosition("A", "smallcap", 850_000.0),
              R.TargetPosition("B", "midcap", 100_000.0),
              R.TargetPosition("C", "mystery", 50_000.0)]
    bw = R.resolve_book_weights(cfg, full_risk_target=target)
    assert "mystery" not in bw.weights
    assert bw.coverage_pct == pytest.approx(95.0)
    # the unclassified 5% is excluded from the denominator, not dumped into largecap
    assert bw.weights["largecap"] == pytest.approx(0.0)


def test_regime_reduced_holdings_are_never_reused_as_strategic_weights(cfg):
    """The core anti-feedback rule: R3 sells smallcaps, leaving a largecap-heavy residual.
    Next week's weights must come from the FULL-RISK target, not that residual — else the
    book looks defensively positioned and manufactures its own recovery signal."""
    full_risk = [R.TargetPosition("S", "smallcap", 500_000.0),
                 R.TargetPosition("M", "midcap", 300_000.0),
                 R.TargetPosition("L", "largecap", 200_000.0)]
    regime_reduced = [R.TargetPosition("L", "largecap", 200_000.0),
                      R.TargetPosition("M", "midcap", 50_000.0)]

    strategic = R.resolve_book_weights(cfg, full_risk_target=full_risk)
    residual = R.resolve_book_weights(cfg, full_risk_target=regime_reduced)

    assert strategic.weights["smallcap"] == pytest.approx(0.5)
    assert residual.weights["largecap"] == pytest.approx(0.8)   # the trap
    assert strategic.weights["largecap"] != residual.weights["largecap"]


def test_book_weights_are_frozen_for_the_evaluation(cfg):
    """Whatever BookWeights the caller froze is exactly what the state reports."""
    frozen = book(cfg, small=0.7, mid=0.2, large=0.1)
    st = evaluate(cfg, bk=frozen)
    assert st.book_category_weights == {"smallcap": 0.7, "midcap": 0.2, "largecap": 0.1}
    assert st.book_weight_source is R.WeightSource.CONFIG_DEFAULT


# =====================================================================================
# weighted health
# =====================================================================================
def test_weighted_health_per_ma_length(cfg):
    bk = book(cfg)
    idx = all_indices(cfg, state_map={
        "largecap": {20: ABOVE, 50: ABOVE, 200: ABOVE},
        "midcap": {20: ABOVE, 50: BELOW, 200: ABOVE},
        "smallcap": {20: BELOW, 50: BELOW, 200: BELOW},
    })
    assert R.weighted_health(bk, idx, cfg, 20) == pytest.approx(0.5)    # large+mid
    assert R.weighted_health(bk, idx, cfg, 50) == pytest.approx(0.2)    # large only
    assert R.weighted_health(bk, idx, cfg, 200) == pytest.approx(0.5)


def test_unknown_contributes_zero_to_health(cfg):
    bk = book(cfg)
    idx = all_indices(cfg, state_map={
        "largecap": {20: UNKNOWN, 50: UNKNOWN, 200: UNKNOWN},
        "midcap": {20: ABOVE, 50: ABOVE, 200: ABOVE},
        "smallcap": {20: ABOVE, 50: ABOVE, 200: ABOVE},
    })
    assert R.weighted_health(bk, idx, cfg, 200) == pytest.approx(0.8)


def test_bearish_stack_weight(cfg):
    bk = book(cfg)
    stacked = forced_signals("NIFTY SMLCAP 250", states={20: BELOW, 50: BELOW, 200: BELOW},
                             close=80.0, mas={20: 90.0, 50: 100.0, 200: 110.0})
    ok = forced_signals("NIFTY 50", states={20: ABOVE, 50: ABOVE, 200: ABOVE},
                        close=120.0, mas={20: 110.0, 50: 100.0, 200: 90.0})
    mid = forced_signals("NIFTY MIDCAP 150", states={20: BELOW, 50: BELOW, 200: BELOW},
                         close=80.0, mas={20: 90.0, 50: 100.0, 200: 110.0})
    idx = {"NIFTY SMLCAP 250": stacked, "NIFTY 50": ok, "NIFTY MIDCAP 150": mid}
    assert R.bearish_stack_weight(bk, idx, cfg) == pytest.approx(0.8)


def test_bearish_stack_requires_both_ordering_and_confirmed_below(cfg):
    # correct MA ordering but the close is confirmed ABOVE the 20-DMA
    sig = forced_signals("NIFTY 50", states={20: ABOVE, 50: BELOW, 200: BELOW},
                         mas={20: 90.0, 50: 100.0, 200: 110.0})
    assert sig.bearish_stack() is False


# =====================================================================================
# classifier boundaries (exact operators are part of the strategy contract)
# =====================================================================================
def sent(state_50=ABOVE, state_200=ABOVE):
    return forced_signals("NIFTY 500 MOMENTUM 50",
                          states={20: ABOVE, 50: state_50, 200: state_200})


def test_r1_requires_all_four_conditions(cfg):
    t, _ = R.classify_candidate(health={20: 1.0, 50: 0.8, 200: 0.8}, stack_weight=0.0,
                                breadth_pct=55.0, breadth_usable=True,
                                sentinel=sent(), cfg=cfg)
    assert t is R.RegimeTier.R1


@pytest.mark.parametrize("h50,h200,brd,expected", [
    (0.80, 0.80, 55.0, R.RegimeTier.R1),      # all exactly at the inclusive boundary
    (0.7999, 0.80, 55.0, R.RegimeTier.R2),    # h50 just below
    (0.80, 0.7999, 55.0, R.RegimeTier.R2),    # h200 just below (but > r3 threshold)
    (0.80, 0.80, 54.99, R.RegimeTier.R2),     # breadth just below
])
def test_r1_boundaries_are_inclusive(cfg, h50, h200, brd, expected):
    t, _ = R.classify_candidate(health={20: 1.0, 50: h50, 200: h200}, stack_weight=0.0,
                                breadth_pct=brd, breadth_usable=True,
                                sentinel=sent(), cfg=cfg)
    assert t is expected


@pytest.mark.parametrize("h200,expected", [
    (0.50, R.RegimeTier.R3),        # <= is inclusive
    (0.5001, R.RegimeTier.R2),
])
def test_r3_h200_boundary_is_inclusive(cfg, h200, expected):
    t, _ = R.classify_candidate(health={20: 1.0, 50: 0.6, 200: h200}, stack_weight=0.0,
                                breadth_pct=60.0, breadth_usable=True,
                                sentinel=sent(), cfg=cfg)
    assert t is expected


@pytest.mark.parametrize("brd,expected", [
    (39.99, R.RegimeTier.R3),
    (40.0, R.RegimeTier.R2),        # < is exclusive
])
def test_r3_breadth_boundary_is_exclusive(cfg, brd, expected):
    t, _ = R.classify_candidate(health={20: 1.0, 50: 0.6, 200: 0.6}, stack_weight=0.0,
                                breadth_pct=brd, breadth_usable=True,
                                sentinel=sent(), cfg=cfg)
    assert t is expected


@pytest.mark.parametrize("stack,brd,expected", [
    (0.60, 29.99, R.RegimeTier.R4),   # >= and < boundaries
    (0.5999, 29.99, R.RegimeTier.R3),
    (0.60, 30.0, R.RegimeTier.R3),
])
def test_r4_stack_and_breadth_boundaries(cfg, stack, brd, expected):
    t, _ = R.classify_candidate(health={20: 0.0, 50: 0.0, 200: 0.4}, stack_weight=stack,
                                breadth_pct=brd, breadth_usable=True,
                                sentinel=sent(), cfg=cfg)
    assert t is expected


def test_r4_via_sentinel_health_and_breadth(cfg):
    t, codes = R.classify_candidate(health={20: 0.0, 50: 0.0, 200: 0.29}, stack_weight=0.0,
                                    breadth_pct=29.0, breadth_usable=True,
                                    sentinel=sent(state_200=BELOW), cfg=cfg)
    assert t is R.RegimeTier.R4
    assert R.Reason.R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH in codes


def test_r2_is_the_default(cfg):
    t, codes = R.classify_candidate(health={20: 0.7, 50: 0.7, 200: 0.7}, stack_weight=0.0,
                                    breadth_pct=50.0, breadth_usable=True,
                                    sentinel=sent(), cfg=cfg)
    assert t is R.RegimeTier.R2 and R.Reason.R2_DEFAULT in codes


def test_unusable_breadth_cannot_permit_r1(cfg):
    t, _ = R.classify_candidate(health={20: 1.0, 50: 1.0, 200: 1.0}, stack_weight=0.0,
                                breadth_pct=90.0, breadth_usable=False,
                                sentinel=sent(), cfg=cfg)
    assert t is R.RegimeTier.R2


def test_unusable_breadth_cannot_independently_force_r4(cfg):
    """Weak breadth that we cannot trust must not trigger a crash reduction."""
    t, _ = R.classify_candidate(health={20: 0.0, 50: 0.0, 200: 0.6}, stack_weight=1.0,
                                breadth_pct=5.0, breadth_usable=False,
                                sentinel=sent(), cfg=cfg)
    assert t is not R.RegimeTier.R4


# =====================================================================================
# momentum sentinel veto
# =====================================================================================
def test_sentinel_below_50_floors_at_r2_and_blocks_buys(cfg):
    st = evaluate(cfg, sentinel=sent(state_50=BELOW))
    assert st.policy_tier.ordinal >= R.RegimeTier.R2.ordinal
    assert st.new_buys is R.NewBuyMode.BLOCKED
    assert R.Reason.SENTINEL_BELOW_50DMA_VETO in st.reason_codes


def test_sentinel_below_50_does_not_cause_binary_full_exit(cfg):
    st = evaluate(cfg, sentinel=sent(state_50=BELOW),
                  exposure=R.ExposureSnapshot(actual_equity_pct=100.0),
                  previous=R.PreviousRegime(R.RegimeTier.R1, D(2026, 5, 29)))
    assert st.policy_tier is R.RegimeTier.R2
    assert st.target_equity_cap_pct == 70.0            # not 0
    assert st.forced_action is R.ForcedAction.TRIM_TO_CAP


def test_sentinel_below_200_forces_at_least_r3(cfg):
    tier, codes = R.apply_sentinel_floor(R.RegimeTier.R1, sent(state_200=BELOW))
    assert tier is R.RegimeTier.R3
    assert R.Reason.SENTINEL_FLOOR_R3 in codes


def test_sentinel_floor_never_makes_the_tier_less_defensive(cfg):
    tier, _ = R.apply_sentinel_floor(R.RegimeTier.R4, sent(state_50=BELOW, state_200=BELOW))
    assert tier is R.RegimeTier.R4


def test_veto_removal_does_not_by_itself_produce_r1(cfg):
    """Reclaiming the 50-DMA lifts the buy veto but R1 still needs health + breadth."""
    idx = all_indices(cfg, state_map={
        "largecap": {20: ABOVE, 50: ABOVE, 200: ABOVE},
        "midcap": {20: ABOVE, 50: BELOW, 200: ABOVE},
        "smallcap": {20: BELOW, 50: BELOW, 200: BELOW},
    })
    st = evaluate(cfg, indices=idx, sentinel=sent(),
                  previous=R.PreviousRegime(R.RegimeTier.R2, D(2026, 5, 29)))
    assert st.policy_tier is not R.RegimeTier.R1


# =====================================================================================
# recovery override
# =====================================================================================
def test_recovery_override_lifts_r4_to_r2_only(cfg):
    tier, active, codes = R.apply_recovery_override(
        R.RegimeTier.R4, health={20: 0.8}, breadth_pct=60.0, breadth_usable=True,
        confirmations=2, cfg=cfg)
    assert tier is R.RegimeTier.R2 and active is True
    assert R.Reason.RECOVERY_OVERRIDE_APPLIED in codes


def test_recovery_override_never_reaches_r1(cfg):
    for start in (R.RegimeTier.R3, R.RegimeTier.R4):
        tier, _, _ = R.apply_recovery_override(
            start, health={20: 1.0}, breadth_pct=99.0, breadth_usable=True,
            confirmations=99, cfg=cfg)
        assert tier is R.RegimeTier.R2


def test_recovery_override_requires_confirmation(cfg):
    tier, active, codes = R.apply_recovery_override(
        R.RegimeTier.R4, health={20: 0.8}, breadth_pct=60.0, breadth_usable=True,
        confirmations=1, cfg=cfg)
    assert tier is R.RegimeTier.R4 and active is False
    assert R.Reason.RECOVERY_OVERRIDE_UNCONFIRMED in codes


def test_recovery_override_requires_h20(cfg):
    tier, active, codes = R.apply_recovery_override(
        R.RegimeTier.R4, health={20: 0.2}, breadth_pct=60.0, breadth_usable=True,
        confirmations=5, cfg=cfg)
    assert active is False and R.Reason.RECOVERY_OVERRIDE_H20_TOO_WEAK in codes


def test_recovery_override_requires_usable_breadth(cfg):
    tier, active, codes = R.apply_recovery_override(
        R.RegimeTier.R4, health={20: 0.9}, breadth_pct=90.0, breadth_usable=False,
        confirmations=5, cfg=cfg)
    assert active is False
    assert R.Reason.RECOVERY_OVERRIDE_BREADTH_UNUSABLE in codes


def test_recovery_override_does_not_bypass_the_rerisk_cap(cfg):
    """Override yields an R2 candidate, but R4 -> R2 is still capped to one tier."""
    policy, limited, _ = R.apply_transition(R.RegimeTier.R4, R.RegimeTier.R2, cfg)
    assert policy is R.RegimeTier.R3 and limited is True


def test_recovery_override_keeps_buys_blocked_under_sentinel_veto(cfg):
    idx = all_indices(cfg, state_map={c: {20: ABOVE, 50: BELOW, 200: BELOW}
                                      for c in cfg.structural_indices})
    st = evaluate(cfg, indices=idx, sentinel=sent(state_50=BELOW, state_200=BELOW),
                  brd=breadth(60.0),
                  previous=R.PreviousRegime(R.RegimeTier.R3, D(2026, 5, 29),
                                            recovery_confirmations=5))
    assert st.new_buys is R.NewBuyMode.BLOCKED


# =====================================================================================
# transitions
# =====================================================================================
def test_risk_off_is_direct_and_skips_tiers(cfg):
    for target in (R.RegimeTier.R3, R.RegimeTier.R4):
        policy, limited, codes = R.apply_transition(R.RegimeTier.R1, target, cfg)
        assert policy is target and limited is False
        assert R.Reason.TRANSITION_RISK_OFF_DIRECT in codes


def test_rerisking_is_capped_to_one_tier_per_week(cfg):
    policy, limited, codes = R.apply_transition(R.RegimeTier.R4, R.RegimeTier.R1, cfg)
    assert policy is R.RegimeTier.R3 and limited is True
    assert R.Reason.TRANSITION_RERISK_CAPPED in codes


def test_single_step_rerisk_is_not_flagged_as_limited(cfg):
    policy, limited, _ = R.apply_transition(R.RegimeTier.R3, R.RegimeTier.R2, cfg)
    assert policy is R.RegimeTier.R2 and limited is False


def test_rerisk_ladder_takes_three_weeks_from_r4_to_r1(cfg):
    tier = R.RegimeTier.R4
    seen = [tier]
    for _ in range(3):
        tier, _, _ = R.apply_transition(tier, R.RegimeTier.R1, cfg)
        seen.append(tier)
    assert seen == [R.RegimeTier.R4, R.RegimeTier.R3, R.RegimeTier.R2, R.RegimeTier.R1]


def test_unchanged_tier_reports_no_transition(cfg):
    policy, limited, codes = R.apply_transition(R.RegimeTier.R2, R.RegimeTier.R2, cfg)
    assert policy is R.RegimeTier.R2 and R.Reason.TRANSITION_NONE in codes


def test_same_week_rerun_is_idempotent(cfg):
    """Identical inputs must give an identical decision AND the same evaluation id."""
    prev = R.PreviousRegime(R.RegimeTier.R1, D(2026, 5, 29))
    a = evaluate(cfg, sentinel=sent(state_200=BELOW), previous=prev)
    b = evaluate(cfg, sentinel=sent(state_200=BELOW), previous=prev)
    assert a.as_dict() == b.as_dict()
    assert a.evaluation_id == b.evaluation_id


def test_evaluation_id_is_deterministic_per_week_and_session(cfg):
    x = R.make_evaluation_id(D(2026, 6, 5), D(2026, 6, 5), cfg)
    y = R.make_evaluation_id(D(2026, 6, 5), D(2026, 6, 5), cfg)
    z = R.make_evaluation_id(D(2026, 6, 12), D(2026, 6, 12), cfg)
    assert x == y and x != z


def test_evaluation_id_changes_with_config(cfg):
    other = R.replace(cfg, confirm_days=5)
    assert (R.make_evaluation_id(D(2026, 6, 5), D(2026, 6, 5), cfg)
            != R.make_evaluation_id(D(2026, 6, 5), D(2026, 6, 5), other))


def test_next_evaluation_lands_on_the_configured_weekday(cfg):
    nxt = R.next_evaluation_date(D(2026, 6, 5), cfg)      # Friday -> next Friday
    assert nxt.weekday() == cfg.weekly_evaluation_weekday
    assert nxt == D(2026, 6, 12)


def test_holiday_week_uses_the_last_completed_session(cfg):
    """Signals are keyed to the session date; the scheduled week end may differ."""
    st = evaluate(cfg, week_end=D(2026, 6, 5), session=D(2026, 6, 4))
    assert st.scheduled_week_end == D(2026, 6, 5)
    assert st.signal_session_date == D(2026, 6, 4)
    assert st.next_evaluation_date == D(2026, 6, 12)


# =====================================================================================
# bootstrap
# =====================================================================================
def test_bootstrap_never_forces_sells(cfg):
    st = evaluate(cfg, indices=all_indices(cfg, state_map={
        c: {20: BELOW, 50: BELOW, 200: BELOW} for c in cfg.structural_indices}),
        sentinel=sent(state_50=BELOW, state_200=BELOW), brd=breadth(20.0),
        exposure=R.ExposureSnapshot(actual_equity_pct=95.0), previous=None)
    assert st.previous_policy_tier is None
    assert st.forced_action is R.ForcedAction.NONE
    assert st.manual_action_required is True
    assert R.Reason.BOOTSTRAP_OBSERVE_ONLY in st.reason_codes


def test_bootstrap_adopt_candidate_policy_allows_reconciliation():
    cfg = R.RegimeConfig(bootstrap_policy=R.BootstrapPolicy.ADOPT_CANDIDATE)
    st = evaluate(cfg, indices=all_indices(cfg, state_map={
        c: {20: BELOW, 50: BELOW, 200: BELOW} for c in cfg.structural_indices}),
        sentinel=sent(state_50=BELOW, state_200=BELOW), brd=breadth(20.0),
        exposure=R.ExposureSnapshot(actual_equity_pct=95.0), previous=None)
    assert st.forced_action is not R.ForcedAction.NONE


# =====================================================================================
# data quality: asymmetric effects
# =====================================================================================
def test_stale_index_retains_tier_blocks_buys_and_forces_no_sell(cfg):
    idx = {n: forced_signals(n, states={20: ABOVE, 50: ABOVE, 200: ABOVE}, stale=True)
           for n in cfg.structural_indices.values()}
    st = evaluate(cfg, indices=idx,
                  exposure=R.ExposureSnapshot(actual_equity_pct=95.0),
                  previous=R.PreviousRegime(R.RegimeTier.R1, D(2026, 5, 29)))
    assert st.policy_tier is R.RegimeTier.R1        # retained
    assert st.new_buys is R.NewBuyMode.BLOCKED
    assert st.forced_action is R.ForcedAction.NONE
    assert st.data_stale is True
    assert R.Reason.TRANSITION_HELD_STALE_DATA in st.reason_codes


def test_missing_index_retains_tier_and_blocks_buys(cfg):
    idx = dict(healthy(cfg))
    idx.pop("NIFTY SMLCAP 250")
    st = evaluate(cfg, indices=idx,
                  previous=R.PreviousRegime(R.RegimeTier.R2, D(2026, 5, 29)))
    assert st.policy_tier is R.RegimeTier.R2
    assert st.new_buys is R.NewBuyMode.BLOCKED
    assert R.Reason.DATA_INDEX_MISSING in st.reason_codes


def test_stale_breadth_blocks_buys_but_causes_no_forced_sell(cfg):
    st = evaluate(cfg, brd=breadth(70.0, as_of=D(2026, 1, 1)),   # months old
                  exposure=R.ExposureSnapshot(actual_equity_pct=95.0),
                  previous=R.PreviousRegime(R.RegimeTier.R1, D(2026, 5, 29)))
    assert st.new_buys is R.NewBuyMode.BLOCKED
    assert st.forced_action is R.ForcedAction.NONE      # R1 cap is 100
    assert R.Reason.BREADTH_STALE in st.reason_codes


def test_genuine_risk_off_still_escalates_through_degraded_data(cfg):
    """Degraded inputs must not FREEZE a real crash either. Two indices are confirmed
    below their 200-DMA; even reading the stale third one charitably, H200 stays weak,
    so the escalation is genuine and must proceed."""
    idx = all_indices(cfg, state_map={
        "largecap": {20: BELOW, 50: BELOW, 200: BELOW},
        "smallcap": {20: BELOW, 50: BELOW, 200: BELOW},
    })
    idx["NIFTY MIDCAP 150"] = forced_signals(
        "NIFTY MIDCAP 150", states={20: ABOVE, 50: ABOVE, 200: ABOVE}, stale=True)
    st = evaluate(cfg, indices=idx, brd=breadth(35.0),
                  exposure=R.ExposureSnapshot(actual_equity_pct=90.0),
                  previous=R.PreviousRegime(R.RegimeTier.R1, D(2026, 5, 29)))
    assert st.policy_tier is R.RegimeTier.R3        # optimistic H200 = 0.3 <= 0.5
    assert st.forced_action is R.ForcedAction.REDUCE_TO_CAP
    assert st.data_stale is True


def test_degraded_data_can_never_rerisk(cfg):
    """Even a perfect-looking market cannot improve the tier while inputs are degraded."""
    idx = {n: forced_signals(n, states={20: ABOVE, 50: ABOVE, 200: ABOVE}, stale=True)
           for n in cfg.structural_indices.values()}
    st = evaluate(cfg, indices=idx, brd=breadth(95.0),
                  previous=R.PreviousRegime(R.RegimeTier.R3, D(2026, 5, 29)))
    assert st.policy_tier is R.RegimeTier.R3
    assert st.new_buys is R.NewBuyMode.BLOCKED
    assert R.Reason.TRANSITION_HELD_STALE_DATA in st.reason_codes


def test_r2_default_is_not_an_affirmative_risk_off_signal():
    """The distinction that fixes the stale-breadth bug: 'R1 not confirmed' is not danger."""
    assert R.Reason.R2_DEFAULT not in R.AFFIRMATIVE_RISK_OFF
    assert R.Reason.R3_H200_WEAK in R.AFFIRMATIVE_RISK_OFF
    assert R.Reason.SENTINEL_FLOOR_R3 in R.AFFIRMATIVE_RISK_OFF


def test_low_coverage_breadth_is_rejected(cfg):
    st = evaluate(cfg, brd=breadth(70.0, coverage=50.0),
                  previous=R.PreviousRegime(R.RegimeTier.R2, D(2026, 5, 29)))
    assert R.Reason.BREADTH_LOW_COVERAGE in st.reason_codes
    assert st.new_buys is R.NewBuyMode.BLOCKED
    assert st.policy_tier is not R.RegimeTier.R1


def test_missing_breadth_is_flagged(cfg):
    st = evaluate(cfg, brd=None,
                  previous=R.PreviousRegime(R.RegimeTier.R2, D(2026, 5, 29)))
    assert R.Reason.BREADTH_MISSING in st.reason_codes
    assert st.new_buys is R.NewBuyMode.BLOCKED


def test_unknown_signal_cannot_permit_r1_or_new_buys(cfg):
    idx = all_indices(cfg, state_map={
        "largecap": {20: ABOVE, 50: ABOVE, 200: ABOVE},
        "midcap": {20: ABOVE, 50: ABOVE, 200: ABOVE},
        "smallcap": {20: ABOVE, 50: ABOVE, 200: UNKNOWN},
    })
    st = evaluate(cfg, indices=idx, brd=breadth(80.0),
                  previous=R.PreviousRegime(R.RegimeTier.R2, D(2026, 5, 29)))
    assert st.raw_candidate_tier is not R.RegimeTier.R1
    assert st.policy_tier is not R.RegimeTier.R1
    assert st.new_buys is R.NewBuyMode.BLOCKED
    assert R.Reason.SIGNAL_UNKNOWN in st.reason_codes


# =====================================================================================
# exposure: signal and execution are separate facts
# =====================================================================================
def test_committed_tier_is_not_evidence_the_cap_was_achieved(cfg):
    st = evaluate(cfg, sentinel=sent(state_200=BELOW),
                  exposure=R.ExposureSnapshot(actual_equity_pct=90.0),
                  previous=R.PreviousRegime(R.RegimeTier.R1, D(2026, 5, 29)))
    assert st.policy_tier is R.RegimeTier.R3
    assert st.target_equity_cap_pct == 40.0
    assert st.actual_equity_pct == 90.0
    assert st.exposure_gap_pct == pytest.approx(50.0)
    assert st.forced_action is R.ForcedAction.REDUCE_TO_CAP


def test_partial_execution_preserves_the_gap_without_a_new_transition(cfg):
    """The retry path: same tier next week, gap still driving the forced action."""
    prev = R.PreviousRegime(R.RegimeTier.R3, D(2026, 6, 5),
                            last_transition_date=D(2026, 6, 5))
    st = evaluate(cfg, sentinel=sent(state_200=BELOW),
                  exposure=R.ExposureSnapshot(actual_equity_pct=65.0,
                                              pending_sell_pct=25.0,
                                              execution_status=R.ExecutionStatus.PARTIAL),
                  previous=prev, week_end=D(2026, 6, 12), session=D(2026, 6, 12))
    assert st.policy_tier is R.RegimeTier.R3
    assert st.transition_limited is False
    assert st.last_transition_date == D(2026, 6, 5)     # unchanged — no new transition
    assert st.exposure_gap_pct == pytest.approx(25.0)
    assert st.forced_action is R.ForcedAction.REDUCE_TO_CAP
    assert st.execution_status is R.ExecutionStatus.PARTIAL
    assert R.Reason.EXPOSURE_RECONCILE_RETRY in st.reason_codes


def test_exposure_within_tolerance_needs_no_action(cfg):
    st = evaluate(cfg, sentinel=sent(state_200=BELOW),
                  exposure=R.ExposureSnapshot(actual_equity_pct=40.5),
                  previous=R.PreviousRegime(R.RegimeTier.R3, D(2026, 5, 29)))
    assert st.forced_action is R.ForcedAction.NONE
    assert R.Reason.EXPOSURE_WITHIN_CAP in st.reason_codes


def test_r4_is_not_a_full_exit_at_the_default_cap(cfg):
    assert cfg.cap_for(R.RegimeTier.R4) == 10.0
    st = evaluate(cfg, indices=all_indices(cfg, state_map={
        c: {20: BELOW, 50: BELOW, 200: BELOW} for c in cfg.structural_indices}),
        sentinel=sent(state_50=BELOW, state_200=BELOW), brd=breadth(20.0),
        exposure=R.ExposureSnapshot(actual_equity_pct=80.0),
        previous=R.PreviousRegime(R.RegimeTier.R1, D(2026, 5, 29)))
    assert st.policy_tier is R.RegimeTier.R4
    assert st.target_equity_cap_pct == 10.0
    assert st.forced_action is R.ForcedAction.CRASH_REDUCE


def test_r4_can_be_configured_to_full_cash():
    cfg = R.RegimeConfig(tier_exposure_pct={"R1": 100.0, "R2": 70.0, "R3": 40.0, "R4": 0.0})
    cfg.validate()
    assert cfg.cap_for(R.RegimeTier.R4) == 0.0


# =====================================================================================
# new-buy modes
# =====================================================================================
def test_r1_permits_full_size_entries(cfg):
    st = evaluate(cfg, brd=breadth(80.0),
                  previous=R.PreviousRegime(R.RegimeTier.R1, D(2026, 5, 29)))
    assert st.policy_tier is R.RegimeTier.R1
    assert st.new_buys is R.NewBuyMode.FULL


def test_r2_permits_half_size_entries(cfg):
    idx = all_indices(cfg, state_map={
        "largecap": {20: ABOVE, 50: ABOVE, 200: ABOVE},
        "midcap": {20: ABOVE, 50: BELOW, 200: ABOVE},
        "smallcap": {20: ABOVE, 50: ABOVE, 200: ABOVE},
    })
    st = evaluate(cfg, indices=idx, brd=breadth(50.0),
                  previous=R.PreviousRegime(R.RegimeTier.R2, D(2026, 5, 29)))
    assert st.policy_tier is R.RegimeTier.R2
    assert st.new_buys is R.NewBuyMode.HALF


@pytest.mark.parametrize("tier", [R.RegimeTier.R3, R.RegimeTier.R4])
def test_r3_and_r4_block_new_entries(cfg, tier):
    mode, codes = R.resolve_new_buys(tier, sentinel=sent(), data_ok=True)
    assert mode is R.NewBuyMode.BLOCKED
    assert R.Reason.NEW_BUYS_BLOCKED_TIER in codes


# =====================================================================================
# configuration validation
# =====================================================================================
def test_default_config_is_valid(cfg):
    cfg.validate()


def test_ma_lengths_must_be_strictly_increasing():
    with pytest.raises(R.ConfigError, match="strictly increasing"):
        R.RegimeConfig(ma_lengths=(50, 20, 200)).validate()
    with pytest.raises(R.ConfigError, match="strictly increasing"):
        R.RegimeConfig(ma_lengths=(20, 20, 200)).validate()


def test_tier_caps_must_be_non_increasing():
    with pytest.raises(R.ConfigError, match="non-increasing"):
        R.RegimeConfig(tier_exposure_pct={"R1": 100.0, "R2": 40.0,
                                          "R3": 70.0, "R4": 10.0}).validate()


def test_confirm_days_must_be_at_least_one():
    with pytest.raises(R.ConfigError, match="confirm_days"):
        R.RegimeConfig(confirm_days=0).validate()


def test_buffer_bps_must_be_in_a_safe_range():
    with pytest.raises(R.ConfigError, match="buffer_bps"):
        R.RegimeConfig(buffer_bps=5000).validate()


def test_thresholds_must_be_in_range():
    with pytest.raises(R.ConfigError, match="percentage"):
        R.RegimeConfig(r1_breadth_min=155.0).validate()
    with pytest.raises(R.ConfigError, match="weighted-health fraction"):
        R.RegimeConfig(r1_h200_min=8.0).validate()


def test_weights_must_be_non_negative_and_normalizable():
    with pytest.raises(R.ConfigError, match="non-negative"):
        R.RegimeConfig(default_book_weights={"smallcap": -1.0, "midcap": 0.5,
                                             "largecap": 0.5}).validate()


def test_index_identifiers_must_be_non_empty():
    with pytest.raises(R.ConfigError, match="non-empty"):
        R.RegimeConfig(momentum_sentinel="  ").validate()


def test_validation_reports_every_problem_at_once():
    with pytest.raises(R.ConfigError) as e:
        R.RegimeConfig(confirm_days=0, buffer_bps=9999).validate()
    assert "confirm_days" in str(e.value) and "buffer_bps" in str(e.value)


# =====================================================================================
# reproducibility contract
# =====================================================================================
def test_state_persists_algorithm_version_and_config_hash(cfg):
    st = evaluate(cfg)
    assert st.algorithm_version == R.ALGORITHM_VERSION
    assert len(st.config_hash) == 16
    assert st.config_hash == cfg.config_hash()


def test_config_hash_is_stable_across_instances():
    assert R.RegimeConfig().config_hash() == R.RegimeConfig().config_hash()


def test_config_hash_changes_when_a_threshold_changes(cfg):
    assert cfg.config_hash() != R.replace(cfg, r1_breadth_min=60.0).config_hash()


def test_config_hash_ignores_rollout_mode(cfg):
    """observe/propose/enforce changes what we DO with a decision, not what it IS —
    including it would break replay of the same market state across rollout stages."""
    assert (R.replace(cfg, mode=R.RegimeMode.OBSERVE).config_hash()
            == R.replace(cfg, mode=R.RegimeMode.ENFORCE).config_hash())


def test_every_reason_code_has_display_text(cfg):
    codes = [v for k, v in vars(R.Reason).items() if not k.startswith("_")]
    missing = [c for c in codes if c not in R.DISPLAY_REASONS]
    assert missing == [], f"reason codes without display text: {missing}"


def test_state_reasons_align_with_codes(cfg):
    st = evaluate(cfg)
    assert len(st.reasons) == len(st.reason_codes)
    assert st.reasons == R.describe(st.reason_codes)


def test_reason_codes_are_deduplicated_and_ordered(cfg):
    st = evaluate(cfg, sentinel=sent(state_50=BELOW, state_200=BELOW))
    assert len(st.reason_codes) == len(set(st.reason_codes))


def test_state_serialises_to_json_safe_primitives(cfg):
    import json
    st = evaluate(cfg)
    blob = json.dumps(st.as_dict())          # must not raise
    assert '"policy_tier"' in blob


def test_live_and_replay_paths_are_identical(cfg):
    """The acceptance requirement: same inputs -> byte-identical decisions."""
    args = dict(indices=healthy(cfg), sentinel=sent(), brd=breadth(80.0),
                bk=book(cfg), exposure=R.ExposureSnapshot(actual_equity_pct=60.0),
                previous=R.PreviousRegime(R.RegimeTier.R1, D(2026, 5, 29)))
    live = evaluate(cfg, **args)
    replay = evaluate(cfg, **args)
    assert live.as_dict() == replay.as_dict()


def test_build_index_signals_is_deterministic(cfg):
    cs = [100.0 + (i % 7) for i in range(260)]
    a = R.build_index_signals("NIFTY 50", candles(cs), cfg)
    b = R.build_index_signals("NIFTY 50", candles(cs), cfg)
    assert a.as_dict() == b.as_dict()


# =====================================================================================
# guardrails: this module must stay pure
# =====================================================================================
def test_regime_module_imports_nothing_from_the_order_path():
    import app.core.regime as mod
    src = open(mod.__file__).read()
    for forbidden in ("place_order", "place_gtt", "OrderGateway", "kiteconnect",
                      "sqlite3", "import requests"):
        assert forbidden not in src, f"regime.py must stay pure — found {forbidden!r}"


def test_regime_module_does_not_read_the_clock():
    import app.core.regime as mod
    src = open(mod.__file__).read()
    assert "date.today()" not in src and "datetime.now(" not in src
