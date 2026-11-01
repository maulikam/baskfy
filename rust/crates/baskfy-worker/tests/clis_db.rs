//! The sleeve and pipeline CLIs against a throwaway clone of the bed, through the binary: a
//! re-run changes nothing (house rule 7), a detected session is answered without work, and a
//! non-trading day aborts the pipeline.
//!
//! Run: `BASKFY_RS_CLIS_DB_URL=postgres://…/rst_… cargo test -p baskfy-worker --test clis_db --
//! --ignored`. No provider is called.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::process::{Command, Output};

use baskfy_db::OrderedJson;
use sqlx::{Connection as _, PgConnection};

const ENV: &str = "BASKFY_RS_CLIS_DB_URL";
const DAY: &str = "2026-10-29";

fn url() -> String {
    let url = std::env::var(ENV).unwrap_or_else(|_| panic!("needs {ENV} (an rst_* clone)"));
    let name = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    assert!(
        name.starts_with("rst_"),
        "refusing the database {name:?}: only rst_* clones"
    );
    url
}

fn worker(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_baskfy-worker"))
        .args(args)
        .env("BASKFY_DATABASE_URL", url())
        .env("BASKFY_SOLE_USER_ID", "1")
        .env("DRY_RUN", "true")
        .env("BASKFY_REDIS_URL", "redis://127.0.0.1:56379/9")
        .env(
            "BASKFY_KITE_TOKEN_PATH",
            "/nonexistent/baskfy-clis/kite-token.enc",
        )
        .output()
        .unwrap()
}

fn json(out: &Output) -> OrderedJson {
    OrderedJson::parse(String::from_utf8_lossy(&out.stdout).trim()).unwrap_or_else(|e| {
        panic!(
            "stdout is not JSON ({e}); stderr: {}",
            String::from_utf8_lossy(&out.stderr)
        )
    })
}

async fn count(sql: &str) -> i64 {
    let mut conn = PgConnection::connect(&url()).await.unwrap();
    let n: i64 = sqlx::query_scalar(sql).fetch_one(&mut conn).await.unwrap();
    conn.close().await.unwrap();
    n
}

fn sessions(payload: &OrderedJson) -> Vec<OrderedJson> {
    match payload.get("sessions") {
        Some(OrderedJson::Array(items)) => items.clone(),
        other => panic!("no sessions: {other:?}"),
    }
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn swing_detection_is_idempotent_per_date() {
    let first = worker(&["swing", "--date", DAY]);
    assert_eq!(
        first.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    let rows = "SELECT count(*) FROM sw_setup_daily WHERE user_id = 1 AND date = '2026-10-29'";
    let after_first = count(rows).await;
    let second = worker(&["swing_cli", "--date", DAY]);
    assert_eq!(second.status.code(), Some(0));
    assert_eq!(count(rows).await, after_first);
    let (a, b) = (json(&first), json(&second));
    assert_eq!(a.get("user_id"), Some(&OrderedJson::Int(1)));
    let pick = |p: &OrderedJson| {
        sessions(p)
            .iter()
            .map(|s| {
                (
                    s.get("date").cloned(),
                    s.get("candidates").cloned(),
                    s.get("status").cloned(),
                )
            })
            .collect::<Vec<_>>()
    };
    assert_eq!(pick(&a), pick(&b));
    assert_eq!(pick(&a).len(), 1);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn a_detected_session_is_answered_without_work_unless_forced() {
    let out = worker(&["twt", "--date", DAY]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let first = &sessions(&json(&out))[0];
    assert_eq!(
        first.get("skipped"),
        Some(&OrderedJson::Str("already detected".to_owned()))
    );
    let out = worker(&["vbt", "--date", DAY]);
    assert_eq!(out.status.code(), Some(0));
    let first = &sessions(&json(&out))[0];
    assert_eq!(
        first.get("skipped"),
        Some(&OrderedJson::Str("already detected".to_owned()))
    );
    let signals = "SELECT count(*) FROM tw_signal_daily WHERE user_id = 1 AND date = '2026-10-29'";
    let before = count(signals).await;
    let forced = worker(&["twt", "--date", DAY, "--force"]);
    assert_eq!(forced.status.code(), Some(0));
    let again = worker(&["twt", "--date", DAY, "--force"]);
    assert_eq!(count(signals).await, before);
    let strip = |p: &OrderedJson| {
        sessions(p)
            .iter()
            .map(|s| (s.get("date").cloned(), s.get("signals").cloned()))
            .collect::<Vec<_>>()
    };
    assert_eq!(strip(&json(&forced)), strip(&json(&again)));
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn sessions_walks_the_calendar_not_the_clock() {
    let out = worker(&["vbt", "--date", DAY, "--sessions", "3"]);
    assert_eq!(out.status.code(), Some(0));
    let dates: Vec<OrderedJson> = sessions(&json(&out))
        .iter()
        .map(|s| s.get("date").cloned().unwrap())
        .collect();
    let expected: Vec<OrderedJson> = ["2026-10-27", "2026-10-28", "2026-10-29"]
        .iter()
        .map(|d| OrderedJson::Str((*d).to_owned()))
        .collect();
    let trading = count(
        "SELECT count(*) FROM trading_day WHERE is_trading_day AND date BETWEEN '2026-10-27' \
         AND '2026-10-29'",
    )
    .await;
    if trading == 3 {
        assert_eq!(dates, expected);
    }
    assert_eq!(dates.len(), 3);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn the_pipeline_aborts_a_non_trading_day() {
    let runs = "SELECT count(*) FROM pipeline_run";
    let before = count(runs).await;
    let out = worker(&["pipeline", "--date", "2026-10-25"]);
    assert_eq!(
        out.status.code(),
        Some(1),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(text.starts_with("2026-10-25: aborted\n  error: "), "{text}");
    let out = worker(&["pipeline", "--date", "2026-10-25", "--json"]);
    assert_eq!(out.status.code(), Some(1));
    let payload = json(&out);
    let OrderedJson::Object(entries) = &payload else {
        panic!("not an object");
    };
    let keys: Vec<&str> = entries.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(
        keys,
        [
            "data_version",
            "error",
            "failed_step",
            "gate",
            "status",
            "steps_completed",
            "trade_date"
        ]
    );
    assert_eq!(
        payload.get("status"),
        Some(&OrderedJson::Str("aborted".to_owned()))
    );
    assert_eq!(payload.get("gate"), Some(&OrderedJson::Null));
    assert_eq!(count(runs).await, before);
}

fn sleeves(payload: &OrderedJson) -> Vec<OrderedJson> {
    match payload.get("sleeves") {
        Some(OrderedJson::Array(items)) => items.clone(),
        other => panic!("no sleeves: {other:?}"),
    }
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn the_options_plan_builders_decide_a_session_once() {
    for (command, at, labels) in [
        ("plan", "2026-10-29T10:00", 2),
        ("plan-o2", "2026-10-29T10:30", 1),
        ("plan-o3", "2026-10-29T09:45", 2),
    ] {
        let first = worker(&["options", command, "--at", at]);
        assert_eq!(
            first.status.code(),
            Some(0),
            "{command}: {}",
            String::from_utf8_lossy(&first.stderr)
        );
        let a = json(&first);
        assert_eq!(
            a.get("at"),
            Some(&OrderedJson::Str(format!("{at}:00+05:30"))),
            "{command}"
        );
        assert!(
            matches!(a.get("alerts"), Some(OrderedJson::Array(_))),
            "{command}"
        );
        assert!(
            matches!(a.get("lapsed"), Some(OrderedJson::Array(_))),
            "{command}"
        );
        assert_eq!(sleeves(&a).len(), labels, "{command}");
        let sessions = count("SELECT count(*) FROM op_plan").await;
        let second = json(&worker(&["options", command, "--at", at]));
        assert_eq!(
            sleeves(&a),
            sleeves(&second),
            "{command}: a decided session changed"
        );
        assert_eq!(
            count("SELECT count(*) FROM op_plan").await,
            sessions,
            "{command}"
        );
    }
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn an_options_plan_on_a_sunday_is_skipped() {
    let out = worker(&["options", "plan-o2", "--at", "2026-10-25T10:30"]);
    assert_eq!(out.status.code(), Some(0));
    assert_eq!(
        json(&out),
        OrderedJson::Object(vec![(
            "skipped".to_owned(),
            OrderedJson::Str("not an NSE trading day".to_owned())
        )])
    );
}
