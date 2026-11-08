//! The options book's weekly summary: `OPTIONS_WEEKLY` (`options.weekly`, OP11) — the week's
//! journal rows and skipped sessions, one line per `(sleeve, simulated, sizing_mode)`, never a
//! pooled number. Read-only: writes nothing and reaches no broker.

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_db::OrderedJson;
use chrono::{Duration, NaiveDate};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::alerting::{Level, OptionsAlert, OptionsName, RUNBOOK_PLAN};
use super::common::iso;
use super::model::{
    JournalRow, SizingMode, Summary, group_value, sleeve_of, sleeve_value, summarize,
};
use crate::error::WorkerError;
use crate::pyjson::{object, s};

const WINDOW_DAYS: i64 = 7;

/// `week_rows(session, user_id, today)`: the week's traded rows and skipped sessions.
pub(crate) async fn week_rows(
    conn: &mut PgConnection,
    user_id: i64,
    today: NaiveDate,
) -> Result<Vec<JournalRow>, WorkerError> {
    let since = today - Duration::days(WINDOW_DAYS - 1);
    let traded: Vec<(String, NaiveDate, bool, String, Decimal, Decimal)> = sqlx::query_as(
        "SELECT op_journal.sleeve::text, op_journal.trade_date, op_journal.simulated, \
         op_journal.sizing_mode, op_journal.net_pnl_inr, op_journal.risk_budget_inr \
         FROM op_journal WHERE op_journal.user_id = $1 \
         AND op_journal.trade_date BETWEEN $2 AND $3",
    )
    .bind(user_id)
    .bind(since)
    .bind(today)
    .fetch_all(&mut *conn)
    .await?;
    let mut rows = Vec::with_capacity(traded.len());
    for (sleeve, trade_date, simulated, sizing, pnl, risk) in traded {
        rows.push(JournalRow {
            sleeve: sleeve_of(&sleeve)?,
            trade_date,
            simulated,
            sizing_mode: SizingMode::of(&sizing)?,
            traded: true,
            skip_reason: None,
            net_pnl_inr: WideDecimal::from(pnl),
            r_inr: WideDecimal::from(risk),
        });
    }
    let capital: Vec<(String, Decimal)> = sqlx::query_as(
        "SELECT op_sleeve_config.sleeve, op_sleeve_config.sleeve_capital_inr \
         FROM op_sleeve_config WHERE op_sleeve_config.user_id = $1",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    let skipped: Vec<(String, NaiveDate, String, Vec<String>)> = sqlx::query_as(
        "SELECT op_session.sleeve::text, op_session.trade_date, op_session.mode, \
         op_session.skip_reasons::text[] FROM op_session WHERE op_session.user_id = $1 \
         AND op_session.trade_date BETWEEN $2 AND $3 AND op_session.state = 'SKIPPED'",
    )
    .bind(user_id)
    .bind(since)
    .bind(today)
    .fetch_all(&mut *conn)
    .await?;
    for (sleeve, trade_date, mode, reasons) in skipped {
        let sleeve = sleeve_of(&sleeve)?;
        // `{c.sleeve: ...}`: a repeated key keeps its last value.
        let funded = capital
            .iter()
            .rev()
            .find(|(k, _)| k == group_value(sleeve))
            .is_some_and(|(_, v)| *v > Decimal::ZERO);
        rows.push(JournalRow {
            sleeve,
            trade_date,
            simulated: mode == "PAPER",
            sizing_mode: if funded {
                SizingMode::Budget
            } else {
                SizingMode::PaperOneLot
            },
            traded: false,
            skip_reason: Some(
                reasons
                    .first()
                    .cloned()
                    .unwrap_or_else(|| "UNKNOWN".to_owned()),
            ),
            net_pnl_inr: WideDecimal::zero(),
            r_inr: WideDecimal::from(1_i64),
        });
    }
    Ok(rows)
}

/// `format(d, ".2f")`: half even, the sign kept.
fn fmt_2f(d: &WideDecimal) -> String {
    d.quantize(-2, Rounding::HalfEven)
        .map_or_else(|_| d.to_string(), |q| q.to_string())
}

/// `format(d, ".0%")`: ×100, half even to a whole number, then `%`.
fn fmt_pct0(d: &WideDecimal) -> String {
    let hundred = d.mul_exact(&WideDecimal::from(100_i64));
    let whole = hundred
        .quantize(0, Rounding::HalfEven)
        .map_or_else(|_| hundred.to_string(), |q| q.to_string());
    format!("{whole}%")
}

/// `_line(summary)`.
fn line(summary: &Summary) -> String {
    let key = summary.key;
    let kind = if key.simulated { "paper" } else { "LIVE" };
    let mut head = format!(
        "{} {kind} {}: {} traded",
        sleeve_value(key.sleeve),
        key.sizing_mode.value(),
        summary.traded
    );
    let skipped: i64 = summary.skipped_by_reason.iter().map(|(_, n)| n).sum();
    if skipped != 0 {
        head.push_str(&format!(", {skipped} skipped"));
    }
    if summary.traded != 0 {
        let or_none = |d: Option<&WideDecimal>, f: fn(&WideDecimal) -> String| {
            d.map_or_else(|| "None".to_owned(), f)
        };
        head.push_str(&format!(
            "; win {}, mean {}R, worst {}R, drawdown {}R",
            or_none(summary.win_rate.as_ref(), fmt_pct0),
            or_none(summary.mean_r.as_ref(), fmt_2f),
            or_none(summary.worst_r.as_ref(), fmt_2f),
            fmt_2f(&summary.max_drawdown_r)
        ));
    }
    head
}

/// `weekly_alert(rows, today)`: one alert, one line per pool; a week with nothing says so.
pub(crate) fn weekly_alert(
    rows: &[JournalRow],
    today: NaiveDate,
) -> Result<OptionsAlert, WorkerError> {
    let summaries = summarize(rows)?;
    let mut lines: Vec<String> = summaries.iter().map(line).collect();
    if lines.is_empty() {
        lines.push("no options session was decided this week".to_owned());
    }
    let pools = OrderedJson::Array(
        summaries
            .iter()
            .map(|sm| {
                object([
                    ("sleeve", s(sleeve_value(sm.key.sleeve))),
                    ("simulated", OrderedJson::Bool(sm.key.simulated)),
                    ("sizing_mode", s(sm.key.sizing_mode.value())),
                    (
                        "traded",
                        OrderedJson::Int(i64::try_from(sm.traded).unwrap_or(i64::MAX)),
                    ),
                    (
                        "skipped",
                        object(
                            sm.skipped_by_reason
                                .iter()
                                .map(|(k, n)| (k.clone(), OrderedJson::Int(*n))),
                        ),
                    ),
                    (
                        "mean_r",
                        sm.mean_r
                            .as_ref()
                            .map_or(OrderedJson::Null, |d| s(d.to_sci_string())),
                    ),
                    (
                        "worst_r",
                        sm.worst_r
                            .as_ref()
                            .map_or(OrderedJson::Null, |d| s(d.to_sci_string())),
                    ),
                ])
            })
            .collect(),
    );
    Ok(OptionsAlert {
        name: OptionsName::Weekly,
        severity: Level::Warning,
        summary: format!("Options week to {} — {}.", iso(today), lines.join("; ")),
        labels: vec![
            ("week_to".to_owned(), iso(today)),
            ("pools".to_owned(), summaries.len().to_string()),
        ],
        detail: object([("pools", pools)]),
        runbook: RUNBOOK_PLAN,
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use baskfy_core::options::gating::Sleeve;

    fn w(text: &str) -> WideDecimal {
        text.parse().unwrap()
    }

    fn row(sleeve: Sleeve, simulated: bool, pnl: Option<&str>) -> JournalRow {
        JournalRow {
            sleeve,
            trade_date: NaiveDate::from_ymd_opt(2026, 10, 27).unwrap(),
            simulated,
            sizing_mode: SizingMode::PaperOneLot,
            traded: pnl.is_some(),
            skip_reason: pnl.is_none().then(|| "NO_TRIGGER".to_owned()),
            net_pnl_inr: w(pnl.unwrap_or("0")),
            r_inr: w("1000"),
        }
    }

    #[test]
    fn one_line_per_pool_never_pooled() {
        let rows = [
            row(Sleeve::O2, true, Some("500")),
            row(Sleeve::O2, true, Some("-250")),
            row(Sleeve::O2, true, None),
            row(Sleeve::O1m, true, None),
        ];
        let alert = weekly_alert(&rows, NaiveDate::from_ymd_opt(2026, 10, 30).unwrap()).unwrap();
        assert_eq!(
            alert.summary,
            "Options week to 2026-10-30 — O1M paper PAPER_ONE_LOT: 0 traded, 1 skipped; O2 paper \
             PAPER_ONE_LOT: 2 traded, 1 skipped; win 50%, mean 0.12R, worst -0.25R, drawdown \
             0.25R."
        );
        assert_eq!(alert.labels[1], ("pools".to_owned(), "2".to_owned()));
    }

    #[test]
    fn a_week_with_nothing_says_so() {
        let alert = weekly_alert(&[], NaiveDate::from_ymd_opt(2026, 10, 30).unwrap()).unwrap();
        assert_eq!(
            alert.summary,
            "Options week to 2026-10-30 — no options session was decided this week."
        );
    }

    #[test]
    fn decimal_formats_round_half_even() {
        assert_eq!(fmt_2f(&w("0.125")), "0.12");
        assert_eq!(fmt_2f(&w("-0.001")), "-0.00");
        assert_eq!(fmt_pct0(&w("0.665")), "66%");
        assert_eq!(fmt_pct0(&w("0.5")), "50%");
    }
}
