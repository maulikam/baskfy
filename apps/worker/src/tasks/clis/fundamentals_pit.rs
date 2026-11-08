//! PM1: `backfill`, quarterly results from NSE's filings into `fundamental_pit`. Resumable: a
//! filing whose URL is already stored is not fetched again.

use std::collections::HashSet;
use std::str::FromStr as _;

use baskfy_providers::factory::{RedisSource, build_nse_provider};
use baskfy_providers::nse::NseProvider;
use baskfy_providers::nse_results::{
    ResultsFacts, ResultsFiling, available_on, parse_results_xbrl,
};
use baskfy_providers::settings::ProviderSettings;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::cli::{Failure, Outcome, Parsed, Spec, code, command, connect, iso, say};
use super::factors::session::{Checkpointed, Session};
use crate::error::WorkerError;

#[derive(Debug, Default)]
struct Report {
    symbols: usize,
    filings_listed: usize,
    already_stored: usize,
    stored: usize,
    not_a_quarter: usize,
    failed: Vec<(String, String)>,
}

/// Consolidated where the period has one, otherwise standalone: every revision of that kind.
fn chosen(filings: Vec<ResultsFiling>, since: NaiveDate) -> Vec<ResultsFiling> {
    let consolidated: HashSet<NaiveDate> = filings
        .iter()
        .filter(|f| f.consolidated)
        .map(|f| f.period_end)
        .collect();
    filings
        .into_iter()
        .filter(|f| {
            f.period_end >= since && (f.consolidated || !consolidated.contains(&f.period_end))
        })
        .collect()
}

async fn scope(
    conn: &mut PgConnection,
    since: NaiveDate,
    symbols: Option<&[String]>,
) -> Result<Vec<(i64, String)>, WorkerError> {
    // Delisted names stay in scope: dropping them would be survivorship.
    Ok(sqlx::query_as(
        "SELECT i.id, i.symbol FROM instrument i \
         WHERE i.instrument_type = 'EQ' AND i.series IN ('EQ', 'BE', 'BZ') \
         AND EXISTS (SELECT 1 FROM ohlcv_daily o WHERE o.instrument_id = i.id AND o.date >= $1) \
         AND ($2::text[] IS NULL OR i.symbol = ANY($2)) ORDER BY i.id",
    )
    .bind(since)
    .bind(symbols)
    .fetch_all(conn)
    .await?)
}

async fn stored_urls(conn: &mut PgConnection, id: i64) -> Result<HashSet<String>, WorkerError> {
    let urls: Vec<String> =
        sqlx::query_scalar("SELECT source_url FROM fundamental_pit WHERE instrument_id = $1")
            .bind(id)
            .fetch_all(conn)
            .await?;
    Ok(urls.into_iter().collect())
}

fn rupees(v: Option<Decimal>) -> Option<Decimal> {
    v.map(|d| d.round_dp(2))
}

async fn store(
    conn: &mut PgConnection,
    id: i64,
    filing: &ResultsFiling,
    facts: &ResultsFacts,
    period_start: NaiveDate,
) -> Result<(), WorkerError> {
    sqlx::query(
        "INSERT INTO fundamental_pit (instrument_id, period_end, filed_on, period_type, filed_at, \
         available_on, period_start, is_revision, revenue, other_income, finance_costs, \
         depreciation, ebitda, pbt, pat, eps_diluted, shares_outstanding, total_debt, \
         total_equity, cfo, capex, source, source_url) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23) \
         ON CONFLICT (instrument_id, period_end, filed_on, period_type) DO UPDATE SET \
         filed_at = EXCLUDED.filed_at, available_on = EXCLUDED.available_on, \
         period_start = EXCLUDED.period_start, is_revision = EXCLUDED.is_revision, \
         revenue = EXCLUDED.revenue, other_income = EXCLUDED.other_income, \
         finance_costs = EXCLUDED.finance_costs, depreciation = EXCLUDED.depreciation, \
         ebitda = EXCLUDED.ebitda, pbt = EXCLUDED.pbt, pat = EXCLUDED.pat, \
         eps_diluted = EXCLUDED.eps_diluted, shares_outstanding = EXCLUDED.shares_outstanding, \
         total_debt = EXCLUDED.total_debt, total_equity = EXCLUDED.total_equity, \
         cfo = EXCLUDED.cfo, capex = EXCLUDED.capex, source = EXCLUDED.source, \
         source_url = EXCLUDED.source_url \
         WHERE fundamental_pit.filed_at <= EXCLUDED.filed_at",
    )
    .bind(id)
    .bind(filing.period_end)
    .bind(filing.filed_at.date())
    .bind(if filing.consolidated { "consolidated" } else { "standalone" })
    .bind(filing.filed_at)
    .bind(available_on(filing.filed_at))
    .bind(period_start)
    .bind(filing.is_revision)
    .bind(rupees(facts.revenue))
    .bind(rupees(facts.other_income))
    .bind(rupees(facts.finance_costs))
    .bind(rupees(facts.depreciation))
    .bind(rupees(facts.ebitda()))
    .bind(rupees(facts.pbt))
    .bind(rupees(facts.pat))
    .bind(facts.eps_diluted.map(|d| d.round_dp(4)))
    .bind(facts.shares_outstanding())
    .bind(rupees(facts.total_debt))
    .bind(rupees(facts.total_equity))
    .bind(rupees(facts.cfo))
    .bind(rupees(facts.capex))
    .bind(filing.source)
    .bind(&filing.xbrl_url)
    .execute(conn)
    .await?;
    Ok(())
}

async fn one_symbol(
    session: &mut Checkpointed,
    provider: &NseProvider,
    (id, symbol): (i64, &str),
    since: NaiveDate,
    on: NaiveDate,
    report: &mut Report,
) -> Result<(), WorkerError> {
    let listed = provider.results_filings(symbol, on).await?;
    let filings = chosen(listed, since);
    report.filings_listed += filings.len();
    let done = stored_urls(session.conn(), id).await?;
    for filing in &filings {
        if done.contains(&filing.xbrl_url) {
            report.already_stored += 1;
            continue;
        }
        let bytes = provider
            .financial_results_xbrl(symbol, &filing.xbrl_url, filing.filed_at.date())
            .await?;
        match parse_results_xbrl(&bytes)? {
            Some(facts) if facts.period_end == Some(filing.period_end) => {
                let start = facts.period_start.unwrap_or(filing.period_end);
                store(session.conn(), id, filing, &facts, start).await?;
                report.stored += 1;
            }
            _ => report.not_a_quarter += 1,
        }
    }
    session.checkpoint().await
}

pub(super) async fn run(args: &[String]) -> Outcome {
    let (_, rest) = command(args, &["backfill"])?;
    let parsed = Parsed::parse(
        rest,
        Spec {
            valued: &["--since", "--on", "--limit", "--symbols"],
            switches: &["--dry-run"],
            ..Spec::default()
        },
    )?;
    let since = day(parsed.get("--since").unwrap_or("2018-01-01"))?;
    let on = match parsed.get("--on") {
        Some(text) => day(text)?,
        None => chrono::Utc::now().date_naive(),
    };
    let limit = parsed.int("--limit")?;
    let symbols: Option<Vec<String>> = parsed
        .get("--symbols")
        .map(|s| s.split(',').map(|t| t.trim().to_uppercase()).collect());
    let db = connect(None)?;
    let provider = build_provider().await.map_err(Failure::Raised)?;
    let outcome = operation(
        &db,
        &provider,
        since,
        on,
        limit,
        symbols,
        parsed.on("--dry-run"),
    )
    .await;
    db.close().await;
    let report = outcome.map_err(Failure::Raised)?;
    say(&format!(
        "PIT since={} symbols={} listed={} already={} stored={} not_a_quarter={} failed={}",
        iso(since),
        report.symbols,
        report.filings_listed,
        report.already_stored,
        report.stored,
        report.not_a_quarter,
        report.failed.len()
    ));
    for (symbol, reason) in &report.failed {
        say(&format!("  {symbol}: {reason}"));
    }
    Ok(code(report.failed.is_empty()))
}

async fn operation(
    db: &baskfy_db::WriteDb,
    provider: &NseProvider,
    since: NaiveDate,
    on: NaiveDate,
    limit: Option<i64>,
    symbols: Option<Vec<String>>,
    dry_run: bool,
) -> Result<Report, WorkerError> {
    let mut session = Checkpointed::open(db).await?;
    let mut names = scope(session.conn(), since, symbols.as_deref()).await?;
    if let Some(n) = limit.and_then(|n| usize::try_from(n).ok()) {
        names.truncate(n);
    }
    let mut report = Report {
        symbols: names.len(),
        ..Report::default()
    };
    if !dry_run {
        for (index, (id, symbol)) in names.iter().enumerate() {
            match one_symbol(
                &mut session,
                provider,
                (*id, symbol),
                since,
                on,
                &mut report,
            )
            .await
            {
                Ok(()) => {}
                Err(WorkerError::Provider(e)) if e.is_provider_error() => {
                    report.failed.push((symbol.clone(), e.to_string()));
                }
                Err(e) => return Err(e),
            }
            if (index + 1) % 25 == 0 {
                say(&format!(
                    "  {}/{} stored={} failed={}",
                    index + 1,
                    names.len(),
                    report.stored,
                    report.failed.len()
                ));
            }
        }
    }
    session.finish().await?;
    Ok(report)
}

fn day(text: &str) -> Result<NaiveDate, Failure> {
    NaiveDate::from_str(text).map_err(|e| Failure::Usage(format!("bad date {text}: {e}")))
}

async fn build_provider() -> Result<NseProvider, WorkerError> {
    let settings =
        ProviderSettings::from_env().map_err(|e| WorkerError::Settings(e.to_string()))?;
    let source = RedisSource::from_settings(&settings);
    Ok(build_nse_provider(&source, &settings, None, None).await?)
}
