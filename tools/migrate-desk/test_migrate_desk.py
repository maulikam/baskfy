"""Tests for the desk migration.

House rule 2: *"Tests assert the **spec**, never current behaviour."* So the spec being asserted
here is D8 and the five defects leaf 1.3.1 measured in `scripts/migrate_to_postgres.py`'s output:

    D8            row counts and checksums are asserted, and an assertion FAILS the run
    §5.5.1        an identity column cannot take a NULL
    §5.5.2/hr7    uniques survive, so re-running is idempotent
    §5.5.3        the partial unique index survives
    §5.1/hr9      money lands as `numeric`, and the value is unchanged
    §5.4          sqlite_sequence is carried, and max(id)+1 is NOT a substitute
    §1a           the fork decision is a required input, not a default

The fixture is a miniature of the desk's real schema, built to contain every awkward thing the
live database contains: a composite PK, an ALTER-added column outside the stored DDL, a quoted
table name, a partial unique index, a foreign key, an AUTOINCREMENT counter standing above
max(id) because rows were deleted, and a REAL money column next to a REAL epoch column.

    pytest tools/migrate-desk/test_migrate_desk.py

Postgres-backed tests skip without a DSN:

    DESK_TEST_DSN=postgresql://baskfy:baskfy@localhost:5433/baskfy pytest …
"""
from __future__ import annotations

import decimal
import json
import os
import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import desk_fork as fork
import desk_pgddl as pgddl
import desk_preflight as preflight
import migrate_desk
from desk_assertions import (
    AssertionLedger,
    MigrationFailure,
    canonical,
    checksum,
    row_count,
    verdict_for,
)
from desk_introspect import SchemaRefusal, open_readonly, read_schema

DSN = os.environ.get("DESK_TEST_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="set DESK_TEST_DSN to run Postgres-backed tests")

FIXTURE_DDL = """
CREATE TABLE snapshots(
    date          TEXT PRIMARY KEY,
    nav           REAL NOT NULL,
    holdings_json TEXT NOT NULL,
    index_value   REAL
);
CREATE TABLE fills(
    trade_id TEXT PRIMARY KEY,
    symbol   TEXT NOT NULL,
    when_ts  REAL NOT NULL,
    price    REAL NOT NULL,
    charges  REAL NOT NULL DEFAULT 0
);
CREATE INDEX ix_fills_symbol ON fills(symbol, when_ts);
CREATE TABLE benchmark(
    index_name TEXT NOT NULL,
    date       TEXT NOT NULL,
    close      REAL,
    PRIMARY KEY(index_name, date)
);
CREATE TABLE rebalance_versions(
    version_id TEXT PRIMARY KEY,
    created_ts REAL NOT NULL,
    note       TEXT
);
CREATE TABLE rebalance_orders(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id  TEXT NOT NULL REFERENCES rebalance_versions(version_id),
    symbol      TEXT NOT NULL,
    planned_ref_price REAL
);
CREATE INDEX ix_rebalance_orders_version ON rebalance_orders(version_id);
CREATE TABLE regime_evaluations(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    committed     INTEGER NOT NULL DEFAULT 0,
    breadth_pct   REAL,
    UNIQUE(evaluation_id, run_id)
);
CREATE UNIQUE INDEX ux_regime_canonical
    ON regime_evaluations(evaluation_id) WHERE run_id = 'canonical';
CREATE TABLE cashflows(
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    date   TEXT NOT NULL,
    amount REAL NOT NULL,
    type   TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_cashflows_natural ON cashflows(date, amount, type);
CREATE TABLE "option_arms"(
    arm_id     TEXT PRIMARY KEY,
    variant_id TEXT NOT NULL,
    entry_spot REAL,
    dte_at_entry REAL
);
CREATE INDEX ix_option_arms_open ON option_arms(entry_spot) WHERE entry_spot IS NULL;
"""

#: Prices chosen to be nasty: values whose shortest decimal repr is not their binary expansion.
PRICES = [1234.5600000000001, 0.1, 2.675, 1e-7, 9999999.99, 3.0, 0.30000000000000004]


@pytest.fixture()
def source(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(path)
    conn.executescript(FIXTURE_DDL)
    # The index is `100 * nav / nav[0]`, and the semantic check re-derives it. Computing the
    # second row here rather than typing a rounded literal is the point: a hand-rounded value
    # would fail `index_value_recomputes`, which is exactly what that check is for.
    base, later = 1000000.5, 1100000.55
    conn.execute(
        "INSERT INTO snapshots VALUES ('2026-08-10', ?, '{\"A\":1}', ?)", (base, 100.0)
    )
    conn.execute(
        "INSERT INTO snapshots VALUES ('2026-08-11', ?, '{\"A\":1}', ?)",
        (later, 100.0 * later / base),
    )
    for i, price in enumerate(PRICES):
        conn.execute(
            "INSERT INTO fills VALUES (?,?,?,?,?)",
            (f"T{i}", "RELIANCE", 1787143663.4758668 + i, price, 0.0),
        )
    conn.execute("INSERT INTO benchmark VALUES ('NIFTY 500', '2026-08-10', 12345.67)")
    conn.execute("INSERT INTO rebalance_versions VALUES ('aaaa', 1787143663.47587, 'box plan')")
    for symbol in ("RELIANCE", "TCS", "INFY"):
        conn.execute(
            "INSERT INTO rebalance_orders(version_id, symbol, planned_ref_price) VALUES (?,?,?)",
            ("aaaa", symbol, 100.25),
        )
    # Delete a row so sqlite_sequence stands ABOVE max(id) — the regime_evaluations case.
    conn.execute("INSERT INTO regime_evaluations(evaluation_id, run_id) VALUES ('e1','preview')")
    conn.execute("INSERT INTO regime_evaluations(evaluation_id, run_id) VALUES ('e2','preview')")
    conn.execute("DELETE FROM regime_evaluations WHERE evaluation_id = 'e2'")
    conn.execute("INSERT INTO option_arms VALUES ('arm1', 'v1', 24500.5, 7.0)")
    # An ALTER-added column, which lives outside the stored CREATE TABLE text.
    conn.execute("ALTER TABLE rebalance_orders ADD COLUMN order_id TEXT")
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def secondary(tmp_path: pathlib.Path) -> pathlib.Path:
    """A forked store: it holds a plan the primary does not, plus one it shares."""
    path = tmp_path / "forked.db"
    conn = sqlite3.connect(path)
    conn.executescript(FIXTURE_DDL)
    conn.execute("INSERT INTO rebalance_versions VALUES ('aaaa', 1787143663.47587, 'box plan')")
    conn.execute("INSERT INTO rebalance_versions VALUES ('zzzz', 1787143999.0, 'orphan plan')")
    for symbol in ("WIPRO", "HCLTECH"):
        conn.execute(
            "INSERT INTO rebalance_orders(version_id, symbol, planned_ref_price) VALUES (?,?,?)",
            ("zzzz", symbol, 55.5),
        )
    conn.commit()
    conn.close()
    return path


# ------------------------------------------------------------------ canonical rendering


def test_canonical_renders_a_float_and_its_numeric_identically():
    """The bridge the whole numeric decision rests on (house rule 9 vs M19.3)."""
    for price in PRICES:
        stored = pgddl.to_pg_value(price, numeric=True)
        assert isinstance(stored, decimal.Decimal)
        assert float(stored) == price, "the numeric must round-trip to the identical float64"
        assert canonical(price) == canonical(stored)


def test_canonical_distinguishes_null_from_empty_and_zero():
    assert canonical(None) != canonical("")
    assert canonical(None) != canonical(0)
    assert canonical(0) != canonical("0.0")


def test_checksum_is_order_independent_but_not_content_independent():
    rows = [(1, "a"), (2, "b"), (3, None)]
    assert checksum(rows) == checksum(list(reversed(rows)))
    assert checksum(rows) != checksum([(1, "a"), (2, "b"), (3, "")])


def test_checksum_notices_a_column_swap():
    assert checksum([("a", "b")]) != checksum([("b", "a")])


def test_row_count_and_checksum_both_have_to_pass():
    good = verdict_for("t", [(1,), (2,)], [(1,), (2,)])
    assert good.ok
    short = verdict_for("t", [(1,), (2,)], [(1,)])
    assert not short.row_count_ok and not short.ok
    changed = verdict_for("t", [(1,), (2,)], [(1,), (3,)])
    assert changed.row_count_ok and not changed.checksum_ok and not changed.ok


def test_a_failed_assertion_raises_rather_than_returning_a_flag():
    """D8's word is *assertions*. This is the test that they are not warnings."""
    ledger = AssertionLedger()
    ledger.add(verdict_for("fills", [(1,)], []))
    with pytest.raises(MigrationFailure) as exc:
        ledger.assert_all()
    assert "fills" in str(exc.value)
    assert "row_count" in str(exc.value)


def test_row_count_helper_is_a_count():
    assert row_count([(1,), (2,)]) == 2


# ------------------------------------------------------------------ the numeric decision


def test_money_lands_as_numeric_and_clocks_do_not(source: pathlib.Path):
    conn = open_readonly(str(source))
    schema = read_schema(conn)
    by_name = {t.name: t for t in schema.tables}

    def kind(table: str, column: str, float_money: bool = False) -> str:
        col = next(c for c in by_name[table].columns if c.name == column)
        return pgddl.pg_type(table, col, float_money)

    assert kind("fills", "price") == "numeric"
    assert kind("fills", "charges") == "numeric"
    assert kind("snapshots", "nav") == "numeric"
    assert kind("benchmark", "close") == "numeric"
    assert kind("rebalance_orders", "planned_ref_price") == "numeric"
    assert kind("regime_evaluations", "breadth_pct") == "numeric"
    assert kind("option_arms", "entry_spot") == "numeric"
    # Not money: an epoch clock and a day count.
    assert kind("fills", "when_ts") == "double precision"
    assert kind("rebalance_versions", "created_ts") == "double precision"
    assert kind("option_arms", "dte_at_entry") == "double precision"
    # M19.3's literal reading remains one flag away.
    assert kind("fills", "price", float_money=True) == "double precision"
    conn.close()


def test_every_float_by_design_column_carries_a_reason():
    assert all(reason for reason in pgddl.FLOAT_BY_DESIGN.values())
    assert not set(pgddl.FLOAT_BY_DESIGN) & set(pgddl.NUMERIC_COLUMNS), (
        "a column cannot be both money and not-money"
    )


def test_to_pg_value_refuses_a_non_finite_float():
    with pytest.raises(SchemaRefusal):
        pgddl.to_pg_value(float("inf"), numeric=True)
    with pytest.raises(SchemaRefusal):
        pgddl.to_pg_value(float("nan"), numeric=True)


def test_to_pg_value_refuses_text_in_a_money_column():
    with pytest.raises(SchemaRefusal):
        pgddl.to_pg_value("1234.56", numeric=True)


# ------------------------------------------------------------------ introspection


def test_alter_added_columns_are_read_from_the_pragma_not_the_ddl(source: pathlib.Path):
    """Five live columns were added by ALTER and are appended to the stored CREATE TABLE text.

    SQLite rewrites `sqlite_master.sql` by splicing the new column in before the closing paren,
    which is why the *pragma* is the only honest source: it reports position, declared type,
    NOT NULL and PK membership, none of which survives a text splice reliably. The live examples
    are `rebalance_orders.order_id` / `.reconciled_at`, `rebalance_versions.evaluation_id`,
    `option_arms.contracts_json` / `.underlying_token`.
    """
    conn = open_readonly(str(source))
    table = read_schema(conn).table("rebalance_orders")
    assert table.column_names[-1] == "order_id", "the ALTER-added column is last"
    assert '"order_id" text' in pgddl.create_table("desk", table)
    conn.close()


def test_the_partial_unique_index_is_read_with_its_predicate(source: pathlib.Path):
    conn = open_readonly(str(source))
    table = read_schema(conn).table("regime_evaluations")
    partial = next(i for i in table.standalone_indexes if i.name == "ux_regime_canonical")
    assert partial.unique
    assert partial.where == "run_id = 'canonical'"
    sql = pgddl.create_index("desk", table, partial)
    assert "CREATE UNIQUE INDEX" in sql and "WHERE run_id = 'canonical'" in sql
    conn.close()


def test_unique_in_the_table_body_becomes_a_constraint(source: pathlib.Path):
    conn = open_readonly(str(source))
    table = read_schema(conn).table("regime_evaluations")
    assert [i.origin for i in table.unique_constraints] == ["u"]
    assert "UNIQUE" in pgddl.create_table("desk", table)
    conn.close()


def test_composite_primary_keys_survive(source: pathlib.Path):
    conn = open_readonly(str(source))
    assert read_schema(conn).table("benchmark").pk_columns == ("index_name", "date")
    conn.close()


def test_the_foreign_key_is_found(source: pathlib.Path):
    conn = open_readonly(str(source))
    table = read_schema(conn).table("rebalance_orders")
    (fk,) = table.foreign_keys
    assert fk.columns == ("version_id",) and fk.ref_table == "rebalance_versions"
    assert "FOREIGN KEY" in pgddl.add_foreign_keys("desk", table)[0]
    conn.close()


def test_a_quoted_table_name_does_not_leak_into_the_target(source: pathlib.Path):
    conn = open_readonly(str(source))
    table = read_schema(conn).table("option_arms")
    assert table.ddl.startswith('CREATE TABLE "option_arms"')
    assert pgddl.create_table("desk", table).startswith('CREATE TABLE "desk"."option_arms"')
    conn.close()


def test_sqlite_sequence_is_carried_and_max_id_is_not_a_substitute(source: pathlib.Path):
    """§5.4: `regime_evaluations` has seq 2 against 1 row here, and 32 against 26 live."""
    conn = open_readonly(str(source))
    table = read_schema(conn).table("regime_evaluations")
    max_id = conn.execute("SELECT max(id) FROM regime_evaluations").fetchone()[0]
    assert table.sequence == 2 and max_id == 1, "a deleted row must leave the counter ahead"
    sql, params, _why = pgddl.setval("desk", table)
    assert params[-1] == 2, "the setval must use sqlite_sequence, not max(id)"
    assert "setval" in sql
    conn.close()


def test_an_autoincrement_table_that_was_never_written_starts_at_one(source: pathlib.Path):
    conn = open_readonly(str(source))
    table = read_schema(conn).table("cashflows")
    assert table.sequence is None
    sql, params, why = pgddl.setval("desk", table)
    assert params[-1] == 1 and "false" in sql and "never been inserted" in why
    conn.close()


def test_the_source_is_opened_so_that_it_cannot_be_written(source: pathlib.Path):
    conn = open_readonly(str(source))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM fills")
    conn.close()


def test_an_unknown_declared_type_is_refused(tmp_path: pathlib.Path):
    path = tmp_path / "weird.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(a VARCHAR2(10))")
    conn.commit()
    conn.close()
    ro = open_readonly(str(path))
    with pytest.raises(SchemaRefusal):
        read_schema(ro)
    ro.close()


# ------------------------------------------------------------------ preflight rails


def test_apply_refuses_a_manifest_that_does_not_say_ok(tmp_path: pathlib.Path):
    bad = tmp_path / "manifest.json"
    bad.write_text(json.dumps({"verified": {"integrity": "*** in database main ***"}}))
    with pytest.raises(preflight.PreflightRefusal, match="not 'ok'"):
        preflight.verify_backup_manifest(bad)


def test_apply_refuses_a_missing_manifest(tmp_path: pathlib.Path):
    with pytest.raises(preflight.PreflightRefusal, match="no backup manifest"):
        preflight.verify_backup_manifest(tmp_path / "nope.json")


def test_apply_refuses_an_archive_inside_the_repository(source: pathlib.Path, tmp_path):
    with pytest.raises(preflight.PreflightRefusal, match="inside the repository"):
        preflight.verify_archive(source, repo_root=tmp_path)


def test_apply_accepts_an_archive_outside_the_repository(source: pathlib.Path):
    out = preflight.verify_archive(source, repo_root=migrate_desk.REPO_ROOT)
    assert out["quick_check"] == "ok" and out["outside_repo"]


def test_apply_refuses_without_a_rehearsal_receipt(tmp_path: pathlib.Path):
    with pytest.raises(preflight.PreflightRefusal, match="no rehearsal receipt"):
        preflight.require_rehearsal(tmp_path, "deadbeef" * 8, fork.BOX_ONLY)


def test_a_rehearsal_of_one_policy_does_not_authorise_another(tmp_path: pathlib.Path):
    sha = "abc123" + "0" * 58
    preflight.write_receipt(
        tmp_path,
        {"ok": True, "fork_policy": fork.BOX_ONLY, "source": {"sha256": sha}},
    )
    assert preflight.require_rehearsal(tmp_path, sha, fork.BOX_ONLY)["ok"]
    with pytest.raises(preflight.PreflightRefusal, match="no rehearsal receipt"):
        preflight.require_rehearsal(tmp_path, sha, fork.MERGE)


def test_a_moved_source_fails_the_run(source: pathlib.Path):
    before = preflight.fingerprint(source)
    conn = sqlite3.connect(source)
    conn.execute("INSERT INTO benchmark VALUES ('X', '2026-01-01', 1.0)")
    conn.commit()
    conn.close()
    with pytest.raises(preflight.PreflightRefusal, match="changed while the migration"):
        preflight.assert_source_unmoved(before, source)


# ------------------------------------------------------------------ the fork decision


def test_the_fork_policy_has_no_default():
    with pytest.raises(SystemExit):
        migrate_desk.main(["rehearse", "--sqlite", "x", "--postgres", "y"])


def test_an_unknown_fork_policy_is_refused(source: pathlib.Path):
    with pytest.raises(MigrationFailure, match="unknown fork policy"):
        migrate_desk.migrate(
            sqlite_path=str(source), postgres_url="postgresql://unused", schema="s",
            fork_policy="whatever-maulik-said", drop_existing=True, commit=False,
        )


def test_orphans_are_discovered_by_a_stable_key(source: pathlib.Path, secondary: pathlib.Path):
    conn = open_readonly(str(source))
    by_name = {t.name: t for t in read_schema(conn).tables}
    store = fork.open_sqlite_secondary("B", str(secondary))
    found = fork.discover(conn, by_name, [store])
    assert [row[0] for _s, row in found.plans] == ["zzzz"], "the shared plan is not an orphan"
    assert len(found.orders) == 2
    store.close()
    conn.close()


def test_a_plan_present_in_two_stores_contributes_its_orders_once(
    source: pathlib.Path, secondary: pathlib.Path, tmp_path: pathlib.Path
):
    """`39efc9eefca7` is in B *and* in C; carrying both copies gives a 2-order plan 4 orders."""
    twin = tmp_path / "forked2.db"
    twin.write_bytes(secondary.read_bytes())
    conn = open_readonly(str(source))
    by_name = {t.name: t for t in read_schema(conn).tables}
    stores = [fork.open_sqlite_secondary("B", str(secondary)),
              fork.open_sqlite_secondary("C", str(twin))]
    found = fork.discover(conn, by_name, stores)
    assert len(found.plans) == 1 and len(found.orders) == 2
    assert found.also_seen_in == {"zzzz": {"C": 2}}, "the duplicate is reported, not merged"
    for s in stores:
        s.close()
    conn.close()


def test_orphan_order_ids_are_reissued_above_the_high_water_mark():
    columns = ("id", "version_id", "symbol")
    orders = [("B", (7, "zzzz", "WIPRO")), ("C", (None, "zzzz", "HCLTECH"))]
    remapped, ledger, top = fork.remap_order_ids(orders, columns, start_above=479)
    assert [r[1][0] for r in remapped] == [480, 481] and top == 481
    assert ledger[0]["old_id"] == 7 and ledger[1]["old_id"] is None


def test_a_secondary_that_is_not_a_desk_database_is_refused(
    source: pathlib.Path, tmp_path: pathlib.Path
):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    conn = open_readonly(str(source))
    by_name = {t.name: t for t in read_schema(conn).tables}
    store = fork.open_sqlite_secondary("X", str(empty))
    with pytest.raises(fork.ForkRefusal, match="not a copy of the desk"):
        fork.discover(conn, by_name, [store])
    store.close()
    conn.close()


# ------------------------------------------------------------------ end to end, on Postgres


def _migrate(source: pathlib.Path, schema: str, **kwargs: object) -> dict:
    defaults = dict(
        sqlite_path=str(source), postgres_url=DSN, schema=schema, fork_policy=fork.BOX_ONLY,
        drop_existing=True, commit=True,
    )
    defaults.update(kwargs)
    return migrate_desk.migrate(**defaults)


def _drop(schema: str) -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}_quarantine" CASCADE')


@pytest.fixture()
def schema_name(request):
    name = "desk_test_" + request.node.name[5:45].replace("[", "_").replace("]", "")
    _drop(name)
    yield name
    _drop(name)


@needs_pg
def test_a_clean_migration_asserts_every_table(source: pathlib.Path, schema_name: str):
    report = _migrate(source, schema_name)
    assert report["ok"] and report["committed"]
    assert {t["table"] for t in report["assertions"]["tables"]} == {
        "benchmark", "cashflows", "fills", "option_arms", "rebalance_orders",
        "rebalance_versions", "regime_evaluations", "snapshots",
    }
    assert all(t["row_count_ok"] and t["checksum_ok"] for t in report["assertions"]["tables"])


@needs_pg
def test_a_row_count_mismatch_fails_the_run_and_commits_nothing(
    source: pathlib.Path, schema_name: str, monkeypatch
):
    """The load is sabotaged mid-transaction. The run must fail and leave no schema behind."""
    real = migrate_desk._read_back

    def lossy(cur: object, schema: str, table: object) -> list:
        rows = real(cur, schema, table)
        return rows[:-1] if table.name == "fills" and rows else rows

    monkeypatch.setattr(migrate_desk, "_read_back", lossy)
    report = _migrate(source, schema_name)
    assert not report["ok"] and not report["committed"]
    assert any("fills: row_count" in f for f in report["assertions"]["failures"])
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        left = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
            (schema_name,),
        ).fetchone()[0]
    assert left == 0, "a failed run must roll back the whole schema"


@needs_pg
def test_a_checksum_mismatch_fails_the_run(
    source: pathlib.Path, schema_name: str, monkeypatch
):
    real = migrate_desk._read_back

    def tampered(cur: object, schema: str, table: object) -> list:
        rows = real(cur, schema, table)
        if table.name == "snapshots" and rows:
            first = list(rows[0])
            first[1] = decimal.Decimal("1.00")   # a NAV of one rupee
            rows = [tuple(first), *rows[1:]]
        return rows

    monkeypatch.setattr(migrate_desk, "_read_back", tampered)
    report = _migrate(source, schema_name)
    assert not report["ok"] and not report["committed"]
    assert any("snapshots: checksum" in f for f in report["assertions"]["failures"])


@needs_pg
def test_money_is_numeric_in_the_catalogue_and_the_value_is_unchanged(
    source: pathlib.Path, schema_name: str
):
    """Gate G5, and the proof that `numeric` did not change a single price."""
    report = _migrate(source, schema_name)
    assert report["ok"]
    types = report["semantic"]["column_types"]
    assert "fills.price" in types["numeric"] and "snapshots.nav" in types["numeric"]
    assert "fills.when_ts" in types["double_precision_by_design"]
    assert types["mismatches"] == []
    import psycopg

    with psycopg.connect(DSN) as conn:
        rows = conn.execute(
            f'SELECT price FROM "{schema_name}".fills ORDER BY trade_id'
        ).fetchall()
        kind = conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='fills' AND column_name='price'",
            (schema_name,),
        ).fetchone()[0]
    assert kind == "numeric"
    assert sorted(float(r[0]) for r in rows) == sorted(PRICES), (
        "every price must come back as the identical float64 the desk computed on (M19.3)"
    )


@needs_pg
def test_float_money_reproduces_the_old_behaviour_when_asked(
    source: pathlib.Path, schema_name: str
):
    report = _migrate(source, schema_name, float_money=True)
    assert report["ok"]
    assert report["semantic"]["column_types"]["numeric"] == []


@needs_pg
def test_an_identity_column_cannot_take_a_null(source: pathlib.Path, schema_name: str):
    """§5.5.1: 67 rows of store C's rebalance_orders have id IS NULL. Here it is impossible."""
    import psycopg

    assert _migrate(source, schema_name)["ok"]
    with psycopg.connect(DSN, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute(
                f'INSERT INTO "{schema_name}".rebalance_orders(id, version_id, symbol) '
                "VALUES (NULL, 'aaaa', 'X')"
            )
        # And an insert that omits the id gets one, rather than writing NULL.
        got = conn.execute(
            f'INSERT INTO "{schema_name}".rebalance_orders(version_id, symbol) '
            "VALUES ('aaaa', 'X') RETURNING id"
        ).fetchone()[0]
    assert got > 3


@needs_pg
def test_the_sequence_starts_above_the_sqlite_high_water_mark(
    source: pathlib.Path, schema_name: str
):
    """§5.4: seq 2 against max(id) 1 — the first new row must not reuse id 2."""
    import psycopg

    assert _migrate(source, schema_name)["ok"]
    with psycopg.connect(DSN, autocommit=True) as conn:
        new_id = conn.execute(
            f'INSERT INTO "{schema_name}".regime_evaluations(evaluation_id, run_id) '
            "VALUES ('e3','preview') RETURNING id"
        ).fetchone()[0]
    assert new_id == 3, "id 2 belonged to a deleted row and must never be reissued"


@needs_pg
def test_the_uniques_survive_so_ingestion_stays_idempotent(
    source: pathlib.Path, schema_name: str
):
    """§5.5.2 / house rule 7: `INSERT … ON CONFLICT DO NOTHING` needs something to conflict on."""
    import psycopg

    assert _migrate(source, schema_name)["ok"]
    with psycopg.connect(DSN, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                f'INSERT INTO "{schema_name}".regime_evaluations(evaluation_id, run_id) '
                "VALUES ('e1','preview')"
            )
        before = conn.execute(
            f'SELECT count(*) FROM "{schema_name}".snapshots'
        ).fetchone()[0]
        conn.execute(
            f'INSERT INTO "{schema_name}".snapshots(date, nav, holdings_json) '
            "VALUES ('2026-08-10', 1.0, '{}') ON CONFLICT DO NOTHING"
        )
        after = conn.execute(f'SELECT count(*) FROM "{schema_name}".snapshots').fetchone()[0]
    assert before == after


@needs_pg
def test_the_partial_unique_index_is_enforced(source: pathlib.Path, schema_name: str):
    """§5.5.3: 'a regime evaluation is committed exactly once'."""
    import psycopg

    assert _migrate(source, schema_name)["ok"]
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            f'INSERT INTO "{schema_name}".regime_evaluations(evaluation_id, run_id) '
            "VALUES ('e9','canonical')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                f'INSERT INTO "{schema_name}".regime_evaluations(evaluation_id, run_id) '
                "VALUES ('e9','canonical')"
            )
        # …but two previews of the same evaluation are still allowed, as in SQLite.
        conn.execute(
            f'INSERT INTO "{schema_name}".regime_evaluations(evaluation_id, run_id) '
            "VALUES ('e9','preview')"
        )


@needs_pg
def test_the_foreign_key_is_enforced(source: pathlib.Path, schema_name: str):
    import psycopg

    assert _migrate(source, schema_name)["ok"]
    with psycopg.connect(DSN, autocommit=True) as conn, pytest.raises(
        psycopg.errors.ForeignKeyViolation
    ):
        conn.execute(
            f'INSERT INTO "{schema_name}".rebalance_orders(version_id, symbol) '
            "VALUES ('no-such-plan', 'X')"
        )


@needs_pg
def test_rerunning_produces_identical_rows(source: pathlib.Path, schema_name: str):
    """House rule 7, applied to the migrator itself."""
    first = _migrate(source, schema_name)
    second = _migrate(source, schema_name)
    assert first["ok"] and second["ok"]
    assert first["schema_digest"] == second["schema_digest"]
    assert (first["assertions"]["row_count_total_target"]
            == second["assertions"]["row_count_total_target"])


@needs_pg
def test_a_populated_schema_is_refused_without_drop_existing(
    source: pathlib.Path, schema_name: str
):
    assert _migrate(source, schema_name)["ok"]
    with pytest.raises(MigrationFailure, match="already holds"):
        _migrate(source, schema_name, drop_existing=False)


@needs_pg
def test_without_commit_the_target_is_untouched(source: pathlib.Path, schema_name: str):
    import psycopg

    report = _migrate(source, schema_name, commit=False)
    assert report["ok"] and not report["committed"]
    with psycopg.connect(DSN, autocommit=True) as conn:
        left = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
            (schema_name,),
        ).fetchone()[0]
    assert left == 0


@needs_pg
def test_verify_reasserts_a_migrated_schema_read_only(source: pathlib.Path, schema_name: str):
    applied = _migrate(source, schema_name)
    checked = migrate_desk.verify(sqlite_path=str(source), postgres_url=DSN, schema=schema_name)
    assert checked["ok"]
    assert checked["schema_digest"] == applied["schema_digest"]
    assert checked["provenance"]["source_sha256"] == applied["source"]["sha256"]


@needs_pg
def test_box_only_leaves_the_orphans_out(
    source: pathlib.Path, secondary: pathlib.Path, schema_name: str
):
    import psycopg

    store = fork.open_sqlite_secondary("B", str(secondary))
    report = _migrate(source, schema_name, fork_policy=fork.BOX_ONLY, secondaries=[store])
    store.close()
    assert report["ok"] and report["fork"]["plans"] == 1
    with psycopg.connect(DSN) as conn:
        n = conn.execute(
            f"SELECT count(*) FROM \"{schema_name}\".rebalance_versions WHERE version_id='zzzz'"
        ).fetchone()[0]
        quarantined = conn.execute(
            "SELECT count(*) FROM information_schema.schemata WHERE schema_name = %s",
            (schema_name + "_quarantine",),
        ).fetchone()[0]
    assert n == 0 and quarantined == 0


@needs_pg
def test_quarantine_preserves_the_orphans_outside_the_live_schema(
    source: pathlib.Path, secondary: pathlib.Path, schema_name: str
):
    import psycopg

    store = fork.open_sqlite_secondary("B", str(secondary))
    report = _migrate(source, schema_name, fork_policy=fork.QUARANTINE, secondaries=[store])
    store.close()
    assert report["ok"]
    with psycopg.connect(DSN) as conn:
        live = conn.execute(
            f"SELECT count(*) FROM \"{schema_name}\".rebalance_versions WHERE version_id='zzzz'"
        ).fetchone()[0]
        held = conn.execute(
            f'SELECT version_id, _orphan_source FROM "{schema_name}_quarantine".'
            "rebalance_versions"
        ).fetchall()
        orders = conn.execute(
            f'SELECT count(*) FROM "{schema_name}_quarantine".rebalance_orders'
        ).fetchone()[0]
    assert live == 0
    assert held == [("zzzz", "B")]
    assert orders == 2


@needs_pg
def test_merge_carries_the_orphans_with_reissued_ids(
    source: pathlib.Path, secondary: pathlib.Path, schema_name: str
):
    import psycopg

    store = fork.open_sqlite_secondary("B", str(secondary))
    report = _migrate(source, schema_name, fork_policy=fork.MERGE, secondaries=[store])
    store.close()
    assert report["ok"]
    assert len(report["fork"]["id_remap"]) == 2
    with psycopg.connect(DSN, autocommit=True) as conn:
        plans = conn.execute(
            f'SELECT count(*) FROM "{schema_name}".rebalance_versions'
        ).fetchone()[0]
        ids = [
            r[0] for r in conn.execute(
                f"SELECT id FROM \"{schema_name}\".rebalance_orders WHERE version_id='zzzz' "
                "ORDER BY id"
            )
        ]
        # The sequence must clear the reissued ids too, or the next insert collides.
        nxt = conn.execute(
            f'INSERT INTO "{schema_name}".rebalance_orders(version_id, symbol) '
            "VALUES ('aaaa','X') RETURNING id"
        ).fetchone()[0]
    assert plans == 2
    assert len(ids) == 2 and min(ids) > 3
    assert nxt > max(ids)


@needs_pg
def test_a_fill_the_box_does_not_have_stops_the_migration(
    source: pathlib.Path, secondary: pathlib.Path, schema_name: str
):
    """The tripwire. A fill on a laptop and not on the box is a finding, never a merge."""
    conn = sqlite3.connect(secondary)
    conn.execute("INSERT INTO fills VALUES ('GHOST','X',1.0,1.0,0.0)")
    conn.commit()
    conn.close()
    store = fork.open_sqlite_secondary("B", str(secondary))
    with pytest.raises(MigrationFailure, match="fills"):
        _migrate(source, schema_name, fork_policy=fork.QUARANTINE, secondaries=[store])
    store.close()


@needs_pg
def test_rehearse_writes_a_receipt_and_leaves_no_schema(
    source: pathlib.Path, schema_name: str, tmp_path: pathlib.Path
):
    import psycopg

    rc = migrate_desk.main([
        "rehearse", "--sqlite", str(source), "--postgres", DSN, "--schema", schema_name,
        "--fork-policy", fork.BOX_ONLY, "--receipts-dir", str(tmp_path),
    ])
    assert rc == 0
    receipts = list(tmp_path.glob("rehearsal-*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["ok"] and receipt["fork_policy"] == fork.BOX_ONLY
    assert receipt["source"]["sha256"] == preflight.fingerprint(source).sha256
    with psycopg.connect(DSN, autocommit=True) as conn:
        for name in (schema_name, schema_name + "_rehearsal"):
            left = conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                (name,),
            ).fetchone()[0]
            assert left == 0
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}_rehearsal" CASCADE')


@needs_pg
def test_apply_refuses_when_the_rehearsal_was_of_a_different_file(
    source: pathlib.Path, schema_name: str, tmp_path: pathlib.Path
):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"verified": {"integrity": "ok"}}))
    rc = migrate_desk.main([
        "apply", "--sqlite", str(source), "--postgres", DSN, "--schema", schema_name,
        "--fork-policy", fork.BOX_ONLY, "--receipts-dir", str(tmp_path),
        "--backup-manifest", str(manifest), "--archive-copy", str(source),
        "--drop-existing",
    ])
    assert rc == 1, "no receipt exists for this source, so apply must refuse"


# ------------------------------------------------------------------ column defaults


def test_literal_defaults_are_carried(source: pathlib.Path):
    """Ten live columns have a DEFAULT and the desk's INSERTs omit them."""
    conn = open_readonly(str(source))
    schema = read_schema(conn)
    evaluations = schema.table("regime_evaluations")
    committed = next(c for c in evaluations.columns if c.name == "committed")
    assert pgddl.default_clause("regime_evaluations", committed) == " DEFAULT 0"
    assert '"committed" bigint DEFAULT 0 NOT NULL' in pgddl.create_table("desk", evaluations)
    conn.close()


def test_a_default_expression_this_tool_cannot_reproduce_is_refused(tmp_path: pathlib.Path):
    """CURRENT_TIMESTAMP means a UTC text string in SQLite and a timestamptz in Postgres."""
    path = tmp_path / "d.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(a TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()
    ro = open_readonly(str(path))
    table = read_schema(ro).table("t")
    with pytest.raises(SchemaRefusal, match="CURRENT_TIMESTAMP"):
        pgddl.create_table("desk", table)
    ro.close()


def test_a_text_default_keeps_its_quotes(tmp_path: pathlib.Path):
    path = tmp_path / "d2.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(status TEXT DEFAULT 'PENDING')")
    conn.commit()
    conn.close()
    ro = open_readonly(str(path))
    assert "DEFAULT 'PENDING'" in pgddl.create_table("desk", read_schema(ro).table("t"))
    ro.close()


@needs_pg
def test_the_desks_insert_shape_still_works_after_migration(
    source: pathlib.Path, schema_name: str
):
    """`INSERT INTO regime_evaluations(evaluation_id, run_id) …` is the desk's own shape."""
    import psycopg

    assert _migrate(source, schema_name)["ok"]
    with psycopg.connect(DSN, autocommit=True) as conn:
        row = conn.execute(
            f'INSERT INTO "{schema_name}".regime_evaluations(evaluation_id, run_id) '
            "VALUES ('e77','preview') RETURNING id, committed"
        ).fetchone()
    assert row[1] == 0, "the DEFAULT must survive the migration, or the flag lands NULL"
