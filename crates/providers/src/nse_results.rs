//! PM1: NSE's two results feeds (Regulation 33 to Dec 2024, Integrated Filing after) and their
//! XBRL. Pure: bytes in, records out.

use std::collections::HashMap;
use std::str::FromStr as _;

use chrono::{Datelike as _, NaiveDate, NaiveDateTime, NaiveTime};
use regex::Regex;
use rust_decimal::Decimal;

use crate::errors::ProviderError;
use crate::nse::{Row, clean, filings_rows, json_context_error};

/// `fundamental_pit.source` of a Regulation 33 filing.
pub const SOURCE_REG33: &str = "NSE_REG33";
/// `fundamental_pit.source` of an Integrated Filing.
pub const SOURCE_INTEGRATED: &str = "NSE_INTEGRATED";

/// One filing a listing names.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResultsFiling {
    /// `period_end`.
    pub period_end: NaiveDate,
    /// `consolidated`.
    pub consolidated: bool,
    /// `filed_at`.
    pub filed_at: NaiveDateTime,
    /// `is_revision`.
    pub is_revision: bool,
    /// `source`.
    pub source: &'static str,
    /// `xbrl_url`.
    pub xbrl_url: String,
}

/// One quarter's figures, in rupees, from the filing's XBRL.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ResultsFacts {
    /// `period_start`.
    pub period_start: Option<NaiveDate>,
    /// `period_end`.
    pub period_end: Option<NaiveDate>,
    /// `revenue`.
    pub revenue: Option<Decimal>,
    /// `other_income`.
    pub other_income: Option<Decimal>,
    /// `finance_costs`.
    pub finance_costs: Option<Decimal>,
    /// `depreciation`.
    pub depreciation: Option<Decimal>,
    /// `pbt`.
    pub pbt: Option<Decimal>,
    /// `pat`.
    pub pat: Option<Decimal>,
    /// `eps_diluted`.
    pub eps_diluted: Option<Decimal>,
    /// `paid_up`.
    pub paid_up: Option<Decimal>,
    /// `face_value`.
    pub face_value: Option<Decimal>,
    /// `total_debt`.
    pub total_debt: Option<Decimal>,
    /// `total_equity`.
    pub total_equity: Option<Decimal>,
    /// `cfo`.
    pub cfo: Option<Decimal>,
    /// `capex`.
    pub capex: Option<Decimal>,
}

impl ResultsFacts {
    /// PBT + finance costs + depreciation − other income; `None` if any part is missing.
    #[must_use]
    pub fn ebitda(&self) -> Option<Decimal> {
        Some(self.pbt? + self.finance_costs? + self.depreciation? - self.other_income?)
    }

    /// Paid-up capital over face value.
    #[must_use]
    pub fn shares_outstanding(&self) -> Option<i64> {
        let face = self.face_value.filter(|f| !f.is_zero())?;
        i64::try_from((self.paid_up? / face).round()).ok()
    }
}

/// The first session whose close can know a filing: a stamp at or after 15:30 IST is next day's.
#[must_use]
pub fn available_on(filed_at: NaiveDateTime) -> NaiveDate {
    let close = NaiveTime::from_hms_opt(15, 30, 0).unwrap_or_default();
    if filed_at.time() >= close {
        filed_at.date().succ_opt().unwrap_or(filed_at.date())
    } else {
        filed_at.date()
    }
}

fn stamp(row: &Row, keys: &[&str]) -> Option<NaiveDateTime> {
    keys.iter().find_map(|k| {
        let text = clean(row.get(k))?;
        ["%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"]
            .iter()
            .find_map(|f| NaiveDateTime::parse_from_str(&text, f).ok())
    })
}

fn day(row: &Row, key: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(&clean(row.get(key))?, "%d-%b-%Y").ok()
}

fn url(row: &Row) -> Option<String> {
    clean(row.get("xbrl")).filter(|u| u.ends_with(".xml"))
}

/// The Regulation 33 listing. A row with no filing stamp or no XBRL is not a usable filing.
///
/// # Errors
/// `UnexpectedPayload` for unreadable JSON.
pub fn parse_reg33_listing(
    payload: &[u8],
    symbol: &str,
) -> Result<Vec<ResultsFiling>, ProviderError> {
    let rows = filings_rows(payload, &format!("{symbol} financial results"))?;
    Ok(rows
        .iter()
        .filter_map(|row| {
            Some(ResultsFiling {
                period_end: day(row, "toDate")?,
                consolidated: clean(row.get("consolidated"))? == "Consolidated",
                filed_at: stamp(row, &["broadCastDate", "exchdisstime", "filingDate"])?,
                is_revision: false,
                source: SOURCE_REG33,
                xbrl_url: url(row)?,
            })
        })
        .collect())
}

/// The Integrated Filing listing. A revision has no broadcast stamp; its revision stamp is when
/// the market could first read it.
///
/// # Errors
/// `UnexpectedPayload` for unreadable JSON.
pub fn parse_integrated_listing(
    payload: &[u8],
    symbol: &str,
) -> Result<Vec<ResultsFiling>, ProviderError> {
    let rows = filings_rows(payload, &format!("{symbol} integrated filings"))?;
    Ok(rows
        .iter()
        .filter_map(|row| {
            Some(ResultsFiling {
                period_end: day(row, "qe_Date")?,
                consolidated: clean(row.get("consolidated"))? == "Consolidated",
                filed_at: stamp(row, &["broadcast_Date", "revised_Date", "creation_Date"])?,
                is_revision: clean(row.get("type_Sub")).is_some_and(|t| t != "Original"),
                source: SOURCE_INTEGRATED,
                xbrl_url: url(row)?,
            })
        })
        .collect())
}

fn number(text: &str) -> Option<Decimal> {
    let t = text.trim();
    Decimal::from_str(t)
        .or_else(|_| Decimal::from_scientific(t))
        .ok()
}

fn quarter_start(end: NaiveDate) -> Option<NaiveDate> {
    let months = end.year() * 12 + i32::try_from(end.month0()).ok()? - 2;
    NaiveDate::from_ymd_opt(
        months.div_euclid(12),
        u32::try_from(months.rem_euclid(12)).ok()? + 1,
        1,
    )
}

/// The quarter's figures, or `None` when `OneD` is not a quarter (half-yearly filers).
///
/// # Errors
/// `UnexpectedPayload` when the bytes are not UTF-8.
pub fn parse_results_xbrl(payload: &[u8]) -> Result<Option<ResultsFacts>, ProviderError> {
    let text = std::str::from_utf8(payload)
        .map_err(|e| json_context_error(format!("results XBRL is not UTF-8: {e}")))?;
    let fact = Regex::new(r#"<in-(?:bse-fin|capmkt):(\w+)\s+contextRef="(\w+)"[^>]*>([^<]*)</"#)
        .map_err(|e| ProviderError::foreign("re.error", e.to_string()))?;
    let mut facts: HashMap<(&str, &str), &str> = HashMap::new();
    for c in fact.captures_iter(text) {
        if let (Some(tag), Some(context), Some(value)) = (c.get(1), c.get(2), c.get(3)) {
            facts
                .entry((tag.as_str(), context.as_str()))
                .or_insert(value.as_str());
        }
    }
    let date = |tag: &str| {
        facts
            .get(&(tag, "OneD"))
            .and_then(|v| NaiveDate::from_str(v.trim()).ok())
    };
    let Some(end) = date("DateOfEndOfReportingPeriod") else {
        return Ok(None);
    };
    // Older banking filings have no start date; "Half yearly" and "Yearly" there are Q2 and Q4,
    // and their OneD column is still the quarter.
    let bank_label = facts
        .get(&("ReportingQuarter", "OneD"))
        .map(|q| q.to_lowercase())
        .is_some_and(|q| q.contains("quarter") || q.contains("year"));
    let start = match date("DateOfStartOfReportingPeriod") {
        Some(start) => start,
        None if bank_label => match quarter_start(end) {
            Some(start) => start,
            None => return Ok(None),
        },
        None => return Ok(None),
    };
    if !(80..=100).contains(&(end - start).num_days()) {
        return Ok(None);
    }
    let in_ctx = |tag: &str, context: &str| facts.get(&(tag, context)).and_then(|v| number(v));
    let quarter = |tag: &str| in_ctx(tag, "OneD");
    // Banks file a different schema; EBITDA has no meaning there and stays NULL.
    let first = |tags: &[&str]| tags.iter().find_map(|t| quarter(t));
    let debt = match (
        in_ctx("BorrowingsCurrent", "OneI"),
        in_ctx("BorrowingsNoncurrent", "OneI"),
    ) {
        (None, None) => None,
        (a, b) => Some(a.unwrap_or_default() + b.unwrap_or_default()),
    };
    Ok(Some(ResultsFacts {
        period_start: Some(start),
        period_end: Some(end),
        revenue: quarter("RevenueFromOperations").or_else(|| quarter("Income")),
        other_income: quarter("OtherIncome"),
        finance_costs: quarter("FinanceCosts"),
        depreciation: quarter("DepreciationDepletionAndAmortisationExpense"),
        pbt: first(&[
            "ProfitBeforeTax",
            "ProfitLossFromOrdinaryActivitiesBeforeTax",
        ]),
        pat: first(&[
            "ProfitLossForPeriod",
            "ProfitLossForThePeriod",
            "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
        ]),
        eps_diluted: first(&[
            "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "DilutedEarningsPerShareAfterExtraordinaryItems",
        ]),
        paid_up: quarter("PaidUpValueOfEquityShareCapital"),
        face_value: quarter("FaceValueOfEquityShareCapital"),
        total_debt: debt,
        total_equity: in_ctx("Equity", "OneI"),
        cfo: in_ctx("CashFlowsFromUsedInOperatingActivities", "FourD"),
        capex: in_ctx(
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
            "FourD",
        ),
    }))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::*;

    fn at(text: &str) -> NaiveDateTime {
        NaiveDateTime::parse_from_str(text, "%Y-%m-%d %H:%M:%S").unwrap()
    }

    #[test]
    fn a_filing_after_the_close_belongs_to_the_next_day() {
        let on = |d: &str| NaiveDate::from_str(d).unwrap();
        assert_eq!(available_on(at("2025-01-16 15:29:59")), on("2025-01-16"));
        assert_eq!(available_on(at("2025-01-16 15:30:00")), on("2025-01-17"));
        assert_eq!(available_on(at("2025-01-16 20:20:21")), on("2025-01-17"));
    }

    #[test]
    fn the_quarter_column_is_read_not_the_year_to_date() {
        let xml = br#"<x>
<in-bse-fin:DateOfStartOfReportingPeriod contextRef="OneD">2024-10-01</in-bse-fin:DateOfStartOfReportingPeriod>
<in-bse-fin:DateOfEndOfReportingPeriod contextRef="OneD">2024-12-31</in-bse-fin:DateOfEndOfReportingPeriod>
<in-bse-fin:RevenueFromOperations contextRef="FourD" unitRef="INR" decimals="-7">300.00</in-bse-fin:RevenueFromOperations>
<in-bse-fin:RevenueFromOperations contextRef="OneD" unitRef="INR" decimals="-7">100.00</in-bse-fin:RevenueFromOperations>
</x>"#;
        let facts = parse_results_xbrl(xml).unwrap().unwrap();
        assert_eq!(facts.revenue, Some(Decimal::from(100)));
    }

    #[test]
    fn a_half_yearly_filing_is_not_a_quarter() {
        let xml = br#"<x>
<in-bse-fin:DateOfStartOfReportingPeriod contextRef="OneD">2024-04-01</in-bse-fin:DateOfStartOfReportingPeriod>
<in-bse-fin:DateOfEndOfReportingPeriod contextRef="OneD">2024-09-30</in-bse-fin:DateOfEndOfReportingPeriod>
</x>"#;
        assert_eq!(parse_results_xbrl(xml).unwrap(), None);
    }

    #[test]
    fn a_bank_filing_without_a_start_date_is_still_its_quarter() {
        let xml = br#"<x>
<in-bse-fin:ReportingQuarter contextRef="OneD">First quarter</in-bse-fin:ReportingQuarter>
<in-bse-fin:DateOfEndOfReportingPeriod contextRef="OneD">2018-06-30</in-bse-fin:DateOfEndOfReportingPeriod>
<in-bse-fin:ProfitLossForThePeriod contextRef="OneD" unitRef="INR" decimals="-7">46.00</in-bse-fin:ProfitLossForThePeriod>
</x>"#;
        let facts = parse_results_xbrl(xml).unwrap().unwrap();
        assert_eq!(facts.period_start, NaiveDate::from_ymd_opt(2018, 4, 1));
        assert_eq!(facts.pat, Some(Decimal::from(46)));
        let yearly = String::from_utf8_lossy(xml).replace("First quarter", "Yearly");
        assert!(parse_results_xbrl(yearly.as_bytes()).unwrap().is_some());
    }

    #[test]
    fn a_revision_is_stamped_when_it_was_revised() {
        let json = br#"{"data":[{"qe_Date":"31-MAR-2026","consolidated":"Standalone","type_Sub":"Revision",
"broadcast_Date":null,"revised_Date":"11-JUL-2026 14:18:12","creation_Date":"11-Jul-2026 14:18:58",
"xbrl":"https://a/b.xml"}]}"#;
        let got = parse_integrated_listing(json, "X").unwrap();
        assert_eq!(got[0].filed_at, at("2026-07-11 14:18:12"));
        assert!(got[0].is_revision);
    }
}
