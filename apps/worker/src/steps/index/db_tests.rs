//! The worker's Python database tests for steps 5 and 6, ported: `test_reference_data.py`
//! (point-in-time membership, its `source`, index registration), `test_snapshots.py` (SW16's
//! guard through the writer) and `test_fundamentals.py` (`store_fundamentals`,
//! `fundamentals_scope`).
//!
//! **Where they run.** On one connection to a *scratch* database, inside `TEMP` tables shaped
//! like the production ones (their keys, the `source` CHECK constraint, the numeric scales) —
//! never the bed's `baskfy`, which is never written, and never a clone the harness owns. Set
//! `BASKFY_RS_SCRATCH_DATABASE_URL` (e.g. the bed container's `postgres` database) and run
//! `cargo test -p baskfy-worker -- --ignored steps::index`; without the variable they are
//! reported `ignored`. A URL naming the `baskfy` database is refused. Temporary tables vanish with
//! the connection, so nothing persists. The step-level parity against Python on a real clone is
//! `nightly_diff.py --step refresh_index_membership|refresh_index_snapshots`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::{BTreeMap, BTreeSet, HashMap};

use baskfy_providers::ProviderError;
use baskfy_providers::records::{
    EquityFundamental, EquityFundamentalFields, IndexSnapshot, IndexSnapshotFields,
};
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::{Connection, PgConnection};

use super::fundamentals::{fundamentals_scope, store_fundamentals};
use super::membership::{MembershipSource, NamedMethod, refresh_membership};
use super::snapshots::{note_result, store_snapshots};
use super::universes::UNIVERSES;
use crate::steps::StepOutcome;

/// The first dashboard id (`FIRST_NON_UNIVERSE_INDEX_ID`).
const FIRST_ID: i16 = 100;

const IGNORE: &str =
    "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored";

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

fn trade_date() -> NaiveDate {
    d(2026, 8, 21)
}

fn past() -> NaiveDate {
    d(2019, 6, 14)
}

fn dec(s: &str) -> Decimal {
    s.parse().unwrap()
}

async fn scratch() -> PgConnection {
    let url =
        std::env::var("BASKFY_RS_SCRATCH_DATABASE_URL").unwrap_or_else(|_| panic!("{IGNORE}"));
    let database = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    assert!(
        database != "baskfy" && !database.is_empty(),
        "refusing the database {database:?}: these tests write temp tables"
    );
    let mut conn = PgConnection::connect(&url).await.unwrap();
    for ddl in [
        "CREATE TEMP TABLE instrument (id bigint PRIMARY KEY, symbol varchar NOT NULL UNIQUE, \
         series varchar, instrument_type varchar NOT NULL)",
        "CREATE TEMP TABLE ohlcv_daily (instrument_id bigint NOT NULL, date date NOT NULL, \
         close_raw numeric(18,4) NOT NULL, PRIMARY KEY (instrument_id, date))",
        "CREATE TEMP TABLE index_def (id smallint PRIMARY KEY, slug varchar NOT NULL UNIQUE, \
         name varchar NOT NULL, is_universe boolean NOT NULL, sort_order smallint NOT NULL)",
        "CREATE TEMP TABLE index_member_daily (index_id smallint NOT NULL, date date NOT NULL, \
         instrument_id bigint NOT NULL, weight numeric(10,6), \
         source varchar NOT NULL DEFAULT 'nse_file' \
         CHECK (source IN ('nse_file', 'reconstructed', 'derived')), \
         PRIMARY KEY (index_id, date, instrument_id))",
        "CREATE TEMP TABLE index_snapshot_daily (index_id smallint NOT NULL, date date NOT NULL, \
         level numeric(18,4), change_abs numeric(18,4), change_pct numeric(10,4), \
         pe numeric(14,4), pb numeric(14,4), div_yield numeric(10,4), \
         PRIMARY KEY (index_id, date))",
        "CREATE TEMP TABLE fundamental_daily (instrument_id bigint NOT NULL, date date NOT NULL, \
         marketcap_cr bigint, pe numeric(14,4), pb numeric(14,4), div_yield numeric(10,4), \
         shares_outstanding bigint, PRIMARY KEY (instrument_id, date))",
    ] {
        sqlx::query(ddl).execute(&mut conn).await.unwrap();
    }
    for u in UNIVERSES {
        sqlx::query(
            "INSERT INTO index_def (id, slug, name, is_universe, sort_order) \
             VALUES ($1, $2, $2, true, $1)",
        )
        .bind(u.index_id)
        .bind(u.slug)
        .execute(&mut conn)
        .await
        .unwrap();
    }
    conn
}

async fn make_instrument(conn: &mut PgConnection, id: i64, symbol: &str, kind: &str) -> i64 {
    sqlx::query(
        "INSERT INTO instrument (id, symbol, series, instrument_type) VALUES ($1, $2, 'EQ', $3)",
    )
    .bind(id)
    .bind(symbol)
    .bind(kind)
    .execute(&mut *conn)
    .await
    .unwrap();
    id
}

async fn add_bar(conn: &mut PgConnection, id: i64, on: NaiveDate, close: &str) {
    sqlx::query("INSERT INTO ohlcv_daily (instrument_id, date, close_raw) VALUES ($1, $2, $3)")
        .bind(id)
        .bind(on)
        .bind(dec(close))
        .execute(&mut *conn)
        .await
        .unwrap();
}

/// `resolve_universe`: the instrument ids stamped with that exact date.
async fn resolve(conn: &mut PgConnection, slug: &str, on: NaiveDate) -> Vec<i64> {
    let index_id = UNIVERSES.iter().find(|u| u.slug == slug).unwrap().index_id;
    sqlx::query_as::<_, (i64,)>(
        "SELECT instrument_id FROM index_member_daily WHERE index_id = $1 AND date = $2 \
         ORDER BY instrument_id",
    )
    .bind(index_id)
    .bind(on)
    .fetch_all(&mut *conn)
    .await
    .unwrap()
    .into_iter()
    .map(|(i,)| i)
    .collect()
}

/// `membership_sources`.
async fn sources(conn: &mut PgConnection, slug: &str, on: NaiveDate) -> BTreeMap<String, i64> {
    let index_id = UNIVERSES.iter().find(|u| u.slug == slug).unwrap().index_id;
    sqlx::query_as::<_, (String, i64)>(
        "SELECT source, count(*) FROM index_member_daily WHERE index_id = $1 AND date = $2 \
         GROUP BY source",
    )
    .bind(index_id)
    .bind(on)
    .fetch_all(&mut *conn)
    .await
    .unwrap()
    .into_iter()
    .collect()
}

/// `ChangingMembershipProvider`: NIFTY 500's constituents by date; every other slug empty; no
/// ETF or F&O list (Python's stub lacks the methods, which answers `[]`).
struct Changing {
    by_date: HashMap<NaiveDate, Vec<&'static str>>,
    /// A slug to fail, and whether as `UnexpectedPayload` (else `UpstreamUnavailable`).
    fail: Option<(&'static str, bool)>,
}

impl Changing {
    fn new(by_date: &[(NaiveDate, &[&'static str])]) -> Self {
        Self {
            by_date: by_date.iter().map(|(k, v)| (*k, v.to_vec())).collect(),
            fail: None,
        }
    }
}

impl MembershipSource for Changing {
    async fn index_constituents(
        &self,
        slug: &str,
        on: NaiveDate,
    ) -> Result<Vec<String>, ProviderError> {
        if let Some((failing, unexpected)) = self.fail
            && failing == slug
        {
            return Err(if unexpected {
                ProviderError::unexpected_payload("no file")
            } else {
                ProviderError::upstream_unavailable("down")
            });
        }
        if slug != "nifty-500" {
            return Ok(Vec::new());
        }
        Ok(self
            .by_date
            .get(&on)
            .map(|v| v.iter().map(|s| (*s).to_owned()).collect())
            .unwrap_or_default())
    }

    async fn named(&self, _method: NamedMethod) -> Result<Vec<String>, ProviderError> {
        Ok(Vec::new())
    }
}

async fn seed_two_dates(conn: &mut PgConnection) -> (i64, i64, i64) {
    let old = make_instrument(conn, 1, "OLDCO", "EQ").await;
    let stay = make_instrument(conn, 2, "STAYCO", "EQ").await;
    let new = make_instrument(conn, 3, "NEWCO", "EQ").await;
    let provider = Changing::new(&[
        (past(), &["OLDCO", "STAYCO"]),
        (trade_date(), &["STAYCO", "NEWCO"]),
    ]);
    for day in [past(), trade_date()] {
        refresh_membership(conn, &provider, day, true)
            .await
            .unwrap();
    }
    (old, stay, new)
}

// --- TestCriterion1PointInTimeMembership ------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_past_date_keeps_its_own_constituents_and_today_differs() {
    let mut conn = scratch().await;
    let (old, stay, new) = seed_two_dates(&mut conn).await;
    let then = resolve(&mut conn, "nifty-500", past()).await;
    let now = resolve(&mut conn, "nifty-500", trade_date()).await;
    assert_eq!(then, vec![old, stay]);
    assert_eq!(now, vec![stay, new]);
    assert_ne!(then, now);
    // The survivorship case and the look-ahead case.
    assert!(then.contains(&old) && !now.contains(&old));
    assert!(!then.contains(&new));
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn refreshing_today_does_not_rewrite_the_past() {
    let mut conn = scratch().await;
    let (old, stay, _) = seed_two_dates(&mut conn).await;
    let provider = Changing::new(&[(trade_date(), &["STAYCO", "NEWCO"])]);
    refresh_membership(&mut conn, &provider, trade_date(), true)
        .await
        .unwrap();
    assert_eq!(
        resolve(&mut conn, "nifty-500", past()).await,
        vec![old, stay]
    );
    // An un-ingested date answers nothing rather than the latest membership.
    assert!(
        resolve(&mut conn, "nifty-500", d(2020, 1, 2))
            .await
            .is_empty()
    );
}

// --- TestMembershipSource ---------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_published_file_is_observed_and_a_missing_one_reconstructed() {
    let mut conn = scratch().await;
    make_instrument(&mut conn, 2, "STAYCO", "EQ").await;
    let provider = Changing::new(&[(trade_date(), &["STAYCO"])]);
    let today = refresh_membership(&mut conn, &provider, trade_date(), true)
        .await
        .unwrap();
    assert_eq!(
        sources(&mut conn, "nifty-500", trade_date()).await,
        BTreeMap::from([("nse_file".to_owned(), 1)])
    );
    let back = refresh_membership(&mut conn, &provider, past(), true)
        .await
        .unwrap();
    assert_eq!(
        sources(&mut conn, "nifty-500", past()).await,
        BTreeMap::from([("reconstructed".to_owned(), 1)])
    );
    // The step's own accounting says the same.
    assert_eq!(
        today.per_source,
        vec![("nse_file".to_owned(), 1)],
        "{today:?}"
    );
    assert!(back.per_source.contains(&("reconstructed".to_owned(), 1)));
    assert_eq!(today.per_universe.len(), UNIVERSES.len());
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn reconstruction_can_be_refused() {
    let mut conn = scratch().await;
    make_instrument(&mut conn, 2, "STAYCO", "EQ").await;
    let provider = Changing::new(&[(trade_date(), &["STAYCO"])]);
    refresh_membership(&mut conn, &provider, trade_date(), true)
        .await
        .unwrap();
    refresh_membership(&mut conn, &provider, past(), false)
        .await
        .unwrap();
    assert!(resolve(&mut conn, "nifty-500", past()).await.is_empty());
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn an_observed_row_is_never_downgraded_to_reconstructed() {
    let mut conn = scratch().await;
    make_instrument(&mut conn, 2, "STAYCO", "EQ").await;
    let observed = Changing::new(&[(past(), &["STAYCO"]), (trade_date(), &["STAYCO"])]);
    refresh_membership(&mut conn, &observed, past(), true)
        .await
        .unwrap();
    refresh_membership(&mut conn, &observed, trade_date(), true)
        .await
        .unwrap();
    let silent = Changing::new(&[(trade_date(), &["STAYCO"])]);
    refresh_membership(&mut conn, &silent, past(), true)
        .await
        .unwrap();
    assert_eq!(
        sources(&mut conn, "nifty-500", past()).await,
        BTreeMap::from([("nse_file".to_owned(), 1)])
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn allcap_is_derived_and_requires_a_bar_and_etf_needs_its_list() {
    let mut conn = scratch().await;
    let traded = make_instrument(&mut conn, 1, "TRADED", "EQ").await;
    make_instrument(&mut conn, 2, "SUSPENDED", "EQ").await;
    let etf = make_instrument(&mut conn, 3, "NIFTYBEES", "ETF").await;
    add_bar(&mut conn, traded, trade_date(), "100").await;
    add_bar(&mut conn, etf, trade_date(), "250").await;
    refresh_membership(&mut conn, &Changing::new(&[]), trade_date(), true)
        .await
        .unwrap();
    assert_eq!(
        resolve(&mut conn, "nifty-allcap", trade_date()).await,
        vec![traded]
    );
    assert_eq!(
        sources(&mut conn, "nifty-allcap", trade_date()).await,
        BTreeMap::from([("derived".to_owned(), 1)])
    );
    // `etf` comes from NSE's ETF list only; the stub publishes none, so nothing is written.
    assert!(resolve(&mut conn, "etf", trade_date()).await.is_empty());
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_provider_error_fails_one_universe_and_an_unexpected_payload_none() {
    let mut conn = scratch().await;
    make_instrument(&mut conn, 2, "STAYCO", "EQ").await;
    let mut provider = Changing::new(&[(trade_date(), &["STAYCO"])]);
    provider.fail = Some(("nifty-50", false));
    let result = refresh_membership(&mut conn, &provider, trade_date(), true)
        .await
        .unwrap();
    assert_eq!(result.failures.len(), 1);
    assert_eq!(result.failures[0].0, "nifty-50");
    assert!(!result.per_universe.iter().any(|(k, _)| k == "nifty-50"));
    assert_eq!(resolve(&mut conn, "nifty-500", trade_date()).await, vec![2]);

    provider.fail = Some(("nifty-50", true));
    let result = refresh_membership(&mut conn, &provider, trade_date(), true)
        .await
        .unwrap();
    assert!(result.failures.is_empty());
    assert!(result.per_universe.contains(&("nifty-50".to_owned(), 0)));
}

// --- test_snapshots.py and TestIndexRegistration ----------------------------------------------

fn snap(slug: &str, on: NaiveDate, level: Option<&str>) -> IndexSnapshot {
    let mut f = IndexSnapshotFields::new(slug, on);
    f.level = level.map(dec);
    IndexSnapshot::new(f).unwrap()
}

async fn level(conn: &mut PgConnection, slug: &str, on: NaiveDate) -> Option<String> {
    sqlx::query_as::<_, (Option<String>,)>(
        "SELECT s.level::text FROM index_snapshot_daily s JOIN index_def d ON d.id = s.index_id \
         WHERE d.slug = $1 AND s.date = $2",
    )
    .bind(slug)
    .bind(on)
    .fetch_optional(&mut *conn)
    .await
    .unwrap()
    .and_then(|(l,)| l)
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn the_writer_refuses_an_implausible_level_and_keeps_the_sane_rows() {
    let mut conn = scratch().await;
    let real = d(2026, 7, 7);
    let fixture = d(2026, 7, 8);
    let seeded = store_snapshots(
        &mut conn,
        real,
        &[
            snap("nifty-50", real, Some("24398.70")),
            snap("nifty-500", real, Some("23368.85")),
        ],
    )
    .await
    .unwrap();
    assert!(seeded.refused.is_empty());

    let result = store_snapshots(
        &mut conn,
        fixture,
        &[
            snap("nifty-50", fixture, Some("1004.6026")),
            snap("nifty-500", fixture, Some("23500.10")),
        ],
    )
    .await
    .unwrap();
    assert_eq!(result.rows_written, 1);
    assert_eq!(result.refused.len(), 1);
    assert!(
        result.refused[0].starts_with("nifty-50 2026-07-08: level 1004.6026 is -95.9%"),
        "{}",
        result.refused[0]
    );
    assert!(result.refused[0].contains("against 24398.7000 on 2026-07-07"));
    assert_eq!(level(&mut conn, "nifty-50", fixture).await, None);
    assert_eq!(
        level(&mut conn, "nifty-500", fixture).await.as_deref(),
        Some("23500.1000")
    );

    // The anchor is the last row within 14 days, across a gap.
    let after_gap = d(2026, 7, 13);
    let gap = store_snapshots(
        &mut conn,
        after_gap,
        &[snap("nifty-50", after_gap, Some("1004.60"))],
    )
    .await
    .unwrap();
    assert_eq!(gap.refused.len(), 1);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn rewriting_a_day_is_idempotent_and_the_step_note_names_refusals() {
    let mut conn = scratch().await;
    let real = d(2026, 7, 7);
    let fixture = d(2026, 7, 8);
    store_snapshots(&mut conn, real, &[snap("nifty-50", real, Some("24398.70"))])
        .await
        .unwrap();
    let mut row = IndexSnapshotFields::new("nifty-50", fixture);
    row.level = Some(dec("24500.35"));
    row.pe = Some(dec("22.4"));
    let rows = [IndexSnapshot::new(row).unwrap()];
    let first = store_snapshots(&mut conn, fixture, &rows).await.unwrap();
    let second = store_snapshots(&mut conn, fixture, &rows).await.unwrap();
    assert_eq!((first.rows_written, first.refused.len()), (1, 0));
    assert_eq!((second.rows_written, second.refused.len()), (1, 0));
    assert_eq!(
        level(&mut conn, "nifty-50", fixture).await.as_deref(),
        Some("24500.3500")
    );

    let refused = store_snapshots(
        &mut conn,
        d(2026, 7, 9),
        &[snap("nifty-50", d(2026, 7, 9), Some("1004.6026"))],
    )
    .await
    .unwrap();
    let mut outcome = StepOutcome::default();
    note_result(&mut outcome, d(2026, 7, 9), &refused);
    let detail = outcome.detail_json();
    let Some(baskfy_db::OrderedJson::Array(names)) = detail.get("refused") else {
        panic!("refused is not a list: {detail:?}");
    };
    assert_eq!(names.len(), 1);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_new_index_is_registered_outside_the_mask_range_once() {
    let mut conn = scratch().await;
    let on = trade_date();
    let mut vix = IndexSnapshotFields::new("India VIX", on);
    vix.level = Some(dec("12.5"));
    vix.change_pct = Some(dec("4.0"));
    let batch = [
        snap("NIFTY 50", on, Some("24500.35")),
        IndexSnapshot::new(vix).unwrap(),
        snap("Nifty50 PR 1x Inverse", on, Some("980.10")),
        snap("!!!", on, Some("1")),
    ];
    let result = store_snapshots(&mut conn, on, &batch).await.unwrap();
    // "NIFTY 50" slugifies onto the pinned universe; the other two are new; "!!!" has no name.
    assert_eq!(result.registered, ["india-vix", "nifty50-pr-1x-inverse"]);
    assert_eq!(result.unnamed, 1);
    assert_eq!(result.rows_written, 3);
    let rows: Vec<(i16, String, String, bool, i16)> = sqlx::query_as(
        "SELECT id, slug, name, is_universe, sort_order FROM index_def WHERE id >= 100 ORDER BY id",
    )
    .fetch_all(&mut conn)
    .await
    .unwrap();
    assert_eq!(
        rows,
        vec![
            (
                FIRST_ID,
                "india-vix".into(),
                "India VIX".into(),
                false,
                FIRST_ID
            ),
            (
                FIRST_ID + 1,
                "nifty50-pr-1x-inverse".into(),
                "Nifty50 PR 1x Inverse".into(),
                false,
                FIRST_ID + 1
            ),
        ]
    );
    // India VIX publishes no fundamentals: NULL, not zero.
    let (pe,): (Option<Decimal>,) =
        sqlx::query_as("SELECT pe FROM index_snapshot_daily WHERE index_id = $1 AND date = $2")
            .bind(FIRST_ID)
            .bind(on)
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(pe, None);
    // The pinned universes keep their ids.
    let pinned: Vec<(i16, String)> =
        sqlx::query_as("SELECT id, slug FROM index_def WHERE id < 100 ORDER BY id")
            .fetch_all(&mut conn)
            .await
            .unwrap();
    for (u, (id, slug)) in UNIVERSES.iter().zip(&pinned) {
        assert_eq!((u.index_id, u.slug), (*id, slug.as_str()));
    }

    // Registering again is idempotent; a later batch takes the next id.
    let again = store_snapshots(&mut conn, on, &batch).await.unwrap();
    assert!(again.registered.is_empty());
    let next = store_snapshots(&mut conn, on, &[snap("NIFTY IT", on, None)])
        .await
        .unwrap();
    assert_eq!(next.registered, ["nifty-it"]);
    let ids: BTreeSet<i16> =
        sqlx::query_as::<_, (i16,)>("SELECT id FROM index_def WHERE id >= 100")
            .fetch_all(&mut conn)
            .await
            .unwrap()
            .into_iter()
            .map(|(i,)| i)
            .collect();
    assert_eq!(ids.len(), 3);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_hundred_and_fifty_indices_fit() {
    let mut conn = scratch().await;
    let on = trade_date();
    let batch: Vec<IndexSnapshot> = (0..150)
        .map(|n| snap(&format!("NIFTY SECTOR {n}"), on, Some(&n.to_string())))
        .collect();
    let result = store_snapshots(&mut conn, on, &batch).await.unwrap();
    assert_eq!(result.rows_written, 150);
    assert_eq!(result.registered.len(), 150);
}

// --- test_fundamentals.py ---------------------------------------------------------------------

fn fundamental(symbol: &str, shares: i64, last: &str, pe: Option<&str>) -> EquityFundamental {
    let mut f = EquityFundamentalFields::new(symbol, trade_date());
    f.shares_outstanding = Some(shares);
    f.last_price = Some(dec(last));
    f.marketcap_cr = Some(1);
    f.pe = pe.map(dec);
    EquityFundamental::new(f).unwrap()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn it_upserts_marketcap_from_shares_and_close_raw() {
    let mut conn = scratch().await;
    let id = make_instrument(&mut conn, 7, "INFY", "EQ").await;
    add_bar(&mut conn, id, trade_date(), "1850.25").await;
    let records = [fundamental("INFY", 4_148_506_959, "9999", Some("24.50"))];
    let result = store_fundamentals(&mut conn, trade_date(), &records)
        .await
        .unwrap();
    assert_eq!(result.rows_written, 1);
    let row: (Option<i64>, Option<String>, Option<i64>) = sqlx::query_as(
        "SELECT marketcap_cr, pe::text, shares_outstanding FROM fundamental_daily \
         WHERE instrument_id = $1 AND date = $2",
    )
    .bind(id)
    .bind(trade_date())
    .fetch_one(&mut conn)
    .await
    .unwrap();
    // 4_148_506_959 * 1850.25 / 1e7 = 767_578; 24.50 * 1850.25 / 9999 = 4.5336.
    assert_eq!(
        row,
        (
            Some(767_578),
            Some("4.5336".to_owned()),
            Some(4_148_506_959)
        )
    );

    // Re-running the day writes identical rows (house rule 7).
    store_fundamentals(&mut conn, trade_date(), &records)
        .await
        .unwrap();
    let (n,): (i64,) = sqlx::query_as("SELECT count(*) FROM fundamental_daily")
        .fetch_one(&mut conn)
        .await
        .unwrap();
    assert_eq!(n, 1);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn an_unknown_symbol_is_skipped_not_invented() {
    let mut conn = scratch().await;
    let records = [fundamental("NOTALISTED", 1_000_000, "10", None)];
    let result = store_fundamentals(&mut conn, trade_date(), &records)
        .await
        .unwrap();
    assert_eq!((result.rows_written, result.unmatched), (0, 1));
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn the_step_scopes_to_the_days_traded_names() {
    let mut conn = scratch().await;
    let traded = make_instrument(&mut conn, 1, "TRADED", "EQ").await;
    add_bar(&mut conn, traded, trade_date(), "100").await;
    make_instrument(&mut conn, 2, "UNTRADED", "EQ").await;
    assert_eq!(
        fundamentals_scope(&mut conn, trade_date()).await.unwrap(),
        vec![("TRADED".to_owned(), Some("EQ".to_owned()))]
    );
}
