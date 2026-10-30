//! `app/core/regime.py` (a re-export of `baskfy_core.exposure.regime`) — the parts the desk's
//! read-only regime pages need: the tiers, the reason codes and their display text, and
//! `config.regime_config()`'s [`RegimeConfig`] with its `canonical()`/`config_hash()`.
//!
//! **Not ported (loud):** the regime *engine* (`evaluate`, the MA signals, the classifier, the
//! transition rules) and `core/regime_alloc.py` (`baskfy_core.exposure.allocation`). Both are
//! reached only from `analytics/regime_run.py` — the weekly/daily evaluation CLI that writes
//! `regime_evaluations` — which stays on Python (DECISIONS-RS, lane analytics_regime, label R-ar.1). The pages read the rows it
//! wrote; they never recompute a signal.

use crate::settings::Settings;

/// `ALGORITHM_VERSION`.
pub const ALGORITHM_VERSION: &str = "regime/1.0.0";

/// `RegimeTier`'s values, most risk-on first.
pub const TIERS: [&str; 4] = ["R1", "R2", "R3", "R4"];

/// `AFFIRMATIVE_RISK_OFF`.
pub const AFFIRMATIVE_RISK_OFF: [&str; 7] = [
    "R4_BEARISH_STACK_AND_WEAK_BREADTH",
    "R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH",
    "R3_H200_WEAK",
    "R3_BREADTH_WEAK",
    "R3_SENTINEL_BELOW_200DMA",
    "SENTINEL_FLOOR_R2",
    "SENTINEL_FLOOR_R3",
];

/// `code in AFFIRMATIVE_RISK_OFF`.
#[must_use]
pub fn is_affirmative_risk_off(code: &str) -> bool {
    AFFIRMATIVE_RISK_OFF.contains(&code)
}

/// `DISPLAY_REASONS`, in the module's order.
pub const DISPLAY_REASONS: &[(&str, &str)] = &[
    (
        "DATA_STALE",
        "Index data is stale — holding the previous tier and blocking new buys.",
    ),
    (
        "DATA_INSUFFICIENT_WARMUP",
        "Not enough history to establish every moving average.",
    ),
    (
        "DATA_INDEX_MISSING",
        "A required structural index has no usable data.",
    ),
    (
        "SIGNAL_UNKNOWN",
        "One or more index/MA pairs have not yet confirmed a side.",
    ),
    ("BREADTH_MISSING", "No breadth reading supplied."),
    (
        "BREADTH_STALE",
        "Breadth reading is older than the staleness allowance.",
    ),
    (
        "BREADTH_LOW_COVERAGE",
        "Breadth coverage is below the configured minimum.",
    ),
    (
        "BREADTH_NOT_USED",
        "This configuration does not use breadth; breadth rules are inactive.",
    ),
    (
        "R4_BEARISH_STACK_AND_WEAK_BREADTH",
        "Bearish MA stack across most of the book with very weak breadth.",
    ),
    (
        "R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH",
        "Momentum sentinel below its 200-DMA with weak long-term health and breadth.",
    ),
    (
        "R3_H200_WEAK",
        "Long-term health (H200) at or below the R3 threshold.",
    ),
    ("R3_BREADTH_WEAK", "Breadth below the R3 threshold."),
    (
        "R3_SENTINEL_BELOW_200DMA",
        "Momentum sentinel confirmed below its 200-DMA.",
    ),
    (
        "R1_ALL_CONDITIONS_MET",
        "Health, breadth and the momentum sentinel all risk-on.",
    ),
    (
        "R2_DEFAULT",
        "No risk-off rule fired and R1 conditions were not all met.",
    ),
    (
        "SENTINEL_BELOW_50DMA_VETO",
        "Momentum sentinel below its 50-DMA — new entries vetoed.",
    ),
    (
        "SENTINEL_FLOOR_R2",
        "Sentinel 50-DMA veto floors the candidate tier at R2.",
    ),
    (
        "SENTINEL_FLOOR_R3",
        "Sentinel 200-DMA break floors the candidate tier at R3.",
    ),
    (
        "SENTINEL_UNKNOWN",
        "Momentum sentinel state is not yet confirmed.",
    ),
    (
        "RECOVERY_OVERRIDE_APPLIED",
        "Breadth recovery override lifted the tier to R2.",
    ),
    (
        "RECOVERY_OVERRIDE_UNCONFIRMED",
        "Recovery conditions met but not yet confirmed for long enough.",
    ),
    (
        "RECOVERY_OVERRIDE_H20_TOO_WEAK",
        "Short-term health below the recovery threshold.",
    ),
    (
        "RECOVERY_OVERRIDE_BREADTH_UNUSABLE",
        "Recovery override needs current, well-covered breadth.",
    ),
    (
        "TRANSITION_RISK_OFF_DIRECT",
        "Risk reduction applied directly to the candidate tier.",
    ),
    (
        "TRANSITION_RERISK_CAPPED",
        "Re-risking limited to one tier per weekly evaluation.",
    ),
    ("TRANSITION_NONE", "No tier change this evaluation."),
    (
        "TRANSITION_HELD_STALE_DATA",
        "Previous tier retained: inputs are degraded and no confirmed risk-off rule fired.",
    ),
    (
        "BOOTSTRAP_NO_PRIOR_STATE",
        "First evaluation — no prior policy tier exists.",
    ),
    (
        "BOOTSTRAP_OBSERVE_ONLY",
        "Bootstrap policy is observe-only: no forced selling from absent history.",
    ),
    (
        "BOOK_WEIGHTS_FULL_RISK_TARGET",
        "Book weights from the proposed full-risk target portfolio.",
    ),
    (
        "BOOK_WEIGHTS_LAST_R1_SNAPSHOT",
        "Book weights from the last committed R1 snapshot.",
    ),
    (
        "BOOK_WEIGHTS_CONFIG_DEFAULT",
        "Book weights fell back to configured defaults.",
    ),
    (
        "BOOK_CLASSIFICATION_COVERAGE_LOW",
        "Market-cap classification coverage below the configured minimum.",
    ),
    ("NEW_BUYS_FULL", "Full-sized new entries permitted."),
    ("NEW_BUYS_HALF", "New entries permitted at half size."),
    (
        "NEW_BUYS_BLOCKED_TIER",
        "New entries blocked by the policy tier.",
    ),
    (
        "NEW_BUYS_BLOCKED_SENTINEL",
        "New entries blocked by the momentum sentinel veto.",
    ),
    (
        "NEW_BUYS_BLOCKED_DATA",
        "New entries blocked by unusable or stale data.",
    ),
    (
        "EXPOSURE_WITHIN_CAP",
        "Actual equity exposure is within the policy cap.",
    ),
    (
        "EXPOSURE_GAP_PENDING",
        "Actual equity exposure exceeds the policy cap.",
    ),
    (
        "EXPOSURE_RECONCILE_RETRY",
        "Previous plan did not complete — reconciling without a new tier transition.",
    ),
    (
        "FORCED_SELL_SUPPRESSED_BOOTSTRAP",
        "Forced selling suppressed: no prior state to justify it.",
    ),
];

/// `DISPLAY_REASONS.get(code)`.
#[must_use]
pub fn display_reason(code: &str) -> Option<&'static str> {
    DISPLAY_REASONS
        .iter()
        .find(|(c, _)| *c == code)
        .map(|(_, t)| *t)
}

/// `describe(codes)`.
#[must_use]
pub fn describe(codes: &[String]) -> Vec<String> {
    codes
        .iter()
        .map(|c| display_reason(c).map_or_else(|| c.clone(), str::to_owned))
        .collect()
}

/// `RegimeConfig` as `config.regime_config()` builds it: the dataclass defaults, with the
/// environment's (or the settings page's) knobs.
#[derive(Debug, Clone, PartialEq)]
pub struct RegimeConfig {
    /// `structural_indices` (category → index tradingsymbol), in insertion order.
    pub structural_indices: Vec<(String, String)>,
    /// `index_aliases`, in insertion order.
    pub index_aliases: Vec<(String, Vec<String>)>,
    /// `momentum_sentinel`.
    pub momentum_sentinel: String,
    /// `exchange`.
    pub exchange: String,
    /// `segment`.
    pub segment: String,
    /// `ma_lengths`.
    pub ma_lengths: Vec<i64>,
    /// `buffer_bps`.
    pub buffer_bps: i64,
    /// `confirm_days`.
    pub confirm_days: i64,
    /// `default_book_weights`, in insertion order.
    pub default_book_weights: Vec<(String, f64)>,
    /// `min_classification_coverage_pct`.
    pub min_classification_coverage_pct: f64,
    /// `r4_bearish_stack_min`.
    pub r4_bearish_stack_min: f64,
    /// `r4_breadth_max`.
    pub r4_breadth_max: f64,
    /// `r4_h200_max`.
    pub r4_h200_max: f64,
    /// `r3_h200_max`.
    pub r3_h200_max: f64,
    /// `r3_breadth_max`.
    pub r3_breadth_max: f64,
    /// `r1_h50_min`.
    pub r1_h50_min: f64,
    /// `r1_h200_min`.
    pub r1_h200_min: f64,
    /// `r1_breadth_min`.
    pub r1_breadth_min: f64,
    /// `recovery_breadth_min`.
    pub recovery_breadth_min: f64,
    /// `recovery_h20_min`.
    pub recovery_h20_min: f64,
    /// `recovery_confirm_evaluations`.
    pub recovery_confirm_evaluations: i64,
    /// `min_breadth_coverage_pct`.
    pub min_breadth_coverage_pct: f64,
    /// `breadth_required`.
    pub breadth_required: bool,
    /// `breadth_stale_days`.
    pub breadth_stale_days: i64,
    /// `index_stale_days`.
    pub index_stale_days: i64,
    /// `tier_exposure_pct`, R1..R4.
    pub tier_exposure_pct: [f64; 4],
    /// `exposure_tolerance_pct`.
    pub exposure_tolerance_pct: f64,
    /// `weekly_evaluation_weekday`.
    pub weekly_evaluation_weekday: i64,
    /// `bootstrap_policy.value`.
    pub bootstrap_policy: String,
    /// `mode.value`.
    pub mode: String,
    /// `algorithm_version`.
    pub algorithm_version: String,
}

impl Default for RegimeConfig {
    fn default() -> Self {
        Self {
            structural_indices: vec![
                ("largecap".to_owned(), "NIFTY 50".to_owned()),
                ("midcap".to_owned(), "NIFTY MIDCAP 150".to_owned()),
                ("smallcap".to_owned(), "NIFTY SMLCAP 250".to_owned()),
            ],
            index_aliases: vec![
                (
                    "NIFTY SMLCAP 250".to_owned(),
                    vec!["NIFTY SMALLCAP 250".to_owned()],
                ),
                (
                    "NIFTY 500 MOMENTUM 50".to_owned(),
                    vec!["NIFTY500MOMENTM50".to_owned()],
                ),
            ],
            momentum_sentinel: "NIFTY 500 MOMENTUM 50".to_owned(),
            exchange: "NSE".to_owned(),
            segment: "INDICES".to_owned(),
            ma_lengths: vec![20, 50, 200],
            buffer_bps: 150,
            confirm_days: 3,
            default_book_weights: vec![
                ("smallcap".to_owned(), 0.50),
                ("midcap".to_owned(), 0.30),
                ("largecap".to_owned(), 0.20),
            ],
            min_classification_coverage_pct: 80.0,
            r4_bearish_stack_min: 0.60,
            r4_breadth_max: 30.0,
            r4_h200_max: 0.30,
            r3_h200_max: 0.50,
            r3_breadth_max: 40.0,
            r1_h50_min: 0.80,
            r1_h200_min: 0.80,
            r1_breadth_min: 55.0,
            recovery_breadth_min: 55.0,
            recovery_h20_min: 0.50,
            recovery_confirm_evaluations: 2,
            min_breadth_coverage_pct: 90.0,
            breadth_required: true,
            breadth_stale_days: 10,
            index_stale_days: 5,
            tier_exposure_pct: [100.0, 70.0, 40.0, 10.0],
            exposure_tolerance_pct: 1.0,
            weekly_evaluation_weekday: 4,
            bootstrap_policy: "observe_only".to_owned(),
            mode: "observe".to_owned(),
            algorithm_version: ALGORITHM_VERSION.to_owned(),
        }
    }
}

/// `ConfigError`: every problem `validate()` found.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("invalid regime configuration:\n  - {}", .0.join("\n  - "))]
pub struct ConfigError(pub Vec<String>);

fn py_tuple_f(v: &[f64]) -> String {
    let parts: Vec<String> = v.iter().map(|x| baskfy_core::py::float_repr(*x)).collect();
    format!("[{}]", parts.join(", "))
}

impl RegimeConfig {
    /// `config.regime_config()` (the validation included).
    ///
    /// # Errors
    /// [`ConfigError`] when `validate()` would raise, or an enum value Python would refuse.
    pub fn from_settings(s: &Settings) -> Result<Self, ConfigError> {
        if !["observe_only", "adopt_candidate"].contains(&s.regime_bootstrap_policy.as_str()) {
            return Err(ConfigError(vec![format!(
                "'{}' is not a valid BootstrapPolicy",
                s.regime_bootstrap_policy
            )]));
        }
        if !["observe", "propose", "enforce"].contains(&s.regime_mode.as_str()) {
            return Err(ConfigError(vec![format!(
                "'{}' is not a valid RegimeMode",
                s.regime_mode
            )]));
        }
        let cfg = Self {
            buffer_bps: s.regime_buffer_bps,
            confirm_days: s.regime_confirm_days,
            weekly_evaluation_weekday: s.regime_weekly_eval_weekday,
            index_stale_days: s.regime_index_stale_days,
            breadth_stale_days: s.regime_breadth_stale_days,
            min_breadth_coverage_pct: s.regime_min_breadth_coverage_pct,
            tier_exposure_pct: [100.0, 70.0, 40.0, s.regime_r4_equity_pct],
            bootstrap_policy: s.regime_bootstrap_policy.clone(),
            mode: s.regime_mode.clone(),
            ..Self::default()
        };
        cfg.validate()?;
        Ok(cfg)
    }

    /// `cap_for(RegimeTier(tier))`; `None` for a value that is not a tier (Python raises).
    #[must_use]
    pub fn cap_for(&self, tier: &str) -> Option<f64> {
        TIERS
            .iter()
            .position(|t| *t == tier)
            .map(|i| self.tier_exposure_pct[i])
    }

    /// `validate()`.
    ///
    /// # Errors
    /// [`ConfigError`] listing every problem.
    pub fn validate(&self) -> Result<(), ConfigError> {
        let mut errs = Vec::new();
        let mut sorted = self.ma_lengths.clone();
        sorted.sort_unstable();
        sorted.dedup();
        if self.ma_lengths != sorted {
            errs.push(format!(
                "ma_lengths must be strictly increasing and unique: ({})",
                self.ma_lengths
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
        if self.ma_lengths.is_empty() {
            errs.push("ma_lengths must not be empty".to_owned());
        }
        let caps = self.tier_exposure_pct;
        if (0..3).any(|i| caps[i] < caps[i + 1]) {
            errs.push(format!(
                "tier exposure caps must be non-increasing R1->R4: {}",
                py_tuple_f(&caps)
            ));
        }
        if caps.iter().any(|c| !(0.0..=100.0).contains(c)) {
            errs.push(format!(
                "tier exposure caps must be within 0-100: {}",
                py_tuple_f(&caps)
            ));
        }
        for (name, val) in [
            ("r4_breadth_max", self.r4_breadth_max),
            ("r3_breadth_max", self.r3_breadth_max),
            ("r1_breadth_min", self.r1_breadth_min),
            ("recovery_breadth_min", self.recovery_breadth_min),
            ("min_breadth_coverage_pct", self.min_breadth_coverage_pct),
            (
                "min_classification_coverage_pct",
                self.min_classification_coverage_pct,
            ),
        ] {
            if !(0.0..=100.0).contains(&val) {
                errs.push(format!(
                    "{name} must be a percentage within 0-100, got {}",
                    baskfy_core::py::float_repr(val)
                ));
            }
        }
        for (name, val) in [
            ("r4_bearish_stack_min", self.r4_bearish_stack_min),
            ("r4_h200_max", self.r4_h200_max),
            ("r3_h200_max", self.r3_h200_max),
            ("r1_h50_min", self.r1_h50_min),
            ("r1_h200_min", self.r1_h200_min),
            ("recovery_h20_min", self.recovery_h20_min),
        ] {
            if !(0.0..=1.0).contains(&val) {
                errs.push(format!(
                    "{name} is a weighted-health fraction within 0-1, got {}",
                    baskfy_core::py::float_repr(val)
                ));
            }
        }
        if self.confirm_days < 1 {
            errs.push(format!(
                "confirm_days must be at least 1, got {}",
                self.confirm_days
            ));
        }
        if !(0..=1000).contains(&self.buffer_bps) {
            errs.push(format!(
                "buffer_bps must be within 0-1000 (0-10%), got {}",
                self.buffer_bps
            ));
        }
        if self.recovery_confirm_evaluations < 1 {
            errs.push("recovery_confirm_evaluations must be at least 1".to_owned());
        }
        if !(0..=6).contains(&self.weekly_evaluation_weekday) {
            errs.push("weekly_evaluation_weekday must be 0-6 (Mon-Sun)".to_owned());
        }
        if errs.is_empty() {
            Ok(())
        } else {
            Err(ConfigError(errs))
        }
    }

    /// `json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))`.
    #[must_use]
    pub fn canonical_json(&self) -> String {
        fn s(x: &str) -> String {
            serde_json::to_string(x).unwrap_or_default()
        }
        fn f(x: f64) -> String {
            baskfy_core::py::float_repr(x)
        }
        fn obj(mut pairs: Vec<(String, String)>) -> String {
            pairs.sort_by(|a, b| a.0.cmp(&b.0));
            let body: Vec<String> = pairs.iter().map(|(k, v)| format!("{}:{v}", s(k))).collect();
            format!("{{{}}}", body.join(","))
        }
        let structural = obj(self
            .structural_indices
            .iter()
            .map(|(k, v)| (k.clone(), s(v)))
            .collect());
        let aliases = obj(self
            .index_aliases
            .iter()
            .map(|(k, v)| {
                let items: Vec<String> = v.iter().map(|a| s(a)).collect();
                (k.clone(), format!("[{}]", items.join(",")))
            })
            .collect());
        let weights = obj(self
            .default_book_weights
            .iter()
            .map(|(k, v)| (k.clone(), f(*v)))
            .collect());
        let tiers = obj(TIERS
            .iter()
            .zip(self.tier_exposure_pct)
            .map(|(k, v)| ((*k).to_owned(), f(v)))
            .collect());
        let mas: Vec<String> = self.ma_lengths.iter().map(ToString::to_string).collect();
        let pairs: Vec<(&str, String)> = vec![
            ("structural_indices", structural),
            ("index_aliases", aliases),
            ("momentum_sentinel", s(&self.momentum_sentinel)),
            ("exchange", s(&self.exchange)),
            ("segment", s(&self.segment)),
            ("ma_lengths", format!("[{}]", mas.join(","))),
            ("buffer_bps", self.buffer_bps.to_string()),
            ("confirm_days", self.confirm_days.to_string()),
            ("default_book_weights", weights),
            (
                "min_classification_coverage_pct",
                f(self.min_classification_coverage_pct),
            ),
            ("r4_bearish_stack_min", f(self.r4_bearish_stack_min)),
            ("r4_breadth_max", f(self.r4_breadth_max)),
            ("r4_h200_max", f(self.r4_h200_max)),
            ("r3_h200_max", f(self.r3_h200_max)),
            ("r3_breadth_max", f(self.r3_breadth_max)),
            ("r1_h50_min", f(self.r1_h50_min)),
            ("r1_h200_min", f(self.r1_h200_min)),
            ("r1_breadth_min", f(self.r1_breadth_min)),
            ("recovery_breadth_min", f(self.recovery_breadth_min)),
            ("recovery_h20_min", f(self.recovery_h20_min)),
            (
                "recovery_confirm_evaluations",
                self.recovery_confirm_evaluations.to_string(),
            ),
            ("min_breadth_coverage_pct", f(self.min_breadth_coverage_pct)),
            (
                "breadth_required",
                if self.breadth_required {
                    "true"
                } else {
                    "false"
                }
                .to_owned(),
            ),
            ("breadth_stale_days", self.breadth_stale_days.to_string()),
            ("index_stale_days", self.index_stale_days.to_string()),
            ("tier_exposure_pct", tiers),
            ("exposure_tolerance_pct", f(self.exposure_tolerance_pct)),
            (
                "weekly_evaluation_weekday",
                self.weekly_evaluation_weekday.to_string(),
            ),
            ("bootstrap_policy", s(&self.bootstrap_policy)),
            ("algorithm_version", s(&self.algorithm_version)),
        ];
        obj(pairs.into_iter().map(|(k, v)| (k.to_owned(), v)).collect())
    }

    /// `config_hash()`: the first 16 hex digits of the canonical JSON's SHA-256.
    #[must_use]
    pub fn config_hash(&self) -> String {
        use sha2::Digest as _;
        let digest = sha2::Sha256::digest(self.canonical_json().as_bytes());
        let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
        hex[..16].to_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_json_is_pythons() {
        // json.dumps(regime_config().canonical(), sort_keys=True, separators=(",", ":")) with
        // every knob at its default, printed by the Python desk.
        let cfg = RegimeConfig::default();
        assert_eq!(cfg.config_hash(), PY_DEFAULT_HASH);
    }

    #[test]
    fn mode_is_not_part_of_the_hash() {
        let a = RegimeConfig::default();
        let b = RegimeConfig {
            mode: "enforce".to_owned(),
            ..RegimeConfig::default()
        };
        assert_eq!(a.config_hash(), b.config_hash());
        let c = RegimeConfig {
            buffer_bps: 151,
            ..RegimeConfig::default()
        };
        assert_ne!(a.config_hash(), c.config_hash());
    }

    #[test]
    fn validate_lists_every_problem() {
        let bad = RegimeConfig {
            tier_exposure_pct: [100.0, 70.0, 40.0, 45.0],
            confirm_days: 0,
            buffer_bps: 2000,
            ..RegimeConfig::default()
        };
        let err = bad.validate().err().map(|e| e.0).unwrap_or_default();
        assert_eq!(
            err,
            vec![
                "tier exposure caps must be non-increasing R1->R4: [100.0, 70.0, 40.0, 45.0]",
                "confirm_days must be at least 1, got 0",
                "buffer_bps must be within 0-1000 (0-10%), got 2000",
            ]
        );
        assert!(RegimeConfig::default().validate().is_ok());
    }

    #[test]
    fn caps_belong_to_the_tier() {
        let cfg = RegimeConfig::default();
        assert_eq!(cfg.cap_for("R1"), Some(100.0));
        assert_eq!(cfg.cap_for("R3"), Some(40.0));
        assert_eq!(cfg.cap_for("R9"), None);
    }

    #[test]
    fn r2_default_is_not_affirmative_risk_off() {
        assert!(!is_affirmative_risk_off("R2_DEFAULT"));
        assert!(is_affirmative_risk_off("R3_H200_WEAK"));
        assert_eq!(
            describe(&["R2_DEFAULT".to_owned(), "UNKNOWN_X".to_owned()]),
            vec![
                "No risk-off rule fired and R1 conditions were not all met.".to_owned(),
                "UNKNOWN_X".to_owned()
            ]
        );
    }

    pub(super) const PY_DEFAULT_HASH: &str = "571bfb93ce64019f";
}
