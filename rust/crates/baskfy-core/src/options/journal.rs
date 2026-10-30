//! Port of `baskfy_core.options.journal`: the journal in R, summarised per sleeve and never pooled (`04` §12).

use std::collections::BTreeMap;

use chrono::{Datelike, NaiveDate};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::config::{ExpiryKind, SizingMode, Sleeve};
use crate::options::num;

/// `net_pnl_inr / R`; R must be positive.
pub fn r_multiple(net_pnl_inr: Decimal, r_inr: Decimal) -> CoreResult<Decimal> {
    if r_inr <= Decimal::ZERO {
        return Err(CoreError::InvalidInput("R must be positive".to_owned()));
    }
    num::div(net_pnl_inr, r_inr)
}

/// One session's record; `traded == false` rows are the skips.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JournalRow {
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The trade date.
    pub trade_date: NaiveDate,
    /// Paper.
    pub simulated: bool,
    /// How it was sized.
    pub sizing_mode: SizingMode,
    /// Whether it traded.
    pub traded: bool,
    /// Why it skipped.
    pub skip_reason: Option<String>,
    /// The expiry's kind.
    pub expiry_kind: Option<ExpiryKind>,
    /// Net P&L.
    pub net_pnl_inr: Decimal,
    /// R.
    pub r_inr: Decimal,
    /// The exit code.
    pub closed_reason: Option<String>,
    /// Minutes held.
    pub minutes_held: i64,
    /// MAE in R.
    pub mae_r: Decimal,
    /// MFE in R.
    pub mfe_r: Decimal,
}

impl JournalRow {
    /// A row with Python's defaults for every optional field.
    pub fn new(
        sleeve: Sleeve,
        trade_date: NaiveDate,
        simulated: bool,
        sizing_mode: SizingMode,
        traded: bool,
    ) -> Self {
        Self {
            sleeve,
            trade_date,
            simulated,
            sizing_mode,
            traded,
            skip_reason: None,
            expiry_kind: None,
            net_pnl_inr: Decimal::ZERO,
            r_inr: Decimal::ONE,
            closed_reason: None,
            minutes_held: 0,
            mae_r: Decimal::ZERO,
            mfe_r: Decimal::ZERO,
        }
    }

    /// The row's R multiple.
    pub fn r(&self) -> CoreResult<Decimal> {
        r_multiple(self.net_pnl_inr, self.r_inr)
    }
}

/// The three things a summary never pools.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct PoolKey {
    /// The sleeve.
    pub sleeve: Sleeve,
    /// Paper.
    pub simulated: bool,
    /// How it was sized.
    pub sizing_mode: SizingMode,
}

/// A row's pool key.
pub fn key_of(row: &JournalRow) -> PoolKey {
    PoolKey {
        sleeve: row.sleeve,
        simulated: row.simulated,
        sizing_mode: row.sizing_mode,
    }
}

/// A split of traded rows: how many, and their mean R.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Bucket {
    /// Rows.
    pub count: i64,
    /// Mean R.
    pub mean_r: Decimal,
}

/// The `04` §12 summary for one key.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Summary {
    /// The key.
    pub key: PoolKey,
    /// Rows.
    pub count: i64,
    /// Traded rows.
    pub traded: i64,
    /// Skips by reason, sorted.
    pub skipped_by_reason: Vec<(String, i64)>,
    /// Winners over traded.
    pub win_rate: Option<Decimal>,
    /// Mean R.
    pub mean_r: Option<Decimal>,
    /// Mean net rupees.
    pub expectancy_inr: Option<Decimal>,
    /// Mean R.
    pub expectancy_r: Option<Decimal>,
    /// Worst R.
    pub worst_r: Option<Decimal>,
    /// Largest drawdown in R.
    pub max_drawdown_r: Decimal,
    /// Largest drawdown in rupees.
    pub max_drawdown_inr: Decimal,
    /// MAE in R, sorted.
    pub mae_r: Vec<Decimal>,
    /// MFE in R, sorted.
    pub mfe_r: Vec<Decimal>,
    /// Mean minutes held.
    pub mean_minutes_held: Option<Decimal>,
    /// By exit code.
    pub by_closed_reason: Vec<(String, Bucket)>,
    /// By expiry kind (O1/O3).
    pub by_expiry_kind: Vec<(String, Bucket)>,
    /// By ISO weekday (O2).
    pub by_weekday: Vec<(i64, Bucket)>,
}

/// The largest peak-to-trough fall of the running sum, from a peak of 0.
pub fn max_drawdown(values: &[Decimal]) -> CoreResult<Decimal> {
    let (mut peak, mut running, mut worst) = (Decimal::ZERO, Decimal::ZERO, Decimal::ZERO);
    for v in values {
        running = num::add(running, *v)?;
        peak = num::max(peak, running);
        worst = num::max(worst, num::sub(peak, running)?);
    }
    Ok(worst)
}

fn count(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

fn mean(values: &[Decimal]) -> CoreResult<Option<Decimal>> {
    if values.is_empty() {
        return Ok(None);
    }
    Ok(Some(num::div(
        num::sum(values.iter().copied())?,
        Decimal::from(count(values.len())),
    )?))
}

fn buckets<K: Clone + Ord + ToString>(
    rs: &[Decimal],
    label: &BTreeMap<usize, K>,
) -> CoreResult<Vec<(K, Bucket)>> {
    let mut grouped: Vec<(K, Vec<Decimal>)> = Vec::new();
    for (i, r) in rs.iter().enumerate() {
        if let Some(k) = label.get(&i) {
            match grouped.iter_mut().find(|(g, _)| g == k) {
                Some((_, v)) => v.push(*r),
                None => grouped.push((k.clone(), vec![*r])),
            }
        }
    }
    grouped.sort_by_key(|(k, _)| k.to_string());
    grouped
        .into_iter()
        .map(|(k, v)| {
            let m = num::div(num::sum(v.iter().copied())?, Decimal::from(count(v.len())))?;
            Ok((
                k,
                Bucket {
                    count: count(v.len()),
                    mean_r: m,
                },
            ))
        })
        .collect()
}

/// The summary for rows of one key; an error (`PooledRows`) otherwise.
pub fn summarize_one(rows: &[JournalRow]) -> CoreResult<Summary> {
    let mut keys: Vec<PoolKey> = rows.iter().map(key_of).collect();
    keys.sort();
    keys.dedup();
    if keys.len() != 1 {
        return Err(CoreError::InvalidInput(format!(
            "one summary per (sleeve, simulated, sizing_mode); got {} keys",
            keys.len()
        )));
    }
    let key = keys[0];
    let mut traded: Vec<&JournalRow> = rows.iter().filter(|r| r.traded).collect();
    traded.sort_by_key(|r| r.trade_date);
    let mut skipped: BTreeMap<String, i64> = BTreeMap::new();
    for row in rows.iter().filter(|r| !r.traded) {
        let reason = row
            .skip_reason
            .clone()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "UNSPECIFIED".to_owned());
        *skipped.entry(reason).or_insert(0) += 1;
    }
    let rs: Vec<Decimal> = traded.iter().map(|r| r.r()).collect::<CoreResult<_>>()?;
    let wins = count(rs.iter().filter(|r| **r > Decimal::ZERO).count());
    let mut kinds = BTreeMap::new();
    let mut weekdays = BTreeMap::new();
    let mut reasons = BTreeMap::new();
    for (i, row) in traded.iter().enumerate() {
        if key.sleeve != Sleeve::O2
            && let Some(k) = row.expiry_kind
        {
            kinds.insert(i, k.value().to_owned());
        }
        if key.sleeve == Sleeve::O2 {
            weekdays.insert(i, i64::from(row.trade_date.weekday().number_from_monday()));
        }
        reasons.insert(
            i,
            row.closed_reason
                .clone()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "UNSPECIFIED".to_owned()),
        );
    }
    let nets: Vec<Decimal> = traded.iter().map(|r| r.net_pnl_inr).collect();
    let minutes: Vec<Decimal> = traded
        .iter()
        .map(|r| Decimal::from(r.minutes_held))
        .collect();
    let mut mae: Vec<Decimal> = traded.iter().map(|r| r.mae_r).collect();
    mae.sort();
    let mut mfe: Vec<Decimal> = traded.iter().map(|r| r.mfe_r).collect();
    mfe.sort();
    let win_rate = if rs.is_empty() {
        None
    } else {
        Some(num::div(
            Decimal::from(wins),
            Decimal::from(count(rs.len())),
        )?)
    };
    Ok(Summary {
        key,
        count: count(rows.len()),
        traded: count(traded.len()),
        skipped_by_reason: skipped.into_iter().collect(),
        win_rate,
        mean_r: mean(&rs)?,
        expectancy_inr: mean(&nets)?,
        expectancy_r: mean(&rs)?,
        worst_r: rs.iter().copied().reduce(num::min),
        max_drawdown_r: max_drawdown(&rs)?,
        max_drawdown_inr: max_drawdown(&nets)?,
        mae_r: mae,
        mfe_r: mfe,
        mean_minutes_held: mean(&minutes)?,
        by_closed_reason: buckets(&rs, &reasons)?,
        by_expiry_kind: buckets(&rs, &kinds)?,
        by_weekday: buckets(&rs, &weekdays)?,
    })
}

/// One summary per `(sleeve, simulated, sizing_mode)` present, in a stable order.
pub fn summarize(rows: &[JournalRow]) -> CoreResult<Vec<Summary>> {
    let mut grouped: BTreeMap<(&'static str, bool, &'static str), Vec<JournalRow>> =
        BTreeMap::new();
    for row in rows {
        let k = key_of(row);
        grouped
            .entry((k.sleeve.value(), k.simulated, k.sizing_mode.value()))
            .or_default()
            .push(row.clone());
    }
    grouped.values().map(|g| summarize_one(g)).collect()
}
