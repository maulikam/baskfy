//! `tests/test_fno_safety.py`'s gateway halves, ported as spec (FO11): the flag matrix at the
//! gateway (`TestTheFlagMatrixAtTheGateway`) and `INTRADAY_ENABLED` never admitting an FO order
//! (`TestIntradayIsNotAnFoFlag`), plus the same matrix for F2's GTT stop, which the FO lane sends
//! through the gateway's GTT branch.
//!
//! **pinned** is the desk's FO gateway exactly as `build_fo_gateway` builds it: the sleeve's
//! `paper_gates` (dry_run pinned true, FO7.1) read per order, and the covered-overnight rule.
//! **unpinned** is the same gateway on `product_gates` alone — the control showing that the pin
//! (and `execute_entry`'s `LIVE_NOT_BUILT`) is what stands between a LIVE row and the broker.
//! Every broker is the replay Kite, which records each write call and sends nothing.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;

use baskfy_core::fno::config::{FoSleeve, FoSleeveGroup, group_of};
use baskfy_core::fno::covered::OptionPosition;
use baskfy_core::fno::vol::OptionType;
use chrono::{NaiveDate, TimeZone};
use rust_decimal::Decimal;

use crate::clock::{Clock, FixedClock, ist};
use crate::fake_kite::ReplayKite;
use crate::gateway::{FoPlanRef, GatesFn, GttStop, PlaceOrder, TenantIds};
use crate::ids::HarnessIds;
use crate::lanes::exec_core::guards::{SignedLeg, core_coverage};
use crate::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use crate::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskLayer, RiskManager};
use crate::lanes::fno::gates::{mode_of, paper_gates, product_gates};
use crate::settings::Settings;
use crate::wire::{PyDict, PyValue};
use baskfy_core::options::gating::Mode;

const FIXTURE: &str =
    r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#;
const LOT: i64 = 65;
const LONG_PUT: i64 = 22600;
const FUT_NOV: &str = "RELIANCE26NOVFUT";

fn nov() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 11, 24).unwrap()
}

fn underlying(sleeve: FoSleeve) -> &'static str {
    match sleeve {
        FoSleeve::F1N | FoSleeve::F3N => "NIFTY",
        FoSleeve::F1B | FoSleeve::F3B => "BANKNIFTY",
        FoSleeve::F2 => "RELIANCE",
    }
}

fn tenant() -> TenantIds {
    TenantIds::new(1, 1).unwrap()
}

fn clock() -> Arc<dyn Clock> {
    Arc::new(FixedClock::new(
        ist().with_ymd_and_hms(2026, 11, 3, 11, 0, 0).unwrap(),
    ))
}

/// `set_flags(...)`: the four switches for `sleeve` (the other groups' flags off) and
/// `INTRADAY_ENABLED`; `true` when the row is LIVE.
fn set_flags(
    sleeve: FoSleeve,
    dry_off: bool,
    options: bool,
    carry: bool,
    execution: bool,
    intraday: bool,
) -> (Settings, bool) {
    let mut s = Settings::defaults().unwrap();
    s.dry_run = !dry_off;
    s.options_enabled = options;
    s.fno_carry_enabled = carry;
    s.intraday_enabled = intraday;
    s.fno_f1_execution_enabled = false;
    s.fno_f2_execution_enabled = false;
    s.fno_f3_execution_enabled = false;
    match group_of(sleeve) {
        FoSleeveGroup::F1 => s.fno_f1_execution_enabled = execution,
        FoSleeveGroup::F2 => s.fno_f2_execution_enabled = execution,
        FoSleeveGroup::F3 => s.fno_f3_execution_enabled = execution,
    }
    (s, dry_off && options && carry && execution)
}

fn rows() -> Vec<(bool, bool, bool, bool)> {
    (0..16u8)
        .map(|b| (b & 1 != 0, b & 2 != 0, b & 4 != 0, b & 8 != 0))
        .collect()
}

struct Bed {
    dir: tempfile::TempDir,
    kite: Arc<ReplayKite>,
}

impl Bed {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("kite.json"), FIXTURE).unwrap();
        std::fs::create_dir(dir.path().join("instruments")).unwrap();
        let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
        Self { dir, kite }
    }

    async fn gateway(&self, gates: GatesFn, name: &str) -> OrderGateway {
        let risk: Arc<dyn RiskLayer> = Arc::new(
            RiskManager::new(RiskConfig::default(), clock(), RiskBacking::Memory)
                .await
                .unwrap(),
        );
        // The replay Kite coerces to the gateway's broker at the call: no write handle is named.
        OrderGateway::new(
            self.kite.clone(),
            risk,
            GatewayOptions {
                gates,
                journal_path: self.dir.path().join(format!("{name}.jsonl")),
                stop_band: None,
                coverage: Some(core_coverage()),
                clock: clock(),
                ids: Arc::new(HarnessIds::new("fno-exec-safety")),
                limits: None,
            },
        )
        .unwrap()
    }

    /// `build_fo_gateway(sleeve, ...)`: the sleeve's paper-pinned switches, read per order.
    async fn pinned(&self, s: &Settings, sleeve: FoSleeve) -> OrderGateway {
        let s = s.clone();
        self.gateway(Arc::new(move || paper_gates(&s, sleeve)), "pinned")
            .await
    }

    /// The same gateway on `product_gates` alone, without FO7.1's pin.
    async fn unpinned(&self, s: &Settings, sleeve: FoSleeve) -> OrderGateway {
        let s = s.clone();
        self.gateway(Arc::new(move || product_gates(&s, sleeve)), "bare")
            .await
    }

    fn calls(&self) -> usize {
        self.kite.write_calls().len()
    }
}

/// `fo_leg(sleeve, product=, with_ref=)`: the first leg a confirm would send — F1's/F3's long
/// put (always covered), F2's future.
fn fo_leg(sleeve: FoSleeve, product: &str, with_ref: bool) -> PlaceOrder {
    let u = underlying(sleeve);
    let plan_id = format!("{}-20261103-{u}-u1", sleeve.value());
    let (symbol, fo_plan, qty) = if group_of(sleeve) == FoSleeveGroup::F2 {
        let r = FoPlanRef {
            plan_id: plan_id.clone(),
            sleeve: sleeve.value().to_owned(),
            step: None,
            broker_positions: Vec::new(),
            plan_filled: Vec::new(),
        };
        (FUT_NOV.to_owned(), r, 500)
    } else {
        let step = OptionPosition {
            underlying: u.to_owned(),
            expiry: nov(),
            option_type: OptionType::Pe,
            strike: Decimal::from(LONG_PUT),
            quantity: LOT,
        };
        let r = FoPlanRef {
            plan_id: plan_id.clone(),
            sleeve: sleeve.value().to_owned(),
            step: Some(
                SignedLeg {
                    underlying: step.underlying.clone(),
                    expiry: step.expiry,
                    option_type: "PE".to_owned(),
                    strike: step.strike,
                    quantity: step.quantity,
                }
                .to_value(),
            ),
            broker_positions: Vec::new(),
            plan_filled: Vec::new(),
        };
        (format!("{u}26NOV{LONG_PUT}PE"), r, LOT)
    };
    PlaceOrder {
        client_id: Some(format!("{plan_id}:{symbol}:{product}")),
        symbol,
        qty,
        side: "BUY".to_owned(),
        product: product.to_owned(),
        order_type: "LIMIT".to_owned(),
        price: Some(10.0),
        exchange: "NFO".to_owned(),
        gross_exposure: Some(10.0 * qty as f64),
        reference_price: Some(10.0),
        fo_plan: with_ref.then_some(fo_plan),
        tenant: Some(tenant()),
        plan_tenant: Some(tenant()),
        ..PlaceOrder::default()
    }
}

fn status(out: &PyDict) -> String {
    out.get("status").map(PyValue::py_str).unwrap_or_default()
}

fn error(out: &PyDict) -> String {
    out.get("error").map(PyValue::py_str).unwrap_or_default()
}

#[tokio::test]
async fn sixteen_rows_twice_and_the_fo_gateway_never_reaches_the_broker() {
    for sleeve in FoSleeve::ALL {
        for (dry_off, options, carry, execution) in rows() {
            for intraday in [false, true] {
                let row = (sleeve, dry_off, options, carry, execution, intraday);
                let (s, live) = set_flags(sleeve, dry_off, options, carry, execution, intraday);
                assert_eq!(mode_of(&s, sleeve) == Mode::Live, live, "{row:?}");
                let order = fo_leg(sleeve, "NRML", true);
                let bed = Bed::new();
                let out = bed.pinned(&s, sleeve).await.place(&order).await.unwrap();
                assert_eq!(bed.calls(), 0, "FO7.1: paper in every row {row:?}");
                let half_flip = execution && !(options && carry);
                if half_flip {
                    let st = status(&out);
                    assert!(st != "DRY_RUN" && st != "PLACED", "{row:?} {out:?}");
                } else {
                    assert_eq!(status(&out), "DRY_RUN", "{row:?} {out:?}");
                }
                let bare = Bed::new();
                let out = bare.unpinned(&s, sleeve).await.place(&order).await.unwrap();
                if live {
                    // The control: with all four on, only the pin stands before the broker.
                    assert_eq!(bare.calls(), 1, "{row:?} {out:?}");
                } else {
                    assert_eq!(bare.calls(), 0, "{row:?} {out:?}");
                }
                if half_flip {
                    let why = error(&out);
                    assert!(
                        why.contains("OPTIONS_ENABLED") || why.contains("BASKFY_FNO_CARRY_ENABLED"),
                        "{row:?} {out:?}"
                    );
                }
            }
        }
    }
}

#[tokio::test]
async fn an_f2_gtt_stop_through_the_fo_gateway_never_reaches_the_broker() {
    // `place_future_stop`'s GTT goes through the same derived gateway's GTT branch; on paper it
    // is simulated in every row of the matrix, LIVE included (FO7.1).
    for (dry_off, options, carry, execution) in rows() {
        for intraday in [false, true] {
            let (s, _live) = set_flags(FoSleeve::F2, dry_off, options, carry, execution, intraday);
            let bed = Bed::new();
            let gw = bed.pinned(&s, FoSleeve::F2).await;
            let plan_id = "F2-20261103-RELIANCE-u1";
            let _ = gw
                .place_gtt_stop(&GttStop {
                    symbol: FUT_NOV.to_owned(),
                    qty: 500,
                    trigger: 1340.0,
                    last_price: 1400.0,
                    exchange: "NFO".to_owned(),
                    series: None,
                    client_id: Some(format!("{plan_id}:{FUT_NOV}:GTT")),
                    tenant: Some(tenant()),
                    plan_tenant: Some(tenant()),
                    limit_fraction: None,
                    fo_plan: Some(FoPlanRef {
                        plan_id: plan_id.to_owned(),
                        sleeve: "F2".to_owned(),
                        step: None,
                        broker_positions: Vec::new(),
                        plan_filled: Vec::new(),
                    }),
                })
                .await;
            assert_eq!(
                bed.calls(),
                0,
                "dry_off={dry_off} options={options} carry={carry} execution={execution} \
                 intraday={intraday}"
            );
        }
    }
}

#[test]
fn product_gates_never_carry_intraday() {
    for sleeve in FoSleeve::ALL {
        let (s, live) = set_flags(sleeve, true, true, true, true, true);
        assert!(live);
        assert_eq!(mode_of(&s, sleeve), Mode::Live);
        assert!(!product_gates(&s, sleeve).intraday_enabled);
        assert!(!paper_gates(&s, sleeve).intraday_enabled);
    }
}

#[tokio::test]
async fn only_nrml_with_a_plan_is_an_fo_order() {
    for sleeve in FoSleeve::ALL {
        let (s, _) = set_flags(sleeve, true, true, true, true, true);
        for (product, with_ref) in [
            ("MIS", true),
            ("NRML", false),
            ("CNC", true),
            ("MIS", false),
        ] {
            for pinned in [true, false] {
                let bed = Bed::new();
                let gw = if pinned {
                    bed.pinned(&s, sleeve).await
                } else {
                    bed.unpinned(&s, sleeve).await
                };
                let out = gw.place(&fo_leg(sleeve, product, with_ref)).await.unwrap();
                let st = status(&out);
                assert!(
                    st != "DRY_RUN" && st != "PLACED",
                    "{sleeve:?} {product} ref={with_ref} pinned={pinned}: {out:?}"
                );
                assert_eq!(bed.calls(), 0, "{sleeve:?} {product} pinned={pinned}");
            }
        }
    }
}
