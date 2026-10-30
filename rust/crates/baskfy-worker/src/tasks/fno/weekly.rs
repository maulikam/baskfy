//! Ports `baskfy_worker.fno.weekly`: `FNO_WEEKLY`, the FO book's week per FO tenant — one line
//! per `(sleeve, simulated)` closed in the week (never pooled), the month per book, the pauses
//! per mode. Read-only; reaches no broker.

use std::collections::BTreeMap;

use baskfy_core::fno::ledger::{ClosedTrade, FoPause, Ledger};
use baskfy_db::OrderedJson;
use chrono::{Days, NaiveDate};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::alerting::{FnoAlert, FnoName, Level};
use super::common::{dstr, iso};
use super::ledger::{load_trades, read_ledger, read_pauses};
use crate::error::WorkerError;
use crate::pyjson::{object, s};

/// `WINDOW_DAYS`.
pub(crate) const WINDOW_DAYS: u64 = 7;

fn mode(simulated: bool) -> &'static str {
    if simulated { "paper" } else { "LIVE" }
}

/// `week_lines`: one line per `(sleeve, simulated)` closed in the week to `today`.
pub(crate) fn week_lines(trades: &[ClosedTrade], today: NaiveDate) -> Vec<String> {
    let since = today
        .checked_sub_days(Days::new(WINDOW_DAYS - 1))
        .unwrap_or(today);
    let mut pools: BTreeMap<(bool, &'static str), Vec<&ClosedTrade>> = BTreeMap::new();
    for t in trades {
        if since <= t.closed_on && t.closed_on <= today {
            pools
                .entry((!t.simulated, t.sleeve.value()))
                .or_default()
                .push(t);
        }
    }
    pools
        .into_iter()
        .map(|((live, sleeve), rows)| {
            let net = rows
                .iter()
                .fold(Decimal::ZERO, |acc, t| acc + t.net_pnl_inr);
            let r = rows.iter().fold(Decimal::ZERO, |acc, t| acc + t.r_multiple);
            let worst = rows
                .iter()
                .map(|t| t.r_multiple)
                .reduce(|a, b| if b < a { b } else { a })
                .unwrap_or(Decimal::ZERO);
            format!(
                "{sleeve} {}: {} closed, ₹{}, {}R (worst {}R)",
                mode(!live),
                rows.len(),
                dstr(net),
                dstr(r),
                dstr(worst)
            )
        })
        .collect()
}

/// `weekly_alert`: the week per pool, the month per book, the pauses per mode.
pub(crate) fn weekly_alert(
    trades: &[ClosedTrade],
    ledger: &Ledger,
    pauses: &[(bool, Vec<FoPause>)],
    today: NaiveDate,
) -> FnoAlert {
    let mut lines = week_lines(trades, today);
    if lines.is_empty() {
        lines.push("no FO structure closed this week".to_owned());
    }
    for (simulated, book) in &ledger.books {
        lines.push(format!(
            "{} book month to date: {} closed, ₹{}, {} open marked ₹{}",
            mode(*simulated),
            book.mtd_closed,
            dstr(book.mtd_realised_inr),
            book.open_positions,
            dstr(book.open_marked_inr)
        ));
    }
    let of = |simulated: bool| -> &[FoPause] {
        pauses
            .iter()
            .find(|(s, _)| *s == simulated)
            .map_or(&[], |(_, p)| p.as_slice())
    };
    for simulated in [true, false] {
        for p in of(simulated) {
            lines.push(format!(
                "{} pause {}: {}",
                mode(simulated),
                p.scope,
                p.message
            ));
        }
    }
    let books = OrderedJson::Array(
        ledger
            .books
            .iter()
            .map(|(simulated, book)| {
                object([
                    ("simulated", OrderedJson::Bool(*simulated)),
                    ("mtd_closed", OrderedJson::Int(book.mtd_closed)),
                    ("mtd_realised_inr", s(dstr(book.mtd_realised_inr))),
                    ("open_positions", OrderedJson::Int(book.open_positions)),
                    ("open_marked_inr", s(dstr(book.open_marked_inr))),
                    ("max_drawdown_r", s(dstr(book.max_drawdown_r))),
                ])
            })
            .collect(),
    );
    let mut paused = Vec::new();
    for simulated in [true, false] {
        for p in of(simulated) {
            paused.push(object([
                ("simulated", OrderedJson::Bool(simulated)),
                ("scope", s(p.scope.clone())),
                ("code", s(p.code.value())),
            ]));
        }
    }
    FnoAlert {
        name: FnoName::Weekly,
        severity: Level::Warning,
        summary: format!("FO week to {} — {}.", iso(today), lines.join("; ")),
        labels: vec![("week_to".to_owned(), iso(today))],
        detail: object([("books", books), ("pauses", OrderedJson::Array(paused))]),
    }
}

/// `build_weekly`: the week's alert for one tenant, from the database (read-only).
pub(crate) async fn build_weekly(
    conn: &mut PgConnection,
    user_id: i64,
    today: NaiveDate,
) -> Result<FnoAlert, WorkerError> {
    let trades = load_trades(conn, user_id).await?;
    let ledger = read_ledger(conn, user_id, today).await?;
    let mut pauses = Vec::new();
    for simulated in [true, false] {
        pauses.push((
            simulated,
            read_pauses(conn, user_id, today, simulated).await?,
        ));
    }
    Ok(weekly_alert(&trades, &ledger, &pauses, today))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use baskfy_core::fno::config::FoSleeve;

    use super::*;

    fn trade(sleeve: FoSleeve, simulated: bool, on: NaiveDate, net: &str, r: &str) -> ClosedTrade {
        ClosedTrade {
            position_id: 1,
            sleeve,
            symbol: "NIFTY".to_owned(),
            simulated,
            closed_on: on,
            closed_at: None,
            net_pnl_inr: net.parse().unwrap(),
            r_multiple: r.parse().unwrap(),
            closed_reason: "PROFIT_TAKE".to_owned(),
            rolls: 0,
        }
    }

    /// `test_fno_weekly.py`: paper before LIVE, by sleeve; never pooled; outside the week out.
    #[test]
    fn a_line_per_pool_paper_first() {
        let day = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap();
        let old = NaiveDate::from_ymd_opt(2026, 10, 23).unwrap();
        let trades = vec![
            trade(FoSleeve::F2, false, day, "-100.00", "-0.50"),
            trade(FoSleeve::F1N, true, day, "250.50", "0.25"),
            trade(FoSleeve::F1N, true, day, "-50.00", "-0.10"),
            trade(FoSleeve::F1B, true, old, "1.00", "0.01"),
        ];
        assert_eq!(
            week_lines(&trades, day),
            vec![
                "F1N paper: 2 closed, ₹200.50, 0.15R (worst -0.10R)".to_owned(),
                "F2 LIVE: 1 closed, ₹-100.00, -0.50R (worst -0.50R)".to_owned(),
            ]
        );
    }
    fn closed(
        n: i64,
        sleeve: FoSleeve,
        r: &str,
        net: &str,
        simulated: bool,
        on: NaiveDate,
    ) -> ClosedTrade {
        ClosedTrade {
            position_id: n,
            sleeve,
            symbol: if sleeve == FoSleeve::F1N {
                "NIFTY"
            } else {
                "ZQA"
            }
            .to_owned(),
            simulated,
            closed_on: on,
            closed_at: None,
            net_pnl_inr: net.parse().unwrap(),
            r_multiple: r.parse().unwrap(),
            closed_reason: "LOSS_CLOSE".to_owned(),
            rolls: 0,
        }
    }

    /// `test_one_line_per_pool_and_the_books_apart`.
    #[test]
    fn one_line_per_pool_and_the_books_apart() {
        use baskfy_core::fno::config::{FnoCeilings, FnoConfig};
        use baskfy_core::fno::ledger::{build_ledger, evaluate_pauses};
        let friday = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap();
        let week_ago = NaiveDate::from_ymd_opt(2026, 10, 23).unwrap();
        let trades = vec![
            closed(1, FoSleeve::F1N, "0.40", "10000", true, friday),
            closed(2, FoSleeve::F1N, "-0.70", "-17500", true, friday),
            closed(3, FoSleeve::F1N, "-1.00", "-25000", false, friday),
            closed(4, FoSleeve::F2, "1.50", "3000", true, friday),
            closed(5, FoSleeve::F2, "-1", "-99", true, week_ago),
        ];
        assert_eq!(
            week_lines(&trades, friday),
            vec![
                "F1N paper: 2 closed, ₹-7500, -0.30R (worst -0.70R)",
                "F2 paper: 1 closed, ₹3000, 1.50R (worst 1.50R)",
                "F1N LIVE: 1 closed, ₹-25000, -1.00R (worst -1.00R)",
            ]
        );
        let pauses: Vec<(bool, Vec<FoPause>)> = [true, false]
            .into_iter()
            .map(|sim| {
                let p = evaluate_pauses(
                    &trades,
                    sim,
                    friday,
                    Decimal::from(20_000),
                    None,
                    &FnoConfig::default(),
                    &FnoCeilings::default(),
                )
                .unwrap();
                (sim, p)
            })
            .collect();
        let alert = weekly_alert(
            &trades,
            &build_ledger(&trades, &[], friday).unwrap(),
            &pauses,
            friday,
        );
        assert_eq!(alert.name, FnoName::Weekly);
        assert!(
            alert
                .summary
                .contains("paper book month to date: 4 closed, ₹-4599"),
            "{}",
            alert.summary
        );
        assert!(
            alert
                .summary
                .contains("LIVE book month to date: 1 closed, ₹-25000")
        );
        let found = match alert.detail.get("pauses") {
            Some(OrderedJson::Array(found)) => found.clone(),
            _ => Vec::new(),
        };
        let got: Vec<(Option<&OrderedJson>, Option<&OrderedJson>)> = found
            .iter()
            .map(|p| (p.get("simulated"), p.get("scope")))
            .collect();
        assert_eq!(
            got,
            vec![(Some(&OrderedJson::Bool(false)), Some(&s("BOOK")))]
        );
    }

    #[test]
    fn a_quiet_week_says_so() {
        let friday = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap();
        let ledger = baskfy_core::fno::ledger::build_ledger(&[], &[], friday).unwrap();
        let alert = weekly_alert(&[], &ledger, &[], friday);
        assert!(alert.summary.contains("no FO structure closed this week"));
    }
}
