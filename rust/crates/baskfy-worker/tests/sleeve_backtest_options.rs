//! `test_options_backtest_task.py::TestTheTask`: `baskfy.options.backtest` is ported, on the
//! compute queue, never on Beat, and its argument refusals come before any database session.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_db::{OrderedJson, WriteDb};
use baskfy_worker::settings::WorkerSettings;
use baskfy_worker::tasks::beat::BEAT_SCHEDULE;
use baskfy_worker::tasks::context::TaskClock;
use baskfy_worker::tasks::{Queue, TaskContext, find, run_task};
use chrono::NaiveDateTime;

const TASK: &str = "baskfy.options.backtest";

#[test]
fn on_the_compute_queue_and_never_on_beat() {
    let spec = find(TASK).unwrap();
    assert!(spec.is_ported());
    assert_eq!(spec.queue_option, Some(Queue::Compute));
    assert!(BEAT_SCHEDULE.iter().all(|e| e.task != TASK));
    let names: Vec<&str> = spec.params.iter().map(|p| p.name).collect();
    assert_eq!(names, ["sleeve", "tier", "date_from", "date_to"]);
}

fn refusal(keywords: Vec<(&str, OrderedJson)>) -> (String, String) {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let _entered = runtime.enter();
    let settings = baskfy_api::Settings::default();
    let db = WriteDb::connect_lazy(&settings.db_settings()).unwrap();
    let now = NaiveDateTime::parse_from_str("2026-10-30T10:00", "%Y-%m-%dT%H:%M").unwrap();
    let ctx = TaskContext::new(
        db,
        settings,
        WorkerSettings::default(),
        TaskClock::starting_at_ist(now),
        None,
        None,
    );
    let keywords = keywords
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v))
        .collect();
    match runtime.block_on(run_task(&ctx, TASK, Vec::new(), keywords)) {
        Err(e) => (e.python_type(), e.to_string()),
        Ok(v) => ("ok".to_owned(), baskfy_worker::pyjson::dumps(&v).unwrap()),
    }
}

fn str_(v: &str) -> OrderedJson {
    OrderedJson::Str(v.to_owned())
}

#[test]
fn a_date_that_is_not_a_string_is_cpythons_type_error() {
    let got = refusal(vec![
        ("sleeve", str_("O2")),
        ("tier", str_("1")),
        ("date_from", OrderedJson::Int(20_261_001)),
    ]);
    assert_eq!(
        got,
        (
            "TypeError".to_owned(),
            "fromisoformat: argument must be str".to_owned()
        )
    );
}

#[test]
fn a_bad_date_is_refused_before_the_tenant_is_read() {
    let got = refusal(vec![
        ("sleeve", str_("O2")),
        ("tier", str_("1")),
        ("date_from", str_("2026-01-01")),
        ("date_to", str_("2026/09/01")),
    ]);
    assert_eq!(
        got,
        (
            "ValueError".to_owned(),
            "Invalid isoformat string: '2026/09/01'".to_owned()
        )
    );
}

#[test]
fn refused_without_a_tenant() {
    if std::env::var("BASKFY_SOLE_USER_ID").is_ok_and(|v| !v.trim().is_empty()) {
        return;
    }
    let got = refusal(vec![
        ("sleeve", str_("O2")),
        ("tier", str_("1")),
        ("date_from", str_("2026-01-01")),
        ("date_to", str_("2026-09-01")),
    ]);
    assert_eq!(
        got,
        (
            "ok".to_owned(),
            r#"{"from": "2026-01-01", "skipped": "no BASKFY_SOLE_USER_ID configured"}"#.to_owned()
        )
    );
}
