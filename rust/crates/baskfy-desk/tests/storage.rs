//! `app.analytics.db` + `app.analytics.pg`: the migrations as the Python holds them, the
//! dialect translation (`tests/test_pg_adapter.py`, ported), psycopg's parameter rendering,
//! and a real SQLite desk database migrated, written and rolled back.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_desk::storage::{
    Db, DbConfig, DbValue, MIGRATIONS, Param, StorageError, pg_render, placeholders, translate,
};

#[test]
fn the_migrations_are_the_pythons_verbatim() {
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("../fixtures/desk_migrations.json")).unwrap();
    let py = fixture.as_object().unwrap();
    assert_eq!(py.len(), MIGRATIONS.len());
    for (version, statements) in MIGRATIONS {
        let want: Vec<&str> = py[&version.to_string()]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s.as_str().unwrap())
            .collect();
        assert_eq!(statements.to_vec(), want, "migration {version}");
    }
}

// --- tests/test_pg_adapter.py ----------------------------------------------------------------

#[test]
fn placeholders_ordinary_quoted_escaped_and_percent() {
    assert_eq!(
        placeholders("select * from t where a=? and b=?"),
        "select * from t where a=%s and b=%s"
    );
    assert_eq!(
        placeholders("select * from t where note = 'why?'"),
        "select * from t where note = 'why?'"
    );
    assert_eq!(
        placeholders("select * from t where s = 'it''s ok?' and a = ?"),
        "select * from t where s = 'it''s ok?' and a = %s"
    );
    assert_eq!(
        placeholders("select * from t where s like '%x%'"),
        "select * from t where s like '%%x%%'"
    );
    assert!(placeholders("select 10 % 3").contains("%%"));
}

#[test]
fn dialect_autoincrement_conflicts_strftime() {
    let out = translate("create table t (id INTEGER PRIMARY KEY AUTOINCREMENT, x text)").unwrap();
    assert!(out.contains("BIGSERIAL PRIMARY KEY") && !out.contains("AUTOINCREMENT"));
    for spelling in [
        "INSERT OR IGNORE INTO t VALUES (?)",
        "INSERT OR REPLACE INTO t VALUES (?)",
    ] {
        let err = translate(spelling).unwrap_err();
        assert!(
            matches!(err, StorageError::Untranslatable(ref m) if m.contains("no Postgres equivalent")),
            "{err}"
        );
    }
    assert_eq!(
        translate("INSERT INTO fills(trade_id) VALUES(?) ON CONFLICT(trade_id) DO NOTHING")
            .unwrap(),
        "INSERT INTO fills(trade_id) VALUES(%s) ON CONFLICT(trade_id) DO NOTHING"
    );
    let out = translate("select strftime('%Y-%m', trade_date) from trades").unwrap();
    assert!(out.contains("to_char((trade_date)::date, 'YYYY-MM')") && !out.contains("strftime"));
    let err = translate("select strftime('%j', d) from t").unwrap_err();
    assert!(err.to_string().contains("no Postgres translation"));
}

#[test]
fn translation_is_not_applied_twice() {
    let once = translate("select * from t where a=? and s like '%x%'").unwrap();
    assert!(once.contains("%s"));
    assert_eq!(once.matches("%%").count(), 2);
}

#[test]
fn lowercase_and_mixed_case_autoincrement_is_rewritten_like_re_i() {
    assert_eq!(
        translate("id integer primary key autoincrement").unwrap(),
        "id BIGSERIAL PRIMARY KEY"
    );
}

// --- psycopg's view of parameters --------------------------------------------------------------

#[test]
fn parameters_render_as_psycopg_sends_them() {
    let sql =
        translate("select * from t where a = ? and b = ? and c like '%x' and d = ? and e = ?")
            .unwrap();
    let out = pg_render(
        &sql,
        &[
            Param::Text("it's".into()),
            Param::Int(-3),
            Param::Float(0.1),
            Param::Null,
        ],
    )
    .unwrap();
    assert_eq!(
        out,
        "select * from t where a = 'it''s' and b = -3 and c like '%x' and d = '0.1'::float8 and e = NULL"
    );
    // With no parameters psycopg does no `%` processing: the doubled percent reaches the server.
    assert_eq!(
        pg_render(&translate("select '%'").unwrap(), &[]).unwrap(),
        "select '%%'"
    );
    assert!(
        pg_render("select %s", &[Param::Text("a\0b".into())]).is_err(),
        "NUL is refused, as psycopg does"
    );
    assert!(
        pg_render("select %s", &[]).unwrap() == "select %s",
        "no params: sent verbatim"
    );
    assert!(pg_render("select %s, %s", &[Param::Int(1)]).is_err());
    assert!(pg_render("select %s", &[Param::Int(1), Param::Int(2)]).is_err());
    assert_eq!(
        pg_render("select %s", &[Param::Float(f64::NAN)]).unwrap(),
        "select 'NaN'::float8"
    );
}

// --- a real SQLite desk database ----------------------------------------------------------------

fn config(dir: &std::path::Path) -> DbConfig {
    DbConfig {
        backend: "sqlite".to_owned(),
        schema: "desk".to_owned(),
        database_url: String::new(),
        db_path: dir.join("data").join("portfolio.db"),
    }
}

#[tokio::test]
async fn a_sqlite_desk_migrates_to_thirteen_and_reads_like_sqlite3() {
    let tmp = tempfile::tempdir().unwrap();
    let db = Db::open(config(tmp.path())).await.unwrap();
    let mut conn = db.connect().await.unwrap();
    assert_eq!(conn.migrate().await.unwrap(), 13);
    assert_eq!(conn.migrate().await.unwrap(), 13, "idempotent");
    let tables = conn
        .execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
            &[],
        )
        .await
        .unwrap();
    let names: Vec<&str> = tables
        .rows
        .iter()
        .filter_map(|r| r.at(0).and_then(DbValue::as_str))
        .collect();
    for want in [
        "snapshots",
        "cashflows",
        "rebalance_versions",
        "rebalance_orders",
        "trades",
        "fills",
        "settings",
        "ops_jobs",
    ] {
        assert!(names.contains(&want), "{want} missing from {names:?}");
    }
    let ins = conn
        .execute(
            "INSERT INTO cashflows(date, amount, type) VALUES(?, ?, ?)",
            &[
                Param::from("2026-10-30"),
                Param::Float(-100000.0),
                Param::from("invest"),
            ],
        )
        .await
        .unwrap();
    assert_eq!(ins.rowcount, 1);
    assert!(ins.lastrowid().unwrap().is_some());
    let row = conn
        .execute("SELECT date, amount, type FROM cashflows", &[])
        .await
        .unwrap();
    let r = row.fetchone().unwrap();
    assert_eq!(r.get("date"), Some(&DbValue::Text("2026-10-30".into())));
    assert_eq!(r.at(1), Some(&DbValue::Float(-100000.0)));
    assert!(
        conn.execute("SELECT ?", &[Param::Decimal(rust_decimal::Decimal::ONE)])
            .await
            .is_err(),
        "sqlite3 refuses Decimal"
    );

    let failed: Result<(), StorageError> = conn
        .transaction(|c| {
            Box::pin(async move {
                c.execute(
                    "INSERT INTO cashflows(date, amount, type) VALUES('2026-10-31', 1, 'withdraw')",
                    &[],
                )
                .await?;
                Err(StorageError::Other("boom".into()))
            })
        })
        .await;
    assert!(failed.is_err());
    let count = conn
        .execute("SELECT count(*) AS n FROM cashflows", &[])
        .await
        .unwrap();
    assert_eq!(
        count.fetchone().unwrap().at(0),
        Some(&DbValue::Int(1)),
        "rolled back"
    );
}
