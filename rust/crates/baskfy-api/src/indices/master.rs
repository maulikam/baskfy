//! `index_master`: the NSE equity index list joined to Kite's INDICES instruments.

use std::collections::HashMap;

use baskfy_providers::kite::KiteProvider;
use baskfy_providers::records::InstrumentType;
use sqlx::PgConnection;

/// The embedded master list (group -> names). `BASKFY_INDEX_MASTER_PATH` overrides it.
const EMBEDDED: &str = include_str!("nse_equity_indices.json");

/// Group order as the JSON lists them.
pub const GROUPS: [&str; 4] = ["Broad market", "Sectoral", "Thematic", "Strategy"];

/// Master name -> Kite tradingsymbol where normalised names differ (or mislead: Kite's
/// "NIFTY FINANCIAL SERVICES" is the Ex-Bank index). Mirrored in alembic 0060.
pub const ALIASES: [(&str, &str); 53] = [
    ("Nifty Midcap Select", "NIFTY MID SELECT"),
    ("Nifty Smallcap 250", "NIFTY SMLCAP 250"),
    ("Nifty Smallcap 50", "NIFTY SMLCAP 50"),
    ("Nifty Smallcap 100", "NIFTY SMLCAP 100"),
    ("Nifty LargeMidcap 250", "NIFTY LARGEMID250"),
    ("Nifty MidSmallcap 400", "NIFTY MIDSML 400"),
    ("Nifty500 Multicap 50:25:25", "NIFTY500 MULTICAP"),
    ("Nifty Total Market", "NIFTY TOTAL MKT"),
    ("Nifty India FPI 150", "NIFTY FPI 150"),
    ("Nifty Private Bank", "NIFTY PVT BANK"),
    ("Nifty Consumer Durables", "NIFTY CONSR DURBL"),
    ("Nifty Financial Services", "NIFTY FIN SERVICE"),
    ("Nifty Financial Services 25/50", "NIFTY FINSRV25 50"),
    ("Nifty Financial Services Ex-Bank", "NIFTY FINSEREXBNK"),
    ("Nifty MidSmall Healthcare", "NIFTY MIDSML HLTH"),
    ("Nifty500 Healthcare", "NIFTY500 HEALTH"),
    ("Nifty India Consumption", "NIFTY CONSUMPTION"),
    ("Nifty Infrastructure", "NIFTY INFRA"),
    ("Nifty India Manufacturing", "NIFTY INDIA MFG"),
    ("Nifty Midcap Liquid 15", "NIFTY MID LIQ 15"),
    ("Nifty Services Sector", "NIFTY SERV SECTOR"),
    ("Nifty100 Liquid 15", "NIFTY100 LIQ 15"),
    (
        "Nifty India Corporate Group Index - Tata Group 25% Cap",
        "NIFTY TATA 25 CAP",
    ),
    ("Nifty100 ESG Sector Leaders", "NIFTY100ESGSECLDR"),
    ("Nifty India Digital", "NIFTY IND DIGITAL"),
    (
        "Nifty500 Multicap India Manufacturing 50:30:20",
        "NIFTY MULTI MFG",
    ),
    ("Nifty500 Multicap Infrastructure 50:30:20", "NIFTY MULTI INFRA"),
    (
        "Nifty India Select 5 Corporate Groups (MAATR)",
        "NIFTY CORP MAATR",
    ),
    ("Nifty India Railways PSU", "NIFTY RAILWAYS PSU"),
    ("Nifty India Internet", "NIFTY INTERNET"),
    ("Nifty India Infrastructure & Logistics", "NIFTY INFRALOG"),
    ("Nifty Conglomerate 50", "NIFTYCONGLOMERATE"),
    ("Nifty 100 Equal Weight", "NIFTY100 EQL WGT"),
    ("Nifty100 Low Volatility 30", "NIFTY100 LOWVOL30"),
    ("Nifty Dividend Opportunities 50", "NIFTY DIV OPPS 50"),
    ("Nifty Alpha Low-Volatility 30", "NIFTY ALPHALOWVOL"),
    ("Nifty100 Quality 30", "NIFTY100 QUALTY30"),
    ("Nifty Growth Sectors 15", "NIFTY GROWSECT 15"),
    ("Nifty 50 Dividend Points", "NIFTY50 DIV POINT"),
    ("Nifty50 Equal weight", "NIFTY50 EQL WGT"),
    ("Nifty50 PR 1x Inverse", "NIFTY50 PR 1X INV"),
    ("Nifty50 PR 2x Leverage", "NIFTY50 PR 2X LEV"),
    ("Nifty50 TR 1x Inverse", "NIFTY50 TR 1X INV"),
    ("Nifty50 TR 2x Leverage", "NIFTY50 TR 2X LEV"),
    ("Nifty200 Quality 30", "NIFTY200 QUALTY30"),
    ("Nifty Midcap150 Quality 50", "NIFTY M150 QLTY50"),
    ("Nifty200 Momentum 30", "NIFTY200MOMENTM30"),
    ("Nifty Midcap150 Momentum 50", "NIFTYM150MOMNTM50"),
    ("Nifty500 Quality 50", "NIFTY500 QLTY50"),
    ("Nifty500 Low Volatility 50", "NIFTY500 LOWVOL50"),
    ("Nifty500 Multifactor MQVLv 50", "NIFTY500 MQVLV50"),
    ("Nifty500 Flexicap Quality 30", "NIFTY500 FLEXICAP"),
    ("Nifty Total Market Momentum Quality 50", "NIFTY TMMQ 50"),
];

/// One Kite INDICES instrument.
#[derive(Debug, Clone)]
pub struct KiteIndex {
    /// `tradingsymbol`.
    pub symbol: String,
    /// `name`.
    pub name: String,
    /// `instrument_token`.
    pub token: i64,
}

/// Uppercase, `NSE INDEX ` prefix dropped, `&` as AND, every non-alphanumeric stripped.
#[must_use]
pub fn norm(name: &str) -> String {
    let up = name.to_uppercase();
    let up = up.strip_prefix("NSE INDEX ").unwrap_or(&up);
    up.replace('&', " AND ")
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect()
}

/// `ind_` + lowercase alphanumerics + `list` (niftyindices' commonest spelling).
#[must_use]
pub fn csv_slug(name: &str) -> String {
    let stem: String = name
        .to_lowercase()
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect();
    format!("ind_{stem}list")
}

/// The master list as `(group, name)` in order.
///
/// # Errors
/// The override file unreadable, or the JSON malformed.
pub fn master_list() -> Result<Vec<(String, String)>, String> {
    let path = std::env::var("BASKFY_INDEX_MASTER_PATH").unwrap_or_default();
    let text = if path.trim().is_empty() {
        EMBEDDED.to_owned()
    } else {
        std::fs::read_to_string(path.trim()).map_err(|e| format!("index master: {e}"))?
    };
    let v: serde_json::Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    let obj = v.as_object().ok_or("index master is not an object")?;
    let mut out = Vec::new();
    let mut keys: Vec<&String> = obj.keys().collect();
    keys.sort_by_key(|k| GROUPS.iter().position(|g| g == k).unwrap_or(GROUPS.len()));
    for grp in keys {
        for name in obj[grp].as_array().map(Vec::as_slice).unwrap_or_default() {
            if let Some(n) = name.as_str() {
                out.push((grp.clone(), n.to_owned()));
            }
        }
    }
    Ok(out)
}

/// Kite's NSE dump narrowed to the INDICES segment.
///
/// # Errors
/// The provider's, as text.
pub async fn kite_index_instruments(kite: &KiteProvider) -> Result<Vec<KiteIndex>, String> {
    let rows = kite.list_instruments().await.map_err(|e| e.to_string())?;
    Ok(rows
        .into_iter()
        .filter(|r| r.instrument_type == InstrumentType::Index)
        .filter_map(|r| {
            r.kite_token.map(|token| KiteIndex {
                symbol: r.symbol.clone(),
                name: r.name.clone(),
                token,
            })
        })
        .collect())
}

/// `instrument` rows of type INDEX (Kite's dump as the nightly last stored it).
async fn stored_index_instruments(conn: &mut PgConnection) -> Result<Vec<KiteIndex>, String> {
    let rows: Vec<(String, String, Option<i64>)> = sqlx::query_as(
        "SELECT symbol, name, kite_token FROM instrument \
         WHERE instrument_type = 'INDEX' AND kite_token IS NOT NULL",
    )
    .fetch_all(&mut *conn)
    .await
    .map_err(|e| e.to_string())?;
    Ok(rows
        .into_iter()
        .filter_map(|(symbol, name, t)| t.map(|token| KiteIndex { symbol, name, token }))
        .collect())
}

/// Resolve each master name to a Kite instrument: alias first, then normalised name/symbol.
#[must_use]
pub fn join<'a>(name: &str, kite: &'a [KiteIndex]) -> Option<&'a KiteIndex> {
    if let Some((_, sym)) = ALIASES.iter().find(|(n, _)| *n == name) {
        return kite.iter().find(|k| k.symbol == *sym);
    }
    let key = norm(name);
    kite.iter()
        .find(|k| norm(&k.name) == key)
        .or_else(|| kite.iter().find(|k| norm(&k.symbol) == key))
}

/// Upsert every master name with its group, order and Kite join. Without Kite rows, the
/// `instrument` table's INDEX rows stand in. A persisted CSV slug is kept. Returns rows written.
///
/// # Errors
/// The master list's or the database's, as text.
pub async fn refresh_master(conn: &mut PgConnection, kite: &[KiteIndex]) -> Result<i64, String> {
    let list = master_list()?;
    let stored;
    let kite = if kite.is_empty() {
        stored = stored_index_instruments(conn).await?;
        stored.as_slice()
    } else {
        kite
    };
    let mut n = 0_i64;
    for (order, (grp, name)) in list.iter().enumerate() {
        let hit = join(name, kite);
        sqlx::query(
            "INSERT INTO index_master (name, grp, kite_token, kite_tradingsymbol, nse_csv_slug, \
             live, sort_order) VALUES ($1, $2, $3, $4, $5, $6, $7) \
             ON CONFLICT (name) DO UPDATE SET grp = excluded.grp, \
             kite_token = excluded.kite_token, kite_tradingsymbol = excluded.kite_tradingsymbol, \
             live = excluded.live, sort_order = excluded.sort_order, \
             nse_csv_slug = COALESCE(index_master.nse_csv_slug, excluded.nse_csv_slug)",
        )
        .bind(name)
        .bind(grp)
        .bind(hit.map(|k| k.token))
        .bind(hit.map(|k| k.symbol.clone()))
        .bind(csv_slug(name))
        .bind(hit.is_some())
        .bind(i32::try_from(order + 1).unwrap_or(i32::MAX))
        .execute(&mut *conn)
        .await
        .map_err(|e| e.to_string())?;
        n += 1;
    }
    Ok(n)
}

/// `index_def` slug overrides where the two lists name an index differently.
const DEF_SLUGS: [(&str, &str); 4] = [
    ("Nifty LargeMidcap 250", "nifty-large-mid-250"),
    ("Nifty MidSmallcap 400", "nifty-mid-small-400"),
    ("Nifty 50 Futures PR", "nifty-50-futures-index"),
    ("Nifty 50 Futures TR", "nifty-50-futures-tr-index"),
];

fn def_key(name: &str) -> String {
    let k = norm(name);
    k.strip_suffix("INDEX").map_or(k.clone(), str::to_owned)
}

/// `index_master.id -> index_def.id` by normalised name (P/E, P/B, yield and the NSE level
/// history live on `index_snapshot_daily`).
///
/// # Errors
/// The database's, as text.
pub async fn index_def_map(conn: &mut PgConnection) -> Result<HashMap<i16, i16>, String> {
    let defs: Vec<(i16, String, String)> =
        sqlx::query_as("SELECT id, slug, name FROM index_def")
            .fetch_all(&mut *conn)
            .await
            .map_err(|e| e.to_string())?;
    let masters: Vec<(i16, String)> = sqlx::query_as("SELECT id, name FROM index_master")
        .fetch_all(&mut *conn)
        .await
        .map_err(|e| e.to_string())?;
    let mut by_key: HashMap<String, i16> = HashMap::new();
    let mut by_slug: HashMap<String, i16> = HashMap::new();
    for (id, slug, name) in &defs {
        by_key.entry(def_key(name)).or_insert(*id);
        by_slug.insert(slug.clone(), *id);
    }
    let mut out = HashMap::new();
    for (mid, name) in masters {
        let hit = DEF_SLUGS
            .iter()
            .find(|(n, _)| *n == name)
            .and_then(|(_, s)| by_slug.get(*s))
            .or_else(|| by_key.get(&def_key(&name)));
        if let Some(d) = hit {
            out.insert(mid, *d);
        }
    }
    Ok(out)
}
