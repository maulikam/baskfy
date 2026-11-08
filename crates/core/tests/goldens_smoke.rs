//! Every function the core's declared modules port has goldens the testkit can load. Porters
//! add their own `tests/<module>.rs` that grade the port; this file only proves the wiring.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

#[test]
fn declared_modules_have_goldens() {
    for f in [
        "baskfy_core.swing.market.exposure_tier",
        "baskfy_core.swing.opening_range.evaluate_trigger",
        "baskfy_core.swing.setups.detect_setups",
        "baskfy_core.swing.sizing.size_position",
        "baskfy_core.swing.stops.manage",
        "baskfy_core.swing.plan.build_entries",
        "baskfy_core.options.condor.exit_decision",
        "baskfy_core.options.costs.charges",
        "baskfy_core.options.execution.never_naked",
        "baskfy_core.options.execution.simulate_fill",
        "baskfy_core.options.gating.options_gates",
        "baskfy_core.options.ledger.journal_figures",
    ] {
        assert!(!baskfy_testkit::cases_for(f).unwrap().is_empty(), "{f}");
    }
}
