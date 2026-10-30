//! The free gates of `baskfy.options.scan` and `plan_o1/o2/o3` through the task entry point —
//! every refusal answered before a database connection — and the builders' law-2 shape
//! (`test_options_scan_task.py`, `test_options_plan*_task.py`'s `TestTheGate`).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_db::{DbSettings, OrderedJson, WriteDb};
use baskfy_providers::secret::Secret;
use baskfy_worker::settings::WorkerSettings;
use baskfy_worker::tasks::context::TaskClock;
use baskfy_worker::tasks::options::PORTED;
use baskfy_worker::tasks::{TaskContext, run_task};
use chrono::NaiveDateTime;

const NO_TENANT: &str = "no BASKFY_SOLE_USER_ID configured";

fn tenant_configured() -> bool {
    baskfy_worker::tasks::context::sole_user_id().is_some()
}

/// Runs `task` at `at` with the given flags; the database is never reachable, so any answer
/// that is not a refusal fails loudly.
fn run(task: &str, at: &str, scan: bool, collect: bool, monitor: bool) -> String {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let _entered = runtime.enter();
    let settings = baskfy_api::Settings::default();
    let db = WriteDb::connect_lazy(&DbSettings::with_url(Secret::new(
        "postgresql://nobody:nothing@127.0.0.1:9/never".to_owned(),
    )))
    .unwrap();
    let worker = WorkerSettings {
        options_scan_enabled: scan,
        options_collect_enabled: collect,
        options_monitor_enabled: monitor,
        ..WorkerSettings::default()
    };
    let ctx = TaskContext::new(
        db,
        settings,
        worker,
        TaskClock::starting_at_ist(
            NaiveDateTime::parse_from_str("2026-10-29T13:15", "%Y-%m-%dT%H:%M").unwrap(),
        ),
        None,
        None,
    );
    let out = runtime
        .block_on(run_task(
            &ctx,
            task,
            Vec::new(),
            vec![("at".to_owned(), OrderedJson::Str(at.to_owned()))],
        ))
        .unwrap_or_else(|e| panic!("{task} at {at}: {e}"));
    let stamped = if at.contains('+') {
        at.to_owned()
    } else {
        format!("{at}+05:30")
    };
    assert_eq!(out.get("at"), Some(&OrderedJson::Str(stamped)));
    match out.get("skipped") {
        Some(OrderedJson::Str(why)) => why.clone(),
        other => panic!("{task} at {at}: not refused: {other:?}"),
    }
}

/// Inside the window every flag on: the tenant refusal, when no tenant is configured.
fn passes_window(task: &str, at: &str) {
    if tenant_configured() {
        return;
    }
    assert_eq!(run(task, at, true, true, true), NO_TENANT, "{task} at {at}");
}

#[test]
fn every_flag_defaults_off() {
    let w = WorkerSettings::default();
    assert!(!w.options_scan_enabled && !w.options_collect_enabled && !w.options_monitor_enabled);
    assert!(!w.options_o1m_execution_enabled && !w.options_o3_execution_enabled);
    assert_eq!(w.options_desk_dry_run, "true");
}

#[test]
fn the_four_bodies_are_ported_and_the_backtest_is_not_this_lanes() {
    for name in [
        "baskfy.options.scan",
        "baskfy.options.plan_o1",
        "baskfy.options.plan_o2",
        "baskfy.options.plan_o3",
    ] {
        assert!(PORTED.contains(&name), "{name}");
        assert!(baskfy_worker::tasks::find(name).is_some_and(|s| s.is_ported()));
    }
    assert!(!PORTED.contains(&"baskfy.options.backtest"));
}

#[test]
fn scan_refuses_flag_by_flag_then_the_window() {
    let inside = "2026-10-27T10:01:20+05:30";
    assert_eq!(
        run("baskfy.options.scan", inside, false, true, false),
        "BASKFY_OPTIONS_SCAN_ENABLED is false"
    );
    assert_eq!(
        run("baskfy.options.scan", inside, false, false, false),
        "BASKFY_OPTIONS_SCAN_ENABLED is false"
    );
    assert_eq!(
        run("baskfy.options.scan", inside, true, false, false),
        "BASKFY_OPTIONS_COLLECT_ENABLED is false (no chain to scan)"
    );
    for outside in [
        "2026-10-27T08:59:00+05:30",
        "2026-10-27T09:14:00+05:30",
        "2026-10-27T15:31:00+05:30",
    ] {
        assert_eq!(
            run("baskfy.options.scan", outside, true, true, false),
            "outside 09:15-15:30 IST"
        );
    }
    for edge in [
        "2026-10-27T09:15:00+05:30",
        "2026-10-27T15:30:00+05:30",
        inside,
    ] {
        passes_window("baskfy.options.scan", edge);
    }
}

#[test]
fn plans_refuse_the_monitor_flag_first_then_the_collector() {
    for (task, at) in [
        ("baskfy.options.plan_o1", "2026-10-27T10:00:40+05:30"),
        ("baskfy.options.plan_o2", "2026-10-19T10:05:45+05:30"),
        ("baskfy.options.plan_o3", "2026-10-13T09:46:20+05:30"),
    ] {
        assert_eq!(
            run(task, at, true, true, false),
            "BASKFY_OPTIONS_MONITOR_ENABLED is false",
            "{task}"
        );
        assert_eq!(
            run(task, at, false, false, true),
            "BASKFY_OPTIONS_COLLECT_ENABLED is false (no chain to plan from)",
            "{task}"
        );
    }
}

#[test]
fn o1_window_is_1000_to_1016() {
    let task = "baskfy.options.plan_o1";
    let outside = "outside 10:00-10:15 IST (+1 minute to lapse)";
    for at in [
        "2026-10-27T09:59:00+05:30",
        "2026-10-27T10:16:01+05:30",
        "2026-10-27T10:17:00+05:30",
    ] {
        assert_eq!(run(task, at, false, true, true), outside, "{at}");
    }
    for at in ["2026-10-27T10:00:00+05:30", "2026-10-27T10:16:00+05:30"] {
        passes_window(task, at);
    }
}

#[test]
fn o2_window_is_the_entry_window_plus_the_no_trigger_grace() {
    let task = "baskfy.options.plan_o2";
    for at in ["2026-10-19T09:29:00+05:30", "2026-10-19T13:34:01+05:30"] {
        assert_eq!(
            run(task, at, false, true, true),
            "outside 09:30-13:34 IST",
            "{at}"
        );
    }
    for at in ["2026-10-19T09:30:00+05:30", "2026-10-19T13:34:00+05:30"] {
        passes_window(task, at);
    }
}

#[test]
fn o3_window_is_o3bs_plan_time_to_o3as_entry_window_plus_the_grace() {
    let task = "baskfy.options.plan_o3";
    for at in ["2026-10-13T09:44:59+05:30", "2026-10-13T13:35:00+05:30"] {
        assert_eq!(
            run(task, at, false, true, true),
            "outside 09:45-13:34 IST",
            "{at}"
        );
    }
    for at in ["2026-10-13T09:45:00+05:30", "2026-10-13T13:34:00+05:30"] {
        passes_window(task, at);
    }
}

#[test]
fn a_naive_at_is_read_as_ist() {
    let out = run(
        "baskfy.options.scan",
        "2026-10-27T08:00:00",
        true,
        true,
        false,
    );
    assert_eq!(out, "outside 09:15-15:30 IST");
}

const SCAN: &str = include_str!("../src/tasks/options/scan.rs");
const PLAN: &str = include_str!("../src/tasks/options/plan.rs");
const PLAN_O2: &str = include_str!("../src/tasks/options/plan_o2.rs");
const PLAN_O3: &str = include_str!("../src/tasks/options/plan_o3.rs");

#[test]
fn no_order_path_in_the_scan_or_the_builders() {
    for (name, source) in [
        ("scan", SCAN),
        ("plan", PLAN),
        ("plan_o2", PLAN_O2),
        ("plan_o3", PLAN_O3),
    ] {
        for verb in [
            "place_order",
            "modify_order",
            "cancel_order",
            "place_gtt",
            "OrderGateway",
            "baskfy_execution",
            ".place(",
        ] {
            assert!(!source.contains(verb), "{name} names {verb}");
        }
    }
}

#[test]
fn the_scan_and_o2_reach_no_provider() {
    for (name, source) in [("scan", SCAN), ("plan_o2", PLAN_O2)] {
        for word in [
            "baskfy_providers::kite",
            "KiteProvider",
            "family_kite",
            "MarginReader",
        ] {
            assert!(!source.contains(word), "{name} names {word}");
        }
    }
}

#[test]
fn the_margin_calculator_is_asked_without_positions() {
    assert!(PLAN.contains("basket_order_margins(&records, false)"));
}

#[test]
fn o3b_decides_before_o3a() {
    assert!(PLAN_O3.contains("const ORDER: [Sleeve; 2] = [Sleeve::O3b, Sleeve::O3a];"));
}
