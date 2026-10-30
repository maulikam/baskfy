//! Port of `tests/test_options_gating.py`: `options_gates()` is `PAPER` for every combination
//! but all-four-true, for every sleeve (`docs/options/02` Track B).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::Path;

use baskfy_core::options::config::{Mode, Sleeve, SleeveGroup, group_of};
use baskfy_core::options::gating::{
    CEILING_ENV, DESK_SWITCH_ENV, EXECUTION_FLAG_ENV, OPERATIONAL_FLAG_ENV, OptionsFlags,
    options_gates,
};

fn rows() -> Vec<(bool, bool, bool, bool)> {
    let b = [false, true];
    let mut out = Vec::new();
    for a in b {
        for o in b {
            for i in b {
                for e in b {
                    out.push((a, o, i, e));
                }
            }
        }
    }
    out
}

fn set_execution(flags: &mut OptionsFlags, group: SleeveGroup, on: bool) {
    match group {
        SleeveGroup::O1m => flags.o1m_execution_enabled = on,
        SleeveGroup::O1w => flags.o1w_execution_enabled = on,
        SleeveGroup::O2 => flags.o2_execution_enabled = on,
        SleeveGroup::O3 => flags.o3_execution_enabled = on,
    }
}

fn flags(
    sleeve: Sleeve,
    dry_run_off: bool,
    options: bool,
    intraday: bool,
    execution: bool,
) -> OptionsFlags {
    let mut f = OptionsFlags {
        dry_run: !dry_run_off,
        options_enabled: options,
        intraday_enabled: intraday,
        ..OptionsFlags::default()
    };
    set_execution(&mut f, group_of(sleeve), execution);
    f
}

const GROUPS: [SleeveGroup; 4] = [
    SleeveGroup::O1m,
    SleeveGroup::O1w,
    SleeveGroup::O2,
    SleeveGroup::O3,
];

#[test]
fn the_table_has_sixteen_rows_and_five_sleeves() {
    assert_eq!(rows().len(), 16);
    assert_eq!(Sleeve::ALL.len(), 5);
}

#[test]
fn live_only_when_all_four_are_on() {
    for sleeve in Sleeve::ALL {
        for (dry_run_off, options, intraday, execution) in rows() {
            let gate = options_gates(
                sleeve,
                &flags(sleeve, dry_run_off, options, intraday, execution),
            );
            let all_four = dry_run_off && options && intraday && execution;
            let want = if all_four { Mode::Live } else { Mode::Paper };
            assert_eq!(gate.mode, want);
            assert_eq!(
                gate.dry_run, !all_four,
                "a PAPER gate must hand the gateway dry_run=True"
            );
            assert_eq!(gate.simulated(), !all_four);
            assert_eq!(gate.sleeve, sleeve);
        }
    }
}

#[test]
fn every_other_sleeves_flag_on_never_makes_this_one_live() {
    for sleeve in Sleeve::ALL {
        let mut f = OptionsFlags {
            dry_run: false,
            options_enabled: true,
            intraday_enabled: true,
            ..OptionsFlags::default()
        };
        for g in GROUPS {
            if g != group_of(sleeve) {
                set_execution(&mut f, g, true);
            }
        }
        assert_eq!(options_gates(sleeve, &f).mode, Mode::Paper);
    }
}

#[test]
fn o3a_and_o3b_share_the_o3_flag() {
    let f = OptionsFlags {
        dry_run: false,
        options_enabled: true,
        intraday_enabled: true,
        o3_execution_enabled: true,
        ..OptionsFlags::default()
    };
    assert_eq!(options_gates(Sleeve::O3a, &f).mode, Mode::Live);
    assert_eq!(options_gates(Sleeve::O3b, &f).mode, Mode::Live);
}

#[test]
fn the_defaults_are_paper_for_every_sleeve() {
    for sleeve in Sleeve::ALL {
        assert_eq!(
            options_gates(sleeve, &OptionsFlags::default()).mode,
            Mode::Paper
        );
    }
}

#[test]
fn a_half_flipped_sleeve_hands_the_gateway_the_real_desk_switches() {
    for sleeve in Sleeve::ALL {
        for options in [false, true] {
            for intraday in [false, true] {
                let gate = options_gates(sleeve, &flags(sleeve, false, options, intraday, true));
                assert_eq!(gate.mode, Mode::Paper);
                assert_eq!(
                    (gate.options_enabled, gate.intraday_enabled),
                    (options, intraday)
                );
            }
        }
    }
}

#[test]
fn with_the_execution_flag_off_paper_runs_the_real_gateway_path() {
    for sleeve in Sleeve::ALL {
        let gate = options_gates(sleeve, &OptionsFlags::default());
        assert_eq!(
            (gate.dry_run, gate.options_enabled, gate.intraday_enabled),
            (true, true, true)
        );
    }
}

#[test]
fn the_flag_names_are_the_documents() {
    let doc =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../docs/options/02-scope-and-gating.md");
    let scope = std::fs::read_to_string(doc).unwrap();
    for (_, name) in EXECUTION_FLAG_ENV {
        assert!(scope.contains(&format!("`{name}`")), "{name}");
    }
    for name in OPERATIONAL_FLAG_ENV.iter().chain(DESK_SWITCH_ENV.iter()) {
        assert!(scope.contains(&format!("`{name}`")), "{name}");
    }
    for (_, name) in CEILING_ENV {
        assert!(scope.contains(&format!("`{name}`")), "{name}");
    }
    assert_eq!(EXECUTION_FLAG_ENV.len() + OPERATIONAL_FLAG_ENV.len() + 2, 9);
}

#[test]
fn there_is_no_auto_execute_input() {
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/options/gating.rs");
    let source = std::fs::read_to_string(src).unwrap();
    let code: Vec<&str> = source.lines().filter(|l| !l.contains("PACK.3")).collect();
    let code = code.join("\n").to_uppercase().replace("AUTO-EXECUTE", "");
    assert!(!code.contains("AUTO_EXECUTE"));
    let fields = serde_json::to_value(OptionsFlags::default()).unwrap();
    assert!(
        fields
            .as_object()
            .unwrap()
            .keys()
            .all(|k| !k.contains("auto"))
    );
}
