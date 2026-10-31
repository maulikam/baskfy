//! What importing `app.main` does before the first request: tighten the credential files,
//! apply the stored settings, state the risk ceilings, report observability.

use crate::app::AppState;
use crate::settings::Settings;

/// `websec.SECRET_FILES`.
pub const SECRET_FILES: [&str; 2] = [".env", "data/.kite_token.json"];

#[allow(clippy::print_stderr)]
fn log_line(line: &str) {
    eprintln!("{line}");
}

/// One credential file a group or other account could read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LooseSecret {
    /// The name as listed.
    pub file: String,
    /// `oct(mode)`.
    pub mode: String,
    /// Whether it was set to 0600.
    pub fixed: bool,
}

/// `check_secret_permissions(fix=)`, relative to the desk's working directory.
#[must_use]
pub fn check_secret_permissions(state: &AppState, fix: bool) -> Vec<LooseSecret> {
    let mut out = Vec::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        for name in SECRET_FILES {
            let path = state.paths.resolve(name);
            let Ok(meta) = std::fs::metadata(&path) else {
                continue;
            };
            let mode = meta.permissions().mode() & 0o777;
            if mode & 0o077 != 0 {
                if fix {
                    let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
                }
                out.push(LooseSecret {
                    file: name.to_owned(),
                    mode: format!("0o{mode:o}"),
                    fixed: fix,
                });
            }
        }
    }
    #[cfg(not(unix))]
    let _ = (state, fix);
    out
}

/// `C.RISK_MAX_POSITION_VALUE or "derived"` as `%s` prints it.
fn or_derived(raw: &str) -> String {
    match crate::settings::py_float(raw) {
        Some(v) if v != 0.0 => baskfy_core::py::float_repr(v),
        _ => "derived".to_owned(),
    }
}

/// `_log_risk_ceilings()`'s line: percentages and multiples only.
#[must_use]
pub fn risk_ceilings_line(s: &Settings) -> String {
    format!(
        "INFO:root:risk ceilings in force (env-only, not editable from /settings): \
         daily_loss={:.2}% position_headroom={:.2}x gross_multiple={:.2}x \
         max_orders_per_day={} max_position_value={} max_gross_exposure={}",
        s.risk_max_daily_loss_pct,
        s.risk_position_headroom,
        s.risk_gross_multiple,
        s.risk_max_orders_per_day,
        or_derived(&s.risk_max_position_value),
        or_derived(&s.risk_max_gross_exposure),
    )
}

fn py_bool(b: bool) -> &'static str {
    if b { "True" } else { "False" }
}

/// The import-time sequence of `app.main`, in Python's order. Never refuses a boot.
pub async fn boot(state: &AppState) {
    for bad in check_secret_permissions(state, true) {
        log_line(&format!(
            "WARNING:root:tightened {} from {} (readable by other accounts on this host)",
            bad.file, bad.mode
        ));
    }
    match super::startup(state).await {
        Ok(mut applied) if !applied.is_empty() => {
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
    let settings = state.settings_snapshot();
    log_line(&risk_ceilings_line(&settings));
    let on = super::telemetry::install(
        &super::telemetry::TelemetryConfig::from_settings(&settings),
        None,
    );
    log_line(&format!(
        "INFO:root:observability: {{'traces': {}, 'metrics': {}, 'errors': {}}}",
        py_bool(on.traces),
        py_bool(on.metrics),
        py_bool(on.errors)
    ));
}
