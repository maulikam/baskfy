//! Port of `baskfy_core.fno.vol` (`decile-blueprint/packages/core/src/baskfy_core/fno/vol.py`).
//!
//! ATM implied volatility per underlying per session (`04` §4 `iv_atm`). The rule: the
//! **nearest monthly with ≥ 8 sessions left**; the **strike nearest the future's settle** (the
//! lower one on a tie); **Black-76 on that settle, r = 0**, via the options pack's solver
//! (`baskfy_core.options.greeks.implied_vol`, carried here as [`greeks`] until
//! `options::greeks` has a Rust home); the **mean of CE and PE**. Null if either leg did not
//! trade that session or the solver refuses a leg. `T` is calendar days to expiry ÷ 365.
//!
//! Floats, deliberately: an IV is a model output, not money (options OP1.8, carried).
//!
//! Goldens: `L1/fno/vol.{iv_expiry,atm_iv,iv_atm_frame}.case_NNN.json`.

use std::cmp::Ordering;
use std::collections::BTreeMap;

use chrono::NaiveDate;
use polars::prelude::DataFrame;
use serde::{Deserialize, Serialize, Serializer, ser::SerializeStruct};

use super::calendar::{monthly_expiries, sessions_between};
use super::config::{FnoError, FnoResult, SeriesConfig, py};
use super::series::cols;

const DAYS_PER_YEAR: f64 = 365.0;

/// `CE` or `PE`.
///
/// Ports `baskfy_core.options.config.OptionType` (the options config has no Rust home yet).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum OptionType {
    /// A call.
    #[serde(rename = "CE")]
    Ce,
    /// A put.
    #[serde(rename = "PE")]
    Pe,
}

impl OptionType {
    /// `OptionType(value)`.
    ///
    /// # Errors
    /// `ValueError` for anything but `CE`/`PE`, with Python's enum message.
    pub fn parse(value: &str) -> FnoResult<Self> {
        match value {
            "CE" => Ok(Self::Ce),
            "PE" => Ok(Self::Pe),
            other => Err(FnoError::Value(format!(
                "{} is not a valid OptionType",
                py::repr_str(other)
            ))),
        }
    }
}

/// Black-76 on the forward, as `baskfy_core.options.greeks` computes it — the pieces
/// `implied_vol` needs.
pub mod greeks {
    use super::OptionType;

    /// The error function. CPython's `math.erf` calls the platform libm, which Rust's stable
    /// std does not expose and law 1's `unsafe` ban keeps out of reach; this is FreeBSD's
    /// `s_erf.c` (the fdlibm algorithm, < 1 ulp), which can differ from Apple's libm by 1-2
    /// ulp (DECISIONS-RS R3.x).
    #[must_use]
    #[allow(clippy::excessive_precision, clippy::unreadable_literal)]
    pub fn erf(x: f64) -> f64 {
        const ERX: f64 = 8.45062911510467529297e-01;
        const EFX8: f64 = 1.02703333676410069053e+00;
        const PP0: f64 = 1.28379167095512558561e-01;
        const PP1: f64 = -3.25042107247001499370e-01;
        const PP2: f64 = -2.84817495755985104766e-02;
        const PP3: f64 = -5.77027029648944159157e-03;
        const PP4: f64 = -2.37630166566501626084e-05;
        const QQ1: f64 = 3.97917223959155352819e-01;
        const QQ2: f64 = 6.50222499887672944485e-02;
        const QQ3: f64 = 5.08130628187576562776e-03;
        const QQ4: f64 = 1.32494738004321644526e-04;
        const QQ5: f64 = -3.96022827877536812320e-06;
        const PA0: f64 = -2.36211856075265944077e-03;
        const PA1: f64 = 4.14856118683748331666e-01;
        const PA2: f64 = -3.72207876035701323847e-01;
        const PA3: f64 = 3.18346619901161753674e-01;
        const PA4: f64 = -1.10894694282396677476e-01;
        const PA5: f64 = 3.54783043256182359371e-02;
        const PA6: f64 = -2.16637559486879084300e-03;
        const QA1: f64 = 1.06420880400844228286e-01;
        const QA2: f64 = 5.40397917702171048937e-01;
        const QA3: f64 = 7.18286544141962662868e-02;
        const QA4: f64 = 1.26171219808761642112e-01;
        const QA5: f64 = 1.36370839120290507362e-02;
        const QA6: f64 = 1.19844998467991074170e-02;
        const RA0: f64 = -9.86494403484714822705e-03;
        const RA1: f64 = -6.93858572707181764372e-01;
        const RA2: f64 = -1.05586262253232909814e+01;
        const RA3: f64 = -6.23753324503260060396e+01;
        const RA4: f64 = -1.62396669462573470355e+02;
        const RA5: f64 = -1.84605092906711035994e+02;
        const RA6: f64 = -8.12874355063065934246e+01;
        const RA7: f64 = -9.81432934416914548592e+00;
        const SA1: f64 = 1.96512716674392571292e+01;
        const SA2: f64 = 1.37657754143519042600e+02;
        const SA3: f64 = 4.34565877475229228821e+02;
        const SA4: f64 = 6.45387271733267880336e+02;
        const SA5: f64 = 4.29008140027567833386e+02;
        const SA6: f64 = 1.08635005541779435134e+02;
        const SA7: f64 = 6.57024977031928170135e+00;
        const SA8: f64 = -6.04244152148580987438e-02;
        const RB0: f64 = -9.86494292470009928597e-03;
        const RB1: f64 = -7.99283237680523006574e-01;
        const RB2: f64 = -1.77579549177547519889e+01;
        const RB3: f64 = -1.60636384855821916062e+02;
        const RB4: f64 = -6.37566443368389627722e+02;
        const RB5: f64 = -1.02509513161107724954e+03;
        const RB6: f64 = -4.83519191608651397019e+02;
        const SB1: f64 = 3.03380607434824582924e+01;
        const SB2: f64 = 3.25792512996573918826e+02;
        const SB3: f64 = 1.53672958608443695994e+03;
        const SB4: f64 = 3.19985821950859553908e+03;
        const SB5: f64 = 2.55305040643316442583e+03;
        const SB6: f64 = 4.74528541206955367215e+02;
        const SB7: f64 = -2.24409524465858183362e+01;

        let bits = x.to_bits();
        let high = u32::try_from(bits >> 32).unwrap_or(0);
        let negative = high >> 31 != 0;
        let ix = high & 0x7fff_ffff;
        if ix >= 0x7ff0_0000 {
            // erf(nan) = nan, erf(+-inf) = +-1.
            let sign = if negative { 1.0 } else { 0.0 };
            return 1.0 - 2.0 * sign + 1.0 / x;
        }
        if ix < 0x3feb_0000 {
            // |x| < 0.84375
            if ix < 0x3e30_0000 {
                // |x| < 2**-28
                return 0.125 * (8.0 * x + EFX8 * x);
            }
            let z = x * x;
            let r = PP0 + z * (PP1 + z * (PP2 + z * (PP3 + z * PP4)));
            let s = 1.0 + z * (QQ1 + z * (QQ2 + z * (QQ3 + z * (QQ4 + z * QQ5))));
            let y = r / s;
            return x + x * y;
        }
        let y = if ix < 0x4018_0000 {
            // 0.84375 <= |x| < 6
            let erfc2 = if ix < 0x3ff4_0000 {
                // |x| < 1.25
                let s = x.abs() - 1.0;
                let p = PA0 + s * (PA1 + s * (PA2 + s * (PA3 + s * (PA4 + s * (PA5 + s * PA6)))));
                let q = 1.0 + s * (QA1 + s * (QA2 + s * (QA3 + s * (QA4 + s * (QA5 + s * QA6)))));
                1.0 - ERX - p / q
            } else {
                let ax = x.abs();
                let s = 1.0 / (ax * ax);
                let (r, big_s) = if ix < 0x4006_db6d {
                    // |x| < 1/0.35
                    (
                        RA0 + s
                            * (RA1
                                + s * (RA2
                                    + s * (RA3 + s * (RA4 + s * (RA5 + s * (RA6 + s * RA7)))))),
                        1.0 + s
                            * (SA1
                                + s * (SA2
                                    + s * (SA3
                                        + s * (SA4
                                            + s * (SA5 + s * (SA6 + s * (SA7 + s * SA8))))))),
                    )
                } else {
                    (
                        RB0 + s * (RB1 + s * (RB2 + s * (RB3 + s * (RB4 + s * (RB5 + s * RB6))))),
                        1.0 + s
                            * (SB1
                                + s * (SB2
                                    + s * (SB3 + s * (SB4 + s * (SB5 + s * (SB6 + s * SB7)))))),
                    )
                };
                let z = f64::from_bits(ax.to_bits() & 0xffff_ffff_0000_0000);
                (-z * z - 0.5625).exp() * ((z - ax) * (z + ax) + r / big_s).exp() / ax
            };
            1.0 - erfc2
        } else {
            1.0 - f64::from_bits(0x0010_0000_0000_0000)
        };
        if negative { -y } else { y }
    }

    fn norm_cdf(x: f64) -> f64 {
        0.5 * (1.0 + erf(x / 2.0_f64.sqrt()))
    }

    fn valid(forward: f64, strike: f64, years: f64, vol: f64) -> bool {
        forward > 0.0 && strike > 0.0 && years > 0.0 && vol > 0.0
    }

    /// The Black-76 premium; the discounted intrinsic when the inputs are degenerate.
    ///
    /// Ports `baskfy_core.options.greeks.black76_price`.
    #[must_use]
    pub fn black76_price(
        forward: f64,
        strike: f64,
        years: f64,
        rate: f64,
        vol: f64,
        kind: OptionType,
    ) -> f64 {
        // Python's max(years, 0.0) keeps the first argument unless the second is greater.
        let discount = (-rate * if 0.0 > years { 0.0 } else { years }).exp();
        if !valid(forward, strike, years, vol) {
            let intrinsic = if kind == OptionType::Ce {
                forward - strike
            } else {
                strike - forward
            };
            return discount * if 0.0 > intrinsic { 0.0 } else { intrinsic };
        }
        let root = vol * years.sqrt();
        let d1 = ((forward / strike).ln() + 0.5 * vol * vol * years) / root;
        let d2 = d1 - root;
        if kind == OptionType::Ce {
            discount * (forward * norm_cdf(d1) - strike * norm_cdf(d2))
        } else {
            discount * (strike * norm_cdf(-d2) - forward * norm_cdf(-d1))
        }
    }

    /// The bisection's bounds and tolerance.
    #[derive(Debug, Clone, Copy, PartialEq)]
    pub struct Solver {
        /// The lowest vol tried.
        pub lower: f64,
        /// The highest vol tried.
        pub upper: f64,
        /// Stop when the bracket is narrower than this.
        pub tolerance: f64,
    }

    /// The Black-76 vol that reprices `price`, by bisection on `[lower, upper]`; `None` —
    /// never a clipped number — when the price is not bracketed or the inputs are degenerate.
    ///
    /// Ports `baskfy_core.options.greeks.implied_vol`.
    #[must_use]
    pub fn implied_vol(
        price: f64,
        forward: f64,
        strike: f64,
        years: f64,
        rate: f64,
        kind: OptionType,
        solver: Solver,
    ) -> Option<f64> {
        if price <= 0.0 || years <= 0.0 || forward <= 0.0 || strike <= 0.0 {
            return None;
        }
        let low_price = black76_price(forward, strike, years, rate, solver.lower, kind);
        let high_price = black76_price(forward, strike, years, rate, solver.upper, kind);
        if price < low_price || price > high_price {
            return None;
        }
        let (mut lo, mut hi) = (solver.lower, solver.upper);
        while hi - lo > solver.tolerance {
            let mid = (lo + hi) / 2.0;
            if black76_price(forward, strike, years, rate, mid, kind) < price {
                lo = mid;
            } else {
                hi = mid;
            }
        }
        Some((lo + hi) / 2.0)
    }
}

/// One option row of the bhavcopy for one underlying and session.
///
/// Ports `baskfy_core.fno.vol.OptionPrint`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OptionPrint {
    /// The contract's expiry.
    pub expiry: NaiveDate,
    /// The strike.
    pub strike: f64,
    /// `CE` or `PE`.
    pub option_type: OptionType,
    /// The close, if it printed one.
    pub close: Option<f64>,
    /// Contracts traded.
    pub volume: i64,
}

/// The answer for one underlying-session. `iv` is `None` when a leg did not trade.
///
/// Ports `baskfy_core.fno.vol.AtmIv`.
#[derive(Debug, Clone, PartialEq)]
pub struct AtmIv {
    /// The monthly used.
    pub expiry: NaiveDate,
    /// The ATM strike.
    pub strike: f64,
    /// The future's settle.
    pub forward: f64,
    /// The call's IV.
    pub iv_ce: Option<f64>,
    /// The put's IV.
    pub iv_pe: Option<f64>,
}

impl AtmIv {
    /// The mean of the two legs' IVs, or `None` if either is missing.
    ///
    /// Ports `baskfy_core.fno.vol.AtmIv.iv`.
    #[must_use]
    pub fn iv(&self) -> Option<f64> {
        match (self.iv_ce, self.iv_pe) {
            (Some(ce), Some(pe)) => Some((ce + pe) / 2.0),
            _ => None,
        }
    }
}

impl Serialize for AtmIv {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut s = serializer.serialize_struct("AtmIv", 6)?;
        s.serialize_field("expiry", &self.expiry)?;
        s.serialize_field("strike", &self.strike)?;
        s.serialize_field("forward", &self.forward)?;
        s.serialize_field("iv_ce", &self.iv_ce)?;
        s.serialize_field("iv_pe", &self.iv_pe)?;
        s.serialize_field("iv", &self.iv())?;
        s.end()
    }
}

/// The nearest monthly with at least `min_left` sessions from `day` to expiry.
///
/// Ports `baskfy_core.fno.vol.iv_expiry`.
#[must_use]
pub fn iv_expiry(
    day: NaiveDate,
    expiries: &[NaiveDate],
    sessions: &[NaiveDate],
    min_left: i64,
) -> Option<NaiveDate> {
    monthly_expiries(expiries)
        .into_iter()
        .filter(|e| *e > day)
        .find(|e| sessions_between(sessions, day, *e).is_some_and(|left| left >= min_left))
}

/// The last print with this `(strike, kind)` (a dict built row by row keeps the last value).
fn print_for(prints: &[&OptionPrint], strike: f64, kind: OptionType) -> Option<OptionPrint> {
    prints
        .iter()
        .rev()
        .find(|p| p.strike.partial_cmp(&strike) == Some(Ordering::Equal) && p.option_type == kind)
        .map(|p| (*p).clone())
}

fn leg_iv(
    prints: &[&OptionPrint],
    strike: f64,
    kind: OptionType,
    forward: f64,
    years: f64,
    config: &SeriesConfig,
) -> Option<f64> {
    let row = print_for(prints, strike, kind)?;
    let close = row.close?;
    if row.volume <= 0 || close <= 0.0 {
        return None;
    }
    greeks::implied_vol(
        close,
        forward,
        strike,
        years,
        config.iv_rate,
        kind,
        greeks::Solver {
            lower: config.iv_lower,
            upper: config.iv_upper,
            tolerance: config.iv_tolerance,
        },
    )
}

/// `04` §4's `iv_atm` for one underlying on `day`. `options` are that underlying's option rows
/// of the day; `futures_settle` maps each listed futures expiry to its settle that day. `None`
/// when no monthly qualifies, its future has no positive settle, or no strike is listed for it.
///
/// Ports `baskfy_core.fno.vol.atm_iv`.
#[must_use]
pub fn atm_iv(
    day: NaiveDate,
    options: &[OptionPrint],
    futures_settle: &BTreeMap<NaiveDate, f64>,
    sessions: &[NaiveDate],
    config: &SeriesConfig,
) -> Option<AtmIv> {
    let listed: Vec<NaiveDate> = options.iter().map(|r| r.expiry).collect();
    let expiry = iv_expiry(day, &listed, sessions, config.iv_min_sessions_left)?;
    let forward = *futures_settle.get(&expiry)?;
    if forward <= 0.0 {
        return None;
    }
    let prints: Vec<&OptionPrint> = options.iter().filter(|r| r.expiry == expiry).collect();
    let mut strikes: Vec<f64> = Vec::new();
    for p in &prints {
        if !strikes.contains(&p.strike) {
            strikes.push(p.strike);
        }
    }
    strikes.sort_by(f64::total_cmp);
    // `min(strikes, key=lambda k: (abs(k - forward), k))`: the first of equal keys.
    let mut best: Option<(f64, f64)> = None;
    for k in strikes {
        let key = ((k - forward).abs(), k);
        let better = match best {
            None => true,
            Some(b) => match key.0.partial_cmp(&b.0) {
                Some(Ordering::Less) => true,
                Some(Ordering::Equal) => key.1 < b.1,
                _ => false,
            },
        };
        if better {
            best = Some(key);
        }
    }
    let (_, strike) = best?;
    let days = (expiry - day).num_days();
    #[allow(clippy::cast_precision_loss)]
    let years = days as f64 / DAYS_PER_YEAR;
    Some(AtmIv {
        expiry,
        strike,
        forward,
        iv_ce: leg_iv(&prints, strike, OptionType::Ce, forward, years, config),
        iv_pe: leg_iv(&prints, strike, OptionType::Pe, forward, years, config),
    })
}

/// `atm_iv` over frames: one row per `(trade_date, symbol)` that has options, sorted by them.
///
/// `options`: `trade_date, symbol, expiry, strike, option_type, close, volume`; `futures`:
/// `trade_date, symbol, expiry, settle`. Returns `trade_date, symbol, iv_expiry, iv_strike,
/// iv_atm` (`iv_atm` null where `04` §4 says null).
///
/// Ports `baskfy_core.fno.vol.iv_atm_frame`.
///
/// # Errors
/// A missing column; `TypeError` for a null `trade_date`/`symbol`; `ValueError` for an
/// `option_type` that is not `CE`/`PE`.
pub fn iv_atm_frame(
    options: &DataFrame,
    futures: &DataFrame,
    sessions: &[NaiveDate],
    config: &SeriesConfig,
) -> FnoResult<DataFrame> {
    type Key = (Option<NaiveDate>, Option<String>);
    let mut settles: BTreeMap<Key, BTreeMap<NaiveDate, f64>> = BTreeMap::new();
    {
        let days = cols::dates(futures, "trade_date")?;
        let symbols = cols::strings(futures, "symbol")?;
        let expiries = cols::dates(futures, "expiry")?;
        let values = cols::floats(futures, "settle")?;
        for i in 0..days.len() {
            if let (Some(settle), Some(expiry)) = (values[i], expiries[i]) {
                settles
                    .entry((days[i], symbols[i].clone()))
                    .or_default()
                    .insert(expiry, settle);
            }
        }
    }
    let days = cols::dates(options, "trade_date")?;
    let symbols = cols::strings(options, "symbol")?;
    let expiries = cols::dates(options, "expiry")?;
    let strikes = cols::floats(options, "strike")?;
    let kinds = cols::strings(options, "option_type")?;
    let closes = cols::floats(options, "close")?;
    let volumes = cols::ints(options, "volume")?;
    // `partition_by("trade_date", "symbol")`, each group in frame order, then sorted by
    // `(str(day), str(symbol))`.
    let mut by_key: BTreeMap<Key, Vec<usize>> = BTreeMap::new();
    for i in 0..days.len() {
        by_key
            .entry((days[i], symbols[i].clone()))
            .or_default()
            .push(i);
    }
    let mut groups: Vec<(Key, Vec<usize>)> = by_key.into_iter().collect();
    let show = |k: &Key| {
        (
            k.0.map_or_else(|| "None".to_owned(), |d| d.to_string()),
            k.1.clone().unwrap_or_else(|| "None".to_owned()),
        )
    };
    groups.sort_by_key(|(k, _)| show(k));
    let mut out_day = Vec::new();
    let mut out_symbol = Vec::new();
    let mut out_expiry: Vec<Option<NaiveDate>> = Vec::new();
    let mut out_strike: Vec<Option<f64>> = Vec::new();
    let mut out_iv: Vec<Option<f64>> = Vec::new();
    for ((day, symbol), rows) in groups {
        let (Some(day), Some(symbol)) = (day, symbol) else {
            return Err(FnoError::Type(
                "trade_date must be a date and symbol a string".to_owned(),
            ));
        };
        let mut prints = Vec::with_capacity(rows.len());
        for i in rows {
            let expiry = expiries[i]
                .ok_or_else(|| FnoError::Type("an option row has no expiry".to_owned()))?;
            let strike = strikes[i]
                .ok_or_else(|| FnoError::Type("an option row has no strike".to_owned()))?;
            let kind = OptionType::parse(kinds[i].as_deref().unwrap_or("None"))?;
            prints.push(OptionPrint {
                expiry,
                strike,
                option_type: kind,
                close: closes[i],
                volume: volumes[i].unwrap_or(0),
            });
        }
        let empty = BTreeMap::new();
        let settle = settles
            .get(&(Some(day), Some(symbol.clone())))
            .unwrap_or(&empty);
        let found = atm_iv(day, &prints, settle, sessions, config);
        out_day.push(day);
        out_symbol.push(symbol);
        match found {
            Some(a) => {
                out_expiry.push(Some(a.expiry));
                out_strike.push(Some(a.strike));
                out_iv.push(a.iv());
            }
            None => {
                out_expiry.push(None);
                out_strike.push(None);
                out_iv.push(None);
            }
        }
    }
    let days_out: Vec<Option<NaiveDate>> = out_day.into_iter().map(Some).collect();
    let symbols_out: Vec<Option<String>> = out_symbol.into_iter().map(Some).collect();
    let height = days_out.len();
    Ok(DataFrame::new(
        height,
        vec![
            cols::date_column("trade_date", &days_out)?,
            cols::string_column("symbol", &symbols_out),
            cols::date_column("iv_expiry", &out_expiry)?,
            cols::float_column("iv_strike", &out_strike),
            cols::float_column("iv_atm", &out_iv),
        ],
    )?)
}
