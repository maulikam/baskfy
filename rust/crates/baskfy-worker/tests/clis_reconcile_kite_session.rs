//! `baskfy-worker reconcile` and `baskfy-worker kite-session`, run as the binary.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::PathBuf;
use std::process::{Command, Output};

const KEY: &str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";

fn worker(args: &[&str], env: &[(&str, &str)]) -> Output {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_baskfy-worker"));
    cmd.args(args)
        .current_dir(std::env::temp_dir())
        .env_remove("BASKFY_KITE_DESK_SSH_TARGET")
        .env_remove("BASKFY_KITE_API_KEY");
    for (k, v) in env {
        cmd.env(k, v);
    }
    cmd.output().unwrap()
}

fn text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

#[test]
fn reconcile_exits_zero_on_the_committed_export_and_says_what_it_did_not_do() {
    let out = worker(&["reconcile"], &[]);
    assert_eq!(out.status.code(), Some(0), "{}", text(&out.stderr));
    let stdout = text(&out.stdout);
    assert!(stdout.contains("0 disagreement(s) over tolerance."));
    assert!(stdout.contains("UNRESOLVED"));
    assert!(stdout.contains("no factor was recomputed from bars in this run"));
}

#[test]
fn reconcile_json_is_sorted_and_filtered() {
    let out = worker(&["reconcile_cli", "--symbols", "cupid", "--json"], &[]);
    assert_eq!(out.status.code(), Some(0), "{}", text(&out.stderr));
    let payload: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(payload["as_of"], "2026-08-18");
    let sharpe = payload["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == "sharpe_identity")
        .unwrap();
    assert_eq!(sharpe["cells"], 5);
    assert!(text(&out.stdout).starts_with("{\n  \"as_of\""));
}

#[test]
fn reconcile_refuses_an_unknown_symbol_with_exit_two() {
    let out = worker(&["reconcile", "--symbols", "NOTALISTING"], &[]);
    assert_eq!(out.status.code(), Some(2));
    assert!(text(&out.stderr).contains("none of ['NOTALISTING']"));
}

#[test]
fn reconcile_writes_the_report_file() {
    let target = std::env::temp_dir()
        .join(format!("baskfy-clis-reconcile-{}", std::process::id()))
        .join("REPORT.md");
    let out = worker(&["reconcile", "--write", target.to_str().unwrap()], &[]);
    assert_eq!(out.status.code(), Some(0));
    let committed = std::fs::read_to_string(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../decile-blueprint/reconciliation/REPORT.md"),
    )
    .unwrap();
    assert_eq!(std::fs::read_to_string(&target).unwrap(), committed);
    let _ = std::fs::remove_dir_all(target.parent().unwrap());
}

#[test]
fn reconcile_refuses_bars_rather_than_skipping_them() {
    let out = worker(&["reconcile", "--bars", "/nonexistent.parquet"], &[]);
    assert_eq!(out.status.code(), Some(1));
    assert!(text(&out.stderr).contains("--bars"));
}

#[test]
fn kite_session_deposit_then_status_never_prints_the_token() {
    let dir = std::env::temp_dir().join(format!("baskfy-clis-kite-bin-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let path = dir.join("kite.enc");
    let env = [
        ("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", KEY),
        ("BASKFY_KITE_TOKEN_PATH", path.to_str().unwrap()),
    ];
    let out = worker(&["kite-session", "status"], &env);
    assert_eq!(out.status.code(), Some(1));
    let out = worker(
        &["kite_session_cli", "deposit", "--token", "live-token-xyz"],
        &env,
    );
    assert_eq!(out.status.code(), Some(0), "{}", text(&out.stderr));
    assert!(!text(&out.stdout).contains("live-token-xyz"));
    assert!(!text(&out.stderr).contains("live-token-xyz"));
    assert!(text(&out.stdout).contains("stored: 14 chars"));
    let out = worker(&["kite-session", "status"], &env);
    assert_eq!(out.status.code(), Some(0));
    assert!(text(&out.stdout).contains("expired   : False"));
    assert!(
        !std::fs::read(&path)
            .unwrap()
            .windows(14)
            .any(|w| w == b"live-token-xyz")
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn kite_session_refuses_without_a_key_and_without_a_desk() {
    let dir = std::env::temp_dir().join(format!("baskfy-clis-kite-nokey-{}", std::process::id()));
    let path = dir.join("kite.enc");
    let out = worker(
        &["kite-session", "deposit", "--token", "x"],
        &[
            ("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", ""),
            ("BASKFY_KITE_TOKEN_PATH", path.to_str().unwrap()),
        ],
    );
    assert_eq!(out.status.code(), Some(1));
    assert!(text(&out.stderr).contains("BASKFY_KITE_TOKEN_ENCRYPTION_KEY is not set"));
    assert!(!path.exists());
    let out = worker(
        &["kite-session", "pull"],
        &[
            ("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", KEY),
            ("BASKFY_KITE_TOKEN_PATH", path.to_str().unwrap()),
        ],
    );
    assert_eq!(out.status.code(), Some(1));
    assert!(text(&out.stderr).contains("BASKFY_KITE_DESK_SSH_TARGET is not set"));
    let out = worker(&["kite-session", "login"], &[]);
    assert_eq!(out.status.code(), Some(2));
}
