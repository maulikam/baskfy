//! `baskfy_core.curated_metrics` — the catalog card arithmetic `compute_all_metrics` uses, ported
//! digit for digit over [`WideDecimal`] (Python's `Decimal` in its default context).
//!
//! Pure: prices, weights and returns in, decimals out. No database, no clock. Kept in the worker
//! lane (not `baskfy-core`) because the curated lane owns only its own files; it is a private
//! submodule of `tasks::curated` (DECISIONS-RS R8.curated).
//!
//! Two places where Python's `decimal` is **not** a plain correctly-rounded operation, both
//! reproduced, not corrected:
//!
//! * `(r - mean) ** 2` — CPython's `libmpdec` raises to an integer power in a work context of
//!   `prec + digits(2) + exp(2) + 2 = 31` digits and then rounds to 28: a **double rounding**
//!   that differs from `x * x` in about one operand in two thousand ([`pow2`]).
//! * `cagr` leaves `Decimal` for `float` (`math.exp(math.log(float(ratio)) / float(years))`) and
//!   re-enters through `str(float)`; the same libm answers both sides on one machine.

use std::collections::{BTreeMap, HashMap};

use baskfy_core::decimal::{DecimalError, Rounding, WideDecimal};
use baskfy_core::factors::resolve_window;
use chrono::{Datelike, NaiveDate};

/// A decimal in Python's semantics.
pub type Dec = WideDecimal;

/// `{date: {symbol: close}}`, dates ascending.
pub type History = BTreeMap<NaiveDate, HashMap<String, Dec>>;

/// A weight map in the order Python's `dict` holds it (`(symbol, weight)`, first position kept).
pub type Weights = Vec<(String, Dec)>;

/// What the arithmetic raises: Python's class name and `str(exc)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MathError {
    /// `type(exc).__name__`.
    pub type_name: &'static str,
    /// `str(exc)`.
    pub message: String,
}

impl MathError {
    fn value(message: impl Into<String>) -> Self {
        Self {
            type_name: "ValueError",
            message: message.into(),
        }
    }
}

impl From<DecimalError> for MathError {
    fn from(e: DecimalError) -> Self {
        let type_name = if e.0.contains("DivisionByZero") {
            "DivisionByZero"
        } else {
            "InvalidOperation"
        };
        Self {
            type_name,
            message: e.0,
        }
    }
}

/// The arithmetic's result.
pub type MathResult<T> = Result<T, MathError>;

/// `VolatilityBasis`.
pub const BASIS_252D: &str = "BASKET_252D";
/// `VolatilityBasis`.
pub const BASIS_FULL_HISTORY: &str = "BASKET_FULL_HISTORY";
/// `VolatilityBasis`.
pub const BASIS_CONSTITUENT_WEIGHTED: &str = "CONSTITUENT_WEIGHTED";

/// `TERCILE_SWITCH_PUBLISHED_COUNT`.
pub const TERCILE_SWITCH_PUBLISHED_COUNT: i64 = 12;
/// `MIN_VOL_OBSERVATIONS`.
const MIN_VOL_OBSERVATIONS: usize = 2;
/// `VOL_WINDOW_BARS`.
pub const VOL_WINDOW_BARS: usize = 252;
/// `MIN_BASKET_VOL_DAYS`.
pub const MIN_BASKET_VOL_DAYS: usize = 60;
/// `PCT_2DP`'s exponent.
const PCT_2DP: i64 = -2;
/// `Decimal("0.0000000001")`'s exponent.
const VOL_10DP: i64 = -10;

/// `CARD_WINDOW_MONTHS`.
pub const MONTHS_1M: i64 = 1;
/// `CARD_WINDOW_MONTHS["6m"]`.
pub const MONTHS_6M: i64 = 6;
/// `CARD_WINDOW_MONTHS["1y"]`.
pub const MONTHS_1Y: i64 = 12;
/// `CARD_WINDOW_MONTHS["3y"]`.
pub const MONTHS_3Y: i64 = 36;
/// `CARD_WINDOW_MONTHS["5y"]`.
pub const MONTHS_5Y: i64 = 60;

/// `RETURN_CONVENTION`.
pub const RETURN_CONVENTION: &str = "PRICE_RETURN";
/// `DIVIDENDS_INCLUDED`.
pub const DIVIDENDS_INCLUDED: bool = false;
/// `RETURN_CONVENTION_NOTE` (with `ESTIMATED_DIVIDEND_DRAG_PCT_PER_YEAR = Decimal("1.2")`).
pub const RETURN_CONVENTION_NOTE: &str = "Returns are price returns computed from split- and \
     bonus-adjusted closes. Cash dividends are excluded (docs/DECISIONS-MERGE.md M39.3), so a \
     total-return series would be higher by roughly the basket's dividend yield each year - \
     about 1.2% for the broad Indian market - compounding.";

/// `Decimal(text)` for a literal this module spells (always valid).
fn lit(text: &str) -> Dec {
    text.parse().unwrap_or_default()
}

fn one() -> Dec {
    Dec::from(1_i64)
}

fn zero() -> Dec {
    Dec::from(0_i64)
}

/// `x` rounded to `prec` significant digits, half-even (`Context(prec).plus(x)` / `_fix`).
fn round_sig(x: &Dec, prec: usize) -> Dec {
    let len = x.coefficient_len();
    if x.is_zero() || len <= prec {
        return x.clone();
    }
    let drop = i64::try_from(len - prec).unwrap_or(0);
    let mut out = x.rescale_exact(x.exponent() + drop, Rounding::HalfEven);
    if out.coefficient_len() > prec {
        // A carry made it 10**prec: one more (exact) digit off, as `_fix` does.
        out = out.rescale_exact(out.exponent() + 1, Rounding::HalfEven);
    }
    out
}

/// `x ** 2` as CPython's `libmpdec` computes it: the product rounded to 31 digits, then to 28.
#[must_use]
pub fn pow2(x: &Dec) -> Dec {
    round_sig(&round_sig(&x.mul_exact(x), 31), 28)
}

/// `money(value)`: quantise to paise, half-up.
///
/// # Errors
/// `InvalidOperation` when the result needs more than 28 digits.
pub fn money(value: &Dec) -> MathResult<Dec> {
    Ok(value.quantize(-2, Rounding::HalfUp)?)
}

/// `value.to_integral_value(rounding=ROUND_CEILING)`.
fn ceiling(value: &Dec) -> Dec {
    if value.exponent() >= 0 {
        return value.clone();
    }
    let down = value.rescale_exact(0, Rounding::Down);
    if down == *value || value.is_sign_negative() {
        down
    } else {
        down.add(&one())
    }
}

/// `min_amount(prices, weights)` with `lot_size = 1`.
///
/// # Errors
/// Python's `ValueError`s (empty input, a non-positive price or weight).
pub fn min_amount(prices: &[Dec], weights: &[Dec]) -> MathResult<Dec> {
    if prices.len() != weights.len() {
        return Err(MathError::value(
            "prices and weights must have the same length",
        ));
    }
    if prices.is_empty() {
        return Err(MathError::value("prices cannot be empty"));
    }
    let lot = Dec::from(1_i64);
    let mut worst = lit("0");
    for (price, weight) in prices.iter().zip(weights) {
        if *price <= zero() {
            return Err(MathError::value(format!(
                "price must be positive, got {}",
                price.to_sci_string()
            )));
        }
        if *weight <= zero() {
            return Err(MathError::value(format!(
                "weight must be positive, got {}",
                weight.to_sci_string()
            )));
        }
        let bound = price.mul(&lot).div(weight)?;
        // `max(worst, bound)`: the first argument wins a tie.
        if bound > worst {
            worst = bound;
        }
    }
    Ok(ceiling(&worst))
}

/// `volatility_bucket_fixed`.
///
/// # Errors
/// `ValueError` for a negative vol.
pub fn volatility_bucket_fixed(vol: &Dec) -> MathResult<&'static str> {
    if *vol < zero() {
        return Err(MathError::value("annualized_vol cannot be negative"));
    }
    if *vol < lit("0.15") {
        return Ok("LOW");
    }
    if *vol < lit("0.25") {
        return Ok("MED");
    }
    Ok("HIGH")
}

/// `volatility_bucket_for_catalog`.
///
/// # Errors
/// `ValueError` for a negative vol.
pub fn volatility_bucket_for_catalog(
    vol: &Dec,
    published_count: i64,
    peer_vols: Option<&[Dec]>,
) -> MathResult<&'static str> {
    let peers = match peer_vols {
        Some(p) if published_count >= TERCILE_SWITCH_PUBLISHED_COUNT && !p.is_empty() => p,
        _ => return volatility_bucket_fixed(vol),
    };
    if *vol < zero() {
        return Err(MathError::value("annualized_vol cannot be negative"));
    }
    let mut ordered: Vec<Dec> = peers.to_vec();
    ordered.sort();
    let n = ordered.len();
    let low_end = n.min((n / 3).max(1));
    let med_end = n.min((low_end + 1).max((2 * n) / 3));
    let low_cut = &ordered[low_end - 1];
    let med_cut = &ordered[med_end - 1];
    if vol < low_cut {
        return Ok("LOW");
    }
    if vol < med_cut {
        return Ok("MED");
    }
    Ok("HIGH")
}

/// `annualized_volatility(daily_returns)`.
///
/// # Errors
/// A decimal operation Python would raise on.
pub fn annualized_volatility(returns: &[Dec]) -> MathResult<Option<Dec>> {
    if returns.len() < MIN_VOL_OBSERVATIONS {
        return Ok(None);
    }
    let n = Dec::from(returns.len());
    let mean = Dec::sum(returns).div(&n)?;
    let squares: Vec<Dec> = returns.iter().map(|r| pow2(&r.sub(&mean))).collect();
    let variance = Dec::sum(&squares).div(&n.sub(&lit("1")))?;
    if variance < zero() {
        return Ok(None);
    }
    let daily_std = variance.sqrt()?;
    let annualized = daily_std.mul(&lit("252").sqrt()?);
    Ok(Some(annualized.quantize(VOL_10DP, Rounding::HalfUp)?))
}

/// `absolute_return(nav_start, nav_end)`.
///
/// # Errors
/// `ValueError` for a non-positive start.
pub fn absolute_return(nav_start: &Dec, nav_end: &Dec) -> MathResult<Dec> {
    if *nav_start <= zero() {
        return Err(MathError::value("nav_start must be positive"));
    }
    let raw = nav_end.div(nav_start)?.sub(&lit("1"));
    Ok(raw.mul(&lit("100")).quantize(PCT_2DP, Rounding::HalfUp)?)
}

/// `float(d)`: the shortest-round-trip parse of `str(d)`.
fn to_float(d: &Dec) -> MathResult<f64> {
    d.to_sci_string()
        .parse::<f64>()
        .map_err(|e| MathError::value(e.to_string()))
}

/// `cagr(nav_start=, nav_end=, years=)`.
///
/// # Errors
/// `ValueError` for non-positive NAVs.
pub fn cagr(nav_start: &Dec, nav_end: &Dec, years: &Dec) -> MathResult<Option<Dec>> {
    if *nav_start <= zero() || *nav_end <= zero() {
        return Err(MathError::value("nav values must be positive"));
    }
    if *years <= zero() {
        return Ok(None);
    }
    let ratio = nav_end.div(nav_start)?;
    let grown = (to_float(&ratio)?.ln() / to_float(years)?).exp();
    if !grown.is_finite() {
        return Err(MathError {
            type_name: "InvalidOperation",
            message: "[<class 'decimal.ConversionSyntax'>]".to_owned(),
        });
    }
    let rate: Dec = format!("{grown:?}")
        .parse::<Dec>()
        .map_err(MathError::from)?
        .sub(&lit("1"));
    Ok(Some(
        rate.mul(&lit("100")).quantize(PCT_2DP, Rounding::HalfUp)?,
    ))
}

/// One version: the weights it held from `effective_date` on.
#[derive(Debug, Clone)]
pub struct BasketVersion {
    /// `effective_date`.
    pub effective_date: NaiveDate,
    /// `weights`, in the dict's order.
    pub weights: Weights,
}

/// `BasketNav`: `points` (date, NAV) from base 100 and the daily returns between them.
#[derive(Debug, Clone, Default)]
pub struct BasketNav {
    /// `points`.
    pub points: Vec<(NaiveDate, Dec)>,
    /// `daily_returns`.
    pub daily_returns: Vec<(NaiveDate, Dec)>,
}

impl BasketNav {
    /// `returns_only`.
    #[must_use]
    pub fn returns_only(&self) -> Vec<Dec> {
        self.daily_returns.iter().map(|(_, r)| r.clone()).collect()
    }
}

/// `basket_day_return(weights, prices_prev, prices_today)`.
///
/// # Errors
/// A decimal operation Python would raise on.
pub fn basket_day_return(
    weights: &Weights,
    prev: &HashMap<String, Dec>,
    today: &HashMap<String, Dec>,
) -> MathResult<Dec> {
    let mut total_w = lit("0");
    let mut contrib = lit("0");
    for (symbol, weight) in weights {
        let (Some(p), Some(t)) = (prev.get(symbol), today.get(symbol)) else {
            continue;
        };
        if *p <= zero() {
            continue;
        }
        total_w = total_w.add(weight);
        contrib = contrib.add(&weight.mul(&t.div(p)?.sub(&lit("1"))));
    }
    if total_w <= zero() {
        return Ok(lit("0"));
    }
    Ok(contrib.div(&total_w)?)
}

/// `bisect.bisect_right`.
fn bisect_right<T: Ord>(items: &[T], x: &T) -> usize {
    items.partition_point(|v| v <= x)
}

/// `bisect.bisect_left`.
fn bisect_left<T: Ord>(items: &[T], x: &T) -> usize {
    items.partition_point(|v| v < x)
}

/// `version_aware_nav(prices_by_date, versions)` with `base = Decimal("100")`.
///
/// # Errors
/// A decimal operation Python would raise on.
pub fn version_aware_nav(history: &History, versions: &[BasketVersion]) -> MathResult<BasketNav> {
    if versions.is_empty() || history.is_empty() {
        return Ok(BasketNav::default());
    }
    let mut ordered: Vec<&BasketVersion> = versions.iter().collect();
    ordered.sort_by_key(|v| v.effective_date);
    let effective: Vec<NaiveDate> = ordered.iter().map(|v| v.effective_date).collect();
    let inception = effective[0];
    let dates: Vec<NaiveDate> = history
        .keys()
        .copied()
        .filter(|d| *d >= inception)
        .collect();
    if dates.is_empty() {
        return Ok(BasketNav::default());
    }
    let empty = HashMap::new();
    let mut returns: Vec<(NaiveDate, Dec)> = Vec::with_capacity(dates.len());
    for pair in dates.windows(2) {
        let (prev_day, day) = (pair[0], pair[1]);
        let index = bisect_right(&effective, &day).saturating_sub(1);
        let weights = &ordered[index].weights;
        let r = basket_day_return(
            weights,
            history.get(&prev_day).unwrap_or(&empty),
            history.get(&day).unwrap_or(&empty),
        )?;
        returns.push((day, r));
    }
    let base = lit("100");
    let mut points = Vec::with_capacity(returns.len() + 1);
    points.push((dates[0], base.clone()));
    let mut nav = base;
    for (day, r) in &returns {
        nav = nav.mul(&lit("1").add(r));
        points.push((*day, nav.clone()));
    }
    Ok(BasketNav {
        points,
        daily_returns: returns,
    })
}

/// `WindowSlice`.
#[derive(Debug, Clone)]
pub struct WindowSlice {
    /// `start_date`: the bar before the window.
    pub start_date: NaiveDate,
    /// `end_date`.
    pub end_date: NaiveDate,
    /// `nav_start`.
    pub nav_start: Dec,
    /// `nav_end`.
    pub nav_end: Dec,
}

impl WindowSlice {
    /// `years`: `Decimal(days) / Decimal("365.25")`.
    ///
    /// # Errors
    /// Never in practice (the divisor is a constant).
    pub fn years(&self) -> MathResult<Dec> {
        let days = (self.end_date - self.start_date).num_days();
        Ok(Dec::from(days).div(&lit("365.25"))?)
    }
}

/// `resolve_nav_window(points, months=)`.
///
/// # Errors
/// `resolve_window`'s `ValueError`.
pub fn resolve_nav_window(
    points: &[(NaiveDate, Dec)],
    months: i64,
) -> MathResult<Option<WindowSlice>> {
    if points.len() < MIN_VOL_OBSERVATIONS {
        return Ok(None);
    }
    let dates: Vec<NaiveDate> = points.iter().map(|(d, _)| *d).collect();
    let Some(last) = dates.last().copied() else {
        return Ok(None);
    };
    let window =
        resolve_window(last, months, &dates).map_err(|e| MathError::value(e.to_string()))?;
    if !window.spans_full_window() {
        return Ok(None);
    }
    let start_index = bisect_left(&dates, &window.start);
    let Some(base_index) = start_index.checked_sub(1) else {
        return Ok(None);
    };
    let (Some(base), Some(end)) = (points.get(base_index), points.last()) else {
        return Ok(None);
    };
    Ok(Some(WindowSlice {
        start_date: base.0,
        end_date: end.0,
        nav_start: base.1.clone(),
        nav_end: end.1.clone(),
    }))
}

/// `window_return(points, months=)`.
///
/// # Errors
/// As [`resolve_nav_window`] and [`absolute_return`].
pub fn window_return(points: &[(NaiveDate, Dec)], months: i64) -> MathResult<Option<Dec>> {
    match resolve_nav_window(points, months)? {
        None => Ok(None),
        Some(w) => absolute_return(&w.nav_start, &w.nav_end).map(Some),
    }
}

/// `window_cagr(points, months=)`.
///
/// # Errors
/// As [`resolve_nav_window`] and [`cagr`].
pub fn window_cagr(points: &[(NaiveDate, Dec)], months: i64) -> MathResult<Option<Dec>> {
    match resolve_nav_window(points, months)? {
        None => Ok(None),
        Some(w) => cagr(&w.nav_start, &w.nav_end, &w.years()?),
    }
}

/// `since_inception_anchor(points, launched_at=)`.
#[must_use]
pub fn since_inception_anchor(
    points: &[(NaiveDate, Dec)],
    launched_at: Option<NaiveDate>,
) -> Option<NaiveDate> {
    if points.len() < MIN_VOL_OBSERVATIONS {
        return None;
    }
    let dates: Vec<NaiveDate> = points.iter().map(|(d, _)| *d).collect();
    let index = launched_at.map_or(0, |l| bisect_left(&dates, &l));
    if index >= points.len() - 1 {
        return None;
    }
    Some(dates[index])
}

/// `since_inception_return(points, launched_at=)`.
///
/// # Errors
/// As [`absolute_return`].
pub fn since_inception_return(
    points: &[(NaiveDate, Dec)],
    launched_at: Option<NaiveDate>,
) -> MathResult<Option<Dec>> {
    let Some(anchor) = since_inception_anchor(points, launched_at) else {
        return Ok(None);
    };
    let dates: Vec<NaiveDate> = points.iter().map(|(d, _)| *d).collect();
    let index = bisect_left(&dates, &anchor);
    let (Some(start), Some(end)) = (points.get(index), points.last()) else {
        return Ok(None);
    };
    absolute_return(&start.1, &end.1).map(Some)
}

/// `daily_returns_for_symbol(prices_by_date, symbol)`.
///
/// # Errors
/// A decimal operation Python would raise on.
pub fn daily_returns_for_symbol(history: &History, symbol: &str) -> MathResult<Vec<Dec>> {
    let series: Vec<&Dec> = history.values().filter_map(|row| row.get(symbol)).collect();
    let mut out = Vec::with_capacity(series.len());
    for pair in series.windows(2) {
        let (prev, today) = (pair[0], pair[1]);
        if *prev <= zero() {
            continue;
        }
        out.push(today.div(prev)?.sub(&lit("1")));
    }
    Ok(out)
}

/// `constituent_volatilities(prices_by_date, symbols)` with the default window and floor.
///
/// # Errors
/// A decimal operation Python would raise on.
pub fn constituent_volatilities(
    history: &History,
    symbols: &[String],
) -> MathResult<HashMap<String, Dec>> {
    let mut out = HashMap::new();
    for symbol in symbols {
        let returns = daily_returns_for_symbol(history, symbol)?;
        if returns.len() < MIN_BASKET_VOL_DAYS {
            continue;
        }
        let tail = &returns[returns.len().saturating_sub(VOL_WINDOW_BARS)..];
        if let Some(value) = annualized_volatility(tail)? {
            out.insert(symbol.clone(), value);
        }
    }
    Ok(out)
}

/// `constituent_weighted_volatility(weights, constituent_vols)`.
///
/// # Errors
/// A decimal operation Python would raise on.
pub fn constituent_weighted_volatility(
    weights: &Weights,
    vols: &HashMap<String, Dec>,
) -> MathResult<Option<Dec>> {
    let mut total_w = lit("0");
    let mut contrib = lit("0");
    for (symbol, weight) in weights {
        let Some(value) = vols.get(symbol) else {
            continue;
        };
        if *weight <= zero() {
            continue;
        }
        total_w = total_w.add(weight);
        contrib = contrib.add(&weight.mul(value));
    }
    if total_w <= zero() {
        return Ok(None);
    }
    Ok(Some(
        contrib
            .div(&total_w)?
            .quantize(VOL_10DP, Rounding::HalfUp)?,
    ))
}

/// `CatalogVolatility`: a publishable value and its basis.
#[derive(Debug, Clone)]
pub struct CatalogVolatility {
    /// `value`.
    pub value: Dec,
    /// `basis`.
    pub basis: &'static str,
}

/// `catalog_volatility(basket_daily_returns=, weights=, constituent_vols=)`.
///
/// # Errors
/// A decimal operation Python would raise on.
pub fn catalog_volatility(
    returns: &[Dec],
    weights: Option<&Weights>,
    constituent_vols: Option<&HashMap<String, Dec>>,
) -> MathResult<Option<CatalogVolatility>> {
    let count = returns.len();
    if count >= VOL_WINDOW_BARS {
        let value = annualized_volatility(&returns[count - VOL_WINDOW_BARS..])?;
        return Ok(value.map(|value| CatalogVolatility {
            value,
            basis: BASIS_252D,
        }));
    }
    if count >= MIN_BASKET_VOL_DAYS {
        let value = annualized_volatility(returns)?;
        return Ok(value.map(|value| CatalogVolatility {
            value,
            basis: BASIS_FULL_HISTORY,
        }));
    }
    let (Some(weights), Some(vols)) = (weights, constituent_vols) else {
        return Ok(None);
    };
    Ok(
        constituent_weighted_volatility(weights, vols)?.map(|value| CatalogVolatility {
            value,
            basis: BASIS_CONSTITUENT_WEIGHTED,
        }),
    )
}

/// `whole_months_between(start, end)`.
#[must_use]
pub fn whole_months_between(start: NaiveDate, end: NaiveDate) -> i64 {
    if end < start {
        return 0;
    }
    let mut months = (i64::from(end.year()) - i64::from(start.year())) * 12
        + (i64::from(end.month()) - i64::from(start.month()));
    if end.day() < start.day() {
        months -= 1;
    }
    months.max(0)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    fn d(s: &str) -> Dec {
        s.parse().unwrap()
    }

    fn day(y: i32, m: u32, dd: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, dd).unwrap()
    }

    // Values printed by CPython 3.12's `decimal` (libmpdec).
    #[test]
    fn sqrt_matches_python() {
        assert_eq!(
            d("252").sqrt().unwrap().to_sci_string(),
            "15.87450786638754354300969452"
        );
        assert_eq!(d("0E-20").sqrt().unwrap().to_sci_string(), "0E-10");
        assert_eq!(d("0.0004").sqrt().unwrap().to_sci_string(), "0.02");
        assert_eq!(d("4E-6").sqrt().unwrap().to_sci_string(), "0.002");
        assert_eq!(d("1.00").sqrt().unwrap().to_sci_string(), "1.0");
    }

    /// `(str(x), str(x.sqrt()), str(x ** 2))` from CPython 3.12 (libmpdec), random operands.
    const VECTORS: &[(&str, &str, &str)] = &[
        (
            "0.000021250919909",
            "0.004609872005706882964150792781",
            "4.51601596978732568281E-10",
        ),
        (
            "960566905443639.394257",
            "30993013.81672391334167880638",
            "9.226887798335696649518584413E+29",
        ),
        ("5.992", "2.447856204927078636343894734", "35.904064"),
        ("6.5E-26", "2.549509756796392415014112055E-13", "4.225E-51"),
        ("1.2E-12", "0.000001095445115010332226913939566", "1.44E-24"),
        (
            "3.3870412123024E-22",
            "1.840391592108157833832437407E-11",
            "1.147204817383491146910904576E-43",
        ),
        (
            "6814977262231.8119",
            "2610551.141470285753114620042",
            "46443915084736602299175772.36",
        ),
        ("3658", "60.48140210014976165706298640", "13380964"),
        (
            "7282237657165537.5090",
            "85336027.89657799340217644388",
            "5.303098529543981661239895549E+31",
        ),
        (
            "9.14761360679426581E-9",
            "0.00009564315765800638710110538559",
            "8.367883469920759669730407990E-17",
        ),
        ("7.2E-31", "8.485281374238570292810132345E-16", "5.184E-61"),
        (
            "1800.188483",
            "42.42862810650375011721980747",
            "3240678.574325841289",
        ),
        (
            "9.354E-18",
            "3.058430970285253961432163724E-9",
            "8.7497316E-35",
        ),
        (
            "7.86295579237787696E-12",
            "0.000002804096252338331727117463378",
            "6.182607379288880692673349564E-23",
        ),
        ("0.9529", "0.9761659694949419819177451957", "0.90801841"),
        (
            "1175499866068982.37638",
            "34285563.52269833082756224464",
            "1.381799935128195504386861698E+30",
        ),
        (
            "901242186544197212165.6",
            "30020695970.35014130396053908",
            "8.122374788069655665106128817E+41",
        ),
        (
            "8.328454E+9",
            "91260.36379502330254777722288",
            "6.9363146030116E+19",
        ),
        (
            "0.00896083772107567231",
            "0.09466170144823973271450690505",
            "0.00008029661266345264842077213732",
        ),
        (
            "4.07104716530737E-7",
            "0.0006380475817137284629017280776",
            "1.657342502215717275930657632E-13",
        ),
        (
            "2.4127885E+11",
            "491201.4352584894473416829550",
            "5.82154834573225E+22",
        ),
        (
            "957695.0468680998536945750",
            "978.6189487579421756301915883",
            "917179802795.6919753868359051",
        ),
        (
            "7.36177974007811463892027E-13",
            "8.580081433225511431935138173E-7",
            "5.419580094142459313240742022E-25",
        ),
        (
            "8.388E-11",
            "0.000009158602513484248815153569220",
            "7.0358544E-21",
        ),
        (
            "7.93920E-14",
            "2.817658602457011646826747174E-7",
            "6.30308966400E-27",
        ),
        (
            "6.4090E-10",
            "0.00002531600284405103316822801262",
            "4.107528100E-19",
        ),
        ("8.6E-35", "9.273618495495703752516416074E-18", "7.396E-69"),
        (
            "606877.2535276230070456408",
            "779.0232689256611717718245324",
            "368300000849.2308115493023062",
        ),
        (
            "75052319080847610563.196",
            "8663274154.778181239962548342",
            "5.632850599413362323289847837E+39",
        ),
        (
            "4177108296121539.1131887672",
            "64630552.34269268654668530285",
            "1.744823371752738769199333673E+31",
        ),
        (
            "9.169230842622302871835E+25",
            "9575610081150.079323041280183",
            "8.407479424529610633537657113E+51",
        ),
        (
            "7.825107366E-13",
            "8.845963693120156594847193117E-7",
            "6.1232305289427457956E-25",
        ),
        (
            "2538927700089796412456.3",
            "50387773319.42538232860306333",
            "6.446153866283263197899913615E+42",
        ),
        (
            "8.089E-34",
            "2.844116734594415342244294534E-17",
            "6.5431921E-67",
        ),
        (
            "4.822308E-26",
            "2.195975409698387310245206659E-13",
            "2.3254654446864E-51",
        ),
        (
            "472631369829172.138717060",
            "21740086.70242996383062159719",
            "2.233804117465996878045303785E+29",
        ),
        (
            "1.71E-10",
            "0.00001307669683062202065671094595",
            "2.9241E-20",
        ),
        (
            "4.890032609298E-20",
            "2.211341811954452237345459599E-10",
            "2.3912418919997806316052804E-39",
        ),
        (
            "6811.38828182492734056475313",
            "82.53113522680351051227467518",
            "46395010.32578193580207901590",
        ),
        (
            "5.4233177938408164613966E+25",
            "7364317886838.411364665462404",
            "2.941237589299042060118592914E+51",
        ),
    ];

    #[test]
    fn sqrt_and_square_match_cpython_vectors() {
        for (x, root, square) in VECTORS {
            let x = d(x);
            assert_eq!(x.sqrt().unwrap().to_sci_string(), *root, "sqrt {x}");
            assert_eq!(pow2(&x).to_sci_string(), *square, "pow2 {x}");
        }
    }

    #[test]
    fn pow2_double_rounds_like_libmpdec() {
        // Python: Decimal("0.1246378347412793291381659136") ** 2 differs from a * a.
        let a = d("0.1246378347412793291381659136");
        assert_eq!(pow2(&a).to_sci_string(), "0.01553458984899445649500321622");
        assert_eq!(a.mul(&a).to_sci_string(), "0.01553458984899445649500321621");
        let b = d("-360956193014687856.3167195173");
        assert_eq!(
            pow2(&b).to_sci_string(),
            "1.302893732756565943990438916E+35"
        );
    }

    // tests/test_curated_metrics.py (core): min_amount = ceil(max(price / weight)).
    #[test]
    fn min_amount_is_the_ceiling_of_the_worst_bound() {
        let prices = [d("100"), d("2500.50")];
        let weights = [d("0.5"), d("0.5")];
        assert_eq!(
            min_amount(&prices, &weights).unwrap().to_sci_string(),
            "5001"
        );
        assert_eq!(
            money(&min_amount(&prices, &weights).unwrap())
                .unwrap()
                .to_sci_string(),
            "5001.00"
        );
        let err = min_amount(&[d("0")], &[d("1")]).unwrap_err();
        assert_eq!(err.message, "price must be positive, got 0");
    }

    #[test]
    fn buckets_fixed_and_terciles() {
        assert_eq!(volatility_bucket_fixed(&d("0.1499")).unwrap(), "LOW");
        assert_eq!(volatility_bucket_fixed(&d("0.15")).unwrap(), "MED");
        assert_eq!(volatility_bucket_fixed(&d("0.25")).unwrap(), "HIGH");
        // Below the switch the fixed cutoffs apply whatever the peers.
        let peers = [d("0.01"), d("0.02"), d("0.03")];
        assert_eq!(
            volatility_bucket_for_catalog(&d("0.2"), 11, Some(&peers)).unwrap(),
            "MED"
        );
        // Terciles: a value on a cut takes the higher bucket; one peer does not index past it.
        let peers: Vec<Dec> = (1..=12).map(|i| Dec::from(i64::from(i))).collect();
        assert_eq!(
            volatility_bucket_for_catalog(&d("3.9"), 12, Some(&peers)).unwrap(),
            "LOW"
        );
        assert_eq!(
            volatility_bucket_for_catalog(&d("4"), 12, Some(&peers)).unwrap(),
            "MED"
        );
        assert_eq!(
            volatility_bucket_for_catalog(&d("8"), 12, Some(&peers)).unwrap(),
            "HIGH"
        );
        let one_peer = [d("0.2")];
        assert_eq!(
            volatility_bucket_for_catalog(&d("0.2"), 12, Some(&one_peer)).unwrap(),
            "HIGH"
        );
    }

    #[test]
    fn a_risk_label_needs_sixty_days() {
        let two = [d("0.02"), d("0.02")];
        assert!(catalog_volatility(&two, None, None).unwrap().is_none());
        let sixty: Vec<Dec> = (0..60)
            .map(|i| if i % 2 == 0 { d("0.01") } else { d("-0.01") })
            .collect();
        let v = catalog_volatility(&sixty, None, None).unwrap().unwrap();
        assert_eq!(v.basis, BASIS_FULL_HISTORY);
        assert_eq!(v.value.to_sci_string(), "0.1600847233");
        let four = [d("0.0123"), d("-0.0045"), d("0.0311"), d("0.00001")];
        assert_eq!(
            annualized_volatility(&four)
                .unwrap()
                .unwrap()
                .to_sci_string(),
            "0.2527078517"
        );
    }

    #[test]
    fn version_aware_nav_uses_the_weights_in_force() {
        let mut history = History::new();
        let row = |a: &str, b: &str| {
            let mut m = HashMap::new();
            m.insert("A".to_owned(), d(a));
            m.insert("B".to_owned(), d(b));
            m
        };
        history.insert(day(2026, 1, 1), row("100", "100"));
        history.insert(day(2026, 1, 2), row("110", "100"));
        history.insert(day(2026, 1, 5), row("110", "120"));
        let versions = vec![
            BasketVersion {
                effective_date: day(2026, 1, 1),
                weights: vec![("A".to_owned(), d("1.0000"))],
            },
            BasketVersion {
                effective_date: day(2026, 1, 5),
                weights: vec![("B".to_owned(), d("1.0000"))],
            },
        ];
        let nav = version_aware_nav(&history, &versions).unwrap();
        let points: Vec<String> = nav.points.iter().map(|(_, n)| n.to_sci_string()).collect();
        assert_eq!(points, vec!["100", "110.0", "132.00"]);
    }

    #[test]
    fn whole_months() {
        assert_eq!(whole_months_between(day(2024, 1, 31), day(2024, 2, 29)), 0);
        assert_eq!(whole_months_between(day(2024, 1, 15), day(2024, 3, 15)), 2);
        assert_eq!(whole_months_between(day(2024, 3, 15), day(2024, 1, 15)), 0);
    }

    #[test]
    fn cagr_uses_the_measured_span() {
        let years = d("365").div(&d("365.25")).unwrap();
        let rate = cagr(&d("100"), &d("110"), &years).unwrap().unwrap();
        // Python: cagr(nav_start=Decimal(100), nav_end=Decimal(110), years=Decimal(365)/Decimal("365.25"))
        assert_eq!(rate.to_sci_string(), "10.01");
        assert!(cagr(&d("100"), &d("110"), &d("0")).unwrap().is_none());
    }
}
