//! `baskfy_core.reconcile`: the docs/05 relationships between the export's published columns.

use baskfy_core::decimal::{Rounding, WideDecimal};

use super::export::{Cell, Frame};
use crate::error::WorkerError;

type R<T> = Result<T, WorkerError>;

fn dec_err(e: &baskfy_core::decimal::DecimalError) -> WorkerError {
    WorkerError::python("InvalidOperation", e.to_string())
}

fn d(text: &str) -> WideDecimal {
    text.parse().unwrap_or_else(|_| WideDecimal::zero())
}

fn n(value: i64) -> WideDecimal {
    WideDecimal::from(value)
}

fn div(a: &WideDecimal, b: &WideDecimal) -> R<WideDecimal> {
    a.div(b).map_err(|e| dec_err(&e))
}

fn quantize(value: &WideDecimal, exp: i64) -> R<WideDecimal> {
    value
        .quantize(exp, Rounding::HalfEven)
        .map_err(|e| dec_err(&e))
}

fn percent_half_ulp() -> WideDecimal {
    d("0.005")
}

fn price_half_ulp() -> WideDecimal {
    d("0.005")
}

fn ratio_half_ulp() -> WideDecimal {
    d("0.00000000005")
}

pub(crate) const WINDOW_SUFFIX: [(i64, &str); 5] = [
    (12, "one_year"),
    (9, "nine_months"),
    (6, "six_months"),
    (3, "three_months"),
    (1, "one_month"),
];

fn suffix(months: i64) -> &'static str {
    WINDOW_SUFFIX
        .iter()
        .find(|(m, _)| *m == months)
        .map_or("", |(_, s)| s)
}

fn window_length(months: i64) -> i64 {
    match months {
        12 => 247,
        9 => 185,
        6 => 121,
        3 => 64,
        _ => 22,
    }
}

const SORTING_BLEND: [i64; 4] = [12, 6, 3, 1];
const MINIMUM_DISTRIBUTION_SAMPLE: usize = 30;

const REFERENCE_EXPORT_UNIVERSES: [&str; 14] = [
    "nifty-50",
    "nifty-next-50",
    "nifty-100",
    "nifty-200",
    "nifty-500",
    "nifty-total-market",
    "nifty-large-mid-250",
    "nifty-midcap-150",
    "nifty-smallcap-250",
    "nifty-microcap-250",
    "nifty-mid-small-400",
    "nifty-allcap",
    "nifty-fno",
    "etf",
];

const UNIVERSE_CONTAINMENT: [(&str, &str); 10] = [
    ("nifty_50", "nifty_100"),
    ("nifty_next_50", "nifty_100"),
    ("nifty_100", "nifty_200"),
    ("nifty_200", "nifty_500"),
    ("nifty_500", "nifty_total_market"),
    ("nifty_100", "nifty_large_mid_250"),
    ("nifty_midcap_150", "nifty_large_mid_250"),
    ("nifty_midcap_150", "nifty_mid_small_400"),
    ("nifty_smallcap_250", "nifty_mid_small_400"),
    ("nifty_total_market", "nifty_allcap"),
];

/// One cell where the published numbers do not satisfy a docs/05 relationship.
#[derive(Debug, Clone)]
pub(crate) struct Disagreement {
    pub symbol: String,
    pub column: String,
    pub expected: WideDecimal,
    pub actual: WideDecimal,
    pub tolerance: WideDecimal,
}

impl Disagreement {
    fn new(
        symbol: impl Into<String>,
        column: impl Into<String>,
        expected: WideDecimal,
        actual: WideDecimal,
        tolerance: WideDecimal,
    ) -> Self {
        Self {
            symbol: symbol.into(),
            column: column.into(),
            expected,
            actual,
            tolerance,
        }
    }

    /// `abs(expected - actual)`.
    pub(crate) fn delta(&self) -> WideDecimal {
        self.expected.sub(&self.actual).abs()
    }
}

impl std::fmt::Display for Disagreement {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{:<14} {:<34} expected={} published={} delta={} tolerance={}",
            self.symbol,
            self.column,
            self.expected.to_sci_string(),
            self.actual.to_sci_string(),
            self.delta().to_sci_string(),
            self.tolerance.to_sci_string()
        )
    }
}

/// One reconciliation check.
#[derive(Debug, Clone)]
pub(crate) struct CheckResult {
    pub name: &'static str,
    pub reference: &'static str,
    pub description: String,
    pub cells: usize,
    pub disagreements: Vec<Disagreement>,
    pub unresolved: Vec<String>,
    pub notes: Vec<String>,
}

impl CheckResult {
    fn new(
        name: &'static str,
        reference: &'static str,
        description: impl Into<String>,
        cells: usize,
        disagreements: Vec<Disagreement>,
    ) -> Self {
        Self {
            name,
            reference,
            description: description.into(),
            cells,
            disagreements,
            unresolved: Vec::new(),
            notes: Vec::new(),
        }
    }

    /// `not disagreements`.
    pub(crate) fn clean(&self) -> bool {
        self.disagreements.is_empty()
    }
}

fn opt(cell: &Cell) -> Option<&Cell> {
    (*cell != Cell::Null).then_some(cell)
}

fn check_sharpe_identity(frame: &Frame) -> R<CheckResult> {
    let mut out = Vec::new();
    let mut cells = 0;
    let hundred = n(100);
    for row in frame.rows() {
        for (_, sfx) in WINDOW_SUFFIX {
            let (Some(ret), Some(vol), Some(sharpe)) = (
                opt(row.get(&format!("absolute_return_{sfx}"))),
                opt(row.get(&format!("volatility_{sfx}"))),
                opt(row.get(&format!("sharpe_return_{sfx}"))),
            ) else {
                continue;
            };
            let volatility = vol.dec()?.mul(&hundred);
            if volatility.is_zero() {
                continue;
            }
            cells += 1;
            let expected = div(&ret.dec()?, &volatility)?;
            let tolerance = percent_half_ulp()
                .add(&div(&percent_half_ulp(), &volatility)?)
                .add(&div(
                    &expected.abs().mul(&ratio_half_ulp().mul(&hundred)),
                    &volatility,
                )?);
            let actual = sharpe.dec()?;
            if expected.sub(&actual).abs() > tolerance {
                out.push(Disagreement::new(
                    row.symbol(),
                    format!("sharpe_return_{sfx}"),
                    expected,
                    actual,
                    tolerance,
                ));
            }
        }
    }
    Ok(CheckResult::new(
        "sharpe_identity",
        "docs/05 §3",
        "the reference product's Sharpe is ret / (vol x 100), with no risk-free rate",
        cells,
        out,
    ))
}

fn check_away_from_high(frame: &Frame) -> R<CheckResult> {
    let mut out = Vec::new();
    let mut cells = 0;
    let hundred = n(100);
    for row in frame.rows() {
        let close = row.get("close").dec()?;
        for (high_column, away_column) in [
            ("high_one_year", "away_from_high_one_year"),
            ("high_all_time", "away_from_high_all_time"),
        ] {
            let high = row.get(high_column).dec()?;
            if high.is_zero() {
                continue;
            }
            cells += 1;
            let expected = div(&close, &high)?.sub(&n(1)).mul(&hundred);
            let tolerance = percent_half_ulp()
                .add(&div(&hundred.mul(&price_half_ulp()), &high)?)
                .add(&div(
                    &hundred.mul(&close).mul(&price_half_ulp()),
                    &high.mul(&high),
                )?);
            let actual = row.get(away_column).dec()?;
            if expected.sub(&actual).abs() > tolerance {
                out.push(Disagreement::new(
                    row.symbol(),
                    away_column,
                    expected,
                    actual,
                    tolerance,
                ));
            }
        }
    }
    Ok(CheckResult::new(
        "away_from_high",
        "docs/05 §10",
        "distance from the 1-year and all-time high, recomputed from close and high",
        cells,
        out,
    ))
}

fn check_positive_days_denominator(frame: &Frame) -> R<CheckResult> {
    let mut out = Vec::new();
    let mut cells = 0;
    let hundred = n(100);
    for row in frame.rows() {
        for (months, sfx) in WINDOW_SUFFIX {
            let Some(value) = opt(row.get(&format!("positive_days_percent_{sfx}"))) else {
                continue;
            };
            let len = window_length(months);
            cells += 1;
            let k = div(&value.dec()?.mul(&n(len)), &hundred)?;
            let nearest = quantize(&k, 0)?;
            let nearest = if nearest.is_zero() { n(0) } else { nearest };
            let tolerance = div(&percent_half_ulp().mul(&n(len)), &hundred)?;
            if k.sub(&nearest).abs() > tolerance {
                out.push(Disagreement::new(
                    row.symbol(),
                    format!("positive_days_percent_{sfx} (N={len})"),
                    nearest,
                    k,
                    tolerance,
                ));
            }
        }
    }
    Ok(CheckResult::new(
        "positive_days_denominator",
        "docs/13 §3, docs/05 §11",
        "the shared denominators {12: 247, 9: 185, 6: 121, 3: 64, 1: 22} still divide every \
         published percentage",
        cells,
        out,
    ))
}

fn check_blend_ordering(frame: &Frame) -> R<CheckResult> {
    let components: Vec<String> = SORTING_BLEND
        .iter()
        .map(|m| format!("sharpe_return_{}", suffix(*m)))
        .collect();
    let blend_tolerance = percent_half_ulp().mul(&n(2));
    let mut values: Vec<(String, WideDecimal)> = Vec::new();
    for row in frame.rows() {
        let parts: Vec<&Cell> = components.iter().map(|c| row.get(c)).collect();
        if parts.iter().any(|p| **p == Cell::Null) {
            continue;
        }
        let mut total = WideDecimal::zero();
        for p in parts {
            total = total.add(&p.dec()?);
        }
        values.push((
            row.symbol(),
            div(&total, &WideDecimal::from(components.len()))?,
        ));
    }
    let mut inversions = 0;
    let mut worst = WideDecimal::zero();
    let mut out = Vec::new();
    for index in 1..values.len() {
        let above = &values[index - 1].1;
        let (symbol, below) = &values[index];
        if below > above {
            inversions += 1;
            let gap = below.sub(above);
            if gap > worst {
                worst = gap.clone();
            }
            if gap > blend_tolerance {
                out.push(Disagreement::new(
                    symbol.clone(),
                    format!("avg_sharpe_12_6_3_1 (row {})", index + 1),
                    above.clone(),
                    below.clone(),
                    blend_tolerance.clone(),
                ));
            }
        }
    }
    let mut result = CheckResult::new(
        "blend_ordering",
        "docs/05 §4, docs/13 §2 finding 3",
        "the export's row order is the mean of its sharpe components, to rounding granularity",
        values.len(),
        out,
    );
    result.notes.push(format!(
        "{inversions} inversions, worst {}, all within {} (docs/13 §2 reports 48 inversions, all \
         <= 0.0075)",
        worst.to_sci_string(),
        blend_tolerance.to_sci_string()
    ));
    Ok(result)
}

fn count(row: &super::export::Row<'_>, column: &str) -> R<i64> {
    row.get(column).int().ok_or_else(|| {
        WorkerError::python(
            "TypeError",
            format!("'>' not supported for the non-integer {column}"),
        )
    })
}

fn check_circuit_nesting(frame: &Frame) -> R<CheckResult> {
    let mut out = Vec::new();
    let order = [1, 3, 6, 9, 12];
    let mut cells = 0;
    for row in frame.rows() {
        let counts: Vec<i64> = order
            .iter()
            .map(|m| count(&row, &format!("circuits_{}", suffix(*m))))
            .collect::<R<_>>()?;
        for index in 0..order.len() - 1 {
            cells += 1;
            if counts[index] > counts[index + 1] {
                out.push(Disagreement::new(
                    row.symbol(),
                    format!(
                        "circuits_{} > circuits_{}",
                        suffix(order[index]),
                        suffix(order[index + 1])
                    ),
                    n(counts[index + 1]),
                    n(counts[index]),
                    n(0),
                ));
            }
        }
        for (index, months) in order.iter().enumerate() {
            let len = window_length(*months);
            cells += 1;
            if counts[index] > len {
                out.push(Disagreement::new(
                    row.symbol(),
                    format!("circuits_{} exceeds its {len}-bar window", suffix(*months)),
                    n(len),
                    n(counts[index]),
                    n(0),
                ));
            }
        }
    }
    let mut non_zero = 0;
    for row in frame.rows() {
        if count(&row, "circuits_one_year")? > 0 {
            non_zero += 1;
        }
    }
    let mut result = CheckResult::new(
        "circuit_nesting",
        "docs/05 §12 (INFERRED)",
        "circuit-hit counts are nested across windows and bounded by the window length",
        cells,
        out,
    );
    result.unresolved = vec![
        "docs/05 §12's detection rule cannot be validated from this snapshot. Validating it needs \
         per-bar high/low/close and the NSE band for each day; the export carries a single day \
         of counts. Nesting and bounds are all the published data can decide."
            .to_owned(),
        format!(
            "{non_zero} of {} rows report at least one circuit-hit day in the last year, which is \
             the only distributional evidence available for calibrating the band heuristic.",
            frame.height()
        ),
    ];
    Ok(result)
}

fn check_turnover_is_exchange_value(frame: &Frame) -> R<CheckResult> {
    let name = "turnover_is_exchange_value";
    let reference = "docs/05 §13, docs/13 §2 finding 5";
    let description = "the `volume` column is VWAP-based value traded, not close x share count";
    let mut ratios: Vec<WideDecimal> = Vec::new();
    for row in frame.rows() {
        let shares = row.get("volume_shares");
        let close = row.get("close").dec()?;
        if !shares.truthy() || close.is_zero() {
            continue;
        }
        ratios.push(div(&row.get("volume").dec()?, &close.mul(&shares.dec()?))?);
    }
    let len = ratios.len();
    if len < MINIMUM_DISTRIBUTION_SAMPLE {
        let mut result = CheckResult::new(name, reference, description, len, Vec::new());
        result.notes.push(format!(
            "skipped: {len} rows is below the {MINIMUM_DISTRIBUTION_SAMPLE} this check needs. \
             docs/13 §2 states the evidence as a mean and a standard deviation, and a handful of \
             rows has neither. Run the report over the whole export to see it."
        ));
        return Ok(result);
    }
    let count = WideDecimal::from(len);
    let mean = div(&WideDecimal::sum(ratios.iter()), &count)?;
    let mut variance_sum = WideDecimal::zero();
    for r in &ratios {
        let gap = r.sub(&mean);
        variance_sum = variance_sum.add(&gap.mul(&gap));
    }
    let variance = div(&variance_sum, &count)?;
    let sd = variance.sqrt().map_err(|e| dec_err(&e))?;
    let mut out = Vec::new();
    if mean.sub(&d("0.9999")).abs() > d("0.0005") {
        out.push(Disagreement::new(
            "(all rows)",
            "mean(volume / (close x volume_shares))",
            d("0.9999"),
            mean.clone(),
            d("0.0005"),
        ));
    }
    if sd.sub(&d("0.008")).abs() > d("0.001") {
        out.push(Disagreement::new(
            "(all rows)",
            "sd(volume / (close x volume_shares))",
            d("0.008"),
            sd.clone(),
            d("0.001"),
        ));
    }
    let mut result = CheckResult::new(name, reference, description, len, out);
    result.notes.push(format!(
        "mean {}, sd {} over {len} rows",
        quantize(&mean, -6)?,
        quantize(&sd, -6)?
    ));
    Ok(result)
}

fn check_price_level_ordering(frame: &Frame) -> R<CheckResult> {
    let mut out = Vec::new();
    let mut cells = 0;
    let slack = price_half_ulp().mul(&n(2));
    for row in frame.rows() {
        let high_1y = row.get("high_one_year").dec()?;
        let high_ath = row.get("high_all_time").dec()?;
        cells += 1;
        if high_ath < high_1y.sub(&slack) {
            out.push(Disagreement::new(
                row.symbol(),
                "high_all_time < high_one_year",
                high_1y.clone(),
                high_ath.clone(),
                slack.clone(),
            ));
        }
        for k in [20, 50, 100, 200] {
            let Some(average) = opt(row.get(&format!("ma_{k}"))) else {
                continue;
            };
            cells += 1;
            let average = average.dec()?;
            if average > high_ath.add(&slack) {
                out.push(Disagreement::new(
                    row.symbol(),
                    format!("ma_{k} exceeds high_all_time"),
                    high_ath.clone(),
                    average,
                    slack.clone(),
                ));
            }
        }
        if let Some(ma_20) = opt(row.get("ma_20")) {
            cells += 1;
            let ma_20 = ma_20.dec()?;
            if ma_20 > high_1y.add(&slack) {
                out.push(Disagreement::new(
                    row.symbol(),
                    "ma_20 exceeds high_one_year",
                    high_1y.clone(),
                    ma_20,
                    slack.clone(),
                ));
            }
        }
    }
    Ok(CheckResult::new(
        "price_level_ordering",
        "docs/05 §9, §10",
        "high_ath >= high_1y >= ma_20, and no moving average exceeds the all-time high",
        cells,
        out,
    ))
}

fn check_value_ranges(frame: &Frame) -> R<CheckResult> {
    let mut out = Vec::new();
    let mut cells = 0;
    let (floor, ceiling, vol_ceiling) = (n(0), n(100), n(5));
    for row in frame.rows() {
        for (_, sfx) in WINDOW_SUFFIX {
            if let Some(rsi) = opt(row.get(&format!("rsi_{sfx}"))) {
                cells += 1;
                let value = rsi.dec()?;
                if value < floor || value > ceiling {
                    out.push(Disagreement::new(
                        row.symbol(),
                        format!("rsi_{sfx} outside [0, 100]"),
                        ceiling.clone(),
                        value,
                        n(0),
                    ));
                }
            }
            if let Some(vol) = opt(row.get(&format!("volatility_{sfx}"))) {
                cells += 1;
                let value = vol.dec()?;
                if value <= floor {
                    out.push(Disagreement::new(
                        row.symbol(),
                        format!("volatility_{sfx} is not positive"),
                        n(0),
                        value.clone(),
                        n(0),
                    ));
                }
                if value > vol_ceiling {
                    out.push(Disagreement::new(
                        row.symbol(),
                        format!("volatility_{sfx} looks like a percentage, not a fraction"),
                        vol_ceiling.clone(),
                        value,
                        n(0),
                    ));
                }
            }
        }
    }
    Ok(CheckResult::new(
        "value_ranges",
        "docs/05 §2, §5; docs/13 §2 finding 4",
        "RSI in [0, 100]; volatility a positive decimal fraction",
        cells,
        out,
    ))
}

fn check_universe_containment(frame: &Frame) -> CheckResult {
    let mut out = Vec::new();
    let mut cells = 0;
    for row in frame.rows() {
        for (inner, outer) in UNIVERSE_CONTAINMENT {
            cells += 1;
            if row.get(&format!("is_{inner}")).is_one() && !row.get(&format!("is_{outer}")).is_one()
            {
                out.push(Disagreement::new(
                    row.symbol(),
                    format!("is_{inner} without is_{outer}"),
                    n(1),
                    n(0),
                    n(0),
                ));
            }
        }
    }
    CheckResult::new(
        "universe_containment",
        "docs/01 §2",
        "NIFTY 50 c 100 c 200 c 500 c Total Market, and the mid/small families nest likewise",
        cells,
        out,
    )
}

fn check_top_risk_flag_monotonicity(frame: &Frame) -> R<CheckResult> {
    let mut out = Vec::new();
    let mut cells = 0;
    let mut notes = Vec::new();
    for universe in REFERENCE_EXPORT_UNIVERSES {
        let slug = universe.replace('-', "_");
        let members: Vec<_> = frame
            .rows()
            .filter(|r| r.get(&format!("is_{slug}")).is_one())
            .collect();
        if members.is_empty() {
            continue;
        }
        for (metric, column) in [("beta", "beta"), ("volatility", "volatility_one_year")] {
            let flag = format!("is_{slug}_top_{metric}");
            let flagged: Vec<_> = members.iter().filter(|r| r.get(&flag).is_one()).collect();
            let unflagged: Vec<_> = members.iter().filter(|r| !r.get(&flag).is_one()).collect();
            if flagged.is_empty() || unflagged.is_empty() {
                continue;
            }
            cells += members.len();
            let mut lowest: Option<WideDecimal> = None;
            for r in &flagged {
                let v = r.get(column).dec()?;
                if lowest.as_ref().is_none_or(|l| v < *l) {
                    lowest = Some(v);
                }
            }
            let lowest = lowest.unwrap_or_else(WideDecimal::zero);
            for row in unflagged {
                let value = row.get(column).dec()?;
                if value > lowest {
                    out.push(Disagreement::new(
                        row.symbol(),
                        format!("{flag} unset despite {column} above the flagged minimum"),
                        lowest.clone(),
                        value,
                        n(0),
                    ));
                }
            }
            #[allow(clippy::cast_precision_loss)]
            let share = (100 * flagged.len()) as f64 / members.len() as f64;
            notes.push(format!(
                "{slug}/{metric}: {}/{} visible rows flagged ({share:.1}%)",
                flagged.len(),
                members.len()
            ));
        }
    }
    let mut result = CheckResult::new(
        "top_risk_flag_monotonicity",
        "docs/06 §Step 4",
        "the top-beta and top-volatility flags are a monotone cut within each universe",
        cells,
        out,
    );
    result.notes = notes;
    Ok(result)
}

/// `skip_month_evidence`: the arithmetic behind docs/05 §8's finding.
pub(crate) fn skip_month_evidence() -> R<Vec<String>> {
    let published_12_1 = d("608.37");
    let published_12_2 = d("852.21");
    let ret_12m = d("753.00");
    let ret_1m = d("37.29");
    let hundred = n(100);
    let growth_12m = n(1).add(&div(&ret_12m, &hundred)?);
    let growth_1m = n(1).add(&div(&ret_1m, &hundred)?);
    let candidate_a = div(&growth_12m, &growth_1m)?.sub(&n(1)).mul(&hundred);
    let gap = published_12_1.sub(&candidate_a);
    let gap_share = div(&gap, &candidate_a)?.mul(&hundred);
    let candidate_b = n(1)
        .add(&div(&published_12_1, &hundred)?)
        .mul(&growth_1m)
        .sub(&n(1))
        .mul(&hundred);
    Ok(vec![
        "docs/05 §8 leaves two candidate definitions open and asks Prompt 19 to settle it \
         empirically. IT CANNOT BE SETTLED FROM THIS BUNDLE. Reasons and arithmetic:"
            .to_owned(),
        "  (1) The 93-column export carries no skip-month column at all — the reference product \
         offers 12-1 and 12-2 as sort keys (docs/01 §3) and never exports the values. So the 271 \
         rows contribute nothing to this question."
            .to_owned(),
        "  (2) The only published values anywhere in docs/ are CUPID's 608.37 and 852.21, from \
         19 Aug 2026 (docs/05 §8)."
            .to_owned(),
        format!(
            "  (3) Candidate A — docs/05's primary, P_(t-21)/P_(t-252) — is fully determined by \
             CUPID's other published figures on that day: (1 + {}%) / (1 + {}%) - 1 = {}%, \
             against a published {}%. The gap is {} percentage points, or {}% of the value. The \
             21-vs-22 and 247-vs-252 bar mismatches between §8's bar offsets and §1's calendar \
             windows are worth about six bars of a name compounding at ~0.86% a day, i.e. ~5% — \
             a quarter of the gap. Candidate A is therefore INCONSISTENT with the published \
             figure, by a margin the offset mismatch does not explain.",
            ret_12m.to_sci_string(),
            ret_1m.to_sci_string(),
            quantize(&candidate_a, -2)?.to_sci_string(),
            published_12_1.to_sci_string(),
            quantize(&gap, -2)?.to_sci_string(),
            quantize(&gap_share, -1)?.to_sci_string(),
        ),
        format!(
            "  (4) Candidate B — P_(t-21)/P_(t-273) — is under-determined: it implies a 13-month \
             return of {}% for CUPID, which nothing in the bundle contradicts and nothing \
             confirms. It is NOT CONFIRMED.",
            quantize(&candidate_b, 0)?
        ),
        format!(
            "  (5) The 12-2 figure ({}%) exceeds the 12-month return ({}%), which under either \
             candidate requires CUPID's price to have fallen materially between t-42 and t-21 \
             while rising 37% over the last month. That is possible and is not evidence either \
             way.",
            published_12_2.to_sci_string(),
            ret_12m.to_sci_string()
        ),
        "  DECISION: the engine keeps shipping candidate A (SkipMonthDefinition.SKIP_END), which \
         is the definition docs/05 §8 instructs us to implement, and the switch to candidate B \
         remains a constructor argument (baskfy_core.momentum.SkipMonthConfig). Resolving it \
         needs CUPID's real adjusted closes for ~294 trading days, which arrives with the first \
         production backfill and not before."
            .to_owned(),
    ])
}

fn check_skip_month_momentum() -> R<CheckResult> {
    let mut result = CheckResult::new(
        "skip_month_momentum",
        "docs/05 §8 (INFERRED)",
        "12-1 and 12-2 momentum: which of docs/05's two candidate definitions the product uses",
        0,
        Vec::new(),
    );
    result.unresolved = skip_month_evidence()?;
    Ok(result)
}

fn check_circuit_detection_rule(frame: &Frame) -> R<CheckResult> {
    let counts: Vec<i64> = frame
        .rows()
        .map(|r| count(&r, "circuits_one_year"))
        .collect::<R<_>>()?;
    let non_zero = counts.iter().filter(|c| **c > 0).count();
    let largest = counts.iter().max().copied().ok_or_else(|| {
        WorkerError::python("ValueError", "max() iterable argument is empty".to_owned())
    })?;
    let mut result = CheckResult::new(
        "circuit_detection_rule",
        "docs/05 §12 (INFERRED)",
        "which rule the reference product uses to call a day circuit-locked",
        counts.len(),
        Vec::new(),
    );
    result.unresolved = vec![
        "docs/05 §12's rule has two branches (published NSE bands; a |r_t| band heuristic with a \
         0.25% tolerance) and the snapshot can distinguish neither. Distinguishing them needs, \
         per bar: high, low, close_raw, the previous close and the day's NSE band. The export \
         has one day of counts."
            .to_owned(),
        format!(
            "What the counts do say: {non_zero} of {} rows recorded at least one hit in the last \
             year; the largest is {largest} days out of {}. A detection rule that fired on, say, \
             every 20% up-day would produce far more than that on a 271-row momentum screen, so \
             the reference product's rule is at least as strict as ours. That is a bound, not a \
             validation.",
            counts.len(),
            window_length(12)
        ),
        "DECISION: the engine keeps docs/05 §12's rule as written, with the published-band \
         branch preferred and the method recorded per row (baskfy_core.circuits.CircuitMethod). \
         Recalibration needs bhavcopy history."
            .to_owned(),
    ];
    Ok(result)
}

/// `reconcile(frame)`: every check, in `ALL_CHECKS` order.
pub(crate) fn reconcile(frame: &Frame) -> R<Vec<CheckResult>> {
    Ok(vec![
        check_sharpe_identity(frame)?,
        check_away_from_high(frame)?,
        check_positive_days_denominator(frame)?,
        check_blend_ordering(frame)?,
        check_circuit_nesting(frame)?,
        check_turnover_is_exchange_value(frame)?,
        check_price_level_ordering(frame)?,
        check_value_ranges(frame)?,
        check_universe_containment(frame),
        check_top_risk_flag_monotonicity(frame)?,
        check_skip_month_momentum()?,
        check_circuit_detection_rule(frame)?,
    ])
}

/// `total_disagreements(results)`.
pub(crate) fn total_disagreements(results: &[CheckResult]) -> usize {
    results.iter().map(|r| r.disagreements.len()).sum()
}
