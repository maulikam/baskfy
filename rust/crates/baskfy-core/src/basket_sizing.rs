//! Port of `baskfy_core.basket_sizing`: a screen's names, an amount, and the arithmetic that
//! joins them. Pure; money is Python `Decimal` ([`WideDecimal`]).

use std::collections::BTreeSet;

use crate::decimal::{Rounding, WideDecimal};

/// `HoldingProfile`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HoldingProfile {
    /// `CONSERVATIVE`.
    Conservative,
    /// `BALANCED`.
    Balanced,
    /// `AGGRESSIVE`.
    Aggressive,
}

impl HoldingProfile {
    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Conservative => "CONSERVATIVE",
            Self::Balanced => "BALANCED",
            Self::Aggressive => "AGGRESSIVE",
        }
    }

    /// `HoldingProfile(value)`.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "CONSERVATIVE" => Some(Self::Conservative),
            "BALANCED" => Some(Self::Balanced),
            "AGGRESSIVE" => Some(Self::Aggressive),
            _ => None,
        }
    }

    fn suggested(self) -> i64 {
        match self {
            Self::Conservative => 25,
            Self::Balanced => 20,
            Self::Aggressive => 12,
        }
    }
}

/// `WeightMethod`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WeightMethod {
    /// `EQUAL`.
    Equal,
    /// `RANK`.
    Rank,
    /// `SCORE`.
    Score,
    /// `INV_VOL`.
    InvVol,
    /// `CUSTOM`.
    Custom,
}

impl WeightMethod {
    /// Every method, in declaration order.
    pub const ALL: [WeightMethod; 5] = [
        Self::Equal,
        Self::Rank,
        Self::Score,
        Self::InvVol,
        Self::Custom,
    ];

    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Equal => "EQUAL",
            Self::Rank => "RANK",
            Self::Score => "SCORE",
            Self::InvVol => "INV_VOL",
            Self::Custom => "CUSTOM",
        }
    }

    /// `WeightMethod(value)`.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|m| m.value() == value)
    }
}

/// `DEFAULT_PROFILE`.
pub const DEFAULT_PROFILE: HoldingProfile = HoldingProfile::Balanced;
/// `DEFAULT_METHOD`.
pub const DEFAULT_METHOD: WeightMethod = WeightMethod::Equal;
/// `MIN_HOLDINGS`.
pub const MIN_HOLDINGS: i64 = 2;
/// `MAX_HOLDINGS`.
pub const MAX_HOLDINGS: i64 = 50;

fn d(v: i64) -> WideDecimal {
    WideDecimal::from(v)
}

/// `MIN_CASH_BUFFER_PCT`.
#[must_use]
pub fn min_cash_buffer_pct() -> WideDecimal {
    d(5)
}

/// `Candidate`.
#[derive(Debug, Clone, PartialEq)]
pub struct Candidate {
    /// `symbol`.
    pub symbol: String,
    /// `rank`.
    pub rank: i64,
    /// `price`.
    pub price: Option<WideDecimal>,
    /// `score`.
    pub score: Option<WideDecimal>,
    /// `vol`.
    pub vol: Option<WideDecimal>,
}

/// `SizedHolding`.
#[derive(Debug, Clone, PartialEq)]
pub struct SizedHolding {
    /// `rank`.
    pub rank: i64,
    /// `symbol`.
    pub symbol: String,
    /// `weight`.
    pub weight: WideDecimal,
    /// `weight_pct_of_amount`.
    pub weight_pct_of_amount: WideDecimal,
    /// `amount`.
    pub amount: WideDecimal,
}

/// `SizedBasket`.
#[derive(Debug, Clone, PartialEq)]
pub struct SizedBasket {
    /// `holdings`.
    pub holdings: Vec<SizedHolding>,
    /// `amount`.
    pub amount: WideDecimal,
    /// `deployed`.
    pub deployed: WideDecimal,
    /// `cash`.
    pub cash: WideDecimal,
    /// `cash_pct`.
    pub cash_pct: WideDecimal,
    /// `profile`.
    pub profile: Option<HoldingProfile>,
    /// `holdings_overridden`.
    pub holdings_overridden: bool,
    /// `minimum`.
    pub minimum: Option<WideDecimal>,
    /// `method`.
    pub method: WeightMethod,
}

impl SizedBasket {
    /// `is_fundable`.
    #[must_use]
    pub fn is_fundable(&self) -> bool {
        self.minimum.as_ref().is_none_or(|m| &self.amount >= m)
    }
}

/// `suggested_holdings(profile)`.
#[must_use]
pub fn suggested_holdings(profile: Option<HoldingProfile>) -> i64 {
    profile.unwrap_or(DEFAULT_PROFILE).suggested()
}

/// `sleeves.cap_for_tier(tier)` with its default table.
fn cap_for_tier(tier: &str) -> Option<WideDecimal> {
    match tier.to_uppercase().as_str() {
        "R1" => Some(d(100)),
        "R2" => Some(d(70)),
        "R3" => Some(d(40)),
        "R4" => Some(d(0)),
        _ => None,
    }
}

/// `cash_pct_for_tier(tier)`.
#[must_use]
pub fn cash_pct_for_tier(tier: Option<&str>) -> WideDecimal {
    let cap = match tier {
        Some(t) if !t.is_empty() => cap_for_tier(t),
        _ => None,
    };
    let Some(cap) = cap else {
        return min_cash_buffer_pct();
    };
    let withheld = d(100).sub(&cap);
    let floor = min_cash_buffer_pct();
    let at_least = if withheld > floor { withheld } else { floor };
    if d(95) < at_least { d(95) } else { at_least }
}

/// `resolve_holdings(available=, profile=, requested=)`.
///
/// # Errors
/// Python's `ValueError` message.
pub fn resolve_holdings(
    available: i64,
    profile: Option<HoldingProfile>,
    requested: Option<i64>,
) -> Result<i64, String> {
    if available < MIN_HOLDINGS {
        return Err(format!(
            "a basket needs at least {MIN_HOLDINGS} names; this screen returned {available}"
        ));
    }
    let Some(requested) = requested else {
        return Ok(suggested_holdings(profile).min(available));
    };
    if !(MIN_HOLDINGS..=MAX_HOLDINGS).contains(&requested) {
        return Err(format!(
            "a basket holds between {MIN_HOLDINGS} and {MAX_HOLDINGS} names; got {requested}"
        ));
    }
    if requested > available {
        return Err(format!(
            "you asked for {requested} names and this screen returned {available}. Widen the \
             screen or lower the count."
        ));
    }
    Ok(requested)
}

fn top(candidates: &[Candidate], holdings: i64) -> Result<Vec<Candidate>, String> {
    let mut sorted: Vec<&Candidate> = candidates.iter().collect();
    sorted.sort_by_key(|c| c.rank);
    let mut seen = BTreeSet::new();
    let mut chosen = Vec::new();
    let want = usize::try_from(holdings).unwrap_or(0);
    for c in sorted {
        let symbol = c.symbol.trim().to_uppercase();
        if symbol.is_empty() || !seen.insert(symbol.clone()) {
            continue;
        }
        chosen.push(Candidate {
            symbol,
            ..c.clone()
        });
        if chosen.len() == want {
            break;
        }
    }
    if chosen.len() < want {
        return Err(format!(
            "only {} unique names available after de-duplication; need {holdings}",
            chosen.len()
        ));
    }
    Ok(chosen)
}

fn err(e: crate::decimal::DecimalError) -> String {
    e.to_string()
}

fn normalize(raw: &[WideDecimal]) -> Result<Vec<WideDecimal>, String> {
    if raw.is_empty() {
        return Err("a basket needs at least one weight".to_owned());
    }
    if raw.iter().any(|v| v.is_sign_negative() && !v.is_zero()) {
        return Err("weights cannot be negative".to_owned());
    }
    let total = WideDecimal::sum(raw.iter());
    if total.is_zero() || total.is_sign_negative() {
        return Err("weights must include at least one positive value".to_owned());
    }
    let mut weights = Vec::with_capacity(raw.len());
    for v in raw {
        weights.push(
            v.div(&total)
                .and_then(|w| w.quantize(-4, Rounding::HalfUp))
                .map_err(err)?,
        );
    }
    let head = WideDecimal::sum(weights[..weights.len() - 1].iter());
    let one: WideDecimal = "1.0000".parse().map_err(err)?;
    if let Some(last) = weights.last_mut() {
        *last = one.sub(&head);
    }
    assert_weights_sum_to_one(&weights)?;
    Ok(weights)
}

/// `curated_baskets.assert_weights_sum_to_one`.
fn assert_weights_sum_to_one(weights: &[WideDecimal]) -> Result<(), String> {
    if weights.is_empty() {
        return Err("constituent weights cannot be empty".to_owned());
    }
    let mut q = Vec::with_capacity(weights.len());
    for w in weights {
        q.push(w.quantize(-4, Rounding::HalfEven).map_err(err)?);
    }
    let total = WideDecimal::sum(q.iter());
    let target: WideDecimal = "1.0000".parse().map_err(err)?;
    let tolerance: WideDecimal = "0.0001".parse().map_err(err)?;
    let delta = total.sub(&target).abs();
    if delta > tolerance {
        return Err(format!(
            "constituent weights must sum to {}, got {} (delta {} exceeds tolerance {})",
            target.to_sci_string(),
            total.to_sci_string(),
            delta.to_sci_string(),
            tolerance.to_sci_string()
        ));
    }
    Ok(())
}

fn custom_raw(
    chosen: &[Candidate],
    custom: Option<&[(String, WideDecimal)]>,
) -> Result<Vec<WideDecimal>, String> {
    let Some(custom) = custom else {
        return Err("custom weighting needs a weight for every chosen name".to_owned());
    };
    let get = |s: &str| custom.iter().rev().find(|(k, _)| k == s).map(|(_, v)| v);
    let mut raw = Vec::new();
    let mut missing = Vec::new();
    for c in chosen {
        match get(&c.symbol) {
            Some(v) if !(v.is_zero() || v.is_sign_negative()) => raw.push(v.clone()),
            _ => missing.push(c.symbol.clone()),
        }
    }
    if !missing.is_empty() {
        return Err(format!(
            "you did not give a positive weight to {}. Every name the screen selected needs one.",
            missing.join(", ")
        ));
    }
    let chosen_set: BTreeSet<&str> = chosen.iter().map(|c| c.symbol.as_str()).collect();
    let extras: BTreeSet<&str> = custom
        .iter()
        .map(|(k, _)| k.as_str())
        .filter(|k| !chosen_set.contains(k))
        .collect();
    if !extras.is_empty() {
        return Err(format!(
            "weights for names this screen did not select: {}. Custom weighting cannot add a \
             holding.",
            extras.into_iter().collect::<Vec<_>>().join(", ")
        ));
    }
    Ok(raw)
}

fn score_raw(chosen: &[Candidate]) -> Result<Vec<WideDecimal>, String> {
    let raw: Vec<WideDecimal> = chosen
        .iter()
        .map(|c| match &c.score {
            Some(s) if !(s.is_zero() || s.is_sign_negative()) => s.clone(),
            _ => WideDecimal::zero(),
        })
        .collect();
    if raw.iter().all(WideDecimal::is_zero) {
        return Err(
            "score weighting needs a positive screen score on at least one name. Use \
                    equal weight, or pick a screen that ranks on a number."
                .to_owned(),
        );
    }
    Ok(raw)
}

fn inv_vol_raw(chosen: &[Candidate]) -> Result<Vec<WideDecimal>, String> {
    let mut raw = Vec::new();
    for c in chosen {
        match &c.vol {
            Some(v) if !(v.is_zero() || v.is_sign_negative()) => {
                raw.push(d(1).div(v).map_err(err)?);
            }
            _ => {
                return Err(format!(
                    "{} has no 1-year volatility, so inverse-vol weighting cannot size it. Use \
                     equal weight or a screen that projects vol_12m.",
                    c.symbol
                ));
            }
        }
    }
    Ok(raw)
}

/// `scheme_weights(chosen, method, custom=)`.
///
/// # Errors
/// Python's `ValueError` message.
pub fn scheme_weights(
    chosen: &[Candidate],
    method: WeightMethod,
    custom: Option<&[(String, WideDecimal)]>,
) -> Result<Vec<WideDecimal>, String> {
    if chosen.is_empty() {
        return Err("a basket needs at least one name".to_owned());
    }
    match method {
        WeightMethod::Custom => normalize(&custom_raw(chosen, custom)?),
        WeightMethod::Equal => normalize(&vec![d(1); chosen.len()]),
        WeightMethod::Rank => {
            let n = i64::try_from(chosen.len()).unwrap_or(i64::MAX);
            let raw: Vec<WideDecimal> = (0..n).map(|p| d(n - p)).collect();
            normalize(&raw)
        }
        WeightMethod::Score => normalize(&score_raw(chosen)?),
        WeightMethod::InvVol => normalize(&inv_vol_raw(chosen)?),
    }
}

/// `minimum_amount(candidates, holdings=, cash_pct=, weights=)`.
///
/// # Errors
/// Python's `ValueError` message.
pub fn minimum_amount(
    candidates: &[Candidate],
    holdings: i64,
    cash_pct: &WideDecimal,
    weights: Option<&[WideDecimal]>,
) -> Result<Option<WideDecimal>, String> {
    let chosen = top(candidates, holdings)?;
    if weights.is_some_and(|w| w.len() != chosen.len()) {
        return Err("weights and chosen names must be the same length".to_owned());
    }
    let deployable = d(100).sub(cash_pct).div(&d(100)).map_err(err)?;
    if deployable.is_zero() || deployable.is_sign_negative() {
        return Ok(None);
    }
    let mut needed: Option<WideDecimal> = None;
    for (index, c) in chosen.iter().enumerate() {
        let Some(price) = &c.price else { continue };
        if price.is_zero() || price.is_sign_negative() {
            continue;
        }
        let share = match weights {
            None => d(1).div(&WideDecimal::from(chosen.len())).map_err(err)?,
            Some(w) => w[index].clone(),
        };
        if share.is_zero() || share.is_sign_negative() {
            continue;
        }
        let bound = price
            .div(&share)
            .and_then(|b| b.div(&deployable))
            .map_err(err)?;
        needed = Some(match needed {
            Some(n) if n >= bound => n,
            _ => bound,
        });
    }
    let Some(needed) = needed else {
        return Ok(None);
    };
    let step = d(100);
    let mut steps = needed
        .div(&step)
        .and_then(|s| s.quantize(0, Rounding::HalfUp))
        .map_err(err)?;
    if steps.mul(&step) < needed {
        steps = steps.add(&d(1));
    }
    Ok(Some(steps.mul(&step)))
}

/// `size_basket(candidates, amount=, profile=, holdings=, cash_pct=, method=, custom_weights=)`.
///
/// # Errors
/// Python's `ValueError` message.
pub fn size_basket(
    candidates: &[Candidate],
    amount: &WideDecimal,
    profile: Option<HoldingProfile>,
    holdings: Option<i64>,
    cash_pct: Option<&WideDecimal>,
    method: Option<WeightMethod>,
    custom_weights: Option<&[(String, WideDecimal)]>,
) -> Result<SizedBasket, String> {
    if amount.is_zero() || amount.is_sign_negative() {
        return Err(format!(
            "an investment amount must be positive; got {}",
            amount.to_sci_string()
        ));
    }
    let cash_fraction = cash_pct.cloned().unwrap_or_else(min_cash_buffer_pct);
    if cash_fraction < d(0) || cash_fraction > d(95) {
        return Err(format!(
            "cash must be between 0% and 95%; got {}%",
            cash_fraction.to_sci_string()
        ));
    }
    let available: BTreeSet<String> = candidates
        .iter()
        .map(|c| c.symbol.trim().to_uppercase())
        .filter(|s| !s.is_empty())
        .collect();
    let count = resolve_holdings(
        i64::try_from(available.len()).unwrap_or(i64::MAX),
        profile,
        holdings,
    )?;
    let chosen = top(candidates, count)?;
    let resolved = method.unwrap_or(DEFAULT_METHOD);
    let normalised: Option<Vec<(String, WideDecimal)>> = custom_weights.map(|c| {
        c.iter()
            .map(|(k, v)| (k.trim().to_uppercase(), v.clone()))
            .collect()
    });
    let weights = scheme_weights(&chosen, resolved, normalised.as_deref())?;
    let whole = amount.quantize(0, Rounding::Down).map_err(err)?;
    let budget = whole
        .mul(&d(100).sub(&cash_fraction))
        .div(&d(100))
        .and_then(|b| b.quantize(0, Rounding::Down))
        .map_err(err)?;
    let amounts: Vec<WideDecimal> = if resolved == WeightMethod::Equal {
        let per = budget
            .div(&d(count))
            .and_then(|p| p.quantize(0, Rounding::Down))
            .map_err(err)?;
        vec![per; chosen.len()]
    } else {
        let mut out = Vec::with_capacity(weights.len());
        for w in &weights {
            out.push(budget.mul(w).quantize(0, Rounding::Down).map_err(err)?);
        }
        out
    };
    let mut rows = Vec::with_capacity(chosen.len());
    for ((c, w), a) in chosen.iter().zip(&weights).zip(&amounts) {
        let pct = if whole.is_zero() {
            WideDecimal::zero()
        } else {
            a.mul(&d(100))
                .div(&whole)
                .and_then(|p| p.quantize(-2, Rounding::HalfUp))
                .map_err(err)?
        };
        rows.push(SizedHolding {
            rank: c.rank,
            symbol: c.symbol.clone(),
            weight: w.clone(),
            weight_pct_of_amount: pct,
            amount: a.clone(),
        });
    }
    let deployed = WideDecimal::sum(amounts.iter());
    let minimum = minimum_amount(&chosen, count, &cash_fraction, Some(&weights))?;
    Ok(SizedBasket {
        holdings: rows,
        cash: whole.sub(&deployed),
        amount: whole,
        deployed,
        cash_pct: cash_fraction,
        profile: if holdings.is_some() {
            None
        } else {
            Some(profile.unwrap_or(DEFAULT_PROFILE))
        },
        holdings_overridden: holdings.is_some(),
        minimum,
        method: resolved,
    })
}
