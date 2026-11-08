//! Port of `tests/options_fixtures.py` (and the few `options_scan_fixtures.py` helpers the bar
//! tests read). Literal lot sizes, strikes and expiries live here only (`04` §1.4).
#![allow(dead_code, clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, TimeDelta};
use rust_decimal::Decimal;

use baskfy_core::options::bars::Bar;
use baskfy_core::options::calendar::Contract;
use baskfy_core::options::chain::{Level, OptionQuote};
use baskfy_core::options::config::OptionType;
use baskfy_core::options::num::{at_ist, combine, ist_offset};

pub const LOT: i64 = 65;

pub fn tick() -> Decimal {
    Decimal::new(5, 2)
}

pub fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

pub fn dec(s: &str) -> Decimal {
    s.parse().unwrap()
}

/// A Python naive datetime, read as IST wall clock.
pub fn ist_dt(y: i32, m: u32, day: u32, hh: u32, mm: u32, ss: u32) -> DateTime<FixedOffset> {
    combine(
        d(y, m, day),
        NaiveTime::from_hms_opt(hh, mm, ss).unwrap(),
        ist_offset(),
    )
}

pub fn oct_tuesdays() -> [NaiveDate; 4] {
    [
        d(2026, 10, 6),
        d(2026, 10, 13),
        d(2026, 10, 20),
        d(2026, 10, 27),
    ]
}

pub fn nov_tuesdays() -> [NaiveDate; 4] {
    [
        d(2026, 11, 3),
        d(2026, 11, 10),
        d(2026, 11, 17),
        d(2026, 11, 24),
    ]
}

pub fn expiries_fixture() -> Vec<NaiveDate> {
    oct_tuesdays().into_iter().chain(nov_tuesdays()).collect()
}

/// The holiday fixture: October's last Tuesday moved to the Monday before it.
pub fn shifted_monthly() -> NaiveDate {
    d(2026, 10, 26)
}

/// A 50-point grid within ±1,000 of `centre` and a 100-point grid out to ±2,000.
pub fn strikes_around(centre: i64) -> Vec<Decimal> {
    let mut all: std::collections::BTreeSet<i64> =
        (centre - 1000..=centre + 1000).step_by(50).collect();
    for k in (centre - 2000..=centre + 2000).step_by(100) {
        if (k - centre).abs() > 1000 {
            all.insert(k);
        }
    }
    all.into_iter().map(Decimal::from).collect()
}

pub fn contract_full(
    expiry: NaiveDate,
    strike: Decimal,
    kind: OptionType,
    lot_size: i64,
    tick_size: Decimal,
    underlying: &str,
    token: i64,
) -> Contract {
    Contract {
        instrument_token: token,
        tradingsymbol: format!(
            "{underlying}{}{strike}{}",
            expiry.format("%y%m%d"),
            kind.value()
        ),
        underlying: underlying.to_owned(),
        expiry,
        strike,
        option_type: kind,
        lot_size,
        tick_size,
    }
}

pub fn contract(expiry: NaiveDate, strike: Decimal, kind: OptionType) -> Contract {
    contract_full(expiry, strike, kind, LOT, tick(), "NIFTY", 0)
}

pub fn master_with(expiries: &[NaiveDate], with_banknifty: bool) -> Vec<Contract> {
    let mut rows = Vec::new();
    let mut i = 0;
    for e in expiries {
        for k in [24950, 25000, 25050] {
            for t in [OptionType::Ce, OptionType::Pe] {
                rows.push(contract_full(
                    *e,
                    Decimal::from(k),
                    t,
                    LOT,
                    tick(),
                    "NIFTY",
                    i,
                ));
                i += 1;
            }
        }
    }
    if with_banknifty {
        rows.push(contract_full(
            d(2026, 10, 29),
            Decimal::from(52000),
            OptionType::Ce,
            30,
            tick(),
            "BANKNIFTY",
            0,
        ));
    }
    rows
}

pub fn master() -> Vec<Contract> {
    master_with(&expiries_fixture(), true)
}

/// The same master with October's monthly moved from Tuesday 27th to Monday 26th.
pub fn shifted_master() -> Vec<Contract> {
    let moved: Vec<NaiveDate> = expiries_fixture()
        .into_iter()
        .map(|e| {
            if e == d(2026, 10, 27) {
                shifted_monthly()
            } else {
                e
            }
        })
        .collect();
    master_with(&moved, true)
}

/// `options_fixtures.quote`'s keyword arguments, with its defaults.
#[derive(Debug, Clone)]
pub struct QuoteOpts {
    pub expiry: NaiveDate,
    pub bids: Option<Vec<Level>>,
    pub asks: Option<Vec<Level>>,
    pub oi: i64,
    pub ts: DateTime<FixedOffset>,
}

impl Default for QuoteOpts {
    fn default() -> Self {
        Self {
            expiry: d(2026, 10, 27),
            bids: None,
            asks: None,
            oi: 1_000_000,
            ts: ist_dt(2026, 10, 27, 10, 0, 0),
        }
    }
}

/// A quote with, by default, three deep levels a side at the touch.
pub fn quote_with(
    strike: Decimal,
    kind: OptionType,
    bid: Option<Decimal>,
    ask: Option<Decimal>,
    opts: QuoteOpts,
) -> OptionQuote {
    let ladder = |p: Option<Decimal>| match p {
        Some(x) if !x.is_zero() => vec![
            Level {
                price: x,
                quantity: 6500
            };
            3
        ],
        _ => Vec::new(),
    };
    OptionQuote {
        instrument_token: 0,
        expiry: opts.expiry,
        strike,
        option_type: kind,
        bid,
        ask,
        bids: opts.bids.unwrap_or_else(|| ladder(bid)),
        asks: opts.asks.unwrap_or_else(|| ladder(ask)),
        oi: opts.oi,
        ts: opts.ts,
        last: None,
        volume: 0,
    }
}

/// `quote(strike, kind, bid, ask)` with string prices (`None` for a missing side).
pub fn quote(strike: i64, kind: OptionType, bid: Option<&str>, ask: Option<&str>) -> OptionQuote {
    quote_with(
        Decimal::from(strike),
        kind,
        bid.map(dec),
        ask.map(dec),
        QuoteOpts::default(),
    )
}

// --- options_scan_fixtures helpers ---

pub fn quiet_monthly() -> NaiveDate {
    d(2026, 10, 27)
}

pub fn at(day: NaiveDate, hh: u32, mm: u32) -> DateTime<FixedOffset> {
    at_ist(day, NaiveTime::from_hms_opt(hh, mm, 0).unwrap())
}

/// One bar a minute from 09:15: each opens at the previous close, wicks `wick` beyond.
pub fn bars_from_closes_wick(
    day: NaiveDate,
    first_open: Decimal,
    closes: &[Decimal],
    wick: Decimal,
) -> Vec<Bar> {
    let mut out = Vec::with_capacity(closes.len());
    let mut prev = first_open;
    for (i, close) in closes.iter().enumerate() {
        let start = at(day, 9, 15) + TimeDelta::minutes(i64::try_from(i).unwrap());
        out.push(Bar {
            ts: start,
            open: prev,
            high: prev.max(*close) + wick,
            low: prev.min(*close) - wick,
            close: *close,
        });
        prev = *close;
    }
    out
}

pub fn bars_from_closes(day: NaiveDate, first_open: Decimal, closes: &[Decimal]) -> Vec<Bar> {
    bars_from_closes_wick(day, first_open, closes, Decimal::TWO)
}

pub fn zigzag(centre: Decimal, amplitude: Decimal, count: usize) -> Vec<Decimal> {
    (0..count)
        .map(|i| {
            if i % 2 == 0 {
                centre + amplitude
            } else {
                centre - amplitude
            }
        })
        .collect()
}

pub fn ramp(start: Decimal, per_minute: Decimal, count: usize) -> Vec<Decimal> {
    (0..count)
        .map(|i| start + per_minute * Decimal::from(i + 1))
        .collect()
}

/// A tiny deterministic generator standing in for Hypothesis.
pub struct Rng(pub u64);

impl Rng {
    pub fn next_u64(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }

    /// Uniform in `[lo, hi]`.
    #[allow(clippy::cast_precision_loss)]
    pub fn float(&mut self, lo: f64, hi: f64) -> f64 {
        let u = (self.next_u64() >> 11) as f64 / (1_u64 << 53) as f64;
        lo + (hi - lo) * u
    }

    pub fn below(&mut self, n: usize) -> usize {
        usize::try_from(self.next_u64() % (n as u64)).unwrap()
    }
}

// --- options_scan_fixtures: the O1/O2 days and the Black-76 chain (batch C) ---

pub fn prev_close() -> Decimal {
    Decimal::from(25000)
}

pub fn trend_weekly() -> NaiveDate {
    d(2026, 10, 20)
}

pub fn o2_up_break() -> NaiveDate {
    d(2026, 10, 19)
}

pub fn o2_counter() -> NaiveDate {
    d(2026, 10, 21)
}

pub fn settle() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap()
}

pub const RATE: f64 = 0.065;
pub const DEPTH: i64 = 1300;
pub const OI: i64 = 500_000;

/// Minutes after 09:15.
pub fn to_minutes(hh: usize, mm: usize) -> usize {
    (hh * 60 + mm) - (9 * 60 + 15)
}

pub fn quiet_monthly_bars() -> Vec<Bar> {
    let closes = zigzag(Decimal::from(25010), Decimal::from(8), to_minutes(15, 30));
    bars_from_closes(quiet_monthly(), Decimal::from(25010), &closes)
}

pub fn trend_weekly_bars() -> Vec<Bar> {
    let mut first = ramp(Decimal::from(25000), Decimal::from(3), to_minutes(10, 15));
    let rest = ramp(
        *first.last().unwrap(),
        Decimal::from(6),
        to_minutes(15, 30) - first.len(),
    );
    first.extend(rest);
    bars_from_closes(trend_weekly(), Decimal::from(25000), &first)
}

pub fn o2_up_break_bars() -> Vec<Bar> {
    let mut before = zigzag(Decimal::from(25020), Decimal::from(10), to_minutes(10, 0));
    let after = ramp(
        *before.last().unwrap(),
        Decimal::from(4),
        to_minutes(15, 30) - before.len(),
    );
    before.extend(after);
    bars_from_closes(o2_up_break(), Decimal::from(25020), &before)
}

pub fn o2_counter_bars() -> Vec<Bar> {
    let mut before = zigzag(Decimal::from(25020), Decimal::from(10), to_minutes(10, 0));
    let after = ramp(
        *before.last().unwrap(),
        Decimal::from(-4),
        to_minutes(15, 30) - before.len(),
    );
    before.extend(after);
    bars_from_closes(o2_counter(), Decimal::from(25020), &before)
}

/// Daily closes rising 20 points a session to `last`.
pub fn rising_closes_to(last: Decimal, count: i64) -> Vec<Decimal> {
    (0..count)
        .map(|i| last - Decimal::from(20 * (count - 1 - i)))
        .collect()
}

pub fn rising_closes() -> Vec<Decimal> {
    rising_closes_to(prev_close(), 60)
}

pub fn falling_closes() -> Vec<Decimal> {
    let count = 60;
    (0..count)
        .map(|i| prev_close() + Decimal::from(20 * (count - 1 - i)))
        .collect()
}

/// `scan.Snapshot`: one collector minute.
#[derive(Debug, Clone)]
pub struct Snapshot {
    pub ts: DateTime<FixedOffset>,
    pub spot: Decimal,
    pub quotes: Vec<OptionQuote>,
}

pub type Smile = dyn Fn(Decimal, OptionType) -> f64;

pub fn flat(vol: f64) -> impl Fn(Decimal, OptionType) -> f64 {
    move |_, _| vol
}

pub fn skew(call_vol: f64, put_vol: f64) -> impl Fn(Decimal, OptionType) -> f64 {
    move |_, kind| {
        if kind == OptionType::Ce {
            call_vol
        } else {
            put_vol
        }
    }
}

/// The 1-based token `nifty_master()` gives (every expiry, a ±1,500 grid of 50, CE then PE).
pub fn token_of(expiry: NaiveDate, strike: Decimal, kind: OptionType) -> i64 {
    let e = expiries_fixture()
        .iter()
        .position(|x| *x == expiry)
        .unwrap();
    let grid: Vec<Decimal> = (23500..=26500).step_by(50).map(Decimal::from).collect();
    let s = grid.iter().position(|x| *x == strike).unwrap();
    let t = usize::from(kind == OptionType::Pe);
    i64::try_from(e * grid.len() * 2 + s * 2 + t + 1).unwrap()
}

fn tick_down(v: Decimal) -> Decimal {
    (v / tick()).floor() * tick()
}

fn tick_up(v: Decimal) -> Decimal {
    (v / tick()).ceil() * tick()
}

/// A quote whose mid is Black-76 at `vol` on `forward`; bid/ask `spread_pct` apart.
#[allow(clippy::too_many_arguments)]
pub fn priced_quote(
    expiry: NaiveDate,
    strike: Decimal,
    kind: OptionType,
    forward: Decimal,
    now: DateTime<FixedOffset>,
    vol: f64,
    spread_pct: Decimal,
    depth: i64,
) -> OptionQuote {
    use baskfy_core::decimal::Rounding;
    use baskfy_core::options::greeks::{black76_price, year_fraction};
    use baskfy_core::options::num;
    let years = year_fraction(&now, expiry, settle());
    let model = num::from_float(black76_price(
        num::to_float(forward),
        num::to_float(strike),
        years,
        RATE,
        vol,
        kind,
    ))
    .unwrap();
    let mid = if model > Decimal::ZERO {
        num::quantize(model, 2, Rounding::HalfUp).unwrap()
    } else {
        Decimal::ZERO
    };
    let half = num::max(tick(), mid * spread_pct / Decimal::from(200));
    let bid = tick_down(mid - half);
    let ask = tick_up(mid + half);
    let bid_value = if bid > Decimal::ZERO { Some(bid) } else { None };
    let level = |p: Decimal| {
        vec![
            Level {
                price: p,
                quantity: depth
            };
            3
        ]
    };
    OptionQuote {
        instrument_token: token_of(expiry, strike, kind),
        expiry,
        strike,
        option_type: kind,
        bid: bid_value,
        ask: Some(ask),
        bids: if bid_value.is_some() {
            level(bid)
        } else {
            Vec::new()
        },
        asks: level(ask),
        oi: OI,
        ts: now,
        last: None,
        volume: 0,
    }
}

/// One collector minute: ATM ±15 strikes on a 50 grid for each expiry, CE and PE.
pub fn snapshot_with(
    now: DateTime<FixedOffset>,
    spot: Decimal,
    expiries: &[NaiveDate],
    smile: &Smile,
    forwards: Option<&std::collections::BTreeMap<NaiveDate, Decimal>>,
    depth: i64,
) -> Snapshot {
    use baskfy_core::decimal::Rounding;
    use baskfy_core::options::num;
    let fifty = Decimal::from(50);
    let centre = num::quantize(spot / fifty, 0, Rounding::HalfUp).unwrap() * fifty;
    let mut quotes = Vec::new();
    for expiry in expiries {
        let fwd = forwards
            .and_then(|f| f.get(expiry).copied())
            .unwrap_or(spot);
        for i in -15_i64..=15 {
            let strike = centre + fifty * Decimal::from(i);
            for kind in [OptionType::Ce, OptionType::Pe] {
                quotes.push(priced_quote(
                    *expiry,
                    strike,
                    kind,
                    fwd,
                    now,
                    smile(strike, kind),
                    Decimal::ONE,
                    depth,
                ));
            }
        }
    }
    Snapshot {
        ts: now,
        spot,
        quotes,
    }
}

/// `spot * e^{rT}`, to the paisa (half even).
pub fn carry_forward(spot: Decimal, now: DateTime<FixedOffset>, expiry: NaiveDate) -> Decimal {
    use baskfy_core::decimal::Rounding;
    use baskfy_core::options::greeks::year_fraction;
    use baskfy_core::options::num;
    let years = year_fraction(&now, expiry, settle()).max(0.0);
    let growth = num::from_float((RATE * years).exp()).unwrap();
    num::quantize(num::mul(spot, growth).unwrap(), 2, Rounding::HalfEven).unwrap()
}

// --- test_options_execution's never-naked walkers ---

use baskfy_core::options::execution::{
    EntryStep, LegRole, advance_entry, entry_sequence, never_naked, next_exit_leg,
};

/// Drive the entry reducer with seeded fills; never-naked after every fill and every close.
pub fn walk_entry(roles: &[LegRole], qty: i64, fills: &[i64]) {
    let seq = entry_sequence(roles).unwrap();
    let mut position: std::collections::BTreeMap<LegRole, i64> =
        seq.iter().map(|r| (*r, 0)).collect();
    let mut outcomes: Vec<i64> = Vec::new();
    let mut draws = fills.iter().copied();
    loop {
        let action = advance_entry(&seq, qty, &outcomes).unwrap();
        match action.step {
            EntryStep::Open => {
                assert!(seq.iter().all(|r| position[r] == qty));
                return;
            }
            EntryStep::Abandon => {
                for (role, filled) in action.close {
                    assert_eq!(position[&role], filled);
                    position.insert(role, 0);
                    assert!(never_naked(&position));
                }
                assert!(position.values().all(|v| *v == 0));
                return;
            }
            EntryStep::Send => {}
        }
        let leg = action.leg.unwrap();
        if !leg.is_long() {
            assert!(
                seq.iter()
                    .filter(|r| r.is_long())
                    .all(|r| position[r] == qty),
                "a short before every long filled"
            );
        }
        let filled = draws.next().unwrap_or(qty).min(qty);
        position.insert(leg, filled);
        outcomes.push(filled);
        assert!(never_naked(&position));
    }
}

pub fn walk_exit(roles: &[LegRole], qty: i64, fills: &[i64]) {
    let mut position: Vec<(LegRole, i64)> = roles.iter().map(|r| (*r, qty)).collect();
    let mut draws = fills.iter().copied();
    for _ in 0..200 {
        let Some(leg) = next_exit_leg(&position) else {
            assert!(position.iter().all(|(_, v)| *v == 0));
            return;
        };
        let qty_of =
            |p: &[(LegRole, i64)], r: LegRole| p.iter().find(|(x, _)| *x == r).map(|x| x.1);
        if leg.is_long() {
            for s in [LegRole::ShortCall, LegRole::ShortPut] {
                if let Some(q) = qty_of(&position, s)
                    && s.protector() == Some(leg)
                {
                    assert_eq!(q, 0);
                }
            }
        }
        let open = qty_of(&position, leg).unwrap();
        let take = draws.next().unwrap_or(qty).min(open);
        for p in &mut position {
            if p.0 == leg {
                p.1 -= take;
            }
        }
        let map: std::collections::BTreeMap<LegRole, i64> = position.iter().copied().collect();
        assert!(never_naked(&map));
    }
    panic!("exit did not reach flat");
}

/// Hypothesis' `st.lists(st.integers(lo, hi), max_size=n)` as a deterministic grid of examples.
pub fn fill_lists(lo: i64, hi: i64, max_size: usize, examples: usize, seed: u64) -> Vec<Vec<i64>> {
    let span = usize::try_from(hi - lo + 1).unwrap();
    let mut out: Vec<Vec<i64>> = vec![Vec::new()];
    for v in [lo, hi] {
        for n in 1..=max_size {
            out.push(vec![v; n]);
        }
    }
    let mut rng = Rng(seed);
    while out.len() < examples {
        let n = rng.below(max_size + 1);
        out.push(
            (0..n)
                .map(|_| lo + i64::try_from(rng.below(span)).unwrap())
                .collect(),
        );
    }
    out
}

// --- options_scan_fixtures: the O3-B day (batch D) ---

pub fn gap_hold_day() -> NaiveDate {
    d(2026, 10, 13)
}

/// Open 25,200 (+0.8 %), half-gap 25,100: a ±10 zig-zag around 25,200 that never nears it.
pub fn gap_hold_bars() -> Vec<Bar> {
    let closes = zigzag(Decimal::from(25200), Decimal::from(10), to_minutes(15, 30));
    bars_from_closes(gap_hold_day(), Decimal::from(25200), &closes)
}

// --- options_scan_fixtures: MASTER, market() and the scan's Snapshot (batch E) ---

pub fn gap_hold() -> NaiveDate {
    gap_hold_day()
}

pub fn o2_tuesday() -> NaiveDate {
    trend_weekly()
}

/// Every fixture expiry, a 50-point grid ±1,500 around 25,000, CE and PE; tokens from 1.
pub fn nifty_master() -> Vec<Contract> {
    let mut rows = Vec::new();
    let mut token = 1;
    for expiry in expiries_fixture() {
        for k in (23500..=26500).step_by(50) {
            for kind in [OptionType::Ce, OptionType::Pe] {
                rows.push(contract_full(
                    expiry,
                    Decimal::from(k),
                    kind,
                    LOT,
                    tick(),
                    "NIFTY",
                    token,
                ));
                token += 1;
            }
        }
    }
    rows
}

/// `market(day, bars, daily=None, vix=14, trading_day=True)`.
pub fn market_with(
    day: NaiveDate,
    bars: Vec<Bar>,
    daily: Option<Vec<Decimal>>,
    vix: Option<Decimal>,
    trading_day: bool,
) -> baskfy_core::options::scan::MarketDay {
    baskfy_core::options::scan::MarketDay {
        trade_date: day,
        trading_day,
        contracts: nifty_master(),
        bars,
        daily_closes: daily.unwrap_or_else(rising_closes),
        vix_prev_close: vix,
    }
}

pub fn market(day: NaiveDate, bars: Vec<Bar>) -> baskfy_core::options::scan::MarketDay {
    market_with(day, bars, None, Some(Decimal::from(14)), true)
}

/// `snapshot(now, spot, expiries, smile, forwards=...)` as `scan.Snapshot`.
pub fn scan_snapshot(
    now: DateTime<FixedOffset>,
    spot: Decimal,
    expiries: &[NaiveDate],
    smile: &Smile,
    forwards: Option<&std::collections::BTreeMap<NaiveDate, Decimal>>,
) -> baskfy_core::options::scan::Snapshot {
    let s = snapshot_with(now, spot, expiries, smile, forwards, DEPTH);
    baskfy_core::options::scan::Snapshot {
        ts: s.ts,
        spot: Some(s.spot),
        quotes: s.quotes,
    }
}
