//! Ports `baskfy_worker.fno.spreads`: the 15:00 spread sample into `fo_spread_sample` and the
//! forward results calendar (`docs/fno/06` FO3, QUESTIONS Q8), and the statistic FO9's re-test
//! reads (`half_spread_stats`).
//!
//! One Kite `quote()` over ATM ± 3 listed strikes of the near monthly of the top 30 stock
//! underlyings by futures turnover plus NIFTY and BANKNIFTY — built **only** when a Kite session
//! exists. Append-only (`ON CONFLICT DO NOTHING`), rounded at write time. **Law 2**: nothing here
//! has an order verb; moves no money.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::fno::config::py;
use baskfy_db::OrderedJson;
use baskfy_providers::errors::ProviderError;
use baskfy_providers::kite::QUOTE_BATCH_SIZE;
use baskfy_providers::nse::NseProvider;
use baskfy_providers::records::{EarningsDateRecord, OptionQuoteRecord};
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, Utc};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::{fno_err, iso, iso_dt, quantize_half_up};
use super::ingest::{FUTURE_TYPE, describe};
use crate::error::WorkerError;
use crate::pyjson::{object, opt_s, s, strings};
use crate::steps::log_warning;

/// `TOP_STOCKS`.
pub(crate) const TOP_STOCKS: usize = 30;
/// `INDEX_UNDERLYINGS`.
pub(crate) const INDEX_UNDERLYINGS: [&str; 2] = ["NIFTY", "BANKNIFTY"];
/// `STRIKES_EACH_SIDE`.
pub(crate) const STRIKES_EACH_SIDE: usize = 3;
/// `TURNOVER_SESSIONS`.
pub(crate) const TURNOVER_SESSIONS: i64 = 20;
/// `MIN_SPREAD_SESSIONS`.
pub(crate) const MIN_SPREAD_SESSIONS: usize = 20;

fn cent(d: Decimal) -> Decimal {
    quantize_half_up(d, 2)
}

/// `statistics.median` over decimals: the middle one, or the two middle ones' mean in
/// Python's default decimal context.
pub(crate) fn decimal_median(values: &[Decimal]) -> Result<Option<Decimal>, WorkerError> {
    let mut data = values.to_vec();
    data.sort();
    let n = data.len();
    if n == 0 {
        return Ok(None);
    }
    if n % 2 == 1 {
        return Ok(Some(data[n / 2]));
    }
    let sum = py::add(data[n / 2 - 1], data[n / 2]).map_err(fno_err)?;
    py::div(sum, Decimal::from(2)).map(Some).map_err(fno_err)
}

/// `rank_by_turnover`: the `top` symbols by median per-session futures turnover; ties by symbol.
pub(crate) fn rank_by_turnover(
    turnover: &BTreeMap<String, Vec<Decimal>>,
    top: usize,
) -> Result<Vec<String>, WorkerError> {
    let mut medians: Vec<(String, Decimal)> = Vec::new();
    for (symbol, values) in turnover {
        if let Some(m) = decimal_median(values)? {
            medians.push((symbol.clone(), m));
        }
    }
    medians.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    Ok(medians.into_iter().take(top).map(|(s, _)| s).collect())
}

/// `monthly_expiries`: the last listed expiry of each calendar month, ascending.
pub(crate) fn monthly_expiries(expiries: impl Iterator<Item = NaiveDate>) -> Vec<NaiveDate> {
    baskfy_core::fno::calendar::monthly_expiries(&expiries.collect::<Vec<_>>())
}

/// `near_monthly`: the first monthly strictly after `today` (FO3.3).
pub(crate) fn near_monthly(
    expiries: impl Iterator<Item = NaiveDate>,
    today: NaiveDate,
) -> Option<NaiveDate> {
    monthly_expiries(expiries).into_iter().find(|e| *e > today)
}

/// `atm_window`: the listed strike nearest `reference` (the lower on a tie) and `each_side`
/// listed strikes either side, by listing order.
pub(crate) fn atm_window(
    strikes: &[Decimal],
    reference: Decimal,
    each_side: usize,
) -> Vec<Decimal> {
    let ladder: Vec<Decimal> = strikes
        .iter()
        .copied()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let Some(centre) = (0..ladder.len()).min_by(|&i, &j| {
        (ladder[i] - reference)
            .abs()
            .cmp(&(ladder[j] - reference).abs())
            .then(ladder[i].cmp(&ladder[j]))
    }) else {
        return Vec::new();
    };
    let lo = centre.saturating_sub(each_side);
    let hi = (centre + each_side + 1).min(ladder.len());
    ladder[lo..hi].to_vec()
}

/// `SampleContract`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SampleContract {
    pub(crate) symbol: String,
    pub(crate) tradingsymbol: String,
    pub(crate) expiry: NaiveDate,
    pub(crate) strike: Decimal,
    pub(crate) option_type: String,
}

impl SampleContract {
    /// `NFO:<tradingsymbol>`.
    pub(crate) fn key(&self) -> String {
        format!("NFO:{}", self.tradingsymbol)
    }
}

/// `pick_sample`: one underlying's near monthly, ATM ± `each_side` strikes, CE and PE.
pub(crate) fn pick_sample(
    contracts: &[SampleContract],
    reference: Decimal,
    today: NaiveDate,
    each_side: usize,
) -> Vec<SampleContract> {
    let Some(expiry) = near_monthly(contracts.iter().map(|c| c.expiry), today) else {
        return Vec::new();
    };
    let series: Vec<&SampleContract> = contracts.iter().filter(|c| c.expiry == expiry).collect();
    let strikes: Vec<Decimal> = series.iter().map(|c| c.strike).collect();
    let wanted: BTreeSet<Decimal> = atm_window(&strikes, reference, each_side)
        .into_iter()
        .collect();
    let mut out: Vec<SampleContract> = series
        .into_iter()
        .filter(|c| wanted.contains(&c.strike))
        .cloned()
        .collect();
    out.sort_by(|a, b| {
        a.strike
            .cmp(&b.strike)
            .then_with(|| a.option_type.cmp(&b.option_type))
    });
    out
}

/// `reference_level`: the settle of `expiry`'s future, else of the listed future nearest it.
pub(crate) fn reference_level(
    futures: &[(NaiveDate, Option<Decimal>)],
    expiry: NaiveDate,
) -> Option<Decimal> {
    if let Some((_, v)) = futures.iter().rev().find(|(e, _)| *e == expiry) {
        return *v;
    }
    let nearest = futures
        .iter()
        .map(|(e, _)| *e)
        .min_by_key(|e| ((*e - expiry).num_days().abs(), *e))?;
    futures
        .iter()
        .rev()
        .find(|(e, _)| *e == nearest)
        .and_then(|(_, v)| *v)
}

/// `quote_calls`: how many `quote()` calls `keys` keys cost.
pub(crate) const fn quote_calls(keys: usize) -> usize {
    if keys == 0 {
        0
    } else {
        keys.div_ceil(QUOTE_BATCH_SIZE)
    }
}

/// One row to store.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct SampleRow {
    pub(crate) symbol: String,
    pub(crate) expiry: NaiveDate,
    pub(crate) strike: Decimal,
    pub(crate) option_type: String,
    pub(crate) bid: Option<Decimal>,
    pub(crate) ask: Option<Decimal>,
    pub(crate) mid: Option<Decimal>,
    pub(crate) oi: Option<i64>,
}

/// `sample_rows`: one per contract Kite answered (FO3.1), rounded at write time.
pub(crate) fn sample_rows(
    contracts: &[SampleContract],
    quotes: &BTreeMap<String, OptionQuoteRecord>,
) -> Vec<SampleRow> {
    let mut rows = Vec::new();
    for c in contracts {
        let Some(record) = quotes.get(&c.key()) else {
            continue;
        };
        let bid = record.bids.first().map(|l| cent(l.price));
        let ask = record.asks.first().map(|l| cent(l.price));
        let mid = match (bid, ask) {
            (Some(b), Some(a)) if a >= b => Some(cent((b + a) / Decimal::from(2))),
            _ => None,
        };
        rows.push(SampleRow {
            symbol: c.symbol.clone(),
            expiry: c.expiry,
            strike: cent(c.strike),
            option_type: c.option_type.clone(),
            bid,
            ask,
            mid,
            oi: record.oi,
        });
    }
    rows
}

/// One stored sample, as the statistic needs it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SpreadObservation {
    pub(crate) trade_date: NaiveDate,
    pub(crate) symbol: String,
    pub(crate) expiry: NaiveDate,
    pub(crate) strike: Decimal,
    pub(crate) option_type: String,
    pub(crate) bid: Option<Decimal>,
    pub(crate) ask: Option<Decimal>,
    pub(crate) taken_at: DateTime<Utc>,
}

/// `SpreadStat`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SpreadStat {
    pub(crate) median_half_spread_pct_of_mid: Decimal,
    pub(crate) sessions: usize,
}

/// `half_spread_ratio`: `(ask - bid) / (ask + bid)`; `None` for a one-sided, empty or crossed
/// book.
pub(crate) fn half_spread_ratio(
    bid: Option<Decimal>,
    ask: Option<Decimal>,
) -> Result<Option<Decimal>, WorkerError> {
    let (Some(bid), Some(ask)) = (bid, ask) else {
        return Ok(None);
    };
    if bid <= Decimal::ZERO || ask < bid {
        return Ok(None);
    }
    py::div(ask - bid, ask + bid).map(Some).map_err(fno_err)
}

/// `half_spread_stats`: per underlying, the median half-spread ÷ mid over its last `sessions`
/// sessions with a two-sided quote. A contract sampled twice in a session counts once, latest.
pub(crate) fn half_spread_stats(
    observations: Vec<SpreadObservation>,
    sessions: usize,
) -> Result<BTreeMap<String, SpreadStat>, WorkerError> {
    if sessions == 0 {
        return Err(WorkerError::python(
            "ValueError",
            "sessions must be positive",
        ));
    }
    type Key = (NaiveDate, String, NaiveDate, Decimal, String);
    let mut latest: Vec<(Key, SpreadObservation)> = Vec::new();
    let mut index: BTreeMap<Key, usize> = BTreeMap::new();
    for o in observations {
        let key = (
            o.trade_date,
            o.symbol.clone(),
            o.expiry,
            o.strike.normalize(),
            o.option_type.clone(),
        );
        match index.get(&key) {
            Some(&i) => {
                if o.taken_at > latest[i].1.taken_at {
                    latest[i].1 = o;
                }
            }
            None => {
                index.insert(key.clone(), latest.len());
                latest.push((key, o));
            }
        }
    }
    let mut by_symbol: BTreeMap<String, BTreeMap<NaiveDate, Vec<Decimal>>> = BTreeMap::new();
    for (_, o) in &latest {
        if let Some(ratio) = half_spread_ratio(o.bid, o.ask)? {
            by_symbol
                .entry(o.symbol.clone())
                .or_default()
                .entry(o.trade_date)
                .or_default()
                .push(ratio);
        }
    }
    let mut out = BTreeMap::new();
    for (symbol, days) in by_symbol {
        let window: Vec<NaiveDate> = days.keys().copied().collect();
        let window = &window[window.len().saturating_sub(sessions)..];
        let ratios: Vec<Decimal> = window
            .iter()
            .flat_map(|d| days.get(d).cloned().unwrap_or_default())
            .collect();
        if let Some(median) = decimal_median(&ratios)? {
            out.insert(
                symbol,
                SpreadStat {
                    median_half_spread_pct_of_mid: median,
                    sessions: window.len(),
                },
            );
        }
    }
    Ok(out)
}

/// `upcoming_results`: each symbol's result meetings on or after `on_or_after`, soonest first,
/// one per date.
pub(crate) fn upcoming_results(
    records: &[EarningsDateRecord],
    on_or_after: NaiveDate,
) -> BTreeMap<String, Vec<NaiveDate>> {
    let mut out: BTreeMap<String, BTreeSet<NaiveDate>> = BTreeMap::new();
    for r in records {
        if r.event_date >= on_or_after {
            out.entry(r.symbol.to_uppercase())
                .or_default()
                .insert(r.event_date);
        }
    }
    out.into_iter()
        .map(|(s, d)| (s, d.into_iter().collect()))
        .collect()
}

/// `in_session`: 09:15 ≤ now ≤ 15:30 IST.
pub(crate) fn in_session(now: DateTime<FixedOffset>) -> bool {
    let clock = now.with_timezone(&baskfy_providers::tokens::ist()).time();
    let open = NaiveTime::from_hms_opt(9, 15, 0).unwrap_or(NaiveTime::MIN);
    let close = NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN);
    open <= clock && clock <= close
}

/// `SpreadReport`.
#[derive(Debug, Clone)]
pub(crate) struct SpreadReport {
    pub(crate) trade_date: NaiveDate,
    pub(crate) taken_at: DateTime<FixedOffset>,
    pub(crate) reference_session: Option<NaiveDate>,
    pub(crate) underlyings: Vec<String>,
    pub(crate) no_sample: Vec<String>,
    pub(crate) keys: usize,
    pub(crate) calls: usize,
    pub(crate) rows: usize,
    pub(crate) inserted: u64,
    pub(crate) unanswered: usize,
    pub(crate) one_sided: usize,
    pub(crate) skipped: Option<String>,
    pub(crate) results: Vec<(String, Vec<String>)>,
    pub(crate) results_errors: Vec<(String, String)>,
}

fn n(v: usize) -> OrderedJson {
    OrderedJson::Int(i64::try_from(v).unwrap_or(i64::MAX))
}

impl SpreadReport {
    /// `as_dict()`.
    pub(crate) fn as_dict(&self) -> OrderedJson {
        object([
            ("trade_date", s(iso(self.trade_date))),
            ("taken_at", s(iso_dt(self.taken_at))),
            (
                "reference_session",
                self.reference_session
                    .map_or(OrderedJson::Null, |d| s(iso(d))),
            ),
            ("underlyings", strings(self.underlyings.iter().cloned())),
            ("no_sample", strings(self.no_sample.iter().cloned())),
            ("keys", n(self.keys)),
            ("calls", n(self.calls)),
            ("rows", n(self.rows)),
            (
                "inserted",
                OrderedJson::Int(i64::try_from(self.inserted).unwrap_or(i64::MAX)),
            ),
            ("unanswered", n(self.unanswered)),
            ("one_sided", n(self.one_sided)),
            ("skipped", opt_s(self.skipped.clone())),
            (
                "results",
                object(
                    self.results
                        .iter()
                        .map(|(k, v)| (k.clone(), strings(v.iter().cloned()))),
                ),
            ),
            (
                "results_errors",
                object(
                    self.results_errors
                        .iter()
                        .map(|(k, v)| (k.clone(), s(v.clone()))),
                ),
            ),
        ])
    }
}

/// `latest_sessions`: the last `count` sessions `fo_contract_daily` holds before `before`,
/// newest first.
pub(crate) async fn latest_sessions(
    conn: &mut PgConnection,
    before: NaiveDate,
    count: i64,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT trade_date FROM fo_contract_daily WHERE trade_date < $1 GROUP BY trade_date \
         ORDER BY trade_date DESC LIMIT $2",
    )
    .bind(before)
    .bind(count)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|(d,)| d).collect())
}

/// `top_stock_underlyings`.
pub(crate) async fn top_stock_underlyings(
    conn: &mut PgConnection,
    sessions: &[NaiveDate],
    top: usize,
) -> Result<Vec<String>, WorkerError> {
    if sessions.is_empty() {
        return Ok(Vec::new());
    }
    let rows: Vec<(String, NaiveDate, Option<Decimal>)> = sqlx::query_as(
        "SELECT symbol, trade_date, sum(turnover) FROM fo_contract_daily \
         WHERE instrument = 'FUTSTK' AND trade_date = ANY($1) AND turnover IS NOT NULL \
         GROUP BY symbol, trade_date",
    )
    .bind(sessions)
    .fetch_all(&mut *conn)
    .await?;
    let mut turnover: BTreeMap<String, Vec<Decimal>> = BTreeMap::new();
    for (symbol, _, total) in rows {
        if let Some(t) = total {
            turnover.entry(symbol).or_default().push(t);
        }
    }
    rank_by_turnover(&turnover, top)
}

/// `future_settles`: each symbol's futures settles on `day`, by expiry (in row order).
pub(crate) async fn future_settles(
    conn: &mut PgConnection,
    day: NaiveDate,
    symbols: &[String],
) -> Result<BTreeMap<String, Vec<(NaiveDate, Option<Decimal>)>>, WorkerError> {
    let rows: Vec<(String, NaiveDate, Option<Decimal>)> = sqlx::query_as(
        "SELECT symbol, expiry, settle FROM fo_contract_daily WHERE trade_date = $1 \
         AND option_type = $2 AND symbol = ANY($3)",
    )
    .bind(day)
    .bind(FUTURE_TYPE)
    .bind(symbols)
    .fetch_all(&mut *conn)
    .await?;
    let mut out: BTreeMap<String, Vec<(NaiveDate, Option<Decimal>)>> = BTreeMap::new();
    for (symbol, expiry, settle) in rows {
        out.entry(symbol).or_default().push((expiry, settle));
    }
    Ok(out)
}

/// `listed_options`: every still-listed option in `op_contract` for `symbols`, expiring on or
/// after today.
pub(crate) async fn listed_options(
    conn: &mut PgConnection,
    symbols: &[String],
    today: NaiveDate,
) -> Result<BTreeMap<String, Vec<SampleContract>>, WorkerError> {
    let rows: Vec<(String, String, NaiveDate, Decimal, String)> = sqlx::query_as(
        "SELECT underlying, tradingsymbol, expiry, strike, option_type FROM op_contract \
         WHERE underlying = ANY($1) AND expired IS false AND expiry >= $2",
    )
    .bind(symbols)
    .bind(today)
    .fetch_all(&mut *conn)
    .await?;
    let mut out: BTreeMap<String, Vec<SampleContract>> = BTreeMap::new();
    for (underlying, tradingsymbol, expiry, strike, option_type) in rows {
        out.entry(underlying.clone())
            .or_default()
            .push(SampleContract {
                symbol: underlying,
                tradingsymbol,
                expiry,
                strike,
                option_type,
            });
    }
    Ok(out)
}

/// `plan_sample`: the stock names (for the calendar) and the contracts to quote.
pub(crate) async fn plan_sample(
    conn: &mut PgConnection,
    today: NaiveDate,
    report: &mut SpreadReport,
) -> Result<(Vec<String>, Vec<SampleContract>), WorkerError> {
    let sessions = latest_sessions(conn, today, TURNOVER_SESSIONS).await?;
    let Some(reference) = sessions.first().copied() else {
        report.skipped = Some(
            "fo_contract_daily holds no session before today (FO2 has not ingested)".to_owned(),
        );
        return Ok((Vec::new(), Vec::new()));
    };
    report.reference_session = Some(reference);
    let stocks = top_stock_underlyings(conn, &sessions, TOP_STOCKS).await?;
    let mut names = stocks.clone();
    for i in INDEX_UNDERLYINGS {
        if !stocks.iter().any(|s| s == i) {
            names.push(i.to_owned());
        }
    }
    report.underlyings.clone_from(&names);
    let settles = future_settles(conn, reference, &names).await?;
    let listed = listed_options(conn, &names, today).await?;
    let mut picked = Vec::new();
    for name in &names {
        let contracts = listed.get(name).cloned().unwrap_or_default();
        let expiry = near_monthly(contracts.iter().map(|c| c.expiry), today);
        let level = expiry
            .and_then(|e| reference_level(settles.get(name).map_or(&[][..], Vec::as_slice), e));
        let chosen = level.map_or_else(Vec::new, |l| {
            pick_sample(&contracts, l, today, STRIKES_EACH_SIDE)
        });
        if chosen.is_empty() {
            report.no_sample.push(name.clone());
        }
        picked.extend(chosen);
    }
    Ok((stocks, picked))
}

/// `insert_samples`: append-only; a moment already stored is left exactly as it was.
pub(crate) async fn insert_samples(
    conn: &mut PgConnection,
    rows: &[SampleRow],
    trade_date: NaiveDate,
    taken_at: DateTime<FixedOffset>,
) -> Result<u64, WorkerError> {
    if rows.is_empty() {
        return Ok(0);
    }
    let done = sqlx::query(
        "INSERT INTO fo_spread_sample (trade_date, symbol, expiry, strike, option_type, bid, \
         ask, mid, oi, taken_at) SELECT $1, t.*, $10 FROM UNNEST($2::varchar[], $3::date[], \
         $4::numeric[], $5::varchar[], $6::numeric[], $7::numeric[], $8::numeric[], \
         $9::int8[]) AS t ON CONFLICT ON CONSTRAINT uq_fo_spread_sample_contract_moment \
         DO NOTHING",
    )
    .bind(trade_date)
    .bind(rows.iter().map(|r| r.symbol.clone()).collect::<Vec<_>>())
    .bind(rows.iter().map(|r| r.expiry).collect::<Vec<_>>())
    .bind(rows.iter().map(|r| r.strike).collect::<Vec<_>>())
    .bind(
        rows.iter()
            .map(|r| r.option_type.clone())
            .collect::<Vec<_>>(),
    )
    .bind(rows.iter().map(|r| r.bid).collect::<Vec<_>>())
    .bind(rows.iter().map(|r| r.ask).collect::<Vec<_>>())
    .bind(rows.iter().map(|r| r.mid).collect::<Vec<_>>())
    .bind(rows.iter().map(|r| r.oi).collect::<Vec<_>>())
    .bind(taken_at)
    .execute(&mut *conn)
    .await?;
    Ok(done.rows_affected())
}

/// `read_results`: Q8, each name's forward result meetings, fail-soft per symbol.
pub(crate) async fn read_results(
    source: &NseProvider,
    symbols: &[String],
    today: NaiveDate,
    report: &mut SpreadReport,
) -> Result<(), WorkerError> {
    for symbol in symbols {
        let meetings = match source
            .results_calendar(std::slice::from_ref(symbol), today)
            .await
        {
            Ok(m) => m,
            Err(e) if e.is_provider_error() => {
                report.results_errors.push((symbol.clone(), describe(&e)));
                log_warning(&format!("results calendar for {symbol} failed: {e}"));
                continue;
            }
            Err(e) => return Err(ProviderError::into(e)),
        };
        let ahead = upcoming_results(&meetings, today)
            .remove(&symbol.to_uppercase())
            .unwrap_or_default();
        report
            .results
            .push((symbol.clone(), ahead.into_iter().map(iso).collect()));
    }
    Ok(())
}

/// Builds the quote reader only when asked (no Kite adapter without a session).
#[allow(async_fn_in_trait)]
pub(crate) trait QuoteSource {
    /// Is there a usable Kite session?
    fn kite_ok(&self) -> bool;
    /// `quotes().option_quotes(keys)`.
    async fn option_quotes(&self, keys: &[String]) -> Result<Vec<OptionQuoteRecord>, WorkerError>;
}

/// `run_spread_sample`: one 15:00 run. The caller has checked the flag, the window and the
/// trading day.
pub(crate) async fn run_spread_sample<Q: QuoteSource>(
    conn: &mut PgConnection,
    now: DateTime<FixedOffset>,
    quotes: &Q,
    results: Option<&NseProvider>,
) -> Result<SpreadReport, WorkerError> {
    let today = now
        .with_timezone(&baskfy_providers::tokens::ist())
        .date_naive();
    let mut report = SpreadReport {
        trade_date: today,
        taken_at: now,
        reference_session: None,
        underlyings: Vec::new(),
        no_sample: Vec::new(),
        keys: 0,
        calls: 0,
        rows: 0,
        inserted: 0,
        unanswered: 0,
        one_sided: 0,
        skipped: None,
        results: Vec::new(),
        results_errors: Vec::new(),
    };
    let (stocks, picked) = plan_sample(conn, today, &mut report).await?;
    if report.skipped.is_none() {
        if picked.is_empty() {
            report.skipped = Some("no listed near-monthly strikes for any sampled name".to_owned());
        } else if !quotes.kite_ok() {
            report.skipped = Some("no usable Kite session".to_owned());
        } else {
            let keys: Vec<String> = picked.iter().map(SampleContract::key).collect();
            report.keys = keys.len();
            report.calls = quote_calls(keys.len());
            let mut answered = BTreeMap::new();
            for q in quotes.option_quotes(&keys).await? {
                answered.insert(q.key(), q);
            }
            let rows = sample_rows(&picked, &answered);
            report.rows = rows.len();
            report.unanswered = picked.len() - rows.len();
            report.one_sided = rows.iter().filter(|r| r.mid.is_none()).count();
            report.inserted = insert_samples(conn, &rows, today, now).await?;
        }
    }
    if let Some(source) = results
        && !stocks.is_empty()
    {
        read_results(source, &stocks, today, &mut report).await?;
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn dec(t: &str) -> Decimal {
        t.parse().unwrap()
    }

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    /// `test_fno_spread_sample.py`: ties by symbol, top N, an even count's median is the mean.
    #[test]
    fn ranks_by_median_turnover() {
        let mut t = BTreeMap::new();
        t.insert("B".to_owned(), vec![dec("10"), dec("30")]);
        t.insert("A".to_owned(), vec![dec("20")]);
        t.insert("C".to_owned(), vec![dec("5")]);
        assert_eq!(rank_by_turnover(&t, 2).unwrap(), vec!["A", "B"]);
        assert_eq!(
            decimal_median(&[dec("1"), dec("2")]).unwrap(),
            Some(dec("1.5"))
        );
    }

    #[test]
    fn the_near_monthly_is_after_today_never_on_it() {
        let listed = [d(2026, 10, 27), d(2026, 10, 20), d(2026, 11, 24)];
        assert_eq!(
            near_monthly(listed.into_iter(), d(2026, 10, 27)),
            Some(d(2026, 11, 24))
        );
        assert_eq!(
            near_monthly(listed.into_iter(), d(2026, 10, 26)),
            Some(d(2026, 10, 27))
        );
    }

    #[test]
    fn the_atm_window_centres_on_the_nearest_lower_on_a_tie() {
        let ladder: Vec<Decimal> = (1..=10).map(|k| Decimal::from(k * 100)).collect();
        assert_eq!(
            atm_window(&ladder, dec("450"), 1),
            vec![dec("300"), dec("400"), dec("500")]
        );
        assert_eq!(atm_window(&ladder, dec("100"), 2).len(), 3);
        assert!(atm_window(&[], dec("1"), 3).is_empty());
    }

    #[test]
    fn the_reference_falls_back_to_the_nearest_future() {
        let f = vec![
            (d(2026, 11, 24), Some(dec("100"))),
            (d(2026, 12, 29), Some(dec("101"))),
        ];
        assert_eq!(reference_level(&f, d(2026, 11, 24)), Some(dec("100")));
        assert_eq!(reference_level(&f, d(2026, 12, 20)), Some(dec("101")));
        assert_eq!(reference_level(&[], d(2026, 12, 20)), None);
    }

    #[test]
    fn half_spreads_skip_one_sided_and_crossed_books() {
        assert_eq!(
            half_spread_ratio(Some(dec("9")), Some(dec("11"))).unwrap(),
            Some(dec("0.1"))
        );
        assert_eq!(half_spread_ratio(None, Some(dec("11"))).unwrap(), None);
        assert_eq!(
            half_spread_ratio(Some(dec("12")), Some(dec("11"))).unwrap(),
            None
        );
        assert_eq!(
            half_spread_ratio(Some(dec("0")), Some(dec("11"))).unwrap(),
            None
        );
    }

    #[test]
    fn quote_calls_batch_at_500() {
        assert_eq!(quote_calls(0), 0);
        assert_eq!(quote_calls(448), 1);
        assert_eq!(quote_calls(501), 2);
    }

    #[test]
    fn the_window_is_inside_0915_1530_ist() {
        let at = |h, m, s| {
            d(2026, 10, 29)
                .and_hms_opt(h, m, s)
                .unwrap()
                .and_local_timezone(baskfy_providers::tokens::ist())
                .unwrap()
        };
        assert!(in_session(at(9, 15, 0)));
        assert!(in_session(at(15, 30, 0)));
        assert!(!in_session(at(15, 30, 1)));
        assert!(!in_session(at(9, 14, 59)));
    }

    fn obs(
        day: NaiveDate,
        bid: &str,
        ask: Option<&str>,
        strike: &str,
        minute: u32,
    ) -> SpreadObservation {
        SpreadObservation {
            trade_date: day,
            symbol: "SBIN".to_owned(),
            expiry: d(2027, 11, 25),
            strike: dec(strike),
            option_type: "CE".to_owned(),
            bid: Some(dec(bid)),
            ask: ask.map(dec),
            taken_at: day.and_hms_opt(9, minute, 0).unwrap().and_utc(),
        }
    }

    /// `TestTheStatistic`: the median over two-sided rows and the session count; a re-run in a
    /// session counts once at its latest; only the last N sessions are read.
    #[test]
    fn the_statistic_reads_two_sided_books_latest_and_last_n() {
        let (s1, s2, s3) = (d(2027, 11, 1), d(2027, 11, 2), d(2027, 11, 3));
        let stats = half_spread_stats(
            vec![
                obs(s1, "9", Some("11"), "800", 30),
                obs(s1, "19", Some("21"), "820", 30),
                obs(s2, "99", Some("101"), "800", 30),
                obs(s3, "5", None, "800", 30),
            ],
            MIN_SPREAD_SESSIONS,
        )
        .unwrap();
        let stat = &stats["SBIN"];
        assert_eq!(stat.median_half_spread_pct_of_mid, dec("0.05"));
        assert_eq!(stat.sessions, 2);
        let rerun = half_spread_stats(
            vec![
                obs(s1, "9", Some("11"), "800", 30),
                obs(s1, "99", Some("101"), "800", 35),
            ],
            MIN_SPREAD_SESSIONS,
        )
        .unwrap();
        assert_eq!(rerun["SBIN"].median_half_spread_pct_of_mid, dec("0.01"));
        let last = half_spread_stats(
            vec![
                obs(s1, "9", Some("11"), "800", 30),
                obs(s2, "99", Some("101"), "800", 30),
            ],
            1,
        )
        .unwrap();
        assert_eq!(last["SBIN"].sessions, 1);
        assert_eq!(last["SBIN"].median_half_spread_pct_of_mid, dec("0.01"));
    }

    /// `TestTheRows`: a two-sided book rounds its mid half up; a crossed book keeps both prices
    /// and no mid; an unanswered key writes nothing.
    #[test]
    fn rows_round_the_mid_and_keep_a_crossed_book() {
        use baskfy_providers::records::{DepthLevelFields, DepthLevelRecord, OptionQuoteFields};
        let level = |p: &str| {
            DepthLevelRecord::new(DepthLevelFields {
                price: dec(p),
                quantity: 1,
                orders: 1,
            })
            .unwrap()
        };
        let quote = |symbol: &str, bid: &str, ask: &str| {
            OptionQuoteRecord::new(OptionQuoteFields {
                symbol: symbol.to_owned(),
                exchange: "NFO".to_owned(),
                instrument_token: None,
                last_price: None,
                volume: 0,
                oi: Some(10),
                oi_day_high: None,
                oi_day_low: None,
                bids: vec![level(bid)],
                asks: vec![level(ask)],
                as_of: None,
                last_trade_time: None,
            })
            .unwrap()
        };
        let contract = |ts: &str| SampleContract {
            symbol: "SBIN".to_owned(),
            tradingsymbol: ts.to_owned(),
            expiry: d(2027, 11, 25),
            strike: dec("800"),
            option_type: "CE".to_owned(),
        };
        let mut quotes = BTreeMap::new();
        quotes.insert("NFO:A".to_owned(), quote("A", "10.00", "10.05"));
        quotes.insert("NFO:B".to_owned(), quote("B", "10.05", "10.00"));
        let rows = sample_rows(&[contract("A"), contract("B"), contract("C")], &quotes);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].mid, Some(dec("10.03")));
        assert_eq!(rows[0].strike.to_string(), "800.00");
        assert_eq!(
            (rows[1].bid, rows[1].ask, rows[1].mid),
            (Some(dec("10.05")), Some(dec("10.00")), None)
        );
    }
    fn contract(symbol: &str, expiry: NaiveDate, strike: &str, kind: &str) -> SampleContract {
        SampleContract {
            symbol: symbol.to_owned(),
            tradingsymbol: format!("{symbol}{}{strike}{kind}", expiry.format("%y%b%d"))
                .to_uppercase(),
            expiry,
            strike: dec(strike),
            option_type: kind.to_owned(),
        }
    }

    /// `TestTheNames`.
    #[test]
    fn ranked_by_median_turnover_not_one_big_day() {
        let mut t = BTreeMap::new();
        t.insert("SPIKE".to_owned(), vec![dec("1"), dec("1"), dec("900")]);
        t.insert("STEADY".to_owned(), vec![dec("50"), dec("50"), dec("50")]);
        t.insert("SMALL".to_owned(), vec![dec("10")]);
        assert_eq!(rank_by_turnover(&t, 2).unwrap(), vec!["STEADY", "SMALL"]);
    }

    #[test]
    fn ties_break_by_symbol_and_the_cut_is_top() {
        let t: BTreeMap<String, Vec<Decimal>> = (0..40)
            .map(|i| (format!("S{i:02}"), vec![dec("5")]))
            .collect();
        let ranked = rank_by_turnover(&t, TOP_STOCKS).unwrap();
        assert_eq!(ranked.len(), TOP_STOCKS);
        assert_eq!(ranked[..2], ["S00", "S01"]);
    }

    #[test]
    fn the_key_budget_fits_one_quote_call() {
        let keys = (TOP_STOCKS + INDEX_UNDERLYINGS.len()) * (2 * STRIKES_EACH_SIDE + 1) * 2;
        assert_eq!(keys, 448);
        assert_eq!(quote_calls(keys), 1);
        for (keys, calls) in [(0, 0), (1, 1), (500, 1), (501, 2), (1001, 3)] {
            assert_eq!(quote_calls(keys), calls);
        }
    }

    /// `TestTheExpiry`.
    #[test]
    fn monthly_is_the_last_listed_expiry_and_weeklies_are_skipped() {
        let (nov, dec_) = (d(2027, 11, 25), d(2027, 12, 30));
        let weekly = d(2027, 11, 9);
        let listed = [weekly, d(2027, 11, 16), nov, d(2027, 12, 7), dec_];
        assert_eq!(monthly_expiries(listed.into_iter()), vec![nov, dec_]);
        assert_eq!(
            near_monthly([weekly, nov, dec_].into_iter(), d(2027, 11, 4)),
            Some(nov)
        );
        assert_eq!(near_monthly([nov, dec_].into_iter(), nov), Some(dec_));
        assert_eq!(
            near_monthly([d(2027, 10, 28)].into_iter(), d(2027, 11, 4)),
            None
        );
        let futures = vec![(nov, Some(dec("810.10"))), (dec_, Some(dec("815.00")))];
        assert_eq!(reference_level(&futures, nov), Some(dec("810.10")));
        assert_eq!(reference_level(&futures[1..], nov), Some(dec("815.00")));
    }

    /// `TestTheStrikes`.
    #[test]
    fn three_listed_strikes_each_side_clipped_at_an_edge() {
        let ladder: Vec<Decimal> = (700..=920).step_by(20).map(Decimal::from).collect();
        let want = |ks: &[i64]| ks.iter().copied().map(Decimal::from).collect::<Vec<_>>();
        assert_eq!(
            atm_window(&ladder, dec("808"), 3),
            want(&[740, 760, 780, 800, 820, 840, 860])
        );
        assert_eq!(atm_window(&ladder, dec("810"), 3)[3], dec("800"));
        assert_eq!(
            atm_window(&ladder, dec("690"), 3),
            want(&[700, 720, 740, 760])
        );
        let irregular: Vec<Decimal> = ["90", "95", "100", "102.5", "105", "110", "120", "140"]
            .map(dec)
            .to_vec();
        assert_eq!(
            atm_window(&irregular, dec("103"), 3),
            ["90", "95", "100", "102.5", "105", "110", "120"]
                .map(dec)
                .to_vec()
        );
    }

    #[test]
    fn pick_is_the_near_monthly_both_types() {
        let (nov, dec_) = (d(2027, 11, 25), d(2027, 12, 30));
        let mut contracts = Vec::new();
        for expiry in [nov, dec_] {
            for k in (700..=920).step_by(20) {
                for kind in ["CE", "PE"] {
                    contracts.push(contract("SBIN", expiry, &k.to_string(), kind));
                }
            }
        }
        let picked = pick_sample(&contracts, dec("808"), d(2027, 11, 4), STRIKES_EACH_SIDE);
        assert_eq!(picked.len(), 14);
        assert!(picked.iter().all(|c| c.expiry == nov));
        let kinds: BTreeSet<&str> = picked.iter().map(|c| c.option_type.as_str()).collect();
        assert_eq!(kinds, BTreeSet::from(["CE", "PE"]));
    }

    fn quote(key: &str, bid: Option<&str>, ask: Option<&str>) -> OptionQuoteRecord {
        use baskfy_providers::records::{DepthLevelFields, DepthLevelRecord, OptionQuoteFields};
        let level = |p: &str| {
            vec![
                DepthLevelRecord::new(DepthLevelFields {
                    price: dec(p),
                    quantity: 50,
                    orders: 1,
                })
                .unwrap(),
            ]
        };
        OptionQuoteRecord::new(OptionQuoteFields {
            symbol: key.trim_start_matches("NFO:").to_owned(),
            exchange: "NFO".to_owned(),
            instrument_token: None,
            last_price: None,
            volume: 0,
            oi: Some(1200),
            oi_day_high: None,
            oi_day_low: None,
            bids: bid.map(level).unwrap_or_default(),
            asks: ask.map(level).unwrap_or_default(),
            as_of: None,
            last_trade_time: None,
        })
        .unwrap()
    }

    /// `TestTheRows`: half-up mid; a one-sided or empty book keeps its nulls.
    #[test]
    fn a_one_sided_or_empty_book_is_stored_with_nulls() {
        let nov = d(2027, 11, 25);
        let (two, bid_only, empty) = (
            contract("SBIN", nov, "780", "CE"),
            contract("SBIN", nov, "800", "CE"),
            contract("SBIN", nov, "820", "CE"),
        );
        let mut quotes = BTreeMap::new();
        quotes.insert(two.key(), quote(&two.key(), Some("10.05"), Some("10.10")));
        quotes.insert(bid_only.key(), quote(&bid_only.key(), Some("3.00"), None));
        quotes.insert(empty.key(), quote(&empty.key(), None, None));
        let rows = sample_rows(&[two, bid_only, empty], &quotes);
        assert_eq!(
            rows[0].mid,
            Some(dec("10.08")),
            "10.075 half-up (house rule 8)"
        );
        assert_eq!(rows[0].oi, Some(1200));
        assert_eq!(
            (rows[1].bid, rows[1].ask, rows[1].mid),
            (Some(dec("3.00")), None, None)
        );
        assert_eq!((rows[2].bid, rows[2].ask, rows[2].mid), (None, None, None));
        assert!(sample_rows(&[contract("SBIN", nov, "800", "CE")], &BTreeMap::new()).is_empty());
    }

    /// `test_the_twenty_session_rule`: nineteen sessions are not a measurement, twenty are.
    #[test]
    fn the_twenty_session_rule() {
        let days: Vec<NaiveDate> = (0..20)
            .map(|i| d(2027, 10, 1) + chrono::Duration::days(i))
            .collect();
        let run = |n: usize| {
            half_spread_stats(
                days[..n]
                    .iter()
                    .map(|&day| obs(day, "9", Some("11"), "800", 30))
                    .collect(),
                MIN_SPREAD_SESSIONS,
            )
            .unwrap()["SBIN"]
                .sessions
        };
        assert!(run(19) < MIN_SPREAD_SESSIONS);
        assert!(run(20) >= MIN_SPREAD_SESSIONS);
        assert!(half_spread_stats(vec![], 0).is_err());
    }

    /// `TestTheResultsCalendar::test_forward_only`.
    #[test]
    fn the_results_calendar_is_forward_only() {
        use baskfy_providers::records::EarningsDateFields;
        let meeting = |day: NaiveDate| {
            EarningsDateRecord::new(EarningsDateFields {
                symbol: "sbin".to_owned(),
                event_date: day,
                purpose: "Financial Results".to_owned(),
                url: "https://example.test/".to_owned(),
            })
            .unwrap()
        };
        let today = d(2027, 11, 4);
        let out = upcoming_results(
            &[
                meeting(d(2027, 10, 20)),
                meeting(today),
                meeting(d(2027, 12, 30)),
                meeting(today),
            ],
            today,
        );
        assert_eq!(out["SBIN"], vec![today, d(2027, 12, 30)]);
    }
}
