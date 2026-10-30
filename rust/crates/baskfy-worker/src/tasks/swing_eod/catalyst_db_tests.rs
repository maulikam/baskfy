//! `services/worker/tests/test_swing_catalyst.py`, ported: the feed's symbols, the links, the
//! watch fill, the earnings flag, fail-soft and the explicit symbol set the overlap scan passes.
//! Same clone and rollback discipline as `db_tests`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Mutex;

use baskfy_db::OrderedJson;
use baskfy_providers::errors::ProviderError;
use baskfy_providers::nse::{CATALYST_SOURCE_ANNOUNCEMENT, CATALYST_SOURCE_EVENT_CALENDAR};
use baskfy_providers::records::{
    CatalystFields, CatalystRecord, EarningsDateFields, EarningsDateRecord, PyDateTime,
};
use chrono::{DateTime, Days, FixedOffset, NaiveDate, TimeZone as _};
use sqlx::PgConnection;

use super::catalyst::{CatalystSource, run_swing_catalyst};
use super::db_tests::{clone, instrument, user};
use crate::steps::{StepOutcome, StepStatus};

const CALENDAR: &str =
    "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar?symbol=";

fn session() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 19).unwrap()
}

fn ist(y: i32, m: u32, d: u32, h: u32, min: u32) -> DateTime<FixedOffset> {
    FixedOffset::east_opt(19_800)
        .unwrap()
        .with_ymd_and_hms(y, m, d, h, min, 0)
        .unwrap()
}

fn announcement(
    symbol: &str,
    stamp: Option<DateTime<FixedOffset>>,
    url: &str,
    headline: &str,
) -> CatalystRecord {
    CatalystRecord::new(CatalystFields {
        symbol: format!("ZZ{symbol}"),
        headline: headline.to_owned(),
        published_at: stamp.map(PyDateTime::Aware),
        url: url.to_owned(),
        source: CATALYST_SOURCE_ANNOUNCEMENT.to_owned(),
    })
    .unwrap()
}

fn result(symbol: &str, on: NaiveDate, purpose: &str) -> EarningsDateRecord {
    EarningsDateRecord::new(EarningsDateFields {
        symbol: format!("ZZ{symbol}"),
        event_date: on,
        purpose: purpose.to_owned(),
        url: format!("{CALENDAR}ZZ{symbol}"),
    })
    .unwrap()
}

/// `FakeNSE`: answers per symbol from a script, counts what it was asked, refuses on demand.
#[derive(Default)]
struct FakeNse {
    announcements: Vec<(String, Vec<CatalystRecord>)>,
    calendar: Vec<(String, Vec<EarningsDateRecord>)>,
    refuse: Vec<String>,
    asked: Mutex<Vec<String>>,
}

impl FakeNse {
    fn with(
        announcements: Vec<(&str, Vec<CatalystRecord>)>,
        calendar: Vec<(&str, Vec<EarningsDateRecord>)>,
        refuse: &[&str],
    ) -> Self {
        Self {
            announcements: announcements
                .into_iter()
                .map(|(s, r)| (format!("ZZ{s}"), r))
                .collect(),
            calendar: calendar
                .into_iter()
                .map(|(s, r)| (format!("ZZ{s}"), r))
                .collect(),
            refuse: refuse.iter().map(|s| format!("ZZ{s}")).collect(),
            asked: Mutex::new(Vec::new()),
        }
    }

    fn asked(&self) -> Vec<String> {
        self.asked
            .lock()
            .unwrap()
            .iter()
            .map(|s| s.trim_start_matches("ZZ").to_owned())
            .collect()
    }

    fn refusal(symbol: &str) -> ProviderError {
        ProviderError::upstream_unavailable(format!("GET {symbol} returned 503"))
            .with_provider("nse")
    }
}

impl CatalystSource for FakeNse {
    async fn announcements(
        &self,
        symbols: &[String],
        _on: NaiveDate,
    ) -> Result<Vec<CatalystRecord>, ProviderError> {
        let mut out = Vec::new();
        for symbol in symbols {
            self.asked.lock().unwrap().push(symbol.clone());
            if self.refuse.contains(symbol) {
                return Err(Self::refusal(symbol));
            }
            for (s, records) in &self.announcements {
                if s == symbol {
                    out.extend(records.iter().cloned());
                }
            }
        }
        Ok(out)
    }

    async fn results_calendar(
        &self,
        symbols: &[String],
        _on: NaiveDate,
    ) -> Result<Vec<EarningsDateRecord>, ProviderError> {
        let mut out = Vec::new();
        for symbol in symbols {
            if self.refuse.contains(symbol) {
                return Err(Self::refusal(symbol));
            }
            for (s, records) in &self.calendar {
                if s == symbol {
                    out.extend(records.iter().cloned());
                }
            }
        }
        Ok(out)
    }
}

async fn last_session(conn: &mut PgConnection, before: NaiveDate) -> NaiveDate {
    let day = before.checked_sub_days(Days::new(1)).unwrap();
    *super::recent_trading_days(conn, day, 1)
        .await
        .unwrap()
        .last()
        .unwrap()
}

async fn watch(
    conn: &mut PgConnection,
    user_id: i64,
    instrument_id: i64,
    state: &str,
    catalyst: Option<&str>,
    source: &str,
) -> i64 {
    let (id,): (i64,) = sqlx::query_as(
        "INSERT INTO sw_watch (user_id, instrument_id, setup, source, added_on, trigger, \
         stop_ref, catalyst, state) VALUES ($1, $2, 'FLAG', $3, $4, 110.00, 104.00, $5, $6) \
         RETURNING id",
    )
    .bind(user_id)
    .bind(instrument_id)
    .bind(source)
    .bind(session())
    .bind(catalyst)
    .bind(state)
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    id
}

async fn watching(conn: &mut PgConnection, user_id: i64, instrument_id: i64) -> i64 {
    watch(conn, user_id, instrument_id, "WATCHING", None, "DETECTOR").await
}

async fn ep_candidate(
    conn: &mut PgConnection,
    user_id: i64,
    instrument_id: i64,
    on: NaiveDate,
    setup: &str,
) {
    sqlx::query(
        "INSERT INTO sw_setup_daily (user_id, date, instrument_id, setup, status, score, close, \
         adj_factor, locked_upper_circuit, listed_within_2y) \
         VALUES ($1, $2, $3, $4, 'SETTING_UP', 80.00, 120.00, 1, false, false)",
    )
    .bind(user_id)
    .bind(on)
    .bind(instrument_id)
    .bind(setup)
    .execute(&mut *conn)
    .await
    .unwrap();
}

async fn vbt_breadth(conn: &mut PgConnection, user_id: i64, on: NaiveDate) {
    sqlx::query(
        "INSERT INTO vb_breadth_daily (user_id, date, universe_count, measured_count, \
         above_count, pct_above_dma, gate) VALUES ($1, $2, 10, 10, 5, 50, 'OPEN')",
    )
    .bind(user_id)
    .bind(on)
    .execute(&mut *conn)
    .await
    .unwrap();
}

async fn vbt_signal(
    conn: &mut PgConnection,
    user_id: i64,
    instrument_id: i64,
    on: NaiveDate,
    state: &str,
) {
    sqlx::query(
        "INSERT INTO vb_signal_daily (user_id, date, instrument_id, state, close, close_raw, \
         limit_price, stop_price) VALUES ($1, $2, $3, $4, 100, 100.50, 101, 95)",
    )
    .bind(user_id)
    .bind(on)
    .bind(instrument_id)
    .bind(state)
    .execute(&mut *conn)
    .await
    .unwrap();
}

async fn twt_breadth(conn: &mut PgConnection, user_id: i64, on: NaiveDate) {
    sqlx::query(
        "INSERT INTO tw_breadth_daily (user_id, date, universe_count, measured_count, \
         above_count, pct_above_dma, gate, dma_bars, thin_session) \
         VALUES ($1, $2, 3413, 1769, 905, 51.1588, 'OPEN', 200, false)",
    )
    .bind(user_id)
    .bind(on)
    .execute(&mut *conn)
    .await
    .unwrap();
}

async fn twt_state(conn: &mut PgConnection, user_id: i64, instrument_id: i64, on: NaiveDate) {
    sqlx::query(
        "INSERT INTO tw_state_daily (user_id, date, instrument_id, close, close_raw, adj_factor, \
         week_close_0, week_close_1, week_close_2, week_range_pct, month_low_3, month_low_ratio, \
         vol_sma_50, volume, turnover_inr, turnover_avg_20, sma_dma, sessions_in_state, \
         bars_in_window, locked_upper_circuit) VALUES ($1, $2, $3, 149.60, 149.60, 1, 149.60, \
         148.90, 147.50, 1.4237, 100.00, 1.4960, 250000, 310000, 500000000, 500000000, 120.00, \
         4, 260, false)",
    )
    .bind(user_id)
    .bind(on)
    .bind(instrument_id)
    .execute(&mut *conn)
    .await
    .unwrap();
}

async fn twt_signal(conn: &mut PgConnection, user_id: i64, instrument_id: i64, on: NaiveDate) {
    twt_state(conn, user_id, instrument_id, on).await;
    sqlx::query(
        "INSERT INTO tw_signal_daily (user_id, date, instrument_id, state, failed_filters, \
         entry_reference_close, stop_preview, sessions_out_before, rank_key, turnover_avg_20) \
         VALUES ($1, $2, $3, 'SIGNAL', '{}', 149.60, 119.68, 7, 500000000, 500000000)",
    )
    .bind(user_id)
    .bind(on)
    .bind(instrument_id)
    .execute(&mut *conn)
    .await
    .unwrap();
}

/// `_run`: the feed over its own selection; the report and the step's note agree.
async fn run(conn: &mut PgConnection, user_id: i64, provider: Option<&FakeNse>) -> StepOutcome {
    let mut outcome = StepOutcome::default();
    let report = run_swing_catalyst(conn, &mut outcome, session(), user_id, provider, None, None)
        .await
        .unwrap();
    assert_eq!(report.as_detail(), outcome.detail_json());
    outcome
}

fn detail(outcome: &StepOutcome, key: &str) -> OrderedJson {
    outcome
        .detail
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.clone())
        .unwrap_or_else(|| panic!("no {key} in the note"))
}

fn int(outcome: &StepOutcome, key: &str) -> i64 {
    match detail(outcome, key) {
        OrderedJson::Int(n) => n,
        other => panic!("{key} is {other:?}"),
    }
}

fn text(outcome: &StepOutcome, key: &str) -> String {
    crate::pyjson::dumps(&detail(outcome, key)).unwrap()
}

/// `feed_symbols` through the feed: the names it asks the provider, in order.
async fn asked_by_feed(conn: &mut PgConnection, user_id: i64) -> Vec<String> {
    let provider = FakeNse::default();
    run(conn, user_id, Some(&provider)).await;
    provider.asked()
}

/// `_catalysts`: `(instrument_id, url, headline, published_at, source, earnings_date)` by id.
#[allow(clippy::type_complexity)]
async fn catalysts(
    conn: &mut PgConnection,
    user_id: i64,
) -> Vec<(
    i64,
    i64,
    String,
    String,
    Option<DateTime<chrono::Utc>>,
    String,
    Option<NaiveDate>,
)> {
    sqlx::query_as(
        "SELECT id, instrument_id, url, headline, published_at, source, earnings_date \
         FROM sw_catalyst WHERE user_id = $1 ORDER BY id",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await
    .unwrap()
}

async fn watch_row(conn: &mut PgConnection, id: i64) -> (Option<String>, Option<NaiveDate>) {
    sqlx::query_as("SELECT catalyst, earnings_date FROM sw_watch WHERE id = $1")
        .bind(id)
        .fetch_one(&mut *conn)
        .await
        .unwrap()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn watching_rows_and_the_last_sessions_ep_candidates_and_nothing_else() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let last = last_session(&mut conn, session()).await;
    let watched = instrument(&mut conn, "WATCHED").await;
    let dismissed = instrument(&mut conn, "DISMISSED").await;
    let triggered = instrument(&mut conn, "TRIGGERED").await;
    let ep = instrument(&mut conn, "EPCAND").await;
    let flag = instrument(&mut conn, "FLAGCAND").await;
    let stale = instrument(&mut conn, "STALEEP").await;
    watching(&mut conn, user_id, watched).await;
    watch(&mut conn, user_id, dismissed, "DISMISSED", None, "DETECTOR").await;
    watch(&mut conn, user_id, triggered, "TRIGGERED", None, "DETECTOR").await;
    ep_candidate(&mut conn, user_id, ep, last, "EP").await;
    ep_candidate(&mut conn, user_id, flag, last, "FLAG").await;
    ep_candidate(
        &mut conn,
        user_id,
        stale,
        last.checked_sub_days(Days::new(7)).unwrap(),
        "EP",
    )
    .await;
    let provider = FakeNse::default();
    let outcome = run(&mut conn, user_id, Some(&provider)).await;
    assert_eq!(provider.asked(), ["EPCAND", "WATCHED"]);
    assert_eq!(outcome.status, StepStatus::Succeeded);
    assert_eq!(int(&outcome, "symbols"), 2);
    assert_eq!(int(&outcome, "rows_written"), 0);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn nothing_watched_is_a_noted_no_op_not_a_provider_call() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let provider = FakeNse::default();
    let outcome = run(&mut conn, user_id, Some(&provider)).await;
    assert!(provider.asked().is_empty());
    assert_eq!(outcome.status, StepStatus::Succeeded);
    assert!(text(&outcome, "skipped_reason").contains("nothing watched"));
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn the_latest_vbt_signal_is_on_the_feed_and_an_older_or_rejected_one_is_not() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let last = last_session(&mut conn, session()).await;
    let older = last_session(&mut conn, last).await;
    let signal = instrument(&mut conn, "VBSIG").await;
    let stale = instrument(&mut conn, "VBSTALE").await;
    let reject = instrument(&mut conn, "VBREJECT").await;
    vbt_breadth(&mut conn, user_id, older).await;
    vbt_breadth(&mut conn, user_id, last).await;
    vbt_signal(&mut conn, user_id, stale, older, "SIGNAL").await;
    vbt_signal(&mut conn, user_id, reject, last, "SCAN_ONLY").await;
    assert!(asked_by_feed(&mut conn, user_id).await.is_empty());
    vbt_signal(&mut conn, user_id, signal, last, "SIGNAL").await;
    assert_eq!(asked_by_feed(&mut conn, user_id).await, ["VBSIG"]);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn the_latest_twt_signal_is_on_the_feed_and_a_name_merely_in_state_is_not() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let last = last_session(&mut conn, session()).await;
    let quiet = instrument(&mut conn, "TWQUIET").await;
    let signal = instrument(&mut conn, "TWSIG").await;
    twt_breadth(&mut conn, user_id, last).await;
    twt_state(&mut conn, user_id, quiet, last).await;
    assert!(asked_by_feed(&mut conn, user_id).await.is_empty());
    twt_signal(&mut conn, user_id, signal, last).await;
    assert_eq!(asked_by_feed(&mut conn, user_id).await, ["TWSIG"]);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_name_both_watched_and_on_the_volume_sleeve_is_asked_once() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let last = last_session(&mut conn, session()).await;
    let both = instrument(&mut conn, "BOTH").await;
    watching(&mut conn, user_id, both).await;
    vbt_breadth(&mut conn, user_id, last).await;
    vbt_signal(&mut conn, user_id, both, last, "SIGNAL").await;
    let provider = FakeNse::default();
    let outcome = run(&mut conn, user_id, Some(&provider)).await;
    assert_eq!(provider.asked(), ["BOTH"]);
    assert_eq!(int(&outcome, "symbols"), 1);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_headline_a_stamp_and_a_url_per_filing_idempotent_on_the_url() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let watched = instrument(&mut conn, "WATCHED").await;
    watching(&mut conn, user_id, watched).await;
    let newest = ist(2026, 8, 18, 18, 30);
    let older = ist(2026, 8, 17, 9, 0);
    let provider = FakeNse::with(
        vec![(
            "WATCHED",
            vec![
                announcement(
                    "WATCHED",
                    Some(newest),
                    "https://a/1.pdf?x=1",
                    "Press Release - deal",
                ),
                announcement("WATCHED", Some(older), "https://a/2.pdf", "Updates - capex"),
            ],
        )],
        vec![],
        &[],
    );
    run(&mut conn, user_id, Some(&provider)).await;
    let first = catalysts(&mut conn, user_id).await;
    run(&mut conn, user_id, Some(&provider)).await;
    let second = catalysts(&mut conn, user_id).await;
    let seen: Vec<_> = first
        .iter()
        .map(|r| (r.2.as_str(), r.3.as_str(), r.4, r.5.as_str()))
        .collect();
    assert_eq!(
        seen,
        [
            (
                "https://a/1.pdf?x=1",
                "Press Release - deal",
                Some(newest.to_utc()),
                CATALYST_SOURCE_ANNOUNCEMENT
            ),
            (
                "https://a/2.pdf",
                "Updates - capex",
                Some(older.to_utc()),
                CATALYST_SOURCE_ANNOUNCEMENT
            ),
        ]
    );
    let ids = |rows: &[(i64, i64, String, String, _, String, _)]| -> Vec<(i64, String)> {
        rows.iter().map(|r| (r.0, r.2.clone())).collect()
    };
    assert_eq!(ids(&second), ids(&first));
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_filing_nse_republishes_under_the_same_url_is_one_row() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let watched = instrument(&mut conn, "WATCHED").await;
    watching(&mut conn, user_id, watched).await;
    let stamp = ist(2026, 8, 18, 18, 30);
    let provider = FakeNse::with(
        vec![(
            "WATCHED",
            vec![
                announcement("WATCHED", Some(stamp), "https://a/1.pdf", "Updates"),
                announcement(
                    "WATCHED",
                    Some(stamp),
                    "https://a/1.pdf",
                    "Updates (re-listed)",
                ),
            ],
        )],
        vec![],
        &[],
    );
    run(&mut conn, user_id, Some(&provider)).await;
    let rows = catalysts(&mut conn, user_id).await;
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].3, "Updates");
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn the_step_counts_what_it_wrote() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let watched = instrument(&mut conn, "WATCHED").await;
    watching(&mut conn, user_id, watched).await;
    let stamp = ist(2026, 8, 18, 18, 30);
    let provider = FakeNse::with(
        vec![(
            "WATCHED",
            vec![announcement(
                "WATCHED",
                Some(stamp),
                "https://a/1.pdf",
                "Updates",
            )],
        )],
        vec![(
            "WATCHED",
            vec![result(
                "WATCHED",
                NaiveDate::from_ymd_opt(2026, 10, 15).unwrap(),
                "Financial Results",
            )],
        )],
        &[],
    );
    let outcome = run(&mut conn, user_id, Some(&provider)).await;
    assert_eq!((outcome.rows_in, outcome.rows_out), (1, 2));
    assert_eq!(int(&outcome, "announcements"), 1);
    assert_eq!(int(&outcome, "earnings_dates"), 1);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn an_empty_or_blank_catalyst_gets_the_newest_headline_and_a_typed_one_never_is_overwritten()
{
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let empty = instrument(&mut conn, "EMPTY").await;
    let blank = instrument(&mut conn, "BLANK").await;
    let typed = instrument(&mut conn, "TYPED").await;
    let empty_id = watching(&mut conn, user_id, empty).await;
    let blank_id = watch(&mut conn, user_id, blank, "WATCHING", Some(""), "DETECTOR").await;
    let typed_id = watch(
        &mut conn,
        user_id,
        typed,
        "WATCHING",
        Some("Q2 result, order book up"),
        "MANUAL",
    )
    .await;
    let newest = ist(2026, 8, 18, 18, 30);
    let older = ist(2026, 8, 17, 9, 0);
    let provider = FakeNse::with(
        vec![
            (
                "EMPTY",
                vec![
                    announcement(
                        "EMPTY",
                        Some(newest),
                        "https://a/1.pdf",
                        "Press Release - deal",
                    ),
                    announcement("EMPTY", Some(older), "https://a/2.pdf", "Updates - capex"),
                ],
            ),
            (
                "BLANK",
                vec![announcement(
                    "BLANK",
                    Some(newest),
                    "https://a/b.pdf",
                    "Press Release",
                )],
            ),
            (
                "TYPED",
                vec![announcement(
                    "TYPED",
                    Some(newest),
                    "https://a/t.pdf",
                    "Press Release",
                )],
            ),
        ],
        vec![],
        &[],
    );
    let outcome = run(&mut conn, user_id, Some(&provider)).await;
    assert_eq!(
        watch_row(&mut conn, empty_id).await.0.as_deref(),
        Some("Press Release - deal")
    );
    assert_eq!(
        watch_row(&mut conn, blank_id).await.0.as_deref(),
        Some("Press Release")
    );
    assert_eq!(
        watch_row(&mut conn, typed_id).await.0.as_deref(),
        Some("Q2 result, order book up")
    );
    assert_eq!(int(&outcome, "catalysts_filled"), 2);
    let typed_links: Vec<String> = catalysts(&mut conn, user_id)
        .await
        .into_iter()
        .filter(|r| r.1 == typed)
        .map(|r| r.2)
        .collect();
    assert_eq!(typed_links, ["https://a/t.pdf"]);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn the_flag_is_written_and_links_out_and_is_cleared_when_the_calendar_drops_it() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let watched = instrument(&mut conn, "WATCHED").await;
    let watch_id = watching(&mut conn, user_id, watched).await;
    let october = NaiveDate::from_ymd_opt(2026, 10, 15).unwrap();
    let provider = FakeNse::with(
        vec![],
        vec![(
            "WATCHED",
            vec![
                result(
                    "WATCHED",
                    NaiveDate::from_ymd_opt(2026, 4, 16).unwrap(),
                    "Financial Results/Dividend",
                ),
                result("WATCHED", october, "Financial Results"),
            ],
        )],
        &[],
    );
    let outcome = run(&mut conn, user_id, Some(&provider)).await;
    assert_eq!(watch_row(&mut conn, watch_id).await.1, Some(october));
    assert_eq!(int(&outcome, "earnings_flagged"), 1);
    let rows: Vec<_> = catalysts(&mut conn, user_id)
        .await
        .into_iter()
        .map(|r| (r.5, r.6, r.3, r.2))
        .collect();
    assert_eq!(
        rows,
        [(
            CATALYST_SOURCE_EVENT_CALENDAR.to_owned(),
            Some(october),
            "Financial Results".to_owned(),
            format!("{CALENDAR}ZZWATCHED"),
        )]
    );
    run(&mut conn, user_id, Some(&FakeNse::default())).await;
    assert_eq!(watch_row(&mut conn, watch_id).await.1, None);
    assert!(catalysts(&mut conn, user_id).await.is_empty());
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_provider_outage_is_a_succeeded_step_with_a_note_and_zero_rows() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let watched = instrument(&mut conn, "WATCHED").await;
    let watch_id = watching(&mut conn, user_id, watched).await;
    let provider = FakeNse::with(vec![], vec![], &["WATCHED"]);
    let outcome = run(&mut conn, user_id, Some(&provider)).await;
    assert_eq!(outcome.status, StepStatus::Succeeded);
    assert_eq!(int(&outcome, "rows_written"), 0);
    assert_eq!(text(&outcome, "failed"), "[\"ZZWATCHED\"]");
    assert!(text(&outcome, "provider_error").contains("503"));
    assert!(catalysts(&mut conn, user_id).await.is_empty());
    assert_eq!(watch_row(&mut conn, watch_id).await.0, None);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn one_refused_symbol_does_not_cost_the_others_their_links() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let good = instrument(&mut conn, "GOOD").await;
    let bad = instrument(&mut conn, "BAD").await;
    watching(&mut conn, user_id, good).await;
    watching(&mut conn, user_id, bad).await;
    let stamp = ist(2026, 8, 18, 18, 30);
    let provider = FakeNse::with(
        vec![(
            "GOOD",
            vec![announcement(
                "GOOD",
                Some(stamp),
                "https://a/g.pdf",
                "Updates",
            )],
        )],
        vec![],
        &["BAD"],
    );
    let outcome = run(&mut conn, user_id, Some(&provider)).await;
    assert_eq!(outcome.status, StepStatus::Succeeded);
    assert_eq!(text(&outcome, "failed"), "[\"ZZBAD\"]");
    let urls: Vec<String> = catalysts(&mut conn, user_id)
        .await
        .into_iter()
        .map(|r| r.2)
        .collect();
    assert_eq!(urls, ["https://a/g.pdf"]);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn the_given_names_are_read_and_the_feeds_own_are_not() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let watched = instrument(&mut conn, "WATCHED").await;
    let quiet = instrument(&mut conn, "TWQUIET").await;
    let flag = instrument(&mut conn, "FLAGNAME").await;
    watching(&mut conn, user_id, watched).await;
    let naive = NaiveDate::from_ymd_opt(2026, 9, 24)
        .unwrap()
        .and_hms_opt(17, 5, 0)
        .unwrap();
    let mut quiet_filing = announcement("TWQUIET", None, "https://x/q.pdf", "Order win");
    quiet_filing = CatalystRecord::new(CatalystFields {
        symbol: quiet_filing.symbol.clone(),
        headline: quiet_filing.headline.clone(),
        published_at: Some(PyDateTime::Naive(naive)),
        url: quiet_filing.url.clone(),
        source: CATALYST_SOURCE_ANNOUNCEMENT.to_owned(),
    })
    .unwrap();
    let provider = FakeNse::with(
        vec![("TWQUIET", vec![quiet_filing])],
        vec![(
            "FLAGNAME",
            vec![result(
                "FLAGNAME",
                session().checked_add_days(Days::new(20)).unwrap(),
                "Financial Results",
            )],
        )],
        &[],
    );
    let mut progress: Vec<(i64, i64)> = Vec::new();
    let mut record = |done: i64, total: i64| progress.push((done, total));
    let symbols = vec![
        ("ZZFLAGNAME".to_owned(), flag),
        ("ZZTWQUIET".to_owned(), quiet),
    ];
    let report = run_swing_catalyst(
        &mut conn,
        &mut StepOutcome::default(),
        session(),
        user_id,
        Some(&provider),
        Some(&symbols),
        Some(&mut record),
    )
    .await
    .unwrap();
    assert_eq!(provider.asked(), ["FLAGNAME", "TWQUIET"]);
    let detail = crate::pyjson::dumps(&report.as_detail()).unwrap();
    assert!(detail.contains("\"symbols\": 2"), "{detail}");
    assert!(detail.contains("\"announcements\": 1"), "{detail}");
    assert!(detail.contains("\"earnings_dates\": 1"), "{detail}");
    assert_eq!(progress, [(1, 2), (2, 2)]);
    let mut stored: Vec<(i64, String)> = catalysts(&mut conn, user_id)
        .await
        .into_iter()
        .map(|r| (r.1, r.5))
        .collect();
    stored.sort();
    let mut expected = vec![
        (quiet, CATALYST_SOURCE_ANNOUNCEMENT.to_owned()),
        (flag, CATALYST_SOURCE_EVENT_CALENDAR.to_owned()),
    ];
    expected.sort();
    assert_eq!(stored, expected);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_name_whose_read_fails_still_counts_toward_progress() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let first = instrument(&mut conn, "AAA").await;
    let refused = instrument(&mut conn, "BBB").await;
    let provider = FakeNse::with(vec![], vec![], &["BBB"]);
    let mut progress: Vec<(i64, i64)> = Vec::new();
    let mut record = |done: i64, total: i64| progress.push((done, total));
    let symbols = vec![("ZZAAA".to_owned(), first), ("ZZBBB".to_owned(), refused)];
    let report = run_swing_catalyst(
        &mut conn,
        &mut StepOutcome::default(),
        session(),
        user_id,
        Some(&provider),
        Some(&symbols),
        Some(&mut record),
    )
    .await
    .unwrap();
    let detail = crate::pyjson::dumps(&report.as_detail()).unwrap();
    assert!(detail.contains("\"failed\": [\"ZZBBB\"]"), "{detail}");
    assert_eq!(progress, [(1, 2), (2, 2)]);
}
