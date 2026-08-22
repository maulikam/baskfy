"""POST /execute must route every order through core/gateway.py.

Before this, /execute called kite_client directly: the SGB guard still fired, but the
risk manager, kill switch, rate limiter and order journal were all bypassed on the one
path that spends real money.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import time

import pytest

from ._source import src_of
from fastapi.testclient import TestClient

from app import config as C
from app import main as M
from app.core.risk import RiskConfig, RiskManager


class FakeKC:
    """Stands in for KiteConnect. Records what would have been sent."""
    VARIETY_REGULAR = "regular"
    TRANSACTION_TYPE_BUY, TRANSACTION_TYPE_SELL = "BUY", "SELL"
    PRODUCT_CNC, ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET = "CNC", "LIMIT", "MARKET"
    VALIDITY_DAY, GTT_TYPE_SINGLE = "DAY", "single"

    def __init__(self):
        self.orders, self.gtts = [], []

    def place_order(self, **kw):
        self.orders.append(kw)
        return f"ORD{len(self.orders):04d}"

    def place_gtt(self, **kw):
        self.gtts.append(kw)
        return {"trigger_id": len(self.gtts)}


class FakeKite:
    def __init__(self):
        self.kc = FakeKC()

    def is_authed(self):
        return True

    def place_gtt_stop(self, symbol, qty, trigger, last_price, exchange="NSE"):
        from app.core.guards import assert_tradeable
        assert_tradeable(symbol)
        if C.DRY_RUN:
            return {"symbol": symbol, "status": "DRY_RUN_GTT", "trigger": trigger}
        return {"symbol": symbol, "status": "GTT_PLACED", "gtt_id": 1}


def order(symbol, delta, price=100.0, qty_final=10, stop=90.0, action="TRIM"):
    return {"symbol": symbol, "action": action, "qty_now": qty_final + abs(delta),
            "delta": delta, "qty_final": qty_final, "ref_price": price,
            "weight": 5.0, "value": qty_final * price, "stop": stop, "pledged": 0,
            "rank": 1, "score": 80.0}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    fake = FakeKite()
    monkeypatch.setattr(M, "_kite", fake)
    monkeypatch.setattr(M, "_gateway", None)
    monkeypatch.setattr(M, "_risk", None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "outputs").mkdir(parents=True)
    return TestClient(M.app), fake


def seed(plan_id="p1", orders=None, book_value=6_000_000.0):
    M.PLANS[plan_id] = {"plan_id": plan_id, "created_at": time.time(),
                        "book_value": book_value, "capital": 10_000_000.0,
                        "orders": orders or [order("DIXON", -10)]}
    return plan_id


def post(client, plan_id, stops="false"):
    return client.post("/execute", data={"plan_id": plan_id, "confirm": "true",
                                         "place_stops": stops})


# =====================================================================================
# the route
# =====================================================================================
def test_orders_go_through_the_gateway_and_are_journalled(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", True)
    pid = seed(orders=[order("DIXON", -10), order("POLYCAB", 5, action="ADD")])
    r = post(c, pid)
    assert r.status_code == 200
    body = r.json()
    assert [o["status"] for o in body["orders"]] == ["DRY_RUN", "DRY_RUN"]
    # DRY_RUN means the gateway simulated: nothing reached the broker
    assert fake.kc.orders == []
    # ...but the gateway still journalled the intent
    assert M.gateway()._sent, "gateway did not record the orders"


def test_live_mode_reaches_the_broker_through_the_gateway(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", False)
    pid = seed(orders=[order("DIXON", -10)])
    body = post(c, pid).json()
    assert body["orders"][0]["status"] == "PLACED"
    assert len(fake.kc.orders) == 1
    sent = fake.kc.orders[0]
    assert sent["tradingsymbol"] == "DIXON" and sent["product"] == "CNC"
    assert sent["transaction_type"] == "SELL" and sent["quantity"] == 10


def test_sells_are_submitted_before_buys(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", False)
    pid = seed(orders=[order("BUYME", 20, action="BUY"), order("SELLME", -30)])
    post(c, pid)
    assert [o["transaction_type"] for o in fake.kc.orders] == ["SELL", "BUY"]


# =====================================================================================
# layers that were previously bypassed
# =====================================================================================
def test_risk_manager_now_blocks_an_oversized_order(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", False)
    M._risk = RiskManager(RiskConfig(max_position_value=100_000.0))
    from app.core.gateway import OrderGateway
    M._gateway = OrderGateway(fake.kc, M._risk)

    pid = seed(orders=[order("DIXON", -100, price=5_000.0)])   # Rs 5,00,000
    body = post(c, pid).json()
    assert body["orders"][0]["status"] == "RISK_BLOCKED"
    assert fake.kc.orders == [], "a risk-blocked order must never reach the broker"


def test_kill_switch_stops_the_batch(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", False)
    M._risk = RiskManager()
    M._risk.kill("manual halt")
    from app.core.gateway import OrderGateway
    M._gateway = OrderGateway(fake.kc, M._risk)

    body = post(c, seed(orders=[order("DIXON", -10)])).json()
    assert body["orders"][0]["status"] == "RISK_BLOCKED"
    assert "KILL SWITCH" in body["orders"][0]["error"]
    assert fake.kc.orders == []


def test_reposting_the_same_plan_does_not_double_send(client, monkeypatch):
    """Idempotency keys off plan_id + symbol, so a double-submit is a no-op."""
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", False)
    pid = seed(orders=[order("DIXON", -10)])
    first = post(c, pid).json()
    second = post(c, pid).json()
    assert first["orders"][0]["status"] == "PLACED"
    assert second["orders"][0]["status"] == "DUPLICATE"
    assert len(fake.kc.orders) == 1, "the broker received the order twice"


# =====================================================================================
# an untouchable instrument must not abort a partially executed batch
# =====================================================================================
def test_protected_instrument_is_blocked_without_aborting_the_batch(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", False)
    pid = seed(orders=[order("SGBDE31III-GB", -5), order("DIXON", -10),
                       order("POLYCAB", 7, action="ADD")])
    body = post(c, pid).json()
    by_symbol = {o["symbol"]: o for o in body["orders"]}

    assert by_symbol["SGBDE31III-GB"]["status"] == "BLOCKED"
    assert "protected instrument" in by_symbol["SGBDE31III-GB"]["error"]
    # the rest still executed — the batch was not left half-done by an exception
    assert by_symbol["DIXON"]["status"] == "PLACED"
    assert by_symbol["POLYCAB"]["status"] == "PLACED"
    assert {o["tradingsymbol"] for o in fake.kc.orders} == {"DIXON", "POLYCAB"}


def test_execute_does_not_arm_stops_any_more(client, monkeypatch):
    """CHANGED DELIBERATELY on 18 Aug 2026. This route used to arm a GTT per order for its
    qty_final, immediately after submitting the batch and therefore before any fill was
    known. Every trigger was sized to the PLANNED position: the book ended the day carrying
    10,383 shares of GTT against 9,478 held, with a 438-share stop on a PARAS position that
    had not filled a single share.

    An over-covered trigger sells shares you do not own when it fires. Arming moved to
    /stops, which sizes from the broker's holdings and can cancel wrong triggers too.
    """
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", True)
    pid = seed(orders=[order("DIXON", -10, qty_final=10, stop=90.0)])
    body = post(c, pid, stops="true").json()
    assert body["gtt"] == []
    assert body["stops_pending"] is True
    assert "fills" in body["stops_note"]


def test_asking_for_stops_no_longer_places_them(client, monkeypatch):
    """place_stops=true is honoured as 'yes, protect this' — by telling you where, not by
    arming the wrong quantity. A silent no-op would be worse than either."""
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", False)
    pid = seed(orders=[order("DIXON", -10, qty_final=10, stop=90.0)])
    body = post(c, pid, stops="true").json()
    assert body["gtt"] == [] and body["stops_pending"] is True


# =====================================================================================
# confirmation gates are untouched
# =====================================================================================
def test_confirm_gate_still_holds(client):
    c, _ = client
    pid = seed()
    r = c.post("/execute", data={"plan_id": pid, "confirm": "false"})
    assert r.status_code == 400


def test_unknown_plan_is_rejected(client):
    c, _ = client
    assert c.post("/execute", data={"plan_id": "nope", "confirm": "true"}).status_code == 404


def test_stale_plan_is_rejected(client):
    c, _ = client
    M.PLANS["old"] = {"plan_id": "old", "created_at": time.time() - 3600,
                      "book_value": 0.0, "orders": []}
    assert c.post("/execute", data={"plan_id": "old", "confirm": "true"}).status_code == 410


# =====================================================================================
# source-level guarantees
# =====================================================================================
def test_execute_no_longer_calls_the_broker_directly():
    src = open("app/main.py").read()
    assert "k.place_cnc_order(" not in src, "an order still bypasses the gateway"
    assert "await gw.place(" in src


def test_blocking_sleep_removed_from_the_async_endpoint():
    """time.sleep in an async handler stalls the whole event loop."""
    src = open("app/main.py").read()
    assert "time.sleep(" not in src


def test_gateway_layer_order_is_unchanged():
    from baskfy_execution import gateway as _gw

    src = src_of(_gw)
    marks = ["layer 1: untouchables", "layer 2: risk", "layer 3: idempotency",
             "layer 4: rate limits"]
    positions = [src.index(m) for m in marks]
    assert positions == sorted(positions)


# =====================================================================================
# risk limits must not contradict the strategy they are protecting
# =====================================================================================
def test_derived_position_cap_permits_the_largest_legal_position():
    """MAX_SINGLE_WEIGHT of NAV is a position the strategy is configured to take.
    A risk cap below it forbids the intended, which is not what a risk cap is for."""
    nav = 10_531_889.0
    cfg = C.risk_config(nav)
    legal_max = nav * C.MAX_SINGLE_WEIGHT / 100.0
    assert cfg.max_position_value >= legal_max
    assert C.risk_coherence(cfg, nav) == []


def test_the_old_hardcoded_cap_is_detected_as_incoherent():
    from app.core.risk import RiskConfig
    problems = C.risk_coherence(RiskConfig(), 10_531_889.0)   # the shipped defaults
    assert any("max_position_value" in p for p in problems)


def test_gross_cap_below_nav_is_flagged():
    from app.core.risk import RiskConfig
    problems = C.risk_coherence(RiskConfig(max_gross_exposure=1_000.0), 10_000_000.0)
    assert any("every order would be refused" in p for p in problems)


def test_order_cap_too_small_for_a_rebalance_is_flagged():
    from app.core.risk import RiskConfig
    problems = C.risk_coherence(RiskConfig(max_orders_per_day=5), 10_000_000.0)
    assert any("rebalance" in p for p in problems)


def test_explicit_env_override_wins_over_derivation(monkeypatch):
    monkeypatch.setattr(C, "RISK_MAX_POSITION_VALUE", "2500000")
    assert C.risk_config(10_000_000.0).max_position_value == 2_500_000.0


def test_limits_scale_with_the_book():
    small, large = C.risk_config(1_000_000.0), C.risk_config(50_000_000.0)
    assert large.max_position_value > small.max_position_value
    assert large.max_daily_loss > small.max_daily_loss


def test_no_nav_falls_back_to_static_defaults():
    cfg = C.risk_config(0.0)
    assert cfg.max_position_value == 1_500_000.0      # the documented fallback


def test_daily_loss_cap_is_no_longer_inert(client, monkeypatch, tmp_path):
    """on_pnl() existed but nothing called it, so day_pnl stayed 0 forever."""
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", False)

    from app.analytics import db
    dbpath = str(tmp_path / "p.db")
    monkeypatch.setattr(C, "DB_PATH", dbpath)
    with db.connect(dbpath) as conn:
        db.migrate(conn)
        db.save_snapshot(conn, {"date": "2026-08-13", "nav": 10_000_000.0,
                                "invested": 6_000_000.0, "cash": 4_000_000.0,
                                "holdings_json": "{}"})

    M._gateway = None
    M._risk = None
    # book_value collapsed from 60L to 40L -> a Rs 20L intraday loss
    pid = seed(orders=[order("DIXON", -10)], book_value=4_000_000.0)
    body = post(c, pid).json()

    assert M._risk.state.day_pnl == pytest.approx(-2_000_000.0)
    assert M._risk.state.killed is True
    assert body["orders"][0]["status"] == "RISK_BLOCKED"
    assert "KILL SWITCH" in body["orders"][0]["error"]
    assert fake.kc.orders == [], "the kill switch must stop the batch reaching the broker"


# =====================================================================================
# the circuit breaker
# =====================================================================================
def test_a_systemic_refusal_stops_the_batch(client, monkeypatch):
    """On 18 Aug 2026 all twenty-one orders were fired into the same rejection — "No IPs
    configured for this app" — because nothing noticed the first three had failed
    identically. Each one still consumed a rate-limit slot and a journal line."""
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", False)

    def always_refuse(**kw):
        raise Exception("No IPs configured for this app. Add allowed IPs on the console.")
    fake.kc.place_order = always_refuse

    pid = seed(orders=[order(f"S{i}", 10, qty_final=10, stop=90.0) for i in range(10)])
    body = post(c, pid).json()
    attempted = [o for o in body["orders"] if o["status"] != "ABORTED"]
    aborted = [o for o in body["orders"] if o["status"] == "ABORTED"]
    assert len(attempted) == 3, f"tried {len(attempted)} before stopping"
    assert len(aborted) == 7
    assert "No IPs configured" in body["aborted_for"]


def test_different_failures_do_not_trip_the_breaker(client, monkeypatch):
    """Distinct rejections are per-order facts, not a systemic fault. Stopping on them
    would abandon a book because two unrelated symbols were illiquid."""
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", False)
    seen = {"n": 0}

    def varied(**kw):
        seen["n"] += 1
        raise Exception(f"reason number {seen['n']}")
    fake.kc.place_order = varied

    pid = seed(orders=[order(f"S{i}", 10, qty_final=10, stop=90.0) for i in range(6)])
    body = post(c, pid).json()
    assert not [o for o in body["orders"] if o["status"] == "ABORTED"]
    assert body["aborted_for"] == ""


def test_a_success_between_failures_resets_the_counter(client, monkeypatch):
    """Two failures, a fill, then two more is not a systemic refusal."""
    c, fake = client
    monkeypatch.setattr(C, "DRY_RUN", False)
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 3:
            return "OK123"
        raise Exception("same message every time")
    fake.kc.place_order = flaky

    pid = seed(orders=[order(f"S{i}", 10, qty_final=10, stop=90.0) for i in range(5)])
    body = post(c, pid).json()
    assert len([o for o in body["orders"] if o["status"] == "ABORTED"]) == 0


# =====================================================================================
# the circuit breaker's failure set, against the gateway's actual vocabulary
# =====================================================================================
def _returned_status_error_pairs() -> set[tuple[str, bool]]:
    """Every (status, carries_an_error) the gateway returns, read from its own source.

    A hand-written set is how the report once counted 16 live GTT triggers as 0 armed.
    The breaker had the same shape and the same defect.
    """
    import ast
    from baskfy_execution import gateway as _gw

    tree = ast.parse(src_of(_gw))
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if "status" not in keys:
            continue
        status = next((v.value for k, v in zip(node.keys, node.values)
                       if isinstance(k, ast.Constant) and k.value == "status"
                       and isinstance(v, ast.Constant)), None)
        if status:
            out.add((status, "error" in keys))
    return out


def test_every_gateway_refusal_that_gives_a_reason_is_counted_by_the_breaker():
    """RISK_BLOCKED was missing. A tripped daily-loss cap or an exposure limit refuses
    every order with the SAME reason, which is precisely the systemic case the breaker
    exists for — and it would have sent all twenty-one anyway, one rate-limit slot and one
    journal line at a time."""
    from app.core.gateway import FAILED_STATUSES
    with_reason = {s for s, has_err in _returned_status_error_pairs() if has_err}
    assert with_reason, "gateway no longer returns a recognisable refusal"
    missing = with_reason - set(FAILED_STATUSES)
    assert not missing, f"{missing} would slip past the circuit breaker"


def test_a_success_is_never_counted_as_a_failure():
    """The dangerous direction: counting PLACED would abort a batch mid-rebalance."""
    from app.core.gateway import FAILED_STATUSES
    assert "PLACED" not in FAILED_STATUSES
    assert "DRY_RUN" not in FAILED_STATUSES
    # DUPLICATE is idempotency working, not a failure, and carries no reason to compare.
    assert "DUPLICATE" not in FAILED_STATUSES


def test_the_route_uses_the_gateway_set_rather_than_its_own():
    import app.main as M
    from app.core.gateway import FAILED_STATUSES
    assert M._FAILED_STATUSES is FAILED_STATUSES
    src = pathlib.Path("app/main.py").read_text()
    assert '("ERROR", "BLOCKED")' not in src, "the hand-written pair is back"


# =====================================================================================
# a refusal the broker names is not an unknown outcome
# =====================================================================================
@pytest.mark.parametrize("exc_name", ["InputException", "OrderException",
                                      "PermissionException", "TokenException"])
def test_a_named_refusal_is_reported_as_definitively_not_placed(exc_name, monkeypatch):
    """Kite refusing an order for bad margin, a disallowed IP, an invalid parameter or an
    expired token decides it BEFORE the exchange sees anything. Every one of those became
    a generic ERROR, and the report reads ERROR as "an order may still have been
    accepted" — so six SHILPAMED rejections that each said "Insufficient funds" in plain
    words each sent the operator to the order book to rule out a double-send that was
    never possible."""
    import kiteconnect.exceptions as KE

    from app import config as C
    from app.core import gateway as G

    # These assert what the gateway reports when the BROKER refuses, which is a live-path
    # outcome: under DRY_RUN the gateway short-circuits at gateway.py:85 and never calls
    # place_order at all. The mode is set here rather than inherited from the environment,
    # because CI forces DRY_RUN=true everywhere (CLAUDE.md safety rails) and a test whose
    # meaning depends on an ambient flag is a test that silently stops testing. Nothing here
    # touches a network or a credential: the broker is the stub KC class below.
    monkeypatch.setattr(C, "DRY_RUN", False)

    exc = getattr(KE, exc_name)("Insufficient funds")

    class KC:
        def place_order(self, **kw):
            raise exc

    gw = G.OrderGateway(KC(), RiskManager())
    res = asyncio.run(gw.place(symbol="AAA", qty=1, side="BUY", product="CNC",
                               order_type="LIMIT", price=10.0, exchange="NSE"))
    assert res["status"] == "REJECTED"
    assert res["reached_exchange"] is False
    assert res["exception"] == exc_name


def test_a_transport_failure_stays_unknown(monkeypatch):
    """The one case where the warning is right. A call that never got an answer may have
    been accepted, and that is worth checking the order book for."""
    from app import config as C
    from app.core import gateway as G

    # These assert what the gateway reports when the BROKER refuses, which is a live-path
    # outcome: under DRY_RUN the gateway short-circuits at gateway.py:85 and never calls
    # place_order at all. The mode is set here rather than inherited from the environment,
    # because CI forces DRY_RUN=true everywhere (CLAUDE.md safety rails) and a test whose
    # meaning depends on an ambient flag is a test that silently stops testing. Nothing here
    # touches a network or a credential: the broker is the stub KC class below.
    monkeypatch.setattr(C, "DRY_RUN", False)

    class KC:
        def place_order(self, **kw):
            raise TimeoutError("read timed out")

    gw = G.OrderGateway(KC(), RiskManager())
    res = asyncio.run(gw.place(symbol="AAA", qty=1, side="BUY", product="CNC",
                               order_type="LIMIT", price=10.0, exchange="NSE"))
    assert res["status"] == "ERROR"
    assert res["reached_exchange"] is None


def test_both_still_stop_the_batch():
    """Whichever it is, three in a row is systemic and the breaker must see it."""
    from app.core.gateway import FAILED_STATUSES
    assert {"REJECTED", "ERROR"} <= set(FAILED_STATUSES)


def test_the_report_tells_them_apart():
    """And says the opposite thing about re-running, which is the whole point."""
    src = pathlib.Path("app/templates/index.html").read_text()
    assert "REJECTED:" in src and "ERROR:" in src
    rej = src.split("REJECTED:")[1].split("},")[0]
    err = src.split("ERROR:")[1].split("},")[0]
    assert "never reached the exchange" in rej
    assert "cannot be sitting live" in rej
    assert "genuinely UNKNOWN" in err


# =====================================================================================
# funding, before you confirm
# =====================================================================================
def test_the_plan_carries_its_funding_shortfall():
    """It was measured only at execute time and rendered nowhere, so the number that
    predicts a rejection sat in the raw JSON while SHILPAMED was refused six times for
    exactly that shortfall. Now measured at analysis and shown on the ticket."""
    import app.main as M

    class KC:
        def basket_order_margins(self, basket, **kw):
            return {"final": {"total": 1_600_000.0}}

    class K:
        kc = KC()
        def available_cash(self):
            return 484_514.91

    out = asyncio.run(M._funding_check(K(), [{"symbol": "AAA", "delta": 800,
                                              "ref_price": 810.0}]))
    assert out["checked"] and out["shortfall"] == round(1_600_000 - 484_514.91)


def test_a_funded_plan_reports_no_shortfall():
    import app.main as M

    class KC:
        def basket_order_margins(self, basket, **kw):
            return {"final": {"total": 100_000.0}}

    class K:
        kc = KC()
        def available_cash(self):
            return 500_000.0

    out = asyncio.run(M._funding_check(K(), [{"symbol": "AAA", "delta": 10,
                                              "ref_price": 100.0}]))
    assert out["checked"] and out["shortfall"] == 0


def test_a_broken_margin_endpoint_never_blocks_a_plan():
    """Reported, not fatal. A margin service that is down must not stop a rebalance the
    operator has decided on — it must only stop the page claiming the plan is funded."""
    import app.main as M

    class KC:
        def basket_order_margins(self, basket, **kw):
            raise RuntimeError("margins down")

    class K:
        kc = KC()
        def available_cash(self):
            return 1.0

    out = asyncio.run(M._funding_check(K(), [{"symbol": "AAA", "delta": 1,
                                              "ref_price": 1.0}]))
    assert out["checked"] is False and "margins down" in out["error"]


def test_analysis_and_execution_ask_the_same_question():
    """Two copies of a margin check drift. /execute re-runs it because cash moves between
    deciding and confirming, but it is the same helper."""
    src = pathlib.Path("app/main.py").read_text()
    assert src.count("async def _funding_check") == 1
    assert src.count("_funding_check(k, plan[\"orders\"])") == 2


def test_the_ticket_shows_the_shortfall_before_confirmation():
    src = pathlib.Path("app/templates/index.html").read_text()
    assert 'id="ticketFunding"' in src
    ticket = src.split("function openTicket()")[1].split("</script>")[0]
    assert "shortfall" in ticket and "will be refused" in ticket


def test_a_funding_check_that_did_not_run_says_so_on_the_page():
    """The preflight raised NameError on every execution — asyncio was never imported in
    main.py — and its own `except` turned that into {"checked": False}, which nothing
    rendered. A safety check that cannot run must not be indistinguishable from one that
    passed, so the report states it explicitly."""
    src = pathlib.Path("app/templates/index.html").read_text()
    block = src.split("const pf=j.preflight")[1].split("const s=document")[0]
    assert "Not checked" in block
    assert "nothing verified" in block


def test_main_can_actually_reach_the_helpers_it_awaits():
    """The defect in one line: `await asyncio.to_thread(...)` with no import. It was
    invisible because the only caller wrapped it in a broad except."""
    import app.main as M
    assert hasattr(M, "asyncio"), "main.py awaits asyncio without importing it"
