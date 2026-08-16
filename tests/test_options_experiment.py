"""The intraday-versus-overnight A/B experiment.

Paper-only bookkeeping. These tests are mostly about what the module REFUSES to do,
because each refusal corresponds to a documented way a paper result fails to survive
contact with a live book.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from app.analytics import db
from app.strategies import options_experiment as X
from app.strategies.options_costs import Fill

LOT = 65
D = dt.date


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "p.db")) as c:
        db.migrate(c)
        yield c


@pytest.fixture()
def variant(conn):
    v = X.Variant(name="base", arm=X.OVERNIGHT,
                  spec={"short_delta": 0.16, "wing_delta": 0.05})
    return X.register(conn, v)


def entry_fills(short=30.0, wing=8.0, q=LOT):
    return [X.executable_fill("SELL", short - 0.05, short + 0.05, q, "short_ce"),
            X.executable_fill("SELL", short - 0.05, short + 0.05, q, "short_pe"),
            X.executable_fill("BUY", wing - 0.05, wing + 0.05, q, "wing_ce"),
            X.executable_fill("BUY", wing - 0.05, wing + 0.05, q, "wing_pe")]


def exit_quotes(short=20.0, wing=5.0):
    return {"short_ce": (short - 0.05, short + 0.05),
            "short_pe": (short - 0.05, short + 0.05),
            "wing_ce": (wing - 0.05, wing + 0.05),
            "wing_pe": (wing - 0.05, wing + 0.05)}


# =====================================================================================
# executable pricing — the refusal that matters most
# =====================================================================================
def test_a_sell_hits_the_bid_and_a_buy_lifts_the_ask():
    assert X.executable_fill("SELL", 10.0, 10.5, LOT).price == 10.0
    assert X.executable_fill("BUY", 10.0, 10.5, LOT).price == 10.5


def test_a_one_sided_or_missing_quote_is_refused_not_approximated():
    """A midpoint fill is the commonest way a paper result dies live."""
    for bid, ask in ((None, 10.0), (10.0, None), (0.0, 10.0), (10.5, 10.0)):
        with pytest.raises(X.ExperimentError):
            X.executable_fill("BUY", bid, ask, LOT, "wing")


def test_crossing_costs_the_spread_in_both_directions():
    sell = X.executable_fill("SELL", 10.0, 10.5, LOT)
    buy = X.executable_fill("BUY", 10.0, 10.5, LOT)
    assert buy.price - sell.price == pytest.approx(0.5)


def test_reversing_a_position_flips_every_side():
    rev = X.reverse(entry_fills(), exit_quotes())
    assert [f.side for f in rev] == ["BUY", "BUY", "SELL", "SELL"]


def test_a_leg_with_no_exit_quote_is_refused():
    """Three of four legs closed is not a closed position."""
    q = exit_quotes()
    q.pop("wing_pe")
    with pytest.raises(X.ExperimentError, match="wing_pe"):
        X.reverse(entry_fills(), q)


# =====================================================================================
# settlement
# =====================================================================================
def test_a_credit_that_decays_produces_a_gross_profit():
    e = entry_fills(short=30.0, wing=8.0)
    x = X.reverse(e, exit_quotes(short=20.0, wing=5.0))
    s = X.settle(e, x, lot_size=LOT)
    assert s.gross_pnl > 0


def test_costs_are_subtracted_and_can_exceed_the_gross():
    """A small credit that decays a little is a net loss. This is the whole finding."""
    e = entry_fills(short=30.0, wing=8.0)
    x = X.reverse(e, exit_quotes(short=29.5, wing=7.8))
    s = X.settle(e, x, lot_size=LOT)
    assert s.gross_pnl > 0 and s.net_pnl < 0


def test_return_on_margin_is_reported_against_the_margin_actually_posted():
    e = entry_fills()
    x = X.reverse(e, exit_quotes())
    s = X.settle(e, x, margin=50_000.0, lot_size=LOT)
    assert s.return_on_margin_pct == pytest.approx(s.net_pnl / 50_000 * 100)


def test_without_a_margin_figure_return_on_margin_is_none_not_zero():
    e = entry_fills()
    s = X.settle(e, X.reverse(e, exit_quotes()), lot_size=LOT)
    assert s.return_on_margin_pct is None


def test_settlement_needs_both_sides():
    with pytest.raises(X.ExperimentError):
        X.settle(entry_fills(), [], lot_size=LOT)


def test_an_unquoted_leg_makes_the_net_unavailable_rather_than_optimistic():
    e = entry_fills()
    x = X.reverse(e, exit_quotes())
    x[0] = Fill("BUY", 20.0, LOT, label="short_ce")      # no bid/ask
    s = X.settle(e, x, lot_size=LOT)
    assert s.cost_complete is False and s.net_pnl is None
    assert s.gross_pnl != 0                               # what is known is still reported


# =====================================================================================
# preregistration
# =====================================================================================
def test_an_unregistered_variant_cannot_open_an_arm(conn):
    """The trial count governs the deflated Sharpe, so it must be fixed before data."""
    with pytest.raises(X.ExperimentError, match="not registered"):
        X.open_arm(conn, variant_id="made:up:now", strategy="seller", arm=X.OVERNIGHT,
                   expiry=D(2026, 8, 25), lots=1, lot_size=LOT,
                   entry_fills=entry_fills(), entry_at=dt.datetime(2026, 8, 17, 15, 15),
                   entry_spot=24400.0)


def test_registering_the_same_spec_twice_is_idempotent(conn):
    v = X.Variant(name="base", arm=X.OVERNIGHT, spec={"wing_delta": 0.05})
    X.register(conn, v)
    X.register(conn, v)
    assert X.trials_registered(conn) == 1


def test_changing_any_parameter_creates_a_distinct_trial(conn):
    X.register(conn, X.Variant("base", X.OVERNIGHT, {"wing_delta": 0.05}))
    X.register(conn, X.Variant("base", X.OVERNIGHT, {"wing_delta": 0.08}))
    assert X.trials_registered(conn) == 2


def test_an_unknown_arm_is_refused():
    with pytest.raises(X.ExperimentError):
        X.Variant("x", "weekly", {})


# =====================================================================================
# lifecycle
# =====================================================================================
def test_an_opened_arm_is_visible_as_open_until_closed(conn, variant):
    aid = X.open_arm(conn, variant_id=variant, strategy="seller", arm=X.OVERNIGHT,
                     expiry=D(2026, 8, 25), lots=1, lot_size=LOT,
                     entry_fills=entry_fills(),
                     entry_at=dt.datetime(2026, 8, 17, 15, 15), entry_spot=24400.0)
    assert [a["arm_id"] for a in X.open_arms(conn)] == [aid]
    X.close_arm(conn, aid, exit_fills=X.reverse(entry_fills(), exit_quotes()),
                exit_at=dt.datetime(2026, 8, 18, 9, 20), exit_spot=24450.0,
                exit_reason="scheduled")
    assert X.open_arms(conn) == []
    assert len(X.closed_arms(conn)) == 1


def test_an_arm_cannot_be_closed_twice(conn, variant):
    aid = X.open_arm(conn, variant_id=variant, strategy="seller", arm=X.OVERNIGHT,
                     expiry=D(2026, 8, 25), lots=1, lot_size=LOT,
                     entry_fills=entry_fills(),
                     entry_at=dt.datetime(2026, 8, 17, 15, 15), entry_spot=24400.0)
    kw = dict(exit_fills=X.reverse(entry_fills(), exit_quotes()),
              exit_at=dt.datetime(2026, 8, 18, 9, 20), exit_spot=24450.0,
              exit_reason="scheduled")
    X.close_arm(conn, aid, **kw)
    with pytest.raises(X.ExperimentError, match="already closed"):
        X.close_arm(conn, aid, **kw)


# =====================================================================================
# the two exits are recorded separately
# =====================================================================================
def test_breach_and_mark_stop_are_tracked_independently(conn, variant):
    """So each exit's contribution can be measured afterwards without re-running."""
    aid = X.open_arm(conn, variant_id=variant, strategy="seller", arm=X.INTRADAY,
                     expiry=D(2026, 8, 25), lots=1, lot_size=LOT,
                     entry_fills=entry_fills(),
                     entry_at=dt.datetime(2026, 8, 17, 9, 45), entry_spot=24400.0)
    X.observe(conn, aid, breach=True, at=dt.datetime(2026, 8, 17, 11, 0))
    row = dict(conn.execute("SELECT * FROM option_arms WHERE arm_id=?", (aid,)).fetchone())
    assert row["breach_seen"] == 1 and row["mark_stop_seen"] is None


def test_only_the_first_occurrence_of_each_condition_is_kept(conn, variant):
    aid = X.open_arm(conn, variant_id=variant, strategy="seller", arm=X.INTRADAY,
                     expiry=D(2026, 8, 25), lots=1, lot_size=LOT,
                     entry_fills=entry_fills(),
                     entry_at=dt.datetime(2026, 8, 17, 9, 45), entry_spot=24400.0)
    X.observe(conn, aid, breach=True, at=dt.datetime(2026, 8, 17, 11, 0))
    X.observe(conn, aid, breach=True, at=dt.datetime(2026, 8, 17, 13, 0))
    row = conn.execute("SELECT breach_at FROM option_arms WHERE arm_id=?", (aid,)).fetchone()
    assert row["breach_at"][11:16] == "11:00", "a later touch must not overwrite the first"


# =====================================================================================
# night classification
# =====================================================================================
def test_a_monday_to_tuesday_hold_is_a_normal_night():
    assert X.classify_night(dt.datetime(2026, 8, 17, 15, 15),
                            dt.datetime(2026, 8, 18, 9, 20)) == X.NIGHT_NORMAL


def test_a_friday_to_monday_hold_is_a_weekend():
    assert X.classify_night(dt.datetime(2026, 8, 21, 15, 15),
                            dt.datetime(2026, 8, 24, 9, 20)) == X.NIGHT_WEEKEND


def test_a_declared_holiday_outranks_a_plain_night():
    assert X.classify_night(dt.datetime(2026, 8, 17, 15, 15),
                            dt.datetime(2026, 8, 19, 9, 20),
                            holidays=[D(2026, 8, 18)]) == X.NIGHT_HOLIDAY


def test_an_event_flag_outranks_everything():
    """The caller knows an RBI policy fell in the window; this module cannot infer it."""
    assert X.classify_night(dt.datetime(2026, 8, 21, 15, 15),
                            dt.datetime(2026, 8, 24, 9, 20),
                            event=True) == X.NIGHT_EVENT


def test_an_intraday_arm_gets_no_night_label(conn, variant):
    v2 = X.register(conn, X.Variant("base", X.INTRADAY, {"wing_delta": 0.05}))
    aid = X.open_arm(conn, variant_id=v2, strategy="seller", arm=X.INTRADAY,
                     expiry=D(2026, 8, 25), lots=1, lot_size=LOT,
                     entry_fills=entry_fills(),
                     entry_at=dt.datetime(2026, 8, 17, 9, 45), entry_spot=24400.0)
    out = X.close_arm(conn, aid, exit_fills=X.reverse(entry_fills(), exit_quotes()),
                      exit_at=dt.datetime(2026, 8, 17, 15, 12), exit_spot=24420.0,
                      exit_reason="square_off")
    assert out["night_type"] is None


# =====================================================================================
# reporting
# =====================================================================================
def test_the_comparison_reports_no_verdict_on_a_tiny_sample(conn, variant):
    v2 = X.register(conn, X.Variant("base", X.INTRADAY, {"wing_delta": 0.05}))
    for arm, vid, day in ((X.OVERNIGHT, variant, 17), (X.INTRADAY, v2, 18)):
        aid = X.open_arm(conn, variant_id=vid, strategy="seller", arm=arm,
                         expiry=D(2026, 8, 25), lots=1, lot_size=LOT,
                         entry_fills=entry_fills(),
                         entry_at=dt.datetime(2026, 8, day, 15, 15), entry_spot=24400.0)
        X.close_arm(conn, aid, exit_fills=X.reverse(entry_fills(), exit_quotes()),
                    exit_at=dt.datetime(2026, 8, day + 1, 9, 20), exit_spot=24450.0,
                    exit_reason="scheduled")
    rep = X.compare(conn)
    assert rep["closed_arms"] == 2
    assert rep["trials_registered"] == 2
    assert rep["difference"]["conclusive"] is False      # n=1 per arm


def test_a_cost_ratio_above_one_means_frictions_ate_everything(conn, variant):
    aid = X.open_arm(conn, variant_id=variant, strategy="seller", arm=X.OVERNIGHT,
                     expiry=D(2026, 8, 25), lots=1, lot_size=LOT,
                     entry_fills=entry_fills(),
                     entry_at=dt.datetime(2026, 8, 17, 15, 15), entry_spot=24400.0)
    X.close_arm(conn, aid, exit_fills=X.reverse(entry_fills(), exit_quotes(29.5, 7.8)),
                exit_at=dt.datetime(2026, 8, 18, 9, 20), exit_spot=24405.0,
                exit_reason="scheduled")
    s = X.summarise(X.closed_arms(conn))
    assert s["cost_over_gross"] > 1.0
    assert s["net_pnl_total"] < 0


def test_an_arm_with_an_incomplete_cost_is_excluded_from_the_summary(conn):
    assert X.summarise([])["n"] == 0


def test_the_sharpe_standard_error_shrinks_with_sample_size():
    assert X.sharpe_se(0.2, 100) > X.sharpe_se(0.2, 400)
    assert X.sharpe_se(0.2, 1) is None
