//! `test_fundamentals_cli.py`, on an `rst_*` clone of the bed (`BASKFY_RS_CLIS_DB_URL`), each
//! test inside one transaction that is rolled back; a far-future date keeps the clone's own rows
//! out of scope.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::cell::RefCell;
use std::collections::HashMap;

use baskfy_providers::ProviderError;
use baskfy_providers::records::{EquityFundamental, EquityFundamentalFields};
use chrono::{Days, NaiveDate};
use rust_decimal::Decimal;
use sqlx::{Connection as _, PgConnection};

use super::super::factors::db_tests::clone_conn;
use super::super::factors::session::Flat;
use super::{FillOptions, FillReport, FundamentalsSource, default_date, fill};
use crate::steps::index::fundamentals::fundamentals_scope;

fn trade_date() -> NaiveDate {
    NaiveDate::from_ymd_opt(2031, 1, 2).unwrap()
}

const QUIET: FillOptions = FillOptions {
    resume: false,
    limit: None,
    dry_run: false,
    progress: false,
};

type Fail = fn(String) -> ProviderError;

struct Stub {
    quotes: HashMap<String, EquityFundamental>,
    failing: Vec<String>,
    error: Fail,
    calls: RefCell<Vec<(String, Option<String>)>>,
}

impl Stub {
    fn new(quotes: &[(&str, EquityFundamental)]) -> Self {
        Self {
            quotes: quotes
                .iter()
                .map(|(s, q)| ((*s).to_owned(), q.clone()))
                .collect(),
            failing: Vec::new(),
            error: ProviderError::upstream_unavailable,
            calls: RefCell::new(Vec::new()),
        }
    }

    fn failing(mut self, symbol: &str, error: Fail) -> Self {
        self.failing.push(symbol.to_owned());
        self.error = error;
        self
    }

    fn called(&self) -> Vec<String> {
        self.calls.borrow().iter().map(|c| c.0.clone()).collect()
    }
}

impl FundamentalsSource for Stub {
    async fn equity_fundamentals(
        &self,
        _on: NaiveDate,
        symbols: &[String],
        series_by_symbol: Option<&HashMap<String, String>>,
    ) -> Result<Vec<EquityFundamental>, ProviderError> {
        let mut out = Vec::new();
        for symbol in symbols {
            let hint = series_by_symbol.and_then(|m| m.get(symbol)).cloned();
            self.calls.borrow_mut().push((symbol.clone(), hint));
            if self.failing.contains(symbol) {
                return Err((self.error)(format!("NSE refused {symbol}")).with_provider("nse"));
            }
            if let Some(q) = self.quotes.get(symbol) {
                out.push(q.clone());
            }
        }
        Ok(out)
    }
}

fn quote(symbol: &str) -> EquityFundamental {
    EquityFundamental::new(EquityFundamentalFields {
        shares_outstanding: Some(1_000_000_000),
        last_price: Some(Decimal::from(100)),
        pe: Some(Decimal::from(20)),
        ..EquityFundamentalFields::new(symbol, trade_date())
    })
    .unwrap()
}

const A: &str = "ZZFA_AAA";
const B: &str = "ZZFA_BBB";
const C: &str = "ZZFA_CCC";

async fn make_instrument(conn: &mut PgConnection, symbol: &str, series: &str) -> i64 {
    sqlx::query_scalar(
        "INSERT INTO instrument (exchange_id, symbol, name, instrument_type, is_active, series) \
         VALUES (1, $1, $1, 'EQ', true, $2) RETURNING id",
    )
    .bind(symbol)
    .bind(series)
    .fetch_one(&mut *conn)
    .await
    .unwrap()
}

async fn add_bar(conn: &mut PgConnection, id: i64, on: NaiveDate) {
    sqlx::query(
        "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, volume, close_raw, \
         volume_raw, source) VALUES ($1, $2, 100, 100, 100, 100, 1, 100, 1, 'kite')",
    )
    .bind(id)
    .bind(on)
    .execute(&mut *conn)
    .await
    .unwrap();
}

async fn universe(conn: &mut PgConnection) {
    for (symbol, series) in [(A, "EQ"), (B, "EQ"), (C, "BE")] {
        let id = make_instrument(conn, symbol, series).await;
        add_bar(conn, id, trade_date()).await;
    }
    make_instrument(conn, "ZZFA_DDD", "EQ").await;
}

async fn run(conn: &mut PgConnection, stub: &Stub, opts: FillOptions) -> FillReport {
    fill(&mut Flat(conn), stub, trade_date(), opts)
        .await
        .unwrap()
}

async fn rows(conn: &mut PgConnection) -> Vec<(i64, Option<i64>, Option<String>)> {
    sqlx::query_as(
        "SELECT instrument_id, marketcap_cr, pe::text FROM fundamental_daily WHERE date = $1 \
         ORDER BY instrument_id",
    )
    .bind(trade_date())
    .fetch_all(&mut *conn)
    .await
    .unwrap()
}

static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

macro_rules! db_test {
    ($name:ident, |$tx:ident| $body:block) => {
        #[tokio::test]
        #[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
        async fn $name() {
            let _guard = SERIAL.lock().await;
            let mut conn = clone_conn().await;
            let mut $tx = conn.begin().await.unwrap();
            universe(&mut $tx).await;
            $body
            $tx.rollback().await.unwrap();
        }
    };
}

db_test!(scope_is_the_dates_own_universe_with_its_series, |tx| {
    let scope = fundamentals_scope(&mut tx, trade_date()).await.unwrap();
    assert_eq!(
        scope,
        vec![
            (A.to_owned(), Some("EQ".to_owned())),
            (B.to_owned(), Some("EQ".to_owned())),
            (C.to_owned(), Some("BE".to_owned())),
        ]
    );
});

async fn unpublish_everything(conn: &mut PgConnection) {
    sqlx::query("UPDATE pipeline_run SET data_version = NULL")
        .execute(&mut *conn)
        .await
        .unwrap();
}

async fn add_run(conn: &mut PgConnection, on: NaiveDate, status: &str, version: Option<i64>) {
    sqlx::query(
        "INSERT INTO pipeline_run (trade_date, status, started_at, data_version) \
         VALUES ($1, $2, now(), $3)",
    )
    .bind(on)
    .bind(status)
    .bind(version)
    .execute(&mut *conn)
    .await
    .unwrap();
}

db_test!(it_prefers_the_published_date_over_the_newest_bars, |tx| {
    let newer = trade_date().checked_add_days(Days::new(3)).unwrap();
    let id = make_instrument(&mut tx, "ZZFA_NEW", "EQ").await;
    add_bar(&mut tx, id, newer).await;
    add_run(&mut tx, trade_date(), "succeeded", Some(1)).await;
    let (chosen, why) = default_date(&mut tx).await.unwrap();
    assert_eq!(chosen, Some(trade_date()));
    assert!(why.contains("published"));
});

db_test!(
    with_nothing_published_it_falls_back_to_the_newest_bars,
    |tx| {
        unpublish_everything(&mut tx).await;
        let (chosen, why) = default_date(&mut tx).await.unwrap();
        assert_eq!(chosen, Some(trade_date()));
        assert!(why.contains("published"));
    }
);

db_test!(an_unpublished_run_does_not_count_as_published, |tx| {
    unpublish_everything(&mut tx).await;
    let newer = trade_date().checked_add_days(Days::new(3)).unwrap();
    let id = make_instrument(&mut tx, "ZZFA_NEW", "EQ").await;
    add_bar(&mut tx, id, newer).await;
    add_run(&mut tx, newer, "failed", None).await;
    let (chosen, _) = default_date(&mut tx).await.unwrap();
    assert_eq!(chosen, Some(newer));
});

db_test!(it_stores_a_row_per_quoted_symbol, |tx| {
    let stub = Stub::new(&[(A, quote(A)), (B, quote(B)), (C, quote(C))]);
    let report = run(&mut tx, &stub, QUIET).await;
    assert_eq!(report.scoped, 3);
    assert_eq!(report.stored, 3);
    assert_eq!(rows(&mut tx).await.len(), 3);
});

db_test!(the_series_hint_reaches_the_provider, |tx| {
    let stub = Stub::new(&[(C, quote(C))]);
    run(&mut tx, &stub, QUIET).await;
    assert!(
        stub.calls
            .borrow()
            .contains(&(C.to_owned(), Some("BE".to_owned())))
    );
});

db_test!(running_it_twice_produces_identical_rows, |tx| {
    let stub = Stub::new(&[(A, quote(A)), (B, quote(B)), (C, quote(C))]);
    run(&mut tx, &stub, QUIET).await;
    let first = rows(&mut tx).await;
    run(&mut tx, &stub, QUIET).await;
    assert_eq!(rows(&mut tx).await, first);
});

db_test!(resume_skips_what_is_already_stored, |tx| {
    let first = Stub::new(&[(A, quote(A))]);
    run(
        &mut tx,
        &first,
        FillOptions {
            limit: Some(1),
            ..QUIET
        },
    )
    .await;
    let second = Stub::new(&[(B, quote(B)), (C, quote(C))]);
    let report = run(
        &mut tx,
        &second,
        FillOptions {
            resume: true,
            ..QUIET
        },
    )
    .await;
    assert_eq!(report.already_present, 1);
    assert_eq!(second.called(), vec![B.to_owned(), C.to_owned()]);
});

db_test!(without_resume_every_symbol_is_attempted_again, |tx| {
    run(&mut tx, &Stub::new(&[(A, quote(A))]), QUIET).await;
    let stub = Stub::new(&[(A, quote(A))]);
    let report = run(&mut tx, &stub, QUIET).await;
    assert_eq!(report.already_present, 0);
    assert_eq!(
        stub.called(),
        vec![A.to_owned(), B.to_owned(), C.to_owned()]
    );
});

db_test!(a_dry_run_touches_neither_nse_nor_the_database, |tx| {
    let stub = Stub::new(&[(A, quote(A))]);
    let report = run(
        &mut tx,
        &stub,
        FillOptions {
            dry_run: true,
            ..QUIET
        },
    )
    .await;
    assert!(stub.called().is_empty());
    assert_eq!(report.stored, 0);
    assert!(rows(&mut tx).await.is_empty());
    assert!(!report.accounted_for());
    assert!(report.render().contains("DRY RUN"));
});

db_test!(every_scoped_symbol_lands_in_exactly_one_bucket, |tx| {
    let stub = Stub::new(&[(A, quote(A))]).failing(B, ProviderError::upstream_unavailable);
    let report = run(&mut tx, &stub, QUIET).await;
    assert_eq!(report.scoped, 3);
    assert_eq!(report.stored, 1);
    assert_eq!(report.no_quote, vec![C.to_owned()]);
    assert_eq!(report.failed.len(), 1);
    assert_eq!(report.failed[0].0, B);
    assert!(report.failed[0].1.contains("NSE refused ZZFA_BBB"));
    assert!(report.render().contains(B));
    assert!(report.accounted_for());
});

db_test!(a_quote_for_an_unknown_symbol_is_counted_not_dropped, |tx| {
    let stray = EquityFundamental::new(EquityFundamentalFields {
        shares_outstanding: Some(1),
        last_price: Some(Decimal::ONE),
        ..EquityFundamentalFields::new("ZZFA_NOTINDB", trade_date())
    })
    .unwrap();
    let report = run(&mut tx, &Stub::new(&[(A, stray)]), QUIET).await;
    assert_eq!(report.unmatched, 1);
    assert_eq!(report.stored, 0);
    assert!(report.accounted_for());
    assert!(report.render().contains("unmatched=1"));
});

db_test!(any_provider_error_is_recorded_not_fatal, |tx| {
    let errors: [(Fail, &str); 4] = [
        (ProviderError::upstream_unavailable, "UpstreamUnavailable"),
        (ProviderError::unexpected_payload, "UnexpectedPayload"),
        (ProviderError::archive_error, "ArchiveError"),
        (ProviderError::provider_unavailable, "ProviderUnavailable"),
    ];
    for (error, name) in errors {
        let stub = Stub::new(&[(A, quote(A)), (C, quote(C))]).failing(B, error);
        let report = run(&mut tx, &stub, QUIET).await;
        assert_eq!(report.stored, 2, "{name}");
        assert_eq!(report.failed.len(), 1);
        assert_eq!(report.failed[0].0, B);
        assert!(
            report.failed[0].1.starts_with(name),
            "{}",
            report.failed[0].1
        );
        assert!(report.accounted_for());
    }
});
