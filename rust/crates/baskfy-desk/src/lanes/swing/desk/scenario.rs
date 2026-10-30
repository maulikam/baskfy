//! `tests/test_swing_desk.py`'s fixtures, ported: the `Scenario` builder over a per-test SQLite
//! file built from the same DDL (`sw_ddl.sql`, 0028_swing.py in SQLite's spelling), and the
//! `client` / `real_client` fixtures as an in-process app over that file.
//!
//! **What is written.** Every value is the text Python's `sqlite3` binds for the same call: a
//! price is the string the test passes (NUMERIC affinity turns it into a REAL, as it does for
//! Python), a bool is 0/1, a date `isoformat()`, a stamp `datetime.isoformat()`. `uuid4()` is a
//! deterministic v4-shaped id per scenario, so a failing assertion names the same plan twice.
//!
//! **The two clients.** Python's `client` monkeypatches the gateway, the last price and the order
//! source; `real_client` builds the real dry-run gateway over a broker that explodes on contact.
//! The Rust desk has no attribute to patch, so both are the same app: the real routes, the real
//! `execute_line`, the real swing gateway in DRY_RUN, over the **replay Kite**, which answers the
//! scenario's `LAST_PRICES` as quotes, records every call, and is asserted to have recorded no
//! order or GTT call — the replay twin of `ExplodingKC` (DECISIONS-RS R8.3).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::Router;
use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, TimeZone};
use tower::ServiceExt as _;

use super::super::store::PgSwingStore;
use crate::app::{AppState, DeskPaths, StateParts, build_app, build_state};
use crate::clock::{FixedClock, ist};
use crate::fake_kite::ReplayKite;
use crate::ids::HarnessIds;
use crate::settings::Settings;
use crate::storage::{Conn, Db, DbConfig, Param, Row};

/// `USER`.
pub const USER: i64 = 1;

/// `TODAY`: a Wednesday.
pub fn today() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 2).unwrap()
}

/// An IST stamp on a given day.
pub fn ist_at(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(y, mo, d, h, mi, 0).unwrap()
}

/// `NOW`: 09:40 on `TODAY`.
pub fn now() -> DateTime<FixedOffset> {
    ist_at(2026, 9, 2, 9, 40)
}

/// `AFTER_CLOSE`: one minute past `monitor_close`, read from the config (SW26 moved it).
pub fn after_close() -> DateTime<FixedOffset> {
    let close = baskfy_core::swing::setups::SwingConfig::default()
        .opening_range
        .monitor_close;
    let at = ist_at(
        2026,
        9,
        2,
        u32::try_from(close.0).unwrap(),
        u32::try_from(close.1).unwrap(),
    );
    at + Duration::minutes(1)
}

/// `LAST_PRICES`: what the broker quotes when the route asks for a last price.
pub const LAST_PRICES: [(&str, i64, f64); 3] = [
    ("DELTAHELD", 444, 104.0),
    ("BETAEP", 222, 215.0),
    ("ALPHAFLAG", 111, 100.8),
];

/// `tests/test_swing_desk.py`'s DDL (0028_swing.py in SQLite's spelling), dumped verbatim.
pub const SW_DDL: &str = include_str!("sw_ddl.sql");

/// Python's `datetime.isoformat()` for an aware stamp.
pub fn iso(t: DateTime<FixedOffset>) -> String {
    super::super::values::iso_dt(&t)
}

/// `date.isoformat()`.
pub fn iso_d(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

fn txt(s: &str) -> Param {
    Param::Text(s.to_owned())
}

fn opt(s: Option<&str>) -> Param {
    s.map_or(Param::Null, txt)
}

/// The ids `Scenario.morning()` returns.
#[derive(Debug, Clone)]
pub struct Morning {
    /// The armed DELTAHELD position.
    pub held: i64,
    /// The naked BETAEP position.
    pub naked: i64,
    /// (pk, plan_id) of the MORNING plan.
    pub morning: (i64, String),
    /// The morning SELL.
    pub sell: i64,
    /// The morning RAISE.
    pub raise: i64,
    /// The waiting ALPHAFLAG buy.
    pub waiting: i64,
    /// The waiting BETAEP buy (held).
    pub waiting_held: i64,
    /// (pk, plan_id) of the 09:31 SIGNAL plan.
    pub signal_plan: (i64, String),
    /// The 09:31 trigger's line.
    pub trigger_line: i64,
    /// The 09:31 TRIGGERED signal.
    pub trigger: i64,
    /// The LOCKED_UPPER_CIRCUIT signal.
    pub locked: i64,
    /// The skipped EPSILON signal.
    pub skipped: i64,
}

/// A line to write (`Scenario.line`'s keyword arguments, with Python's defaults).
#[derive(Debug, Clone)]
pub struct LineSpec<'a> {
    /// `kind`.
    pub kind: &'a str,
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: &'a str,
    /// `quantity`.
    pub quantity: i64,
    /// `trigger`.
    pub trigger: Option<&'a str>,
    /// `stop`.
    pub stop: Option<&'a str>,
    /// `state`.
    pub state: &'a str,
    /// `note`.
    pub note: Option<&'a str>,
    /// `setup`.
    pub setup: Option<&'a str>,
}

impl<'a> LineSpec<'a> {
    /// `scenario.line(..., kind=, instrument_id=, symbol=)` with every other default.
    pub fn new(kind: &'a str, instrument_id: i64, symbol: &'a str) -> Self {
        Self {
            kind,
            instrument_id,
            symbol,
            quantity: 100,
            trigger: Some("100.80"),
            stop: Some("97.80"),
            state: "PROPOSED",
            note: None,
            setup: Some("FLAG"),
        }
    }
}

/// A position to write (`Scenario.position`'s keyword arguments).
#[derive(Debug, Clone)]
pub struct PositionSpec<'a> {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `entry_avg`.
    pub entry_avg: &'a str,
    /// `quantity`.
    pub quantity: i64,
    /// `quantity_open`.
    pub quantity_open: Option<i64>,
    /// `stop`.
    pub stop: &'a str,
    /// `gtt_id`.
    pub gtt_id: Option<&'a str>,
    /// `state`.
    pub state: &'a str,
    /// `simulated`.
    pub simulated: bool,
    /// `initial_stop`.
    pub initial_stop: Option<&'a str>,
}

impl<'a> PositionSpec<'a> {
    /// `scenario.position(instrument_id=)` with every other default.
    pub fn new(instrument_id: i64) -> Self {
        Self {
            instrument_id,
            entry_avg: "100.80",
            quantity: 300,
            quantity_open: None,
            stop: "97.80",
            gtt_id: Some("G1"),
            state: "OPEN",
            simulated: true,
            initial_stop: None,
        }
    }
}

/// A signal to write (`Scenario.signal`'s keyword arguments).
#[derive(Debug, Clone)]
pub struct SignalSpec<'a> {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `setup`.
    pub setup: &'a str,
    /// `state`.
    pub state: &'a str,
    /// `raised_at`.
    pub raised_at: DateTime<FixedOffset>,
    /// `range_high`.
    pub range_high: &'a str,
    /// `last_price`.
    pub last_price: &'a str,
    /// `entry`.
    pub entry: Option<&'a str>,
    /// `stop`.
    pub stop: Option<&'a str>,
    /// `plan_line_id`.
    pub plan_line_id: Option<i64>,
}

impl<'a> SignalSpec<'a> {
    /// `scenario.signal(instrument_id=, setup=, state=, raised_at=)` with every other default.
    pub fn new(
        instrument_id: i64,
        setup: &'a str,
        state: &'a str,
        raised_at: DateTime<FixedOffset>,
    ) -> Self {
        Self {
            instrument_id,
            setup,
            state,
            raised_at,
            range_high: "100.50",
            last_price: "100.80",
            entry: Some("100.80"),
            stop: Some("97.80"),
            plan_line_id: None,
        }
    }
}

/// `Scenario`: the per-test SQLite file and the connection the fixtures write through.
pub struct Scenario {
    /// The directory holding the file (and the app's working directory).
    pub dir: tempfile::TempDir,
    /// `swing.db`.
    pub path: PathBuf,
    /// The fixture's own connection (`scenario.conn`).
    pub conn: Conn,
    uuids: u64,
}

/// `uuid4()`, deterministic.
fn uuid_n(n: u64) -> String {
    format!("{n:08x}-5eed-4000-8000-{n:012x}")
}

impl Scenario {
    /// `Scenario(path)`: the DDL and the five instruments.
    pub async fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("swing.db");
        let mut conn = Db::connect_sqlite_path(&path).await.unwrap();
        for ddl in SW_DDL.split(";\n").filter(|s| !s.trim().is_empty()) {
            conn.execute(ddl, &[]).await.unwrap();
        }
        conn.execute(
            "INSERT INTO instrument(id, symbol, name, kite_token) VALUES \
             (1,'ALPHAFLAG','Alpha Flag Ltd',111),(2,'BETAEP','Beta EP Ltd',222),\
             (3,'GAMMALOCK','Gamma Lock Ltd',333),(4,'DELTAHELD','Delta Held Ltd',444),\
             (5,'EPSILON','Epsilon Ltd',555)",
            &[],
        )
        .await
        .unwrap();
        Self {
            dir,
            path,
            conn,
            uuids: 0,
        }
    }

    /// `scenario.conn.execute(sql, params)`.
    pub async fn exec(&mut self, sql: &str, params: &[Param]) -> Vec<Row> {
        self.conn.execute(sql, params).await.unwrap().rows
    }

    /// `scenario.store()`: a store over its own connection to the file.
    pub async fn store(&self) -> PgSwingStore {
        self.store_for(USER).await
    }

    /// `PgSwingStore(_connect(path), user_id=user, schema="")`.
    pub async fn store_for(&self, user: i64) -> PgSwingStore {
        let conn = Db::connect_sqlite_path(&self.path).await.unwrap();
        PgSwingStore {
            conn,
            user_id: user,
            schema: String::new(),
            broker_account_id: (user == USER).then_some(1),
            live_level: None,
            settings: Settings::defaults().unwrap(),
            clock: Arc::new(FixedClock::new(now())),
        }
    }

    /// `scenario.config(capital, risk, first_live, market=, detected=)`.
    pub async fn config_with(
        &mut self,
        capital: &str,
        risk: &str,
        first_live: i64,
        market: bool,
        detected: bool,
    ) {
        self.exec(
            "INSERT INTO sw_config(user_id, sleeve_capital_inr, risk_per_trade_pct, \
             first_live_sessions_left) VALUES (?, ?, ?, ?)",
            &[
                Param::Int(USER),
                txt(capital),
                txt(risk),
                Param::Int(first_live),
            ],
        )
        .await;
        if market {
            self.market(MarketSpec::default()).await;
        }
        if detected {
            for (instrument_id, setup) in [
                (1, "FLAG"),
                (2, "EP"),
                (3, "FLAG"),
                (4, "FLAG"),
                (5, "FLAG"),
            ] {
                self.detected(instrument_id, setup).await;
            }
        }
    }

    /// `scenario.config()`.
    pub async fn config(&mut self) {
        self.config_with("1000000", "0.500", 5, true, true).await;
    }

    /// `scenario.market(...)`.
    pub async fn market(&mut self, m: MarketSpec) {
        self.exec(
            "INSERT INTO sw_market_daily(user_id, date, constituent_count, gate, exposure_level, \
             max_open_positions, max_exposure_pct, new_entries_allowed, drawdown_locked) \
             VALUES (?, ?, 40, ?, ?, ?, ?, ?, ?)",
            &[
                Param::Int(USER),
                txt(&iso_d(m.on)),
                txt(m.gate),
                Param::Int(m.rung),
                Param::Int(m.max_open_positions),
                txt(m.max_exposure_pct),
                Param::Bool(m.new_entries_allowed),
                Param::Bool(m.drawdown_locked),
            ],
        )
        .await;
    }

    /// `scenario.detected(instrument_id, setup=)` with the default ADR, turnover and score.
    pub async fn detected(&mut self, instrument_id: i64, setup: &str) {
        self.exec(
            "INSERT INTO sw_setup_daily(user_id, date, instrument_id, setup, status, score, \
             adr_pct, turnover_avg) VALUES (?, ?, ?, ?, 'SETTING_UP', ?, ?, ?)",
            &[
                Param::Int(USER),
                txt(&iso_d(today() - Duration::days(1))),
                Param::Int(instrument_id),
                txt(setup),
                txt("72.00"),
                txt("5.00"),
                Param::Int(100_000_000),
            ],
        )
        .await;
    }

    /// `scenario.plan(source=, built_at=, as_of=, gate=, ttl_minutes=)`.
    pub async fn plan_full(
        &mut self,
        source: &str,
        built_at: DateTime<FixedOffset>,
        as_of: NaiveDate,
        ttl_minutes: i64,
    ) -> (i64, String) {
        self.uuids += 1;
        let plan_id = uuid_n(self.uuids);
        let rows = self
            .exec(
                "INSERT INTO sw_plan(plan_id, user_id, as_of, source, built_at, expires_at, \
                 plan_hash, gate, exposure_level, total_risk_inr, total_new_exposure_inr) \
                 VALUES (?, ?, ?, ?, ?, ?, 'h', ?, 2, '5000.00', '150000.00') RETURNING id",
                &[
                    txt(&plan_id),
                    Param::Int(USER),
                    txt(&iso_d(as_of)),
                    txt(source),
                    txt(&iso(built_at)),
                    txt(&iso(built_at + Duration::minutes(ttl_minutes))),
                    txt("GREEN"),
                ],
            )
            .await;
        (int_of(&rows[0], "id"), plan_id)
    }

    /// `scenario.plan(source=, built_at=)` dated `TODAY`.
    pub async fn plan(&mut self, source: &str, built_at: DateTime<FixedOffset>) -> (i64, String) {
        self.plan_full(source, built_at, today(), 30).await
    }

    /// `scenario.line(plan_pk, plan_id, ...)`.
    pub async fn line(&mut self, plan_pk: i64, plan_id: &str, l: LineSpec<'_>) -> i64 {
        let rows = self
            .exec(
                "INSERT INTO sw_plan_line(plan_id, user_id, kind, instrument_id, setup, quantity, \
                 trigger, stop, risk_inr, position_value, trail, note, state, client_id) \
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, '300.00', '10080.00', 'MA10', ?, ?, ?) RETURNING id",
                &[
                    Param::Int(plan_pk),
                    Param::Int(USER),
                    txt(l.kind),
                    Param::Int(l.instrument_id),
                    opt(l.setup),
                    Param::Int(l.quantity),
                    opt(l.trigger),
                    opt(l.stop),
                    opt(l.note),
                    txt(l.state),
                    txt(&format!("{plan_id}:{}:{}", l.symbol, l.kind)),
                ],
            )
            .await;
        int_of(&rows[0], "id")
    }

    /// `scenario.skip(plan_pk, instrument_id=, symbol=, reason=, detail=)`.
    pub async fn skip(
        &mut self,
        plan_pk: i64,
        instrument_id: i64,
        symbol: &str,
        reason: &str,
        detail: Option<&str>,
    ) {
        self.exec(
            "INSERT INTO sw_plan_skip(plan_id, user_id, instrument_id, symbol, reason, detail) \
             VALUES (?, ?, ?, ?, ?, ?)",
            &[
                Param::Int(plan_pk),
                Param::Int(USER),
                Param::Int(instrument_id),
                txt(symbol),
                txt(reason),
                opt(detail),
            ],
        )
        .await;
    }

    /// `scenario.signal(...)`.
    pub async fn signal(&mut self, s: SignalSpec<'_>) -> i64 {
        let rows = self
            .exec(
                "INSERT INTO sw_signal(user_id, instrument_id, setup, session_date, raised_at, state, \
                 or_window_minutes, range_high, range_low, low_of_day, last_price, entry, stop, \
                 plan_line_id) VALUES (?, ?, ?, ?, ?, ?, 5, ?, '98.00', '97.80', ?, ?, ?, ?) \
                 RETURNING id",
                &[
                    Param::Int(USER),
                    Param::Int(s.instrument_id),
                    txt(s.setup),
                    txt(&iso_d(today())),
                    txt(&iso(s.raised_at)),
                    txt(s.state),
                    txt(s.range_high),
                    txt(s.last_price),
                    opt(s.entry),
                    opt(s.stop),
                    s.plan_line_id.map_or(Param::Null, Param::Int),
                ],
            )
            .await;
        int_of(&rows[0], "id")
    }

    /// `scenario.position(...)`.
    pub async fn position(&mut self, p: PositionSpec<'_>) -> i64 {
        let armed = p.gtt_id.is_some();
        let rows = self
            .exec(
                "INSERT INTO sw_position(user_id, broker_account_id, instrument_id, setup, entry_date, \
                 entry_avg, quantity_entered, initial_stop, stop, gtt_id, gtt_trigger, gtt_armed_at, \
                 trail, quantity_open, state, simulated) VALUES (?, 1, ?, 'FLAG', ?, ?, ?, ?, ?, \
                 ?, ?, ?, 'MA10', ?, ?, ?) RETURNING id",
                &[
                    Param::Int(USER),
                    Param::Int(p.instrument_id),
                    txt(&iso_d(today() - Duration::days(3))),
                    txt(p.entry_avg),
                    Param::Int(p.quantity),
                    txt(p.initial_stop.unwrap_or(p.stop)),
                    txt(p.stop),
                    opt(p.gtt_id),
                    if armed { txt(p.stop) } else { Param::Null },
                    if armed {
                        txt(&iso(now() - Duration::days(3)))
                    } else {
                        Param::Null
                    },
                    Param::Int(p.quantity_open.unwrap_or(p.quantity)),
                    txt(p.state),
                    Param::Bool(p.simulated),
                ],
            )
            .await;
        int_of(&rows[0], "id")
    }

    /// `scenario.session(**counters)`.
    pub async fn session(&mut self, monitor_ran: bool, signals: i64, confirms: i64, fills: i64) {
        self.exec(
            "INSERT INTO sw_session(user_id, session_date, mode, monitor_ran, signals, confirms, \
             fills, manage_actions) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            &[
                Param::Int(USER),
                txt(&iso_d(today())),
                txt("DRY_RUN"),
                Param::Bool(monitor_ran),
                Param::Int(signals),
                Param::Int(confirms),
                Param::Int(fills),
                Param::Int(0),
            ],
        )
        .await;
    }

    /// `scenario.morning()`: the desk as the page finds it at 09:40.
    pub async fn morning(&mut self) -> Morning {
        self.config().await;
        let held = self
            .position(PositionSpec {
                gtt_id: Some("G-44"),
                ..PositionSpec::new(4)
            })
            .await;
        let naked = self
            .position(PositionSpec {
                gtt_id: None,
                entry_avg: "210.50",
                stop: "204.50",
                ..PositionSpec::new(2)
            })
            .await;
        let (preview_pk, preview_id) = self
            .plan_full(
                "EOD_PREVIEW",
                ist_at(2026, 9, 1, 21, 5),
                NaiveDate::from_ymd_opt(2026, 9, 1).unwrap(),
                30,
            )
            .await;
        let sell_spec = LineSpec {
            quantity: 100,
            trigger: None,
            stop: None,
            note: Some("partial into strength, day 3"),
            ..LineSpec::new("SELL_AT_OPEN", 4, "DELTAHELD")
        };
        self.line(preview_pk, &preview_id, sell_spec.clone()).await;
        let morning = self.plan("MORNING", ist_at(2026, 9, 2, 9, 12)).await;
        let sell = self.line(morning.0, &morning.1, sell_spec).await;
        let raise = self
            .line(
                morning.0,
                &morning.1,
                LineSpec {
                    quantity: 0,
                    trigger: None,
                    stop: Some("100.80"),
                    note: Some("breakeven after partial"),
                    ..LineSpec::new("RAISE_GTT_STOP", 4, "DELTAHELD")
                },
            )
            .await;
        let waiting = self
            .line(
                morning.0,
                &morning.1,
                LineSpec {
                    quantity: 1666,
                    ..LineSpec::new("BUY_ON_TRIGGER", 1, "ALPHAFLAG")
                },
            )
            .await;
        let waiting_held = self
            .line(
                morning.0,
                &morning.1,
                LineSpec {
                    quantity: 200,
                    trigger: Some("212.00"),
                    stop: Some("204.50"),
                    setup: Some("EP"),
                    ..LineSpec::new("BUY_ON_TRIGGER", 2, "BETAEP")
                },
            )
            .await;
        self.skip(
            morning.0,
            3,
            "GAMMALOCK",
            "LOCKED_UPPER_CIRCUIT",
            Some("upper circuit at 52.50"),
        )
        .await;
        let signal_plan = self.plan("SIGNAL", ist_at(2026, 9, 2, 9, 31)).await;
        let trigger_line = self
            .line(
                signal_plan.0,
                &signal_plan.1,
                LineSpec {
                    quantity: 1666,
                    ..LineSpec::new("BUY_ON_TRIGGER", 1, "ALPHAFLAG")
                },
            )
            .await;
        let trigger = self
            .signal(SignalSpec {
                plan_line_id: Some(trigger_line),
                ..SignalSpec::new(1, "FLAG", "TRIGGERED", ist_at(2026, 9, 2, 9, 31))
            })
            .await;
        let locked = self
            .signal(SignalSpec {
                range_high: "52.50",
                last_price: "52.50",
                entry: None,
                stop: None,
                ..SignalSpec::new(3, "FLAG", "LOCKED_UPPER_CIRCUIT", ist_at(2026, 9, 2, 9, 20))
            })
            .await;
        let (skipped_pk, _) = self.plan("SIGNAL", ist_at(2026, 9, 2, 9, 35)).await;
        self.skip(
            skipped_pk,
            5,
            "EPSILON",
            "TIER_FULL",
            Some("2 of 2 positions open"),
        )
        .await;
        let skipped = self
            .signal(SignalSpec {
                range_high: "300.00",
                last_price: "300.50",
                entry: Some("300.50"),
                stop: Some("295.00"),
                ..SignalSpec::new(5, "FLAG", "TRIGGERED", ist_at(2026, 9, 2, 9, 35))
            })
            .await;
        self.session(false, 3, 1, 1).await;
        Morning {
            held,
            naked,
            morning,
            sell,
            raise,
            waiting,
            waiting_held,
            signal_plan,
            trigger_line,
            trigger,
            locked,
            skipped,
        }
    }
}

/// `scenario.market(...)`'s keyword arguments, with Python's defaults.
#[derive(Debug, Clone)]
pub struct MarketSpec {
    /// `on`.
    pub on: NaiveDate,
    /// `gate`.
    pub gate: &'static str,
    /// `rung`.
    pub rung: i64,
    /// `max_open_positions`.
    pub max_open_positions: i64,
    /// `max_exposure_pct`.
    pub max_exposure_pct: &'static str,
    /// `new_entries_allowed`.
    pub new_entries_allowed: bool,
    /// `drawdown_locked`.
    pub drawdown_locked: bool,
}

impl Default for MarketSpec {
    fn default() -> Self {
        Self {
            on: today() - Duration::days(1),
            gate: "GREEN",
            rung: 2,
            max_open_positions: 6,
            max_exposure_pct: "75.00",
            new_entries_allowed: true,
            drawdown_locked: false,
        }
    }
}

/// An integer column of a fixture row.
pub fn int_of(row: &Row, key: &str) -> i64 {
    super::super::values::int(row.get(key))
}

// =============================================================================================
// The app over the scenario's file
// =============================================================================================

/// The kite fixture: `LAST_PRICES` as quotes, and the order history the reconcile reads.
fn kite_fixture(dir: &Path, prices: &[(&str, i64, f64)], orders: &serde_json::Value) {
    let mut quotes = serde_json::Map::new();
    for &(symbol, token, price) in prices {
        quotes.insert(
            format!("NSE:{symbol}"),
            serde_json::json!({"instrument_token": token, "last_price": price}),
        );
    }
    let fixture = serde_json::json!({
        "session": {"authed": true},
        "routes": {
            "order.info": {"by_url_arg": "order_id", "answers": orders},
        },
        "quotes": quotes,
    });
    std::fs::write(
        dir.join("kite.json"),
        serde_json::to_vec_pretty(&fixture).unwrap(),
    )
    .unwrap();
}

/// The mounted desk over a scenario: `client` / `real_client`.
pub struct Client {
    /// The router.
    pub app: Router,
    /// Its state (the settings are mutable: `monkeypatch.setattr(C, ...)`).
    pub state: AppState,
    /// The clock (`monkeypatch.setattr(swing_desk, "_now", ...)`).
    pub clock: Arc<FixedClock>,
    /// The replay Kite — every call it was asked, none of them sent.
    pub kite: Arc<ReplayKite>,
    _kite_dir: tempfile::TempDir,
}

/// A response: status, headers, body text.
pub struct Reply {
    /// The status.
    pub status: StatusCode,
    /// The headers.
    pub headers: axum::http::HeaderMap,
    /// The body.
    pub text: String,
}

impl Reply {
    /// `r.json()`.
    pub fn json(&self) -> serde_json::Value {
        serde_json::from_str(&self.text).unwrap()
    }
}

/// The desk's templates and static files in this checkout.
pub fn desk_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../kite-momentum-rebalancer")
}

impl Client {
    /// The desk over `scenario`'s file at `NOW`, in DRY_RUN with the swing flags off, a Kite
    /// that answers `LAST_PRICES`, and no token on disk.
    pub async fn new(scenario: &Scenario) -> Self {
        Self::build(scenario, &LAST_PRICES, &serde_json::json!({})).await
    }

    /// The same, with recorded `order_history` answers by order id.
    pub async fn with_orders(scenario: &Scenario, orders: &serde_json::Value) -> Self {
        Self::build(scenario, &LAST_PRICES, orders).await
    }

    /// The same, quoting only `prices`.
    pub async fn with_quotes(scenario: &Scenario, prices: &[(&str, i64, f64)]) -> Self {
        Self::build(scenario, prices, &serde_json::json!({})).await
    }

    /// The app over `scenario` with the given quotes and order histories.
    pub async fn build(
        scenario: &Scenario,
        prices: &[(&str, i64, f64)],
        orders: &serde_json::Value,
    ) -> Self {
        let mut settings = Settings::defaults().unwrap();
        settings.dry_run = true;
        settings.swing_execution_enabled = false;
        settings.swing_monitor_enabled = false;
        settings.swing_auto_execute = false;
        settings.sole_user_id = USER;
        settings.sole_broker_account_id = 1;
        // A placeholder key so `Kite()` constructs (the replay client answers; nothing is sent).
        settings.kite_api_key = "replay-test-key".to_owned();
        settings.db_backend = "sqlite".to_owned();
        settings.db_path = scenario.path.display().to_string();
        settings.token_file = scenario
            .dir
            .path()
            .join("no-such-token.json")
            .display()
            .to_string();
        let kite_dir = tempfile::tempdir().unwrap();
        kite_fixture(kite_dir.path(), prices, orders);
        let kite =
            Arc::new(ReplayKite::load(kite_dir.path(), None, &settings.kite_api_key).unwrap());
        let clock = Arc::new(FixedClock::new(now()));
        let db = Db::open(DbConfig::from_settings(&settings, None))
            .await
            .unwrap();
        let root = desk_root();
        let paths = DeskPaths {
            root: scenario.dir.path().to_path_buf(),
            templates: root.join("app").join("templates"),
            static_dir: root.join("app").join("static"),
        };
        let state = build_state(StateParts {
            settings,
            clock: clock.clone(),
            ids: Arc::new(HarnessIds::new("swing-desk-tests")),
            db,
            kite: kite.clone(),
            paths,
            harness: None,
        });
        Self {
            app: build_app(state.clone()),
            state,
            clock,
            kite,
            _kite_dir: kite_dir,
        }
    }

    /// `monkeypatch.setattr(swing_desk, "_now", lambda: at)`.
    pub fn at(&self, at: DateTime<FixedOffset>) {
        self.clock.set(at);
    }

    /// `monkeypatch.setattr(C, ...)` on the live settings.
    pub fn settings(&self, edit: impl FnOnce(&mut Settings)) {
        let mut s = self.state.settings.write().unwrap();
        edit(&mut s);
    }

    async fn send(&self, request: Request<Body>) -> Reply {
        let r = self.app.clone().oneshot(request).await.unwrap();
        let status = r.status();
        let headers = r.headers().clone();
        let bytes = to_bytes(r.into_body(), usize::MAX).await.unwrap();
        Reply {
            status,
            headers,
            text: String::from_utf8(bytes.to_vec()).unwrap(),
        }
    }

    /// `client.get(path)`.
    pub async fn get(&self, path: &str) -> Reply {
        self.send(
            Request::get(path)
                .header(header::HOST, "127.0.0.1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
    }

    /// `client.get(path).text`.
    pub async fn html(&self, path: &str) -> String {
        let r = self.get(path).await;
        assert_eq!(r.status, StatusCode::OK, "{path}: {}", r.text);
        r.text
    }

    /// `client.post(path, data=form)` from the desk's own origin.
    pub async fn post(&self, path: &str, form: &[(&str, &str)]) -> Reply {
        self.post_from(path, form, Some("http://127.0.0.1")).await
    }

    /// `client.post(...)` with a given `Origin` (or none).
    pub async fn post_from(
        &self,
        path: &str,
        form: &[(&str, &str)],
        origin: Option<&str>,
    ) -> Reply {
        let body: String = form
            .iter()
            .map(|(k, v)| format!("{}={}", urlencode(k), urlencode(v)))
            .collect::<Vec<_>>()
            .join("&");
        let mut b = Request::post(path)
            .header(header::HOST, "127.0.0.1")
            .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded");
        if let Some(o) = origin {
            b = b.header(header::ORIGIN, o);
        }
        self.send(b.body(Body::from(body)).unwrap()).await
    }

    /// The order and GTT calls the replay Kite recorded: none may exist in DRY_RUN.
    pub fn broker_writes(&self) -> usize {
        self.kite.write_calls().len()
    }
}

fn urlencode(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (b as char).to_string()
            }
            b' ' => "+".to_owned(),
            other => format!("%{other:02X}"),
        })
        .collect()
}

/// `_forms(html, action)`: every `<form ... action="...">...</form>`.
pub fn forms(html: &str, action: &str) -> Vec<String> {
    let re = regex::Regex::new(&format!(
        r#"(?s)<form[^>]*action="{}"[^>]*>.*?</form>"#,
        regex::escape(action)
    ))
    .unwrap();
    re.find_iter(html).map(|m| m.as_str().to_owned()).collect()
}

/// The first match of a `(?s)` regex, or a panic naming it.
pub fn find(pattern: &str, html: &str) -> String {
    let re = regex::Regex::new(&format!("(?s){pattern}")).unwrap();
    re.find(html)
        .map(|m| m.as_str().to_owned())
        .unwrap_or_else(|| panic!("no match for {pattern}"))
}

/// `html[html.index(a):html.index(b)]`.
pub fn between<'a>(html: &'a str, a: &str, b: &str) -> &'a str {
    let i = html.find(a).unwrap_or_else(|| panic!("no {a}"));
    let j = html.find(b).unwrap_or_else(|| panic!("no {b}"));
    &html[i..j]
}

/// `html[html.index(a):]`.
pub fn from<'a>(html: &'a str, a: &str) -> &'a str {
    let i = html.find(a).unwrap_or_else(|| panic!("no {a}"));
    &html[i..]
}
