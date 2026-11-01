//! Ports `baskfy_worker.fno.retest`: the quarterly re-test's I/O (`docs/fno/04` §6, FO9) —
//! `fo_contract_daily` in, `fo_backtest_run` out (append-only, one row per family per FO tenant).
//!
//! The arithmetic is `baskfy_core::fno::retest` (pure, synchronous). As in Python, it runs on
//! its own thread and its loaders call back into this task for each batch of option rows, so
//! the process never holds the whole option panel (DECISIONS-FO FO9.3): the compute thread sends
//! a request over a channel and blocks on the answer; this side reads the database and replies.
//! Reads no network; moves no flag or plan.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::fno::config::FnoError;
use baskfy_core::fno::research;
use baskfy_core::fno::retest::{
    self as rt, DIRECTIONAL_EXPIRY_DAYS, FamilyResult, RETEST_MONTHS, RunKnobs,
};
use baskfy_core::fno::series::cols;
use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDate, Utc};
use polars::prelude::{Column, DataFrame, DataType, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::{dec_to_f64, fno_err, opt_float};
use super::scan::scan_users;
use super::spreads::{MIN_SPREAD_SESSIONS, SpreadObservation, half_spread_stats};
use crate::error::WorkerError;
use crate::pyjson::{object, s};

/// `SAMPLE_FROM`: the re-test starts where the research did.
pub(crate) fn sample_from() -> NaiveDate {
    NaiveDate::from_ymd_opt(2022, 1, 3).unwrap_or_default()
}

/// `BOX_SYMBOL_BATCH`.
pub(crate) const BOX_SYMBOL_BATCH: i64 = 10;

fn polars_err(e: &polars::error::PolarsError) -> WorkerError {
    WorkerError::python("ComputeError", e.to_string())
}

fn floats(name: &str, values: Vec<Option<Decimal>>) -> Column {
    cols::float_column(
        name,
        &values
            .into_iter()
            .map(|v| v.map(dec_to_f64))
            .collect::<Vec<_>>(),
    )
}

type FutureDbRow = (
    NaiveDate,
    String,
    String,
    NaiveDate,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<i64>,
    Option<i64>,
    Option<i64>,
    Option<Decimal>,
    Option<i32>,
);

/// `load_futures`: the futures rows whole, as the research's panel frame.
pub(crate) async fn load_futures(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<DataFrame, WorkerError> {
    let rows: Vec<FutureDbRow> = sqlx::query_as(
        "SELECT trade_date, instrument, symbol, expiry, open, high, low, close, settle, \
         underlying, open_interest, oi_change, volume, turnover, lot_size FROM fo_contract_daily \
         WHERE instrument = ANY($1) AND trade_date >= $2 AND trade_date <= $3",
    )
    .bind(vec!["FUTSTK".to_owned(), "FUTIDX".to_owned()])
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    let n = rows.len();
    let dec = |f: fn(&FutureDbRow) -> Option<Decimal>| rows.iter().map(f).collect::<Vec<_>>();
    let int = |f: fn(&FutureDbRow) -> Option<i64>| rows.iter().map(f).collect::<Vec<_>>();
    DataFrame::new(
        n,
        vec![
            cols::date_column("date", &rows.iter().map(|r| Some(r.0)).collect::<Vec<_>>())
                .map_err(fno_err)?,
            Series::new(
                "instrument".into(),
                rows.iter().map(|r| r.1.clone()).collect::<Vec<_>>(),
            )
            .into_column(),
            Series::new(
                "symbol".into(),
                rows.iter().map(|r| r.2.clone()).collect::<Vec<_>>(),
            )
            .into_column(),
            cols::date_column(
                "expiry",
                &rows.iter().map(|r| Some(r.3)).collect::<Vec<_>>(),
            )
            .map_err(fno_err)?,
            floats("open", dec(|r| r.4)),
            floats("high", dec(|r| r.5)),
            floats("low", dec(|r| r.6)),
            floats("close", dec(|r| r.7)),
            floats("settle", dec(|r| r.8)),
            floats("underlying", dec(|r| r.9)),
            cols::int_column("open_interest", &int(|r| r.10)),
            cols::int_column("oi_change", &int(|r| r.11)),
            cols::int_column("volume", &int(|r| r.12)),
            floats("turnover", dec(|r| r.13)),
            cols::int_column("lot_size", &int(|r| r.14.map(i64::from))),
        ],
    )
    .map_err(|e| polars_err(&e))
}

type OptionDbRow = (
    NaiveDate,
    String,
    String,
    NaiveDate,
    Decimal,
    String,
    Option<Decimal>,
    Option<Decimal>,
    Option<i64>,
    Option<i64>,
);

fn option_frame(rows: &[OptionDbRow]) -> Result<DataFrame, WorkerError> {
    DataFrame::new(
        rows.len(),
        vec![
            cols::date_column("date", &rows.iter().map(|r| Some(r.0)).collect::<Vec<_>>())
                .map_err(fno_err)?,
            Series::new(
                "instrument".into(),
                rows.iter().map(|r| r.1.clone()).collect::<Vec<_>>(),
            )
            .into_column(),
            Series::new(
                "symbol".into(),
                rows.iter().map(|r| r.2.clone()).collect::<Vec<_>>(),
            )
            .into_column(),
            cols::date_column(
                "expiry",
                &rows.iter().map(|r| Some(r.3)).collect::<Vec<_>>(),
            )
            .map_err(fno_err)?,
            floats("strike", rows.iter().map(|r| Some(r.4)).collect()),
            Series::new(
                "option_type".into(),
                rows.iter().map(|r| r.5.clone()).collect::<Vec<_>>(),
            )
            .into_column(),
            floats("close", rows.iter().map(|r| r.6).collect()),
            floats("settle", rows.iter().map(|r| r.7).collect()),
            cols::int_column(
                "open_interest",
                &rows.iter().map(|r| r.8).collect::<Vec<_>>(),
            ),
            cols::int_column("volume", &rows.iter().map(|r| r.9).collect::<Vec<_>>()),
        ],
    )
    .map_err(|e| polars_err(&e))
}

/// `pl.concat([a, b], how="diagonal_relaxed")` for frames whose shared columns already agree
/// on dtype: the union of columns in first-seen order, a missing column null.
pub(crate) fn concat_diagonal(a: &DataFrame, b: &DataFrame) -> Result<DataFrame, WorkerError> {
    let mut names: Vec<(String, DataType)> = Vec::new();
    for frame in [a, b] {
        for c in frame.columns() {
            if !names.iter().any(|(n, _)| n.as_str() == c.name().as_str()) {
                names.push((c.name().to_string(), c.dtype().clone()));
            }
        }
    }
    let widen = |frame: &DataFrame| -> Result<DataFrame, WorkerError> {
        let mut cols_out = Vec::with_capacity(names.len());
        for (name, dtype) in &names {
            match frame.column(name) {
                Ok(c) => cols_out.push(c.cast(dtype).map_err(|e| polars_err(&e))?),
                Err(_) => cols_out.push(Column::full_null(
                    name.as_str().into(),
                    frame.height(),
                    dtype,
                )),
            }
        }
        DataFrame::new(frame.height(), cols_out).map_err(|e| polars_err(&e))
    };
    let mut out = widen(a)?;
    out.vstack_mut(&widen(b)?).map_err(|e| polars_err(&e))?;
    Ok(out)
}

fn filter_symbols(
    frame: &DataFrame,
    keep: impl Fn(&str, &str) -> bool,
) -> Result<DataFrame, WorkerError> {
    let sym = cols::strings(frame, "symbol").map_err(fno_err)?;
    let ins = cols::strings(frame, "instrument").map_err(fno_err)?;
    let mask: Vec<bool> = sym
        .iter()
        .zip(&ins)
        .map(|(s, i)| keep(s.as_deref().unwrap_or(""), i.as_deref().unwrap_or("")))
        .collect();
    frame
        .filter(&polars::prelude::BooleanChunked::new("".into(), mask))
        .map_err(|e| polars_err(&e))
}

const OPTION_COLUMNS: &str = "trade_date, instrument, symbol, expiry, strike, option_type, \
     close, settle, open_interest, volume";

/// `load_options`: the pure filter applied to what the table has for `symbols`.
pub(crate) async fn load_options(
    conn: &mut PgConnection,
    futures: &DataFrame,
    symbols: &BTreeSet<String>,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<DataFrame, WorkerError> {
    let wanted: Vec<String> = symbols.iter().cloned().collect();
    let rows: Vec<OptionDbRow> = sqlx::query_as(&format!(
        "SELECT {OPTION_COLUMNS} FROM fo_contract_daily WHERE instrument = ANY($1) \
         AND symbol = ANY($2) AND trade_date >= $3 AND trade_date <= $4"
    ))
    .bind(vec!["OPTSTK".to_owned(), "OPTIDX".to_owned()])
    .bind(&wanted)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    if rows.is_empty() {
        return rt::empty_options().map_err(fno_err);
    }
    let options = option_frame(&rows)?;
    let mine = filter_symbols(futures, |s, _| symbols.contains(s))?;
    let both = concat_diagonal(&mine, &options)?;
    Ok(rt::panels_from_contracts(&both).map_err(fno_err)?.options)
}

/// `load_index_rows`: `symbol`'s FUTIDX rows and its OPTIDX rows of every listed expiry within
/// `DIRECTIONAL_EXPIRY_DAYS` of the session.
pub(crate) async fn load_index_rows(
    conn: &mut PgConnection,
    futures: &DataFrame,
    symbol: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<DataFrame, WorkerError> {
    let rows: Vec<OptionDbRow> = sqlx::query_as(&format!(
        "SELECT {OPTION_COLUMNS} FROM fo_contract_daily WHERE instrument = 'OPTIDX' \
         AND symbol = $1 AND trade_date >= $2 AND trade_date <= $3 \
         AND expiry <= trade_date + $4::int"
    ))
    .bind(symbol)
    .bind(start)
    .bind(end)
    .bind(i32::try_from(DIRECTIONAL_EXPIRY_DAYS).unwrap_or(i32::MAX))
    .fetch_all(&mut *conn)
    .await?;
    let mine = filter_symbols(futures, |s, i| s == symbol && i == "FUTIDX")?;
    if rows.is_empty() {
        return Ok(mine);
    }
    concat_diagonal(&mine, &option_frame(&rows)?)
}

/// `measured_spreads`: FO3's statistic per underlying, `(median half-spread ÷ mid, sessions)`.
pub(crate) async fn measured_spreads(
    conn: &mut PgConnection,
) -> Result<BTreeMap<String, (f64, i64)>, WorkerError> {
    type Row = (
        NaiveDate,
        String,
        NaiveDate,
        Decimal,
        String,
        Option<Decimal>,
        Option<Decimal>,
        DateTime<Utc>,
    );
    let rows: Vec<Row> = sqlx::query_as(
        "SELECT trade_date, symbol, expiry, strike, option_type, bid, ask, taken_at \
         FROM fo_spread_sample",
    )
    .fetch_all(&mut *conn)
    .await?;
    let observations = rows
        .into_iter()
        .map(|r| SpreadObservation {
            trade_date: r.0,
            symbol: r.1,
            expiry: r.2,
            strike: r.3,
            option_type: r.4,
            bid: r.5,
            ask: r.6,
            taken_at: r.7,
        })
        .collect();
    Ok(half_spread_stats(observations, MIN_SPREAD_SESSIONS)?
        .into_iter()
        .map(|(k, v)| {
            (
                k,
                (
                    v.median_half_spread_pct_of_mid
                        .to_string()
                        .parse::<f64>()
                        .unwrap_or(f64::NAN),
                    i64::try_from(v.sessions).unwrap_or(i64::MAX),
                ),
            )
        })
        .collect())
}

/// `last_run`: the UTC date of the latest `fo_backtest_run`.
pub(crate) async fn last_run(conn: &mut PgConnection) -> Result<Option<NaiveDate>, WorkerError> {
    let row: (Option<DateTime<Utc>>,) = sqlx::query_as("SELECT max(run_at) FROM fo_backtest_run")
        .fetch_one(&mut *conn)
        .await?;
    Ok(row.0.map(|t| t.date_naive()))
}

/// `repr(list_of_str)`.
fn py_list(items: &[String]) -> String {
    let inner: Vec<String> = items
        .iter()
        .map(|i| baskfy_providers::py::repr_str(i))
        .collect();
    format!("[{}]", inner.join(", "))
}

enum Request {
    Options(BTreeSet<String>),
    Index(String),
}

type Reply = std::sync::mpsc::Sender<Result<DataFrame, FnoError>>;

fn json_text<T: serde::Serialize>(value: &T) -> Result<String, WorkerError> {
    serde_json::to_string(value).map_err(|e| WorkerError::python("ValueError", e.to_string()))
}

/// `run_retest`: the families (all by default), one row per family per tenant.
#[allow(clippy::too_many_lines)]
pub(crate) async fn run_retest(
    conn: &mut PgConnection,
    today: NaiveDate,
    force: bool,
    families: Option<Vec<String>>,
) -> Result<OrderedJson, WorkerError> {
    if !force && !rt::retest_due(today, last_run(conn).await?) {
        let months: Vec<String> = RETEST_MONTHS.iter().map(ToString::to_string).collect();
        return Ok(object([(
            "skipped",
            s(format!(
                "not due: the re-test runs once in each of [{}]",
                months.join(", ")
            )),
        )]));
    }
    let wanted = families.filter(|f| !f.is_empty());
    let known = rt::family_keys();
    if let Some(asked) = &wanted {
        let unknown: Vec<String> = asked
            .iter()
            .filter(|f| !known.contains(*f))
            .cloned()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        if !unknown.is_empty() {
            let known: Vec<String> = known.iter().cloned().collect();
            return Ok(object([(
                "refused",
                s(format!(
                    "unknown families {}; known: {}",
                    py_list(&unknown),
                    py_list(&known)
                )),
            )]));
        }
    }
    let users = scan_users(conn).await?;
    if users.is_empty() {
        return Ok(object([(
            "skipped",
            s("no user has fo_sleeve_config rows (run fno_cli seed)"),
        )]));
    }
    let stop = today;
    let start = sample_from();
    let futures = load_futures(conn, start, stop).await?;
    if futures.height() == 0 {
        return Ok(object([(
            "skipped",
            s("fo_contract_daily holds no futures rows (run the backfill)"),
        )]));
    }
    let measured = measured_spreads(conn).await?;

    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<(Request, Reply)>();
    let thread_futures = futures.clone();
    let compute = std::thread::spawn(move || -> Result<Vec<FamilyResult>, FnoError> {
        let ask = |req: Request| -> Result<DataFrame, FnoError> {
            let (reply, answer) = std::sync::mpsc::channel();
            tx.send((req, reply))
                .map_err(|_| FnoError::Value("the re-test's loader is gone".to_owned()))?;
            answer
                .recv()
                .map_err(|_| FnoError::Value("the re-test's loader did not answer".to_owned()))?
        };
        let loader = |symbols: &BTreeSet<String>| ask(Request::Options(symbols.clone()));
        let index_loader = |symbol: &str| ask(Request::Index(symbol.to_owned()));
        let chosen: Vec<rt::Family> = rt::families()
            .into_iter()
            .filter(|f| wanted.as_ref().is_none_or(|w| w.contains(&f.key)))
            .collect();
        let cont = research::continuous(&thread_futures)?;
        chosen
            .iter()
            .map(|f| {
                rt::run_family(
                    f,
                    &thread_futures,
                    &loader,
                    &measured,
                    RunKnobs {
                        cont: Some(&cont),
                        batch: BOX_SYMBOL_BATCH,
                        load_index: Some(&index_loader),
                    },
                )
            })
            .collect()
    });
    let mut loader_error: Option<WorkerError> = None;
    while let Some((request, reply)) = rx.recv().await {
        let answer = match request {
            Request::Options(symbols) => load_options(conn, &futures, &symbols, start, stop).await,
            Request::Index(symbol) => load_index_rows(conn, &futures, &symbol, start, stop).await,
        };
        let sent = match answer {
            Ok(frame) => Ok(frame),
            Err(e) => {
                let message = e.to_string();
                loader_error.get_or_insert(e);
                Err(FnoError::Value(message))
            }
        };
        let _ = reply.send(sent);
    }
    let joined = compute.join().map_err(|_| {
        WorkerError::python("RuntimeError", "the re-test's compute thread panicked")
    })?;
    if let Some(e) = loader_error {
        return Err(e);
    }
    let results = joined.map_err(fno_err)?;

    for user_id in &users {
        for res in &results {
            sqlx::query(
                "INSERT INTO fo_backtest_run (user_id, family, params, tier, caveat, sample_from, \
                 sample_to, n, net_r, gross_r, per_year, slippage_source) \
                 VALUES ($1, $2, $3::jsonb, '2E', $4, $5, $6, $7, $8::numeric, $9::numeric, \
                 $10::jsonb, $11)",
            )
            .bind(user_id)
            .bind(&res.family)
            .bind(json_text(&res.params)?)
            .bind(&res.caveat)
            .bind(res.sample_from)
            .bind(res.sample_to)
            .bind(i32::try_from(res.n).unwrap_or(i32::MAX))
            .bind(res.net_r.map(baskfy_core::py::float_repr))
            .bind(res.gross_r.map(baskfy_core::py::float_repr))
            .bind(json_text(&res.per_year)?)
            .bind(slippage(res))
            .execute(&mut *conn)
            .await?;
        }
    }
    let n_users = i64::try_from(users.len()).unwrap_or(i64::MAX);
    let n_results = i64::try_from(results.len()).unwrap_or(i64::MAX);
    Ok(object([
        ("users", OrderedJson::Int(n_users)),
        ("rows", OrderedJson::Int(n_users * n_results)),
        (
            "families",
            object(results.iter().map(|res| {
                (
                    res.family.clone(),
                    object([
                        ("n", OrderedJson::Int(res.n)),
                        ("net_r", opt_float(res.net_r)),
                        ("gross_r", opt_float(res.gross_r)),
                        ("slippage", s(slippage(res))),
                    ]),
                )
            })),
        ),
    ]))
}

fn slippage(res: &FamilyResult) -> &'static str {
    match res.slippage_source {
        rt::SlippageSource::Assumed => "ASSUMED",
        rt::SlippageSource::Measured => "MEASURED",
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn a_diagonal_concat_fills_missing_columns_with_null() {
        let a = DataFrame::new(1, vec![Series::new("x".into(), [1_i64]).into_column()]).unwrap();
        let b = DataFrame::new(
            1,
            vec![
                Series::new("y".into(), ["a"]).into_column(),
                Series::new("x".into(), [2_i64]).into_column(),
            ],
        )
        .unwrap();
        let out = concat_diagonal(&a, &b).unwrap();
        assert_eq!(out.get_column_names(), ["x", "y"]);
        assert_eq!(out.height(), 2);
        assert_eq!(
            cols::strings(&out, "y").unwrap(),
            vec![None, Some("a".to_owned())]
        );
    }

    #[test]
    fn lists_print_as_python_reprs() {
        assert_eq!(
            py_list(&["B4".to_owned(), "F3N".to_owned()]),
            "['B4', 'F3N']"
        );
    }
}
