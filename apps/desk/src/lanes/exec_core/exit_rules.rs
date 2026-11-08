//! Ports `app/exit_rules.py` (LV6 — review P2.3): the exact exit rule each sleeve runs, said
//! from its config, so the trade card cannot drift from the code that manages the position.
//! Since LV9 (Maulik, 28 Sep 2026) all three sleeves run Qullamaggie's exits with each sleeve's
//! own hard stop under it. Nothing here decides anything.
//!
//! The sentences are built from `baskfy_core`'s `Default` configs (Python's `DEFAULT_*_CONFIG`)
//! and are graded character for character against the live Python in
//! `tests/exec_gateway_exit_rules.rs`.

use std::str::FromStr as _;

use baskfy_core::swing::setups::SwingConfig;
use baskfy_core::swing::stops::StopConfig;
use baskfy_core::twt::config::TwtConfig;
use baskfy_core::vbt::config::VbtConfig;
use rust_decimal::Decimal;

/// `_pct(value)`: `f"{Decimal(str(value)).normalize():f}"` — `20.0` and `Decimal("20.0")` both
/// read `20`; `12.5` stays `12.5`.
#[must_use]
pub fn pct_f64(value: f64) -> String {
    // Rust's shortest round-trip `Display` is Python's `str(float)` without the trailing `.0`
    // and never in exponent form, which is what `.normalize():f` prints anyway.
    Decimal::from_str(&format!("{value}")).map_or_else(|_| format!("{value}"), pct_dec)
}

/// `_pct` of a `Decimal`.
#[must_use]
pub fn pct_dec(value: Decimal) -> String {
    value.normalize().to_string()
}

fn swing_rule(s: &StopConfig) -> String {
    format!(
        "stop under the range low, at most {}% below entry; \
         {}/{} sold between bar {} \
         and {}, then the stop to breakeven (or after {}R); \
         the rest trails the 10-day MA (ADR at or above {}%) or the \
         20-day MA; the GTT is the exit",
        pct_f64(s.max_stop_distance_pct),
        s.partial_numerator,
        s.partial_denominator,
        s.partial_earliest_bar,
        s.partial_latest_bar,
        pct_f64(s.breakeven_after_r),
        pct_f64(s.fast_trail_min_adr_pct),
    )
}

fn qulla_rule(prefix: &str, s: &StopConfig) -> String {
    format!(
        "{prefix}; \
         {}/{} sold into strength between bar \
         {} and bar {} after entry if green, then the \
         stop to breakeven (or after {}R); the rest trails the 10-day MA \
         (ADR at or above {}%) or the 20-day MA and is sold at the \
         next open on a close below it; the GTT is the hard stop (Qullamaggie's rule)",
        s.partial_numerator,
        s.partial_denominator,
        s.partial_earliest_bar,
        s.partial_latest_bar,
        pct_f64(s.breakeven_after_r),
        pct_f64(s.fast_trail_min_adr_pct),
    )
}

fn twt_rule(cfg: &TwtConfig) -> String {
    let e = &cfg.exits;
    if e.qulla_exits {
        return qulla_rule(
            &format!("{}% initial stop under the fill (GTT)", pct_dec(e.stop_pct)),
            &e.qulla,
        );
    }
    format!(
        "{}% initial stop under the fill; a {}% high-water \
         trail ratchets the GTT each evening; no target, no time stop — the GTT is the only exit",
        pct_dec(e.stop_pct),
        pct_dec(e.trail_pct),
    )
}

fn vbt_rule(cfg: &VbtConfig) -> String {
    let e = &cfg.exits;
    if e.qulla_exits {
        return qulla_rule(
            &format!(
                "{}% initial stop under the signal close (GTT)",
                pct_f64(e.stop_pct)
            ),
            &e.qulla,
        );
    }
    format!(
        "{}% initial stop under the signal close (GTT); the working exit is a \
         close below the 21-day EMA, sold at the next open; no target",
        pct_f64(e.stop_pct),
    )
}

/// `EXIT_RULES`, in Python's key order: `twt`, `vbt`, `swing`.
#[must_use]
pub fn exit_rules() -> [(&'static str, String); 3] {
    [
        ("twt", twt_rule(&TwtConfig::default())),
        ("vbt", vbt_rule(&VbtConfig::default())),
        ("swing", swing_rule(&SwingConfig::default().stops)),
    ]
}

/// `EXIT_RULES[sleeve]`.
#[must_use]
pub fn exit_rule(sleeve: &str) -> Option<String> {
    exit_rules()
        .into_iter()
        .find(|(k, _)| *k == sleeve)
        .map(|(_, v)| v)
}
