//! Lane **fno_exec** (wave D2): `tests/test_fno_f3.py` ported as spec — F3-5, the desk's F3
//! replay (the directional index credit spread; `docs/fno/04` §11; DECISIONS-FO M.5, Maulik's
//! three answers, 28 Sep 2026). On FO7's world: the scan's CANDIDATE → the 09:20 plan (wing
//! first, short second, on mids, the intraday check and the level from the index) → the
//! confirm → the watch (a level break, the cut, the target, the hard exit) → the exit ISSUED for
//! the click with `BASKFY_FNO_F3_AUTO_EXIT` off and sent by the monitor with it on → the add the
//! next session → the nightly mark → the page. The replay Kite records no call anywhere.
//!
//! Skipped without `BASKFY_RS_FNO_TEST_PG`.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines
)]

mod fno_world;

use std::sync::atomic::Ordering;

use baskfy_core::fno::config::FoSleeve;
use baskfy_desk::lanes::fno::desk::{Live, LiveSources, build_view};
use baskfy_desk::lanes::fno::fno_exec::execute::{ExecError, FoOutcome};
use baskfy_desk::lanes::fno::fno_exec::monitor::entry_plan_id;
use baskfy_desk::lanes::fno::fno_exec::monitor::seams::{Print, PrintKey};
use baskfy_desk::lanes::fno::fno_exec::store::FoStore;
use baskfy_desk::lanes::fno::gates::{f3_auto_exit_enabled, fno_gates};
use baskfy_desk::lanes::options::py;
use baskfy_desk::settings::Settings;
use baskfy_desk::storage::Param;
use baskfy_desk::wire::PyValue;
use chrono::NaiveDate;
use fno_world::*;
use rust_decimal::Decimal;

const SHORT: &str = "SHORT_PUT";
const WING: &str = "LONG_PUT";

fn entry() -> NaiveDate {
    before(nov(), 12)
}

fn level() -> Decimal {
    d("24000")
}

fn spread_book(
    short_mid: &str,
    wing_mid: &str,
) -> Vec<(String, baskfy_desk::lanes::fno::fno_exec::store::FoQuote)> {
    let (s, w) = (d(short_mid), d(wing_mid));
    vec![
        (
            opt_symbol("NIFTY", SHORT),
            book(&py::dstr(s - d("0.5")), &py::dstr(s + d("0.5"))),
        ),
        (
            opt_symbol("NIFTY", WING),
            book(&py::dstr(w - d("0.25")), &py::dstr(w + d("0.25"))),
        ),
    ]
}

/// `F3World`: the index at 24,300 over a 24,200 open, the flag off.
async fn f3_world(w: &mut World) {
    w.index_at("24300", "24200");
    w.auto_exit.store(false, Ordering::SeqCst);
}

async fn f3_scan(w: &mut World, state: &str, trade_date: Option<NaiveDate>) {
    let legs = serde_json::json!([
        {"entry_seq": 1, "role": WING, "strike": strike(WING).to_string(), "option_type": "PE",
         "expiry": nov().to_string(), "qty_sign": 1},
        {"entry_seq": 2, "role": SHORT, "strike": strike(SHORT).to_string(), "option_type": "PE",
         "expiry": nov().to_string(), "qty_sign": -1},
    ]);
    let detail = serde_json::json!({
        "underlying": "NIFTY", "entry_session": entry().to_string(), "expiry": nov().to_string(),
        "expiry_kind": "weekly", "direction": "UP", "direction_daily": "UP",
        "level": py::dstr(level()), "short_strike": strike(SHORT).to_string(),
        "wing_strike": strike(WING).to_string(), "option_type": "PE", "legs": legs,
        "lot_size": LOT,
    });
    let day = trade_date.unwrap_or_else(|| before(entry(), 1));
    w.scan("F3N", day, "NIFTY", detail, None, state).await;
}

async fn open_f3(w: &mut World) -> (String, i64) {
    f3_scan(w, "CANDIDATE", None).await;
    w.books(spread_book("30", "6")); // credit 24 on mids
    let raised = w.tick(at(entry(), 9, 20)).await.raised;
    assert_eq!(
        raised,
        vec![entry_plan_id(FoSleeve::F3N, entry(), "NIFTY", w.uid)]
    );
    let out = w.confirm(&raised[0], at(entry(), 9, 25)).await.unwrap();
    assert_eq!(out.outcome, "OPEN", "{out:?}");
    (raised[0].clone(), out.position_id.unwrap())
}

fn code_of(r: Result<FoOutcome, ExecError>) -> String {
    match r {
        Err(ExecError::Refused(x)) => x.code,
        other => panic!("not refused: {other:?}"),
    }
}

// --- the entry ----------------------------------------------------------------------------------

#[tokio::test]
async fn entry_the_0920_plan_is_wing_then_short_priced_on_mids_with_the_level() {
    with_world!(|w| {
        f3_world(&mut w).await;
        f3_scan(&mut w, "CANDIDATE", None).await;
        w.books(spread_book("30", "6"));
        assert!(w.tick(at(entry(), 9, 19)).await.raised.is_empty());
        let plan_id = w.tick(at(entry(), 9, 20)).await.raised[0].clone();
        assert!(w.tick(at(entry(), 9, 21)).await.raised.is_empty()); // a second raise is a no-op
        let plan = w.plan_row(&plan_id).await;
        assert_eq!(
            (
                tcol(&plan, "status"),
                tcol(&plan, "kind"),
                tcol(&plan, "structure"),
                icol(&plan, "lots")
            ),
            ("ISSUED".into(), "ENTRY".into(), "CREDIT_SPREAD".into(), 1)
        );
        assert_eq!(dcol(&plan, "credit_points"), d("24.00"));
        assert_eq!(
            dcol(&plan, "width_points"),
            Decimal::from(strike(SHORT) - strike(WING))
        );
        assert_eq!(py::opt_day(col(&plan, "hard_exit_date")), Some(nov())); // expiry day, not E-1
        let legs = w
            .rows("SELECT entry_seq, role, side, quantity FROM fo_leg WHERE plan_id = ? ORDER BY entry_seq", &[Param::Int(icol(&plan, "id"))])
            .await;
        assert_eq!(
            legs.iter()
                .map(|l| (tcol(l, "role"), tcol(l, "side"), icol(l, "quantity")))
                .collect::<Vec<_>>(),
            vec![
                (WING.to_owned(), "BUY".to_owned(), LOT),
                (SHORT.to_owned(), "SELL".to_owned(), LOT)
            ]
        );
        let detail = jcol(&plan, "detail");
        assert_eq!(py::dec_py(detail.get("level")), Some(level()));
        assert_eq!(jtext(&detail, "direction"), "UP");
        assert_eq!(py::dec_py(detail.get("decay_target_mark")), Some(d("4.80")));
        assert_eq!(py::dec_py(detail.get("loss_cut_mark")), Some(d("48.00")));
        assert!(w.fills(&plan_id).await.is_empty()); // raised, never sent
    });
}

#[tokio::test]
async fn entry_the_intraday_check_and_a_broken_level_refuse_by_name() {
    with_world!(|w| {
        f3_world(&mut w).await;
        f3_scan(&mut w, "CANDIDATE", None).await;
        w.books(spread_book("30", "6"));
        w.index_at("24150", "24200"); // below the open: not UP
        let id = w.tick(at(entry(), 9, 20)).await.raised[0].clone();
        let plan = w.plan_row(&id).await;
        assert_eq!(tcol(&plan, "status"), "REJECTED_STRUCTURE");
        assert!(tcol(&plan, "reason").contains("intraday check"));
        let n = w
            .rows(
                "SELECT count(*) AS n FROM fo_leg WHERE plan_id = ?",
                &[Param::Int(icol(&plan, "id"))],
            )
            .await;
        assert_eq!(icol(&n[0], "n"), 0);
    });
}

#[tokio::test]
async fn entry_a_level_already_broken_is_no_entry() {
    with_world!(|w| {
        f3_world(&mut w).await;
        f3_scan(&mut w, "CANDIDATE", None).await;
        w.books(spread_book("30", "6"));
        w.index_at("23900", "24200");
        let id = w.tick(at(entry(), 9, 20)).await.raised[0].clone();
        let plan = w.plan_row(&id).await;
        assert_eq!(tcol(&plan, "status"), "REJECTED_STRUCTURE");
        assert!(tcol(&plan, "reason").contains("already broken"));
    });
}

#[tokio::test]
async fn entry_no_index_quote_is_a_refusal_not_a_guess() {
    with_world!(|w| {
        f3_world(&mut w).await;
        f3_scan(&mut w, "CANDIDATE", None).await;
        w.books(spread_book("30", "6"));
        w.index.set_all(None);
        let id = w.tick(at(entry(), 9, 20)).await.raised[0].clone();
        let plan = w.plan_row(&id).await;
        assert_eq!(tcol(&plan, "status"), "REJECTED_STRUCTURE");
        assert!(tcol(&plan, "reason").contains("no live quote"));
    });
}

#[tokio::test]
async fn entry_the_confirm_fills_the_wing_first_and_opens_the_spread_with_its_level() {
    with_world!(|w| {
        f3_world(&mut w).await;
        let (plan_id, pid) = open_f3(&mut w).await;
        assert_eq!(w.fill_roles(&plan_id).await, [WING, SHORT]);
        let pos = w.position(pid).await;
        assert_eq!(
            (
                tcol(&pos, "structure"),
                icol(&pos, "lots"),
                icol(&pos, "lot_size")
            ),
            ("CREDIT_SPREAD".into(), 1, LOT)
        );
        assert_eq!(dcol(&pos, "stop_price"), level());
        assert_eq!(dcol(&pos, "entry_credit"), d("23.25"));
        assert_eq!(dcol(&pos, "profit_take_points"), d("4.65"));
        assert_eq!(dcol(&pos, "loss_close_points"), d("46.50"));
        let legs = jcol(&pos, "legs");
        let Some(PyValue::Map(carry)) = legs.get("carry") else {
            panic!()
        };
        assert_eq!(jtext(carry, "direction"), "UP");
        assert_eq!(w.broker_calls(), 0);
    });
}

// --- the exit -----------------------------------------------------------------------------------

#[tokio::test]
async fn exit_a_level_break_with_the_flag_off_is_raised_for_the_click_not_sent() {
    with_world!(|w| {
        f3_world(&mut w).await;
        let (plan_id, pid) = open_f3(&mut w).await;
        let day = before(nov(), 5);
        w.index_at("24100", "24200");
        assert!(w.tick(at(day, 11, 0)).await.actions.is_empty()); // above the level
        w.index_at("23970", "24200"); // beyond 24,000 by > 0.10 %
        let report = w.tick(at(day, 11, 1)).await;
        assert_eq!(report.actions, vec![format!("{plan_id}-X")]);
        let exit = w.plan_row(&format!("{plan_id}-X")).await;
        assert_eq!(
            (tcol(&exit, "kind"), tcol(&exit, "status")),
            ("EXIT".into(), "ISSUED".into())
        );
        let detail = jcol(&exit, "detail");
        assert_eq!(jtext(&detail, "code"), "LEVEL_BREAK");
        assert_eq!(detail.get("awaits_click"), Some(&PyValue::Bool(true)));
        assert!(w.fills(&format!("{plan_id}-X")).await.is_empty() && report.done.is_empty());
        assert!(is_null(&w.position(pid).await, "closed_at"));
        assert!(w.alerts().iter().any(|a| a.contains("waits for the click")));
        assert!(w.tick(at(day, 11, 2)).await.actions.is_empty()); // raised once
    });
}

#[tokio::test]
async fn exit_the_click_sends_the_held_exit_short_first() {
    with_world!(|w| {
        f3_world(&mut w).await;
        let (plan_id, pid) = open_f3(&mut w).await;
        let day = before(nov(), 5);
        w.index_at("23970", "24200");
        w.tick(at(day, 11, 1)).await;
        let out = w
            .click_exit(&format!("{plan_id}-X"), at(day, 11, 3))
            .await
            .unwrap();
        assert_eq!(out.outcome, "CLOSED");
        assert_eq!(w.fill_roles(&format!("{plan_id}-X")).await, [SHORT, WING]);
        let pos = w.position(pid).await;
        assert!(!is_null(&pos, "closed_at"));
        assert_eq!(tcol(&pos, "closed_reason"), "LEVEL_BREAK");
        assert_eq!(tcol(&w.plan_row(&plan_id).await, "status"), "CLOSED");
        assert_eq!(w.broker_calls(), 0);
    });
}

#[tokio::test]
async fn exit_with_the_flag_on_the_monitor_sends_the_level_break_itself() {
    with_world!(|w| {
        f3_world(&mut w).await;
        let (plan_id, pid) = open_f3(&mut w).await;
        w.auto_exit.store(true, Ordering::SeqCst);
        let day = before(nov(), 5);
        w.index_at("23970", "24200");
        let report = w.tick(at(day, 11, 1)).await;
        assert_eq!(report.actions, vec![format!("{plan_id}-X")]);
        assert_eq!(
            report
                .done
                .iter()
                .map(|o| o.outcome.clone())
                .collect::<Vec<_>>(),
            ["CLOSED"]
        );
        let exit = w.plan_row(&format!("{plan_id}-X")).await;
        assert_eq!(tcol(&exit, "status"), "CLOSED");
        assert!(!is_null(&exit, "confirmed_at"));
        assert_eq!(w.fill_roles(&format!("{plan_id}-X")).await, [SHORT, WING]);
        assert_eq!(tcol(&w.position(pid).await, "closed_reason"), "LEVEL_BREAK");
        assert_eq!(w.broker_calls(), 0);
    });
}

#[tokio::test]
async fn exit_the_loss_cut_at_twice_the_credit() {
    with_world!(|w| {
        f3_world(&mut w).await;
        let (plan_id, pid) = open_f3(&mut w).await;
        w.auto_exit.store(true, Ordering::SeqCst);
        let day = before(nov(), 6);
        w.books(spread_book("48", "3")); // mark 45 < 46.50
        assert!(w.tick(at(day, 13, 0)).await.actions.is_empty());
        w.books(spread_book("52", "4")); // mark 48 >= 46.50
        assert_eq!(
            w.tick(at(day, 13, 1)).await.actions,
            vec![format!("{plan_id}-X")]
        );
        assert_eq!(tcol(&w.position(pid).await, "closed_reason"), "LOSS_CUT");
    });
}

#[tokio::test]
async fn exit_the_decay_target_at_eighty_percent() {
    with_world!(|w| {
        f3_world(&mut w).await;
        let (plan_id, pid) = open_f3(&mut w).await;
        w.auto_exit.store(true, Ordering::SeqCst);
        let day = before(nov(), 4);
        w.books(spread_book("6", "1")); // mark 5 > 4.65
        assert!(w.tick(at(day, 14, 0)).await.actions.is_empty());
        w.books(spread_book("5.5", "1")); // mark 4.5 <= 4.65
        assert_eq!(
            w.tick(at(day, 14, 1)).await.actions,
            vec![format!("{plan_id}-X")]
        );
        assert_eq!(
            tcol(&w.position(pid).await, "closed_reason"),
            "DECAY_TARGET"
        );
    });
}

#[tokio::test]
async fn exit_the_hard_exit_on_expiry_day_at_1500() {
    with_world!(|w| {
        f3_world(&mut w).await;
        let (plan_id, pid) = open_f3(&mut w).await;
        w.auto_exit.store(true, Ordering::SeqCst);
        assert!(w.tick(at(hard_exit(), 15, 0)).await.actions.is_empty()); // E-1 is not F3's day
        assert!(w.tick(at(nov(), 14, 59)).await.actions.is_empty());
        assert_eq!(
            w.tick(at(nov(), 15, 0)).await.actions,
            vec![format!("{plan_id}-X")]
        );
        assert_eq!(tcol(&w.position(pid).await, "closed_reason"), "HARD_EXIT");
    });
}

#[tokio::test]
async fn exit_the_click_refuses_a_non_exit_and_a_second_click() {
    with_world!(|w| {
        f3_world(&mut w).await;
        let (plan_id, _pid) = open_f3(&mut w).await;
        assert_eq!(
            code_of(w.click_exit(&plan_id, at(entry(), 9, 30)).await),
            "NOT_AN_EXIT"
        );
        let day = before(nov(), 5);
        w.index_at("23970", "24200");
        w.tick(at(day, 11, 1)).await;
        w.click_exit(&format!("{plan_id}-X"), at(day, 11, 3))
            .await
            .unwrap();
        assert_eq!(
            code_of(w.click_exit(&format!("{plan_id}-X"), at(day, 11, 4)).await),
            "NOT_ISSUED"
        );
    });
}

// --- the add ------------------------------------------------------------------------------------

#[tokio::test]
async fn add_is_raised_the_next_session_only_while_working_and_confirmed_by_click() {
    with_world!(|w| {
        f3_world(&mut w).await;
        let (plan_id, pid) = open_f3(&mut w).await;
        let nxt = before(nov(), 11);
        f3_scan(&mut w, "OPEN_POSITION", Some(entry())).await; // last night's read: still UP
        w.books(spread_book("20", "3")); // mark 17 <= 18.60: working
        assert!(
            w.tick(at(entry(), 9, 30)).await.raised.is_empty(),
            "not on the entry session"
        );
        let raised = w.tick(at(nxt, 9, 20)).await.raised;
        assert_eq!(raised, vec![format!("{plan_id}-A1")]);
        let add = w.plan_row(&format!("{plan_id}-A1")).await;
        assert_eq!(
            (
                tcol(&add, "kind"),
                tcol(&add, "status"),
                icol(&add, "lots"),
                tcol(&add, "parent_plan_id")
            ),
            ("ADD".into(), "ISSUED".into(), 1, plan_id.clone())
        );
        assert!(w.fills(&format!("{plan_id}-A1")).await.is_empty()); // a click, never the monitor's
        let legs = w
            .rows(
                "SELECT role, side, quantity FROM fo_leg WHERE plan_id = ? ORDER BY entry_seq",
                &[Param::Int(icol(&add, "id"))],
            )
            .await;
        assert_eq!(
            legs.iter()
                .map(|l| (tcol(l, "role"), tcol(l, "side")))
                .collect::<Vec<_>>(),
            vec![
                (WING.to_owned(), "BUY".to_owned()),
                (SHORT.to_owned(), "SELL".to_owned())
            ]
        );
        let out = w
            .confirm(&format!("{plan_id}-A1"), at(nxt, 9, 22))
            .await
            .unwrap();
        assert_eq!(out.outcome, "OPEN");
        assert_eq!(out.position_id, Some(pid));
        let pos = w.position(pid).await;
        assert_eq!(icol(&pos, "lots"), 2);
        assert_eq!(dcol(&pos, "entry_credit"), d("19.75"));
        let legs = jcol(&pos, "legs");
        let Some(PyValue::Map(carry)) = legs.get("carry") else {
            panic!()
        };
        assert_eq!(carry.get("added"), Some(&PyValue::Bool(true)));
        let Some(PyValue::List(ls)) = legs.get("legs") else {
            panic!()
        };
        assert!(
            ls.iter().all(
                |l| matches!(l, PyValue::Map(m) if py::py_int(m.get("quantity"), 0) == 2 * LOT)
            )
        );
        assert!(
            w.tick(at(nxt, 9, 25)).await.raised.is_empty(),
            "one add per position"
        );
        assert_eq!(w.broker_calls(), 0);
    });
}

#[tokio::test]
async fn add_none_while_the_trade_is_not_working() {
    with_world!(|w| {
        f3_world(&mut w).await;
        let (plan_id, _pid) = open_f3(&mut w).await;
        f3_scan(&mut w, "OPEN_POSITION", Some(entry())).await;
        w.books(spread_book("24", "4")); // mark 20 > 18.60
        assert!(w.tick(at(before(nov(), 11), 9, 20)).await.raised.is_empty());
        assert!(!w.plan_exists(&format!("{plan_id}-A1")).await);
    });
}

// --- the night and the page ---------------------------------------------------------------------

#[tokio::test]
async fn night_the_nightly_mark_is_the_settles_spread() {
    with_world!(|w| {
        f3_world(&mut w).await;
        let (_plan_id, pid) = open_f3(&mut w).await;
        let key = |r: &str| -> PrintKey {
            (
                "NIFTY".into(),
                nov(),
                Some(Decimal::from(strike(r))),
                "PE".into(),
            )
        };
        w.market.land(
            entry(),
            [
                (
                    key(SHORT),
                    Print {
                        close: d("20"),
                        settle: d("20"),
                    },
                ),
                (
                    key(WING),
                    Print {
                        close: d("5"),
                        settle: d("5"),
                    },
                ),
            ],
        );
        let report = w.night(entry()).await;
        assert_eq!(report.marked, 1);
        let mark = w
            .rows(
                "SELECT mark_points, pnl_inr FROM fo_mark WHERE position_id = ?",
                &[Param::Int(pid)],
            )
            .await;
        assert_eq!(dcol(&mark[0], "mark_points"), d("15.00"));
        assert_eq!(dcol(&mark[0], "pnl_inr"), d("536.25")); // (23.25 - 15) x 65
    });
}

/// `view_sources()` for the page: the world's books and a fixed index level.
struct PageSources {
    books: std::sync::Arc<baskfy_desk::lanes::fno::fno_exec::monitor::seams::SharedBooks>,
    spot: Option<Decimal>,
}

impl LiveSources for PageSources {
    fn quotes<'a>(
        &'a self,
        symbols: &'a [String],
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Live, String>> + Send + 'a>>
    {
        Box::pin(async move {
            Ok(symbols
                .iter()
                .filter_map(|s| self.books.get(s).map(|q| (s.clone(), q)))
                .collect())
        })
    }

    fn spot<'a>(
        &'a self,
        _symbol: &'a str,
    ) -> std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Option<Decimal>, String>> + Send + 'a>,
    > {
        Box::pin(async move { Ok(self.spot) })
    }
}

#[tokio::test]
async fn page_shows_the_spread_the_held_exit_and_the_flag() {
    with_world!(|w| {
        f3_world(&mut w).await;
        let (plan_id, pid) = open_f3(&mut w).await;
        let day = before(nov(), 5);
        w.index_at("23970", "24200");
        w.tick(at(day, 11, 1)).await;
        let settings = Settings::defaults().unwrap();
        let src = PageSources {
            books: w.quotes.clone(),
            spot: Some(d("23970")),
        };
        let uid = w.uid;
        let mut store = FoStore::new(&mut w.conn, uid);
        let view = build_view(&mut store, &settings, at(day, 11, 2), Some(&src), None)
            .await
            .unwrap();
        assert_eq!(view.get("f3_auto_exit"), Some(&PyValue::Bool(false)));
        let Some(PyValue::List(spreads)) = view.get("spreads") else {
            panic!("{view:?}")
        };
        let PyValue::Map(spread) = &spreads[0] else {
            panic!()
        };
        assert_eq!(spread.get("id"), Some(&PyValue::Int(pid)));
        assert_eq!(jtext(spread, "direction"), "UP");
        assert_eq!(jtext(spread, "level"), "24000.00");
        assert_eq!(jtext(spread, "mark"), "24.00");
        assert_eq!(jtext(spread, "decay_target"), "4.65");
        assert!(jtext(spread, "next_rule").contains("out at once if NIFTY trades below 24000.00"));
        let Some(PyValue::List(actions)) = view.get("actions") else {
            panic!()
        };
        let held = actions
            .iter()
            .find_map(|a| match a {
                PyValue::Map(m) if jtext(m, "plan_id") == format!("{plan_id}-X") => Some(m.clone()),
                _ => None,
            })
            .expect("the held exit is an action");
        assert_eq!(held.get("confirmable"), Some(&PyValue::Bool(true)));
        assert_eq!(held.get("awaits_click"), Some(&PyValue::Bool(true)));
        assert!(jtext(&held, "sentence").contains("short first"));
        // the plan card lives on its own day's page
        let src2 = PageSources {
            books: w.quotes.clone(),
            spot: None,
        };
        let entry_view = build_view(&mut store, &settings, at(entry(), 9, 30), Some(&src2), None)
            .await
            .unwrap();
        let Some(PyValue::List(plans)) = entry_view.get("plans") else {
            panic!()
        };
        let plan = plans
            .iter()
            .find_map(|p| match p {
                PyValue::Map(m) if jtext(m, "plan_id") == plan_id => Some(m.clone()),
                _ => None,
            })
            .unwrap();
        assert_eq!(jtext(&plan, "group"), "F3");
        assert!(jtext(&plan, "sentence").contains("BASKFY_FNO_F3_AUTO_EXIT"));
        assert_eq!(py::dec_py(plan.get("level")), Some(level()));
        assert_eq!(jtext(&plan, "direction"), "UP");
        let Some(PyValue::Map(modes)) = view.get("modes") else {
            panic!()
        };
        assert_eq!(jtext(modes, "F3N"), "PAPER");
    });
}

#[test]
fn the_flags_default_false_and_the_monitor_source_names_no_auto_flag() {
    let s = Settings::defaults().unwrap();
    assert!(!f3_auto_exit_enabled(&s));
    assert_eq!(
        fno_gates(&s, FoSleeve::F3N).mode,
        baskfy_core::options::gating::Mode::Paper
    );
    let re = regex::Regex::new(r"FNO_\w*AUTO").unwrap();
    for (name, src) in [
        (
            "execute.rs",
            include_str!("../src/lanes/fno/fno_exec/execute.rs"),
        ),
        (
            "engine.rs",
            include_str!("../src/lanes/fno/fno_exec/monitor/engine.rs"),
        ),
        (
            "night.rs",
            include_str!("../src/lanes/fno/fno_exec/monitor/night.rs"),
        ),
        (
            "seams.rs",
            include_str!("../src/lanes/fno/fno_exec/monitor/seams.rs"),
        ),
    ] {
        assert!(!re.is_match(src), "{name}");
    }
    let _ = NaiveDate::MIN;
}
