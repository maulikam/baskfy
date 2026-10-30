//! The Rust task registry and Beat table, held equal to the Python worker's Celery surface
//! (`tests/fixtures/python_task_registry.json`, written by `tools/parity/task_registry.py` from the
//! live `celery_app` — DECISIONS-RS R8.1). A task added, renamed, re-routed, re-scheduled or given
//! a new parameter or retry policy in Python fails here until the port follows.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::{BTreeMap, BTreeSet};

use baskfy_worker::settings::WorkerSettings;
use baskfy_worker::tasks::beat::{BEAT_SCHEDULE, Schedule};
use baskfy_worker::tasks::registry::REGISTRY;
use baskfy_worker::tasks::{
    CELERY_DEFAULT_RETRY_DELAY_SECONDS, CELERY_RETRY_BACKOFF_MAX_SECONDS, DEFAULT_QUEUE, Lane,
    PyDefault, QUEUES, TASK_ROUTES, find,
};
use chrono::NaiveDateTime;
use serde_json::{Value, json};

fn fixture() -> Value {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/python_task_registry.json"
    );
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn default_json(d: PyDefault) -> Value {
    match d {
        PyDefault::Required => json!({"required": true}),
        PyDefault::None => json!({"required": false, "default": null}),
        PyDefault::Str(s) => json!({"required": false, "default": s}),
        PyDefault::Int(i) => json!({"required": false, "default": i}),
        PyDefault::Bool(b) => json!({"required": false, "default": b}),
    }
}

#[test]
fn every_python_task_is_registered_and_nothing_else() {
    let fx = fixture();
    let python: BTreeSet<&str> = fx["tasks"]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    let rust: BTreeSet<&str> = REGISTRY.iter().map(|t| t.name).collect();
    assert_eq!(rust.len(), REGISTRY.len(), "a name is registered twice");
    assert_eq!(rust, python);
    assert_eq!(fx["task_count"], json!(REGISTRY.len()));
}

#[test]
fn each_task_matches_python_signature_policy_and_routing() {
    let fx = fixture();
    let worker = WorkerSettings::default();
    for spec in REGISTRY {
        let py = &fx["tasks"][spec.name];
        let ctx = spec.name;
        assert_eq!(py["python"], json!(spec.python), "{ctx}: python function");
        let params: Vec<Value> = spec
            .params
            .iter()
            .map(|p| {
                let mut v = json!({"name": p.name, "annotation": p.annotation});
                for (k, x) in default_json(p.default).as_object().unwrap() {
                    v[k] = x.clone();
                }
                v
            })
            .collect();
        assert_eq!(py["params"], json!(params), "{ctx}: parameters");
        assert_eq!(py["bound"], json!(spec.bound), "{ctx}: bind");
        assert_eq!(py["acks_late"], json!(spec.acks_late), "{ctx}: acks_late");
        assert_eq!(
            py["max_retries"],
            json!(spec.max_retries(&worker)),
            "{ctx}: max_retries"
        );
        assert_eq!(
            py["default_retry_delay"],
            json!(CELERY_DEFAULT_RETRY_DELAY_SECONDS),
            "{ctx}: default_retry_delay"
        );
        assert_eq!(
            py["autoretry_for"],
            json!(spec.autoretry_for),
            "{ctx}: autoretry_for"
        );
        assert_eq!(
            py["retry_backoff"],
            json!(spec.retry_backoff),
            "{ctx}: retry_backoff"
        );
        assert_eq!(
            py["retry_backoff_max"],
            json!(CELERY_RETRY_BACKOFF_MAX_SECONDS),
            "{ctx}"
        );
        assert_eq!(py["retry_jitter"], json!(true), "{ctx}: retry_jitter");
        assert_eq!(
            py["time_limit"],
            json!(spec.time_limit),
            "{ctx}: time_limit"
        );
        assert_eq!(py["soft_time_limit"], Value::Null, "{ctx}: soft_time_limit");
        assert_eq!(
            py["queue_option"],
            json!(spec.queue_option.map(|q| q.name())),
            "{ctx}: queue option"
        );
        assert_eq!(
            py["queue_by_name"],
            json!(spec.queue_by_name().name()),
            "{ctx}: queue by name"
        );
        assert_eq!(
            py["queue_via_task"],
            json!(spec.queue_via_task().name()),
            "{ctx}: queue via task"
        );
    }
}

#[test]
fn routes_queues_and_app_configuration_match() {
    let fx = fixture();
    let routes: Vec<(String, String)> = fx["task_routes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|pair| {
            (
                pair[0].as_str().unwrap().to_owned(),
                pair[1].as_str().unwrap().to_owned(),
            )
        })
        .collect();
    let rust: Vec<(String, String)> = TASK_ROUTES
        .iter()
        .map(|(k, q)| ((*k).to_owned(), q.name().to_owned()))
        .collect();
    assert_eq!(routes, rust);
    let queues: Vec<&str> = QUEUES.iter().map(|q| q.name()).collect();
    assert_eq!(fx["queues"], json!(queues));
    let conf = &fx["conf"];
    assert_eq!(conf["timezone"], json!("Asia/Kolkata"));
    assert_eq!(conf["enable_utc"], json!(false));
    assert_eq!(conf["task_default_queue"], json!(DEFAULT_QUEUE.name()));
    assert_eq!(conf["task_acks_late"], json!(true));
    // The configured delay (`BASKFY_TASK_RETRY_BACKOFF_SECONDS`, 60) never reaches a task:
    // Celery's class default (180) wins. Pinned both ways, so a Celery upgrade that changes it
    // shows up here.
    assert_eq!(
        conf["task_default_retry_delay"],
        json!(WorkerSettings::default().task_retry_backoff_seconds)
    );
    assert_eq!(
        conf["task_annotations"],
        json!({"*": {"max_retries": 3, "max_redeliveries": 3}})
    );
}

#[test]
fn the_beat_schedule_matches_python_entry_by_entry() {
    let fx = fixture();
    let py = fx["beat"].as_array().unwrap();
    assert_eq!(py.len(), BEAT_SCHEDULE.len(), "entry count");
    for (p, r) in py.iter().zip(BEAT_SCHEDULE) {
        let ctx = r.name;
        assert_eq!(p["name"], json!(r.name), "entry order");
        assert_eq!(p["task"], json!(r.task), "{ctx}: task");
        assert!(find(r.task).is_some(), "{ctx}: an unregistered task");
        assert_eq!(p["args"], json!([]), "{ctx}: args");
        let kwargs: BTreeMap<&str, &str> = r.kwargs.iter().copied().collect();
        assert_eq!(p["kwargs"], json!(kwargs), "{ctx}: kwargs");
        let mut options = json!({"queue": r.options.queue.name()});
        if let Some(e) = r.options.expires {
            options["expires"] = json!(e);
        }
        if let Some(c) = r.options.countdown {
            options["countdown"] = json!(c);
        }
        assert_eq!(p["options"], options, "{ctx}: options");
        match r.schedule {
            Schedule::Every { seconds } => {
                assert_eq!(p["schedule"]["kind"], json!("every"), "{ctx}");
                assert_eq!(p["schedule"]["seconds"], json!(f64::from(seconds)), "{ctx}");
            }
            Schedule::Crontab(c) => {
                let s = &p["schedule"];
                assert_eq!(s["kind"], json!("crontab"), "{ctx}");
                let written = json!({
                    "minute": c.minute, "hour": c.hour, "day_of_week": c.day_of_week,
                    "day_of_month": c.day_of_month, "month_of_year": c.month_of_year,
                });
                assert_eq!(s["written"], written, "{ctx}: crontab as written");
                let e = c.expand().unwrap();
                let expanded = json!({
                    "minute": e.minute, "hour": e.hour, "day_of_week": e.day_of_week,
                    "day_of_month": e.day_of_month, "month_of_year": e.month_of_year,
                });
                assert_eq!(
                    s["expanded"], expanded,
                    "{ctx}: crontab as Celery expands it"
                );
            }
        }
    }
}

fn ist(s: &str) -> NaiveDateTime {
    NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M").unwrap()
}

#[test]
fn due_entries_follow_celery_semantics() {
    use baskfy_worker::tasks::beat::due_at;
    let names =
        |at: &str| -> Vec<&str> { due_at(ist(at)).unwrap().iter().map(|e| e.name).collect() };
    // Thursday 29 Oct 2026, 18:45 IST: the nightly, and the hourly F&O alert sweep.
    let at = names("2026-10-29T18:45");
    assert!(at.contains(&"refresh-reference-data"), "{at:?}");
    assert!(!at.contains(&"session-catch-up"));
    // Saturday 31 Oct 06:45: the catch-up sweep runs mon-sat; Sunday it does not.
    assert!(names("2026-10-31T06:45").contains(&"session-catch-up"));
    assert!(!names("2026-11-01T06:45").contains(&"session-catch-up"));
    // fno-retest: 06:00 on a Saturday that is also day 1–7 of Jan/Apr/Jul/Oct (both must hold).
    assert!(names("2026-10-03T06:00").contains(&"fno-retest"));
    assert!(!names("2026-10-10T06:00").contains(&"fno-retest"));
    assert!(!names("2026-11-07T06:00").contains(&"fno-retest"));
    // The options collector every minute 09:00–15:59 on weekdays; never on a Sunday.
    assert!(names("2026-10-29T15:59").contains(&"options-collect-chain"));
    assert!(!names("2026-11-01T10:00").contains(&"options-collect-chain"));
    // Interval entries are never listed by minute.
    assert!(!names("2026-10-29T10:00").contains(&"twt-scan-publish"));
}

#[test]
fn ported_lists_name_only_their_own_lanes_tasks() {
    let lanes = [
        Lane::Pipeline,
        Lane::Screener,
        Lane::ApiRoutes,
        Lane::Curated,
        Lane::BacktestEngine,
        Lane::SleeveBacktests,
        Lane::SwingEod,
        Lane::SwingLive,
        Lane::TwtJobs,
        Lane::VbtJobs,
        Lane::Portfolio,
        Lane::OpsAlerts,
        Lane::Fno,
        Lane::Options,
        Lane::Backfills,
        Lane::Clis,
    ];
    for lane in lanes {
        for name in lane.ported() {
            let spec =
                find(name).unwrap_or_else(|| panic!("{name} in {lane:?}::PORTED is not a task"));
            assert_eq!(spec.lane, lane, "{name} is listed by the wrong lane");
        }
    }
    // The foundation wires exactly the four tasks wave 5's code answers.
    let wired: Vec<&str> = REGISTRY
        .iter()
        .filter(|t| t.lane == Lane::Pipeline)
        .map(|t| t.name)
        .collect();
    assert_eq!(wired.len(), 4);
    assert!(wired.iter().all(|n| find(n).is_some_and(|t| t.is_ported())));
}

/// Every declared stub answers the typed "not ported" error through the registry, after its
/// arguments bind — no database, no network (a lazy pool that is never used).
#[test]
fn every_stub_refuses_with_task_not_ported() {
    use baskfy_db::{OrderedJson, WriteDb};
    use baskfy_worker::WorkerError;
    use baskfy_worker::tasks::context::TaskClock;
    use baskfy_worker::tasks::{TaskContext, run_task};

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let _entered = runtime.enter();
    let settings = baskfy_api::Settings::default();
    let db = WriteDb::connect_lazy(&settings.db_settings()).unwrap();
    let ctx = TaskContext::new(
        db,
        settings,
        WorkerSettings::default(),
        TaskClock::starting_at_ist(ist("2026-10-29T13:15")),
        None,
        None,
    );
    let mut stubs = 0;
    for spec in REGISTRY.iter().filter(|t| !t.is_ported()) {
        // The required arguments, as a Celery message would carry them.
        let keywords: Vec<(String, OrderedJson)> = spec
            .params
            .iter()
            .filter(|p| p.default == PyDefault::Required)
            .map(|p| {
                let v = if p.annotation == "int" {
                    OrderedJson::Int(1)
                } else {
                    OrderedJson::Str("2026-10-29".to_owned())
                };
                (p.name.to_owned(), v)
            })
            .collect();
        let result = runtime.block_on(run_task(&ctx, spec.name, Vec::new(), keywords));
        match result {
            Err(WorkerError::TaskNotPorted { task, lane }) => {
                assert_eq!(task, spec.name);
                assert_eq!(lane, spec.lane.name());
                stubs += 1;
            }
            other => panic!("{}: expected TaskNotPorted, got {other:?}", spec.name),
        }
    }
    let ported = REGISTRY.iter().filter(|t| t.is_ported()).count();
    for spec in REGISTRY.iter() {
        for name in spec.lane.ported() {
            assert!(
                REGISTRY.iter().any(|t| t.name == *name),
                "{name} is PORTED but not registered"
            );
        }
    }
    assert_eq!(stubs + ported, REGISTRY.len());
    let e = WorkerError::TaskNotPorted {
        task: "baskfy.fno.scan",
        lane: "fno",
    };
    assert_eq!(e.python_type(), "NotPorted");
}
