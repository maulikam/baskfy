//! The worker's settings. Ports `baskfy_worker.settings.WorkerSettings`.
//!
//! The same 53 fields, defaults, environment names (`BASKFY_` + the field, and the desk's
//! unprefixed `DRY_RUN`, `OPTIONS_ENABLED`, `INTRADAY_ENABLED` for the three aliased ones),
//! pydantic's lax coercion and every `Field` constraint — held equal to Python's by
//! `tests/fixtures/python_worker_settings.json`, generated from `WorkerSettings.model_fields`
//! (`tests/settings.rs`). Read case-insensitively, process environment over `../../.env` over
//! `.env` over the defaults (R4.7's rules).
//!
//! The worker also reads `baskfy_api.settings.Settings` (the database URL, mail, the alert
//! sinks) — the Rust worker reads [`baskfy_api::Settings`] for those, as Python does.
//!
//! The parsers duplicate `baskfy_api::settings`' private ones (DECISIONS-RS R7.2: to collapse
//! into one shared module with the next API foundation change).

use std::collections::HashMap;
use std::path::PathBuf;

use chrono::NaiveTime;
use rust_decimal::Decimal;

use crate::error::WorkerError;

fn dec(text: &str) -> Decimal {
    Decimal::from_str_exact(text).unwrap_or_default()
}

fn time(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap_or_default()
}

/// `WorkerSettings`.
#[derive(Debug, Clone, PartialEq)]
pub struct WorkerSettings {
    /// `BASKFY_REDIS_URL` (Python default `redis://localhost:6380/0`).
    pub redis_url: String,
    /// `BASKFY_CELERY_BROKER_URL` (Python default ``).
    pub celery_broker_url: String,
    /// `BASKFY_CELERY_RESULT_BACKEND` (Python default ``).
    pub celery_result_backend: String,
    /// `BASKFY_INGEST_CONCURRENCY` (Python default `3`).
    pub ingest_concurrency: i64,
    /// `BASKFY_GATE_MIN_BAR_RATIO` (Python default `0.9`).
    pub gate_min_bar_ratio: f64,
    /// `BASKFY_GATE_MAX_UNEXPLAINED_RETURN` (Python default `0.5`).
    pub gate_max_unexplained_return: f64,
    /// `BASKFY_GATE_MEMBERSHIP_TOLERANCE` (Python default `0.05`).
    pub gate_membership_tolerance: f64,
    /// `BASKFY_GATE_INDEX_LEVEL_TOLERANCE` (Python default `0.001`).
    pub gate_index_level_tolerance: f64,
    /// `BASKFY_GATE_RET_MEDIAN_SIGMA` (Python default `3.0`).
    pub gate_ret_median_sigma: f64,
    /// `BASKFY_SWING_EXECUTION_ENABLED` (Python default `False`).
    pub swing_execution_enabled: bool,
    /// `BASKFY_SWING_MONITOR_ENABLED` (Python default `False`).
    pub swing_monitor_enabled: bool,
    /// `BASKFY_SWING_EP_PREMARKET_ENABLED` (Python default `False`).
    pub swing_ep_premarket_enabled: bool,
    /// `BASKFY_SWING_RISK_PER_TRADE_PCT_MAX` (Python default `1.0`).
    pub swing_risk_per_trade_pct_max: f64,
    /// `BASKFY_SWING_MAX_POSITION_PCT_MAX` (Python default `30.0`).
    pub swing_max_position_pct_max: f64,
    /// `BASKFY_SWING_MAX_OPEN_POSITIONS_MAX` (Python default `20`).
    pub swing_max_open_positions_max: i64,
    /// `BASKFY_SWING_INDEX_SLUG` (Python default `nifty-smallcap-250`).
    pub swing_index_slug: String,
    /// `BASKFY_SWING_TIMING_PROBE` (Python default `False`).
    pub swing_timing_probe: bool,
    /// `BASKFY_SWING_TIMING_PROBE_DIR` (Python default `docs/swing/status`).
    pub swing_timing_probe_dir: String,
    /// `BASKFY_VBT_EXECUTION_ENABLED` (Python default `False`).
    pub vbt_execution_enabled: bool,
    /// `BASKFY_VBT_NIGHTLY_ENABLED` (Python default `True`).
    pub vbt_nightly_enabled: bool,
    /// `BASKFY_VBT_MAX_OPEN_POSITIONS_MAX` (Python default `15`).
    pub vbt_max_open_positions_max: i64,
    /// `BASKFY_VBT_MAX_POSITION_PCT_MAX` (Python default `15.0`).
    pub vbt_max_position_pct_max: f64,
    /// `BASKFY_VBT_STOP_PCT_MAX` (Python default `15.0`).
    pub vbt_stop_pct_max: f64,
    /// `BASKFY_TWT_EXECUTION_ENABLED` (Python default `False`).
    pub twt_execution_enabled: bool,
    /// `BASKFY_TWT_NIGHTLY_ENABLED` (Python default `True`).
    pub twt_nightly_enabled: bool,
    /// `BASKFY_TWT_MAX_OPEN_POSITIONS_MAX` (Python default `15`).
    pub twt_max_open_positions_max: i64,
    /// `BASKFY_TWT_MAX_POSITION_PCT_MAX` (Python default `15.0`).
    pub twt_max_position_pct_max: f64,
    /// `BASKFY_TWT_STOP_PCT_MAX` (Python default `25.0`).
    pub twt_stop_pct_max: f64,
    /// `BASKFY_TWT_TRAIL_PCT_MIN` (Python default `18.0`).
    pub twt_trail_pct_min: f64,
    /// `BASKFY_OPTIONS_O1M_EXECUTION_ENABLED` (Python default `False`).
    pub options_o1m_execution_enabled: bool,
    /// `BASKFY_OPTIONS_O1W_EXECUTION_ENABLED` (Python default `False`).
    pub options_o1w_execution_enabled: bool,
    /// `BASKFY_OPTIONS_O2_EXECUTION_ENABLED` (Python default `False`).
    pub options_o2_execution_enabled: bool,
    /// `BASKFY_OPTIONS_O3_EXECUTION_ENABLED` (Python default `False`).
    pub options_o3_execution_enabled: bool,
    /// `BASKFY_OPTIONS_MONITOR_ENABLED` (Python default `False`).
    pub options_monitor_enabled: bool,
    /// `BASKFY_OPTIONS_COLLECT_ENABLED` (Python default `False`).
    pub options_collect_enabled: bool,
    /// `BASKFY_OPTIONS_SCAN_ENABLED` (Python default `False`).
    pub options_scan_enabled: bool,
    /// `BASKFY_EQ_BARS_ENABLED` (Python default `True`).
    pub eq_bars_enabled: bool,
    /// `DRY_RUN` (Python default `true`).
    pub options_desk_dry_run: String,
    /// `OPTIONS_ENABLED` (Python default `false`).
    pub options_desk_options_enabled: String,
    /// `INTRADAY_ENABLED` (Python default `false`).
    pub options_desk_intraday_enabled: String,
    /// `BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX` (Python default `25000`).
    pub options_risk_per_trade_inr_max: Decimal,
    /// `BASKFY_OPTIONS_RISK_PCT_MAX` (Python default `1.0`).
    pub options_risk_pct_max: Decimal,
    /// `BASKFY_OPTIONS_MAX_LOTS_MAX` (Python default `10`).
    pub options_max_lots_max: i64,
    /// `BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX` (Python default `30000`).
    pub options_book_daily_loss_inr_max: Decimal,
    /// `BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX` (Python default `75000`).
    pub options_book_monthly_loss_inr_max: Decimal,
    /// `BASKFY_OPTIONS_HARD_EXIT_LATEST` (Python default `15:00:00`).
    pub options_hard_exit_latest: NaiveTime,
    /// `BASKFY_FNO_SCAN_ENABLED` (Python default `False`).
    pub fno_scan_enabled: bool,
    /// `BASKFY_FNO_MONITOR_ENABLED` (Python default `False`).
    pub fno_monitor_enabled: bool,
    /// `BASKFY_KITE_LOGIN_NUDGE_ENABLED` (Python default `False`).
    pub kite_login_nudge_enabled: bool,
    /// `BASKFY_KITE_LOGIN_NUDGE_TO` (Python default ``).
    pub kite_login_nudge_to: String,
    /// `BASKFY_TASK_MAX_RETRIES` (Python default `3`).
    pub task_max_retries: i64,
    /// `BASKFY_TASK_MAX_REDELIVERIES` (Python default `3`).
    pub task_max_redeliveries: i64,
    /// `BASKFY_TASK_RETRY_BACKOFF_SECONDS` (Python default `60`).
    pub task_retry_backoff_seconds: i64,
}

impl Default for WorkerSettings {
    fn default() -> Self {
        Self {
            redis_url: "redis://localhost:6380/0".to_owned(),
            celery_broker_url: "".to_owned(),
            celery_result_backend: "".to_owned(),
            ingest_concurrency: 3,
            gate_min_bar_ratio: 0.9,
            gate_max_unexplained_return: 0.5,
            gate_membership_tolerance: 0.05,
            gate_index_level_tolerance: 0.001,
            gate_ret_median_sigma: 3.0,
            swing_execution_enabled: false,
            swing_monitor_enabled: false,
            swing_ep_premarket_enabled: false,
            swing_risk_per_trade_pct_max: 1.0,
            swing_max_position_pct_max: 30.0,
            swing_max_open_positions_max: 20,
            swing_index_slug: "nifty-smallcap-250".to_owned(),
            swing_timing_probe: false,
            swing_timing_probe_dir: "docs/swing/status".to_owned(),
            vbt_execution_enabled: false,
            vbt_nightly_enabled: true,
            vbt_max_open_positions_max: 15,
            vbt_max_position_pct_max: 15.0,
            vbt_stop_pct_max: 15.0,
            twt_execution_enabled: false,
            twt_nightly_enabled: true,
            twt_max_open_positions_max: 15,
            twt_max_position_pct_max: 15.0,
            twt_stop_pct_max: 25.0,
            twt_trail_pct_min: 18.0,
            options_o1m_execution_enabled: false,
            options_o1w_execution_enabled: false,
            options_o2_execution_enabled: false,
            options_o3_execution_enabled: false,
            options_monitor_enabled: false,
            options_collect_enabled: false,
            options_scan_enabled: false,
            eq_bars_enabled: true,
            options_desk_dry_run: "true".to_owned(),
            options_desk_options_enabled: "false".to_owned(),
            options_desk_intraday_enabled: "false".to_owned(),
            options_risk_per_trade_inr_max: dec("25000"),
            options_risk_pct_max: dec("1.0"),
            options_max_lots_max: 10,
            options_book_daily_loss_inr_max: dec("30000"),
            options_book_monthly_loss_inr_max: dec("75000"),
            options_hard_exit_latest: time(15, 0),
            fno_scan_enabled: false,
            fno_monitor_enabled: false,
            kite_login_nudge_enabled: false,
            kite_login_nudge_to: "".to_owned(),
            task_max_retries: 3,
            task_max_redeliveries: 3,
            task_retry_backoff_seconds: 60,
        }
    }
}

#[derive(Debug, Clone, Copy)]
enum Cmp {
    Gt,
    Ge,
    Le,
}

impl Cmp {
    fn words(self) -> &'static str {
        match self {
            Self::Gt => "greater than",
            Self::Ge => "greater than or equal to",
            Self::Le => "less than or equal to",
        }
    }
}

fn bound_f(errors: &mut Vec<String>, field: &str, value: f64, cmp: Cmp, x: f64) {
    let failed = match cmp {
        Cmp::Gt => value.is_nan() || value <= x,
        Cmp::Ge => value.is_nan() || value < x,
        Cmp::Le => value.is_nan() || value > x,
    };
    if failed {
        let shown = if x.fract() == 0.0 {
            format!("{x:.0}")
        } else {
            baskfy_core::py::float_repr(x)
        };
        errors.push(format!("{field}: Input should be {} {shown}", cmp.words()));
    }
}

fn bound_i(errors: &mut Vec<String>, field: &str, value: i64, cmp: Cmp, x: i64) {
    let failed = match cmp {
        Cmp::Gt => value <= x,
        Cmp::Ge => value < x,
        Cmp::Le => value > x,
    };
    if failed {
        errors.push(format!("{field}: Input should be {} {x}", cmp.words()));
    }
}

fn bound_d(errors: &mut Vec<String>, field: &str, value: Decimal, cmp: Cmp, x: &str) {
    let limit = dec(x);
    let failed = match cmp {
        Cmp::Gt => value <= limit,
        Cmp::Ge => value < limit,
        Cmp::Le => value > limit,
    };
    if failed {
        errors.push(format!("{field}: Input should be {} {x}", cmp.words()));
    }
}

mod parse {
    //! pydantic's lax coercion of one environment string.
    use chrono::NaiveTime;
    use rust_decimal::Decimal;

    pub(super) fn boolean(raw: &str) -> Result<bool, String> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "1" | "on" | "t" | "true" | "y" | "yes" => Ok(true),
            "0" | "off" | "f" | "false" | "n" | "no" => Ok(false),
            _ => Err("Input should be a valid boolean, unable to interpret input".to_owned()),
        }
    }

    fn digits(text: &str) -> Option<String> {
        let body = text.strip_prefix(['+', '-']).unwrap_or(text);
        if body.is_empty() || body.starts_with('_') || body.ends_with('_') || body.contains("__") {
            return None;
        }
        if !body.chars().all(|c| c.is_ascii_digit() || c == '_') {
            return None;
        }
        Some(text.replace('_', ""))
    }

    pub(super) fn integer(raw: &str) -> Result<i64, String> {
        let text = raw.trim();
        let message = "Input should be a valid integer, unable to parse string as an integer";
        let whole = match text.split_once('.') {
            Some((w, frac)) if !frac.is_empty() && frac.chars().all(|c| c == '0') => w,
            Some(_) => return Err(message.to_owned()),
            None => text,
        };
        digits(whole)
            .and_then(|d| d.parse::<i64>().ok())
            .ok_or_else(|| message.to_owned())
    }

    pub(super) fn float(raw: &str) -> Result<f64, String> {
        let text = raw.trim().replace('_', "");
        let lowered = text.to_ascii_lowercase();
        let parsed = match lowered.trim_start_matches(['+', '-']) {
            "inf" | "infinity" | "nan" => {
                let v = if lowered.contains("nan") {
                    f64::NAN
                } else {
                    f64::INFINITY
                };
                Some(if lowered.starts_with('-') { -v } else { v })
            }
            _ => text.parse::<f64>().ok(),
        };
        parsed.ok_or_else(|| {
            "Input should be a valid number, unable to parse string as a number".to_owned()
        })
    }

    pub(super) fn decimal(raw: &str) -> Result<Decimal, String> {
        let text = raw.trim();
        Decimal::from_str_exact(text)
            .or_else(|_| Decimal::from_scientific(text))
            .map_err(|_| "Input should be a valid decimal".to_owned())
    }

    pub(super) fn time(raw: &str) -> Result<NaiveTime, String> {
        let text = raw.trim();
        ["%H:%M:%S%.f", "%H:%M:%S", "%H:%M"]
            .iter()
            .find_map(|fmt| NaiveTime::parse_from_str(text, fmt).ok())
            .ok_or_else(|| "Input should be in a valid time format".to_owned())
    }
}

/// One `KEY=VALUE` per line, `#` comments, optional quotes (python-dotenv's simple cases).
fn read_dotenv(path: &PathBuf) -> Vec<(String, String)> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let line = line.strip_prefix("export ").unwrap_or(line);
        if let Some((k, v)) = line.split_once('=') {
            let v = v.trim();
            let v = v
                .strip_prefix('"')
                .and_then(|x| x.strip_suffix('"'))
                .or_else(|| v.strip_prefix('\'').and_then(|x| x.strip_suffix('\'')))
                .unwrap_or(v);
            out.push((k.trim().to_owned(), v.to_owned()));
        }
    }
    out
}

impl WorkerSettings {
    /// Every field, in Python's declaration order.
    pub const FIELDS: [&'static str; 53] = [
        "redis_url",
        "celery_broker_url",
        "celery_result_backend",
        "ingest_concurrency",
        "gate_min_bar_ratio",
        "gate_max_unexplained_return",
        "gate_membership_tolerance",
        "gate_index_level_tolerance",
        "gate_ret_median_sigma",
        "swing_execution_enabled",
        "swing_monitor_enabled",
        "swing_ep_premarket_enabled",
        "swing_risk_per_trade_pct_max",
        "swing_max_position_pct_max",
        "swing_max_open_positions_max",
        "swing_index_slug",
        "swing_timing_probe",
        "swing_timing_probe_dir",
        "vbt_execution_enabled",
        "vbt_nightly_enabled",
        "vbt_max_open_positions_max",
        "vbt_max_position_pct_max",
        "vbt_stop_pct_max",
        "twt_execution_enabled",
        "twt_nightly_enabled",
        "twt_max_open_positions_max",
        "twt_max_position_pct_max",
        "twt_stop_pct_max",
        "twt_trail_pct_min",
        "options_o1m_execution_enabled",
        "options_o1w_execution_enabled",
        "options_o2_execution_enabled",
        "options_o3_execution_enabled",
        "options_monitor_enabled",
        "options_collect_enabled",
        "options_scan_enabled",
        "eq_bars_enabled",
        "options_desk_dry_run",
        "options_desk_options_enabled",
        "options_desk_intraday_enabled",
        "options_risk_per_trade_inr_max",
        "options_risk_pct_max",
        "options_max_lots_max",
        "options_book_daily_loss_inr_max",
        "options_book_monthly_loss_inr_max",
        "options_hard_exit_latest",
        "fno_scan_enabled",
        "fno_monitor_enabled",
        "kite_login_nudge_enabled",
        "kite_login_nudge_to",
        "task_max_retries",
        "task_max_redeliveries",
        "task_retry_backoff_seconds",
    ];

    /// The environment variable each field reads, in [`Self::FIELDS`]' order.
    pub const ENV_NAMES: [&'static str; 53] = [
        "BASKFY_REDIS_URL",
        "BASKFY_CELERY_BROKER_URL",
        "BASKFY_CELERY_RESULT_BACKEND",
        "BASKFY_INGEST_CONCURRENCY",
        "BASKFY_GATE_MIN_BAR_RATIO",
        "BASKFY_GATE_MAX_UNEXPLAINED_RETURN",
        "BASKFY_GATE_MEMBERSHIP_TOLERANCE",
        "BASKFY_GATE_INDEX_LEVEL_TOLERANCE",
        "BASKFY_GATE_RET_MEDIAN_SIGMA",
        "BASKFY_SWING_EXECUTION_ENABLED",
        "BASKFY_SWING_MONITOR_ENABLED",
        "BASKFY_SWING_EP_PREMARKET_ENABLED",
        "BASKFY_SWING_RISK_PER_TRADE_PCT_MAX",
        "BASKFY_SWING_MAX_POSITION_PCT_MAX",
        "BASKFY_SWING_MAX_OPEN_POSITIONS_MAX",
        "BASKFY_SWING_INDEX_SLUG",
        "BASKFY_SWING_TIMING_PROBE",
        "BASKFY_SWING_TIMING_PROBE_DIR",
        "BASKFY_VBT_EXECUTION_ENABLED",
        "BASKFY_VBT_NIGHTLY_ENABLED",
        "BASKFY_VBT_MAX_OPEN_POSITIONS_MAX",
        "BASKFY_VBT_MAX_POSITION_PCT_MAX",
        "BASKFY_VBT_STOP_PCT_MAX",
        "BASKFY_TWT_EXECUTION_ENABLED",
        "BASKFY_TWT_NIGHTLY_ENABLED",
        "BASKFY_TWT_MAX_OPEN_POSITIONS_MAX",
        "BASKFY_TWT_MAX_POSITION_PCT_MAX",
        "BASKFY_TWT_STOP_PCT_MAX",
        "BASKFY_TWT_TRAIL_PCT_MIN",
        "BASKFY_OPTIONS_O1M_EXECUTION_ENABLED",
        "BASKFY_OPTIONS_O1W_EXECUTION_ENABLED",
        "BASKFY_OPTIONS_O2_EXECUTION_ENABLED",
        "BASKFY_OPTIONS_O3_EXECUTION_ENABLED",
        "BASKFY_OPTIONS_MONITOR_ENABLED",
        "BASKFY_OPTIONS_COLLECT_ENABLED",
        "BASKFY_OPTIONS_SCAN_ENABLED",
        "BASKFY_EQ_BARS_ENABLED",
        "DRY_RUN",
        "OPTIONS_ENABLED",
        "INTRADAY_ENABLED",
        "BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX",
        "BASKFY_OPTIONS_RISK_PCT_MAX",
        "BASKFY_OPTIONS_MAX_LOTS_MAX",
        "BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX",
        "BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX",
        "BASKFY_OPTIONS_HARD_EXIT_LATEST",
        "BASKFY_FNO_SCAN_ENABLED",
        "BASKFY_FNO_MONITOR_ENABLED",
        "BASKFY_KITE_LOGIN_NUDGE_ENABLED",
        "BASKFY_KITE_LOGIN_NUDGE_TO",
        "BASKFY_TASK_MAX_RETRIES",
        "BASKFY_TASK_MAX_REDELIVERIES",
        "BASKFY_TASK_RETRY_BACKOFF_SECONDS",
    ];

    /// `get_worker_settings()`: the process environment over the dotenv files over defaults.
    ///
    /// # Errors
    /// [`WorkerError::Settings`] listing every field that does not parse or validate.
    pub fn from_env() -> Result<Self, WorkerError> {
        let files = [PathBuf::from(".env"), PathBuf::from("../../.env")];
        Self::from_sources(
            std::env::vars_os()
                .filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?))),
            &files,
        )
    }

    /// The settings from an explicit environment and dotenv files.
    ///
    /// # Errors
    /// As [`WorkerSettings::from_env`].
    pub fn from_sources(
        env: impl IntoIterator<Item = (String, String)>,
        env_files: &[PathBuf],
    ) -> Result<Self, WorkerError> {
        let mut values: HashMap<String, String> = HashMap::new();
        for file in env_files {
            for (k, v) in read_dotenv(file) {
                values.insert(k.to_lowercase(), v);
            }
        }
        for (k, v) in env {
            values.insert(k.to_lowercase(), v);
        }
        let mut settings = Self::default();
        let mut errors = Vec::new();
        for (field, env_name) in Self::FIELDS.iter().zip(Self::ENV_NAMES) {
            if let Some(raw) = values.get(&env_name.to_lowercase())
                && let Err(e) = settings.set(field, raw)
            {
                errors.push(format!("{field}: {e}"));
            }
        }
        if errors.is_empty() {
            errors = settings.field_errors();
        }
        if !errors.is_empty() {
            return Err(WorkerError::Settings(errors.join("; ")));
        }
        Ok(settings)
    }

    fn set(&mut self, field: &str, raw: &str) -> Result<(), String> {
        match field {
            "redis_url" => raw.clone_into(&mut self.redis_url),
            "celery_broker_url" => raw.clone_into(&mut self.celery_broker_url),
            "celery_result_backend" => raw.clone_into(&mut self.celery_result_backend),
            "ingest_concurrency" => self.ingest_concurrency = parse::integer(raw)?,
            "gate_min_bar_ratio" => self.gate_min_bar_ratio = parse::float(raw)?,
            "gate_max_unexplained_return" => self.gate_max_unexplained_return = parse::float(raw)?,
            "gate_membership_tolerance" => self.gate_membership_tolerance = parse::float(raw)?,
            "gate_index_level_tolerance" => self.gate_index_level_tolerance = parse::float(raw)?,
            "gate_ret_median_sigma" => self.gate_ret_median_sigma = parse::float(raw)?,
            "swing_execution_enabled" => self.swing_execution_enabled = parse::boolean(raw)?,
            "swing_monitor_enabled" => self.swing_monitor_enabled = parse::boolean(raw)?,
            "swing_ep_premarket_enabled" => self.swing_ep_premarket_enabled = parse::boolean(raw)?,
            "swing_risk_per_trade_pct_max" => {
                self.swing_risk_per_trade_pct_max = parse::float(raw)?
            }
            "swing_max_position_pct_max" => self.swing_max_position_pct_max = parse::float(raw)?,
            "swing_max_open_positions_max" => {
                self.swing_max_open_positions_max = parse::integer(raw)?
            }
            "swing_index_slug" => raw.clone_into(&mut self.swing_index_slug),
            "swing_timing_probe" => self.swing_timing_probe = parse::boolean(raw)?,
            "swing_timing_probe_dir" => raw.clone_into(&mut self.swing_timing_probe_dir),
            "vbt_execution_enabled" => self.vbt_execution_enabled = parse::boolean(raw)?,
            "vbt_nightly_enabled" => self.vbt_nightly_enabled = parse::boolean(raw)?,
            "vbt_max_open_positions_max" => self.vbt_max_open_positions_max = parse::integer(raw)?,
            "vbt_max_position_pct_max" => self.vbt_max_position_pct_max = parse::float(raw)?,
            "vbt_stop_pct_max" => self.vbt_stop_pct_max = parse::float(raw)?,
            "twt_execution_enabled" => self.twt_execution_enabled = parse::boolean(raw)?,
            "twt_nightly_enabled" => self.twt_nightly_enabled = parse::boolean(raw)?,
            "twt_max_open_positions_max" => self.twt_max_open_positions_max = parse::integer(raw)?,
            "twt_max_position_pct_max" => self.twt_max_position_pct_max = parse::float(raw)?,
            "twt_stop_pct_max" => self.twt_stop_pct_max = parse::float(raw)?,
            "twt_trail_pct_min" => self.twt_trail_pct_min = parse::float(raw)?,
            "options_o1m_execution_enabled" => {
                self.options_o1m_execution_enabled = parse::boolean(raw)?
            }
            "options_o1w_execution_enabled" => {
                self.options_o1w_execution_enabled = parse::boolean(raw)?
            }
            "options_o2_execution_enabled" => {
                self.options_o2_execution_enabled = parse::boolean(raw)?
            }
            "options_o3_execution_enabled" => {
                self.options_o3_execution_enabled = parse::boolean(raw)?
            }
            "options_monitor_enabled" => self.options_monitor_enabled = parse::boolean(raw)?,
            "options_collect_enabled" => self.options_collect_enabled = parse::boolean(raw)?,
            "options_scan_enabled" => self.options_scan_enabled = parse::boolean(raw)?,
            "eq_bars_enabled" => self.eq_bars_enabled = parse::boolean(raw)?,
            "options_desk_dry_run" => raw.clone_into(&mut self.options_desk_dry_run),
            "options_desk_options_enabled" => {
                raw.clone_into(&mut self.options_desk_options_enabled)
            }
            "options_desk_intraday_enabled" => {
                raw.clone_into(&mut self.options_desk_intraday_enabled)
            }
            "options_risk_per_trade_inr_max" => {
                self.options_risk_per_trade_inr_max = parse::decimal(raw)?
            }
            "options_risk_pct_max" => self.options_risk_pct_max = parse::decimal(raw)?,
            "options_max_lots_max" => self.options_max_lots_max = parse::integer(raw)?,
            "options_book_daily_loss_inr_max" => {
                self.options_book_daily_loss_inr_max = parse::decimal(raw)?
            }
            "options_book_monthly_loss_inr_max" => {
                self.options_book_monthly_loss_inr_max = parse::decimal(raw)?
            }
            "options_hard_exit_latest" => self.options_hard_exit_latest = parse::time(raw)?,
            "fno_scan_enabled" => self.fno_scan_enabled = parse::boolean(raw)?,
            "fno_monitor_enabled" => self.fno_monitor_enabled = parse::boolean(raw)?,
            "kite_login_nudge_enabled" => self.kite_login_nudge_enabled = parse::boolean(raw)?,
            "kite_login_nudge_to" => raw.clone_into(&mut self.kite_login_nudge_to),
            "task_max_retries" => self.task_max_retries = parse::integer(raw)?,
            "task_max_redeliveries" => self.task_max_redeliveries = parse::integer(raw)?,
            "task_retry_backoff_seconds" => self.task_retry_backoff_seconds = parse::integer(raw)?,
            _ => return Err(format!("unknown field {field}")),
        }
        Ok(())
    }

    fn field_errors(&self) -> Vec<String> {
        let mut errors = Vec::new();
        bound_i(
            &mut errors,
            "ingest_concurrency",
            self.ingest_concurrency,
            Cmp::Gt,
            0,
        );
        bound_f(
            &mut errors,
            "gate_min_bar_ratio",
            self.gate_min_bar_ratio,
            Cmp::Gt,
            0.0,
        );
        bound_f(
            &mut errors,
            "gate_min_bar_ratio",
            self.gate_min_bar_ratio,
            Cmp::Le,
            1.0,
        );
        bound_f(
            &mut errors,
            "gate_max_unexplained_return",
            self.gate_max_unexplained_return,
            Cmp::Gt,
            0.0,
        );
        bound_f(
            &mut errors,
            "gate_membership_tolerance",
            self.gate_membership_tolerance,
            Cmp::Gt,
            0.0,
        );
        bound_f(
            &mut errors,
            "gate_membership_tolerance",
            self.gate_membership_tolerance,
            Cmp::Le,
            1.0,
        );
        bound_f(
            &mut errors,
            "gate_index_level_tolerance",
            self.gate_index_level_tolerance,
            Cmp::Gt,
            0.0,
        );
        bound_f(
            &mut errors,
            "gate_ret_median_sigma",
            self.gate_ret_median_sigma,
            Cmp::Gt,
            0.0,
        );
        bound_f(
            &mut errors,
            "swing_risk_per_trade_pct_max",
            self.swing_risk_per_trade_pct_max,
            Cmp::Gt,
            0.0,
        );
        bound_f(
            &mut errors,
            "swing_risk_per_trade_pct_max",
            self.swing_risk_per_trade_pct_max,
            Cmp::Le,
            5.0,
        );
        bound_f(
            &mut errors,
            "swing_max_position_pct_max",
            self.swing_max_position_pct_max,
            Cmp::Gt,
            0.0,
        );
        bound_f(
            &mut errors,
            "swing_max_position_pct_max",
            self.swing_max_position_pct_max,
            Cmp::Le,
            100.0,
        );
        bound_i(
            &mut errors,
            "swing_max_open_positions_max",
            self.swing_max_open_positions_max,
            Cmp::Gt,
            0,
        );
        bound_i(
            &mut errors,
            "swing_max_open_positions_max",
            self.swing_max_open_positions_max,
            Cmp::Le,
            50,
        );
        bound_i(
            &mut errors,
            "vbt_max_open_positions_max",
            self.vbt_max_open_positions_max,
            Cmp::Gt,
            0,
        );
        bound_i(
            &mut errors,
            "vbt_max_open_positions_max",
            self.vbt_max_open_positions_max,
            Cmp::Le,
            30,
        );
        bound_f(
            &mut errors,
            "vbt_max_position_pct_max",
            self.vbt_max_position_pct_max,
            Cmp::Gt,
            0.0,
        );
        bound_f(
            &mut errors,
            "vbt_max_position_pct_max",
            self.vbt_max_position_pct_max,
            Cmp::Le,
            100.0,
        );
        bound_f(
            &mut errors,
            "vbt_stop_pct_max",
            self.vbt_stop_pct_max,
            Cmp::Gt,
            0.0,
        );
        bound_f(
            &mut errors,
            "vbt_stop_pct_max",
            self.vbt_stop_pct_max,
            Cmp::Le,
            50.0,
        );
        bound_i(
            &mut errors,
            "twt_max_open_positions_max",
            self.twt_max_open_positions_max,
            Cmp::Gt,
            0,
        );
        bound_i(
            &mut errors,
            "twt_max_open_positions_max",
            self.twt_max_open_positions_max,
            Cmp::Le,
            30,
        );
        bound_f(
            &mut errors,
            "twt_max_position_pct_max",
            self.twt_max_position_pct_max,
            Cmp::Gt,
            0.0,
        );
        bound_f(
            &mut errors,
            "twt_max_position_pct_max",
            self.twt_max_position_pct_max,
            Cmp::Le,
            100.0,
        );
        bound_f(
            &mut errors,
            "twt_stop_pct_max",
            self.twt_stop_pct_max,
            Cmp::Gt,
            0.0,
        );
        bound_f(
            &mut errors,
            "twt_stop_pct_max",
            self.twt_stop_pct_max,
            Cmp::Le,
            50.0,
        );
        bound_f(
            &mut errors,
            "twt_trail_pct_min",
            self.twt_trail_pct_min,
            Cmp::Gt,
            0.0,
        );
        bound_f(
            &mut errors,
            "twt_trail_pct_min",
            self.twt_trail_pct_min,
            Cmp::Le,
            100.0,
        );
        bound_d(
            &mut errors,
            "options_risk_per_trade_inr_max",
            self.options_risk_per_trade_inr_max,
            Cmp::Gt,
            "0",
        );
        bound_d(
            &mut errors,
            "options_risk_pct_max",
            self.options_risk_pct_max,
            Cmp::Gt,
            "0",
        );
        bound_d(
            &mut errors,
            "options_risk_pct_max",
            self.options_risk_pct_max,
            Cmp::Le,
            "100",
        );
        bound_i(
            &mut errors,
            "options_max_lots_max",
            self.options_max_lots_max,
            Cmp::Gt,
            0,
        );
        bound_i(
            &mut errors,
            "options_max_lots_max",
            self.options_max_lots_max,
            Cmp::Le,
            100,
        );
        bound_d(
            &mut errors,
            "options_book_daily_loss_inr_max",
            self.options_book_daily_loss_inr_max,
            Cmp::Gt,
            "0",
        );
        bound_d(
            &mut errors,
            "options_book_monthly_loss_inr_max",
            self.options_book_monthly_loss_inr_max,
            Cmp::Gt,
            "0",
        );
        bound_i(
            &mut errors,
            "task_max_retries",
            self.task_max_retries,
            Cmp::Ge,
            0,
        );
        bound_i(
            &mut errors,
            "task_max_redeliveries",
            self.task_max_redeliveries,
            Cmp::Ge,
            0,
        );
        bound_i(
            &mut errors,
            "task_retry_backoff_seconds",
            self.task_retry_backoff_seconds,
            Cmp::Gt,
            0,
        );
        errors
    }

    /// A field's value as Python's `str()` would print it (`True`, `0.9`, `15:00:00`); the
    /// fixture test compares these with the Python defaults.
    #[must_use]
    pub fn show(&self, field: &str) -> Option<String> {
        Some(match field {
            "redis_url" => self.redis_url.clone(),
            "celery_broker_url" => self.celery_broker_url.clone(),
            "celery_result_backend" => self.celery_result_backend.clone(),
            "ingest_concurrency" => self.ingest_concurrency.to_string(),
            "gate_min_bar_ratio" => baskfy_core::py::float_repr(self.gate_min_bar_ratio),
            "gate_max_unexplained_return" => {
                baskfy_core::py::float_repr(self.gate_max_unexplained_return)
            }
            "gate_membership_tolerance" => {
                baskfy_core::py::float_repr(self.gate_membership_tolerance)
            }
            "gate_index_level_tolerance" => {
                baskfy_core::py::float_repr(self.gate_index_level_tolerance)
            }
            "gate_ret_median_sigma" => baskfy_core::py::float_repr(self.gate_ret_median_sigma),
            "swing_execution_enabled" => (if self.swing_execution_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "swing_monitor_enabled" => (if self.swing_monitor_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "swing_ep_premarket_enabled" => (if self.swing_ep_premarket_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "swing_risk_per_trade_pct_max" => {
                baskfy_core::py::float_repr(self.swing_risk_per_trade_pct_max)
            }
            "swing_max_position_pct_max" => {
                baskfy_core::py::float_repr(self.swing_max_position_pct_max)
            }
            "swing_max_open_positions_max" => self.swing_max_open_positions_max.to_string(),
            "swing_index_slug" => self.swing_index_slug.clone(),
            "swing_timing_probe" => (if self.swing_timing_probe {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "swing_timing_probe_dir" => self.swing_timing_probe_dir.clone(),
            "vbt_execution_enabled" => (if self.vbt_execution_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "vbt_nightly_enabled" => (if self.vbt_nightly_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "vbt_max_open_positions_max" => self.vbt_max_open_positions_max.to_string(),
            "vbt_max_position_pct_max" => {
                baskfy_core::py::float_repr(self.vbt_max_position_pct_max)
            }
            "vbt_stop_pct_max" => baskfy_core::py::float_repr(self.vbt_stop_pct_max),
            "twt_execution_enabled" => (if self.twt_execution_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "twt_nightly_enabled" => (if self.twt_nightly_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "twt_max_open_positions_max" => self.twt_max_open_positions_max.to_string(),
            "twt_max_position_pct_max" => {
                baskfy_core::py::float_repr(self.twt_max_position_pct_max)
            }
            "twt_stop_pct_max" => baskfy_core::py::float_repr(self.twt_stop_pct_max),
            "twt_trail_pct_min" => baskfy_core::py::float_repr(self.twt_trail_pct_min),
            "options_o1m_execution_enabled" => (if self.options_o1m_execution_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "options_o1w_execution_enabled" => (if self.options_o1w_execution_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "options_o2_execution_enabled" => (if self.options_o2_execution_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "options_o3_execution_enabled" => (if self.options_o3_execution_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "options_monitor_enabled" => (if self.options_monitor_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "options_collect_enabled" => (if self.options_collect_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "options_scan_enabled" => (if self.options_scan_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "eq_bars_enabled" => (if self.eq_bars_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "options_desk_dry_run" => self.options_desk_dry_run.clone(),
            "options_desk_options_enabled" => self.options_desk_options_enabled.clone(),
            "options_desk_intraday_enabled" => self.options_desk_intraday_enabled.clone(),
            "options_risk_per_trade_inr_max" => self.options_risk_per_trade_inr_max.to_string(),
            "options_risk_pct_max" => self.options_risk_pct_max.to_string(),
            "options_max_lots_max" => self.options_max_lots_max.to_string(),
            "options_book_daily_loss_inr_max" => self.options_book_daily_loss_inr_max.to_string(),
            "options_book_monthly_loss_inr_max" => {
                self.options_book_monthly_loss_inr_max.to_string()
            }
            "options_hard_exit_latest" => {
                self.options_hard_exit_latest.format("%H:%M:%S").to_string()
            }
            "fno_scan_enabled" => (if self.fno_scan_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "fno_monitor_enabled" => (if self.fno_monitor_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "kite_login_nudge_enabled" => (if self.kite_login_nudge_enabled {
                "True"
            } else {
                "False"
            })
            .to_owned(),
            "kite_login_nudge_to" => self.kite_login_nudge_to.clone(),
            "task_max_retries" => self.task_max_retries.to_string(),
            "task_max_redeliveries" => self.task_max_redeliveries.to_string(),
            "task_retry_backoff_seconds" => self.task_retry_backoff_seconds.to_string(),
            _ => return None,
        })
    }

    /// `broker_url()`.
    #[must_use]
    pub fn broker_url(&self) -> &str {
        if self.celery_broker_url.is_empty() {
            &self.redis_url
        } else {
            &self.celery_broker_url
        }
    }
}
