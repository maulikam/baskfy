//! `test_fno_ingest.py`'s UDiFF fixture, shared by the pure and the database tests.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_providers::errors::ProviderError;
use chrono::NaiveDate;
use polars::prelude::DataFrame;

use super::ingest::{FoBhavcopyReader, FoRow, read_rows};

pub(crate) fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

pub(crate) fn day() -> NaiveDate {
    d(2030, 1, 2)
}

pub(crate) fn next() -> NaiveDate {
    d(2030, 1, 3)
}

pub(crate) fn jan() -> NaiveDate {
    d(2030, 1, 29)
}

pub(crate) fn feb() -> NaiveDate {
    d(2030, 2, 26)
}

pub(crate) fn mar() -> NaiveDate {
    d(2030, 3, 26)
}

pub(crate) fn nifty_weeklies() -> [NaiveDate; 2] {
    [d(2030, 1, 8), d(2030, 1, 15)]
}

pub(crate) fn nifty_far() -> NaiveDate {
    d(2030, 6, 25)
}

pub(crate) fn banknifty_jan() -> NaiveDate {
    d(2030, 1, 28)
}

const HEADER: &str = "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,\
FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,\
PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,\
TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n";

struct Line<'a> {
    kind: &'a str,
    symbol: &'a str,
    expiry: NaiveDate,
    strike: &'a str,
    opt: &'a str,
    settle: &'a str,
    oi: i64,
    volume: i64,
    lot: i64,
}

fn line(stamp: NaiveDate, l: &Line<'_>) -> String {
    let (s, e) = (stamp.to_string(), l.expiry.to_string());
    format!(
        "{s},{s},FO,NSE,{},1,,{},,{e},{e},{},{},X,99,101,98,{},100,99,100,{},{},0,{},1000000.555,1,F1,{},,,,,\n",
        l.kind, l.symbol, l.strike, l.opt, l.settle, l.settle, l.oi, l.volume, l.lot
    )
}

#[allow(clippy::too_many_arguments)]
fn l<'a>(
    kind: &'a str,
    symbol: &'a str,
    expiry: NaiveDate,
    strike: &'a str,
    opt: &'a str,
    settle: &'a str,
    oi: i64,
    volume: i64,
    lot: i64,
) -> Line<'a> {
    Line {
        kind,
        symbol,
        expiry,
        strike,
        opt,
        settle,
        oi,
        volume,
        lot,
    }
}

/// `udiff_zip(stamp)`'s CSV: 17 rows.
pub(crate) fn udiff_csv(stamp: NaiveDate) -> Vec<u8> {
    let p = "100.00";
    let rows = [
        l("STF", "SBIN", jan(), "", "", "810.355", 1000, 10, 750),
        l("STF", "SBIN", feb(), "", "", p, 0, 0, 750),
        l("STO", "SBIN", jan(), "820", "CE", p, 1000, 10, 750),
        l("STO", "SBIN", feb(), "820", "PE", p, 1000, 10, 750),
        l("STO", "SBIN", mar(), "820", "CE", p, 1000, 10, 750),
        l("STO", "SBIN", jan(), "900", "CE", p, 0, 0, 750),
        l("STO", "SBIN", jan(), "880", "CE", p, 0, 5, 750),
        l("IDF", "NIFTY", jan(), "", "", p, 1000, 10, 65),
        l(
            "IDO",
            "NIFTY",
            nifty_weeklies()[0],
            "25000",
            "CE",
            p,
            1000,
            10,
            65,
        ),
        l(
            "IDO",
            "NIFTY",
            nifty_weeklies()[1],
            "25000",
            "PE",
            p,
            1000,
            10,
            65,
        ),
        l("IDO", "NIFTY", jan(), "25000", "CE", p, 1000, 10, 65),
        l("IDO", "NIFTY", feb(), "25000", "CE", p, 1000, 10, 65),
        l("IDO", "NIFTY", nifty_far(), "25000", "CE", p, 1000, 10, 65),
        l(
            "IDO",
            "BANKNIFTY",
            banknifty_jan(),
            "55000",
            "CE",
            p,
            1000,
            10,
            30,
        ),
        l(
            "IDO",
            "BANKNIFTY",
            d(2030, 1, 21),
            "55000",
            "PE",
            p,
            1000,
            10,
            30,
        ),
        l(
            "STO",
            "IDEA",
            d(2030, 1, 21),
            "8",
            "CE",
            p,
            1000,
            10,
            71_475,
        ),
        l("STO", "IDEA", jan(), "8", "CE", p, 1000, 10, 71_475),
    ];
    let mut out = HEADER.to_owned();
    for r in &rows {
        out.push_str(&line(stamp, r));
    }
    out.into_bytes()
}

/// The provider's own parse of a payload for `on` (a wrong stamp is refused, as NSE's is).
pub(crate) fn parse(payload: &[u8], on: NaiveDate) -> Result<DataFrame, ProviderError> {
    let frame = baskfy_providers::nse::read_csv(payload, "fo bhavcopy")?;
    baskfy_providers::nse::fo_bhavcopy_to_frame(frame, on)
}

pub(crate) fn day_rows() -> Vec<FoRow> {
    read_rows(&parse(&udiff_csv(day()), day()).unwrap()).unwrap()
}

/// A reader serving one stamped file and an optional ban file; counts its file reads.
pub(crate) struct FileReader {
    pub(crate) stamp: Option<NaiveDate>,
    pub(crate) ban: Option<Vec<u8>>,
    pub(crate) reads: std::sync::atomic::AtomicUsize,
}

impl FileReader {
    pub(crate) fn new(stamp: Option<NaiveDate>, ban: Option<Vec<u8>>) -> Self {
        Self {
            stamp,
            ban,
            reads: std::sync::atomic::AtomicUsize::new(0),
        }
    }

    pub(crate) fn reads(&self) -> usize {
        self.reads.load(std::sync::atomic::Ordering::SeqCst)
    }
}

fn not_found(what: &str) -> ProviderError {
    ProviderError::unexpected_payload(format!("GET .../{what} returned 404")).with_provider("nse")
}

impl FoBhavcopyReader for FileReader {
    async fn fo_bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError> {
        self.reads.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        match self.stamp {
            Some(stamp) => parse(&udiff_csv(stamp), on),
            None => Err(not_found(&on.to_string())),
        }
    }

    async fn fo_ban_list(&self, for_session: NaiveDate) -> Result<Vec<String>, ProviderError> {
        self.reads.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let Some(payload) = &self.ban else {
            return Err(not_found("no ban file"));
        };
        let (stamp, symbols) = baskfy_providers::nse::parse_fo_ban_list(payload)?;
        if stamp != for_session {
            return Err(ProviderError::unexpected_payload(format!(
                "wrong file: ban list for {stamp}, asked for {for_session}"
            ))
            .with_provider("nse"));
        }
        Ok(symbols)
    }
}

/// `ban_file(stamp, *symbols)`.
pub(crate) fn ban_file(stamp: &str, symbols: &[&str]) -> Vec<u8> {
    let mut lines = vec![format!("Securities in Ban For Trade Date {stamp}:")];
    for (i, s) in symbols.iter().enumerate() {
        lines.push(format!("{},{s}", i + 1));
    }
    format!("{}\n", lines.join("\n")).into_bytes()
}
