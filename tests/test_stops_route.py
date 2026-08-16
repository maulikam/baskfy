"""The arm-stops flow: proposing and doing are separate acts.

Every gate here is the difference between a status page and seventeen live triggers.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import config as C
from app import main as M


class FakeKC:
    def __init__(self, gtts=None):
        self._gtts = gtts or []
        self.placed = []

    def get_gtts(self):
        return list(self._gtts)


class FakeKite:
    def __init__(self, holdings=None, gtts=None, authed=True):
        self._h = holdings or [{"symbol": "AAA", "quantity": 100, "last_price": 1000.0,
                                "pledged_qty": 0}]
        self.kc = FakeKC(gtts)
        self._authed = authed
        self.stops = []

    def is_authed(self):
        return self._authed

    def holdings(self):
        return [dict(h) for h in self._h]

    def place_gtt_stop(self, symbol, qty, trigger, last_price):
        self.stops.append((symbol, qty, trigger))
        return {"symbol": symbol, "status": "DRY_RUN_GTT", "trigger": trigger}


@pytest.fixture()
def kite(monkeypatch):
    fake = FakeKite()
    monkeypatch.setattr(M, "kite", lambda: fake)
    M.STOP_PLANS.clear()
    return fake


@pytest.fixture()
def client(kite):
    return TestClient(M.app)


def make_plan(client):
    client.get("/stops")
    return next(iter(M.STOP_PLANS))


# =====================================================================================
# reviewing places nothing
# =====================================================================================
def test_the_review_page_arms_nothing(client, kite):
    client.get("/stops")
    assert kite.stops == []


def test_the_review_page_issues_a_plan(client):
    client.get("/stops")
    assert len(M.STOP_PLANS) == 1


def test_an_expired_session_explains_itself_instead_of_erroring(monkeypatch):
    monkeypatch.setattr(M, "kite", lambda: FakeKite(authed=False))
    r = TestClient(M.app).get("/stops")
    assert r.status_code == 200 and "session expired" in r.text.lower()


def test_nothing_to_arm_is_stated_plainly(monkeypatch):
    fake = FakeKite(gtts=[{"status": "active",
                           "condition": {"tradingsymbol": "AAA",
                                         "trigger_values": [900.0]},
                           "orders": [{"quantity": 100}]}])
    monkeypatch.setattr(M, "kite", lambda: fake)
    assert "already carries a stop" in TestClient(M.app).get("/stops").text


# =====================================================================================
# the gates
# =====================================================================================
def test_arming_without_confirmation_is_refused(client):
    pid = make_plan(client)
    r = client.post("/stops/arm", data={"plan_id": pid, "confirm": "false"})
    assert r.status_code == 400


def test_arming_an_unknown_plan_is_refused(client):
    r = client.post("/stops/arm", data={"plan_id": "nope", "confirm": "true"})
    assert r.status_code == 404


def test_a_stale_plan_is_refused_because_triggers_are_priced_live(client):
    pid = make_plan(client)
    M.STOP_PLANS[pid]["created_at"] = time.time() - 3600
    r = client.post("/stops/arm", data={"plan_id": pid, "confirm": "true"})
    assert r.status_code == 410


def test_a_refused_attempt_places_nothing(client, kite):
    pid = make_plan(client)
    client.post("/stops/arm", data={"plan_id": pid, "confirm": "false"})
    assert kite.stops == []


# =====================================================================================
# arming
# =====================================================================================
def test_confirming_places_one_stop_per_row(client, kite):
    pid = make_plan(client)
    rows = M.STOP_PLANS[pid]["rows"]
    r = client.post("/stops/arm", data={"plan_id": pid, "confirm": "true"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert len(kite.stops) == len(rows)
    assert kite.stops[0][0] == rows[0]["symbol"]


def test_a_plan_is_single_use(client, kite):
    """Re-posting must not double-arm; the browser back button is a real hazard here."""
    pid = make_plan(client)
    client.post("/stops/arm", data={"plan_id": pid, "confirm": "true"},
                follow_redirects=False)
    first = len(kite.stops)
    again = client.post("/stops/arm", data={"plan_id": pid, "confirm": "true"},
                        follow_redirects=False)
    assert again.status_code == 404 and len(kite.stops) == first


def test_one_rejection_does_not_abandon_the_rest(client, kite, monkeypatch):
    fake = FakeKite(holdings=[
        {"symbol": "AAA", "quantity": 100, "last_price": 1000.0, "pledged_qty": 0},
        {"symbol": "BBB", "quantity": 100, "last_price": 500.0, "pledged_qty": 0}])
    calls = {"n": 0}

    def flaky(symbol, qty, trigger, last_price):
        calls["n"] += 1
        if symbol == "AAA":
            raise RuntimeError("exchange rejected")
        fake.stops.append((symbol, qty, trigger))
        return {"symbol": symbol, "status": "DRY_RUN_GTT"}

    fake.place_gtt_stop = flaky
    monkeypatch.setattr(M, "kite", lambda: fake)
    M.STOP_PLANS.clear()
    c = TestClient(M.app)
    pid = make_plan(c)
    r = c.post("/stops/arm", data={"plan_id": pid, "confirm": "true"},
               follow_redirects=False)
    assert calls["n"] == 2                      # both attempted
    assert fake.stops == [("BBB", 100, pytest.approx(fake.stops[0][2]))]
    assert "failed" in r.headers["location"]


def test_the_outcome_is_reported_back(client):
    pid = make_plan(client)
    r = client.post("/stops/arm", data={"plan_id": pid, "confirm": "true"},
                    follow_redirects=False)
    assert "armed=" in r.headers["location"]


def test_dry_run_says_simulated_rather_than_armed(client, monkeypatch):
    monkeypatch.setattr(C, "DRY_RUN", True)
    pid = make_plan(client)
    r = client.post("/stops/arm", data={"plan_id": pid, "confirm": "true"},
                    follow_redirects=False)
    assert "simulated" in r.headers["location"]


# =====================================================================================
# both bugs the first live arming run exposed
# =====================================================================================
def test_the_success_status_matches_what_kite_client_actually_returns():
    """The first live run placed 16 triggers and reported '0 armed, 17 failed' because
    GTT_PLACED was not in a hand-written success set. A false failure invites a re-arm,
    which is how a position ends up with two stops."""
    from app.kite_client import Kite
    src = open("app/kite_client.py").read()
    returned = {s for s in ("GTT_PLACED", "DRY_RUN_GTT") if f'"status": "{s}"' in src}
    assert returned, "kite_client no longer returns a recognisable GTT status"
    assert returned <= M.STOP_OK, f"{returned - M.STOP_OK} would be reported as failures"


def test_a_real_failure_is_still_reported_with_its_reason(client, kite, monkeypatch):
    def reject(symbol, qty, trigger, last_price):
        return {"symbol": symbol, "status": "GTT_ERROR",
                "error": "Trigger price should be a multiple of tick size 1.00."}
    kite.place_gtt_stop = reject
    pid = make_plan(client)
    r = client.post("/stops/arm", data={"plan_id": pid, "confirm": "true"},
                    follow_redirects=False)
    from urllib.parse import unquote_plus
    msg = unquote_plus(r.headers["location"])
    assert "0 of 1" in msg
    assert "1 failed" in msg and "tick size" in msg      # the reason travels with it


@pytest.mark.parametrize("price,tick,expected", [
    (10528.9, 1.0, 10529.0),      # OFSS: one decimal is not a valid price here
    (1291.13, 0.1, 1291.1),
    (198.567, 0.01, 198.57),
    (447.6, 0.05, 447.60),
])
def test_prices_snap_to_the_instruments_tick(price, tick, expected):
    from app.kite_client import Kite
    assert Kite.to_tick(price, tick) == pytest.approx(expected)


def test_a_snapped_price_is_always_a_whole_number_of_ticks():
    from app.kite_client import Kite
    for tick in (0.01, 0.05, 0.1, 1.0):
        for price in (10528.9, 1291.13, 198.567, 47.111):
            snapped = Kite.to_tick(price, tick)
            assert abs(round(snapped / tick) * tick - snapped) < 1e-6


def test_an_unknown_tick_does_not_crash_the_arming():
    from app.kite_client import Kite
    assert Kite.to_tick(100.123, 0) == 100.12
