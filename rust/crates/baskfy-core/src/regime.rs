//! Index-led regime (Maulik, 6 Nov 2026): which NSE indices lead, whether the swing gate is open,
//! and the universe the leading indices' constituents give. Pure — closes in, verdicts out.

use std::collections::BTreeMap;

use chrono::NaiveDate;

pub mod signal;

/// `sw_market_daily.index_slug` (and the desk's live label) when the index-led verdict set the gate.
pub const INDEX_LED_SLUG: &str = "index-led";
/// `sw_market_daily.index_slug` when the per-stock signal holds the index half (breadth only here).
pub const PER_STOCK_SLUG: &str = "per-stock";
/// The relative-strength benchmark (`index_master.name`).
pub const BENCHMARK: &str = "Nifty 50";
/// Calendar days behind the 30-day return.
pub const RET_DAYS: u64 = 30;
/// Sessions in the fast and slow averages.
pub const FAST: usize = 10;
/// Slow average.
pub const SLOW: usize = 20;
/// Weight of each extra leading index a symbol belongs to.
pub const COUNT_WEIGHT: f64 = 0.5;

/// `regime.mode`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum RegimeMode {
    /// Each swing entry gated by that stock's own index signal (Maulik, 7 Nov 2026).
    #[default]
    PerStock,
    /// The leading-index verdict gates entries and picks the universe.
    IndexLed,
    /// The SW29 Smallcap-250 gate, untouched.
    Legacy,
}

impl RegimeMode {
    /// The env/wire value.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::PerStock => "per_stock",
            Self::IndexLed => "index_led",
            Self::Legacy => "legacy",
        }
    }

    /// Parse `per_stock` / `index_led` / `legacy` (case-insensitive, `-` accepted).
    #[must_use]
    pub fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_ascii_lowercase().replace('-', "_").as_str() {
            "per_stock" => Some(Self::PerStock),
            "index_led" => Some(Self::IndexLed),
            "legacy" => Some(Self::Legacy),
            _ => None,
        }
    }
}

/// `BASKFY_REGIME_*`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegimeConfig {
    /// `BASKFY_REGIME_MODE`.
    pub mode: RegimeMode,
    /// `BASKFY_REGIME_MIN_LEADING`.
    pub min_leading: usize,
    /// `BASKFY_REGIME_INDEX_GROUPS` (`index_master.grp` values).
    pub groups: Vec<String>,
}

/// Env names, in declaration order.
pub const ENV_MODE: &str = "BASKFY_REGIME_MODE";
/// Min leading.
pub const ENV_MIN_LEADING: &str = "BASKFY_REGIME_MIN_LEADING";
/// Groups.
pub const ENV_GROUPS: &str = "BASKFY_REGIME_INDEX_GROUPS";
/// Default groups.
pub const DEFAULT_GROUPS: &str = "Broad market,Sectoral,Thematic";

impl Default for RegimeConfig {
    fn default() -> Self {
        Self {
            mode: RegimeMode::PerStock,
            min_leading: 1,
            groups: split_groups(DEFAULT_GROUPS),
        }
    }
}

fn split_groups(raw: &str) -> Vec<String> {
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect()
}

impl RegimeConfig {
    /// From raw values (`None`/blank = default).
    ///
    /// # Errors
    /// A mode that is not `per_stock`, `index_led` or `legacy`, or a non-integer minimum.
    pub fn from_raw(
        mode: Option<&str>,
        min_leading: Option<&str>,
        groups: Option<&str>,
    ) -> Result<Self, String> {
        let mut out = Self::default();
        if let Some(m) = mode.filter(|s| !s.trim().is_empty()) {
            out.mode = RegimeMode::parse(m)
                .ok_or_else(|| format!("{ENV_MODE}: {m:?} is not per_stock, index_led or legacy"))?;
        }
        if let Some(n) = min_leading.filter(|s| !s.trim().is_empty()) {
            out.min_leading = n
                .trim()
                .parse::<usize>()
                .map_err(|e| format!("{ENV_MIN_LEADING}: {e}"))?;
        }
        if let Some(g) = groups.filter(|s| !s.trim().is_empty()) {
            out.groups = split_groups(g);
        }
        Ok(out)
    }

    /// From a lookup the caller owns (the environment, a map).
    ///
    /// # Errors
    /// As [`RegimeConfig::from_raw`].
    pub fn from_lookup(lookup: &dyn Fn(&str) -> Option<String>) -> Result<Self, String> {
        Self::from_raw(
            lookup(ENV_MODE).as_deref(),
            lookup(ENV_MIN_LEADING).as_deref(),
            lookup(ENV_GROUPS).as_deref(),
        )
    }

    /// Is `group` enabled (case-insensitive)?
    #[must_use]
    pub fn has_group(&self, group: &str) -> bool {
        self.groups.iter().any(|g| g.eq_ignore_ascii_case(group))
    }
}

/// One index's closes, oldest first.
#[derive(Debug, Clone, PartialEq)]
pub struct IndexSeries {
    /// `index_master.name`.
    pub name: String,
    /// `(date, close)`, ascending.
    pub closes: Vec<(NaiveDate, f64)>,
}

impl IndexSeries {
    /// The closes on or before `on`.
    #[must_use]
    pub fn upto(&self, on: NaiveDate) -> &[(NaiveDate, f64)] {
        let end = self.closes.partition_point(|(d, _)| *d <= on);
        self.closes.get(..end).unwrap_or(&[])
    }
}

/// One index's reading.
#[derive(Debug, Clone, PartialEq)]
pub struct IndexStat {
    /// Name.
    pub name: String,
    /// Last close.
    pub close: f64,
    /// SMA(10).
    pub dma10: f64,
    /// SMA(20).
    pub dma20: f64,
    /// `close / close 30 calendar days back − 1`.
    pub ret_30d: Option<f64>,
}

impl IndexStat {
    /// `close > 20DMA`.
    #[must_use]
    pub fn above20(&self) -> bool {
        self.close > self.dma20
    }
    /// `10DMA > 20DMA`.
    #[must_use]
    pub fn cross(&self) -> bool {
        self.dma10 > self.dma20
    }
    /// `close / 20DMA − 1`.
    #[must_use]
    pub fn strength(&self) -> f64 {
        self.close / self.dma20 - 1.0
    }
}

fn sma(values: &[(NaiveDate, f64)], n: usize) -> Option<f64> {
    let tail = values.get(values.len().checked_sub(n)?..)?;
    #[allow(clippy::cast_precision_loss)]
    let len = n as f64;
    Some(tail.iter().map(|(_, c)| c).sum::<f64>() / len)
}

/// The reading over `closes` (oldest first); `None` with fewer than [`SLOW`] closes.
#[must_use]
pub fn index_stat(name: &str, closes: &[(NaiveDate, f64)]) -> Option<IndexStat> {
    let &(last_day, close) = closes.last()?;
    let dma20 = sma(closes, SLOW)?;
    let dma10 = sma(closes, FAST)?;
    if !(close.is_finite() && dma20.is_finite() && dma20 > 0.0) {
        return None;
    }
    let back = last_day.checked_sub_days(chrono::Days::new(RET_DAYS))?;
    let base = closes
        .get(..closes.partition_point(|(d, _)| *d <= back))
        .and_then(<[_]>::last)
        .map(|(_, c)| *c)
        .filter(|c| *c > 0.0);
    Some(IndexStat {
        name: name.to_owned(),
        close,
        dma10,
        dma20,
        ret_30d: base.map(|b| close / b - 1.0),
    })
}

/// A leading index.
#[derive(Debug, Clone, PartialEq)]
pub struct LeadingIndex {
    /// Name.
    pub index: String,
    /// `close / 20DMA − 1`.
    pub strength: f64,
    /// `ret_30d − NIFTY 50 ret_30d`.
    pub rs: Option<f64>,
}

/// The scan's verdict.
#[derive(Debug, Clone, PartialEq)]
pub struct RegimeVerdict {
    /// Indices with enough closes to read.
    pub evaluated: usize,
    /// Leaders, ranked strength then rs (descending).
    pub leading: Vec<LeadingIndex>,
    /// The configured minimum.
    pub min_leading: usize,
    /// `leading.len() >= min_leading` (never open with nothing evaluated).
    pub gate_open: bool,
}

/// Leading = above 20DMA and 10DMA > 20DMA, over `series` as of `on` (`None` = their last close).
#[must_use]
pub fn scan(
    series: &[IndexSeries],
    benchmark: Option<&IndexSeries>,
    on: Option<NaiveDate>,
    min_leading: usize,
) -> RegimeVerdict {
    let cut = |s: &IndexSeries| -> Vec<(NaiveDate, f64)> {
        on.map_or_else(|| s.closes.clone(), |d| s.upto(d).to_vec())
    };
    let bench_ret = benchmark.and_then(|b| index_stat(&b.name, &cut(b)).and_then(|s| s.ret_30d));
    let mut evaluated = 0;
    let mut leading = Vec::new();
    for s in series {
        let Some(stat) = index_stat(&s.name, &cut(s)) else {
            continue;
        };
        evaluated += 1;
        if stat.above20() && stat.cross() {
            leading.push(LeadingIndex {
                index: stat.name.clone(),
                strength: stat.strength(),
                rs: stat.ret_30d.zip(bench_ret).map(|(r, b)| r - b),
            });
        }
    }
    leading.sort_by(|a, b| {
        b.strength
            .total_cmp(&a.strength)
            .then_with(|| match (a.rs, b.rs) {
                (Some(x), Some(y)) => y.total_cmp(&x),
                (Some(_), None) => std::cmp::Ordering::Less,
                (None, Some(_)) => std::cmp::Ordering::Greater,
                (None, None) => std::cmp::Ordering::Equal,
            })
            .then_with(|| a.index.cmp(&b.index))
    });
    let gate_open = evaluated > 0 && leading.len() >= min_leading;
    RegimeVerdict {
        evaluated,
        leading,
        min_leading,
        gate_open,
    }
}

/// One index a member came through.
#[derive(Debug, Clone, PartialEq)]
pub struct Via {
    /// Index name.
    pub index: String,
    /// Its strength.
    pub strength: f64,
}

/// One universe member.
#[derive(Debug, Clone, PartialEq)]
pub struct UniverseMember {
    /// NSE symbol.
    pub symbol: String,
    /// The leading indices it belongs to, strongest first.
    pub via: Vec<Via>,
    /// `max strength + 0.5 × count`.
    pub score: f64,
}

/// The union of the leaders' constituents (`constituents`: index name → symbols), deduplicated.
#[must_use]
pub fn universe(
    leading: &[LeadingIndex],
    constituents: &BTreeMap<String, Vec<String>>,
) -> BTreeMap<String, UniverseMember> {
    let mut out: BTreeMap<String, UniverseMember> = BTreeMap::new();
    for l in leading {
        for symbol in constituents.get(&l.index).map_or(&[][..], Vec::as_slice) {
            let m = out.entry(symbol.clone()).or_insert_with(|| UniverseMember {
                symbol: symbol.clone(),
                via: Vec::new(),
                score: 0.0,
            });
            if !m.via.iter().any(|v| v.index == l.index) {
                m.via.push(Via {
                    index: l.index.clone(),
                    strength: l.strength,
                });
            }
        }
    }
    for m in out.values_mut() {
        let max = m
            .via
            .iter()
            .map(|v| v.strength)
            .fold(f64::NEG_INFINITY, f64::max);
        #[allow(clippy::cast_precision_loss)]
        let count = m.via.len() as f64;
        m.score = max + COUNT_WEIGHT * count;
    }
    out
}

/// `score = scanner score × universe_score`.
#[must_use]
pub fn candidate_score(scanner_score: f64, universe_score: f64) -> f64 {
    scanner_score * universe_score
}

/// A scanner's rank turned into a 0–1 score: first 1.0, falling evenly.
#[must_use]
pub fn rank_score(position: usize, count: usize) -> f64 {
    if count == 0 {
        return 0.0;
    }
    #[allow(clippy::cast_precision_loss)]
    let s = 1.0 - position as f64 / count as f64;
    s
}
