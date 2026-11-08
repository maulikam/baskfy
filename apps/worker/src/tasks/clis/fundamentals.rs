//! `baskfy_worker.fundamentals_cli`: `fill`, the one-off `equity_fundamentals` fetch for a date.

#[cfg(test)]
mod db_tests;

use std::collections::{HashMap, HashSet};
use std::future::Future;

use baskfy_providers::ProviderError;
use baskfy_providers::composite::CompositeProvider;
use baskfy_providers::records::EquityFundamental;
use chrono::NaiveDate;
use sqlx::PgConnection;

use super::cli::{
    Failure, Outcome, Parsed, Spec, code, command, connect, iso, parse_date, say, warn,
};
use super::factors::session::{Checkpointed, Session};
use crate::error::WorkerError;
use crate::steps::index::fundamentals::{fundamentals_scope, store_fundamentals};

/// `BATCH_SIZE`: symbols per commit.
pub(super) const BATCH_SIZE: usize = 25;

/// `PROGRESS_EVERY`.
const PROGRESS_EVERY: usize = 100;

/// `FillOptions`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct FillOptions {
    pub resume: bool,
    pub limit: Option<i64>,
    pub dry_run: bool,
    pub progress: bool,
}

impl Default for FillOptions {
    fn default() -> Self {
        Self {
            resume: false,
            limit: None,
            dry_run: false,
            progress: true,
        }
    }
}

/// `FillReport`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct FillReport {
    pub date: NaiveDate,
    pub scoped: usize,
    pub already_present: usize,
    pub stored: i64,
    pub no_quote: Vec<String>,
    pub failed: Vec<(String, String)>,
    pub unmatched: i64,
    pub dry_run: bool,
}

impl FillReport {
    fn new(date: NaiveDate, dry_run: bool) -> Self {
        Self {
            date,
            scoped: 0,
            already_present: 0,
            stored: 0,
            no_quote: Vec::new(),
            failed: Vec::new(),
            unmatched: 0,
            dry_run,
        }
    }

    /// `attempted`.
    pub(super) fn attempted(&self) -> i64 {
        to_i64(self.scoped) - to_i64(self.already_present)
    }

    /// `accounted_for()`: a dry run accounts for nothing.
    pub(super) fn accounted_for(&self) -> bool {
        if self.dry_run {
            return false;
        }
        to_i64(self.already_present)
            + self.stored
            + self.unmatched
            + to_i64(self.no_quote.len())
            + to_i64(self.failed.len())
            == to_i64(self.scoped)
    }

    /// `render()`.
    pub(super) fn render(&self) -> String {
        if self.dry_run {
            return format!(
                "DRY RUN date={} scoped={} would_fetch={} already_present={}",
                iso(self.date),
                self.scoped,
                self.attempted(),
                self.already_present
            );
        }
        let mut lines = vec![
            format!(
                "FILL date={} scoped={} attempted={} stored={} already_present={} no_quote={} \
                 unmatched={} failed={}",
                iso(self.date),
                self.scoped,
                self.attempted(),
                self.stored,
                self.already_present,
                self.no_quote.len(),
                self.unmatched,
                self.failed.len()
            ),
            format!(
                "ACCOUNTED {}",
                if self.accounted_for() {
                    "OK"
                } else {
                    "MISMATCH"
                }
            ),
        ];
        if !self.no_quote.is_empty() {
            let mut names = self.no_quote.clone();
            names.sort();
            lines.push(format!("no quote from NSE: {}", names.join(", ")));
        }
        if !self.failed.is_empty() {
            let mut failed = self.failed.clone();
            failed.sort();
            lines.push("failed:".to_owned());
            lines.extend(
                failed
                    .iter()
                    .map(|(symbol, reason)| format!("  {symbol}: {reason}")),
            );
        }
        lines.join("\n")
    }
}

fn to_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// The one provider read `fill` makes (`provider.equity_fundamentals`).
pub(super) trait FundamentalsSource {
    fn equity_fundamentals(
        &self,
        on: NaiveDate,
        symbols: &[String],
        series_by_symbol: Option<&HashMap<String, String>>,
    ) -> impl Future<Output = Result<Vec<EquityFundamental>, ProviderError>>;
}

impl FundamentalsSource for CompositeProvider {
    async fn equity_fundamentals(
        &self,
        on: NaiveDate,
        symbols: &[String],
        series_by_symbol: Option<&HashMap<String, String>>,
    ) -> Result<Vec<EquityFundamental>, ProviderError> {
        Self::equity_fundamentals(self, on, symbols, series_by_symbol).await
    }
}

/// `default_date(session)`: the latest published date, else the newest bar date.
pub(super) async fn default_date(
    conn: &mut PgConnection,
) -> Result<(Option<NaiveDate>, &'static str), WorkerError> {
    let published: Option<NaiveDate> = sqlx::query_scalar(
        "SELECT max(pipeline_run.trade_date) FROM pipeline_run \
         WHERE pipeline_run.data_version IS NOT NULL",
    )
    .fetch_one(&mut *conn)
    .await?;
    if let Some(day) = published {
        return Ok((Some(day), "the latest published date (what the API serves)"));
    }
    let newest: Option<NaiveDate> =
        sqlx::query_scalar("SELECT max(ohlcv_daily.date) FROM ohlcv_daily")
            .fetch_one(&mut *conn)
            .await?;
    Ok((
        newest,
        "the newest bar date (nothing has been published yet)",
    ))
}

/// `already_stored(session, on)`: upper-cased symbols with a `fundamental_daily` row for `on`.
pub(super) async fn already_stored(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<HashSet<String>, WorkerError> {
    let rows: Vec<String> = sqlx::query_scalar(
        "SELECT instrument.symbol FROM instrument JOIN fundamental_daily \
         ON fundamental_daily.instrument_id = instrument.id WHERE fundamental_daily.date = $1",
    )
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|s| s.to_uppercase()).collect())
}

fn sliced<T>(mut scope: Vec<T>, limit: Option<i64>) -> Vec<T> {
    let Some(limit) = limit else {
        return scope;
    };
    let len = to_i64(scope.len());
    let keep = if limit < 0 {
        (len + limit).max(0)
    } else {
        limit.min(len)
    };
    scope.truncate(usize::try_from(keep).unwrap_or(0));
    scope
}

/// `fill(session, provider, on, options, checkpoint)`.
pub(super) async fn fill<S: Session, P: FundamentalsSource>(
    session: &mut S,
    provider: &P,
    on: NaiveDate,
    opts: FillOptions,
) -> Result<FillReport, WorkerError> {
    let mut report = FillReport::new(on, opts.dry_run);
    let scope = sliced(fundamentals_scope(session.conn(), on).await?, opts.limit);
    report.scoped = scope.len();
    let done = if opts.resume {
        already_stored(session.conn(), on).await?
    } else {
        HashSet::new()
    };
    let pending: Vec<(String, Option<String>)> = scope
        .into_iter()
        .filter(|(symbol, _)| !done.contains(&symbol.to_uppercase()))
        .collect();
    report.already_present = report.scoped - pending.len();
    if opts.progress {
        say(&format!(
            "scope={} pending={} already_present={} date={}",
            report.scoped,
            pending.len(),
            report.already_present,
            iso(on)
        ));
    }
    if opts.dry_run {
        return Ok(report);
    }
    for (index, batch) in pending.chunks(BATCH_SIZE).enumerate() {
        fill_batch(session.conn(), provider, on, batch, &mut report).await?;
        session.checkpoint().await?;
        let reached = index * BATCH_SIZE + batch.len();
        if opts.progress && reached % PROGRESS_EVERY < BATCH_SIZE {
            say(&format!(
                "  {reached}/{} stored={} no_quote={} failed={}",
                pending.len(),
                report.stored,
                report.no_quote.len(),
                report.failed.len()
            ));
        }
    }
    Ok(report)
}

async fn fill_batch<P: FundamentalsSource>(
    conn: &mut PgConnection,
    provider: &P,
    on: NaiveDate,
    batch: &[(String, Option<String>)],
    report: &mut FillReport,
) -> Result<(), WorkerError> {
    let mut records: Vec<EquityFundamental> = Vec::new();
    for (symbol, series) in batch {
        let hint: Option<HashMap<String, String>> = series
            .as_ref()
            .filter(|s| !s.is_empty())
            .map(|s| HashMap::from([(symbol.clone(), s.clone())]));
        match provider
            .equity_fundamentals(on, std::slice::from_ref(symbol), hint.as_ref())
            .await
        {
            Ok(got) if got.is_empty() => report.no_quote.push(symbol.clone()),
            Ok(got) => records.extend(got),
            Err(e) if e.is_provider_error() => report
                .failed
                .push((symbol.clone(), format!("{}: {e}", e.python_type()))),
            Err(e) => return Err(WorkerError::Provider(e)),
        }
    }
    if !records.is_empty() {
        let result = store_fundamentals(conn, on, &records).await?;
        report.stored += result.rows_written;
        report.unmatched += result.unmatched;
    }
    Ok(())
}

pub(super) async fn run(args: &[String]) -> Outcome {
    let (_, rest) = command(args, &["fill"])?;
    let parsed = Parsed::parse(
        rest,
        Spec {
            valued: &["--date", "--limit"],
            switches: &["--resume", "--dry-run"],
            ..Spec::default()
        },
    )?;
    let limit = parsed.int("--limit")?;
    let date = parsed.get("--date").map(parse_date).transpose();
    let provider = crate::tasks::context::provider(None)
        .await
        .map_err(|e| Failure::Raised(WorkerError::Settings(e)))?;
    let date = date?;
    let opts = FillOptions {
        resume: parsed.on("--resume"),
        limit,
        dry_run: parsed.on("--dry-run"),
        progress: true,
    };
    let db = connect(None)?;
    let outcome = operation(&db, &provider, date, opts).await;
    db.close().await;
    let report = match outcome {
        Ok(Ok(report)) => report,
        Ok(Err(lookup)) => {
            warn(&lookup);
            return Ok(code(false));
        }
        Err(e) => return Err(Failure::Raised(e)),
    };
    say(&report.render());
    if report.dry_run {
        return Ok(code(true));
    }
    Ok(code(report.accounted_for() && report.failed.is_empty()))
}

async fn operation(
    db: &baskfy_db::WriteDb,
    provider: &CompositeProvider,
    date: Option<NaiveDate>,
    opts: FillOptions,
) -> Result<Result<FillReport, String>, WorkerError> {
    let mut session = Checkpointed::open(db).await?;
    let on = match date {
        Some(on) => on,
        None => {
            let (resolved, why) = default_date(session.conn()).await?;
            let Some(on) = resolved else {
                return Ok(Err(
                    "no bars in ohlcv_daily, so there is no date to fill".to_owned()
                ));
            };
            say(&format!("no --date given; filling {} — {why}", iso(on)));
            on
        }
    };
    let report = fill(&mut session, provider, on, opts).await?;
    session.finish().await?;
    Ok(Ok(report))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::*;

    fn report() -> FillReport {
        FillReport::new(NaiveDate::from_ymd_opt(2026, 8, 21).unwrap(), false)
    }

    #[test]
    fn a_dry_run_never_claims_to_be_accounted_for() {
        let mut r = FillReport::new(NaiveDate::from_ymd_opt(2026, 8, 21).unwrap(), true);
        r.scoped = 3;
        assert!(!r.accounted_for());
        assert_eq!(
            r.render(),
            "DRY RUN date=2026-08-21 scoped=3 would_fetch=3 already_present=0"
        );
    }

    #[test]
    fn every_bucket_is_counted_and_named() {
        let mut r = report();
        r.scoped = 4;
        r.stored = 1;
        r.unmatched = 1;
        r.no_quote = vec!["CCC".to_owned()];
        r.failed = vec![(
            "BBB".to_owned(),
            "UpstreamUnavailable: [nse] NSE refused BBB".to_owned(),
        )];
        assert!(r.accounted_for());
        assert_eq!(
            r.render(),
            "FILL date=2026-08-21 scoped=4 attempted=4 stored=1 already_present=0 no_quote=1 \
             unmatched=1 failed=1\nACCOUNTED OK\nno quote from NSE: CCC\nfailed:\n  BBB: \
             UpstreamUnavailable: [nse] NSE refused BBB"
        );
        r.stored = 0;
        assert!(!r.accounted_for());
        assert!(r.render().contains("ACCOUNTED MISMATCH"));
    }

    #[test]
    fn limit_slices_as_python_does() {
        let v = vec![1, 2, 3, 4];
        assert_eq!(sliced(v.clone(), None), vec![1, 2, 3, 4]);
        assert_eq!(sliced(v.clone(), Some(2)), vec![1, 2]);
        assert_eq!(sliced(v.clone(), Some(9)), vec![1, 2, 3, 4]);
        assert_eq!(sliced(v.clone(), Some(-1)), vec![1, 2, 3]);
        assert_eq!(sliced(v, Some(-9)), Vec::<i32>::new());
    }
}
