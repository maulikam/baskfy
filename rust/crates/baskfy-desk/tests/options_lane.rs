//! Lane **options**: the NIFTY options book's non-negotiables, ported test-first from the desk's
//! `tests/test_options_gates.py`, `test_options_safety.py` (the gateway halves),
//! `test_options_execute.py` (the static halves), `test_options_hardening.py`
//! (`is_token_error`) and `test_options_monitor.py` (the flag defaults), plus the executor's
//! sequencing rules (`baskfy_core.options.executor`: longs before shorts in, shorts before longs
//! out, never naked).
//!
//! The DB-backed halves (a confirm's four simulated fills, 410/409 refusals, abandonment, the
//! exit shorts first, the ledger's pauses) are `tests/options_confirm_db.rs`, run against a bed
//! clone (`BASKFY_RS_OPTIONS_TEST_PG`); the routes' views and refusals are graded against the
//! Python desk by `tools/parity/desk_diff.py` over `desk_cases/options.json`.
//!
//! Every order here goes to the replay Kite, which records each call and sends nothing.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;
use std::sync::Arc;

use baskfy_core::options::execution::{LegRole, never_naked};
use baskfy_core::options::gating::{Mode, Sleeve};
use baskfy_desk::clock::{Clock, FixedClock, ist};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::gateway::{GatesFn, PlaceOrder, ProductGates, TenantIds};
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use baskfy_desk::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskLayer, RiskManager};
use baskfy_desk::lanes::options::core::{
    Direction, EntryAction, Structure, advance_entry, direction_of, entry_sequence, exit_sequence,
    next_exit_leg, structure_of,
};
use baskfy_desk::lanes::options::{clock as op_clock, desk, execute, gates};
use baskfy_desk::settings::Settings;
use baskfy_desk::wire::{PyDict, PyValue};
use chrono::TimeZone;

const SLEEVES: [Sleeve; 5] = [
    Sleeve::O1m,
    Sleeve::O1w,
    Sleeve::O2,
    Sleeve::O3a,
    Sleeve::O3b,
];

const FIXTURE: &str =
    r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#;

fn defaults() -> Settings {
    Settings::defaults().unwrap()
}

/// `_set(...)`: DRY_RUN, OPTIONS_ENABLED, INTRADAY_ENABLED and **only** this sleeve's
/// execution flag; the other groups' flags are false.
fn settings(
    sleeve: Sleeve,
    dry_run_off: bool,
    options: bool,
    intraday: bool,
    execution: bool,
) -> Settings {
    let mut s = defaults();
    s.dry_run = !dry_run_off;
    s.options_enabled = options;
    s.intraday_enabled = intraday;
    s.options_o1m_execution_enabled = false;
    s.options_o1w_execution_enabled = false;
    s.options_o2_execution_enabled = false;
    s.options_o3_execution_enabled = false;
    match sleeve {
        Sleeve::O1m => s.options_o1m_execution_enabled = execution,
        Sleeve::O1w => s.options_o1w_execution_enabled = execution,
        Sleeve::O2 => s.options_o2_execution_enabled = execution,
        Sleeve::O3a | Sleeve::O3b => s.options_o3_execution_enabled = execution,
    }
    s
}

fn rows() -> Vec<(bool, bool, bool, bool)> {
    let mut out = Vec::new();
    for a in [false, true] {
        for b in [false, true] {
            for c in [false, true] {
                for d in [false, true] {
                    out.push((a, b, c, d));
                }
            }
        }
    }
    out
}

// --- OP2: the gates -------------------------------------------------------------------------

#[test]
fn paper_for_every_row_but_all_four() {
    for sleeve in SLEEVES {
        for (dry_run_off, options, intraday, execution) in rows() {
            let s = settings(sleeve, dry_run_off, options, intraday, execution);
            let all_four = dry_run_off && options && intraday && execution;
            let want = if all_four { Mode::Live } else { Mode::Paper };
            assert_eq!(gates::options_gates(&s, sleeve).mode, want, "{sleeve:?}");
            assert_eq!(gates::mode_of(&s, sleeve), want);
            assert_eq!(
                gates::product_gates(&s, sleeve).dry_run,
                !all_four,
                "{sleeve:?}"
            );
        }
    }
}

#[test]
fn another_groups_flag_never_makes_a_sleeve_live() {
    // Every switch on, but the execution flag belongs to a different group.
    for sleeve in SLEEVES {
        let mut s = settings(sleeve, true, true, true, false);
        s.options_o1m_execution_enabled = sleeve != Sleeve::O1m;
        s.options_o1w_execution_enabled = sleeve != Sleeve::O1w;
        s.options_o2_execution_enabled = sleeve != Sleeve::O2;
        s.options_o3_execution_enabled = !matches!(sleeve, Sleeve::O3a | Sleeve::O3b);
        assert_eq!(gates::mode_of(&s, sleeve), Mode::Paper, "{sleeve:?}");
        assert!(gates::product_gates(&s, sleeve).dry_run);
    }
}

#[test]
fn the_shipped_defaults_are_paper_for_every_sleeve() {
    let s = defaults();
    assert!(s.dry_run, "DRY_RUN defaults true");
    assert!(!s.options_enabled, "OPTIONS_ENABLED defaults off");
    assert!(!s.intraday_enabled, "INTRADAY_ENABLED defaults off");
    assert!(!s.options_o1m_execution_enabled);
    assert!(!s.options_o1w_execution_enabled);
    assert!(!s.options_o2_execution_enabled);
    assert!(!s.options_o3_execution_enabled);
    for sleeve in SLEEVES {
        assert_eq!(gates::options_gates(&s, sleeve).mode, Mode::Paper);
        let g = gates::product_gates(&s, sleeve);
        assert!(g.dry_run);
        assert!(
            !g.fno_carry_enabled,
            "an options leg never carries overnight"
        );
    }
}

#[test]
fn the_weekly_desk_gateway_is_untouched_by_the_options_flags() {
    let mut s = defaults();
    s.options_o1m_execution_enabled = true;
    s.options_o1w_execution_enabled = true;
    s.options_o2_execution_enabled = true;
    s.options_o3_execution_enabled = true;
    let g = ProductGates::from_settings(&s);
    assert!(!g.options_enabled);
    assert!(!g.intraday_enabled);
    assert!(g.dry_run);
}

#[test]
fn there_is_no_options_auto_execute_flag() {
    // Non-negotiable 1: exactly three named exceptions, none of them the options book's.
    let s = defaults();
    let flags = s.auto_execute_flags();
    assert_eq!(flags.len(), 3);
    for (name, on) in flags {
        assert!(!on, "{name} must default false");
        assert!(!name.starts_with("OPTIONS"), "{name}");
    }
}

#[test]
fn the_monitor_flag_defaults_off() {
    assert!(!defaults().options_monitor_enabled);
}

#[test]
fn the_frozen_lab_is_never_enabled() {
    let mut s = defaults();
    for on in [false, true] {
        s.options_enabled = on;
        assert!(!baskfy_desk::lanes::options::lab_enabled(&s));
    }
    // The worktree carries no thawed lab: the probe over an empty root finds none.
    let dir = tempfile::tempdir().unwrap();
    s.options_enabled = true;
    assert!(!gates::lab_enabled(&s, dir.path()));
}

// --- the gateway, handed the sleeve's gates (TestTheFourFlags / TestNeverOvernight) -----------

fn tenant() -> TenantIds {
    TenantIds::new(1, 1).unwrap()
}

fn clock() -> Arc<dyn Clock> {
    Arc::new(FixedClock::new(
        ist().with_ymd_and_hms(2026, 10, 27, 11, 0, 0).unwrap(),
    ))
}

struct Bed {
    dir: tempfile::TempDir,
    kite: Arc<ReplayKite>,
}

impl Bed {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("kite.json"), FIXTURE).unwrap();
        std::fs::create_dir(dir.path().join("instruments")).unwrap();
        let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
        Self { dir, kite }
    }

    async fn gateway(&self, gates: GatesFn) -> OrderGateway {
        let risk: Arc<dyn RiskLayer> = Arc::new(
            RiskManager::new(RiskConfig::default(), clock(), RiskBacking::Memory)
                .await
                .unwrap(),
        );
        OrderGateway::new(
            self.kite.clone(),
            risk,
            GatewayOptions {
                gates,
                journal_path: self.dir.path().join("options_orders_journal.jsonl"),
                stop_band: None,
                coverage: None,
                clock: clock(),
                ids: Arc::new(HarnessIds::new("options-lane-test")),
                limits: None,
            },
        )
        .unwrap()
    }

    fn sent(&self) -> usize {
        self.kite.write_calls().len()
    }
}

fn leg(symbol: &str, product: &str) -> PlaceOrder {
    PlaceOrder {
        symbol: symbol.to_owned(),
        qty: 65,
        side: "BUY".to_owned(),
        product: product.to_owned(),
        order_type: "LIMIT".to_owned(),
        price: Some(10.0),
        exchange: "NFO".to_owned(),
        client_id: Some(format!("plan:{symbol}")),
        gross_exposure: Some(650.0),
        tenant: Some(tenant()),
        plan_tenant: Some(tenant()),
        ..PlaceOrder::default()
    }
}

fn status(out: &PyDict) -> String {
    match out.get("status") {
        Some(PyValue::Str(s)) => s.clone(),
        _ => String::new(),
    }
}

fn error(out: &PyDict) -> String {
    match out.get("error") {
        Some(PyValue::Str(s)) => s.clone(),
        Some(PyValue::None) | None => String::new(),
        Some(other) => format!("{other:?}"),
    }
}

fn fixed(g: ProductGates) -> GatesFn {
    Arc::new(move || g)
}

fn sleeve_gates(s: Settings, sleeve: Sleeve) -> GatesFn {
    Arc::new(move || gates::product_gates(&s, sleeve))
}

#[tokio::test]
async fn a_paper_gateway_simulates_an_nfo_mis_leg_and_calls_no_broker() {
    let bed = Bed::new();
    let gw = bed.gateway(sleeve_gates(defaults(), Sleeve::O1m)).await;
    let out = gw.place(&leg("NIFTY2692223700CE", "MIS")).await.unwrap();
    assert_eq!(status(&out), "DRY_RUN", "{out:?}");
    assert_eq!(bed.sent(), 0);
}

#[tokio::test]
async fn fifteen_of_sixteen_never_reach_the_broker() {
    for sleeve in SLEEVES {
        for (dry_run_off, options, intraday, execution) in rows() {
            let s = settings(sleeve, dry_run_off, options, intraday, execution);
            if gates::mode_of(&s, sleeve) == Mode::Live {
                continue; // the one LIVE row: execute_entry refuses it (LIVE_NOT_BUILT)
            }
            let bed = Bed::new();
            let gw = bed.gateway(sleeve_gates(s, sleeve)).await;
            let out = gw.place(&leg("NIFTY26OCT23000PE", "MIS")).await.unwrap();
            assert_ne!(status(&out), "PLACED", "{sleeve:?} {out:?}");
            assert_eq!(
                bed.sent(),
                0,
                "{sleeve:?} dry_run_off={dry_run_off} options={options} intraday={intraday} execution={execution}"
            );
        }
    }
}

#[tokio::test]
async fn a_half_flipped_sleeve_is_refused_by_the_gateway_leg_by_leg() {
    for sleeve in SLEEVES {
        // DRY_RUN off, INTRADAY on, the sleeve's flag on — but OPTIONS_ENABLED off.
        let s = settings(sleeve, true, false, true, true);
        let bed = Bed::new();
        let gw = bed.gateway(sleeve_gates(s, sleeve)).await;
        for (strike, kind) in [
            ("23400", "CE"),
            ("23300", "CE"),
            ("22700", "PE"),
            ("22600", "PE"),
        ] {
            let out = gw
                .place(&leg(&format!("NIFTY26OCT{strike}{kind}"), "MIS"))
                .await
                .unwrap();
            let st = status(&out);
            assert!(st != "DRY_RUN" && st != "PLACED", "{sleeve:?} {out:?}");
            assert!(error(&out).contains("OPTIONS_ENABLED"), "{out:?}");
        }
        assert_eq!(bed.sent(), 0);
    }
}

#[tokio::test]
async fn a_carry_product_on_an_option_is_refused_before_the_broker() {
    let all_on = ProductGates {
        dry_run: false,
        intraday_enabled: true,
        options_enabled: true,
        fno_carry_enabled: false,
    };
    for product in ["NRML", "CNC"] {
        let bed = Bed::new();
        let gw = bed.gateway(fixed(all_on)).await;
        let out = gw.place(&leg("NIFTY26OCT23000PE", product)).await.unwrap();
        let st = status(&out);
        assert!(st != "DRY_RUN" && st != "PLACED", "{product}: {out:?}");
        assert_eq!(bed.sent(), 0, "{product}");
    }
}

#[tokio::test]
async fn mis_is_the_one_product_the_same_gateway_would_send() {
    // The control: the refusal above is the product, not a broken gateway. The broker is the
    // replay Kite, which records the call and sends nothing.
    let all_on = ProductGates {
        dry_run: false,
        intraday_enabled: true,
        options_enabled: true,
        fno_carry_enabled: false,
    };
    let bed = Bed::new();
    let gw = bed.gateway(fixed(all_on)).await;
    let _ = gw.place(&leg("NIFTY26OCT23000PE", "MIS")).await.unwrap();
    assert_eq!(bed.sent(), 1);
}

// --- OP10: the static halves of test_options_execute.py ---------------------------------------

#[test]
fn client_ids_are_plan_colon_symbol_with_close_and_retry_suffixes() {
    assert_eq!(
        execute::client_id("P1", "NIFTY26OCT23000PE", false, 1),
        "P1:NIFTY26OCT23000PE"
    );
    assert_eq!(
        execute::client_id("P1", "NIFTY26OCT23000PE", true, 1),
        "P1:NIFTY26OCT23000PE:CLOSE"
    );
    assert_eq!(
        execute::client_id("P1", "NIFTY26OCT23000PE", false, 2),
        "P1:NIFTY26OCT23000PE:R2"
    );
    assert_eq!(
        execute::client_id("P1", "NIFTY26OCT23000PE", true, 3),
        "P1:NIFTY26OCT23000PE:CLOSE:R3"
    );
}

#[test]
fn the_confirm_sentences_are_05s_verbatim() {
    assert_eq!(
        desk::sentence_for("O1M"),
        "Confirming this plan also authorises its rule-driven exits — profit at ½C, stop at 1.5C, \
         a short-strike touch, and the mandatory flat at 14:30 — without a second click."
    );
    assert_eq!(desk::sentence_for("O1W"), desk::sentence_for("O1M"));
    assert_eq!(
        desk::sentence_for("O2"),
        "Confirming this plan also authorises its exits — the 30 % stop, the 60 % target, a close \
         back inside the opening range, the 45-minute time stop, and the flat at 15:00 — without a \
         second click."
    );
    assert_eq!(
        desk::sentence_for("O3A"),
        "Confirming this plan also authorises its exits — 80 % of width, half the debit lost, the \
         setup's invalidation, and the flat at 14:45 — without a second click."
    );
    assert_eq!(desk::sentence_for("O3B"), desk::sentence_for("O3A"));
}

#[test]
fn every_leg_is_nfo_mis_limit_and_no_path_places_a_gtt() {
    // The lane's source sends only NFO / MIS / LIMIT and never calls place_gtt_stop.
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lanes/options");
    let mut sources = String::new();
    for entry in std::fs::read_dir(&root).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_some_and(|e| e == "rs") {
            sources.push_str(&std::fs::read_to_string(path).unwrap());
        }
    }
    sources.push_str(&std::fs::read_to_string(root.with_extension("rs")).unwrap());
    assert!(
        !sources.contains("place_gtt_stop"),
        "an options leg never gets a GTT"
    );
    assert!(!sources.contains("\"NRML\""), "no carry product");
    assert!(!sources.contains("\"CNC\""), "no delivery product");
    let exec = std::fs::read_to_string(root.join("execute.rs")).unwrap();
    assert!(exec.contains("\"NFO\""));
    assert!(exec.contains("\"MIS\""));
    assert!(exec.contains("\"LIMIT\""));
    assert!(!exec.contains("\"MARKET\""));
}

#[test]
fn the_routes_are_exactly_two_posts_and_no_get_executes() {
    let src = std::fs::read_to_string(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lanes/options.rs"),
    )
    .unwrap();
    let posts: Vec<&str> = src.lines().filter(|l| l.contains("post(")).collect();
    assert_eq!(posts.len(), 2, "{posts:?}");
    assert!(
        posts
            .iter()
            .any(|l| l.contains("\"/nifty-options/execute\""))
    );
    assert!(posts.iter().any(|l| l.contains("\"/nifty-options/close\"")));
    for l in src.lines().filter(|l| l.contains("get(")) {
        assert!(!l.contains("execute") && !l.contains("close"), "{l}");
    }
}

// --- the executor's sequencing (docs/options/04 §7) -------------------------------------------

#[test]
fn a_condor_enters_wings_first_and_exits_shorts_first() {
    let condor = [
        LegRole::ShortCall,
        LegRole::LongCall,
        LegRole::ShortPut,
        LegRole::LongPut,
    ];
    let entry = entry_sequence(&condor).unwrap();
    let first_short = entry
        .iter()
        .position(|r| matches!(r, LegRole::ShortCall | LegRole::ShortPut))
        .unwrap();
    assert!(
        entry[..first_short].contains(&LegRole::LongCall)
            && entry[..first_short].contains(&LegRole::LongPut),
        "{entry:?}"
    );
    let exit = exit_sequence(condor);
    let first_long = exit
        .iter()
        .position(|r| matches!(r, LegRole::LongCall | LegRole::LongPut))
        .unwrap();
    assert!(
        exit[..first_long].contains(&LegRole::ShortCall)
            && exit[..first_long].contains(&LegRole::ShortPut),
        "{exit:?}"
    );
}

#[test]
fn a_naked_short_is_refused() {
    assert!(entry_sequence(&[LegRole::ShortCall]).is_err());
    assert!(entry_sequence(&[LegRole::ShortPut, LegRole::LongCall]).is_err());
    assert!(entry_sequence(&[LegRole::LongPut, LegRole::LongPut]).is_err());
    assert!(entry_sequence(&[LegRole::LongCall]).is_ok());
}

#[test]
fn a_thin_long_abandons_and_no_short_is_ever_sent() {
    let seq = entry_sequence(&[LegRole::ShortCall, LegRole::LongCall]).unwrap();
    assert_eq!(seq[0], LegRole::LongCall);
    // The long filled 30 of 65: the short is never offered; the 30 are closed.
    match advance_entry(&seq, 65, &[30]).unwrap() {
        EntryAction::Abandon(close) => assert_eq!(close, vec![(LegRole::LongCall, 30)]),
        other => panic!("{other:?}"),
    }
    // Nothing filled: nothing to close, still abandoned.
    match advance_entry(&seq, 65, &[0]).unwrap() {
        EntryAction::Abandon(close) => assert!(close.is_empty()),
        other => panic!("{other:?}"),
    }
    assert_eq!(
        advance_entry(&seq, 65, &[65]).unwrap(),
        EntryAction::Send(LegRole::ShortCall)
    );
    assert_eq!(
        advance_entry(&seq, 65, &[65, 65]).unwrap(),
        EntryAction::Open
    );
    assert!(advance_entry(&seq, 65, &[66]).is_err());
}

#[test]
fn a_long_is_never_offered_while_its_short_is_open() {
    let open = vec![(LegRole::LongCall, 65), (LegRole::ShortCall, 65)];
    assert_eq!(next_exit_leg(&open), Some(LegRole::ShortCall));
    let open = vec![(LegRole::LongCall, 65), (LegRole::ShortCall, 0)];
    assert_eq!(next_exit_leg(&open), Some(LegRole::LongCall));
    assert_eq!(next_exit_leg(&vec![(LegRole::LongPut, 0)]), None);
    let mut book = BTreeMap::new();
    book.insert(LegRole::LongCall, 65);
    book.insert(LegRole::ShortCall, 65);
    assert!(never_naked(&book));
    book.insert(LegRole::ShortCall, 130);
    assert!(!never_naked(&book));
}

#[test]
fn every_structure_parses_from_its_python_name() {
    for name in ["IRON_CONDOR", "LONG_OPTION", "DEBIT_SPREAD"] {
        assert_eq!(structure_of(name).map(Structure::value), Some(name));
    }
    assert!(structure_of("SHORT_STRANGLE").is_none());
    assert_eq!(direction_of("UP"), Some(Direction::Up));
    assert!(direction_of("up").is_none());
}

// --- B10: token death (test_options_hardening.py) ---------------------------------------------

#[test]
fn only_kites_token_exception_is_a_dead_token() {
    assert!(op_clock::is_token_error("TokenException"));
    assert!(!op_clock::is_token_error("RuntimeError"));
    assert!(!op_clock::is_token_error("NetworkException"));
}
