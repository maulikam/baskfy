"""LV3 — the desk's shared risk row over a sqlite file: lock, load, save, and two managers.

`gates/live-3-risk.md` K4. Postgres takes the row `FOR UPDATE`; sqlite takes the write lock with
`BEGIN IMMEDIATE`. Either way one process decides at a time between lock and save.
"""

from __future__ import annotations

import datetime as dt
import functools

import pytest

from app.analytics import db as _db
from app.core.risk import RiskConfig, RiskManager
from app.core.risk_store import PgRiskStateStore

DAY = dt.date(2026, 9, 28)


@pytest.fixture
def store(tmp_path) -> PgRiskStateStore:  # noqa: ANN001
    path = str(tmp_path / "desk.db")
    with _db.connect(path) as conn:
        conn.execute(
            "CREATE TABLE risk_ledger (user_id INTEGER NOT NULL, day DATE NOT NULL, "
            "payload TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (user_id, day))"
        )
    return PgRiskStateStore(functools.partial(_db.connect, path), user_id=1, schema="", today=lambda: DAY)


def test_the_row_is_read_and_written_only_under_the_lock(store: PgRiskStateStore) -> None:
    with pytest.raises(RuntimeError):
        store.load()
    with store.lock():
        assert store.load() is None
        store.save({"day": DAY.isoformat(), "orders_today": 2, "position_value": {"A": 5.0}})
    with store.lock():
        assert store.load() == {"day": DAY.isoformat(), "orders_today": 2, "position_value": {"A": 5.0}}


def test_two_managers_over_the_desk_row_share_one_cap(store: PgRiskStateStore) -> None:
    cfg = RiskConfig(max_position_value=10_000.0)
    desk, twt_auto = RiskManager(cfg, store=store), RiskManager(cfg, store=store)
    assert desk.pre_order("SAME", 6_000.0, 6_000.0)[0]
    ok, why = twt_auto.pre_order("SAME", 6_000.0, 6_000.0)
    assert not ok and "12,000 > cap" in why
    desk.kill("operator")
    assert twt_auto.pre_order("OTHER", 1.0, 1.0)[1].startswith("KILL SWITCH")
    with store.lock():
        row = store.load()
    assert row is not None and row["killed"] is True and row["orders_today"] == 1


def test_the_gateway_wires_the_row_only_on_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main as _main

    monkeypatch.setattr(_db, "DB_BACKEND", "sqlite")
    assert _main._risk_store() is None  # noqa: SLF001 - the wiring under test
    monkeypatch.setattr(_db, "DB_BACKEND", "postgres")
    assert isinstance(_main._risk_store(), PgRiskStateStore)  # noqa: SLF001
