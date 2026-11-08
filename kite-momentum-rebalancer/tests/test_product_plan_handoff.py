"""A product plan crosses into the desk as reviewable intent, never as an execution call."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.analytics import db
from app.analytics import plan_store as PS


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "handoff.db")) as connection:
        assert db.migrate(connection) >= 14
        yield connection


def _plan(created: float = 1_700_000_000.0) -> dict:
    return {
        "plan_id": "sc3abc123",
        "created_at": created,
        "capital": 5000,
        "book_value": 5000,
        "cash": 0,
        "buys_value": 3000,
        "sells_value": 1000,
        "net_cash_use": 2000,
        "pledged_sells_margin_note": [],
        "cluster_warnings": [],
        "excluded": [],
        "unpriced": [],
        "funding": {"checked": False, "reason": "verified again by the desk"},
        "curated": {"schema": "baskfy.curated-plan/v1", "batch_id": 7},
        "orders": [
            {
                "symbol": "AAA",
                "delta": 10,
                "ref_price": 300.0,
                "action": "BUY",
                "qty_now": 0,
                "qty_final": 10,
                "value": 3000,
                "weight": 60.0,
            },
            {
                "symbol": "BBB",
                "delta": -5,
                "ref_price": 200.0,
                "action": "EXIT",
                "qty_now": 5,
                "qty_final": 0,
                "value": 0,
                "weight": 0.0,
            },
        ],
    }


def test_saved_plan_round_trips_through_a_normalized_journal(conn) -> None:
    plan = _plan()
    PS.save_plan(conn, plan, note="product handoff")
    restored = PS.load_executable_plan(conn, plan["plan_id"], now=plan["created_at"] + 1)
    assert restored == plan
    assert restored["curated"]["schema"] == "baskfy.curated-plan/v1"


def test_exact_expiry_boundary_is_live_and_the_next_instant_is_stale(conn) -> None:
    plan = _plan()
    PS.save_plan(conn, plan)
    assert PS.load_executable_plan(
        conn, plan["plan_id"], now=plan["created_at"] + 1800
    )["plan_id"] == plan["plan_id"]
    with pytest.raises(PS.StoredPlanError, match="older than 30 minutes"):
        PS.load_executable_plan(conn, plan["plan_id"], now=plan["created_at"] + 1800.001)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["orders"][0].update(delta=11), "disagrees"),
        (lambda value: value["orders"][1].update(symbol="AAA"), "duplicate"),
        (lambda value: value.update(book_value=float("inf")), "finite"),
        (lambda value: value.update(plan_id="another"), "plan id disagrees"),
    ],
)
def test_tampered_or_malformed_envelopes_fail_closed(conn, mutation, message: str) -> None:
    plan = _plan()
    PS.save_plan(conn, plan)
    stored = json.loads(
        conn.execute(
            "SELECT plan_json FROM rebalance_versions WHERE version_id=?", (plan["plan_id"],)
        ).fetchone()["plan_json"]
    )
    mutation(stored)
    conn.execute(
        "UPDATE rebalance_versions SET plan_json=? WHERE version_id=?",
        (json.dumps(stored), plan["plan_id"]),
    )
    with pytest.raises(PS.StoredPlanError, match=message):
        PS.load_executable_plan(conn, plan["plan_id"], now=plan["created_at"] + 1)


def test_legacy_plan_without_a_payload_cannot_be_guessed(conn) -> None:
    plan = _plan()
    PS.save_plan(conn, plan)
    conn.execute(
        "UPDATE rebalance_versions SET plan_json=NULL WHERE version_id=?", (plan["plan_id"],)
    )
    with pytest.raises(PS.StoredPlanError, match="no durable review payload"):
        PS.load_executable_plan(conn, plan["plan_id"], now=plan["created_at"] + 1)


class _Session:
    def __init__(self, authenticated: bool) -> None:
        self._authenticated = authenticated

    def is_authed(self) -> bool:
        return self._authenticated


def test_authenticated_get_restores_review_but_never_calls_the_gateway(monkeypatch) -> None:
    plan = _plan(time.time())
    with db.connect() as connection:
        db.migrate(connection)
        PS.save_plan(connection, plan)
    main.PLANS.clear()
    monkeypatch.setattr(main, "_kite", _Session(True))

    def no_gateway():
        raise AssertionError("a GET handoff must never reach execution")

    monkeypatch.setattr(main, "gateway", no_gateway)
    response = TestClient(main.app).get(f"/?plan_id={plan['plan_id']}")
    assert response.status_code == 200
    assert "product plan loaded" in response.text
    assert "Review every line" in response.text
    assert main.PLANS[plan["plan_id"]]["curated"]["batch_id"] == 7


def test_stale_broker_session_does_not_put_a_handoff_in_memory(monkeypatch) -> None:
    plan = _plan(time.time())
    with db.connect() as connection:
        db.migrate(connection)
        PS.save_plan(connection, plan)
    main.PLANS.clear()
    monkeypatch.setattr(main, "_kite", _Session(False))
    response = TestClient(main.app).get(f"/?plan_id={plan['plan_id']}")
    assert response.status_code == 200
    assert "handoff refused" in response.text
    assert "Kite session expired" in response.text
    assert plan["plan_id"] not in main.PLANS
