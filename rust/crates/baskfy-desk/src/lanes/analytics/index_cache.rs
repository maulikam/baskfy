//! `app/analytics/index_cache.py`: the index OHLC repository for the regime overlay — its reads,
//! the injected session clock, and the strict instrument resolver.
//!
//! Safety properties kept: instrument resolution **fails** on zero or several matches (never the
//! first row); a session that has not closed is never returned to the signal path
//! (`final_only`); the clock is injected.
//!
//! **Not ported (loud):** the Kite fetch side (`load_instruments`, `fetch_daily`, `update_index`,
//! `upsert`), reached only from `regime_run.py`'s collection, which stays on Python with the
//! engine (DECISIONS-RS, lane analytics_regime, label R-ar.1).

use chrono::{NaiveDate, NaiveDateTime, NaiveTime};

use crate::storage::{Conn, DbValue, Param, StorageError};
use crate::wire::PyValue;

use super::regime::RegimeConfig;

/// `core.regime.Candle`.
#[derive(Debug, Clone, PartialEq)]
pub struct Candle {
    /// `date`.
    pub date: NaiveDate,
    /// `open`.
    pub open: Option<f64>,
    /// `high`.
    pub high: Option<f64>,
    /// `low`.
    pub low: Option<f64>,
    /// `close` (`None` only where the stored value is NULL).
    pub close: Option<f64>,
    /// `is_final`.
    pub is_final: bool,
}

/// `MARKET_CLOSE`.
#[must_use]
pub fn market_close() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or_default()
}

/// `Clock.session_is_closed(session)` for an IST wall time `now`.
#[must_use]
pub fn session_is_closed(now: NaiveDateTime, session: NaiveDate) -> bool {
    let today = now.date();
    if session < today {
        return true;
    }
    if session > today {
        return false;
    }
    now.time() >= market_close()
}

/// `InstrumentNotFoundError` / `AmbiguousInstrumentError`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ResolveError {
    /// No instrument matched.
    #[error("{0}")]
    NotFound(String),
    /// More than one matched; refusing to choose.
    #[error("{0}")]
    Ambiguous(String),
}

fn field(row: &crate::wire::PyDict, key: &str) -> String {
    match row.get(key) {
        Some(PyValue::Str(s)) => s.clone(),
        Some(PyValue::Int(i)) => i.to_string(),
        _ => String::new(),
    }
}

/// `resolve_index_instrument(instruments, index_name, cfg)`: exactly one instrument or an
/// error naming what was accepted.
///
/// # Errors
/// [`ResolveError`] on zero or several matches.
pub fn resolve_index_instrument<'a>(
    instruments: &'a [crate::wire::PyDict],
    index_name: &str,
    cfg: &RegimeConfig,
) -> Result<&'a crate::wire::PyDict, ResolveError> {
    let mut accepted = vec![index_name.trim().to_uppercase()];
    if let Some((_, aliases)) = cfg.index_aliases.iter().find(|(k, _)| k == index_name) {
        accepted.extend(aliases.iter().map(|a| a.trim().to_uppercase()));
    }
    let hits: Vec<&crate::wire::PyDict> = instruments
        .iter()
        .filter(|r| {
            let exchange = match r.get("exchange") {
                Some(PyValue::Str(s)) => s.to_uppercase(),
                _ => cfg.exchange.to_uppercase(),
            };
            field(r, "segment").to_uppercase() == cfg.segment.to_uppercase()
                && exchange == cfg.exchange.to_uppercase()
                && accepted.contains(&field(r, "tradingsymbol").trim().to_uppercase())
        })
        .collect();
    let mut sorted_accepted = accepted.clone();
    sorted_accepted.sort();
    sorted_accepted.dedup();
    let repr_list = |v: &[String]| {
        format!(
            "[{}]",
            v.iter()
                .map(|s| format!("'{s}'"))
                .collect::<Vec<_>>()
                .join(", ")
        )
    };
    match hits.len() {
        0 => {
            let first = index_name
                .split_whitespace()
                .next()
                .unwrap_or("")
                .to_uppercase();
            let mut near: Vec<String> = instruments
                .iter()
                .map(|r| field(r, "tradingsymbol"))
                .filter(|s| s.to_uppercase().contains(&first))
                .collect();
            near.sort();
            near.dedup();
            near.truncate(10);
            let near_text = if near.is_empty() {
                "none".to_owned()
            } else {
                repr_list(&near)
            };
            Err(ResolveError::NotFound(format!(
                "index '{index_name}' not found on {}/{}. Accepted symbols/aliases: {}. Nearby: {near_text}",
                cfg.exchange,
                cfg.segment,
                repr_list(&sorted_accepted)
            )))
        }
        1 => Ok(hits[0]),
        n => Err(ResolveError::Ambiguous(format!(
            "index '{index_name}' matched {n} instruments: [{}]. Narrow the configured symbol/aliases — refusing to choose.",
            hits.iter()
                .map(|h| format!(
                    "('{}', {})",
                    field(h, "tradingsymbol"),
                    field(h, "instrument_token")
                ))
                .collect::<Vec<_>>()
                .join(", ")
        ))),
    }
}

/// `IndexRepository.load(index_name, start=, end=, final_only=)`.
///
/// # Errors
/// The driver's.
pub async fn load(
    conn: &mut Conn,
    index_name: &str,
    start: Option<NaiveDate>,
    end: Option<NaiveDate>,
    final_only: bool,
) -> Result<Vec<Candle>, StorageError> {
    let mut sql =
        "SELECT date, open, high, low, close, is_final FROM index_series WHERE index_name=?"
            .to_owned();
    let mut args: Vec<Param> = vec![index_name.into()];
    if final_only {
        sql.push_str(" AND is_final=1");
    }
    if let Some(s) = start {
        sql.push_str(" AND date >= ?");
        args.push(s.format("%Y-%m-%d").to_string().into());
    }
    if let Some(e) = end {
        sql.push_str(" AND date <= ?");
        args.push(e.format("%Y-%m-%d").to_string().into());
    }
    sql.push_str(" ORDER BY date");
    let cur = conn.execute(&sql, &args).await?;
    let mut out = Vec::with_capacity(cur.rows.len());
    for r in &cur.rows {
        let date = r
            .get("date")
            .and_then(DbValue::as_str)
            .and_then(super::ops::parse_iso_date)
            .ok_or_else(|| {
                StorageError::Other("ValueError: Invalid isoformat string".to_owned())
            })?;
        out.push(Candle {
            date,
            open: r.get("open").and_then(DbValue::as_f64),
            high: r.get("high").and_then(DbValue::as_f64),
            low: r.get("low").and_then(DbValue::as_f64),
            close: r.get("close").and_then(DbValue::as_f64),
            is_final: super::regime_store::db_truthy(r.get("is_final")),
        });
    }
    Ok(out)
}

/// `IndexRepository.last_date(index_name, final_only=)`.
///
/// # Errors
/// The driver's.
pub async fn last_date(
    conn: &mut Conn,
    index_name: &str,
    final_only: bool,
) -> Result<Option<NaiveDate>, StorageError> {
    let mut sql = "SELECT MAX(date) d FROM index_series WHERE index_name=?".to_owned();
    if final_only {
        sql.push_str(" AND is_final=1");
    }
    let cur = conn.execute(&sql, &[index_name.into()]).await?;
    // `dt.date.fromisoformat(row["d"]) if row and row["d"] else None`: a stored value that is
    // not an ISO date raises, as in Python; only an absent or empty one is `None`.
    match cur
        .fetchone()
        .and_then(|r| r.get("d"))
        .and_then(DbValue::as_str)
    {
        None | Some("") => Ok(None),
        Some(s) => super::ops::parse_iso_date(s).map(Some).ok_or_else(|| {
            StorageError::Other(format!("ValueError: Invalid isoformat string: '{s}'"))
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::wire::{PyDict, dict};

    fn inst(sym: &str, token: i64) -> PyDict {
        dict([
            ("tradingsymbol", PyValue::Str(sym.to_owned())),
            ("segment", PyValue::Str("INDICES".to_owned())),
            ("exchange", PyValue::Str("NSE".to_owned())),
            ("instrument_token", PyValue::Int(token)),
        ])
    }

    #[test]
    fn resolves_exactly_one_through_an_alias() {
        let cfg = RegimeConfig::default();
        let rows = vec![
            inst("NIFTY 50", 1),
            inst("NIFTY SMALLCAP 250", 2),
            inst("NIFTY BANK", 3),
        ];
        let hit = resolve_index_instrument(&rows, "NIFTY SMLCAP 250", &cfg);
        assert_eq!(
            hit.ok().and_then(|h| h.get("instrument_token").cloned()),
            Some(PyValue::Int(2))
        );
    }

    #[test]
    fn refuses_to_guess() {
        let cfg = RegimeConfig::default();
        let bank = [inst("NIFTY BANK", 3)];
        let none = resolve_index_instrument(&bank, "NIFTY 50", &cfg);
        assert!(matches!(none, Err(ResolveError::NotFound(_))));
        let two = vec![inst("NIFTY 50", 1), inst("nifty 50 ", 9)];
        assert!(matches!(
            resolve_index_instrument(&two, "NIFTY 50", &cfg),
            Err(ResolveError::Ambiguous(_))
        ));
    }

    #[test]
    fn a_session_is_final_only_after_the_close() {
        let d = |s: &str| NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap_or_default();
        let at = |s: &str| NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M").unwrap_or_default();
        assert!(session_is_closed(at("2026-10-30 09:00"), d("2026-10-29")));
        assert!(!session_is_closed(at("2026-10-30 15:29"), d("2026-10-30")));
        assert!(session_is_closed(at("2026-10-30 15:30"), d("2026-10-30")));
        assert!(!session_is_closed(at("2026-10-30 18:00"), d("2026-10-31")));
    }

    #[tokio::test]
    async fn load_returns_only_final_sessions_in_date_order() {
        let Ok(dir) = tempfile::tempdir() else { return };
        let Ok(mut conn) = crate::storage::Db::connect_sqlite_path(&dir.path().join("t.db")).await
        else {
            return;
        };
        assert!(conn.migrate().await.is_ok());
        for (date, close, fin) in [
            ("2026-10-29", 101.5, 1),
            ("2026-10-27", 100.0, 1),
            ("2026-10-30", 102.0, 0),
        ] {
            let ok = conn
                .execute(
                    "INSERT INTO index_series(index_name, date, open, high, low, close, instrument_token, is_final, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    &["NIFTY 50".into(), date.into(), Param::from(None::<f64>), Param::from(None::<f64>), Param::from(None::<f64>), close.into(), Param::from(None::<i64>), Param::from(fin), "x".into()],
                )
                .await;
            assert!(ok.is_ok());
        }
        let got = load(&mut conn, "NIFTY 50", None, None, true)
            .await
            .unwrap_or_default();
        assert_eq!(
            got.iter().map(|c| c.close).collect::<Vec<_>>(),
            vec![Some(100.0), Some(101.5)]
        );
        let all = load(&mut conn, "NIFTY 50", None, None, false)
            .await
            .unwrap_or_default();
        assert_eq!(all.len(), 3);
        assert_eq!(
            last_date(&mut conn, "NIFTY 50", true).await.ok().flatten(),
            NaiveDate::from_ymd_opt(2026, 10, 29)
        );
    }
}
