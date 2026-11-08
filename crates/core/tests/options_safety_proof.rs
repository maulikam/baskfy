//! Port of the pure parts of `tests/test_options_safety_proof.py` (OP13): never overnight.
//! Not ported: the executor never-naked property (executor.py is not in Rust), the source
//! scans and the Beat/doc checks (they read Python, TS and Markdown files, not core logic).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, TimeDelta};
use rust_decimal::Decimal;

use baskfy_core::options::bars::ist_offset;
use baskfy_core::options::config::{OptionsConfig, Sleeve};
use baskfy_core::options::execution::LegRole;
use baskfy_core::options::exits::{IndexState, LegMark, OpenLeg, OpenPosition, evaluate};
use baskfy_core::options::num::combine;
use baskfy_core::options::structures::{Direction, Structure};
use options_common::{Rng, d, dec};

const QTY: i64 = 65;

fn roles(sleeve: Sleeve) -> Vec<LegRole> {
    match sleeve {
        Sleeve::O1m | Sleeve::O1w => vec![
            LegRole::ShortCall,
            LegRole::ShortPut,
            LegRole::LongCall,
            LegRole::LongPut,
        ],
        Sleeve::O2 => vec![LegRole::LongCall],
        Sleeve::O3a => vec![LegRole::LongCall, LegRole::ShortCall],
        Sleeve::O3b => vec![LegRole::LongPut, LegRole::ShortPut],
    }
}

fn hard_exit_ceiling() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 0, 0).unwrap()
}

fn day() -> NaiveDate {
    d(2026, 10, 27)
}

fn position(sleeve: Sleeve) -> OpenPosition {
    let roles = roles(sleeve);
    let structure = match roles.len() {
        4 => Structure::IronCondor,
        1 => Structure::LongOption,
        _ => Structure::DebitSpread,
    };
    let legs = roles
        .iter()
        .enumerate()
        .map(|(i, role)| {
            let i = i64::try_from(i).unwrap();
            OpenLeg {
                role: *role,
                strike: Decimal::from(25000 + 100 * i),
                quantity: QTY,
                fill_price: dec("20.00"),
                instrument_token: i + 1,
            }
        })
        .collect();
    OpenPosition {
        sleeve,
        structure,
        legs,
        entry_points: dec("20.00"),
        opened_at: combine(
            day(),
            NaiveTime::from_hms_opt(10, 5, 0).unwrap(),
            ist_offset(),
        ),
        risk_budget_inr: Decimal::from(50000),
        direction: Some(Direction::Up),
        range_high: Some(Decimal::from(25100)),
        range_low: Some(Decimal::from(24900)),
        half_gap: Some(Decimal::from(24950)),
        width_points: None,
    }
}

#[test]
fn no_sleeve_holds_past_the_ceiling() {
    let options = OptionsConfig::default();
    for sleeve in Sleeve::ALL {
        assert!(
            options.hard_exit_time(sleeve) <= hard_exit_ceiling(),
            "{sleeve:?}"
        );
    }
}

struct Case {
    sleeve: Sleeve,
    minutes_after: i64,
    bid: Option<Decimal>,
    ask: Option<Decimal>,
    spot: Option<Decimal>,
    tick_age: i64,
}

fn check(case: &Case, options: &OptionsConfig) {
    let hard: DateTime<FixedOffset> =
        combine(day(), options.hard_exit_time(case.sleeve), ist_offset());
    let now = hard + TimeDelta::minutes(case.minutes_after);
    let pos = position(case.sleeve);
    let marks: BTreeMap<LegRole, LegMark> = pos
        .legs
        .iter()
        .map(|lg| {
            (
                lg.role,
                LegMark {
                    bid: case.bid,
                    ask: case.ask,
                    at: now,
                },
            )
        })
        .collect();
    let index = IndexState {
        spot: case.spot,
        last_tick_at: Some(now - TimeDelta::seconds(case.tick_age)),
        bars: Vec::new(),
    };
    let verdict = evaluate(&pos, &marks, &index, &now, options, false).unwrap();
    assert!(
        verdict.is_some(),
        "a position was held at or past its hard exit: {:?} +{}m bid {:?} ask {:?} spot {:?} age {}",
        case.sleeve,
        case.minutes_after,
        case.bid,
        case.ask,
        case.spot,
        case.tick_age
    );
}

fn price(rng: &mut Rng) -> Option<Decimal> {
    // st.one_of(st.none(), st.decimals("0.05", "500", places=2))
    if rng.below(4) == 0 {
        None
    } else {
        Some(Decimal::new(
            5 + i64::try_from(rng.below(49_996)).unwrap(),
            2,
        ))
    }
}

#[test]
fn at_or_after_the_hard_exit_every_position_closes() {
    let options = OptionsConfig::default();
    let edge_prices = [None, Some(dec("0.05")), Some(Decimal::from(500))];
    let edge_spots = [None, Some(Decimal::from(24000)), Some(Decimal::from(26000))];
    for sleeve in Sleeve::ALL {
        for minutes_after in [0, 30] {
            for bid in edge_prices {
                for ask in edge_prices {
                    for spot in edge_spots {
                        for tick_age in [0, 600] {
                            check(
                                &Case {
                                    sleeve,
                                    minutes_after,
                                    bid,
                                    ask,
                                    spot,
                                    tick_age,
                                },
                                &options,
                            );
                        }
                    }
                }
            }
        }
    }
    let mut rng = Rng(0x0513_0001);
    for _ in 0..300 {
        let sleeve = Sleeve::ALL[rng.below(Sleeve::ALL.len())];
        let minutes_after = i64::try_from(rng.below(31)).unwrap();
        let bid = price(&mut rng);
        let ask = price(&mut rng);
        let spot = if rng.below(4) == 0 {
            None
        } else {
            Some(Decimal::new(
                2_400_000 + i64::try_from(rng.below(200_001)).unwrap(),
                2,
            ))
        };
        let tick_age = i64::try_from(rng.below(601)).unwrap();
        check(
            &Case {
                sleeve,
                minutes_after,
                bid,
                ask,
                spot,
                tick_age,
            },
            &options,
        );
    }
}
