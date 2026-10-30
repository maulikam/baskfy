//! `baskfy_worker.tasks.desk`: the retired Beat names refuse; `--check` runs the desk's script.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Instant;

use baskfy_db::OrderedJson;

use crate::steps::log_error;

/// What the refusal stubs log (`_MOVED`).
pub(super) const MOVED: &str = "retired from Beat; collection runs in the desk-daily container \
                                (NEEDS-MAULIK §33, Maulik 13 Sep 2026)";

/// `DESK_ROOT`: the desk beside this workspace in the monorepo, resolved rather than configured.
pub(super) fn desk_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .join("kite-momentum-rebalancer")
}

/// `DeskRunResult`.
pub(super) fn result(
    entry: &str,
    argv: Vec<OrderedJson>,
    exit_code: i64,
    seconds: f64,
) -> OrderedJson {
    OrderedJson::Object(vec![
        ("entry".to_owned(), OrderedJson::Str(entry.to_owned())),
        ("argv".to_owned(), OrderedJson::Array(argv)),
        ("exit_code".to_owned(), OrderedJson::Int(exit_code)),
        ("seconds".to_owned(), OrderedJson::Float(seconds)),
    ])
}

/// `_refused(entry, argv)`: exit 2, the desk's "did not collect".
pub(super) fn refused(entry: &str, argv: Vec<OrderedJson>) -> OrderedJson {
    log_error(&format!("baskfy.desk.{entry}: {MOVED}"));
    result(entry, argv, 2, 0.0)
}

fn interpreter(root: &Path) -> PathBuf {
    let venv = root.join(".venv").join("bin").join("python");
    if venv.is_file() {
        venv
    } else {
        PathBuf::from("python3")
    }
}

const RUNNER: &str = "import importlib, sys\n\
sys.argv = sys.argv[1:]\n\
module = importlib.import_module('scripts.' + sys.argv[0])\n\
try:\n    code = int(module.main())\n\
except SystemExit as exc:\n    code = int(exc.code or 0)\n\
sys.exit(code)\n";

/// `_run(argv, entry)`: the desk's `scripts.<entry>.main()` with `argv`, in the desk's directory.
/// A non-zero exit is reported, never raised; the script's output goes to stderr.
pub(super) async fn run_entry(root: &Path, entry: &str, argv: &[&str]) -> OrderedJson {
    let json_argv = || {
        argv.iter()
            .map(|a| OrderedJson::Str((*a).to_owned()))
            .collect::<Vec<_>>()
    };
    if !root.is_dir() {
        log_error(&format!(
            "desk tree missing at {}; collection runs in the desk-daily container \
             (NEEDS-MAULIK §33, Maulik 13 Sep 2026)",
            root.display()
        ));
        return result(entry, json_argv(), 2, 0.0);
    }
    let started = Instant::now();
    let root_owned = root.to_path_buf();
    let entry_owned = entry.to_owned();
    let args: Vec<String> = argv.iter().map(|a| (*a).to_owned()).collect();
    let status = tokio::task::spawn_blocking(move || {
        Command::new(interpreter(&root_owned))
            .arg("-c")
            .arg(RUNNER)
            .arg(&entry_owned)
            .args(&args)
            .current_dir(&root_owned)
            .env("PYTHONPATH", &root_owned)
            .stdin(Stdio::null())
            .stdout(Stdio::from(std::io::stderr()))
            .status()
    })
    .await;
    let code = match status {
        Ok(Ok(s)) => s.code().map_or(1, i64::from),
        Ok(Err(e)) => {
            log_error(&format!("desk {entry} could not start: {e}"));
            1
        }
        Err(e) => {
            log_error(&format!("desk {entry} could not start: {e}"));
            1
        }
    };
    let seconds = (started.elapsed().as_secs_f64() * 100.0).round() / 100.0;
    result(entry, json_argv(), code, seconds)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]

    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("baskfy-clis-desk-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("scripts")).unwrap();
        std::fs::write(dir.join("scripts").join("__init__.py"), "").unwrap();
        dir
    }

    fn python_available() -> bool {
        Command::new("python3")
            .arg("-c")
            .arg("pass")
            .status()
            .is_ok_and(|s| s.success())
    }

    #[test]
    fn the_retired_names_refuse_with_exit_two_rather_than_collect() {
        let out = refused("autorun", Vec::new());
        assert_eq!(out.get("exit_code"), Some(&OrderedJson::Int(2)));
        assert_eq!(out.get("seconds"), Some(&OrderedJson::Float(0.0)));
        assert_eq!(
            out.get("entry"),
            Some(&OrderedJson::Str("autorun".to_owned()))
        );
    }

    #[test]
    fn it_finds_the_desk() {
        assert!(desk_root().ends_with("kite-momentum-rebalancer"));
    }

    #[tokio::test]
    async fn a_missing_desk_tree_is_reported_not_raised() {
        let out = run_entry(Path::new("/nonexistent/desk-tree"), "daily", &["--check"]).await;
        assert_eq!(out.get("exit_code"), Some(&OrderedJson::Int(2)));
        assert_eq!(
            out.get("argv"),
            Some(&OrderedJson::Array(vec![OrderedJson::Str(
                "--check".to_owned()
            )]))
        );
    }

    #[tokio::test]
    async fn a_failing_step_is_reported_not_raised() {
        if !python_available() {
            return;
        }
        let dir = scratch("fail");
        std::fs::write(
            dir.join("scripts").join("daily.py"),
            "import sys\ndef main():\n    assert sys.argv == ['daily', '--check'], sys.argv\n    return 3\n",
        )
        .unwrap();
        let out = run_entry(&dir, "daily", &["--check"]).await;
        assert_eq!(out.get("exit_code"), Some(&OrderedJson::Int(3)));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn a_step_that_calls_sys_exit_is_also_reported_not_raised() {
        if !python_available() {
            return;
        }
        let dir = scratch("exit");
        std::fs::write(
            dir.join("scripts").join("daily.py"),
            "import os, sys\ndef main():\n    assert os.getcwd() == os.path.realpath(os.path.dirname(os.path.dirname(__file__)))\n    sys.exit(4)\n",
        )
        .unwrap();
        let out = run_entry(&dir, "daily", &["--check"]).await;
        assert_eq!(out.get("exit_code"), Some(&OrderedJson::Int(4)));
        std::fs::write(
            dir.join("scripts").join("daily.py"),
            "def main():\n    return 0\n",
        )
        .unwrap();
        let out = run_entry(&dir, "daily", &["--check"]).await;
        assert_eq!(out.get("exit_code"), Some(&OrderedJson::Int(0)));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
