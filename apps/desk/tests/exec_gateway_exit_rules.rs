//! `app/exit_rules.py`'s `EXIT_RULES`, character for character. The expected sentences were
//! printed by the live Python (`python -c "import app.exit_rules as e; print(e.EXIT_RULES)"`,
//! 30 Oct 2026) from `DEFAULT_{TWT,VBT,SWING}_CONFIG`; they change only when a config does.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_desk::lanes::exec_core::exit_rules::{exit_rule, exit_rules, pct_dec, pct_f64};
use rust_decimal::Decimal;

const TWT: &str = "20% initial stop under the fill (GTT); 1/3 sold into strength between bar 3 and bar 5 after entry if green, then the stop to breakeven (or after 1R); the rest trails the 10-day MA (ADR at or above 6%) or the 20-day MA and is sold at the next open on a close below it; the GTT is the hard stop (Qullamaggie's rule)";
const VBT: &str = "12% initial stop under the signal close (GTT); 1/3 sold into strength between bar 3 and bar 5 after entry if green, then the stop to breakeven (or after 1R); the rest trails the 10-day MA (ADR at or above 6%) or the 20-day MA and is sold at the next open on a close below it; the GTT is the hard stop (Qullamaggie's rule)";
const SWING: &str = "stop under the range low, at most 10% below entry; 1/3 sold between bar 3 and 5, then the stop to breakeven (or after 1R); the rest trails the 10-day MA (ADR at or above 6%) or the 20-day MA; the GTT is the exit";

#[test]
fn every_sleeve_says_its_exit_rule_as_python_does() {
    let rules = exit_rules();
    let keys: Vec<&str> = rules.iter().map(|(k, _)| *k).collect();
    assert_eq!(keys, ["twt", "vbt", "swing"], "Python's dict order");
    assert_eq!(exit_rule("twt").unwrap(), TWT);
    assert_eq!(exit_rule("vbt").unwrap(), VBT);
    assert_eq!(exit_rule("swing").unwrap(), SWING);
    assert!(exit_rule("weekly").is_none());
}

#[test]
fn no_rule_names_a_target() {
    for (_, rule) in exit_rules() {
        assert!(
            !rule.contains("target ") && !rule.contains("profit target"),
            "{rule}"
        );
    }
}

#[test]
fn pct_reads_like_decimal_normalize() {
    assert_eq!(pct_f64(20.0), "20");
    assert_eq!(pct_f64(12.5), "12.5");
    assert_eq!(pct_f64(0.5), "0.5");
    assert_eq!(pct_f64(100.0), "100");
    assert_eq!(pct_dec(Decimal::new(200, 1)), "20");
    assert_eq!(pct_dec(Decimal::new(125, 1)), "12.5");
}
