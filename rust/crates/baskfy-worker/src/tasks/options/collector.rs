//! The once-a-minute chain collector (`options.collector`, OP3): the two nearest NIFTY expiries'
//! strikes within 15 steps of ATM, CE and PE, plus `NSE:NIFTY 50`, in **one** `quote()` call;
//! each contract one `op_chain_snapshot` row with top of book, Kite's depth, volume, OI, the
//! expiry's parity forward and Black-76 IV and greeks computed at write and rounded to the column
//! (house rule 8). Append-only and idempotent (`ON CONFLICT DO NOTHING`, house rule 7); this
//! month's and next month's partitions are ensured first (there is no DEFAULT partition).
//!
//! The ATM hint is the newest stored NIFTY 50 close of the last four days; only without one is a
//! second `quote()` spent on the spot alone. Reads prices only — no order path (law 2).

use std::collections::BTreeMap;
use std::time::Instant;

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_core::fno::vol::OptionType;
use baskfy_db::OrderedJson;
use baskfy_providers::kite::{KiteProvider, QUOTE_BATCH_SIZE};
use baskfy_providers::records::OptionQuoteRecord;
use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, Timelike};
use rust_decimal::Decimal;
use sqlx::{PgConnection, Postgres, QueryBuilder};

use super::common::{count, dstr, in_ist, iso, iso_dt, quantize_half_up, seconds_json};
use super::index_bars::{NIFTY_50, latest_close};
use super::model::{
    Contract, GREEKS_MODEL, Greeks, OptionQuote, RATE, SNAPSHOT_STRIKES, market_close, market_open,
    option_type_value, parity_forward, quote_greeks, snapshot_strikes, strike_step, year_fraction,
};
use crate::error::WorkerError;
use crate::pyjson::{object, opt_s, s};

/// `collector.SPOT_KEY`.
pub(crate) const SPOT_KEY: &str = "NSE:NIFTY 50";
const NFO: &str = "NFO";
const EXPIRIES_PER_SNAPSHOT: usize = 2;
const SOURCE_QUOTE: &str = "QUOTE";

/// `collector.CollectReport`.
#[derive(Debug, Clone, Default)]
pub(crate) struct CollectReport {
    pub(crate) ts: Option<DateTime<FixedOffset>>,
    pub(crate) rows: usize,
    pub(crate) inserted: u64,
    pub(crate) calls: usize,
    pub(crate) symbols: usize,
    pub(crate) expiries: Vec<String>,
    pub(crate) unquoted: usize,
    pub(crate) no_greeks: usize,
    pub(crate) spot: Option<String>,
    pub(crate) spot_hint: Option<String>,
    pub(crate) skipped: Option<String>,
    pub(crate) partitions: Vec<String>,
    pub(crate) seconds: f64,
}

impl CollectReport {
    /// `as_dict()`.
    pub(crate) fn as_dict(&self) -> OrderedJson {
        object([
            ("ts", opt_s(self.ts.map(iso_dt))),
            ("rows", count(self.rows)),
            (
                "inserted",
                OrderedJson::Int(i64::try_from(self.inserted).unwrap_or(i64::MAX)),
            ),
            ("calls", count(self.calls)),
            ("symbols", count(self.symbols)),
            (
                "expiries",
                OrderedJson::Array(self.expiries.iter().map(|e| s(e.clone())).collect()),
            ),
            ("unquoted", count(self.unquoted)),
            ("no_greeks", count(self.no_greeks)),
            ("spot", opt_s(self.spot.clone())),
            ("spot_hint", opt_s(self.spot_hint.clone())),
            ("skipped", opt_s(self.skipped.clone())),
            (
                "partitions",
                OrderedJson::Array(self.partitions.iter().map(|p| s(p.clone())).collect()),
            ),
            ("seconds", seconds_json(self.seconds)),
        ])
    }
}

// ---------------------------------------------------------------------------------------------
// Pure: which contracts, and what a row holds
// ---------------------------------------------------------------------------------------------

/// `collector.ChainPick`.
#[derive(Debug, Clone, Default)]
pub(crate) struct ChainPick {
    pub(crate) expiries: Vec<NaiveDate>,
    pub(crate) contracts: Vec<Contract>,
    pub(crate) steps: Vec<(NaiveDate, Decimal)>,
}

impl ChainPick {
    /// `keys()`: `NFO:<symbol>` per contract, then the spot — one batch, or `TooManySymbols`.
    pub(crate) fn keys(&self) -> Result<Vec<String>, WorkerError> {
        let mut keys: Vec<String> = self
            .contracts
            .iter()
            .map(|c| format!("{NFO}:{}", c.tradingsymbol))
            .collect();
        keys.push(SPOT_KEY.to_owned());
        if keys.len() > QUOTE_BATCH_SIZE {
            return Err(WorkerError::python(
                "TooManySymbols",
                format!(
                    "{} symbols exceed one quote() call ({QUOTE_BATCH_SIZE}); the collector makes \
                     exactly one call a minute (docs/options/03 §5)",
                    keys.len()
                ),
            ));
        }
        Ok(keys)
    }
}

/// `nearest_expiries(contracts, today)`: the two nearest listed expiries on or after `today`.
fn nearest_expiries(contracts: &[Contract], today: NaiveDate) -> Vec<NaiveDate> {
    let set: std::collections::BTreeSet<NaiveDate> = contracts
        .iter()
        .map(|c| c.expiry)
        .filter(|e| *e >= today)
        .collect();
    set.into_iter().take(EXPIRIES_PER_SNAPSHOT).collect()
}

/// `pick_contracts(contracts, spot, today, config)`.
pub(crate) fn pick_contracts(
    contracts: &[Contract],
    spot: Decimal,
    today: NaiveDate,
    window: usize,
) -> Result<ChainPick, WorkerError> {
    let wanted = nearest_expiries(contracts, today);
    let mut pick = ChainPick::default();
    for expiry in wanted {
        let rows: Vec<&Contract> = contracts.iter().filter(|c| c.expiry == expiry).collect();
        let Some(step) = strike_step(rows.iter().map(|c| c.strike), spot, window) else {
            continue;
        };
        pick.steps.push((expiry, step));
        pick.expiries.push(expiry);
        let strikes = snapshot_strikes(rows.iter().map(|c| c.strike), spot, step, window)?;
        let mut chosen: Vec<&Contract> = rows
            .into_iter()
            .filter(|c| strikes.contains(&c.strike))
            .collect();
        chosen.sort_by(|a, b| {
            (a.strike, option_type_value(a.option_type))
                .cmp(&(b.strike, option_type_value(b.option_type)))
        });
        pick.contracts.extend(chosen.into_iter().cloned());
    }
    Ok(pick)
}

/// `to_option_quote(record, contract, ts)` — the fields the forward and greeks read.
fn to_option_quote(record: &OptionQuoteRecord, contract: &Contract) -> OptionQuote {
    OptionQuote {
        expiry: contract.expiry,
        strike: contract.strike,
        option_type: contract.option_type,
        bid: record.bids.first().map(|l| l.price),
        ask: record.asks.first().map(|l| l.price),
    }
}

/// `_cent(value)`.
fn cent(value: Option<Decimal>) -> Option<Decimal> {
    value.map(|v| quantize_half_up(v, 2))
}

/// `_greek(value)`: `Decimal(repr(value))` half-up to six places, `None` when not finite or
/// outside `numeric(10,6)` (never truncated).
fn greek(value: f64) -> Option<Decimal> {
    if !value.is_finite() {
        return None;
    }
    let exact: WideDecimal = baskfy_core::py::float_repr(value).parse().ok()?;
    let rounded = exact.rescale_exact(-6, Rounding::HalfUp);
    if rounded.abs() >= WideDecimal::from(10_000_i64) {
        return None;
    }
    // Postgres' numeric has no negative zero (`-0.000000` is stored as `0.000000`).
    rounded
        .to_decimal()
        .map(|d| if d.is_zero() { d.abs() } else { d })
}

/// `_depth_json(record)`: five levels a side, as Kite gave them.
fn depth_json(record: &OptionQuoteRecord) -> OrderedJson {
    let side = |levels: &[baskfy_providers::records::DepthLevelRecord]| {
        OrderedJson::Array(
            levels
                .iter()
                .map(|lv| {
                    object([
                        ("price", s(dstr(lv.price))),
                        ("quantity", OrderedJson::Int(lv.quantity)),
                        ("orders", OrderedJson::Int(lv.orders)),
                    ])
                })
                .collect(),
        )
    };
    object([("buy", side(&record.bids)), ("sell", side(&record.asks))])
}

/// One `op_chain_snapshot` row.
#[derive(Debug, Clone)]
pub(crate) struct SnapshotRow {
    pub(crate) ts: DateTime<FixedOffset>,
    pub(crate) instrument_token: i64,
    pub(crate) expiry: NaiveDate,
    pub(crate) strike: Decimal,
    pub(crate) option_type: OptionType,
    pub(crate) spot: Option<Decimal>,
    pub(crate) bid: Option<Decimal>,
    pub(crate) ask: Option<Decimal>,
    pub(crate) last: Option<Decimal>,
    pub(crate) bid_qty: Option<i64>,
    pub(crate) ask_qty: Option<i64>,
    pub(crate) depth_json: OrderedJson,
    pub(crate) volume: i64,
    pub(crate) oi: Option<i64>,
    pub(crate) forward: Option<Decimal>,
    pub(crate) greeks: [Option<Decimal>; 5],
    pub(crate) greeks_model: Option<&'static str>,
}

/// `snapshot_rows(pick, quotes, spot, minute=..., now=..., config=...)`: the rows for one minute
/// and how many carry no greeks.
pub(crate) fn snapshot_rows(
    pick: &ChainPick,
    quotes: &BTreeMap<String, OptionQuoteRecord>,
    spot: Option<Decimal>,
    minute: DateTime<FixedOffset>,
    now: DateTime<FixedOffset>,
) -> Result<(Vec<SnapshotRow>, usize), WorkerError> {
    let mut core: Vec<(i64, OptionQuote)> = Vec::new();
    for c in &pick.contracts {
        if let Some(record) = quotes.get(&format!("{NFO}:{}", c.tradingsymbol)) {
            let quote = to_option_quote(record, c);
            if let Some(slot) = core.iter_mut().find(|(t, _)| *t == c.instrument_token) {
                slot.1 = quote;
            } else {
                core.push((c.instrument_token, quote));
            }
        }
    }
    let mut forwards: Vec<(NaiveDate, Option<WideDecimal>)> = Vec::new();
    for (expiry, step) in &pick.steps {
        let years = year_fraction(now, *expiry, market_close());
        let same: Vec<&OptionQuote> = core
            .iter()
            .map(|(_, q)| q)
            .filter(|q| q.expiry == *expiry)
            .collect();
        let forward = match spot {
            Some(spot) if years > 0.0 && !same.is_empty() => {
                parity_forward(&same, spot, *step, years, RATE)?
            }
            _ => None,
        };
        forwards.push((*expiry, forward));
    }
    let mut rows = Vec::new();
    let mut no_greeks = 0;
    for c in &pick.contracts {
        let Some(record) = quotes.get(&format!("{NFO}:{}", c.tradingsymbol)) else {
            continue;
        };
        let Some((_, quote)) = core.iter().find(|(t, _)| *t == c.instrument_token) else {
            continue;
        };
        let forward = forwards
            .iter()
            .find(|(e, _)| *e == c.expiry)
            .and_then(|(_, f)| f.clone());
        let greeks: Option<Greeks> = forward
            .as_ref()
            .and_then(|f| quote_greeks(quote, f, now, c.tick_size));
        if greeks.is_none() {
            no_greeks += 1;
        }
        let forward_cent = match &forward {
            Some(f) => Some(
                f.quantize(-2, Rounding::HalfUp)
                    .ok()
                    .and_then(|q| q.to_decimal())
                    .ok_or_else(|| {
                        WorkerError::python("InvalidOperation", "a forward beyond numeric(18,2)")
                    })?,
            ),
            None => None,
        };
        let g = |pick: fn(&Greeks) -> f64| greeks.as_ref().and_then(|x| greek(pick(x)));
        rows.push(SnapshotRow {
            ts: minute,
            instrument_token: c.instrument_token,
            expiry: c.expiry,
            strike: quantize_half_up(c.strike, 2),
            option_type: c.option_type,
            spot: cent(spot),
            bid: cent(quote.bid),
            ask: cent(quote.ask),
            last: cent(record.last_price),
            bid_qty: record.bids.first().map(|l| l.quantity),
            ask_qty: record.asks.first().map(|l| l.quantity),
            depth_json: depth_json(record),
            volume: record.volume,
            oi: record.oi,
            forward: forward_cent,
            greeks: [
                g(|x| x.iv),
                g(|x| x.delta),
                g(|x| x.gamma),
                g(|x| x.theta),
                g(|x| x.vega),
            ],
            greeks_model: forward.is_some().then_some(GREEKS_MODEL),
        });
    }
    Ok((rows, no_greeks))
}

/// `minute_of(now)`: `now` in IST, floored to the minute.
pub(crate) fn minute_of(now: DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    let ist = in_ist(now);
    ist.with_second(0)
        .and_then(|t| t.with_nanosecond(0))
        .unwrap_or(ist)
}

/// `in_session(now)`: 09:15 ≤ now ≤ 15:30 IST.
pub(crate) fn in_session(now: DateTime<FixedOffset>) -> bool {
    let clock = in_ist(now).time();
    market_open() <= clock && clock <= market_close()
}

// ---------------------------------------------------------------------------------------------
// I/O
// ---------------------------------------------------------------------------------------------

/// `partitions.month_bounds(day)`: the first day of `day`'s month and of the month after.
pub(crate) fn month_bounds(day: NaiveDate) -> (NaiveDate, NaiveDate) {
    let start = day.with_day0(0).unwrap_or(day);
    let end = if day.month0() == 11 {
        NaiveDate::from_ymd_opt(day.year() + 1, 1, 1)
    } else {
        NaiveDate::from_ymd_opt(day.year(), day.month() + 1, 1)
    }
    .unwrap_or(start);
    (start, end)
}

/// `partitions.ensure_chain_partition(session, day)`: `day`'s month partition, created if
/// missing (bounds are IST midnights; the name and bounds come from a date, never input text).
pub(crate) async fn ensure_chain_partition(
    conn: &mut PgConnection,
    day: NaiveDate,
) -> Result<String, WorkerError> {
    let (start, end) = month_bounds(day);
    let name = format!("op_chain_snapshot_{:04}{:02}", start.year(), start.month());
    let ddl = format!(
        "CREATE TABLE IF NOT EXISTS {name} PARTITION OF op_chain_snapshot \
         FOR VALUES FROM ('{} 00:00:00+05:30') TO ('{} 00:00:00+05:30')",
        iso(start),
        iso(end)
    );
    sqlx::raw_sql(&ddl).execute(&mut *conn).await?;
    Ok(name)
}

/// `ensure_partitions(session, day)`: this month's and next month's.
async fn ensure_partitions(
    conn: &mut PgConnection,
    day: NaiveDate,
) -> Result<Vec<String>, WorkerError> {
    let (_, next_month) = month_bounds(day);
    Ok(vec![
        ensure_chain_partition(conn, day).await?,
        ensure_chain_partition(conn, next_month).await?,
    ])
}

/// `insert_rows(session, rows)`: append-only; the rows actually inserted.
async fn insert_rows(conn: &mut PgConnection, rows: &[SnapshotRow]) -> Result<u64, WorkerError> {
    if rows.is_empty() {
        return Ok(0);
    }
    let mut depth: Vec<String> = Vec::with_capacity(rows.len());
    for row in rows {
        depth.push(
            row.depth_json
                .to_python_json()
                .map_err(|e| WorkerError::python("ValueError", e.to_string()))?,
        );
    }
    let mut q: QueryBuilder<'_, Postgres> = QueryBuilder::new(
        "INSERT INTO op_chain_snapshot (ts, instrument_token, expiry, strike, option_type, spot, \
         bid, ask, last, bid_qty, ask_qty, depth_json, volume, oi, forward, iv, delta, gamma, \
         theta, vega, greeks_model, source) ",
    );
    let mut bad_qty = false;
    q.push_values(rows.iter().zip(depth), |mut b, (row, depth)| {
        let mut qty = |v: Option<i64>| -> Option<i32> {
            v.map(|n| {
                i32::try_from(n).unwrap_or_else(|_| {
                    bad_qty = true;
                    0
                })
            })
        };
        let [iv, delta, gamma, theta, vega] = row.greeks;
        b.push_bind(row.ts)
            .push_bind(row.instrument_token)
            .push_bind(row.expiry)
            .push_bind(row.strike)
            .push_bind(option_type_value(row.option_type))
            .push_bind(row.spot)
            .push_bind(row.bid)
            .push_bind(row.ask)
            .push_bind(row.last)
            .push_bind(qty(row.bid_qty))
            .push_bind(qty(row.ask_qty))
            .push_bind(depth)
            .push_unseparated("::jsonb")
            .push_bind(row.volume)
            .push_bind(row.oi)
            .push_bind(row.forward)
            .push_bind(iv)
            .push_bind(delta)
            .push_bind(gamma)
            .push_bind(theta)
            .push_bind(vega)
            .push_bind(row.greeks_model)
            .push_bind(SOURCE_QUOTE);
    });
    if bad_qty {
        return Err(WorkerError::python(
            "DataError",
            "a depth quantity is out of range for type integer",
        ));
    }
    q.push(" ON CONFLICT (ts, instrument_token) DO NOTHING");
    let done = q.build().execute(&mut *conn).await?;
    Ok(done.rows_affected())
}

/// One `op_contract` row as read.
type ContractRow = (
    i64,
    String,
    String,
    NaiveDate,
    Decimal,
    String,
    i32,
    Decimal,
);

/// `master.load_contracts(session)`: every listed (`expired = false`) NIFTY contract.
pub(crate) async fn load_contracts(conn: &mut PgConnection) -> Result<Vec<Contract>, WorkerError> {
    let rows: Vec<ContractRow> = sqlx::query_as(
        "SELECT op_contract.instrument_token, op_contract.tradingsymbol, \
             op_contract.underlying, op_contract.expiry, op_contract.strike, \
             op_contract.option_type, op_contract.lot_size, op_contract.tick_size \
             FROM op_contract WHERE op_contract.underlying = $1 AND op_contract.expired IS false",
    )
    .bind(super::model::UNDERLYING)
    .fetch_all(&mut *conn)
    .await?;
    rows.into_iter()
        .map(
            |(token, symbol, underlying, expiry, strike, kind, lot, tick)| {
                let option_type = match kind.as_str() {
                    "CE" => OptionType::Ce,
                    "PE" => OptionType::Pe,
                    other => {
                        return Err(WorkerError::python(
                            "ValueError",
                            format!(
                                "{} is not a valid OptionType",
                                baskfy_providers::py::repr_str(other)
                            ),
                        ));
                    }
                };
                Ok(Contract {
                    instrument_token: token,
                    tradingsymbol: symbol,
                    underlying,
                    expiry,
                    strike,
                    option_type,
                    lot_size: i64::from(lot),
                    tick_size: tick,
                })
            },
        )
        .collect()
}

async fn quote(
    reader: &KiteProvider,
    keys: &[String],
) -> Result<Vec<OptionQuoteRecord>, WorkerError> {
    let refs: Vec<&str> = keys.iter().map(String::as_str).collect();
    reader
        .option_quotes(&refs)
        .await
        .map_err(WorkerError::Provider)
}

/// `collect_minute(session, reader, now)`: one minute's snapshot (the caller has checked the
/// flag, the window and the day).
pub(crate) async fn collect_minute(
    conn: &mut PgConnection,
    reader: &KiteProvider,
    now: DateTime<FixedOffset>,
) -> Result<CollectReport, WorkerError> {
    let started = Instant::now();
    let mut report = CollectReport {
        ts: Some(minute_of(now)),
        ..CollectReport::default()
    };
    let today = in_ist(now).date_naive();
    report.partitions = ensure_partitions(conn, today).await?;
    let contracts = load_contracts(conn).await?;
    if contracts.is_empty() {
        report.skipped =
            Some("no NIFTY contracts in op_contract (the nightly master has not run)".to_owned());
        return Ok(report);
    }
    let mut hint = latest_close(conn, NIFTY_50, now - Duration::days(4)).await?;
    if hint.is_none() {
        let spot_only = quote(reader, &[SPOT_KEY.to_owned()]).await?;
        report.calls += 1;
        hint = spot_only
            .iter()
            .find(|q| q.key() == SPOT_KEY)
            .and_then(|q| q.last_price);
    }
    let Some(hint) = hint.filter(|h| *h > Decimal::ZERO) else {
        report.skipped = Some("no NIFTY 50 level to centre the strikes on".to_owned());
        return Ok(report);
    };
    report.spot_hint = Some(dstr(hint));
    let pick = pick_contracts(&contracts, hint, today, SNAPSHOT_STRIKES)?;
    if pick.contracts.is_empty() {
        report.skipped =
            Some("no listed strikes near the spot on the two nearest expiries".to_owned());
        return Ok(report);
    }
    let keys = pick.keys()?;
    report.symbols = keys.len();
    report.expiries = pick.expiries.iter().map(|e| iso(*e)).collect();
    // `{q.key: q for q in ...}`: a repeated key keeps its last record.
    let mut answered: BTreeMap<String, OptionQuoteRecord> = BTreeMap::new();
    for record in quote(reader, &keys).await? {
        answered.insert(record.key(), record);
    }
    report.calls += 1;
    let spot = answered.get(SPOT_KEY).and_then(|q| q.last_price);
    report.spot = spot.map(dstr);
    let minute = report.ts.unwrap_or_else(|| minute_of(now));
    let (rows, no_greeks) = snapshot_rows(&pick, &answered, spot, minute, now)?;
    report.no_greeks = no_greeks;
    report.rows = rows.len();
    report.unquoted = pick.contracts.len() - rows.len();
    report.inserted = insert_rows(conn, &rows).await?;
    report.seconds = started.elapsed().as_secs_f64();
    Ok(report)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    const LOT: i64 = 65;
    const BASE: i64 = 8_000_000_000;

    fn day(m: u32, d: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, m, d).unwrap()
    }

    fn expiries() -> [NaiveDate; 3] {
        [day(9, 22), day(9, 29), day(10, 6)]
    }

    fn strikes() -> Vec<Decimal> {
        let mut out: Vec<Decimal> = (24000..=26000).step_by(50).map(Decimal::from).collect();
        out.extend((22000..24000).step_by(100).map(Decimal::from));
        out.extend((26100..=28000).step_by(100).map(Decimal::from));
        out.sort();
        out
    }

    fn contracts(list: &[NaiveDate]) -> Vec<Contract> {
        let mut out = Vec::new();
        let mut n = 0;
        for expiry in list {
            for strike in strikes() {
                for kind in [OptionType::Ce, OptionType::Pe] {
                    n += 1;
                    out.push(Contract {
                        instrument_token: BASE + n,
                        tradingsymbol: format!("FX{}{}", BASE + n, option_type_value(kind)),
                        underlying: "NIFTY".to_owned(),
                        expiry: *expiry,
                        strike,
                        option_type: kind,
                        lot_size: LOT,
                        tick_size: "0.05".parse().unwrap(),
                    });
                }
            }
        }
        out
    }

    fn spot() -> Decimal {
        "25012.35".parse().unwrap()
    }

    #[test]
    fn two_nearest_expiries_fifteen_strikes_a_side_both_types() {
        let pick = pick_contracts(&contracts(&expiries()), spot(), day(9, 22), 15).unwrap();
        assert_eq!(pick.expiries, expiries()[..2].to_vec());
        assert!(pick.steps.iter().all(|(_, s)| *s == Decimal::from(50)));
        assert_eq!(pick.contracts.len(), 124);
        let keys = pick.keys().unwrap();
        assert!(keys.contains(&SPOT_KEY.to_owned()));
        assert_eq!(keys.len(), 125);
    }

    #[test]
    fn on_an_expiry_day_after_it_the_next_two() {
        let pick = pick_contracts(&contracts(&expiries()), spot(), day(9, 23), 15).unwrap();
        assert_eq!(pick.expiries, expiries()[1..].to_vec());
    }

    #[test]
    fn a_pick_that_would_need_two_calls_is_refused() {
        // A dense 25-point grid of 401 strikes on the two nearest expiries.
        let dense: Vec<Contract> = contracts(&expiries()[..2])
            .into_iter()
            .take(4)
            .flat_map(|c| {
                (0..401_i64).map(move |i| Contract {
                    strike: Decimal::from(20_000 + 25 * i),
                    instrument_token: c.instrument_token * 1000 + i,
                    ..c.clone()
                })
            })
            .collect();
        let pick = pick_contracts(&dense, spot(), day(9, 22), 200).unwrap();
        assert!(pick.contracts.len() > QUOTE_BATCH_SIZE);
        assert!(pick.keys().is_err());
    }

    #[test]
    fn the_window_is_0915_to_1530_ist() {
        let at = |h, m, s| {
            day(9, 22)
                .and_hms_opt(h, m, s)
                .unwrap()
                .and_local_timezone(baskfy_providers::tokens::ist())
                .unwrap()
        };
        assert!(!in_session(at(9, 14, 59)));
        assert!(in_session(at(9, 15, 0)));
        assert!(in_session(at(15, 30, 0)));
        assert!(!in_session(at(15, 30, 1)));
        assert_eq!(minute_of(at(10, 0, 20)), at(10, 0, 0));
    }

    #[test]
    fn greeks_round_half_up_and_refuse_the_unstorable() {
        assert_eq!(greek(0.123_456_5).unwrap().to_string(), "0.123457");
        assert!(greek(-0.000_000_4).unwrap().is_zero());
        assert!(greek(f64::NAN).is_none());
        assert!(greek(10_000.0).is_none());
    }

    #[test]
    fn month_bounds_roll_the_year() {
        assert_eq!(
            month_bounds(day(12, 15)),
            (day(12, 1), NaiveDate::from_ymd_opt(2027, 1, 1).unwrap())
        );
        assert_eq!(month_bounds(day(10, 29)), (day(10, 1), day(11, 1)));
    }
}
