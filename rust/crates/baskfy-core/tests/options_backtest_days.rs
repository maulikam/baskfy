//! Port of `tests/test_options_backtest_days.py` (OP12, `04` §13): the backtest days and runs, by the live code.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::BTreeMap;
use std::path::PathBuf;

use baskfy_core::decimal::Rounding;
use baskfy_core::options::backtest::{TIER1_CAVEAT, TIER2_CAVEAT, TIER2_O2_CAVEAT, Tier};
use baskfy_core::options::backtest_suite::{run_tier, thresholds, tier1_sensitivity};
use baskfy_core::options::bars::{Bar, closed};
use baskfy_core::options::chain::{Level, OptionQuote};
use baskfy_core::options::config::{OptionType, OptionsCeilings, OptionsConfig, Side, Sleeve};
use baskfy_core::options::costs::{CostFill, charges};
use baskfy_core::options::num;
use baskfy_core::options::replay_day::{
    ModelChain, SnapshotSource, StoredChain, priced_day, tier1_day,
};
use baskfy_core::options::scan::{DayContext, MarketDay, Snapshot};
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime};
use options_common as fx;
use rust_decimal::Decimal;

fn cfg() -> OptionsConfig {
    OptionsConfig::default()
}

fn ceil() -> OptionsCeilings {
    OptionsCeilings::default()
}

fn quiet() -> MarketDay {
    fx::market(fx::quiet_monthly(), fx::quiet_monthly_bars())
}

fn trend() -> MarketDay {
    fx::market(fx::trend_weekly(), fx::trend_weekly_bars())
}

fn gap() -> MarketDay {
    fx::market(fx::gap_hold(), fx::gap_hold_bars())
}

fn up() -> MarketDay {
    fx::market(fx::o2_up_break(), fx::o2_up_break_bars())
}

fn all() -> Vec<MarketDay> {
    vec![quiet(), trend(), gap(), up()]
}

fn hm(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap()
}

/// The collector's minute at a 14 % flat vol (the OP8 fixture).
fn chain(bars: &[Bar], minute: DateTime<FixedOffset>) -> Snapshot {
    let spot = closed(bars, &minute).last().unwrap().close;
    let day = minute.date_naive();
    let mut expiries: Vec<NaiveDate> = fx::expiries_fixture()
        .into_iter()
        .filter(|e| *e >= day)
        .collect();
    expiries.sort();
    expiries.truncate(2);
    let forwards: BTreeMap<NaiveDate, Decimal> = expiries
        .iter()
        .map(|e| (*e, fx::carry_forward(spot, minute, *e)))
        .collect();
    fx::scan_snapshot(minute, spot, &expiries, &fx::flat(0.14), Some(&forwards))
}

fn model(market: &MarketDay) -> Box<dyn SnapshotSource> {
    Box::new(ModelChain::new(market.clone(), cfg()))
}

// --- Tier 1 ---

#[test]
fn each_fixture_day_by_the_sleeves_own_gate() {
    let ctx = DayContext::default();
    let q = tier1_day(Sleeve::O1m, &quiet(), &ctx, &cfg())
        .unwrap()
        .unwrap();
    assert!(q.traded && q.trigger_time == Some(hm(10, 0)));
    let t = tier1_day(Sleeve::O1w, &trend(), &ctx, &cfg())
        .unwrap()
        .unwrap();
    assert_eq!(
        (t.traded, t.skip_reason.as_deref()),
        (false, Some("NOT_CONTAINED"))
    );
    let g = tier1_day(Sleeve::O3b, &gap(), &ctx, &cfg())
        .unwrap()
        .unwrap();
    assert!(g.traded && g.trigger_time == Some(hm(9, 45)));
}

#[test]
fn o2_and_o3a_record_the_index_move_after_the_trigger() {
    let ctx = DayContext::default();
    let o2 = tier1_day(Sleeve::O2, &up(), &ctx, &cfg()).unwrap().unwrap();
    assert!(o2.traded && o2.trigger_time == Some(hm(10, 4)));
    assert!(o2.mfe_points.unwrap() > Decimal::ZERO);
    assert_eq!(o2.mae_points, Some(Decimal::ZERO));
    assert!(o2.net_pnl_inr.is_none() && o2.r_multiple.is_none());
    let o3a = tier1_day(Sleeve::O3a, &trend(), &ctx, &cfg())
        .unwrap()
        .unwrap();
    assert_eq!(o3a.trigger_time, Some(hm(10, 19)));
}

#[test]
fn a_day_that_is_not_the_sleeves_is_not_a_session() {
    let ctx = DayContext::default();
    assert!(
        tier1_day(Sleeve::O1m, &trend(), &ctx, &cfg())
            .unwrap()
            .is_none()
    );
    assert!(
        tier1_day(Sleeve::O3a, &up(), &ctx, &cfg())
            .unwrap()
            .is_none()
    );
}

#[test]
fn an_event_day_is_a_skipped_session() {
    let ctx = DayContext {
        event_days: vec![fx::gap_hold()],
        ..DayContext::default()
    };
    let outcome = tier1_day(Sleeve::O3b, &gap(), &ctx, &cfg())
        .unwrap()
        .unwrap();
    assert_eq!(outcome.skip_reason.as_deref(), Some("EVENT_DAY"));
}

#[test]
fn no_previous_close_is_a_skipped_session() {
    let mut m = trend();
    m.daily_closes.clear();
    let outcome = tier1_day(Sleeve::O3a, &m, &DayContext::default(), &cfg())
        .unwrap()
        .unwrap();
    assert_eq!(outcome.skip_reason.as_deref(), Some("NO_PREV_CLOSE"));
}

#[test]
fn a_tier_one_run_per_sleeve() {
    for sleeve in Sleeve::ALL {
        let r = run_tier(
            sleeve,
            Tier::Signals,
            &all(),
            &DayContext::default(),
            &cfg(),
            &ceil(),
            None,
        )
        .unwrap();
        assert_eq!((r.sleeve, r.tier), (sleeve, Tier::Signals));
        assert!(r.net_pnl_inr.is_none());
        assert_eq!(r.caveats, vec![TIER1_CAVEAT.to_owned()]);
    }
}

// --- Tier 2 ---

#[test]
fn the_model_chain_trades_the_trend_day_to_target() {
    let t = trend();
    let mut source = ModelChain::new(t.clone(), cfg());
    let outcome = priced_day(
        Sleeve::O3a,
        &t,
        &DayContext::default(),
        &mut source,
        &cfg(),
        &ceil(),
    )
    .unwrap()
    .unwrap();
    assert!(outcome.traded && outcome.closed_reason.as_deref() == Some("TARGET"));
    assert!(outcome.r_multiple.unwrap() > Decimal::ZERO);
}

#[test]
fn a_tier_two_run_carries_its_caveat_and_o2s_extra_sentence() {
    let mut make = model;
    let o2 = run_tier(
        Sleeve::O2,
        Tier::Modelled,
        &[up()],
        &DayContext::default(),
        &cfg(),
        &ceil(),
        Some(&mut make),
    )
    .unwrap();
    assert_eq!(
        o2.caveats,
        vec![TIER2_CAVEAT.to_owned(), TIER2_O2_CAVEAT.to_owned()]
    );
    assert!(o2.traded == 1 && o2.net_pnl_inr.is_some());
}

#[test]
fn a_priced_tier_without_a_source_is_an_error() {
    let e = run_tier(
        Sleeve::O2,
        Tier::Modelled,
        &[up()],
        &DayContext::default(),
        &cfg(),
        &ceil(),
        None,
    )
    .unwrap_err();
    assert!(e.to_string().contains("needs a snapshot source"), "{e}");
}

// --- the planted day ---

fn weekly_call(q: &OptionQuote) -> bool {
    q.expiry == fx::trend_weekly() && q.option_type == OptionType::Ce
}

fn leg(snap: &Snapshot, strike: i64) -> OptionQuote {
    snap.quotes
        .iter()
        .find(|q| q.strike == Decimal::from(strike) && weekly_call(q))
        .cloned()
        .unwrap()
}

fn planted() -> StoredChain {
    let day = fx::trend_weekly();
    let bars = trend().bars;
    let (decision, entry, exit) = (
        fx::at(day, 10, 20),
        fx::at(day, 10, 21),
        fx::at(day, 10, 30),
    );
    let mut at_exit = chain(&bars, exit);
    let plant = |strike: Decimal| -> Option<(Decimal, Decimal)> {
        if strike == Decimal::from(25200) {
            Some((fx::dec("95.00"), fx::dec("95.20")))
        } else if strike == Decimal::from(25300) {
            Some((fx::dec("9.80"), fx::dec("10.00")))
        } else {
            None
        }
    };
    for q in &mut at_exit.quotes {
        if let Some((bid, ask)) = plant(q.strike).filter(|_| weekly_call(q)) {
            q.bid = Some(bid);
            q.ask = Some(ask);
            q.bids = vec![
                Level {
                    price: bid,
                    quantity: 1300
                };
                3
            ];
            q.asks = vec![
                Level {
                    price: ask,
                    quantity: 1300
                };
                3
            ];
        }
    }
    StoredChain {
        minutes: BTreeMap::from([
            (decision, chain(&bars, decision)),
            (entry, chain(&bars, entry)),
            (exit, at_exit),
        ]),
    }
}

#[test]
fn the_planted_day_reproduces_its_r_to_the_paisa() {
    let mut source = planted();
    let t = trend();
    let outcome = priced_day(
        Sleeve::O3a,
        &t,
        &DayContext::default(),
        &mut source,
        &cfg(),
        &ceil(),
    )
    .unwrap()
    .unwrap();
    assert!(outcome.traded && outcome.closed_reason.as_deref() == Some("TARGET"));
    let day = fx::trend_weekly();
    let dsnap = source.at(&fx::at(day, 10, 20)).unwrap().unwrap();
    let esnap = source.at(&fx::at(day, 10, 21)).unwrap().unwrap();
    let (long_d, short_d) = (leg(&dsnap, 25200), leg(&dsnap, 25300));
    let (long_e, short_e) = (leg(&esnap, 25200), leg(&esnap, 25300));
    let lot = Decimal::from(65);
    let r = (long_d.ask.unwrap() - short_d.bid.unwrap()) * lot + Decimal::from(500);
    let fills = [
        CostFill {
            side: Side::Buy,
            price: long_e.ask.unwrap(),
            quantity: 65,
        },
        CostFill {
            side: Side::Sell,
            price: short_e.bid.unwrap(),
            quantity: 65,
        },
        CostFill {
            side: Side::Buy,
            price: fx::dec("10.00"),
            quantity: 65,
        },
        CostFill {
            side: Side::Sell,
            price: fx::dec("95.00"),
            quantity: 65,
        },
    ];
    let gross =
        (fx::dec("95.00") - fx::dec("10.00") - (long_e.ask.unwrap() - short_e.bid.unwrap())) * lot;
    let total = charges(&fills, &cfg().costs).unwrap().total().unwrap();
    let net = num::quantize(gross - total, 2, Rounding::HalfEven).unwrap();
    assert_eq!(outcome.net_pnl_inr, Some(net));
    assert_eq!(
        outcome.r_multiple,
        Some(num::quantize(num::div(net, r).unwrap(), 2, Rounding::HalfEven).unwrap())
    );
}

#[test]
fn tier_three_over_the_planted_snapshots_is_the_same_rupees() {
    let t = trend();
    let mut source = planted();
    let day = priced_day(
        Sleeve::O3a,
        &t,
        &DayContext::default(),
        &mut source,
        &cfg(),
        &ceil(),
    )
    .unwrap()
    .unwrap();
    let stored = planted();
    let mut make = |_: &MarketDay| -> Box<dyn SnapshotSource> { Box::new(stored.clone()) };
    let r = run_tier(
        Sleeve::O3a,
        Tier::Observed,
        &[t],
        &DayContext::default(),
        &cfg(),
        &ceil(),
        Some(&mut make),
    )
    .unwrap();
    assert_eq!(r.net_pnl_inr, day.net_pnl_inr);
    assert_eq!(r.caveats[0], "Observed sample: 1 sessions.");
    assert!(r.caveats[1].starts_with("Insufficient sample: 1 of 20"));
}

#[test]
fn an_empty_stored_chain_closes_the_window() {
    let t = trend();
    let mut source = StoredChain::default();
    let outcome = priced_day(
        Sleeve::O3a,
        &t,
        &DayContext::default(),
        &mut source,
        &cfg(),
        &ceil(),
    )
    .unwrap()
    .unwrap();
    assert!(!outcome.traded);
}

// --- the caveats ---

#[test]
fn tier_one_and_two_are_07s_words_verbatim() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../docs");
    let options_07 = std::fs::read_to_string(root.join("options/07-data-reality.md")).unwrap();
    let condor_07 = std::fs::read_to_string(root.join("condor/07-data-reality.md")).unwrap();
    assert!(options_07.contains(&format!("\"{TIER1_CAVEAT}\"")));
    assert!(condor_07.contains(&format!("\"{TIER2_CAVEAT}\"")));
    let rule = std::fs::read_to_string(root.join("options/04-business-rules.md")).unwrap();
    let flat = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ");
    assert!(flat(&rule).contains(&flat(TIER2_O2_CAVEAT)));
}

// --- one sleeve, one tier ---

#[test]
fn a_run_is_one_sleeve_and_one_tier_and_skips_other_days() {
    let r = run_tier(
        Sleeve::O3b,
        Tier::Signals,
        &all(),
        &DayContext::default(),
        &cfg(),
        &ceil(),
        None,
    )
    .unwrap();
    assert_eq!((r.sleeve, r.tier), (Sleeve::O3b, Tier::Signals));
    assert_eq!((r.sessions, r.traded), (3, 1));
}

// --- sensitivity ---

#[test]
fn every_threshold_at_minus_and_plus_25_percent() {
    for sleeve in Sleeve::ALL {
        let rows = tier1_sensitivity(sleeve, &all(), &DayContext::default(), &cfg()).unwrap();
        assert_eq!(rows.len(), 2 * thresholds(sleeve).len());
        let mut factors: Vec<Decimal> = rows.iter().map(|r| r.factor).collect();
        factors.sort();
        factors.dedup();
        assert_eq!(factors, vec![fx::dec("0.75"), fx::dec("1.25")]);
    }
}

#[test]
fn loosening_a_maximum_never_loses_a_signal() {
    let rows = tier1_sensitivity(Sleeve::O1w, &all(), &DayContext::default(), &cfg()).unwrap();
    for field in ["gap_max_pct", "range_max_pct", "er_max"] {
        let signals = |f: &str| {
            rows.iter()
                .find(|r| r.field == field && r.factor == fx::dec(f))
                .unwrap()
                .signals
        };
        assert!(signals("1.25") >= signals("0.75"));
    }
}
