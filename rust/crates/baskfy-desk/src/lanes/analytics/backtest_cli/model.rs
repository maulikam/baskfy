use std::collections::{BTreeMap, HashMap, HashSet};

use chrono::{Datelike, Duration, NaiveDate};

use crate::lanes::analytics::analytics_views::metrics as m;
use crate::lanes::analytics::regime::RegimeConfig;
use crate::lanes::analytics::regime::engine as r;
use crate::lanes::analytics::{breadth, index_cache as ic};
use crate::lanes::weekly::pandas::{np_round, py_round};
use crate::storage::Conn;
use crate::wire::{PyDict, PyValue, dict};

pub(super) type Series = m::Series;

#[derive(Debug)]
pub(super) enum BtError {
    Exit(String),
    Py(String),
}

impl From<r::ExposureError> for BtError {
    fn from(e: r::ExposureError) -> Self {
        Self::Py(e.to_string())
    }
}

impl From<crate::lanes::analytics::regime::ConfigError> for BtError {
    fn from(e: crate::lanes::analytics::regime::ConfigError) -> Self {
        Self::Py(format!("ConfigError: {e}"))
    }
}

#[derive(Debug, Clone, Copy)]
pub(super) struct CostModel {
    stt_pct: f64,
    brokerage_pct: f64,
    exchange_pct: f64,
    sebi_pct: f64,
    gst_pct: f64,
    stamp_pct_buy: f64,
    dp_per_scrip: f64,
    slippage_pct: f64,
}

impl CostModel {
    pub(super) const fn with_slippage_of(&self, slippage_pct: f64) -> Self {
        Self {
            slippage_pct,
            ..*self
        }
    }

    pub(super) const fn with_slippage(slippage_pct: f64) -> Self {
        Self {
            stt_pct: 0.001,
            brokerage_pct: 0.0,
            exchange_pct: 0.000_032_5,
            sebi_pct: 0.000_001,
            gst_pct: 0.18,
            stamp_pct_buy: 0.000_15,
            dp_per_scrip: 0.0,
            slippage_pct,
        }
    }

    pub(super) fn one_way(&self, notional: f64, is_buy: bool) -> f64 {
        let n = notional.abs();
        let stt = n * self.stt_pct;
        let brokerage = n * self.brokerage_pct;
        let exch = n * self.exchange_pct;
        let sebi = n * self.sebi_pct;
        let gst = (brokerage + exch) * self.gst_pct;
        let stamp = if is_buy { n * self.stamp_pct_buy } else { 0.0 };
        let slip = n * self.slippage_pct;
        stt + brokerage + exch + sebi + gst + stamp + slip
    }

    pub(super) fn round_trip(&self, notional: f64) -> f64 {
        self.one_way(notional, true) + self.one_way(notional, false)
    }

    pub(super) fn breakdown(&self, notional: f64, is_buy: bool) -> Vec<(&'static str, f64)> {
        let n = notional.abs();
        let (brokerage, exch) = (n * self.brokerage_pct, n * self.exchange_pct);
        vec![
            ("stt", n * self.stt_pct),
            ("brokerage", brokerage),
            ("exchange", exch),
            ("sebi", n * self.sebi_pct),
            ("gst", (brokerage + exch) * self.gst_pct),
            ("stamp", if is_buy { n * self.stamp_pct_buy } else { 0.0 }),
            ("dp", self.dp_per_scrip),
            ("slippage", n * self.slippage_pct),
        ]
    }
}

pub(super) const SLIPPAGE_SCENARIOS: [(&str, f64); 3] = [
    ("low", 0.0002),
    ("base", 0.0005),
    ("stressed_smallcap", 0.0025),
];

pub(super) fn slippage_of(name: &str) -> f64 {
    SLIPPAGE_SCENARIOS
        .iter()
        .find(|(k, _)| *k == name)
        .map_or(0.0005, |(_, v)| *v)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Kind {
    None,
    Binary,
    Tiered,
}

#[derive(Debug, Clone, Copy)]
pub(super) struct Variant {
    pub(super) key: &'static str,
    pub(super) label: &'static str,
    pub(super) kind: Kind,
    ma: usize,
    buffered: bool,
    use_structural: bool,
    use_sentinel: bool,
    pub(super) use_breadth: bool,
    pub(super) available: bool,
    pub(super) unavailable_reason: &'static str,
}

const fn var(key: &'static str, label: &'static str, kind: Kind) -> Variant {
    Variant {
        key,
        label,
        kind,
        ma: 0,
        buffered: true,
        use_structural: true,
        use_sentinel: true,
        use_breadth: true,
        available: true,
        unavailable_reason: "",
    }
}

const fn binary(key: &'static str, label: &'static str, ma: usize, buffered: bool) -> Variant {
    Variant {
        ma,
        buffered,
        ..var(key, label, Kind::Binary)
    }
}

const fn tiered(
    key: &'static str,
    label: &'static str,
    use_structural: bool,
    use_sentinel: bool,
    use_breadth: bool,
) -> Variant {
    Variant {
        use_structural,
        use_sentinel,
        use_breadth,
        ..var(key, label, Kind::Tiered)
    }
}

pub(super) const VARIANTS: [Variant; 14] = [
    var("A", "No regime overlay", Kind::None),
    binary("B_raw", "Binary M50 50-DMA, raw crossing", 50, false),
    binary("B_buf", "Binary M50 50-DMA, buffer+confirm", 50, true),
    binary("C_raw", "Binary M50 200-DMA, raw crossing", 200, false),
    binary("C_buf", "Binary M50 200-DMA, buffer+confirm", 200, true),
    Variant {
        available: false,
        unavailable_reason: "requires stock-level holdings, exit decisions and skipped replacements; cannot be approximated from an index series",
        ..var(
            "D",
            "M50 50-DMA middle ground (skip entries, no liquidation)",
            Kind::Tiered,
        )
    },
    tiered("E", "Three-index tiered, no sentinel", true, false, true),
    tiered("F", "Full tiered model", true, true, true),
    tiered("abl_breadth", "Ablation: breadth only", false, false, true),
    tiered(
        "abl_sentinel",
        "Ablation: Momentum 50 only",
        false,
        true,
        false,
    ),
    tiered(
        "abl_structural",
        "Ablation: structural indices only",
        true,
        false,
        false,
    ),
    tiered(
        "abl_struct_breadth",
        "Ablation: structural + breadth",
        true,
        false,
        true,
    ),
    tiered(
        "abl_struct_sentinel",
        "Ablation: structural + Momentum 50",
        true,
        true,
        false,
    ),
    tiered("abl_complete", "Ablation: complete model", true, true, true),
];

pub(super) fn variant(key: &str) -> Variant {
    VARIANTS
        .iter()
        .find(|v| v.key == key)
        .copied()
        .unwrap_or(VARIANTS[0])
}

pub(super) struct Store {
    ohlc: HashMap<String, Series>,
    pub(super) breadth: Vec<(NaiveDate, f64)>,
}

pub(super) fn index_names(cfg: &RegimeConfig) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for n in cfg
        .structural_indices
        .iter()
        .map(|(_, v)| v.clone())
        .chain(std::iter::once(cfg.momentum_sentinel.clone()))
    {
        if !out.contains(&n) {
            out.push(n);
        }
    }
    out
}

impl Store {
    pub(super) async fn load(conn: &mut Conn, cfg: &RegimeConfig) -> Result<Self, String> {
        let mut ohlc = HashMap::new();
        for name in index_names(cfg) {
            let candles = ic::load(conn, &name, None, None, true)
                .await
                .map_err(|e| e.to_string())?;
            let s: Series = candles
                .iter()
                .map(|c| (c.date, c.close.unwrap_or(f64::NAN)))
                .collect();
            ohlc.insert(name, s);
        }
        let rows = breadth::history(conn, "scan")
            .await
            .map_err(|e| e.to_string())?;
        let mut brd: Vec<(NaiveDate, f64)> = Vec::new();
        for row in rows {
            if let Some(slot) = brd.iter_mut().find(|p| p.0 == row.as_of_date) {
                slot.1 = row.pct_above_20dma;
            } else {
                brd.push((row.as_of_date, row.pct_above_20dma));
            }
        }
        Ok(Self { ohlc, breadth: brd })
    }

    pub(super) fn frame(&self, name: &str) -> &[(NaiveDate, f64)] {
        self.ohlc.get(name).map_or(&[], Vec::as_slice)
    }

    pub(super) fn window(&self, name: &str, start: NaiveDate, end: NaiveDate) -> Series {
        between(self.frame(name), start, end)
    }

    pub(super) fn breadth_covers_window(&self, start: NaiveDate, end: NaiveDate) -> bool {
        let inside: Vec<NaiveDate> = self
            .breadth
            .iter()
            .map(|p| p.0)
            .filter(|d| *d >= start && *d <= end)
            .collect();
        let (Some(first), Some(last)) = (inside.first(), inside.last()) else {
            return false;
        };
        if inside.len() < 26 {
            return false;
        }
        let span = (*last - *first).num_days() as f64;
        let total = (end - start).num_days().max(1) as f64;
        span / total >= 0.5
    }
}

pub(super) fn between(s: &[(NaiveDate, f64)], start: NaiveDate, end: NaiveDate) -> Series {
    s.iter()
        .copied()
        .filter(|(d, _)| *d >= start && *d <= end)
        .collect()
}

fn weekly_signal_sessions(sessions: &[NaiveDate]) -> Vec<NaiveDate> {
    let mut groups: BTreeMap<(i32, u32), NaiveDate> = BTreeMap::new();
    for d in sessions {
        let w = d.iso_week();
        let slot = groups.entry((w.year(), w.week())).or_insert(*d);
        if *d > *slot {
            *slot = *d;
        }
    }
    let mut out: Vec<NaiveDate> = groups.into_values().collect();
    out.sort_unstable();
    out
}

pub(super) struct SignalTape {
    name: String,
    closes: Vec<f64>,
    pos: HashMap<NaiveDate, usize>,
    mas: Vec<(usize, Vec<Option<f64>>)>,
    states: Vec<Vec<(r::SignalState, i64)>>,
}

pub(super) type Tapes = HashMap<String, SignalTape>;

impl SignalTape {
    fn new(name: &str, frame: &[(NaiveDate, f64)], core: &r::CoreConfig) -> Result<Self, BtError> {
        let closes: Vec<f64> = frame.iter().map(|p| p.1).collect();
        let pos = frame.iter().enumerate().map(|(i, p)| (p.0, i)).collect();
        let mut mas = Vec::new();
        let mut states = Vec::new();
        for &length in &core.ma_lengths {
            let ma = r::moving_average(&closes, length)?;
            states.push(r::confirmed_state_series(
                &closes,
                &ma,
                core.buffer(),
                core.confirm_days,
            ));
            mas.push((length, ma));
        }
        Ok(Self {
            name: name.to_owned(),
            closes,
            pos,
            mas,
            states,
        })
    }

    fn at(&self, session: NaiveDate) -> r::IndexSignals {
        let i = self.pos.get(&session).copied().unwrap_or(0);
        let close = self.closes.get(i).copied().unwrap_or(f64::NAN);
        let mut signals = BTreeMap::new();
        for ((length, mas), states) in self.mas.iter().zip(&self.states) {
            let ma = mas.get(i).copied().flatten();
            let (state, run) = states
                .get(i)
                .copied()
                .unwrap_or((r::SignalState::Unknown, 0));
            let distance_pct = match ma {
                Some(v) if v != 0.0 => Some((close / v - 1.0) * 100.0),
                _ => None,
            };
            signals.insert(
                *length,
                r::MaSignal {
                    ma_length: *length,
                    close,
                    ma,
                    distance_pct,
                    state,
                    confirming_closes: run,
                },
            );
        }
        r::IndexSignals {
            index_name: self.name.clone(),
            as_of_date: Some(session),
            close: Some(close),
            signals,
            is_stale: false,
            has_data: true,
            instrument_token: None,
        }
    }
}

pub(super) fn build_tapes(store: &Store, cfg: &RegimeConfig) -> Result<Tapes, BtError> {
    let core = cfg.to_core()?;
    let mut out = HashMap::new();
    for n in index_names(cfg) {
        out.insert(n.clone(), SignalTape::new(&n, store.frame(&n), &core)?);
    }
    Ok(out)
}

#[derive(Debug, Clone)]
pub(super) struct Row {
    pub(super) session: NaiveDate,
    pub(super) tier: &'static str,
    pub(super) raw_tier: &'static str,
    pub(super) exposure: f64,
    pub(super) breadth: Option<f64>,
}

pub(super) fn replay(
    store: &Store,
    cfg: &RegimeConfig,
    variant: &Variant,
    start: NaiveDate,
    end: NaiveDate,
    tapes: Option<&Tapes>,
) -> Result<Vec<Row>, BtError> {
    let core = cfg.to_core()?;
    let names: Vec<String> = cfg
        .structural_indices
        .iter()
        .map(|(_, v)| v.clone())
        .collect();
    let sentinel_name = cfg.momentum_sentinel.clone();
    let all = index_names(cfg);
    let missing: Vec<String> = all
        .iter()
        .filter(|n| store.frame(n).is_empty())
        .map(|n| format!("'{n}'"))
        .collect();
    if !missing.is_empty() {
        return Err(BtError::Exit(format!(
            "no cached history for [{}] — run with --fetch first",
            missing.join(", ")
        )));
    }
    let mut common: Option<HashSet<NaiveDate>> = None;
    for n in &all {
        let set: HashSet<NaiveDate> = store.frame(n).iter().map(|p| p.0).collect();
        common = Some(match common {
            None => set,
            Some(c) => c.intersection(&set).copied().collect(),
        });
    }
    let mut aligned: Vec<NaiveDate> = common
        .unwrap_or_default()
        .into_iter()
        .filter(|d| *d >= start && *d <= end)
        .collect();
    aligned.sort_unstable();
    let weeks = weekly_signal_sessions(&aligned);
    let owned;
    let tapes = if let Some(t) = tapes {
        t
    } else {
        owned = build_tapes(store, cfg)?;
        &owned
    };

    let book = r::resolve_book_weights(&core, None, None)?;
    let snapshot = r::ExposureSnapshot::new(0.0);
    let mut prev: Option<r::PreviousRegime> = None;
    let mut out = Vec::new();

    for session in weeks {
        let sig = |n: &str| tapes.get(n).map(|t| t.at(session));
        let mut structural: BTreeMap<String, r::IndexSignals> = BTreeMap::new();
        for n in &names {
            if let Some(mut s) = sig(n) {
                if !variant.use_structural {
                    s.signals = BTreeMap::new();
                }
                structural.insert(n.clone(), s);
            }
        }
        let sentinel_sig = sig(&sentinel_name);
        let sentinel = if variant.use_sentinel {
            sentinel_sig.clone()
        } else {
            None
        };

        let mut reading = None;
        if variant.use_breadth
            && !store.breadth.is_empty()
            && let Some((d, pct)) = store.breadth.iter().rev().find(|p| p.0 <= session)
        {
            {
                reading = Some(r::BreadthReading {
                    as_of_date: *d,
                    pct_above_20dma: *pct,
                    eligible_count: 500,
                    observed_count: 500,
                    coverage_pct: 100.0,
                    universe_id: "scan".to_owned(),
                    universe_hash: "replay".to_owned(),
                    audit_run_id: "replay".to_owned(),
                    calculation_version: "replay".to_owned(),
                });
            }
        }

        let st = r::evaluate(&r::EvaluateArgs {
            as_of_date: session,
            scheduled_week_end: session,
            signal_session_date: session,
            index_signals: &structural,
            sentinel: sentinel.as_ref(),
            breadth: reading.as_ref(),
            book: &book,
            exposure: &snapshot,
            cfg: &core,
            previous: prev.as_ref(),
        })?;

        let exposure = match variant.kind {
            Kind::None => 100.0,
            Kind::Binary => {
                let below = sentinel_sig.as_ref().is_some_and(|s| {
                    if variant.buffered {
                        s.is_below(variant.ma)
                    } else {
                        let ma = s.signals.get(&variant.ma).and_then(|x| x.ma);
                        match (ma, s.close) {
                            (Some(ma), Some(c)) => ma != 0.0 && !ma.is_nan() && c < ma,
                            _ => false,
                        }
                    }
                });
                if below { 0.0 } else { 100.0 }
            }
            Kind::Tiered => core.cap_for(st.policy_tier)?,
        };

        out.push(Row {
            session,
            tier: st.policy_tier.as_str(),
            raw_tier: st.raw_candidate_tier.as_str(),
            exposure,
            breadth: reading.as_ref().map(|b| b.pct_above_20dma),
        });
        prev = Some(r::PreviousRegime {
            policy_tier: st.policy_tier,
            scheduled_week_end: session,
            last_transition_date: st.last_transition_date,
            recovery_confirmations: 0,
            book_weights: Some(book.clone()),
        });
    }
    if out.is_empty() {
        return Err(BtError::Py(
            "KeyError: \"None of ['session'] are in the columns\"".to_owned(),
        ));
    }
    Ok(out)
}

#[derive(Debug, Clone)]
pub(super) struct Sim {
    pub(super) curve: Series,
    pub(super) exposure: Series,
    traded_frac: f64,
    cost_frac: f64,
    transitions: i64,
}

fn pct_change(v: &[f64]) -> Vec<f64> {
    (0..v.len())
        .map(|i| {
            if i == 0 {
                f64::NAN
            } else {
                v[i] / v[i - 1] - 1.0
            }
        })
        .collect()
}

pub(super) fn simulate(
    schedule: &[Row],
    proxy: &[(NaiveDate, f64)],
    costs: &CostModel,
    cash_rate: f64,
) -> Sim {
    let sessions: Vec<NaiveDate> = proxy.iter().map(|p| p.0).collect();
    let pos: HashMap<NaiveDate, usize> =
        sessions.iter().enumerate().map(|(i, d)| (*d, i)).collect();
    let mut effective: HashMap<NaiveDate, f64> = HashMap::new();
    for row in schedule {
        let Some(&i) = pos.get(&row.session) else {
            continue;
        };
        if i + 1 >= sessions.len() {
            continue;
        }
        effective.insert(sessions[i + 1], row.exposure);
    }
    let (mut nav, mut exposure) = (1.0_f64, 100.0_f64);
    let closes: Vec<f64> = proxy.iter().map(|p| p.1).collect();
    let daily_r: Vec<f64> = pct_change(&closes)
        .into_iter()
        .map(|x| if x.is_nan() { 0.0 } else { x })
        .collect();
    let cash_daily = (1.0 + cash_rate).powf(1.0 / 252.0) - 1.0;
    let (mut navs, mut exps, mut transitions) = (Vec::new(), Vec::new(), 0_i64);
    let (mut traded_frac, mut cost_frac) = (0.0_f64, 0.0_f64);
    for (i, d) in sessions.iter().enumerate() {
        if let Some(&new) = effective.get(d)
            && (new - exposure).abs() > 1e-9
        {
            let delta = (new - exposure).abs() / 100.0;
            let notional = nav * delta;
            let cost = costs.one_way(notional, new > exposure);
            cost_frac += cost / nav;
            nav -= cost;
            traded_frac += delta;
            exposure = new;
            transitions += 1;
        }
        let r = daily_r[i];
        nav *= 1.0 + (exposure / 100.0) * r + (1.0 - exposure / 100.0) * cash_daily;
        navs.push(nav);
        exps.push(exposure);
    }
    Sim {
        curve: sessions
            .iter()
            .zip(&navs)
            .map(|(d, n)| (*d, n * 100.0))
            .collect(),
        exposure: sessions.iter().copied().zip(exps).collect(),
        traded_frac,
        cost_frac,
        transitions,
    }
}

pub(super) fn count_whipsaws(
    schedule: &[Row],
    benchmark: &[(NaiveDate, f64)],
    window_evals: usize,
) -> (i64, f64) {
    let exp: Vec<f64> = schedule.iter().map(|r| r.exposure).collect();
    let dates: Vec<NaiveDate> = schedule.iter().map(|r| r.session).collect();
    let mut hits = 0_i64;
    for i in 1..exp.len() {
        let drop = exp[i - 1] - exp[i];
        if drop < 20.0 {
            continue;
        }
        let j_end = (i + window_evals).min(exp.len() - 1);
        let peak = exp[i..=j_end]
            .iter()
            .copied()
            .fold(f64::NEG_INFINITY, f64::max);
        let restored = peak - exp[i];
        if restored < 0.80 * drop {
            continue;
        }
        let window = between(benchmark, dates[i], dates[j_end]);
        if window.len() < 2 {
            continue;
        }
        let dd = m::max_drawdown(&window).depth.abs();
        if dd < 0.05 {
            hits += 1;
        }
    }
    let years = match (dates.first(), dates.last()) {
        (Some(a), Some(b)) if dates.len() > 1 => ((*b - *a).num_days() as f64 / 365.0).max(1e-9),
        _ => 1.0,
    };
    (hits, py_round(hits as f64 / years, 3))
}

fn exposure_reversals(schedule: &[Row]) -> i64 {
    let mut signs = Vec::new();
    for w in schedule.windows(2) {
        let d = w[1].exposure - w[0].exposure;
        if d > 0.0 {
            signs.push(1);
        } else if d < 0.0 {
            signs.push(-1);
        }
    }
    signs.windows(2).filter(|w| w[0] != w[1]).count() as i64
}

pub(super) fn tier_counts(schedule: &[Row]) -> Vec<(String, f64)> {
    let mut counts: Vec<(&str, usize)> = Vec::new();
    for row in schedule {
        if let Some(c) = counts.iter_mut().find(|c| c.0 == row.tier) {
            c.1 += 1;
        } else {
            counts.push((row.tier, 1));
        }
    }
    counts.sort_by_key(|a| std::cmp::Reverse(a.1));
    let n = schedule.len() as f64;
    counts
        .into_iter()
        .map(|(k, c)| (k.to_owned(), np_round(c as f64 / n * 100.0, 2)))
        .collect()
}

#[derive(Debug, Clone)]
pub(super) struct Perf {
    pub(super) cagr: f64,
    pub(super) max_drawdown_pct: f64,
    dd_duration_days: i64,
    dd_recovered: bool,
    volatility_pct: f64,
    pub(super) sharpe: f64,
    sortino: f64,
    calmar: f64,
    roll_1y: Option<f64>,
    roll_3y: Option<f64>,
    roll_5y: Option<f64>,
    pct_time_in_cash: f64,
    avg_equity_exposure: f64,
    tier_time_pct: Vec<(String, f64)>,
    transitions: i64,
    transitions_per_year: f64,
    exposure_reversals: i64,
    pub(super) annual_turnover_pct: f64,
    pub(super) total_cost_drag_pct_yr: f64,
    whipsaws: i64,
    pub(super) whipsaws_per_year: f64,
    up_capture: Option<f64>,
    down_capture: Option<f64>,
}

fn opt(v: Option<f64>) -> PyValue {
    v.map_or(PyValue::None, PyValue::Float)
}

impl Perf {
    pub(super) fn to_dict(&self) -> PyDict {
        let tiers: PyDict = self
            .tier_time_pct
            .iter()
            .map(|(k, v)| (k.clone(), PyValue::Float(*v)))
            .collect();
        dict([
            ("CAGR", PyValue::Float(self.cagr)),
            ("max_drawdown_pct", PyValue::Float(self.max_drawdown_pct)),
            ("dd_duration_days", PyValue::Int(self.dd_duration_days)),
            ("dd_recovered", PyValue::Bool(self.dd_recovered)),
            ("volatility_pct", PyValue::Float(self.volatility_pct)),
            ("sharpe", PyValue::Float(self.sharpe)),
            ("sortino", PyValue::Float(self.sortino)),
            ("calmar", PyValue::Float(self.calmar)),
            ("roll_1Y_worst_pct", opt(self.roll_1y)),
            ("roll_3Y_med_pct", opt(self.roll_3y)),
            ("roll_5Y_med_pct", opt(self.roll_5y)),
            ("pct_time_in_cash", PyValue::Float(self.pct_time_in_cash)),
            (
                "avg_equity_exposure",
                PyValue::Float(self.avg_equity_exposure),
            ),
            ("tier_time_pct", PyValue::Map(tiers)),
            ("transitions", PyValue::Int(self.transitions)),
            (
                "transitions_per_year",
                PyValue::Float(self.transitions_per_year),
            ),
            ("exposure_reversals", PyValue::Int(self.exposure_reversals)),
            (
                "annual_turnover_pct",
                PyValue::Float(self.annual_turnover_pct),
            ),
            (
                "total_cost_drag_pct_yr",
                PyValue::Float(self.total_cost_drag_pct_yr),
            ),
            ("whipsaws", PyValue::Int(self.whipsaws)),
            ("whipsaws_per_year", PyValue::Float(self.whipsaws_per_year)),
            ("up_capture", opt(self.up_capture)),
            ("down_capture", opt(self.down_capture)),
        ])
    }
}

pub(super) fn performance(
    sim: &Sim,
    schedule: &[Row],
    benchmark: &[(NaiveDate, f64)],
) -> Result<Perf, BtError> {
    let curve = &sim.curve;
    let (Some(first), Some(last)) = (curve.first(), curve.last()) else {
        return Err(BtError::Py(
            "IndexError: index -1 is out of bounds for axis 0 with size 0".to_owned(),
        ));
    };
    let levels: Vec<f64> = curve.iter().map(|p| p.1).collect();
    let rets: Series = curve
        .iter()
        .zip(pct_change(&levels))
        .map(|(p, x)| (p.0, x))
        .filter(|p| !p.1.is_nan())
        .collect();
    let dd = m::max_drawdown(curve);
    let exps: Vec<f64> = sim.exposure.iter().map(|p| p.1).collect();
    let years = ((last.0 - first.0).num_days() as f64 / 365.0).max(1e-9);
    let rolling = m::rolling_returns(curve, &[("1Y", 252), ("3Y", 756), ("5Y", 1260)]);
    let col = |name: &str| -> Vec<f64> {
        rolling
            .iter()
            .find(|c| c.0 == name)
            .map(|c| c.1.iter().map(|p| p.1).filter(|x| !x.is_nan()).collect())
            .unwrap_or_default()
    };
    let roll_min = |name: &str| {
        let v = col(name);
        (!v.is_empty())
            .then(|| py_round(v.iter().copied().fold(f64::INFINITY, f64::min) * 100.0, 2))
    };
    let roll_med = |name: &str| {
        let v = col(name);
        (!v.is_empty()).then(|| py_round(m::nanmedian(&v) * 100.0, 2))
    };
    let bench_months = m::monthly_returns(benchmark);
    let port_months = m::monthly_returns(curve);
    let captures = bench_months.len() > 2;
    let (whipsaws, whipsaws_per_year) = count_whipsaws(schedule, benchmark, 4);
    let n = exps.len() as f64;
    let cash = exps.iter().filter(|x| **x < 1e-9).count() as f64;
    Ok(Perf {
        cagr: py_round(m::cagr(curve) * 100.0, 2),
        max_drawdown_pct: py_round(dd.depth * 100.0, 2),
        dd_duration_days: dd.duration_days,
        dd_recovered: dd.recovered,
        volatility_pct: py_round(m::ann_volatility(&rets) * 100.0, 2),
        sharpe: py_round(m::sharpe(&rets, 0.0), 3),
        sortino: py_round(m::sortino(&rets, 0.0), 3),
        calmar: py_round(m::calmar(curve), 3),
        roll_1y: roll_min("1Y"),
        roll_3y: roll_med("3Y"),
        roll_5y: roll_med("5Y"),
        pct_time_in_cash: py_round(cash / n * 100.0, 2),
        avg_equity_exposure: py_round(m::nanmean(&exps), 2),
        tier_time_pct: tier_counts(schedule),
        transitions: sim.transitions,
        transitions_per_year: py_round(sim.transitions as f64 / years, 2),
        exposure_reversals: exposure_reversals(schedule),
        annual_turnover_pct: py_round(sim.traded_frac / years * 100.0, 1),
        total_cost_drag_pct_yr: py_round(sim.cost_frac / years * 100.0, 3),
        whipsaws,
        whipsaws_per_year,
        up_capture: captures.then(|| py_round(m::up_capture(&port_months, &bench_months), 3)),
        down_capture: captures.then(|| py_round(m::down_capture(&port_months, &bench_months), 3)),
    })
}

fn week_label(d: NaiveDate) -> NaiveDate {
    let wd = i64::from(d.weekday().num_days_from_monday());
    d + Duration::days((4 - wd).rem_euclid(7))
}

fn weekly(s: &[(NaiveDate, f64)], how: fn(&[f64]) -> f64) -> Vec<(NaiveDate, f64)> {
    let mut groups: BTreeMap<NaiveDate, Vec<f64>> = BTreeMap::new();
    for (d, v) in s {
        groups.entry(week_label(*d)).or_default().push(*v);
    }
    groups.into_iter().map(|(k, v)| (k, how(&v))).collect()
}

fn last_valid(v: &[f64]) -> f64 {
    v.iter()
        .rev()
        .copied()
        .find(|x| !x.is_nan())
        .unwrap_or(f64::NAN)
}

fn min_valid(v: &[f64]) -> f64 {
    v.iter()
        .copied()
        .filter(|x| !x.is_nan())
        .reduce(f64::min)
        .unwrap_or(f64::NAN)
}

fn rounded(v: f64, nd: usize) -> PyValue {
    if v.is_nan() {
        PyValue::None
    } else {
        PyValue::Float(py_round(v, nd))
    }
}

pub(super) fn curve_payload(detail: &[(&'static str, Vec<Row>, Sim)]) -> PyValue {
    let mut series = PyDict::new();
    let mut index: Option<Vec<NaiveDate>> = None;
    for (key, _, sim) in detail {
        if key.starts_with("abl_") || sim.curve.is_empty() {
            continue;
        }
        let curve = &sim.curve;
        let mut run = f64::NEG_INFINITY;
        let drawdown: Series = curve
            .iter()
            .map(|(d, v)| {
                run = run.max(*v);
                (*d, (v / run - 1.0) * 100.0)
            })
            .collect();
        let eq: BTreeMap<NaiveDate, f64> = weekly(curve, last_valid)
            .into_iter()
            .filter(|p| !p.1.is_nan())
            .collect();
        let dd: BTreeMap<NaiveDate, f64> = weekly(&drawdown, min_valid)
            .into_iter()
            .filter(|p| !p.1.is_nan())
            .collect();
        let own: Vec<NaiveDate> = eq.keys().filter(|k| dd.contains_key(*k)).copied().collect();
        let exp: Option<BTreeMap<NaiveDate, f64>> = (!sim.exposure.is_empty())
            .then(|| weekly(&sim.exposure, m::nanmean).into_iter().collect());
        let idx = index.get_or_insert_with(|| own.clone()).clone();
        let at =
            |m: &BTreeMap<NaiveDate, f64>, d: &NaiveDate| m.get(d).copied().unwrap_or(f64::NAN);
        let eq_v: Vec<PyValue> = idx.iter().map(|d| rounded(at(&eq, d), 1)).collect();
        let dd_v: Vec<PyValue> = idx.iter().map(|d| rounded(at(&dd, d), 2)).collect();
        let exp_v = exp.map_or(PyValue::None, |e| {
            PyValue::List(own.iter().map(|d| rounded(at(&e, d), 1)).collect())
        });
        series.insert(
            *key,
            PyValue::Map(dict([
                ("equity", PyValue::List(eq_v)),
                ("drawdown", PyValue::List(dd_v)),
                ("exposure", exp_v),
            ])),
        );
    }
    match index {
        None => PyValue::Map(PyDict::new()),
        Some(idx) => PyValue::Map(dict([
            (
                "dates",
                PyValue::List(
                    idx.iter()
                        .map(|d| PyValue::Str(d.format("%Y-%m-%d").to_string()))
                        .collect(),
                ),
            ),
            ("series", PyValue::Map(series)),
        ])),
    }
}
