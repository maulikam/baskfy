//! `baskfy_worker.seeds.fno_config`: the FO book row and the three sleeve rows for one user.

use baskfy_core::fno::config::{FnoConfig, f1_seed_capital_inr};
use baskfy_db::OrderedJson;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use crate::error::WorkerError;

pub(crate) const SEEDED_BY: &str = "seed";

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SleeveSeed {
    pub(crate) sleeve: &'static str,
    pub(crate) capital_inr: Decimal,
    pub(crate) risk_per_trade_pct: Decimal,
    pub(crate) max_lots: i64,
    pub(crate) max_open_positions: i64,
    pub(crate) source: &'static str,
}

fn count(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `SLEEVE_SEEDS`.
pub(crate) fn sleeve_seeds() -> Vec<SleeveSeed> {
    let config = FnoConfig::default();
    let mut f1_capital = f1_seed_capital_inr();
    f1_capital.rescale(2);
    vec![
        SleeveSeed {
            sleeve: "F1",
            capital_inr: f1_capital,
            risk_per_trade_pct: config.f1.risk_per_trade_pct,
            max_lots: config.common.max_lots,
            max_open_positions: config.f1.max_open_per_underlying
                * count(config.f1.underlyings.len()),
            source: "Maulik, 25 Sep 2026 (DECISIONS-FO M.2): F1 capital Rs 25 lakh, both \
                     underlyings together",
        },
        SleeveSeed {
            sleeve: "F2",
            capital_inr: Decimal::new(0, 2),
            risk_per_trade_pct: config.f2.risk_per_trade_pct,
            max_lots: config.common.max_lots,
            max_open_positions: config.f2.max_open,
            source: "docs/fno/01 §1b: Q1 was asked for F1 only; F2 paper one lot, live refuses \
                     NO_SLEEVE_CAPITAL",
        },
        SleeveSeed {
            sleeve: "F3",
            capital_inr: Decimal::new(0, 2),
            risk_per_trade_pct: config.f3.risk_per_trade_pct,
            max_lots: config.common.max_lots,
            max_open_positions: config.f3.max_open_per_underlying
                * count(config.f3.underlyings.len()),
            source: "docs/fno/DECISIONS-FO M.5 (28 Sep 2026): F3 capital Rs 0 until QUESTIONS \
                     Q10 is answered; paper one lot, live refuses NO_SLEEVE_CAPITAL",
        },
    ]
}

/// `seed_fno(session, user_id)`: rows written per table; `ON CONFLICT DO NOTHING` everywhere.
pub(crate) async fn seed_fno(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<(String, OrderedJson)>, WorkerError> {
    let book = sqlx::query(
        "INSERT INTO fo_book_config (user_id, updated_by) VALUES ($1, $2) \
         ON CONFLICT (user_id) DO NOTHING",
    )
    .bind(user_id)
    .bind(SEEDED_BY)
    .execute(&mut *conn)
    .await?
    .rows_affected();
    let seeds = sleeve_seeds();
    let mut inserted: Vec<String> = Vec::new();
    for seed in &seeds {
        let row: Option<(String,)> = sqlx::query_as(
            "INSERT INTO fo_sleeve_config (user_id, sleeve, capital_inr, risk_per_trade_pct, \
             max_lots, max_open_positions, updated_by) VALUES ($1, $2, round($3::numeric, 2), $4, $5, $6, $7) \
             ON CONFLICT (user_id, sleeve) DO NOTHING RETURNING sleeve",
        )
        .bind(user_id)
        .bind(seed.sleeve)
        .bind(seed.capital_inr)
        .bind(seed.risk_per_trade_pct)
        .bind(i16::try_from(seed.max_lots).unwrap_or(i16::MAX))
        .bind(i16::try_from(seed.max_open_positions).unwrap_or(i16::MAX))
        .bind(SEEDED_BY)
        .fetch_optional(&mut *conn)
        .await?;
        if let Some((sleeve,)) = row {
            inserted.push(sleeve);
        }
    }
    inserted.sort();
    for sleeve in &inserted {
        let Some(seed) = seeds.iter().find(|s| s.sleeve == sleeve) else {
            continue;
        };
        sqlx::query(
            "INSERT INTO fo_config_audit (user_id, scope, key, old_value, new_value, changed_by, \
             note) VALUES ($1, $2, 'capital_inr', NULL, $3, $4, $5)",
        )
        .bind(user_id)
        .bind(sleeve)
        .bind(seed.capital_inr.to_string())
        .bind(SEEDED_BY)
        .bind(seed.source)
        .execute(&mut *conn)
        .await?;
    }
    let n = count(inserted.len());
    Ok(vec![
        (
            "fo_book_config".to_owned(),
            OrderedJson::Int(i64::try_from(book).unwrap_or(i64::MAX)),
        ),
        ("fo_sleeve_config".to_owned(), OrderedJson::Int(n)),
        ("fo_config_audit".to_owned(), OrderedJson::Int(n)),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_seed_carries_mauliks_capital_and_the_documents_defaults() {
        let seeds = sleeve_seeds();
        let names: Vec<&str> = seeds.iter().map(|s| s.sleeve).collect();
        assert_eq!(names, ["F1", "F2", "F3"]);
        assert_eq!(seeds[0].capital_inr.to_string(), "2500000.00");
        assert_eq!(seeds[1].capital_inr.to_string(), "0.00");
        assert_eq!(seeds[2].capital_inr.to_string(), "0.00");
        for seed in &seeds {
            assert_eq!(seed.risk_per_trade_pct, Decimal::new(10, 1));
            assert_eq!(seed.max_lots, 2);
        }
        assert_eq!(
            (seeds[0].max_open_positions, seeds[1].max_open_positions),
            (2, 5)
        );
        assert_eq!(
            seeds[0].capital_inr * seeds[0].risk_per_trade_pct / Decimal::from(100),
            Decimal::from(25_000)
        );
        assert!(seeds[0].source.contains("M.2"));
    }
}
