//! Step 3 — `fetch_corporate_actions` (docs/03: "NSE; upsert into corporate_action"). Ports
//! `baskfy_worker.tasks.corporate_actions.run_fetch_corporate_actions`.
//!
//! The key is `(instrument_id, action_type, ex_date)`, so an overlapping window converges. Two
//! announcements sharing that key in one file (SIYSIL's two bonus NCRPS on one ex-date) collapse
//! to the last, as sequential upserts would, and are reported as `duplicate_keys` (§21.5).
//! Returns the instrument ids touched — step 4's input.

use std::collections::{BTreeSet, HashMap};

use baskfy_db::OrderedJson;
use baskfy_providers::composite::CompositeProvider;
use baskfy_providers::records::CorporateActionFields;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;

fn to_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

/// One row to upsert.
#[derive(Debug, Clone, PartialEq)]
pub(super) struct ActionRow {
    pub(super) instrument_id: i64,
    pub(super) action_type: &'static str,
    pub(super) ex_date: NaiveDate,
    pub(super) ratio_from: Option<Decimal>,
    pub(super) ratio_to: Option<Decimal>,
    pub(super) amount: Option<Decimal>,
    pub(super) raw: String,
}

/// The collapse: the last occurrence of each key, in first-appearance-of-the-survivor order
/// (`values[i] for i in sorted(last_index.values())`), and every collision as `id:type:date`.
pub(super) fn collapse_duplicates(values: Vec<ActionRow>) -> (Vec<ActionRow>, Vec<String>) {
    let mut last_index: HashMap<(i64, &'static str, NaiveDate), usize> = HashMap::new();
    let mut collisions = Vec::new();
    for (index, value) in values.iter().enumerate() {
        let key = (value.instrument_id, value.action_type, value.ex_date);
        if last_index.contains_key(&key) {
            collisions.push(format!("{}:{}:{}", key.0, key.1, iso(key.2)));
        }
        last_index.insert(key, index);
    }
    let mut keep: Vec<usize> = last_index.into_values().collect();
    keep.sort_unstable();
    let mut slots: Vec<Option<ActionRow>> = values.into_iter().map(Some).collect();
    let kept = keep
        .into_iter()
        .filter_map(|i| slots.get_mut(i).and_then(Option::take))
        .collect();
    (kept, collisions)
}

/// `run_fetch_corporate_actions(session, provider, step, since)`.
pub(super) async fn run_fetch_corporate_actions(
    conn: &mut PgConnection,
    provider: &CompositeProvider,
    outcome: &mut StepOutcome,
    since: NaiveDate,
) -> Result<BTreeSet<i64>, WorkerError> {
    let records: Vec<CorporateActionFields> = match provider.corporate_actions(since).await {
        Ok(rows) => rows.into_iter().map(|r| (*r).clone()).collect(),
        Err(e) => {
            if e.is_provider_error() {
                outcome.note("error", pyjson::s(e.to_string()));
            }
            return Err(e.into());
        }
    };
    outcome.rows_in = to_i64(records.len());
    if records.is_empty() {
        outcome.note("since", pyjson::s(iso(since)));
        outcome.note("touched", pyjson::int(0));
        return Ok(BTreeSet::new());
    }

    let rows: Vec<(String, i64)> =
        sqlx::query_as("SELECT instrument.symbol, instrument.id FROM instrument")
            .fetch_all(&mut *conn)
            .await?;
    let mut ids: HashMap<String, i64> = HashMap::with_capacity(rows.len());
    for (symbol, id) in rows {
        ids.insert(symbol, id);
    }
    let unknown: BTreeSet<String> = records
        .iter()
        .filter(|r| !ids.contains_key(&r.symbol))
        .map(|r| r.symbol.clone())
        .collect();
    let unknown_json = if unknown.is_empty() {
        OrderedJson::Null
    } else {
        pyjson::strings(unknown)
    };

    let mut values = Vec::new();
    for record in &records {
        let Some(&instrument_id) = ids.get(&record.symbol) else {
            continue;
        };
        values.push(ActionRow {
            instrument_id,
            action_type: record.action_type.as_str(),
            ex_date: record.ex_date,
            ratio_from: record.ratio_from,
            ratio_to: record.ratio_to,
            amount: record.amount,
            raw: serde_json::to_string(&record.raw)
                .map_err(|e| WorkerError::python("TypeError", e.to_string()))?,
        });
    }
    if values.is_empty() {
        outcome.note("since", pyjson::s(iso(since)));
        outcome.note("unknown_symbols", unknown_json);
        outcome.note("touched", pyjson::int(0));
        return Ok(BTreeSet::new());
    }
    let (values, collisions) = collapse_duplicates(values);

    sqlx::query(
        "INSERT INTO corporate_action (instrument_id, action_type, ex_date, ratio_from, \
         ratio_to, amount, raw) \
         SELECT u.instrument_id, u.action_type, u.ex_date, u.ratio_from, u.ratio_to, u.amount, \
         CAST(u.raw AS JSONB) \
         FROM UNNEST($1::int8[], $2::varchar[], $3::date[], $4::numeric[], $5::numeric[], \
         $6::numeric[], $7::text[]) \
         AS u(instrument_id, action_type, ex_date, ratio_from, ratio_to, amount, raw) \
         ON CONFLICT (instrument_id, action_type, ex_date) DO UPDATE SET \
         ratio_from = excluded.ratio_from, ratio_to = excluded.ratio_to, \
         amount = excluded.amount, raw = excluded.raw",
    )
    .bind(values.iter().map(|v| v.instrument_id).collect::<Vec<_>>())
    .bind(
        values
            .iter()
            .map(|v| v.action_type.to_owned())
            .collect::<Vec<_>>(),
    )
    .bind(values.iter().map(|v| v.ex_date).collect::<Vec<_>>())
    .bind(values.iter().map(|v| v.ratio_from).collect::<Vec<_>>())
    .bind(values.iter().map(|v| v.ratio_to).collect::<Vec<_>>())
    .bind(values.iter().map(|v| v.amount).collect::<Vec<_>>())
    .bind(values.iter().map(|v| v.raw.clone()).collect::<Vec<_>>())
    .execute(&mut *conn)
    .await?;

    let touched: BTreeSet<i64> = records
        .iter()
        .filter_map(|r| ids.get(&r.symbol).copied())
        .collect();
    outcome.rows_out = to_i64(values.len());
    outcome.note("since", pyjson::s(iso(since)));
    outcome.note("touched", pyjson::int(to_i64(touched.len())));
    outcome.note("unknown_symbols", unknown_json);
    let duplicates: BTreeSet<String> = collisions.into_iter().collect();
    outcome.note(
        "duplicate_keys",
        if duplicates.is_empty() {
            OrderedJson::Null
        } else {
            pyjson::strings(duplicates)
        },
    );
    Ok(touched)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;

    fn row(id: i64, kind: &'static str, day: u32, amount: i64) -> ActionRow {
        ActionRow {
            instrument_id: id,
            action_type: kind,
            ex_date: NaiveDate::from_ymd_opt(2026, 8, day).unwrap(),
            ratio_from: None,
            ratio_to: None,
            amount: Some(Decimal::new(amount, 0)),
            raw: "{}".to_owned(),
        }
    }

    #[test]
    fn two_announcements_sharing_a_key_collapse_to_the_last_and_are_reported() {
        let (kept, collisions) = collapse_duplicates(vec![
            row(1, "bonus", 21, 3),
            row(2, "dividend", 21, 5),
            row(1, "bonus", 21, 4),
        ]);
        assert_eq!(kept.len(), 2);
        // The survivor of the collision sits where its last occurrence was.
        assert_eq!(kept[0].instrument_id, 2);
        assert_eq!(kept[1].amount, Some(Decimal::new(4, 0)));
        assert_eq!(collisions, ["1:bonus:2026-08-21"]);
    }

    #[test]
    fn distinct_keys_are_all_kept_in_order() {
        let (kept, collisions) =
            collapse_duplicates(vec![row(1, "split", 1, 1), row(1, "bonus", 1, 1)]);
        assert_eq!(kept.len(), 2);
        assert_eq!(kept[0].action_type, "split");
        assert!(collisions.is_empty());
    }
}
