"""POST /execute must route every order through core/gateway.py.

Before this, /execute called kite_client directly: the SGB guard still fired, but the
risk manager, kill switch, rate limiter and order journal were all bypassed on the one
path that spends real money.
"""
from __future__ import annotations

import json
import time

import pytest
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
    src = open("app/core/gateway.py").read()
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
