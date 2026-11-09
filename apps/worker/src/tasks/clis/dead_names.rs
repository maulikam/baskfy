//! PM0.7: names that stopped trading on NSE, back into the plant from the bhavcopy archive.
//! `backfill` is idempotent; `fetch-file` archives one NSE reference file.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::str::FromStr as _;

use baskfy_providers::factory::{RedisSource, build_nse_provider};
use baskfy_providers::nse::NseProvider;
use baskfy_providers::nse_bhavcopy::{
    BhavRow, SCHEMES, explaining_scheme, healthy_at_stop, implied_factors, issuer,
};
use baskfy_providers::settings::ProviderSettings;
use chrono::{Datelike as _, NaiveDate};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::cli::{Failure, Outcome, Parsed, Spec, code, command, connect, iso, say};
use super::factors::session::{Checkpointed, Session};
use crate::error::WorkerError;

/// Reasons from NSE's list of delisted companies, as `symbol,status,reason`.
const REASONS: &str = baskfy_providers::nse_bhavcopy::DELISTED_REASONS;
/// A name still printing this close to the end is not dead.
const STILL_TRADING_SESSIONS: usize = 5;
/// An existing instrument silent for this long has stopped.
const STOPPED_SESSIONS: usize = 30;
/// Calendar days of prints that say whether a name was still trading when it stopped.
const HEALTHY_WINDOW_DAYS: u64 = 40;

#[derive(Debug, Default)]
struct Report {
    sessions: usize,
    missing_days: usize,
    failed_days: Vec<(NaiveDate, String)>,
    resurrected: usize,
    updated: usize,
    bars: usize,
    still_trading_unregistered: usize,
    with_implied_action: usize,
    existing_stopped: u64,
    by_last_year: BTreeMap<i32, usize>,
    by_status: BTreeMap<String, usize>,
}

struct Known {
    live: HashSet<String>,
    dead: HashMap<String, i64>,
    symbols: HashMap<String, i64>,
    exchange_id: i16,
}

async fn known(conn: &mut PgConnection) -> Result<Known, WorkerError> {
    let rows: Vec<(i64, String, Option<String>, bool, i16)> =
        sqlx::query_as("SELECT id, symbol, isin, is_active, exchange_id FROM instrument")
            .fetch_all(conn)
            .await?;
    let mut out = Known {
        live: HashSet::new(),
        dead: HashMap::new(),
        symbols: HashMap::new(),
        exchange_id: rows.first().map_or(1, |r| r.4),
    };
    for (id, symbol, isin, active, exchange) in rows {
        out.exchange_id = exchange;
        out.symbols.insert(symbol, id);
        if let Some(isin) = isin.filter(|i| i.starts_with("INE")) {
            if active {
                out.live.insert(issuer(&isin).to_owned());
            } else {
                out.dead.insert(issuer(&isin).to_owned(), id);
            }
        }
    }
    Ok(out)
}

fn reasons() -> HashMap<String, (String, String)> {
    REASONS
        .lines()
        .skip(1)
        .filter_map(|line| {
            let mut f = line.splitn(3, ',');
            let (symbol, status, reason) = (f.next()?, f.next()?, f.next().unwrap_or(""));
            Some((symbol.to_owned(), (status.to_owned(), reason.to_owned())))
        })
        .collect()
}

/// Why a name stopped: NSE's delisted list by symbol, schemes by issuer.
struct Evidence {
    why: HashMap<String, (String, String)>,
    schemes: HashMap<String, Vec<(NaiveDate, String)>>,
}

fn schemes() -> HashMap<String, Vec<(NaiveDate, String)>> {
    let mut out: HashMap<String, Vec<(NaiveDate, String)>> = HashMap::new();
    for line in SCHEMES.lines().skip(1) {
        let mut f = line.splitn(3, ',');
        if let (Some(key), Some(Ok(ex)), Some(subject)) =
            (f.next(), f.next().map(NaiveDate::from_str), f.next())
        {
            out.entry(key.to_owned())
                .or_default()
                .push((ex, subject.to_owned()));
        }
    }
    out
}

fn series_rank(series: &str) -> u8 {
    match series {
        "EQ" => 0,
        "BE" => 1,
        _ => 2,
    }
}

#[derive(Default)]
struct BarColumns {
    open: Vec<Decimal>,
    high: Vec<Decimal>,
    low: Vec<Decimal>,
    close: Vec<Decimal>,
    volume: Vec<i64>,
    close_raw: Vec<Decimal>,
    volume_raw: Vec<i64>,
    adj: Vec<Decimal>,
}

/// One name's prints in date order, a day's best series only.
type History = BTreeMap<NaiveDate, BhavRow>;

fn collect(
    day: NaiveDate,
    rows: Vec<BhavRow>,
    skip: &HashSet<String>,
    into: &mut HashMap<String, History>,
) {
    for row in rows {
        let key = issuer(&row.isin).to_owned();
        if skip.contains(&key) {
            continue;
        }
        let history = into.entry(key).or_default();
        let better = history
            .get(&day)
            .is_none_or(|have| series_rank(&row.series) < series_rank(&have.series));
        if better {
            history.insert(day, row);
        }
    }
}

fn free_symbol(symbol: &str, key: &str, id: Option<i64>, used: &HashMap<String, i64>) -> String {
    match used.get(symbol) {
        Some(owner) if Some(*owner) != id => format!("{symbol}~{key}"),
        _ => symbol.to_owned(),
    }
}

fn after(sessions: &[NaiveDate], day: NaiveDate) -> NaiveDate {
    sessions
        .iter()
        .find(|s| **s > day)
        .copied()
        .unwrap_or_else(|| day.succ_opt().unwrap_or(day))
}

async fn store_name(
    conn: &mut PgConnection,
    key: &str,
    history: &History,
    sessions: &[NaiveDate],
    state: &mut Known,
    evidence: &Evidence,
    report: &mut Report,
) -> Result<(), WorkerError> {
    let (Some((first, _)), Some((last, tail))) =
        (history.first_key_value(), history.last_key_value())
    else {
        return Ok(());
    };
    let existing = state.dead.get(key).copied();
    let symbol = free_symbol(&tail.symbol, key, existing, &state.symbols);
    let delisted_on = after(sessions, *last);
    let id: i64 = if let Some(id) = existing {
        sqlx::query(
            "UPDATE instrument SET symbol = $2, series = $3, isin = $4, listed_on = $5, delisted_on = $6, \
             is_active = false, kite_token = NULL WHERE id = $1",
        )
        .bind(id)
        .bind(&symbol)
        .bind(&tail.series)
        .bind(&tail.isin)
        .bind(first)
        .bind(delisted_on)
        .execute(&mut *conn)
        .await?;
        report.updated += 1;
        id
    } else {
        let id: i64 = sqlx::query_scalar(
            "INSERT INTO instrument (exchange_id, symbol, name, isin, instrument_type, series, listed_on, \
             delisted_on, is_active, created_at, updated_at) \
             VALUES ($1, $2, $2, $3, 'EQ', $4, $5, $6, false, now(), now()) RETURNING id",
        )
        .bind(state.exchange_id)
        .bind(&symbol)
        .bind(&tail.isin)
        .bind(&tail.series)
        .bind(first)
        .bind(delisted_on)
        .fetch_one(&mut *conn)
        .await?;
        state.symbols.insert(symbol.clone(), id);
        state.dead.insert(key.to_owned(), id);
        report.resurrected += 1;
        *report.by_last_year.entry(last.year()).or_default() += 1;
        id
    };
    let days: Vec<NaiveDate> = history.keys().copied().collect();
    let closes: Vec<Decimal> = history.values().map(|r| r.close).collect();
    let prevs: Vec<Option<Decimal>> = history.values().map(|r| r.prev_close).collect();
    let factors = implied_factors(&closes, &prevs);
    if factors.first().is_some_and(|f| *f != Decimal::ONE) {
        report.with_implied_action += 1;
    }
    let scale = |v: Decimal, f: Decimal| (v * f).round_dp(4);
    let mut columns = BarColumns::default();
    for (row, factor) in history.values().zip(&factors) {
        columns.open.push(scale(row.open, *factor));
        columns.high.push(scale(row.high, *factor));
        columns.low.push(scale(row.low, *factor));
        columns.close.push(scale(row.close, *factor));
        let adjusted = if factor.is_zero() {
            Decimal::from(row.volume)
        } else {
            Decimal::from(row.volume) / factor
        };
        columns
            .volume
            .push(i64::try_from(adjusted.round()).unwrap_or(row.volume));
        columns.close_raw.push(row.close.round_dp(4));
        columns.volume_raw.push(row.volume);
        columns.adj.push(factor.round_dp(10));
    }
    sqlx::query(
        "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, volume, close_raw, volume_raw, adj_factor, source) \
         SELECT $1, t.d, t.o, t.h, t.l, t.c, t.v, t.cr, t.vr, t.f, 'nse' FROM unnest($2::date[], $3::numeric[], $4::numeric[], $5::numeric[], \
         $6::numeric[], $7::bigint[], $8::numeric[], $9::bigint[], $10::numeric[]) AS t(d, o, h, l, c, v, cr, vr, f) \
         ON CONFLICT (instrument_id, date) DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, \
         close = EXCLUDED.close, volume = EXCLUDED.volume, close_raw = EXCLUDED.close_raw, volume_raw = EXCLUDED.volume_raw, \
         adj_factor = EXCLUDED.adj_factor, source = EXCLUDED.source",
    )
    .bind(id)
    .bind(&days)
    .bind(&columns.open)
    .bind(&columns.high)
    .bind(&columns.low)
    .bind(&columns.close)
    .bind(&columns.volume)
    .bind(&columns.close_raw)
    .bind(&columns.volume_raw)
    .bind(&columns.adj)
    .execute(&mut *conn)
    .await?;
    report.bars += days.len();
    let recent: Vec<(Decimal, i64)> = last
        .checked_sub_days(chrono::Days::new(HEALTHY_WINDOW_DAYS))
        .map_or_else(Vec::new, |start| {
            history
                .range(start..=*last)
                .map(|(_, r)| (r.close, r.volume))
                .collect()
        });
    let healthy = healthy_at_stop(tail.close, &recent);
    let scheme = evidence
        .schemes
        .get(key)
        .and_then(|e| explaining_scheme(*last, e));
    let (status, reason, source) = match (evidence.why.get(&tail.symbol), scheme) {
        (Some((status, reason)), _) => {
            (status.as_str(), Some(reason.as_str()), "NSE_DELISTED_LIST")
        }
        (None, Some(subject)) => ("merged", Some(subject), "NSE_CORPORATE_ACTION"),
        (None, None) if healthy => (
            "merged",
            Some("priced and traded at its stop"),
            "HEALTHY_AT_STOP",
        ),
        (None, None) => ("suspended", None, "BHAVCOPY_GAP"),
    };
    *report.by_status.entry(status.to_owned()).or_default() += 1;
    sqlx::query(
        "INSERT INTO instrument_terminal (instrument_id, status, reason, last_bar, source) VALUES ($1, $2, $3, $4, $5) \
         ON CONFLICT (instrument_id) DO UPDATE SET status = EXCLUDED.status, reason = EXCLUDED.reason, \
         last_bar = EXCLUDED.last_bar, source = EXCLUDED.source",
    )
    .bind(id)
    .bind(status)
    .bind(reason)
    .bind(last)
    .bind(source)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// Instruments already in the register whose bars have stopped.
async fn mark_existing(
    conn: &mut PgConnection,
    sessions: &[NaiveDate],
) -> Result<u64, WorkerError> {
    let Some(cutoff) = sessions
        .len()
        .checked_sub(STOPPED_SESSIONS)
        .and_then(|i| sessions.get(i))
    else {
        return Ok(0);
    };
    let done = sqlx::query(
        "WITH stopped AS (SELECT i.id, max(o.date) AS last_bar FROM instrument i JOIN ohlcv_daily o ON o.instrument_id = i.id \
         WHERE i.is_active AND i.isin LIKE 'INE%' AND i.series IN ('EQ', 'BE', 'BZ') GROUP BY i.id HAVING max(o.date) < $1), \
         u AS (UPDATE instrument i SET is_active = false, delisted_on = (SELECT min(t.date) FROM trading_day t WHERE t.is_trading_day AND t.date > s.last_bar) \
         FROM stopped s WHERE i.id = s.id RETURNING i.id) \
         INSERT INTO instrument_terminal (instrument_id, status, reason, last_bar, source) \
         SELECT s.id, 'suspended', NULL, s.last_bar, 'BARS_STOPPED' FROM stopped s JOIN u ON u.id = s.id \
         ON CONFLICT (instrument_id) DO UPDATE SET last_bar = EXCLUDED.last_bar",
    )
    .bind(cutoff)
    .execute(conn)
    .await?;
    Ok(done.rows_affected())
}

async fn operation(
    db: &baskfy_db::WriteDb,
    provider: &NseProvider,
    from: NaiveDate,
    to: NaiveDate,
) -> Result<Report, WorkerError> {
    let mut session = Checkpointed::open(db).await?;
    let mut state = known(session.conn()).await?;
    let sessions: Vec<NaiveDate> = sqlx::query_scalar(
        "SELECT date FROM trading_day WHERE is_trading_day AND date BETWEEN $1 AND $2 ORDER BY date",
    )
    .bind(from)
    .bind(to)
    .fetch_all(session.conn())
    .await?;
    let mut report = Report {
        sessions: sessions.len(),
        ..Report::default()
    };
    let mut names: HashMap<String, History> = HashMap::new();
    for (index, day) in sessions.iter().enumerate() {
        match provider.bhav_rows(*day).await {
            Ok(Some(rows)) => collect(*day, rows, &state.live, &mut names),
            Ok(None) => report.missing_days += 1,
            Err(e) if e.is_provider_error() => report.failed_days.push((*day, e.to_string())),
            Err(e) => return Err(WorkerError::Provider(e)),
        }
        if (index + 1) % 250 == 0 {
            say(&format!(
                "  {}/{} files, names so far {}",
                index + 1,
                sessions.len(),
                names.len()
            ));
        }
    }
    let evidence = Evidence {
        why: reasons(),
        schemes: schemes(),
    };
    let tail_start = sessions
        .len()
        .checked_sub(STILL_TRADING_SESSIONS)
        .and_then(|i| sessions.get(i))
        .copied()
        .unwrap_or(from);
    let mut keys: Vec<String> = names.keys().cloned().collect();
    keys.sort();
    for key in &keys {
        let Some(history) = names.get(key) else {
            continue;
        };
        if history
            .last_key_value()
            .is_some_and(|(last, _)| *last >= tail_start)
        {
            report.still_trading_unregistered += 1;
            continue;
        }
        store_name(
            session.conn(),
            key,
            history,
            &sessions,
            &mut state,
            &evidence,
            &mut report,
        )
        .await?;
        session.checkpoint().await?;
    }
    report.existing_stopped = mark_existing(session.conn(), &sessions).await?;
    session.finish().await?;
    Ok(report)
}

pub(super) async fn run(args: &[String]) -> Outcome {
    let (what, rest) = command(args, &["backfill", "fetch-file", "fetch-actions"])?;
    let parsed = Parsed::parse(
        rest,
        Spec {
            valued: &["--from", "--to", "--url", "--name"],
            ..Spec::default()
        },
    )?;
    let provider = build_provider().await.map_err(Failure::Raised)?;
    if what == "fetch-actions" {
        for year in 2011..=2026 {
            let bytes = provider
                .corporate_actions_year(year)
                .await
                .map_err(|e| Failure::Raised(WorkerError::Provider(e)))?;
            say(&format!("{year}: {} bytes", bytes.len()));
        }
        return Ok(code(true));
    }
    if what == "fetch-file" {
        let (Some(url), Some(name)) = (parsed.get("--url"), parsed.get("--name")) else {
            return Err(Failure::Usage(
                "fetch-file needs --url and --name".to_owned(),
            ));
        };
        let on = chrono::Utc::now().date_naive();
        let bytes = provider
            .reference_file(name, url, on)
            .await
            .map_err(|e| Failure::Raised(WorkerError::Provider(e)))?;
        say(&format!("archived {} bytes under {name}", bytes.len()));
        return Ok(code(true));
    }
    let from = day(parsed.get("--from").unwrap_or("2011-01-01"))?;
    let to = day(parsed.get("--to").unwrap_or("2026-11-06"))?;
    let db = connect(None)?;
    let outcome = operation(&db, &provider, from, to).await;
    db.close().await;
    let r = outcome.map_err(Failure::Raised)?;
    say(&format!(
        "DEAD {}..{} sessions={} missing_days={} failed_days={} resurrected={} updated={} bars={} implied_action_names={} still_trading_unregistered={} existing_stopped={}",
        iso(from),
        iso(to),
        r.sessions,
        r.missing_days,
        r.failed_days.len(),
        r.resurrected,
        r.updated,
        r.bars,
        r.with_implied_action,
        r.still_trading_unregistered,
        r.existing_stopped
    ));
    say(&format!("by last-bar year: {:?}", r.by_last_year));
    say(&format!("by status: {:?}", r.by_status));
    for (day, reason) in r.failed_days.iter().take(20) {
        say(&format!("  {}: {reason}", iso(*day)));
    }
    Ok(code(r.failed_days.is_empty()))
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
