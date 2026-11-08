"""Phase 1 acceptance: schema, idempotency, and index chaining."""
from __future__ import annotations

import json

import pytest

from app.analytics import db


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "portfolio.db")) as c:
        db.migrate(c)
        yield c


def snap(date, nav, invested=None, cash=0.0):
    inv = nav - cash if invested is None else invested
    return {"date": date, "nav": nav, "invested": inv, "cash": cash,
            "holdings_json": json.dumps({"positions": []})}


# --- schema ---------------------------------------------------------------------------
def test_migration_creates_every_table(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"snapshots", "cashflows", "benchmark", "rebalance_versions",
            "rebalance_orders", "trades"} <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_wal_is_enabled(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == db.SCHEMA_VERSION
    assert db.migrate(conn) == db.SCHEMA_VERSION


def test_snapshots_columns_match_spec(conn):
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")]
    assert cols == ["date", "nav", "invested", "cash", "holdings_json", "index_value"]


# --- acceptance: inception index is exactly 100 ---------------------------------------
def test_inception_index_is_exactly_100(conn):
    db.save_snapshot(conn, snap("2026-01-01", 1_000_000.0))
    row = db.get_snapshot(conn, "2026-01-01")
    assert row["index_value"] == 100.0          # exact, not approx


def test_inception_index_is_100_regardless_of_nav(conn):
    db.save_snapshot(conn, snap("2026-01-01", 7_654_321.99))
    assert db.get_snapshot(conn, "2026-01-01")["index_value"] == 100.0


# --- acceptance: re-running a date is idempotent --------------------------------------
def test_rerun_same_date_creates_no_duplicate_and_changes_nothing(conn):
    db.save_snapshot(conn, snap("2026-01-01", 1_000_000.0))
    db.save_snapshot(conn, snap("2026-01-02", 1_020_000.0))
    before = [dict(r) for r in db.snapshot_series(conn)]

    # re-run both days with DIFFERENT numbers — stored values must not move
    assert db.save_snapshot(conn, snap("2026-01-01", 999.0)) == "unchanged"
    assert db.save_snapshot(conn, snap("2026-01-02", 111.0)) == "unchanged"

    after = [dict(r) for r in db.snapshot_series(conn)]
    assert len(after) == 2
    assert before == after


def test_force_overwrites_and_rechains(conn):
    db.save_snapshot(conn, snap("2026-01-01", 1_000_000.0))
    db.save_snapshot(conn, snap("2026-01-02", 1_100_000.0))
    assert db.get_snapshot(conn, "2026-01-02")["index_value"] == pytest.approx(110.0)

    assert db.save_snapshot(conn, snap("2026-01-02", 1_050_000.0), force=True) == "updated"
    assert db.get_snapshot(conn, "2026-01-02")["nav"] == 1_050_000.0
    assert db.get_snapshot(conn, "2026-01-02")["index_value"] == pytest.approx(105.0)
    assert len(db.snapshot_series(conn)) == 2


def test_cashflow_insert_is_idempotent(conn):
    assert db.record_cashflow(conn, "2026-01-05", -500_000.0, "invest") is True
    assert db.record_cashflow(conn, "2026-01-05", -500_000.0, "invest") is False
    assert conn.execute("SELECT COUNT(*) c FROM cashflows").fetchone()["c"] == 1


# --- index chaining -------------------------------------------------------------------
def test_index_tracks_nav_when_there_are_no_cashflows(conn):
    navs = [1_000_000.0, 1_050_000.0, 1_030_000.0, 1_200_000.0]
    for i, nav in enumerate(navs, start=1):
        db.save_snapshot(conn, snap(f"2026-01-{i:02d}", nav))
    for row, nav in zip(db.snapshot_series(conn), navs):
        assert row["index_value"] == pytest.approx(100.0 * nav / navs[0], rel=1e-12)


def test_cashflow_does_not_register_as_performance(conn):
    """Adding money must not move the index; only market moves may."""
    db.save_snapshot(conn, snap("2026-02-01", 1_000_000.0))
    db.record_cashflow(conn, "2026-02-02", -500_000.0, "invest")   # invest = negative
    db.save_snapshot(conn, snap("2026-02-02", 1_500_000.0))        # pure contribution
    assert db.get_snapshot(conn, "2026-02-02")["index_value"] == pytest.approx(100.0)


def test_return_is_measured_on_the_pre_flow_base(conn):
    db.save_snapshot(conn, snap("2026-02-01", 1_000_000.0))
    db.record_cashflow(conn, "2026-02-02", -500_000.0, "invest")
    db.save_snapshot(conn, snap("2026-02-02", 1_550_000.0))
    # (1_550_000 - 500_000) / 1_000_000 - 1 = 5%
    assert db.get_snapshot(conn, "2026-02-02")["index_value"] == pytest.approx(105.0)


def test_withdrawal_does_not_register_as_loss(conn):
    db.save_snapshot(conn, snap("2026-03-01", 1_000_000.0))
    db.record_cashflow(conn, "2026-03-02", 200_000.0, "withdraw")  # withdraw = positive
    db.save_snapshot(conn, snap("2026-03-02", 800_000.0))
    assert db.get_snapshot(conn, "2026-03-02")["index_value"] == pytest.approx(100.0)


def test_backdated_snapshot_rechains_the_whole_series(conn):
    db.save_snapshot(conn, snap("2026-04-01", 1_000_000.0))
    db.save_snapshot(conn, snap("2026-04-03", 1_200_000.0))
    assert db.get_snapshot(conn, "2026-04-03")["index_value"] == pytest.approx(120.0)

    # a day arrives late, in the middle — inception moves, everything re-chains
    db.save_snapshot(conn, snap("2026-03-31", 800_000.0))
    rows = db.snapshot_series(conn)
    assert [r["date"] for r in rows] == ["2026-03-31", "2026-04-01", "2026-04-03"]
    assert rows[0]["index_value"] == 100.0
    assert rows[2]["index_value"] == pytest.approx(100.0 * 1_200_000.0 / 800_000.0)


def test_zero_nav_day_does_not_divide_by_zero(conn):
    db.save_snapshot(conn, snap("2026-05-01", 0.0))
    db.save_snapshot(conn, snap("2026-05-02", 0.0))
    db.record_cashflow(conn, "2026-05-03", -1_000_000.0, "invest")
    db.save_snapshot(conn, snap("2026-05-03", 1_000_000.0))
    rows = db.snapshot_series(conn)
    assert rows[0]["index_value"] == 100.0
    assert all(r["index_value"] == pytest.approx(100.0) for r in rows)


def test_rechain_is_deterministic(conn):
    for i, nav in enumerate([1_000_000.0, 1_010_000.0, 990_000.0], start=1):
        db.save_snapshot(conn, snap(f"2026-06-{i:02d}", nav))
    first = [r["index_value"] for r in db.snapshot_series(conn)]
    db.rechain_index(conn)
    db.rechain_index(conn)
    assert [r["index_value"] for r in db.snapshot_series(conn)] == first


# --- cashflow CSV import --------------------------------------------------------------
def test_csv_import_signed_amount(conn, tmp_path):
    p = tmp_path / "cf.csv"
    p.write_text("date,amount,type\n2026-01-05,-500000,invest\n2026-02-05,100000,withdraw\n")
    assert db.import_cashflows_csv(conn, str(p)) == {"inserted": 2, "skipped_existing": 0}
    assert db.import_cashflows_csv(conn, str(p)) == {"inserted": 0, "skipped_existing": 2}
    assert db.cashflows_by_date(conn) == {"2026-01-05": 500_000.0, "2026-02-05": -100_000.0}


def test_csv_import_debit_credit_ledger(conn, tmp_path):
    p = tmp_path / "ledger.csv"
    p.write_text("date,debit,credit\n2026-01-05,500000,0\n2026-02-05,0,100000\n")
    db.import_cashflows_csv(conn, str(p))
    # debit = money in = investment = negative under the XIRR convention
    rows = {r["date"]: r["amount"] for r in conn.execute("SELECT date, amount FROM cashflows")}
    assert rows == {"2026-01-05": -500_000.0, "2026-02-05": 100_000.0}


def test_unrecorded_cashflow_hint(conn):
    db.save_snapshot(conn, snap("2026-07-01", 1_000_000.0, invested=900_000.0, cash=100_000.0))
    # cash jumps 400k with holdings flat -> looks like an unrecorded deposit
    db.save_snapshot(conn, snap("2026-07-02", 1_400_000.0, invested=900_000.0, cash=500_000.0))
    hits = db.suggest_unrecorded_cashflows(conn)
    assert [h["date"] for h in hits] == ["2026-07-02"]
    assert hits[0]["unexplained"] == pytest.approx(400_000.0)

    # once recorded, it stops being flagged
    db.record_cashflow(conn, "2026-07-02", -400_000.0, "invest")
    assert db.suggest_unrecorded_cashflows(conn) == []
