"""V4: the locks, the failover, the kill switch and the live order path.

The most important test in this file is the last one, which asserts that live trading is
still impossible today. Everything else describes machinery that must not run yet.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.strategies.strangle import book as B
from app.strategies.strangle import config as SC
from app.strategies.strangle import fills_live as FL
from app.strategies.strangle import journal as JN
from app.strategies.strangle import live as LV
from app.strategies.strangle import market as MK
from app.strategies.strangle import session as S

CFG = SC.load()
LOT, UNITS = 65, 1300
NOW = dt.datetime(2026, 8, 24, 11, 0)


class Cfg:
    def __init__(self, options=True, intraday=True):
        self.OPTIONS_ENABLED, self.INTRADAY_ENABLED = options, intraday


def journal_with(tmp_path, sessions):
    j = JN.Journal(str(tmp_path / "j.jsonl"))
    for s in sessions:
        j.write("session_closed", **s)
    return j


def ok_session(pnl=5000.0):
    return {"status": "OK", "flat": True, "pnl": pnl, "adjustments": 1}


# =====================================================================================
# the locks
# =====================================================================================
def test_every_lock_is_closed_today(tmp_path):
    """The state that actually matters. If this ever passes as 'cleared' by accident, the
    system has become able to trade live without anyone deciding that it should."""
    pf = LV.preflight(CFG, journal_with(tmp_path, []))
    assert not pf.cleared
    assert {"live.enabled", "typed_confirmation", "paper_sessions"} <= set(pf.closed)


def test_paper_sessions_are_counted_not_configured(tmp_path):
    j = journal_with(tmp_path, [ok_session() for _ in range(3)])
    assert len(LV.completed_paper_sessions(j)) == 3


def test_an_unclosed_session_is_not_experience(tmp_path):
    """A session that never reached a flat book proves nothing, and counting it would let
    sixty broken days look like sixty days of evidence."""
    j = journal_with(tmp_path, [
        {"status": "UNCLOSED", "flat": False, "pnl": None},
        {"status": "OK", "flat": True, "pnl": 100.0},
        {"status": "OK", "flat": True, "pnl": None},        # skipped: never opened
    ])
    assert len(LV.completed_paper_sessions(j)) == 1


def test_sixty_good_sessions_still_need_the_typed_phrase(tmp_path):
    j = journal_with(tmp_path, [ok_session() for _ in range(60)])
    live_on = {**CFG, "live": {**CFG["live"], "enabled": True}}
    pf = LV.preflight(live_on, j, app_config=Cfg(), secondary_authenticated=True)
    assert not pf.cleared and "typed_confirmation" in pf.closed
    cleared = LV.preflight(live_on, j, confirmation=LV.CONFIRMATION_PHRASE,
                           app_config=Cfg(), secondary_authenticated=True)
    assert cleared.cleared


def test_a_near_miss_phrase_does_not_count(tmp_path):
    j = journal_with(tmp_path, [ok_session() for _ in range(60)])
    live_on = {**CFG, "live": {**CFG["live"], "enabled": True}}
    for phrase in ("trade live money", "TRADE LIVE MONEY ", "yes", ""):
        pf = LV.preflight(live_on, j, confirmation=phrase, app_config=Cfg(),
                          secondary_authenticated=True)
        assert "typed_confirmation" in pf.closed, phrase


def test_the_product_gates_are_locks_in_their_own_right(tmp_path):
    """They are enforced inside the gateway too, so a caller that skipped preflight would
    still be refused at the layer that talks to Kite."""
    j = journal_with(tmp_path, [ok_session() for _ in range(60)])
    live_on = {**CFG, "live": {**CFG["live"], "enabled": True}}
    pf = LV.preflight(live_on, j, confirmation=LV.CONFIRMATION_PHRASE,
                      app_config=Cfg(options=False, intraday=False),
                      secondary_authenticated=True)
    assert {"options_enabled", "intraday_enabled"} <= set(pf.closed)


def test_a_losing_paper_record_blocks_live(tmp_path):
    j = journal_with(tmp_path, [ok_session(-2000.0) for _ in range(60)])
    live_on = {**CFG, "live": {**CFG["live"], "enabled": True}}
    pf = LV.preflight(live_on, j, confirmation=LV.CONFIRMATION_PHRASE, app_config=Cfg(),
                      secondary_authenticated=True)
    assert "expectancy_positive" in pf.closed


def test_expectancy_reports_its_own_noise(tmp_path):
    e = LV.observed_expectancy([ok_session(p) for p in (1000, -500, 2000, 300, -100)])
    assert e["n"] == 5 and e["standard_error"] is not None and e["t"] is not None
    assert e["win_rate_pct"] == 60.0


# =====================================================================================
# broker failover — rule R1
# =====================================================================================
class Broker:
    def __init__(self, alive=True):
        self.alive = alive

    def is_authed(self):
        if self.alive == "raise":
            raise RuntimeError("token revoked")
        return self.alive


def test_the_pool_is_not_ready_without_a_second_session():
    assert not LV.BrokerPool(Broker(), None).ready
    assert LV.BrokerPool(Broker(), Broker()).ready


def test_the_primary_token_being_revoked_mid_session_fails_over():
    """The V4 acceptance test. The source lost Rs 15 lakh to an outage and concluded the
    failure was his own for having no redundancy."""
    primary, secondary = Broker(), Broker()
    pool = LV.BrokerPool(primary, secondary)
    assert pool.current() is primary
    primary.alive = "raise"                       # token revoked mid-session
    assert pool.current(now=NOW) is secondary
    assert pool.active == "secondary" and len(pool.failovers) == 1


def test_losing_both_sessions_raises_rather_than_guessing():
    pool = LV.BrokerPool(Broker(alive=False), Broker(alive=False))
    with pytest.raises(LV.NoBrokerAvailable):
        pool.current()


def test_failover_does_not_flap_back_on_every_call():
    primary, secondary = Broker(), Broker()
    pool = LV.BrokerPool(primary, secondary)
    primary.alive = False
    pool.current(now=NOW)
    pool.current(now=NOW)
    assert len(pool.failovers) == 1


# =====================================================================================
# kill switch — rules R2 and R6
# =====================================================================================
def ks():
    return LV.KillSwitch.from_config(CFG)


def test_a_healthy_tick_does_not_trip():
    k = ks()
    k.beat(NOW)
    assert k.check(now=NOW, stale_seconds=0.5) == ""


def test_a_stale_feed_trips_immediately():
    k = ks()
    k.beat(NOW)
    assert "stale" in k.check(now=NOW, stale_seconds=30.0)


def test_a_missed_heartbeat_trips_after_the_configured_misses():
    k = ks()
    k.beat(NOW)
    assert k.check(now=NOW + dt.timedelta(seconds=15), stale_seconds=0.0) == ""
    assert "heartbeat" in k.check(now=NOW + dt.timedelta(seconds=45), stale_seconds=0.0)


def test_a_book_past_twice_its_stop_trips_the_switch():
    """R6: the stop did not hold, so the position is not what the book thinks it is."""
    bk = B.Book(units_at_entry=UNITS, stop_points=5.0, target_points=10.0)
    bk.add(B.Leg("C1", "CE", 24_800, "SELL", UNITS, 27.0, "short_call"))
    k = ks()
    k.beat(NOW)
    assert "past 2x the stop" in k.check(now=NOW, stale_seconds=0.0, book=bk,
                                         marks={"C1": 40.0})


def test_the_switch_stays_tripped_once_it_has_fired():
    k = ks()
    k.beat(NOW)
    k.check(now=NOW, stale_seconds=30.0)
    assert k.check(now=NOW, stale_seconds=0.0) != ""      # does not un-trip


def test_killing_the_feed_mid_position_flattens_the_book(tmp_path):
    """The other V4 acceptance test, driven through the real session loop."""
    import tests.test_strangle_session as TS
    frames = [TS.frame(0, 24_400), TS.frame(1, 24_400)]
    stale = MK.ChainSnapshot(as_of=frames[1].as_of, spot=24_400, expiry=frames[1].expiry,
                             rows=frames[1].rows, stale_seconds=120.0)
    bk = TS.hedged_book()
    events = []
    rec = S.run_session(book=bk, provider=iter([frames[0], stale]), cfg=CFG,
                        levels=TS.LEVELS, oi=TS.OI,
                        journal=JN.Journal(str(tmp_path / "j.jsonl")), lot_size=LOT,
                        step=50, kill_switch=ks(),
                        on_event=lambda k, p: events.append((k, p)))
    assert rec["exit_code"] == "KILL" and rec["flat"]
    assert [k for k, _ in events if k == "emergency_flatten"]


# =====================================================================================
# the live order path
# =====================================================================================
class FakeGateway:
    def __init__(self, results):
        self.results, self.calls = results, []

    async def place(self, **kw):
        self.calls.append(kw)
        return self.results[kw["symbol"]]


class FakeKC:
    def __init__(self, rows):
        self.rows, self.cancelled = rows, []

    def order_history(self, oid):
        return [self.rows[oid]] if oid in self.rows else []

    def cancel_order(self, variety, order_id):
        self.cancelled.append(order_id)


def order(symbol, side="SELL", qty=UNITS, price=27.0):
    return {"symbol": symbol, "side": side, "quantity": qty, "price": price}


def test_a_complete_basket_returns_fills(tmp_path):
    gw = FakeGateway({"A": {"status": "PLACED", "order_id": "1"},
                      "B": {"status": "PLACED", "order_id": "2"}})
    kc = FakeKC({"1": {"status": "COMPLETE", "filled_quantity": UNITS,
                       "average_price": 27.0},
                 "2": {"status": "COMPLETE", "filled_quantity": UNITS,
                       "average_price": 30.0}})
    fills = FL.execute_basket([order("A"), order("B")], CFG, gateway=gw, kc=kc,
                              client_prefix="s1", timeout_seconds=1, poll_seconds=0.01)
    assert all(f.complete for f in fills)
    assert [f.avg_price for f in fills] == [27.0, 30.0]


def test_every_leg_goes_out_as_MIS_never_a_carry_product():
    """NRML would be refused by the overnight-option guard anyway; sending it would be a
    bug that only the guard catches."""
    gw = FakeGateway({"A": {"status": "PLACED", "order_id": "1"}})
    kc = FakeKC({"1": {"status": "COMPLETE", "filled_quantity": UNITS,
                       "average_price": 27.0}})
    FL.execute_basket([order("A")], CFG, gateway=gw, kc=kc, client_prefix="s1",
                      timeout_seconds=1, poll_seconds=0.01)
    assert gw.calls[0]["product"] == "MIS"
    assert gw.calls[0]["client_id"] == "s1:A:SELL"


def test_a_rejected_leg_makes_the_whole_basket_an_incident():
    """A condor missing a leg is not a condor. The caller must flatten what filled, not
    retry — retrying after a partial fill is how one leg becomes three."""
    gw = FakeGateway({"A": {"status": "PLACED", "order_id": "1"},
                      "B": {"status": "ERROR", "error": "insufficient margin"}})
    kc = FakeKC({"1": {"status": "COMPLETE", "filled_quantity": UNITS,
                       "average_price": 27.0}})
    with pytest.raises(FL.LiveExecutionError, match="do not retry"):
        FL.execute_basket([order("A"), order("B")], CFG, gateway=gw, kc=kc,
                          client_prefix="s1", timeout_seconds=1, poll_seconds=0.01)


def test_a_partial_fill_is_reported_as_incomplete():
    gw = FakeGateway({"A": {"status": "PLACED", "order_id": "1"}})
    kc = FakeKC({"1": {"status": "COMPLETE", "filled_quantity": UNITS // 2,
                       "average_price": 27.0}})
    with pytest.raises(FL.LiveExecutionError, match="650/1300"):
        FL.execute_basket([order("A")], CFG, gateway=gw, kc=kc, client_prefix="s1",
                          timeout_seconds=1, poll_seconds=0.01)


def test_an_order_still_open_at_the_timeout_is_cancelled_not_left_working():
    """An order you have stopped watching is an unbounded position."""
    gw = FakeGateway({"A": {"status": "PLACED", "order_id": "1"}})
    kc = FakeKC({"1": {"status": "OPEN", "filled_quantity": 0}})
    with pytest.raises(FL.LiveExecutionError, match="TIMEOUT"):
        FL.execute_basket([order("A")], CFG, gateway=gw, kc=kc, client_prefix="s1",
                          timeout_seconds=0.05, poll_seconds=0.01)
    assert kc.cancelled == ["1"]


def test_a_dry_run_result_is_not_mistaken_for_a_fill():
    gw = FakeGateway({"A": {"status": "DRY_RUN", "order_id": "DRY-1"}})
    with pytest.raises(FL.LiveExecutionError, match="DRY_RUN"):
        FL.execute_basket([order("A")], CFG, gateway=gw, kc=FakeKC({}),
                          client_prefix="s1", timeout_seconds=0.05, poll_seconds=0.01)


# =====================================================================================
# the order path is still the gateway, and live is still impossible
# =====================================================================================
def test_only_the_live_adapter_may_reach_kite_and_only_to_cancel():
    """Order SUBMISSION goes through the gateway in every module. The one direct broker
    call is a cancellation, which can only ever reduce exposure."""
    import ast
    import pathlib
    # CALL SITES, not names. fills_live's docstring lists the API surface it cannot use,
    # and a substring test on that prose fails while the code is entirely correct — the
    # same trap that once made a test pass on a comment explaining the defect.
    for f in sorted(pathlib.Path("app/strategies/strangle").glob("*.py")):
        tree = ast.parse(f.read_text())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "place_order" not in called, f.name
        assert "place_gtt_stop" not in called and "place_gtt" not in called, f.name
        assert "modify_order" not in called, f.name
        if "cancel_order" in called:
            assert f.name == "fills_live.py", f"{f.name} must not cancel orders"


def test_submission_goes_through_the_gateway():
    src = open("app/strategies/strangle/fills_live.py").read()
    assert "gateway.place(" in src


def test_live_trading_is_impossible_on_this_machine_today(tmp_path):
    """The state of the world, asserted. Five locks, all closed, none of them a comment."""
    from app import config as app_cfg
    pf = LV.preflight(CFG, journal_with(tmp_path, []))
    assert not pf.cleared
    assert CFG["live"]["enabled"] is False
    assert app_cfg.OPTIONS_ENABLED is False
    assert app_cfg.INTRADAY_ENABLED is False
    assert len(pf.closed) >= 4
