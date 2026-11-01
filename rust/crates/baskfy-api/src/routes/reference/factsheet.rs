//! `baskfy_api.instruments` — the factsheet, the typeahead and the instrument lookup — with
//! `baskfy_core.pros_cons` (the PROS/CONS rule table) beside it.
//!
//! Every block reads published rows; nothing is recomputed except docs/05 §15's two Wasserstein
//! distances, which [`super::regime`] reproduces from the same 400 bars Python reads.

use std::collections::HashMap;
use std::str::FromStr;

use baskfy_core::screener::Payload;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::postgres::PgRow;
use sqlx::{Column, Row, TypeInfo, ValueRef};

use super::universes::{UNIVERSES, Universe};
use super::{db, dbe, p_date, p_dec, p_dec_from_float, p_map, p_opt_date, p_opt_dec, p_str};
use crate::problems::Problem;
use crate::state::AppState;

/// `WINDOW_MONTHS`.
pub(crate) const WINDOW_MONTHS: [u32; 5] = [12, 9, 6, 3, 1];
/// `WINDOW_LABELS`.
pub(crate) const WINDOW_LABELS: [&str; 5] = ["1Y", "9M", "6M", "3M", "1M"];
/// `REGIME_HISTORY_BARS`.
const REGIME_HISTORY_BARS: i64 = 400;
/// `MIN_CLOSES_FOR_RETURNS`.
const MIN_CLOSES_FOR_RETURNS: usize = 2;

/// `RETURN_KEYS`.
fn return_keys() -> Vec<String> {
    let mut keys: Vec<String> = WINDOW_MONTHS.iter().map(|m| format!("ret_{m}m")).collect();
    keys.push("ret_12m_minus_1m".to_owned());
    keys.push("ret_12m_minus_2m".to_owned());
    keys
}

/// `RETURN_LABELS`.
const RETURN_LABELS: [&str; 7] = ["1Y", "9M", "6M", "3M", "1M", "12M-1M", "12M-2M"];

/// One cell of a fact row as Python holds it.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Cell {
    /// `None`.
    Null,
    /// `Decimal`.
    Dec(Decimal),
    /// `int`.
    Int(i64),
    /// `str`.
    Str(String),
    /// `bool`.
    Bool(bool),
    /// `date`.
    Date(NaiveDate),
}

impl Cell {
    fn is_null(&self) -> bool {
        *self == Self::Null
    }

    /// `_numeric(value)` as a payload: a `Decimal` or an `int`, anything else `None`.
    fn numeric(&self) -> Payload {
        match self {
            Self::Dec(d) => p_dec(*d),
            Self::Int(i) => Payload::Int(*i),
            _ => Payload::Null,
        }
    }

    /// `pros_cons._num`: a `Decimal` view, or `None`.
    fn num(&self) -> Option<Decimal> {
        match self {
            Self::Dec(d) => Some(*d),
            Self::Int(i) => Some(Decimal::from(*i)),
            Self::Str(s) => Decimal::from_str(s).ok(),
            _ => None,
        }
    }
}

/// A row, by column name.
pub(crate) type FactRow = HashMap<String, Cell>;

fn get<'a>(row: &'a FactRow, key: &str) -> &'a Cell {
    static NULL: Cell = Cell::Null;
    row.get(key).unwrap_or(&NULL)
}

/// Every column of a row, typed as asyncpg types it.
pub(crate) fn read_row(row: &PgRow) -> Result<FactRow, Problem> {
    let mut out = HashMap::new();
    for column in row.columns() {
        let name = column.name();
        let raw = row.try_get_raw(name).map_err(db)?;
        if raw.is_null() {
            out.insert(name.to_owned(), Cell::Null);
            continue;
        }
        let type_name = raw.type_info().name().to_owned();
        let cell = match type_name.as_str() {
            "NUMERIC" => Cell::Dec(baskfy_db::row::decimal(row, name).map_err(|e| dbe(&e))?),
            "INT2" => Cell::Int(i64::from(row.try_get::<i16, _>(name).map_err(db)?)),
            "INT4" => Cell::Int(i64::from(row.try_get::<i32, _>(name).map_err(db)?)),
            "INT8" => Cell::Int(row.try_get::<i64, _>(name).map_err(db)?),
            "BOOL" => Cell::Bool(row.try_get::<bool, _>(name).map_err(db)?),
            "DATE" => Cell::Date(baskfy_db::row::date(row, name).map_err(|e| dbe(&e))?),
            "TEXT" | "VARCHAR" | "BPCHAR" | "NAME" => {
                Cell::Str(row.try_get::<String, _>(name).map_err(db)?)
            }
            _ => Cell::Null,
        };
        out.insert(name.to_owned(), cell);
    }
    Ok(out)
}

// ---------------------------------------------------------------------------------------------
// pros_cons
// ---------------------------------------------------------------------------------------------

struct Rule {
    key: String,
    inputs: Vec<String>,
    pro: String,
    con: String,
    predicate: Box<dyn Fn(&FactRow) -> bool + Send + Sync>,
}

fn rules() -> Vec<Rule> {
    let mut out: Vec<Rule> = Vec::new();
    for length in [200, 100, 50, 20] {
        let ma = format!("ma_{length}");
        let ma_key = ma.clone();
        out.push(Rule {
            key: format!("close_above_ma_{length}"),
            inputs: vec!["close".to_owned(), ma.clone()],
            pro: format!("The close is above {length}-day moving average."),
            con: format!("The close is below {length}-day moving average."),
            predicate: Box::new(move |row| {
                match (get(row, "close").num(), get(row, &ma_key).num()) {
                    (Some(close), Some(ma)) => close > ma,
                    _ => false,
                }
            }),
        });
    }
    out.push(Rule {
        key: "within_25pct_of_ath".to_owned(),
        inputs: vec!["away_high_ath".to_owned()],
        pro: "The close is within 25% of all time high.".to_owned(),
        con: "The close is more than 25% below its all time high.".to_owned(),
        predicate: Box::new(|row| {
            get(row, "away_high_ath")
                .num()
                .is_some_and(|a| a.abs() <= Decimal::from(25))
        }),
    });
    out.push(Rule {
        key: "beta_below_ceiling".to_owned(),
        inputs: vec!["beta_12m".to_owned()],
        pro: "The beta is less than 1.25".to_owned(),
        con: "The beta is 1.25 or higher".to_owned(),
        predicate: Box::new(|row| {
            get(row, "beta_12m")
                .num()
                .is_some_and(|b| b < Decimal::new(125, 2))
        }),
    });
    out.push(Rule {
        key: "positive_days_above_floor".to_owned(),
        inputs: vec!["pos_days_12m".to_owned()],
        pro: "More than 55% of days in the last year closed positive.".to_owned(),
        con: "55% or fewer of days in the last year closed positive.".to_owned(),
        predicate: Box::new(|row| {
            get(row, "pos_days_12m")
                .num()
                .is_some_and(|v| v > Decimal::from(55))
        }),
    });
    out.push(Rule {
        key: "liquid_enough".to_owned(),
        inputs: vec!["median_vol_12m".to_owned()],
        pro: "Median daily turnover is above ₹1 crore.".to_owned(),
        con: "Median daily turnover is below ₹1 crore.".to_owned(),
        predicate: Box::new(|row| {
            get(row, "median_vol_12m")
                .num()
                .is_some_and(|v| v >= Decimal::from(10_000_000))
        }),
    });
    out
}

fn decided(row: &FactRow, rule: &Rule) -> bool {
    rule.inputs.iter().all(|c| !get(row, c).is_null())
}

/// `pros_cons.evaluate(row)` and `pros_cons.undecided(row)`.
pub(crate) fn pros_cons(row: &FactRow) -> (Vec<String>, Vec<String>, Vec<String>) {
    let (mut pros, mut cons, mut undecided) = (Vec::new(), Vec::new(), Vec::new());
    for rule in rules() {
        if !decided(row, &rule) {
            undecided.push(rule.key.clone());
            continue;
        }
        if (rule.predicate)(row) {
            pros.push(rule.pro.clone());
        } else {
            cons.push(rule.con.clone());
        }
    }
    (pros, cons, undecided)
}

// ---------------------------------------------------------------------------------------------
// Instruments
// ---------------------------------------------------------------------------------------------

/// The instrument columns the API reads.
#[derive(Debug, Clone)]
pub(crate) struct Instrument {
    /// `id`.
    pub id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `name`.
    pub name: String,
    /// `isin`.
    pub isin: Option<String>,
    /// `series`.
    pub series: Option<String>,
    /// `face_value`.
    pub face_value: Option<Decimal>,
    /// `listed_on`.
    pub listed_on: Option<NaiveDate>,
}

/// `load_instrument(session, symbol)`: by `symbol.upper()`; `None` is `InstrumentNotFound`.
pub(crate) async fn load_instrument(
    state: &AppState,
    symbol: &str,
) -> Result<Option<Instrument>, Problem> {
    let rows = sqlx::query(
        "SELECT id, symbol, name, isin, series, face_value, listed_on FROM instrument \
         WHERE symbol = $1",
    )
    .bind(super::py_upper(symbol))
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    if rows.len() > 1 {
        return Err(super::unexpected("MultipleResultsFound: instrument"));
    }
    let Some(r) = rows.first() else {
        return Ok(None);
    };
    Ok(Some(Instrument {
        id: r.try_get("id").map_err(db)?,
        symbol: r.try_get("symbol").map_err(db)?,
        name: r.try_get("name").map_err(db)?,
        isin: r.try_get("isin").map_err(db)?,
        series: r.try_get("series").map_err(db)?,
        face_value: baskfy_db::row::opt_decimal(r, "face_value").map_err(|e| dbe(&e))?,
        listed_on: baskfy_db::row::opt_date(r, "listed_on").map_err(|e| dbe(&e))?,
    }))
}

/// One typeahead hit.
#[derive(Debug, Clone)]
pub(crate) struct Hit {
    /// `symbol`.
    pub symbol: String,
    /// `name`.
    pub name: String,
    /// `series`.
    pub series: Option<String>,
}

/// `search_instruments(session, query, limit)`: exact symbol, then prefix, then the rest.
pub(crate) async fn search_instruments(
    state: &AppState,
    query: &str,
    limit: i64,
) -> Result<Vec<Hit>, Problem> {
    let needle = super::py_upper(super::py_strip(query));
    if needle.is_empty() {
        return Ok(Vec::new());
    }
    let rows = sqlx::query(
        "SELECT symbol, name, series FROM instrument \
         WHERE is_active IS true AND delisted_on IS NULL \
           AND (symbol ILIKE $1 OR name ILIKE $1) \
         ORDER BY CASE WHEN symbol = $2 THEN 0 WHEN symbol ILIKE $3 THEN 1 ELSE 2 END ASC, \
                  symbol ASC LIMIT $4",
    )
    .bind(format!("%{needle}%"))
    .bind(&needle)
    .bind(format!("{needle}%"))
    .bind(limit)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    rows.iter()
        .map(|r| {
            Ok(Hit {
                symbol: r.try_get("symbol").map_err(db)?,
                name: r.try_get("name").map_err(db)?,
                series: r.try_get("series").map_err(db)?,
            })
        })
        .collect()
}

/// `_memberships`: point-in-time index membership, in `UNIVERSES` order.
async fn memberships(
    state: &AppState,
    instrument_id: i64,
    as_of: NaiveDate,
) -> Result<Vec<Universe>, Problem> {
    let ids: Vec<i16> = sqlx::query_scalar(
        "SELECT index_id FROM index_member_daily WHERE instrument_id = $1 AND date = $2",
    )
    .bind(instrument_id)
    .bind(as_of)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    Ok(UNIVERSES
        .iter()
        .copied()
        .filter(|u| ids.iter().any(|i| i64::from(*i) == u.index_id))
        .collect())
}

/// `primary_universe`: the narrowest populated membership (size, then id), skipping the two
/// classifications.
async fn primary_universe(
    state: &AppState,
    members: &[Universe],
    as_of: NaiveDate,
) -> Result<Option<Universe>, Problem> {
    let mut best: Option<(i64, Universe)> = None;
    for u in members
        .iter()
        .filter(|u| u.slug != "etf" && u.slug != "nifty-fno")
    {
        let size: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM index_member_daily WHERE index_id = $1 AND date = $2",
        )
        .bind(i16::try_from(u.index_id).unwrap_or(i16::MAX))
        .bind(as_of)
        .fetch_one(state.db.pool())
        .await
        .map_err(db)?;
        if size <= 0 {
            continue;
        }
        if best.is_none_or(|(s, b)| (size, u.index_id) < (s, b.index_id)) {
            best = Some((size, *u));
        }
    }
    Ok(best.map(|(_, u)| u))
}

/// `_percentiles`: each column's `percent_rank()` within the universe on `as_of`.
async fn percentiles(
    state: &AppState,
    universe: Option<Universe>,
    as_of: NaiveDate,
    instrument_id: i64,
    columns: &[String],
) -> Result<HashMap<String, f64>, Problem> {
    let Some(universe) = universe else {
        return Ok(HashMap::new());
    };
    if columns.is_empty() {
        return Ok(HashMap::new());
    }
    let ranks: Vec<String> = columns
        .iter()
        .map(|c| format!("percent_rank() OVER (ORDER BY f.{c} ASC NULLS LAST) AS p_{c}"))
        .collect();
    let values: Vec<String> = columns.iter().map(|c| format!("f.{c} AS v_{c}")).collect();
    let sql = format!(
        "WITH universe_members AS (SELECT instrument_id FROM index_member_daily \
           WHERE index_id = $1 AND date = $2), \
         ranked AS (SELECT f.instrument_id, {}, {} FROM factor_daily f \
           JOIN universe_members ON f.instrument_id = universe_members.instrument_id \
           WHERE f.date = $3) \
         SELECT * FROM ranked WHERE instrument_id = $4",
        ranks.join(", "),
        values.join(", ")
    );
    let rows = sqlx::query(&sql)
        .bind(i16::try_from(universe.index_id).unwrap_or(i16::MAX))
        .bind(as_of)
        .bind(as_of)
        .bind(instrument_id)
        .fetch_all(state.db.pool())
        .await
        .map_err(db)?;
    if rows.len() > 1 {
        return Err(super::unexpected("MultipleResultsFound: ranked"));
    }
    let Some(row) = rows.first() else {
        return Ok(HashMap::new());
    };
    let mut out = HashMap::new();
    for c in columns {
        let v = row.try_get_raw(format!("v_{c}").as_str()).map_err(db)?;
        if v.is_null() {
            continue;
        }
        // SQLAlchemy types `percent_rank()` as `Numeric()`: asyncpg's float8 goes through
        // `Decimal("%.10f" % value)` before the router's `float(...)`.
        let p: Option<f64> = row.try_get(format!("p_{c}").as_str()).map_err(db)?;
        if let Some(p) = p {
            out.insert(c.clone(), format!("{p:.10}").parse::<f64>().unwrap_or(p));
        }
    }
    Ok(out)
}

/// `_median_from_facts` / `_median_close`: `percentile_cont(0.5)` and the observation count.
async fn median(
    state: &AppState,
    table: &str,
    column: &str,
    instrument_id: i64,
    as_of: NaiveDate,
) -> Result<(Option<f64>, i64), Problem> {
    let sql = format!(
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY {column} ASC) AS m, \
         count({column}) AS n FROM {table} WHERE instrument_id = $1 AND date <= $2"
    );
    let row = sqlx::query(&sql)
        .bind(instrument_id)
        .bind(as_of)
        .fetch_one(state.db.pool())
        .await
        .map_err(db)?;
    Ok((row.try_get("m").map_err(db)?, row.try_get("n").map_err(db)?))
}

/// `_regime`: the label and both distances from the last 400 closes.
async fn regime(
    state: &AppState,
    instrument_id: i64,
    as_of: NaiveDate,
) -> Result<(Option<&'static str>, Option<f64>, Option<f64>), Problem> {
    let rows = sqlx::query(
        "SELECT close FROM ohlcv_daily WHERE instrument_id = $1 AND date <= $2 \
         ORDER BY date DESC LIMIT $3",
    )
    .bind(instrument_id)
    .bind(as_of)
    .bind(REGIME_HISTORY_BARS)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    if rows.len() < MIN_CLOSES_FOR_RETURNS {
        return Ok((None, None, None));
    }
    let mut prices = Vec::with_capacity(rows.len());
    for r in rows.iter().rev() {
        prices.push(super::dec_to_f64(
            baskfy_db::row::decimal(r, "close").map_err(|e| dbe(&e))?,
        ));
    }
    let returns: Vec<f64> = prices.windows(2).map(|w| (w[1] - w[0]) / w[0]).collect();
    let (label, to_bull, to_bear) = super::regime::classify_series(&returns);
    let keep = |x: f64| if x.is_nan() { None } else { Some(x) };
    Ok((Some(label), keep(to_bull), keep(to_bear)))
}

fn cell(key: &str, label: &str, value: Payload, percentile: Option<f64>) -> Payload {
    p_map(vec![
        ("key", p_str(key)),
        ("label", p_str(label)),
        ("value", value),
        (
            "percentile",
            percentile.map_or(Payload::Null, Payload::Float),
        ),
    ])
}

fn cells(row: &FactRow, keys: &[String], labels: &[&str], pct: &HashMap<String, f64>) -> Payload {
    Payload::List(
        keys.iter()
            .zip(labels)
            .map(|(k, l)| cell(k, l, get(row, k).numeric(), pct.get(k).copied()))
            .collect(),
    )
}

fn membership(u: Universe) -> Payload {
    p_map(vec![("slug", p_str(u.slug)), ("name", p_str(u.name))])
}

/// `build_factsheet(session, symbol, as_of, data_version)` as the `FactsheetOut` payload;
/// `None` is `InstrumentNotFound`.
pub(crate) async fn build_factsheet(
    state: &AppState,
    symbol: &str,
    as_of: NaiveDate,
    data_version: i64,
) -> Result<Option<Payload>, Problem> {
    let Some(instrument) = load_instrument(state, symbol).await? else {
        return Ok(None);
    };
    let fact = sqlx::query(
        "SELECT * FROM factor_daily WHERE instrument_id = $1 AND date <= $2 \
         ORDER BY date DESC LIMIT 1",
    )
    .bind(instrument.id)
    .bind(as_of)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let bar = sqlx::query(
        "SELECT close, close_raw FROM ohlcv_daily WHERE instrument_id = $1 AND date <= $2 \
         ORDER BY date DESC LIMIT 1",
    )
    .bind(instrument.id)
    .bind(as_of)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let members = memberships(state, instrument.id, as_of).await?;
    let universe = primary_universe(state, &members, as_of).await?;

    let mut notes: Vec<Payload> = Vec::new();
    let row: FactRow = match &fact {
        None => {
            notes.push(p_str(
                "No factor row has been published for this instrument yet.",
            ));
            HashMap::new()
        }
        Some(r) => {
            let row = read_row(r)?;
            if let Cell::Date(d) = get(&row, "date")
                && *d != as_of
            {
                notes.push(Payload::Str(format!(
                    "No row for {}; showing {}, the most recent one.",
                    crate::wire::py_date(as_of),
                    crate::wire::py_date(*d)
                )));
            }
            row
        }
    };

    let mut percentile_columns = return_keys();
    for prefix in ["sharpe", "vol", "rsi"] {
        percentile_columns.extend(WINDOW_MONTHS.iter().map(|m| format!("{prefix}_{m}m")));
    }
    let pct = percentiles(state, universe, as_of, instrument.id, &percentile_columns).await?;

    let (pros, cons, undecided) = pros_cons(&row);

    let mut cards = Vec::new();
    // `percentile_cont` returns float8, and SQLAlchemy types it as the ordered column: a
    // `Numeric(p, s)` column's result goes through `Decimal("%.{s}f" % value)`, an integer
    // column's stays a float (then `Decimal(str(value))`). The scale is each column's own.
    for (key, label, table, scale) in [
        ("close", "Closing Price", "ohlcv_daily", Some(4_usize)),
        (
            "ret_12m",
            "Rolling 1-Yr Returns (%)",
            "factor_daily",
            Some(2),
        ),
        ("pe", "Price to Earnings", "factor_daily", Some(4)),
        ("marketcap_cr", "Marketcap", "factor_daily", None),
        ("rsi_12m", "1-Year RSI", "factor_daily", Some(4)),
    ] {
        let (m, observations) = median(state, table, key, instrument.id, as_of).await?;
        let median_payload = match (m, scale) {
            (None, _) => Payload::Null,
            (Some(x), None) => p_dec_from_float(x),
            (Some(x), Some(s)) if x.is_finite() => Decimal::from_str(&format!("{x:.s$}"))
                .map_or_else(|_| Payload::Opaque("Decimal".to_owned()), p_dec),
            (Some(_), Some(_)) => Payload::Opaque("Decimal".to_owned()),
        };
        cards.push(p_map(vec![
            ("key", p_str(key)),
            ("label", p_str(label)),
            ("value", get(&row, key).numeric()),
            ("median", median_payload),
            ("observations", Payload::Int(observations)),
        ]));
    }

    let (computed, to_bull, to_bear) = regime(state, instrument.id, as_of).await?;
    let regime_label = match get(&row, "regime") {
        Cell::Str(s) => Payload::Str(s.clone()),
        _ => computed.map_or(Payload::Null, p_str),
    };

    let actions = sqlx::query(
        "SELECT action_type, ex_date, ratio_from, ratio_to, amount FROM corporate_action \
         WHERE instrument_id = $1 ORDER BY ex_date DESC",
    )
    .bind(instrument.id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let actions = corporate_actions(&actions)?;

    let (close_raw, close) = match &bar {
        Some(b) => (
            p_dec(baskfy_db::row::decimal(b, "close_raw").map_err(|e| dbe(&e))?),
            p_dec(baskfy_db::row::decimal(b, "close").map_err(|e| dbe(&e))?),
        ),
        None => (get(&row, "close").numeric(), get(&row, "close").numeric()),
    };

    let window_keys = |prefix: &str| -> Vec<String> {
        WINDOW_MONTHS
            .iter()
            .map(|m| format!("{prefix}_{m}m"))
            .collect()
    };
    let labelled = |prefix: &str| -> Payload {
        Payload::List(
            WINDOW_MONTHS
                .iter()
                .zip(WINDOW_LABELS)
                .map(|(m, label)| {
                    p_map(vec![
                        ("label", p_str(label)),
                        ("value", get(&row, &format!("{prefix}_{m}m")).numeric()),
                    ])
                })
                .collect(),
        )
    };
    let fixed = |key: &str, label: &str, value: Payload| cell(key, label, value, None);
    let payload = p_map(vec![
        ("symbol", p_str(&instrument.symbol)),
        ("as_of", p_date(as_of)),
        ("data_version", Payload::Int(data_version)),
        (
            "header",
            p_map(vec![
                ("symbol", p_str(&instrument.symbol)),
                ("name", p_str(&instrument.name)),
                ("exchange", p_str("NSE")),
                ("close_raw", close_raw),
                ("close", close),
                ("isin", super::p_opt_str(instrument.isin.as_deref())),
            ]),
        ),
        (
            "key_stats",
            Payload::List(vec![
                fixed("pe", "P/E", get(&row, "pe").numeric()),
                fixed(
                    "marketcap_cr",
                    "Marketcap (cr)",
                    get(&row, "marketcap_cr").numeric(),
                ),
                fixed("beta_12m", "Beta", get(&row, "beta_12m").numeric()),
                fixed(
                    "series",
                    "Series",
                    super::p_opt_str(instrument.series.as_deref()),
                ),
                fixed("listed_on", "Listed On", p_opt_date(instrument.listed_on)),
            ]),
        ),
        (
            "pros",
            Payload::List(pros.into_iter().map(Payload::Str).collect()),
        ),
        (
            "cons",
            Payload::List(cons.into_iter().map(Payload::Str).collect()),
        ),
        (
            "undecided",
            Payload::List(undecided.into_iter().map(Payload::Str).collect()),
        ),
        ("metric_cards", Payload::List(cards)),
        (
            "price_and_mas",
            Payload::List(vec![
                fixed("face_value", "Face Value", p_opt_dec(instrument.face_value)),
                fixed("high_1y", "1Y High", get(&row, "high_1y").numeric()),
                fixed(
                    "away_high_1y",
                    "Away from 1Y High",
                    get(&row, "away_high_1y").numeric(),
                ),
                fixed("high_ath", "ATH", get(&row, "high_ath").numeric()),
                fixed(
                    "away_high_ath",
                    "Away from ATH",
                    get(&row, "away_high_ath").numeric(),
                ),
                fixed("ma_200", "MA 200", get(&row, "ma_200").numeric()),
                fixed("ma_100", "MA 100", get(&row, "ma_100").numeric()),
                fixed("ma_50", "MA 50", get(&row, "ma_50").numeric()),
                fixed("ma_20", "MA 20", get(&row, "ma_20").numeric()),
            ]),
        ),
        ("returns", cells(&row, &return_keys(), &RETURN_LABELS, &pct)),
        (
            "sharpe_returns",
            cells(&row, &window_keys("sharpe"), &WINDOW_LABELS, &pct),
        ),
        (
            "volatility",
            cells(&row, &window_keys("vol"), &WINDOW_LABELS, &pct),
        ),
        (
            "rsi",
            cells(&row, &window_keys("rsi"), &WINDOW_LABELS, &pct),
        ),
        (
            "market_quality",
            p_map(vec![
                ("regime", regime_label),
                (
                    "regime_distance_bull",
                    to_bull.map_or(Payload::Null, Payload::Float),
                ),
                (
                    "regime_distance_bear",
                    to_bear.map_or(Payload::Null, Payload::Float),
                ),
                ("median_vol_12m", get(&row, "median_vol_12m").numeric()),
                ("circuits", labelled("circuits")),
                ("positive_days", labelled("pos_days")),
            ]),
        ),
        ("corporate_actions", actions),
        (
            "index_memberships",
            Payload::List(members.iter().copied().map(membership).collect()),
        ),
        (
            "percentile_universe",
            universe.map_or(Payload::Null, membership),
        ),
        ("notes", Payload::List(notes)),
    ]);
    Ok(Some(payload))
}

/// `CorporateActionOut` rows (newest first, as queried).
pub(crate) fn corporate_actions(rows: &[PgRow]) -> Result<Payload, Problem> {
    let mut out = Vec::with_capacity(rows.len());
    for r in rows {
        let dec = |c: &str| baskfy_db::row::opt_decimal(r, c).map_err(|e| dbe(&e));
        out.push(p_map(vec![
            (
                "action_type",
                Payload::Str(r.try_get("action_type").map_err(db)?),
            ),
            (
                "ex_date",
                p_date(baskfy_db::row::date(r, "ex_date").map_err(|e| dbe(&e))?),
            ),
            ("ratio_from", p_opt_dec(dec("ratio_from")?)),
            ("ratio_to", p_opt_dec(dec("ratio_to")?)),
            ("amount", p_opt_dec(dec("amount")?)),
        ]));
    }
    Ok(Payload::List(out))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_null_input_decides_nothing() {
        let (pros, cons, undecided) = pros_cons(&HashMap::new());
        assert!(pros.is_empty() && cons.is_empty());
        assert_eq!(undecided.len(), 8);
        assert_eq!(undecided[0], "close_above_ma_200");
    }

    #[test]
    fn the_rules_read_the_row() {
        let mut row = HashMap::new();
        row.insert("close".to_owned(), Cell::Dec(Decimal::new(110, 0)));
        row.insert("ma_200".to_owned(), Cell::Dec(Decimal::new(100, 0)));
        row.insert("beta_12m".to_owned(), Cell::Dec(Decimal::new(125, 2)));
        row.insert("median_vol_12m".to_owned(), Cell::Int(10_000_000));
        let (pros, cons, _) = pros_cons(&row);
        assert_eq!(
            pros,
            vec![
                "The close is above 200-day moving average.",
                "Median daily turnover is above ₹1 crore."
            ]
        );
        assert_eq!(cons, vec!["The beta is 1.25 or higher"]);
    }
}
