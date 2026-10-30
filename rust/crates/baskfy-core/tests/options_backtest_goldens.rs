//! Grades the backtest, its suite, the replay day and the executor against every golden in
//! `go/testdata/golden/L1/options`, each called from its stored inputs as `golden_options.py` does.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;
use std::str::FromStr;

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_core::error::CoreResult;
use baskfy_core::options::backtest::{DayOutcome, Tier, run, tier2_quote};
use baskfy_core::options::backtest_suite::{run_tier, tier1_sensitivity};
use baskfy_core::options::config::{
    CostRates, ExecutionConfig, OptionType, OptionsCeilings, OptionsConfig, Sleeve,
};
use baskfy_core::options::execution::{Attempt, LegRole};
use baskfy_core::options::executor::{Book, Fill, Outcome, Rules, Venue, run_entry, run_exit};
use baskfy_core::options::replay_day::{
    ModelChain, SnapshotSource, StoredChain, priced_day, tier1_day,
};
use baskfy_core::options::scan::{DayContext, MarketDay, Snapshot};
use baskfy_testkit::{Golden, cases_for};
use chrono::{DateTime, FixedOffset};
use rust_decimal::Decimal;
use serde::Deserialize;
use serde_json::{Value, json};

fn grade(fn_name: &str, run: impl Fn(&Golden) -> Result<(), String>) -> usize {
    let cases = cases_for(fn_name).unwrap();
    let failures: Vec<String> = cases
        .iter()
        .filter_map(|g| run(g).err().map(|e| format!("{}: {e}", g.case)))
        .collect();
    assert!(
        failures.is_empty(),
        "{fn_name}: {} of {} cases failed:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
    cases.len()
}

fn err(e: impl std::fmt::Display) -> String {
    e.to_string()
}

fn rules(g: &Golden) -> Result<(OptionsConfig, OptionsCeilings), String> {
    Ok((
        g.input_as("options").map_err(err)?,
        g.input_as("ceilings").map_err(err)?,
    ))
}

#[derive(Deserialize)]
struct BookRow {
    bid: Decimal,
    ask: Decimal,
}

/// `golden_options._ScriptVenue`.
struct ScriptVenue {
    books: BTreeMap<String, BookRow>,
    script: Vec<i64>,
}

impl Venue for ScriptVenue {
    fn quote(&mut self, role: LegRole) -> CoreResult<Book> {
        let b = &self.books[role.value()];
        Ok(Book {
            bid: b.bid,
            ask: b.ask,
        })
    }

    fn send(
        &mut self,
        _role: LegRole,
        attempt: &Attempt,
        quantity: i64,
        _closing: bool,
    ) -> CoreResult<Fill> {
        let filled = if self.script.is_empty() {
            quantity
        } else {
            self.script.remove(0).min(quantity)
        };
        Ok(Fill {
            quantity: filled,
            avg_price: (filled != 0).then_some(attempt.price),
        })
    }
}

fn venue(g: &Golden) -> Result<ScriptVenue, String> {
    Ok(ScriptVenue {
        books: g.input_as("books").map_err(err)?,
        script: g.input_as("script").map_err(err)?,
    })
}

fn exec_rules(g: &Golden) -> Result<(Sleeve, Decimal, ExecutionConfig), String> {
    Ok((
        g.input_as("sleeve").map_err(err)?,
        g.input_decimal("tick").map_err(err)?,
        g.input_as("config").map_err(err)?,
    ))
}

#[derive(Deserialize)]
struct Minute {
    minute: DateTime<FixedOffset>,
    snapshot: Snapshot,
}

#[derive(Deserialize)]
struct SourceSpec {
    kind: String,
    #[serde(default)]
    minutes: Vec<Minute>,
}

fn source(
    spec: &SourceSpec,
    market: &MarketDay,
    options: &OptionsConfig,
) -> Box<dyn SnapshotSource> {
    if spec.kind == "model" {
        return Box::new(ModelChain::new(market.clone(), options.clone()));
    }
    Box::new(StoredChain {
        minutes: spec
            .minutes
            .iter()
            .map(|m| (m.minute, m.snapshot.clone()))
            .collect(),
    })
}

#[test]
fn backtest_run_matches_every_golden() {
    let n = grade("baskfy_core.options.backtest.run", |g| {
        let sleeve: Sleeve = g.input_as("sleeve").map_err(err)?;
        let tier: Tier = g.input_as("tier").map_err(err)?;
        let outcomes: Vec<DayOutcome> = g.input_as("outcomes").map_err(err)?;
        let min: i64 = g.input_as("min_sessions").map_err(err)?;
        g.check_output(&run(sleeve, tier, outcomes, |o| o, min).map_err(err)?)
    });
    assert_eq!(n, 9);
}

#[test]
fn tier2_quote_matches_every_golden() {
    let n = grade("baskfy_core.options.backtest.tier2_quote", |g| {
        let years: f64 = g.input_as("years").map_err(err)?;
        let rate: f64 = g.input_as("rate").map_err(err)?;
        let kind: OptionType = g.input_as("kind").map_err(err)?;
        let rates: CostRates = g.input_as("rates").map_err(err)?;
        let q = tier2_quote(
            g.input_decimal("spot").map_err(err)?,
            g.input_decimal("strike").map_err(err)?,
            years,
            g.input_decimal("vix_close").map_err(err)?,
            kind,
            rate,
            &rates,
        )
        .map_err(err)?;
        // The premium is a float printed exactly; erf here and in the platform libm can differ
        // by an ulp, so each field is graded to 1e-12 relative and to the paisa exactly.
        let expected: BTreeMap<String, String> = g.output_as().map_err(err)?;
        let actual: BTreeMap<String, String> =
            serde_json::from_value(serde_json::to_value(&q).map_err(err)?).map_err(err)?;
        for (field, want) in &expected {
            let got = &actual[field];
            let (w, a): (f64, f64) = (want.parse().map_err(err)?, got.parse().map_err(err)?);
            if (w - a).abs() > 1e-12 * w.abs().max(1.0) {
                return Err(format!("{field}: expected {want}, actual {got}"));
            }
            let paisa = |v: &str| {
                WideDecimal::from_str(v)
                    .and_then(|d| d.quantize(-2, Rounding::Down))
                    .map(|d| d.to_string())
                    .map_err(err)
            };
            let (wp, ap) = (paisa(want)?, paisa(got)?);
            if wp != ap {
                return Err(format!("{field}: paisa {wp} != {ap}"));
            }
        }
        Ok(())
    });
    assert_eq!(n, 6);
}

#[test]
fn run_entry_matches_every_golden() {
    let n = grade("baskfy_core.options.executor.run_entry", |g| {
        let mut v = venue(g)?;
        let (sleeve, tick, config) = exec_rules(g)?;
        let rules = Rules {
            sleeve,
            tick,
            config: &config,
        };
        let roles: Vec<LegRole> = g.input_as("roles").map_err(err)?;
        let quantity: i64 = g.input_as("quantity").map_err(err)?;
        let entry = run_entry(&mut v, &roles, quantity, rules).map_err(err)?;
        let exit = if entry.outcome == Outcome::Open {
            Some(run_exit(&mut v, entry.position.clone(), rules).map_err(err)?)
        } else {
            None
        };
        g.check_output(&json!({ "entry": entry, "exit": exit }))
    });
    assert_eq!(n, 20);
}

#[test]
fn run_exit_matches_every_golden() {
    let n = grade("baskfy_core.options.executor.run_exit", |g| {
        let mut v = venue(g)?;
        let (sleeve, tick, config) = exec_rules(g)?;
        let rules = Rules {
            sleeve,
            tick,
            config: &config,
        };
        let position: BTreeMap<LegRole, i64> = g.input_as("position").map_err(err)?;
        g.check_output(&run_exit(&mut v, position, rules).map_err(err)?)
    });
    assert_eq!(n, 6);
}

#[test]
fn tier1_day_matches_every_golden() {
    let n = grade("baskfy_core.options.replay_day.tier1_day", |g| {
        let (options, _) = rules(g)?;
        let market: MarketDay = g.input_as("market").map_err(err)?;
        let context: DayContext = g.input_as("context").map_err(err)?;
        let sleeves: Vec<Sleeve> = g.input_as("sleeves").map_err(err)?;
        let mut out = serde_json::Map::new();
        for s in sleeves {
            let day = tier1_day(s, &market, &context, &options).map_err(err)?;
            out.insert(
                s.value().to_owned(),
                serde_json::to_value(day).map_err(err)?,
            );
        }
        g.check_output(&Value::Object(out))
    });
    assert_eq!(n, 9);
}

#[test]
fn priced_day_matches_every_golden() {
    let n = grade("baskfy_core.options.replay_day.priced_day", |g| {
        let (options, ceilings) = rules(g)?;
        let sleeve: Sleeve = g.input_as("sleeve").map_err(err)?;
        let market: MarketDay = g.input_as("market").map_err(err)?;
        let context: DayContext = g.input_as("context").map_err(err)?;
        let spec: SourceSpec = g.input_as("source").map_err(err)?;
        let mut src = source(&spec, &market, &options);
        let day = priced_day(sleeve, &market, &context, src.as_mut(), &options, &ceilings)
            .map_err(err)?;
        g.check_output(&day)
    });
    assert_eq!(n, 11);
}

#[test]
fn run_tier_matches_every_golden() {
    let n = grade("baskfy_core.options.backtest_suite.run_tier", |g| {
        let (options, ceilings) = rules(g)?;
        let sleeves: Vec<Sleeve> = g.input_as("sleeves").map_err(err)?;
        let tier: Tier = g.input_as("tier").map_err(err)?;
        let days: Vec<MarketDay> = g.input_as("days").map_err(err)?;
        let context: DayContext = g.input_as("context").map_err(err)?;
        let spec: Option<SourceSpec> = g.input_as("source").map_err(err)?;
        let mut out = Vec::new();
        for s in sleeves {
            let mut make = |m: &MarketDay| source(spec.as_ref().unwrap(), m, &options);
            let source_for = spec
                .as_ref()
                .map(|_| &mut make as &mut dyn FnMut(&MarketDay) -> Box<dyn SnapshotSource>);
            out.push(
                run_tier(s, tier, &days, &context, &options, &ceilings, source_for).map_err(err)?,
            );
        }
        g.check_output(&out)
    });
    assert_eq!(n, 4);
}

#[test]
fn tier1_sensitivity_matches_every_golden() {
    let n = grade(
        "baskfy_core.options.backtest_suite.tier1_sensitivity",
        |g| {
            let (options, _) = rules(g)?;
            let sleeves: Vec<Sleeve> = g.input_as("sleeves").map_err(err)?;
            let days: Vec<MarketDay> = g.input_as("days").map_err(err)?;
            let context: DayContext = g.input_as("context").map_err(err)?;
            let mut out = serde_json::Map::new();
            for s in sleeves {
                let rows = tier1_sensitivity(s, &days, &context, &options).map_err(err)?;
                out.insert(
                    s.value().to_owned(),
                    serde_json::to_value(rows).map_err(err)?,
                );
            }
            g.check_output(&Value::Object(out))
        },
    );
    assert_eq!(n, 1);
}
