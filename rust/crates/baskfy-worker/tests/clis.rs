//! The clis lane through the binary: dispatch, argparse's exit 2, the desk tasks' refusals and
//! the sole-user checks that answer before any database or network read.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::process::{Command, Output};

use baskfy_db::OrderedJson;

fn worker(args: &[&str], env: &[(&str, &str)]) -> Output {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_baskfy-worker"));
    cmd.args(args)
        .env_remove("BASKFY_SOLE_USER_ID")
        .env(
            "BASKFY_DATABASE_URL",
            "postgresql://nobody:nothing@127.0.0.1:1/never",
        )
        .env("DRY_RUN", "true");
    for (k, v) in env {
        cmd.env(k, v);
    }
    cmd.output().unwrap()
}

fn stdout_json(out: &Output) -> OrderedJson {
    OrderedJson::parse(String::from_utf8_lossy(&out.stdout).trim())
        .unwrap_or_else(|e| panic!("stdout is not JSON ({e}): {:?}", out))
}

fn stderr(out: &Output) -> String {
    String::from_utf8_lossy(&out.stderr).into_owned()
}

#[test]
fn an_unknown_command_is_argparses_exit_two() {
    let out = worker(&["fno", "explode"], &[]);
    assert_eq!(out.status.code(), Some(2));
    assert!(
        stderr(&out).contains("invalid choice: 'explode'"),
        "{}",
        stderr(&out)
    );
    let out = worker(&["factors_cli"], &[]);
    assert_eq!(out.status.code(), Some(2));
    assert!(stderr(&out).contains("required: command"));
    let out = worker(&["eq-bars", "backfill"], &[]);
    assert_eq!(out.status.code(), Some(2));
    assert!(stderr(&out).contains("--from"));
}

#[test]
fn a_required_or_mistyped_flag_is_exit_two() {
    let out = worker(&["pipeline"], &[]);
    assert_eq!(out.status.code(), Some(2));
    assert!(stderr(&out).contains("required: --date"));
    let out = worker(&["swing", "--premarket", "NOON"], &[]);
    assert_eq!(out.status.code(), Some(2));
    assert!(stderr(&out).contains("invalid choice: 'NOON'"));
    let out = worker(&["vbt", "--plan", "NOON"], &[]);
    assert_eq!(out.status.code(), Some(2));
    let out = worker(&["twt", "--sessions", "many"], &[]);
    assert_eq!(out.status.code(), Some(2));
    assert!(stderr(&out).contains("invalid int value: 'many'"));
    let out = worker(&["swing", "--date", "2026-09-01", "--bogus"], &[]);
    assert_eq!(out.status.code(), Some(2));
    assert!(stderr(&out).contains("unrecognized arguments: --bogus"));
}

#[test]
fn the_sleeve_clis_refuse_without_a_sole_user_before_any_read() {
    for (args, schema) in [
        (vec!["swing", "--date", "2026-09-01"], "sw_"),
        (vec!["twt", "--date", "2026-09-10"], "tw_"),
        (vec!["twt", "--plan"], "tw_"),
        (vec!["vbt", "--date", "2026-09-10"], "vb_"),
        (vec!["vbt", "--backtest"], "vb_"),
    ] {
        let out = worker(&args, &[]);
        assert_eq!(out.status.code(), Some(1), "{args:?}: {}", stderr(&out));
        let Some(OrderedJson::Str(error)) = stdout_json(&out).get("error").cloned() else {
            panic!("{args:?} printed no error");
        };
        assert!(
            error.starts_with("BASKFY_SOLE_USER_ID is not set")
                && error.contains(&format!("the {schema} schema is keyed by user")),
            "{error}"
        );
    }
    let out = worker(&["swing", "--premarket", "LEVELS"], &[]);
    assert_eq!(
        stdout_json(&out),
        OrderedJson::Object(vec![(
            "error".to_owned(),
            OrderedJson::Str(
                "BASKFY_SOLE_USER_ID is not set, and the sw_ schema is keyed by user.".to_owned()
            )
        )])
    );
}

#[test]
fn the_retired_desk_names_refuse_with_exit_code_two() {
    let out = worker(
        &["task", "baskfy.desk.daily", "--args", "[\"manual\"]"],
        &[],
    );
    assert_eq!(out.status.code(), Some(0), "{}", stderr(&out));
    let payload = stdout_json(&out);
    assert_eq!(
        payload.get("entry"),
        Some(&OrderedJson::Str("daily".to_owned()))
    );
    assert_eq!(payload.get("exit_code"), Some(&OrderedJson::Int(2)));
    assert_eq!(
        payload.get("argv"),
        Some(&OrderedJson::Array(
            ["--quiet", "--source", "manual"]
                .iter()
                .map(|s| OrderedJson::Str((*s).to_owned()))
                .collect()
        ))
    );
    assert!(stderr(&out).contains("retired from Beat"));
    let out = worker(&["task", "baskfy.desk.autorun"], &[]);
    let payload = stdout_json(&out);
    assert_eq!(payload.get("exit_code"), Some(&OrderedJson::Int(2)));
    assert_eq!(payload.get("argv"), Some(&OrderedJson::Array(Vec::new())));
    let out = worker(&["task", "baskfy.desk.autorun", "--args", "[1]"], &[]);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(
        stdout_json(&out).get("raised"),
        Some(&OrderedJson::Str("TypeError".to_owned()))
    );
}

#[test]
fn the_desk_tasks_are_listed_as_ported() {
    let out = worker(&["tasks"], &[]);
    let text = String::from_utf8_lossy(&out.stdout);
    for name in [
        "baskfy.desk.daily",
        "baskfy.desk.autorun",
        "baskfy.desk.daily_check",
    ] {
        let line = text
            .lines()
            .find(|l| l.contains(&format!("\"{name}\"")))
            .unwrap_or_else(|| panic!("{name} is not listed"));
        assert!(
            line.contains("\"lane\":\"clis\"") && line.contains("\"ported\":true"),
            "{line}"
        );
    }
}
