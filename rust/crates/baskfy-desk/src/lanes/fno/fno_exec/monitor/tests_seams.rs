//! `tests/test_fno_monitor_loop.py` (`TestTheLoop`, `TestTheKiteSeams`) and the monitor's
//! pure gating (`test_fno_monitor.py::TestGating`'s source and flag halves).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;

use super::seams::{
    Instruments, KiteFoQuotes, KiteMargins, MarginLeg, MarginQuote, MarginReads, MarginSource,
    NfoDump, PgKiteInstruments, QuoteRaw,
};
use super::service::{RETRY_SECONDS, next_run, run_forever};
use crate::lanes::fno::fno_exec::execute::FoQuoteSource;
use crate::wire::{PyValue, dict};

fn wed() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 10, 28).unwrap()
}

fn at(day: NaiveDate, hh: u32, mm: u32) -> DateTime<FixedOffset> {
    crate::clock::ist()
        .from_local_datetime(&day.and_hms_opt(hh, mm, 0).unwrap())
        .single()
        .unwrap()
}

fn d(s: &str) -> Decimal {
    s.parse().unwrap()
}

#[test]
fn the_loop_starts_at_0914_and_at_once_inside_the_window() {
    assert_eq!(next_run(at(wed(), 8, 0), false), at(wed(), 9, 14));
    assert_eq!(next_run(at(wed(), 19, 30), false), at(wed(), 19, 30));
    assert_eq!(
        next_run(at(wed(), 23, 45), false),
        at(wed() + Duration::days(1), 9, 14)
    );
    let sat = NaiveDate::from_ymd_opt(2026, 10, 31).unwrap();
    let mon = NaiveDate::from_ymd_opt(2026, 11, 2).unwrap();
    assert_eq!(next_run(at(sat, 10, 0), false), at(mon, 9, 14));
    assert_eq!(
        next_run(at(wed(), 10, 0), true),
        at(wed() + Duration::days(1), 9, 14)
    );
}

#[tokio::test]
async fn a_crash_in_the_window_restarts_and_a_clean_exit_ends_the_day() {
    let moments: Vec<DateTime<FixedOffset>> = std::iter::repeat_n(at(wed(), 10, 0), 3)
        .chain(std::iter::repeat_n(at(wed(), 10, 1), 3))
        .chain(std::iter::repeat_n(at(wed(), 23, 31), 6))
        .collect();
    let moments = Mutex::new(moments.into_iter());
    let codes = Mutex::new(vec![1, 0].into_iter());
    let calls: Arc<Mutex<Vec<Vec<String>>>> = Arc::default();
    let sleeps: Arc<Mutex<Vec<f64>>> = Arc::default();
    let (c2, s2) = (calls.clone(), sleeps.clone());
    run_forever(
        || moments.lock().unwrap().next().unwrap(),
        move |s| {
            s2.lock().unwrap().push(s);
            async {}
        },
        move |argv| {
            c2.lock().unwrap().push(argv);
            let rc = codes.lock().unwrap().next().unwrap();
            async move { rc }
        },
        Some(2),
    )
    .await;
    let calls = calls.lock().unwrap();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0], vec!["service", "fno-monitor"]);
    assert!(sleeps.lock().unwrap().contains(&RETRY_SECONDS));
}

#[derive(Default)]
struct FakeKite {
    calls: Mutex<Vec<String>>,
    broken: bool,
}

impl FakeKite {
    fn calls(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }
}

fn level(price: f64, quantity: i64) -> PyValue {
    PyValue::Map(dict([
        ("price", PyValue::Float(price)),
        ("quantity", PyValue::Int(quantity)),
    ]))
}

#[async_trait]
impl QuoteRaw for FakeKite {
    async fn quote_raw(&self, keys: &[String]) -> Result<PyValue, String> {
        self.calls
            .lock()
            .unwrap()
            .push(format!("quote_raw:{}", keys.len()));
        let depth = dict([
            ("buy", PyValue::List(vec![level(60.0, 650), level(0.0, 0)])),
            ("sell", PyValue::List(vec![level(61.0, 1300)])),
        ]);
        let q = dict([
            ("last_price", PyValue::Float(60.4)),
            ("oi", PyValue::Int(1_300_000)),
            ("depth", PyValue::Map(depth)),
        ]);
        Ok(PyValue::Map(dict([(
            "NFO:NIFTY26NOV25000CE",
            PyValue::Map(q),
        )])))
    }
}

#[async_trait]
impl MarginReads for FakeKite {
    async fn slot(&self) {
        self.calls.lock().unwrap().push("slot".to_owned());
    }

    async fn basket(&self, orders: &[PyValue]) -> Result<PyValue, String> {
        if self.broken {
            return Err("ConnectionError: fixture: the margin endpoint is down".to_owned());
        }
        let product = match orders.first() {
            Some(PyValue::Map(m)) => m.get("product").map(PyValue::py_str).unwrap_or_default(),
            _ => String::new(),
        };
        self.calls
            .lock()
            .unwrap()
            .push(format!("basket:{}:{product}", orders.len()));
        Ok(PyValue::Map(dict([
            (
                "initial",
                PyValue::Map(dict([("total", PyValue::Float(190_000.0))])),
            ),
            (
                "final",
                PyValue::Map(dict([("total", PyValue::Float(61_000.0))])),
            ),
        ])))
    }

    async fn equity(&self) -> Result<PyValue, String> {
        self.calls.lock().unwrap().push("margins:equity".to_owned());
        Ok(PyValue::Map(dict([("net", PyValue::Float(500_000.0))])))
    }
}

fn fut(name: &str, kind: &str, symbol: &str, token: i64, expiry: &str) -> PyValue {
    PyValue::Map(dict([
        ("name", PyValue::Str(name.to_owned())),
        ("instrument_type", PyValue::Str(kind.to_owned())),
        ("tradingsymbol", PyValue::Str(symbol.to_owned())),
        ("instrument_token", PyValue::Int(token)),
        ("lot_size", PyValue::Int(500)),
        ("expiry", PyValue::Str(expiry.to_owned())),
    ]))
}

#[async_trait]
impl NfoDump for FakeKite {
    async fn nfo(&self) -> Result<Vec<PyValue>, String> {
        self.calls
            .lock()
            .unwrap()
            .push("instruments:NFO".to_owned());
        Ok(vec![
            fut("RELIANCE", "FUT", "RELIANCE26DECFUT", 2, "2026-12-29"),
            fut("RELIANCE", "FUT", "RELIANCE26NOVFUT", 1, "2026-11-24"),
            fut("RELIANCE", "CE", "RELIANCE26NOV1500CE", 3, "2026-11-24"),
        ])
    }
}

#[tokio::test]
async fn quotes_carry_depth_last_and_oi() {
    let kite = Arc::new(FakeKite::default());
    let books = KiteFoQuotes(kite.clone())
        .quotes(&[
            "NIFTY26NOV25000CE".to_owned(),
            "NIFTY26NOV25000CE".to_owned(),
        ])
        .await
        .unwrap();
    let q = &books["NIFTY26NOV25000CE"];
    assert_eq!(
        (q.bid, q.ask, q.last, q.oi),
        (
            Some(d("60.0")),
            Some(d("61.0")),
            Some(d("60.4")),
            Some(1_300_000)
        )
    );
    assert_eq!(q.bids.len(), 1);
    assert_eq!(q.mid(), Some(d("60.5")));
    assert_eq!(kite.calls(), vec!["quote_raw:1"]);
}

#[tokio::test]
async fn margins_are_the_baskets_final_total_against_free_margin() {
    let kite = Arc::new(FakeKite::default());
    let leg = MarginLeg {
        tradingsymbol: "NIFTY26NOV25000CE".to_owned(),
        side: "SELL".to_owned(),
        quantity: 65,
        price: d("60"),
    };
    let quote = KiteMargins(kite.clone()).margins(&[leg]).await.unwrap();
    assert_eq!(
        quote,
        Some(MarginQuote {
            required_inr: d("61000.0"),
            free_inr: d("500000.0"),
        })
    );
    assert!(kite.calls().contains(&"basket:1:NRML".to_owned()));
}

#[tokio::test]
async fn an_unreadable_margin_is_none() {
    let kite = Arc::new(FakeKite {
        broken: true,
        ..FakeKite::default()
    });
    assert_eq!(KiteMargins(kite).margins(&[]).await.unwrap(), None);
}

#[tokio::test]
async fn futures_come_from_the_nfo_master_once() {
    let kite = Arc::new(FakeKite::default());
    let master = PgKiteInstruments::new(None, kite.clone());
    let nov_day = NaiveDate::from_ymd_opt(2026, 11, 24).unwrap();
    let nov = master.future("RELIANCE", nov_day).await.unwrap().unwrap();
    assert_eq!(nov.tradingsymbol, "RELIANCE26NOVFUT");
    let nxt = master
        .next_future("RELIANCE", nov_day)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        (nxt.tradingsymbol.as_str(), nxt.lot_size),
        ("RELIANCE26DECFUT", 500)
    );
    let dec_day = NaiveDate::from_ymd_opt(2026, 12, 29).unwrap();
    assert!(
        master
            .next_future("RELIANCE", dec_day)
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(kite.calls(), vec!["instruments:NFO"]);
}

#[test]
fn there_is_no_auto_flag_and_the_monitor_never_confirms() {
    let sources = [
        include_str!("engine.rs"),
        include_str!("night.rs"),
        include_str!("service.rs"),
        include_str!("seams.rs"),
        include_str!("../execute.rs"),
    ];
    for src in sources {
        let code: String = src
            .lines()
            .filter(|l| !l.trim_start().starts_with("//"))
            .collect::<Vec<_>>()
            .join("\n");
        assert!(
            !code.contains("FNO_AUTO") && !code.contains("_AUTO_EXECUTE"),
            "no auto-execute flag"
        );
    }
    let monitor: String = sources[..4]
        .iter()
        .flat_map(|s| s.lines())
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n");
    assert!(
        !monitor.contains("execute_entry"),
        "the monitor never confirms an entry"
    );
    assert!(!monitor.contains("execute_click_exit"));
}

#[tokio::test]
async fn the_flag_off_builds_nothing_and_returns_ok() {
    use crate::app::{DeskPaths, StateParts, build_state};
    use crate::clock::FixedClock;
    use crate::fake_kite::ReplayKite;
    use crate::ids::HarnessIds;
    use crate::settings::Settings;
    use crate::storage::{Db, DbConfig};

    let dir = tempfile::tempdir().unwrap();
    let mut settings = Settings::defaults().unwrap();
    assert!(!settings.fno_monitor_enabled, "the flag defaults false");
    assert!(settings.dry_run);
    settings.kite_api_key = "replay-test-key".to_owned();
    settings.db_backend = "sqlite".to_owned();
    let db_file = dir.path().join("desk.db");
    settings.db_path = db_file.display().to_string();
    settings.token_file = dir.path().join("no-token.json").display().to_string();
    std::fs::write(
        dir.path().join("kite.json"),
        serde_json::json!({"session": {"authed": true}, "routes": {}}).to_string(),
    )
    .unwrap();
    let kite = Arc::new(ReplayKite::load(dir.path(), None, &settings.kite_api_key).unwrap());
    let db = Db::open(DbConfig::from_settings(&settings, None))
        .await
        .unwrap();
    let state = build_state(StateParts {
        settings,
        clock: Arc::new(FixedClock::new(at(wed(), 9, 20))),
        ids: Arc::new(HarnessIds::new("fno-monitor-off")),
        db,
        kite: kite.clone(),
        paths: DeskPaths::under(dir.path()),
        harness: None,
    });
    let ran = crate::lanes::fno::run_service("fno-monitor", &state).await;
    assert_eq!(ran, Some(Ok(())));
    assert!(kite.calls().is_empty(), "no Kite read with the flag off");
    assert!(!db_file.exists(), "no database opened with the flag off");
}
