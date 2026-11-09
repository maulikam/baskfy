//! PM0.7: every cash-equity print in a daily bhavcopy, keyed by ISIN, from either file layout
//! (`cm02JAN2012bhav.csv` to 2023, UDiFF from 2024). Pure: bytes in, records out.

use std::str::FromStr as _;

use chrono::NaiveDate;
use rust_decimal::Decimal;

use crate::errors::ProviderError;
use crate::nse::json_context_error;

/// NSE's list of delisted companies as `symbol,status,reason` (`reference/nse_delisted.csv`).
pub const DELISTED_REASONS: &str = include_str!("reference/nse_delisted.csv");

/// Every scheme, merger or amalgamation NSE lists, as `issuer,ex_date,subject`
/// (`reference/nse_schemes.csv`).
pub const SCHEMES: &str = include_str!("reference/nse_schemes.csv");

/// A scheme explains a stop when its ex-date falls this long before the last bar...
const SCHEME_BEFORE_LAST_BAR_DAYS: i64 = 30;
/// ...or this long after it: trading stops ahead of the record date, sometimes months ahead.
const SCHEME_AFTER_LAST_BAR_DAYS: i64 = 180;

/// The scheme, if any, that explains why a name's bars ended on `last`. A demerger years earlier
/// falls outside the window: the name kept trading after it.
#[must_use]
pub fn explaining_scheme(last: NaiveDate, events: &[(NaiveDate, String)]) -> Option<&str> {
    events
        .iter()
        .find(|(ex, _)| {
            let days = (*ex - last).num_days();
            (-SCHEME_BEFORE_LAST_BAR_DAYS..=SCHEME_AFTER_LAST_BAR_DAYS).contains(&days)
        })
        .map(|(_, subject)| subject.as_str())
}

/// Stopped at or above this close, with [`HEALTHY_TURNOVER_RUPEES`] a day: absorbed, not failed.
const HEALTHY_LAST_CLOSE: i64 = 50;
const HEALTHY_TURNOVER_RUPEES: i64 = 10_000_000;

/// NSE's files do not label most mergers (HDFC, Mindtree, Ranbaxy); a name that failed has
/// usually decayed in price and volume long before its bars end. `recent` is (close, volume).
#[must_use]
pub fn healthy_at_stop(last_close: Decimal, recent: &[(Decimal, i64)]) -> bool {
    if recent.is_empty() || last_close < Decimal::from(HEALTHY_LAST_CLOSE) {
        return false;
    }
    let total: Decimal = recent.iter().map(|(c, v)| *c * Decimal::from(*v)).sum();
    total / Decimal::from(recent.len()) >= Decimal::from(HEALTHY_TURNOVER_RUPEES)
}

/// One listed company's print. Only `INE` ISINs in series EQ, BE or BZ are kept: ETFs and funds
/// are `INF`, partly paid `IN9`, and SM/ST are the SME board.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BhavRow {
    /// Trading symbol that day.
    pub symbol: String,
    /// EQ, BE or BZ.
    pub series: String,
    /// ISIN that day.
    pub isin: String,
    /// Raw open.
    pub open: Decimal,
    /// Raw high.
    pub high: Decimal,
    /// Raw low.
    pub low: Decimal,
    /// Raw close.
    pub close: Decimal,
    /// NSE's previous close, restated on an ex-date.
    pub prev_close: Option<Decimal>,
    /// Shares traded.
    pub volume: i64,
}

/// The issuer part of an ISIN (`INE002A`): stable across a split or a rename, which change the
/// serial digits or the symbol, and different when a symbol is reused by another company.
#[must_use]
pub fn issuer(isin: &str) -> &str {
    isin.get(..7).unwrap_or(isin)
}

fn number(record: &csv::StringRecord, at: Option<usize>) -> Option<Decimal> {
    Decimal::from_str(record.get(at?)?.trim()).ok()
}

/// Rows of one day's CSV.
///
/// # Errors
/// `UnexpectedPayload` when the file has no symbol or ISIN column.
pub fn parse_bhav_rows(payload: &[u8]) -> Result<Vec<BhavRow>, ProviderError> {
    let mut reader = csv::ReaderBuilder::new()
        .flexible(true)
        .from_reader(payload);
    let header = reader
        .headers()
        .map_err(|e| json_context_error(format!("bhavcopy header: {e}")))?
        .clone();
    let col = |names: &[&str]| header.iter().position(|h| names.contains(&h.trim()));
    let (Some(symbol_at), Some(series_at), Some(isin_at)) = (
        col(&["TckrSymb", "SYMBOL"]),
        col(&["SctySrs", "SERIES"]),
        col(&["ISIN"]),
    ) else {
        return Err(json_context_error(
            "bhavcopy has no symbol, series or ISIN column".to_owned(),
        ));
    };
    let (open, high, low, close, prev, volume) = (
        col(&["OpnPric", "OPEN"]),
        col(&["HghPric", "HIGH"]),
        col(&["LwPric", "LOW"]),
        col(&["ClsPric", "CLOSE"]),
        col(&["PrvsClsgPric", "PREVCLOSE"]),
        col(&["TtlTradgVol", "TOTTRDQTY"]),
    );
    let mut rows = Vec::new();
    for record in reader.records().flatten() {
        let field = |at: usize| record.get(at).map_or("", str::trim).to_owned();
        let (symbol, series, isin) = (field(symbol_at), field(series_at), field(isin_at));
        if !matches!(series.as_str(), "EQ" | "BE" | "BZ") || !isin.starts_with("INE") {
            continue;
        }
        let (Some(o), Some(h), Some(l), Some(c), Some(v)) = (
            number(&record, open),
            number(&record, high),
            number(&record, low),
            number(&record, close),
            number(&record, volume),
        ) else {
            continue;
        };
        rows.push(BhavRow {
            symbol,
            series,
            isin,
            open: o,
            high: h,
            low: l,
            close: c,
            prev_close: number(&record, prev),
            volume: i64::try_from(v.trunc()).unwrap_or(0),
        });
    }
    Ok(rows)
}

/// Backward split and bonus factors for one name's bars in date order: where `prev_close` differs
/// from the last close by 5 % or more, NSE restated it for a corporate action. Dividends fall
/// under the threshold and are left alone.
#[must_use]
pub fn implied_factors(closes: &[Decimal], prev_closes: &[Option<Decimal>]) -> Vec<Decimal> {
    let n = closes.len();
    let mut factors = vec![Decimal::ONE; n];
    let threshold = Decimal::new(5, 2);
    let (floor, cap) = (Decimal::new(2, 2), Decimal::from(20));
    for i in (1..n).rev() {
        factors[i - 1] = factors[i];
        if let Some(prev) = prev_closes[i]
            && !closes[i - 1].is_zero()
        {
            let ratio = prev / closes[i - 1];
            if (ratio - Decimal::ONE).abs() >= threshold && ratio >= floor && ratio <= cap {
                factors[i - 1] *= ratio;
            }
        }
    }
    factors
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::*;

    fn d(t: &str) -> Decimal {
        Decimal::from_str(t).unwrap()
    }

    const LEGACY_HEADER: &str = "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,\n";

    #[test]
    fn a_2012_file_keeps_listed_equity_only() {
        let csv = format!(
            "{LEGACY_HEADER}\
ABAN,EQ,380.1,392.0,379.0,390.55,391,377.8,812345,317000000.5,02-JAN-2012,12003,INE421A01028,\n\
NIFTYBEES,EQ,478,480,477,479,479,477,1000,479000,02-JAN-2012,12,INF732E01037,\n\
SOMESME,SM,10,10,10,10,10,10,1000,10000,02-JAN-2012,1,INE999Z01011,\n"
        );
        let got = parse_bhav_rows(csv.as_bytes()).unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].symbol, "ABAN");
        assert_eq!(got[0].close, d("390.55"));
        assert_eq!(got[0].prev_close, Some(d("377.8")));
        assert_eq!(got[0].volume, 812_345);
    }

    #[test]
    fn a_2018_file_with_a_be_series_and_a_bad_row() {
        let csv = format!(
            "{LEGACY_HEADER}\
JPINFRATEC,BE,3.1,3.3,3.0,3.2,3.2,3.05,4000000,12800000,14-JUN-2018,900,INE099J01015,\n\
BROKEN,EQ,-,-,-,-,-,-,0,0,14-JUN-2018,0,INE111B01011,\n"
        );
        let got = parse_bhav_rows(csv.as_bytes()).unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].series, "BE");
    }

    #[test]
    fn a_2024_udiff_file_reads_by_header() {
        let csv = "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd01,Rsvd02,Rsvd03,Rsvd04\n\
2024-01-02,2024-01-02,CM,NSE,STK,304,IN0020170067,SGBOCT25IV,GB,,,,,2.50% GOLDBONDS2025SR-IV,6180.00,6422.91,6131.00,6422.91,6422.91,6131.00,,6422.91,,,23,142629.27,6,F1,1,,,,,\n\
2024-01-02,2024-01-02,CM,NSE,STK,13620,INE242C01024,ANANTRAJ,EQ,,,,,ANANT RAJ LIMITED,303.20,309.00,300.30,304.45,305.00,298.35,,304.45,,,2179168,665273419.60,28472,F1,1,,,,,\n";
        let got = parse_bhav_rows(csv.as_bytes()).unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(
            (got[0].symbol.as_str(), got[0].isin.as_str()),
            ("ANANTRAJ", "INE242C01024")
        );
        assert_eq!(got[0].close, d("304.45"));
        assert_eq!(got[0].volume, 2_179_168);
    }

    #[test]
    fn a_split_keeps_the_issuer_and_a_reused_symbol_does_not() {
        assert_eq!(issuer("INE002A01018"), issuer("INE002A01026"));
        assert_ne!(issuer("INE002A01018"), issuer("INE003A01018"));
    }

    #[test]
    fn only_a_scheme_near_the_last_bar_explains_a_stop() {
        let on = |t: &str| NaiveDate::from_str(t).unwrap();
        let events = vec![
            (on("2015-03-01"), "Demerger".to_owned()),
            (on("2018-07-01"), "Scheme Of Amalgamation".to_owned()),
        ];
        assert_eq!(
            explaining_scheme(on("2018-05-15"), &events),
            Some("Scheme Of Amalgamation")
        );
        assert_eq!(explaining_scheme(on("2017-01-01"), &events), None);
    }

    #[test]
    fn a_priced_and_traded_name_is_healthy_at_its_stop() {
        assert!(healthy_at_stop(d("2724"), &[(d("2724"), 100_000)]));
        assert!(!healthy_at_stop(d("8"), &[(d("8"), 90_000_000)]));
        assert!(!healthy_at_stop(d("60"), &[(d("60"), 100)]));
        assert!(!healthy_at_stop(d("60"), &[]));
    }

    #[test]
    fn a_one_for_five_split_scales_only_the_bars_before_it() {
        let closes = [d("100"), d("102"), d("20.5"), d("21")];
        let prev = [None, Some(d("100")), Some(d("20.4")), Some(d("20.5"))];
        assert_eq!(
            implied_factors(&closes, &prev),
            vec![d("0.2"), d("0.2"), Decimal::ONE, Decimal::ONE]
        );
    }

    #[test]
    fn an_ordinary_gap_or_small_dividend_adjusts_nothing() {
        let closes = [d("100"), d("90")];
        assert_eq!(
            implied_factors(&closes, &[None, Some(d("98"))]),
            vec![Decimal::ONE, Decimal::ONE]
        );
        assert_eq!(
            implied_factors(&closes, &[None, None]),
            vec![Decimal::ONE, Decimal::ONE]
        );
    }
}
