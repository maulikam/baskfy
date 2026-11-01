//! `tests/test_fno_ledger.py`'s halves that tick the real monitor (FO10): a paused sleeve's
//! 09:20 entry is written `REJECTED_PAUSED` and nothing is sent; a pause stops entries only (the
//! open structure runs to its own `HARD_EXIT`); the night marks and then records the pauses.
//!
//! Skipped without `BASKFY_RS_FNO_TEST_PG` (a bed clone `rsd_*`).
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines
)]

mod fno_world;

use baskfy_core::fno::config::{FnoCeilings, FnoConfig, FoSleeve};
use baskfy_desk::lanes::fno::fno_exec::store::FoStore;
use baskfy_desk::lanes::fno::ledger;
use baskfy_desk::storage::Param;
use chrono::NaiveDate;
use fno_world::*;
use rust_decimal::Decimal;

/// `closed(w, sleeve, r, on=)`: one closed position and its journal row.
async fn closed(w: &mut World, sleeve: &str, r: &str, on: NaiveDate) -> i64 {
    let symbol = match sleeve {
        "F1N" => "NIFTY",
        "F1B" => "BANKNIFTY",
        _ => "RELIANCE",
    };
    let structure = if sleeve == "F2" {
        "FUTURE"
    } else {
        "IRON_CONDOR"
    };
    let at_close = at(on, 15, 0);
    let uid = w.uid;
    let found = w
        .rows(
            "INSERT INTO fo_position (user_id, sleeve, symbol, structure, entry_plan_id, legs, \
             lots, lot_size, max_loss_inr, opened_at, closed_at, closed_reason, simulated) \
             VALUES (?, ?, ?, ?, ?, '{}', 1, 65, 10000, ?, ?, 'LOSS_CLOSE', true) RETURNING id",
            &[
                Param::Int(uid),
                Param::Text(sleeve.to_owned()),
                Param::Text(symbol.to_owned()),
                Param::Text(structure.to_owned()),
                Param::Text(format!("seed-{sleeve}-{r}-{on}")),
                Param::Aware(at_close),
                Param::Aware(at_close),
            ],
        )
        .await;
    let pid = icol(&found[0], "id");
    let rr: Decimal = r.parse().unwrap();
    let net = rr * Decimal::from(10_000);
    w.rows(
        "INSERT INTO fo_journal (position_id, user_id, sleeve, symbol, structure, opened_on, \
         closed_on, entry_inr, exit_inr, gross_pnl_inr, costs_inr, net_pnl_inr, \
         risk_budget_inr, r_multiple, closed_reason, sessions_held, simulated, sizing_mode) \
         VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 0, ?, 10000, ?, 'LOSS_CLOSE', 1, true, \
         'BUDGET')",
        &[
            Param::Int(pid),
            Param::Int(uid),
            Param::Text(sleeve.to_owned()),
            Param::Text(symbol.to_owned()),
            Param::Text(structure.to_owned()),
            Param::Date(on),
            Param::Date(on),
            Param::Decimal(net),
            Param::Decimal(net),
            Param::Decimal(rr),
        ],
    )
    .await;
    pid
}

async fn block(w: &mut World, sleeve: FoSleeve, day: NaiveDate) -> Vec<String> {
    let uid = w.uid;
    let mut store = FoStore::new(&mut w.conn, uid);
    ledger::entry_block(
        &mut store,
        sleeve,
        day,
        true,
        &FnoConfig::default(),
        &FnoCeilings::default(),
    )
    .await
    .unwrap()
}

async fn audits(w: &mut World, key: &str) -> usize {
    let uid = w.uid;
    w.rows(
        "SELECT id FROM fo_config_audit WHERE user_id = ? AND key = ?",
        &[Param::Int(uid), Param::Text(key.to_owned())],
    )
    .await
    .len()
}

#[tokio::test]
async fn three_f1_closes_at_minus_0_6r_refuse_the_monitors_next_entry() {
    with_world!(|w| {
        for (i, r) in ["-0.60", "-0.73", "-0.60"].iter().enumerate() {
            closed(
                &mut w,
                "F1N",
                r,
                before(f1_entry(), 5 - i64::try_from(i).unwrap()),
            )
            .await;
        }
        let uid = w.uid;
        let found = {
            let mut store = FoStore::new(&mut w.conn, uid);
            ledger::apply_pauses(
                &mut store,
                true,
                f1_entry(),
                &FnoConfig::default(),
                &FnoCeilings::default(),
            )
            .await
            .unwrap()
        };
        let got: Vec<(String, String)> = found
            .iter()
            .map(|p| (p.scope.clone(), p.reason()))
            .collect();
        assert_eq!(got, [("F1N".to_owned(), "PAPER:F1_LOSS_RUN".to_owned())]);
        assert_eq!(audits(&mut w, "pause:F1N").await, 1);
        f1_scan(&mut w).await;
        w.books(condor_book("NIFTY", "60.5", "55.5", "12.25", "10.25"));
        let raised = w.tick(at(f1_entry(), 9, 20)).await.raised;
        assert_eq!(raised.len(), 1, "{raised:?}");
        let plan = w.plan_row(&raised[0]).await;
        assert_eq!(tcol(&plan, "status"), "REJECTED_PAUSED");
        assert!(
            tcol(&plan, "reason").contains("-0.6R"),
            "{}",
            tcol(&plan, "reason")
        );
        assert!(w.fills(&raised[0]).await.is_empty());
        assert_eq!(w.broker_calls(), 0);
        // A second tick re-raises nothing: the refusal is this session's.
        assert!(w.tick(at(f1_entry(), 9, 21)).await.raised.is_empty());
        assert!(block(&mut w, FoSleeve::F1B, f1_entry()).await.is_empty());
    });
}

#[tokio::test]
async fn a_pause_stops_entries_only_and_the_open_structure_runs_to_its_exit() {
    with_world!(|w| {
        let (_plan_id, pid) = open_f1(&mut w).await;
        for i in 0..3 {
            closed(&mut w, "F1N", "-0.7", before(f1_entry(), 3 - i)).await;
        }
        assert!(!block(&mut w, FoSleeve::F1N, hard_exit()).await.is_empty());
        w.tick(at(hard_exit(), 15, 0)).await;
        let pos = w.position(pid).await;
        assert_eq!(tcol(&pos, "closed_reason"), "HARD_EXIT");
        assert_eq!(w.broker_calls(), 0);
    });
}

#[tokio::test]
async fn the_night_marks_then_records_the_pauses() {
    with_world!(|w| {
        for (i, r) in ["-2", "-2", "-2"].iter().enumerate() {
            let on = date(2026, 11, 2 + u32::try_from(i).unwrap());
            closed(&mut w, "F2", r, on).await;
        }
        w.market.land(hard_exit(), []);
        let night = w.night(hard_exit()).await;
        assert!(night.landed);
        assert_eq!(night.pauses, vec!["PAPER:F2_MONTH_R:F2".to_owned()]);
    });
}
