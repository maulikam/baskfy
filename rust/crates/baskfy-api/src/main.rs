//! `baskfy-api`: the Rust API server. `uvicorn baskfy_api.app:get_app --factory --host H --port P`
//! in Python; here `baskfy-api --host H --port P` (defaults `127.0.0.1` and `8000`).
//!
//! Settings come from `BASKFY_*` exactly as Python's (`.env` and `../../.env` relative to the
//! working directory, then the process environment). The pool is read-only (baskfy-db).

use std::net::SocketAddr;

use baskfy_api::{AppState, Settings, build_app, logging};

fn arg(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

fn fail(message: &str) -> std::process::ExitCode {
    logging::error("baskfy_api.app", message, &[]);
    std::process::ExitCode::FAILURE
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let host = arg(&args, "--host").unwrap_or_else(|| "127.0.0.1".to_owned());
    let port = arg(&args, "--port").unwrap_or_else(|| "8000".to_owned());
    let settings = match Settings::from_env() {
        Ok(s) => s,
        Err(e) => return fail(&e.to_string()),
    };
    if let Err(e) = settings.require_configured() {
        return fail(&e);
    }
    logging::configure(&settings.log_level, settings.log_json);
    let environment = settings.environment.clone();
    let state = match AppState::from_settings(settings) {
        Ok(s) => s,
        Err(e) => return fail(&e.to_string()),
    };
    let app = build_app(state);
    let Ok(address) = format!("{host}:{port}").parse::<SocketAddr>() else {
        return fail("--host/--port do not form a socket address");
    };
    let listener = match tokio::net::TcpListener::bind(address).await {
        Ok(l) => l,
        Err(e) => return fail(&format!("cannot bind {address}: {e}")),
    };
    logging::info(
        "baskfy_api.app",
        "api started",
        &[("environment", &environment)],
    );
    let served = axum::serve(
        listener,
        app.into_make_service_with_connect_info::<SocketAddr>(),
    )
    .await;
    match served {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => fail(&format!("server stopped: {e}")),
    }
}
