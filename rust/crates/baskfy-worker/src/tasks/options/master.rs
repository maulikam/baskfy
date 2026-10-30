//! The nightly NFO master: `op_contract` and `op_expiry` (`options.master`, OP2, widened by FO2
//! to every F&O underlying).
//!
//! Tonight's dump is upserted per underlying — **rows are never deleted**; a contract absent
//! from tonight's dump is marked `expired` — and each underlying's `op_expiry` is rebuilt from
//! its own contracts with the calendar's `kind` (monthly = the last master expiry of its month;
//! no weekday arithmetic). A lot-size change, a kind change or a withdrawn future expiry is a
//! [`MasterChange`], written into that expiry's `detail`; NIFTY's raise
//! `OPTIONS_MASTER_CHANGED`. A dump with no NIFTY option is refused whole and writes nothing.
//! Idempotent (house rule 7): the same dump on the same night changes nothing.
//!
//! Python's ORM unit of work is reproduced by its outcome: a new expiry is inserted, a changed
//! one is updated, an unchanged one is left alone (SQLAlchemy issues no `UPDATE` for an
//! attribute set to its own value). The rows of `op_expiry` are read without an `ORDER BY`, as
//! Python's are, so a night with several withdrawals lists them in the same (heap) order.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_db::OrderedJson;
use baskfy_providers::records::{OptionContractRecord, OptionType as RecordOptionType};
use chrono::NaiveDate;
use sqlx::{PgConnection, Postgres, QueryBuilder};

use super::alerting::{Level, OptionsAlert, OptionsName};
use super::common::{count, iso};
use super::model::{ExpiryKind, UNDERLYING, kind_in};
use crate::error::WorkerError;
use crate::pyjson::{object, opt_s, s};

/// `master.EmptyMaster`: tonight's dump held nothing usable; nothing was written.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct EmptyMaster(pub(crate) String);

/// `master.MasterChange`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct MasterChange {
    pub(crate) expiry: NaiveDate,
    pub(crate) what: &'static str,
    pub(crate) before: Option<String>,
    pub(crate) after: Option<String>,
}

impl MasterChange {
    pub(crate) fn as_dict(&self) -> OrderedJson {
        object([
            ("expiry", s(iso(self.expiry))),
            ("what", s(self.what)),
            ("before", opt_s(self.before.clone())),
            ("after", opt_s(self.after.clone())),
        ])
    }
}

/// `master.MasterReport`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct MasterReport {
    pub(crate) as_of: NaiveDate,
    pub(crate) contracts_seen: usize,
    pub(crate) contracts_new: usize,
    pub(crate) contracts_expired: u64,
    pub(crate) expiries: usize,
    pub(crate) changes: Vec<MasterChange>,
}

impl MasterReport {
    fn new(as_of: NaiveDate) -> Self {
        Self {
            as_of,
            contracts_seen: 0,
            contracts_new: 0,
            contracts_expired: 0,
            expiries: 0,
            changes: Vec::new(),
        }
    }

    /// `as_dict()`, as `(key, value)` pairs (the task spreads it into its payload).
    pub(crate) fn pairs(&self) -> Vec<(String, OrderedJson)> {
        vec![
            ("as_of".to_owned(), s(iso(self.as_of))),
            ("contracts_seen".to_owned(), count(self.contracts_seen)),
            ("contracts_new".to_owned(), count(self.contracts_new)),
            (
                "contracts_expired".to_owned(),
                OrderedJson::Int(i64::try_from(self.contracts_expired).unwrap_or(i64::MAX)),
            ),
            ("expiries".to_owned(), count(self.expiries)),
            (
                "changes".to_owned(),
                OrderedJson::Array(self.changes.iter().map(MasterChange::as_dict).collect()),
            ),
        ]
    }
}

/// `master.MasterReports`: tonight's master for every underlying.
#[derive(Debug, Clone)]
pub(crate) struct MasterReports {
    pub(crate) by_underlying: BTreeMap<String, MasterReport>,
    pub(crate) delisted: Vec<String>,
}

impl MasterReports {
    /// `reports.nifty` (present: a dump without NIFTY is refused before any report exists).
    pub(crate) fn nifty(&self) -> Option<&MasterReport> {
        self.by_underlying.get(UNDERLYING)
    }

    /// `summary()`.
    pub(crate) fn summary(&self) -> Vec<(String, OrderedJson)> {
        let changed = object(
            self.by_underlying
                .iter()
                .filter(|(name, r)| !r.changes.is_empty() && name.as_str() != UNDERLYING)
                .map(|(name, r)| {
                    (
                        name.clone(),
                        OrderedJson::Array(r.changes.iter().map(MasterChange::as_dict).collect()),
                    )
                }),
        );
        vec![
            ("underlyings".to_owned(), count(self.by_underlying.len())),
            (
                "contracts_seen_all".to_owned(),
                count(self.by_underlying.values().map(|r| r.contracts_seen).sum()),
            ),
            (
                "contracts_new_all".to_owned(),
                count(self.by_underlying.values().map(|r| r.contracts_new).sum()),
            ),
            ("other_underlying_changes".to_owned(), changed),
            (
                "delisted_underlyings".to_owned(),
                OrderedJson::Array(self.delisted.iter().map(|u| s(u.clone())).collect()),
            ),
        ]
    }
}

const fn record_type(kind: RecordOptionType) -> &'static str {
    match kind {
        RecordOptionType::Ce => "CE",
        RecordOptionType::Pe => "PE",
    }
}

/// `refresh_master_all(session, records, as_of=day)`.
///
/// # Errors
/// [`EmptyMaster`] (as `Ok(Err(..))`) for a dump without NIFTY; the database's otherwise.
pub(crate) async fn refresh_master_all(
    conn: &mut PgConnection,
    records: &[OptionContractRecord],
    as_of: NaiveDate,
) -> Result<Result<MasterReports, EmptyMaster>, WorkerError> {
    let mut grouped: BTreeMap<String, Vec<&OptionContractRecord>> = BTreeMap::new();
    for record in records {
        grouped
            .entry(record.underlying.clone())
            .or_default()
            .push(record);
    }
    if grouped.get(UNDERLYING).is_none_or(Vec::is_empty) {
        return Ok(Err(EmptyMaster(format!(
            "the NFO dump held no {UNDERLYING} option on {}; refusing the whole dump ({} rows) \
             rather than expiring every other underlying",
            iso(as_of),
            records.len()
        ))));
    }
    let mut out = MasterReports {
        by_underlying: BTreeMap::new(),
        delisted: Vec::new(),
    };
    for (underlying, tonight) in &grouped {
        let report = refresh_master(conn, tonight, as_of, underlying).await?;
        out.by_underlying.insert(underlying.clone(), report);
    }
    let live: Vec<(String,)> =
        sqlx::query_as("SELECT DISTINCT op_contract.underlying FROM op_contract WHERE op_contract.expired IS false")
            .fetch_all(&mut *conn)
            .await?;
    let gone: Vec<String> = live
        .into_iter()
        .map(|(u,)| u)
        .filter(|u| !grouped.contains_key(u))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    if !gone.is_empty() {
        sqlx::query(
            "UPDATE op_contract SET expired=true \
             WHERE op_contract.underlying = ANY($1) AND op_contract.expired IS false",
        )
        .bind(&gone)
        .execute(&mut *conn)
        .await?;
        out.delisted = gone;
    }
    Ok(Ok(out))
}

/// `refresh_master(session, records, as_of=..., underlying=...)` for one underlying's rows.
async fn refresh_master(
    conn: &mut PgConnection,
    tonight: &[&OptionContractRecord],
    as_of: NaiveDate,
    underlying: &str,
) -> Result<MasterReport, WorkerError> {
    let mut report = MasterReport::new(as_of);
    report.contracts_seen = tonight.len();
    write_contracts(conn, tonight, as_of, underlying, &mut report).await?;
    rebuild_expiries(conn, tonight, as_of, underlying, &mut report).await?;
    Ok(report)
}

async fn write_contracts(
    conn: &mut PgConnection,
    tonight: &[&OptionContractRecord],
    as_of: NaiveDate,
    underlying: &str,
    report: &mut MasterReport,
) -> Result<(), WorkerError> {
    let known: BTreeSet<i64> = sqlx::query_scalar(
        "SELECT op_contract.instrument_token FROM op_contract WHERE op_contract.underlying = $1",
    )
    .bind(underlying)
    .fetch_all(&mut *conn)
    .await?
    .into_iter()
    .collect();
    let tokens: BTreeSet<i64> = tonight.iter().map(|r| r.instrument_token).collect();
    report.contracts_new = tokens.difference(&known).count();
    for batch in tonight.chunks(1000) {
        let mut q: QueryBuilder<'_, Postgres> = QueryBuilder::new(
            "INSERT INTO op_contract (instrument_token, tradingsymbol, underlying, expiry, strike, \
             option_type, lot_size, tick_size, first_seen, last_seen, expired) ",
        );
        let mut bad_lot: Option<i64> = None;
        q.push_values(batch.iter(), |mut b, r| {
            let lot = i32::try_from(r.lot_size).unwrap_or_else(|_| {
                bad_lot = Some(r.lot_size);
                0
            });
            b.push_bind(r.instrument_token)
                .push_bind(r.tradingsymbol.clone())
                .push_bind(r.underlying.clone())
                .push_bind(r.expiry)
                .push_bind(r.strike)
                .push_bind(record_type(r.option_type))
                .push_bind(lot)
                .push_bind(r.tick_size)
                .push_bind(as_of)
                .push_bind(as_of)
                .push_bind(false);
        });
        if let Some(lot) = bad_lot {
            return Err(WorkerError::python(
                "DataError",
                format!("lot_size {lot} is out of range for type integer"),
            ));
        }
        q.push(
            " ON CONFLICT (instrument_token) DO UPDATE SET tradingsymbol = excluded.tradingsymbol, \
             lot_size = excluded.lot_size, tick_size = excluded.tick_size, \
             last_seen = greatest(op_contract.last_seen, excluded.last_seen), expired = false",
        );
        q.build().execute(&mut *conn).await?;
    }
    let gone: Vec<i64> = known.difference(&tokens).copied().collect();
    if !gone.is_empty() {
        let done = sqlx::query(
            "UPDATE op_contract SET expired=true WHERE op_contract.underlying = $1 \
             AND op_contract.instrument_token = ANY($2) AND op_contract.expired IS false",
        )
        .bind(underlying)
        .bind(&gone)
        .execute(&mut *conn)
        .await?;
        report.contracts_expired = done.rows_affected();
    }
    Ok(())
}

/// One stored `op_expiry` row.
struct ExpiryRow {
    expiry: NaiveDate,
    kind: String,
    lot_size: i64,
    seen_on: NaiveDate,
    detail: Vec<(String, OrderedJson)>,
}

fn has_withdrawn(detail: &[(String, OrderedJson)]) -> bool {
    detail.iter().any(|(k, _)| k == "withdrawn_on")
}

/// `_with_history(detail, changes, as_of, restored=...)`.
fn with_history(
    detail: &[(String, OrderedJson)],
    changes: &[MasterChange],
    as_of: NaiveDate,
    restored: bool,
) -> Vec<(String, OrderedJson)> {
    let mut entries: Vec<OrderedJson> = match detail.iter().find(|(k, _)| k == "history") {
        Some((_, OrderedJson::Array(items))) => items.clone(),
        _ => Vec::new(),
    };
    for c in changes {
        let OrderedJson::Object(mut pairs) = c.as_dict() else {
            continue;
        };
        crate::pyjson::update(&mut pairs, "on", s(iso(as_of)));
        entries.push(OrderedJson::Object(pairs));
    }
    let mut out: Vec<(String, OrderedJson)> = detail
        .iter()
        .filter(|(k, _)| !(restored && k == "withdrawn_on"))
        .cloned()
        .collect();
    crate::pyjson::update(&mut out, "history", OrderedJson::Array(entries));
    out
}

fn detail_text(detail: Vec<(String, OrderedJson)>) -> Result<String, WorkerError> {
    OrderedJson::Object(detail)
        .to_python_json()
        .map_err(|e| WorkerError::python("ValueError", e.to_string()))
}

async fn rebuild_expiries(
    conn: &mut PgConnection,
    tonight: &[&OptionContractRecord],
    as_of: NaiveDate,
    underlying: &str,
    report: &mut MasterReport,
) -> Result<(), WorkerError> {
    // `expiry_lot_sizes`: each expiry's lot sizes; the larger is kept, two is "ambiguous".
    let mut lots: BTreeMap<NaiveDate, BTreeSet<i64>> = BTreeMap::new();
    for r in tonight {
        lots.entry(r.expiry).or_default().insert(r.lot_size);
    }
    let listed: Vec<NaiveDate> = lots.keys().copied().collect();
    let raw: Vec<(NaiveDate, String, i32, NaiveDate, String)> = sqlx::query_as(
        "SELECT op_expiry.expiry_date, op_expiry.kind, op_expiry.lot_size, op_expiry.seen_on, \
         op_expiry.detail::text FROM op_expiry WHERE op_expiry.underlying = $1",
    )
    .bind(underlying)
    .fetch_all(&mut *conn)
    .await?;
    let mut existing: Vec<ExpiryRow> = Vec::with_capacity(raw.len());
    for (expiry, kind, lot_size, seen_on, detail) in raw {
        let detail = match OrderedJson::parse(&detail)
            .map_err(|e| WorkerError::python("ValueError", e.to_string()))?
        {
            OrderedJson::Object(pairs) => pairs,
            _ => Vec::new(),
        };
        existing.push(ExpiryRow {
            expiry,
            kind,
            lot_size: i64::from(lot_size),
            seen_on,
            detail,
        });
    }
    report.expiries = lots.len();
    for (expiry, sizes) in &lots {
        let kind_now: ExpiryKind = kind_in(&listed, *expiry, underlying)?;
        let lot_now = sizes.iter().copied().max().unwrap_or(0);
        let Some(row) = existing.iter().find(|r| r.expiry == *expiry) else {
            let detail = if sizes.len() > 1 {
                vec![("ambiguous_lot_size".to_owned(), s(iso(as_of)))]
            } else {
                Vec::new()
            };
            sqlx::query(
                "INSERT INTO op_expiry (underlying, expiry_date, kind, lot_size, first_seen, \
                 seen_on, detail) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)",
            )
            .bind(underlying)
            .bind(*expiry)
            .bind(kind_now.value())
            .bind(i32::try_from(lot_now).unwrap_or(i32::MAX))
            .bind(as_of)
            .bind(as_of)
            .bind(detail_text(detail)?)
            .execute(&mut *conn)
            .await?;
            continue;
        };
        let mut changes: Vec<MasterChange> = Vec::new();
        if row.lot_size != lot_now {
            changes.push(MasterChange {
                expiry: *expiry,
                what: "LOT_SIZE",
                before: Some(row.lot_size.to_string()),
                after: Some(lot_now.to_string()),
            });
        }
        if row.kind != kind_now.value() {
            changes.push(MasterChange {
                expiry: *expiry,
                what: "KIND",
                before: Some(row.kind.clone()),
                after: Some(kind_now.value().to_owned()),
            });
        }
        let seen_on = row.seen_on.max(as_of);
        if !changes.is_empty() || has_withdrawn(&row.detail) {
            let restored = has_withdrawn(&row.detail);
            let detail = with_history(&row.detail, &changes, as_of, restored);
            sqlx::query(
                "UPDATE op_expiry SET kind=$1, lot_size=$2, seen_on=$3, detail=$4::jsonb \
                 WHERE op_expiry.underlying = $5 AND op_expiry.expiry_date = $6",
            )
            .bind(kind_now.value())
            .bind(i32::try_from(lot_now).unwrap_or(i32::MAX))
            .bind(seen_on)
            .bind(detail_text(detail)?)
            .bind(underlying)
            .bind(*expiry)
            .execute(&mut *conn)
            .await?;
            report.changes.extend(changes);
        } else if seen_on != row.seen_on {
            sqlx::query(
                "UPDATE op_expiry SET seen_on=$1 \
                 WHERE op_expiry.underlying = $2 AND op_expiry.expiry_date = $3",
            )
            .bind(seen_on)
            .bind(underlying)
            .bind(*expiry)
            .execute(&mut *conn)
            .await?;
        }
    }
    for row in &existing {
        if lots.contains_key(&row.expiry) || row.expiry < as_of || has_withdrawn(&row.detail) {
            continue;
        }
        let change = MasterChange {
            expiry: row.expiry,
            what: "WITHDRAWN",
            before: Some(row.kind.clone()),
            after: None,
        };
        let mut detail = with_history(&row.detail, std::slice::from_ref(&change), as_of, false);
        crate::pyjson::update(&mut detail, "withdrawn_on", s(iso(as_of)));
        sqlx::query(
            "UPDATE op_expiry SET detail=$1::jsonb \
             WHERE op_expiry.underlying = $2 AND op_expiry.expiry_date = $3",
        )
        .bind(detail_text(detail)?)
        .bind(underlying)
        .bind(row.expiry)
        .execute(&mut *conn)
        .await?;
        report.changes.push(change);
    }
    Ok(())
}

/// `master_alert(report)`: `OPTIONS_MASTER_CHANGED` when anything changed (runbook `""`, as
/// Python's constructor leaves it).
pub(crate) fn master_alert(report: &MasterReport) -> Option<OptionsAlert> {
    if report.changes.is_empty() {
        return None;
    }
    let lines: Vec<String> = report
        .changes
        .iter()
        .map(|c| {
            format!(
                "{} {} {}->{}",
                iso(c.expiry),
                c.what,
                c.before.as_deref().unwrap_or("None"),
                c.after.as_deref().unwrap_or("None")
            )
        })
        .collect();
    Some(OptionsAlert {
        name: OptionsName::MasterChanged,
        severity: Level::Warning,
        summary: format!(
            "The NIFTY options master changed on {}: {}. Sizing and each sleeve's day role read \
             op_expiry; check it before the open.",
            iso(report.as_of),
            lines.join(", ")
        ),
        labels: vec![("trade_date".to_owned(), iso(report.as_of))],
        detail: OrderedJson::Object(report.pairs()),
        runbook: "",
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn day(m: u32, d: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, m, d).unwrap()
    }

    #[test]
    fn history_appends_and_a_restore_drops_withdrawn_on() {
        let detail = vec![
            ("withdrawn_on".to_owned(), s("2026-09-20")),
            ("history".to_owned(), OrderedJson::Array(vec![s("old")])),
        ];
        let change = MasterChange {
            expiry: day(9, 29),
            what: "LOT_SIZE",
            before: Some("75".to_owned()),
            after: Some("65".to_owned()),
        };
        let out = with_history(&detail, std::slice::from_ref(&change), day(9, 22), true);
        assert!(!has_withdrawn(&out));
        let Some((_, OrderedJson::Array(items))) = out.iter().find(|(k, _)| k == "history") else {
            unreachable!("history is set");
        };
        assert_eq!(items.len(), 2);
        let kept = with_history(&detail, &[], day(9, 22), false);
        assert!(has_withdrawn(&kept));
    }

    #[test]
    fn an_ordinary_night_raises_nothing() {
        let report = MasterReport::new(day(9, 22));
        assert!(master_alert(&report).is_none());
    }

    #[test]
    fn a_withdrawal_is_alerted_with_pythons_none() {
        let mut report = MasterReport::new(day(9, 22));
        report.changes.push(MasterChange {
            expiry: day(9, 29),
            what: "WITHDRAWN",
            before: Some("WEEKLY".to_owned()),
            after: None,
        });
        let alert = master_alert(&report).unwrap();
        assert!(alert.summary.contains("2026-09-29 WITHDRAWN WEEKLY->None"));
        assert_eq!(alert.runbook, "");
    }
}
