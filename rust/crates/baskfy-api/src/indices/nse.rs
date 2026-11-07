//! Constituent lists from niftyindices.com: one request at a time, at least
//! [`MIN_GAP`] apart, a browser UA and Referer, and a silence after a 403/429.
//! niftyindices answers a wrong filename with a 200 HTML page, so only a CSV header is trusted.

use std::sync::LazyLock;
use std::time::{Duration, Instant};

use baskfy_providers::net::{GuardedClient, client_builder};
use tokio::sync::Mutex;

use super::CONSTITUENTS_MAX_AGE_DAYS;

/// The constituent CSV host.
pub const BASE: &str = "https://www.niftyindices.com/IndexConstituent";
/// Minimum gap between two requests.
pub const MIN_GAP: Duration = Duration::from_secs(3);
/// Silence after a 403/429.
pub const BACKOFF: Duration = Duration::from_secs(15 * 60);
const UA: &str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 \
                  (KHTML, like Gecko) Chrome/124.0 Safari/537.36";
const REFERER: &str = "https://www.niftyindices.com/";

/// `ACRONYMS`: kept whole in a camel-case filename (`ind_niftyNBFC_list.csv`); from the desk.
const ACRONYMS: [&str; 21] = [
    "IT", "NBFC", "FMCG", "PSU", "PSE", "CPSE", "ESG", "MNC", "EV", "IPO", "SME", "FPI", "USD",
    "TR", "PR", "MAATR", "REIT", "REITS", "INVIT", "INVITS", "GS",
];

struct Gate {
    last: Option<Instant>,
    blocked_until: Option<Instant>,
}

static GATE: LazyLock<Mutex<Gate>> = LazyLock::new(|| {
    Mutex::new(Gate {
        last: None,
        blocked_until: None,
    })
});

/// One constituent row.
#[derive(Debug, Clone)]
pub struct Constituent {
    /// `Symbol`.
    pub symbol: String,
    /// `Company Name`.
    pub name: String,
    /// `ISIN Code`.
    pub isin: String,
    /// `Industry` (NSE's industry; empty when the file has none).
    pub industry: String,
    /// `Weightage` when the file carries one.
    pub weight: Option<rust_decimal::Decimal>,
}

fn py_title(p: &str) -> String {
    let mut out = String::with_capacity(p.len());
    let mut prev = false;
    for ch in p.chars() {
        if ch.is_alphabetic() {
            if prev {
                out.extend(ch.to_lowercase());
            } else {
                out.extend(ch.to_uppercase());
            }
            prev = true;
        } else {
            out.push(ch);
            prev = false;
        }
    }
    out
}

fn py_isupper(p: &str) -> bool {
    let mut cased = false;
    for ch in p.chars() {
        if ch.is_lowercase() {
            return false;
        }
        if ch.is_uppercase() {
            cased = true;
        }
    }
    cased
}

/// `camel(name)`: `"Nifty Sugar & Ethanol"` -> `"niftySugarEthanol"` (the desk's rule).
#[must_use]
pub fn camel(name: &str) -> String {
    let parts: Vec<&str> = name
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|p| !p.is_empty())
        .collect();
    let mut out = String::new();
    for (i, p) in parts.iter().enumerate() {
        if i == 0 {
            out.push_str(&p.to_lowercase());
        } else if py_isupper(p) && !ACRONYMS.contains(p) {
            out.push_str(&py_title(p));
        } else {
            let mut chars = p.chars();
            if let Some(first) = chars.next() {
                out.extend(first.to_uppercase());
                out.push_str(chars.as_str());
            }
        }
    }
    out
}

/// niftyindices filenames that no rule derives (stems, without `.csv`).
const KNOWN: &[(&str, &str)] = &[
    ("Nifty Financial Services", "ind_niftyfinancelist"),
    ("Nifty Private Bank", "ind_nifty_privatebanklist"),
    ("Nifty Financial Services 25/50", "ind_niftyfinancialservices25-50list"),
    ("Nifty MidSmall Financial Services", "ind_niftymidsmallfinancailservice_list"),
    ("Nifty MidSmall IT & Telecom", "ind_niftymidsmallitAndtelecom_list"),
    ("Nifty India Consumption", "ind_niftyconsumptionlist"),
    ("Nifty Infrastructure", "ind_niftyinfralist"),
    ("Nifty Services Sector", "ind_niftyservicelist"),
    ("Nifty Midcap Liquid 15", "ind_Nifty_Midcap_Liquid15"),
    ("Nifty100 Liquid 15", "ind_Nifty100_Liquid15"),
    ("Nifty Shariah 25", "ind_niftyshariah25list"),
    ("Nifty50 Shariah", "ind_nifty50shariahlist"),
    ("Nifty500 Shariah", "ind_nifty500shariahlist"),
    ("Nifty SME EMERGE", "ind_niftysmelist"),
    ("Nifty100 ESG", "ind_nifty100esglist"),
    ("Nifty100 Enhanced ESG", "ind_nifty100enhancedesglist"),
    ("Nifty100 ESG Sector Leaders", "ind_nifty100esgsecleaderslist"),
    ("Nifty Non-Cyclical Consumer", "ind_niftynon-cyclicalconsumer_list"),
    ("Nifty Housing", "ind_niftyhousing_list"),
    ("Nifty Transportation & Logistics", "ind_niftytransportationandlogistics_list"),
    ("Nifty EV & New Age Automotive", "ind_niftyEv_NewAgeAutomotive_list"),
    ("Nifty India Infrastructure & Logistics", "ind_niftyIndiaInfrastructure_Logistics_list"),
    ("Nifty REITs & InvITs", "ind_niftyREITs_InvITs_list"),
    ("Nifty 100 Equal Weight", "ind_nifty100list"),
    ("Nifty50 Equal weight", "ind_Nifty50EqualWeight"),
    ("Nifty Alpha 50", "ind_nifty_Alpha_Index"),
    ("Nifty High Beta 50", "nifty_High_Beta50_Index"),
    ("Nifty Low Volatility 50", "nifty_Low_Volatility50_Index"),
    ("Nifty Dividend Opportunities 50", "ind_niftydivopp50list"),
    ("Nifty Alpha Low-Volatility 30", "ind_nifty_alpha_lowvol30list"),
    ("Nifty Quality Low-Volatility 30", "ind_nifty_quality_lowvol30list"),
    ("Nifty Alpha Quality Low-Volatility 30", "ind_nifty_alpha_quality_lowvol30list"),
    ("Nifty Alpha Quality Value Low-Volatility 30", "ind_nifty_alpha_quality_value_lowvol30list"),
    ("Nifty Growth Sectors 15", "ind_NiftyGrowth_Sectors15_Index"),
    ("Nifty50 Value 20", "ind_Nifty50_Value20"),
    ("Nifty Smallcap250 Quality 50", "ind_niftySmallcap250_Quality50_list"),
    ("Nifty India Corporate Group Index - Tata Group", "ind_nifty_tatalist"),
    ("Nifty India Corporate Group Index - Tata Group 25% Cap", "ind_nifty_tata25caplist"),
    ("Nifty India Corporate Group Index - Mahindra Group", "ind_nifty_mahindralist"),
    ("Nifty India Corporate Group Index - Aditya Birla Group", "ind_nifty_adityabirlalist"),
    ("Nifty Small Finance Banks & Microfinance Institutions", "ind_niftySFBsMfis_list"),
    // Built on Nifty 50 and publish no list of their own.
    ("Nifty50 USD", "ind_nifty50list"),
    ("Nifty50 PR 2x Leverage", "ind_nifty50list"),
    ("Nifty50 PR 1x Inverse", "ind_nifty50list"),
    ("Nifty50 TR 2x Leverage", "ind_nifty50list"),
    ("Nifty50 TR 1x Inverse", "ind_nifty50list"),
    ("Nifty 50 Arbitrage", "ind_nifty50list"),
    ("Nifty 50 Futures PR", "ind_nifty50list"),
    ("Nifty 50 Futures TR", "ind_nifty50list"),
    ("Nifty 50 Dividend Points", "ind_nifty50list"),
    ("Nifty Equity Savings", "ind_nifty50list"),
    ("Nifty50 and Short Duration Debt – Dynamic P/B", "ind_nifty50list"),
    ("Nifty50 and Short Duration Debt – Dynamic P/E", "ind_nifty50list"),
];

/// Filename stems to try: the persisted one, lowercase, then the two camel spellings.
#[must_use]
pub fn candidates(name: &str, persisted: Option<&str>) -> Vec<String> {
    let c = camel(name);
    let mut out: Vec<String> = Vec::new();
    for s in [
        persisted.map(str::to_owned),
        KNOWN.iter().find(|(n, _)| *n == name).map(|(_, f)| (*f).to_owned()),
        Some(super::master::csv_slug(name)),
        Some(format!("ind_{c}list")),
        Some(format!("ind_{c}_list")),
    ]
    .into_iter()
    .flatten()
    {
        if !out.contains(&s) {
            out.push(s);
        }
    }
    out
}

/// Parse a constituent CSV (header `Company Name,Industry,Symbol,Series,ISIN Code`).
#[must_use]
pub fn parse(text: &str) -> Vec<Constituent> {
    let mut rdr = csv::ReaderBuilder::new()
        .flexible(true)
        .from_reader(text.as_bytes());
    let Ok(headers) = rdr.headers().cloned() else {
        return Vec::new();
    };
    let pos = |k: &str| headers.iter().position(|h| h.trim().eq_ignore_ascii_case(k));
    let (Some(sym), name, isin) = (pos("Symbol"), pos("Company Name").or_else(|| pos("Company")), pos("ISIN Code")) else {
        return Vec::new();
    };
    let industry = pos("Industry");
    let weight = headers
        .iter()
        .position(|h| h.trim().to_lowercase().starts_with("weight"));
    let mut out = Vec::new();
    for rec in rdr.records().flatten() {
        let get = |i: Option<usize>| {
            i.and_then(|i| rec.get(i))
                .unwrap_or_default()
                .trim()
                .to_owned()
        };
        let symbol = get(Some(sym)).to_uppercase();
        if symbol.is_empty() {
            continue;
        }
        out.push(Constituent {
            symbol,
            name: get(name),
            isin: get(isin),
            industry: get(industry),
            weight: get(weight)
                .parse::<rust_decimal::Decimal>()
                .ok()
                .map(|d| d.round_dp(4)),
        });
    }
    out
}

pub(crate) fn client() -> Result<GuardedClient, String> {
    GuardedClient::from_builder(client_builder().timeout(Duration::from_secs(20)))
        .map_err(|e| e.to_string())
}

/// One throttled GET. `Ok((status, body))`; `Err` for a transport failure or the backoff.
async fn get(client: &GuardedClient, url: &str) -> Result<(u16, String), String> {
    let mut gate = GATE.lock().await;
    if let Some(until) = gate.blocked_until {
        if Instant::now() < until {
            return Err("niftyindices backoff in force".into());
        }
        gate.blocked_until = None;
    }
    if let Some(last) = gate.last {
        let since = last.elapsed();
        if since < MIN_GAP {
            tokio::time::sleep(MIN_GAP - since).await;
        }
    }
    let mut attempt = 0_u32;
    loop {
        gate.last = Some(Instant::now());
        let sent = client
            .get(url)
            .map_err(|e| e.to_string())?
            .header("User-Agent", UA)
            .header("Referer", REFERER)
            .header("Accept", "text/csv,text/plain,*/*")
            .send()
            .await;
        match sent {
            Ok(r) => {
                let status = r.status().as_u16();
                if status == 403 || status == 429 {
                    gate.blocked_until = Some(Instant::now() + BACKOFF);
                    return Ok((status, String::new()));
                }
                if status >= 500 && attempt < 2 {
                    attempt += 1;
                    tokio::time::sleep(MIN_GAP * (2_u32.pow(attempt))).await;
                    continue;
                }
                let body = r.text().await.map_err(|e| e.to_string())?;
                return Ok((status, body));
            }
            Err(e) if attempt < 2 => {
                attempt += 1;
                tokio::time::sleep(MIN_GAP * (2_u32.pow(attempt))).await;
                drop(e);
            }
            Err(e) => return Err(e.to_string()),
        }
    }
}

/// Fetch one index's list: `(stem that answered, rows)`.
///
/// # Errors
/// The backoff, a 403/429, or no candidate answering a CSV.
pub async fn fetch(name: &str, persisted: Option<&str>) -> Result<(String, Vec<Constituent>), String> {
    let client = client()?;
    let mut last = String::new();
    for stem in candidates(name, persisted) {
        let url = format!("{BASE}/{stem}.csv");
        let (status, body) = get(&client, &url).await?;
        if status == 403 || status == 429 {
            return Err(format!("niftyindices HTTP {status}"));
        }
        if status == 200 && body.trim_start().starts_with("Company") {
            let rows = parse(&body);
            if !rows.is_empty() {
                return Ok((stem, rows));
            }
        }
        last = format!("{stem} -> HTTP {status}");
    }
    Err(format!("no constituent list for {name} ({last})"))
}

/// Replace one index's constituents and stamp the master row.
///
/// # Errors
/// The database's, as text.
pub async fn write(
    conn: &mut sqlx::PgConnection,
    index_id: i16,
    stem: &str,
    rows: &[Constituent],
) -> Result<(), String> {
    let today = super::now_ist().date_naive();
    sqlx::query("DELETE FROM index_constituents WHERE index_id = $1")
        .bind(index_id)
        .execute(&mut *conn)
        .await
        .map_err(|e| e.to_string())?;
    let mut seen = std::collections::HashSet::new();
    let rows: Vec<&Constituent> = rows.iter().filter(|r| seen.insert(r.symbol.clone())).collect();
    sqlx::query(
        "INSERT INTO index_constituents (index_id, symbol, isin, name, weight, as_of, industry) \
         SELECT $1, t.symbol, t.isin, t.name, t.weight, $6, NULLIF(t.industry, '') \
         FROM UNNEST($2::text[], $3::text[], $4::text[], $5::numeric[], $7::text[]) \
         AS t(symbol, isin, name, weight, industry)",
    )
    .bind(index_id)
    .bind(rows.iter().map(|r| r.symbol.clone()).collect::<Vec<_>>())
    .bind(rows.iter().map(|r| r.isin.clone()).collect::<Vec<_>>())
    .bind(rows.iter().map(|r| r.name.clone()).collect::<Vec<_>>())
    .bind(rows.iter().map(|r| r.weight).collect::<Vec<_>>())
    .bind(today)
    .bind(rows.iter().map(|r| r.industry.clone()).collect::<Vec<_>>())
    .execute(&mut *conn)
    .await
    .map_err(|e| e.to_string())?;
    sqlx::query("UPDATE index_master SET last_constituents_at = now(), nse_csv_slug = $2 WHERE id = $1")
        .bind(index_id)
        .bind(stem)
        .execute(&mut *conn)
        .await
        .map_err(|e| e.to_string())?;
    Ok(())
}

/// The master rows whose list is missing or older than the cut-off (all with `force`).
///
/// # Errors
/// The database's, as text.
pub async fn due(
    conn: &mut sqlx::PgConnection,
    force: bool,
) -> Result<Vec<(i16, String, Option<String>)>, String> {
    sqlx::query_as(
        "SELECT id, name, nse_csv_slug FROM index_master \
         WHERE $1 OR last_constituents_at IS NULL \
            OR last_constituents_at < now() - make_interval(days => $2) \
         ORDER BY sort_order",
    )
    .bind(force)
    .bind(i32::try_from(CONSTITUENTS_MAX_AGE_DAYS).unwrap_or(7))
    .fetch_all(&mut *conn)
    .await
    .map_err(|e| e.to_string())
}

/// Fetch every due list (network first), returning what to write.
pub async fn fetch_due(
    due: &[(i16, String, Option<String>)],
) -> (Vec<(i16, String, Vec<Constituent>)>, Vec<String>) {
    let mut got = Vec::new();
    let mut failed = Vec::new();
    for (id, name, stem) in due {
        match fetch(name, stem.as_deref()).await {
            Ok((stem, rows)) => got.push((*id, stem, rows)),
            Err(_) => failed.push(name.clone()),
        }
    }
    (got, failed)
}
