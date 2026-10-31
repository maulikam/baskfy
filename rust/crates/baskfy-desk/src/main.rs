//! `baskfy-desk`: the desk server. Python: `uvicorn app.main:app --host H --port P` run from the
//! desk's directory; here `baskfy-desk --host H --port P [--root DIR]` (defaults `127.0.0.1`,
//! `8420`, the working directory), `baskfy-desk service <name>` for a background service, or
//! `baskfy-desk snapshot [args]` for `python -m app.analytics.snapshot`.
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
use baskfy_desk::lanes::exec_core::kite_client::DeskKite;
use baskfy_desk::lanes::weekly::weekly_books::snapshot::SnapshotKite;
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

/// The flags `main` itself reads, each with its value.
const MAIN_FLAGS: [&str; 8] = [
    "--host",
    "--port",
    "--root",
    "--env-file",
    "--harness",
    "--harness-clock",
    "--harness-seed",
    "--kite-log",
];

/// The arguments after `snapshot`, without the server's own flags.
fn snapshot_args(rest: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    let mut it = rest.iter();
    while let Some(a) = it.next() {
        if MAIN_FLAGS.contains(&a.as_str()) {
            it.next();
        } else {
            out.push(a.clone());
        }
    }
    out
}

/// `Kite()` built lazily, as `snapshot.run` does: a missing key fails the first read only.
struct LazyKite(Result<Arc<DeskKite>, String>);

impl LazyKite {
    fn get(&self) -> Result<&DeskKite, String> {
        self.0.as_deref().map_err(Clone::clone)
    }
}

#[async_trait]
impl SnapshotKite for LazyKite {
    async fn is_authed(&self) -> bool {
        match self.get() {
            Ok(k) => SnapshotKite::is_authed(k).await,
            Err(_) => false,
        }
    }
    async fn holdings(&self) -> Result<Vec<PyDict>, String> {
        SnapshotKite::holdings(self.get()?).await
    }
    async fn available_cash(&self) -> Result<f64, String> {
        SnapshotKite::available_cash(self.get()?).await
    }
    async fn quote_raw(&self, keys: &[String]) -> Result<PyDict, String> {
        SnapshotKite::quote_raw(self.get()?, keys).await
    }
    async fn instruments(&self, exchange: &str) -> Result<Vec<PyValue>, String> {
        SnapshotKite::instruments(self.get()?, exchange).await
    }
    async fn historical(&self, token: i64, day: chrono::NaiveDate) -> Result<Vec<PyValue>, String> {
        SnapshotKite::historical(self.get()?, token, day).await
    }
}

/// `python -m app.analytics.snapshot [args]`: the EOD snapshot (reads only).
#[allow(clippy::print_stdout, clippy::print_stderr)]
async fn snapshot(state: &baskfy_desk::app::AppState, argv: &[String]) -> ExitCode {
    use baskfy_desk::lanes::weekly::weekly_books::snapshot::{CollectOptions, cli};
    let settings = state.settings_snapshot();
    let kite = LazyKite(
        baskfy_desk::lanes::exec_core::exec_kite::kite(state)
            .map_err(|e| format!("{}: {}", e.python_type, e.message)),
    );
    let mut conn = match state.db.connect().await {
        Ok(c) => c,
        Err(e) => return fail(&e.to_string()),
    };
    let base = CollectOptions {
        on_date: None,
        price_source: "holdings",
        cash: None,
        allow_stale_cash: false,
        today: state.clock.now_ist().date_naive(),
        include_untouchable_in_nav: settings.include_untouchable_in_nav,
        root: &state.paths.root,
    };
    let out = cli(argv, &kite, &mut conn, base, settings.dry_run).await;
    for line in &out.stdout {
        println!("{line}");
    }
    for line in &out.stderr {
        eprintln!("{line}");
    }
    ExitCode::from(u8::try_from(out.code).unwrap_or(1))
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
    // `load_dotenv()` also feeds the token store's function-level key read (R8.11).
    baskfy_desk::lanes::exec_core::token_store::install_dotenv(&dotenv);
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

    if args.get(1).is_some_and(|a| a == "snapshot") {
        return snapshot(&state, &snapshot_args(&args[2..])).await;
    }

    // `import app.main`: secret files tightened, stored settings applied, ceilings and
    // observability logged. Never a refused boot.
    baskfy_desk::lanes::session::boot::boot(&state).await;

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
