//! Per-stock regime signal (Maulik, 7 Nov 2026; docs/swing/REGIME-SIGNAL.md): a stock is GREEN
//! when any index it belongs to, or any index its NSE industry maps to, has EMA(10) > EMA(20);
//! RED when none does; with no index at all, the Smallcap-250 rule decides (`fallback`). Pure.

use std::collections::BTreeMap;

/// The fallback index (`index_master.name`), the legacy gate's SW29 index.
pub const FALLBACK_INDEX: &str = "Nifty Smallcap 250";
/// `fallback_reason` when nothing maps.
pub const NO_MAPPING: &str = "No index mapping — Smallcap fallback";
/// `fallback_reason` when indices map but none has an EMA reading.
pub const NO_DATA: &str = "No index data — Smallcap fallback";

/// How an index was reached.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Via {
    /// The stock is a constituent.
    Member,
    /// The stock's industry maps to it (`sector_index_map`).
    Sector,
    /// No membership or industry: the size index for its market-cap rank ([`size_index`]).
    Size,
}

impl Via {
    /// The wire value.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Member => "member",
            Self::Sector => "sector",
            Self::Size => "size",
        }
    }
}

/// One index's EMA pair on the day.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Momentum {
    /// EMA(10).
    pub ema10: f64,
    /// EMA(20).
    pub ema20: f64,
    /// EMA(10) > EMA(20), as compared before rounding (`index_stats_daily.ema_cross`).
    pub up: bool,
}

impl Momentum {
    /// From the two averages.
    #[must_use]
    pub fn new(ema10: f64, ema20: f64) -> Self {
        Self {
            ema10,
            ema20,
            up: ema10 > ema20,
        }
    }
}

/// An index the rule read.
#[derive(Debug, Clone, PartialEq)]
pub struct Considered {
    /// `index_master.name`.
    pub name: String,
    /// Member wins over sector when both.
    pub via: Via,
    /// EMA(10).
    pub ema10: f64,
    /// EMA(20).
    pub ema20: f64,
    /// EMA(10) > EMA(20).
    pub up: bool,
}

/// The verdict for one stock.
#[derive(Debug, Clone, PartialEq)]
pub struct StockSignal {
    /// GREEN.
    pub green: bool,
    /// Decided by the Smallcap-250 rule.
    pub fallback: bool,
    /// Every index read, in order (members first, then sector).
    pub indices: Vec<Considered>,
    /// The considered indices that are up.
    pub triggered: Vec<String>,
    /// Why it fell back.
    pub fallback_reason: Option<&'static str>,
}

impl StockSignal {
    /// `GREEN` / `RED`.
    #[must_use]
    pub const fn signal(&self) -> &'static str {
        if self.green { "GREEN" } else { "RED" }
    }
}

/// The indices `industry` maps to (`sector_index_map`), empty when unmapped.
#[must_use]
pub fn sector_indices<'a>(
    industry: Option<&str>,
    map: &'a BTreeMap<String, Vec<String>>,
) -> &'a [String] {
    industry
        .map(str::trim)
        .and_then(|i| {
            map.iter()
                .find(|(k, _)| k.trim().eq_ignore_ascii_case(i))
                .map(|(_, v)| v.as_slice())
        })
        .unwrap_or(&[])
}

/// The NSE size index for a market-cap rank (1 = largest listed company): top 100 Nifty 100,
/// 101-250 Midcap 150, 251-500 Smallcap 250, beyond that Microcap 250 (the smallest size index NSE
/// publishes, the nearest proxy below rank 750).
#[must_use]
pub const fn size_index(rank: u32) -> &'static str {
    match rank {
        0..=100 => "Nifty 100",
        101..=250 => "Nifty Midcap 150",
        251..=500 => "Nifty Smallcap 250",
        _ => "Nifty Microcap 250",
    }
}

/// The rule. `member_of` / `sector` are index names; `momentum` holds the day's EMA pair of every
/// index with one; `size` is the market-cap size index, read only when nothing else maps;
/// `smallcap_up` is the fallback's verdict for the day.
#[must_use]
pub fn stock_signal(
    member_of: &[String],
    sector: &[String],
    size: Option<&str>,
    momentum: &BTreeMap<String, Momentum>,
    smallcap_up: bool,
) -> StockSignal {
    let mut indices: Vec<Considered> = Vec::new();
    let tagged = member_of
        .iter()
        .map(|n| (n, Via::Member))
        .chain(sector.iter().map(|n| (n, Via::Sector)));
    let mut mapped = false;
    for (name, via) in tagged {
        mapped = true;
        if indices.iter().any(|c| &c.name == name) {
            continue;
        }
        if let Some(m) = momentum.get(name) {
            indices.push(Considered {
                name: name.clone(),
                via,
                ema10: m.ema10,
                ema20: m.ema20,
                up: m.up,
            });
        }
    }
    if !mapped
        && let Some(name) = size
        && let Some(m) = momentum.get(name)
    {
        indices.push(Considered {
            name: name.to_owned(),
            via: Via::Size,
            ema10: m.ema10,
            ema20: m.ema20,
            up: m.up,
        });
    }
    if indices.is_empty() {
        return StockSignal {
            green: smallcap_up,
            fallback: true,
            indices,
            triggered: Vec::new(),
            fallback_reason: Some(if mapped { NO_DATA } else { NO_MAPPING }),
        };
    }
    let triggered: Vec<String> = indices
        .iter()
        .filter(|c| c.up)
        .map(|c| c.name.clone())
        .collect();
    StockSignal {
        green: !triggered.is_empty(),
        fallback: false,
        indices,
        triggered,
        fallback_reason: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn up() -> Momentum {
Momentum::new(110.0, 100.0)
    }
    fn down() -> Momentum {
Momentum::new(90.0, 100.0)
    }
    fn names(xs: &[&str]) -> Vec<String> {
        xs.iter().map(|s| (*s).to_owned()).collect()
    }
    fn momentum(xs: &[(&str, Momentum)]) -> BTreeMap<String, Momentum> {
        xs.iter().map(|(n, m)| ((*n).to_owned(), *m)).collect()
    }

    #[test]
    fn one_index_member_up_is_green_and_down_is_red() {
        let m = momentum(&[("Nifty IT", up())]);
        let s = stock_signal(&names(&["Nifty IT"]), &[], None, &m, false);
        assert!(s.green && !s.fallback);
        assert_eq!(s.triggered, names(&["Nifty IT"]));
        assert_eq!(s.indices[0].via, Via::Member);

        let m = momentum(&[("Nifty IT", down())]);
        let s = stock_signal(&names(&["Nifty IT"]), &[], None, &m, true);
        assert!(!s.green && !s.fallback);
        assert!(s.triggered.is_empty());
        assert_eq!(s.signal(), "RED");
    }

    #[test]
    fn mixed_memberships_are_green_when_any_index_is_up() {
        let m = momentum(&[
            ("Nifty 500", down()),
            ("Nifty Midcap 150", down()),
            ("Nifty Pharma", up()),
        ]);
        let s = stock_signal(
            &names(&["Nifty 500", "Nifty Midcap 150", "Nifty Pharma"]),
            &[],
            None,
            &m,
            false,
        );
        assert!(s.green);
        assert_eq!(s.indices.len(), 3);
        assert_eq!(s.triggered, names(&["Nifty Pharma"]));
    }

    #[test]
    fn no_membership_but_a_sector_match_reads_the_sector_indices() {
        let mut map = BTreeMap::new();
        map.insert(
            "Chemicals".to_owned(),
            names(&["Nifty Chemicals", "Nifty Commodities"]),
        );
        let sector = sector_indices(Some("chemicals"), &map);
        let m = momentum(&[("Nifty Chemicals", down()), ("Nifty Commodities", up())]);
        let s = stock_signal(&[], sector, None, &m, false);
        assert!(s.green && !s.fallback);
        assert!(s.indices.iter().all(|c| c.via == Via::Sector));
        assert_eq!(s.triggered, names(&["Nifty Commodities"]));
    }

    #[test]
    fn no_mapping_reads_the_size_index_for_the_market_cap_rank() {
        let m = momentum(&[("Nifty Microcap 250", up()), ("Nifty Smallcap 250", down())]);
        assert_eq!(size_index(1068), "Nifty Microcap 250");
        assert_eq!(size_index(300), "Nifty Smallcap 250");
        let s = stock_signal(&[], &[], Some(size_index(1068)), &m, false);
        assert!(s.green && !s.fallback);
        assert_eq!(s.indices[0].via, Via::Size);
    }

    #[test]
    fn no_mapping_at_all_falls_back_to_the_smallcap_rule() {
        let map = BTreeMap::new();
        let m = momentum(&[("Nifty IT", up())]);
        let sector = sector_indices(Some("Textiles"), &map);
        let s = stock_signal(&[], sector, None, &m, true);
        assert!(s.green && s.fallback);
        assert_eq!(s.fallback_reason, Some(NO_MAPPING));
        let s = stock_signal(&[], sector, None, &m, false);
        assert!(!s.green && s.fallback);
        assert_eq!(s.fallback_reason, Some(NO_MAPPING));
    }
}
