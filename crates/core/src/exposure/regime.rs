//! Port of `baskfy_core.exposure.regime`: the pure R1-R4 regime engine.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;

use chrono::{Datelike, Duration, NaiveDate};
use serde::ser::SerializeMap;
use serde::{Deserialize, Serialize, Serializer};

use super::{
    ExposureError, ExposureResult, StrMap, fr, json_float, json_str, py_max, py_round, py_str_repr,
    py_sum, sha256_hex,
};

/// `ALGORITHM_VERSION`.
pub const ALGORITHM_VERSION: &str = "regime/1.0.0";

str_enum!(
    /// `RegimeTier`: R1 is most risk-on, R4 most risk-off.
    RegimeTier { R1 => "R1", R2 => "R2", R3 => "R3", R4 => "R4" }
);

impl RegimeTier {
    /// 1 = most risk-on, 4 = most risk-off.
    #[must_use]
    pub fn ordinal(self) -> i64 {
        match self {
            Self::R1 => 1,
            Self::R2 => 2,
            Self::R3 => 3,
            Self::R4 => 4,
        }
    }

    /// `RegimeTier.from_ordinal`, clamped to 1..=4.
    #[must_use]
    pub fn from_ordinal(n: i64) -> Self {
        match n.clamp(1, 4) {
            1 => Self::R1,
            2 => Self::R2,
            3 => Self::R3,
            _ => Self::R4,
        }
    }
}

str_enum!(
    /// `SignalState`.
    SignalState { Above => "above", Below => "below", Unknown => "unknown" }
);

str_enum!(
    /// `NewBuyMode`.
    NewBuyMode { Full => "full", Half => "half", Blocked => "blocked" }
);

str_enum!(
    /// `ForcedAction`.
    ForcedAction {
        None => "none",
        TrimToCap => "trim_to_cap",
        ReduceToCap => "reduce_to_cap",
        CrashReduce => "crash_reduce",
        ManualReview => "manual_review",
    }
);

str_enum!(
    /// `ExecutionStatus`.
    ExecutionStatus {
        NotPlanned => "not_planned",
        Planned => "planned",
        Partial => "partial",
        Completed => "completed",
        Failed => "failed",
    }
);

str_enum!(
    /// `WeightSource`.
    WeightSource {
        FullRiskTarget => "full_risk_target",
        LastR1Snapshot => "last_r1_snapshot",
        ConfigDefault => "config_default",
    }
);

str_enum!(
    /// `BootstrapPolicy`: what the first evaluation does with no prior tier.
    BootstrapPolicy { ObserveOnly => "observe_only", AdoptCandidate => "adopt_candidate" }
);

str_enum!(
    /// `RegimeMode`.
    RegimeMode { Observe => "observe", Propose => "propose", Enforce => "enforce" }
);

/// `Reason`: the stable reason codes.
pub mod reason {
    /// `DATA_STALE`.
    pub const DATA_STALE: &str = "DATA_STALE";
    /// `DATA_INSUFFICIENT_WARMUP`.
    pub const DATA_INSUFFICIENT_WARMUP: &str = "DATA_INSUFFICIENT_WARMUP";
    /// `DATA_INDEX_MISSING`.
    pub const DATA_INDEX_MISSING: &str = "DATA_INDEX_MISSING";
    /// `SIGNAL_UNKNOWN`.
    pub const SIGNAL_UNKNOWN: &str = "SIGNAL_UNKNOWN";
    /// `BREADTH_MISSING`.
    pub const BREADTH_MISSING: &str = "BREADTH_MISSING";
    /// `BREADTH_STALE`.
    pub const BREADTH_STALE: &str = "BREADTH_STALE";
    /// `BREADTH_LOW_COVERAGE`.
    pub const BREADTH_LOW_COVERAGE: &str = "BREADTH_LOW_COVERAGE";
    /// `BREADTH_NOT_USED`.
    pub const BREADTH_NOT_USED: &str = "BREADTH_NOT_USED";
    /// `R4_BEARISH_STACK_AND_WEAK_BREADTH`.
    pub const R4_BEARISH_STACK_AND_WEAK_BREADTH: &str = "R4_BEARISH_STACK_AND_WEAK_BREADTH";
    /// `R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH`.
    pub const R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH: &str = "R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH";
    /// `R3_H200_WEAK`.
    pub const R3_H200_WEAK: &str = "R3_H200_WEAK";
    /// `R3_BREADTH_WEAK`.
    pub const R3_BREADTH_WEAK: &str = "R3_BREADTH_WEAK";
    /// `R3_SENTINEL_BELOW_200DMA`.
    pub const R3_SENTINEL_BELOW_200DMA: &str = "R3_SENTINEL_BELOW_200DMA";
    /// `R1_ALL_CONDITIONS_MET`.
    pub const R1_ALL_CONDITIONS_MET: &str = "R1_ALL_CONDITIONS_MET";
    /// `R2_DEFAULT`.
    pub const R2_DEFAULT: &str = "R2_DEFAULT";
    /// `SENTINEL_BELOW_50DMA_VETO`.
    pub const SENTINEL_BELOW_50DMA_VETO: &str = "SENTINEL_BELOW_50DMA_VETO";
    /// `SENTINEL_FLOOR_R2`.
    pub const SENTINEL_FLOOR_R2: &str = "SENTINEL_FLOOR_R2";
    /// `SENTINEL_FLOOR_R3`.
    pub const SENTINEL_FLOOR_R3: &str = "SENTINEL_FLOOR_R3";
    /// `SENTINEL_UNKNOWN`.
    pub const SENTINEL_UNKNOWN: &str = "SENTINEL_UNKNOWN";
    /// `RECOVERY_OVERRIDE_APPLIED`.
    pub const RECOVERY_OVERRIDE_APPLIED: &str = "RECOVERY_OVERRIDE_APPLIED";
    /// `RECOVERY_OVERRIDE_UNCONFIRMED`.
    pub const RECOVERY_OVERRIDE_UNCONFIRMED: &str = "RECOVERY_OVERRIDE_UNCONFIRMED";
    /// `RECOVERY_OVERRIDE_H20_TOO_WEAK`.
    pub const RECOVERY_OVERRIDE_H20_TOO_WEAK: &str = "RECOVERY_OVERRIDE_H20_TOO_WEAK";
    /// `RECOVERY_OVERRIDE_BREADTH_UNUSABLE`.
    pub const RECOVERY_OVERRIDE_BREADTH_UNUSABLE: &str = "RECOVERY_OVERRIDE_BREADTH_UNUSABLE";
    /// `TRANSITION_RISK_OFF_DIRECT`.
    pub const TRANSITION_RISK_OFF_DIRECT: &str = "TRANSITION_RISK_OFF_DIRECT";
    /// `TRANSITION_RERISK_CAPPED`.
    pub const TRANSITION_RERISK_CAPPED: &str = "TRANSITION_RERISK_CAPPED";
    /// `TRANSITION_NONE`.
    pub const TRANSITION_NONE: &str = "TRANSITION_NONE";
    /// `TRANSITION_HELD_STALE_DATA`.
    pub const TRANSITION_HELD_STALE_DATA: &str = "TRANSITION_HELD_STALE_DATA";
    /// `BOOTSTRAP_NO_PRIOR_STATE`.
    pub const BOOTSTRAP_NO_PRIOR_STATE: &str = "BOOTSTRAP_NO_PRIOR_STATE";
    /// `BOOTSTRAP_OBSERVE_ONLY`.
    pub const BOOTSTRAP_OBSERVE_ONLY: &str = "BOOTSTRAP_OBSERVE_ONLY";
    /// `BOOK_WEIGHTS_FULL_RISK_TARGET`.
    pub const BOOK_WEIGHTS_FULL_RISK_TARGET: &str = "BOOK_WEIGHTS_FULL_RISK_TARGET";
    /// `BOOK_WEIGHTS_LAST_R1_SNAPSHOT`.
    pub const BOOK_WEIGHTS_LAST_R1_SNAPSHOT: &str = "BOOK_WEIGHTS_LAST_R1_SNAPSHOT";
    /// `BOOK_WEIGHTS_CONFIG_DEFAULT`.
    pub const BOOK_WEIGHTS_CONFIG_DEFAULT: &str = "BOOK_WEIGHTS_CONFIG_DEFAULT";
    /// `BOOK_CLASSIFICATION_COVERAGE_LOW`.
    pub const BOOK_CLASSIFICATION_COVERAGE_LOW: &str = "BOOK_CLASSIFICATION_COVERAGE_LOW";
    /// `NEW_BUYS_FULL`.
    pub const NEW_BUYS_FULL: &str = "NEW_BUYS_FULL";
    /// `NEW_BUYS_HALF`.
    pub const NEW_BUYS_HALF: &str = "NEW_BUYS_HALF";
    /// `NEW_BUYS_BLOCKED_TIER`.
    pub const NEW_BUYS_BLOCKED_TIER: &str = "NEW_BUYS_BLOCKED_TIER";
    /// `NEW_BUYS_BLOCKED_SENTINEL`.
    pub const NEW_BUYS_BLOCKED_SENTINEL: &str = "NEW_BUYS_BLOCKED_SENTINEL";
    /// `NEW_BUYS_BLOCKED_DATA`.
    pub const NEW_BUYS_BLOCKED_DATA: &str = "NEW_BUYS_BLOCKED_DATA";
    /// `EXPOSURE_WITHIN_CAP`.
    pub const EXPOSURE_WITHIN_CAP: &str = "EXPOSURE_WITHIN_CAP";
    /// `EXPOSURE_GAP_PENDING`.
    pub const EXPOSURE_GAP_PENDING: &str = "EXPOSURE_GAP_PENDING";
    /// `EXPOSURE_RECONCILE_RETRY`.
    pub const EXPOSURE_RECONCILE_RETRY: &str = "EXPOSURE_RECONCILE_RETRY";
    /// `FORCED_SELL_SUPPRESSED_BOOTSTRAP`.
    pub const FORCED_SELL_SUPPRESSED_BOOTSTRAP: &str = "FORCED_SELL_SUPPRESSED_BOOTSTRAP";
}

/// `AFFIRMATIVE_RISK_OFF`: rules that are an affirmative risk-off signal.
pub const AFFIRMATIVE_RISK_OFF: &[&str] = &[
    reason::R4_BEARISH_STACK_AND_WEAK_BREADTH,
    reason::R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH,
    reason::R3_H200_WEAK,
    reason::R3_BREADTH_WEAK,
    reason::R3_SENTINEL_BELOW_200DMA,
    reason::SENTINEL_FLOOR_R2,
    reason::SENTINEL_FLOOR_R3,
];

/// `DISPLAY_REASONS`, in the Python dict's order; also the list of every reason code.
pub const DISPLAY_REASONS: &[(&str, &str)] = &[
    (
        reason::DATA_STALE,
        "Index data is stale — holding the previous tier and blocking new buys.",
    ),
    (
        reason::DATA_INSUFFICIENT_WARMUP,
        "Not enough history to establish every moving average.",
    ),
    (
        reason::DATA_INDEX_MISSING,
        "A required structural index has no usable data.",
    ),
    (
        reason::SIGNAL_UNKNOWN,
        "One or more index/MA pairs have not yet confirmed a side.",
    ),
    (reason::BREADTH_MISSING, "No breadth reading supplied."),
    (
        reason::BREADTH_STALE,
        "Breadth reading is older than the staleness allowance.",
    ),
    (
        reason::BREADTH_LOW_COVERAGE,
        "Breadth coverage is below the configured minimum.",
    ),
    (
        reason::BREADTH_NOT_USED,
        "This configuration does not use breadth; breadth rules are inactive.",
    ),
    (
        reason::R4_BEARISH_STACK_AND_WEAK_BREADTH,
        "Bearish MA stack across most of the book with very weak breadth.",
    ),
    (
        reason::R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH,
        "Momentum sentinel below its 200-DMA with weak long-term health and breadth.",
    ),
    (
        reason::R3_H200_WEAK,
        "Long-term health (H200) at or below the R3 threshold.",
    ),
    (reason::R3_BREADTH_WEAK, "Breadth below the R3 threshold."),
    (
        reason::R3_SENTINEL_BELOW_200DMA,
        "Momentum sentinel confirmed below its 200-DMA.",
    ),
    (
        reason::R1_ALL_CONDITIONS_MET,
        "Health, breadth and the momentum sentinel all risk-on.",
    ),
    (
        reason::R2_DEFAULT,
        "No risk-off rule fired and R1 conditions were not all met.",
    ),
    (
        reason::SENTINEL_BELOW_50DMA_VETO,
        "Momentum sentinel below its 50-DMA — new entries vetoed.",
    ),
    (
        reason::SENTINEL_FLOOR_R2,
        "Sentinel 50-DMA veto floors the candidate tier at R2.",
    ),
    (
        reason::SENTINEL_FLOOR_R3,
        "Sentinel 200-DMA break floors the candidate tier at R3.",
    ),
    (
        reason::SENTINEL_UNKNOWN,
        "Momentum sentinel state is not yet confirmed.",
    ),
    (
        reason::RECOVERY_OVERRIDE_APPLIED,
        "Breadth recovery override lifted the tier to R2.",
    ),
    (
        reason::RECOVERY_OVERRIDE_UNCONFIRMED,
        "Recovery conditions met but not yet confirmed for long enough.",
    ),
    (
        reason::RECOVERY_OVERRIDE_H20_TOO_WEAK,
        "Short-term health below the recovery threshold.",
    ),
    (
        reason::RECOVERY_OVERRIDE_BREADTH_UNUSABLE,
        "Recovery override needs current, well-covered breadth.",
    ),
    (
        reason::TRANSITION_RISK_OFF_DIRECT,
        "Risk reduction applied directly to the candidate tier.",
    ),
    (
        reason::TRANSITION_RERISK_CAPPED,
        "Re-risking limited to one tier per weekly evaluation.",
    ),
    (reason::TRANSITION_NONE, "No tier change this evaluation."),
    (
        reason::TRANSITION_HELD_STALE_DATA,
        "Previous tier retained: inputs are degraded and no confirmed risk-off rule fired.",
    ),
    (
        reason::BOOTSTRAP_NO_PRIOR_STATE,
        "First evaluation — no prior policy tier exists.",
    ),
    (
        reason::BOOTSTRAP_OBSERVE_ONLY,
        "Bootstrap policy is observe-only: no forced selling from absent history.",
    ),
    (
        reason::BOOK_WEIGHTS_FULL_RISK_TARGET,
        "Book weights from the proposed full-risk target portfolio.",
    ),
    (
        reason::BOOK_WEIGHTS_LAST_R1_SNAPSHOT,
        "Book weights from the last committed R1 snapshot.",
    ),
    (
        reason::BOOK_WEIGHTS_CONFIG_DEFAULT,
        "Book weights fell back to configured defaults.",
    ),
    (
        reason::BOOK_CLASSIFICATION_COVERAGE_LOW,
        "Market-cap classification coverage below the configured minimum.",
    ),
    (reason::NEW_BUYS_FULL, "Full-sized new entries permitted."),
    (reason::NEW_BUYS_HALF, "New entries permitted at half size."),
    (
        reason::NEW_BUYS_BLOCKED_TIER,
        "New entries blocked by the policy tier.",
    ),
    (
        reason::NEW_BUYS_BLOCKED_SENTINEL,
        "New entries blocked by the momentum sentinel veto.",
    ),
    (
        reason::NEW_BUYS_BLOCKED_DATA,
        "New entries blocked by unusable or stale data.",
    ),
    (
        reason::EXPOSURE_WITHIN_CAP,
        "Actual equity exposure is within the policy cap.",
    ),
    (
        reason::EXPOSURE_GAP_PENDING,
        "Actual equity exposure exceeds the policy cap.",
    ),
    (
        reason::EXPOSURE_RECONCILE_RETRY,
        "Previous plan did not complete — reconciling without a new tier transition.",
    ),
    (
        reason::FORCED_SELL_SUPPRESSED_BOOTSTRAP,
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

/// `describe`: codes to display text, unknown codes passed through.
#[must_use]
pub fn describe<S: AsRef<str>>(codes: &[S]) -> Vec<String> {
    codes
        .iter()
        .map(|c| display_reason(c.as_ref()).unwrap_or(c.as_ref()).to_owned())
        .collect()
}

/// `RegimeConfig`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct RegimeConfig {
    /// Category to index tradingsymbol.
    pub structural_indices: StrMap<String>,
    /// Index name to alternative tradingsymbols.
    pub index_aliases: StrMap<Vec<String>>,
    /// The momentum sentinel index.
    pub momentum_sentinel: String,
    /// Exchange.
    pub exchange: String,
    /// Segment.
    pub segment: String,
    /// Moving-average lengths.
    pub ma_lengths: Vec<usize>,
    /// Hysteresis buffer, basis points.
    pub buffer_bps: i64,
    /// Confirming closes required.
    pub confirm_days: i64,
    /// Fallback book weights.
    pub default_book_weights: StrMap<f64>,
    /// Minimum classification coverage, percent.
    pub min_classification_coverage_pct: f64,
    /// R4 bearish-stack weight minimum (>=).
    pub r4_bearish_stack_min: f64,
    /// R4 breadth maximum (<).
    pub r4_breadth_max: f64,
    /// R4 H200 maximum (<).
    pub r4_h200_max: f64,
    /// R3 H200 maximum (<=).
    pub r3_h200_max: f64,
    /// R3 breadth maximum (<).
    pub r3_breadth_max: f64,
    /// R1 H50 minimum (>=).
    pub r1_h50_min: f64,
    /// R1 H200 minimum (>=).
    pub r1_h200_min: f64,
    /// R1 breadth minimum (>=).
    pub r1_breadth_min: f64,
    /// Recovery breadth minimum (>=).
    pub recovery_breadth_min: f64,
    /// Recovery H20 minimum (>=).
    pub recovery_h20_min: f64,
    /// Recovery confirmations required.
    pub recovery_confirm_evaluations: i64,
    /// Minimum breadth coverage, percent.
    pub min_breadth_coverage_pct: f64,
    /// Whether breadth is part of the model.
    pub breadth_required: bool,
    /// Breadth staleness allowance, days.
    pub breadth_stale_days: i64,
    /// Index staleness allowance, days.
    pub index_stale_days: i64,
    /// Tier to equity cap, percent.
    pub tier_exposure_pct: StrMap<f64>,
    /// Exposure tolerance, percent.
    pub exposure_tolerance_pct: f64,
    /// Weekly evaluation weekday, Monday=0.
    pub weekly_evaluation_weekday: i64,
    /// Bootstrap policy.
    pub bootstrap_policy: BootstrapPolicy,
    /// Rollout mode (not hashed).
    pub mode: RegimeMode,
    /// Algorithm version.
    pub algorithm_version: String,
}

fn smap<V, const N: usize>(pairs: [(&str, V); N]) -> StrMap<V> {
    pairs.into_iter().collect()
}

impl Default for RegimeConfig {
    fn default() -> Self {
        Self {
            structural_indices: smap([
                ("largecap", "NIFTY 50".to_owned()),
                ("midcap", "NIFTY MIDCAP 150".to_owned()),
                ("smallcap", "NIFTY SMLCAP 250".to_owned()),
            ]),
            index_aliases: smap([
                ("NIFTY SMLCAP 250", vec!["NIFTY SMALLCAP 250".to_owned()]),
                (
                    "NIFTY 500 MOMENTUM 50",
                    vec!["NIFTY500MOMENTM50".to_owned()],
                ),
            ]),
            momentum_sentinel: "NIFTY 500 MOMENTUM 50".to_owned(),
            exchange: "NSE".to_owned(),
            segment: "INDICES".to_owned(),
            ma_lengths: vec![20, 50, 200],
            buffer_bps: 150,
            confirm_days: 3,
            default_book_weights: smap([("smallcap", 0.50), ("midcap", 0.30), ("largecap", 0.20)]),
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
            tier_exposure_pct: smap([("R1", 100.0), ("R2", 70.0), ("R3", 40.0), ("R4", 10.0)]),
            exposure_tolerance_pct: 1.0,
            weekly_evaluation_weekday: 4,
            bootstrap_policy: BootstrapPolicy::ObserveOnly,
            mode: RegimeMode::Observe,
            algorithm_version: ALGORITHM_VERSION.to_owned(),
        }
    }
}

fn i64_f64(n: i64) -> f64 {
    n as f64
}

fn usize_f64(n: usize) -> f64 {
    n as f64
}

fn json_map(entries: &mut [(String, String)]) -> String {
    entries.sort_by(|a, b| a.0.cmp(&b.0));
    let mut out = String::from("{");
    for (i, (k, v)) in entries.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        json_str(k, &mut out);
        out.push(':');
        out.push_str(v);
    }
    out.push('}');
    out
}

fn jstr(s: &str) -> String {
    let mut out = String::new();
    json_str(s, &mut out);
    out
}

fn py_tuple_repr(v: &[usize]) -> String {
    let items: Vec<String> = v.iter().map(ToString::to_string).collect();
    if items.len() == 1 {
        format!("({},)", items[0])
    } else {
        format!("({})", items.join(", "))
    }
}

fn py_dict_repr(m: &StrMap<f64>) -> String {
    let items: Vec<String> = m
        .iter()
        .map(|(k, v)| format!("{}: {}", py_str_repr(k), fr(*v)))
        .collect();
    format!("{{{}}}", items.join(", "))
}

impl RegimeConfig {
    /// Buffer as a fraction (150 bps is 0.015).
    #[must_use]
    pub fn buffer(&self) -> f64 {
        i64_f64(self.buffer_bps) / 10_000.0
    }

    /// `cap_for(tier)`; a missing tier is Python's `KeyError`.
    pub fn cap_for(&self, tier: RegimeTier) -> ExposureResult<f64> {
        self.tier_exposure_pct
            .get(tier.as_str())
            .copied()
            .ok_or_else(|| ExposureError::Key(py_str_repr(tier.as_str())))
    }

    /// `json.dumps(canonical(), sort_keys=True, separators=(",", ":"))`.
    #[must_use]
    pub fn canonical_json(&self) -> String {
        let fmap = |m: &StrMap<f64>| {
            let mut e: Vec<(String, String)> = m
                .iter()
                .map(|(k, v)| (k.to_owned(), json_float(*v)))
                .collect();
            json_map(&mut e)
        };
        let mut structural: Vec<(String, String)> = self
            .structural_indices
            .iter()
            .map(|(k, v)| (k.to_owned(), jstr(v)))
            .collect();
        let mut aliases: Vec<(String, String)> = self
            .index_aliases
            .iter()
            .map(|(k, v)| {
                let items: Vec<String> = v.iter().map(|s| jstr(s)).collect();
                (k.to_owned(), format!("[{}]", items.join(",")))
            })
            .collect();
        let ma: Vec<String> = self.ma_lengths.iter().map(ToString::to_string).collect();
        let mut top: Vec<(String, String)> = vec![
            ("structural_indices".into(), json_map(&mut structural)),
            ("index_aliases".into(), json_map(&mut aliases)),
            ("momentum_sentinel".into(), jstr(&self.momentum_sentinel)),
            ("exchange".into(), jstr(&self.exchange)),
            ("segment".into(), jstr(&self.segment)),
            ("ma_lengths".into(), format!("[{}]", ma.join(","))),
            ("buffer_bps".into(), self.buffer_bps.to_string()),
            ("confirm_days".into(), self.confirm_days.to_string()),
            (
                "default_book_weights".into(),
                fmap(&self.default_book_weights),
            ),
            (
                "min_classification_coverage_pct".into(),
                json_float(self.min_classification_coverage_pct),
            ),
            (
                "r4_bearish_stack_min".into(),
                json_float(self.r4_bearish_stack_min),
            ),
            ("r4_breadth_max".into(), json_float(self.r4_breadth_max)),
            ("r4_h200_max".into(), json_float(self.r4_h200_max)),
            ("r3_h200_max".into(), json_float(self.r3_h200_max)),
            ("r3_breadth_max".into(), json_float(self.r3_breadth_max)),
            ("r1_h50_min".into(), json_float(self.r1_h50_min)),
            ("r1_h200_min".into(), json_float(self.r1_h200_min)),
            ("r1_breadth_min".into(), json_float(self.r1_breadth_min)),
            (
                "recovery_breadth_min".into(),
                json_float(self.recovery_breadth_min),
            ),
            ("recovery_h20_min".into(), json_float(self.recovery_h20_min)),
            (
                "recovery_confirm_evaluations".into(),
                self.recovery_confirm_evaluations.to_string(),
            ),
            (
                "min_breadth_coverage_pct".into(),
                json_float(self.min_breadth_coverage_pct),
            ),
            ("breadth_required".into(), self.breadth_required.to_string()),
            (
                "breadth_stale_days".into(),
                self.breadth_stale_days.to_string(),
            ),
            ("index_stale_days".into(), self.index_stale_days.to_string()),
            ("tier_exposure_pct".into(), fmap(&self.tier_exposure_pct)),
            (
                "exposure_tolerance_pct".into(),
                json_float(self.exposure_tolerance_pct),
            ),
            (
                "weekly_evaluation_weekday".into(),
                self.weekly_evaluation_weekday.to_string(),
            ),
            (
                "bootstrap_policy".into(),
                jstr(self.bootstrap_policy.as_str()),
            ),
            ("algorithm_version".into(), jstr(&self.algorithm_version)),
        ];
        json_map(&mut top)
    }

    /// `config_hash()`: the first 16 hex digits of the canonical JSON's SHA-256.
    #[must_use]
    pub fn config_hash(&self) -> String {
        let mut h = sha256_hex(self.canonical_json().as_bytes());
        h.truncate(16);
        h
    }

    /// `validate()`: every problem at once, as one `ConfigError`.
    pub fn validate(&self) -> ExposureResult<()> {
        let mut errs: Vec<String> = Vec::new();

        let uniq: BTreeSet<usize> = self.ma_lengths.iter().copied().collect();
        if !self.ma_lengths.iter().copied().eq(uniq.iter().copied()) {
            errs.push(format!(
                "ma_lengths must be strictly increasing and unique: {}",
                py_tuple_repr(&self.ma_lengths)
            ));
        }
        if self.ma_lengths.is_empty() {
            errs.push("ma_lengths must not be empty".into());
        }

        let caps: Vec<Option<f64>> = RegimeTier::ALL
            .iter()
            .map(|t| self.tier_exposure_pct.get(t.as_str()).copied())
            .collect();
        if caps.iter().any(Option::is_none) {
            errs.push("tier_exposure_pct must define R1, R2, R3 and R4".into());
        } else {
            let c: Vec<f64> = caps.iter().flatten().copied().collect();
            let repr = format!(
                "[{}]",
                c.iter().map(|x| fr(*x)).collect::<Vec<_>>().join(", ")
            );
            if c.windows(2).any(|w| w[0] < w[1]) {
                errs.push(format!(
                    "tier exposure caps must be non-increasing R1->R4: {repr}"
                ));
            }
            if c.iter().any(|x| !(0.0..=100.0).contains(x)) {
                errs.push(format!("tier exposure caps must be within 0-100: {repr}"));
            }
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
                    fr(val)
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
                    fr(val)
                ));
            }
        }

        let w = &self.default_book_weights;
        if w.values().any(|v| *v < 0.0) {
            errs.push(format!(
                "default_book_weights must be non-negative: {}",
                py_dict_repr(w)
            ));
        }
        if py_sum(w.values().copied()) <= 0.0 {
            errs.push("default_book_weights must be normalizable (sum > 0)".into());
        }
        let missing: BTreeSet<&str> = self
            .structural_indices
            .keys()
            .filter(|k| !w.contains_key(k))
            .collect();
        if !missing.is_empty() {
            let items: Vec<String> = missing.iter().map(|k| py_str_repr(k)).collect();
            errs.push(format!(
                "default_book_weights missing categories: [{}]",
                items.join(", ")
            ));
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
            errs.push("recovery_confirm_evaluations must be at least 1".into());
        }
        if !(0..=6).contains(&self.weekly_evaluation_weekday) {
            errs.push("weekly_evaluation_weekday must be 0-6 (Mon-Sun)".into());
        }

        if self.structural_indices.is_empty() {
            errs.push("structural_indices must not be empty".into());
        }
        if self
            .structural_indices
            .values()
            .any(|v| v.trim().is_empty())
        {
            errs.push("structural index identifiers must be non-empty".into());
        }
        if self.momentum_sentinel.trim().is_empty() {
            errs.push("momentum_sentinel must be non-empty".into());
        }

        if errs.is_empty() {
            Ok(())
        } else {
            Err(ExposureError::Config(format!(
                "invalid regime configuration:\n  - {}",
                errs.join("\n  - ")
            )))
        }
    }
}

/// `Candle`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Candle {
    /// Session date.
    pub date: NaiveDate,
    /// Open.
    pub open: Option<f64>,
    /// High.
    pub high: Option<f64>,
    /// Low.
    pub low: Option<f64>,
    /// Close.
    pub close: f64,
    /// Whether the session is complete.
    #[serde(default = "yes")]
    pub is_final: bool,
}

fn yes() -> bool {
    true
}

/// `MaSignal`; serialises as its `as_dict()`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MaSignal {
    /// MA length.
    pub ma_length: usize,
    /// Last close.
    pub close: f64,
    /// Last MA value.
    pub ma: Option<f64>,
    /// Distance of close from MA, percent.
    pub distance_pct: Option<f64>,
    /// Confirmed state.
    pub state: SignalState,
    /// Trailing run of closes on today's side.
    pub confirming_closes: i64,
}

/// `IndexSignals`; serialises as its `as_dict()`.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct IndexSignals {
    /// Index name.
    pub index_name: String,
    /// Last final candle's date.
    pub as_of_date: Option<NaiveDate>,
    /// Last final close.
    pub close: Option<f64>,
    /// MA length to signal.
    pub signals: BTreeMap<usize, MaSignal>,
    /// Stale against the as-of date.
    #[serde(default)]
    pub is_stale: bool,
    /// Any final candle existed.
    #[serde(default = "yes")]
    pub has_data: bool,
    /// Kite instrument token.
    #[serde(default)]
    pub instrument_token: Option<i64>,
}

impl IndexSignals {
    /// `state(ma_length)`.
    #[must_use]
    pub fn state(&self, ma_length: usize) -> SignalState {
        self.signals
            .get(&ma_length)
            .map_or(SignalState::Unknown, |s| s.state)
    }

    /// `is_above(ma_length)`.
    #[must_use]
    pub fn is_above(&self, ma_length: usize) -> bool {
        self.state(ma_length) == SignalState::Above
    }

    /// `is_below(ma_length)`.
    #[must_use]
    pub fn is_below(&self, ma_length: usize) -> bool {
        self.state(ma_length) == SignalState::Below
    }

    /// `any_unknown`.
    #[must_use]
    pub fn any_unknown(&self) -> bool {
        !self.has_data
            || self
                .signals
                .values()
                .any(|s| s.state == SignalState::Unknown)
    }

    /// `bearish_stack(lengths)`: MAs strictly ascending by length and close confirmed below the first.
    #[must_use]
    pub fn bearish_stack(&self, lengths: &[usize]) -> bool {
        let mut mas = Vec::with_capacity(lengths.len());
        for n in lengths {
            match self.signals.get(n) {
                Some(s) => mas.push(s.ma),
                None => return false,
            }
        }
        let Some(mas) = mas.into_iter().collect::<Option<Vec<f64>>>() else {
            return false;
        };
        let Some(first) = lengths.first() else {
            return false;
        };
        mas.windows(2).all(|w| w[0] < w[1]) && self.is_below(*first)
    }
}

impl Serialize for IndexSignals {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut m = s.serialize_map(Some(8))?;
        m.serialize_entry("index_name", &self.index_name)?;
        m.serialize_entry("as_of_date", &self.as_of_date)?;
        m.serialize_entry("close", &self.close)?;
        m.serialize_entry("is_stale", &self.is_stale)?;
        m.serialize_entry("has_data", &self.has_data)?;
        m.serialize_entry("instrument_token", &self.instrument_token)?;
        let sig: StrMap<&MaSignal> = self
            .signals
            .iter()
            .map(|(k, v)| (k.to_string(), v))
            .collect();
        m.serialize_entry("signals", &sig)?;
        m.serialize_entry("bearish_stack", &self.bearish_stack(&[20, 50, 200]))?;
        m.end()
    }
}

/// `BreadthReading`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BreadthReading {
    /// Reading date.
    pub as_of_date: NaiveDate,
    /// Percent of the universe above its 20-DMA.
    pub pct_above_20dma: f64,
    /// Eligible names.
    pub eligible_count: i64,
    /// Observed names.
    pub observed_count: i64,
    /// Coverage, percent.
    pub coverage_pct: f64,
    /// Universe id.
    pub universe_id: String,
    /// Universe hash.
    pub universe_hash: String,
    /// Audit run id.
    pub audit_run_id: String,
    /// Calculation version.
    pub calculation_version: String,
}

impl BreadthReading {
    /// `is_usable(cfg, as_of)`.
    #[must_use]
    pub fn is_usable(&self, cfg: &RegimeConfig, as_of: NaiveDate) -> (bool, Vec<&'static str>) {
        let mut codes = Vec::new();
        if self.coverage_pct < cfg.min_breadth_coverage_pct {
            codes.push(reason::BREADTH_LOW_COVERAGE);
        }
        if (as_of - self.as_of_date).num_days() > cfg.breadth_stale_days {
            codes.push(reason::BREADTH_STALE);
        }
        (codes.is_empty(), codes)
    }
}

/// `BookWeights`; serialises as its `as_dict()`.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct BookWeights {
    /// Category weights.
    pub weights: StrMap<f64>,
    /// Where the weights came from.
    pub source: WeightSource,
    /// Classification coverage, percent.
    pub coverage_pct: f64,
    /// Reason codes.
    #[serde(default)]
    pub reason_codes: Vec<String>,
}

impl Serialize for BookWeights {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut m = s.serialize_map(Some(4))?;
        m.serialize_entry("weights", &self.weights.sorted())?;
        m.serialize_entry("source", &self.source)?;
        m.serialize_entry("coverage_pct", &self.coverage_pct)?;
        m.serialize_entry("reason_codes", &self.reason_codes)?;
        m.end()
    }
}

/// `TargetPosition`: a line of the full-risk target book.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TargetPosition {
    /// Symbol.
    pub symbol: String,
    /// Market-cap category, or `None` when unclassified.
    pub category: Option<String>,
    /// Market value.
    pub market_value: f64,
}

/// `ExposureSnapshot`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExposureSnapshot {
    /// Actual equity, percent of NAV.
    pub actual_equity_pct: f64,
    /// Pending buys, percent.
    #[serde(default)]
    pub pending_buy_pct: f64,
    /// Pending sells, percent.
    #[serde(default)]
    pub pending_sell_pct: f64,
    /// Execution status.
    #[serde(default = "not_planned")]
    pub execution_status: ExecutionStatus,
}

fn not_planned() -> ExecutionStatus {
    ExecutionStatus::NotPlanned
}

impl ExposureSnapshot {
    /// `ExposureSnapshot(actual_equity_pct=...)` with the defaults.
    #[must_use]
    pub fn new(actual_equity_pct: f64) -> Self {
        Self {
            actual_equity_pct,
            pending_buy_pct: 0.0,
            pending_sell_pct: 0.0,
            execution_status: ExecutionStatus::NotPlanned,
        }
    }
}

/// `PreviousRegime`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PreviousRegime {
    /// Committed policy tier.
    pub policy_tier: RegimeTier,
    /// Its scheduled week end.
    pub scheduled_week_end: NaiveDate,
    /// Last transition date.
    #[serde(default)]
    pub last_transition_date: Option<NaiveDate>,
    /// Recovery confirmations so far.
    #[serde(default)]
    pub recovery_confirmations: i64,
    /// Frozen book weights for the week.
    #[serde(default)]
    pub book_weights: Option<BookWeights>,
}

impl PreviousRegime {
    /// `PreviousRegime(tier, week_end)` with the defaults.
    #[must_use]
    pub fn new(policy_tier: RegimeTier, scheduled_week_end: NaiveDate) -> Self {
        Self {
            policy_tier,
            scheduled_week_end,
            last_transition_date: None,
            recovery_confirmations: 0,
            book_weights: None,
        }
    }
}

/// `RegimeState`; serialises as its `as_dict()`.
#[derive(Debug, Clone, PartialEq)]
pub struct RegimeState {
    /// Raw candidate tier.
    pub raw_candidate_tier: RegimeTier,
    /// Previous policy tier.
    pub previous_policy_tier: Option<RegimeTier>,
    /// Committed policy tier.
    pub policy_tier: RegimeTier,
    /// Re-risking was capped.
    pub transition_limited: bool,
    /// Equity cap, percent.
    pub target_equity_cap_pct: f64,
    /// Actual equity, percent.
    pub actual_equity_pct: f64,
    /// Actual minus cap.
    pub exposure_gap_pct: f64,
    /// Pending sells, percent.
    pub pending_sell_pct: f64,
    /// Pending buys, percent.
    pub pending_buy_pct: f64,
    /// Execution status.
    pub execution_status: ExecutionStatus,
    /// New-buy mode.
    pub new_buys: NewBuyMode,
    /// Forced action.
    pub forced_action: ForcedAction,
    /// Ordered, de-duplicated reason codes.
    pub reason_codes: Vec<String>,
    /// Display text per code.
    pub reasons: Vec<String>,
    /// As-of date.
    pub as_of_date: NaiveDate,
    /// Signal session date.
    pub signal_session_date: NaiveDate,
    /// Scheduled week end.
    pub scheduled_week_end: NaiveDate,
    /// Deterministic evaluation id.
    pub evaluation_id: String,
    /// Breadth, percent.
    pub breadth_pct: Option<f64>,
    /// Breadth coverage, percent.
    pub breadth_coverage_pct: Option<f64>,
    /// Recovery override applied.
    pub override_active: bool,
    /// Book category weights.
    pub book_category_weights: StrMap<f64>,
    /// Book weight source.
    pub book_weight_source: WeightSource,
    /// Index diagnostics, each an `IndexSignals.as_dict()`.
    pub index_diagnostics: StrMap<IndexSignals>,
    /// Last transition date.
    pub last_transition_date: Option<NaiveDate>,
    /// Next evaluation date.
    pub next_evaluation_date: NaiveDate,
    /// Index data missing or stale.
    pub data_stale: bool,
    /// Manual action required.
    pub manual_action_required: bool,
    /// Algorithm version.
    pub algorithm_version: String,
    /// Config hash.
    pub config_hash: String,
    /// Weighted health per MA length.
    pub health: BTreeMap<usize, f64>,
    /// Bearish-stack weight.
    pub bearish_stack_weight: f64,
}

impl Serialize for RegimeState {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut m = s.serialize_map(Some(32))?;
        m.serialize_entry("raw_candidate_tier", &self.raw_candidate_tier)?;
        m.serialize_entry("previous_policy_tier", &self.previous_policy_tier)?;
        m.serialize_entry("policy_tier", &self.policy_tier)?;
        m.serialize_entry("transition_limited", &self.transition_limited)?;
        m.serialize_entry("target_equity_cap_pct", &self.target_equity_cap_pct)?;
        m.serialize_entry("actual_equity_pct", &self.actual_equity_pct)?;
        m.serialize_entry("exposure_gap_pct", &self.exposure_gap_pct)?;
        m.serialize_entry("pending_sell_pct", &self.pending_sell_pct)?;
        m.serialize_entry("pending_buy_pct", &self.pending_buy_pct)?;
        m.serialize_entry("execution_status", &self.execution_status)?;
        m.serialize_entry("new_buys", &self.new_buys)?;
        m.serialize_entry("forced_action", &self.forced_action)?;
        m.serialize_entry("reason_codes", &self.reason_codes)?;
        m.serialize_entry("reasons", &self.reasons)?;
        m.serialize_entry("as_of_date", &self.as_of_date)?;
        m.serialize_entry("signal_session_date", &self.signal_session_date)?;
        m.serialize_entry("scheduled_week_end", &self.scheduled_week_end)?;
        m.serialize_entry("evaluation_id", &self.evaluation_id)?;
        m.serialize_entry("breadth_pct", &self.breadth_pct)?;
        m.serialize_entry("breadth_coverage_pct", &self.breadth_coverage_pct)?;
        m.serialize_entry("override_active", &self.override_active)?;
        m.serialize_entry(
            "book_category_weights",
            &self.book_category_weights.sorted(),
        )?;
        m.serialize_entry("book_weight_source", &self.book_weight_source)?;
        m.serialize_entry("index_diagnostics", &self.index_diagnostics)?;
        m.serialize_entry("last_transition_date", &self.last_transition_date)?;
        m.serialize_entry("next_evaluation_date", &self.next_evaluation_date)?;
        m.serialize_entry("data_stale", &self.data_stale)?;
        m.serialize_entry("manual_action_required", &self.manual_action_required)?;
        m.serialize_entry("algorithm_version", &self.algorithm_version)?;
        m.serialize_entry("config_hash", &self.config_hash)?;
        let health: StrMap<f64> = self
            .health
            .iter()
            .map(|(k, v)| (k.to_string(), *v))
            .collect();
        m.serialize_entry("health", &health)?;
        m.serialize_entry("bearish_stack_weight", &self.bearish_stack_weight)?;
        m.end()
    }
}

/// `moving_average(closes, length)`: `None` until `length` observations exist.
pub fn moving_average(closes: &[f64], length: usize) -> ExposureResult<Vec<Option<f64>>> {
    if length == 0 && !closes.is_empty() {
        return Err(ExposureError::ZeroDivision("float division by zero".into()));
    }
    let len_f = usize_f64(length);
    let mut out = Vec::with_capacity(closes.len());
    let mut running = 0.0_f64;
    for (i, c) in closes.iter().enumerate() {
        running += c;
        if i >= length {
            running -= closes[i - length];
        }
        out.push(if i + 1 >= length {
            Some(running / len_f)
        } else {
            None
        });
    }
    Ok(out)
}

/// `confirmed_state_series`: per-day (confirmed state, trailing confirming closes).
#[must_use]
pub fn confirmed_state_series(
    closes: &[f64],
    mas: &[Option<f64>],
    buffer: f64,
    confirm_days: i64,
) -> Vec<(SignalState, i64)> {
    let n = closes.len();
    let mut above = vec![false; n];
    let mut below = vec![false; n];
    for i in 0..n {
        if let Some(Some(ma)) = mas.get(i) {
            above[i] = closes[i] > ma * (1.0 + buffer);
            below[i] = closes[i] < ma * (1.0 - buffer);
        }
    }
    let mut out = Vec::with_capacity(n);
    let mut state = SignalState::Unknown;
    let (mut run_above, mut run_below) = (0_i64, 0_i64);
    for i in 0..n {
        let i64i = i64::try_from(i).unwrap_or(i64::MAX);
        if i64i + 1 >= confirm_days {
            let start = usize::try_from(i64i - confirm_days + 1).unwrap_or(0);
            let window = start..=i;
            if start > i || window.clone().all(|k| above[k]) {
                state = SignalState::Above;
            } else if window.clone().all(|k| below[k]) {
                state = SignalState::Below;
            }
        }
        run_above = if above[i] { run_above + 1 } else { 0 };
        run_below = if below[i] { run_below + 1 } else { 0 };
        let run = if above[i] {
            run_above
        } else if below[i] {
            run_below
        } else {
            0
        };
        out.push((state, run));
    }
    out
}

/// `build_index_signals`: final candles to confirmed signals.
pub fn build_index_signals(
    index_name: &str,
    candles: &[Candle],
    cfg: &RegimeConfig,
    as_of: Option<NaiveDate>,
    instrument_token: Option<i64>,
) -> ExposureResult<IndexSignals> {
    let mut sorted: Vec<&Candle> = candles.iter().collect();
    sorted.sort_by_key(|c| c.date);
    let finals: Vec<&Candle> = sorted.into_iter().filter(|c| c.is_final).collect();
    let Some(last) = finals.last() else {
        return Ok(IndexSignals {
            index_name: index_name.to_owned(),
            as_of_date: None,
            close: None,
            signals: BTreeMap::new(),
            is_stale: true,
            has_data: false,
            instrument_token,
        });
    };
    let closes: Vec<f64> = finals.iter().map(|c| c.close).collect();
    let mut signals = BTreeMap::new();
    for &length in &cfg.ma_lengths {
        let mas = moving_average(&closes, length)?;
        let states = confirmed_state_series(&closes, &mas, cfg.buffer(), cfg.confirm_days);
        let ma_last = mas.last().copied().flatten();
        let (state, run) = states.last().copied().unwrap_or((SignalState::Unknown, 0));
        let distance_pct = match ma_last {
            Some(m) if m != 0.0 => Some((last.close / m - 1.0) * 100.0),
            _ => None,
        };
        signals.insert(
            length,
            MaSignal {
                ma_length: length,
                close: last.close,
                ma: ma_last,
                distance_pct,
                state,
                confirming_closes: run,
            },
        );
    }
    let reference = as_of.unwrap_or(last.date);
    let stale = (reference - last.date).num_days() > cfg.index_stale_days;
    Ok(IndexSignals {
        index_name: index_name.to_owned(),
        as_of_date: Some(last.date),
        close: Some(last.close),
        signals,
        is_stale: stale,
        has_data: true,
        instrument_token,
    })
}

fn normalise(weights: &StrMap<f64>) -> ExposureResult<StrMap<f64>> {
    let total = py_sum(weights.values().copied().filter(|v| *v > 0.0));
    if total <= 0.0 {
        return Err(ExposureError::Config(format!(
            "book weights are not normalizable: {}",
            py_dict_repr(weights)
        )));
    }
    Ok(weights
        .iter()
        .map(|(k, v)| (k, if *v > 0.0 { v / total } else { 0.0 }))
        .collect())
}

/// `resolve_book_weights`: full-risk target, else last R1 snapshot, else config defaults.
pub fn resolve_book_weights(
    cfg: &RegimeConfig,
    full_risk_target: Option<&[TargetPosition]>,
    last_r1_snapshot: Option<&BookWeights>,
) -> ExposureResult<BookWeights> {
    let mut codes: Vec<String> = Vec::new();

    if let Some(target) = full_risk_target.filter(|t| !t.is_empty()) {
        let total = py_sum(target.iter().map(|p| py_max(p.market_value, 0.0)));
        if total > 0.0 {
            let mut known: StrMap<f64> = cfg.structural_indices.keys().map(|c| (c, 0.0)).collect();
            let mut classified = 0.0_f64;
            for p in target {
                let v = py_max(p.market_value, 0.0);
                if let Some(slot) = p.category.as_deref().and_then(|c| known.get_mut(c)) {
                    *slot += v;
                    classified += v;
                }
            }
            let coverage = classified / total * 100.0;
            if coverage >= cfg.min_classification_coverage_pct && classified > 0.0 {
                return Ok(BookWeights {
                    weights: normalise(&known)?,
                    source: WeightSource::FullRiskTarget,
                    coverage_pct: py_round(coverage, 2),
                    reason_codes: vec![reason::BOOK_WEIGHTS_FULL_RISK_TARGET.into()],
                });
            }
            codes.push(reason::BOOK_CLASSIFICATION_COVERAGE_LOW.into());
        }
    }

    if let Some(snap) = last_r1_snapshot.filter(|s| !s.weights.is_empty()) {
        codes.push(reason::BOOK_WEIGHTS_LAST_R1_SNAPSHOT.into());
        return Ok(BookWeights {
            weights: normalise(&snap.weights)?,
            source: WeightSource::LastR1Snapshot,
            coverage_pct: snap.coverage_pct,
            reason_codes: codes,
        });
    }

    codes.push(reason::BOOK_WEIGHTS_CONFIG_DEFAULT.into());
    Ok(BookWeights {
        weights: normalise(&cfg.default_book_weights)?,
        source: WeightSource::ConfigDefault,
        coverage_pct: 0.0,
        reason_codes: codes,
    })
}

/// `weighted_health`: sum of category weights whose index is confirmed above `MA_m`.
#[must_use]
pub fn weighted_health(
    book: &BookWeights,
    index_signals: &BTreeMap<String, IndexSignals>,
    cfg: &RegimeConfig,
    ma_length: usize,
) -> f64 {
    let mut total = 0.0_f64;
    for (category, index_name) in cfg.structural_indices.iter() {
        let w = book.weights.get(category).copied().unwrap_or(0.0);
        if index_signals
            .get(index_name)
            .is_some_and(|s| s.is_above(ma_length))
        {
            total += w;
        }
    }
    py_round(total, 10)
}

/// `optimistic_health`: health with every degraded signal read as above.
#[must_use]
pub fn optimistic_health(
    book: &BookWeights,
    index_signals: &BTreeMap<String, IndexSignals>,
    cfg: &RegimeConfig,
    ma_length: usize,
) -> f64 {
    let mut total = 0.0_f64;
    for (category, index_name) in cfg.structural_indices.iter() {
        let w = book.weights.get(category).copied().unwrap_or(0.0);
        match index_signals.get(index_name) {
            None => total += w,
            Some(s) if !s.has_data || s.is_stale => total += w,
            Some(s) if s.state(ma_length) != SignalState::Below => total += w,
            Some(_) => {}
        }
    }
    py_round(total, 10)
}

/// `optimistic_sentinel`: a degraded sentinel imposes no floor.
#[must_use]
pub fn optimistic_sentinel(sentinel: Option<&IndexSignals>) -> Option<&IndexSignals> {
    sentinel.filter(|s| s.has_data && !s.is_stale)
}

/// `bearish_stack_weight`.
#[must_use]
pub fn bearish_stack_weight(
    book: &BookWeights,
    index_signals: &BTreeMap<String, IndexSignals>,
    cfg: &RegimeConfig,
) -> f64 {
    let mut total = 0.0_f64;
    for (category, index_name) in cfg.structural_indices.iter() {
        if index_signals
            .get(index_name)
            .is_some_and(|s| s.bearish_stack(&cfg.ma_lengths))
        {
            total += book.weights.get(category).copied().unwrap_or(0.0);
        }
    }
    py_round(total, 10)
}

fn h(health: &BTreeMap<usize, f64>, m: usize) -> f64 {
    health.get(&m).copied().unwrap_or(0.0)
}

/// `classify_candidate`: R4, then R3, then R1, else R2.
#[must_use]
pub fn classify_candidate(
    health: &BTreeMap<usize, f64>,
    stack_weight: f64,
    breadth_pct: Option<f64>,
    breadth_usable: bool,
    sentinel: Option<&IndexSignals>,
    cfg: &RegimeConfig,
) -> (RegimeTier, Vec<&'static str>) {
    let mut codes = Vec::new();
    let h50 = h(health, 50);
    let h200 = h(health, 200);
    let b = if breadth_usable { breadth_pct } else { None };

    let sentinel_below_200 = sentinel.is_some_and(|s| s.is_below(200));
    let sentinel_above_50 = sentinel.is_some_and(|s| s.is_above(50));

    if let Some(b) = b {
        if stack_weight >= cfg.r4_bearish_stack_min && b < cfg.r4_breadth_max {
            codes.push(reason::R4_BEARISH_STACK_AND_WEAK_BREADTH);
            return (RegimeTier::R4, codes);
        }
        if sentinel_below_200 && h200 < cfg.r4_h200_max && b < cfg.r4_breadth_max {
            codes.push(reason::R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH);
            return (RegimeTier::R4, codes);
        }
    }

    let mut r3 = false;
    if h200 <= cfg.r3_h200_max {
        codes.push(reason::R3_H200_WEAK);
        r3 = true;
    }
    if b.is_some_and(|b| b < cfg.r3_breadth_max) {
        codes.push(reason::R3_BREADTH_WEAK);
        r3 = true;
    }
    if sentinel_below_200 {
        codes.push(reason::R3_SENTINEL_BELOW_200DMA);
        r3 = true;
    }
    if r3 {
        return (RegimeTier::R3, codes);
    }

    let breadth_ok_for_r1 = if cfg.breadth_required {
        b.is_some_and(|b| b >= cfg.r1_breadth_min)
    } else {
        true
    };
    if h50 >= cfg.r1_h50_min && h200 >= cfg.r1_h200_min && breadth_ok_for_r1 && sentinel_above_50 {
        codes.push(reason::R1_ALL_CONDITIONS_MET);
        return (RegimeTier::R1, codes);
    }

    codes.push(reason::R2_DEFAULT);
    (RegimeTier::R2, codes)
}

/// `apply_sentinel_floor`: the sentinel only makes the tier more defensive.
#[must_use]
pub fn apply_sentinel_floor(
    tier: RegimeTier,
    sentinel: Option<&IndexSignals>,
) -> (RegimeTier, Vec<&'static str>) {
    let mut codes = Vec::new();
    let Some(s) = sentinel.filter(|s| s.has_data) else {
        codes.push(reason::SENTINEL_UNKNOWN);
        return (tier, codes);
    };
    let mut out = tier;
    if s.is_below(50) && out.ordinal() < RegimeTier::R2.ordinal() {
        out = RegimeTier::R2;
        codes.push(reason::SENTINEL_FLOOR_R2);
    }
    if s.is_below(200) && out.ordinal() < RegimeTier::R3.ordinal() {
        out = RegimeTier::R3;
        codes.push(reason::SENTINEL_FLOOR_R3);
    }
    if s.is_below(50) {
        codes.push(reason::SENTINEL_BELOW_50DMA_VETO);
    }
    (out, codes)
}

/// `apply_recovery_override`: lift R3/R4 to no better than R2. Returns (tier, active, codes).
#[must_use]
pub fn apply_recovery_override(
    tier: RegimeTier,
    health: &BTreeMap<usize, f64>,
    breadth_pct: Option<f64>,
    breadth_usable: bool,
    confirmations: i64,
    cfg: &RegimeConfig,
) -> (RegimeTier, bool, Vec<&'static str>) {
    let mut codes = Vec::new();
    if tier.ordinal() < RegimeTier::R3.ordinal() {
        return (tier, false, codes);
    }
    let Some(b) = breadth_pct.filter(|_| breadth_usable) else {
        codes.push(reason::RECOVERY_OVERRIDE_BREADTH_UNUSABLE);
        return (tier, false, codes);
    };
    if b < cfg.recovery_breadth_min {
        return (tier, false, codes);
    }
    if h(health, 20) < cfg.recovery_h20_min {
        codes.push(reason::RECOVERY_OVERRIDE_H20_TOO_WEAK);
        return (tier, false, codes);
    }
    if confirmations < cfg.recovery_confirm_evaluations {
        codes.push(reason::RECOVERY_OVERRIDE_UNCONFIRMED);
        return (tier, false, codes);
    }
    codes.push(reason::RECOVERY_OVERRIDE_APPLIED);
    (RegimeTier::R2, true, codes)
}

/// `apply_transition`: risk-off is immediate, re-risking one tier per week.
#[must_use]
pub fn apply_transition(
    previous: Option<RegimeTier>,
    candidate: RegimeTier,
) -> (RegimeTier, bool, Vec<&'static str>) {
    let Some(previous) = previous else {
        return (candidate, false, vec![reason::BOOTSTRAP_NO_PRIOR_STATE]);
    };
    if candidate.ordinal() > previous.ordinal() {
        return (candidate, false, vec![reason::TRANSITION_RISK_OFF_DIRECT]);
    }
    if candidate.ordinal() < previous.ordinal() {
        let stepped = RegimeTier::from_ordinal(previous.ordinal() - 1);
        let limited = stepped != candidate;
        let codes = if limited {
            vec![reason::TRANSITION_RERISK_CAPPED]
        } else {
            Vec::new()
        };
        return (stepped, limited, codes);
    }
    (previous, false, vec![reason::TRANSITION_NONE])
}

/// `next_evaluation_date`: the next date strictly after `after` on the evaluation weekday.
#[must_use]
pub fn next_evaluation_date(after: NaiveDate, cfg: &RegimeConfig) -> NaiveDate {
    let wd = i64::from(after.weekday().num_days_from_monday());
    let delta = (cfg.weekly_evaluation_weekday - wd).rem_euclid(7);
    after + Duration::days(if delta == 0 { 7 } else { delta })
}

/// `make_evaluation_id`: deterministic per week, session and config.
#[must_use]
pub fn make_evaluation_id(
    scheduled_week_end: NaiveDate,
    signal_session_date: NaiveDate,
    cfg: &RegimeConfig,
) -> String {
    let mut blob = String::new();
    let _ = write!(
        blob,
        "{}|{}|{}|{}",
        scheduled_week_end.format("%Y-%m-%d"),
        signal_session_date.format("%Y-%m-%d"),
        cfg.algorithm_version,
        cfg.config_hash()
    );
    let mut h = sha256_hex(blob.as_bytes());
    h.truncate(24);
    h
}

/// `resolve_new_buys`.
#[must_use]
pub fn resolve_new_buys(
    tier: RegimeTier,
    sentinel: Option<&IndexSignals>,
    data_ok: bool,
) -> (NewBuyMode, Vec<&'static str>) {
    if !data_ok {
        return (NewBuyMode::Blocked, vec![reason::NEW_BUYS_BLOCKED_DATA]);
    }
    if sentinel.is_some_and(|s| s.is_below(50)) {
        return (NewBuyMode::Blocked, vec![reason::NEW_BUYS_BLOCKED_SENTINEL]);
    }
    match tier {
        RegimeTier::R1 => (NewBuyMode::Full, vec![reason::NEW_BUYS_FULL]),
        RegimeTier::R2 => (NewBuyMode::Half, vec![reason::NEW_BUYS_HALF]),
        _ => (NewBuyMode::Blocked, vec![reason::NEW_BUYS_BLOCKED_TIER]),
    }
}

/// `resolve_forced_action`: driven by the exposure gap.
#[must_use]
pub fn resolve_forced_action(
    tier: RegimeTier,
    gap_pct: f64,
    cfg: &RegimeConfig,
    bootstrap_suppressed: bool,
) -> (ForcedAction, Vec<&'static str>) {
    if gap_pct <= cfg.exposure_tolerance_pct {
        return (ForcedAction::None, vec![reason::EXPOSURE_WITHIN_CAP]);
    }
    if bootstrap_suppressed {
        return (
            ForcedAction::None,
            vec![reason::FORCED_SELL_SUPPRESSED_BOOTSTRAP],
        );
    }
    let codes = vec![reason::EXPOSURE_GAP_PENDING];
    let action = match tier {
        RegimeTier::R4 => ForcedAction::CrashReduce,
        RegimeTier::R3 => ForcedAction::ReduceToCap,
        RegimeTier::R2 => ForcedAction::TrimToCap,
        RegimeTier::R1 => ForcedAction::None,
    };
    (action, codes)
}

/// The keyword arguments of `evaluate`.
#[derive(Debug, Clone, Copy)]
pub struct EvaluateArgs<'a> {
    /// As-of date.
    pub as_of_date: NaiveDate,
    /// Scheduled week end.
    pub scheduled_week_end: NaiveDate,
    /// Signal session date.
    pub signal_session_date: NaiveDate,
    /// Structural index signals by index name.
    pub index_signals: &'a BTreeMap<String, IndexSignals>,
    /// Momentum sentinel.
    pub sentinel: Option<&'a IndexSignals>,
    /// Breadth reading.
    pub breadth: Option<&'a BreadthReading>,
    /// Frozen book weights.
    pub book: &'a BookWeights,
    /// Exposure snapshot.
    pub exposure: &'a ExposureSnapshot,
    /// Configuration.
    pub cfg: &'a RegimeConfig,
    /// Previous committed regime.
    pub previous: Option<&'a PreviousRegime>,
}

fn has_affirmative(codes: &[&str]) -> bool {
    codes.iter().any(|c| AFFIRMATIVE_RISK_OFF.contains(c))
}

/// `evaluate`: the single, pure entry point.
pub fn evaluate(a: &EvaluateArgs<'_>) -> ExposureResult<RegimeState> {
    let cfg = a.cfg;
    let idx = a.index_signals;
    let mut codes: Vec<String> = Vec::new();
    let push =
        |codes: &mut Vec<String>, cs: &[&str]| codes.extend(cs.iter().map(|c| (*c).to_owned()));

    let required: Vec<&str> = cfg
        .structural_indices
        .values()
        .map(String::as_str)
        .collect();
    let missing = required
        .iter()
        .any(|n| idx.get(*n).is_none_or(|s| !s.has_data));
    let stale = required
        .iter()
        .any(|n| idx.get(*n).is_some_and(|s| s.is_stale));
    let unknown = required
        .iter()
        .any(|n| idx.get(*n).is_some_and(IndexSignals::any_unknown));

    if missing {
        push(&mut codes, &[reason::DATA_INDEX_MISSING]);
    }
    if stale || a.sentinel.is_some_and(|s| s.is_stale) {
        push(&mut codes, &[reason::DATA_STALE]);
    }
    if unknown || a.sentinel.is_some_and(IndexSignals::any_unknown) {
        push(&mut codes, &[reason::SIGNAL_UNKNOWN]);
    }
    let data_stale = missing || stale;
    let mut data_ok = !(missing || stale || unknown);

    let mut breadth_usable = false;
    let breadth_pct = a.breadth.map(|b| b.pct_above_20dma);
    let breadth_cov = a.breadth.map(|b| b.coverage_pct);
    if !cfg.breadth_required {
        push(&mut codes, &[reason::BREADTH_NOT_USED]);
    } else if let Some(b) = a.breadth {
        let (usable, bcodes) = b.is_usable(cfg, a.as_of_date);
        breadth_usable = usable;
        push(&mut codes, &bcodes);
    } else {
        push(&mut codes, &[reason::BREADTH_MISSING]);
    }
    if cfg.breadth_required && !breadth_usable {
        data_ok = false;
    }

    codes.extend(a.book.reason_codes.iter().cloned());

    let health: BTreeMap<usize, f64> = cfg
        .ma_lengths
        .iter()
        .map(|m| (*m, weighted_health(a.book, idx, cfg, *m)))
        .collect();
    let stack_w = bearish_stack_weight(a.book, idx, cfg);

    let (candidate, ccodes) = classify_candidate(
        &health,
        stack_w,
        breadth_pct,
        breadth_usable,
        a.sentinel,
        cfg,
    );
    push(&mut codes, &ccodes);

    let (candidate, scodes) = apply_sentinel_floor(candidate, a.sentinel);
    push(&mut codes, &scodes);

    let confirmations = a.previous.map_or(0, |p| p.recovery_confirmations);
    let (candidate, override_active, rcodes) = apply_recovery_override(
        candidate,
        &health,
        breadth_pct,
        breadth_usable,
        confirmations,
        cfg,
    );
    push(&mut codes, &rcodes);

    let mut raw_candidate = candidate;
    if unknown && raw_candidate == RegimeTier::R1 {
        raw_candidate = RegimeTier::R2;
    }

    let prev_tier = a.previous.map(|p| p.policy_tier);
    let degraded = missing || stale || unknown || (cfg.breadth_required && !breadth_usable);

    let (policy_tier, transition_limited) = match prev_tier {
        Some(prev) if degraded => {
            let health_opt: BTreeMap<usize, f64> = cfg
                .ma_lengths
                .iter()
                .map(|m| (*m, optimistic_health(a.book, idx, cfg, *m)))
                .collect();
            let sent_opt = optimistic_sentinel(a.sentinel);
            let (cand_opt, mut codes_opt) = classify_candidate(
                &health_opt,
                stack_w,
                breadth_pct,
                breadth_usable,
                sent_opt,
                cfg,
            );
            let (cand_opt, floor_opt) = apply_sentinel_floor(cand_opt, sent_opt);
            codes_opt.extend(floor_opt);
            if has_affirmative(&codes_opt) && cand_opt.ordinal() > prev.ordinal() {
                push(&mut codes, &[reason::TRANSITION_RISK_OFF_DIRECT]);
                (cand_opt, false)
            } else {
                push(&mut codes, &[reason::TRANSITION_HELD_STALE_DATA]);
                (prev, false)
            }
        }
        _ => {
            let (tier, limited, tcodes) = apply_transition(prev_tier, raw_candidate);
            push(&mut codes, &tcodes);
            (tier, limited)
        }
    };

    let bootstrap = prev_tier.is_none();
    let bootstrap_suppressed = bootstrap && cfg.bootstrap_policy == BootstrapPolicy::ObserveOnly;
    if bootstrap_suppressed {
        push(&mut codes, &[reason::BOOTSTRAP_OBSERVE_ONLY]);
    }

    let cap = cfg.cap_for(policy_tier)?;
    let gap = py_round(a.exposure.actual_equity_pct - cap, 6);
    if matches!(
        a.exposure.execution_status,
        ExecutionStatus::Partial | ExecutionStatus::Failed
    ) {
        push(&mut codes, &[reason::EXPOSURE_RECONCILE_RETRY]);
    }

    let (new_buys, nbcodes) = resolve_new_buys(policy_tier, a.sentinel, data_ok);
    push(&mut codes, &nbcodes);

    let (forced, fcodes) = resolve_forced_action(policy_tier, gap, cfg, bootstrap_suppressed);
    push(&mut codes, &fcodes);

    let manual = bootstrap_suppressed && gap > cfg.exposure_tolerance_pct;

    let transitioned = prev_tier.is_some_and(|p| p != policy_tier);
    let last_transition = if transitioned {
        Some(a.signal_session_date)
    } else {
        a.previous.and_then(|p| p.last_transition_date)
    };

    let mut seen: BTreeSet<String> = BTreeSet::new();
    let ordered: Vec<String> = codes
        .into_iter()
        .filter(|c| seen.insert(c.clone()))
        .collect();

    let mut diagnostics: StrMap<IndexSignals> =
        idx.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
    if let Some(s) = a.sentinel {
        diagnostics.insert(s.index_name.clone(), s.clone());
    }

    Ok(RegimeState {
        raw_candidate_tier: raw_candidate,
        previous_policy_tier: prev_tier,
        policy_tier,
        transition_limited,
        target_equity_cap_pct: cap,
        actual_equity_pct: a.exposure.actual_equity_pct,
        exposure_gap_pct: gap,
        pending_sell_pct: a.exposure.pending_sell_pct,
        pending_buy_pct: a.exposure.pending_buy_pct,
        execution_status: a.exposure.execution_status,
        new_buys,
        forced_action: forced,
        reasons: describe(&ordered),
        reason_codes: ordered,
        as_of_date: a.as_of_date,
        signal_session_date: a.signal_session_date,
        scheduled_week_end: a.scheduled_week_end,
        evaluation_id: make_evaluation_id(a.scheduled_week_end, a.signal_session_date, cfg),
        breadth_pct,
        breadth_coverage_pct: breadth_cov,
        override_active,
        book_category_weights: a.book.weights.clone(),
        book_weight_source: a.book.source,
        index_diagnostics: diagnostics,
        last_transition_date: last_transition,
        next_evaluation_date: next_evaluation_date(a.scheduled_week_end, cfg),
        data_stale,
        manual_action_required: manual,
        algorithm_version: cfg.algorithm_version.clone(),
        config_hash: cfg.config_hash(),
        health,
        bearish_stack_weight: stack_w,
    })
}
