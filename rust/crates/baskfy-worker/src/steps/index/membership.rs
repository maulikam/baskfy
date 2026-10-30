//! Step 5 — `refresh_index_membership`. Ports `baskfy_worker.tasks.membership`.
//!
//! docs/03: "NSE constituent files -> index_member_daily, PIT". Each date gets its own rows and
//! earlier dates are never rewritten: resolving NIFTY 500 for 2019 must return the 2019
//! constituents, including the ones that have since been dropped (docs/06 §2, "the single most
//! common source of backtest look-ahead bias").
//!
//! Three ways a row gets here, recorded in `source` (docs/09 §Backfill):
//!
//! * `nse_file` — read from a published constituent file for that exact date. Certain.
//! * `derived` — resolved by rule (`nifty-allcap`: every EQ instrument with a bar on the date;
//!   `nse-sme-emerge`: the same, restricted to the Emerge series; `nifty-fno`: every underlying
//!   with a futures contract in Kite's dump). Certain, but not from a file.
//! * `reconstructed` — carried back from the earliest **later** observed file, because NSE
//!   publishes none for that date; marked so backtests can exclude uncertain periods. Only ever
//!   reached when the provider answers nothing for a past date.
//!
//! A row already recorded as `nse_file` is never downgraded by a later pass (the upsert's
//! `WHERE`).

use std::collections::HashMap;

use baskfy_db::OrderedJson;
use baskfy_providers::ProviderError;
use baskfy_providers::composite::CompositeProvider;
use baskfy_providers::errors::ErrorKind;
use chrono::NaiveDate;
use sqlx::PgConnection;

use super::universes::{SME_SERIES_SORTED, UNIVERSES, Universe};
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;

/// `SOURCE_NSE_FILE`.
pub(crate) const SOURCE_NSE_FILE: &str = "nse_file";
/// `SOURCE_RECONSTRUCTED`.
pub(crate) const SOURCE_RECONSTRUCTED: &str = "reconstructed";
/// `SOURCE_DERIVED`.
pub(crate) const SOURCE_DERIVED: &str = "derived";

/// `DERIVED_BY_RULE`: universes defined by rule rather than by a published file. `etf` left
/// this set on 9 Sep 2026 for [`PROVIDER_SOURCED`]; the two are disjoint.
pub(crate) const DERIVED_BY_RULE: [&str; 2] = ["nifty-allcap", "nse-sme-emerge"];

/// Which provider list answers a provider-sourced universe (`PROVIDER_SOURCED`'s method names).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum NamedMethod {
    /// `fno_underlyings` — Kite's instrument dump, the distinct NFO futures names.
    FnoUnderlyings,
    /// `etf_symbols` — NSE's `eq_etfseclist.csv`.
    EtfSymbols,
}

/// `PROVIDER_SOURCED`: slug → (provider method, the `source` the answer is stamped with).
/// `source` has a CHECK constraint of exactly three values; each label is the true one.
pub(crate) const PROVIDER_SOURCED: [(&str, NamedMethod, &str); 2] = [
    ("nifty-fno", NamedMethod::FnoUnderlyings, SOURCE_DERIVED),
    ("etf", NamedMethod::EtfSymbols, SOURCE_NSE_FILE),
];

/// What the provider half of membership needs: the composite in production, a stub in tests.
pub(crate) trait MembershipSource {
    /// `provider.index_constituents(slug, on)`.
    async fn index_constituents(
        &self,
        slug: &str,
        on: NaiveDate,
    ) -> Result<Vec<String>, ProviderError>;
    /// One of [`PROVIDER_SOURCED`]'s methods.
    async fn named(&self, method: NamedMethod) -> Result<Vec<String>, ProviderError>;
}

impl MembershipSource for CompositeProvider {
    async fn index_constituents(
        &self,
        slug: &str,
        on: NaiveDate,
    ) -> Result<Vec<String>, ProviderError> {
        CompositeProvider::index_constituents(self, slug, on).await
    }

    async fn named(&self, method: NamedMethod) -> Result<Vec<String>, ProviderError> {
        match method {
            NamedMethod::FnoUnderlyings => self.fno_underlyings().await,
            NamedMethod::EtfSymbols => self.etf_symbols().await,
        }
    }
}

/// `MembershipResult`. The maps keep Python's `dict` insertion order.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct MembershipResult {
    pub(crate) rows_written: i64,
    pub(crate) per_universe: Vec<(String, i64)>,
    pub(crate) per_source: Vec<(String, i64)>,
    pub(crate) failures: Vec<(String, String)>,
}

impl MembershipResult {
    fn add_source(&mut self, source: &str, n: i64) {
        if let Some(slot) = self.per_source.iter_mut().find(|(k, _)| k == source) {
            slot.1 += n;
        } else {
            self.per_source.push((source.to_owned(), n));
        }
    }

    /// The step's `note(date=..., per_universe=..., per_source=..., failures=... or None)`.
    pub(crate) fn note(&self, outcome: &mut StepOutcome, on: NaiveDate) {
        outcome.note("date", pyjson::s(on.format("%Y-%m-%d").to_string()));
        outcome.note(
            "per_universe",
            pyjson::object(
                self.per_universe
                    .iter()
                    .map(|(k, v)| (k.clone(), pyjson::int(*v))),
            ),
        );
        outcome.note(
            "per_source",
            pyjson::object(
                self.per_source
                    .iter()
                    .map(|(k, v)| (k.clone(), pyjson::int(*v))),
            ),
        );
        outcome.note(
            "failures",
            if self.failures.is_empty() {
                OrderedJson::Null
            } else {
                pyjson::object(self.failures.iter().map(|(k, v)| (k.clone(), pyjson::s(v))))
            },
        );
    }
}

/// `run_refresh_index_membership(session, provider, outcome, on)`: write point-in-time
/// membership for every selectable universe on `on`, and record it on the step.
pub(crate) async fn run_refresh_index_membership<P: MembershipSource>(
    conn: &mut PgConnection,
    provider: &P,
    outcome: &mut StepOutcome,
    on: NaiveDate,
) -> Result<i64, WorkerError> {
    let result = refresh_membership(conn, provider, on, true).await?;
    outcome.rows_in = i64::try_from(UNIVERSES.len()).unwrap_or(i64::MAX);
    outcome.rows_out = result.rows_written;
    result.note(outcome, on);
    Ok(result.rows_written)
}

/// `refresh_membership(session, provider, on, allow_reconstruction=...)`.
pub(crate) async fn refresh_membership<P: MembershipSource>(
    conn: &mut PgConnection,
    provider: &P,
    on: NaiveDate,
    allow_reconstruction: bool,
) -> Result<MembershipResult, WorkerError> {
    // `dict(select(Instrument.symbol, Instrument.id))`: `instrument.symbol` is unique per
    // exchange and the bed has one exchange; the last row wins, as in a dict.
    let ids: HashMap<String, i64> = sqlx::query_as::<_, (String, i64)>(
        "SELECT instrument.symbol, instrument.id FROM instrument",
    )
    .fetch_all(&mut *conn)
    .await?
    .into_iter()
    .collect();
    let mut result = MembershipResult::default();

    for universe in UNIVERSES {
        let (symbols, source) =
            match members_for(conn, provider, universe, on, allow_reconstruction).await {
                Ok(found) => found,
                // `except ProviderError as exc`: anything outside the provider tree is raised.
                Err(WorkerError::Provider(exc)) if exc.is_provider_error() => {
                    result
                        .failures
                        .push((universe.slug.to_owned(), exc.to_string()));
                    continue;
                }
                Err(other) => return Err(other),
            };

        let instrument_ids: Vec<i64> = symbols
            .iter()
            .filter_map(|symbol| ids.get(symbol).copied())
            .collect();
        let n = i64::try_from(instrument_ids.len()).unwrap_or(i64::MAX);
        result.per_universe.push((universe.slug.to_owned(), n));
        if instrument_ids.is_empty() {
            continue;
        }
        upsert_members(conn, universe.index_id, on, &instrument_ids, source).await?;
        result.rows_written += n;
        result.add_source(source, n);
    }
    Ok(result)
}

/// The one `INSERT ... ON CONFLICT DO UPDATE ... WHERE source != 'nse_file'` per universe.
///
/// Python sends a multi-row `VALUES`; this sends the same rows as arrays. A repeated symbol makes
/// both fail identically ("ON CONFLICT DO UPDATE command cannot affect row a second time").
async fn upsert_members(
    conn: &mut PgConnection,
    index_id: i16,
    on: NaiveDate,
    instrument_ids: &[i64],
    source: &str,
) -> Result<(), WorkerError> {
    let n = instrument_ids.len();
    sqlx::query(
        "INSERT INTO index_member_daily (index_id, date, instrument_id, source) \
         SELECT * FROM UNNEST($1::smallint[], $2::date[], $3::bigint[], $4::varchar[]) \
         ON CONFLICT (index_id, date, instrument_id) DO UPDATE SET source = excluded.source \
         WHERE index_member_daily.source != $5",
    )
    .bind(vec![index_id; n])
    .bind(vec![on; n])
    .bind(instrument_ids)
    .bind(vec![source.to_owned(); n])
    .bind(SOURCE_NSE_FILE)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// `_members_for`: one universe's members for `on`, and where the answer came from.
async fn members_for<P: MembershipSource>(
    conn: &mut PgConnection,
    provider: &P,
    universe: Universe,
    on: NaiveDate,
    allow_reconstruction: bool,
) -> Result<(Vec<String>, &'static str), WorkerError> {
    if let Some((_, method, source)) = PROVIDER_SOURCED
        .iter()
        .find(|(slug, _, _)| *slug == universe.slug)
    {
        return Ok((from_named_method(provider, *method).await?, source));
    }

    if DERIVED_BY_RULE.contains(&universe.slug) {
        return Ok((derived_members(conn, universe, on).await?, SOURCE_DERIVED));
    }

    let symbols = from_provider(provider, universe, on).await?;
    if !symbols.is_empty() {
        return Ok((symbols, SOURCE_NSE_FILE));
    }

    if !allow_reconstruction {
        return Ok((Vec::new(), SOURCE_NSE_FILE));
    }

    Ok((reconstruct(conn, universe, on).await?, SOURCE_RECONSTRUCTED))
}

/// `_from_named_method`: empty rather than raising on any provider error — a provider that
/// cannot serve one universe must not fail a run that has landed every other one. An exception
/// outside the provider tree still raises.
async fn from_named_method<P: MembershipSource>(
    provider: &P,
    method: NamedMethod,
) -> Result<Vec<String>, WorkerError> {
    match provider.named(method).await {
        Ok(symbols) => Ok(symbols),
        Err(e) if e.is_provider_error() => Ok(Vec::new()),
        Err(e) => Err(WorkerError::Provider(e)),
    }
}

/// `_from_provider`: `UnexpectedPayload` (the provider knows the slug but publishes no file for
/// it) is an empty answer, not a failure; every other error is raised to `refresh_membership`.
async fn from_provider<P: MembershipSource>(
    provider: &P,
    universe: Universe,
    on: NaiveDate,
) -> Result<Vec<String>, WorkerError> {
    match provider.index_constituents(universe.slug, on).await {
        Ok(symbols) => Ok(symbols),
        Err(e) if e.kind() == ErrorKind::UnexpectedPayload => Ok(Vec::new()),
        Err(e) => Err(WorkerError::Provider(e)),
    }
}

/// `_derived_members`: every EQ (or, for `etf`, ETF) instrument with a bar on `on`; for
/// `nse-sme-emerge`, restricted to the Emerge series. Requiring a bar keeps a suspended,
/// delisted or not-yet-listed name out of the universe on that date.
async fn derived_members(
    conn: &mut PgConnection,
    universe: Universe,
    on: NaiveDate,
) -> Result<Vec<String>, WorkerError> {
    let instrument_type = if universe.slug == "etf" { "ETF" } else { "EQ" };
    let base = "SELECT DISTINCT instrument.symbol FROM instrument \
                JOIN ohlcv_daily ON ohlcv_daily.instrument_id = instrument.id \
                WHERE instrument.instrument_type = $1 AND ohlcv_daily.date = $2";
    let rows: Vec<(String,)> = if universe.slug == "nse-sme-emerge" {
        sqlx::query_as(&format!(
            "{base} AND instrument.series = ANY($3::varchar[])"
        ))
        .bind(instrument_type)
        .bind(on)
        .bind(SME_SERIES_SORTED.map(str::to_owned).to_vec())
        .fetch_all(&mut *conn)
        .await?
    } else {
        sqlx::query_as(base)
            .bind(instrument_type)
            .bind(on)
            .fetch_all(&mut *conn)
            .await?
    };
    Ok(rows.into_iter().map(|(s,)| s).collect())
}

/// `_reconstruct`: carry membership back from the earliest **later** `nse_file` date. Never from
/// another reconstruction, never from an earlier date.
async fn reconstruct(
    conn: &mut PgConnection,
    universe: Universe,
    on: NaiveDate,
) -> Result<Vec<String>, WorkerError> {
    let earliest: Option<(NaiveDate,)> = sqlx::query_as(
        "SELECT index_member_daily.date FROM index_member_daily \
         WHERE index_member_daily.index_id = $1 AND index_member_daily.source = $2 \
         AND index_member_daily.date > $3 ORDER BY index_member_daily.date LIMIT 1",
    )
    .bind(universe.index_id)
    .bind(SOURCE_NSE_FILE)
    .bind(on)
    .fetch_optional(&mut *conn)
    .await?;
    let Some((earliest,)) = earliest else {
        return Ok(Vec::new());
    };
    let rows: Vec<(String,)> = sqlx::query_as(
        "SELECT instrument.symbol FROM instrument \
         JOIN index_member_daily ON index_member_daily.instrument_id = instrument.id \
         WHERE index_member_daily.index_id = $1 AND index_member_daily.date = $2",
    )
    .bind(universe.index_id)
    .bind(earliest)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|(s,)| s).collect())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn etf_and_derived_by_rule_are_disjoint() {
        // test_reference_data.TestMembershipSource.test_etf_and_derived_by_rule_are_disjoint.
        let sourced: Vec<&str> = PROVIDER_SOURCED.iter().map(|(s, _, _)| *s).collect();
        assert!(sourced.contains(&"etf"));
        assert!(!DERIVED_BY_RULE.contains(&"etf"));
        assert!(sourced.iter().all(|s| !DERIVED_BY_RULE.contains(s)));
        // Every branch's slug is a real universe.
        for slug in sourced.iter().chain(DERIVED_BY_RULE.iter()) {
            assert!(UNIVERSES.iter().any(|u| u.slug == *slug), "{slug}");
        }
    }

    #[test]
    fn the_labels_are_the_three_the_check_constraint_allows() {
        // The `source` CHECK constraint holds exactly these; nifty-fno is `derived`, etf `nse_file`.
        assert_eq!(PROVIDER_SOURCED[0].2, "derived");
        assert_eq!(PROVIDER_SOURCED[1].2, "nse_file");
        for label in [SOURCE_NSE_FILE, SOURCE_RECONSTRUCTED, SOURCE_DERIVED] {
            assert!(["nse_file", "reconstructed", "derived"].contains(&label));
        }
    }

    #[test]
    fn the_note_is_pythons_dict() {
        let result = MembershipResult {
            rows_written: 3,
            per_universe: vec![("nifty-50".into(), 2), ("nifty-allcap".into(), 1)],
            per_source: vec![("nse_file".into(), 2), ("derived".into(), 1)],
            failures: vec![],
        };
        let mut outcome = StepOutcome::default();
        result.note(&mut outcome, NaiveDate::from_ymd_opt(2026, 10, 29).unwrap());
        assert_eq!(
            pyjson::dumps(&outcome.detail_json()).unwrap(),
            r#"{"date": "2026-10-29", "per_universe": {"nifty-50": 2, "nifty-allcap": 1}, "per_source": {"nse_file": 2, "derived": 1}, "failures": null}"#
        );
        let failed = MembershipResult {
            failures: vec![("nifty-50".into(), "[composite] down".into())],
            ..MembershipResult::default()
        };
        let mut outcome = StepOutcome::default();
        failed.note(&mut outcome, NaiveDate::from_ymd_opt(2026, 10, 29).unwrap());
        assert_eq!(
            pyjson::dumps(&outcome.detail_json()).unwrap(),
            r#"{"date": "2026-10-29", "per_universe": {}, "per_source": {}, "failures": {"nifty-50": "[composite] down"}}"#
        );
    }

    #[test]
    fn per_source_accumulates_in_first_seen_order() {
        let mut r = MembershipResult::default();
        r.add_source("nse_file", 2);
        r.add_source("derived", 5);
        r.add_source("nse_file", 3);
        assert_eq!(
            r.per_source,
            vec![("nse_file".to_owned(), 5), ("derived".to_owned(), 5)]
        );
    }
}
