"""The browser-facing defences.

The threat changed when the host did. On a laptop this ran on loopback behind a firewall
nobody could route to; on a cloud box with a static IP the machine is findable, the
tunnel is open for hours, and the browser holding it also visits the rest of the internet.
Neither attack below needs a packet to reach port 8420 from outside.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app import config as C
from app import main as M


@pytest.fixture()
def client():
    return TestClient(M.app)


ORIGIN = "http://testserver:8420"


# =====================================================================================
# DNS rebinding: reading the whole book from another origin
# =====================================================================================
def test_a_request_under_another_name_is_refused(client):
    """An attacker's page resolves its own domain to 127.0.0.1 and then reads
    /performance/data as same-origin — every holding, every figure. The Host header is
    what gives it away: this app answers to localhost and nothing else."""
    r = client.get("/performance/data", headers={"Host": "evil.example"})
    assert r.status_code == 421
    assert "evil.example" in r.text


def test_the_names_it_does_answer_to_still_work(client):
    for host in ("127.0.0.1", "localhost"):
        assert client.get("/status", headers={"Host": host}).status_code == 200


def test_the_allowlist_is_configurable(monkeypatch, client):
    monkeypatch.setattr(C, "DESK_ALLOWED_HOSTS", "desk.internal")
    assert client.get("/status", headers={"Host": "desk.internal"}).status_code == 200
    assert client.get("/status", headers={"Host": "127.0.0.1"}).status_code == 421


# =====================================================================================
# CSRF: posting to the desk from somewhere else
# =====================================================================================
def test_a_post_with_no_origin_is_refused(client):
    """A form on any page can POST here without a preflight. /execute and /stops/arm are
    safe by accident — both need a plan_id that is a fresh uuid4 held in memory — but
    /settings takes named strategy knobs and /ops/run takes an operation name, and
    neither carries a secret."""
    r = client.post("/ops/run", data={"op": "daily"}, headers={"Origin": ""})
    assert r.status_code == 403
    assert "did not come from the desk" in r.text


def test_a_post_from_another_origin_is_refused(client):
    r = client.post("/settings", data={"note": "x"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_a_post_from_the_desk_is_allowed(client):
    """It must reach the route and be judged on its own merits, not blocked here."""
    r = client.post("/ops/run", data={"op": "definitely-not-an-op"},
                    headers={"Origin": ORIGIN})
    assert r.status_code != 403


def test_a_referer_stands_in_when_origin_is_absent(client):
    r = client.post("/ops/run", data={"op": "nope"},
                    headers={"Origin": "", "Referer": ORIGIN + "/ops"})
    assert r.status_code != 403


def test_reads_are_never_blocked_by_the_origin_rule(client):
    """GET is safe by definition and the OAuth callback arrives as a top-level navigation
    with no Origin at all — blocking it would break the login this desk depends on."""
    for path in ("/", "/status", "/performance"):
        assert client.get(path, headers={"Origin": ""}).status_code == 200


# =====================================================================================
# the optional password
# =====================================================================================
def test_no_password_by_default_so_the_laptop_is_unchanged():
    assert C.DESK_PASSWORD == ""


def test_when_set_every_page_requires_it(monkeypatch, client):
    """Loopback is not a boundary between users of the same host: without this, every
    route is reachable by anything on the box that can open a socket."""
    monkeypatch.setattr(C, "DESK_PASSWORD", "hunter2")
    assert client.get("/").status_code == 401
    assert "Basic" in client.get("/").headers.get("WWW-Authenticate", "")


def test_the_right_password_gets_in(monkeypatch, client):
    monkeypatch.setattr(C, "DESK_PASSWORD", "hunter2")
    tok = base64.b64encode(b"desk:hunter2").decode()
    assert client.get("/", headers={"Authorization": f"Basic {tok}"}).status_code == 200


def test_a_wrong_password_does_not(monkeypatch, client):
    monkeypatch.setattr(C, "DESK_PASSWORD", "hunter2")
    tok = base64.b64encode(b"desk:hunter3").decode()
    assert client.get("/", headers={"Authorization": f"Basic {tok}"}).status_code == 401


def test_status_stays_reachable_so_a_health_check_still_works(monkeypatch, client):
    monkeypatch.setattr(C, "DESK_PASSWORD", "hunter2")
    assert client.get("/status").status_code == 200


def test_the_comparison_is_constant_time():
    """A byte-at-a-time comparison leaks the password to anything that can time it."""
    import inspect

    from app.core import websec as W
    src = inspect.getsource(W._basic_ok)
    assert "compare_digest" in src


def test_malformed_credentials_are_rejected_rather_than_crashing(monkeypatch, client):
    monkeypatch.setattr(C, "DESK_PASSWORD", "hunter2")
    for bad in ("", "Basic", "Basic !!!not-base64!!!", "Bearer hunter2"):
        assert client.get("/", headers={"Authorization": bad}).status_code == 401


# =====================================================================================
# the API docs
# =====================================================================================
def test_the_docs_are_closed_by_default(client):
    """They enumerate every route, including the ones that place orders."""
    assert C.DESK_DOCS is False
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
