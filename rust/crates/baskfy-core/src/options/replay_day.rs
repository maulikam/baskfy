//! Port of `baskfy_core.options.replay_day`: one backtest day per sleeve, by the live code (`04` §13).

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, TimeDelta, Timelike};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::{Rounding, WideDecimal};
use crate::error::{CoreError, CoreResult};
use crate::options::backtest::{DayOutcome, or_zero, tier2_quote};
use crate::options::bars::{Bar, closed, ist};
use crate::options::calendar::{RoleReason, expiries, role};
use crate::options::chain::{Level, OptionQuote};
use crate::options::condor;
use crate::options::config::{OptionsCeilings, OptionsConfig, Side, Sleeve};
use crate::options::directional::{self, Range};
use crate::options::execution::{Attempt, LegRole, simulate_fill};
use crate::options::executor::{
    Book, ExecResult, Fill, Outcome, Rules, Venue, run_entry, run_exit,
};
use crate::options::exits::{IndexState, LegMark, OpenLeg, OpenPosition, evaluate};
use crate::options::expiry_setups::{self, SetupClock};
use crate::options::greeks::year_fraction;
use crate::options::ledger::{JournalSession, LegFill, journal_figures};
use crate::options::num::{self, at_ist, ist_offset};
use crate::options::plan::{PlanLeg, PlanState, decide_o1};
use crate::options::plan_o2::{decide_o2, wanted_minutes as o2_wanted};
use crate::options::plan_o3::{decide_o3, wanted_minutes as o3_wanted};
use crate::options::scan::{DayContext, MarketDay, MinuteKey, Snapshot, SnapshotBook};
use crate::options::structures::{Candidate, Direction, Structure};

fn tick() -> Decimal {
    Decimal::new(5, 2)
}

fn hm(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap_or(NaiveTime::MIN)
}

fn session_end() -> NaiveTime {
    hm(15, 30)
}

fn is_o1(sleeve: Sleeve) -> bool {
    matches!(sleeve, Sleeve::O1m | Sleeve::O1w)
}

/// A chain at a minute, or `None` when that minute has none.
pub trait SnapshotSource {
    /// The chain at `minute` (to the IST minute).
    fn at(&mut self, minute: &DateTime<FixedOffset>) -> CoreResult<Option<Snapshot>>;
}

fn to_minute(at: &DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    let t = at.with_timezone(&ist_offset());
    t.with_second(0)
        .and_then(|x| x.with_nanosecond(0))
        .unwrap_or(t)
}

/// Tier 3: the collector's minutes, keyed by minute.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct StoredChain {
    /// Snapshots by minute.
    pub minutes: BTreeMap<DateTime<FixedOffset>, Snapshot>,
}

impl SnapshotSource for StoredChain {
    fn at(&mut self, minute: &DateTime<FixedOffset>) -> CoreResult<Option<Snapshot>> {
        Ok(self.minutes.get(&to_minute(minute)).cloned())
    }
}

/// Tier 2: Black-76 at the previous India VIX close as a flat IV, forward = spot, with §6.2's
/// synthetic spread and `depth` units at each touch (`04` §13.2).
#[derive(Debug, Clone)]
pub struct ModelChain {
    /// The day.
    pub market: MarketDay,
    /// The rules.
    pub options: OptionsConfig,
    /// Strikes either side of spot.
    pub strikes: i64,
    /// Units at each level.
    pub depth: i64,
    /// The model's rate.
    pub rate: f64,
    cache: BTreeMap<DateTime<FixedOffset>, Option<Snapshot>>,
}

impl ModelChain {
    /// `ModelChain(market, options)` with the defaults.
    pub fn new(market: MarketDay, options: OptionsConfig) -> Self {
        Self {
            market,
            options,
            strikes: 15,
            depth: 1300,
            rate: 0.065,
            cache: BTreeMap::new(),
        }
    }

    fn build(&self, minute: &DateTime<FixedOffset>) -> CoreResult<Option<Snapshot>> {
        let seen = closed(&self.market.bars, &(*minute + TimeDelta::minutes(1)));
        let (Some(last), Some(vix)) = (seen.last(), self.market.vix_prev_close) else {
            return Ok(None);
        };
        if vix <= Decimal::ZERO {
            return Ok(None);
        }
        let spot = last.close;
        let day = self.market.trade_date;
        let nearest: Vec<NaiveDate> = expiries(&self.market.contracts, "NIFTY")
            .into_iter()
            .filter(|e| *e >= day)
            .take(2)
            .collect();
        let mut quotes = Vec::new();
        for contract in &self.market.contracts {
            if !nearest.contains(&contract.expiry) {
                continue;
            }
            let steps = num::div(
                num::abs(num::sub(contract.strike, spot)?)?,
                Decimal::from(50),
            )?;
            if steps > Decimal::from(self.strikes) {
                continue;
            }
            let years = year_fraction(minute, contract.expiry, session_end()).max(1e-6);
            let model = tier2_quote(
                spot,
                contract.strike,
                years,
                vix,
                contract.option_type,
                self.rate,
                &self.options.costs,
            )?;
            let bid = to_paisa(&model.bid, true)?;
            let ask = num::max(to_paisa(&model.ask, false)?, bid);
            let ladder = |price: Decimal| {
                vec![
                    Level {
                        price,
                        quantity: self.depth,
                    };
                    3
                ]
            };
            let positive = bid > Decimal::ZERO;
            quotes.push(OptionQuote {
                instrument_token: contract.instrument_token,
                expiry: contract.expiry,
                strike: contract.strike,
                option_type: contract.option_type,
                bid: positive.then_some(bid),
                ask: Some(ask),
                bids: if positive { ladder(bid) } else { Vec::new() },
                asks: ladder(ask),
                oi: 5_000_000,
                ts: *minute,
                last: None,
                volume: 0,
            });
        }
        Ok(Some(Snapshot {
            ts: *minute,
            spot: Some(spot),
            quotes,
        }))
    }
}

/// `d.quantize(Decimal("0.01"), ROUND_CEILING | ROUND_FLOOR)` for a non-negative `d`.
fn to_paisa(d: &WideDecimal, ceiling: bool) -> CoreResult<Decimal> {
    let arith = |e: crate::decimal::DecimalError| CoreError::Arithmetic(e.to_string());
    let floor = d.quantize(-2, Rounding::Down).map_err(arith)?;
    let out = if ceiling && floor != *d {
        floor.add(&WideDecimal::from(Decimal::new(1, 2)))
    } else {
        floor
    };
    out.try_to_decimal().map_err(arith)
}

impl SnapshotSource for ModelChain {
    fn at(&mut self, minute: &DateTime<FixedOffset>) -> CoreResult<Option<Snapshot>> {
        let key = to_minute(minute);
        if let Some(hit) = self.cache.get(&key) {
            return Ok(hit.clone());
        }
        let built = self.build(&key)?;
        self.cache.insert(key, built.clone());
        Ok(built)
    }
}

// --- Tier 1 ---

fn move_after(
    bars: &[Bar],
    start: NaiveTime,
    end: NaiveTime,
    direction: Direction,
    level: Decimal,
) -> CoreResult<(Decimal, Decimal)> {
    let sign = if direction == Direction::Up {
        Decimal::ONE
    } else {
        Decimal::NEGATIVE_ONE
    };
    let mut path = Vec::new();
    for b in bars {
        let t = ist(&b.ts).time();
        if start < t && t <= end {
            path.push(num::mul(sign, num::sub(b.close, level)?)?);
        }
    }
    if path.is_empty() {
        return Ok((Decimal::ZERO, Decimal::ZERO));
    }
    path.push(Decimal::ZERO);
    let best = path.iter().copied().reduce(num::max).unwrap_or_default();
    let worst = path.iter().copied().reduce(num::min).unwrap_or_default();
    Ok((best, worst))
}

fn traded(day: NaiveDate, trigger: NaiveTime, move_: Option<(Decimal, Decimal)>) -> DayOutcome {
    DayOutcome {
        trigger_time: Some(trigger),
        mfe_points: move_.map(|m| m.0),
        mae_points: move_.map(|m| m.1),
        ..DayOutcome::bare(day, true)
    }
}

/// `04` §13.1: the gate and the trigger only. `None` for a day that is not the sleeve's.
pub fn tier1_day(
    sleeve: Sleeve,
    market: &MarketDay,
    context: &DayContext,
    options: &OptionsConfig,
) -> CoreResult<Option<DayOutcome>> {
    let day = market.trade_date;
    let verdict_role = role(
        day,
        sleeve,
        &market.contracts,
        &context.event_days,
        market.trading_day,
        "NIFTY",
    )?;
    if !verdict_role.trades {
        if verdict_role.reason == RoleReason::EventDay {
            return Ok(Some(DayOutcome::skipped(day, RoleReason::EventDay.value())));
        }
        return Ok(None);
    }
    let Some(prev) = market.daily_closes.last().copied() else {
        return Ok(Some(DayOutcome::skipped(day, "NO_PREV_CLOSE")));
    };
    let end = at_ist(day, session_end());
    let grace = options.chain.stale_scan_seconds;
    let bars = closed(&market.bars, &end);
    let skip = |reason: &str| Ok(Some(DayOutcome::skipped(day, reason)));
    if is_o1(sleeve) {
        let cfg = if sleeve == Sleeve::O1m {
            &options.condor_monthly
        } else {
            &options.condor_weekly
        };
        let gate = condor::observe(&market.bars, prev, &end, false, cfg, grace)?;
        if let Some(reason) = gate.reasons.first() {
            return skip(reason.value());
        }
        return Ok(Some(traded(day, cfg.plan_time, None)));
    }
    if sleeve == Sleeve::O2 {
        let cfg2 = &options.directional;
        let filters = directional::day_filters(
            &market.bars,
            &market.daily_closes,
            market.vix_prev_close,
            &end,
            false,
            cfg2,
            grace,
        )?;
        let trend = match (filters.reasons.first(), filters.trend) {
            (Some(reason), _) => return skip(reason.value()),
            (None, None) => return skip("TREND_UNKNOWN"),
            (None, Some(t)) => t,
        };
        let trig = directional::find_trigger(
            &market.bars,
            Range {
                or_high: filters.or_high.unwrap_or(Decimal::ZERO),
                or_low: filters.or_low.unwrap_or(Decimal::ZERO),
                direction: trend,
            },
            &end,
            cfg2,
            options.calendar.market_open,
            grace,
        )?;
        let Some(trigger) = trig.trigger else {
            return skip("NO_TRIGGER");
        };
        let mv = move_after(
            &bars,
            trigger.close_time,
            cfg2.hard_exit_time,
            trend,
            trigger.close,
        )?;
        return Ok(Some(traded(day, trigger.close_time, Some(mv))));
    }
    let cfg3 = &options.expiry_setups;
    let clock = SetupClock {
        now: end,
        event_day: false,
        session_open: options.calendar.market_open,
        grace_seconds: grace,
    };
    if sleeve == Sleeve::O3b {
        let gh = expiry_setups::gap_hold(&market.bars, prev, &clock, cfg3)?;
        if let Some(reason) = gh.reasons.first() {
            return skip(reason.value());
        }
        let Some(direction) = gh.direction.filter(|_| gh.triggered) else {
            return skip("NO_TRIGGER");
        };
        let level = bars
            .iter()
            .rev()
            .find(|b| ist(&b.ts).time() <= cfg3.o3b_hold_end)
            .map_or(prev, |b| b.close);
        let mv = move_after(
            &bars,
            cfg3.o3b_hold_end,
            cfg3.hard_exit_time,
            direction,
            level,
        )?;
        return Ok(Some(traded(day, cfg3.o3b_plan_time, Some(mv))));
    }
    let rb = expiry_setups::range_break(&market.bars, prev, &clock, cfg3)?;
    if let Some(reason) = rb.reasons.first() {
        return skip(reason.value());
    }
    let (Some(at), Some(direction), Some(close)) =
        (rb.trigger_time, rb.direction, rb.trigger_close)
    else {
        return skip("NO_TRIGGER");
    };
    let mv = move_after(&bars, at, cfg3.hard_exit_time, direction, close)?;
    Ok(Some(traded(day, at, Some(mv))))
}

// --- Tier 2 / Tier 3 ---

struct SnapshotVenue<'a> {
    snapshot: Snapshot,
    legs: &'a [(LegRole, i64)],
    options: &'a OptionsConfig,
    fills: Vec<LegFill>,
}

impl SnapshotVenue<'_> {
    fn find(&self, role: LegRole) -> CoreResult<&OptionQuote> {
        let token = self
            .legs
            .iter()
            .find(|(r, _)| *r == role)
            .map(|(_, t)| *t)
            .ok_or_else(|| CoreError::InvalidInput(format!("no leg {}", role.value())))?;
        self.snapshot
            .quotes
            .iter()
            .find(|q| q.instrument_token == token)
            .ok_or_else(|| CoreError::InvalidInput(format!("token {token} not in the snapshot")))
    }
}

impl Venue for SnapshotVenue<'_> {
    fn quote(&mut self, role: LegRole) -> CoreResult<Book> {
        let q = self.find(role)?;
        Ok(Book {
            bid: or_zero(q.bid),
            ask: or_zero(q.ask),
        })
    }

    fn send(
        &mut self,
        role: LegRole,
        attempt: &Attempt,
        quantity: i64,
        closing: bool,
    ) -> CoreResult<Fill> {
        let q = self.find(role)?;
        let ladder = if attempt.side == Side::Buy {
            &q.asks
        } else {
            &q.bids
        };
        let sim = simulate_fill(
            attempt.side,
            ladder,
            quantity,
            attempt.price,
            tick(),
            &self.options.execution,
        )?;
        if sim.filled != 0
            && let Some(price) = sim.avg_price
        {
            self.fills.push(LegFill {
                side: attempt.side,
                price,
                quantity: sim.filled,
                closing,
            });
        }
        Ok(Fill {
            quantity: sim.filled,
            avg_price: sim.avg_price,
        })
    }
}

fn entry_points(structure: Structure, result: &ExecResult) -> CoreResult<Decimal> {
    let mut total = Decimal::ZERO;
    for role in result.fills.keys() {
        let price = or_zero(result.avg_price(*role)?);
        let credit = structure == Structure::IronCondor;
        let signed = if role.is_long() == credit {
            num::neg(price)?
        } else {
            price
        };
        total = num::add(total, signed)?;
    }
    Ok(total)
}

/// The plan builder's decided plan for a day.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Decided {
    /// Legs in send order.
    pub legs: Vec<PlanLeg>,
    /// The candidate.
    pub candidate: Candidate,
    /// The snapshot minute decided on.
    pub decision_minute: DateTime<FixedOffset>,
    /// Direction (O2/O3).
    pub direction: Option<Direction>,
    /// The range high.
    pub range_high: Option<Decimal>,
    /// The range low.
    pub range_low: Option<Decimal>,
    /// O3-B's half gap.
    pub half_gap: Option<Decimal>,
}

/// What [`decide_plan`] concluded: Python's `Decided | str | None`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub enum DayPlan {
    /// A plan.
    Planned(Box<Decided>),
    /// A skip reason.
    Skipped(String),
    /// Not the sleeve's session.
    NoSession,
}

struct Step {
    state: PlanState,
    reasons: Vec<String>,
    candidate: Option<Candidate>,
    legs: Vec<PlanLeg>,
    as_of: Option<DateTime<FixedOffset>>,
    direction: Option<Direction>,
    high: Option<Decimal>,
    low: Option<Decimal>,
    half: Option<Decimal>,
}

fn loaded(
    source: &mut dyn SnapshotSource,
    wanted: impl IntoIterator<Item = MinuteKey>,
) -> CoreResult<SnapshotBook> {
    let mut snapshots = BTreeMap::new();
    for m in wanted.into_iter().flatten() {
        snapshots.insert(Some(m), source.at(&m)?);
    }
    Ok(SnapshotBook::new(snapshots))
}

#[allow(clippy::too_many_arguments)]
fn step(
    sleeve: Sleeve,
    market: &MarketDay,
    context: &DayContext,
    source: &mut dyn SnapshotSource,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<Step> {
    let day = market.trade_date;
    if is_o1(sleeve) {
        let cfg = if sleeve == Sleeve::O1m {
            &options.condor_monthly
        } else {
            &options.condor_weekly
        };
        let snap = source.at(&at_ist(day, cfg.plan_time))?;
        let o1 = decide_o1(
            sleeve,
            market,
            context,
            snap.as_ref(),
            now,
            options,
            ceilings,
        )?;
        return Ok(Step {
            state: o1.state,
            reasons: o1.reasons,
            candidate: o1.candidate,
            legs: o1.legs,
            as_of: o1.as_of_minute,
            direction: None,
            high: None,
            low: None,
            half: None,
        });
    }
    if sleeve == Sleeve::O2 {
        let wanted = o2_wanted(market, context, now, options, ceilings)?;
        let mut book = loaded(source, wanted)?;
        let o2 = decide_o2(market, context, &mut book, now, options, ceilings)?;
        return Ok(Step {
            state: o2.state,
            reasons: o2.reasons,
            candidate: o2.candidate,
            legs: o2.legs,
            as_of: o2.as_of_minute,
            direction: o2.direction,
            high: o2.or_high,
            low: o2.or_low,
            half: None,
        });
    }
    let wanted = o3_wanted(sleeve, market, context, now, options, ceilings)?;
    let mut book = loaded(source, wanted)?;
    let o3 = decide_o3(sleeve, market, context, &mut book, now, options, ceilings)?;
    Ok(Step {
        state: o3.state,
        reasons: o3.reasons,
        candidate: o3.candidate,
        legs: o3.legs,
        as_of: o3.as_of_minute,
        direction: o3.direction,
        high: o3.range_high,
        low: o3.range_low,
        half: o3.half_gap,
    })
}

/// The plan builder minute by minute: a decided plan, a skip reason, or no session.
pub fn decide_plan(
    sleeve: Sleeve,
    market: &MarketDay,
    context: &DayContext,
    source: &mut dyn SnapshotSource,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<DayPlan> {
    let day = market.trade_date;
    let start = match sleeve {
        Sleeve::O1m => options.condor_monthly.plan_time,
        Sleeve::O1w => options.condor_weekly.plan_time,
        _ => hm(9, 30),
    };
    let mut minute = at_ist(day, start);
    while minute.time() < hm(14, 0) {
        let now = minute + TimeDelta::seconds(45);
        let s = step(sleeve, market, context, source, &now, options, ceilings)?;
        if s.state == PlanState::Planned
            && let (Some(candidate), Some(as_of)) = (s.candidate, s.as_of)
        {
            return Ok(DayPlan::Planned(Box::new(Decided {
                legs: s.legs,
                candidate,
                decision_minute: as_of,
                direction: s.direction,
                range_high: s.high,
                range_low: s.low,
                half_gap: s.half,
            })));
        }
        match s.state {
            PlanState::Skipped => {
                let reason = s
                    .reasons
                    .into_iter()
                    .next()
                    .unwrap_or_else(|| "SKIPPED".to_owned());
                return Ok(DayPlan::Skipped(reason));
            }
            PlanState::NoSession => return Ok(DayPlan::NoSession),
            PlanState::WindowClosed => return Ok(DayPlan::Skipped("WINDOW_CLOSED".to_owned())),
            _ => {}
        }
        minute += TimeDelta::minutes(1);
    }
    Ok(DayPlan::Skipped("WINDOW_CLOSED".to_owned()))
}

/// `04` §13.2 / §13.3: the day as the desk would have traded it on `source`'s prices.
pub fn priced_day(
    sleeve: Sleeve,
    market: &MarketDay,
    context: &DayContext,
    source: &mut dyn SnapshotSource,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<Option<DayOutcome>> {
    let day = market.trade_date;
    let decided = match decide_plan(sleeve, market, context, source, options, ceilings)? {
        DayPlan::NoSession => return Ok(None),
        DayPlan::Skipped(reason) => return Ok(Some(DayOutcome::skipped(day, &reason))),
        DayPlan::Planned(d) => *d,
    };
    let candidate = &decided.candidate;
    let quantity = candidate.lots * candidate.lot_size.unwrap_or(0);
    let mut tokens: Vec<(LegRole, i64)> = Vec::new();
    for lg in &decided.legs {
        match tokens.iter_mut().find(|(r, _)| *r == lg.role) {
            Some(slot) => slot.1 = lg.instrument_token,
            None => tokens.push((lg.role, lg.instrument_token)),
        }
    }
    let rules = Rules {
        sleeve,
        tick: tick(),
        config: &options.execution,
    };
    let entry_minute = decided.decision_minute + TimeDelta::minutes(1);
    let Some(entry_snap) = source.at(&entry_minute)? else {
        return Ok(Some(DayOutcome::skipped(day, "NO_ENTRY_SNAPSHOT")));
    };
    let mut venue = SnapshotVenue {
        snapshot: entry_snap,
        legs: &tokens,
        options,
        fills: Vec::new(),
    };
    let roles: Vec<LegRole> = decided.legs.iter().map(|lg| lg.role).collect();
    let entry = run_entry(&mut venue, &roles, quantity, rules)?;
    if entry.outcome != Outcome::Open {
        return Ok(Some(DayOutcome::skipped(
            day,
            Outcome::AbandonedEntry.value(),
        )));
    }
    let mut fills = venue.fills;
    let points = entry_points(candidate.structure, &entry)?;
    let r_inr = or_zero(candidate.risk_per_lot_inr);
    let strikes: Vec<(LegRole, Decimal)> =
        decided.legs.iter().map(|lg| (lg.role, lg.strike)).collect();
    let legs = tokens
        .iter()
        .map(|(role, token)| {
            Ok(OpenLeg {
                role: *role,
                strike: strikes
                    .iter()
                    .rev()
                    .find(|(r, _)| r == role)
                    .map_or(Decimal::ZERO, |(_, s)| *s),
                quantity,
                fill_price: or_zero(entry.avg_price(*role)?),
                instrument_token: *token,
            })
        })
        .collect::<CoreResult<Vec<_>>>()?;
    let risk = num::muli(r_inr, candidate.lots)?;
    let position = OpenPosition {
        sleeve,
        structure: candidate.structure,
        legs,
        entry_points: points,
        opened_at: entry_minute,
        risk_budget_inr: risk,
        direction: decided.direction,
        range_high: decided.range_high,
        range_low: decided.range_low,
        half_gap: decided.half_gap,
        width_points: (candidate.structure == Structure::DebitSpread)
            .then_some(options.expiry_setups.width_points),
    };
    let mut minute = entry_minute + TimeDelta::minutes(1);
    let last = at_ist(day, hm(15, 29));
    while minute <= last {
        if let Some(snap) = source.at(&minute)? {
            let mut marks = BTreeMap::new();
            for (role, token) in &tokens {
                for q in snap.quotes.iter().filter(|q| q.instrument_token == *token) {
                    marks.insert(
                        *role,
                        LegMark {
                            bid: q.bid,
                            ask: q.ask,
                            at: snap.ts,
                        },
                    );
                }
            }
            let bars = closed(&market.bars, &minute);
            let index = IndexState {
                spot: bars.last().map(|b| b.close),
                last_tick_at: Some(minute),
                bars,
            };
            let verdict = evaluate(
                &position,
                &marks,
                &index,
                &(minute + TimeDelta::seconds(1)),
                options,
                false,
            )?;
            if let Some(verdict) = verdict {
                let mut exit_venue = SnapshotVenue {
                    snapshot: snap,
                    legs: &tokens,
                    options,
                    fills: Vec::new(),
                };
                run_exit(&mut exit_venue, entry.position.clone(), rules)?;
                fills.extend(exit_venue.fills);
                let figures = journal_figures(
                    &fills,
                    &JournalSession {
                        r_inr: risk,
                        quantity,
                        opened_at: entry_minute,
                        closed_at: minute,
                        peak_points: None,
                        trough_points: None,
                    },
                    &options.costs,
                )?;
                return Ok(Some(DayOutcome {
                    net_pnl_inr: Some(figures.net_pnl_inr),
                    r_multiple: Some(figures.r_multiple),
                    closed_reason: Some(verdict.code),
                    trigger_time: Some(decided.decision_minute.time()),
                    ..DayOutcome::bare(day, true)
                }));
            }
        }
        minute += TimeDelta::minutes(1);
    }
    Ok(Some(DayOutcome::skipped(day, "NO_EXIT_DATA")))
}
