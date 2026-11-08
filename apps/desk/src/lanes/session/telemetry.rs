//! `app/telemetry.py` — observability over the order path (M20 §1): spans, metrics, errors,
//! each optional, and **degrading to nothing is a feature, not a fallback**.
//!
//! The Mumbai box ran none of OpenTelemetry, `prometheus_client` or `sentry_sdk`; every import in
//! Python is guarded and an absent library makes its helper a no-op that cannot raise. This crate
//! links none of the three either (a new dependency is a DECISIONS-RS entry, and nothing on the
//! box consumed them), so the Rust port **is** Python's library-absent branch, exactly:
//! [`install`] reports what actually came up (nothing), logging one "not installed" warning per
//! view that was configured, and [`span`], [`count`], [`observe`], [`gauge`] and [`capture`] return
//! without effect and cannot fail. Observability must never be the reason an order does not get
//! placed.
//!
//! What it would record is shape, never content: no price, quantity, NAV or token is a label
//! here (the metric table [`METRICS`] names every series the alert rules read).

use std::sync::atomic::{AtomicBool, Ordering};

/// The env reads `telemetry.py` makes at import.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TelemetryConfig {
    /// `OTEL_EXPORTER_OTLP_ENDPOINT`.
    pub otlp_endpoint: String,
    /// `SENTRY_DSN`.
    pub sentry_dsn: String,
    /// `METRICS_PORT`.
    pub metrics_port: String,
    /// `OTEL_SERVICE_NAME` (`"momentum-desk"`).
    pub service_name: String,
}

impl TelemetryConfig {
    /// From the desk's settings table (R8.1: the env is read once, there).
    #[must_use]
    pub fn from_settings(settings: &crate::settings::Settings) -> Self {
        Self {
            otlp_endpoint: settings.otlp_endpoint.clone(),
            sentry_dsn: settings.sentry_dsn.clone(),
            metrics_port: settings.metrics_port.clone(),
            service_name: settings.service_name.clone(),
        }
    }
}

/// What came up — not what was configured (`{"traces", "metrics", "errors"}`).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Enabled {
    /// Spans.
    pub traces: bool,
    /// Prometheus.
    pub metrics: bool,
    /// Sentry.
    pub errors: bool,
}

static INSTALLED: AtomicBool = AtomicBool::new(false);

#[allow(clippy::print_stderr)]
fn warn(line: &str) {
    eprintln!("app.telemetry: {line}");
}

/// The warnings [`install`] would log for `config` (one per configured view: none of the
/// libraries is linked into this binary).
#[must_use]
pub fn install_warnings(config: &TelemetryConfig, metrics_port: Option<u16>) -> Vec<String> {
    let mut out = Vec::new();
    if !config.otlp_endpoint.is_empty() {
        out.push("tracing not installed: No module named 'opentelemetry'".to_owned());
    }
    let port = match metrics_port {
        Some(p) => Ok(i64::from(p)),
        None if config.metrics_port.is_empty() => Ok(0),
        None => py_int(&config.metrics_port).ok_or(()),
    };
    match port {
        Ok(0) => {}
        Ok(_) => out.push("metrics not installed: No module named 'prometheus_client'".to_owned()),
        // `int(METRICS_PORT)` raising escapes `install` in Python; here it is one warning.
        Err(()) => out.push(format!(
            "metrics not installed: invalid literal for int() with base 10: {}",
            baskfy_providers::py::repr_str(&config.metrics_port)
        )),
    }
    if !config.sentry_dsn.is_empty() {
        out.push("error capture not installed: No module named 'sentry_sdk'".to_owned());
    }
    out
}

/// `int(text)` for a base-10 string: surrounding whitespace, an optional sign, and digits with
/// single underscores between them (`"+9_100"` is 9100, `"-1"` is -1 — a truthy port).
fn py_int(text: &str) -> Option<i64> {
    let t = text.trim();
    let (sign, digits) = match t.strip_prefix('-') {
        Some(rest) => (-1, rest),
        None => (1, t.strip_prefix('+').unwrap_or(t)),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
        || !digits.chars().all(|c| c.is_ascii_digit() || c == '_')
    {
        return None;
    }
    digits
        .replace('_', "")
        .parse::<i64>()
        .ok()
        .map(|n| n * sign)
}

/// `install(metrics_port=None)`: turn on whichever views are configured and available. Safe to
/// call more than once; the views that came up are always none in this binary.
pub fn install(config: &TelemetryConfig, metrics_port: Option<u16>) -> Enabled {
    if INSTALLED.swap(true, Ordering::SeqCst) {
        return Enabled::default();
    }
    for line in install_warnings(config, metrics_port) {
        warn(&line);
    }
    Enabled::default()
}

/// Every metric the install path defines: `(key, Prometheus name, labels)` — the names
/// `infra/prometheus/alerts.yml` and the desk suite read.
pub const METRICS: &[(&str, &str, &[&str])] = &[
    ("swing_signals", "desk_swing_signals_total", &["state"]),
    (
        "swing_confirms",
        "desk_swing_confirms_total",
        &["kind", "outcome"],
    ),
    (
        "swing_notifications",
        "desk_swing_notifications_total",
        &["channel", "outcome"],
    ),
    (
        "swing_sweeps",
        "desk_swing_sweeps_total",
        &["sweep", "outcome"],
    ),
    ("swing_verdict_seconds", "desk_swing_verdict_seconds", &[]),
    ("swing_confirm_seconds", "desk_swing_confirm_seconds", &[]),
    ("swing_monitor_up", "desk_swing_monitor_up", &[]),
    (
        "swing_quote_polls",
        "desk_swing_quote_polls_total",
        &["outcome"],
    ),
    (
        "twt_auto_execute",
        "desk_twt_auto_execute_total",
        &["outcome"],
    ),
    ("twt_auto_runs", "desk_twt_auto_runs_total", &["outcome"]),
    ("plans", "desk_plans_built_total", &["source"]),
    ("orders", "desk_orders_total", &["action", "outcome"]),
    ("refusals", "desk_guard_refusals_total", &["guard"]),
    ("gtt", "desk_gtt_total", &["outcome"]),
    ("execute_seconds", "desk_execute_seconds", &[]),
    ("batch_size", "desk_execute_orders", &[]),
];

/// One span, or nothing at all: the body always runs, and its result comes back as itself.
pub fn span<T>(name: &str, attributes: &[(&str, &str)], body: impl FnOnce(Option<()>) -> T) -> T {
    let _ = (name, attributes);
    body(None)
}

/// Increment a counter if metrics are on. Never fails.
pub fn count(metric: &str, labels: &[(&str, &str)]) {
    let _ = (metric, labels);
}

/// Record into a histogram if metrics are on. Never fails.
pub fn observe(metric: &str, value: f64) {
    let _ = (metric, value);
}

/// Set a gauge if metrics are on. Never fails.
pub fn gauge(metric: &str, value: f64, labels: &[(&str, &str)]) {
    let _ = (metric, value, labels);
}

/// Send an error to Sentry with context, if error capture is on. Never fails.
pub fn capture(error: &dyn std::fmt::Display, context: &[(&str, &str)]) {
    let _ = (error.to_string(), context);
}

#[cfg(test)]
mod tests {
    //! `tests/test_telemetry.py`, ported (the branch that runs in production: nothing installed).
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn span_yields_and_does_not_raise_with_no_tracer() {
        let got = span("anything", &[("a", "b")], |current| {
            assert!(current.is_none());
            7
        });
        assert_eq!(got, 7);
    }

    #[test]
    fn counters_histograms_and_gauges_are_silent_when_metrics_are_off() {
        count("orders", &[("action", "BUY"), ("outcome", "COMPLETE")]);
        observe("execute_seconds", 1.25);
        gauge("swing_monitor_up", 1.0, &[]);
    }

    #[test]
    fn an_unknown_metric_name_is_not_an_error() {
        count("no_such_metric", &[("x", "y")]);
        observe("no_such_metric", 1.0);
    }

    #[test]
    fn capture_is_silent_without_a_dsn() {
        capture(&"boom", &[("plan_id", "p1")]);
    }

    #[test]
    fn the_bodys_own_error_comes_back_out_as_itself() {
        let r: Result<(), String> = span("desk.execute", &[], |_| Err("HTTP 400".to_owned()));
        assert_eq!(r, Err("HTTP 400".to_owned()));
    }

    #[test]
    fn the_metrics_that_exist_are_the_ones_alerts_reference() {
        for name in [
            "plans",
            "orders",
            "refusals",
            "gtt",
            "execute_seconds",
            "batch_size",
        ] {
            assert!(METRICS.iter().any(|(k, _, _)| *k == name), "{name}");
        }
        for (_, _, labels) in METRICS {
            for forbidden in ["price", "qty", "quantity", "nav", "token", "cash"] {
                assert!(!labels.iter().any(|l| l.contains(forbidden)));
            }
        }
    }

    #[test]
    fn install_reports_what_actually_came_up() {
        let configured = TelemetryConfig {
            otlp_endpoint: "http://collector:4318".to_owned(),
            sentry_dsn: "https://k@sentry.invalid/1".to_owned(),
            metrics_port: "9100".to_owned(),
            service_name: "momentum-desk".to_owned(),
        };
        // A DSN set against a missing library is off.
        assert_eq!(install(&configured, None), Enabled::default());
        assert_eq!(install(&configured, None), Enabled::default());
        assert_eq!(install_warnings(&configured, None).len(), 3);
        assert!(install_warnings(&TelemetryConfig::default(), None).is_empty());
        assert_eq!(
            install_warnings(&TelemetryConfig::default(), Some(9101)).len(),
            1
        );
        // `int(METRICS_PORT)` as Python reads it: a signed or underscored port still asks for
        // prometheus_client; only a non-number is the int() error.
        for port in [" +9_100 ", "-1"] {
            let c = TelemetryConfig {
                metrics_port: port.to_owned(),
                ..TelemetryConfig::default()
            };
            assert_eq!(
                install_warnings(&c, None),
                vec!["metrics not installed: No module named 'prometheus_client'".to_owned()]
            );
        }
        for port in ["0", " 0 "] {
            let c = TelemetryConfig {
                metrics_port: port.to_owned(),
                ..TelemetryConfig::default()
            };
            assert!(install_warnings(&c, None).is_empty(), "{port}");
        }
        let c = TelemetryConfig {
            metrics_port: "9_".to_owned(),
            ..TelemetryConfig::default()
        };
        assert!(install_warnings(&c, None)[0].contains("invalid literal for int()"));
    }

    #[test]
    fn nothing_here_can_place_an_order() {
        let source = include_str!("telemetry.rs");
        let code = source.split("#[cfg(test)]").next().unwrap();
        for forbidden in [
            "place_order",
            concat!("Kite", "Writes"),
            "Gateway",
            "place_gtt",
        ] {
            assert!(!code.contains(forbidden), "{forbidden}");
        }
    }
}
