//! `tests/test_swing_track_c.py` (SW10): the desk half of the swing book's Track-B and Track-C
//! claims as theorems, every test under its Python name.
//!
//! Two differences of means, not of claim, recorded in DECISIONS-RS R8.15.1:
//!
//! * Python drives the Track-B buy / sell / raise / re-arm **through the routes** over its
//!   sqlite scenario. The Rust SQLite binder refuses `Decimal` writes by design (R8.2), so here
//!   the same four confirms run through `execute_line` / `rearm_gtt` over the in-memory store and
//!   the real gateway with the spy; the route-level run is graded on Postgres by
//!   `tools/parity/desk_cases/swing_exec.json` (the same four kinds through `/swing/execute` and
//!   `/swing/rearm`, every journal line and the replay Kite's order/GTT log EQUAL to Python's,
//!   and the log empty). `test_after_a_full_morning_every_sw_row_belongs_to_the_sole_user` runs
//!   the real `PgSwingStore` on a bed clone named by `BASKFY_RS_SWING_TEST_PG` (Python's
//!   `requires_db` shape, as `tests/options_confirm_db.rs` does).
//! * The seeded property loops draw from SplitMix64 seeded with Python's `SEED`, not from
//!   CPython's Mersenne Twister, so the 500 cases are other cases of the same distributions; the
//!   case count, the bounds and every assertion (and the "the loop was not lopsided" floors)
//!   are Python's.

use super::py::python_tests;
use super::real::{Rig, exec, fill_val, int, rearm, st};
use super::*;

const SEED: u64 = 20_260_902;
const CASES: usize = 500;

/// Every journal event the gateway can write on a path that did NOT reach a broker.
const DRY_EVENTS: [&str; 5] = [
    "dry_run",
    "gtt_dry_run",
    "gtt_dry_run_delete",
    "gtt_band_warning",
    "gtt_risk_note",
];
/// The events that mean a broker answered. None may ever appear.
const BROKER_EVENTS: [&str; 7] = [
    "placed",
    "gtt_placed",
    "gtt_deleted",
    "rejected",
    "error",
    "gtt_error",
    "gtt_delete_error",
];

fn why(what: &str) -> String {
    format!("{what} (seed {SEED})")
}

/// SplitMix64 with `random.Random`'s three calls the suite makes.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// `randint(a, b)`, both ends included.
    fn randint(&mut self, a: i64, b: i64) -> i64 {
        let span = u64::try_from(b - a + 1).unwrap();
        a + i64::try_from(self.next() % span).unwrap()
    }

    /// `random()`.
    #[allow(clippy::cast_precision_loss)]
    fn random(&mut self) -> f64 {
        (self.next() >> 11) as f64 / (1u64 << 53) as f64
    }

    /// `choice(seq)`.
    fn choice<T: Clone>(&mut self, items: &[T]) -> T {
        let n = i64::try_from(items.len()).unwrap();
        items[usize::try_from(self.randint(0, n - 1)).unwrap()].clone()
    }
}

/// `Decimal.quantize(Decimal("0.05"))`: the exponent of 0.05 — two places, half-even.
fn q05(v: Decimal) -> Decimal {
    v.round_dp_with_strategy(2, rust_decimal::RoundingStrategy::MidpointNearestEven)
}

fn dd(i: i64) -> Decimal {
    Decimal::from(i)
}

fn code_only(source: &str) -> String {
    source
        .split("#[cfg(test)]")
        .next()
        .unwrap_or_default()
        .lines()
        .map(|l| l.split_once("//").map_or(l, |(code, _)| code))
        .collect::<Vec<_>>()
        .join("\n")
}

const EXECUTE_RS: &str = include_str!("../execute.rs");
const STORE_RS: &str = include_str!("../../store.rs");
const DESK_RS: &str = include_str!("../../desk.rs");
const RUNNER_RS: &str = include_str!("../../swing_monitor/runner.rs");
const BREAKOUT_RS: &str = include_str!("../../breakout.rs");
const MONITOR_RS: &str = include_str!("../../swing_monitor.rs");

// =======================================================================================
// Track B: with the flag off, nothing from a confirm reaches a non-dry-run branch
// =======================================================================================

async fn every_gateway_call_ran_dry(dry_run: bool) {
    let mut settings = paper();
    settings.swing_execution_enabled = false;
    settings.dry_run = dry_run;
    assert!(x::swing_gates(&settings).dry_run);
    let spy = Rig::new(&settings).await;
    let mut store = MemoryStore::new();
    // The held position rests on an integer id, so the sell's cancel goes through the gateway.
    let held = store.add_position_with(|p| {
        "HELD".clone_into(&mut p.symbol);
        p.instrument_id = 31;
        p.gtt_id = Some("4242".to_owned());
        p.quantity_entered = 300;
        p.quantity_open = 300;
    });
    let naked = store.add_position_with(|p| {
        "NAKED".clone_into(&mut p.symbol);
        p.instrument_id = 32;
        p.gtt_id = None;
        p.gtt_trigger = None;
        p.gtt_armed_at = None;
    });
    let plan = store.add_plan();
    let trigger_line = store.add_line(&plan);
    let sell = store.add_line_with(&plan, |l| {
        "SELL_AT_OPEN".clone_into(&mut l.kind);
        "HELD".clone_into(&mut l.symbol);
        l.instrument_id = 31;
        l.quantity = 100;
        l.trigger = None;
        l.stop = None;
        l.note = Some("PARTIAL_INTO_STRENGTH".to_owned());
    });
    let raise = store.add_line_with(&plan, |l| {
        "RAISE_GTT_STOP".clone_into(&mut l.kind);
        "HELD".clone_into(&mut l.symbol);
        l.instrument_id = 31;
        l.quantity = 0;
        l.trigger = None;
        l.stop = Some(d("100.00"));
    });
    let buy = exec(&mut store, &spy, &plan, trigger_line, None)
        .await
        .unwrap();
    let sold = exec(&mut store, &spy, &plan, sell, Some(d("110")))
        .await
        .unwrap();
    let raised = exec(&mut store, &spy, &plan, raise, Some(d("110")))
        .await
        .unwrap();
    let armed = rearm(&mut store, &spy, &settings, naked, "true", None)
        .await
        .unwrap();
    for outcome in [&buy, &sold, &raised, &armed] {
        assert_eq!(outcome.status, "SIMULATED", "{outcome:?}");
        assert!(outcome.simulated, "{outcome:?}");
    }
    assert_eq!(st(buy.order.as_ref().unwrap()), "DRY_RUN");
    assert_eq!(st(buy.gtt.as_ref().unwrap()), "DRY_RUN_GTT");
    let methods = spy.methods();
    assert_eq!(
        methods.iter().filter(|m| *m == "place").count(),
        2,
        "{methods:?}"
    );
    assert_eq!(
        methods.iter().filter(|m| *m == "place_gtt_stop").count(),
        4,
        "{methods:?}"
    );
    // The sell cancels 4242 through the gateway; by the raise the resting id is the
    // simulated `DRY-…` the sell armed, recorded as cancelled without a call.
    assert_eq!(methods.iter().filter(|m| *m == "delete_gtt").count(), 1);
    let seen = spy.seen.lock().unwrap().clone();
    assert!(
        seen.iter().all(|(_, dry)| *dry),
        "{}",
        why(&format!("a call ran live: {seen:?}"))
    );
    let events = spy.events();
    assert!(!events.is_empty(), "the gateway journalled nothing");
    assert!(
        events.iter().all(|e| DRY_EVENTS.contains(&e.as_str())),
        "{events:?}"
    );
    assert!(!events.iter().any(|e| BROKER_EVENTS.contains(&e.as_str())));
    for pid in [buy.position_id.unwrap(), held, naked] {
        assert!(store.book.positions[&pid].simulated);
    }
    assert!(
        store
            .book
            .fills
            .iter()
            .all(|f| fill_val(f, "simulated") == &Val::Bool(true))
    );
    assert_eq!(spy.write_calls(), 0);
}

#[tokio::test]
async fn test_every_gateway_call_through_the_route_ran_dry() {
    // `@pytest.mark.parametrize("dry_run", [True, False])`.
    every_gateway_call_ran_dry(true).await;
    every_gateway_call_ran_dry(false).await;
}

#[test]
fn test_the_gate_truth_table_has_exactly_one_live_cell() {
    let mut table = Vec::new();
    for flag in [false, true] {
        for dry in [true, false] {
            let mut s = paper();
            s.swing_execution_enabled = flag;
            s.dry_run = dry;
            table.push(((flag, dry), x::swing_gates(&s).dry_run));
        }
    }
    assert_eq!(
        table,
        [
            ((false, true), true),
            ((false, false), true),
            ((true, true), true),
            ((true, false), false)
        ]
    );
}

#[tokio::test]
async fn test_the_spy_is_not_blind_the_live_cell_is_stopped_by_the_fake() {
    let spy = Rig::new(&live()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = exec(&mut store, &spy, &plan, line, Some(d("100.00")))
        .await
        .unwrap();
    assert!(out.status == "REJECTED" && !out.simulated, "{out:?}");
    assert!(out.position_id.is_none() && out.gtt.is_none());
    assert_eq!(*spy.seen.lock().unwrap(), [("place".to_owned(), false)]);
    assert!(
        spy.events().iter().any(|e| e == "error"),
        "the fake's refusal, journalled"
    );
    assert!(store.open_position_for(11).await.unwrap().is_none());
    assert_eq!(store.line_state(line), "REJECTED");
}

#[test]
fn test_the_route_builds_no_gateway_at_import_and_resolves_the_flag_per_order() {
    // Nothing in Rust runs at import; the desk builds the swing gateway inside the request
    // (`swing_gateway(&state)`), handing it the FUNCTION `swing_gates`, which the sleeve
    // gateway calls at the moment of each order with the settings as they stand then.
    let code = code_only(DESK_RS);
    let start = code.find("pub async fn swing_gateway(").unwrap();
    let body = &code[start..start + code[start..].find("\n}\n").unwrap()];
    assert!(
        body.contains("swing_gates,"),
        "the gates are handed as a callable"
    );
    assert!(
        !body.contains("swing_gates(&"),
        "never as a value read once"
    );
    assert!(!code.contains("static SWING_GATEWAY") && !code.contains("OnceLock<"));
    // `twt::sleeve_gateway` (the derived gateway) reads the settings per order.
    let twt = include_str!("../../../twt.rs");
    let start = twt.find("fn sleeve_gateway(").unwrap();
    let body = &twt[start..start + twt[start..].find("\n}\n").unwrap()];
    assert!(
        body.contains("settings_snapshot()") || body.contains("settings.read()"),
        "{body}"
    );
}

// =======================================================================================
// Track C §3: the monitor's strategy has no place call
// =======================================================================================

const PLACING: [&str; 7] = [
    r"\bplace\b",
    r"place_order",
    r"place_gtt",
    r"\.place\(",
    r"delete_gtt",
    r"OrderGateway",
    r"\border\b",
];
const STRATEGY_ONLY: [&str; 4] = [r"\bself\.gw\b", r"\bkc\.", r"kiteconnect", r"kite_client"];

/// The Rust strategy keeps its watchlist's token sequence in a field the porter named `order`
/// (`order: Vec<i64>`). That is the one meaning of the word the scan may meet; every hit must be
/// that sequence (DECISIONS-RS R8.15.1 asks the owning lane to rename it, after which this list
/// matches nothing and the scan is Python's exactly).
fn is_token_sequence(line: &str) -> bool {
    let l = line.trim();
    l == "order: Vec<i64>,"
        || l == "order,"
        || l.starts_with("let order: Vec<i64> =")
        || l.starts_with("let state = order.iter()")
        || l.starts_with("self.order.clone()")
        || l.starts_with("self.order.push(")
        || l.starts_with("self.order.retain(")
}

fn hits(code: &str, pattern: &str) -> Vec<String> {
    let re = regex::Regex::new(pattern).unwrap();
    code.lines()
        .filter(|l| re.is_match(l))
        .map(str::to_owned)
        .collect()
}

#[test]
fn test_the_strategy_code_never_names() {
    let code = code_only(BREAKOUT_RS);
    for pattern in PLACING.iter().chain(STRATEGY_ONLY.iter()) {
        let found = hits(&code, pattern);
        if *pattern == r"\border\b" {
            assert!(
                found.iter().all(|l| is_token_sequence(l)),
                "breakout.rs names {pattern}: {found:?}"
            );
        } else {
            assert!(found.is_empty(), "breakout.rs names {pattern}: {found:?}");
        }
    }
}

#[test]
fn test_the_runner_code_never_names() {
    for (name, source) in [
        ("swing_monitor/runner.rs", RUNNER_RS),
        ("swing_monitor.rs", MONITOR_RS),
    ] {
        let code = code_only(source);
        for pattern in PLACING {
            let found = hits(&code, pattern);
            assert!(found.is_empty(), "{name} names {pattern}: {found:?}");
        }
    }
}

#[test]
fn test_the_runner_hands_the_strategy_no_gateway_and_builds_nothing_with_the_flag_off() {
    use crate::lanes::swing::swing_monitor::runner::build_monitor;
    let code = code_only(RUNNER_RS);
    let start = code.find("pub fn build_monitor(").unwrap();
    let signature = &code[start..start + code[start..].find('{').unwrap()];
    assert!(
        !signature.contains("Gateway"),
        "build_monitor takes no gateway: {signature}"
    );
    let day = NaiveDate::from_ymd_opt(2026, 9, 2).unwrap();
    let built = build_monitor(
        false,
        Vec::new(),
        day,
        baskfy_core::swing::setups::SwingConfig::default(),
        None,
    )
    .unwrap();
    assert!(built.is_none());
}

#[test]
fn test_generate_targets_is_empty_at_runtime() {
    use crate::lanes::swing::breakout::SwingBreakout;
    let day = NaiveDate::from_ymd_opt(2026, 9, 2).unwrap();
    let strategy = SwingBreakout::new(
        Vec::new(),
        day,
        None,
        baskfy_core::swing::setups::SwingConfig::default(),
    )
    .unwrap();
    assert!(strategy.generate_targets().is_empty());
    // `strategy.gw is None`: the type has no field that could carry a gateway.
    assert!(!code_only(BREAKOUT_RS).contains("Gateway"));
}

#[test]
fn test_the_scan_is_over_code_not_prose() {
    // The strategy's doc comment says the word ("never places"); the scan must not read it.
    let place = regex::Regex::new(r"\bplace").unwrap();
    assert!(
        place.is_match(BREAKOUT_RS),
        "the strategy's doc comment says the word"
    );
    assert!(!place.is_match(&code_only(BREAKOUT_RS)));
}

// =======================================================================================
// Track C §6: every sw_ write in the desk carries the sole user id
// =======================================================================================

/// The one statement allowed to address a row without `user_id`: the row was inserted with the
/// store's own `user_id` just before, and this UPDATE addresses it by the id that INSERT
/// returned.
const WHITELISTED_UPDATES: [(&str, &str); 1] = [(
    "swing_monitor.raise_signal",
    "UPDATE {SCHEMA}.sw_signal SET plan_line_id = ? WHERE id = ?",
)];

/// (where, the statement with whitespace collapsed, the enclosing function's code) for every
/// `INSERT INTO` / `UPDATE` string literal on an `sw_` table: a literal `{SCHEMA}.sw_x`, or
/// `{}` filled by `self.t("sw_x")`.
fn sql_statements(module: &str, source: &str) -> Vec<(String, String, String)> {
    let code = code_only(source);
    let starts = regex::Regex::new(r#""(INSERT\s+INTO|UPDATE)\s"#).unwrap();
    let fn_re = regex::Regex::new(r"fn ([a-z_0-9]+)\(").unwrap();
    let table_arg_re = regex::Regex::new(r#"t\("(sw_[a-z_]+)"\)"#).unwrap();
    let table_re = regex::Regex::new(r"(?i)^(?:INSERT\s+INTO|UPDATE)\s+(\S+)").unwrap();
    let mut out = Vec::new();
    for m in starts.find_iter(&code) {
        let literal_start = m.start() + 1;
        let mut end = literal_start;
        let bytes = code.as_bytes();
        while end < bytes.len() && !(bytes[end] == b'"' && bytes[end - 1] != b'\\') {
            end += 1;
        }
        let literal = code[literal_start..end].replace("\\\n", " ");
        let statement = literal.split_whitespace().collect::<Vec<_>>().join(" ");
        let after = &code[end..(end + 300).min(code.len())];
        let table_arg = table_arg_re.captures(after).map(|c| c[1].to_owned());
        let table = table_re
            .captures(&statement)
            .map(|c| c[1].to_owned())
            .unwrap_or_default();
        let is_sw = table.contains("sw_") || (table.starts_with("{}") && table_arg.is_some());
        if !is_sw {
            continue;
        }
        let before = &code[..m.start()];
        let function = fn_re
            .captures_iter(before)
            .last()
            .map(|c| c[1].to_owned())
            .unwrap_or_default();
        let fn_start = before.rfind(&format!("fn {function}(")).unwrap_or(0);
        let fn_end = code[m.start()..]
            .find("\n    }\n")
            .or_else(|| code[m.start()..].find("\n}\n"))
            .map_or(code.len(), |e| m.start() + e);
        out.push((
            format!("{module}.{function}"),
            statement,
            code[fn_start..fn_end].to_owned(),
        ));
    }
    out
}

fn carries_user_id(statement: &str, function_code: &str) -> bool {
    if statement.to_uppercase().starts_with("INSERT") {
        let columns = statement.split("VALUES").next().unwrap_or_default();
        if columns.contains("({})") {
            // A dynamic column list (`create_position`, `add_fill`): the function builds it
            // with `user_id` first.
            return function_code.contains("\"user_id\"");
        }
        return columns.contains("user_id");
    }
    regex::Regex::new(r"(?is)WHERE\b.*\buser_id\s*=\s*\?")
        .unwrap()
        .is_match(statement)
}

fn all_statements() -> Vec<(String, String, String)> {
    let mut statements = sql_statements("swing_desk", STORE_RS);
    statements.extend(sql_statements("swing_desk", DESK_RS));
    statements.extend(sql_statements("swing_monitor", RUNNER_RS));
    statements.extend(sql_statements("swing_execute", EXECUTE_RS));
    statements
}

#[test]
fn test_every_insert_and_update_names_user_id() {
    let statements = all_statements();
    assert!(
        statements.len() >= 8,
        "the scan found only {} writes: {statements:?}",
        statements.len()
    );
    let offenders: Vec<String> = statements
        .iter()
        .filter(|(w, s, _)| !WHITELISTED_UPDATES.contains(&(w.as_str(), s.as_str())))
        .filter(|(_, s, code)| !carries_user_id(s, code))
        .map(|(w, s, _)| format!("{w}: {s}"))
        .collect();
    assert!(
        offenders.is_empty(),
        "sw_ writes without the user: {offenders:?}"
    );
}

#[test]
fn test_the_whitelist_is_still_real() {
    let statements: Vec<(String, String)> = sql_statements("swing_monitor", RUNNER_RS)
        .into_iter()
        .map(|(w, s, _)| (w, s))
        .collect();
    for (w, s) in WHITELISTED_UPDATES {
        assert!(
            statements.contains(&(w.to_owned(), s.to_owned())),
            "{w} no longer issues {s:?}: {statements:?}"
        );
    }
}

#[test]
fn test_the_scan_sees_the_dynamic_inserts() {
    let desk: BTreeSet<String> = sql_statements("swing_desk", STORE_RS)
        .into_iter()
        .map(|(w, _, _)| w)
        .collect();
    for name in [
        "swing_desk.create_position",
        "swing_desk.add_fill",
        "swing_desk.bump_session",
        "swing_desk.set_line",
        "swing_desk.update_position",
    ] {
        assert!(desk.contains(name), "{name} not seen: {desk:?}");
    }
    let monitor: BTreeSet<String> = sql_statements("swing_monitor", RUNNER_RS)
        .into_iter()
        .map(|(w, _, _)| w)
        .collect();
    for name in [
        "swing_monitor.raise_signal",
        "swing_monitor.plan_for",
        "swing_monitor.record_monitor_ran",
    ] {
        assert!(monitor.contains(name), "{name} not seen: {monitor:?}");
    }
}

#[test]
fn test_the_user_id_is_the_sole_tenant_and_never_a_literal() {
    let desk = code_only(DESK_RS).replace(' ', "");
    assert!(desk.contains("user_id:settings.sole_user_id"));
    let exec_code = code_only(EXECUTE_RS);
    assert!(
        exec_code.contains("settings.sole_user_id")
            && exec_code.contains("settings.sole_broker_account_id")
    );
    let literal = regex::Regex::new(r"\buser_id\s*[:=]\s*[0-9]").unwrap();
    for (name, source) in [
        ("desk.rs", DESK_RS),
        ("store.rs", STORE_RS),
        ("runner.rs", RUNNER_RS),
        ("execute.rs", EXECUTE_RS),
    ] {
        let found = hits(&code_only(source), literal.as_str());
        assert!(
            found.is_empty(),
            "{name} passes a literal user_id: {found:?}"
        );
    }
}

#[tokio::test]
async fn test_after_a_full_morning_every_sw_row_belongs_to_the_sole_user() {
    use crate::lanes::swing::store::PgSwingStore;
    use crate::storage::{Db, DbConfig, Param};
    #[allow(clippy::print_stderr)]
    let Ok(url) = std::env::var("BASKFY_RS_SWING_TEST_PG") else {
        eprintln!("BASKFY_RS_SWING_TEST_PG is not set: skipped (Python's requires_db)");
        return;
    };
    assert!(
        url.contains("/rsd_"),
        "a bed clone rsd_*, never the template"
    );
    let db = Db::open(DbConfig {
        backend: "postgres".to_owned(),
        schema: "public".to_owned(),
        database_url: url,
        db_path: std::path::PathBuf::from("unused.db"),
    })
    .await
    .unwrap();
    let mut conn = db.connect().await.unwrap();
    // A user of our own, so every row this test writes is ours and is deleted at the end.
    let user: i64 = {
        let rows = conn
            .execute(
                "INSERT INTO public.app_user (public_id, email) VALUES (?, ?) RETURNING id",
                &[
                    Param::Text(format!("rs-swing-{}", std::process::id())),
                    Param::Text(format!("rs-swing-{}@example.invalid", std::process::id())),
                ],
            )
            .await
            .unwrap()
            .rows;
        rows[0]
            .get("id")
            .and_then(crate::storage::DbValue::as_i64)
            .unwrap()
    };
    let settings = {
        let mut s = paper();
        s.sole_user_id = user;
        s
    };
    let day = now().date_naive();
    let plan_id = format!(
        "00000000-0000-4000-8000-{:012}",
        900_000 + u64::from(std::process::id())
    );
    let ins = |sql: &str| sql.to_owned();
    for (sql, params) in [
        (
            ins(
                "INSERT INTO public.sw_config (user_id, sleeve_capital_inr, risk_per_trade_pct, max_position_pct, max_open_positions, first_live_sessions_left) VALUES (?, 1000000, 0.5, 20, 10, 0)",
            ),
            vec![Param::Int(user)],
        ),
        (
            ins(
                "INSERT INTO public.sw_market_daily (user_id, date, gate, exposure_level, max_open_positions, max_exposure_pct, new_entries_allowed) VALUES (?, ?, 'GREEN', 3, 10, 100, true)",
            ),
            vec![
                Param::Int(user),
                Param::Text((day - Duration::days(1)).to_string()),
            ],
        ),
        (
            ins(
                "INSERT INTO public.sw_setup_daily (user_id, date, instrument_id, setup, status, score, adr_pct, turnover_avg) VALUES (?, ?, 78, 'FLAG', 'BREAKOUT_TODAY', 80, 5, 1000000000)",
            ),
            vec![
                Param::Int(user),
                Param::Text((day - Duration::days(1)).to_string()),
            ],
        ),
        (
            ins(
                "INSERT INTO public.sw_plan (plan_id, user_id, as_of, source, built_at, expires_at, plan_hash, gate, exposure_level) VALUES (?::uuid, ?, ?, 'MORNING', ?, ?, 'rs-test', 'GREEN', 3)",
            ),
            vec![
                Param::Text(plan_id.clone()),
                Param::Int(user),
                Param::Text(day.to_string()),
                Param::Text((now() - Duration::minutes(5)).to_rfc3339()),
                Param::Text((now() + Duration::minutes(25)).to_rfc3339()),
            ],
        ),
    ] {
        conn.execute(&sql, &params).await.unwrap();
    }
    let plan_pk = conn
        .execute(
            "SELECT id FROM public.sw_plan WHERE plan_id = ?::uuid",
            &[Param::Text(plan_id.clone())],
        )
        .await
        .unwrap()
        .rows[0]
        .get("id")
        .and_then(crate::storage::DbValue::as_i64)
        .unwrap();
    let line = conn
        .execute(
            "INSERT INTO public.sw_plan_line (plan_id, user_id, kind, instrument_id, setup, quantity, trigger, stop, risk_inr, position_value, trail, note, client_id) VALUES (?, ?, 'BUY_ON_TRIGGER', 78, 'FLAG', 10, 357.00, 345.00, 120, 3570, 'MA20', '', ?) RETURNING id",
            &[Param::Int(plan_pk), Param::Int(user), Param::Text(format!("{plan_id}:ABB:BUY_ON_TRIGGER"))],
        )
        .await
        .unwrap()
        .rows[0]
        .get("id")
        .and_then(crate::storage::DbValue::as_i64)
        .unwrap();
    let spy = Rig::new(&settings).await;
    let mut store = PgSwingStore {
        conn: db.connect().await.unwrap(),
        user_id: user,
        schema: "public".to_owned(),
        broker_account_id: None,
        live_level: None,
        settings: settings.clone(),
        clock: std::sync::Arc::new(crate::clock::FixedClock::new(now())),
    };
    let clock = FakeClock::default();
    let cx = ExecContext {
        settings: &settings,
        orders: None,
        timer: &clock,
    };
    let out = x::execute_line(
        &mut store,
        &spy,
        &cx,
        &plan_id,
        line,
        "true",
        now(),
        Some(d("357.15")),
    )
    .await;
    let mut foreign = Vec::new();
    for table in [
        "sw_position",
        "sw_fill",
        "sw_session",
        "sw_plan_line",
        "sw_plan",
    ] {
        let n = conn
            .execute(
                &format!("SELECT count(*) AS n FROM public.{table} WHERE user_id = ? "),
                &[Param::Int(user)],
            )
            .await
            .unwrap()
            .rows[0]
            .get("n")
            .and_then(crate::storage::DbValue::as_i64)
            .unwrap();
        foreign.push((table, n));
    }
    let other = {
        let mut s = PgSwingStore {
            conn: db.connect().await.unwrap(),
            user_id: user + 1_000_000,
            schema: "public".to_owned(),
            broker_account_id: None,
            live_level: None,
            settings: settings.clone(),
            clock: std::sync::Arc::new(crate::clock::FixedClock::new(now())),
        };
        (
            s.open_positions().await.unwrap(),
            s.line(line).await.unwrap(),
        )
    };
    // Clean up before asserting, so a failure leaves nothing behind.
    conn.execute(
        "DELETE FROM public.app_user WHERE id = ?",
        &[Param::Int(user)],
    )
    .await
    .unwrap();
    let out = out.unwrap();
    assert_eq!(out.status, "SIMULATED", "{out:?}");
    let written: BTreeMap<&str, i64> = foreign.into_iter().collect();
    assert_eq!(written["sw_position"], 1);
    assert_eq!(written["sw_fill"], 1);
    assert_eq!(written["sw_session"], 1);
    assert!(written["sw_plan_line"] >= 1);
    assert!(
        other.0.is_empty() && other.1.is_none(),
        "another tenant's store sees none of it"
    );
}

// =======================================================================================
// Track C §5 and 04 §6.5 at the desk: execute_line's guards, as seeded property loops
// =======================================================================================

#[tokio::test]
async fn test_sell_is_blocked_exactly_when_it_exceeds_quantity_open_over_500_cases() {
    let fast = Rig::fast(&paper()).await;
    let mut rng = Rng(SEED);
    let (mut blocked, mut filled) = (0, 0);
    for case in 0..CASES {
        let entered = rng.randint(1, 5000);
        let open_qty = rng.randint(1, entered);
        let options = [
            rng.randint(-5, 0),
            rng.randint(1, open_qty),
            rng.randint(open_qty + 1, 2 * open_qty + 5),
        ];
        let sell_qty = rng.choice(&options);
        let present = rng.random() > 0.1;
        let mut store = MemoryStore::new();
        let plan = store.add_plan();
        if present {
            store.add_position_with(|p| {
                p.quantity_entered = entered;
                p.quantity_open = open_qty;
                p.gtt_id = Some("DRY-x".to_owned());
                if open_qty != entered {
                    "PARTIAL".clone_into(&mut p.state);
                }
            });
        }
        let line = store.add_line_with(&plan, |l| {
            "SELL_AT_OPEN".clone_into(&mut l.kind);
            l.quantity = sell_qty;
            l.trigger = None;
            l.stop = None;
            l.note = Some("PARTIAL_INTO_STRENGTH".to_owned());
        });
        let out = exec(&mut store, &fast, &plan, line, Some(d("104.00")))
            .await
            .unwrap();
        let should_block = !present || sell_qty <= 0 || sell_qty > open_qty;
        if should_block {
            blocked += 1;
            assert_eq!(
                out.status,
                "BLOCKED",
                "{}",
                why(&format!(
                    "case {case}: {sell_qty} of {open_qty} open (present={present}) → {out:?}"
                ))
            );
            assert!(
                store.book.fills.is_empty(),
                "{}",
                why(&format!("case {case}: a blocked sell wrote a fill"))
            );
            if present {
                assert_eq!(store.book.positions[&1].quantity_open, open_qty);
            }
        } else {
            filled += 1;
            assert_eq!(
                out.status,
                "SIMULATED",
                "{}",
                why(&format!("case {case}: {out:?}"))
            );
            assert_eq!(store.book.fills.len(), 1);
            let fill = &store.book.fills[0];
            assert_eq!(fill_val(fill, "quantity"), &Val::Int(sell_qty));
            assert_eq!(fill_val(fill, "side"), &Val::Text("SELL".to_owned()));
            let left = store.book.positions[&1].quantity_open;
            assert!(left == open_qty - sell_qty && left >= 0);
        }
    }
    assert!(
        blocked > 50 && filled > 50,
        "{}",
        why(&format!("the loop was lopsided: {blocked}/{filled}"))
    );
}

#[test]
fn test_a_sell_never_consults_the_brokers_holdings() {
    let code = code_only(EXECUTE_RS);
    let sell = &code[code.find("async fn sell(").unwrap()..];
    let sell = &sell[..sell.find("\n}\n").unwrap()];
    let pick = &code[code.find("async fn position_for_line(").unwrap()..];
    let pick = &pick[..pick.find("\n}\n").unwrap()];
    let both = format!("{sell}\n{pick}");
    assert!(both.contains("position_for_line") && both.contains("open_position_for"));
    for word in ["holdings", "kc.", "positions()"] {
        assert!(!both.contains(word), "{word}");
    }
}

#[tokio::test]
async fn test_stop_never_falls_a_raise_at_or_below_the_resting_stop_is_blocked_over_500_cases() {
    let fast = Rig::fast(&paper()).await;
    let mut rng = Rng(SEED);
    let (mut blocked, mut raised) = (0, 0);
    for case in 0..CASES {
        let entry = dd(rng.randint(2000, 100_000)) / dd(20);
        let mut resting = q05(entry * (Decimal::ONE - dd(rng.randint(5, 1000)) / dd(10_000)));
        if resting >= entry {
            resting = entry - d("0.05");
        }
        let last = entry * (Decimal::ONE + dd(rng.randint(0, 3000)) / dd(10_000));
        let options = [
            resting - dd(rng.randint(0, 2000)) / dd(20),
            resting,
            resting + dd(rng.randint(1, 4000)) / dd(20),
        ];
        let new_stop = q05(rng.choice(&options));
        let mut store = MemoryStore::new();
        let plan = store.add_plan();
        store.add_position_with(|p| {
            p.entry_avg = Some(entry);
            p.initial_stop = Some(resting);
            p.stop = Some(resting);
            p.gtt_trigger = Some(resting);
            p.gtt_id = Some("DRY-old".to_owned());
        });
        let line = store.add_line_with(&plan, |l| {
            "RAISE_GTT_STOP".clone_into(&mut l.kind);
            l.quantity = 0;
            l.trigger = None;
            l.stop = Some(new_stop);
            l.note = Some("BREAKEVEN_AT_R".to_owned());
        });
        let out = exec(&mut store, &fast, &plan, line, Some(last))
            .await
            .unwrap();
        let p = store.book.positions[&1].clone();
        if new_stop <= resting {
            blocked += 1;
            assert_eq!(
                out.status,
                "BLOCKED",
                "{}",
                why(&format!("case {case}: {new_stop} vs {resting}: {out:?}"))
            );
            assert!(out.reason.contains("never falls"));
            assert!(
                p.stop == Some(resting) && p.gtt_id.as_deref() == Some("DRY-old"),
                "{}",
                why(&format!(
                    "case {case}: a blocked raise touched the position"
                ))
            );
        } else if new_stop >= last {
            assert!(out.status == "BLOCKED" && p.stop == Some(resting));
        } else {
            raised += 1;
            assert_eq!(
                out.status,
                "SIMULATED",
                "{}",
                why(&format!("case {case}: {out:?}"))
            );
            assert!(p.stop == Some(new_stop) && new_stop > resting);
            assert!(
                p.gtt_trigger == Some(new_stop) && p.gtt_id.as_deref() != Some("DRY-old"),
                "{}",
                why(&format!("case {case}: {p:?}"))
            );
        }
    }
    assert!(
        blocked > 100 && raised > 50,
        "{}",
        why(&format!("lopsided: {blocked}/{raised}"))
    );
}

// =======================================================================================
// Track C §1/§2: CNC only, no leverage, no intraday, no options
// =======================================================================================

#[test]
fn test_every_order_the_module_sends_is_cnc_on_nse_without_a_variety() {
    let code = code_only(EXECUTE_RS);
    let places: Vec<&str> = code
        .match_indices(".place(PlaceOrder {")
        .map(|(i, _)| {
            let rest = &code[i..];
            &rest[..rest.find("})").unwrap()]
        })
        .collect();
    assert_eq!(places.len(), 2, "the buy and the sell");
    for call in places {
        assert!(call.contains("product: \"CNC\".to_owned()"), "{call}");
        assert!(call.contains("exchange: \"NSE\".to_owned()"), "{call}");
        assert!(
            call.contains("order_type: \"MARKET\".to_owned()")
                || call.contains("order_type: order_type.clone()"),
            "{call}"
        );
        assert!(!call.contains("variety"), "{call}");
    }
    assert_eq!(x::ENTRY_ORDER_TYPES, ["LIMIT", "MARKET"]);
    // ...and the module refuses anything else at run time rather than sending it.
    assert!(code.contains("if !ENTRY_ORDER_TYPES.contains(&order_type.as_str())"));
    for word in ["\"MIS\"", "\"NFO\"", "\"BFO\"", "\"co\"", "\"bo\"", "MTF"] {
        assert!(!code.contains(word), "{word}");
    }
}

#[test]
fn test_the_gates_never_allow_intraday_or_options_whatever_the_weekly_desk_says() {
    let mut s = paper();
    s.intraday_enabled = true;
    s.options_enabled = true;
    let gates = x::swing_gates(&s);
    assert!(!gates.intraday_enabled && !gates.options_enabled);
}

#[tokio::test]
async fn test_an_mis_order_would_be_blocked_by_the_gateway_itself() {
    let fast = Rig::fast(&paper()).await;
    let sole = crate::gateway::TenantIds::new(1, 1).unwrap();
    let out = fast
        .place(PlaceOrder {
            symbol: "ALPHA".to_owned(),
            qty: 1,
            side: "BUY".to_owned(),
            product: "MIS".to_owned(),
            order_type: "MARKET".to_owned(),
            exchange: "NSE".to_owned(),
            client_id: Some("x:MIS".to_owned()),
            tenant: Some(sole),
            plan_tenant: Some(sole),
            ..PlaceOrder::default()
        })
        .await
        .unwrap();
    assert_eq!(st(&out), "BLOCKED");
    let error = match out.get("error") {
        Some(PyValue::Str(s)) => s.clone(),
        other => panic!("{other:?}"),
    };
    assert!(error.contains("MIS"), "{error}");
}

// =======================================================================================
// A5 (SW10.4): no sequence of confirms exceeds the ceiling, the count or the cap
// =======================================================================================

fn random_book(rng: &mut Rng, store: &mut MemoryStore, n: i64) {
    for i in 0..n {
        let entry = dd(rng.randint(50, 900));
        let qty = rng.randint(10, 400);
        store.add_position_with(|p| {
            p.symbol = format!("HELD{i}");
            p.instrument_id = 100 + i;
            p.entry_avg = Some(entry);
            p.quantity_entered = qty;
            p.quantity_open = qty;
            p.initial_stop = Some(entry - dd(2));
            p.stop = Some(entry - dd(2));
        });
    }
}

fn random_line(rng: &mut Rng, store: &mut MemoryStore, plan: &str, i: i64) -> i64 {
    let trigger = dd(rng.randint(2000, 90_000)) / dd(100);
    let adr = dd(rng.randint(400, 1000)) / dd(100);
    let adr_bp = i64::try_from((adr * dd(100)).trunc().mantissa()).unwrap();
    let distance_pct = dd(rng.randint(50, adr_bp)) / dd(100);
    let stop = q05(trigger * (Decimal::ONE - distance_pct / dd(100)));
    let symbol = format!("LINE{i}");
    let turnover = dd(rng.choice(&[50, 100, 500]) * 1_000_000);
    let score = dd(rng.randint(40, 95));
    store
        .detected
        .insert(symbol.clone(), (adr, Some(turnover), score));
    let options = [
        rng.randint(1, 50),
        rng.randint(50, 3000),
        rng.randint(3000, 20_000),
    ];
    let quantity = rng.choice(&options);
    store.add_line_with(plan, |l| {
        l.symbol = symbol;
        l.instrument_id = 200 + i;
        l.quantity = quantity;
        l.trigger = Some(trigger);
        l.stop = Some(stop);
        l.risk_inr = Some((trigger - stop) * dd(quantity));
        l.position_value = Some(trigger * dd(quantity));
    })
}

fn held_book(store: &MemoryStore) -> (Decimal, usize) {
    let held: Vec<&PositionRow> = store
        .book
        .positions
        .values()
        .filter(|p| p.state != "CLOSED" && p.quantity_open > 0)
        .collect();
    (
        held.iter()
            .map(|p| p.entry_avg.unwrap() * dd(p.quantity_open))
            .sum(),
        held.len(),
    )
}

#[tokio::test]
#[allow(clippy::cast_precision_loss, clippy::print_stderr)]
async fn test_a_sequence_of_confirms_never_exceeds_ceiling_count_or_cap_over_500_sequences() {
    let codes = [
        "EXPOSURE_FULL",
        "TIER_FULL",
        "SESSION_CAP",
        "SIZE_REFUSED",
        "ALREADY_HELD",
        "GATE_RED",
        "DRAWDOWN_LOCKOUT",
        "NOT_TRADEABLE_SETUP",
    ];
    let mut seen: BTreeMap<String, usize> = BTreeMap::new();
    for k in ["sized", "re-sized"].iter().chain(codes.iter()) {
        seen.insert((*k).to_owned(), 0);
    }
    let mut rng = Rng(SEED);
    for case in 0..CASES {
        let rung = usize::try_from(rng.randint(0, 3)).unwrap();
        let cap = rng.randint(1, 10);
        let capital = dd(rng.randint(2, 50)) * dd(100_000);
        let mut store = MemoryStore::with(0, rung, MarketGate::Green, capital);
        {
            let config = store.book.config.as_mut().unwrap();
            config.max_open_positions = cap;
            config.risk_per_trade_pct = d(rng.choice(&["0.250", "0.500", "1.000"]));
        }
        let n = rng.randint(0, 3);
        random_book(&mut rng, &mut store, n);
        let gw = Rig::fast(&paper()).await;
        let plan = store.add_plan();
        let count = rng.randint(1, 8);
        let lines: Vec<i64> = (0..count)
            .map(|i| random_line(&mut rng, &mut store, &plan, i))
            .collect();
        let (count_limit, ceiling_pct) = TIERS[rung];
        let ceiling = capital * Decimal::try_from(ceiling_pct).unwrap() / dd(100);
        let allowed_count = usize::try_from(count_limit.min(cap)).unwrap();
        let mut entries = 0;
        let why = format!("seed {SEED} case {case}: rung {rung} cap {cap} sleeve {capital}");
        for line in &lines {
            let (before, count_before) = held_book(&store);
            let planned = store.book.lines[line].quantity;
            let out = exec(&mut store, &gw, &plan, *line, None).await.unwrap();
            let (after, count_after) = held_book(&store);
            if out.status == "SIMULATED" {
                entries += 1;
                let sent = store.book.lines[line].quantity;
                assert!(
                    after <= ceiling,
                    "{why}: book {after} over the ceiling {ceiling}"
                );
                assert!(
                    count_after <= allowed_count,
                    "{why}: {count_after} > {allowed_count}"
                );
                assert!(entries <= 3, "{why}: a fourth entry went out");
                assert!(
                    sent <= planned && sent > 0,
                    "{why}: sent {sent} of {planned}"
                );
                assert_eq!(
                    store.book.positions[&out.position_id.unwrap()].quantity_open,
                    sent
                );
                assert_eq!(
                    fill_val(store.book.fills.last().unwrap(), "quantity"),
                    &Val::Int(sent)
                );
                assert_eq!(int(out.gtt.as_ref().unwrap(), "qty"), sent);
                *seen
                    .get_mut(if sent < planned { "re-sized" } else { "sized" })
                    .unwrap() += 1;
            } else {
                assert_eq!(out.status, "BLOCKED", "{why}: {out:?}");
                let code = out.reason.split(':').next().unwrap_or_default().to_owned();
                assert!(codes.contains(&code.as_str()), "{why}: {}", out.reason);
                *seen.get_mut(&code).unwrap() += 1;
                assert_eq!(
                    (after, count_after),
                    (before, count_before),
                    "{why}: refused, moved"
                );
                assert_eq!(store.line_state(*line), "REJECTED");
                assert_eq!(store.book.lines[line].quantity, planned);
            }
        }
        assert!(entries <= 3, "{why}");
        assert_eq!(
            store.locks,
            vec![now().date_naive(); lines.len()],
            "{why}: a confirm skipped the lock"
        );
        let events = gw.events();
        assert!(
            events.iter().all(|e| DRY_EVENTS.contains(&e.as_str()))
                && !events.iter().any(|e| BROKER_EVENTS.contains(&e.as_str())),
            "{why}: {events:?}"
        );
        // A stop closer than the band's floor is a warning beside the GTT, not an order.
        let sent_events: Vec<&String> =
            events.iter().filter(|e| *e != "gtt_band_warning").collect();
        let expected: Vec<&str> = (0..entries)
            .flat_map(|_| ["dry_run", "gtt_dry_run"])
            .collect();
        assert_eq!(sent_events, expected, "{why}: {events:?}");
    }
    eprintln!("confirm sequences over {CASES} cases: {seen:?}");
    assert!(seen["sized"] > 0 && seen["re-sized"] > 0, "{seen:?}");
    for code in ["EXPOSURE_FULL", "TIER_FULL", "SESSION_CAP", "SIZE_REFUSED"] {
        assert!(seen[code] > 0, "{code} never occurred: {seen:?}");
    }
}

#[tokio::test]
async fn test_two_tabs_confirming_the_same_line_send_it_once() {
    let fast = Rig::fast(&paper()).await;
    let mut store = MemoryStore::with(0, 3, MarketGate::Green, d("1000000"));
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.quantity = 100);
    let first = exec(&mut store, &fast, &plan, line, None).await.unwrap();
    assert_eq!(first.status, "SIMULATED");
    let second = exec(&mut store, &fast, &plan, line, None).await;
    assert!(
        matches!(second, Err(ExecError::Http(ref h)) if h.status == 409),
        "{second:?}"
    );
    assert_eq!(fast.sent_quantities(), [100]);
    assert_eq!(store.book.positions.len(), 1);
}

// --- the coverage check -----------------------------------------------------------------------

const PORTED: &[&str] = &[
    "test_every_gateway_call_through_the_route_ran_dry",
    "test_the_gate_truth_table_has_exactly_one_live_cell",
    "test_the_spy_is_not_blind_the_live_cell_is_stopped_by_the_fake",
    "test_the_route_builds_no_gateway_at_import_and_resolves_the_flag_per_order",
    "test_the_strategy_code_never_names",
    "test_the_runner_code_never_names",
    "test_the_runner_hands_the_strategy_no_gateway_and_builds_nothing_with_the_flag_off",
    "test_generate_targets_is_empty_at_runtime",
    "test_the_scan_is_over_code_not_prose",
    "test_every_insert_and_update_names_user_id",
    "test_the_whitelist_is_still_real",
    "test_the_scan_sees_the_dynamic_inserts",
    "test_the_user_id_is_the_sole_tenant_and_never_a_literal",
    "test_after_a_full_morning_every_sw_row_belongs_to_the_sole_user",
    "test_sell_is_blocked_exactly_when_it_exceeds_quantity_open_over_500_cases",
    "test_a_sell_never_consults_the_brokers_holdings",
    "test_stop_never_falls_a_raise_at_or_below_the_resting_stop_is_blocked_over_500_cases",
    "test_every_order_the_module_sends_is_cnc_on_nse_without_a_variety",
    "test_the_gates_never_allow_intraday_or_options_whatever_the_weekly_desk_says",
    "test_an_mis_order_would_be_blocked_by_the_gateway_itself",
    "test_a_sequence_of_confirms_never_exceeds_ceiling_count_or_cap_over_500_sequences",
    "test_two_tabs_confirming_the_same_line_send_it_once",
];

#[test]
fn every_python_test_has_a_rust_port() {
    let python = python_tests("test_swing_track_c.py");
    assert_eq!(python.len(), PORTED.len(), "{python:?}");
    let missing: Vec<&String> = python
        .iter()
        .filter(|n| !PORTED.contains(&n.as_str()))
        .collect();
    assert!(missing.is_empty(), "not ported: {missing:?}");
}
