//! `test_reference_data.py`'s `TestReferenceBackfill` and `TestCriterion3IndexSnapshots`, on an
//! `rst_*` clone of the bed (`BASKFY_RS_CLIS_DB_URL`). The backfill commits per day, so it runs
//! over future sessions the clone has no rows for, and deletes what it wrote afterwards.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use baskfy_db::{DbSettings, WriteDb};
use baskfy_providers::ProviderError;
use baskfy_providers::composite::{CompositeProvider, Registered};
use baskfy_providers::ports::{
    Capability, CapabilitySet, HealthReporting, ProviderHealth, ReferenceProvider,
};
use baskfy_providers::records::{
    CorporateAction, EquityFundamental, IndexSnapshot, IndexSnapshotFields, ListingRecord,
};
use baskfy_providers::secret::Secret;
use chrono::{Datelike as _, NaiveDate, Weekday};
use polars::prelude::DataFrame;
use rust_decimal::Decimal;

use super::{backfill_reference_data, trading_days_in};
use crate::window::DateWindow;

type Fut<'a, T> = Pin<Box<dyn Future<Output = Result<T, ProviderError>> + Send + 'a>>;

struct Snapshots;

impl HealthReporting for Snapshots {
    fn name(&self) -> &str {
        "snapshots"
    }

    fn capabilities(&self) -> CapabilitySet {
        [
            Capability::IndexConstituents,
            Capability::IndexSnapshots,
            Capability::Listings,
        ]
        .into()
    }

    fn check(&self) -> ProviderHealth {
        ProviderHealth {
            name: "snapshots".to_owned(),
            available: true,
            capabilities: self.capabilities(),
            detail: "test".to_owned(),
        }
    }
}

fn snapshot(slug: &str, on: NaiveDate, level: &str) -> IndexSnapshot {
    IndexSnapshot::new(IndexSnapshotFields {
        level: Some(level.parse::<Decimal>().unwrap()),
        change_abs: Some(Decimal::ONE),
        change_pct: Some(Decimal::ONE),
        ..IndexSnapshotFields::new(slug, on)
    })
    .unwrap()
}

impl ReferenceProvider for Snapshots {
    fn index_constituents<'life0, 'life1, 'async_trait>(
        &'life0 self,
        _index_slug: &'life1 str,
        _on: NaiveDate,
    ) -> Fut<'async_trait, Vec<String>>
    where
        'life0: 'async_trait,
        'life1: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Ok(Vec::new()) })
    }

    fn index_snapshots<'life0, 'async_trait>(
        &'life0 self,
        on: NaiveDate,
    ) -> Fut<'async_trait, Vec<IndexSnapshot>>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async move {
            Ok(vec![
                snapshot("NIFTY 50", on, "25000.5"),
                snapshot("India VIX", on, "12.5"),
            ])
        })
    }

    fn corporate_actions<'life0, 'async_trait>(
        &'life0 self,
        _since: NaiveDate,
    ) -> Fut<'async_trait, Vec<CorporateAction>>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Ok(Vec::new()) })
    }

    fn listings<'life0, 'async_trait>(&'life0 self) -> Fut<'async_trait, Vec<ListingRecord>>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Ok(Vec::new()) })
    }

    fn bhavcopy<'life0, 'async_trait>(&'life0 self, _on: NaiveDate) -> Fut<'async_trait, DataFrame>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Err(ProviderError::provider_unavailable("no bhavcopy")) })
    }

    fn equity_fundamentals<'life0, 'life1, 'life2, 'async_trait>(
        &'life0 self,
        _on: NaiveDate,
        _symbols: &'life1 [String],
        _series_by_symbol: Option<&'life2 HashMap<String, String>>,
    ) -> Fut<'async_trait, Vec<EquityFundamental>>
    where
        'life0: 'async_trait,
        'life1: 'async_trait,
        'life2: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Ok(Vec::new()) })
    }
}

fn composite() -> Arc<CompositeProvider> {
    let stub = Arc::new(Snapshots);
    Arc::new(
        CompositeProvider::with_defaults(vec![Registered::new(stub.clone()).with_reference(stub)])
            .unwrap(),
    )
}

fn db() -> WriteDb {
    let url = std::env::var("BASKFY_RS_CLIS_DB_URL")
        .unwrap_or_else(|_| panic!("needs BASKFY_RS_CLIS_DB_URL (an rst_* clone of the bed)"));
    let name = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    assert!(
        name.starts_with("rst_"),
        "refusing the database {name:?}: only rst_* clones"
    );
    WriteDb::connect_lazy(&DbSettings::with_url(Secret::new(url.as_str()))).unwrap()
}

fn window() -> DateWindow {
    DateWindow::new(
        NaiveDate::from_ymd_opt(2026, 11, 2).unwrap(),
        NaiveDate::from_ymd_opt(2026, 11, 10).unwrap(),
    )
    .unwrap()
}

async fn snapshot_rows(db: &WriteDb) -> Vec<(i16, NaiveDate)> {
    let mut tx = db.begin().await.unwrap();
    let rows = sqlx::query_as(
        "SELECT index_id, date FROM index_snapshot_daily WHERE date BETWEEN $1 AND $2 \
         ORDER BY date, index_id",
    )
    .bind(window().start())
    .bind(window().end())
    .fetch_all(&mut *tx)
    .await
    .unwrap();
    tx.rollback().await.unwrap();
    rows
}

async fn cleanup(db: &WriteDb) {
    let mut tx = db.begin().await.unwrap();
    for table in [
        "index_snapshot_daily",
        "index_member_daily",
        "market_health_daily",
    ] {
        sqlx::query(&format!("DELETE FROM {table} WHERE date BETWEEN $1 AND $2"))
            .bind(window().start())
            .bind(window().end())
            .execute(&mut *tx)
            .await
            .unwrap();
    }
    tx.commit().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn reference_backfill_covers_trading_days_only_and_is_idempotent() {
    let db = db();
    cleanup(&db).await;
    let expected = trading_days_in(&db, window()).await.unwrap();
    assert_eq!(expected.len(), 6);

    let report = backfill_reference_data(&db, composite(), window(), true, true)
        .await
        .unwrap();
    assert!(report.succeeded(), "{:?}", report.failures);
    assert_eq!(report.trading_days, expected.len());
    assert_eq!(report.snapshot_rows, 2 * 6);
    assert_eq!(report.market_health_rows, 12 * 6);
    let first = snapshot_rows(&db).await;
    let mut days: Vec<NaiveDate> = first.iter().map(|r| r.1).collect();
    days.dedup();
    assert_eq!(days, expected);
    assert!(
        days.iter()
            .all(|d| !matches!(d.weekday(), Weekday::Sat | Weekday::Sun))
    );

    let again = backfill_reference_data(&db, composite(), window(), false, false)
        .await
        .unwrap();
    assert!(again.succeeded());
    assert_eq!(again.listings_written, 0);
    assert_eq!(snapshot_rows(&db).await, first);
    cleanup(&db).await;
    db.close().await;
}
