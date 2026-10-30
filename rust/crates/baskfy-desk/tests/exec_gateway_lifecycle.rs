//! `kite-momentum-rebalancer/tests/test_lifecycle.py` (LV6): one lifecycle per trade, the stop
//! judged at the broker, and explicit adoption. Python's books are the executor suites'
//! in-memory stores given the two reads adoption needs; here they are [`MemoryBook`], the same
//! contract ([`AdoptBook`]) over vectors. The broker's GTT list is a list of Kite-shaped dicts;
//! the gateway is a recorder that answers `GTT_PLACED`. The real stores are graded against the
//! Python desk by `desk_diff.py` (`desk_cases/exec_gateway_lifecycle.json` and the two
//! `desk_cases/exec_gateway_variants/lifecycle_*.json`).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Mutex;

use chrono::TimeZone as _;

use std::collections::HashMap;
use std::str::FromStr as _;

use async_trait::async_trait;
use baskfy_desk::gateway::{
    CancelOrder, DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder,
};
use baskfy_desk::lanes::exec_core::exit_rules::exit_rules;
use baskfy_desk::lanes::exec_core::lifecycle::*;
use baskfy_desk::lanes::twt::common::{Rec, Val, dstr, ist, jsonable_rec, quantize};
use baskfy_desk::settings::Settings;
use baskfy_desk::wire::{PyDict, PyValue, dict};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate};
use rust_decimal::Decimal;

fn now() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 28, 11, 0, 0).unwrap()
}

fn d(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

/// `gtt(gtt_id, symbol, quantity, status=, trigger=)`: Kite's GTT row.
fn gtt(id: &str, symbol: &str, quantity: i64, status: &str, trigger: &str) -> PyValue {
    PyValue::Map(dict([
        ("id", PyValue::Str(id.to_owned())),
        ("status", PyValue::Str(status.to_owned())),
        (
            "condition",
            PyValue::Map(dict([
                ("tradingsymbol", PyValue::Str(symbol.to_owned())),
                (
                    "trigger_values",
                    PyValue::List(vec![PyValue::Float(trigger.parse().unwrap())]),
                ),
            ])),
        ),
        (
            "orders",
            PyValue::List(vec![PyValue::Map(dict([
                ("transaction_type", PyValue::Str("SELL".to_owned())),
                ("quantity", PyValue::Int(quantity)),
            ]))]),
        ),
    ]))
}

fn active(id: &str, symbol: &str, quantity: i64) -> PyValue {
    gtt(id, symbol, quantity, "active", "80")
}

/// `position(sleeve, pid, symbol, qty=, entry=, stop=, gtt_id=)`.
fn position(
    sleeve: &str,
    pid: i64,
    symbol: &str,
    qty: i64,
    entry: &str,
    stop: &str,
    gtt_id: Option<&str>,
) -> Rec {
    let mut r = Rec::of([
        ("id", Val::Int(pid)),
        ("symbol", Val::Str(symbol.to_owned())),
        (
            "entry_date",
            Val::Date(NaiveDate::from_ymd_opt(2026, 9, 25).unwrap()),
        ),
        ("entry_avg", Val::Dec(d(entry))),
        ("quantity_entered", Val::Int(qty)),
        ("quantity_open", Val::Int(qty)),
        ("initial_stop", Val::Dec(d(stop))),
        ("gtt_id", Val::opt(gtt_id.map(str::to_owned), Val::Str)),
        ("simulated", Val::Bool(false)),
    ]);
    r.set(
        if sleeve == "swing" {
            "stop"
        } else {
            "stop_price"
        },
        Val::Dec(d(stop)),
    );
    r
}

fn rules() -> HashMap<&'static str, String> {
    exit_rules().into_iter().collect()
}

// --- TestRowsAndPage ------------------------------------------------------------------------

#[test]
fn test_rows_cover_every_sleeve_with_entry_quantities_risk_and_the_exact_exit_rule() {
    let positions = [
        (
            "swing",
            vec![position(
                "swing",
                1,
                "SWINGCO",
                300,
                "120",
                "110",
                Some("11"),
            )],
        ),
        (
            "twt",
            vec![position("twt", 2, "TWTCO", 100, "100", "80", Some("22"))],
        ),
        (
            "vbt",
            vec![position("vbt", 3, "VBTCO", 50, "96", "84.45", Some("33"))],
        ),
    ];
    let gtts = gtt_index(&[
        active("11", "SWINGCO", 300),
        active("22", "TWTCO", 100),
        active("33", "VBTCO", 50),
    ]);
    let rows = build_rows(&positions, Some(&gtts), &[], now());
    let by: HashMap<&str, &TradeLifecycle> = rows.iter().map(|r| (r.symbol.as_str(), r)).collect();
    let mut keys: Vec<_> = by.keys().copied().collect();
    keys.sort_unstable();
    assert_eq!(keys, vec!["SWINGCO", "TWTCO", "VBTCO"]);
    assert_eq!(by["TWTCO"].rupee_risk, d("2000.00"));
    assert_eq!(dstr(by["TWTCO"].rupee_risk), "2000.00");
    assert_eq!(by["TWTCO"].filled_qty, 100);
    assert_eq!(by["SWINGCO"].rupee_risk, d("3000.00"));
    assert_eq!(by["VBTCO"].rupee_risk, d("577.50"));
    assert_eq!(dstr(by["VBTCO"].rupee_risk), "577.50");
    assert!(rows.iter().all(|r| r.stop_state == ARMED && r.resolved()));
    let rules = rules();
    assert_eq!(by["TWTCO"].exit_rule, rules["twt"]);
    assert!(by["TWTCO"].exit_rule.contains("20%"));
    assert!(by["VBTCO"].exit_rule.contains("20-day MA"));
    assert!(by["SWINGCO"].exit_rule.contains("trails"));
}

#[test]
fn test_rows_put_unresolved_and_overdue_first_and_carry_the_reconcilers_findings() {
    let positions = [(
        "twt",
        vec![
            position("twt", 1, "GOOD", 100, "100", "80", Some("1")),
            position("twt", 2, "NAKEDCO", 100, "100", "80", None),
        ],
    )];
    let issues = [Rec::of([
        ("sleeve", Val::Str("twt".to_owned())),
        ("position_id", Val::Int(2)),
        ("symbol", Val::Str("NAKEDCO".to_owned())),
        ("kind", Val::Str("NAKED".to_owned())),
        (
            "detail",
            Val::Str("100 share(s) held with no resting stop".to_owned()),
        ),
        ("seen_at", Val::Stamp(now() - Duration::hours(1))),
    ])];
    let gtts = gtt_index(&[active("1", "GOOD", 100)]);
    let rows = build_rows(&positions, Some(&gtts), &issues, now());
    let symbols: Vec<_> = rows.iter().map(|r| r.symbol.as_str()).collect();
    assert_eq!(symbols, vec!["NAKEDCO", "GOOD"]);
    let naked = &rows[0];
    assert!(naked.stop_state == NAKED && naked.overdue && !naked.resolved());
    assert_eq!(
        naked.issues,
        vec!["NAKED: 100 share(s) held with no resting stop".to_owned()]
    );
    assert!(naked.next_action.contains("arm the stop"));
}

fn templates() -> baskfy_desk::templates::Templates {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../kite-momentum-rebalancer/app/templates");
    let mut t = baskfy_desk::templates::Templates::new(&dir, "X", false);
    baskfy_desk::lanes::register_templates(&mut t);
    t
}

#[test]
fn test_page_renders_the_rows_and_says_when_the_broker_was_not_checked() {
    let positions = [
        ("swing", Vec::new()),
        (
            "twt",
            vec![position("twt", 7, "PAGECO", 100, "100", "80", None)],
        ),
        ("vbt", Vec::new()),
    ];
    let rows = build_rows(&positions, None, &[], now());
    let view = View {
        rows,
        gtt_checked: false,
        issues: Vec::new(),
        now: now(),
        error: None,
    };
    let ctx = minijinja::Value::from_pairs(vec![
        (
            "request".to_owned(),
            minijinja::Value::from_pairs(vec![(
                "url".to_owned(),
                minijinja::Value::from_pairs(vec![(
                    "path".to_owned(),
                    minijinja::Value::from("/lifecycle"),
                )]),
            )]),
        ),
        (
            "v".to_owned(),
            baskfy_desk::lanes::twt::common::template_value(&Val::Map(view_rec(&view, true))),
        ),
    ]);
    let page = templates().render("lifecycle.html", ctx).unwrap();
    assert!(page.contains("PAGECO") && page.contains("NAKED"));
    assert!(page.contains("not checked: no Kite session"));
    let data = jsonable_rec(&view_rec(&view, false));
    assert_eq!(data.get("unresolved"), Some(&PyValue::Int(1)));
    let Some(PyValue::List(rows)) = data.get("rows") else {
        panic!("rows")
    };
    let PyValue::Map(first) = &rows[0] else {
        panic!("row")
    };
    assert_eq!(
        first.get("symbol"),
        Some(&PyValue::Str("PAGECO".to_owned()))
    );
    // `r.__dict__`: the dataclass's fields, not the `resolved` property.
    assert!(first.get("resolved").is_none());
}

// --- TestStopState --------------------------------------------------------------------------

fn one(gtt_id: Option<&str>, gtts: Option<&[PyValue]>, qty: i64) -> TradeLifecycle {
    let positions = [(
        "twt",
        vec![position("twt", 1, "X", qty, "100", "80", gtt_id)],
    )];
    let index = gtts.map(gtt_index);
    build_rows(&positions, index.as_ref(), &[], now())
        .into_iter()
        .next()
        .unwrap()
}

#[test]
fn test_stop_state_armed_when_the_broker_rests_the_open_quantity() {
    let row = one(Some("9"), Some(&[active("9", "X", 100)]), 100);
    assert_eq!((row.stop_state, row.stop_qty), (ARMED, Some(100)));
}

#[test]
fn test_stop_state_missing_when_the_broker_no_longer_lists_it() {
    assert_eq!(one(Some("9"), Some(&[]), 100).stop_state, GTT_MISSING);
    assert_eq!(
        one(
            Some("9"),
            Some(&[gtt("9", "X", 100, "cancelled", "80")]),
            100
        )
        .stop_state,
        GTT_MISSING
    );
}

#[test]
fn test_stop_state_oversized_and_undersized_name_the_residual() {
    let over = one(Some("9"), Some(&[active("9", "X", 150)]), 100);
    let under = one(Some("9"), Some(&[active("9", "X", 60)]), 100);
    assert_eq!((over.stop_state, over.stop_qty), (GTT_OVERSIZED, Some(150)));
    assert_eq!(
        (under.stop_state, under.stop_qty),
        (GTT_UNDERSIZED, Some(60))
    );
    assert!(over.next_action.contains("sells what is not there"));
}

#[test]
fn test_stop_state_triggered_but_unfilled_is_an_unresolved_position() {
    let row = one(
        Some("9"),
        Some(&[gtt("9", "X", 100, "triggered", "80")]),
        100,
    );
    assert!(row.stop_state == TRIGGERED_UNFILLED && !row.resolved());
    let module_doc = include_str!("../src/lanes/exec_core/lifecycle.rs");
    assert!(module_doc.contains("not a fill"));
    assert!(row.next_action.contains("LIMIT order"));
}

#[test]
fn test_stop_state_unverified_without_a_broker_list_and_naked_without_a_gtt() {
    assert_eq!(one(Some("9"), None, 100).stop_state, UNVERIFIED);
    assert_eq!(one(None, Some(&[]), 100).stop_state, NAKED);
    // A rehearsal's trigger rests nowhere.
    assert_eq!(one(Some("DRY-x"), Some(&[]), 100).stop_state, ARMED);
}

// --- adoption -------------------------------------------------------------------------------

/// A recording gateway: `place_gtt_stop` answers `GTT_PLACED` with id 777; any order verb is
/// a test failure ("adoption must never place a buy").
#[derive(Default)]
struct RecordingGateway {
    armed: Mutex<Vec<GttStop>>,
}

#[async_trait]
impl Gateway for RecordingGateway {
    async fn place(&self, _order: PlaceOrder) -> Result<PyDict, GatewayError> {
        panic!("adoption must never place a buy")
    }
    async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError> {
        let trigger = stop.trigger;
        self.armed.lock().unwrap().push(stop);
        Ok(dict([
            ("status", PyValue::Str("GTT_PLACED".to_owned())),
            ("gtt_id", PyValue::Int(777)),
            ("trigger", PyValue::Float(trigger)),
        ]))
    }
    async fn delete_gtt(&self, _req: DeleteGtt) -> Result<PyDict, GatewayError> {
        panic!("adoption never removes a stop")
    }
    async fn modify_gtt_quantity(&self, _req: ModifyGtt) -> Result<PyDict, GatewayError> {
        panic!("adoption never re-sizes a stop")
    }
    async fn cancel_order(&self, _req: CancelOrder) -> Result<PyDict, GatewayError> {
        panic!("adoption never cancels an order")
    }
}

/// One `lv_adoption` row: `(sleeve, symbol, quantity, gtt_id, note)`.
type AdoptionRow = (String, String, i64, Option<String>, Option<String>);

/// The executor suites' `MemoryStore`, given `instrument` (HELDCO 42, NEWCO 43) and
/// `lv_adoption`.
#[derive(Default)]
struct MemoryBook {
    positions: Vec<Rec>,
    fills: Vec<Rec>,
    adoptions: Vec<AdoptionRow>,
}

impl MemoryBook {
    fn position(&self, id: i64) -> &Rec {
        self.positions.iter().find(|p| p.int("id") == id).unwrap()
    }
}

#[async_trait]
impl AdoptBook for MemoryBook {
    async fn instrument_id(&mut self, symbol: &str) -> Result<Option<i64>, String> {
        Ok(match symbol {
            "HELDCO" => Some(42),
            "NEWCO" => Some(43),
            _ => None,
        })
    }
    async fn holds(&mut self, instrument_id: i64) -> Result<bool, String> {
        Ok(self
            .positions
            .iter()
            .any(|p| p.int("instrument_id") == instrument_id && p.int("quantity_open") > 0))
    }
    async fn adj_factor(&mut self, _i: i64, _day: NaiveDate) -> Result<Decimal, String> {
        Ok(Decimal::ONE)
    }
    async fn create_position(&mut self, mut fields: Rec) -> Result<i64, String> {
        let id = i64::try_from(self.positions.len()).unwrap() + 1;
        fields.set("id", Val::Int(id));
        fields.set("gtt_id", Val::None);
        self.positions.push(fields);
        Ok(id)
    }
    async fn add_fill(&mut self, fields: Rec) -> Result<(), String> {
        self.fills.push(fields);
        Ok(())
    }
    async fn update_position(&mut self, position_id: i64, fields: Rec) -> Result<(), String> {
        let p = self
            .positions
            .iter_mut()
            .find(|p| p.int("id") == position_id)
            .unwrap();
        p.update(&fields);
        Ok(())
    }
    async fn record_adoption(
        &mut self,
        _position_id: i64,
        a: &Adoption,
        gtt_id: Option<&str>,
    ) -> Result<(), String> {
        self.adoptions.push((
            a.sleeve.clone(),
            a.symbol.clone(),
            a.quantity,
            gtt_id.map(str::to_owned),
            a.note.clone(),
        ));
        Ok(())
    }
}

fn adoption(sleeve: &str) -> Adoption {
    Adoption {
        sleeve: sleeve.to_owned(),
        symbol: "NEWCO".to_owned(),
        quantity: 40,
        avg_cost: d("250.00"),
        gtt_id: None,
        setup: (sleeve == "swing").then(|| "FLAG".to_owned()),
        note: Some("bought by hand".to_owned()),
    }
}

fn settings() -> Settings {
    Settings::defaults().unwrap()
}

async fn run(
    book: &mut MemoryBook,
    gw: &RecordingGateway,
    a: &Adoption,
    confirm: &str,
    gtts: Option<&GttIndex>,
) -> Result<Rec, AdoptError> {
    adopt(book, gw, &settings(), a, confirm, now(), gtts).await
}

fn status_of(e: &AdoptError) -> (u16, String) {
    match e {
        AdoptError::Http(h) => (h.status, h.detail.clone()),
        other => panic!("not an HTTP refusal: {other:?}"),
    }
}

#[tokio::test]
async fn test_adopt_creates_the_position_on_the_stated_numbers_arms_a_stop_and_records_it() {
    for sleeve in ["twt", "vbt", "swing"] {
        let mut book = MemoryBook::default();
        let gw = RecordingGateway::default();
        let empty = GttIndex::new();
        let out = run(&mut book, &gw, &adoption(sleeve), "true", Some(&empty))
            .await
            .unwrap();
        let pos = book.position(out.int("position_id"));
        assert_eq!(pos.int("quantity_open"), 40);
        assert_eq!(pos.dec("entry_avg"), Some(d("250.00")));
        assert_eq!(pos.get("simulated"), Some(&Val::Bool(false)));
        let stop = stop_of(pos).unwrap();
        assert!(stop < d("250.00"), "{sleeve}");
        assert_eq!(pos.dec("initial_stop"), Some(stop));
        assert_eq!(
            pos.opt_str("gtt_id").as_deref(),
            Some("777"),
            "armed through the gateway, and linked"
        );
        let armed = gw.armed.lock().unwrap();
        assert_eq!(armed.len(), 1);
        assert_eq!(armed[0].qty, 40);
        assert_eq!(armed[0].symbol, "NEWCO");
        assert_eq!(
            book.adoptions,
            vec![(
                sleeve.to_owned(),
                "NEWCO".to_owned(),
                40,
                Some("777".to_owned()),
                Some("bought by hand".to_owned())
            )]
        );
    }
}

#[tokio::test]
async fn test_adopt_links_a_hand_armed_gtt_instead_of_arming_a_second() {
    let mut book = MemoryBook::default();
    let gw = RecordingGateway::default();
    let gtts = gtt_index(&[gtt("5150", "NEWCO", 40, "active", "230")]);
    let mut a = adoption("vbt");
    a.gtt_id = Some("5150".to_owned());
    let out = run(&mut book, &gw, &a, "true", Some(&gtts)).await.unwrap();
    let pos = book.position(out.int("position_id"));
    assert_eq!(pos.opt_str("gtt_id").as_deref(), Some("5150"));
    assert_eq!(pos.dec("stop_price"), Some(d("230")));
    assert!(gw.armed.lock().unwrap().is_empty());
}

#[tokio::test]
async fn test_adopt_without_confirm_writes_nothing() {
    let mut book = MemoryBook::default();
    let gw = RecordingGateway::default();
    let e = run(&mut book, &gw, &adoption("twt"), "", Some(&GttIndex::new()))
        .await
        .unwrap_err();
    assert_eq!(status_of(&e).0, 400);
    assert!(book.positions.is_empty() && book.adoptions.is_empty());
}

#[tokio::test]
async fn test_adopt_refuses_an_unknown_sleeve_a_held_name_and_an_unknown_symbol() {
    let mut book = MemoryBook::default();
    book.positions.push(Rec::of([
        ("id", Val::Int(99)),
        ("instrument_id", Val::Int(42)),
        ("symbol", Val::Str("HELDCO".to_owned())),
        ("quantity_open", Val::Int(10)),
    ]));
    let gw = RecordingGateway::default();
    let mut weekly = adoption("twt");
    weekly.sleeve = "weekly".to_owned();
    let mut held = adoption("twt");
    held.symbol = "HELDCO".to_owned();
    let mut unknown = adoption("twt");
    unknown.symbol = "NOSUCH".to_owned();
    for (a, code) in [(weekly, 400), (held, 409), (unknown, 404)] {
        let e = run(&mut book, &gw, &a, "true", Some(&GttIndex::new()))
            .await
            .unwrap_err();
        assert_eq!(status_of(&e).0, code, "{a:?}");
    }
    assert!(gw.armed.lock().unwrap().is_empty());
}

#[tokio::test]
async fn test_adopt_into_swing_names_its_setup() {
    let mut book = MemoryBook::default();
    let gw = RecordingGateway::default();
    let mut a = adoption("swing");
    a.setup = None;
    let e = run(&mut book, &gw, &a, "true", Some(&GttIndex::new()))
        .await
        .unwrap_err();
    let (code, detail) = status_of(&e);
    assert!(code == 400 && detail.contains("setup"));
}

#[test]
fn the_stop_is_quantized_to_the_paisa_not_the_tick() {
    // Python's `.quantize(Decimal("0.05"))` takes the exponent of 0.05 — two places — and
    // rounds half-even there; it does not snap to a five-paisa grid. 250.00 x 0.80 = 200.00;
    // 123.45 x 0.80 = 98.76 (not 98.75).
    let hundred = Decimal::from(100);
    let pct = stop_pct("twt");
    assert_eq!(
        dstr(quantize(d("123.45") * (hundred - pct) / hundred, -2)),
        "98.76"
    );
}

// --- TestTheTradeCard -----------------------------------------------------------------------

#[test]
fn test_card_rules_come_from_the_configs_and_name_the_numbers() {
    let r = rules();
    assert!(r["twt"].contains("20% initial stop") && r["twt"].contains("1/3 sold"));
    assert!(r["vbt"].contains("12% initial stop") && r["vbt"].contains("20-day MA"));
    assert!(r["swing"].contains("1/3 sold") && r["swing"].contains("10-day MA"));
    assert!(r["twt"].contains("Qullamaggie") && r["vbt"].contains("Qullamaggie"));
    for rule in r.values() {
        assert!(!rule.replace("no target", "").contains("target"));
    }
}

#[test]
fn test_card_shows_initial_stop_rupee_risk_and_the_exit_rule() {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../kite-momentum-rebalancer/app/templates");
    for page in ["twt.html", "vbt.html", "swing.html"] {
        let text = std::fs::read_to_string(dir.join(page)).unwrap();
        assert!(
            text.contains("exit_rule") && text.contains("₹ at risk"),
            "{page}"
        );
        assert!(text.to_lowercase().contains("initial"), "{page}");
    }
}

// --- TestReadonly ---------------------------------------------------------------------------

fn source() -> &'static str {
    include_str!("../src/lanes/exec_core/lifecycle.rs")
}

#[test]
fn test_readonly_the_page_has_one_write_and_it_is_adopt() {
    let routes: Vec<&str> = source()
        .lines()
        .map(str::trim)
        .filter(|l| l.starts_with(".route("))
        .collect();
    assert_eq!(
        routes,
        vec![
            ".route(\"/lifecycle\", get(lifecycle_page))",
            ".route(\"/lifecycle/data\", get(lifecycle_data))",
            ".route(\"/lifecycle/adopt\", post(lifecycle_adopt))",
        ]
    );
}

#[test]
fn test_no_order_verb_in_the_module() {
    let code: String = source()
        .lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n");
    for verb in [
        "gateway.place(",
        ".place_order(",
        ".cancel_order(",
        ".delete_gtt(",
        ".modify_gtt_quantity(",
        "kc.",
        "KiteWrites",
    ] {
        assert!(!code.contains(verb), "{verb}");
    }
}
