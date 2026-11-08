//! PM2: NSE's shareholding-pattern filings and their XBRL. Pure: bytes in, records out.

use std::collections::HashMap;
use std::str::FromStr as _;

use chrono::{Datelike as _, NaiveDate, NaiveDateTime};
use regex::Regex;
use rust_decimal::Decimal;

use crate::errors::ProviderError;
use crate::nse::{Row, clean, filings_rows, json_context_error};

/// `shareholding_pit.source`.
pub const SOURCE_SHP: &str = "NSE_SHP";

/// One quarter-end filing the listing names, with the feed's own promoter and public totals.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShareholdingFiling {
    /// Quarter end.
    pub period_end: NaiveDate,
    /// When the market could first read it (IST).
    pub filed_at: NaiveDateTime,
    /// A revised filing.
    pub is_revision: bool,
    /// Promoter and promoter group, percent.
    pub promoter_pct: Option<Decimal>,
    /// Public, percent.
    pub public_pct: Option<Decimal>,
    /// The XBRL document.
    pub xbrl_url: String,
}

/// The categories only the XBRL has, in percent.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ShareholdingFacts {
    /// Encumbered share of the promoters' own holding.
    pub pledged_pct: Decimal,
    /// Foreign portfolio investors.
    pub fii_pct: Decimal,
    /// Domestic institutions.
    pub dii_pct: Decimal,
    /// Mutual funds.
    pub mf_pct: Decimal,
    /// Holders in total.
    pub n_shareholders: Option<i64>,
}

fn stamp(row: &Row, keys: &[&str]) -> Option<NaiveDateTime> {
    keys.iter()
        .find_map(|k| NaiveDateTime::parse_from_str(&clean(row.get(k))?, "%d-%b-%Y %H:%M:%S").ok())
}

fn is_quarter_end(d: NaiveDate) -> bool {
    d.month().is_multiple_of(3) && d.succ_opt().is_some_and(|n| n.month() != d.month())
}

fn percent(row: &Row, key: &str) -> Option<Decimal> {
    Decimal::from_str(&clean(row.get(key))?).ok()
}

/// The listing's quarter-end rows. Ad hoc filings (a capital change mid-quarter) are not quarters.
///
/// # Errors
/// `UnexpectedPayload` for unreadable JSON.
pub fn parse_shareholding_listing(
    payload: &[u8],
    symbol: &str,
) -> Result<Vec<ShareholdingFiling>, ProviderError> {
    let rows = filings_rows(payload, &format!("{symbol} shareholding"))?;
    Ok(rows
        .iter()
        .filter_map(|row| {
            let period_end =
                NaiveDate::parse_from_str(&clean(row.get("date"))?, "%d-%b-%Y").ok()?;
            if !is_quarter_end(period_end) {
                return None;
            }
            let is_revision = clean(row.get("revisedData")).is_some_and(|v| v != "N");
            let keys: &[&str] = if is_revision {
                &["revisedDate", "revisionDate", "broadcastDate", "systemDate"]
            } else {
                &["broadcastDate", "systemDate"]
            };
            Some(ShareholdingFiling {
                period_end,
                filed_at: stamp(row, keys)?,
                is_revision,
                promoter_pct: percent(row, "pr_and_prgrp"),
                public_pct: percent(row, "public_val"),
                xbrl_url: clean(row.get("xbrl")).filter(|u| u.ends_with(".xml"))?,
            })
        })
        .collect())
}

/// The XBRL's categories, or `None` when neither schema's total is there.
///
/// Filings report `25.83` or `0.2583` depending on the year; the filing's own total row, 100 or
/// 1, says which.
///
/// # Errors
/// `UnexpectedPayload` when the bytes are not UTF-8.
pub fn parse_shareholding_xbrl(payload: &[u8]) -> Result<Option<ShareholdingFacts>, ProviderError> {
    const SHARE: &str = "ShareholdingAsAPercentageOfTotalNumberOfShares";
    let text = std::str::from_utf8(payload)
        .map_err(|e| json_context_error(format!("shareholding XBRL is not UTF-8: {e}")))?;
    let fact = Regex::new(r#"<in-[a-z-]+:(\w+)\s+contextRef="(\w+)"[^>]*>([^<]*)</"#)
        .map_err(|e| ProviderError::foreign("re.error", e.to_string()))?;
    let mut facts: HashMap<(&str, &str), Decimal> = HashMap::new();
    for c in fact.captures_iter(text) {
        if let (Some(tag), Some(context), Some(value)) = (c.get(1), c.get(2), c.get(3))
            && let Ok(number) = Decimal::from_str(value.as_str().trim())
        {
            facts
                .entry((tag.as_str(), context.as_str()))
                .or_insert(number);
        }
    }
    let total = |c: &str| facts.contains_key(&(SHARE, c));
    let share = |c: &str| facts.get(&(SHARE, c)).copied().unwrap_or_default();
    let count = |c: &str| {
        facts
            .get(&("NumberOfShareholders", c))
            .and_then(|n| i64::try_from(*n).ok())
    };
    let scale = |c: &str| {
        if share(c) <= Decimal::TWO {
            Decimal::ONE_HUNDRED
        } else {
            Decimal::ONE
        }
    };
    if total("ShareholdingPattern_ContextI") {
        let k = scale("ShareholdingPattern_ContextI");
        let pledged = facts
            .get(&(
                "EncumberedSharesHeldAsPercentageOfTotalNumberOfShares",
                "ShareholdingOfPromoterAndPromoterGroup_ContextI",
            ))
            .copied()
            .unwrap_or_default();
        return Ok(Some(ShareholdingFacts {
            pledged_pct: pledged * k,
            fii_pct: (share("InstitutionsForeignPortfolioInvestorCategoryOne_ContextI")
                + share("InstitutionsForeignPortfolioInvestorCategoryTwo_ContextI"))
                * k,
            dii_pct: share("InstitutionsDomestic_ContextI") * k,
            mf_pct: share("MutualFundsOrUTI_ContextI") * k,
            n_shareholders: count("ShareholdingPattern_ContextI"),
        }));
    }
    if total("ShareholdingPatternI") {
        let k = scale("ShareholdingPatternI");
        let fpi = share("InstitutionsForeignPortfolioInvestorI");
        let pledged = facts
            .get(&(
                "PledgedOrEncumberedSharesHeldAsPercentageOfTotalNumberOfShares",
                "ShareholdingOfPromoterAndPromoterGroupI",
            ))
            .copied()
            .unwrap_or_default();
        return Ok(Some(ShareholdingFacts {
            pledged_pct: pledged * k,
            fii_pct: fpi * k,
            dii_pct: (share("InstitutionsI") - fpi - share("ForeignVentureCapitalInvestorsI")) * k,
            mf_pct: share("MutualFundsOrUtiI") * k,
            n_shareholders: count("ShareholdingPatternI"),
        }));
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::*;

    const SHARE: &str = "ShareholdingAsAPercentageOfTotalNumberOfShares";

    fn fact(tag: &str, context: &str, value: &str) -> String {
        format!(
            r#"<in-bse-shp:{tag} contextRef="{context}" decimals="2">{value}</in-bse-shp:{tag}>"#
        )
    }

    fn d(text: &str) -> Decimal {
        Decimal::from_str(text).unwrap()
    }

    #[test]
    fn a_bank_with_no_promoter_on_the_new_schema_reads_in_percent() {
        // HDFCBANK, 30 Sep 2026.
        let xml = [
            fact(SHARE, "ShareholdingPattern_ContextI", "1"),
            fact(
                "NumberOfShareholders",
                "ShareholdingPattern_ContextI",
                "4807998",
            ),
            fact(SHARE, "MutualFundsOrUTI_ContextI", "0.3188"),
            fact(SHARE, "InstitutionsDomestic_ContextI", "0.4363"),
            fact(
                SHARE,
                "InstitutionsForeignPortfolioInvestorCategoryOne_ContextI",
                "0.3762",
            ),
            fact(
                SHARE,
                "InstitutionsForeignPortfolioInvestorCategoryTwo_ContextI",
                "0.0177",
            ),
        ]
        .concat();
        let got = parse_shareholding_xbrl(xml.as_bytes()).unwrap().unwrap();
        assert_eq!(got.mf_pct, d("31.88"));
        assert_eq!(got.dii_pct, d("43.63"));
        assert_eq!(got.fii_pct, d("39.39"));
        assert_eq!(got.pledged_pct, Decimal::ZERO);
        assert_eq!(got.n_shareholders, Some(4_807_998));
    }

    #[test]
    fn an_encumbered_promoter_counts_every_kind_of_encumbrance() {
        // VEDL, 30 Sep 2026: all of it under non-disposal and other encumbrances, none pledged.
        let xml = [
            fact(SHARE, "ShareholdingPattern_ContextI", "1"),
            fact(
                "EncumberedSharesHeldAsPercentageOfTotalNumberOfShares",
                "ShareholdingOfPromoterAndPromoterGroup_ContextI",
                "1",
            ),
            fact(
                "EncumberedSharesHeldAsPercentageOfTotalNumberOfShares",
                "ShareholdingPattern_ContextI",
                "0.5472",
            ),
        ]
        .concat();
        let got = parse_shareholding_xbrl(xml.as_bytes()).unwrap().unwrap();
        assert_eq!(got.pledged_pct, d("100"));
    }

    #[test]
    fn the_old_schema_is_already_in_percent() {
        // RELIANCE, 30 Sep 2021.
        let xml = [
            fact(SHARE, "ShareholdingPatternI", "100"),
            fact("NumberOfShareholders", "ShareholdingPatternI", "3043511"),
            fact(SHARE, "MutualFundsOrUtiI", "4.6"),
            fact(SHARE, "InstitutionsI", "38.61"),
            fact(SHARE, "InstitutionsForeignPortfolioInvestorI", "25.39"),
            fact(SHARE, "ForeignVentureCapitalInvestorsI", "0"),
        ]
        .concat();
        let got = parse_shareholding_xbrl(xml.as_bytes()).unwrap().unwrap();
        assert_eq!(got.mf_pct, d("4.6"));
        assert_eq!(got.fii_pct, d("25.39"));
        assert_eq!(got.dii_pct, d("13.22"));
    }

    #[test]
    fn the_new_contexts_in_percent_are_not_scaled_twice() {
        let xml = [
            fact(SHARE, "ShareholdingPattern_ContextI", "100"),
            fact(SHARE, "MutualFundsOrUTI_ContextI", "31.88"),
        ]
        .concat();
        let got = parse_shareholding_xbrl(xml.as_bytes()).unwrap().unwrap();
        assert_eq!(got.mf_pct, d("31.88"));
    }

    #[test]
    fn a_quote_carries_the_classification_and_a_dash_is_no_level() {
        // UPL, archived quote of 15 Oct 2026.
        let on = NaiveDate::from_ymd_opt(2026, 10, 15).unwrap();
        let json = br#"{"equityResponse":[{"metaData":{"symbol":"UPL"},"tradeInfo":{"issuedSize":100,"lastPrice":10},
"orderBook":{},"secInfo":{"macro":"Commodities","sector":"Chemicals","industryInfo":"Fertilizers & Agrochemicals","basicIndustry":"-"}}]}"#;
        let got = crate::nse::parse_get_symbol_data(json, on, "UPL")
            .unwrap()
            .unwrap();
        let class = got.industry_class.clone().unwrap();
        assert_eq!(class.macro_sector.as_deref(), Some("Commodities"));
        assert_eq!(class.sector.as_deref(), Some("Chemicals"));
        assert_eq!(
            class.industry.as_deref(),
            Some("Fertilizers & Agrochemicals")
        );
        assert_eq!(class.basic_industry, None);
    }

    #[test]
    fn neither_schema_is_no_filing() {
        assert_eq!(parse_shareholding_xbrl(b"<x></x>").unwrap(), None);
    }

    #[test]
    fn an_ad_hoc_filing_is_not_a_quarter_and_a_revision_keeps_its_own_stamp() {
        let json = br#"[
{"date":"08-DEC-2021","broadcastDate":"07-JAN-2022 03:25:01","revisedData":"N","pr_and_prgrp":"50","public_val":"50","xbrl":"https://a/1.xml"},
{"date":"31-MAR-2023","broadcastDate":"24-JUL-2023 16:54:21","revisedData":"Y","revisedDate":null,"pr_and_prgrp":"25.5","public_val":"74.5","xbrl":"https://a/2.xml"}
]"#;
        let got = parse_shareholding_listing(json, "X").unwrap();
        assert_eq!(got.len(), 1);
        assert!(got[0].is_revision);
        assert_eq!(
            got[0].filed_at.date(),
            NaiveDate::from_ymd_opt(2023, 7, 24).unwrap()
        );
        assert_eq!(got[0].promoter_pct, Some(d("25.5")));
    }
}
