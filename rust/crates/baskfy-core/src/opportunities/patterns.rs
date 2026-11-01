//! Pattern setups read off an instrument's bar history (RS8 opps-setups; no Python twin).
//!
//! Every detector sees only the bars up to and including the as-of session: `History`'s last
//! row is the bar being evaluated, so nothing looks ahead. Where the desk already defines the
//! setup (TWT, VBT, the swing book's FLAG and EP) its thresholds are read from the desk's own
//! config; the rest are standard published definitions (DECISIONS-RS R8.os).

use std::sync::LazyLock;

use chrono::{Datelike, Days, NaiveDate};
use rust_decimal::Decimal;

use crate::opportunities::config::{SetupType, SwingParams};
use crate::opportunities::indicators::IndicatorRow;
use crate::opportunities::swing::{
    Evaluation, Evidence, Target, entry_basis, num, reach, status_of, targets,
};
use crate::swing::setups::SwingConfig;
use crate::twt::config::TwtConfig;
use crate::vbt::config::VbtConfig;

const PCT: f64 = 100.0;

static TWT: LazyLock<TwtConfig> = LazyLock::new(TwtConfig::default);
static VBT: LazyLock<VbtConfig> = LazyLock::new(VbtConfig::default);
static SWING: LazyLock<SwingConfig> = LazyLock::new(SwingConfig::default);

/// One instrument's bars, oldest first. Detectors read the bar at `end` and the bars before it,
/// never after: [`History::set_end`] moves the as-of bar without copying (the walk-forward test).
#[derive(Debug, Clone)]
pub struct History<'a> {
    end: usize,
    rows: &'a [IndicatorRow],
    dates: Vec<NaiveDate>,
    open: Vec<f64>,
    high: Vec<f64>,
    low: Vec<f64>,
    close: Vec<f64>,
    volume: Vec<f64>,
    adj: Vec<f64>,
}

impl<'a> History<'a> {
    /// `None` when the history is empty or any bar lacks a date, price or volume.
    #[must_use]
    pub fn new(rows: &'a [IndicatorRow]) -> Option<Self> {
        if rows.is_empty() {
            return None;
        }
        let n = rows.len();
        let mut h = Self {
            end: n - 1,
            rows,
            dates: Vec::with_capacity(n),
            open: Vec::with_capacity(n),
            high: Vec::with_capacity(n),
            low: Vec::with_capacity(n),
            close: Vec::with_capacity(n),
            volume: Vec::with_capacity(n),
            adj: Vec::with_capacity(n),
        };
        for r in rows {
            h.dates.push(r.date?);
            h.open.push(num(r.open)?);
            h.high.push(num(r.high)?);
            h.low.push(num(r.low)?);
            h.close.push(num(r.close)?);
            h.volume.push(num(r.volume)?);
            h.adj.push(num(r.adj_factor).filter(|a| *a > 0.0).unwrap_or(1.0));
        }
        Some(h)
    }

    /// Whether the as-of bar is `day`.
    #[must_use]
    pub fn ends_on(&self, day: NaiveDate) -> bool {
        self.dates.get(self.end) == Some(&day)
    }

    /// Make bar `end` the as-of bar; `false` (unchanged) when it does not exist.
    pub fn set_end(&mut self, end: usize) -> bool {
        let ok = end < self.close.len();
        if ok {
            self.end = end;
        }
        ok
    }

    fn last(&self) -> usize {
        self.end
    }

    fn row(&self) -> &IndicatorRow {
        &self.rows[self.last()]
    }

    fn close_raw(&self, i: usize) -> f64 {
        self.close[i] / self.adj[i]
    }

    fn turnover_raw(&self, i: usize) -> f64 {
        self.close_raw(i) * self.volume[i]
    }
}

// ---------------------------------------------------------------------------------------------
// Window helpers: every range is `[start, end)` over bars that exist, `None` when empty.
// ---------------------------------------------------------------------------------------------

fn mean(xs: &[f64]) -> Option<f64> {
    (!xs.is_empty()).then(|| xs.iter().sum::<f64>() / xs.len() as f64)
}

fn max_of(xs: &[f64]) -> Option<f64> {
    xs.iter().copied().reduce(f64::max)
}

fn min_of(xs: &[f64]) -> Option<f64> {
    xs.iter().copied().reduce(f64::min)
}

/// Index of the first maximum in `xs[start..end]`, absolute.
fn arg_max(xs: &[f64], start: usize, end: usize) -> Option<usize> {
    let mut best: Option<usize> = None;
    for i in start..end.min(xs.len()) {
        if best.is_none_or(|b| xs[i] > xs[b]) {
            best = Some(i);
        }
    }
    best
}

/// Index of the first minimum in `xs[start..end]`, absolute.
fn arg_min(xs: &[f64], start: usize, end: usize) -> Option<usize> {
    let mut best: Option<usize> = None;
    for i in start..end.min(xs.len()) {
        if best.is_none_or(|b| xs[i] < xs[b]) {
            best = Some(i);
        }
    }
    best
}

/// The `n` values ending at `end` (inclusive), or `None` with fewer than `n` bars.
fn tail(xs: &[f64], end: usize, n: usize) -> Option<&[f64]> {
    (n > 0 && end + 1 >= n && end < xs.len()).then(|| &xs[end + 1 - n..=end])
}

/// Mean of the last `window` values ending at `end`, with at least `min_samples` of them
/// (the desk's rolling `min_samples` over a gap-free series).
fn rolling_mean(xs: &[f64], end: usize, window: usize, min_samples: usize) -> Option<f64> {
    let have = (end + 1).min(window);
    if have < min_samples.max(1) || end >= xs.len() {
        return None;
    }
    mean(&xs[end + 1 - have..=end])
}

/// `ewm(span, adjust=False)` seeded with the first value.
fn ema(xs: &[f64], span: i64) -> Vec<f64> {
    let alpha = 2.0 / (span as f64 + 1.0);
    let mut out = Vec::with_capacity(xs.len());
    let mut prev: Option<f64> = None;
    for x in xs {
        let next = prev.map_or(*x, |p| alpha * x + (1.0 - alpha) * p);
        out.push(next);
        prev = Some(next);
    }
    out
}

fn to_usize(n: i64) -> usize {
    usize::try_from(n).unwrap_or(0)
}

fn dec_f(d: Decimal) -> f64 {
    d.to_string().parse::<f64>().unwrap_or(f64::NAN)
}

/// The swing book's ADR%: mean of `(high / low - 1) x 100` over `adr_bars`, today included.
fn adr_pct(h: &History<'_>, end: usize) -> Option<f64> {
    let n = to_usize(SWING.liquidity.adr_bars);
    let ranges: Vec<f64> = tail(&h.high, end, n)?
        .iter()
        .zip(tail(&h.low, end, n)?)
        .map(|(hi, lo)| (hi / lo - 1.0) * PCT)
        .collect();
    mean(&ranges)
}

/// The swing book's `rvol`: volume over the mean of the `ep.rvol_bars` bars before it.
fn rvol_prior(h: &History<'_>, i: usize) -> Option<f64> {
    let n = to_usize(SWING.ep.rvol_bars);
    let avg = mean(tail(&h.volume, i.checked_sub(1)?, n)?)?;
    (avg > 0.0).then(|| h.volume[i] / avg)
}

fn close_position(h: &History<'_>, i: usize) -> f64 {
    let range = h.high[i] - h.low[i];
    if range > 0.0 {
        (h.close[i] - h.low[i]) / range
    } else {
        0.5
    }
}

/// The swing book's widest stop (`docs/swing/04` §6.1): one ADR, capped at 10 %.
fn stop_too_wide(entry: f64, stop: f64, adr: f64) -> bool {
    let stops = &SWING.stops;
    let widest = (adr * stops.max_stop_adr_multiple).min(stops.max_stop_distance_pct);
    adr <= 0.0 || (entry - stop) / entry * PCT > widest
}

/// Targets at `extension_r` and one R beyond, for setups whose method has no price target.
fn r_targets(entry: f64, stop: f64, extension_r: f64) -> Vec<Target> {
    let risk = entry - stop;
    [extension_r, extension_r + 1.0]
        .into_iter()
        .enumerate()
        .map(|(k, r)| Target {
            label: format!("T{} {r}R", k + 1),
            price: entry + r * risk,
            r_multiple: r,
        })
        .collect()
}

/// A level break the breakout way: triggered above `pivot` on volume within the extension
/// cap, pending just below it. `(triggered, extension_atr, distance_pct)`.
fn pivot_state(close: f64, pivot: f64, atr: f64, rvol: Option<f64>, params: &SwingParams) -> Option<(bool, f64, f64)> {
    let p = &params.breakout;
    let extension = (close - pivot) / atr;
    let distance = (pivot - close) / close * PCT;
    let volume_ok = rvol.is_some_and(|r| r >= p.volume_multiple);
    let triggered = close > pivot && volume_ok && extension <= p.max_extension_atr;
    let pending = close <= pivot && distance <= p.near_pivot_pct;
    (triggered || pending).then_some((triggered, extension, distance))
}

struct Levels {
    setup: SetupType,
    triggered: bool,
    condition: String,
    trigger_level: f64,
    entry: f64,
    stop: f64,
    targets: Vec<Target>,
    evidence: Vec<Evidence>,
    extension_atr: Option<f64>,
    distance_pct: Option<f64>,
}

fn finish(l: Levels, horizon: (i64, i64)) -> Option<Evaluation> {
    (l.stop < l.entry && !l.targets.is_empty()).then(|| Evaluation {
        setup: l.setup,
        status: status_of(l.triggered),
        trigger_condition: l.condition,
        trigger_level: l.trigger_level,
        entry_basis: entry_basis(l.triggered).to_owned(),
        entry: l.entry,
        invalidation: l.stop,
        targets: l.targets,
        evidence: l.evidence,
        horizon,
        extension_atr: l.extension_atr.filter(|_| l.triggered),
        distance_pct: l.distance_pct.filter(|_| !l.triggered),
    })
}

fn ev(key: &str, label: impl Into<String>, value: Option<f64>, threshold: Option<f64>, unit: &str, passed: bool) -> Evidence {
    Evidence::new(key, label, value, threshold, unit, passed)
}

// ---------------------------------------------------------------------------------------------
// VCP — Minervini's volatility contraction pattern (standard definition)
// ---------------------------------------------------------------------------------------------

/// Successively shallower pullbacks from the base high, volume drying up, a break of the last
/// contraction's high.
#[must_use]
pub fn evaluate_vcp(h: &History<'_>, params: &SwingParams, horizon: (i64, i64)) -> Option<Evaluation> {
    let p = &params.patterns.vcp;
    let row = h.row();
    let last = h.last();
    let close = h.close[last];
    let atr = num(row.atr).filter(|a| *a > 0.0)?;
    let sma_mid = num(row.sma_mid)?;
    let trend = close > sma_mid && num(row.sma_long).is_none_or(|l| sma_mid > l);
    let hi52 = max_of(&h.high[last.saturating_sub(251)..=last])?;
    let below_high = (1.0 - close / hi52) * PCT;
    let near_high = below_high <= p.max_below_52w_high_pct;
    if !(trend && near_high) {
        return None;
    }
    let start = last.saturating_sub(to_usize(p.base_max_bars));
    let h0 = arg_max(&h.high, start, last)?;
    let base_len = last - h0;
    if base_len < to_usize(p.base_min_bars) {
        return None;
    }
    let k = to_usize(p.swing_bars).max(1);
    let min_depth = 2.0;
    // Swing highs after the base high; a higher one replaces the peaks it clears, and a
    // pullback shallower than `min_depth` is noise, not a contraction.
    let mut points = vec![h0];
    for j in h0 + 1..=last.saturating_sub(k) {
        let left = max_of(&h.high[j.saturating_sub(k)..j]).unwrap_or(f64::NEG_INFINITY);
        let right = max_of(&h.high[j + 1..=j + k]).unwrap_or(f64::NEG_INFINITY);
        if !(h.high[j] > left && h.high[j] >= right) {
            continue;
        }
        while points.len() > 1 && points.last().is_some_and(|&q| h.high[j] > h.high[q]) {
            points.pop();
        }
        let prev = *points.last()?;
        let depth = (h.high[prev] - min_of(&h.low[prev + 1..=j])?) / h.high[prev] * PCT;
        if depth >= min_depth {
            points.push(j);
        }
    }
    let mut depths: Vec<f64> = Vec::with_capacity(points.len());
    for w in points.windows(2) {
        let (a, b) = (w[0], w[1]);
        depths.push((h.high[a] - min_of(&h.low[a + 1..=b])?) / h.high[a] * PCT);
    }
    let p_last = *points.last()?;
    let last_low = min_of(&h.low[p_last + 1..=last])?;
    let pivot = max_of(&h.high[p_last..last])?;
    depths.push((pivot - last_low) / pivot * PCT);
    let count = depths.len();
    let first_depth = *depths.first()?;
    let last_depth = *depths.last()?;
    let shrinking = depths.windows(2).all(|w| w[1] < w[0]);
    let count_ok = count >= to_usize(p.min_contractions);
    let first_ok = first_depth <= p.max_first_depth_pct;
    let last_ok = last_depth <= p.max_last_depth_pct;
    let recent = mean(tail(&h.volume, last.checked_sub(1)?, 10)?)?;
    let longer = mean(tail(&h.volume, last.checked_sub(1)?, 50)?)?;
    let dryup = (longer > 0.0).then(|| recent / longer);
    let dryup_ok = dryup.is_some_and(|d| d <= p.dryup_max_ratio);
    if !(count_ok && shrinking && first_ok && last_ok && dryup_ok) {
        return None;
    }
    let rvol = num(row.rvol);
    let (triggered, extension, distance) = pivot_state(close, pivot, atr, rvol, params)?;
    let entry = if triggered { close } else { pivot };
    let stop = last_low - params.risk.stop_buffer_atr * atr;
    let depths_txt: Vec<String> = depths.iter().map(|d| format!("{d:.1}%")).collect();
    let evidence = vec![
        ev("trend", "Close above 50-session average (50 above 200)", Some(close), Some(sma_mid), "₹", trend),
        ev("near_high", "Below 52-week high", Some(below_high), Some(p.max_below_52w_high_pct), "%", near_high),
        ev("contractions", format!("Contractions ({})", depths_txt.join(" → ")), Some(count as f64), Some(p.min_contractions as f64), "count", count_ok && shrinking),
        ev("first_depth", "First contraction depth", Some(first_depth), Some(p.max_first_depth_pct), "%", first_ok),
        ev("last_depth", "Last contraction depth", Some(last_depth), Some(p.max_last_depth_pct), "%", last_ok),
        ev("dryup", "10- over 50-session volume", dryup, Some(p.dryup_max_ratio), "x", dryup_ok),
        ev("close_vs_pivot", "Close vs last contraction high", Some(close), Some(pivot), "₹", close > pivot),
        ev("rvol", "Volume vs 20-session average", rvol, Some(params.breakout.volume_multiple), "x", rvol.is_some_and(|r| r >= params.breakout.volume_multiple)),
    ];
    finish(
        Levels {
            setup: SetupType::Vcp,
            triggered,
            condition: format!(
                "Daily close above {pivot:.2} (last contraction high) with volume at least {}x the 20-session average",
                params.breakout.volume_multiple
            ),
            trigger_level: pivot,
            entry,
            stop,
            targets: targets(entry, stop, Some(h.high[h0]), reach(entry, atr, horizon, params), params.risk.extension_r),
            evidence,
            extension_atr: Some(extension),
            distance_pct: Some(distance),
        },
        horizon,
    )
}

// ---------------------------------------------------------------------------------------------
// THREE_WEEKS_TIGHT — the desk's TWT-1 (docs/twt/04 §3)
// ---------------------------------------------------------------------------------------------

fn week_start(d: NaiveDate) -> NaiveDate {
    d - Days::new(u64::from(d.weekday().num_days_from_monday()))
}

fn month_index(d: NaiveDate) -> i64 {
    i64::from(d.year()) * 12 + i64::from(d.month0())
}

fn is_etf_symbol(symbol: &str) -> bool {
    ["BEES", "ETF", "IETF"].iter().any(|s| symbol.ends_with(s))
}

/// `tight_state` at bar `i` (lines 1, 2, 3, 5): `(range_pct, month_low, vol_sma, state)`.
fn twt_state(h: &History<'_>, i: usize) -> (Option<f64>, Option<f64>, Option<f64>, bool) {
    let scan = &TWT.scan;
    let weeks = to_usize(scan.tight_weeks);
    let this_week = week_start(h.dates[i]);
    let mut closes = vec![Some(h.close[i])];
    for back in 1..weeks {
        let target = this_week - Days::new(7 * back as u64);
        let found = (0..i)
            .rev()
            .find(|&j| week_start(h.dates[j]) == target)
            .map(|j| h.close[j]);
        closes.push(found);
    }
    let range_pct = if closes.iter().all(Option::is_some) {
        let vals: Vec<f64> = closes.into_iter().flatten().collect();
        match (max_of(&vals), min_of(&vals)) {
            (Some(hi), Some(lo)) => Some((hi / lo - 1.0) * PCT),
            _ => None,
        }
    } else {
        None
    };
    let month = month_index(h.dates[i]) - scan.month_low_months_back;
    let month_lows: Vec<f64> = (0..i)
        .filter(|&j| month_index(h.dates[j]) == month)
        .map(|j| h.low[j])
        .collect();
    let month_low = min_of(&month_lows);
    let vol_bars = to_usize(scan.vol_sma_bars);
    let vol_sma = rolling_mean(&h.volume, i, vol_bars, to_usize(TWT.data.min_samples(scan.vol_sma_bars)));
    let state = h.close_raw(i) > dec_f(scan.min_close_raw)
        && month_low.is_some_and(|m| h.close[i] >= m * dec_f(scan.month_low_multiple))
        && range_pct.is_some_and(|r| r <= dec_f(scan.tight_band_pct))
        && vol_sma.is_some_and(|v| v >= dec_f(scan.min_vol_sma));
    (range_pct, month_low, vol_sma, state)
}

/// The desk's TWT entry event on the as-of bar: the state today, out of it on each of the
/// `entry_min_sessions_out` sessions before, above the liquidity floor, not an ETF.
#[must_use]
pub fn evaluate_twt(h: &History<'_>, params: &SwingParams, horizon: (i64, i64)) -> Option<Evaluation> {
    let row = h.row();
    let last = h.last();
    if row.symbol.as_deref().is_some_and(is_etf_symbol) {
        return None;
    }
    let gap = to_usize(TWT.entry.entry_min_sessions_out);
    if last < gap {
        return None;
    }
    let (range_pct, month_low, vol_sma, state) = twt_state(h, last);
    if !state {
        return None;
    }
    let out_before = (last - gap..last).all(|i| !twt_state(h, i).3);
    let turnover: Vec<f64> = (0..=last).map(|i| h.turnover_raw(i)).collect();
    let tbars = to_usize(TWT.entry.turnover_avg_bars);
    let turnover_avg = rolling_mean(&turnover, last, tbars, to_usize(TWT.data.min_samples(TWT.entry.turnover_avg_bars)));
    let floor = dec_f(TWT.entry.min_turnover_inr);
    let floor_ok = turnover_avg.is_some_and(|t| t >= floor);
    if !(out_before && floor_ok) {
        return None;
    }
    let close = h.close[last];
    let scan = &TWT.scan;
    let entry = close;
    let stop = entry * (1.0 - dec_f(TWT.exits.stop_pct) / PCT);
    let evidence = vec![
        ev("tight", format!("{} weekly closes within band", scan.tight_weeks), range_pct, Some(dec_f(scan.tight_band_pct)), "%", true),
        ev("month_low", format!("Close vs {}x the low {} months back", scan.month_low_multiple, scan.month_low_months_back), Some(close), month_low.map(|m| m * dec_f(scan.month_low_multiple)), "₹", true),
        ev("vol_sma", format!("{}-session average volume", scan.vol_sma_bars), vol_sma, Some(dec_f(scan.min_vol_sma)), "shares", true),
        ev("close_raw", "Exchange close", Some(h.close_raw(last)), Some(dec_f(scan.min_close_raw)), "₹", true),
        ev("entry_event", format!("Out of the state the prior {gap} sessions"), None, Some(gap as f64), "sessions", out_before),
        ev("turnover", "20-session average turnover", turnover_avg.map(|t| t / 1e7), Some(floor / 1e7), "₹ cr", floor_ok),
    ];
    finish(
        Levels {
            setup: SetupType::ThreeWeeksTight,
            triggered: true,
            condition: format!(
                "Three-weeks-tight entry event on the close (desk: buy next open, {}% stop, {}% trail)",
                TWT.exits.stop_pct, TWT.exits.trail_pct
            ),
            trigger_level: close,
            entry,
            stop,
            targets: r_targets(entry, stop, params.risk.extension_r),
            evidence,
            extension_atr: Some(0.0),
            distance_pct: None,
        },
        horizon,
    )
}

// ---------------------------------------------------------------------------------------------
// EPISODIC_PIVOT — the swing book's EP (docs/swing/04 §3)
// ---------------------------------------------------------------------------------------------

struct Gap {
    gap_pct: f64,
    rvol: f64,
    close_position: f64,
    prior_move: f64,
    adr: f64,
}

fn ep_on(h: &History<'_>, j: usize) -> Option<Gap> {
    let ep = &SWING.ep;
    let prior = to_usize(ep.prior_bars);
    if j < prior.max(1) {
        return None;
    }
    let gap_pct = (h.open[j] / h.close[j - 1] - 1.0) * PCT;
    let rvol = rvol_prior(h, j)?;
    let cp = close_position(h, j);
    let prior_move = (h.close[j - 1] / h.close[j - prior] - 1.0) * PCT;
    let adr = adr_pct(h, j)?;
    let ok = adr >= SWING.liquidity.adr_min_pct
        && gap_pct >= ep.min_gap_pct
        && rvol >= ep.min_rvol
        && h.close[j] >= h.open[j]
        && cp >= ep.min_close_position
        && prior_move <= ep.max_prior_gain_pct;
    ok.then_some(Gap { gap_pct, rvol, close_position: cp, prior_move, adr })
}

/// A gap of at least 10 % on 3x volume that held, after a quiet stretch; enterable above the
/// gap day's high for `ep.valid_bars` sessions while its low holds.
#[must_use]
pub fn evaluate_episodic_pivot(h: &History<'_>, params: &SwingParams, horizon: (i64, i64)) -> Option<Evaluation> {
    let ep = &SWING.ep;
    let row = h.row();
    let last = h.last();
    let atr = num(row.atr).filter(|a| *a > 0.0)?;
    let first = last.saturating_sub(to_usize(ep.valid_bars).saturating_sub(1));
    let (j, gap) = (first..=last).rev().find_map(|j| ep_on(h, j).map(|g| (j, g)))?;
    if j < last && min_of(&h.low[j + 1..=last])? < h.low[j] {
        return None;
    }
    let close = h.close[last];
    let level = h.high[j];
    let triggered = j < last && close > level;
    let extension = (close - level) / atr;
    if triggered && extension > params.breakout.max_extension_atr {
        return None;
    }
    let entry = if triggered { close } else { level };
    let stop = h.low[j];
    if stop_too_wide(entry, stop, gap.adr) {
        return None;
    }
    let p = &SWING.ep;
    let evidence = vec![
        ev("gap", "Opening gap", Some(gap.gap_pct), Some(p.min_gap_pct), "%", true),
        ev("rvol", format!("Volume vs prior {}-session average", p.rvol_bars), Some(gap.rvol), Some(p.min_rvol), "x", true),
        ev("held", "Close position in the gap day's range", Some(gap.close_position), Some(p.min_close_position), "", true),
        ev("neglect", format!("Move over the {} sessions before the gap", p.prior_bars), Some(gap.prior_move), Some(p.max_prior_gain_pct), "%", true),
        ev("adr", "Average daily range", Some(gap.adr), Some(SWING.liquidity.adr_min_pct), "%", true),
        ev("close_vs_level", "Close vs gap-day high", Some(close), Some(level), "₹", close > level),
    ];
    finish(
        Levels {
            setup: SetupType::EpisodicPivot,
            triggered,
            condition: format!(
                "Price trades above {level:.2} (gap-day high, {}) while the gap-day low {stop:.2} holds",
                h.dates[j]
            ),
            trigger_level: level,
            entry,
            stop,
            targets: targets(entry, stop, None, reach(entry, atr, horizon, params), params.risk.extension_r),
            evidence,
            extension_atr: Some(extension),
            distance_pct: Some((level - close) / close * PCT),
        },
        horizon,
    )
}

// ---------------------------------------------------------------------------------------------
// FLAG — the swing book's FLAG (docs/swing/04 §2), with O'Neil's high-tight label
// ---------------------------------------------------------------------------------------------

fn sma(xs: &[f64], end: usize, n: usize) -> Option<f64> {
    mean(tail(xs, end, n)?)
}

/// A 30 %+ pole, a 10–60-session base no deeper than 30 %, tight, on rising averages with
/// volume drying up; triggered on a close above the 20-session pivot on 1.5x volume.
#[must_use]
pub fn evaluate_flag(h: &History<'_>, params: &SwingParams, horizon: (i64, i64)) -> Option<Evaluation> {
    let f = &SWING.flag;
    let hp = &params.patterns.flag;
    let row = h.row();
    let atr = num(row.atr).filter(|a| *a > 0.0)?;
    let last = h.last();
    let window = to_usize(f.lookback_bars + f.base_max_bars);
    let ws = (last + 1).saturating_sub(window);
    if last < ws + 1 {
        return None;
    }
    let pole = arg_max(&h.high, ws, last)?;
    let base_bars = last - pole;
    let pole_high = h.high[pole];
    let pole_start = pole.saturating_sub(to_usize(f.lookback_bars)).max(ws);
    let pole_low_idx = arg_min(&h.low, pole_start, pole)?;
    let pole_low = h.low[pole_low_idx];
    let base = &h.low[pole + 1..=last];
    let base_low = min_of(base)?;
    let half = base_bars / 2;
    let first_half_low = min_of(&h.low[pole + 1..pole + 1 + half]);
    let second_half_low = min_of(&h.low[pole + 1 + half..=last]);
    let base_vol = mean(&h.volume[pole + 1..=last])?;
    let adr = adr_pct(h, last)?;
    let tb = to_usize(f.tight_bars);
    let tight_high = max_of(tail(&h.high, last, tb)?)?;
    let tight_low = min_of(tail(&h.low, last, tb)?)?;
    let pb = to_usize(f.pivot_bars);
    let pivot_incl = max_of(tail(&h.high, last, pb)?)?;
    let pivot_prev = max_of(tail(&h.high, last - 1, pb)?)?;
    let close = h.close[last];
    let ma_fast = sma(&h.close, last, to_usize(SWING.ma_fast))?;
    let ma_slow = sma(&h.close, last, to_usize(SWING.ma_slow))?;
    let ma_trend = sma(&h.close, last, to_usize(SWING.ma_trend))?;
    let ma_slow_before = sma(&h.close, last.checked_sub(to_usize(f.ma_rising_bars))?, to_usize(SWING.ma_slow))?;
    let vol_fast = sma(&h.volume, last, to_usize(f.dryup_bars))?;
    let rvol = rvol_prior(h, last);

    let prior_move = (pole_high / pole_low - 1.0) * PCT;
    let depth = (1.0 - base_low / pole_high) * PCT;
    let tightness = ((tight_high - tight_low) / tight_low * PCT) / adr;
    let dryup = vol_fast / base_vol;
    let dist_fast = (close / ma_fast - 1.0) * PCT;
    let dist_slow = (close / ma_slow - 1.0) * PCT;
    let liquid = adr >= SWING.liquidity.adr_min_pct;
    let base_ok = base_bars >= to_usize(f.base_min_bars)
        && base_bars <= to_usize(f.base_max_bars)
        && prior_move >= f.flagpole_min_gain_pct
        && depth <= f.base_max_depth_pct;
    let higher_lows = !f.require_higher_lows
        || matches!((first_half_low, second_half_low), (Some(a), Some(b)) if b >= a);
    let structure = liquid
        && base_ok
        && tightness <= f.tight_max_adr_multiple
        && dist_fast <= f.max_extension_adr * adr
        && dist_slow >= -f.ma_tolerance_pct
        && close >= ma_trend
        && ma_slow >= ma_slow_before
        && dryup <= f.dryup_max_ratio
        && higher_lows;
    let breakout = close > pivot_prev && rvol.is_some_and(|r| r >= f.breakout_min_rvol);
    if !(structure || (liquid && breakout && base_ok)) {
        return None;
    }
    let triggered = breakout;
    let trigger_level = if triggered { pivot_prev } else { pivot_incl };
    let entry = if triggered { close } else { pivot_incl };
    let stop = h.low[last];
    if stop_too_wide(entry, stop, adr) {
        return None;
    }
    let high_tight = prior_move >= hp.htf_min_pole_pct
        && pole - pole_low_idx <= to_usize(hp.htf_max_pole_bars)
        && depth <= hp.htf_max_depth_pct;
    let kind = if high_tight { "High tight flag" } else { "Flag" };
    let evidence = vec![
        ev("pole", "Flagpole gain", Some(prior_move), Some(f.flagpole_min_gain_pct), "%", prior_move >= f.flagpole_min_gain_pct),
        ev("base_bars", "Base length", Some(base_bars as f64), Some(f.base_max_bars as f64), "sessions", base_ok),
        ev("depth", "Base depth from pole high", Some(depth), Some(f.base_max_depth_pct), "%", depth <= f.base_max_depth_pct),
        ev("tightness", format!("{tb}-session range in ADRs"), Some(tightness), Some(f.tight_max_adr_multiple), "ADR", tightness <= f.tight_max_adr_multiple),
        ev("dryup", "Volume dry-up (10-session over base)", Some(dryup), Some(f.dryup_max_ratio), "x", dryup <= f.dryup_max_ratio),
        ev("adr", "Average daily range", Some(adr), Some(SWING.liquidity.adr_min_pct), "%", liquid),
        ev("high_tight", format!("High tight flag ({}%+ pole in ≤{} sessions, ≤{}% deep)", hp.htf_min_pole_pct, hp.htf_max_pole_bars, hp.htf_max_depth_pct), Some(prior_move), Some(hp.htf_min_pole_pct), "%", high_tight),
        ev("rvol", format!("Volume vs prior {}-session average", SWING.ep.rvol_bars), rvol, Some(f.breakout_min_rvol), "x", rvol.is_some_and(|r| r >= f.breakout_min_rvol)),
    ];
    finish(
        Levels {
            setup: SetupType::Flag,
            triggered,
            condition: format!(
                "{kind}: daily close above {trigger_level:.2} ({pb}-session high) with volume at least {}x average",
                f.breakout_min_rvol
            ),
            trigger_level,
            entry,
            stop,
            targets: targets(entry, stop, Some(trigger_level + (pole_high - pole_low)), reach(entry, atr, horizon, params), params.risk.extension_r),
            evidence,
            extension_atr: Some((close - pivot_prev) / atr),
            distance_pct: Some((pivot_incl - close) / close * PCT),
        },
        horizon,
    )
}

// ---------------------------------------------------------------------------------------------
// INSIDE_BAR_NR7 — Crabel's NR7 and the inside bar (standard definitions)
// ---------------------------------------------------------------------------------------------

fn squeeze_on(h: &History<'_>, i: usize, nr: usize) -> Option<(bool, bool)> {
    if i + 1 < nr || i == 0 {
        return None;
    }
    let range = h.high[i] - h.low[i];
    let prior: Vec<f64> = (i + 1 - nr..i).map(|j| h.high[j] - h.low[j]).collect();
    let nr7 = min_of(&prior).is_some_and(|m| range < m);
    let inside = h.high[i] <= h.high[i - 1] && h.low[i] >= h.low[i - 1];
    Some((nr7, inside))
}

fn squeeze_name(nr7: bool, inside: bool, nr: usize) -> String {
    match (nr7, inside) {
        (true, true) => format!("Inside bar + NR{nr}"),
        (true, false) => format!("NR{nr}"),
        _ => "Inside bar".to_owned(),
    }
}

/// An inside bar or the narrowest range of seven in an uptrend: pending above its high,
/// triggered on the next close above it.
#[must_use]
pub fn evaluate_squeeze(h: &History<'_>, params: &SwingParams, horizon: (i64, i64)) -> Option<Evaluation> {
    let nr = to_usize(params.patterns.squeeze.nr_bars).max(2);
    let row = h.row();
    let last = h.last();
    let atr = num(row.atr).filter(|a| *a > 0.0)?;
    let sma_mid = num(row.sma_mid)?;
    let close = h.close[last];
    let trend = close > sma_mid;
    if !trend {
        return None;
    }
    let buffer = params.risk.stop_buffer_atr * atr;
    let (bar, triggered, nr7, inside) = match last.checked_sub(1).and_then(|y| squeeze_on(h, y, nr).map(|s| (y, s))) {
        Some((y, (n7, ib))) if (n7 || ib) && close > h.high[y] => (y, true, n7, ib),
        _ => match squeeze_on(h, last, nr) {
            Some((n7, ib)) if n7 || ib => (last, false, n7, ib),
            _ => return None,
        },
    };
    let level = h.high[bar];
    let extension = (close - level) / atr;
    if triggered && extension > params.breakout.max_extension_atr {
        return None;
    }
    let entry = if triggered { close } else { level };
    let stop = h.low[bar] - buffer;
    let name = squeeze_name(nr7, inside, nr);
    let evidence = vec![
        ev("trend", "Close above 50-session average", Some(close), Some(sma_mid), "₹", trend),
        ev("nr7", format!("Narrowest range of {nr} sessions"), Some(h.high[bar] - h.low[bar]), None, "₹", nr7),
        ev("inside", "Inside the prior bar's range", None, None, "", inside),
        ev("break", "Close above the squeeze bar's high", Some(close), Some(level), "₹", triggered),
    ];
    finish(
        Levels {
            setup: SetupType::InsideBarNr7,
            triggered,
            condition: format!("{name} on {}: price trades above {level:.2}", h.dates[bar]),
            trigger_level: level,
            entry,
            stop,
            targets: targets(entry, stop, None, reach(entry, atr, horizon, params), params.risk.extension_r),
            evidence,
            extension_atr: Some(extension),
            distance_pct: Some((level - close) / close * PCT),
        },
        horizon,
    )
}

// ---------------------------------------------------------------------------------------------
// HIGH_52W_BREAKOUT (standard definition)
// ---------------------------------------------------------------------------------------------

/// A close above the prior 252 sessions' high on volume, or within reach of it.
#[must_use]
pub fn evaluate_high_52w(h: &History<'_>, params: &SwingParams, horizon: (i64, i64)) -> Option<Evaluation> {
    let lookback = to_usize(params.patterns.high_52w.lookback_bars);
    let row = h.row();
    let last = h.last();
    if last < lookback {
        return None;
    }
    let atr = num(row.atr).filter(|a| *a > 0.0)?;
    let sma_mid = num(row.sma_mid)?;
    let close = h.close[last];
    let trend = close > sma_mid;
    let pivot = max_of(&h.high[last - lookback..last])?;
    if !trend {
        return None;
    }
    let rvol = num(row.rvol);
    let (triggered, extension, distance) = pivot_state(close, pivot, atr, rvol, params)?;
    let entry = if triggered { close } else { pivot };
    let stop = entry - params.risk.stop_atr * atr;
    let p = &params.breakout;
    let evidence = vec![
        ev("trend", "Close above 50-session average", Some(close), Some(sma_mid), "₹", trend),
        ev("close_vs_high", format!("Close vs prior {lookback}-session high"), Some(close), Some(pivot), "₹", close > pivot),
        ev("rvol", "Volume vs 20-session average", rvol, Some(p.volume_multiple), "x", rvol.is_some_and(|r| r >= p.volume_multiple)),
        ev("extension", "Extension above the high", triggered.then_some(extension), Some(p.max_extension_atr), "ATR", triggered),
    ];
    finish(
        Levels {
            setup: SetupType::High52wBreakout,
            triggered,
            condition: format!(
                "Daily close above {pivot:.2} (52-week high) with volume at least {}x the 20-session average",
                p.volume_multiple
            ),
            trigger_level: pivot,
            entry,
            stop,
            targets: targets(entry, stop, None, reach(entry, atr, horizon, params), params.risk.extension_r),
            evidence,
            extension_atr: Some(extension),
            distance_pct: Some(distance),
        },
        horizon,
    )
}

// ---------------------------------------------------------------------------------------------
// CUP_WITH_HANDLE — O'Neil (standard definition)
// ---------------------------------------------------------------------------------------------

/// A rounded 12–35 % cup after a 30 % advance, a right lip near the left, a short handle in
/// the cup's upper half on lighter volume; triggered above the handle's high.
#[must_use]
pub fn evaluate_cup(h: &History<'_>, params: &SwingParams, horizon: (i64, i64)) -> Option<Evaluation> {
    let p = &params.patterns.cup;
    let row = h.row();
    let last = h.last();
    let atr = num(row.atr).filter(|a| *a > 0.0)?;
    let r_lo = last.checked_sub(to_usize(p.handle_max_bars))?;
    let r_hi = last.checked_sub(to_usize(p.handle_min_bars))?;
    let r = arg_max(&h.high, r_lo, r_hi + 1)?;
    let lip = h.high[r];
    if max_of(&h.high[r + 1..last]).is_some_and(|m| m > lip) {
        return None;
    }
    let l_hi = r.checked_sub(to_usize(p.cup_min_bars))?;
    let l_lo = r.saturating_sub(to_usize(p.cup_max_bars));
    let l = arg_max(&h.high, l_lo, l_hi + 1)?;
    let left = h.high[l];
    if max_of(&h.high[l + 1..r]).is_some_and(|m| m > left.max(lip)) {
        return None;
    }
    let lip_ok = lip >= left * (1.0 - p.right_lip_min_pct / PCT) && lip <= left * (1.0 + p.right_lip_max_pct / PCT);
    let b = arg_min(&h.low, l + 1, r)?;
    let bottom = h.low[b];
    let depth = (left - bottom) / left * PCT;
    let depth_ok = depth >= p.min_depth_pct && depth <= p.max_depth_pct;
    let share = (b - l) as f64 / (r - l) as f64;
    let rounded = share >= p.bottom_edge_share && share <= 1.0 - p.bottom_edge_share;
    let prior_start = l.checked_sub(to_usize(p.prior_bars))?;
    let prior_low = min_of(&h.low[prior_start..l])?;
    let advance = (left / prior_low - 1.0) * PCT;
    let advance_ok = advance >= p.prior_uptrend_pct;
    let handle_low = min_of(&h.low[r + 1..=last])?;
    let handle_depth = (lip - handle_low) / lip * PCT;
    let upper_half = handle_low >= bottom + 0.5 * (lip - bottom);
    let handle_ok = handle_depth <= p.handle_max_depth_pct && upper_half;
    let handle_vol = mean(&h.volume[r + 1..last])?;
    let cup_vol = mean(&h.volume[l..=r])?;
    let dry = handle_vol <= cup_vol;
    if !(lip_ok && depth_ok && rounded && advance_ok && handle_ok && dry) {
        return None;
    }
    let close = h.close[last];
    let rvol = num(row.rvol);
    let (triggered, extension, distance) = pivot_state(close, lip, atr, rvol, params)?;
    let entry = if triggered { close } else { lip };
    let stop = handle_low - params.risk.stop_buffer_atr * atr;
    let evidence = vec![
        ev("advance", format!("Advance into the left lip ({} sessions)", p.prior_bars), Some(advance), Some(p.prior_uptrend_pct), "%", advance_ok),
        ev("depth", "Cup depth", Some(depth), Some(p.max_depth_pct), "%", depth_ok),
        ev("length", "Cup length", Some((r - l) as f64), Some(p.cup_min_bars as f64), "sessions", true),
        ev("right_lip", "Right lip vs left lip", Some((lip / left - 1.0) * PCT), Some(-p.right_lip_min_pct), "%", lip_ok),
        ev("handle_depth", "Handle depth", Some(handle_depth), Some(p.handle_max_depth_pct), "%", handle_ok),
        ev("handle_volume", "Handle volume over cup volume", Some(handle_vol / cup_vol), Some(1.0), "x", dry),
        ev("close_vs_pivot", "Close vs handle high", Some(close), Some(lip), "₹", close > lip),
        ev("rvol", "Volume vs 20-session average", rvol, Some(params.breakout.volume_multiple), "x", rvol.is_some_and(|v| v >= params.breakout.volume_multiple)),
    ];
    finish(
        Levels {
            setup: SetupType::CupWithHandle,
            triggered,
            condition: format!(
                "Daily close above {lip:.2} (handle high) with volume at least {}x the 20-session average",
                params.breakout.volume_multiple
            ),
            trigger_level: lip,
            entry,
            stop,
            targets: targets(entry, stop, Some(lip + (left - bottom)), reach(entry, atr, horizon, params), params.risk.extension_r),
            evidence,
            extension_atr: Some(extension),
            distance_pct: Some(distance),
        },
        horizon,
    )
}

// ---------------------------------------------------------------------------------------------
// MA_BOUNCE — 10/20/50 EMA (standard definition)
// ---------------------------------------------------------------------------------------------

/// Stacked, rising EMAs; a recent low tagged one of them and the close held above it; triggered
/// on a close above the prior bar's high.
#[must_use]
pub fn evaluate_ma_bounce(h: &History<'_>, params: &SwingParams, horizon: (i64, i64)) -> Option<Evaluation> {
    let p = &params.patterns.ma_bounce;
    let row = h.row();
    let last = h.last();
    let atr = num(row.atr).filter(|a| *a > 0.0)?;
    let [s_fast, s_mid, s_slow] = p.spans;
    if last < to_usize(s_slow + p.slope_bars) {
        return None;
    }
    let emas = [ema(&h.close, s_fast), ema(&h.close, s_mid), ema(&h.close, s_slow)];
    let (e10, e20, e50) = (emas[0][last], emas[1][last], emas[2][last]);
    let e50_before = emas[2][last - to_usize(p.slope_bars)];
    let close = h.close[last];
    let stacked = e10 > e20 && e20 > e50;
    let rising = e50 > e50_before;
    if !(stacked && rising && close > e50) {
        return None;
    }
    let reach_atr = p.touch_atr * atr;
    let first = (last + 1).saturating_sub(to_usize(p.touch_bars).max(1));
    let (t, k) = (first..=last).rev().find_map(|t| {
        (0..3).rev().find(|&k| h.low[t] <= emas[k][t] + reach_atr && h.close[t] > emas[k][t]).map(|k| (t, k))
    })?;
    let span = p.spans[k];
    let support = emas[k][t];
    let prev_high = h.high[last - 1];
    let triggered = close > prev_high;
    let trigger_level = if triggered { prev_high } else { h.high[last] };
    let entry = if triggered { close } else { h.high[last] };
    let low = min_of(&h.low[t..=last])?;
    let stop = low.min(support) - params.risk.stop_buffer_atr * atr;
    let swing_high = num(row.swing_high);
    let evidence = vec![
        ev("stack", format!("EMA {s_fast} > {s_mid} > {s_slow}"), Some(e10), Some(e20), "₹", stacked),
        ev("slope", format!("{s_slow} EMA rising over {} sessions", p.slope_bars), Some(e50), Some(e50_before), "₹", rising),
        ev("touch", format!("Low tagged the {span} EMA on {}", h.dates[t]), Some(h.low[t]), Some(support), "₹", true),
        ev("reversal", "Close above prior bar high", Some(close), Some(prev_high), "₹", triggered),
    ];
    finish(
        Levels {
            setup: SetupType::MaBounce,
            triggered,
            condition: format!("Bounce off the {span} EMA: price trades above {trigger_level:.2}"),
            trigger_level,
            entry,
            stop,
            targets: targets(entry, stop, swing_high, reach(entry, atr, horizon, params), params.risk.extension_r),
            evidence,
            extension_atr: Some((close - prev_high) / atr),
            distance_pct: Some((h.high[last] - close) / close * PCT),
        },
        horizon,
    )
}

// ---------------------------------------------------------------------------------------------
// VBT — the desk's VBT-1 (docs/vbt/04 §3)
// ---------------------------------------------------------------------------------------------

/// Chartink's five lines and filters A–F on bar `last`; the evidence of a full signal
/// (`SIGNAL`), `None` otherwise.
fn vbt_signal(h: &History<'_>, last: usize) -> Option<Vec<Evidence>> {
    let (scan, trend) = (&VBT.scan, &VBT.trend);
    if last == 0 {
        return None;
    }
    let samples = |bars: i64| VBT.data.min_samples(bars).ok().map(to_usize);
    let close = h.close[last];
    let volume = h.volume[last];
    let change = (close / h.close[last - 1] - 1.0) * PCT;
    let vol_sma = rolling_mean(&h.volume, last, to_usize(scan.vol_sma_bars), samples(scan.vol_sma_bars)?);
    let line1 = vol_sma.is_some_and(|s| volume > s * scan.vol_mult);
    let line2 = h.close_raw(last) > scan.min_close_raw_inr;
    let line3 = change >= scan.min_change_pct;
    let line4 = vol_sma.is_some_and(|s| s >= scan.min_vol_sma);
    let line5 = volume > scan.min_volume;
    if !(line1 && line2 && line3 && line4 && line5) {
        return None;
    }
    let sma_dma = rolling_mean(&h.close, last, to_usize(trend.dma_bars), samples(trend.dma_bars)?);
    let hb = to_usize(trend.breakout_high_bars);
    let high_prior = {
        let end = last - 1;
        let have = (end + 1).min(hb);
        (have >= samples(trend.breakout_high_bars)?).then(|| max_of(&h.high[end + 1 - have..=end])).flatten()
    };
    let ret = last
        .checked_sub(to_usize(trend.ret_bars))
        .map(|b| (close / h.close[b] - 1.0) * PCT);
    let cp = close_position(h, last);
    let turnover: Vec<f64> = (0..=last).map(|i| h.turnover_raw(i)).collect();
    let turnover_avg = rolling_mean(&turnover, last, to_usize(trend.turnover_bars), samples(trend.turnover_bars)?);
    let a = sma_dma.is_some_and(|s| close > s);
    let b = high_prior.is_some_and(|hp| close > hp);
    let c = ret.is_some_and(|r| r < trend.max_ret_20_pct);
    let d = cp >= trend.min_close_position;
    let e = change <= trend.max_change_pct;
    let f = turnover_avg.is_some_and(|t| t >= trend.min_turnover_avg_inr);
    if !(a && b && c && d && e && f) {
        return None;
    }
    Some(vec![
        ev("volume", format!("Volume vs {}x the {}-session average", scan.vol_mult, scan.vol_sma_bars), vol_sma.map(|s| volume / s), Some(scan.vol_mult), "x", line1),
        ev("change", "Change on the day", Some(change), Some(scan.min_change_pct), "%", line3 && e),
        ev("A", format!("Close above {}-session average", trend.dma_bars), Some(close), sma_dma, "₹", a),
        ev("B", format!("Close above prior {hb}-session high"), Some(close), high_prior, "₹", b),
        ev("C", format!("{}-session return below ceiling", trend.ret_bars), ret, Some(trend.max_ret_20_pct), "%", c),
        ev("D", "Close position in the day's range", Some(cp), Some(trend.min_close_position), "", d),
        ev("F", "20-session average turnover", turnover_avg.map(|t| t / 1e7), Some(trend.min_turnover_avg_inr / 1e7), "₹ cr", f),
    ])
}

/// The desk's VBT-1 entry (`docs/vbt/04` §7): a buy limit at the signal bar's close, working
/// `entry.valid_sessions` sessions. Triggered on the signal bar itself; pending while a recent
/// signal's limit is still unfilled; the stop is `exits.stop_pct` below the limit.
#[must_use]
pub fn evaluate_vbt(h: &History<'_>, params: &SwingParams, horizon: (i64, i64)) -> Option<Evaluation> {
    let last = h.last();
    let valid = to_usize(VBT.entry.valid_sessions).max(1);
    let first = (last + 1).saturating_sub(valid);
    let (j, mut evidence) = (first..=last).rev().find_map(|j| vbt_signal(h, j).map(|e| (j, e)))?;
    let limit = h.close[j];
    let through = limit * (1.0 - VBT.entry.fill_through_pct / PCT);
    if j < last && min_of(&h.low[j + 1..=last]).is_some_and(|l| l <= through) {
        return None;
    }
    let triggered = j == last;
    let close = h.close[last];
    let stop = limit * (1.0 - VBT.exits.stop_pct / PCT);
    evidence.push(ev("limit", format!("Signal on {}; limit unfilled", h.dates[j]), Some(close), Some(limit), "₹", true));
    finish(
        Levels {
            setup: SetupType::Vbt,
            triggered,
            condition: format!(
                "VBT-1 buy limit at {limit:.2} (signal close {}), working {valid} sessions; {}% stop",
                h.dates[j], VBT.exits.stop_pct
            ),
            trigger_level: limit,
            entry: limit,
            stop,
            targets: r_targets(limit, stop, params.risk.extension_r),
            evidence,
            extension_atr: Some(0.0),
            distance_pct: Some((limit - close) / close * PCT),
        },
        horizon,
    )
}

/// Dispatch a history setup; `None` for the row-only setups.
#[must_use]
pub fn evaluate_history(
    h: &History<'_>,
    setup: SetupType,
    params: &SwingParams,
    horizon: (i64, i64),
) -> Option<Evaluation> {
    match setup {
        SetupType::Breakout | SetupType::Pullback | SetupType::RelativeStrength => None,
        SetupType::Vcp => evaluate_vcp(h, params, horizon),
        SetupType::ThreeWeeksTight => evaluate_twt(h, params, horizon),
        SetupType::EpisodicPivot => evaluate_episodic_pivot(h, params, horizon),
        SetupType::Flag => evaluate_flag(h, params, horizon),
        SetupType::InsideBarNr7 => evaluate_squeeze(h, params, horizon),
        SetupType::High52wBreakout => evaluate_high_52w(h, params, horizon),
        SetupType::CupWithHandle => evaluate_cup(h, params, horizon),
        SetupType::MaBounce => evaluate_ma_bounce(h, params, horizon),
        SetupType::Vbt => evaluate_vbt(h, params, horizon),
    }
}
