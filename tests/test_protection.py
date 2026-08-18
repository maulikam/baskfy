"""Stop coverage: is every position actually protected, checked against the broker.

The rules say every buy gets a GTT stop and the execution path places one. Nothing ever
verified it afterwards — and on the live account that turned out to mean 17 positions
worth Rs 63L with no stop at all.
"""
from __future__ import annotations

import pytest

from app import config as C
from app.analytics import protection as P


def hold(symbol, qty=100, px=100.0):
    return {"symbol": symbol, "quantity": qty, "last_price": px, "pledged_qty": 0}


def gtt(symbol, trigger, qty=100, status="active"):
    return {"status": status,
            "condition": {"tradingsymbol": symbol, "trigger_values": [trigger]},
            "orders": [{"quantity": qty}]}


# =====================================================================================
# the finding that matters
# =====================================================================================
def test_a_position_with_no_trigger_is_reported_missing():
    rev = P.review([hold("AAA")], [])
    assert [f["kind"] for f in rev["findings"]] == [P.MISSING]
    assert rev["healthy"] is False


def test_a_covered_position_is_healthy():
    rev = P.review([hold("AAA", 100, 100.0)], [gtt("AAA", 90.0, 100)])
    assert rev["findings"] == [] and rev["healthy"] is True
    assert rev["coverage_pct"] == 100.0


def test_a_stop_for_fewer_shares_than_held_is_partial():
    """Buy more after arming a stop and the newer shares are uncovered."""
    rev = P.review([hold("AAA", 200, 100.0)], [gtt("AAA", 90.0, 100)])
    f = next(f for f in rev["findings"] if f["kind"] == P.PARTIAL)
    assert f["covered"] == 100
    assert f["value"] == 10_000          # only the uncovered 100 shares
    assert rev["healthy"] is False


def test_an_inactive_trigger_does_not_count_as_protection():
    for status in ("cancelled", "expired", "rejected", "triggered", "deleted"):
        rev = P.review([hold("AAA")], [gtt("AAA", 90.0, status=status)])
        assert rev["findings"][0]["kind"] == P.MISSING, status


def test_a_trigger_with_no_holding_is_an_orphan():
    """It can still fire against a position that is gone."""
    rev = P.review([], [gtt("ZZZ", 90.0)])
    assert [f["kind"] for f in rev["findings"]] == [P.ORPHAN]


# =====================================================================================
# the band
# =====================================================================================
def test_a_stop_further_than_the_band_is_flagged():
    rev = P.review([hold("AAA", 100, 100.0)],
                   [gtt("AAA", 100.0 * (1 - C.STOP_MAX - 0.05), 100)])
    assert any(f["kind"] == P.TOO_FAR for f in rev["findings"])


def test_a_stop_tighter_than_the_band_is_flagged():
    rev = P.review([hold("AAA", 100, 100.0)],
                   [gtt("AAA", 100.0 * (1 - C.STOP_MIN + 0.02), 100)])
    assert any(f["kind"] == P.TOO_CLOSE for f in rev["findings"])


def test_a_stop_inside_the_band_is_not_flagged():
    mid = (C.STOP_MIN + C.STOP_MAX) / 2
    rev = P.review([hold("AAA", 100, 100.0)], [gtt("AAA", 100.0 * (1 - mid), 100)])
    assert rev["findings"] == []


def test_the_binding_stop_is_the_highest_one():
    """Two triggers on one symbol: the higher fires first, so it is the real stop."""
    rev = P.review([hold("AAA", 100, 100.0)],
                   [gtt("AAA", 50.0, 50), gtt("AAA", 92.0, 50)])
    assert not any(f["kind"] == P.TOO_FAR for f in rev["findings"])


# =====================================================================================
# untouchable instruments
# =====================================================================================
def test_an_untouchable_holding_is_not_called_unprotected():
    """It is never traded, so a missing stop on it is correct, not a gap."""
    rev = P.review([hold("SGBDE31III-GB", 392, 15306.0), hold("AAA")], [])
    assert "SGBDE31III-GB" in rev["excluded"]
    assert [f["symbol"] for f in rev["findings"]] == ["AAA"]


def test_untouchable_value_is_left_out_of_the_totals():
    rev = P.review([hold("SGBDE31III-GB", 392, 15306.0)], [])
    assert rev["tradeable_value"] == 0 and rev["findings"] == []


# =====================================================================================
# totals must be trustworthy — this panel exists to be believed
# =====================================================================================
def test_unprotected_never_exceeds_tradeable():
    """Rounding each finding before summing made the unprotected total larger than the
    book it is a subset of."""
    holds = [hold(f"S{i}", 7, 1_449.93) for i in range(17)]
    rev = P.review(holds, [])
    assert rev["unprotected_value"] <= rev["tradeable_value"]


def test_coverage_is_never_negative_zero():
    rev = P.review([hold("AAA", 7, 1_449.93)], [])
    assert rev["coverage_pct"] == 0.0


def test_coverage_with_nothing_held_is_none_not_zero():
    """Zero would read as a measurement of total failure."""
    assert P.review([], [])["coverage_pct"] is None


def test_findings_are_ordered_worst_first_then_by_value():
    rev = P.review([hold("SMALL", 1, 100.0), hold("BIG", 100, 100.0)], [])
    assert [f["symbol"] for f in rev["findings"]] == ["BIG", "SMALL"]


# =====================================================================================
# it must never act
# =====================================================================================
def test_the_module_never_places_or_cancels_a_stop():
    src = open("app/analytics/protection.py").read()
    for forbidden in ("place_gtt", "delete_gtt", "modify_gtt", "place_order"):
        assert forbidden not in src, forbidden


def test_the_summary_line_states_the_money_at_risk():
    rev = P.review([hold("AAA", 100, 1000.0)], [])
    assert "100,000" in P.summary_line(rev)


def test_the_summary_line_is_positive_when_everything_is_covered():
    rev = P.review([hold("AAA", 100, 100.0)], [gtt("AAA", 90.0, 100)])
    assert "carry a stop" in P.summary_line(rev)


# =====================================================================================
# the stop plan — proposing is not arming
# =====================================================================================
def test_a_plan_proposes_one_stop_per_uncovered_position():
    plan = P.build_stop_plan([hold("AAA", 100, 100.0), hold("BBB", 50, 200.0)], [])
    assert plan["count"] == 2
    assert {r["symbol"] for r in plan["rows"]} == {"AAA", "BBB"}


def test_a_covered_position_is_not_proposed_for():
    plan = P.build_stop_plan([hold("AAA", 100, 100.0)], [gtt("AAA", 90.0, 100)])
    assert plan["count"] == 0


def test_a_partial_position_is_proposed_for_the_uncovered_shares_only():
    """Arming must not double up on quantity already protected."""
    plan = P.build_stop_plan([hold("AAA", 200, 100.0)], [gtt("AAA", 90.0, 100)])
    assert [r["qty"] for r in plan["rows"]] == [100]


def test_a_stop_outside_the_band_is_left_alone():
    """Replacing it means cancelling a live trigger — a different, riskier action."""
    plan = P.build_stop_plan([hold("AAA", 100, 100.0)], [gtt("AAA", 10.0, 100)])
    assert plan["count"] == 0


def test_an_untouchable_position_is_never_proposed_for():
    plan = P.build_stop_plan([hold("SGBDE31III-GB", 392, 15306.0)], [])
    assert plan["count"] == 0


def test_the_trigger_sits_inside_the_configured_band():
    plan = P.build_stop_plan([hold("AAA", 100, 1000.0)], [])
    drop = plan["rows"][0]["drop_pct"] / 100
    assert C.STOP_MIN - 1e-9 <= drop <= C.STOP_MAX + 1e-9


def test_a_higher_volatility_gives_a_wider_stop():
    tight = P.build_stop_plan([hold("AAA", 100, 1000.0)], [],
                              vol_by_symbol={"AAA": 0.10})["rows"][0]
    wide = P.build_stop_plan([hold("AAA", 100, 1000.0)], [],
                             vol_by_symbol={"AAA": 0.90})["rows"][0]
    assert wide["drop_pct"] > tight["drop_pct"]
    assert tight["vol_source"] == "scan"


def test_a_missing_volatility_is_labelled_not_hidden():
    plan = P.build_stop_plan([hold("AAA", 100, 100.0)], [])
    assert plan["rows"][0]["vol_source"] == "default"
    assert plan["using_default_vol"] == ["AAA"]


def test_at_risk_is_the_distance_from_price_to_trigger():
    r = P.build_stop_plan([hold("AAA", 100, 1000.0)], [])["rows"][0]
    assert r["at_risk"] == pytest.approx((1000.0 - r["trigger"]) * 100, abs=1)


def test_rows_are_ordered_by_value_at_stake():
    plan = P.build_stop_plan([hold("SMALL", 1, 100.0), hold("BIG", 500, 100.0)], [])
    assert [r["symbol"] for r in plan["rows"]] == ["BIG", "SMALL"]


def test_every_plan_gets_its_own_id():
    a = P.build_stop_plan([hold("AAA")], [])["plan_id"]
    b = P.build_stop_plan([hold("AAA")], [])["plan_id"]
    assert a != b


def test_a_zero_price_position_is_skipped_rather_than_priced_at_zero():
    assert P.build_stop_plan([hold("AAA", 100, 0.0)], [])["count"] == 0


# =====================================================================================
# EXCESS — the short-delivery risk, added 18 Aug 2026
# =====================================================================================
def gtt_for(sym, qty, trigger=90.0, gid=1):
    return {"id": gid, "status": "active",
            "condition": {"tradingsymbol": sym, "trigger_values": [trigger]},
            "orders": [{"tradingsymbol": sym, "quantity": qty,
                        "transaction_type": "SELL"}]}


def test_a_stop_covering_more_than_is_held_is_flagged():
    """The failure this was added for: stops armed from PLANNED quantities before the
    orders filled left 10,383 shares of GTT against 9,478 held. Firing sells shares you do
    not own, which is short delivery and an auction penalty."""
    from app.analytics import protection as P
    rev = P.review([{"symbol": "SONACOMS", "quantity": 342, "last_price": 800.0}],
                   [gtt_for("SONACOMS", 861)])
    kinds = {f["kind"] for f in rev["findings"]}
    assert P.EXCESS in kinds
    ex = next(f for f in rev["findings"] if f["kind"] == P.EXCESS)
    assert ex["covered"] == 861 and ex["qty"] == 342
    assert "519 you do not own" in ex["detail"]


def test_excess_outranks_everything_but_a_missing_stop():
    """An uncovered position loses money on a fall; an over-covered one sells shares that
    do not exist. Only having no stop at all is worse."""
    from app.analytics import protection as P
    assert P.SEVERITY[P.MISSING] < P.SEVERITY[P.EXCESS] < P.SEVERITY[P.PARTIAL]


def test_an_exact_cover_is_not_flagged():
    from app.analytics import protection as P
    rev = P.review([{"symbol": "TITAN", "quantity": 100, "last_price": 3000.0}],
                   [gtt_for("TITAN", 100, trigger=2700.0)])
    assert not [f for f in rev["findings"] if f["kind"] == P.EXCESS]


def test_an_excess_position_is_cancelled_then_re_armed_for_what_is_held():
    """Adding another trigger cannot fix over-coverage, so the plan cancels every trigger
    on the symbol and arms ONE for the quantity actually held."""
    from app.analytics import protection as P
    plan = P.build_stop_plan(
        [{"symbol": "SONACOMS", "quantity": 342, "last_price": 800.0}],
        [gtt_for("SONACOMS", 861, gid=111), gtt_for("SONACOMS", 400, gid=222)])
    assert {c["gtt_id"] for c in plan["cancels"]} == {111, 222}
    row = next(r for r in plan["rows"] if r["symbol"] == "SONACOMS")
    assert row["qty"] == 342, "re-arm must cover the held quantity, not a remainder"


def test_an_orphan_trigger_is_cancelled():
    from app.analytics import protection as P
    plan = P.build_stop_plan([], [gtt_for("PARAS", 438, gid=999)])
    assert [c["gtt_id"] for c in plan["cancels"]] == [999]
    assert plan["rows"] == []


def test_a_partial_cover_still_only_tops_up_the_uncovered_shares():
    """Unchanged behaviour: arming for the full quantity would double up on shares already
    protected, which is how an under-cover becomes an over-cover."""
    from app.analytics import protection as P
    plan = P.build_stop_plan(
        [{"symbol": "TITAN", "quantity": 100, "last_price": 3000.0}],
        [gtt_for("TITAN", 40, trigger=2700.0)])
    assert next(r for r in plan["rows"] if r["symbol"] == "TITAN")["qty"] == 60
    assert plan["cancels"] == []


def test_protection_still_cannot_place_or_cancel_anything():
    """It proposes; main.py acts. The module stays read-only so a review can never have a
    side effect."""
    src = open("app/analytics/protection.py").read()
    for token in ("place_gtt", "delete_gtt", "modify_gtt", "place_order"):
        assert token not in src, token
