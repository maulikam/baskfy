//! Lane **fno_exec** (wave D2): `tests/test_fno_monitor.py` ported as spec — FO7, the desk's FO
//! monitor replayed over a recorded fixture month (`docs/fno/06` FO7): plans are raised, exits
//! happen, positions carry, on paper. Each replay drives the real engine, the real executor and
//! the real FO gateway (paper-pinned, the covered-overnight rule wired) over a real Postgres
//! with the `fo_` schema, and the recording broker (the replay Kite) must see **nothing**:
//!
//! * `TestF1Replay` — the 09:20 plan, a thin wing abandoning before any short, the rejections,
//!   the lapse, the confirm and the nightly marks, the profit take, the loss close, the `E - 1`
//!   15:00 exit and the `LATE_EXIT` at the next open;
//! * `TestF2Replay` — the entry, its same-session GTT, the trail through the gateway (never
//!   lower), the roll with the stop carried, the time exit, the stop-out, and the GTT that fails
//!   to place (`NAKED_FUTURE`, exited at the next check);
//! * `TestGating` — no path forms an entry without a confirm; every flag false is simulated and
//!   the broker sees nothing; the paper pin holds with every flag on;
//! * `TestPgMarket` — the night's reads of the shared market tables.
//!
//! Skipped without `BASKFY_RS_FNO_TEST_PG` (Python's `requires_db`).
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines
)]

mod fno_world;

use baskfy_core::fno::config::FoSleeve;
use baskfy_core::options::execution::Level;
use baskfy_desk::lanes::fno::fno_exec::execute::ExecError;
use baskfy_desk::lanes::fno::fno_exec::monitor::entry_plan_id;
use baskfy_desk::lanes::fno::fno_exec::monitor::seams::{
    MarginQuote, Market, PgMarket, Print, PrintKey,
};
use baskfy_desk::lanes::fno::gates::{mode_of, paper_gates};
use baskfy_desk::lanes::options::py;
use baskfy_desk::storage::Param;
use baskfy_desk::wire::PyValue;
use fno_world::*;
use rust_decimal::Decimal;

fn refused_code(
    r: Result<baskfy_desk::lanes::fno::fno_exec::execute::FoOutcome, ExecError>,
) -> (u16, String) {
    match r {
        Err(ExecError::Refused(x)) => (x.status, x.code),
        other => panic!("not refused: {other:?}"),
    }
}

// --- F1 -----------------------------------------------------------------------------------------

#[tokio::test]
async fn f1_the_0920_plan_from_the_scan_repriced_live_longs_first() {
    with_world!(|w| {
        f1_scan(&mut w).await;
        w.books(neutral("NIFTY"));
        assert!(w.tick(at(f1_entry(), 9, 19)).await.raised.is_empty()); // 09:20, not before
        let raised = w.tick(at(f1_entry(), 9, 20)).await.raised;
        assert_eq!(
            raised,
            vec![entry_plan_id(FoSleeve::F1N, f1_entry(), "NIFTY", w.uid)]
        );
        assert!(w.tick(at(f1_entry(), 9, 21)).await.raised.is_empty()); // a second raise is a no-op
        let plan = w.plan_row(&raised[0]).await;
        assert_eq!(
            (
                tcol(&plan, "status"),
                tcol(&plan, "kind"),
                icol(&plan, "lots"),
                icol(&plan, "lot_size")
            ),
            ("ISSUED".into(), "ENTRY".into(), 1, LOT)
        );
        assert_eq!(
            py::aware(col(&plan, "expires_at")),
            Some(at(f1_entry(), 9, 50))
        );
        assert_eq!(py::opt_day(col(&plan, "hard_exit_date")), Some(hard_exit()));
        assert_eq!(dcol(&plan, "credit_points"), d("93.50")); // on live mids
        let legs = w
            .rows(
                "SELECT entry_seq, role, side, quantity FROM fo_leg WHERE plan_id = ? ORDER BY entry_seq",
                &[Param::Int(icol(&plan, "id"))],
            )
            .await;
        let got: Vec<(String, String)> = legs
            .iter()
            .map(|l| (tcol(l, "role"), tcol(l, "side")))
            .collect();
        assert_eq!(
            got,
            [
                ("LONG_PUT", "BUY"),
                ("LONG_CALL", "BUY"),
                ("SHORT_PUT", "SELL"),
                ("SHORT_CALL", "SELL")
            ]
            .map(|(a, b)| (a.to_owned(), b.to_owned()))
        );
        assert!(legs.iter().all(|l| icol(l, "quantity") == LOT));
        assert!(w.fills(&raised[0]).await.is_empty()); // raised, never sent
        assert_eq!(w.broker_calls(), 0);
    });
}

#[tokio::test]
async fn f1_a_thin_protecting_leg_abandons_before_any_short() {
    with_world!(|w| {
        f1_scan(&mut w).await;
        w.books(neutral("NIFTY"));
        let plan_id = w.tick(at(f1_entry(), 9, 20)).await.raised[0].clone();
        let sym = opt_symbol("NIFTY", "LONG_CALL");
        let mut thin = w.quotes.get(&sym).unwrap();
        thin.asks = vec![Level {
            price: thin.ask.unwrap(),
            quantity: 20,
        }];
        w.books(vec![(sym, thin)]);
        let out = w.confirm(&plan_id, at(f1_entry(), 9, 21)).await.unwrap();
        assert_eq!(out.outcome, "ABANDONED_PARTIAL");
        assert!(out.position_id.is_none());
        let fills = w.fills(&plan_id).await;
        assert!(!fills.iter().any(|f| tcol(f, "role").starts_with("SHORT"))); // no short ever sent
        let mut net: std::collections::BTreeMap<String, i64> = std::collections::BTreeMap::new();
        for f in &fills {
            let q = icol(f, "quantity");
            *net.entry(tcol(f, "role")).or_default() +=
                if tcol(f, "side") == "BUY" { q } else { -q };
        }
        assert!(net.values().all(|v| *v == 0), "{net:?}");
        let plan = w.plan_row(&plan_id).await;
        assert_eq!(tcol(&plan, "status"), "ABANDONED_PARTIAL");
        assert!(tcol(&plan, "reason").contains("abandoned"));
        let uid = w.uid;
        let rows = w
            .rows(
                "SELECT closed_at, closed_reason FROM fo_position WHERE user_id = ?",
                &[Param::Int(uid)],
            )
            .await;
        let got: Vec<(bool, String)> = rows
            .iter()
            .map(|r| (!is_null(r, "closed_at"), tcol(r, "closed_reason")))
            .collect();
        assert_eq!(got, vec![(true, "ABANDONED_PARTIAL".to_owned())]);
        assert_eq!(w.broker_calls(), 0);
    });
}

#[tokio::test]
async fn f1_rejections_carry_their_reason() {
    with_world!(|w| {
        f1_scan(&mut w).await;
        w.books(neutral("NIFTY"));
        w.margin.set(Some(MarginQuote {
            required_inr: Decimal::from(900_000),
            free_inr: Decimal::from(100_000),
        }));
        let id = w.tick(at(f1_entry(), 9, 20)).await.raised[0].clone();
        let plan = w.plan_row(&id).await;
        assert_eq!(tcol(&plan, "status"), "REJECTED_MARGIN");
        assert!(tcol(&plan, "reason").contains("margin"));
        let r = w.confirm(&id, at(f1_entry(), 9, 21)).await;
        assert!(matches!(r, Err(ExecError::Refused(_))), "{r:?}");
    });
}

#[tokio::test]
async fn f1_a_wide_short_is_refused_by_name() {
    with_world!(|w| {
        f1_scan(&mut w).await;
        w.books(neutral("NIFTY"));
        w.books(vec![(opt_symbol("NIFTY", "SHORT_CALL"), book("55", "66"))]);
        let id = w.tick(at(f1_entry(), 9, 20)).await.raised[0].clone();
        let plan = w.plan_row(&id).await;
        assert_eq!(tcol(&plan, "status"), "REJECTED_LIQUIDITY");
        let reason = tcol(&plan, "reason");
        assert!(
            reason.contains("short call 25000") && reason.contains("spread"),
            "{reason}"
        );
    });
}

#[tokio::test]
async fn f1_an_unconfirmed_plan_lapses() {
    with_world!(|w| {
        f1_scan(&mut w).await;
        w.books(neutral("NIFTY"));
        let plan_id = w.tick(at(f1_entry(), 9, 20)).await.raised[0].clone();
        for minute in 21..60 {
            w.tick(at(f1_entry(), 9, minute)).await;
        }
        assert_eq!(tcol(&w.plan_row(&plan_id).await, "status"), "LAPSED");
        assert!(w.fills(&plan_id).await.is_empty());
        let (status, _) = refused_code(w.confirm(&plan_id, at(f1_entry(), 9, 59)).await);
        assert_eq!(status, 409);
    });
}

#[tokio::test]
async fn f1_confirm_fills_longs_first_and_carries_with_nightly_marks() {
    with_world!(|w| {
        let (plan_id, pid) = open_f1(&mut w).await;
        let fills = w.fills(&plan_id).await;
        assert_eq!(
            fills.iter().map(|f| tcol(f, "role")).collect::<Vec<_>>(),
            ["LONG_PUT", "LONG_CALL", "SHORT_PUT", "SHORT_CALL"]
        );
        assert!(fills.iter().all(|f| py::truthy(col(f, "simulated"))));
        let pos = w.position(pid).await;
        assert_eq!(dcol(&pos, "entry_credit"), d("92.00")); // 60 + 55 - 12.5 - 10.5
        assert!(py::truthy(col(&pos, "simulated")));
        assert_eq!(py::opt_day(col(&pos, "hard_exit_date")), Some(hard_exit()));
        assert_eq!(tcol(&w.plan_row(&plan_id).await, "status"), "OPEN");
        let nxt = before(hard_exit(), 13);
        assert!(!w.night(nxt).await.landed);
        land_condor(
            &w.market,
            "NIFTY",
            f1_entry(),
            &[
                ("SHORT_CALL", "58"),
                ("SHORT_PUT", "54"),
                ("LONG_CALL", "12"),
                ("LONG_PUT", "10"),
            ],
        );
        land_condor(
            &w.market,
            "NIFTY",
            nxt,
            &[
                ("SHORT_CALL", "50"),
                ("SHORT_PUT", "50"),
                ("LONG_CALL", "10"),
                ("LONG_PUT", "9"),
            ],
        );
        assert_eq!(w.night(nxt).await.marked, 2); // both sessions, the missed one caught up
        assert_eq!(w.night(nxt).await.marked, 0); // idempotent
        let marks = w
            .rows("SELECT trade_date, mark_points, pnl_inr FROM fo_mark WHERE position_id = ? ORDER BY trade_date", &[Param::Int(pid)])
            .await;
        let got: Vec<_> = marks
            .iter()
            .map(|m| {
                (
                    py::opt_day(col(m, "trade_date")).unwrap(),
                    dcol(m, "mark_points"),
                    dcol(m, "pnl_inr"),
                )
            })
            .collect();
        assert_eq!(
            got,
            vec![
                (f1_entry(), d("90.00"), d("130.00")),
                (nxt, d("81.00"), d("715.00"))
            ]
        );
        assert_eq!(w.broker_calls(), 0);
    });
}

#[tokio::test]
async fn f1_profit_take_on_live_mids_closes_shorts_first() {
    with_world!(|w| {
        let (plan_id, pid) = open_f1(&mut w).await;
        let day = before(hard_exit(), 5);
        assert!(w.tick(at(day, 11, 0)).await.actions.is_empty()); // neutral book: nothing due
        w.books(condor_book("NIFTY", "20", "18", "3", "2")); // cost to close 33 <= 46
        let report = w.tick(at(day, 11, 1)).await;
        assert_eq!(report.actions, vec![format!("{plan_id}-X")]);
        let exit = w.plan_row(&format!("{plan_id}-X")).await;
        assert_eq!(
            (
                tcol(&exit, "kind"),
                tcol(&exit, "status"),
                tcol(&exit, "parent_plan_id")
            ),
            ("EXIT".into(), "CLOSED".into(), plan_id.clone())
        );
        assert!(!is_null(&exit, "confirmed_at")); // the entry's confirm, not a new one
        assert_eq!(jtext(&jcol(&exit, "detail"), "code"), "PROFIT_TAKE");
        assert_eq!(
            w.fill_roles(&format!("{plan_id}-X")).await,
            ["SHORT_CALL", "SHORT_PUT", "LONG_CALL", "LONG_PUT"]
        );
        let pos = w.position(pid).await;
        assert!(!is_null(&pos, "closed_at"));
        assert_eq!(tcol(&pos, "closed_reason"), "PROFIT_TAKE");
        assert_eq!(tcol(&w.plan_row(&plan_id).await, "status"), "CLOSED");
        assert_eq!(w.broker_calls(), 0);
    });
}

#[tokio::test]
async fn f1_loss_close_at_two_and_a_half_credits() {
    with_world!(|w| {
        let (plan_id, pid) = open_f1(&mut w).await;
        let day = before(hard_exit(), 8);
        w.books(condor_book("NIFTY", "200", "20", "30", "5")); // 170 + 15 = 185 < 230
        assert!(w.tick(at(day, 13, 0)).await.actions.is_empty());
        w.books(condor_book("NIFTY", "250", "20", "30", "5")); // 220 + 15 = 235 >= 230
        assert_eq!(
            w.tick(at(day, 13, 1)).await.actions,
            vec![format!("{plan_id}-X")]
        );
        assert_eq!(tcol(&w.position(pid).await, "closed_reason"), "LOSS_CLOSE");
        assert_eq!(
            w.fill_roles(&format!("{plan_id}-X")).await[..2],
            ["SHORT_CALL", "SHORT_PUT"]
        );
    });
}

#[tokio::test]
async fn f1_the_e_minus_1_exit_at_1500() {
    with_world!(|w| {
        let (plan_id, pid) = open_f1(&mut w).await;
        assert!(w.tick(at(hard_exit(), 14, 59)).await.actions.is_empty());
        assert_eq!(
            w.tick(at(hard_exit(), 15, 0)).await.actions,
            vec![format!("{plan_id}-X")]
        );
        assert_eq!(tcol(&w.position(pid).await, "closed_reason"), "HARD_EXIT");
        let detail = jcol(&w.plan_row(&plan_id).await, "detail");
        assert_eq!(detail.get("violations"), Some(&PyValue::List(Vec::new())));
    });
}

#[tokio::test]
async fn f1_a_hard_exit_passed_while_down_is_a_late_exit_at_the_next_open() {
    with_world!(|w| {
        let (plan_id, pid) = open_f1(&mut w).await;
        assert_eq!(
            w.tick(at(nov(), 9, 15)).await.actions,
            vec![format!("{plan_id}-X")]
        );
        assert_eq!(tcol(&w.position(pid).await, "closed_reason"), "LATE_EXIT");
        let detail = jcol(&w.plan_row(&plan_id).await, "detail");
        let Some(PyValue::List(v)) = detail.get("violations") else {
            panic!("{detail:?}")
        };
        let codes: Vec<String> = v
            .iter()
            .map(|x| match x {
                PyValue::Map(m) => jtext(m, "code"),
                _ => String::new(),
            })
            .collect();
        assert_eq!(codes, ["LATE_EXIT"]);
        assert!(w.alerts().iter().any(|a| a.contains("LATE_EXIT")));
    });
}

// --- F2 -----------------------------------------------------------------------------------------

#[tokio::test]
async fn f2_a_month_entry_trail_roll_and_time_exit() {
    with_world!(|w| {
        let (plan_id, pid) = open_f2(&mut w).await;
        let plan = w.plan_row(&plan_id).await;
        assert_eq!(
            (icol(&plan, "lots"), tcol(&plan, "sizing_mode")),
            (1, "PAPER_ONE_LOT".to_owned())
        );
        assert_eq!(
            jcol(&plan, "detail").get("lots_at_ceiling"),
            Some(&PyValue::Int(0))
        );
        let pos = w.position(pid).await;
        assert_eq!(dcol(&pos, "entry_price"), d("1400.00"));
        assert_eq!(dcol(&pos, "stop_price"), d("1340.00")); // 1400 - 3 x 20
        assert_eq!(tcol(&pos, "gtt_id"), format!("PAPER-{pid}")); // placed in the fill's session
        assert_eq!(py::opt_day(col(&pos, "next_roll_date")), Some(hard_exit()));
        // Evening 1: a new high moves the stop to 1450 - 60 and the GTT with it.
        land_future(&w.market, f2_entry(), nov(), "1450", "1449");
        let night = w.night(f2_entry()).await;
        assert_eq!(
            night.trailed,
            vec!["RELIANCE:1340.00->1390.00:DRY_RUN_GTT_MODIFY".to_owned()]
        );
        assert_eq!(dcol(&w.position(pid).await, "stop_price"), d("1390.00"));
        let journal = std::fs::read_to_string(w.dir.path().join("fo-F2.jsonl")).unwrap();
        let modify: Vec<serde_json::Value> = journal
            .lines()
            .filter(|x| x.contains("gtt_dry_run_modify"))
            .map(|x| serde_json::from_str(x).unwrap())
            .collect();
        let last = modify.last().unwrap();
        assert_eq!(last["product"], "NRML");
        assert_eq!(last["floor_trigger"].as_f64(), Some(1340.0));
        // Evening 2: a lower close never lowers it.
        let s = sessions();
        let day2 = s[s.iter().position(|x| *x == f2_entry()).unwrap() + 1];
        land_future(&w.market, day2, nov(), "1420", "1421");
        assert!(w.night(day2).await.trailed.is_empty());
        assert_eq!(dcol(&w.position(pid).await, "stop_price"), d("1390.00"));
        let marks = w.rows("SELECT mark_points, pnl_inr FROM fo_mark WHERE position_id = ? ORDER BY trade_date", &[Param::Int(pid)]).await;
        assert_eq!(
            marks.iter().map(|m| dcol(m, "pnl_inr")).collect::<Vec<_>>(),
            vec![d("24500.00"), d("10500.00")]
        );
        // E-1 15:00: the roll, same quantity, stop carried, under the original confirm.
        w.books(vec![
            (
                FUT_NOV.to_owned(),
                book_last("1430.00", "1430.10", "1430.05"),
            ),
            (
                FUT_DEC.to_owned(),
                book_last("1438.00", "1438.10", "1438.05"),
            ),
        ]);
        assert!(w.tick(at(hard_exit(), 14, 59)).await.actions.is_empty());
        assert_eq!(
            w.tick(at(hard_exit(), 15, 0)).await.actions,
            vec![format!("{plan_id}-R1")]
        );
        let roll = w.plan_row(&format!("{plan_id}-R1")).await;
        assert_eq!(
            (
                tcol(&roll, "kind"),
                tcol(&roll, "status"),
                tcol(&roll, "parent_plan_id")
            ),
            ("ROLL".into(), "OPEN".into(), plan_id.clone())
        );
        let fills = w.fills(&format!("{plan_id}-R1")).await;
        assert_eq!(
            fills
                .iter()
                .map(|f| (tcol(f, "side"), icol(f, "quantity")))
                .collect::<Vec<_>>(),
            vec![("SELL".to_owned(), F2_LOT), ("BUY".to_owned(), F2_LOT)]
        );
        let pos = w.position(pid).await;
        assert!(is_null(&pos, "closed_at"));
        let legs = jcol(&pos, "legs");
        let Some(PyValue::List(ls)) = legs.get("legs") else {
            panic!()
        };
        let PyValue::Map(first) = &ls[0] else {
            panic!()
        };
        assert_eq!(jtext(first, "tradingsymbol"), FUT_DEC);
        let Some(PyValue::Map(carry)) = legs.get("carry") else {
            panic!()
        };
        assert_eq!(carry.get("rolls"), Some(&PyValue::Int(1)));
        assert_eq!(dcol(&pos, "stop_price"), d("1390.00")); // carried
        assert_eq!(
            py::opt_day(col(&pos, "next_roll_date")),
            Some(before(dec_(), 1))
        );
        assert_eq!(tcol(&pos, "gtt_id"), format!("PAPER-{pid}"));
        // The 40th session of the hold, 15:00: the time exit (before December's roll).
        let time_exit = s[s.iter().position(|x| *x == f2_entry()).unwrap() + 39];
        assert!(time_exit < before(dec_(), 1));
        assert!(w.tick(at(time_exit, 14, 59)).await.actions.is_empty());
        assert_eq!(
            w.tick(at(time_exit, 15, 0)).await.actions,
            vec![format!("{plan_id}-X")]
        );
        assert_eq!(tcol(&w.position(pid).await, "closed_reason"), "TIME_EXIT");
        assert_eq!(
            tcol(&w.fills(&format!("{plan_id}-X")).await[0], "side"),
            "SELL"
        );
        assert_eq!(w.broker_calls(), 0);
    });
}

#[tokio::test]
async fn f2_a_stop_out() {
    with_world!(|w| {
        let (plan_id, pid) = open_f2(&mut w).await;
        let day = before(hard_exit(), 10);
        w.books(vec![(
            FUT_NOV.to_owned(),
            book_last("1345", "1345.10", "1345"),
        )]);
        assert!(w.tick(at(day, 10, 0)).await.actions.is_empty());
        w.books(vec![(
            FUT_NOV.to_owned(),
            book_last("1338", "1338.10", "1339.95"),
        )]);
        assert_eq!(
            w.tick(at(day, 10, 1)).await.actions,
            vec![format!("{plan_id}-X")]
        );
        let pos = w.position(pid).await;
        assert_eq!(tcol(&pos, "closed_reason"), "STOP");
        assert!(!is_null(&pos, "closed_at"));
        assert_eq!(w.broker_calls(), 0);
    });
}

#[tokio::test]
async fn f2_a_gtt_that_fails_is_naked_and_exits_at_the_next_check() {
    with_world!(|w| {
        f2_scan(&mut w).await;
        w.books(vec![(
            FUT_NOV.to_owned(),
            book_last("1399.90", "1400.00", "1400.00"),
        )]);
        let plan_id = w.tick(at(f2_entry(), 9, 20)).await.raised[0].clone();
        let real = w.gateway_for(FoSleeve::F2);
        w.gateways
            .lock()
            .unwrap()
            .insert(FoSleeve::F2, std::sync::Arc::new(RefuseGtt(real)));
        let out = w.confirm(&plan_id, at(f2_entry(), 9, 22)).await.unwrap();
        assert_eq!(out.outcome, "OPEN");
        let pid = out.position_id.unwrap();
        assert!(is_null(&w.position(pid).await, "gtt_id"));
        assert!(
            w.alerts()
                .iter()
                .any(|a| a.contains("GTT stop did not place")),
            "{:?}",
            w.alerts()
        );
        let detail = jcol(&w.plan_row(&plan_id).await, "detail");
        let Some(PyValue::List(v)) = detail.get("violations") else {
            panic!()
        };
        assert_eq!(v.len(), 1);
        let PyValue::Map(m) = &v[0] else { panic!() };
        assert_eq!(jtext(m, "code"), "NAKED_FUTURE");
        assert_eq!(
            w.tick(at(f2_entry(), 9, 23)).await.actions,
            vec![format!("{plan_id}-X")]
        );
        assert_eq!(
            tcol(&w.position(pid).await, "closed_reason"),
            "NAKED_FUTURE"
        );
    });
}

// --- gating -------------------------------------------------------------------------------------

#[tokio::test]
async fn gating_no_path_forms_an_entry_without_a_confirm() {
    with_world!(|w| {
        f1_scan(&mut w).await;
        f2_scan(&mut w).await;
        w.books(neutral("NIFTY"));
        w.books(vec![(
            FUT_NOV.to_owned(),
            book_last("1399.90", "1400.00", "1400.00"),
        )]);
        for minute in 20..60 {
            w.tick(at(f1_entry(), 9, minute)).await;
        }
        let uid = w.uid;
        let n = w
            .rows(
                "SELECT count(*) AS n FROM fo_fill WHERE user_id = ?",
                &[Param::Int(uid)],
            )
            .await;
        assert_eq!(icol(&n[0], "n"), 0);
        let plan_id = tcol(
            &w.rows(
                "SELECT plan_id FROM fo_plan WHERE user_id = ? AND sleeve = 'F1N'",
                &[Param::Int(uid)],
            )
            .await[0],
            "plan_id",
        );
        let (_, code) = refused_code(w.confirm_with(&plan_id, at(f1_entry(), 9, 21), false).await);
        assert_eq!(code, "CONFIRM_REQUIRED");
        assert_eq!(w.broker_calls(), 0);
    });
}

#[tokio::test]
async fn gating_every_flag_false_is_simulated_and_the_broker_sees_nothing() {
    with_world!(|w| {
        w.set(|s| {
            s.options_enabled = false;
            s.fno_carry_enabled = false;
            s.fno_f1_execution_enabled = false;
            s.fno_f2_execution_enabled = false;
        });
        for dry in [true, false] {
            w.set(|s| s.dry_run = dry);
            assert!(paper_gates(&w.settings.read().unwrap(), FoSleeve::F1N).dry_run);
        }
        let (_plan_id, pid) = open_f1(&mut w).await;
        w.tick(at(hard_exit(), 15, 0)).await;
        assert_eq!(tcol(&w.position(pid).await, "closed_reason"), "HARD_EXIT");
        let uid = w.uid;
        let r = w
            .rows(
                "SELECT bool_and(simulated) AS s, count(*) AS n FROM fo_fill WHERE user_id = ?",
                &[Param::Int(uid)],
            )
            .await;
        assert!(py::truthy(col(&r[0], "s")));
        assert_eq!(icol(&r[0], "n"), 8);
        assert_eq!(w.broker_calls(), 0);
    });
}

#[tokio::test]
async fn gating_the_paper_pin_holds_with_every_flag_on() {
    with_world!(|w| {
        let (_plan_id, pid) = open_f1(&mut w).await; // confirmed on paper
        w.set(|s| {
            s.options_enabled = true;
            s.fno_carry_enabled = true;
            s.fno_f1_execution_enabled = true;
            s.dry_run = false;
        });
        let s = w.settings.read().unwrap().clone();
        assert_eq!(
            mode_of(&s, FoSleeve::F1N),
            baskfy_core::options::gating::Mode::Live
        );
        assert!(paper_gates(&s, FoSleeve::F1N).dry_run); // FO7 builds no live path
        w.tick(at(hard_exit(), 15, 0)).await; // the exit still runs, on paper
        assert_eq!(tcol(&w.position(pid).await, "closed_reason"), "HARD_EXIT");
        assert_eq!(w.broker_calls(), 0);
    });
}

// --- PgMarket -----------------------------------------------------------------------------------

#[tokio::test]
async fn pg_market_landed_prints_and_the_calendar() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let day = date(2022, 2, 19); // a Saturday and a symbol no feed prints
    let market = PgMarket::new(connect().await.unwrap());
    let exp = date(2022, 2, 24);
    rows(
        &mut conn,
        "INSERT INTO fo_contract_daily (trade_date, instrument, symbol, expiry, strike, option_type, \
         close, settle, source_key) VALUES \
         (?, 'FUTSTK', 'FO7TEST', ?, 0, 'XX', 101.5, 101.25, 'fo7-test'), \
         (?, 'OPTSTK', 'FO7TEST', ?, 100, 'CE', 3.5, 3.45, 'fo7-test')",
        &[Param::Date(day), Param::Date(exp), Param::Date(day), Param::Date(exp)],
    )
    .await;
    let outcome = tokio::spawn(async move {
        assert!(!market.landed(day).await.unwrap()); // rows alone are not an ingested night
        let mut c = connect().await.unwrap();
        rows(
            &mut c,
            "INSERT INTO fo_ingest_day (trade_date, status) VALUES (?, 'INGESTED')",
            &[Param::Date(day)],
        )
        .await;
        assert!(market.landed(day).await.unwrap());
        let fut: PrintKey = ("FO7TEST".into(), exp, None, "XX".into());
        let opt: PrintKey = ("FO7TEST".into(), exp, Some(Decimal::from(100)), "CE".into());
        let gone: PrintKey = ("FO7TEST".into(), exp, Some(Decimal::from(200)), "CE".into());
        let prints = market
            .prints(day, &[fut.clone(), opt.clone(), gone.clone()])
            .await
            .unwrap();
        assert_eq!(
            prints[&fut],
            Print {
                close: d("101.5"),
                settle: d("101.25")
            }
        );
        assert_eq!(prints[&opt].settle, d("3.45"));
        assert!(!prints.contains_key(&gone));
        let s = market.sessions().await.unwrap();
        assert!(s.contains(&date(2022, 2, 18)) && !s.contains(&day));
    })
    .await;
    rows(
        &mut conn,
        "DELETE FROM fo_contract_daily WHERE symbol = 'FO7TEST'",
        &[],
    )
    .await;
    rows(
        &mut conn,
        "DELETE FROM fo_ingest_day WHERE trade_date = ?",
        &[Param::Date(day)],
    )
    .await;
    if let Err(e) = outcome {
        std::panic::resume_unwind(e.into_panic());
    }
}
