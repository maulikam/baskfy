//! `baskfy-desk`: the desk server. Python: `uvicorn app.main:app --host H --port P` run from the
//! desk's directory; here `baskfy-desk --host H --port P [--root DIR]` (defaults `127.0.0.1`,
//! `8420`, the working directory), or `baskfy-desk service <name>` for a background service.
//!
//! Settings come from the process environment (plus `--env-file F`, never overriding it, as
//! `load_dotenv()` does). **There is no live Kite client in this build** (R8.0): without
//! `--harness` the desk runs with no broker session — every Kite read fails, pages degrade to
//! "not logged in", and nothing can reach an order.
//!
//! `--harness <fixture dir>` (`tools/parity/desk_diff.py` only): the replay Kite over that
//! fixture (calls appended to `--kite-log`), a settable clock starting at `--harness-clock`
//! (RFC 3339), harness ids seeded by `--harness-seed`, and the two `/__harness/*` routes. It
//! refuses to start unless `DRY_RUN` is true.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;

use async_trait::async_trait;
use baskfy_desk::app::{DeskPaths, Harness, StateParts};
use baskfy_desk::clock::{Clock, FixedClock, SystemClock};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::ids::{HarnessIds, IdSource, RandomIds};
use baskfy_desk::kite::{
    GttParams, KiteError, KiteExceptionKind, KiteReads, KiteWrites, OrderParams,
};
use baskfy_desk::settings::{Settings, dotenv_lookup};
use baskfy_desk::storage::{Db, DbConfig};
use baskfy_desk::wire::{PyDict, PyValue};
use baskfy_providers::secret::Secret;

fn arg(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

/// One operator log line on stderr (the desk has no logging dependency).
#[allow(clippy::print_stderr)]
fn log_line(message: &str) {
    eprintln!("{message}");
}

#[allow(clippy::print_stderr)]
fn fail(message: &str) -> ExitCode {
    eprintln!("baskfy-desk: {message}");
    ExitCode::FAILURE
}

/// The four seams the harness replaces.
type Seams = (
    Arc<dyn Clock>,
    Arc<dyn IdSource>,
    Arc<dyn KiteWrites>,
    Option<Harness>,
);

/// No broker at all: what the desk has before a live client is ported (R8.0).
#[derive(Debug)]
struct NoKite;

fn no_session() -> KiteError {
    KiteError::Kite {
        kind: KiteExceptionKind::Token,
        message: "no Kite client in this build of baskfy-desk (R8.0)".to_owned(),
        code: 403,
    }
}

#[async_trait]
impl KiteReads for NoKite {
    fn login_url(&self) -> String {
        String::new()
    }
    fn set_access_token(&self, _token: &Secret) {}
    async fn generate_session(&self, _r: &str, _s: &Secret) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn profile(&self) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn holdings(&self) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn positions(&self) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn margins(&self, _s: Option<&str>) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn ltp(&self, _i: &[String]) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn ohlc(&self, _i: &[String]) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn quote(&self, _i: &[String]) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn instruments(&self, _e: Option<&str>) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn historical_data(
        &self,
        _t: i64,
        _f: &str,
        _to: &str,
        _i: &str,
        _c: bool,
        _o: bool,
    ) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn orders(&self) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn order_history(&self, _o: &str) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn trades(&self) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn get_gtts(&self) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn basket_order_margins(
        &self,
        _p: &[PyValue],
        _c: bool,
        _m: Option<&str>,
    ) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
}

#[async_trait]
impl KiteWrites for NoKite {
    async fn place_order(&self, _o: &OrderParams) -> Result<String, KiteError> {
        Err(no_session())
    }
    async fn modify_order(&self, _v: &str, _o: &str, _p: &PyDict) -> Result<String, KiteError> {
        Err(no_session())
    }
    async fn cancel_order(
        &self,
        _v: &str,
        _o: &str,
        _p: Option<&str>,
    ) -> Result<String, KiteError> {
        Err(no_session())
    }
    async fn place_gtt(&self, _g: &GttParams) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn modify_gtt(&self, _t: i64, _g: &GttParams) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
    async fn delete_gtt(&self, _t: i64) -> Result<PyValue, KiteError> {
        Err(no_session())
    }
}

#[tokio::main]
async fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let host = arg(&args, "--host").unwrap_or_else(|| "127.0.0.1".to_owned());
    let port = arg(&args, "--port").unwrap_or_else(|| "8420".to_owned());
    let root = arg(&args, "--root").map_or_else(
        || std::env::current_dir().unwrap_or_default(),
        PathBuf::from,
    );
    let dotenv = match arg(&args, "--env-file") {
        Some(path) => match std::fs::read_to_string(&path) {
            Ok(text) => dotenv_lookup(&text),
            Err(e) => return fail(&format!("--env-file {path}: {e}")),
        },
        None => std::collections::BTreeMap::new(),
    };
    let lookup = |k: &str| std::env::var(k).ok().or_else(|| dotenv.get(k).cloned());
    let settings = match Settings::from_lookup(&lookup) {
        Ok(s) => s,
        Err(e) => return fail(&e.to_string()),
    };
    let paths = DeskPaths::under(&root);
    let mut db_config = DbConfig::from_settings(&settings, lookup("DESK_DATABASE_URL"));
    db_config.db_path = paths.resolve(&settings.db_path);
    let db = match Db::open(db_config).await {
        Ok(db) => db,
        Err(e) => return fail(&e.to_string()),
    };

    let harness_dir = arg(&args, "--harness");
    let (clock, ids, kite, harness): Seams = match harness_dir {
        Some(dir) => {
            if !settings.dry_run {
                return fail("--harness refuses to start with DRY_RUN false");
            }
            let start = arg(&args, "--harness-clock")
                .unwrap_or_else(|| "2026-10-30T09:16:00+05:30".to_owned());
            let Ok(at) = chrono::DateTime::parse_from_rfc3339(&start) else {
                return fail("--harness-clock is not RFC 3339");
            };
            let clock = Arc::new(FixedClock::new(at));
            let log = arg(&args, "--kite-log").map(PathBuf::from);
            let replay = match ReplayKite::load(&PathBuf::from(dir), log, &settings.kite_api_key) {
                Ok(k) => Arc::new(k),
                Err(e) => return fail(&e.to_string()),
            };
            let seed = arg(&args, "--harness-seed").unwrap_or_else(|| "desk".to_owned());
            let harness = Harness {
                clock: clock.clone(),
                kite: replay.clone(),
            };
            (
                clock,
                Arc::new(HarnessIds::new(seed)),
                replay,
                Some(harness),
            )
        }
        None => (
            Arc::new(SystemClock),
            Arc::new(RandomIds),
            Arc::new(NoKite),
            None,
        ),
    };

    let state = baskfy_desk::build_state(StateParts {
        settings,
        clock,
        ids,
        db,
        kite,
        paths,
        harness,
    });

    if args.get(1).is_some_and(|a| a == "service") {
        let Some(name) = args.get(2) else {
            return fail("usage: baskfy-desk service <name>");
        };
        return match baskfy_desk::lanes::run_service(name, &state).await {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => fail(&e),
        };
    }

    // `main._apply_stored_settings()`, run at import in Python: migrate, then push the stored
    // overrides onto the settings. A failure is a warning, never a refused boot.
    match baskfy_desk::lanes::session::startup(&state).await {
        Ok(applied) if !applied.is_empty() => {
            let mut applied = applied;
            applied.sort();
            log_line(&format!(
                "INFO:root:applied {} stored setting override(s): {}",
                applied.len(),
                applied.join(", ")
            ));
        }
        Ok(_) => {}
        Err(e) => log_line(&format!(
            "WARNING:root:could not apply stored settings: {e}"
        )),
    }

    // The `fno_badge()` nav global reads the book through the app state from the first page on,
    // as Python's module-level global does (DECISIONS-RS R8.F2 / R8.8).
    baskfy_desk::lanes::fno::install(&state);

    let app = baskfy_desk::build_app(state);
    let Ok(address) = format!("{host}:{port}").parse::<SocketAddr>() else {
        return fail("--host/--port do not form a socket address");
    };
    let listener = match tokio::net::TcpListener::bind(address).await {
        Ok(l) => l,
        Err(e) => return fail(&format!("cannot bind {address}: {e}")),
    };
    match axum::serve(
        listener,
        app.into_make_service_with_connect_info::<SocketAddr>(),
    )
    .await
    {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => fail(&e.to_string()),
    }
}
