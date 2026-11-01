//! `baskfy_worker.factors_cli`: `explain`, `recompute`, `backfill-ranking`.

pub(super) mod ranking;
pub(super) mod session;

#[cfg(test)]
pub(super) mod db_tests;

use baskfy_core::factor_registry::{self, Factor};
use baskfy_db::{OrderedJson, WriteDb};
use chrono::NaiveDate;
use polars::prelude::{AnyValue, DataFrame};
use sqlx::PgConnection;

use super::cli::{
    Failure, Outcome, Parsed, Spec, code, command, connect, emit, iso, parse_date, say, sort_keys,
    warn,
};
use crate::error::WorkerError;
use crate::steps::StepOutcome;
use crate::steps::compute::factors::{ints, load_history};
use crate::steps::compute::pl_err;

/// `SERIES_PREVIEW`.
const SERIES_PREVIEW: usize = 10;

const PREVIEW_COLUMNS: [&str; 7] = [
    "date",
    "close",
    "close_raw",
    "high",
    "low",
    "volume_raw",
    "turnover",
];

/// A Python value read out of a polars row.
#[derive(Debug, Clone, PartialEq)]
pub(super) enum Value {
    None,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Date(NaiveDate),
}

impl Value {
    fn of(value: &AnyValue<'_>) -> Self {
        match value {
            AnyValue::Null => Self::None,
            AnyValue::Boolean(b) => Self::Bool(*b),
            AnyValue::Int8(v) => Self::Int(i64::from(*v)),
            AnyValue::Int16(v) => Self::Int(i64::from(*v)),
            AnyValue::Int32(v) => Self::Int(i64::from(*v)),
            AnyValue::Int64(v) => Self::Int(*v),
            AnyValue::UInt8(v) => Self::Int(i64::from(*v)),
            AnyValue::UInt16(v) => Self::Int(i64::from(*v)),
            AnyValue::UInt32(v) => Self::Int(i64::from(*v)),
            AnyValue::UInt64(v) => Self::Int(i64::try_from(*v).unwrap_or(i64::MAX)),
            AnyValue::Float32(v) => Self::Float(f64::from(*v)),
            AnyValue::Float64(v) => Self::Float(*v),
            AnyValue::String(s) => Self::Str((*s).to_owned()),
            AnyValue::StringOwned(s) => Self::Str(s.to_string()),
            AnyValue::Date(days) => Self::Date(
                NaiveDate::from_num_days_from_ce_opt(*days + 719_163).unwrap_or(NaiveDate::MIN),
            ),
            other => Self::Str(other.to_string()),
        }
    }

    fn as_float(&self) -> Option<f64> {
        match self {
            Self::Float(f) => Some(*f),
            #[allow(clippy::cast_precision_loss)]
            Self::Int(i) => Some(*i as f64),
            Self::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
            Self::Str(s) => s.parse().ok(),
            Self::None | Self::Date(_) => None,
        }
    }

    /// `str(value)`.
    fn text(&self) -> String {
        match self {
            Self::None => "None".to_owned(),
            Self::Bool(true) => "True".to_owned(),
            Self::Bool(false) => "False".to_owned(),
            Self::Int(i) => i.to_string(),
            Self::Float(f) => baskfy_core::py::float_repr(*f),
            Self::Str(s) => s.clone(),
            Self::Date(d) => iso(*d),
        }
    }

    /// `_jsonable(value)`.
    fn json(&self) -> OrderedJson {
        match self {
            Self::None => OrderedJson::Null,
            Self::Bool(b) => OrderedJson::Bool(*b),
            Self::Int(i) => OrderedJson::Int(*i),
            Self::Float(f) => OrderedJson::Float(*f),
            Self::Str(s) => OrderedJson::Str(s.clone()),
            Self::Date(d) => OrderedJson::Str(iso(*d)),
        }
    }
}

type Row = Vec<(String, Value)>;

fn get<'a>(row: &'a Row, key: &str) -> &'a Value {
    row.iter()
        .find(|(k, _)| k == key)
        .map_or(&Value::None, |(_, v)| v)
}

fn row_of(frame: &DataFrame, index: usize, columns: &[String]) -> Result<Row, WorkerError> {
    columns
        .iter()
        .map(|name| {
            let column = frame.column(name).map_err(|e| pl_err(&e))?;
            let value = column.get(index).map_err(|e| pl_err(&e))?;
            Ok((name.clone(), Value::of(&value)))
        })
        .collect()
}

/// `Explanation`.
#[derive(Debug, Clone, PartialEq)]
pub(super) struct Explanation {
    pub symbol: String,
    pub date: NaiveDate,
    pub factor: String,
    pub value: Value,
    pub window_lengths: Vec<(i64, usize)>,
    pub inputs: Row,
    pub series_preview: Vec<Row>,
    pub notes: Vec<String>,
}

impl Explanation {
    /// `to_payload()`.
    pub(super) fn to_payload(&self) -> OrderedJson {
        let row =
            |r: &Row| OrderedJson::Object(r.iter().map(|(k, v)| (k.clone(), v.json())).collect());
        OrderedJson::Object(vec![
            ("symbol".to_owned(), OrderedJson::Str(self.symbol.clone())),
            ("date".to_owned(), OrderedJson::Str(iso(self.date))),
            ("factor".to_owned(), OrderedJson::Str(self.factor.clone())),
            ("value".to_owned(), self.value.json()),
            (
                "window_lengths".to_owned(),
                OrderedJson::Object(
                    self.window_lengths
                        .iter()
                        .map(|(k, v)| {
                            (
                                k.to_string(),
                                OrderedJson::Int(i64::try_from(*v).unwrap_or(i64::MAX)),
                            )
                        })
                        .collect(),
                ),
            ),
            ("inputs".to_owned(), row(&self.inputs)),
            (
                "series_preview".to_owned(),
                OrderedJson::Array(self.series_preview.iter().map(row).collect()),
            ),
            (
                "notes".to_owned(),
                OrderedJson::Array(
                    self.notes
                        .iter()
                        .map(|n| OrderedJson::Str(n.clone()))
                        .collect(),
                ),
            ),
        ])
    }
}

/// Python's `repr(str)` for a message (quotes chosen as CPython chooses them).
fn py_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(quote);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// What `explain` raises: `LookupError` and `KeyError`, printed as `str(exc).strip("'")`.
#[derive(Debug)]
pub(super) enum ExplainError {
    Lookup(String),
    Key(String),
    Worker(WorkerError),
}

impl ExplainError {
    /// `str(exc).strip("'")`.
    pub(super) fn printed(&self) -> String {
        let text = match self {
            Self::Lookup(m) => m.clone(),
            Self::Key(m) => py_repr(m),
            Self::Worker(e) => e.to_string(),
        };
        text.trim_matches('\'').to_owned()
    }
}

impl From<WorkerError> for ExplainError {
    fn from(e: WorkerError) -> Self {
        Self::Worker(e)
    }
}

impl From<sqlx::Error> for ExplainError {
    fn from(e: sqlx::Error) -> Self {
        Self::Worker(WorkerError::from(e))
    }
}

/// `explain(session, symbol, on, factor_key)`.
pub(super) async fn explain(
    conn: &mut PgConnection,
    symbol: &str,
    on: NaiveDate,
    factor_key: &str,
) -> Result<Explanation, ExplainError> {
    let instrument_id: Option<i64> =
        sqlx::query_scalar("SELECT instrument.id FROM instrument WHERE instrument.symbol = $1")
            .bind(symbol)
            .fetch_optional(&mut *conn)
            .await?;
    let Some(instrument_id) = instrument_id else {
        return Err(ExplainError::Lookup(format!(
            "no instrument with symbol {}",
            py_repr(symbol)
        )));
    };

    let history = load_history(conn, on).await?;
    let (frame, window_lengths) = ranking::engine_frame(&history, on)?;
    let mut explanation = Explanation {
        symbol: symbol.to_owned(),
        date: on,
        factor: factor_key.to_owned(),
        value: Value::None,
        window_lengths,
        inputs: Vec::new(),
        series_preview: Vec::new(),
        notes: Vec::new(),
    };
    let ids = ints(&frame, "instrument_id")?;
    let Some(index) = ids.iter().position(|i| *i == Some(instrument_id)) else {
        explanation.notes.push(format!(
            "{symbol} has no bar on {}, so no factor row exists for that date",
            iso(on)
        ));
        return Ok(explanation);
    };
    let columns: Vec<String> = frame
        .get_column_names()
        .iter()
        .map(|c| c.to_string())
        .collect();
    let values = row_of(&frame, index, &columns)?;

    let factor = factor_registry::lookup(factor_key);
    if factor_key == "all" {
        explanation.inputs = values
            .iter()
            .filter(|(k, _)| !k.starts_with('_'))
            .cloned()
            .collect();
    } else if let Some(factor) = factor {
        explanation.value = evaluate(factor, &values);
        explanation.inputs = inputs_for(factor, &values);
        explanation.notes.extend(notes_for(factor));
    } else {
        let mut keys: Vec<&str> = factor_registry::factors()
            .iter()
            .map(|f| f.key.as_str())
            .collect();
        keys.sort_unstable();
        let first: Vec<&str> = keys.into_iter().take(8).collect();
        return Err(ExplainError::Key(format!(
            "unknown factor {}; not in the registry. Try one of: {}, ...",
            py_repr(factor_key),
            first.join(", ")
        )));
    }

    let bar_ids = ints(&history.bars, "instrument_id")?;
    let bar_columns: Vec<String> = PREVIEW_COLUMNS.iter().map(|c| (*c).to_owned()).collect();
    let mut mine: Vec<(NaiveDate, usize)> = Vec::new();
    for (i, id) in bar_ids.iter().enumerate() {
        if *id == Some(instrument_id) {
            let row = row_of(&history.bars, i, &bar_columns[..1])?;
            if let Some((_, Value::Date(d))) = row.first() {
                mine.push((*d, i));
            }
        }
    }
    mine.sort_by_key(|(d, _)| *d);
    let skip = mine.len().saturating_sub(SERIES_PREVIEW);
    for (_, i) in mine.into_iter().skip(skip) {
        explanation
            .series_preview
            .push(row_of(&history.bars, i, &bar_columns)?);
    }
    Ok(explanation)
}

/// `_evaluate(factor, values)`.
pub(super) fn evaluate(factor: &Factor, values: &Row) -> Value {
    if factor.is_stored {
        return get(values, &factor.key).clone();
    }
    let components: Vec<&Value> = factor
        .components
        .iter()
        .filter(|n| n.as_str() != "beta_12m")
        .map(|n| get(values, n))
        .collect();
    if components.iter().any(|c| matches!(c, Value::None)) {
        return Value::None;
    }
    let numbers: Vec<f64> = components.iter().filter_map(|c| c.as_float()).collect();
    if numbers.len() != components.len() || numbers.is_empty() {
        return Value::None;
    }
    #[allow(clippy::cast_precision_loss)]
    let blended = numbers.iter().fold(0.0, |a, b| a + b) / numbers.len() as f64;
    if factor.components.iter().any(|c| c == "beta_12m") {
        return match get(values, "beta_12m").as_float() {
            Some(beta) if beta > 0.0 => Value::Float(blended / beta),
            _ => Value::None,
        };
    }
    Value::Float(blended)
}

fn entry(values: &Row, key: &str) -> (String, Value) {
    (key.to_owned(), get(values, key).clone())
}

fn formula(text: &str) -> (String, Value) {
    ("formula".to_owned(), Value::Str(text.to_owned()))
}

/// `_inputs_for(factor, values)`.
pub(super) fn inputs_for(factor: &Factor, values: &Row) -> Row {
    if !factor.is_stored {
        return factor.components.iter().map(|n| entry(values, n)).collect();
    }
    let key = factor.key.as_str();
    if let Some(window) = key.strip_prefix("sharpe_") {
        return vec![
            entry(values, &format!("ret_{window}")),
            entry(values, &format!("vol_{window}")),
            formula("ret / (vol * 100)"),
        ];
    }
    if key.starts_with("away_high_") {
        let high = if key.ends_with("1y") {
            "high_1y"
        } else {
            "high_ath"
        };
        return vec![
            entry(values, "close"),
            entry(values, high),
            formula("(close / high - 1) * 100"),
        ];
    }
    if key.starts_with("ret_") && !key.contains("minus") {
        return vec![entry(values, "close"), formula("(P_t / P_{t-N} - 1) * 100")];
    }
    vec![entry(values, key)]
}

fn enum_value<T: serde::Serialize>(value: &T) -> String {
    match serde_json::to_value(value) {
        Ok(serde_json::Value::String(s)) => s,
        _ => String::new(),
    }
}

/// `_notes_for(factor)`.
pub(super) fn notes_for(factor: &Factor) -> Vec<String> {
    let mut notes = vec![
        format!("family: {}", factor.family.value()),
        format!("null policy: {}", enum_value(&factor.null_policy)),
    ];
    if !factor.is_stored {
        notes.push(format!("SQL: {}", factor.sql_expr));
    }
    if factor.key.starts_with("ret_12m_minus") {
        notes.push(
            "INFERRED (docs/05 §8): the reference product's published values do not reconcile \
             with this definition; see baskfy_core.momentum for the alternative reading."
                .to_owned(),
        );
    }
    if factor.key.starts_with("circuits_") {
        notes.push(
            "INFERRED (docs/05 §12): see baskfy_core.circuits for the detection rule.".to_owned(),
        );
    }
    notes
}

/// `render_text(explanation)`.
pub(super) fn render_text(e: &Explanation) -> String {
    let mut windows = e.window_lengths.clone();
    windows.sort_unstable();
    let mut lines = vec![
        format!("{} — {} on {}", e.symbol, e.factor, iso(e.date)),
        "=".repeat(72),
        format!("value: {}", e.value.text()),
        String::new(),
        "window lengths (trading days, recovered from the calendar):".to_owned(),
        format!(
            "  {}",
            windows
                .iter()
                .map(|(k, v)| format!("{k}m={v}"))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        String::new(),
        "inputs:".to_owned(),
    ];
    lines.extend(
        e.inputs
            .iter()
            .map(|(name, value)| format!("  {name:<24} {}", value.text())),
    );
    if !e.series_preview.is_empty() {
        lines.push(String::new());
        lines.push(format!("last {} bars:", e.series_preview.len()));
        lines.extend(e.series_preview.iter().map(|row| {
            format!(
                "  {}",
                row.iter()
                    .map(|(k, v)| format!("{k}={}", v.text()))
                    .collect::<Vec<_>>()
                    .join("  ")
            )
        }));
    }
    if !e.notes.is_empty() {
        lines.push(String::new());
        lines.push("notes:".to_owned());
        lines.extend(e.notes.iter().map(|n| format!("  - {n}")));
    }
    lines.join("\n")
}

/// `run_in_session`: one transaction, committed when the body returns.
async fn in_session<T, F>(db: &WriteDb, body: F) -> Result<T, Failure>
where
    F: for<'c> FnOnce(
        &'c mut PgConnection,
    ) -> std::pin::Pin<Box<dyn Future<Output = Result<T, Failure>> + 'c>>,
{
    let mut tx = db.begin().await?;
    match body(&mut tx).await {
        Ok(v) => {
            tx.commit().await?;
            Ok(v)
        }
        Err(e) => {
            let _ = tx.rollback().await;
            Err(e)
        }
    }
}

pub(super) async fn run(args: &[String]) -> Outcome {
    let (cmd, rest) = command(args, &["explain", "recompute", "backfill-ranking"])?;
    match cmd {
        "backfill-ranking" => backfill_ranking(rest).await,
        "recompute" => {
            let parsed = Parsed::parse(
                rest,
                Spec {
                    valued: &["--date"],
                    ..Spec::default()
                },
            )?;
            let on = parse_date(parsed.required("--date")?)?;
            let db = connect(None)?;
            let written = in_session(&db, |conn| {
                Box::pin(async move {
                    let mut outcome = StepOutcome::default();
                    Ok(crate::steps::compute::factors::run(conn, &mut outcome, on).await?)
                })
            })
            .await;
            db.close().await;
            let written = written?;
            say(&format!("RECOMPUTED date={} rows={written}", iso(on)));
            Ok(code(written != 0))
        }
        _ => {
            let parsed = Parsed::parse(
                rest,
                Spec {
                    valued: &["--symbol", "--date", "--factor"],
                    switches: &["--json"],
                    ..Spec::default()
                },
            )?;
            let symbol = parsed.required("--symbol")?.to_owned();
            let date = parsed.required("--date")?.to_owned();
            let factor = parsed.required("--factor")?.to_owned();
            let on = parse_date(&date)?;
            let db = connect(None)?;
            let mut tx = db.begin().await?;
            let result = explain(&mut tx, &symbol, on, &factor).await;
            let committed = tx.commit().await;
            db.close().await;
            let explanation = match result {
                Ok(e) => e,
                Err(ExplainError::Worker(e)) => return Err(Failure::Raised(e)),
                Err(other) => {
                    warn(&other.printed());
                    return Ok(code(false));
                }
            };
            committed?;
            if parsed.on("--json") {
                emit(&sort_keys(explanation.to_payload()));
            } else {
                say(&render_text(&explanation));
            }
            Ok(code(true))
        }
    }
}

async fn backfill_ranking(rest: &[String]) -> Outcome {
    let parsed = Parsed::parse(
        rest,
        Spec {
            valued: &["--from", "--to", "--database-url"],
            switches: &["--resume", "--force"],
            ..Spec::default()
        },
    )?;
    let start_text = parsed.required("--from")?.to_owned();
    let end_text = parsed.required("--to")?.to_owned();
    if parsed.on("--resume") && parsed.on("--force") {
        return Err(Failure::Usage(
            "--resume skips complete days and --force recomputes them; pick one".to_owned(),
        ));
    }
    let start = parse_date(&start_text)?;
    let end = parse_date(&end_text)?;
    if end < start {
        return Err(Failure::Usage("--to is before --from".to_owned()));
    }
    let db = connect(parsed.get("--database-url"))?;
    let outcome = backfill_checkpointed(&db, start, end, parsed.on("--force")).await;
    db.close().await;
    let report = outcome?;
    say(&ranking::render(&report));
    Ok(code(report.errors().is_empty()))
}

async fn backfill_checkpointed(
    db: &WriteDb,
    start: NaiveDate,
    end: NaiveDate,
    force: bool,
) -> Result<ranking::Report, WorkerError> {
    let mut session = session::Checkpointed::open(db).await?;
    let report =
        ranking::run_backfill_ranking(&mut session, start, end, force, ranking::desk_score).await?;
    session.finish().await?;
    Ok(report)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]

    use super::*;

    fn row(pairs: &[(&str, Value)]) -> Row {
        pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), v.clone()))
            .collect()
    }

    #[test]
    fn a_stored_sharpe_shows_its_ratio_inputs() {
        let factor = factor_registry::lookup("sharpe_12m").unwrap();
        let values = row(&[
            ("sharpe_12m", Value::Float(5.19)),
            ("ret_12m", Value::Float(80.1)),
            ("vol_12m", Value::Float(0.15)),
        ]);
        assert_eq!(evaluate(factor, &values), Value::Float(5.19));
        let inputs = inputs_for(factor, &values);
        assert_eq!(
            inputs,
            row(&[
                ("ret_12m", Value::Float(80.1)),
                ("vol_12m", Value::Float(0.15)),
                ("formula", Value::Str("ret / (vol * 100)".to_owned())),
            ])
        );
        let notes = notes_for(factor);
        assert!(notes[0].starts_with("family: "));
        assert_eq!(notes[1], "null policy: null_on_undefined_ratio");
    }

    #[test]
    fn a_blend_is_null_when_any_component_is_null() {
        let blend = factor_registry::factors()
            .iter()
            .find(|f| {
                !f.is_stored
                    && f.components.len() > 1
                    && !f.components.iter().any(|c| c == "beta_12m")
            })
            .unwrap();
        let mut values: Row = blend
            .components
            .iter()
            .enumerate()
            .map(|(i, c)| {
                #[allow(clippy::cast_precision_loss)]
                let v = Value::Float(i as f64 + 1.0);
                (c.clone(), v)
            })
            .collect();
        #[allow(clippy::cast_precision_loss)]
        let mean = (1..=blend.components.len()).map(|i| i as f64).sum::<f64>()
            / blend.components.len() as f64;
        assert_eq!(evaluate(blend, &values), Value::Float(mean));
        values[0].1 = Value::None;
        assert_eq!(evaluate(blend, &values), Value::None);
        assert!(notes_for(blend).iter().any(|n| n.starts_with("SQL: ")));
    }

    #[test]
    fn a_beta_blend_is_null_on_a_non_positive_beta() {
        let Some(blend) = factor_registry::factors()
            .iter()
            .find(|f| !f.is_stored && f.components.iter().any(|c| c == "beta_12m"))
        else {
            return;
        };
        let mut values: Row = blend
            .components
            .iter()
            .map(|c| (c.clone(), Value::Float(2.0)))
            .collect();
        assert_eq!(evaluate(blend, &values), Value::Float(1.0));
        for (k, v) in &mut values {
            if k == "beta_12m" {
                *v = Value::Float(0.0);
            }
        }
        assert_eq!(evaluate(blend, &values), Value::None);
    }

    #[test]
    fn errors_print_as_python_strips_them() {
        let lookup = ExplainError::Lookup(format!("no instrument with symbol {}", py_repr("ZZZ")));
        assert_eq!(lookup.printed(), "no instrument with symbol 'ZZZ");
        let key = ExplainError::Key("unknown factor 'nope'; not in the registry.".to_owned());
        assert_eq!(
            key.printed(),
            "\"unknown factor 'nope'; not in the registry.\""
        );
    }

    #[test]
    fn render_text_lays_out_the_working() {
        let e = Explanation {
            symbol: "CUPID".to_owned(),
            date: NaiveDate::from_ymd_opt(2026, 8, 18).unwrap(),
            factor: "sharpe_12m".to_owned(),
            value: Value::Float(5.19),
            window_lengths: vec![(3, 63), (1, 21)],
            inputs: row(&[("ret_12m", Value::None)]),
            series_preview: vec![row(&[
                (
                    "date",
                    Value::Date(NaiveDate::from_ymd_opt(2026, 8, 18).unwrap()),
                ),
                ("close", Value::Float(10.0)),
            ])],
            notes: vec!["family: x".to_owned()],
        };
        let text = render_text(&e);
        let expected = format!(
            "CUPID — sharpe_12m on 2026-08-18\n{}\nvalue: 5.19\n\nwindow lengths (trading days, \
             recovered from the calendar):\n  1m=21, 3m=63\n\ninputs:\n  ret_12m{} None\n\nlast \
             1 bars:\n  date=2026-08-18  close=10.0\n\nnotes:\n  - family: x",
            "=".repeat(72),
            " ".repeat(17)
        );
        assert_eq!(text, expected);
        let payload = sort_keys(e.to_payload());
        assert_eq!(
            payload.get("window_lengths"),
            Some(&OrderedJson::Object(vec![
                ("1".to_owned(), OrderedJson::Int(21)),
                ("3".to_owned(), OrderedJson::Int(63)),
            ]))
        );
    }

    #[tokio::test]
    async fn backfill_ranking_refuses_resume_with_force() {
        let args: Vec<String> = [
            "backfill-ranking",
            "--from",
            "2026-08-01",
            "--to",
            "2026-08-18",
            "--resume",
            "--force",
        ]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
        let Err(Failure::Usage(m)) = run(&args).await else {
            panic!("--resume with --force must be a usage error");
        };
        assert!(m.contains("pick one"));
        let args: Vec<String> = [
            "backfill-ranking",
            "--from",
            "2026-08-18",
            "--to",
            "2026-08-01",
        ]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
        let Err(Failure::Usage(m)) = run(&args).await else {
            panic!("--to before --from must be a usage error");
        };
        assert_eq!(m, "--to is before --from");
    }
}
