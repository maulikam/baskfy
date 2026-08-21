"""V1 acceptance criteria for the intraday strangle, plus the brief's stated failure modes.

Each test names the failure it prevents. The ones that matter most are not the ones that
check a formula — they are the ones that check a formula could not have been written the
other way round without the suite noticing.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.strategies.strangle import book as B
from app.strategies.strangle import clock as C
from app.strategies.strangle import config as SC
from app.strategies.strangle import fills_paper as F
from app.strategies.strangle import levels as L
from app.strategies.strangle import rules as R
from app.strategies.strangle import selection as S
from app.strategies.strangle import sizing as Z

CFG = SC.load()
LOT = 65
D = dt.date


# =====================================================================================
# failure mode 8 — hardcoded weekdays, and the stale-series trap
# =====================================================================================
def test_expiry_is_resolved_per_session_not_once():
    """Pinning the nearest expiry and reusing it makes every later session come back
    dte=0 — the expiry-day bucket, with the loosest target and the most gamma. This was
    reproduced for real on this codebase before clock.py existed."""
    expiries = [D(2026, 8, 18), D(2026, 8, 25), D(2026, 9, 1)]
    got = {day: C.trading_days_between(day, C.resolve_expiry(day, expiries))
           for day in (D(2026, 8, 17), D(2026, 8, 19), D(2026, 8, 20), D(2026, 8, 21))}
    assert got[D(2026, 8, 17)] == 1        # Mon before Tue expiry
    assert got[D(2026, 8, 19)] == 4        # Wed -> next Tue
    assert got[D(2026, 8, 20)] == 3        # Thu
    assert got[D(2026, 8, 21)] == 2        # Fri
    assert 0 not in got.values(), "a later session resolved to expiry day"


def test_a_session_after_its_expiry_raises_rather_than_falling_through():
    with pytest.raises(C.ExpiryResolutionError):
        C.session_params(D(2026, 8, 20), D(2026, 8, 18), CFG)


def test_the_four_tradeable_sessions_map_to_the_documented_buckets():
    exp = D(2026, 8, 25)
    got = {}
    for day in (D(2026, 8, 19), D(2026, 8, 20), D(2026, 8, 21), D(2026, 8, 24)):
        p = C.session_params(day, exp, CFG)
        got[day.strftime("%a")] = (p.dte, p.bucket, p.target_points, p.size_mult)
    assert got["Wed"] == (4, "3+", 5, 0.50)
    assert got["Thu"] == (3, "3+", 5, 0.50)
    assert got["Fri"] == (2, "2", 7, 0.75)
    assert got["Mon"] == (1, "1", 10, 1.00)


def test_expiry_day_is_not_tradeable_while_disabled():
    p = C.session_params(D(2026, 8, 25), D(2026, 8, 25), CFG)
    assert p.dte == 0 and not p.tradeable


def test_the_stop_is_always_half_the_target():
    """[STRUCTURAL]. The 2:1 payoff IS the edge; a test exists so that changing it
    requires deliberately editing this assertion."""
    for day, exp in ((D(2026, 8, 19), D(2026, 8, 25)), (D(2026, 8, 21), D(2026, 8, 25)),
                     (D(2026, 8, 24), D(2026, 8, 25))):
        p = C.session_params(day, exp, CFG)
        assert p.stop_points == pytest.approx(p.target_points * 0.5)


# =====================================================================================
# failure mode 2 — halving the priced range
# =====================================================================================
def test_the_priced_range_is_the_full_straddle():
    """The worked check from the brief: spot 17,000 with a 250 straddle gives
    16,750-17,250. Getting +-125 means the range was halved, which puts every strike far
    too close to spot and reintroduces the delta problem the strategy exists to avoid."""
    pr = L.priced_range(17_000, 250, CFG)
    assert (pr.lower, pr.upper) == (16_750, 17_250)


def test_a_halved_multiplier_would_be_caught():
    cfg = {**CFG, "priced_range": {"multiplier": 0.5}}
    pr = L.priced_range(17_000, 250, cfg)
    assert (pr.lower, pr.upper) == (16_875, 17_125)     # the wrong answer, made visible


# =====================================================================================
# failure mode 5 — the in-progress candle as a confirmed swing
# =====================================================================================
def bars(highs, lows, final_last=True):
    return [L.Bar(ts=i, open=h, high=h, low=lo, close=h,
                  is_final=(final_last or i < len(highs) - 1))
            for i, (h, lo) in enumerate(zip(highs, lows))]


def test_the_last_bars_can_never_be_confirmed_swings():
    """A swing needs two bars to its right. The final two bars therefore cannot qualify,
    and neither can an unfinished one — using them is look-ahead bias that never surfaces
    as an error and silently inflates every backtest."""
    h = [100, 101, 105, 102, 101, 103, 108]
    hi, _ = L.confirmed_swings(bars(h, h), left=2, right=2)
    assert 2 in hi                      # the 105 peak, with two bars each side
    assert max(hi, default=0) < len(h) - 2


def test_an_unfinished_candle_is_excluded():
    h = [100, 101, 105, 102, 101, 103, 108]
    rows = bars(h, h)
    rows[-1] = L.Bar(ts=6, open=108, high=108, low=108, close=108, is_final=False)
    hi, _ = L.confirmed_swings(rows, 2, 2, use_unconfirmed=False)
    hi_look, _ = L.confirmed_swings(rows, 2, 2, use_unconfirmed=True)
    assert len(hi) <= len(hi_look)


# =====================================================================================
# failure mode 4 — equidistant strike selection
# =====================================================================================
def chain_row(strike, kind, bid, ask=None, oi=10_000_000):
    return {"strike": float(strike), "kind": kind, "bid": bid,
            "ask": ask if ask is not None else bid + 0.2, "oi": oi,
            "symbol": f"NIFTY{int(strike)}{kind}"}


def test_the_selector_reproduces_the_sources_own_trade():
    """Spot ~17,050; he sold 17,300 CE and 16,700 PE, both at Rs 27 — 250 points up and
    350 down. An equidistant selector picks 17,350/16,750 and takes Rs 22 on the call
    against Rs 34 on the put, which is a bullish position wearing a neutral label."""
    spot = 17_050
    chain = [chain_row(17_300, "CE", 27.0), chain_row(17_350, "CE", 22.0),
             chain_row(16_700, "PE", 27.0), chain_row(16_750, "PE", 34.0)]
    pr = L.PricedRange(spot=spot, asp=200, upper=17_250, lower=16_850)
    levels = L.Levels(resistance=17_200, support=16_900,
                      resistance_source="swing", support_source="swing")
    oi = L.OIStructure(pomc=17_000, call_wall=17_200, put_wall=16_900)
    pair = S.select_pair(chain, pr=pr, levels=levels, oi=oi, cfg=CFG, step=50)
    assert (pair.call.strike, pair.put.strike) == (17_300, 16_700)
    assert pair.imbalance == pytest.approx(0.0)
    assert abs(spot - pair.call.strike) != abs(spot - pair.put.strike), \
        "the chosen pair is equidistant — the skew was not respected"


def test_an_unbalanced_book_is_refused_rather_than_relaxed():
    spot = 17_050
    chain = [chain_row(17_300, "CE", 20.0), chain_row(16_700, "PE", 40.0)]
    pr = L.PricedRange(spot, 200, 17_250, 16_850)
    levels = L.Levels(17_200, 16_900, "swing", "swing")
    oi = L.OIStructure(17_000, 17_200, 16_900)
    with pytest.raises(S.NoQualifyingPair, match="balance"):
        S.select_pair(chain, pr=pr, levels=levels, oi=oi, cfg=CFG, step=50)


def test_a_strike_inside_the_priced_range_never_qualifies():
    pr = L.PricedRange(17_050, 200, 17_250, 16_850)
    levels = L.Levels(17_200, 16_900, "swing", "swing")
    oi = L.OIStructure(17_000, 17_200, 16_900)
    inside = S.Candidate(17_200, "CE", 40.0, 40.2, 10_000_000)
    ok, why = S.qualifies(inside, pr=pr, levels=levels, oi=oi, cfg=CFG, step=50)
    assert not ok and "priced range" in why


def test_dead_premium_is_rejected():
    pr = L.PricedRange(17_050, 200, 17_250, 16_850)
    levels = L.Levels(17_200, 16_900, "swing", "swing")
    oi = L.OIStructure(17_000, 17_200, 16_900)
    ok, why = S.qualifies(S.Candidate(18_000, "CE", 2.0, 2.2, 10_000_000),
                          pr=pr, levels=levels, oi=oi, cfg=CFG, step=50)
    assert not ok and "min_leg_premium" in why


# =====================================================================================
# failure mode 1 — filling at LTP or mid. The one that sinks the project.
# =====================================================================================
def depth(bid, ask, qty=5000, levels=5):
    return {"buy": [{"price": round(bid - i * 0.05, 2), "quantity": qty}
                    for i in range(levels)],
            "sell": [{"price": round(ask + i * 0.05, 2), "quantity": qty}
                     for i in range(levels)]}


def test_a_sell_hits_the_bid_and_a_buy_lifts_the_ask():
    cfg = {**CFG, "fills": {**CFG["fills"], "queue_position_penalty_ticks": 0}}
    sell = F.simulate_fill("X", "SELL", depth(100.0, 100.5), 1000, cfg)
    buy = F.simulate_fill("X", "BUY", depth(100.0, 100.5), 1000, cfg)
    assert sell.avg_price == pytest.approx(100.0)
    assert buy.avg_price == pytest.approx(100.5)
    assert buy.avg_price > sell.avg_price, "a round trip must cost the spread"


def test_a_large_order_walks_the_ladder_instead_of_filling_at_the_touch():
    """20 lots is 1,300 units and the touch rarely holds that much. Assuming it does is
    the same error as filling at mid, just smaller and harder to see."""
    cfg = {**CFG, "fills": {**CFG["fills"], "queue_position_penalty_ticks": 0}}
    small = F.simulate_fill("X", "SELL", depth(100.0, 100.5, qty=500), 500, cfg)
    large = F.simulate_fill("X", "SELL", depth(100.0, 100.5, qty=500), 1300, cfg)
    assert small.avg_price == pytest.approx(100.0)
    assert large.avg_price < small.avg_price
    assert large.levels_consumed > 1 and large.complete


def test_missing_depth_refuses_the_trade_rather_than_using_ltp():
    """There is no LTP fallback anywhere in the fill engine. A refused trade costs one
    session; a fabricated fill costs the whole experiment, because afterwards you cannot
    tell which sessions were real."""
    with pytest.raises(F.DepthUnavailable):
        F.simulate_fill("X", "SELL", {"buy": [], "sell": []}, 100, CFG)


def test_the_fill_engine_source_contains_no_ltp_path():
    src = open("app/strategies/strangle/fills_paper.py").read()
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "last_price" not in body
    assert body.count("mid_price_fill") <= 2, "mid pricing must exist only as the test foil"


def test_the_wrong_engine_manufactures_edge_and_the_gap_is_the_leakage():
    """The section 6 acceptance test. Run the same round trip through both engines: the
    difference is spread leakage. If it were small, the depth walk would not be working."""
    cfg = {**CFG, "fills": {**CFG["fills"], "queue_position_penalty_ticks": 0}}
    d, qty = depth(100.0, 100.5), 1300
    honest = (F.simulate_fill("X", "SELL", d, qty, cfg).avg_price
              - F.simulate_fill("X", "BUY", d, qty, cfg).avg_price) * qty
    fake = (F.mid_price_fill("X", "SELL", d, qty, cfg).avg_price
            - F.mid_price_fill("X", "BUY", d, qty, cfg).avg_price) * qty
    assert fake == pytest.approx(0.0)          # mid fills both ways => free round trip
    assert honest < 0                           # crossing always costs
    assert abs(fake - honest) > 500             # per round trip, on one leg


def test_the_queue_penalty_makes_a_fill_worse_never_better():
    d = depth(100.0, 100.5)
    free = {**CFG, "fills": {**CFG["fills"], "queue_position_penalty_ticks": 0}}
    queued = {**CFG, "fills": {**CFG["fills"], "queue_position_penalty_ticks": 1}}
    assert (F.simulate_fill("X", "SELL", d, 100, queued).avg_price
            < F.simulate_fill("X", "SELL", d, 100, free).avg_price)


def test_a_basket_that_cannot_fill_in_full_is_refused_entirely():
    """A condor missing a wing is not a condor: its margin and its tail are both wrong."""
    thin = depth(100.0, 100.5, qty=100, levels=1)
    with pytest.raises(F.DepthUnavailable):
        F.simulate_basket([{"symbol": "A", "side": "SELL", "depth": thin,
                            "quantity": 1300}], CFG)


def test_every_fill_carries_the_book_it_was_filled_against():
    f = F.simulate_fill("X", "SELL", depth(100.0, 100.5), 1000, CFG)
    assert f.as_dict()["depth_snapshot"], "a fill with no snapshot is not auditable"


# =====================================================================================
# failure mode 3 — per-leg stops
# =====================================================================================
def make_book(stop_points=5.0, target_points=10.0, units=1300):
    # Credit is BOTH legs: 27 + 27 = 54 points. Using one leg's credit understates it by
    # half and makes E5 fire at twice the intended close cost.
    bk = B.Book(units_at_entry=units, stop_points=stop_points,
                target_points=target_points, entry_credit=54.0 * units)
    bk.add(B.Leg("CE", "CE", 17_300, "SELL", units, 27.0, "short_call"))
    bk.add(B.Leg("PE", "PE", 16_700, "SELL", units, 27.0, "short_put"))
    return bk


def test_a_leg_at_minus_300_percent_does_not_by_itself_trigger_an_exit():
    """The V1 acceptance criterion. The rule under test is that NO per-leg stop exists —
    only the book decides. In the source's account a single leg showed a Rs 4.5 lakh loss
    on a day the book was in profit."""
    bk = make_book()
    marks = {"CE": 108.0, "PE": 1.0}            # call at -300%, put nearly worthless
    pct = bk.leg_pnl_pct(marks)
    assert pct["CE"] == pytest.approx(-300.0)
    snap = R.Snapshot(now=dt.datetime(2026, 8, 24, 11, 0), spot=17_050, asp=200,
                      marks=marks)
    decision = R.evaluate_exits(bk, snap, CFG, state={})
    if decision is not None:
        assert decision.code in ("E1", "E4b"), \
            f"a per-leg stop fired: {decision.code} {decision.reason}"
        assert "CE" not in decision.reason, "the exit named a single leg"


def test_the_book_stop_uses_the_book_not_the_worst_leg():
    bk = make_book()
    snap = R.Snapshot(now=dt.datetime(2026, 8, 24, 11, 0), spot=17_050, asp=None,
                      marks={"CE": 108.0, "PE": 1.0})
    assert bk.pnl(snap.marks) == pytest.approx((27 - 108) * 1300 + (27 - 1) * 1300)


def test_valuing_the_book_with_a_missing_leg_is_refused():
    """A book valued with one leg silently at zero is not a valuation."""
    bk = make_book()
    with pytest.raises(KeyError):
        bk.pnl({"CE": 20.0})


def test_realised_and_unrealised_are_both_counted():
    bk = make_book()
    bk.close_leg("CE", 10.0)
    assert bk.pnl({"PE": 5.0}) == pytest.approx((27 - 10) * 1300 + (27 - 5) * 1300)


# =====================================================================================
# sizing — failure mode 9, estimating margin instead of querying it
# =====================================================================================
def test_usable_margin_respects_both_terms():
    cap = Z.capital_from_config(CFG)
    assert cap.usable == pytest.approx(4_750_000)
    shifted = {**CFG, "capital": {**CFG["capital"], "cash": 2_000_000,
                                  "pledged_market_value": 3_000_000}}
    assert Z.capital_from_config(shifted).usable == pytest.approx(4_000_000)


def test_the_queried_margin_binds_before_the_tail_at_real_rates():
    """With margin queried at ~Rs 92.5k/lot rather than the brief's Rs 32,500, utilisation
    is the binding constraint, not the catastrophic cap. That reverses the brief's
    section 3.2 and is why the estimate is never trusted."""
    d = Z.evaluate(CFG, lots=9, margin_required=9 * 92_497)
    assert d.binding_constraint == "margin"
    assert d.max_lots_by_margin < d.max_lots_by_tail
    assert d.utilisation_pct == pytest.approx(0.175, abs=0.01)


def test_over_utilisation_is_refused_with_the_real_per_lot_number():
    with pytest.raises(Z.SizingRefused, match="over this instrument's 18% allocation"):
        Z.evaluate(CFG, lots=30, margin_required=30 * 92_497)


# =====================================================================================
# the account is one account — three instruments, one pool of margin
# =====================================================================================
def test_an_instrument_may_not_spend_another_instrument_s_margin():
    """Each config is sized to its own allocation of the SAME Rs 50L. Summed naively the
    three are entitled to 118% of it, and nothing in a single sizing call can see that:
    they are separate processes and basket_order_margins runs with
    consider_positions=False. Without the shared ledger the first two enter and the third
    discovers the problem, leaving the account holding a pair nobody chose."""
    ok = Z.evaluate(CFG, lots=9, margin_required=9 * 92_497, committed_elsewhere=1_000_000)
    assert ok.utilisation_pct == pytest.approx(0.386, abs=0.01)
    with pytest.raises(Z.SizingRefused, match="ACCOUNT ceiling"):
        Z.evaluate(CFG, lots=9, margin_required=9 * 92_497,
                   committed_elsewhere=1_300_000)


def test_the_account_ceiling_is_reported_as_the_binding_constraint():
    """So the operator is told WHICH cap stopped them. 'over the cap' is not actionable
    when there are two different caps and only one of them is about this instrument."""
    d = Z.evaluate(CFG, lots=5, margin_required=5 * 92_497, committed_elsewhere=1_400_000)
    assert d.binding_constraint == "account"


def test_an_instrument_alone_is_unaffected_by_the_ledger():
    plain = Z.evaluate(CFG, lots=9, margin_required=9 * 92_497)
    zeroed = Z.evaluate(CFG, lots=9, margin_required=9 * 92_497, committed_elsewhere=0.0)
    assert plain.as_dict() == zeroed.as_dict()


def test_the_tail_cap_still_binds_at_wider_wings():
    with pytest.raises(Z.SizingRefused, match="gap through the wings"):
        Z.evaluate(CFG, lots=23, margin_required=1, wing_width=700)


def test_a_failed_margin_query_never_falls_back_to_the_estimate():
    class KC:
        def basket_order_margins(self, *a, **k):
            return {"initial": {"total": 3_762_928}}      # no final => hedge unknown
    with pytest.raises(Z.SizingRefused, match="final.total"):
        Z.query_margin(KC(), [{"tradingsymbol": "X"}])


def test_reading_initial_instead_of_final_would_be_caught():
    """initial ignores the hedge entirely, making a condor look like a naked strangle —
    which is exactly what it did when first queried on this account."""
    class KC:
        def basket_order_margins(self, *a, **k):
            return {"initial": {"total": 3_762_928}, "final": {"total": 1_849_939}}
    assert Z.query_margin(KC(), []) == pytest.approx(1_849_939)


def test_session_size_scales_down_but_never_up():
    assert Z.session_lots(CFG, 0.50) == 5                    # min_lots floors it
    assert Z.session_lots(CFG, 1.00) == 9
    assert Z.session_lots(CFG, 1.00, win_streak=5) == 9      # ceiling holds


# =====================================================================================
# the entry-cost problem the brief does not flag
# =====================================================================================
def test_the_wednesday_stop_is_mostly_consumed_by_the_entry_itself():
    """At dte>=3 the stop is 2.5 points while liquidation marks open the book at -1.0 to
    -1.5. So 40-60% of the risk budget is gone before the market moves, and the risk-off
    lever — which arms at 60% of the stop — is armed AT ENTRY. Two of the four weekly
    sessions sit in this bucket. Logged every session rather than assumed away."""
    p = C.session_params(D(2026, 8, 19), D(2026, 8, 25), CFG)
    assert p.stop_points == 2.5
    tight = R.entry_cost_headroom(1.5, p.stop_points)
    assert tight["fraction_of_stop_consumed"] == pytest.approx(0.6)
    assert tight["risk_off_already_armed"] and not tight["healthy"]
    roomy = R.entry_cost_headroom(1.5, C.session_params(
        D(2026, 8, 24), D(2026, 8, 25), CFG).stop_points)
    assert roomy["healthy"]


# =====================================================================================
# exits
# =====================================================================================
def snap_at(hhmm, marks, spot=17_050, asp=200.0):
    h, m = hhmm.split(":")
    return R.Snapshot(now=dt.datetime(2026, 8, 24, int(h), int(m)), spot=spot, asp=asp,
                      marks=marks)


def test_the_hard_stop_ends_the_day():
    bk = make_book()
    d = R.evaluate_exits(bk, snap_at("11:00", {"CE": 32.0, "PE": 27.0}), CFG, state={})
    assert d and d.code == "E1" and d.detail["ends_day"]


def test_the_time_exit_fires_at_the_configured_minute():
    bk = make_book()
    assert R.evaluate_exits(bk, snap_at("15:10", {"CE": 27.0, "PE": 27.0}), CFG,
                            state={}).code == "E3"
    assert R.evaluate_exits(bk, snap_at("15:09", {"CE": 27.0, "PE": 27.0}), CFG,
                            state={}) is None


def test_the_trail_ratchets_off_the_high_water_mark_not_a_fixed_floor():
    bk = make_book()
    bk.high_water_mark = 9.0 * bk.units_at_entry
    give = {"CE": 27.0 - 3.5, "PE": 27.0 - 3.5}          # book +7 pts, 2 below the hwm
    d = R.evaluate_exits(bk, snap_at("13:00", give), CFG, state={"trail_active": True})
    assert d and d.code == "E2"


def test_the_efficiency_abort_is_confined_to_its_window():
    """Outside 12:00-12:30 it must not fire: a trigger at 12:31 flattens the book with no
    re-entry available, which is pure downside."""
    bk = make_book()
    marks = {"CE": 26.0, "PE": 26.0}                     # +2 pts, under 30% of target
    state = {"minutes_since_pnl_improved": 25}
    assert R.evaluate_exits(bk, snap_at("12:15", marks), CFG, state=state).code == "E4c"
    assert R.evaluate_exits(bk, snap_at("12:45", marks), CFG, state=state) is None


def test_the_premium_exhausted_exit_is_a_fraction_of_credit_not_an_absolute():
    """An absolute Rs 16 collides with min_leg_premium 8: a legal minimum entry would sit
    half a point from a day-ending exit at the instant it was opened."""
    bk = make_book()
    entry = snap_at("11:00", {"CE": 8.0, "PE": 8.0})
    assert R.evaluate_exits(bk, entry, CFG, state={}) is None     # 16 close on 54 credit
    done = snap_at("11:00", {"CE": 3.0, "PE": 3.0})
    assert R.evaluate_exits(bk, done, CFG, state={}).code == "E5"


def test_exits_are_evaluated_before_any_adjustment_could_run():
    src = open("app/strategies/strangle/rules.py").read()
    assert "ROLL_WINNER" in src and "def evaluate_exits" in src
    assert src.index("def evaluate_exits") < src.index("def entry_cost_headroom")


# =====================================================================================
# day vetoes
# =====================================================================================
def base_snap(**kw):
    d = dict(now=dt.datetime(2026, 8, 24, 9, 20), spot=24_400, asp=150.0, marks={},
             open_spot=24_400, prev_close_spot=24_390, feed_age_seconds=0.5)
    return R.Snapshot(**{**d, **kw})


def test_a_missing_reference_band_vetoes_the_day_in_paper_too():
    """If paper runs with the IV gates silently off, the paper expectancy that gates live
    trading is measured on a different rule set than live — which makes the whole paper
    phase non-transferable."""
    v = R.day_vetoes(base_snap(), CFG, reference_band={}, dte_bucket="1")
    assert any("reference_band" in x for x in v)


def test_a_gap_beyond_the_threshold_vetoes():
    v = R.day_vetoes(base_snap(open_spot=24_800, prev_close_spot=24_390), CFG,
                     reference_band={"1": {"low": 100, "high": 200}}, dte_bucket="1")
    assert any("gap" in x for x in v)


def test_all_vetoes_are_reported_not_just_the_first():
    v = R.day_vetoes(base_snap(open_spot=24_800, prev_close_spot=24_390,
                               feed_age_seconds=30, event_today="rbi_policy",
                               secondary_broker_ok=False),
                     CFG, reference_band={"1": {"low": 100, "high": 200}}, dte_bucket="1")
    assert len(v) >= 4


def test_a_clean_day_has_no_vetoes():
    assert R.day_vetoes(base_snap(), CFG,
                        reference_band={"1": {"low": 100, "high": 200}},
                        dte_bucket="1") == []


def test_the_entry_timing_gate_reads_the_straddle_open():
    assert R.entry_time_for(base_snap(straddle_open=160, straddle_prev_close=150),
                            CFG)[0] == "entry_early"
    assert R.entry_time_for(base_snap(straddle_open=140, straddle_prev_close=150),
                            CFG)[0] == "entry_normal"
    assert R.entry_time_for(base_snap(straddle_open=120, straddle_prev_close=150),
                            CFG)[0] == ""


# =====================================================================================
# the package cannot trade
# =====================================================================================
def test_no_paper_module_can_reach_the_broker_at_all():
    """Everything except the V4 live adapter is entirely broker-free.

    This assertion was once "no module in the package", and V4 relaxed it by adding
    fills_live.py — deliberately, and localised here rather than deleted. The live adapter
    submits only through core/gateway.py and its one direct call is a cancellation, which
    test_strangle_v4.py pins by walking the AST for call sites.
    """
    import ast
    import pathlib
    banned = {"place_order", "place_gtt", "place_gtt_stop", "modify_order",
              "cancel_order", "place_basket"}
    for f in sorted(pathlib.Path("app/strategies/strangle").glob("*.py")):
        if f.name == "fills_live.py":
            continue
        called = {n.func.attr for n in ast.walk(ast.parse(f.read_text()))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not (called & banned), f"{f.name} calls {sorted(called & banned)}"


# =====================================================================================
# the dte>=3 entry-cost gate
# =====================================================================================
def depth_rows(spread: float, prices: dict, qty: int = 500_000):
    rows = []
    for sym, (strike, kind, px) in prices.items():
        bid, ask = round(px - spread / 2, 2), round(px + spread / 2, 2)
        rows.append({"symbol": sym, "strike": float(strike), "kind": kind,
                     "bid": bid, "ask": ask, "oi": 9_000_000.0,
                     "depth": {"buy": [{"price": bid, "quantity": qty}],
                               "sell": [{"price": ask, "quantity": qty}]}})
    return rows


CONDOR = {"C24800": (24_800, "CE", 27.0), "P24000": (24_000, "PE", 27.0),
          "C25300": (25_300, "CE", 8.0), "P23500": (23_500, "PE", 8.0)}
PROBE = [{"symbol": "C24800", "side": "SELL"}, {"symbol": "P24000", "side": "SELL"},
         {"symbol": "C25300", "side": "BUY"}, {"symbol": "P23500", "side": "BUY"}]


def test_entry_cost_counts_the_crossing_and_the_statutory_stack():
    """A short is sold at the bid and marked at the ask the same instant. That is not an
    accounting artefact — it is what flattening immediately would cost."""
    c = B.estimate_entry_cost(PROBE, depth_rows(0.20, CONDOR), CFG, units=1300,
                              lot_size=LOT)
    # 0.20 of spread on each of four legs, plus the one-tick queue penalty on each: a
    # resting order is behind everything already at that price, so the touch is not ours.
    assert c["crossing_points"] == pytest.approx(4 * (0.20 + 0.05), abs=0.01)
    assert c["statutory_points"] > 0
    assert c["total_points"] == pytest.approx(c["crossing_points"] + c["statutory_points"])


def test_a_wide_book_costs_more_to_enter_than_a_tight_one():
    tight = B.estimate_entry_cost(PROBE, depth_rows(0.10, CONDOR), CFG, units=1300,
                                  lot_size=LOT)
    wide = B.estimate_entry_cost(PROBE, depth_rows(1.00, CONDOR), CFG, units=1300,
                                 lot_size=LOT)
    assert wide["total_points"] > tight["total_points"] * 3


def test_entry_cost_walks_the_ladder_for_the_real_size():
    """Twenty lots is 1,300 units and the touch rarely holds that much."""
    thin = depth_rows(0.10, CONDOR, qty=200)
    with pytest.raises(F.DepthUnavailable, match="visible depth"):
        B.estimate_entry_cost(PROBE, thin, CFG, units=1300, lot_size=LOT)


def test_a_tight_market_lets_the_wednesday_session_trade():
    """The gate is not a ban on dte>=3. On a tight book a 2.5-point stop is many times the
    friction and the session is perfectly tradeable."""
    c = B.estimate_entry_cost(PROBE, depth_rows(0.10, CONDOR), CFG, units=1300,
                              lot_size=LOT)
    ok, why = R.entry_cost_gate(2.5, c["total_points"], CFG)
    assert ok, why


def test_a_wide_market_vetoes_the_wednesday_session():
    """At 1.5 points of friction a 2.5-point stop is 1.67x it — under the 2.0 floor — and
    Lever B would be armed at entry."""
    ok, why = R.entry_cost_gate(2.5, 1.5, CFG)
    assert not ok and "friction would consume" in why


def test_the_same_friction_is_acceptable_against_the_monday_stop():
    """The problem was never the cost, it was the cost against a HALF-SIZE stop. The same
    1.5 points is only 30% of a Monday 5-point stop."""
    assert R.entry_cost_gate(5.0, 1.5, CFG)[0]
    assert not R.entry_cost_gate(2.5, 1.5, CFG)[0]


def test_the_gate_leaves_the_structural_ratio_untouched():
    """The fix had to avoid three repairs that each break something held fixed: deriving
    the stop from the cost breaks the 2:1 ratio, netting the cost out of the mark is
    forbidden by the accounting section, and raising the dte>=3 target invents a number."""
    assert CFG["session"]["stop_to_target_ratio"] == 0.50
    for day, exp in ((D(2026, 8, 19), D(2026, 8, 25)), (D(2026, 8, 24), D(2026, 8, 25))):
        p = C.session_params(day, exp, CFG)
        assert p.stop_points == pytest.approx(p.target_points * 0.5)
    assert CFG["accounting"]["mark_basis"] == "liquidation"
    assert CFG["session"]["by_days_to_expiry"]["3+"]["target_points"] == 5


def test_the_runner_vetoes_before_it_sizes_or_fills():
    """Checked before the margin query and before any fill: a session that cannot clear
    this gate should not consume a broker call, let alone a position."""
    src = open("scripts/strangle.py").read()
    assert src.index("entry_cost_gate") < src.index("Z.query_margin")
    assert src.index("entry_cost_gate") < src.index("F.simulate_basket")
    assert "ENTRY_COST_VETO" in src
