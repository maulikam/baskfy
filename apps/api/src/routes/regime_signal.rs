//! `regime_signal` on scan rows (docs/swing/REGIME-SIGNAL.md): the worker's `stock_regime_signal`
//! row for (scan date, symbol) — the latest `as_of` on or before the scan date. Read only.

use std::collections::HashMap;

use baskfy_db::OrderedJson;
use chrono::{Days, NaiveDate};

use crate::problems::Problem;
use crate::routes::sleeves::common::{J, Obj, db, symbol_key};

/// The field name on every row.
pub(crate) const FIELD: &str = "regime_signal";
/// A signal older than this many days before the scan date is not shown.
const MAX_AGE_DAYS: u64 = 10;

fn evidence_j(signal: String, fallback: bool, evidence: Option<&OrderedJson>) -> J {
    let take = |k: &str| {
        evidence
            .and_then(|e| e.get(k))
            .cloned()
            .unwrap_or(OrderedJson::Array(Vec::new()))
    };
    Obj::new()
        .put("signal", J::Str(signal))
        .put("fallback", J::Bool(fallback))
        .put("indices", J::Raw(take("indices")))
        .put("triggered", J::Raw(take("triggered")))
        .done()
}

/// Appends `regime_signal` (or `null`) to each row object of `rows`.
///
/// # Errors
/// 500 on a database error.
pub(crate) async fn attach(
    pool: &sqlx::PgPool,
    on: Option<NaiveDate>,
    rows: &mut [J],
) -> Result<(), Problem> {
    let mut found: HashMap<String, J> = HashMap::new();
    if let Some(day) = on
        && !rows.is_empty()
    {
        let ready: bool =
            sqlx::query_scalar("SELECT to_regclass('stock_regime_signal') IS NOT NULL")
                .fetch_one(pool)
                .await
                .map_err(db)?;
        if ready {
            let symbols: Vec<String> = rows
                .iter()
                .map(symbol_key)
                .filter(|s| !s.is_empty())
                .collect();
            let oldest = day.checked_sub_days(Days::new(MAX_AGE_DAYS)).unwrap_or(day);
            let got: Vec<(String, String, bool, String)> = sqlx::query_as(
                "SELECT DISTINCT ON (symbol) symbol, signal, fallback, evidence::text \
                 FROM stock_regime_signal WHERE symbol = ANY($1) AND as_of <= $2 AND as_of >= $3 \
                 ORDER BY symbol, as_of DESC",
            )
            .bind(&symbols)
            .bind(day)
            .bind(oldest)
            .fetch_all(pool)
            .await
            .map_err(db)?;
            for (symbol, signal, fallback, evidence) in got {
                let ev = OrderedJson::parse(&evidence).ok();
                found.insert(symbol, evidence_j(signal, fallback, ev.as_ref()));
            }
        }
    }
    // The NSE series (EQ, BE, BZ, SM, ST...) so the UI can flag trade-for-trade / SME names.
    let symbols: Vec<String> = rows
        .iter()
        .map(symbol_key)
        .filter(|s| !s.is_empty())
        .collect();
    let series: HashMap<String, String> = if symbols.is_empty() {
        HashMap::new()
    } else {
        sqlx::query_as::<_, (String, String)>(
            "SELECT DISTINCT ON (i.symbol) i.symbol, i.series FROM instrument i \
             WHERE i.symbol = ANY($1) AND i.series IS NOT NULL AND i.series <> '' \
             ORDER BY i.symbol, i.is_active DESC, \
               (i.exchange_id = (SELECT id FROM exchange WHERE code = 'NSE')) DESC, i.id DESC",
        )
        .bind(&symbols)
        .fetch_all(pool)
        .await
        .map_err(db)?
        .into_iter()
        .collect()
    };
    for row in rows.iter_mut() {
        let key = symbol_key(row);
        let value = found.get(&key).cloned().unwrap_or(J::Null);
        if let J::Obj(fields) = row {
            fields.push((FIELD.to_owned(), value));
            if !fields.iter().any(|(k, _)| k == "series") {
                let s = series.get(&key).map_or(J::Null, |v| J::Str(v.clone()));
                fields.push(("series".to_owned(), s));
            }
        }
    }
    Ok(())
}

/// `GET /meta/series-flags` — every NSE symbol whose series is not EQ (BE, BZ, SM, ST...), so any
/// stock list in the web app can flag trade-for-trade and SME names.
pub async fn get_series_flags(
    axum::extract::State(state): axum::extract::State<crate::state::AppState>,
) -> axum::response::Response {
    let rows = sqlx::query_as::<_, (String, String)>(
        "SELECT symbol, series FROM (SELECT DISTINCT ON (i.symbol) i.symbol, i.series FROM instrument i \
         WHERE i.series IS NOT NULL AND i.series <> '' \
         ORDER BY i.symbol, i.is_active DESC, \
           (i.exchange_id = (SELECT id FROM exchange WHERE code = 'NSE')) DESC, i.id DESC) s \
         WHERE series <> 'EQ' ORDER BY symbol",
    )
    .fetch_all(state.db.pool())
    .await;
    match rows {
        Ok(rows) => {
            let data: std::collections::BTreeMap<String, String> = rows.into_iter().collect();
            crate::wire::json(&serde_json::json!({ "data": data }))
        }
        Err(e) => axum::response::IntoResponse::into_response(db(e)),
    }
}
