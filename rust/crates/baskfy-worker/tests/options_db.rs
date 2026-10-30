//! `test_options_scan_task.py`, `test_options_plan_task.py`, `test_options_plan_o2_task.py` and
//! `test_options_plan_o3_task.py`'s database classes, against a throwaway clone of the bed. Each
//! test seeds the core's fixture day inside one transaction and rolls it back.
//!
//! Run: `BASKFY_RS_OPTIONS_DB_URL=postgres://…/rst_… cargo test -p baskfy-worker --test
//! options_db -- --ignored --test-threads=1`. No provider is built; the margin calculator is a
//! fixed in-process answer.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines,
    clippy::type_complexity,
    dead_code
)]

#[path = "../../baskfy-core/tests/options_common/mod.rs"]
mod f;

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use baskfy_core::options::bars::Bar;
use baskfy_core::options::config::Side;
use baskfy_core::options::plan::MarginLeg;
use baskfy_db::{DbSettings, OrderedJson, WriteDb};
use baskfy_providers::secret::Secret;
use baskfy_worker::settings::WorkerSettings;
use baskfy_worker::tasks::options::{MarginReader, PlanFamily, plan_minute, scan_minute};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, Utc};
use rust_decimal::Decimal;
use sqlx::PgConnection;

const ENV: &str = "BASKFY_RS_OPTIONS_DB_URL";
const IGNORE: &str = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored";

static LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

fn db() -> WriteDb {
    let url = std::env::var(ENV).unwrap_or_else(|_| panic!("{IGNORE}"));
    let name = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    assert!(
        name.starts_with("rst_"),
        "refusing {name:?}: these tests write, so they need a throwaway clone of the bed"
    );
    WriteDb::connect_lazy(&DbSettings::with_url(Secret::new(url))).unwrap()
}

fn dec(t: &str) -> Decimal {
    t.parse().unwrap()
}

fn ist(y: i32, m: u32, d: u32, hh: u32, mm: u32, ss: u32) -> DateTime<FixedOffset> {
    f::ist_dt(y, m, d, hh, mm, ss)
}

// --- seeding ----------------------------------------------------------------------------------

fn chain_at(
    bars: &[Bar],
    minute: DateTime<FixedOffset>,
    expiries: &[NaiveDate],
    smile: &f::Smile,
) -> f::Snapshot {
    let spot = bars
        .iter()
        .rfind(|b| b.ts + Duration::minutes(1) <= minute)
        .unwrap()
        .close;
    let fwd: BTreeMap<NaiveDate, Decimal> = expiries
        .iter()
        .map(|e| (*e, f::carry_forward(spot, minute, *e)))
        .collect();
    f::snapshot_with(minute, spot, expiries, smile, Some(&fwd), f::DEPTH)
}

async fn exec(conn: &mut PgConnection, sql: &str) {
    sqlx::raw_sql(sql).execute(&mut *conn).await.unwrap();
}

async fn index_def(conn: &mut PgConnection, slug: &str, ident: i16) -> i16 {
    let found: Option<i16> = sqlx::query_scalar("SELECT id FROM index_def WHERE slug = $1")
        .bind(slug)
        .fetch_optional(&mut *conn)
        .await
        .unwrap();
    let id = match found {
        Some(id) => id,
        None => {
            sqlx::query(
                "INSERT INTO index_def (id, slug, name, is_universe, sort_order) \
                 VALUES ($1, $2, $2, false, 0)",
            )
            .bind(ident)
            .bind(slug)
            .execute(&mut *conn)
            .await
            .unwrap();
            ident
        }
    };
    sqlx::query("DELETE FROM index_snapshot_daily WHERE index_id = $1")
        .bind(id)
        .execute(&mut *conn)
        .await
        .unwrap();
    id
}

/// The fixture day as OP3 stores it: the master, the day's bars before `until`, the daily levels
/// and the given chain minutes; a fresh user.
async fn seed(
    conn: &mut PgConnection,
    day: NaiveDate,
    bars: &[Bar],
    until: DateTime<FixedOffset>,
    chains: &[f::Snapshot],
) -> i64 {
    let tag = format!(
        "w8-{}",
        Utc::now().timestamp_nanos_opt().unwrap_or_default() % 1_000_000_007
    );
    let user: i64 = sqlx::query_scalar(
        "INSERT INTO app_user (public_id, email) VALUES ($1, $1 || '@x.test') RETURNING id",
    )
    .bind(&tag)
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    exec(
        conn,
        "UPDATE op_contract SET expired = true WHERE underlying = 'NIFTY'",
    )
    .await;
    for c in f::nifty_master() {
        sqlx::query(
            "INSERT INTO op_contract (instrument_token, tradingsymbol, underlying, expiry, strike, \
             option_type, lot_size, tick_size, first_seen, last_seen, expired) \
             VALUES ($1, $2, 'NIFTY', $3, $4, $5, $6::integer, $7, $8, $8, false) \
             ON CONFLICT (instrument_token) DO UPDATE SET tradingsymbol = excluded.tradingsymbol, \
             underlying = excluded.underlying, expiry = excluded.expiry, strike = excluded.strike, \
             option_type = excluded.option_type, lot_size = excluded.lot_size, \
             tick_size = excluded.tick_size, expired = false",
        )
        .bind(c.instrument_token)
        .bind(&c.tradingsymbol)
        .bind(c.expiry)
        .bind(c.strike)
        .bind(c.option_type.value())
        .bind(c.lot_size)
        .bind(c.tick_size)
        .bind(day)
        .execute(&mut *conn)
        .await
        .unwrap();
    }
    exec(conn, "DELETE FROM op_index_minute").await;
    let nifty: i64 = sqlx::query_scalar(
        "UPDATE instrument SET instrument_type = 'INDEX', kite_token = 256265 \
         WHERE exchange_id = 1 AND symbol = 'NIFTY 50' RETURNING id",
    )
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    for b in bars.iter().filter(|b| b.ts < until) {
        sqlx::query(
            "INSERT INTO op_index_minute (instrument_id, ts, open, high, low, close, source) \
             VALUES ($1, $2, $3, $4, $5, $6, 'KITE_HIST')",
        )
        .bind(nifty)
        .bind(b.ts)
        .bind(b.open)
        .bind(b.high)
        .bind(b.low)
        .bind(b.close)
        .execute(&mut *conn)
        .await
        .unwrap();
    }
    let n50 = index_def(conn, "nifty-50", 32001).await;
    let vix = index_def(conn, "india-vix", 32002).await;
    let closes = f::rising_closes();
    let n = i64::try_from(closes.len()).unwrap();
    let mut levels: Vec<(i16, NaiveDate, Decimal)> = closes
        .iter()
        .enumerate()
        .map(|(i, level)| {
            (
                n50,
                day - Duration::days(n - i64::try_from(i).unwrap()),
                *level,
            )
        })
        .collect();
    levels.push((vix, day - Duration::days(1), Decimal::from(14)));
    for (index, date, level) in levels {
        sqlx::query("INSERT INTO index_snapshot_daily (index_id, date, level) VALUES ($1, $2, $3)")
            .bind(index)
            .bind(date)
            .bind(level)
            .execute(&mut *conn)
            .await
            .unwrap();
    }
    sqlx::query("DELETE FROM op_chain_snapshot WHERE ts >= $1 AND ts < $2")
        .bind(f::at(day, 0, 0))
        .bind(f::at(day, 0, 0) + Duration::days(1))
        .execute(&mut *conn)
        .await
        .unwrap();
    for snap in chains {
        for q in &snap.quotes {
            let side = |levels: &[baskfy_core::options::chain::Level]| {
                serde_json::Value::Array(
                    levels
                        .iter()
                        .map(|lv| {
                            serde_json::json!({"price": lv.price.to_string(), "quantity": lv.quantity})
                        })
                        .collect(),
                )
            };
            let depth = serde_json::json!({"buy": side(&q.bids), "sell": side(&q.asks)});
            sqlx::query(
                "INSERT INTO op_chain_snapshot (ts, instrument_token, expiry, strike, option_type, \
                 spot, bid, ask, last, bid_qty, ask_qty, depth_json, volume, oi, source) \
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL, $9::integer, $10::integer, \
                 $11::jsonb, 0, $12, 'QUOTE')",
            )
            .bind(snap.ts)
            .bind(q.instrument_token)
            .bind(q.expiry)
            .bind(q.strike)
            .bind(q.option_type.value())
            .bind(snap.spot)
            .bind(q.bid)
            .bind(q.ask)
            .bind(q.bids.first().map(|l| l.quantity))
            .bind(q.asks.first().map(|l| l.quantity))
            .bind(depth.to_string())
            .bind(q.oi)
            .execute(&mut *conn)
            .await
            .unwrap();
        }
    }
    user
}

fn quiet_day() -> NaiveDate {
    f::quiet_monthly()
}

/// `test_options_scan_task._seed_day`: the quiet monthly, bars to 10:10, chains at 10:00 and 10:05.
async fn seed_quiet(conn: &mut PgConnection) -> i64 {
    let day = quiet_day();
    let bars = f::quiet_monthly_bars();
    let exps = [day, f::d(2026, 11, 3)];
    let chains = [
        chain_at(&bars, f::at(day, 10, 0), &exps, &f::skew(0.30, 0.33)),
        chain_at(&bars, f::at(day, 10, 5), &exps, &f::skew(0.5, 0.5)),
    ];
    seed(conn, day, &bars, f::at(day, 10, 10), &chains).await
}

fn o2_day() -> NaiveDate {
    f::o2_up_break()
}

/// `test_options_plan_o2_task._seed_o2_day`.
async fn seed_o2(conn: &mut PgConnection, bars: &[Bar], until: DateTime<FixedOffset>) -> i64 {
    let day = o2_day();
    let exps = [f::d(2026, 10, 20), f::d(2026, 10, 27)];
    let chains = [chain_at(
        bars,
        f::at(day, 10, 5),
        &exps,
        &f::skew(0.30, 0.33),
    )];
    seed(conn, day, bars, until, &chains).await
}

fn counter_bars() -> Vec<Bar> {
    let day = o2_day();
    let mut before = f::zigzag(
        Decimal::from(25020),
        Decimal::from(10),
        f::to_minutes(10, 0),
    );
    let after = f::ramp(
        *before.last().unwrap(),
        Decimal::from(-4),
        f::to_minutes(15, 30) - before.len(),
    );
    before.extend(after);
    f::bars_from_closes(day, Decimal::from(25020), &before)
}

fn o3_day() -> NaiveDate {
    f::gap_hold_day()
}

fn both_bars() -> Vec<Bar> {
    let mut first = f::ramp(Decimal::from(25200), dec("0.5"), f::to_minutes(10, 15));
    let rest = f::ramp(
        *first.last().unwrap(),
        Decimal::from(6),
        f::to_minutes(15, 30) - first.len(),
    );
    first.extend(rest);
    f::bars_from_closes(o3_day(), Decimal::from(25200), &first)
}

/// `test_options_plan_o3_task._seed_expiry`.
async fn seed_o3(
    conn: &mut PgConnection,
    bars: &[Bar],
    until: DateTime<FixedOffset>,
    minutes: &[DateTime<FixedOffset>],
) -> i64 {
    let day = o3_day();
    let mut exps: Vec<NaiveDate> = f::nifty_master()
        .iter()
        .map(|c| c.expiry)
        .filter(|e| *e >= day)
        .collect();
    exps.sort_unstable();
    exps.dedup();
    exps.truncate(2);
    let chains: Vec<f::Snapshot> = minutes
        .iter()
        .map(|m| chain_at(bars, *m, &exps, &f::flat(0.14)))
        .collect();
    seed(conn, day, bars, until, &chains).await
}

// --- reads ------------------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct SessionRow {
    id: i64,
    state: String,
    mode: String,
    verdict: Option<String>,
    expiry: Option<NaiveDate>,
    skip_reasons: Vec<String>,
    plan_id: Option<String>,
}

async fn sessions(conn: &mut PgConnection, user: i64, sleeve: &str) -> Vec<SessionRow> {
    let rows: Vec<(
        i64,
        String,
        String,
        Option<String>,
        Option<NaiveDate>,
        Vec<String>,
        Option<String>,
    )> = sqlx::query_as(
        "SELECT id, state, mode, verdict, expiry_used, skip_reasons::text[], plan_id \
         FROM op_session WHERE user_id = $1 AND sleeve::text = $2",
    )
    .bind(user)
    .bind(sleeve)
    .fetch_all(&mut *conn)
    .await
    .unwrap();
    rows.into_iter()
        .map(
            |(id, state, mode, verdict, expiry, skip_reasons, plan_id)| SessionRow {
                id,
                state,
                mode,
                verdict,
                expiry,
                skip_reasons,
                plan_id,
            },
        )
        .collect()
}

#[derive(Debug)]
struct PlanRow {
    id: i64,
    plan_id: String,
    status: String,
    kind: String,
    structure: String,
    sizing_mode: String,
    expires_at: DateTime<Utc>,
    credit: Option<Decimal>,
    debit: Option<Decimal>,
    width: Option<Decimal>,
    lots: i16,
    lot_size: i16,
    risk_per_lot: Option<Decimal>,
    max_loss: Option<Decimal>,
    profit_target: Option<Decimal>,
    stop: Option<Decimal>,
    expected_cost: Option<Decimal>,
    cost_share: Option<Decimal>,
    margin: Option<Decimal>,
    detail: serde_json::Value,
}

async fn plan_of(conn: &mut PgConnection, session_id: i64) -> PlanRow {
    let r: (
        i64,
        String,
        String,
        String,
        String,
        String,
        DateTime<Utc>,
        Option<Decimal>,
        Option<Decimal>,
        Option<Decimal>,
        i16,
        i16,
    ) = sqlx::query_as(
        "SELECT id, plan_id, status, kind, structure, sizing_mode, expires_at, credit_points, \
         debit_points, width_points, lots, lot_size FROM op_plan WHERE session_id = $1",
    )
    .bind(session_id)
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    let m: (
        Option<Decimal>,
        Option<Decimal>,
        Option<Decimal>,
        Option<Decimal>,
        Option<Decimal>,
        Option<Decimal>,
        Option<Decimal>,
        String,
    ) = sqlx::query_as(
        "SELECT risk_per_lot_inr, max_loss_inr, profit_target_inr, stop_inr, expected_cost_inr, \
         cost_share, margin_required_inr, detail::text FROM op_plan WHERE id = $1",
    )
    .bind(r.0)
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    PlanRow {
        id: r.0,
        plan_id: r.1,
        status: r.2,
        kind: r.3,
        structure: r.4,
        sizing_mode: r.5,
        expires_at: r.6,
        credit: r.7,
        debit: r.8,
        width: r.9,
        lots: r.10,
        lot_size: r.11,
        risk_per_lot: m.0,
        max_loss: m.1,
        profit_target: m.2,
        stop: m.3,
        expected_cost: m.4,
        cost_share: m.5,
        margin: m.6,
        detail: serde_json::from_str(&m.7).unwrap(),
    }
}

type LegRow = (i16, String, String, String, Decimal, i32, bool, String);

async fn legs_of(conn: &mut PgConnection, plan_pk: i64) -> Vec<LegRow> {
    sqlx::query_as(
        "SELECT seq, role, side, tradingsymbol, limit_price, quantity, simulated, status \
         FROM op_leg WHERE plan_id = $1 ORDER BY seq",
    )
    .bind(plan_pk)
    .fetch_all(&mut *conn)
    .await
    .unwrap()
}

async fn count_plans(conn: &mut PgConnection, user: i64) -> i64 {
    sqlx::query_scalar("SELECT count(*) FROM op_plan WHERE user_id = $1")
        .bind(user)
        .fetch_one(&mut *conn)
        .await
        .unwrap()
}

fn get<'a>(json: &'a OrderedJson, path: &[&str]) -> &'a OrderedJson {
    path.iter().fold(json, |v, k| {
        v.get(k).unwrap_or_else(|| panic!("no {k} in {v:?}"))
    })
}

fn text(json: &OrderedJson) -> &str {
    match json {
        OrderedJson::Str(s) => s,
        other => panic!("not a string: {other:?}"),
    }
}

fn array(json: &OrderedJson) -> &[OrderedJson] {
    match json {
        OrderedJson::Array(a) => a,
        other => panic!("not an array: {other:?}"),
    }
}

fn sleeve_report<'a>(payload: &'a OrderedJson, sleeve: &str) -> &'a OrderedJson {
    array(get(payload, &["sleeves"]))
        .iter()
        .find(|r| text(get(r, &["sleeve"])) == sleeve)
        .unwrap()
}

fn summary(alert: &OrderedJson) -> &str {
    text(get(alert, &["summary"]))
}

type Asked = Arc<Mutex<Vec<Vec<(String, Side, i64)>>>>;

/// The broker's calculator, faked: records every basket and answers `answer(len)`.
fn calculator(answer: fn(usize) -> Result<Option<Decimal>, String>) -> (MarginReader, Asked) {
    let asked: Asked = Arc::new(Mutex::new(Vec::new()));
    let log = Arc::clone(&asked);
    let reader = MarginReader::Fixed(Arc::new(move |legs: &[MarginLeg]| {
        log.lock().unwrap().push(
            legs.iter()
                .map(|l| (l.tradingsymbol.clone(), l.side, l.quantity))
                .collect(),
        );
        answer(legs.len())
    }));
    (reader, asked)
}

fn o1_answer(n: usize) -> Result<Option<Decimal>, String> {
    Ok(Some(if n == 4 {
        dec("130903.05")
    } else {
        dec("98250.00")
    }))
}

fn o3_answer(_: usize) -> Result<Option<Decimal>, String> {
    Ok(Some(dec("24812.50")))
}

fn o3_down(_: usize) -> Result<Option<Decimal>, String> {
    Err("RuntimeError: calculator down".to_owned())
}

async fn minute(
    family: PlanFamily,
    conn: &mut PgConnection,
    user: i64,
    now: DateTime<FixedOffset>,
    margin: Option<&MarginReader>,
) -> (OrderedJson, Vec<OrderedJson>) {
    plan_minute(
        family,
        conn,
        user,
        now,
        margin,
        &WorkerSettings::default(),
        Utc::now(),
    )
    .await
    .unwrap()
}

// --- the scan ---------------------------------------------------------------------------------

#[derive(Debug, PartialEq)]
struct ScanRow {
    sleeve: String,
    state: String,
    reasons: Vec<String>,
    candidates: String,
    stale: bool,
    as_of_minute: Option<DateTime<Utc>>,
    ts: DateTime<Utc>,
    numbers: String,
}

async fn scan_rows(conn: &mut PgConnection, user: i64) -> Vec<ScanRow> {
    let rows: Vec<(
        String,
        String,
        Vec<String>,
        String,
        bool,
        Option<DateTime<Utc>>,
        DateTime<Utc>,
        String,
    )> = sqlx::query_as(
        "SELECT sleeve::text, state, reasons::text[], candidates::text, stale, as_of_minute, ts, \
         numbers::text FROM op_scan WHERE user_id = $1 ORDER BY sleeve",
    )
    .bind(user)
    .fetch_all(&mut *conn)
    .await
    .unwrap();
    rows.into_iter()
        .map(
            |(sleeve, state, reasons, candidates, stale, as_of_minute, ts, numbers)| ScanRow {
                sleeve,
                state,
                reasons,
                candidates,
                stale,
                as_of_minute,
                ts,
                numbers,
            },
        )
        .collect()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn scan_a_minute_writes_one_row_per_sleeve_idempotently() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_quiet(&mut tx).await;
    let now = ist(2026, 10, 27, 10, 1, 20);
    let first = scan_minute(&mut tx, user, now, &WorkerSettings::default())
        .await
        .unwrap();
    assert_eq!(get(&first, &["written"]), &OrderedJson::Int(5));
    assert_eq!(get(&first, &["skipped"]), &OrderedJson::Null);
    assert_eq!(text(get(&first, &["ts"])), "2026-10-27T10:01:00+05:30");
    let before = scan_rows(&mut tx, user).await;
    let again = scan_minute(&mut tx, user, now, &WorkerSettings::default())
        .await
        .unwrap();
    assert_eq!(get(&again, &["written"]), &OrderedJson::Int(5));
    let after = scan_rows(&mut tx, user).await;
    assert_eq!(after.len(), 5);
    assert_eq!(after, before);
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn scan_the_quiet_monthly_would_trade_from_the_1000_snapshot() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_quiet(&mut tx).await;
    let now = ist(2026, 10, 27, 10, 6, 20);
    let report = scan_minute(&mut tx, user, now, &WorkerSettings::default())
        .await
        .unwrap();
    assert_eq!(text(get(&report, &["states", "O1M"])), "WOULD_TRADE");
    assert_eq!(text(get(&report, &["states", "O1W"])), "NOT_TODAY");
    let states: Vec<&str> = match get(&report, &["states"]) {
        OrderedJson::Object(e) => e.iter().map(|(k, _)| k.as_str()).collect(),
        _ => panic!(),
    };
    assert_eq!(states, ["O1M", "O1W", "O2", "O3B", "O3A"]);
    let snapshots: Vec<&str> = array(get(&report, &["snapshots"]))
        .iter()
        .map(text)
        .collect();
    assert!(
        snapshots.contains(&"2026-10-27T10:00:00+05:30 -> 2026-10-27T04:30:00+00:00"),
        "{snapshots:?}"
    );
    let rows = scan_rows(&mut tx, user).await;
    let o1m = rows.iter().find(|r| r.sleeve == "O1M").unwrap();
    assert_eq!(
        o1m.as_of_minute,
        Some(f::at(quiet_day(), 10, 0).with_timezone(&Utc))
    );
    assert_eq!(o1m.ts, ist(2026, 10, 27, 10, 6, 0).with_timezone(&Utc));
    let candidates: serde_json::Value = serde_json::from_str(&o1m.candidates).unwrap();
    let list = candidates.as_array().unwrap();
    assert_eq!(list.len(), 1);
    assert_eq!(list[0]["points"], "39.65");
    assert_eq!(list[0]["lots"], 1);
    assert_eq!(list[0]["sizing_mode"], "PAPER_ONE_LOT");
    let numbers: serde_json::Value = serde_json::from_str(&o1m.numbers).unwrap();
    assert_eq!(numbers["prev_close"], "25000.00");
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn scan_a_paused_book_shows_paused() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_quiet(&mut tx).await;
    sqlx::query(
        "INSERT INTO op_book_config (user_id, paused_until, paused_reason) VALUES ($1, $2, 'BOOK')",
    )
    .bind(user)
    .bind(quiet_day())
    .execute(&mut *tx)
    .await
    .unwrap();
    let report = scan_minute(
        &mut tx,
        user,
        ist(2026, 10, 27, 10, 1, 20),
        &WorkerSettings::default(),
    )
    .await
    .unwrap();
    assert_eq!(text(get(&report, &["states", "O1M"])), "PAUSED");
    assert_eq!(text(get(&report, &["states", "O2"])), "PAUSED");
    assert_eq!(text(get(&report, &["states", "O1W"])), "NOT_TODAY");
    tx.rollback().await.unwrap();
}

// --- O1 ---------------------------------------------------------------------------------------

fn o1_now() -> DateTime<FixedOffset> {
    ist(2026, 10, 27, 10, 0, 40)
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o1_the_fixture_morning_writes_one_plan_to_the_rupee() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_quiet(&mut tx).await;
    let (margin, asked) = calculator(o1_answer);
    let (payload, alerts) = minute(PlanFamily::O1, &mut tx, user, o1_now(), Some(&margin)).await;
    let report = sleeve_report(&payload, "O1M");
    assert_eq!(text(get(report, &["state"])), "PLANNED");
    assert_eq!(get(report, &["created"]), &OrderedJson::Bool(true));
    assert_eq!(text(get(report, &["detail", "margin"])), "asked");
    assert_eq!(
        text(get(sleeve_report(&payload, "O1W"), &["state"])),
        "NO_SESSION"
    );
    assert!(sessions(&mut tx, user, "O1W").await.is_empty());
    let rows = sessions(&mut tx, user, "O1M").await;
    let [row] = rows.as_slice() else { panic!() };
    assert_eq!(
        (
            row.state.as_str(),
            row.mode.as_str(),
            row.verdict.as_deref(),
            row.expiry
        ),
        ("PLANNED", "PAPER", Some("TRADE"), Some(quiet_day()))
    );
    let plan = plan_of(&mut tx, row.id).await;
    assert_eq!(Some(&plan.plan_id), row.plan_id.as_ref());
    assert_eq!(text(get(report, &["plan_id"])), plan.plan_id);
    assert_eq!(
        (
            plan.status.as_str(),
            plan.kind.as_str(),
            plan.structure.as_str(),
            plan.sizing_mode.as_str()
        ),
        ("ISSUED", "ENTRY", "IRON_CONDOR", "PAPER_ONE_LOT")
    );
    assert_eq!(
        plan.expires_at,
        f::at(quiet_day(), 10, 15).with_timezone(&Utc)
    );
    assert_eq!(
        (plan.credit, plan.width, plan.lots, plan.lot_size),
        (Some(dec("39.65")), Some(dec("150.00")), 1, 65)
    );
    assert_eq!(plan.max_loss, Some(dec("7172.75")));
    assert_eq!(plan.risk_per_lot, Some(dec("8172.75")));
    assert_eq!(plan.expected_cost, Some(dec("198.33")));
    assert_eq!(plan.cost_share, Some(dec("0.1539")));
    assert_eq!(plan.margin, Some(dec("130903.05")));
    assert_eq!(plan.detail["costs"]["gst"], "29.32");
    assert_eq!(plan.detail["costs"]["stt"], "6.01");
    let legs = legs_of(&mut tx, plan.id).await;
    let shape: Vec<(&str, &str, &str, Decimal)> = legs
        .iter()
        .map(|l| (l.1.as_str(), l.2.as_str(), l.3.as_str(), l.4))
        .collect();
    assert_eq!(
        shape,
        [
            ("LONG_PUT", "BUY", "NIFTY26102724700PE", dec("5.45")),
            ("LONG_CALL", "BUY", "NIFTY26102725300CE", dec("5.85")),
            ("SHORT_PUT", "SELL", "NIFTY26102724850PE", dec("23.75")),
            ("SHORT_CALL", "SELL", "NIFTY26102725150CE", dec("27.00")),
        ]
    );
    assert!(legs.iter().all(|l| l.5 == 65 && l.6 && l.7 == "PENDING"));
    let asked = asked.lock().unwrap().clone();
    assert_eq!(asked.iter().map(Vec::len).collect::<Vec<_>>(), [4, 3]);
    assert_eq!(asked[0][3].1, Side::Sell);
    let [alert] = alerts.as_slice() else { panic!() };
    assert_eq!(text(get(alert, &["alert"])), "OPTIONS_PLAN");
    assert_eq!(
        text(get(alert, &["runbook"])),
        "docs/runbooks/11-options-plan.md"
    );
    assert_eq!(text(get(alert, &["labels", "plan_id"])), plan.plan_id);
    for want in [
        "O1-M 2026-10-27 PAPER plan O1M-20261027-",
        "credit 39.65 pts (₹2577.25)",
        "1 lot(s) of 65",
        "max loss ₹7172.75",
        "round-trip costs ₹198.33",
        "margin ₹130903.05",
        "1. BUY 65 NIFTY26102724700PE @ 5.45",
        "4. SELL 65 NIFTY26102725150CE @ 27.00",
        "Expires 10:15",
    ] {
        assert!(summary(alert).contains(want), "{want}: {}", summary(alert));
    }
    assert_eq!(array(get(alert, &["detail", "legs"])).len(), 4);
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o1_idempotent_per_date() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_quiet(&mut tx).await;
    let (margin, asked) = calculator(o1_answer);
    let (first, _) = minute(PlanFamily::O1, &mut tx, user, o1_now(), Some(&margin)).await;
    let later = o1_now() + Duration::minutes(3);
    let (again, alerts) = minute(PlanFamily::O1, &mut tx, user, later, Some(&margin)).await;
    let (a, b) = (sleeve_report(&first, "O1M"), sleeve_report(&again, "O1M"));
    assert_eq!(get(a, &["plan_id"]), get(b, &["plan_id"]));
    assert_eq!(get(b, &["created"]), &OrderedJson::Bool(false));
    assert!(alerts.is_empty());
    assert_eq!(asked.lock().unwrap().len(), 2);
    assert_eq!(sessions(&mut tx, user, "O1M").await.len(), 1);
    assert_eq!(count_plans(&mut tx, user).await, 1);
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o1_a_slot_held_by_o3_skips_with_the_code() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_quiet(&mut tx).await;
    sqlx::query(
        "INSERT INTO op_session (user_id, sleeve, trade_date, mode, state, slot_holder) \
         VALUES ($1, 'O3B', $2, 'PAPER', 'CONFIRMED', 'O3B')",
    )
    .bind(user)
    .bind(quiet_day())
    .execute(&mut *tx)
    .await
    .unwrap();
    let (margin, asked) = calculator(o1_answer);
    let (_, alerts) = minute(PlanFamily::O1, &mut tx, user, o1_now(), Some(&margin)).await;
    let rows = sessions(&mut tx, user, "O1M").await;
    let [row] = rows.as_slice() else { panic!() };
    assert_eq!(
        (
            row.state.as_str(),
            row.skip_reasons.clone(),
            row.plan_id.clone()
        ),
        ("SKIPPED", vec!["REJECTED_SLOT_TAKEN".to_owned()], None)
    );
    assert!(asked.lock().unwrap().is_empty());
    let [alert] = alerts.as_slice() else { panic!() };
    assert!(summary(alert).contains("REJECTED_SLOT_TAKEN"));
    assert!(summary(alert).starts_with("O1-M 2026-10-27: no trade — "));
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o1_the_minute_lapses_an_expired_plan() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_quiet(&mut tx).await;
    let (margin, _) = calculator(o1_answer);
    let (first, alerts) = minute(PlanFamily::O1, &mut tx, user, o1_now(), Some(&margin)).await;
    assert_eq!(alerts.len(), 1);
    assert!(array(get(&first, &["lapsed"])).is_empty());
    let after = ist(2026, 10, 27, 10, 15, 40);
    let (second, alerts) = minute(PlanFamily::O1, &mut tx, user, after, None).await;
    assert!(alerts.is_empty());
    assert_eq!(array(get(&second, &["lapsed"])).len(), 1);
    let rows = sessions(&mut tx, user, "O1M").await;
    let [row] = rows.as_slice() else { panic!() };
    assert_eq!(row.state, "LAPSED");
    assert_eq!(plan_of(&mut tx, row.id).await.status, "LAPSED");
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o1_no_kite_session_is_margin_unknown_on_paper() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_quiet(&mut tx).await;
    let (payload, alerts) = minute(PlanFamily::O1, &mut tx, user, o1_now(), None).await;
    let report = sleeve_report(&payload, "O1M");
    assert_eq!(text(get(report, &["state"])), "PLANNED");
    assert_eq!(
        text(get(report, &["detail", "margin"])),
        "no Kite session: the margin calculator was not asked"
    );
    let rows = sessions(&mut tx, user, "O1M").await;
    let plan = plan_of(&mut tx, rows[0].id).await;
    assert_eq!(plan.margin, None);
    assert!(
        plan.detail["warnings"]
            .as_array()
            .unwrap()
            .iter()
            .any(|w| w == "MARGIN_UNKNOWN")
    );
    assert!(summary(&alerts[0]).contains("margin not known"));
    tx.rollback().await.unwrap();
}

// --- O2 ---------------------------------------------------------------------------------------

fn o2_now() -> DateTime<FixedOffset> {
    ist(2026, 10, 19, 10, 5, 45)
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o2_the_fixture_morning_writes_one_plan_to_the_rupee() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_o2(&mut tx, &f::o2_up_break_bars(), f::at(o2_day(), 10, 10)).await;
    let (payload, alerts) = minute(PlanFamily::O2, &mut tx, user, o2_now(), None).await;
    let report = sleeve_report(&payload, "O2");
    assert_eq!(text(get(report, &["state"])), "PLANNED");
    assert_eq!(get(report, &["created"]), &OrderedJson::Bool(true));
    let rows = sessions(&mut tx, user, "O2").await;
    let [row] = rows.as_slice() else { panic!() };
    assert_eq!(
        (
            row.state.as_str(),
            row.mode.as_str(),
            row.verdict.as_deref(),
            row.expiry
        ),
        ("PLANNED", "PAPER", Some("TRADE"), Some(f::d(2026, 10, 20)))
    );
    let plan = plan_of(&mut tx, row.id).await;
    assert_eq!(Some(&plan.plan_id), row.plan_id.as_ref());
    assert_eq!(
        (
            plan.status.as_str(),
            plan.kind.as_str(),
            plan.structure.as_str(),
            plan.sizing_mode.as_str()
        ),
        ("ISSUED", "ENTRY", "LONG_OPTION", "PAPER_ONE_LOT")
    );
    assert_eq!(
        plan.expires_at,
        (o2_now() + Duration::minutes(30)).with_timezone(&Utc)
    );
    assert_eq!((plan.credit, plan.width, plan.margin), (None, None, None));
    assert_eq!(
        (plan.debit, plan.lots, plan.lot_size),
        (Some(dec("203.75")), 1, 65)
    );
    assert_eq!(plan.max_loss, Some(dec("3973.13")));
    assert_eq!(plan.risk_per_lot, Some(dec("4273.13")));
    assert_eq!(plan.profit_target, Some(dec("7946.25")));
    assert_eq!(plan.expected_cost, Some(dec("78.34")));
    assert_eq!(plan.cost_share, Some(dec("0.0099")));
    assert_eq!(plan.detail["costs"]["stt"], "19.66");
    assert_eq!(plan.detail["costs"]["orders"], 2);
    assert_eq!(plan.detail["exits"]["stop_price"], "142.63");
    assert_eq!(plan.detail["exits"]["target_price"], "326.00");
    assert_eq!(plan.detail["gap_through_inr"], "13243.75");
    let legs = legs_of(&mut tx, plan.id).await;
    let [leg] = legs.as_slice() else { panic!() };
    assert_eq!(
        (leg.1.as_str(), leg.2.as_str(), leg.3.as_str(), leg.4),
        ("LONG_CALL", "BUY", "NIFTY26102025000CE", dec("203.75"))
    );
    assert!(leg.5 == 65 && leg.6 && leg.7 == "PENDING");
    let [alert] = alerts.as_slice() else { panic!() };
    for want in [
        "O2 2026-10-19 PAPER plan O2-20261019-",
        "BUY 65 NIFTY26102025000CE @ 203.75",
        "1 lot(s) of 65",
        "UP break of 25044.52 at 10:04",
        "premium ₹13243.75",
        "stop 142.63",
        "target 326.00",
        "time stop 45 min",
        "hard exit 15:00",
        "gap-through worst case ₹13243.75",
        "round-trip costs ₹78.34",
        "Expires 10:35",
    ] {
        assert!(summary(alert).contains(want), "{want}: {}", summary(alert));
    }
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o2_idempotent_per_date() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_o2(&mut tx, &f::o2_up_break_bars(), f::at(o2_day(), 10, 10)).await;
    let (first, _) = minute(PlanFamily::O2, &mut tx, user, o2_now(), None).await;
    let later = o2_now() + Duration::minutes(2);
    let (again, alerts) = minute(PlanFamily::O2, &mut tx, user, later, None).await;
    let (a, b) = (sleeve_report(&first, "O2"), sleeve_report(&again, "O2"));
    assert_eq!(get(a, &["plan_id"]), get(b, &["plan_id"]));
    assert_eq!(get(b, &["created"]), &OrderedJson::Bool(false));
    assert!(alerts.is_empty());
    assert_eq!(sessions(&mut tx, user, "O2").await.len(), 1);
    assert_eq!(count_plans(&mut tx, user).await, 1);
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o2_a_day_that_never_triggers_is_a_skipped_session() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_o2(&mut tx, &counter_bars(), f::at(o2_day(), 13, 35)).await;
    let (waiting, _) = minute(PlanFamily::O2, &mut tx, user, o2_now(), None).await;
    assert_eq!(
        text(get(sleeve_report(&waiting, "O2"), &["state"])),
        "NOT_READY"
    );
    assert!(sessions(&mut tx, user, "O2").await.is_empty());
    let after = ist(2026, 10, 19, 13, 33, 0);
    let (_, alerts) = minute(PlanFamily::O2, &mut tx, user, after, None).await;
    let rows = sessions(&mut tx, user, "O2").await;
    let [row] = rows.as_slice() else { panic!() };
    assert_eq!(
        (
            row.state.as_str(),
            row.skip_reasons.clone(),
            row.plan_id.clone()
        ),
        ("SKIPPED", vec!["NO_TRIGGER".to_owned()], None)
    );
    assert!(summary(&alerts[0]).contains("NO_TRIGGER"));
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o2_the_minute_lapses_an_expired_plan() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_o2(&mut tx, &f::o2_up_break_bars(), f::at(o2_day(), 10, 10)).await;
    let (first, alerts) = minute(PlanFamily::O2, &mut tx, user, o2_now(), None).await;
    assert_eq!(alerts.len(), 1);
    assert!(array(get(&first, &["lapsed"])).is_empty());
    let after = o2_now() + Duration::minutes(31);
    let (second, alerts) = minute(PlanFamily::O2, &mut tx, user, after, None).await;
    assert!(alerts.is_empty());
    assert_eq!(array(get(&second, &["lapsed"])).len(), 1);
    let rows = sessions(&mut tx, user, "O2").await;
    assert_eq!(rows[0].state, "LAPSED");
    assert_eq!(plan_of(&mut tx, rows[0].id).await.status, "LAPSED");
    tx.rollback().await.unwrap();
}

// --- O3 ---------------------------------------------------------------------------------------

fn o3_now() -> DateTime<FixedOffset> {
    ist(2026, 10, 13, 9, 46, 20)
}

fn o3a_now() -> DateTime<FixedOffset> {
    ist(2026, 10, 13, 10, 21, 20)
}

async fn seed_gap_hold(conn: &mut PgConnection) -> i64 {
    let day = o3_day();
    seed_o3(
        conn,
        &f::gap_hold_bars(),
        f::at(day, 9, 46),
        &[f::at(day, 9, 45)],
    )
    .await
}

async fn seed_both(conn: &mut PgConnection) -> i64 {
    let day = o3_day();
    seed_o3(
        conn,
        &both_bars(),
        f::at(day, 10, 21),
        &[f::at(day, 9, 45), f::at(day, 10, 20)],
    )
    .await
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o3_the_gap_hold_expiry_writes_one_spread_to_the_rupee() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_gap_hold(&mut tx).await;
    let (margin, asked) = calculator(o3_answer);
    let (payload, alerts) = minute(PlanFamily::O3, &mut tx, user, o3_now(), Some(&margin)).await;
    let order: Vec<&str> = array(get(&payload, &["sleeves"]))
        .iter()
        .map(|r| text(get(r, &["sleeve"])))
        .collect();
    assert_eq!(order, ["O3B", "O3A"]);
    let report = sleeve_report(&payload, "O3B");
    assert_eq!(text(get(report, &["state"])), "PLANNED");
    assert_eq!(text(get(report, &["detail", "margin"])), "asked");
    let rows = sessions(&mut tx, user, "O3B").await;
    let [row] = rows.as_slice() else { panic!() };
    assert_eq!(
        (
            row.state.as_str(),
            row.mode.as_str(),
            row.verdict.as_deref(),
            row.expiry
        ),
        ("PLANNED", "PAPER", Some("TRADE"), Some(o3_day()))
    );
    let plan = plan_of(&mut tx, row.id).await;
    assert_eq!(Some(&plan.plan_id), row.plan_id.as_ref());
    assert_eq!(
        (
            plan.status.as_str(),
            plan.kind.as_str(),
            plan.structure.as_str(),
            plan.sizing_mode.as_str()
        ),
        ("ISSUED", "ENTRY", "DEBIT_SPREAD", "PAPER_ONE_LOT")
    );
    assert_eq!(plan.expires_at, f::at(o3_day(), 10, 0).with_timezone(&Utc));
    assert_eq!(
        (plan.debit, plan.width, plan.credit),
        (Some(dec("26.95")), Some(dec("100.00")), None)
    );
    assert_eq!((plan.lots, plan.lot_size), (1, 65));
    assert_eq!(plan.max_loss, Some(dec("1751.75")));
    assert_eq!(plan.risk_per_lot, Some(dec("2251.75")));
    assert_eq!(plan.profit_target, Some(dec("3448.25")));
    assert_eq!(plan.stop, Some(dec("875.88")));
    assert_eq!(plan.margin, Some(dec("24812.50")));
    let exits = &plan.detail["exits"];
    assert_eq!(
        (
            &exits["target_value"],
            &exits["stop_value"],
            &exits["invalidation_level"]
        ),
        (
            &serde_json::json!("80.00"),
            &serde_json::json!("13.48"),
            &serde_json::json!("25100.00")
        )
    );
    let legs = legs_of(&mut tx, plan.id).await;
    let shape: Vec<(i16, &str, &str, &str)> = legs
        .iter()
        .map(|l| (l.0, l.1.as_str(), l.2.as_str(), l.3.as_str()))
        .collect();
    assert_eq!(
        shape,
        [
            (1, "LONG_CALL", "BUY", "NIFTY26101325200CE"),
            (2, "SHORT_CALL", "SELL", "NIFTY26101325300CE"),
        ]
    );
    assert!(legs.iter().all(|l| l.5 == 65 && l.6));
    assert_eq!(
        *asked.lock().unwrap(),
        vec![vec![
            ("NIFTY26101325200CE".to_owned(), Side::Buy, 65),
            ("NIFTY26101325300CE".to_owned(), Side::Sell, 65),
        ]]
    );
    let alert = alerts
        .iter()
        .find(|a| text(get(a, &["labels", "sleeve"])) == "O3B")
        .unwrap();
    for want in [
        "O3B 2026-10-13 PAPER plan O3B-20261013-",
        "UP debit spread 25200/25300 CE",
        "BUY NIFTY26101325200CE first, then SELL NIFTY26101325300CE",
        "1 lot(s) of 65",
        "debit 26.95 of a 100-point width = max loss ₹1751.75",
        "target value 80.00 (₹3448.25)",
        "stop value 13.48 (₹875.88)",
        "invalidation at 25100",
        "hard exit 14:45",
        "margin ₹24812.50",
        "Expires 10:00",
    ] {
        assert!(summary(alert).contains(want), "{want}: {}", summary(alert));
    }
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o3_a_failing_calculator_is_a_warning_on_paper() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_gap_hold(&mut tx).await;
    let (margin, _) = calculator(o3_down);
    let (payload, _) = minute(PlanFamily::O3, &mut tx, user, o3_now(), Some(&margin)).await;
    let report = sleeve_report(&payload, "O3B");
    assert_eq!(text(get(report, &["state"])), "PLANNED");
    assert!(text(get(report, &["detail", "margin"])).starts_with("margin calculator failed"));
    let rows = sessions(&mut tx, user, "O3B").await;
    let plan = plan_of(&mut tx, rows[0].id).await;
    assert_eq!(plan.margin, None);
    assert!(
        plan.detail["warnings"]
            .as_array()
            .unwrap()
            .iter()
            .any(|w| w == "MARGIN_UNKNOWN")
    );
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o3_one_o3_a_day_on_the_database() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_both(&mut tx).await;
    minute(PlanFamily::O3, &mut tx, user, o3_now(), None).await;
    sqlx::query("UPDATE op_session SET state = 'CONFIRMED' WHERE user_id = $1 AND sleeve = 'O3B'")
        .bind(user)
        .execute(&mut *tx)
        .await
        .unwrap();
    sqlx::query("UPDATE op_plan SET status = 'CONFIRMED' WHERE user_id = $1")
        .bind(user)
        .execute(&mut *tx)
        .await
        .unwrap();
    let (second, alerts) = minute(PlanFamily::O3, &mut tx, user, o3a_now(), None).await;
    let o3b = sessions(&mut tx, user, "O3B").await;
    let o3a = sessions(&mut tx, user, "O3A").await;
    assert_eq!(o3b[0].state, "CONFIRMED");
    assert_eq!(
        (o3a[0].state.as_str(), o3a[0].skip_reasons.clone()),
        (
            "SKIPPED",
            vec!["REJECTED_SLOT_TAKEN".to_owned(), "O3B_HOLDS".to_owned()]
        )
    );
    let [alert] = alerts.as_slice() else { panic!() };
    assert!(summary(alert).contains("O3B_HOLDS"));
    assert!(array(get(&second, &["lapsed"])).is_empty());
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o3_idempotent_per_date() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_gap_hold(&mut tx).await;
    let (first, _) = minute(PlanFamily::O3, &mut tx, user, o3_now(), None).await;
    let later = o3_now() + Duration::minutes(2);
    let (again, alerts) = minute(PlanFamily::O3, &mut tx, user, later, None).await;
    let (a, b) = (sleeve_report(&first, "O3B"), sleeve_report(&again, "O3B"));
    assert_eq!(get(a, &["plan_id"]), get(b, &["plan_id"]));
    assert_eq!(get(b, &["created"]), &OrderedJson::Bool(false));
    assert!(alerts.is_empty());
    assert_eq!(sessions(&mut tx, user, "O3B").await.len(), 1);
    assert_eq!(count_plans(&mut tx, user).await, 1);
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o3_the_minute_lapses_an_expired_plan() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_gap_hold(&mut tx).await;
    let (first, alerts) = minute(PlanFamily::O3, &mut tx, user, o3_now(), None).await;
    assert_eq!(alerts.len(), 1);
    assert!(array(get(&first, &["lapsed"])).is_empty());
    let after = f::at(o3_day(), 10, 0) + Duration::seconds(30);
    let (second, _) = minute(PlanFamily::O3, &mut tx, user, after, None).await;
    assert_eq!(array(get(&second, &["lapsed"])).len(), 1);
    assert_eq!(sessions(&mut tx, user, "O3B").await[0].state, "LAPSED");
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o3_a_non_expiry_day_writes_nothing() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_gap_hold(&mut tx).await;
    let monday = ist(2026, 10, 12, 9, 46, 20);
    let (payload, alerts) = minute(PlanFamily::O3, &mut tx, user, monday, None).await;
    assert!(alerts.is_empty());
    let states: Vec<&str> = array(get(&payload, &["sleeves"]))
        .iter()
        .map(|r| text(get(r, &["state"])))
        .collect();
    assert_eq!(states, ["NO_SESSION", "NO_SESSION"]);
    assert!(sessions(&mut tx, user, "O3B").await.is_empty());
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn o3_an_unconfirmed_o3b_lapses_and_frees_o3a() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user = seed_both(&mut tx).await;
    minute(PlanFamily::O3, &mut tx, user, o3_now(), None).await;
    minute(PlanFamily::O3, &mut tx, user, o3a_now(), None).await;
    let o3b = sessions(&mut tx, user, "O3B").await;
    let o3a = sessions(&mut tx, user, "O3A").await;
    assert_eq!(
        (o3b[0].state.as_str(), o3a[0].state.as_str()),
        ("LAPSED", "PLANNED")
    );
    tx.rollback().await.unwrap();
}

// --- the bed's own recorded day ---------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPTIONS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn the_beds_recorded_day_scans_and_rebuilds_its_sessions() {
    let _g = LOCK.lock().await;
    let mut tx = db().begin().await.unwrap();
    let user: i64 = sqlx::query_scalar("SELECT min(user_id) FROM op_session")
        .fetch_one(&mut *tx)
        .await
        .unwrap();
    let now = ist(2026, 10, 29, 13, 15, 20);
    let report = scan_minute(&mut tx, user, now, &WorkerSettings::default())
        .await
        .unwrap();
    assert_eq!(get(&report, &["written"]), &OrderedJson::Int(5));
    let stamped: Vec<&str> = array(get(&report, &["snapshots"]))
        .iter()
        .map(text)
        .collect();
    assert!(stamped.iter().all(|s| !s.ends_with("none")), "{stamped:?}");
    let (kept, alerts) = minute(
        PlanFamily::O2,
        &mut tx,
        user,
        ist(2026, 10, 29, 13, 33, 0),
        None,
    )
    .await;
    assert!(alerts.is_empty());
    assert_eq!(
        get(sleeve_report(&kept, "O2"), &["created"]),
        &OrderedJson::Bool(false)
    );
    sqlx::query("DELETE FROM op_session WHERE user_id = $1 AND trade_date = '2026-10-29'")
        .bind(user)
        .execute(&mut *tx)
        .await
        .unwrap();
    let (rebuilt, alerts) = minute(
        PlanFamily::O2,
        &mut tx,
        user,
        ist(2026, 10, 29, 13, 33, 0),
        None,
    )
    .await;
    let o2 = sleeve_report(&rebuilt, "O2");
    assert_eq!(get(o2, &["created"]), &OrderedJson::Bool(true));
    assert_eq!(alerts.len(), 1);
    let rows = sessions(&mut tx, user, "O2").await;
    assert!(rows.iter().any(|r| r.state == text(get(o2, &["state"]))));
    tx.rollback().await.unwrap();
}
