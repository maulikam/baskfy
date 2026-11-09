//! PM1 and PM2: `backfill` (results into `fundamental_pit`), `shareholding` (`shareholding_pit`),
//! `all` (both, through one provider and so one limiter). Resumable: a filing whose URL is already
//! stored is not fetched again.

use std::collections::HashSet;
use std::str::FromStr as _;

use baskfy_providers::ProviderError;
use baskfy_providers::factory::{RedisSource, build_nse_provider};
use baskfy_providers::nse::NseProvider;
use baskfy_providers::nse_results::{
    ResultsFacts, ResultsFiling, available_on, parse_results_xbrl,
};
use baskfy_providers::nse_shareholding::{
    SOURCE_SHP, ShareholdingFacts, ShareholdingFiling, parse_shareholding_xbrl,
};
use baskfy_providers::settings::ProviderSettings;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::cli::{Failure, Outcome, Parsed, Spec, code, command, connect, iso, say};
use super::factors::session::{Checkpointed, Session};
use crate::error::WorkerError;

#[derive(Debug, Clone, Copy)]
struct Jobs {
    results: bool,
    shareholding: bool,
}

#[derive(Debug, Default)]
struct Report {
    shp_listed: usize,
    shp_already: usize,
    shp_stored: usize,
    shp_unread: usize,
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
    // PM1.6: names that ever met the investable floors, then the other dead names, then the rest.
    Ok(sqlx::query_as(
        "WITH bars AS (SELECT instrument_id, close_raw, avg(close_raw * volume_raw) OVER w AS t20, count(*) OVER w AS n \
         FROM ohlcv_daily WINDOW w AS (PARTITION BY instrument_id ORDER BY date ROWS 19 PRECEDING)), \
         investable AS (SELECT DISTINCT instrument_id FROM bars WHERE n = 20 AND close_raw >= 50 AND t20 >= 5e7) \
         SELECT i.id, i.symbol FROM instrument i \
         LEFT JOIN investable v ON v.instrument_id = i.id \
         LEFT JOIN instrument_terminal t ON t.instrument_id = i.id \
         WHERE i.instrument_type = 'EQ' AND i.series IN ('EQ', 'BE', 'BZ') \
         AND EXISTS (SELECT 1 FROM ohlcv_daily o WHERE o.instrument_id = i.id AND o.date >= $1) \
         AND ($2::text[] IS NULL OR i.symbol = ANY($2)) \
         ORDER BY CASE WHEN v.instrument_id IS NOT NULL THEN 0 WHEN t.instrument_id IS NOT NULL THEN 1 ELSE 2 END, i.id",
    )
    .bind(since)
    .bind(symbols)
    .fetch_all(conn)
    .await?)
}

async fn stored_urls(
    conn: &mut PgConnection,
    sql: &'static str,
    id: i64,
) -> Result<HashSet<String>, WorkerError> {
    let urls: Vec<String> = sqlx::query_scalar(sql).bind(id).fetch_all(conn).await?;
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

/// A listed document NSE no longer serves costs that filing, not the symbol's remaining ones.
fn present(fetched: Result<Vec<u8>, ProviderError>) -> Result<Option<Vec<u8>>, WorkerError> {
    match fetched {
        Ok(bytes) => Ok(Some(bytes)),
        Err(e) if e.to_string().contains("returned 404") => Ok(None),
        Err(e) => Err(WorkerError::Provider(e)),
    }
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
    let done = stored_urls(
        session.conn(),
        "SELECT source_url FROM fundamental_pit WHERE instrument_id = $1",
        id,
    )
    .await?;
    for filing in &filings {
        if done.contains(&filing.xbrl_url) {
            report.already_stored += 1;
            continue;
        }
        let fetched = provider
            .financial_results_xbrl(symbol, &filing.xbrl_url, filing.filed_at.date())
            .await;
        let Some(bytes) = present(fetched)? else {
            report.not_a_quarter += 1;
            continue;
        };
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

async fn store_shareholding(
    conn: &mut PgConnection,
    id: i64,
    filing: &ShareholdingFiling,
    facts: &ShareholdingFacts,
) -> Result<(), WorkerError> {
    let pct = |v: Decimal| v.round_dp(4);
    sqlx::query(
        "INSERT INTO shareholding_pit (instrument_id, period_end, filed_on, filed_at, \
         available_on, is_revision, promoter_pct, promoter_pledged_pct, fii_pct, dii_pct, mf_pct, \
         public_pct, n_shareholders, source, source_url) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) \
         ON CONFLICT (instrument_id, period_end, filed_on) DO UPDATE SET \
         filed_at = EXCLUDED.filed_at, available_on = EXCLUDED.available_on, \
         is_revision = EXCLUDED.is_revision, promoter_pct = EXCLUDED.promoter_pct, \
         promoter_pledged_pct = EXCLUDED.promoter_pledged_pct, fii_pct = EXCLUDED.fii_pct, \
         dii_pct = EXCLUDED.dii_pct, mf_pct = EXCLUDED.mf_pct, public_pct = EXCLUDED.public_pct, \
         n_shareholders = EXCLUDED.n_shareholders, source = EXCLUDED.source, \
         source_url = EXCLUDED.source_url \
         WHERE shareholding_pit.filed_at <= EXCLUDED.filed_at",
    )
    .bind(id)
    .bind(filing.period_end)
    .bind(filing.filed_at.date())
    .bind(filing.filed_at)
    .bind(available_on(filing.filed_at))
    .bind(filing.is_revision)
    .bind(filing.promoter_pct.map(pct))
    .bind(pct(facts.pledged_pct))
    .bind(pct(facts.fii_pct))
    .bind(pct(facts.dii_pct))
    .bind(pct(facts.mf_pct))
    .bind(filing.public_pct.map(pct))
    .bind(facts.n_shareholders)
    .bind(SOURCE_SHP)
    .bind(&filing.xbrl_url)
    .execute(conn)
    .await?;
    Ok(())
}

async fn shareholding_symbol(
    session: &mut Checkpointed,
    provider: &NseProvider,
    (id, symbol): (i64, &str),
    since: NaiveDate,
    on: NaiveDate,
    report: &mut Report,
) -> Result<(), WorkerError> {
    let filings: Vec<ShareholdingFiling> = provider
        .shareholding_filings(symbol, on)
        .await?
        .into_iter()
        .filter(|f| f.period_end >= since)
        .collect();
    report.shp_listed += filings.len();
    let done = stored_urls(
        session.conn(),
        "SELECT source_url FROM shareholding_pit WHERE instrument_id = $1",
        id,
    )
    .await?;
    for filing in &filings {
        if done.contains(&filing.xbrl_url) {
            report.shp_already += 1;
            continue;
        }
        let fetched = provider
            .shareholding_xbrl(symbol, &filing.xbrl_url, filing.filed_at.date())
            .await;
        let Some(bytes) = present(fetched)? else {
            report.shp_unread += 1;
            continue;
        };
        match parse_shareholding_xbrl(&bytes)? {
            Some(facts) => {
                store_shareholding(session.conn(), id, filing, &facts).await?;
                report.shp_stored += 1;
            }
            None => report.shp_unread += 1,
        }
    }
    session.checkpoint().await
}

pub(super) async fn run(args: &[String]) -> Outcome {
    let (what, rest) = command(args, &["backfill", "shareholding", "all"])?;
    let jobs = Jobs {
        results: what != "shareholding",
        shareholding: what != "backfill",
    };
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
    let plan = Plan {
        jobs,
        since,
        on,
        limit,
        symbols,
        dry_run: parsed.on("--dry-run"),
    };
    let outcome = operation(&db, &provider, plan).await;
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
    if jobs.shareholding {
        say(&format!(
            "SHP listed={} already={} stored={} unread={}",
            report.shp_listed, report.shp_already, report.shp_stored, report.shp_unread
        ));
    }
    for (symbol, reason) in &report.failed {
        say(&format!("  {symbol}: {reason}"));
    }
    Ok(code(report.failed.is_empty()))
}

struct Plan {
    jobs: Jobs,
    since: NaiveDate,
    on: NaiveDate,
    limit: Option<i64>,
    symbols: Option<Vec<String>>,
    dry_run: bool,
}

fn note(
    outcome: Result<(), WorkerError>,
    symbol: &str,
    report: &mut Report,
) -> Result<(), WorkerError> {
    match outcome {
        Err(WorkerError::Provider(e)) if e.is_provider_error() => {
            say(&format!("FAILED {symbol}: {e}"));
            report.failed.push((symbol.to_owned(), e.to_string()));
            Ok(())
        }
        other => other,
    }
}

async fn operation(
    db: &baskfy_db::WriteDb,
    provider: &NseProvider,
    plan: Plan,
) -> Result<Report, WorkerError> {
    let Plan {
        jobs,
        since,
        on,
        limit,
        symbols,
        dry_run,
    } = plan;
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
            let name = (*id, symbol.as_str());
            if jobs.results {
                let done = one_symbol(&mut session, provider, name, since, on, &mut report).await;
                note(done, symbol, &mut report)?;
            }
            if jobs.shareholding {
                let done =
                    shareholding_symbol(&mut session, provider, name, since, on, &mut report).await;
                note(done, symbol, &mut report)?;
            }
            if (index + 1) % 25 == 0 {
                say(&format!(
                    "  {}/{} stored={} shp={} failed={}",
                    index + 1,
                    names.len(),
                    report.stored,
                    report.shp_stored,
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
